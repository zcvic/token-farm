"""端到端验证：密钥版本隔离在真实 HTTP 链路上生效。

不走网关的上游转发（那需要真实上游），只验证**鉴权层**的判定——这是隔离的
执行点。用 FastAPI TestClient 打真实的路由与依赖注入，避免「测试里对、线上
不对」（例如参数名写错、依赖没接上）。

覆盖：
  * 国际版密钥调国内版模型 → 403，且不消耗上游额度；
  * 国内版密钥调国际版模型 → 403；
  * 各自调各自的模型 → 放行（上游不可达时是 502，证明已过鉴权）；
  * 不限定版本的密钥 → 两版都放行。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from server import config, db, keysvc, platforms, security  # noqa: E402


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _FakeUpstream:
    """假上游：`get` 一律返回给定清单，不碰真实网络。"""

    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **kw):
        return _Resp(self._payload)


def _fake_http(payload):
    return mock.patch.object(config, 'http_client', lambda *a, **k: _FakeUpstream(payload))


class GatewayRealmIsolationE2ETest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        self._orig_users = config.USERS_FILE
        config.DB_PATH = Path(self._tmp.name) / 'k.db'
        config.USERS_FILE = Path(self._tmp.name) / 'users.json'
        db._conn = None
        db.connect()
        security.save_users({
            'secret': 'S',
            'users': [{'username': 'admin', 'role': 'admin', 'pwd_hash': security.make_hash('p')}],
            'api_keys': [],
        })

        from server.main import app
        self.client = TestClient(app)

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db
        config.USERS_FILE = self._orig_users
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _token(self, **kw) -> str:
        return keysvc.create_key('t', **kw)['key']

    def _chat(self, token: str, model: str):
        """发一次对话请求。

        上游不可达 → 502，说明**已经过了鉴权**；被隔离拦下是 403。
        这两种状态码的区别就是本测试的断言点。
        """
        with mock.patch.object(config, 'WB2API_BASE', 'http://127.0.0.1:1'):
            return self.client.post(
                '/v1/chat/completions',
                headers={'Authorization': f'Bearer {token}'},
                json={'model': model, 'messages': [{'role': 'user', 'content': 'hi'}]},
            )

    def test_global_key_cannot_call_cn_model(self) -> None:
        """版本不匹配用 **400**：一批客户端（DeepSeek Harness 等）把 401/403
        一律显示成「API 密钥无效」，用 403 会让「密钥版本配错了」看起来像
        「密钥坏了」——issue #18 里用户因此反复重建密钥。断言点有两个：
        仍被拦住（不放行到上游）**且**状态码不会被客户端折叠成认证错误。
        """
        r = self._chat(self._token(realm='global'), 'glm-5.2')
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn('国际版', r.json()['error']['message'])

    def test_cn_key_cannot_call_global_model(self) -> None:
        r = self._chat(self._token(realm='cn'), 'global:gpt-5.6-sol')
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn('国内版', r.json()['error']['message'])

    def test_scoped_key_passes_auth_for_own_realm(self) -> None:
        """放行与否看状态码：502 = 过了鉴权但上游连不上（正是我们想确认的）。

        必须断言**确切**的 502，不能只断言「不是 403」——版本不匹配改 400 之后，
        「不是 403」把 400 也放过了，测试就白写了。
        """
        r = self._chat(self._token(realm='global'), 'global:gpt-5.6-sol')
        self.assertEqual(r.status_code, 502, f'同版本模型该过鉴权（502=上游不可达）：{r.text}')

    def test_unscoped_key_passes_both_realms(self) -> None:
        token = self._token()
        for model in ('glm-5.2', 'global:gpt-5.6-sol'):
            r = self._chat(token, model)
            self.assertEqual(r.status_code, 502, f'{model} 该过鉴权：{r.text}')

    def test_missing_model_rejected_for_scoped_key(self) -> None:
        """不带 model 走的就是上游默认模型（国内版）——隔离密钥不能借此绕过。"""
        r = self._chat(self._token(realm='global'), '')
        # 空 model 先被网关拦成 400；这里确认没被放行到上游
        self.assertEqual(r.status_code, 400, r.text)

    def test_models_endpoint_scoped_by_key(self) -> None:
        """/v1/models 按密钥版本裁剪（用假上游响应，不依赖真实上游）。

        平台模型（`trae/…`）不参与本版本的 realm 裁剪（见 `_merge_platform_models`），
        它们会按本机缓存/内置目录并进列表。这个用例只验证上游清单的版本裁剪，
        所以把平台目录置空，避免断言被环境里的 Trae 模型数量带偏。
        """
        payload = {
            'object': 'list',
            'data': [{'id': 'cn:glm-5.2'}, {'id': 'glm-5.1'}, {'id': 'global:gpt-5.6-sol'}],
        }

        with _fake_http(payload), \
                mock.patch.object(platforms, 'all_adapters', return_value=[]):
            gl = self.client.get('/v1/models', headers={
                'Authorization': f"Bearer {self._token(realm='global')}"})
            cn = self.client.get('/v1/models', headers={
                'Authorization': f"Bearer {self._token(realm='cn')}"})

        self.assertEqual([m['id'] for m in gl.json()['data']], ['global:gpt-5.6-sol'])
        self.assertEqual([m['id'] for m in cn.json()['data']], ['cn:glm-5.2', 'glm-5.1'])

    def test_platform_models_not_scoped_by_realm(self) -> None:
        """平台模型（`trae/…`）不受版本裁剪。

        `cn` / `global` 描述的是腾讯账号池，平台模型不走那条路。调用侧对这类
        名字本来就跳过版本校验（见 `keysvc.validate` 的 `platform_model`），
        列表若反过来把它们滤掉，限定版本的密钥会看不到自己明明能调的模型。
        """
        payload = {'object': 'list', 'data': [
            {'id': 'cn:glm-5.2'}, {'id': 'global:gpt-5.6-sol'}]}
        trae = platforms.get('trae')

        async def _fake_list_models(*a, **k):
            return [{'id': 'trae/glm-5.2', 'object': 'model', 'owned_by': 'trae'}]

        with _fake_http(payload), \
                mock.patch.object(trae, 'list_models', _fake_list_models):
            ids = {}
            for realm in ('cn', 'global'):
                r = self.client.get('/v1/models', headers={
                    'Authorization': f"Bearer {self._token(realm=realm)}"})
                ids[realm] = [m['id'] for m in r.json()['data']]

        self.assertIn('trae/glm-5.2', ids['cn'], ids['cn'])
        self.assertIn('trae/glm-5.2', ids['global'], ids['global'])


if __name__ == '__main__':
    unittest.main()
