"""MiniMax Code 平台：账号存储、目录、翻译、适配器策略。

全部离线：OAuth / 对话 / 签到 / 积分都替换成假客户端，测的是
「上游字段 → token-farm 形状」的翻译与选号 / 轮换 / 降级策略。
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from server import config, db
from server.platforms.base import PlatformError
from server.platforms.minimax import catalog, client, constants as C, store
from server.platforms.minimax import translate
from server.platforms.minimax.adapter import MinimaxAdapter, _amount

_ACCESS = 'access-token-abcdefghijklmnop'
_REFRESH = 'refresh-token-abcdefghijklmnop'


class MinimaxStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(config, 'DATA_DIR', Path(self.tmp.name))
        patch.start()
        self.addCleanup(patch.stop)

    def test_derive_uid_is_stable_and_realm_scoped(self) -> None:
        first = store.derive_uid('cn', _REFRESH)
        self.assertTrue(first.startswith('mm_'))
        self.assertEqual(first, store.derive_uid('cn', _REFRESH))
        self.assertNotEqual(first, store.derive_uid('global', _REFRESH))
        with self.assertRaises(ValueError):
            store.derive_uid('cn', '')

    def test_public_account_hides_tokens(self) -> None:
        saved = store.save_account({
            'uid': 'mm_1', 'realm': 'cn', 'token': _ACCESS,
            'refreshToken': _REFRESH, 'realUserID': 'u-1', 'email': 'a@b.c',
            'expiredAt': store.iso_from_epoch(time.time() + 3600),
        })
        public = store.public_account(saved)
        dumped = json.dumps(public, ensure_ascii=False)
        self.assertNotIn('abcdefghijklmnop', dumped)
        self.assertTrue(public['has_token'])
        self.assertTrue(public['has_refresh_token'])
        self.assertEqual(public['realm'], 'cn')
        self.assertEqual(public['realm_label'], '国内版')
        self.assertEqual(public['email'], 'a@b.c')

    def test_safe_id_rejects_traversal(self) -> None:
        for bad in ('../x', 'a/b', 'a\\b', '', 'x' * 200):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    store.safe_id(bad)

    def test_weight_and_cooling_mutations(self) -> None:
        store.save_account({'uid': 'mm_1', 'realm': 'cn', 'refreshToken': _REFRESH})
        store.set_weight('mm_1', 2.5)
        self.assertEqual(store.read_account('mm_1')['weight'], 2.5)
        store.set_cooling('mm_1', time.time() + 60, 'test')
        self.assertTrue(store.is_cooling(store.read_account('mm_1')))
        store.clear_cooling('mm_1')
        self.assertFalse(store.is_cooling(store.read_account('mm_1')))

    def test_realm_aliases(self) -> None:
        self.assertEqual(store.normalize_realm('intl'), 'global')
        self.assertEqual(store.normalize_realm('com'), 'global')
        self.assertEqual(store.normalize_realm(''), 'cn')


class MinimaxCatalogTest(unittest.TestCase):
    def test_parse_live_models_keeps_order_and_metadata(self) -> None:
        payload = {'providers': [{
            'providerId': 'minimax',
            'config': {
                'model_order': ['MiniMax-M3', 'MiniMax-M2.7'],
                'models': {
                    'MiniMax-M3': {
                        'name': 'M3',
                        'limit': {'context': 512000, 'output': 128000},
                        'modalities': {'input': ['text', 'image']},
                        'thinking_config': {'mode': 'switchable'},
                        'thinking': {'effortOptions': ['low', 'high']},
                    },
                    'MiniMax-M2.7': {
                        'name': 'M2.7',
                        'limit': {'context': 200000, 'output': 128000},
                        'modalities': {'input': ['text']},
                        'thinking_config': {'mode': 'forced_off'},
                    },
                },
            },
        }]}
        models = catalog.parse_models(payload)
        self.assertEqual([m['id'] for m in models], ['MiniMax-M3', 'MiniMax-M2.7'])
        first = models[0]
        # 实测上限覆盖客户端的保守声明
        self.assertEqual(first['context_length'], 1048576)
        self.assertEqual(first['max_tokens'], 524288)
        self.assertEqual(first['modalities'], ['text', 'image'])
        self.assertEqual(first['efforts'], ['low', 'high'])
        self.assertTrue(first['reasoning'])
        self.assertFalse(models[1]['reasoning'])

    def test_parse_models_rejects_wrong_shape(self) -> None:
        self.assertEqual(catalog.parse_models({}), [])
        self.assertEqual(catalog.parse_models({'providers': []}), [])
        self.assertEqual(catalog.parse_models({'providers': [{'providerId': 'x'}]}), [])

    def test_default_catalog_has_m3(self) -> None:
        models = catalog.default_models()
        ids = {m['id'] for m in models}
        self.assertIn('MiniMax-M3', ids)
        self.assertIn('MiniMax-M3.1-Flash-Preview', ids)
        self.assertTrue(all(m['type'] == 'chat' for m in models))


class MinimaxTranslateTest(unittest.TestCase):
    def test_openai_request_to_anthropic(self) -> None:
        body = {
            'messages': [
                {'role': 'system', 'content': 'sys'},
                {'role': 'user', 'content': 'hi'},
                {'role': 'assistant', 'content': None, 'tool_calls': [{
                    'id': 'c1', 'type': 'function',
                    'function': {'name': 'f', 'arguments': '{"a": 1}'}},
                ]},
                {'role': 'tool', 'tool_call_id': 'c1', 'content': 'ok'},
                {'role': 'user', 'content': [
                    {'type': 'text', 'text': 'go'},
                    {'type': 'image_url', 'image_url': {
                        'url': 'data:image/png;base64,AAA'}},
                ]},
            ],
            'max_tokens': 10,
            'temperature': 0.2,
            'stop': ['x'],
            'tools': [{'type': 'function', 'function': {
                'name': 'f', 'parameters': {'type': 'object'}}}],
            'tool_choice': {'type': 'function', 'function': {'name': 'f'}},
        }
        out = translate.to_anthropic(body)
        self.assertEqual(out['system'], 'sys')
        self.assertEqual(out['max_tokens'], 10)
        self.assertEqual(out['stop_sequences'], ['x'])
        self.assertEqual(out['temperature'], 0.2)
        self.assertEqual(out['tools'][0]['name'], 'f')
        self.assertEqual(out['tool_choice'], {'type': 'tool', 'name': 'f'})
        roles = [m['role'] for m in out['messages']]
        self.assertEqual(roles, ['user', 'assistant', 'user'])
        assistant = out['messages'][1]['content'][0]
        self.assertEqual(assistant['type'], 'tool_use')
        self.assertEqual(assistant['input'], {'a': 1})
        user = out['messages'][2]['content']
        self.assertEqual(user[0]['type'], 'tool_result')
        self.assertEqual(user[1]['text'], 'go')
        self.assertEqual(user[2]['source']['type'], 'base64')

    def test_openai_request_clamps_max_tokens(self) -> None:
        out = translate.to_anthropic(
            {'messages': [{'role': 'user', 'content': 'hi'}],
             'max_tokens': 1_000_000}, output_cap=524288)
        self.assertEqual(out['max_tokens'], 524288)

    def test_anthropic_response_to_openai(self) -> None:
        response = {
            'id': 'msg_1',
            'content': [
                {'type': 'thinking', 'thinking': 'think'},
                {'type': 'text', 'text': 'hello'},
                {'type': 'tool_use', 'id': 't1', 'name': 'f', 'input': {'a': 1}},
            ],
            'stop_reason': 'tool_use',
            'usage': {'input_tokens': 3, 'output_tokens': 4,
                      'cache_read_input_tokens': 2},
        }
        out = translate.from_anthropic(response, 'minimaxcn/MiniMax-M3')
        message = out['choices'][0]['message']
        self.assertEqual(message['content'], 'hello')
        self.assertEqual(message['reasoning_content'], 'think')
        self.assertEqual(message['tool_calls'][0]['function']['name'], 'f')
        self.assertEqual(out['choices'][0]['finish_reason'], 'tool_calls')
        self.assertEqual(out['usage']['prompt_tokens'], 5)
        self.assertEqual(out['usage']['prompt_tokens_details']['cached_tokens'], 2)

    def test_stream_translator(self) -> None:
        usage: dict = {}
        translator = translate.StreamTranslator('minimaxcn/MiniMax-M3', usage)
        events = [
            {'type': 'message_start',
             'message': {'id': 'msg_1', 'usage': {'input_tokens': 5}}},
            {'type': 'content_block_start', 'index': 0,
             'content_block': {'type': 'text'}},
            {'type': 'content_block_delta', 'index': 0,
             'delta': {'type': 'text_delta', 'text': 'hi'}},
            {'type': 'content_block_start', 'index': 1,
             'content_block': {'type': 'tool_use', 'id': 't1', 'name': 'f'}},
            {'type': 'content_block_delta', 'index': 1,
             'delta': {'type': 'input_json_delta', 'partial_json': '{"a"'}},
            {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'},
             'usage': {'output_tokens': 7}},
        ]
        chunks = [chunk for event in events for chunk in translator.feed(event)]
        chunks += translator.finish()
        text = b''.join(translate.sse(c) for c in chunks).decode('utf-8')
        self.assertIn('"content": "hi"', text)
        self.assertIn('"name": "f"', text)
        self.assertIn('"finish_reason": "tool_calls"', text)
        self.assertEqual(usage['completion_tokens'], 7)
        self.assertEqual(usage['prompt_tokens'], 5)

    def test_stream_error_event(self) -> None:
        translator = translate.StreamTranslator('m', {})
        with self.assertRaises(translate._StreamError):
            translator.feed({'type': 'error', 'error': {'message': 'boom'}})


class MinimaxClientSigningTest(unittest.TestCase):
    def test_signed_request_is_deterministic(self) -> None:
        info = client.site('cn')
        url, headers, body = client.signed_request(
            info, C.IDENTITY_PATH, access='a', user_id='u1', now_ms=1700000000000)
        self.assertIsNone(body)
        self.assertIn('user_id=u1', url)
        self.assertIn('unix=1700000000000', url)
        self.assertEqual(len(headers['yy']), 32)
        self.assertEqual(len(headers['x-signature']), 32)
        self.assertEqual(headers['Authorization'], 'Bearer a')
        again = client.signed_request(
            info, C.IDENTITY_PATH, access='a', user_id='u1', now_ms=1700000000000)
        self.assertEqual(url, again[0])
        self.assertEqual(headers['yy'], again[1]['yy'])
        self.assertEqual(headers['x-signature'], again[1]['x-signature'])

    def test_parse_plan(self) -> None:
        windows = client.parse_plan({'model_remains': [{
            'model_name': 'general',
            'current_interval_status': 1,
            'current_interval_used_percent': 30,
            'current_interval_remaining_percent': 70,
            'end_time': 1765000000000,
            'current_weekly_status': 3,
        }]})
        self.assertEqual(len(windows), 2)
        self.assertEqual(windows[0]['name'], 'general')
        self.assertEqual(windows[0]['used_percent'], 30)
        self.assertEqual(windows[0]['resets_at'], 1765000000000)
        self.assertTrue(windows[1]['unlimited'])

    def test_amount_parsing(self) -> None:
        self.assertEqual(_amount('1,200.5'), 1200.5)
        self.assertEqual(_amount('-3', clamp=True), 0.0)
        self.assertIsNone(_amount('nope'))


class MinimaxAdapterTest(unittest.TestCase):
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
        self.adapter = MinimaxAdapter()

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db

    def _seed(self, **overrides) -> dict:
        uid = str(overrides.pop('uid', 'mm_1'))
        data = {
            'uid': uid, 'realm': overrides.pop('realm', 'cn'),
            'token': overrides.pop('token', _ACCESS),
            'refreshToken': overrides.pop('refreshToken', _REFRESH),
            'realUserID': overrides.pop('realUserID', 'u-1'),
            'expiredAt': store.iso_from_epoch(time.time() + 3600),
        }
        data.update(overrides)
        store.save_account(data)
        return data

    # ── 导入 / 目录 ───────────────────────────────────────
    def test_import_derives_uid_and_hides_tokens(self) -> None:
        public = self.adapter.import_account({
            'realm': 'global', 'refresh_token': _REFRESH,
            'access_token': _ACCESS, 'expires_in': 3600,
        })
        self.assertTrue(public['uid'].startswith('mm_'))
        self.assertEqual(public['realm'], 'global')
        raw = store.read_account(public['uid'])
        self.assertEqual(raw['refreshToken'], _REFRESH)
        self.assertNotIn(_REFRESH,
                         json.dumps(public, ensure_ascii=False))

    def test_import_requires_a_token(self) -> None:
        with self.assertRaises(PlatformError):
            self.adapter.import_account({'nickname': 'x'})

    def test_identity_keeps_uid_stable_across_token_rotation(self) -> None:
        first = self.adapter.import_account({
            'realm': 'cn', 'refresh_token': 'r-old', 'realUserID': 'u-9'})
        second = self.adapter.import_account({
            'realm': 'cn', 'refresh_token': 'r-new', 'realUserID': 'u-9'})
        self.assertEqual(first['uid'], second['uid'])
        self.assertEqual(store.read_account(first['uid'])['refreshToken'], 'r-new')

    def test_list_models_emits_both_realms(self) -> None:
        models = asyncio.run(self.adapter.list_models())
        ids = {m['id'] for m in models}
        self.assertIn('minimaxcn/MiniMax-M3', ids)
        self.assertIn('minimaxcom/MiniMax-M3', ids)
        self.assertTrue(all(m['platform'] == 'minimax' for m in models))
        self.assertIn('minimaxcn/MiniMax-M3', self.adapter.cached_model_ids())

    def test_list_models_prefers_live_catalog(self) -> None:
        live = [{'id': 'only-live', 'name': 'only', 'context_length': 10,
                 'max_tokens': 10, 'modalities': ['text']}]
        with mock.patch.object(self.adapter, '_live_models',
                               new=mock.AsyncMock(return_value=live)):
            models = asyncio.run(self.adapter.list_models(refresh=True))
        self.assertEqual({m['model_name'] for m in models}, {'only-live'})

    # ── 选号 ──────────────────────────────────────────────
    def test_candidates_filter_realm_and_skip_disabled(self) -> None:
        self._seed(uid='mm_1', realm='cn')
        self._seed(uid='mm_2', realm='global')
        self._seed(uid='mm_3', realm='cn', disabled=True)
        ids = [a['uid'] for a in self.adapter._candidates('cn')]
        self.assertEqual(ids, ['mm_1'])
        self.assertEqual([a['uid'] for a in self.adapter._candidates('global')],
                         ['mm_2'])

    def test_candidates_skip_checkin_only(self) -> None:
        self._seed(uid='mm_1', realm='cn')
        self._seed(uid='mm_2', realm='cn', checkinOnly=True)
        self.assertEqual([a['uid'] for a in self.adapter._candidates('cn')],
                         ['mm_1'])

    def test_checkin_only_state_roundtrip(self) -> None:
        """仅签到与完全停用互斥：开一个必须清掉另一个。"""
        self._seed(uid='mm_1', realm='cn')
        only = store.set_checkin_only('mm_1', True)
        self.assertTrue(only['checkin_only'])
        self.assertFalse(only['disabled'])

        frozen = store.set_disabled('mm_1', True, '完全停用')
        self.assertTrue(frozen['disabled'])
        self.assertFalse(frozen['checkin_only'])

        only_again = store.set_checkin_only('mm_1', True)
        self.assertTrue(only_again['checkin_only'])
        self.assertFalse(only_again['disabled'])

        off = store.set_checkin_only('mm_1', False)
        self.assertFalse(off['checkin_only'])

    def test_weights_rotate_first_choice(self) -> None:
        self._seed(uid='mm_1')
        self._seed(uid='mm_2')
        firsts = [self.adapter._candidates('cn')[0]['uid'] for _ in range(4)]
        self.assertEqual(sorted(firsts), ['mm_1', 'mm_1', 'mm_2', 'mm_2'])

    # ── token 轮换 ────────────────────────────────────────
    def test_refresh_rotates_and_persists_before_use(self) -> None:
        self._seed(token='', expiredAt='')
        calls: list[str] = []

        async def fake_refresh(realm, refresh, **kwargs):
            calls.append(refresh)
            return {'access_token': 'new-access', 'refresh_token': 'new-refresh',
                    'expires_in': 3600}

        with mock.patch.object(client, 'refresh_token', new=fake_refresh):
            token = asyncio.run(self.adapter._token_for(
                store.read_account('mm_1'), proxy=None))
        self.assertEqual(token, 'new-access')
        self.assertEqual(calls, [_REFRESH])
        raw = store.read_account('mm_1')
        self.assertEqual(raw['refreshToken'], 'new-refresh')
        self.assertEqual(raw['token'], 'new-access')

    def test_valid_access_token_is_reused(self) -> None:
        self._seed()
        called = False

        async def fake_refresh(*args, **kwargs):
            nonlocal called
            called = True
            raise AssertionError('should not refresh')

        with mock.patch.object(client, 'refresh_token', new=fake_refresh):
            token = asyncio.run(self.adapter._token_for(
                store.read_account('mm_1'), proxy=None))
        self.assertEqual(token, _ACCESS)
        self.assertFalse(called)

    # ── 对话 ──────────────────────────────────────────────
    def test_chat_translates_and_clamps_max_tokens(self) -> None:
        self._seed()
        captured: dict = {}

        async def fake_chat(realm, token, body, **kwargs):
            captured['realm'] = realm
            captured['token'] = token
            captured.update(body)
            return {'id': 'msg_1', 'content': [{'type': 'text', 'text': 'ok'}],
                    'stop_reason': 'end_turn',
                    'usage': {'input_tokens': 1, 'output_tokens': 2}}

        with mock.patch.object(client, 'chat', new=fake_chat):
            result = asyncio.run(self.adapter.chat('MiniMax-M3', {
                'messages': [{'role': 'user', 'content': 'hi'}],
                'max_tokens': 1_000_000,
            }))
        self.assertEqual(captured['realm'], 'cn')
        self.assertEqual(captured['max_tokens'], C.OUTPUT_CAPS['MiniMax-M3'])
        self.assertEqual(result.model, 'minimaxcn/MiniMax-M3')
        self.assertEqual(result.realm, 'cn')
        self.assertEqual(result.payload['choices'][0]['message']['content'], 'ok')
        self.assertEqual(result.usage['completion_tokens'], 2)

    def test_chat_retries_once_after_401_with_rotated_token(self) -> None:
        self._seed()
        tokens: list[str] = []

        async def fake_refresh(realm, refresh, **kwargs):
            return {'access_token': 't-new', 'refresh_token': 'r-new',
                    'expires_in': 3600}

        async def fake_chat(realm, token, body, **kwargs):
            tokens.append(token)
            if token == _ACCESS:
                raise PlatformError('token 失效', status=401,
                                    code='login_expired')
            return {'id': 'msg_1', 'content': [{'type': 'text', 'text': 'ok'}],
                    'stop_reason': 'end_turn'}

        with mock.patch.object(client, 'refresh_token', new=fake_refresh), \
                mock.patch.object(client, 'chat', new=fake_chat):
            result = asyncio.run(self.adapter.chat(
                'MiniMax-M3', {'messages': [{'role': 'user', 'content': 'hi'}]}))
        self.assertEqual(tokens, [_ACCESS, 't-new'])
        self.assertEqual(result.payload['choices'][0]['message']['content'], 'ok')

    def test_streaming_relay_translates_sse(self) -> None:
        self._seed()

        class FakeResponse:
            def __init__(self) -> None:
                self.closed = False

            async def aiter_lines(self):
                for line in (
                    'event: message_start',
                    'data: {"type":"message_start","message":{"id":"msg_1",'
                    '"usage":{"input_tokens":2}}}',
                    '',
                    'data: {"type":"content_block_start","index":0,'
                    '"content_block":{"type":"text"}}',
                    '',
                    'data: {"type":"content_block_delta","index":0,'
                    '"delta":{"type":"text_delta","text":"hello"}}',
                    '',
                    'data: {"type":"message_delta",'
                    '"delta":{"stop_reason":"end_turn"},'
                    '"usage":{"output_tokens":3}}',
                    '',
                    'data: {"type":"message_stop"}',
                    '',
                ):
                    yield line

            async def aclose(self):
                self.closed = True

        response = FakeResponse()

        async def fake_open(*args, **kwargs):
            return response

        async def collect():
            result = await self.adapter.chat('MiniMax-M3', {
                'stream': True,
                'messages': [{'role': 'user', 'content': 'hi'}],
            })
            data = b''
            async for chunk in result.raw_stream:
                data += chunk
            return result, data

        with mock.patch.object(client, 'open_chat', new=fake_open):
            result, data = asyncio.run(collect())
        text = data.decode('utf-8')
        self.assertIn('"content": "hello"', text)
        self.assertIn('data: [DONE]', text)
        self.assertEqual(result.usage['completion_tokens'], 3)
        self.assertEqual(result.usage['prompt_tokens'], 2)
        self.assertIsNotNone(result.first_token_ms)
        self.assertTrue(response.closed)

    # ── 签到 / 积分 / 额度 ───────────────────────────────
    def test_checkin_claims_today_and_records_streak(self) -> None:
        self._seed()
        claimed: list[int] = []

        async def fake_status(realm, token, user_id, **kwargs):
            days = [{'day_no': i, 'status': 3 if i == 1 else (2 if i == 2 else 1),
                     'is_today': i == 2} for i in range(1, 8)]
            return {'days': days}

        async def fake_claim(realm, token, user_id, **kwargs):
            claimed.append(1)
            return {'claim_result': 1, 'points': 5}

        async def fake_ledger(realm, token, user_id, **kwargs):
            return [{'credit_type': 2, 'granted_amount': '100',
                     'remaining_amount': '80',
                     'expire_at_ms': (time.time() + 86400) * 1000}]

        with mock.patch.object(client, 'checkin_status', new=fake_status), \
                mock.patch.object(client, 'claim_checkin', new=fake_claim), \
                mock.patch.object(client, 'credit_details', new=fake_ledger):
            result = asyncio.run(self.adapter.checkin('mm_1'))
        self.assertTrue(result['ok'])
        self.assertTrue(result['checked_in'])
        self.assertFalse(result['already'])
        self.assertEqual(result['earned_credit'], 5)
        self.assertEqual(result['credits'], 80)
        self.assertEqual(result['streak'], 1)
        self.assertEqual(claimed, [1])
        self.assertEqual(store.read_account('mm_1')['checkin']['streak'], 1)

    def test_checkin_does_not_claim_when_already_done(self) -> None:
        self._seed()

        async def fake_status(realm, token, user_id, **kwargs):
            return {'days': [{'day_no': i, 'status': 3 if i == 1 else 1,
                              'is_today': i == 1} for i in range(1, 8)]}

        async def fail_claim(*args, **kwargs):
            raise AssertionError('already done should not claim')

        with mock.patch.object(client, 'checkin_status', new=fake_status), \
                mock.patch.object(client, 'claim_checkin', new=fail_claim), \
                mock.patch.object(client, 'credit_details', new=mock.AsyncMock(
                    return_value=[{'credit_type': 2, 'granted_amount': '10',
                                   'remaining_amount': '10',
                                   'expire_at_ms': (time.time() + 60) * 1000}])):
            result = asyncio.run(self.adapter.checkin('mm_1'))
        self.assertTrue(result['already'])
        self.assertEqual(result['earned_credit'], 0)

    def test_credits_sums_live_batches(self) -> None:
        self._seed()

        async def fake_ledger(realm, token, user_id, **kwargs):
            now_ms = time.time() * 1000
            return [
                {'credit_type': 2, 'granted_amount': '100',
                 'remaining_amount': '60', 'expire_at_ms': now_ms + 86_400_000},
                {'credit_type': 1, 'granted_amount': '1,200.5',
                 'remaining_amount': '-5', 'expire_at_ms': now_ms + 172_800_000},
                {'credit_type': 2, 'granted_amount': '9',
                 'remaining_amount': '9', 'expire_at_ms': now_ms - 1000},
            ]

        with mock.patch.object(client, 'credit_details', new=fake_ledger):
            out = asyncio.run(self.adapter.credits('mm_1'))
        self.assertEqual(out['credits'], 60)
        self.assertEqual(out['limit'], 1300.5)
        self.assertEqual(out['used'], 1240.5)
        self.assertEqual(out['unit'], 'credits')
        self.assertEqual(len(out['expires']), 1)

    def test_usage_collects_errors_without_raising(self) -> None:
        self._seed()

        async def fail(*args, **kwargs):
            raise PlatformError('登录态已失效', status=401,
                                code='login_expired')

        with mock.patch.object(client, 'credit_details', new=fail), \
                mock.patch.object(client, 'fetch_membership', new=fail):
            out = asyncio.run(self.adapter.usage('mm_1'))
        self.assertIsNone(out['session_usage'])
        codes = {item['code'] for item in out['errors']}
        self.assertIn('login_expired', codes)


if __name__ == '__main__':
    unittest.main()
