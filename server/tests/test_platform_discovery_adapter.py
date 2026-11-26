"""Discovery（Intern TokenPlan）平台：账号存储、目录、错误映射与适配器策略。

全部离线：探针 / 对话都替换成假客户端。这里钉住三件容易回归的事：

  · 凭证是 API Key，没有 refresh token——`refresh_account` 只是探活；
  · `429 quota_exceeded` 必须按 **402** 上报（额度耗尽），否则适配器会按
    「限流几十秒」处理，等于每分钟重打一次已经耗尽的账号；
  · 空模型名补 `auto`，其余原样透传（不做参考实现的 `force-model` 静默改写）。
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
from server.platforms import settings as runtime_settings
from server.platforms.base import PlatformError
from server.platforms.discovery import catalog, client, constants as C, store
from server.platforms.discovery.adapter import DiscoveryAdapter, _scan_sse

_KEY = 'dc-key-abcdefghijklmnop'


class DiscoveryStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(config, 'DATA_DIR', Path(self.tmp.name))
        patch.start()
        self.addCleanup(patch.stop)

    def test_derive_uid_is_stable(self) -> None:
        first = store.derive_uid(_KEY)
        self.assertTrue(first.startswith('dc_'))
        self.assertEqual(first, store.derive_uid(f'  Bearer {_KEY}  '))
        self.assertNotEqual(first, store.derive_uid('other-key'))

    def test_derive_uid_requires_key(self) -> None:
        with self.assertRaises(ValueError):
            store.derive_uid('   ')

    def test_public_account_hides_key(self) -> None:
        saved = store.save_account({'uid': 'dc_1', 'apiKey': _KEY})
        public = store.public_account(saved)
        dumped = json.dumps(public, ensure_ascii=False)
        self.assertNotIn(_KEY, dumped)
        self.assertTrue(public['has_token'])
        self.assertFalse(public['has_refresh_token'])
        self.assertEqual(public['realm'], 'cn')
        self.assertTrue(public['token_preview'].endswith(_KEY[-4:]))

    def test_safe_id_rejects_traversal(self) -> None:
        for bad in ('../x', 'a/b', 'a\\b', '', 'x' * 200):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    store.safe_id(bad)

    def test_weight_and_cooling_mutations(self) -> None:
        store.save_account({'uid': 'dc_1', 'apiKey': _KEY})
        store.set_weight('dc_1', 2.5)
        self.assertEqual(store.read_account('dc_1')['weight'], 2.5)
        store.set_cooling('dc_1', time.time() + 60, 'test')
        self.assertTrue(store.is_cooling(store.read_account('dc_1')))
        store.clear_cooling('dc_1')
        self.assertFalse(store.is_cooling(store.read_account('dc_1')))


class DiscoveryCatalogTest(unittest.TestCase):
    def test_normalize_keeps_order_and_dedupes(self) -> None:
        models = catalog.normalize([
            {'id': 'auto', 'name': 'Auto', 'reasoning': True},
            {'id': 'AUTO'},
            {'id': '', 'name': 'bad'},
            'not-a-dict',
            {'model': 'deepseek-v3', 'context_length': 64000},
        ])
        self.assertEqual([m['id'] for m in models], ['auto', 'deepseek-v3'])
        self.assertTrue(models[0]['reasoning'])
        self.assertEqual(models[1]['context_length'], 64000)

    def test_default_catalog_has_auto(self) -> None:
        ids = {m['id'] for m in catalog.default_models()}
        self.assertIn('auto', ids)
        self.assertTrue(all(m['type'] == 'chat' for m in catalog.default_models()))


class DiscoveryClientErrorTest(unittest.TestCase):
    """错误映射：额度耗尽与限流不能混成同一个状态码。"""

    @staticmethod
    def _error(status: int, payload: object, headers: dict | None = None):
        response = httpx.Response(status, json=payload, headers=headers or {})
        return client._response_error(response, json.dumps(payload), payload)

    def test_quota_exceeded_is_402(self) -> None:
        exc = self._error(429, {'error': {'code': C.CODE_QUOTA,
                                          'message': 'quota used up'}})
        self.assertEqual(exc.status, 402)
        self.assertEqual(exc.code, 'quota_exceeded')

    def test_plain_rate_limit_stays_429(self) -> None:
        exc = self._error(429, {'error': {'code': C.CODE_RATE,
                                          'message': 'slow down'}},
                          {'retry-after': '7'})
        self.assertEqual(exc.status, 429)
        self.assertEqual(exc.retry_after, 7)

    def test_invalid_key_is_401(self) -> None:
        exc = self._error(401, {'error': {'code': C.CODE_INVALID_KEY,
                                          'message': 'bad key'}})
        self.assertEqual(exc.status, 401)
        self.assertEqual(exc.code, 'login_expired')

    def test_cluster_tpm_429_is_not_a_per_key_rate_limit(self) -> None:
        # 上游的账户级上限：换 Key 逃不掉，适配器要靠这个码区分。
        exc = self._error(429, {'error': {
                              'code': 'upstream_request_rejected',
                              'message': 'Cluster TPM rate limit exceeded.'}},
                          {'retry-after': '5'})
        self.assertEqual(exc.status, 429)
        self.assertEqual(exc.code, 'cluster_rate_limited')
        self.assertEqual(exc.retry_after, 5)

    def test_model_disabled_surfaces_suggested_model(self) -> None:
        exc = self._error(404, {'error': {
                              'code': 'model_disabled',
                              'message': "Model 'old' was disabled",
                              'suggested_model': 'new'}})
        self.assertEqual(exc.status, 404)
        self.assertEqual(exc.code, 'model_disabled')
        self.assertIn("Model 'old' was disabled", exc.message)
        self.assertIn('new', exc.message)

    def test_parse_models_rejects_wrong_shape(self) -> None:
        self.assertEqual(client.parse_models({}), [])
        self.assertEqual(client.parse_models({'data': 'x'}), [])


class DiscoveryAdapterTest(unittest.TestCase):
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
        self.adapter = DiscoveryAdapter()

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db

    def _seed(self, **overrides) -> dict:
        uid = str(overrides.pop('uid', 'dc_1'))
        data = {'uid': uid, 'apiKey': overrides.pop('apiKey', _KEY)}
        data.update(overrides)
        store.save_account(data)
        return data

    # ── 导入 ──────────────────────────────────────────────
    def test_import_account_derives_uid_and_hides_key(self) -> None:
        public = self.adapter.import_account({'api_key': f'Bearer {_KEY}'})
        self.assertTrue(public['uid'].startswith('dc_'))
        self.assertTrue(public['has_token'])
        self.assertEqual(store.read_account(public['uid'])['apiKey'], _KEY)

    def test_import_requires_key(self) -> None:
        with self.assertRaises(PlatformError):
            self.adapter.import_account({'nickname': 'x'})

    # ── 探活 ──────────────────────────────────────────────
    def test_refresh_account_records_probe_state(self) -> None:
        self._seed()

        async def fake_probe(api_key, **kwargs):
            return {'state': 'ok', 'message': '', 'models': [{'id': 'auto'}]}

        with mock.patch.object(client, 'probe', new=fake_probe):
            public = asyncio.run(self.adapter.refresh_account('dc_1'))
        self.assertEqual(public['quota']['state'], 'ok')
        self.assertEqual(public['login_state'], '正常')

    def test_refresh_account_raises_401_when_key_invalid(self) -> None:
        self._seed()

        async def fake_probe(api_key, **kwargs):
            return {'state': 'invalid', 'message': 'Key 无效', 'models': []}

        with mock.patch.object(client, 'probe', new=fake_probe):
            with self.assertRaises(PlatformError) as ctx:
                asyncio.run(self.adapter.refresh_account('dc_1'))
        self.assertEqual(ctx.exception.status, 401)
        self.assertEqual(store.read_account('dc_1')['loginState'], '登录已失效')

    # ── 模型目录 ──────────────────────────────────────────
    def test_list_models_adds_prefix_and_falls_back(self) -> None:
        models = asyncio.run(self.adapter.list_models())
        ids = {m['id'] for m in models}
        self.assertIn('discovery/auto', ids)
        self.assertTrue(all(m['realm'] == 'cn' for m in models))
        self.assertIn('discovery/auto', self.adapter.cached_model_ids())

    def test_live_catalog_uses_any_key(self) -> None:
        self._seed()

        async def fake_models(api_key, **kwargs):
            return [{'id': 'glm-4', 'name': 'GLM-4'}]

        with mock.patch.object(client, 'fetch_models', new=fake_models):
            models = asyncio.run(self.adapter.list_models(refresh=True))
        self.assertEqual([m['model_name'] for m in models], ['glm-4'])

    # ── 选号 ──────────────────────────────────────────────
    def test_candidates_skip_disabled_and_cooling(self) -> None:
        self._seed(uid='dc_1')
        self._seed(uid='dc_2', disabled=True)
        self._seed(uid='dc_3', coolingUntil=time.time() + 600)
        ids = [a['uid'] for a in self.adapter._candidates()]
        self.assertEqual(ids, ['dc_1'])

    def test_weights_rotate_first_choice(self) -> None:
        self._seed(uid='dc_1', weight=1)
        self._seed(uid='dc_2', weight=1)
        firsts = [self.adapter._candidates()[0]['uid'] for _ in range(4)]
        self.assertEqual(sorted(firsts), ['dc_1', 'dc_1', 'dc_2', 'dc_2'])

    # ── 对话 ──────────────────────────────────────────────
    def test_chat_nonstream_passes_body_through(self) -> None:
        self._seed()
        seen: dict = {}

        async def fake_chat(api_key, body, **kwargs):
            seen.update({'key': api_key, 'body': body})
            return {'choices': [{'message': {'content': 'ok'}}],
                    'usage': {'prompt_tokens': 1, 'completion_tokens': 2}}

        with mock.patch.object(client, 'chat', new=fake_chat):
            result = asyncio.run(self.adapter.chat('glm-4', {
                'messages': [{'role': 'user', 'content': 'hi'}],
                'max_tokens': 4096,
            }))
        self.assertEqual(seen['key'], _KEY)
        self.assertEqual(seen['body']['model'], 'glm-4')
        # 不做 force-model：客户端写的 max_tokens 与模型名都原样保留。
        self.assertEqual(seen['body']['max_tokens'], 4096)
        self.assertEqual(result.model, 'discovery/glm-4')
        self.assertEqual(result.realm, 'cn')

    def test_empty_model_falls_back_to_auto(self) -> None:
        self._seed()
        seen: dict = {}

        async def fake_chat(api_key, body, **kwargs):
            seen.update(body)
            return {'choices': []}

        with mock.patch.object(client, 'chat', new=fake_chat):
            asyncio.run(self.adapter.chat('', {'messages': []}))
        self.assertEqual(seen['model'], C.DEFAULT_MODEL)

    def test_quota_error_cools_the_account(self) -> None:
        self._seed()

        async def fake_chat(api_key, body, **kwargs):
            raise PlatformError('额度耗尽', status=402, code='quota_exceeded')

        with mock.patch.object(client, 'chat', new=fake_chat):
            with self.assertRaises(PlatformError) as ctx:
                asyncio.run(self.adapter.chat('glm-4', {'messages': []}))
        self.assertEqual(ctx.exception.status, 402)
        raw = store.read_account('dc_1')
        self.assertGreater(raw.get('coolingUntil') or 0, time.time())
        self.assertIn('402', raw.get('coolingReason') or '')

    def test_transport_error_is_not_charged_to_the_key(self) -> None:
        self._seed()
        # 即使运维把 5xx 冷却打开，链路失败也不该冻号。
        with mock.patch.object(runtime_settings, 'cooldown_5xx',
                               return_value=60):
            exc = PlatformError('连接 Discovery 失败：timeout', status=502,
                                code='network_error')
            self.assertFalse(self.adapter._cool_down('dc_1', exc))
        self.assertIsNone(store.read_account('dc_1').get('coolingUntil'))

    def test_cluster_rate_limit_is_not_charged_to_the_key(self) -> None:
        self._seed()
        with mock.patch.object(runtime_settings, 'cooldown_429',
                               return_value=60):
            exc = PlatformError('Cluster TPM rate limit exceeded.',
                                status=429, code='cluster_rate_limited',
                                retry_after=5)
            self.assertFalse(self.adapter._cool_down('dc_1', exc))
        self.assertIsNone(store.read_account('dc_1').get('coolingUntil'))

    def test_transport_error_rotates_to_next_account(self) -> None:
        self._seed(uid='dc_1', apiKey='key-one', weight=10)
        self._seed(uid='dc_2', apiKey='key-two', weight=1)
        calls: list[str] = []

        async def fake_chat(api_key, body, **kwargs):
            calls.append(api_key)
            if api_key == 'key-one':
                raise PlatformError('连接 Discovery 失败：timeout', status=502,
                                    code='network_error')
            return {'choices': [{'message': {'content': 'ok'}}]}

        with mock.patch.object(client, 'chat', new=fake_chat):
            result = asyncio.run(self.adapter.chat('glm-4', {'messages': []}))
        self.assertEqual(calls, ['key-one', 'key-two'])
        self.assertEqual(result.payload['choices'][0]['message']['content'],
                         'ok')
        self.assertIsNone(store.read_account('dc_1').get('coolingUntil'))

    def test_cluster_tpm_waits_out_the_window_then_retries_same_account(
            self) -> None:
        self._seed(uid='dc_1', apiKey='key-one', weight=10)
        self._seed(uid='dc_2', apiKey='key-two', weight=1)
        calls: list[str] = []

        async def fake_chat(api_key, body, **kwargs):
            calls.append(api_key)
            if len(calls) == 1:
                raise PlatformError('Cluster TPM rate limit exceeded.',
                                    status=429, code='cluster_rate_limited',
                                    retry_after=7)
            return {'choices': [{'message': {'content': 'ok'}}]}

        slept: list[float] = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        with mock.patch.object(client, 'chat', new=fake_chat), \
                mock.patch.object(asyncio, 'sleep', new=fake_sleep):
            result = asyncio.run(self.adapter.chat('glm-4', {'messages': []}))
        # 同一个 Key 再来一次：账户级上限换号也逃不掉。
        self.assertEqual(calls, ['key-one', 'key-one'])
        self.assertEqual(slept, [7.0])
        self.assertEqual(result.payload['choices'][0]['message']['content'],
                         'ok')
        self.assertIsNone(store.read_account('dc_1').get('coolingUntil'))

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
            result = await self.adapter.chat('glm-4', {
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

    def test_scan_sse_extracts_usage_and_content(self) -> None:
        usage: dict = {}
        pending, saw, error = _scan_sse(
            b'data: {"choices":[{"delta":{"content":"hi"}}]}\n'
            b'data: {"choices":[],"usage":{"prompt_tokens":3}}\n', usage)
        self.assertTrue(saw)
        self.assertEqual(error, '')
        self.assertEqual(pending, b'')
        self.assertEqual(usage['prompt_tokens'], 3)

    def test_scan_sse_keeps_partial_line(self) -> None:
        usage: dict = {}
        pending, saw, _ = _scan_sse(b'data: {"choices":[{"delta":{"con', usage)
        self.assertEqual(pending, b'data: {"choices":[{"delta":{"con')
        self.assertFalse(saw)

    # ── 请求方言归一（上游 f06bb548）───────────────────────
    def test_normalize_body_maps_roles_and_moves_max_tokens(self) -> None:
        body = {
            'model': 'x', 'reasoning_effort': 'high', 'store': True,
            'max_completion_tokens': 100,
            'messages': [{'role': 'developer', 'content': 'sys'},
                         {'role': 'user', 'content': 'hi'}],
        }
        out, notes = client.normalize_body(body)
        self.assertNotIn('reasoning_effort', out)
        self.assertNotIn('store', out)
        self.assertNotIn('max_completion_tokens', out)
        self.assertEqual(out['max_tokens'], 100)
        self.assertEqual(out['messages'][0]['role'], 'system')
        self.assertEqual(out['messages'][1]['role'], 'user')
        self.assertTrue(any(n.startswith('role:developer') for n in notes))
        # 原 body 不能被子改动污染（组合会复用同一份）。
        self.assertEqual(body['messages'][0]['role'], 'developer')
        self.assertIn('reasoning_effort', body)

    def test_normalize_body_keeps_existing_max_tokens(self) -> None:
        out, _ = client.normalize_body(
            {'max_tokens': 10, 'max_completion_tokens': 99, 'messages': []})
        self.assertEqual(out['max_tokens'], 10)
        self.assertNotIn('max_completion_tokens', out)

    def test_normalize_body_is_idempotent_and_passes_standard_bodies(self) -> None:
        standard = {'model': 'deepseek-v3', 'max_tokens': 50, 'temperature': 0.7,
                    'messages': [{'role': 'system', 'content': 's'},
                                 {'role': 'user', 'content': 'hi'}]}
        out, notes = client.normalize_body(standard)
        self.assertEqual(notes, [])
        self.assertIs(out, standard, '标准体必须原样返回同一个对象')
        once, _ = client.normalize_body(
            {'max_completion_tokens': 9,
             'messages': [{'role': 'developer', 'content': 'x'}]})
        twice, notes2 = client.normalize_body(once)
        self.assertEqual(notes2, [])
        self.assertIs(twice, once)

    # ── 慢速 400 跨键重试（上游 35a8d552）──────────────────
    def test_transient_rejection_needs_slow_elapsed_and_code(self) -> None:
        rejected = PlatformError('upstream_request_rejected', status=400,
                                 code='bad_request',
                                 payload={'error': {'code': 'upstream_request_rejected'}})
        self.assertTrue(client.is_transient_rejection(rejected, 12.0))
        self.assertFalse(client.is_transient_rejection(rejected, 0.4),
                         '快 400 是 schema 错误，必须快速失败')
        other = PlatformError('bad schema', status=400, code='bad_request',
                              payload={'error': {'code': 'invalid_value'}})
        self.assertFalse(client.is_transient_rejection(other, 20.0))
        self.assertFalse(client.is_transient_rejection(
            PlatformError('x', status=502, code='upstream_error'), 20.0))

    def test_slow_400_rotates_to_another_key_once(self) -> None:
        self._seed(uid='dc_1')
        self._seed(uid='dc_2', apiKey='dc-key-2')
        seen: list[str] = []

        async def fake_chat(api_key, body, **kwargs):
            seen.append(api_key)
            if len(seen) == 1:
                raise PlatformError(
                    'upstream_request_rejected', status=400, code='bad_request',
                    payload={'error': {'code': 'upstream_request_rejected'}})
            return {'choices': [], 'usage': {}}

        clock = {'t': 0.0}

        def fake_monotonic() -> float:
            clock['t'] += 20.0
            return clock['t']

        with mock.patch.object(client, 'chat', new=fake_chat), \
                mock.patch('server.platforms.discovery.adapter.time.monotonic',
                           new=fake_monotonic):
            result = asyncio.run(self.adapter.chat('glm-4', {'messages': []}))
        self.assertEqual(len(seen), 2, '慢速 400 应换一把 Key 重试')
        self.assertNotEqual(seen[0], seen[1])
        self.assertEqual(result.model, 'discovery/glm-4')

    def test_fast_400_does_not_rotate(self) -> None:
        self._seed(uid='dc_1')
        self._seed(uid='dc_2', apiKey='dc-key-2')
        calls: list[str] = []

        async def fake_chat(api_key, body, **kwargs):
            calls.append(api_key)
            raise PlatformError('bad schema', status=400, code='bad_request')

        with mock.patch.object(client, 'chat', new=fake_chat):
            with self.assertRaises(PlatformError):
                asyncio.run(self.adapter.chat('glm-4', {'messages': []}))
        self.assertEqual(len(calls), 1, '快 400 不该换号')

    # ── 目录陈旧兜底 ──────────────────────────────────────
    def test_stale_catalog_survives_refresh_failure(self) -> None:
        self._seed()
        good = [{'id': 'glm-4', 'name': 'GLM-4'}]
        self.adapter._save_models(catalog.normalize(good))
        # 把缓存时间推到很久以前，模拟 TTL 过期。
        db.set_setting('platforms.discovery.models',
                       {'fetched_at': 0, 'models': catalog.normalize(good)})

        async def failing(api_key, **kwargs):
            raise PlatformError('上游挂了', status=502, code='upstream_error')

        with mock.patch.object(client, 'fetch_models', new=failing), \
                mock.patch.object(client, 'probe',
                                  new=mock.AsyncMock(return_value={
                                      'state': 'unknown', 'message': '', 'models': []})):
            models = asyncio.run(self.adapter.list_models(refresh=True))
        self.assertEqual([m['model_name'] for m in models], ['glm-4'],
                         '拉不到新目录时要继续用上一份真实目录，而不是内置快照')

    # ── 额度 / 能力 ───────────────────────────────────────
    def test_credits_is_unsupported(self) -> None:
        self._seed()
        with self.assertRaises(PlatformError) as ctx:
            asyncio.run(self.adapter.credits('dc_1'))
        self.assertEqual(ctx.exception.code, 'credits_unsupported')

    def test_usage_reports_probe_state_without_raising(self) -> None:
        self._seed()

        async def fake_probe(api_key, **kwargs):
            return {'state': 'quota_exhausted', 'message': '额度用尽',
                    'models': []}

        with mock.patch.object(client, 'probe', new=fake_probe):
            out = asyncio.run(self.adapter.usage('dc_1'))
        self.assertEqual(out['session_usage']['state'], 'quota_exhausted')
        self.assertTrue(out['entitlement']['exhausted'])
        self.assertEqual(out['uid'], 'dc_1')

    def test_checkin_is_unsupported(self) -> None:
        self._seed()
        with self.assertRaises(PlatformError) as ctx:
            asyncio.run(self.adapter.checkin('dc_1'))
        self.assertEqual(ctx.exception.code, 'checkin_unsupported')

    def test_no_web_login(self) -> None:
        self.assertFalse(self.adapter.supports_web_login)
        self.assertFalse(self.adapter.supports_checkin)
        self.assertFalse(self.adapter.realm_aware)


if __name__ == '__main__':
    unittest.main()
