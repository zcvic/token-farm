"""Kilo（kilo.ai）平台：账号存储、目录、余额与适配器策略。

全部离线：资料 / 余额 / 对话 / 设备码都替换成假客户端。这里钉住三件容易回归
的事：

  · token 是**一年期、无 refresh**——`refresh_account` 只是探活 + 读余额，
    不能把它当成续期（401 只标失效，不尝试换 token）；
  · 余额接口回的是**美元**，对外 `unit='usd'`，不能混进积分口径；
  · 目录**免鉴权**可读：一个账号都没有时模型页也要有兜底清单。
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import httpx

from server import config, db
from server.platforms.base import PlatformError
from server.platforms.kilo import catalog, client, constants as C, login, store
from server.platforms.kilo.adapter import KiloAdapter, TOKEN_LIFETIME_SECONDS

_TOKEN = 'kilo-token-abcdefghijklmnop'


class KiloStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(config, 'DATA_DIR', Path(self.tmp.name))
        patch.start()
        self.addCleanup(patch.stop)

    def test_derive_uid_is_stable(self) -> None:
        first = store.derive_uid(_TOKEN)
        self.assertTrue(first.startswith('kl_'))
        self.assertEqual(first, store.derive_uid(f'  Bearer {_TOKEN}  '))
        self.assertNotEqual(first, store.derive_uid('other-token'))

    def test_derive_uid_requires_token(self) -> None:
        with self.assertRaises(ValueError):
            store.derive_uid('   ')

    def test_public_account_hides_token(self) -> None:
        saved = store.save_account({
            'uid': 'kl_1', 'token': _TOKEN, 'email': 'a@b.c',
            'balance': {'usd': 1.5, 'at': 100, 'message': ''},
        })
        public = store.public_account(saved)
        dumped = json.dumps(public, ensure_ascii=False)
        self.assertNotIn(_TOKEN, dumped)
        self.assertTrue(public['has_token'])
        self.assertFalse(public['has_refresh_token'])
        self.assertEqual(public['realm'], 'cn')
        self.assertEqual(public['balance']['usd'], 1.5)
        self.assertTrue(public['token_preview'].endswith(_TOKEN[-6:]))

    def test_safe_id_rejects_traversal(self) -> None:
        for bad in ('../x', 'a/b', 'a\\b', '', 'x' * 200):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    store.safe_id(bad)

    def test_weight_and_cooling_mutations(self) -> None:
        store.save_account({'uid': 'kl_1', 'token': _TOKEN})
        store.set_weight('kl_1', 2.5)
        self.assertEqual(store.read_account('kl_1')['weight'], 2.5)
        store.set_cooling('kl_1', time.time() + 60, 'test')
        self.assertTrue(store.is_cooling(store.read_account('kl_1')))
        store.clear_cooling('kl_1')
        self.assertFalse(store.is_cooling(store.read_account('kl_1')))

    def test_checkin_only_and_disabled_are_exclusive(self) -> None:
        store.save_account({'uid': 'kl_1', 'token': _TOKEN})
        only = store.set_checkin_only('kl_1', True)
        self.assertTrue(only['checkin_only'])
        self.assertFalse(only['disabled'])
        frozen = store.set_disabled('kl_1', True, '停用')
        self.assertTrue(frozen['disabled'])
        self.assertFalse(frozen['checkin_only'])


class KiloCatalogTest(unittest.TestCase):
    def test_parse_models_reads_openrouter_schema(self) -> None:
        models = client.parse_models({'data': [
            {
                'id': 'kilo-auto/free', 'name': 'Auto Free',
                'context_length': 256000,
                'top_provider': {'max_completion_tokens': 8192},
                'architecture': {'input_modalities': ['text', 'image']},
                'pricing': {'prompt': '0', 'completion': '0'},
                'supported_parameters': ['tools', 'reasoning'],
                'isFree': True,
            },
            {'id': 'paid/model', 'pricing': {'prompt': '0.000001',
                                             'completion': '0.000002'}},
            {'id': 'KILO-AUTO/FREE'},
            'not-a-dict',
        ]})
        self.assertEqual([m['id'] for m in models], ['kilo-auto/free', 'paid/model'])
        first = models[0]
        self.assertEqual(first['context_length'], 256000)
        self.assertEqual(first['max_tokens'], 8192)
        self.assertEqual(first['modalities'], ['text', 'image'])
        self.assertTrue(first['reasoning'])
        self.assertTrue(first['tool_call'])
        self.assertTrue(first['free'])
        self.assertFalse(models[1]['free'])

    def test_parse_models_rejects_wrong_shape(self) -> None:
        self.assertEqual(client.parse_models({}), [])
        self.assertEqual(client.parse_models({'data': 'x'}), [])

    def test_default_catalog_has_free_auto(self) -> None:
        models = catalog.default_models()
        ids = {m['id'] for m in models}
        self.assertIn('kilo-auto/free', ids)
        self.assertTrue(all(m['type'] == 'chat' for m in models))

    def test_normalize_keeps_order_and_dedupes(self) -> None:
        models = catalog.normalize([
            {'id': 'a', 'modalities': ['text']},
            {'id': 'A'},
            {'id': ''},
        ])
        self.assertEqual([m['id'] for m in models], ['a'])

    # ── 网关地址可覆盖（上游 d7496501）─────────────────────
    def test_gateway_base_honours_env_override(self) -> None:
        self.assertEqual(
            C.resolve_gateway_base({'KILO_AI_GATEWAY_URL': 'https://gw.example/v1/'}),
            'https://gw.example/v1')
        # legacy 变量：走 API 的网关改写路径。
        self.assertEqual(
            C.resolve_gateway_base({'KILO_API_URL': 'https://api.example/'}),
            'https://api.example/api/gateway/v1')
        # 两个都给时 KILO_AI_GATEWAY_URL 优先。
        self.assertEqual(
            C.resolve_gateway_base({
                'KILO_AI_GATEWAY_URL': 'https://gw.example/v1',
                'KILO_API_URL': 'https://api.example'}),
            'https://gw.example/v1')
        self.assertEqual(C.resolve_gateway_base({}), C.DEFAULT_GATEWAY_BASE)

    # ── 推理档位（上游 cf41438e）───────────────────────────
    def test_reasoning_efforts_maps_toggle_and_effort_options(self) -> None:
        self.assertEqual(catalog.reasoning_efforts([]), ['high'],
                         '空数组 = 推理常开，只有 thinking 一档')
        self.assertEqual(
            catalog.reasoning_efforts([{'type': 'toggle'}]), ['none', 'high'])
        self.assertEqual(
            catalog.reasoning_efforts([
                {'type': 'effort', 'values': ['low', 'high', 'high', 'max']}]),
            ['low', 'high', 'max'], '重复档位去重')
        self.assertEqual(
            catalog.reasoning_efforts([
                {'type': 'toggle'},
                {'type': 'effort', 'values': ['medium']}]),
            ['none', 'medium'], 'toggle 补一档 none')
        self.assertEqual(
            catalog.reasoning_efforts([
                {'type': 'toggle'},
                {'type': 'effort', 'values': ['none', 'high']}]),
            ['none', 'high'], '已有 none 不重复补')

    def test_reasoning_efforts_drops_unknown_entries_one_by_one(self) -> None:
        self.assertEqual(
            catalog.reasoning_efforts([
                {'type': 'budget_tokens', 'values': [1024]},
                {'type': 'effort', 'values': ['high', 'bogus', 3]}]),
            ['high'], '认不出的条目与取值逐条丢弃')
        self.assertEqual(catalog.reasoning_efforts('not-a-list'), [])

    def test_parse_models_reads_reasoning_options(self) -> None:
        models = client.parse_models({'data': [
            {'id': 'm1', 'opencode': {
                'reasoning_options': [{'type': 'effort',
                                       'values': ['low', 'high']}]}},
            {'id': 'm2', 'opencode': {'variants': {'thinking': {}}}},
        ]})
        self.assertEqual(models[0]['efforts'], ['low', 'high'])
        self.assertEqual(models[1]['efforts'], [],
                         'legacy variants 不当档位用')

    def test_normalize_derives_efforts_from_raw_options(self) -> None:
        models = catalog.normalize([{
            'id': 'm', 'opencode': {'reasoning_options': [{'type': 'toggle'}]}}])
        self.assertEqual(models[0]['efforts'], ['none', 'high'])
        self.assertTrue(models[0]['reasoning'], '有档位就算支持推理')


class KiloClientTest(unittest.TestCase):
    """错误映射与资料 / 余额解析。"""

    @staticmethod
    def _error(status: int, payload: object, headers: dict | None = None):
        response = httpx.Response(status, json=payload, headers=headers or {})
        return client._response_error(response, json.dumps(payload), payload)

    def test_auth_errors_map_to_401(self) -> None:
        for status in (401, 403):
            with self.subTest(status=status):
                exc = self._error(status, {'error': {'message': 'nope'}})
                self.assertEqual(exc.status, 401)
                self.assertEqual(exc.code, 'login_expired')

    def test_payment_and_rate_limit_are_distinct(self) -> None:
        pay = self._error(402, {'error': {'message': 'no balance'}})
        self.assertEqual((pay.status, pay.code), (402, 'insufficient_balance'))
        rate = self._error(429, {'error': {'message': 'slow'}},
                           {'retry-after': '9'})
        self.assertEqual((rate.status, rate.retry_after), (429, 9))

    def test_upstream_5xx_becomes_502(self) -> None:
        exc = self._error(503, {'error': {'message': 'down'}})
        self.assertEqual((exc.status, exc.code), (502, 'upstream_error'))

    def test_parse_profile_handles_user_wrapper_and_org(self) -> None:
        out = client.parse_profile({
            'user': {'email': 'x@y.z', 'name': 'X'},
            'organizations': [{'id': 'org-1'}],
            'selectedOrganizationId': 'org-1',
            'hasPersonalAccount': False,
        })
        self.assertEqual(out['email'], 'x@y.z')
        self.assertEqual(out['organizationId'], 'org-1')
        # 选中的组织不在列表里 → 不认，避免把请求打到无权访问的组织上。
        other = client.parse_profile({
            'user': {'email': 'x@y.z'},
            'organizations': [{'id': 'org-1'}],
            'selectedOrganizationId': 'org-2',
        })
        self.assertEqual(other['organizationId'], '')

    def test_parse_balance_accepts_numbers_and_rejects_junk(self) -> None:
        self.assertEqual(client.parse_balance({'balance': 12.5}), 12.5)
        self.assertEqual(client.parse_balance({'balance': '3'}), 3.0)
        self.assertIsNone(client.parse_balance({'balance': True}))
        self.assertIsNone(client.parse_balance({'balance': 'x'}))
        self.assertIsNone(client.parse_balance(None))

    def test_chat_body_strips_only_openai_specific_fields(self) -> None:
        body = client.chat_body({'model': 'kilo-auto/free', 'store': True,
                                 'service_tier': 'flex', 'stream_options':
                                 {'include_usage': True},
                                 'metadata': {'a': 1}}, 'kilo-auto/free')
        self.assertNotIn('store', body)
        self.assertNotIn('service_tier', body)
        # Kilo 网关认这两项，不能被一起剥掉。
        self.assertIn('stream_options', body)
        self.assertEqual(body['metadata'], {'a': 1})

    def test_chat_body_defaults_model(self) -> None:
        self.assertEqual(client.chat_body({})['model'], C.DEFAULT_MODEL)


class KiloAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._orig_db = config.DB_PATH
        config.DB_PATH = Path(self.tmp.name) / 'a.db'
        db._conn = None
        db.connect()
        patch = mock.patch.object(config, 'DATA_DIR', Path(self.tmp.name))
        patch.start()
        self.addCleanup(patch.stop)
        self.adapter = KiloAdapter()

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db

    def _seed(self, **overrides) -> dict:
        uid = str(overrides.pop('uid', 'kl_1'))
        data = {'uid': uid, 'token': overrides.pop('token', _TOKEN)}
        data.update(overrides)
        store.save_account(data)
        return data

    # ── 导入 ──────────────────────────────────────────────
    def test_import_account_derives_uid_and_defaults_one_year(self) -> None:
        before = time.time()
        public = self.adapter.import_account({'token': f'Bearer {_TOKEN}'})
        self.assertTrue(public['uid'].startswith('kl_'))
        self.assertTrue(public['has_token'])
        raw = store.read_account(public['uid'])
        self.assertEqual(raw['token'], _TOKEN)
        expires = store.epoch_from_iso(raw['expiredAt'])
        self.assertAlmostEqual(expires - before, TOKEN_LIFETIME_SECONDS,
                               delta=120)

    def test_import_account_accepts_nested_and_alt_keys(self) -> None:
        public = self.adapter.import_account(
            {'account': {'apiKey': _TOKEN, 'nickname': 'n'}})
        self.assertTrue(public['has_token'])
        self.assertEqual(public['nickname'], 'n')

    def test_import_requires_token(self) -> None:
        with self.assertRaises(PlatformError):
            self.adapter.import_account({'nickname': 'x'})

    # ── 探活 / 余额 ───────────────────────────────────────
    def test_refresh_account_reads_profile_and_balance(self) -> None:
        self._seed()

        async def fake_profile(token, **kwargs):
            return {'email': 'a@b.c', 'name': 'A', 'organizationId': 'org-1'}

        async def fake_balance(token, **kwargs):
            return 7.25

        with mock.patch.object(client, 'fetch_profile', new=fake_profile), \
                mock.patch.object(client, 'fetch_balance', new=fake_balance):
            public = asyncio.run(self.adapter.refresh_account('kl_1'))
        self.assertEqual(public['email'], 'a@b.c')
        self.assertEqual(public['balance']['usd'], 7.25)
        self.assertEqual(public['organization_id'], 'org-1')

    def test_refresh_account_marks_expired_on_401(self) -> None:
        self._seed()

        async def fake_profile(token, **kwargs):
            raise PlatformError('token 失效', status=401, code='login_expired')

        with mock.patch.object(client, 'fetch_profile', new=fake_profile):
            with self.assertRaises(PlatformError):
                asyncio.run(self.adapter.refresh_account('kl_1'))
        self.assertEqual(store.read_account('kl_1')['loginState'], '登录已失效')

    def test_credits_reports_usd_and_caches(self) -> None:
        self._seed()
        calls: list[str] = []

        async def fake_balance(token, **kwargs):
            calls.append(token)
            return 12.5

        with mock.patch.object(client, 'fetch_balance', new=fake_balance):
            out = asyncio.run(self.adapter.credits('kl_1'))
            again = asyncio.run(self.adapter.credits('kl_1'))
        self.assertEqual(out['unit'], 'usd')
        self.assertEqual(out['credits'], 12.5)
        self.assertEqual(again['credits'], 12.5)
        self.assertEqual(len(calls), 1, '第二次应命中 60 秒缓存')

    def test_credits_raises_when_balance_unavailable(self) -> None:
        self._seed()

        async def fake_balance(token, **kwargs):
            return None

        with mock.patch.object(client, 'fetch_balance', new=fake_balance):
            with self.assertRaises(PlatformError) as ctx:
                asyncio.run(self.adapter.credits('kl_1'))
        self.assertEqual(ctx.exception.code, 'credits_unavailable')

    def test_usage_collects_errors_without_raising(self) -> None:
        self._seed()

        async def fake_balance(token, **kwargs):
            raise PlatformError('登录态已失效', status=401,
                                code='login_expired')

        with mock.patch.object(client, 'fetch_balance', new=fake_balance):
            out = asyncio.run(self.adapter.usage('kl_1'))
        self.assertIsNone(out['session_usage'])
        self.assertEqual(out['errors'][0]['code'], 'login_expired')

    # ── 模型目录 ──────────────────────────────────────────
    def test_list_models_adds_prefix_and_falls_back(self) -> None:
        models = asyncio.run(self.adapter.list_models())
        ids = {m['id'] for m in models}
        self.assertIn('kilo/kilo-auto/free', ids)
        self.assertTrue(all(m['realm'] == 'cn' for m in models))
        self.assertIn('kilo/kilo-auto/free', self.adapter.cached_model_ids())

    def test_live_catalog_is_used_without_any_account(self) -> None:
        async def fake_models(**kwargs):
            return [{'id': 'live/model', 'name': 'Live'}]

        with mock.patch.object(client, 'fetch_models', new=fake_models):
            models = asyncio.run(self.adapter.list_models(refresh=True))
        self.assertEqual([m['model_name'] for m in models], ['live/model'])

    # ── 选号 ──────────────────────────────────────────────
    def test_candidates_skip_disabled_cooling_and_tokenless(self) -> None:
        self._seed(uid='kl_1')
        self._seed(uid='kl_2', disabled=True)
        self._seed(uid='kl_3', coolingUntil=time.time() + 600)
        self._seed(uid='kl_4', checkinOnly=True)
        store.save_account({'uid': 'kl_5', 'token': ''})
        ids = [a['uid'] for a in self.adapter._candidates()]
        self.assertEqual(ids, ['kl_1'])

    def test_weights_rotate_first_choice(self) -> None:
        self._seed(uid='kl_1', weight=1)
        self._seed(uid='kl_2', weight=1)
        firsts = [self.adapter._candidates()[0]['uid'] for _ in range(4)]
        self.assertEqual(sorted(firsts), ['kl_1', 'kl_1', 'kl_2', 'kl_2'])

    def test_affinity_pins_the_same_account(self) -> None:
        self._seed(uid='kl_1')
        self._seed(uid='kl_2')
        first = self.adapter._candidates('s1')[0]['uid']
        again = self.adapter._candidates('s1')[0]['uid']
        self.assertEqual(first, again)

    # ── 对话 ──────────────────────────────────────────────
    def test_chat_nonstream_passes_body_through(self) -> None:
        self._seed()
        seen: dict = {}

        async def fake_chat(token, body, **kwargs):
            seen.update({'token': token, 'body': body})
            return {'choices': [{'message': {'content': 'ok'}}],
                    'usage': {'prompt_tokens': 1, 'completion_tokens': 2}}

        with mock.patch.object(client, 'chat', new=fake_chat):
            result = asyncio.run(self.adapter.chat('kilo-auto/free', {
                'messages': [{'role': 'user', 'content': 'hi'}],
                'max_tokens': 4096,
            }))
        self.assertEqual(seen['token'], _TOKEN)
        self.assertEqual(seen['body']['model'], 'kilo-auto/free')
        # Kilo 不做 max_tokens 改写。
        self.assertEqual(seen['body']['max_tokens'], 4096)
        self.assertEqual(result.model, 'kilo/kilo-auto/free')
        self.assertEqual(result.realm, 'cn')

    def test_empty_model_falls_back_to_default(self) -> None:
        self._seed()
        seen: dict = {}

        async def fake_chat(token, body, **kwargs):
            seen.update(body)
            return {'choices': []}

        with mock.patch.object(client, 'chat', new=fake_chat):
            asyncio.run(self.adapter.chat('', {'messages': []}))
        self.assertEqual(seen['model'], C.DEFAULT_MODEL)

    def test_chat_401_switches_account_without_cooling(self) -> None:
        self._seed(uid='kl_1')
        self._seed(uid='kl_2', token='second-token')
        seen: list[str] = []

        async def fake_chat(token, body, **kwargs):
            seen.append(token)
            if token == _TOKEN:
                raise PlatformError('token 失效', status=401,
                                    code='login_expired')
            return {'choices': [{'message': {'content': 'ok'}}]}

        with mock.patch.object(client, 'chat', new=fake_chat):
            result = asyncio.run(self.adapter.chat(
                'kilo-auto/free', {'messages': []}))
        self.assertEqual(sorted(seen), sorted([_TOKEN, 'second-token']))
        self.assertEqual(result.payload['choices'][0]['message']['content'], 'ok')
        # 401 不冻结账号：凭证问题冻了也不会自己好。
        self.assertFalse(store.is_cooling(store.read_account('kl_1')))

    def test_429_cools_the_account(self) -> None:
        self._seed()

        async def fake_chat(token, body, **kwargs):
            raise PlatformError('限流', status=429, code='rate_limited',
                                retry_after=30)

        with mock.patch.object(client, 'chat', new=fake_chat):
            with self.assertRaises(PlatformError) as ctx:
                asyncio.run(self.adapter.chat('kilo-auto/free', {'messages': []}))
        self.assertEqual(ctx.exception.status, 429)
        raw = store.read_account('kl_1')
        self.assertTrue(store.is_cooling(raw))

    def test_stream_relays_bytes_and_fills_usage(self) -> None:
        self._seed()

        class FakeResponse:
            def __init__(self) -> None:
                self.closed = False

            async def aiter_bytes(self):
                yield (b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
                       b'data: {"choices":[],"usage":{"prompt_tokens":2,'
                       b'"completion_tokens":3}}\n\n'
                       b'data: [DONE]\n\n')

            async def aclose(self):
                self.closed = True

        response = FakeResponse()

        async def fake_open(*args, **kwargs):
            return response

        async def collect():
            result = await self.adapter.chat('kilo-auto/free', {
                'stream': True, 'messages': [{'role': 'user', 'content': 'hi'}]})
            data = b''
            async for chunk in result.raw_stream:
                data += chunk
            return result, data

        with mock.patch.object(client, 'open_chat', new=fake_open):
            result, data = asyncio.run(collect())
        self.assertIn(b'"content":"hi"', data)
        self.assertEqual(result.usage['completion_tokens'], 3)
        self.assertIsNotNone(result.first_token_ms)
        self.assertTrue(response.closed)

    # ── 能力 ──────────────────────────────────────────────
    def test_checkin_is_unsupported(self) -> None:
        self._seed()
        with self.assertRaises(PlatformError) as ctx:
            asyncio.run(self.adapter.checkin('kl_1'))
        self.assertEqual(ctx.exception.code, 'checkin_unsupported')

    def test_web_login_is_enabled_and_wired(self) -> None:
        self.assertTrue(self.adapter.supports_web_login)
        self.assertFalse(self.adapter.supports_checkin)
        self.assertFalse(self.adapter.realm_aware)
        with mock.patch.object(login, 'start',
                               return_value={'login_id': 'x',
                                             'status': 'pending'}) as starter:
            self.assertEqual(self.adapter.login_start()['login_id'], 'x')
        starter.assert_called_once_with(None)


if __name__ == '__main__':
    unittest.main()
