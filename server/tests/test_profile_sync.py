"""手动刷新时同步账号昵称（issue #94 同步）。

钉住四层语义：
  1. 资料接口只认 nickname/uid：响应里的手机号等字段不得进入任何返回值；
  2. 信封与顶层平铺两种响应形态都接；空昵称按失败返回（参考仓 d78131ba）；
  3. uid 不一致必须报错（防串号），业务/HTTP 错误原样抛给调用方静默跳过；
  4. 昵称同步只在手动刷新（force=true）触发，页面加载/后台定时不碰资料接口；
  5. 回写只改 `account.nickname`，其余凭证字段原样保留；未变化不写盘。
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config  # noqa: E402
from server.routers import accounts  # noqa: E402
from server.services import profilesync, tencent, wb2api  # noqa: E402


def _client_factory(payload, *, status=200):
    """造一个 httpx 风格的最小客户端，并记录请求。"""
    seen: dict = {}

    class _Resp:
        status_code = status

        def json(self):
            return payload

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            seen['url'] = url
            seen['headers'] = dict(headers or {})
            return _Resp()

    return (lambda *a, **kw: _Client()), seen


class FetchAccountProfileTest(unittest.TestCase):
    def _fetch(self, payload, *, status=200, auth=None):
        factory, seen = _client_factory(payload, status=status)
        auth = auth if auth is not None else {
            'access_token': 'at', 'uid': 'u1', 'realm': 'cn'}
        with mock.patch.object(config, 'http_client', factory):
            got = asyncio.run(tencent.fetch_account_profile(auth))
        return got, seen

    def test_returns_nickname_and_never_surfaces_phone(self) -> None:
        got, seen = self._fetch({
            'code': 0, 'msg': 'OK',
            'data': {'uid': 'u1', 'nickname': '新名字', 'phoneNumber': '13800000000'},
        })
        self.assertEqual(got, '新名字')
        self.assertNotIn('13800000000', str(got))
        self.assertTrue(seen['url'].endswith('/console/account'), seen['url'])
        self.assertTrue(seen['url'].startswith('https://www.workbuddy.cn'), seen['url'])
        h = seen['headers']
        self.assertEqual(h.get('Authorization'), 'Bearer at')
        self.assertEqual(h.get('x-client-platform'), 'web')
        self.assertEqual(h.get('Origin'), 'https://www.workbuddy.cn')

    def test_global_realm_uses_global_web_base(self) -> None:
        with mock.patch.object(tencent, 'realm_of', return_value='global'):
            _, seen = self._fetch(
                {'code': 0, 'data': {'uid': 'u1', 'nickname': 'n'}},
                auth={'access_token': 'at', 'uid': 'u1'})
        self.assertTrue(seen['url'].startswith('https://www.workbuddy.ai'), seen['url'])
        self.assertEqual(seen['headers'].get('Origin'), 'https://www.workbuddy.ai')

    def test_flat_top_level_shape_is_accepted(self) -> None:
        # 参考仓 d78131ba：实测出现过没有 code/data 的顶层平铺响应。
        got, _ = self._fetch(
            {'uid': 'u1', 'nickname': '平铺', 'phoneNumber': '13800000000'})
        self.assertEqual(got, '平铺')

    def test_empty_nickname_is_failure(self) -> None:
        # 空昵称若当成功返回，调用方既不算更新也不算失败，整批会静默什么都不做。
        for payload in (
            {'code': 0, 'data': {'uid': 'u1'}},
            {'code': 0, 'data': {'uid': 'u1', 'nickname': '   '}},
            {'code': 0, 'data': {'uid': 'u1', 'nickname': None}},
            {'uid': 'u1'},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(RuntimeError):
                    self._fetch(payload)

    def test_uid_mismatch_is_rejected(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            self._fetch({'code': 0, 'data': {'uid': 'someone-else', 'nickname': 'x'}})
        self.assertIn('uid', str(ctx.exception))

    def test_business_error_raises_without_body(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            self._fetch({'code': 1002, 'msg': 'unauthorized',
                         'data': {'phoneNumber': '13800000000'}}, status=401)
        msg = str(ctx.exception)
        self.assertIn('401', msg)
        self.assertNotIn('13800000000', msg, '错误信息不得夹带敏感字段')

    def test_missing_token_rejected(self) -> None:
        with self.assertRaises(RuntimeError):
            asyncio.run(tencent.fetch_account_profile({'uid': 'u1'}))


class SetAccountNicknameTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.file = 'workbuddy-u1.json'
        self.raw = {
            'account': {'uid': 'u1', 'nickname': '旧名字', 'enterpriseId': 'e1'},
            'auth': {'accessToken': 'at', 'refreshToken': 'rt', 'expiresAt': 1},
            'device_token': 'dt',
            'proxy': 'p',
        }
        (self.dir / self.file).write_text(
            json.dumps(self.raw, ensure_ascii=False), encoding='utf-8')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_rewrites_nickname_only(self) -> None:
        self.assertTrue(wb2api.set_account_nickname(self.file, '新名字', self.dir))
        got = json.loads((self.dir / self.file).read_text(encoding='utf-8'))
        self.assertEqual(got['account']['nickname'], '新名字')
        # 其余字段逐字保留（token / 设备指纹 / 线路 / 企业 id）
        self.assertEqual(got['auth'], self.raw['auth'])
        self.assertEqual(got['device_token'], 'dt')
        self.assertEqual(got['proxy'], 'p')
        self.assertEqual(got['account']['enterpriseId'], 'e1')

    def test_unchanged_nickname_does_not_write(self) -> None:
        before = (self.dir / self.file).read_bytes()
        self.assertFalse(wb2api.set_account_nickname(self.file, '旧名字', self.dir))
        self.assertEqual((self.dir / self.file).read_bytes(), before)


class SyncNicknamesTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _account(self, uid: str, nickname: str) -> dict:
        name = f'workbuddy-{uid}.json'
        (self.dir / name).write_text(json.dumps({
            'account': {'uid': uid, 'nickname': nickname},
            'auth': {'accessToken': 'at-' + uid},
        }, ensure_ascii=False), encoding='utf-8')
        return {'uid': uid, 'file': name, 'nickname': nickname}

    def test_updates_changed_and_returns_uid(self) -> None:
        acc = self._account('u1', '旧名字')

        async def fake(_auth):
            return '新名字'

        with mock.patch.object(tencent, 'fetch_account_profile', fake):
            got = asyncio.run(profilesync.sync_nicknames([acc], self.dir))
        self.assertEqual(got, ['u1'])
        raw = json.loads((self.dir / acc['file']).read_text(encoding='utf-8'))
        self.assertEqual(raw['account']['nickname'], '新名字')

    def test_failure_is_silent_and_does_not_block_others(self) -> None:
        a1 = self._account('u1', '一号')
        a2 = self._account('u2', '二号')

        async def fake(auth):
            if auth['uid'] == 'u1':
                raise RuntimeError('boom')
            return '二号新'

        with mock.patch.object(tencent, 'fetch_account_profile', fake):
            got = asyncio.run(profilesync.sync_nicknames([a1, a2], self.dir))
        self.assertEqual(got, ['u2'])
        raw1 = json.loads((self.dir / a1['file']).read_text(encoding='utf-8'))
        self.assertEqual(raw1['account']['nickname'], '一号', '失败账号不该被改')

    def test_unchanged_nickname_not_reported(self) -> None:
        acc = self._account('u1', '同名')

        async def fake(_auth):
            return '同名'

        with mock.patch.object(tencent, 'fetch_account_profile', fake):
            self.assertEqual(
                asyncio.run(profilesync.sync_nicknames([acc], self.dir)), [])

    def test_concurrency_capped(self) -> None:
        accs = [self._account(f'u{i}', f'n{i}') for i in range(6)]
        active = {'now': 0, 'max': 0}

        async def fake(_auth):
            active['now'] += 1
            active['max'] = max(active['max'], active['now'])
            await asyncio.sleep(0.01)
            active['now'] -= 1
            return 'x'

        with mock.patch.object(tencent, 'fetch_account_profile', fake), \
                mock.patch.object(config, 'PROFILE_CONCURRENCY', 3):
            asyncio.run(profilesync.sync_nicknames(accs, self.dir))
        self.assertLessEqual(active['max'], 3, f'并发 {active["max"]} 超过上限')

    def test_empty_list_is_noop(self) -> None:
        self.assertEqual(asyncio.run(profilesync.sync_nicknames([], self.dir)), [])


class RefreshEndpointTriggerTest(unittest.TestCase):
    """只有手动刷新（force=true）才同步昵称。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.acc = {'uid': 'u1', 'file': 'workbuddy-u1.json', 'nickname': 'n'}

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, *, force: bool):
        calls: list = []

        async def fake_get(*a, **kw):
            return True, 1, 'ok', False, None, []

        async def fake_sync(accounts, auth_dir):
            calls.append((accounts, auth_dir))
            return []

        with mock.patch.object(accounts, '_group', return_value={}), \
                mock.patch.object(accounts, '_require_dir', return_value=self.dir), \
                mock.patch.object(wb2api, 'list_auth_accounts', return_value=[self.acc]), \
                mock.patch.object(accounts.wb2api, 'read_account_file',
                                  return_value={'account': {'uid': 'u1'}, 'auth': {}}), \
                mock.patch.object(accounts.creditsvc, 'get_credits', fake_get), \
                mock.patch.object(profilesync, 'sync_nicknames', fake_sync):
            asyncio.run(accounts.refresh_all_credits(
                force=force, upstream_id=None, user={'role': 'admin'}))
        return calls

    def test_manual_refresh_syncs(self) -> None:
        calls = self._run(force=True)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], [self.acc])

    def test_page_load_does_not_sync(self) -> None:
        """页面加载（force=false）不该额外触碰资料接口。"""
        self.assertEqual(self._run(force=False), [])


if __name__ == '__main__':
    unittest.main()
