"""WorkBuddy 作为「模型组合」成员（v2）的回归测试。

锁定的性质：

  · 伪适配器**只给组合用**：不出现在平台下拉，也不参与 `<平台>/<模型>` 前缀
    解析（否则会凭空多出一个与直连语义重复、且绕开请求上下文的入口）；
  · 它出现在组合编辑页的模型目录里，并标注「池级权重」；
  · 组合调用成员时把请求上下文（key / ip / 路径）透传下去，成员按**这把密钥**
    绑定的上游转发，body 里的模型名换成成员的真名；
  · 记账只做一次（`_record` 一次），`mapped_model` 是成员的裸名 → 日志的
    `platform` 列为空串、`realm` 由 `global:` 前缀判定 —— 与直连 WorkBuddy 一致；
  · 原始字节流**逐字节**透传，usage / 首字延迟在收尾时回填并记账；
  · 版本校验在**调用成员之前**：限定国内版的密钥配了国际版成员时直接 400，
    不会白发一次上游请求。
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

from server import config, db, platforms, upstreamsvc  # noqa: E402
from server.platforms.base import ChatContext  # noqa: E402
from server.routers import gateway  # noqa: E402
from server.routers import platforms as platforms_router  # noqa: E402
from server.services import modelcatalog, wbforward  # noqa: E402


def _combo_member_platform() -> object:
    return platforms.get('workbuddy')


class RegistrationTest(unittest.TestCase):
    """伪适配器的三条注册属性，以及它在两处清单里的可见性。"""

    def test_adapter_flags(self) -> None:
        adapter = _combo_member_platform()
        self.assertIsNotNone(adapter, 'workbuddy 适配器必须注册（组合成员靠它）')
        self.assertFalse(adapter.ui_visible, '不该出现在平台下拉 / 账号页')
        self.assertTrue(adapter.combo_member, '必须能被组合编辑页选中')
        self.assertFalse(adapter.routable, '不能参与 <平台>/<模型> 前缀解析')

    def test_prefix_does_not_route(self) -> None:
        """`workbuddy/glm-5.2` 不该被解析成平台调用。"""
        self.assertIsNone(platforms.route_model('workbuddy/glm-5.2'))
        self.assertIsNone(platforms.route_model('workbuddy/global:gpt-5.6-sol'))

    def test_record_name_is_bare(self) -> None:
        """记账名必须是裸模型名：加前缀会把 realm / platform 两列都判错。"""
        adapter = _combo_member_platform()
        self.assertEqual(adapter.record_name('glm-5.2'), 'glm-5.2')
        self.assertEqual(adapter.record_name('global:gpt-5.6-sol'), 'global:gpt-5.6-sol')

    def test_not_in_platform_list(self) -> None:
        names = [p['name'] for p in platforms_router.list_platforms(user={})['platforms']]
        self.assertNotIn('workbuddy', names)
        self.assertIn('trae', names)

    def test_catalog_includes_workbuddy_with_pool_weight(self) -> None:
        adapter = _combo_member_platform()

        async def fake_models(*, refresh: bool = False):
            return [{'id': 'workbuddy/glm-5.2', 'model_name': 'glm-5.2',
                     'name': 'GLM', 'platform': 'workbuddy'}]

        with mock.patch.object(adapter, 'list_models', fake_models):
            catalog = asyncio.run(
                platforms_router.model_catalog(user={}))
        entry = [p for p in catalog['platforms'] if p['name'] == 'workbuddy'][0]
        self.assertTrue(entry['pool_weight'], 'WorkBuddy 的权重是池级的')
        self.assertEqual([m['model_name'] for m in entry['models']], ['glm-5.2'])

    def test_platform_list_has_no_pool_weight(self) -> None:
        """可见平台（Trae）的权重是成员级，不该被标成池级。"""
        adapter = platforms.get('trae')

        async def fake_models(*, refresh: bool = False):
            return []

        with mock.patch.object(adapter, 'list_models', fake_models):
            catalog = asyncio.run(platforms_router.model_catalog(user={}))
        trae = [p for p in catalog['platforms'] if p['name'] == 'trae'][0]
        self.assertFalse(trae['pool_weight'])


class ModelCatalogTest(unittest.TestCase):
    """成员模型清单：国际版补 `global:` 前缀（否则上游按国内版查到 11102）。"""

    def test_global_models_are_prefixed(self) -> None:
        async def fake_catalog(realm: str, force: bool = False) -> dict:
            if realm == 'cn':
                return {'models': [{'id': 'glm-5.2', 'name': 'GLM'}]}
            return {'models': [{'id': 'gpt-5.6-sol', 'name': 'GPT'}]}

        adapter = _combo_member_platform()
        with mock.patch.object(modelcatalog, 'catalog', fake_catalog):
            models = asyncio.run(adapter.list_models())
        by_realm = {m['realm']: m for m in models}
        self.assertEqual(by_realm['cn']['model_name'], 'glm-5.2')
        self.assertEqual(by_realm['global']['model_name'], 'global:gpt-5.6-sol')
        # 列表 id 带平台前缀只是为了唯一，不能被当成可调用名（routable=False）。
        self.assertEqual(by_realm['global']['id'], 'workbuddy/global:gpt-5.6-sol')

    def test_one_realm_failure_does_not_hide_the_other(self) -> None:
        async def fake_catalog(realm: str, force: bool = False) -> dict:
            if realm == 'global':
                raise RuntimeError('boom')
            return {'models': [{'id': 'glm-5.2'}]}

        adapter = _combo_member_platform()
        with mock.patch.object(modelcatalog, 'catalog', fake_catalog):
            models = asyncio.run(adapter.list_models())
        self.assertEqual([m['model_name'] for m in models], ['glm-5.2'])


class MergeModelsTest(unittest.TestCase):
    def test_workbuddy_models_are_not_merged(self) -> None:
        """`/v1/models` 不该出现调不通的 `workbuddy/...` 名字。"""
        adapter = _combo_member_platform()

        async def fake_models(*, refresh: bool = False):
            return [{'id': 'workbuddy/glm-5.2', 'model_name': 'glm-5.2'}]

        payload = {'object': 'list', 'data': [{'id': 'glm-5.2'}]}
        with mock.patch.object(adapter, 'list_models', fake_models):
            merged = asyncio.run(gateway._merge_platform_models(payload, None))
        self.assertNotIn('workbuddy/glm-5.2', [m['id'] for m in merged['data']])


class _DbCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'combo-wb.db'
        db._conn = None
        db.connect()

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass


def _key(realm: str = '') -> dict:
    return {'id': 1, 'realm': realm, 'models': [], 'upstream_id': None,
            'ip_whitelist': ''}


def _install_combo(platform: str, model: str) -> None:
    db.set_setting('model_combos', [{
        'name': 'fast',
        'enabled': True,
        'members': [{'platform': platform, 'model': model, 'weight': 1,
                     'enabled': True}],
    }])


class ComboWorkBuddyChatTest(_DbCase):
    """组合 → WorkBuddy 成员：上下文透传、记账一次、原始字节透传。"""

    def _ctx(self, key: dict) -> ChatContext:
        return ChatContext(key=key, ip='10.0.0.7', ua='pytest',
                           upstream_path='/v1/chat/completions')

    def test_non_stream_records_once_with_member_name(self) -> None:
        _install_combo('workbuddy', 'glm-5.2')
        key = _key()
        seen: dict = {}

        async def fake_forward(*, upstream, upstream_path, body, stream,
                               started=None):
            seen['upstream'] = upstream
            seen['path'] = upstream_path
            seen['body'] = dict(body)
            return wbforward.ForwardOutcome(
                status=200,
                payload={'id': 'x', 'choices': [],
                         'usage': {'prompt_tokens': 7, 'completion_tokens': 9}},
                usage={'prompt_tokens': 7, 'completion_tokens': 9})

        upstream = {'name': 'g1', 'base_url': 'http://up', 'api_key': 'k'}
        with mock.patch.object(upstreamsvc, 'resolve_for_key', return_value=upstream) \
                as resolve, \
                mock.patch.object(wbforward, 'forward', fake_forward), \
                mock.patch.object(gateway, '_record') as record:
            resp = asyncio.run(gateway._platform_chat(
                adapter=platforms.get('combo'), bare_model='fast',
                body={'model': 'combo/fast', 'messages': []}, key=key,
                ip='10.0.0.7', requested_model='combo/fast', ua='pytest',
                stream=False, effort=None, ctx=self._ctx(key)))

        self.assertEqual(resp.status_code, 200)
        resolve.assert_called_once_with(key)
        self.assertIs(seen['upstream'], upstream, '必须用这把密钥绑定的上游')
        self.assertEqual(seen['path'], '/v1/chat/completions')
        self.assertEqual(seen['body']['model'], 'glm-5.2', 'body 里要是成员真名')
        record.assert_called_once()
        args = record.call_args.args
        self.assertEqual(args[2], 'combo/fast', '请求名照原样记')
        self.assertEqual(args[3], 'glm-5.2', 'mapped_model 是成员裸名')
        self.assertEqual(args[4], 200)
        # 裸名 → 没有平台前缀 → 日志 platform 列为空（WorkBuddy 的归属）
        self.assertIsNone(platforms.route_model(args[3]))
        self.assertEqual(record.call_args.kwargs['usage'],
                         {'prompt_tokens': 7, 'completion_tokens': 9})

    def test_global_member_records_global_realm(self) -> None:
        _install_combo('workbuddy', 'global:gpt-5.6-sol')
        key = _key('global')

        async def fake_forward(**kwargs):
            return wbforward.ForwardOutcome(status=200, payload={'choices': []})

        with mock.patch.object(upstreamsvc, 'resolve_for_key', return_value={'base_url': 'http://up'}), \
                mock.patch.object(wbforward, 'forward', fake_forward), \
                mock.patch.object(gateway, '_record') as record:
            asyncio.run(gateway._platform_chat(
                adapter=platforms.get('combo'), bare_model='fast',
                body={'model': 'combo/fast', 'messages': []}, key=key,
                ip='10.0.0.7', requested_model='combo/fast', ua=None,
                stream=False, effort=None, ctx=self._ctx(key)))

        mapped = record.call_args.args[3]
        self.assertEqual(mapped, 'global:gpt-5.6-sol')
        # `_record` 用这个前缀判 realm；这里直接锁住判据本身
        self.assertTrue(mapped.lower().startswith('global:'))

    def test_cn_key_cannot_use_global_member(self) -> None:
        """版本隔离的补丁：拒绝发生在**调用成员之前**，不白花一次上游请求。"""
        _install_combo('workbuddy', 'global:gpt-5.6-sol')
        key = _key('cn')

        async def fake_forward(**kwargs):  # pragma: no cover —— 不该被调用
            raise AssertionError('版本不匹配的成员不该真的发出去')

        with mock.patch.object(upstreamsvc, 'resolve_for_key', return_value={'base_url': 'http://up'}), \
                mock.patch.object(wbforward, 'forward', fake_forward), \
                mock.patch.object(gateway, '_record') as record:
            resp = asyncio.run(gateway._platform_chat(
                adapter=platforms.get('combo'), bare_model='fast',
                body={'model': 'combo/fast', 'messages': []}, key=key,
                ip='10.0.0.7', requested_model='combo/fast', ua=None,
                stream=False, effort=None, ctx=self._ctx(key)))

        self.assertEqual(resp.status_code, 400)
        body = json.loads(resp.body)
        self.assertEqual(body['error']['code'], 'realm_mismatch')
        record.assert_called_once()
        self.assertEqual(record.call_args.args[4], 400)

    def test_raw_stream_is_passed_through_and_recorded_once(self) -> None:
        _install_combo('workbuddy', 'glm-5.2')
        key = _key()
        chunks = [b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
                  b'data: [DONE]\n\n']
        usage: dict = {}

        def make_forward():
            async def fake_forward(**kwargs):
                outcome = wbforward.ForwardOutcome(
                    status=200, content_type='text/event-stream', usage=usage)

                async def gen():
                    for chunk in chunks:
                        usage.update({'prompt_tokens': 2, 'completion_tokens': 3})
                        outcome.first_token_ms = 42
                        yield chunk

                outcome.stream = gen()
                return outcome
            return fake_forward

        async def run():
            resp = await gateway._platform_chat(
                adapter=platforms.get('combo'), bare_model='fast',
                body={'model': 'combo/fast', 'messages': [], 'stream': True},
                key=key, ip='10.0.0.7', requested_model='combo/fast', ua=None,
                stream=True, effort=None, ctx=self._ctx(key))
            got = [chunk async for chunk in resp.body_iterator]
            return resp, got

        with mock.patch.object(upstreamsvc, 'resolve_for_key', return_value={'base_url': 'http://up'}), \
                mock.patch.object(wbforward, 'forward', make_forward()), \
                mock.patch.object(gateway, '_record') as record:
            resp, got = asyncio.run(run())

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(got, chunks, 'WorkBuddy 的 SSE 必须逐字节透传')
        record.assert_called_once()
        self.assertEqual(record.call_args.args[3], 'glm-5.2')
        self.assertEqual(record.call_args.kwargs['first_token'], 42,
                         '首字延迟由适配器在流结束时回填')
        self.assertEqual(record.call_args.kwargs['usage'],
                         {'prompt_tokens': 2, 'completion_tokens': 3})


class SaveCombosTest(_DbCase):
    """保存路径：WorkBuddy 是合法成员，未知平台仍被拒绝。"""

    def test_workbuddy_member_is_accepted(self) -> None:
        from server.routers import settings as settings_router

        body = {'combos': [{
            'name': 'fast',
            'members': [{'platform': 'workbuddy', 'model': 'glm-5.2',
                         'weight': 1, 'enabled': True}],
        }]}
        out = settings_router.save_combos(body, user={'username': 'admin'})
        self.assertEqual(out['combos'][0]['members'][0]['platform'], 'workbuddy')

    def test_unknown_platform_is_still_rejected(self) -> None:
        from fastapi import HTTPException

        from server.routers import settings as settings_router

        body = {'combos': [{
            'name': 'fast',
            'members': [{'platform': 'nope', 'model': 'x', 'weight': 1}],
        }]}
        with self.assertRaises(HTTPException) as ctx:
            settings_router.save_combos(body, user={'username': 'admin'})
        self.assertEqual(ctx.exception.status_code, 400)


if __name__ == '__main__':
    unittest.main()
