"""Cline（cline.bot）平台：账号存储、目录、token 轮换与适配器策略。

全部离线：刷新 / 对话 / 设备码都替换成假客户端。这里钉住四件容易回归的事：

  · refresh token 是**一次性**的：刷新结果必须**先落盘再使用**，401 时同账号
    只换一份 token 重试一次；
  · 免费档（`deepseek/` / `cline-free/` / `cline-pass/`）**只认流式**：非流式
    请求也要按流式发，再用 `sse_to_completion` 聚合回 `chat.completion`；
  · 上游把限流伪装成 200 正常帧（`empty response content` / `Daily free limit`
    / `INFERENCE_CAP_ERROR`）时必须判成 429 换号，不能把空回复交给用户；
  · 免费档请求体不能带 `max_tokens`（上游直接 500）。
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
from server.platforms.cline import catalog, client, constants as C, login, store
from server.platforms.cline.adapter import ClineAdapter, _collect_sse, _scan_sse

_REFRESH = 'cline-refresh-abcdefghijklmnop'


def _sse(*frames: str) -> str:
    return ''.join(f'data: {frame}\n\n' for frame in frames)


class ClineStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(config, 'DATA_DIR', Path(self.tmp.name))
        patch.start()
        self.addCleanup(patch.stop)

    def test_derive_uid_is_stable(self) -> None:
        first = store.derive_uid(_REFRESH)
        self.assertTrue(first.startswith('cl_'))
        self.assertEqual(first, store.derive_uid(f'  Bearer {_REFRESH}  '))
        self.assertNotEqual(first, store.derive_uid('other'))

    def test_derive_uid_requires_refresh(self) -> None:
        with self.assertRaises(ValueError):
            store.derive_uid('  ')

    def test_public_account_hides_both_tokens(self) -> None:
        saved = store.save_account({
            'uid': 'cl_1', 'token': 'access-abcdefghij',
            'refreshToken': _REFRESH, 'email': 'a@b.c',
        })
        public = store.public_account(saved)
        dumped = json.dumps(public, ensure_ascii=False)
        self.assertNotIn(_REFRESH, dumped)
        self.assertNotIn('access-abcdefghij', dumped)
        self.assertTrue(public['has_token'])
        self.assertTrue(public['has_refresh_token'])
        self.assertEqual(public['realm'], 'cn')
        # access token 只有约 10 分钟且由适配器按需换：界面不能把到期当故障。
        self.assertTrue(public['token_auto_refresh'])

    def test_safe_id_rejects_traversal(self) -> None:
        for bad in ('../x', 'a/b', 'a\\b', '', 'x' * 200):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    store.safe_id(bad)

    def test_weight_and_cooling_mutations(self) -> None:
        store.save_account({'uid': 'cl_1', 'refreshToken': _REFRESH})
        store.set_weight('cl_1', 3)
        self.assertEqual(store.read_account('cl_1')['weight'], 3)
        store.set_cooling('cl_1', time.time() + 60, 'test')
        self.assertTrue(store.is_cooling(store.read_account('cl_1')))
        store.clear_cooling('cl_1')
        self.assertFalse(store.is_cooling(store.read_account('cl_1')))

    def test_epoch_roundtrip_handles_iso_and_millis(self) -> None:
        iso = store.iso_from_epoch(1765000000)
        self.assertEqual(store.epoch_from_iso(iso), 1765000000)
        # 上游 `expiresAt` 可能给毫秒。
        self.assertEqual(store.epoch_from_iso('1765000000000'), 1765000000)
        self.assertEqual(store.epoch_from_iso(''), 0.0)


class ClineCatalogTest(unittest.TestCase):
    def test_parse_models_skips_batch_variants(self) -> None:
        models = client.parse_models({'data': [
            {'id': 'cline-free/a', 'name': 'A'},
            {'id': 'cline-free/a:batch'},
            {'id': 'CLINE-FREE/A'},
            {'id': ''},
        ]})
        self.assertEqual([m['id'] for m in models], ['cline-free/a'])

    def test_parse_models_marks_free_group(self) -> None:
        models = client.parse_models(
            {'data': [{'id': 'cline-free/a'}]}, free=True)
        self.assertTrue(models[0]['free'])

    def test_merge_puts_free_first_and_keeps_flag(self) -> None:
        merged = catalog.merge(
            [{'id': 'free/x'}],
            [{'id': 'paid/y'}, {'id': 'free/x', 'name': 'dup'}])
        self.assertEqual([m['id'] for m in merged], ['free/x', 'paid/y'])
        self.assertTrue(merged[0]['free'])
        self.assertFalse(merged[1]['free'])

    def test_default_catalog_is_all_free(self) -> None:
        models = catalog.default_models()
        self.assertTrue(models)
        self.assertTrue(all(m['free'] for m in models))
        self.assertTrue(all(m['id'].startswith('cline-free/') for m in models))

    def test_needs_force_stream(self) -> None:
        for model in ('cline-free/x', 'deepseek/v4', 'cline-pass/y'):
            with self.subTest(model=model):
                self.assertTrue(client.needs_force_stream(model))
        for model in ('openai/gpt-4o', 'anthropic/claude', ''):
            with self.subTest(model=model):
                self.assertFalse(client.needs_force_stream(model))

    def test_limit_signal_markers(self) -> None:
        for text in ('empty response content', 'Daily free limit reached',
                     'INFERENCE_CAP_ERROR'):
            with self.subTest(text=text):
                self.assertTrue(client.limit_signal(text))
        self.assertFalse(client.limit_signal('hello'))
        self.assertFalse(client.limit_signal(''))


class ClineClientTest(unittest.TestCase):
    @staticmethod
    def _error(status: int, payload: object, headers: dict | None = None):
        response = httpx.Response(status, json=payload, headers=headers or {})
        return client._response_error(response, json.dumps(payload), payload)

    def test_error_mapping(self) -> None:
        auth = self._error(401, {'error': {'message': 'x'}})
        self.assertEqual((auth.status, auth.code), (401, 'token_expired'))
        pay = self._error(400, {'error': {'code': 'insufficient_credits',
                                          'message': 'no credits'}})
        self.assertEqual((pay.status, pay.code), (402, 'insufficient_credits'))
        rate = self._error(429, {'error': {'message': 'slow'}},
                           {'retry-after': '12'})
        self.assertEqual((rate.status, rate.retry_after), (429, 12))
        forbid = self._error(403, {'error': {'message': 'product surfaces'}})
        self.assertEqual((forbid.status, forbid.code), (403, 'forbidden'))
        down = self._error(500, {'error': {'message': 'boom'}})
        self.assertEqual((down.status, down.code), (502, 'upstream_error'))

    def test_parse_refresh_reads_data_wrapper(self) -> None:
        out = client.parse_refresh({'data': {
            'accessToken': 'at', 'refreshToken': 'rt', 'expiresAt': 1765000000}})
        self.assertEqual(out['access_token'], 'at')
        self.assertEqual(out['refresh_token'], 'rt')
        self.assertEqual(out['expires_at'], 1765000000)
        # 毫秒 → 秒。
        ms = client.parse_refresh({'data': {'accessToken': 'at',
                                            'expiresAt': 1765000000000}})
        self.assertEqual(ms['expires_at'], 1765000000)

    def test_chat_body_adds_client_fields_and_strips_free_max_tokens(self) -> None:
        body = client.chat_body({'model': 'cline-free/x', 'max_tokens': 100,
                                 'max_completion_tokens': 50}, 'cline-free/x',
                                session_id='sess-1')
        self.assertEqual(body['session_id'], 'sess-1')
        self.assertEqual(body['reasoning_effort'], 'high')
        self.assertNotIn('max_tokens', body)
        self.assertNotIn('max_completion_tokens', body)

    def test_chat_body_keeps_max_tokens_for_paid_models(self) -> None:
        body = client.chat_body({'max_tokens': 100}, 'openai/gpt-4o')
        self.assertEqual(body['max_tokens'], 100)

    def test_sse_to_completion_aggregates_content_and_usage(self) -> None:
        events = [
            {'id': 'c1', 'choices': [{'delta': {'content': 'he'}}]},
            {'choices': [{'delta': {'content': 'llo'},
                          'finish_reason': 'stop'}],
             'usage': {'prompt_tokens': 3, 'completion_tokens': 2}},
        ]
        out = client.sse_to_completion(events, 'cline/x')
        self.assertEqual(out['object'], 'chat.completion')
        self.assertEqual(out['model'], 'cline/x')
        self.assertEqual(out['choices'][0]['message']['content'], 'hello')
        self.assertEqual(out['choices'][0]['finish_reason'], 'stop')
        self.assertEqual(out['usage']['completion_tokens'], 2)

    def test_sse_to_completion_falls_back_to_reasoning(self) -> None:
        events = [{'choices': [{'delta': {'reasoning_content': 'think'}}]}]
        out = client.sse_to_completion(events, 'cline/x')
        self.assertEqual(out['choices'][0]['message']['content'], 'think')

    def test_sse_to_completion_keeps_tool_calls(self) -> None:
        events = [{'choices': [{'delta': {'tool_calls': [
            {'index': 0, 'id': 'call_1', 'type': 'function',
             'function': {'name': 'f', 'arguments': '{}'}}]},
            'finish_reason': 'tool_calls'}]}]
        out = client.sse_to_completion(events, 'cline/x')
        message = out['choices'][0]['message']
        self.assertEqual(message['tool_calls'][0]['id'], 'call_1')
        self.assertEqual(out['choices'][0]['finish_reason'], 'tool_calls')

    def test_sse_to_completion_raises_on_disguised_limit(self) -> None:
        events = [{'choices': [{'delta': {
            'content': 'Daily free limit reached. Try again tomorrow.'}}]}]
        with self.assertRaises(PlatformError) as ctx:
            client.sse_to_completion(events, 'cline/x')
        self.assertEqual(ctx.exception.status, 429)
        self.assertEqual(ctx.exception.code, 'upstream_limited')


class ClineAdapterTest(unittest.TestCase):
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
        self.adapter = ClineAdapter()

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db

    def _seed(self, **overrides) -> dict:
        uid = str(overrides.pop('uid', 'cl_1'))
        data = {'uid': uid,
                'refreshToken': overrides.pop('refreshToken', _REFRESH)}
        data.update(overrides)
        store.save_account(data)
        return data

    # ── 导入 ──────────────────────────────────────────────
    def test_import_account_derives_uid_from_refresh(self) -> None:
        public = self.adapter.import_account({'refresh_token': f'Bearer {_REFRESH}'})
        self.assertTrue(public['uid'].startswith('cl_'))
        self.assertTrue(public['has_refresh_token'])
        self.assertEqual(store.read_account(public['uid'])['refreshToken'],
                         _REFRESH)

    def test_import_account_accepts_access_token(self) -> None:
        public = self.adapter.import_account({
            'refreshToken': _REFRESH, 'accessToken': 'at-1',
            'expires_in': 600})
        raw = store.read_account(public['uid'])
        self.assertEqual(raw['token'], 'at-1')
        self.assertTrue(raw['expiredAt'])

    def test_import_requires_refresh_token(self) -> None:
        with self.assertRaises(PlatformError):
            self.adapter.import_account({'accessToken': 'at-1'})

    # ── token 轮换 ────────────────────────────────────────
    def test_token_is_cached_until_skew(self) -> None:
        self._seed()
        calls: list[str] = []

        async def fake_refresh(refresh, **kwargs):
            calls.append(refresh)
            return {'access_token': 'at-1', 'refresh_token': 'rt-2',
                    'expires_at': time.time() + 3600}

        with mock.patch.object(client, 'refresh_token', new=fake_refresh):
            first = asyncio.run(self.adapter._token_for(
                store.read_account('cl_1'), proxy=None))
            second = asyncio.run(self.adapter._token_for(
                store.read_account('cl_1'), proxy=None))
        self.assertEqual((first, second), ('at-1', 'at-1'))
        self.assertEqual(len(calls), 1)

    def test_refresh_persists_new_tokens_before_use(self) -> None:
        """一次性 refresh token：换发后旧的立即作废，必须先落盘。"""
        self._seed()

        async def fake_refresh(refresh, **kwargs):
            return {'access_token': 'at-new', 'refresh_token': 'rt-new',
                    'expires_at': time.time() + 3600}

        with mock.patch.object(client, 'refresh_token', new=fake_refresh):
            token = asyncio.run(self.adapter._token_for(
                store.read_account('cl_1'), proxy=None))
        self.assertEqual(token, 'at-new')
        raw = store.read_account('cl_1')
        self.assertEqual(raw['token'], 'at-new')
        self.assertEqual(raw['refreshToken'], 'rt-new')

    def test_refresh_401_marks_login_expired(self) -> None:
        self._seed()

        async def fake_refresh(refresh, **kwargs):
            raise PlatformError('invalid_grant', status=401,
                                code='login_expired')

        with mock.patch.object(client, 'refresh_token', new=fake_refresh):
            with self.assertRaises(PlatformError):
                asyncio.run(self.adapter._token_for(
                    store.read_account('cl_1'), proxy=None))
        self.assertEqual(store.read_account('cl_1')['loginState'], '登录已失效')

    def test_refresh_5xx_cools_before_retrying(self) -> None:
        self._seed()

        async def fake_refresh(refresh, **kwargs):
            raise PlatformError('boom', status=502, code='upstream_error')

        with mock.patch.object(client, 'refresh_token', new=fake_refresh):
            with self.assertRaises(PlatformError):
                asyncio.run(self.adapter._token_for(
                    store.read_account('cl_1'), proxy=None))
        # 上游抖动时不该每个请求都去轮换一次性 token。
        self.assertTrue(store.is_cooling(store.read_account('cl_1')))

    def test_missing_refresh_token_is_login_expired(self) -> None:
        self._seed(refreshToken='')
        with self.assertRaises(PlatformError) as ctx:
            asyncio.run(self.adapter._token_for(
                store.read_account('cl_1'), proxy=None))
        self.assertEqual(ctx.exception.status, 401)

    # ── 探活 / 额度 ───────────────────────────────────────
    def test_refresh_account_rotates_and_clears_error(self) -> None:
        self._seed(lastRefreshError='旧错误')

        async def fake_refresh(refresh, **kwargs):
            return {'access_token': 'at-1', 'refresh_token': 'rt-2',
                    'expires_at': time.time() + 3600}

        with mock.patch.object(client, 'refresh_token', new=fake_refresh):
            public = asyncio.run(self.adapter.refresh_account('cl_1'))
        self.assertEqual(public['lastRefreshError'], '')
        self.assertEqual(public['login_state'], '正常')

    def test_usage_reports_error_without_raising(self) -> None:
        self._seed()

        async def fake_refresh(refresh, **kwargs):
            raise PlatformError('invalid_grant', status=401,
                                code='login_expired')

        with mock.patch.object(client, 'refresh_token', new=fake_refresh):
            out = asyncio.run(self.adapter.usage('cl_1'))
        self.assertIsNone(out['session_usage'])
        self.assertEqual(out['errors'][0]['code'], 'login_expired')

    def test_usage_succeeds_without_balance_endpoint(self) -> None:
        self._seed()

        async def fake_refresh(refresh, **kwargs):
            return {'access_token': 'at-1', 'refresh_token': 'rt-2',
                    'expires_at': time.time() + 3600}

        with mock.patch.object(client, 'refresh_token', new=fake_refresh):
            out = asyncio.run(self.adapter.usage('cl_1'))
        self.assertIn('message', out['session_usage'])
        self.assertIsNone(out['entitlement'])

    def test_credits_is_unsupported(self) -> None:
        self._seed()
        with self.assertRaises(PlatformError) as ctx:
            asyncio.run(self.adapter.credits('cl_1'))
        self.assertEqual(ctx.exception.code, 'credits_unsupported')

    # ── 模型目录 ──────────────────────────────────────────
    def test_list_models_adds_prefix_and_falls_back(self) -> None:
        models = asyncio.run(self.adapter.list_models())
        ids = {m['id'] for m in models}
        self.assertIn('cline/cline-free/nemotron-3.5-super-vl', ids)
        self.assertTrue(all(m['realm'] == 'cn' for m in models))
        self.assertIn('cline/cline-free/nemotron-3.5-super-vl',
                      self.adapter.cached_model_ids())

    def test_live_catalog_merges_free_and_full(self) -> None:
        async def fake_free(**kwargs):
            return [{'id': 'cline-free/x', 'name': 'Free', 'free': True}]

        async def fake_models(**kwargs):
            return [{'id': 'openai/gpt-4o', 'name': 'GPT'}]

        with mock.patch.object(client, 'fetch_recommended', new=fake_free), \
                mock.patch.object(client, 'fetch_models', new=fake_models):
            models = asyncio.run(self.adapter.list_models(refresh=True))
        self.assertEqual([m['model_name'] for m in models],
                         ['cline-free/x', 'openai/gpt-4o'])
        self.assertTrue(models[0]['free'])

    def test_live_catalog_survives_one_endpoint_failing(self) -> None:
        async def boom(**kwargs):
            raise PlatformError('down', status=502, code='upstream_error')

        async def fake_models(**kwargs):
            return [{'id': 'openai/gpt-4o', 'name': 'GPT'}]

        with mock.patch.object(client, 'fetch_recommended', new=boom), \
                mock.patch.object(client, 'fetch_models', new=fake_models):
            models = asyncio.run(self.adapter.list_models(refresh=True))
        self.assertEqual([m['model_name'] for m in models], ['openai/gpt-4o'])

    # ── 选号 ──────────────────────────────────────────────
    def test_candidates_skip_disabled_cooling_and_refreshless(self) -> None:
        self._seed(uid='cl_1')
        self._seed(uid='cl_2', disabled=True)
        self._seed(uid='cl_3', coolingUntil=time.time() + 600)
        self._seed(uid='cl_4', checkinOnly=True)
        store.save_account({'uid': 'cl_5', 'refreshToken': ''})
        ids = [a['uid'] for a in self.adapter._candidates()]
        self.assertEqual(ids, ['cl_1'])

    def test_weights_rotate_first_choice(self) -> None:
        self._seed(uid='cl_1', weight=1)
        self._seed(uid='cl_2', weight=1)
        firsts = [self.adapter._candidates()[0]['uid'] for _ in range(4)]
        self.assertEqual(sorted(firsts), ['cl_1', 'cl_1', 'cl_2', 'cl_2'])

    # ── 对话 ──────────────────────────────────────────────
    def _patch_token(self):
        async def fake_refresh(refresh, **kwargs):
            return {'access_token': 'at-1', 'refresh_token': 'rt-2',
                    'expires_at': time.time() + 3600}
        return mock.patch.object(client, 'refresh_token', new=fake_refresh)

    def test_chat_paid_model_uses_nonstream(self) -> None:
        self._seed()
        seen: dict = {}

        async def fake_chat(access, body, **kwargs):
            seen.update({'access': access, 'body': body})
            return {'choices': [{'message': {'content': 'ok'}}],
                    'usage': {'prompt_tokens': 1, 'completion_tokens': 2}}

        with self._patch_token(), \
                mock.patch.object(client, 'chat', new=fake_chat):
            result = asyncio.run(self.adapter.chat(
                'openai/gpt-4o', {'messages': [{'role': 'user',
                                                'content': 'hi'}]}))
        self.assertEqual(seen['access'], 'at-1')
        self.assertEqual(seen['body']['model'], 'openai/gpt-4o')
        self.assertEqual(result.payload['choices'][0]['message']['content'], 'ok')
        self.assertEqual(result.model, 'cline/openai/gpt-4o')

    def test_free_model_forces_stream_and_aggregates(self) -> None:
        self._seed()
        seen: dict = {}

        class FakeResponse:
            def __init__(self) -> None:
                self.closed = False

            async def aiter_lines(self):
                for line in _sse(
                        '{"choices":[{"delta":{"content":"he"}}]}',
                        '{"choices":[{"delta":{"content":"llo"},'
                        '"finish_reason":"stop"}],"usage":{"prompt_tokens":3,'
                        '"completion_tokens":2}}',
                        '[DONE]').splitlines():
                    yield line

            async def aclose(self):
                self.closed = True

        response = FakeResponse()

        async def fake_open(access, body, **kwargs):
            seen.update(body)
            return response

        with self._patch_token(), \
                mock.patch.object(client, 'open_chat', new=fake_open):
            result = asyncio.run(self.adapter.chat(
                'cline-free/x', {'messages': [{'role': 'user', 'content': 'hi'}],
                                 'max_tokens': 100}))
        # 非流式请求被强制走流式：body 里 stream=True 且免费档剥掉 max_tokens。
        self.assertTrue(seen['stream'])
        self.assertNotIn('max_tokens', seen)
        self.assertIn('session_id', seen)
        self.assertEqual(result.payload['object'], 'chat.completion')
        self.assertEqual(
            result.payload['choices'][0]['message']['content'], 'hello')
        self.assertEqual(result.usage['completion_tokens'], 2)
        self.assertTrue(response.closed)

    def test_free_model_disguised_limit_switches_account(self) -> None:
        self._seed(uid='cl_1')
        self._seed(uid='cl_2', refreshToken='rt-second')
        attempts: list[str] = []

        class FakeResponse:
            def __init__(self, text: str) -> None:
                self._text = text

            async def aiter_lines(self):
                for line in self._text.splitlines():
                    yield line

            async def aclose(self):
                pass

        async def fake_open(access, body, **kwargs):
            attempts.append(access)
            if len(attempts) == 1:
                return FakeResponse(_sse(
                    '{"choices":[{"delta":{"content":'
                    '"Daily free limit reached"}}]}', '[DONE]'))
            return FakeResponse(_sse(
                '{"choices":[{"delta":{"content":"ok"},'
                '"finish_reason":"stop"}]}', '[DONE]'))

        tokens = iter(['at-1', 'at-2'])

        async def fake_refresh(refresh, **kwargs):
            return {'access_token': next(tokens), 'refresh_token': refresh,
                    'expires_at': time.time() + 3600}

        with mock.patch.object(client, 'refresh_token', new=fake_refresh), \
                mock.patch.object(client, 'open_chat', new=fake_open):
            result = asyncio.run(self.adapter.chat(
                'cline-free/x', {'messages': []}))
        self.assertEqual(len(attempts), 2)
        self.assertEqual(
            result.payload['choices'][0]['message']['content'], 'ok')
        self.assertTrue(store.is_cooling(store.read_account('cl_1')))

    def test_chat_401_retries_same_account_with_fresh_token(self) -> None:
        self._seed()
        seen: list[str] = []
        tokens = iter(['at-old', 'at-new'])

        async def fake_refresh(refresh, **kwargs):
            return {'access_token': next(tokens), 'refresh_token': refresh,
                    'expires_at': time.time() + 3600}

        async def fake_chat(access, body, **kwargs):
            seen.append(access)
            if access == 'at-old':
                raise PlatformError('expired', status=401,
                                    code='token_expired')
            return {'choices': [{'message': {'content': 'ok'}}]}

        with mock.patch.object(client, 'refresh_token', new=fake_refresh), \
                mock.patch.object(client, 'chat', new=fake_chat):
            result = asyncio.run(self.adapter.chat(
                'openai/gpt-4o', {'messages': []}))
        self.assertEqual(seen, ['at-old', 'at-new'])
        self.assertEqual(result.payload['choices'][0]['message']['content'], 'ok')

    def test_empty_model_falls_back_to_free_default(self) -> None:
        self._seed()
        seen: dict = {}

        class FakeResponse:
            async def aiter_lines(self):
                for line in _sse(
                        '{"choices":[{"delta":{"content":"ok"},'
                        '"finish_reason":"stop"}]}', '[DONE]').splitlines():
                    yield line

            async def aclose(self):
                pass

        async def fake_open(access, body, **kwargs):
            seen.update(body)
            return FakeResponse()

        with self._patch_token(), \
                mock.patch.object(client, 'open_chat', new=fake_open):
            asyncio.run(self.adapter.chat('', {'messages': []}))
        self.assertEqual(seen['model'], C.DEFAULT_MODEL)

    def test_stream_relays_bytes_and_flags_error(self) -> None:
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
            result = await self.adapter.chat('cline-free/x', {
                'stream': True, 'messages': [{'role': 'user', 'content': 'hi'}]})
            data = b''
            async for chunk in result.raw_stream:
                data += chunk
            return result, data

        with self._patch_token(), \
                mock.patch.object(client, 'open_chat', new=fake_open):
            result, data = asyncio.run(collect())
        self.assertIn(b'"content":"hi"', data)
        self.assertEqual(result.usage['completion_tokens'], 3)
        self.assertIsNotNone(result.first_token_ms)
        self.assertTrue(response.closed)

    # ── 能力 ──────────────────────────────────────────────
    def test_checkin_is_unsupported(self) -> None:
        self._seed()
        with self.assertRaises(PlatformError) as ctx:
            asyncio.run(self.adapter.checkin('cl_1'))
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


class ClineScanTest(unittest.TestCase):
    def test_scan_sse_extracts_usage(self) -> None:
        usage: dict = {}
        pending, saw, error = _scan_sse(
            b'data: {"choices":[{"delta":{"content":"hi"}}]}\n'
            b'data: {"choices":[],"usage":{"prompt_tokens":3}}\n', usage)
        self.assertTrue(saw)
        self.assertEqual(error, '')
        self.assertEqual(pending, b'')
        self.assertEqual(usage['prompt_tokens'], 3)

    def test_scan_sse_flags_disguised_limit(self) -> None:
        usage: dict = {}
        _, saw, error = _scan_sse(
            b'data: {"choices":[{"delta":{"content":'
            b'"INFERENCE_CAP_ERROR"}}]}\n', usage)
        self.assertTrue(saw)
        self.assertEqual(error, 'INFERENCE_CAP_ERROR')

    def test_scan_sse_keeps_partial_line(self) -> None:
        usage: dict = {}
        pending, saw, _ = _scan_sse(b'data: {"choices":[{"delta":{"con', usage)
        self.assertEqual(pending, b'data: {"choices":[{"delta":{"con')
        self.assertFalse(saw)

    def test_collect_sse_parses_frames(self) -> None:
        class FakeResponse:
            async def aiter_lines(self):
                for line in ('data: {"a": 1}', '', 'data: {"b": 2}', '',
                             'data: [DONE]', ''):
                    yield line

        events = asyncio.run(_collect_sse(FakeResponse()))
        self.assertEqual(events, [{'a': 1}, {'b': 2}])


if __name__ == '__main__':
    unittest.main()
