"""豆包（www.doubao.com）平台：账号存储、请求体构造、SSE 翻译与适配器策略。

全部离线：上游 HTTP 一律用 `httpx.MockTransport` 打桩。这里钉住几件容易回归
的事：

  · Cookie 只落盘、不出圈：对外结构只有摘要与尾部，明文只在 `export_accounts`；
  · `model_item_key` 必须同时出现在请求体的三处（只改一处上游会按默认模型答）；
  · 上游 SSE 的两种正文形状（`CHUNK_DELTA` 增量 / `STREAM_CHUNK` 快照）都要能
    翻成 OpenAI chunk，且 `SSE_REPLY_END.brief` 只在没有正文时兜底；
  · 上游不返回 usage：`completion_tokens` 是「字符 ÷ 1.7」的**估算值**；
  · 额度汇总要能穿透 `window_limits` 并清洗千分位 / 货币符号；
  · 流中途出错要**抛出去**（`platforms/sse` 据此补错误帧并记 502），不能安静
    地以 200 正常结束。
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
from server.platforms.doubao import catalog, chat, client, constants as C, store
from server.platforms.doubao.adapter import DoubaoAdapter
from server.platforms.sse import DONE, openai_sse

_COOKIE = ('sessionid=abc123def456; sid_tt=xyz789; ttwid=tt-001; '
           's_v_web_id=verify_1; odin_tt=odin-1; passport_csrf_token=csrf-1')


def _sse(*frames: tuple[str, dict]) -> bytes:
    out = []
    for event, payload in frames:
        out.append(f'event: {event}\ndata: '
                   f'{json.dumps(payload, ensure_ascii=False)}\n\n')
    return ''.join(out).encode('utf-8')


class _FakeResponse:
    """最小上游响应：只需要 `aiter_lines` / `text` / `aclose`。"""

    def __init__(self, body: bytes, *, status: int = 200,
                 content_type: str = 'text/event-stream') -> None:
        self._body = body
        self.status_code = status
        self.headers = {'content-type': content_type}
        self.closed = False
        self.text = body.decode('utf-8', 'replace')

    async def aiter_lines(self):
        for line in self.text.split('\n'):
            yield line.rstrip('\r')

    async def aread(self) -> bytes:
        return self._body

    async def aclose(self) -> None:
        self.closed = True


def _client(reply) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(reply))


# ── 账号存储 ────────────────────────────────────────────────
class DoubaoStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(config, 'DATA_DIR', Path(self.tmp.name))
        patch.start()
        self.addCleanup(patch.stop)

    def test_normalize_cookie_strips_prefix_and_newlines(self) -> None:
        raw = 'Cookie: sessionid=a;  sid_tt=b\r\nttwid=c;\n\n'
        self.assertEqual(store.normalize_cookie(raw),
                         'sessionid=a; sid_tt=b; ttwid=c')

    def test_has_login_accepts_any_known_field(self) -> None:
        self.assertTrue(store.has_login('ttwid=only-one'))
        self.assertFalse(store.has_login('foo=1; bar=2'))
        self.assertFalse(store.has_login(''))

    def test_uid_is_stable_and_cookie_never_leaves_the_process(self) -> None:
        first = store.derive_uid(_COOKIE)
        second = store.derive_uid('sessionid=abc123def456; other=changed')
        self.assertEqual(first, second, '同一 sessionid 必须命中同一条账号')
        self.assertTrue(first.startswith('db_'))

        saved = store.save_account({'uid': first, 'cookie': _COOKIE})
        public = store.public_account(saved)
        self.assertNotIn('cookie', public)
        self.assertTrue(public['has_token'])
        self.assertTrue(public['cookie_summary']['has_sessionid'])
        self.assertEqual(public['cookie_summary']['sessionid_tail'], 'def456')
        self.assertTrue(public['token_preview'].startswith('…'))
        self.assertNotIn('abc123def456', json.dumps(public, ensure_ascii=False))

    def test_device_id_is_derived_and_stable(self) -> None:
        account = {'cookie': _COOKIE}
        first = store.device_of(account)
        self.assertEqual(first, store.device_of({'cookie': _COOKIE}))
        self.assertEqual(store.device_of({'cookie': _COOKIE, 'deviceId': 'dev-1'}),
                         'dev-1')

    def test_public_account_reports_checkin_and_quota(self) -> None:
        saved = store.save_account({
            'uid': 'db_1', 'cookie': _COOKIE,
            'checkin': {'last_at': 1700000000, 'last_result': 'ok',
                        'continuous_days': 3, 'reward': '10 积分'},
            'quota': {'state': 'ok', 'at': 1700000001, 'remaining': 12.5},
        })
        public = store.public_account(saved)
        self.assertEqual(public['checkin']['last_result'], 'ok')
        self.assertEqual(public['checkin']['continuous_days'], 3)
        self.assertEqual(public['quota']['remaining'], 12.5)
        self.assertEqual(public['realm'], 'cn')


# ── 请求体 ──────────────────────────────────────────────────
class DoubaoBuildBodyTest(unittest.TestCase):
    def _body(self, **kwargs):
        model = kwargs.pop('model', 'deepseek-v4-flash')
        entry = C.model_entry(model)
        return chat.build_body(
            kwargs.pop('messages', [{'role': 'user', 'content': '你好'}]),
            C.upstream_model_key(model),
            entry, account={'deviceId': 'dev-1'}, **kwargs)

    def test_model_key_appears_in_all_three_places(self) -> None:
        body = self._body(model='kimi-k3')
        key = 'person-kimi-k3-261111'
        option = body['option']
        self.assertEqual(option['conversation_init_ext']['model_item_key'], key)
        self.assertEqual(option['model_config']['model_item_key'], key)
        self.assertEqual(option['aggregate_params']['model_item_key'], key)

    def test_model_params_follow_the_catalogue(self) -> None:
        body = self._body(model='auto')
        extra = body['option']['model_config']['model_extra_params']
        self.assertEqual(extra['memory_profile'], '256k')
        self.assertEqual(extra['total_window_size'], '256000')

    def test_history_is_flattened_into_the_last_user_turn(self) -> None:
        body = self._body(messages=[
            {'role': 'system', 'content': '你是助手'},
            {'role': 'user', 'content': '第一问'},
            {'role': 'assistant', 'content': '第一答'},
            {'role': 'user', 'content': '第二问'},
        ])
        text = body['messages'][0]['content_block'][0]['content']['text_block']['text']
        self.assertIn('系统：你是助手', text)
        self.assertIn('用户：第一问', text)
        self.assertIn('助手：第一答', text)
        self.assertTrue(text.endswith('用户：第二问'))
        self.assertEqual(text.count('第二问'), 1, '最后一条 user 不能重复')

    def test_missing_user_message_is_a_400(self) -> None:
        with self.assertRaises(PlatformError) as ctx:
            self._body(messages=[{'role': 'system', 'content': 'x'}])
        self.assertEqual(ctx.exception.status, 400)

    def test_effort_accepts_digits_only(self) -> None:
        self.assertEqual(self._body(effort='2')['option']['model_config']
                         ['reasoning_effort'], 2)
        default = int(C.REASONING_EFFORT)
        self.assertEqual(self._body(effort='high')['option']['model_config']
                         ['reasoning_effort'], default)

    def test_work_mode_fields_are_present_and_sandbox_is_off(self) -> None:
        option = self._body()['option']
        self.assertEqual(option['agent_mode'], 1)
        self.assertEqual(option['conversation_mode'], 2)
        client_option = option['general_task_param']['client_option']
        self.assertFalse(client_option['enable_sandbox'],
                         'headless 场景必须关沙箱，否则上游要求本地沙箱')
        self.assertEqual(option['ext']['use_deep_think'], '10001')


# ── SSE 翻译 ────────────────────────────────────────────────
class DoubaoTranslatorTest(unittest.TestCase):
    def test_chunk_delta_streams_content(self) -> None:
        tr = chat.StreamTranslator('doubao/x')
        tr.start()
        chunks = tr.feed('CHUNK_DELTA', {'text': '你'})
        chunks += tr.feed('CHUNK_DELTA', {'text': '好'})
        self.assertEqual(''.join(c['choices'][0]['delta'].get('content', '')
                                 for c in chunks), '你好')
        tr.feed('SSE_REPLY_END', {'end_type': 1, 'msg_finish_attr': {'brief': '你好'}})
        self.assertEqual(tr.text, '你好', 'brief 不能与正文重复')

    def test_stream_chunk_snapshot_only_yields_the_delta(self) -> None:
        tr = chat.StreamTranslator('doubao/x')
        frame = {'patch_op': [{'patch_value': {'content_block': [
            {'block_type': 10000,
             'content': {'text_block': {'text': '你好世界'}}}]}}]}
        first = tr.feed('STREAM_CHUNK', frame)
        second = tr.feed('STREAM_CHUNK', frame)
        self.assertEqual(first[0]['choices'][0]['delta']['content'], '你好世界')
        self.assertEqual(second, [], '同一份快照不能重复发')
        self.assertEqual(tr.text, '你好世界')

    def test_thinking_block_becomes_reasoning_content(self) -> None:
        tr = chat.StreamTranslator('doubao/x')
        frame = {'patch_op': [{'patch_value': {'content_block': [
            {'block_type': 10040,
             'content': {'thinking_block': {'streaming_title': '思考中'}}}]}}]}
        chunks = tr.feed('STREAM_CHUNK', frame)
        self.assertEqual(chunks[0]['choices'][0]['delta']['reasoning_content'],
                         '思考中')
        self.assertEqual(tr.reasoning, '思考中')

    def test_brief_is_used_only_when_there_is_no_body(self) -> None:
        tr = chat.StreamTranslator('doubao/x')
        chunks = tr.feed('SSE_REPLY_END',
                         {'end_type': 2, 'msg_finish_attr': {'brief': '兜底答案'}})
        self.assertEqual(chunks[0]['choices'][0]['delta']['content'], '兜底答案')
        self.assertEqual(tr.text, '兜底答案')

    def test_usage_is_estimated_from_characters(self) -> None:
        tr = chat.StreamTranslator('doubao/x', prompt_chars=17)
        tr.feed('CHUNK_DELTA', {'text': 'x' * 34})
        usage = tr.usage_dict()
        self.assertEqual(usage['completion_tokens'], int(34 / C.CHARS_PER_TOKEN))
        self.assertEqual(usage['prompt_tokens'], int(17 / C.CHARS_PER_TOKEN))
        self.assertEqual(usage['total_tokens'],
                         usage['prompt_tokens'] + usage['completion_tokens'])

    def test_inline_error_raises(self) -> None:
        tr = chat.StreamTranslator('doubao/x')
        with self.assertRaises(chat.StreamError):
            tr.feed('CHUNK_DELTA', {'error': {'message': '额度不足',
                                              'code': 'quota'}})


# ── 协议层 ──────────────────────────────────────────────────
class DoubaoClientTest(unittest.TestCase):
    def _account(self) -> dict:
        return {'uid': 'db_1', 'cookie': _COOKIE, 'deviceId': 'dev-1'}

    def test_iter_events_joins_multiline_data_and_skips_noise(self) -> None:
        response = _FakeResponse(
            b': comment\n\n'
            b'event: CHUNK_DELTA\ndata: {"text":"hi"}\n\n'
            b'event: STREAM_CHUNK\ndata: {"patch_op":\ndata: []}\n\n'
            b'event: X\ndata: [DONE]\n\n')

        async def collect():
            return [item async for item in client.iter_events(response)]

        events = asyncio.run(collect())
        self.assertEqual(events[0], ('CHUNK_DELTA', {'text': 'hi'}))
        self.assertEqual(events[1], ('STREAM_CHUNK', {'patch_op': []}))

    def test_open_chat_raises_401_for_json_login_error(self) -> None:
        def reply(request):
            return httpx.Response(200, json={'code': 1001, 'message': '未登录'})

        async def run():
            with mock.patch.object(config, 'http_client',
                                   lambda *a, **k: _client(reply)):
                await client.open_chat(self._account(), {})

        with self.assertRaises(PlatformError) as ctx:
            asyncio.run(run())
        self.assertEqual(ctx.exception.status, 401)
        self.assertEqual(ctx.exception.code, 'login_expired')

    def test_open_chat_maps_429_and_5xx(self) -> None:
        async def run(status):
            def reply(request):
                return httpx.Response(status, json={'message': 'busy'})

            with mock.patch.object(config, 'http_client',
                                   lambda *a, **k: _client(reply)):
                await client.open_chat(self._account(), {})

        with self.assertRaises(PlatformError) as ctx:
            asyncio.run(run(429))
        self.assertEqual(ctx.exception.status, 429)
        with self.assertRaises(PlatformError) as ctx:
            asyncio.run(run(503))
        self.assertEqual(ctx.exception.status, 502)

    def test_open_chat_sends_device_query_and_headers(self) -> None:
        seen: dict = {}

        def reply(request):
            seen['url'] = request.url
            seen['headers'] = request.headers
            return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                                  content=b'event: CHUNK_DELTA\ndata: {"text":"x"}\n\n')

        async def run():
            with mock.patch.object(config, 'http_client',
                                   lambda *a, **k: _client(reply)):
                response = await client.open_chat(self._account(), {'messages': []})
                await response.aclose()

        asyncio.run(run())
        query = dict(seen['url'].params)
        self.assertEqual(query['device_id'], 'dev-1')
        self.assertEqual(query['fp'], 'verify_dev-1')
        self.assertEqual(query['tea_uuid'], 'dev-1')
        self.assertEqual(query['aid'], C.QUERY['aid'])
        self.assertIn('web_tab_id', query)
        self.assertEqual(seen['headers']['cookie'], _COOKIE)

    def test_checkin_parses_ok_already_and_no_campaign(self) -> None:
        def make(payload):
            def reply(request):
                return httpx.Response(200, json=payload)
            return reply

        async def run(payload):
            with mock.patch.object(config, 'http_client',
                                   lambda *a, **k: _client(make(payload))):
                return await client.checkin(self._account())

        ok = asyncio.run(run({'code': 0, 'data': {
            'checked_in': True, 'reward_amount': 30, 'continuous_days': 4}}))
        self.assertTrue(ok['ok'])
        self.assertFalse(ok['already'])
        self.assertEqual(ok['reward_amount'], 30)
        self.assertEqual(ok['days'], 4)

        already = asyncio.run(run({'code': 0, 'data': {'idempotent': True}}))
        self.assertTrue(already['already'])
        self.assertEqual(already['reward_amount'], 0)

        none = asyncio.run(run({'code': 710010702, 'message': '无活动'}))
        self.assertFalse(none['ok'])
        self.assertEqual(none['code'], 'no_campaign')

    def test_quota_sums_window_limits_and_cleans_amounts(self) -> None:
        payload = {'code': 0, 'data': {
            'feature_group_name': 'root',
            'children': [{
                'feature_group_name': '活动额度',
                'window_limits': [
                    {'total_amount': '1,000', 'used_amount': '250',
                     'used_percent': 25, 'end_time': 1800000000},
                    {'total_amount': '¥500.5', 'used_amount': '0.5',
                     'used_percent': 0.1, 'end_time': 1800000001},
                ],
            }],
        }}

        def reply(request):
            return httpx.Response(200, json=payload)

        async def run():
            with mock.patch.object(config, 'http_client',
                                   lambda *a, **k: _client(reply)):
                return await client.quota(self._account())

        out = asyncio.run(run())
        self.assertAlmostEqual(out['total'], 1500.5, places=4)
        self.assertAlmostEqual(out['used'], 250.5, places=4)
        self.assertAlmostEqual(out['remaining'], 1250.0, places=4)
        self.assertEqual(len(out['groups']), 2)
        self.assertEqual(out['groups'][0]['name'], '活动额度')

    def test_quota_empty_payload_is_zeroed(self) -> None:
        def reply(request):
            return httpx.Response(200, json={'code': 0, 'data': {}})

        async def run():
            with mock.patch.object(config, 'http_client',
                                   lambda *a, **k: _client(reply)):
                return await client.quota(self._account())

        out = asyncio.run(run())
        self.assertEqual(out['total'], 0.0)
        self.assertEqual(out['percent'], 0.0)


# ── 适配器 ──────────────────────────────────────────────────
class DoubaoAdapterTest(unittest.TestCase):
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
        self.adapter = DoubaoAdapter()

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db

    def _seed(self, **overrides) -> dict:
        uid = str(overrides.pop('uid', 'db_1'))
        data = {'uid': uid, 'cookie': overrides.pop('cookie', _COOKIE)}
        data.update(overrides)
        store.save_account(data)
        return data

    # ── 导入 / 探活 ───────────────────────────────────────
    def test_import_account_derives_uid_and_hides_cookie(self) -> None:
        public = self.adapter.import_account({'cookie': _COOKIE})
        self.assertTrue(public['uid'].startswith('db_'))
        self.assertTrue(public['has_token'])
        self.assertEqual(store.read_account(public['uid'])['cookie'], _COOKIE)
        self.assertEqual(public['cookie_summary']['sessionid_tail'], 'def456')
        # 设备字段默认带桌面端 app id。
        self.assertEqual(store.read_account(public['uid'])['localAppId'],
                         C.DEFAULT_LOCAL_APP_ID)

    def test_import_rejects_cookie_without_login_state(self) -> None:
        with self.assertRaises(PlatformError) as ctx:
            self.adapter.import_account({'cookie': 'foo=1; bar=2'})
        self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(PlatformError):
            self.adapter.import_account({'nickname': 'x'})

    def test_refresh_account_records_probe_state(self) -> None:
        self._seed()

        async def fake_probe(account, **kwargs):
            return {'state': 'ok', 'message': ''}

        with mock.patch.object(client, 'probe', new=fake_probe):
            public = asyncio.run(self.adapter.refresh_account('db_1'))
        self.assertEqual(public['login_state'], '正常')

    def test_refresh_account_raises_401_when_cookie_invalid(self) -> None:
        self._seed()

        async def fake_probe(account, **kwargs):
            return {'state': 'invalid', 'message': 'Cookie 无效'}

        with mock.patch.object(client, 'probe', new=fake_probe):
            with self.assertRaises(PlatformError) as ctx:
                asyncio.run(self.adapter.refresh_account('db_1'))
        self.assertEqual(ctx.exception.status, 401)
        self.assertEqual(store.read_account('db_1')['loginState'], '登录已失效')

    # ── 模型目录 ──────────────────────────────────────────
    def test_list_models_is_offline_and_prefixed(self) -> None:
        models = asyncio.run(self.adapter.list_models())
        ids = {m['id'] for m in models}
        self.assertIn('doubao/deepseek-v4-flash', ids)
        self.assertNotIn('doubao/glm-5.3', ids, '上游报错的模型不进目录')
        self.assertTrue(all(m['realm'] == 'cn' for m in models))
        self.assertTrue(all(m['attachment'] is False for m in models))
        self.assertIn('doubao/auto', self.adapter.cached_model_ids())
        self.assertEqual(catalog.default_models()[0]['id'], 'auto')

    def test_upstream_model_translates_alias_and_passthrough(self) -> None:
        self.assertEqual(self.adapter.upstream_model('doubao'),
                         'person-deepseek-v4-1-flash-261017')
        self.assertEqual(self.adapter.upstream_model('glm-5.3'),
                         'glm-5.3', '未收录的 key 原样透传')

    # ── 选号 / 冷却 ───────────────────────────────────────
    def test_candidates_skip_disabled_checkin_only_and_cooling(self) -> None:
        self._seed(uid='db_1')
        self._seed(uid='db_2', disabled=True)
        self._seed(uid='db_3', coolingUntil=time.time() + 600)
        self._seed(uid='db_4', checkinOnly=True)
        ids = [a['uid'] for a in self.adapter._candidates()]
        self.assertEqual(ids, ['db_1'])

    def test_weights_rotate_first_choice(self) -> None:
        self._seed(uid='db_1', weight=1)
        self._seed(uid='db_2', weight=1)
        firsts = [self.adapter._candidates()[0]['uid'] for _ in range(4)]
        self.assertEqual(sorted(firsts), ['db_1', 'db_1', 'db_2', 'db_2'])

    def test_429_cools_the_account(self) -> None:
        self._seed()
        db.set_setting('platforms.doubao.cooldown_429', 60)

        async def fake_open(*args, **kwargs):
            raise PlatformError('busy', status=429, code='rate_limited')

        with mock.patch.object(client, 'open_chat', new=fake_open):
            with self.assertRaises(PlatformError) as ctx:
                asyncio.run(self.adapter.chat('auto', {
                    'messages': [{'role': 'user', 'content': 'hi'}]}))
        self.assertEqual(ctx.exception.status, 429)
        raw = store.read_account('db_1')
        self.assertGreater(raw.get('coolingUntil') or 0, time.time())

    def test_401_does_not_cool_but_switches_account(self) -> None:
        self._seed(uid='db_1')
        self._seed(uid='db_2')
        seen: list[str] = []

        async def fake_open(account, body, **kwargs):
            seen.append(str(account.get('uid')))
            if len(seen) == 1:
                raise PlatformError('登录已失效', status=401, code='login_expired')
            return _FakeResponse(_sse(('CHUNK_DELTA', {'text': 'ok'})))

        with mock.patch.object(client, 'open_chat', new=fake_open):
            result = asyncio.run(self.adapter.chat('auto', {
                'messages': [{'role': 'user', 'content': 'hi'}]}))
        self.assertEqual(result.payload['choices'][0]['message']['content'], 'ok')
        self.assertEqual(len(seen), 2)
        # 401 是凭证问题：只换号，不冻结（冻了也不会自己好）。
        for uid in ('db_1', 'db_2'):
            self.assertFalse(store.read_account(uid).get('coolingUntil'))

    # ── 对话 ──────────────────────────────────────────────
    def test_chat_nonstream_aggregates_sse(self) -> None:
        self._seed()
        captured: dict = {}

        async def fake_open(account, body, **kwargs):
            captured['body'] = body
            return _FakeResponse(_sse(
                ('CHUNK_DELTA', {'text': '你'}),
                ('CHUNK_DELTA', {'text': '好'}),
                ('SSE_REPLY_END', {'end_type': 1,
                                   'msg_finish_attr': {'brief': '你好'}})))

        with mock.patch.object(client, 'open_chat', new=fake_open):
            result = asyncio.run(self.adapter.chat('kimi-k3', {
                'messages': [{'role': 'user', 'content': 'hi'}]}))
        self.assertEqual(result.payload['object'], 'chat.completion')
        self.assertEqual(result.payload['choices'][0]['message']['content'], '你好')
        self.assertEqual(result.model, 'doubao/kimi-k3')
        self.assertEqual(result.realm, 'cn')
        self.assertGreater(result.usage['completion_tokens'], 0)
        self.assertEqual(captured['body']['option']['model_config']
                         ['model_item_key'], 'person-kimi-k3-261111')

    def test_chat_stream_yields_openai_chunks(self) -> None:
        self._seed()

        async def fake_open(account, body, **kwargs):
            return _FakeResponse(_sse(
                ('CHUNK_DELTA', {'text': 'hi'}),
                ('SSE_REPLY_END', {'end_type': 1})))

        async def collect():
            result = await self.adapter.chat('auto', {
                'stream': True,
                'messages': [{'role': 'user', 'content': 'hi'}]})
            chunks = [c async for c in result.stream]
            return result, chunks

        with mock.patch.object(client, 'open_chat', new=fake_open):
            result, chunks = asyncio.run(collect())
        self.assertEqual(chunks[0]['choices'][0]['delta']['role'], 'assistant')
        text = ''.join(c['choices'][0]['delta'].get('content', '') if c['choices']
                       else '' for c in chunks)
        self.assertEqual(text, 'hi')
        self.assertEqual(chunks[-2]['choices'][0]['finish_reason'], 'stop')
        self.assertEqual(result.usage['completion_tokens'],
                         int(2 / C.CHARS_PER_TOKEN) or 1)

    def test_chat_nonstream_empty_output_is_an_error(self) -> None:
        self._seed()

        async def fake_open(account, body, **kwargs):
            return _FakeResponse(_sse(('SSE_REPLY_END', {'end_type': 1})))

        with mock.patch.object(client, 'open_chat', new=fake_open):
            with self.assertRaises(PlatformError) as ctx:
                asyncio.run(self.adapter.chat('auto', {
                    'messages': [{'role': 'user', 'content': 'hi'}]}))
        self.assertEqual(ctx.exception.code, 'empty_response')

    def test_stream_error_is_raised_after_the_frames_are_sent(self) -> None:
        """上游中途报错时，流必须**抛出去**（而不是安静地正常结束）。

        只设 `chat.error` 不抛的话，客户端会收到一条「正常结束但没有正文」的
        流，日志里还记 200 —— 故障被藏起来了。`platforms/sse.openai_sse` 接到
        异常会补一帧 error 并记 502。
        """
        self._seed()

        async def fake_open(account, body, **kwargs):
            return _FakeResponse(_sse(
                ('CHUNK_DELTA', {'text': 'hi'}),
                ('CHUNK_DELTA', {'error': {'message': '额度不足',
                                          'code': 'quota'}})))

        async def collect():
            result = await self.adapter.chat('auto', {
                'stream': True,
                'messages': [{'role': 'user', 'content': 'hi'}]})
            return result, [c async for c in result.stream]

        with mock.patch.object(client, 'open_chat', new=fake_open):
            with self.assertRaises(PlatformError) as ctx:
                asyncio.run(collect())
        self.assertEqual(ctx.exception.status, 502)
        self.assertIn('额度不足', ctx.exception.message)

    def test_stream_network_error_becomes_502(self) -> None:
        """流中途断网：原始 httpx 异常要翻成 502 的 `network_error`。"""
        self._seed()

        class _BrokenResponse(_FakeResponse):
            async def aiter_lines(self):
                yield 'event: CHUNK_DELTA'
                yield 'data: {"text":"hi"}'
                yield ''
                raise httpx.ReadError('peer closed')

        async def fake_open(account, body, **kwargs):
            return _BrokenResponse(b'')

        async def collect():
            result = await self.adapter.chat('auto', {
                'stream': True,
                'messages': [{'role': 'user', 'content': 'hi'}]})
            return [c async for c in result.stream]

        with mock.patch.object(client, 'open_chat', new=fake_open):
            with self.assertRaises(PlatformError) as ctx:
                asyncio.run(collect())
        self.assertEqual(ctx.exception.status, 502)
        self.assertEqual(ctx.exception.code, 'network_error')

    def test_stream_error_reaches_the_sse_layer_as_502(self) -> None:
        """端到端：适配器抛出的错 → `openai_sse` 补一帧 error 并记 502。

        这是「故障不会被藏起来」的那一环：只设 `chat.error` 不抛的话，客户端
        收到一条正常结束的空流，日志里也还是 200。
        """
        self._seed()

        async def fake_open(account, body, **kwargs):
            return _FakeResponse(_sse(
                ('CHUNK_DELTA', {'text': 'hi'}),
                ('CHUNK_DELTA', {'error': {'message': '额度不足',
                                          'code': 'quota'}})))

        calls: list[tuple] = []

        async def run():
            with mock.patch.object(client, 'open_chat', new=fake_open):
                outcome = await self.adapter.chat('auto', {
                    'stream': True,
                    'messages': [{'role': 'user', 'content': 'hi'}]})
                return [frame async for frame in openai_sse(
                    outcome, started=0.0, on_finish=lambda *a: calls.append(a))]

        frames = asyncio.run(run())
        self.assertNotIn(DONE, frames, '错误流不能发 [DONE]：客户端会当成正常结束')
        error = json.loads(frames[-1][6:].decode('utf-8'))['error']
        self.assertIn('额度不足', error['message'])
        self.assertEqual(calls[0][0], 502, '出错必须记 502，不能记 200')

    def test_no_account_raises_503(self) -> None:
        with self.assertRaises(PlatformError) as ctx:
            asyncio.run(self.adapter.chat('auto', {
                'messages': [{'role': 'user', 'content': 'hi'}]}))
        self.assertEqual(ctx.exception.status, 503)
        self.assertEqual(ctx.exception.code, 'no_account')

    # ── 签到 / 额度 ───────────────────────────────────────
    def test_checkin_records_snapshot_and_earned_credit(self) -> None:
        self._seed()

        async def fake_checkin(account, **kwargs):
            return {'ok': True, 'already': False, 'code': 0,
                    'message': '签到成功', 'reward': '30 积分',
                    'reward_amount': 30, 'days': 4}

        with mock.patch.object(client, 'checkin', new=fake_checkin):
            out = asyncio.run(self.adapter.checkin('db_1'))
        self.assertTrue(out['ok'])
        self.assertEqual(out['earned_credit'], 30)
        raw = store.read_account('db_1')
        self.assertEqual(raw['checkin']['last_result'], 'ok')
        self.assertEqual(raw['checkin']['continuous_days'], 4)

    def test_checkin_already_earns_nothing(self) -> None:
        self._seed()

        async def fake_checkin(account, **kwargs):
            return {'ok': True, 'already': True, 'code': 0,
                    'message': '今日已签到', 'reward': '',
                    'reward_amount': 0, 'days': 4}

        with mock.patch.object(client, 'checkin', new=fake_checkin):
            out = asyncio.run(self.adapter.checkin('db_1'))
        self.assertTrue(out['already'])
        self.assertEqual(out['earned_credit'], 0)

    def test_credits_exposes_remaining_and_expiries(self) -> None:
        self._seed()
        future = int(time.time()) + 86400

        async def fake_quota(account, **kwargs):
            return {'total': 1000.0, 'used': 250.0, 'remaining': 750.0,
                    'percent': 25.0,
                    'groups': [{'name': '活动额度', 'total': 1000.0,
                                'used': 250.0, 'percent': 25.0,
                                'end_time': future}]}

        with mock.patch.object(client, 'quota', new=fake_quota):
            out = asyncio.run(self.adapter.credits('db_1'))
        self.assertEqual(out['credits'], 750.0)
        self.assertEqual(out['limit'], 1000.0)
        self.assertEqual(out['next_expire_at'], future)
        self.assertEqual(out['expires'][0]['amount'], 750.0)
        self.assertEqual(store.read_account('db_1')['quota']['remaining'], 750.0)

    def test_usage_reports_quota_errors_without_raising(self) -> None:
        self._seed()

        async def fake_quota(account, **kwargs):
            raise PlatformError('boom', status=502, code='upstream_error')

        with mock.patch.object(client, 'quota', new=fake_quota):
            out = asyncio.run(self.adapter.usage('db_1'))
        self.assertIsNone(out['session_usage'])
        self.assertEqual(out['errors'][0]['code'], 'upstream_error')


# ── 设置能力表 ──────────────────────────────────────────────
class DoubaoSettingsTest(unittest.TestCase):
    def test_capabilities_exclude_the_fake_catalog_switches(self) -> None:
        caps = runtime_settings.capabilities('doubao')
        self.assertTrue(caps['max_accounts_try'])
        self.assertTrue(caps['session_affinity'])
        self.assertTrue(caps['credits_ttl'])
        self.assertTrue(caps['cooldown_429'])
        # 豆包没有在线目录：露出来就是「保存成功但不生效」。
        self.assertFalse(caps['catalog_ttl'])
        self.assertFalse(caps['catalog_timeout'])
        self.assertFalse(caps['min_credits'])


if __name__ == '__main__':
    unittest.main()