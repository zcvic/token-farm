"""Accio（阿里国际站 AI 电商工作台）：协议 / 目录 / 对话 / 积分 / 登录。

全部离线。这里钉住几条**实测过、接错就会静默出错**的事实：

  · `expiresAt` 上游是**秒**，必须归一成毫秒；写错量纲会让每个请求都去续期；
  · 对话的 `token` 在**请求体**里（放 `Authorization` 头恒回 HTTP 200 + 流内
    402）；`system_instruction` 是**字符串**，`tools` 是**扁平**形态且 schema
    字符串化；
  · 帧是顶层 `{content:{parts}}`，工具参数叫 `args_json`，且分片到达，必须跨帧
    合并（否则同一个工具会被执行 N 次）；
  · 失败分级：402 → 次日 24:00 冷却；429 → 1 小时；401/403 只换号**不冻号**；
  · 没有签到端点：`supports_checkin=False`，`checkin()` 必须明确报不支持。
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from server import config, db
from server.platforms.base import PlatformError
from server.platforms.accio import (
    catalog, chat, client, constants as C, login, protocol as P, store)
from server.platforms.accio.adapter import AccioAdapter
from server.platforms import settings as runtime_settings


def _sse(*frames: object) -> list[str]:
    return ['data: ' + json.dumps(item, ensure_ascii=False) for item in frames]


class _DbCase(unittest.TestCase):
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

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db


# ── 时间 / 区域 ─────────────────────────────────────────────
class ProtocolTest(unittest.TestCase):
    def test_expires_at_seconds_is_normalized_to_ms(self) -> None:
        self.assertEqual(P.expires_at_ms(1767225600), 1767225600000)
        # 已是毫秒的值保持不变（容忍上游某天改单位）。
        self.assertEqual(P.expires_at_ms(1767225600000), 1767225600000)

    def test_expires_at_rejects_garbage(self) -> None:
        for value in (None, '', 'x', 0, -1, True):
            self.assertIsNone(P.expires_at_ms(value))

    def test_realm_aliases(self) -> None:
        self.assertEqual(P.normalize_realm('intl'), 'global')
        self.assertEqual(P.normalize_realm('国际版'), 'global')
        self.assertEqual(P.normalize_realm(''), 'cn')
        self.assertEqual(P.login_url('global'),
                         'https://zh.accio.com/login')
        self.assertEqual(P.login_url('cn'), 'https://www.accio-ai.com/login')

    def test_pkce_pair_is_s256_and_urlsafe(self) -> None:
        verifier, challenge = P.pkce_pair()
        import base64
        import hashlib
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode('ascii')).digest()
        ).rstrip(b'=').decode()
        self.assertEqual(challenge, expected)
        self.assertTrue(verifier)
        self.assertNotIn('=', verifier)

    def test_auth_url_carries_pkce_parameters(self) -> None:
        url = P.build_auth_url('cn', 'http://127.0.0.1:9/auth/callback',
                               'state-x', 'challenge-y')
        self.assertTrue(url.startswith('https://www.accio-ai.com/login?'))
        self.assertIn('client_id=accio-work', url)
        self.assertIn('code_challenge_method=S256', url)
        self.assertIn('code_challenge=challenge-y', url)
        self.assertIn('state=state-x', url)
        self.assertIn('language=zh', url)
        self.assertIn('ttid=ttid_', url)


# ── 请求体 ──────────────────────────────────────────────────
class ChatBodyTest(unittest.TestCase):
    def _body(self, **kwargs):
        base = dict(model='1Drift', token='tok', messages=[
            {'role': 'system', 'content': 'SYS'},
            {'role': 'user', 'content': 'hi'},
        ], utdid='utdid-1', request='req-fixed')
        base.update(kwargs)
        return P.build_chat_body(**base)

    def test_token_is_in_body_not_header(self) -> None:
        body = self._body()
        self.assertEqual(body['token'], 'tok')
        headers = client.headers({'realm': 'cn', 'utdid': 'u'})
        self.assertNotIn('Authorization', headers)
        self.assertNotIn('token', {k.lower() for k in headers})

    def test_system_instruction_is_a_string(self) -> None:
        body = self._body()
        self.assertEqual(body['system_instruction'], 'SYS')
        # 空 system 时整个键都不存在（不能是 null / 空串）。
        body = self._body(messages=[{'role': 'user', 'content': 'hi'}])
        self.assertNotIn('system_instruction', body)

    def test_tools_are_flat_and_schema_is_a_string(self) -> None:
        body = self._body(tools=[{'type': 'function', 'function': {
            'name': 'read_file', 'description': 'd',
            'parameters': {'type': 'object', 'properties': {
                'filePath': {'type': 'string'}}}}}])
        tool = body['tools'][0]
        self.assertEqual(tool['name'], 'read_file')
        self.assertIsInstance(tool['parameters_json'], str)
        # 参数名是数据，不能被 snake 化。
        self.assertIn('filePath', tool['parameters_json'])
        self.assertNotIn('function_declarations', json.dumps(body))

    def test_empty_tools_key_is_omitted(self) -> None:
        self.assertNotIn('tools', self._body(tools=[]))

    def test_properties_subtree_keeps_mixed_key_shape(self) -> None:
        props = self._body()['properties']
        self.assertEqual(props['appKey'], 'OneChat')
        self.assertEqual(props['normalized_response'], 'true')
        self.assertEqual(props['utdid'], 'utdid-1')

    def test_contents_follow_roles(self) -> None:
        body = self._body(messages=[
            {'role': 'user', 'content': 'hi'},
            {'role': 'assistant', 'content': 'yo', 'tool_calls': [
                {'id': 'c1', 'type': 'function',
                 'function': {'name': 'f', 'arguments': '{"a":1}'}}]},
            {'role': 'tool', 'tool_call_id': 'c1', 'name': 'f',
             'content': '{"ok":true}'},
        ])
        contents = body['contents']
        self.assertEqual(contents[0], {'role': 'user', 'parts': [{'text': 'hi'}]})
        self.assertEqual(contents[1]['role'], 'model')
        self.assertEqual(contents[1]['parts'][0], {'text': 'yo'})
        self.assertEqual(contents[1]['parts'][1]['function_call']['args_json'],
                         '{"a":1}')
        self.assertEqual(contents[2]['role'], 'user')
        response = contents[2]['parts'][0]['function_response']
        self.assertEqual(response['id'], 'c1')
        self.assertEqual(response['response_json'], '{"ok":true}')

    def test_images_raise_instead_of_being_dropped(self) -> None:
        with self.assertRaises(P.UnsupportedContent):
            P.serialize_messages([{'role': 'user', 'content': [
                {'type': 'image_url', 'image_url': {'url': 'http://x'}}]}])

    def test_fixed_envelope_fields(self) -> None:
        body = self._body()
        for key, value in (('empid', ''), ('tenant', ''), ('iai_tag', ''),
                           ('stop_sequences', []), ('incremental', True)):
            self.assertEqual(body[key], value)
        self.assertTrue(body['message_id'])
        self.assertNotEqual(body['message_id'], body['request_id'])


# ── 错误分级 ────────────────────────────────────────────────
class FailureClassTest(unittest.TestCase):
    def test_http_status_matrix(self) -> None:
        cases = {
            401: P.KIND_AUTH,
            403: P.KIND_AUTH,
            402: P.KIND_QUOTA,
            429: P.KIND_RATE,
            400: P.KIND_INVALID,
            500: P.KIND_SERVER,
        }
        for status, kind in cases.items():
            failure = P.classify_failure(status, body={})
            self.assertIsNotNone(failure)
            self.assertEqual(failure.kind, kind, status)

    def test_403_with_html_is_permission_denied(self) -> None:
        failure = P.classify_failure(
            403, body=None, media_type='text/html;charset=UTF-8')
        self.assertEqual(failure.kind, P.KIND_PERMISSION)
        self.assertFalse(P.should_rotate(failure.kind))

    def test_429_keeps_retry_after(self) -> None:
        failure = P.classify_failure(429, body={}, retry_after='120')
        self.assertEqual(failure.retry_after, 120)

    def test_stream_error_frame(self) -> None:
        self.assertEqual(P.failure_for_stream('402', 'unauthorized').kind,
                         P.KIND_QUOTA)
        self.assertEqual(P.failure_for_stream('500', 'boom').kind,
                         P.KIND_SERVER)

    def test_error_code_200_is_success_sentinel(self) -> None:
        self.assertEqual(P.frame_error_of({'error_code': '200'}), ('', ''))
        self.assertEqual(P.frame_error_of({'error_code': 402}),
                         ('402', ''))

    def test_html_denial_detection(self) -> None:
        self.assertTrue(P.is_html_denial('text/html; charset=UTF-8'))
        self.assertFalse(P.is_html_denial('text/event-stream'))


# ── 请求头 ──────────────────────────────────────────────────
class HeadersTest(unittest.TestCase):
    def test_package_region_per_realm(self) -> None:
        self.assertEqual(
            client.headers({'realm': 'cn'})['x-package-region'], 'CN')
        self.assertEqual(
            client.headers({'realm': 'global'})['x-package-region'], 'GLOBAL')

    def test_optional_headers_are_omitted_when_empty(self) -> None:
        headers = client.headers({'realm': 'cn', 'teamId': '',
                                  'routeRegion': '', 'sgcookie': ''})
        for key in ('x-team-id', 'x-accio-route-region', 'sgcookie'):
            self.assertNotIn(key, headers)

    def test_optional_headers_are_sent_when_present(self) -> None:
        headers = client.headers({'realm': 'cn', 'teamId': 't1',
                                  'routeRegion': 'CN',
                                  'sgcookie': 'sg'})
        self.assertEqual(headers['x-team-id'], 't1')
        self.assertEqual(headers['x-accio-route-region'], 'CN')
        self.assertEqual(headers['sgcookie'], 'sg')
        self.assertEqual(headers['utdid'], store.utdid_for(
            {'realm': 'cn', 'teamId': 't1'}))


# ── 目录归一 ────────────────────────────────────────────────
class CatalogTest(unittest.TestCase):
    def test_slug_strips_multiplier_suffix(self) -> None:
        self.assertEqual(catalog.slugify('DeepSeek V4.1 Flash · x1.5'),
                         'deepseek-v4.1-flash')
        self.assertEqual(catalog.slugify('GLM-5.3 · 免费'), 'glm-5.3')

    def test_invisible_models_are_dropped(self) -> None:
        self.assertIsNone(catalog.normalize_one(
            {'modelCode': 'X', 'modelDisplayName': 'X', 'visible': False}))
        self.assertIsNone(catalog.normalize_one(
            {'modelCode': 'X', 'modelDisplayName': 'X'}))

    def test_multimodal_false_declares_text_only(self) -> None:
        item = catalog.normalize_one({
            'modelCode': 'X', 'modelDisplayName': 'Model X',
            'visible': True, 'multimodal': False})
        self.assertEqual(item['modalities'], ['text'])
        self.assertEqual(item['slug'], 'model-x')

    def test_reasoning_efforts_are_kept_or_single_default(self) -> None:
        item = catalog.normalize_one({
            'modelCode': 'X', 'modelDisplayName': 'M', 'visible': True,
            'reasoningEfforts': ['low', 'high'],
            'defaultReasoningEffort': 'high'})
        self.assertEqual(item['efforts'], ['low', 'high'])
        self.assertEqual(item['default_effort'], 'high')
        # null 档位 + 有默认值 ⇒ 单档。
        item = catalog.normalize_one({
            'modelCode': 'X', 'modelDisplayName': 'M', 'visible': True,
            'reasoningEfforts': None, 'defaultReasoningEffort': 'medium'})
        self.assertEqual(item['efforts'], ['medium'])
        # 默认值不在集合里 ⇒ 不声明默认档。
        item = catalog.normalize_one({
            'modelCode': 'X', 'modelDisplayName': 'M', 'visible': True,
            'reasoningEfforts': ['low'], 'defaultReasoningEffort': 'high'})
        self.assertIsNone(item['default_effort'])

    def test_parse_reads_data_model_list_and_sorts_by_multiple(self) -> None:
        payload = {'success': True, 'data': [{'modelList': [
            {'modelCode': 'B', 'modelDisplayName': 'B · x2',
             'visible': True, 'effectiveMultiple': 2},
            {'modelCode': 'A', 'modelDisplayName': 'A · x0.5',
             'visible': True, 'effectiveMultiple': 0.5},
            {'modelCode': 'C', 'modelDisplayName': 'C', 'visible': True},
        ]}]}
        models = catalog.parse(payload, realm='cn')
        self.assertEqual([m['slug'] for m in models], ['a', 'b', 'c'])

    def test_no_offline_snapshot(self) -> None:
        self.assertEqual(catalog.default_models(), [])


# ── 帧 → OpenAI chunk ───────────────────────────────────────
class TranslatorTest(unittest.TestCase):
    def test_tool_fragments_are_merged_into_one_call(self) -> None:
        frames = [
            {'content': {'parts': [{'function_call': {
                'id': 'c1', 'name': 'f'}}]}},
            {'content': {'parts': [{'function_call': {
                'id': 'c1', 'name': 'f', 'args_json': '{"a":'}}]}},
            {'content': {'parts': [{'function_call': {
                'id': 'c1', 'name': 'f', 'args_json': '{"a":1}'}}]}},
            {'content': {'role': 'model'}, 'finish_reason': 'STOP'},
        ]
        translator = chat.StreamTranslator('acciocn/m')
        chunks = []
        for frame in frames:
            chunks.extend(translator.feed(frame))
        chunks.extend(translator.finish())
        calls = [call for chunk in chunks
                 for choice in chunk.get('choices') or []
                 for call in (choice['delta'].get('tool_calls') or [])]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['function']['arguments'], '{"a":1}')
        self.assertEqual(calls[0]['id'], 'c1')

    def test_args_json_empty_string_falls_back_to_args_object(self) -> None:
        frames = [{'content': {'parts': [{'function_call': {
            'id': 'c1', 'name': 'f', 'args_json': '',
            'args': {'x': 1}}}]}}]
        translator = chat.StreamTranslator('m')
        for frame in frames:
            translator.feed(frame)
        completion = translator.completion()
        arguments = completion['choices'][0]['message']['tool_calls'][0][
            'function']['arguments']
        self.assertEqual(json.loads(arguments), {'x': 1})

    def test_usage_takes_the_largest_total(self) -> None:
        translator = chat.StreamTranslator('m')
        translator.feed({'content': {'parts': [{'text': 'a'}]},
                         'usage_metadata': {
                             'promptTokenCount': 1, 'candidatesTokenCount': 1,
                             'totalTokenCount': 2}})
        translator.feed({'content': {'parts': [{'text': 'b'}]},
                         'usage_metadata': {
                             'promptTokenCount': 3, 'candidatesTokenCount': 5,
                             'totalTokenCount': 8}})
        self.assertEqual(translator.usage_dict()['total_tokens'], 8)

    def test_finish_and_usage_only_frame_is_not_dropped(self) -> None:
        translator = chat.StreamTranslator('m')
        translator.feed({'content': {'parts': [{'text': 'hi'}]}})
        chunks = translator.feed({
            'content': {'role': 'model'}, 'finish_reason': 'MAX_TOKENS',
            'usage_metadata': {'promptTokenCount': 1,
                               'candidatesTokenCount': 2,
                               'totalTokenCount': 3}})
        chunks.extend(translator.finish())
        finish = [choice['finish_reason'] for chunk in chunks
                  for choice in chunk.get('choices') or []
                  if choice.get('finish_reason')]
        self.assertEqual(finish[-1], 'length')
        self.assertTrue(any(chunk.get('usage') for chunk in chunks))

    def test_stream_error_402_raises_quota_failure(self) -> None:
        translator = chat.StreamTranslator('m')
        with self.assertRaises(PlatformError) as ctx:
            translator.feed({'turn_complete': True, 'error_code': '402',
                             'error_message': 'unauthorized'})
        self.assertEqual(ctx.exception.status, 402)
        self.assertEqual(ctx.exception.code, 'QUOTA_EXCEEDED')

    def test_unnamed_tool_call_is_dropped_but_text_survives(self) -> None:
        translator = chat.StreamTranslator('m')
        chunks = translator.feed({'content': {'parts': [
            {'text': 'hello'},
            {'function_call': {'id': 'x'}},
        ]}})
        text = ''.join(choice['delta'].get('content') or ''
                       for chunk in chunks
                       for choice in chunk.get('choices') or [])
        self.assertEqual(text, 'hello')

    def test_aggregate_non_stream(self) -> None:
        result = chat.aggregate([{'content': {'parts': [{'text': 'hi'}]}},
                                 {'content': {'role': 'model'},
                                  'finish_reason': 'STOP',
                                  'usage_metadata': {
                                      'promptTokenCount': 1,
                                      'candidatesTokenCount': 1,
                                      'totalTokenCount': 2}}], 'm')
        self.assertEqual(result['object'], 'chat.completion')
        self.assertEqual(result['choices'][0]['message']['content'], 'hi')
        self.assertEqual(result['usage']['total_tokens'], 2)


class IterFramesTest(unittest.TestCase):
    def test_data_lines_without_blank_separator_are_each_parsed(self) -> None:
        class FakeResponse:
            async def aiter_lines(self):
                for line in ('data: {"a":1}', 'data: {"b":2}', ''):
                    yield line

        async def run():
            return [item async for item in client.iter_frames(FakeResponse())]

        self.assertEqual(asyncio.run(run()), [{'a': 1}, {'b': 2}])


# ── 账号文件 ────────────────────────────────────────────────
class StoreTest(_DbCase):
    def test_uid_is_stable_and_realm_scoped(self) -> None:
        first = store.derive_uid('cn', 'user-1')
        self.assertEqual(first, store.derive_uid('cn', 'user-1'))
        self.assertNotEqual(first, store.derive_uid('global', 'user-1'))
        self.assertTrue(first.startswith(store.UID_PREFIX))

    def test_epoch_roundtrip_through_iso(self) -> None:
        iso = store.iso_from_epoch(1767225600)
        self.assertAlmostEqual(store.epoch_from_iso(iso), 1767225600, delta=1)

    def test_save_account_is_atomic_and_leaves_no_temp_file(self) -> None:
        account = {'uid': 'ao_test', 'realm': 'cn',
                   'accessToken': 'AT', 'refreshToken': 'RT'}
        store.save_account(account)
        names = [p.name for p in store.accounts_dir().iterdir()]
        self.assertEqual(names, ['ao_test.json'])
        self.assertEqual(store.read_account('ao_test')['accessToken'], 'AT')

    def test_cooling_and_disabled_persist(self) -> None:
        store.save_account({'uid': 'ao_cool', 'realm': 'cn',
                            'accessToken': 'AT'})
        store.set_cooling('ao_cool', time.time() + 600, 'quota')
        store.set_disabled('ao_cool', True)
        raw = store.read_account('ao_cool')
        self.assertTrue(store.is_cooling(raw))
        self.assertTrue(store.is_disabled(raw))
        store.clear_cooling('ao_cool')
        self.assertFalse(store.is_cooling(store.read_account('ao_cool')))


# ── 适配器 ──────────────────────────────────────────────────
class AdapterTest(_DbCase):
    def setUp(self) -> None:
        super().setUp()
        self.adapter = AccioAdapter()

    def _import(self, **overrides) -> dict:
        payload = {
            'realm': 'cn',
            'access_token': 'AT-1',
            'refresh_token': 'RT-1',
            'expires_at': int(time.time()) + 3600,
            'user_id': 'u-1',
            'user_name': 'alice',
        }
        payload.update(overrides)
        return self.adapter.import_account(payload)

    def test_no_checkin_endpoint(self) -> None:
        self.assertFalse(AccioAdapter.supports_checkin)
        with self.assertRaises(PlatformError) as ctx:
            asyncio.run(self.adapter.checkin('ao_x'))
        self.assertEqual(ctx.exception.code, 'checkin_unsupported')

    def test_realm_and_fingerprint_are_persisted(self) -> None:
        public = self._import(realm='global')
        self.assertEqual(public['realm'], 'global')
        raw = store.read_account(public['uid'])
        self.assertEqual(raw['realm'], 'global')
        self.assertTrue(raw['utdid'])
        # 同一账号连续读两次，设备号不能变（变了会被当成换设备）。
        first = store.utdid_for(store.read_account(public['uid']))
        second = store.utdid_for(store.read_account(public['uid']))
        self.assertEqual(first, second)

    def test_import_rejects_missing_credentials(self) -> None:
        with self.assertRaises(PlatformError):
            self.adapter.import_account({'realm': 'cn', 'name': 'x'})

    def test_cookie_shaped_token_is_unwrapped(self) -> None:
        public = self._import(
            access_token='ac_token=AT-2; other=1', refresh_token='')
        raw = store.read_account(public['uid'])
        self.assertEqual(raw['accessToken'], 'AT-2')

    def test_token_for_refreshes_and_persists_new_values(self) -> None:
        public = self._import(expires_at=int(time.time()) - 10)
        seen = {}

        async def fake_refresh(account, **kwargs):
            seen['refresh'] = account.get('refreshToken')
            return {'access_token': 'AT-NEW', 'refresh_token': 'RT-NEW',
                    'expires_at': int(time.time() + 7200) * 1000}

        with mock.patch.object(client, 'refresh', new=fake_refresh):
            token = asyncio.run(self.adapter._token_for(
                store.read_account(public['uid']), proxy=None))
        self.assertEqual(token, 'AT-NEW')
        self.assertEqual(seen['refresh'], 'RT-1')
        raw = store.read_account(public['uid'])
        # 先落盘再使用：文件里已经是新令牌。
        self.assertEqual(raw['accessToken'], 'AT-NEW')
        self.assertEqual(raw['refreshToken'], 'RT-NEW')
        self.assertGreater(store.epoch_from_iso(raw['expiredAt']),
                           time.time() + 3600)

    def test_access_only_account_is_used_once_without_network(self) -> None:
        public = self._import(refresh_token='',
                              expires_at=int(time.time()) - 10)
        called = []

        async def fake_refresh(account, **kwargs):
            called.append(account)
            return {}

        with mock.patch.object(client, 'refresh', new=fake_refresh):
            token = asyncio.run(self.adapter._token_for(
                store.read_account(public['uid']), proxy=None))
        # 手工粘贴的 access token 没有 refresh：先按原样用一次，401 再换号。
        self.assertEqual(token, 'AT-1')
        self.assertFalse(called)

    def test_terminal_refresh_error_marks_account_expired(self) -> None:
        public = self._import(expires_at=int(time.time()) - 10)

        async def fake_refresh(account, **kwargs):
            raise PlatformError('invalid_grant', status=400,
                                code='invalid_grant')

        with mock.patch.object(client, 'refresh', new=fake_refresh):
            with self.assertRaises(PlatformError):
                asyncio.run(self.adapter._token_for(
                    store.read_account(public['uid']), proxy=None))
        self.assertEqual(store.read_account(public['uid'])['loginState'],
                         '登录已失效')

    def test_cool_down_grades(self) -> None:
        public = self._import()
        uid = public['uid']
        captured = {}

        def fake_cooling(account_id, until, reason=''):
            captured['until'] = until
            captured['reason'] = reason
            return {}

        with mock.patch.object(store, 'set_cooling', new=fake_cooling):
            quota = PlatformError('quota', status=402, code='QUOTA_EXCEEDED')
            self.assertTrue(self.adapter._cool_down(uid, quota))
            self.assertGreater(captured['until'], time.time() + 60)

            captured.clear()
            rate = PlatformError('rate', status=429, code='RATE_LIMIT')
            self.assertTrue(self.adapter._cool_down(uid, rate))
            self.assertAlmostEqual(captured['until'], time.time() + 3600,
                                   delta=60)

            captured.clear()
            auth = PlatformError('auth', status=401, code='AUTH')
            self.assertTrue(self.adapter._cool_down(uid, auth))
            # 401 / 403 只换号，绝不写冷却。
            self.assertEqual(captured, {})

            invalid = PlatformError('bad', status=400, code='INVALID_REQUEST')
            self.assertFalse(self.adapter._cool_down(uid, invalid))

    def test_selection_filters_realm_and_disabled(self) -> None:
        cn = self._import()
        other = self._import(realm='global', user_id='u-2')
        store.set_disabled(other['uid'], True)
        candidates = self.adapter._candidates('cn')
        self.assertEqual([a['uid'] for a in candidates], [cn['uid']])
        self.assertEqual(self.adapter._candidates('global'), [])

    def test_credits_reads_three_buckets_in_fixed_order(self) -> None:
        public = self._import()
        subscription = {
            'entitlement': {
                'referral': {'remaining': 5, 'total': 5, 'used': 0},
                'daily': {'remaining': 100, 'total': 200, 'used': 100},
                'monthly': {'remaining': 50, 'total': 50, 'used': 0},
            },
        }

        async def fake_sub(account, access, **kwargs):
            return subscription

        async def fake_quota(account, access, **kwargs):
            return {'usagePercent': 30, 'refreshCountdownSeconds': 3600}

        with mock.patch.object(client, 'subscription', new=fake_sub), \
                mock.patch.object(client, 'quota', new=fake_quota), \
                mock.patch.object(self.adapter, '_token_for',
                                  new=mock.AsyncMock(return_value='AT')):
            out = asyncio.run(self.adapter.credits(public['uid']))

        self.assertEqual([row['key'] for row in out['buckets']],
                         ['daily', 'monthly', 'referral'])
        self.assertEqual(out['credits'], 155)
        self.assertEqual(out['unit'], C.CREDIT_UNIT)
        # 只有每日桶知道到期时刻（来自 quota 倒计时）。
        self.assertEqual(len(out['expires']), 1)
        self.assertEqual(out['expires'][0]['label'], '每日积分')

    def test_credits_prefer_server_total(self) -> None:
        public = self._import()

        async def fake_sub(account, access, **kwargs):
            return {'remaining': 42, 'entitlement': {
                'daily': {'remaining': 10}}}

        async def fake_quota(account, access, **kwargs):
            return {}

        with mock.patch.object(client, 'subscription', new=fake_sub), \
                mock.patch.object(client, 'quota', new=fake_quota), \
                mock.patch.object(self.adapter, '_token_for',
                                  new=mock.AsyncMock(return_value='AT')):
            out = asyncio.run(self.adapter.credits(public['uid']))
        self.assertEqual(out['credits'], 42)

    def test_credits_skips_bucket_without_remaining(self) -> None:
        public = self._import()

        async def fake_sub(account, access, **kwargs):
            return {'entitlement': {'daily': {'total': 200},
                                    'monthly': {'remaining': 3}}}

        async def fake_quota(account, access, **kwargs):
            return {}

        with mock.patch.object(client, 'subscription', new=fake_sub), \
                mock.patch.object(client, 'quota', new=fake_quota), \
                mock.patch.object(self.adapter, '_token_for',
                                  new=mock.AsyncMock(return_value='AT')):
            out = asyncio.run(self.adapter.credits(public['uid']))
        self.assertEqual([row['key'] for row in out['buckets']], ['monthly'])
        self.assertEqual(out['credits'], 3)

    def test_upstream_model_reverse_lookup(self) -> None:
        db.set_setting('platforms.accio.models.cn', {
            'fetched_at': int(time.time()),
            'models': [{'id': '1Drift-ABC', 'slug': 'deepseek-v4.1-flash'}],
        })
        asyncio.run(self.adapter.list_models(refresh=False))
        self.assertEqual(
            self.adapter.upstream_model('deepseek-v4.1-flash'), '1Drift-ABC')
        self.assertEqual(self.adapter.upstream_model('unknown'), 'unknown')
        self.assertEqual(self.adapter.upstream_model(''), '')
        self.assertEqual(self.adapter.upstream_model('default'), '')

    def test_unknown_model_rejected_only_when_catalog_known(self) -> None:
        self.adapter._assert_known_model('anything', 'cn')  # 目录为空 → no-op
        db.set_setting('platforms.accio.models.cn', {
            'fetched_at': int(time.time()),
            'models': [{'id': 'X', 'slug': 'known'}],
        })
        with self.assertRaises(PlatformError) as ctx:
            self.adapter._assert_known_model('other', 'cn')
        self.assertEqual(ctx.exception.code, 'model_not_found')

    def test_cached_model_ids_are_prefixed(self) -> None:
        db.set_setting('platforms.accio.models.cn', {
            'fetched_at': int(time.time()),
            'models': [{'id': 'X', 'slug': 'known'}],
        })
        self.assertEqual(self.adapter.cached_model_ids(), {'acciocn/known'})

    def test_stream_chat_end_to_end(self) -> None:
        public = self._import()

        class FakeResponse:
            headers = {'content-type': 'text/event-stream'}

            async def aiter_lines(self):
                for line in _sse(
                        {'content': {'parts': [{'text': 'hel'}]}},
                        {'content': {'parts': [{'text': 'lo'}]}},
                        {'content': {'role': 'model'},
                         'finish_reason': 'STOP',
                         'usage_metadata': {
                             'promptTokenCount': 2,
                             'candidatesTokenCount': 1,
                             'totalTokenCount': 3}}):
                    yield line
                yield ''

            async def aclose(self):
                return None

        async def fake_open(account, access, payload, **kwargs):
            return FakeResponse()

        async def run():
            with mock.patch.object(client, 'open_chat', new=fake_open), \
                    mock.patch.object(self.adapter, '_token_for',
                                      new=mock.AsyncMock(return_value='AT')):
                result = await self.adapter.chat(
                    'known', {'messages': [{'role': 'user', 'content': 'hi'}],
                              'stream': True})
                chunks = [chunk async for chunk in result.stream]
                await result.aclose()
                return result, chunks

        result, chunks = asyncio.run(run())
        text = ''.join(choice['delta'].get('content') or ''
                       for chunk in chunks
                       for choice in chunk.get('choices') or [])
        self.assertEqual(text, 'hello')
        self.assertEqual(result.usage.get('total_tokens'), 3)
        self.assertEqual(result.account_hint, public['uid'])

    def test_non_stream_chat_returns_payload(self) -> None:
        self._import()

        class FakeResponse:
            headers = {'content-type': 'text/event-stream'}

            async def aiter_lines(self):
                for line in _sse(
                        {'content': {'parts': [{'text': 'hi'}]}},
                        {'content': {'role': 'model'},
                         'finish_reason': 'STOP'}):
                    yield line
                yield ''

            async def aclose(self):
                return None

        async def fake_open(account, access, payload, **kwargs):
            return FakeResponse()

        async def run():
            with mock.patch.object(client, 'open_chat', new=fake_open), \
                    mock.patch.object(self.adapter, '_token_for',
                                      new=mock.AsyncMock(return_value='AT')):
                return await self.adapter.chat(
                    'known', {'messages': [{'role': 'user', 'content': 'hi'}],
                              'stream': False})

        result = asyncio.run(run())
        self.assertEqual(result.payload['choices'][0]['message']['content'],
                         'hi')


# ── 登录 ────────────────────────────────────────────────────
class LoginTest(unittest.TestCase):
    def test_callback_url_parses_code_and_state(self) -> None:
        params = login.parse_callback_url(
            'http://127.0.0.1:4321/auth/callback?code=abc&state=def')
        self.assertEqual(params['code'], 'abc')
        self.assertEqual(params['state'], 'def')

    def test_callback_url_rejects_foreign_host(self) -> None:
        with self.assertRaises(PlatformError):
            login.parse_callback_url('http://evil.example/auth/callback?code=a')

    def test_state_must_match_exactly(self) -> None:
        self.assertTrue(login._state_matches({'oauthState': 'abc'}, 'abc'))
        self.assertFalse(login._state_matches({'oauthState': 'abc'}, 'abd'))
        self.assertFalse(login._state_matches({'oauthState': ''}, ''))


class SettingsTest(unittest.TestCase):
    def test_capabilities_match_the_implemented_switches(self) -> None:
        caps = runtime_settings.capabilities('accio')
        self.assertTrue(caps['max_accounts_try'])
        self.assertTrue(caps['session_affinity'])
        self.assertTrue(caps['catalog_timeout'])
        self.assertTrue(caps['credits_ttl'])
        # 冷却策略固定，不放出改了也不生效的假开关。
        self.assertFalse(caps['cooldown_429'])
        self.assertFalse(caps['cooldown_5xx'])
        self.assertFalse(caps['min_credits'])
        self.assertFalse(caps['credit_meter'])
        self.assertFalse(caps['desktop_sync'])

    def test_prefix_routing(self) -> None:
        from server.platforms import registry
        self.assertEqual(registry.route_model('acciocn/x'), ('accio', 'x', 'cn'))
        self.assertEqual(registry.route_model('acciocom/x'),
                         ('accio', 'x', 'global'))
        self.assertEqual(registry.route_model('accio/x'), ('accio', 'x', None))


if __name__ == '__main__':
    unittest.main()
