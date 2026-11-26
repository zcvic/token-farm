"""WPS 灵犀平台：账号存储、模型目录、请求体归一、SSE 翻译与适配器策略。

全部离线：上游 HTTP 一律用 `httpx.MockTransport` 打桩。这里钉住几件容易回归
的事：

  · Cookie 只落盘、不出圈：对外结构只有摘要与尾部，明文只在 `export_accounts`；
  · 一次对话是「建会话 → 领 run → SSE」，run 在 TTL 内复用、失效时重建一次；
  · 请求体两条硬约束：`content` 必须展平成字符串、`tool_choice` 必须是字符串；
  · 正文在 `choices[].text`，没有 `[DONE]`，靠 `finish_reason` 收尾；
  · `usage.total_tokens` 是上下文大小，必须 `prompt + completion` 重算；
  · 审核判定只看诊断字段，正文里出现 `sensitive` 不能误判；
  · 流中途出错要**抛出去**（`platforms/sse` 据此补错误帧并记 502）。
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
from server.platforms.lingxi import (catalog, chat, client, constants as C,
                                     store)
from server.platforms.lingxi.adapter import LingxiAdapter
from server.platforms.sse import DONE, openai_sse

_COOKIE = 'wps_sid=abc123def456; csrf=xyz789; other=1'


def _sse(*frames: dict) -> bytes:
    return ''.join(f'data: {json.dumps(frame, ensure_ascii=False)}\n\n'
                   for frame in frames).encode('utf-8')


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
class LingxiStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(config, 'DATA_DIR', Path(self.tmp.name))
        patch.start()
        self.addCleanup(patch.stop)

    def test_normalize_cookie_strips_prefix_newlines_and_bare_sid(self) -> None:
        raw = 'Cookie: wps_sid=a;  csrf=b\r\nother=c;\n\n'
        self.assertEqual(store.normalize_cookie(raw),
                         'wps_sid=a; csrf=b; other=c')
        self.assertEqual(store.normalize_cookie('abc123'), 'wps_sid=abc123')

    def test_has_login_requires_wps_sid(self) -> None:
        self.assertTrue(store.has_login('wps_sid=only-one'))
        self.assertTrue(store.has_login('csrf=1; wps_sid=x'))
        self.assertFalse(store.has_login('csrf=1; other=2'))
        self.assertFalse(store.has_login(''))

    def test_uid_is_stable_and_cookie_never_leaves_the_process(self) -> None:
        first = store.derive_uid(_COOKIE)
        second = store.derive_uid('wps_sid=abc123def456; other=changed')
        self.assertEqual(first, second, '同一 wps_sid 必须命中同一条账号')
        self.assertTrue(first.startswith('lx_'))

        saved = store.save_account({'uid': first, 'cookie': _COOKIE})
        public = store.public_account(saved)
        self.assertNotIn('cookie', public)
        self.assertTrue(public['has_token'])
        self.assertTrue(public['cookie_summary']['has_wps_sid'])
        self.assertEqual(public['cookie_summary']['wps_sid_tail'], 'def456')
        self.assertTrue(public['token_preview'].startswith('…'))
        self.assertNotIn('abc123def456', json.dumps(public, ensure_ascii=False))

    def test_device_id_is_derived_and_stable(self) -> None:
        account = {'cookie': _COOKIE}
        first = store.device_of(account)
        self.assertEqual(first, store.device_of({'cookie': _COOKIE}))
        self.assertEqual(len(first), 32)
        self.assertEqual(store.device_of(
            {'cookie': _COOKIE, 'deviceId': 'dev-1'}), 'dev-1')

    def test_cookie_region_reads_branch(self) -> None:
        self.assertEqual(
            store.cookie_region('wps_sid=x; copilot_server_branch=gray-2'),
            'gray-2')
        self.assertEqual(store.cookie_region('wps_sid=x'), '')

    def test_public_account_reports_checkin_and_balance(self) -> None:
        saved = store.save_account({
            'uid': 'lx_1', 'cookie': _COOKIE,
            'checkin': {'last_at': 1700000000, 'last_result': 'ok',
                        'continuous_days': 3, 'reward': 10},
            'balance': {'state': 'ok', 'at': 1700000001, 'remaining': 12.5},
            'coolingUntil': time.time() + 600,
            'coolingReason': 'rate limited',
        })
        public = store.public_account(saved)
        self.assertEqual(public['checkin']['last_result'], 'ok')
        self.assertEqual(public['checkin']['continuous_days'], 3)
        self.assertEqual(public['balance']['remaining'], 12.5)
        self.assertTrue(public['cooling'])
        self.assertGreater(public['cool_remaining_sec'], 0)
        self.assertEqual(public['realm'], 'cn')

    def test_weight_and_switches_round_trip(self) -> None:
        store.save_account({'uid': 'lx_1', 'cookie': _COOKIE})
        self.assertEqual(store.set_weight('lx_1', 2)['weight'], 2.0)
        self.assertTrue(store.set_disabled('lx_1', True, '手动')['disabled'])
        self.assertTrue(store.set_checkin_only('lx_1', True)['checkin_only'])
        self.assertFalse(store.read_account('lx_1').get('disabled'))
        self.assertEqual(store.set_weight('lx_1', 0)['weight'], 0.0)
        with self.assertRaises(ValueError):
            store.set_weight('lx_1', 1000)


# ── 常量 / 模型目录 ─────────────────────────────────────────
class LingxiCatalogTest(unittest.TestCase):
    def test_upstream_model_strips_known_prefixes_and_default(self) -> None:
        self.assertEqual(C.upstream_model_id('lingxi/GLM-5.3'), 'GLM-5.3')
        self.assertEqual(C.upstream_model_id('wps-lingxi/GLM-5.3'), 'GLM-5.3')
        self.assertEqual(C.upstream_model_id('default'), C.DEFAULT_MODEL)
        self.assertEqual(C.upstream_model_id('brand-new-model'),
                         'brand-new-model')

    def test_parse_plans_reads_plans_then_models_and_normalizes_fields(self) -> None:
        payload = {'data': {
            'plans': [{'key': 'plan_pro', 'name': 'Pro 模式',
                       'desc': '日常任务', 'max_context_tokens': 1000,
                       'max_new_tokens': 200, 'support_multimodal': True}],
            'models': [{
                'key': 'deepseek-x', 'name': 'DeepSeek X', 'desc': '',
                'intro': '一句话介绍', 'tag': 'NEW', 'multiplier': '～0.12x',
                'reasoning_efforts': ['low', 'high'],
                'support_multimodal': True,
                'max_context_tokens': 2000, 'max_new_tokens': 400,
            }, {'key': 'plain-model', 'name': 'Plain'}],
        }}
        models = catalog.parse_plans(payload)
        self.assertEqual([m['id'] for m in models],
                         ['plan_pro', 'deepseek-x', 'plain-model'])
        first = models[1]
        self.assertEqual(first['multiplier'], '~0.12x')
        self.assertTrue(first['reasoning'])
        self.assertEqual(first['reasoning_efforts'], ['low', 'high'])
        self.assertEqual(first['badge'], 'NEW')
        self.assertEqual(first['description'], '一句话介绍')
        self.assertEqual(first['context_length'], 2000)
        self.assertEqual(first['max_tokens'], 400)
        self.assertFalse(models[2]['reasoning'], '没有档位声明就不是思考模型')

    def test_default_catalog_is_usable_offline(self) -> None:
        models = catalog.default_models()
        ids = {m['id'] for m in models}
        self.assertIn('plan_pro', ids)
        self.assertIn(C.DEFAULT_MODEL, ids)
        public = catalog.to_public(catalog.model_entry(models, C.DEFAULT_MODEL),
                                   'lingxi')
        self.assertEqual(public['id'], f'lingxi/{C.DEFAULT_MODEL}')
        self.assertEqual(public['model_name'], C.DEFAULT_MODEL)
        self.assertTrue(public['supports_reasoning'])
        self.assertEqual(public['reasoning_efforts'], ['low', 'high', 'max'])

    def test_parse_plans_bad_shapes_return_empty(self) -> None:
        self.assertEqual(catalog.parse_plans(None), [])
        self.assertEqual(catalog.parse_plans({'data': []}), [])
        self.assertEqual(catalog.parse_plans({'data': {'plans': 'x'}}), [])


# ── 请求体 ──────────────────────────────────────────────────
class LingxiBuildBodyTest(unittest.TestCase):
    def test_content_is_flattened_and_developer_becomes_system(self) -> None:
        body, dropped = chat.build_body({
            'model': 'lingxi/x',
            'messages': [
                {'role': 'developer', 'content': [
                    {'type': 'text', 'text': '你是助手'},
                    {'type': 'image_url', 'image_url': {'url': 'x'}},
                ]},
                {'role': 'user', 'content': [{'text': '第一段'},
                                             {'text': '第二段'}]},
            ],
        }, 'deepseek-v4.1-flash')
        self.assertEqual(dropped, 1)
        self.assertEqual(body['messages'][0]['role'], 'system')
        self.assertEqual(body['messages'][0]['content'], '你是助手')
        self.assertEqual(body['messages'][1]['content'], '第一段\n第二段')
        self.assertTrue(body['stream'], '上游固定流式')

    def test_tool_choice_object_is_downgraded_to_string(self) -> None:
        tools = [{'type': 'function', 'function': {'name': 'f',
                                                    'strict': True}}]
        body, _ = chat.build_body({
            'model': 'x',
            'messages': [{'role': 'user', 'content': 'hi'}],
            'tools': tools,
            'tool_choice': {'type': 'function', 'function': {'name': 'f'}},
        }, 'deepseek-v4.1-flash')
        self.assertEqual(body['tool_choice'], 'required')
        self.assertEqual(body['tools'], tools, 'tools 原样透传（含 strict）')

        body, _ = chat.build_body({
            'model': 'x',
            'messages': [{'role': 'user', 'content': 'hi'}],
            'tools': tools,
            'tool_choice': 'none',
        }, 'x')
        self.assertEqual(body['tool_choice'], 'none')

    def test_max_tokens_goes_into_base_llm_arguments(self) -> None:
        body, _ = chat.build_body({
            'model': 'x',
            'messages': [{'role': 'user', 'content': 'hi'}],
            'max_tokens': 512,
        }, 'x')
        self.assertEqual(body['base_llm_arguments'], {'max_tokens': 512})
        self.assertNotIn('max_tokens', body)

        fallback, _ = chat.build_body({
            'model': 'x',
            'messages': [{'role': 'user', 'content': 'hi'}],
            'max_completion_tokens': 1024,
        }, 'x')
        self.assertEqual(fallback['base_llm_arguments'], {'max_tokens': 1024})

    def test_reasoning_effort_is_written_to_both_places(self) -> None:
        body, _ = chat.build_body({
            'model': 'x',
            'messages': [{'role': 'user', 'content': 'hi'}],
            'reasoning_effort': 'high',
        }, 'x')
        self.assertEqual(body['reasoning_effort'], 'high')
        self.assertEqual(body['llm_arguments'], {'reasoning_effort': 'high'})

    def test_empty_messages_is_a_400(self) -> None:
        with self.assertRaises(PlatformError) as ctx:
            chat.build_body({'model': 'x', 'messages': []}, 'x')
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(ctx.exception.code, 'empty_messages')


# ── SSE 翻译 ────────────────────────────────────────────────
class LingxiTranslatorTest(unittest.TestCase):
    def test_text_and_reasoning_use_upstream_field_names(self) -> None:
        tr = chat.StreamTranslator('lingxi/x')
        chunks = tr.start()
        chunks += tr.feed({'code': 'Success', 'choices': [
            {'index': 0, 'reasoning_content': '想一想'}]})
        chunks += tr.feed({'code': 'Success', 'choices': [
            {'index': 0, 'text': '你'}]})
        chunks += tr.feed({'code': 'Success', 'choices': [
            {'index': 0, 'text': '好'}]})
        self.assertEqual(chunks[0]['choices'][0]['delta']['role'], 'assistant')
        self.assertEqual(chunks[1]['choices'][0]['delta']['reasoning_content'],
                         '想一想')
        self.assertEqual(''.join(c['choices'][0]['delta'].get('content', '')
                                 for c in chunks), '你好')
        self.assertEqual(tr.text, '你好')
        self.assertEqual(tr.reasoning, '想一想')

    def test_finish_reason_ends_the_stream_and_finish_emits_it(self) -> None:
        tr = chat.StreamTranslator('lingxi/x')
        tr.start()
        tr.feed({'code': 'Success', 'choices': [{'text': 'hi'}]})
        self.assertFalse(tr.finished)
        self.assertEqual(tr.feed({'code': 'Success', 'choices': [
            {'finish_reason': 'stop'}]}), [], '收尾帧留到 finish() 再发')
        self.assertTrue(tr.finished)
        tail = tr.finish()
        self.assertEqual(tail[0]['choices'][0]['finish_reason'], 'stop')
        self.assertEqual(tail[1]['choices'], [])
        self.assertIn('usage', tail[1])
        self.assertEqual(tr.finish(), [], 'finish() 必须幂等')

    def test_usage_total_is_recomputed(self) -> None:
        tr = chat.StreamTranslator('lingxi/x')
        tr.feed({'code': 'Success',
                 'usage': {'prompt_tokens': 100, 'completion_tokens': 7,
                           'total_tokens': 9999},
                 'choices': [{'text': 'x'}]})
        completion = tr.completion()
        self.assertEqual(completion['usage']['total_tokens'], 107)
        self.assertEqual(completion['usage']['prompt_tokens'], 100)
        self.assertEqual(completion['usage']['completion_tokens'], 7)

    def test_usage_keeps_cache_and_reasoning_details(self) -> None:
        self.assertEqual(
            chat.normalize_usage({
                'prompt_tokens': 10, 'completion_tokens': 4,
                'prompt_tokens_details': {'cached_tokens': 6},
                'completion_tokens_details': {'reasoning_tokens': 3},
            }),
            {'prompt_tokens': 10, 'completion_tokens': 4, 'total_tokens': 14,
             'prompt_tokens_details': {'cached_tokens': 6},
             'completion_tokens_details': {'reasoning_tokens': 3}})

    def test_tool_calls_are_merged_by_index(self) -> None:
        tr = chat.StreamTranslator('lingxi/x')
        tr.start()
        tr.feed({'code': 'Success', 'choices': [{'tool_calls': [
            {'index': 0, 'id': 'call_1', 'function': {
                'name': 'lookup', 'arguments': '{"q"'}}]}]})
        tr.feed({'code': 'Success', 'choices': [{'tool_calls': [
            {'index': 0, 'function': {'arguments': ':"x"}'}}]}]})
        message = tr.completion()['choices'][0]['message']
        self.assertEqual(message['tool_calls'][0]['id'], 'call_1')
        self.assertEqual(message['tool_calls'][0]['function']['name'], 'lookup')
        self.assertEqual(message['tool_calls'][0]['function']['arguments'],
                         '{"q":"x"}')

    def test_audit_and_bad_code_raise_stream_errors(self) -> None:
        tr = chat.StreamTranslator('lingxi/x')
        with self.assertRaises(chat.StreamError) as ctx:
            tr.feed({'code': 'AuditFailed', 'message': '内容被审核'})
        self.assertEqual(ctx.exception.code, 'audit_blocked')

        tr = chat.StreamTranslator('lingxi/x')
        with self.assertRaises(chat.StreamError) as ctx:
            tr.feed({'code': 'InternalError', 'msg': 'boom'})
        self.assertEqual(ctx.exception.code, 'upstream_error')

    def test_audit_never_matches_model_body(self) -> None:
        """正文里出现 `sensitive` 这种普通英文词不能判成审核拦截。"""
        tr = chat.StreamTranslator('lingxi/x')
        chunks = tr.feed({'code': 'Success', 'choices': [
            {'reasoning_content': 'for the sensitive lines, ...',
             'text': 'ok'}]})
        content = ''.join(c['choices'][0]['delta'].get('content', '')
                          for c in chunks)
        self.assertEqual(content, 'ok')

    def test_empty_completion_is_aggregated_without_content(self) -> None:
        tr = chat.StreamTranslator('lingxi/x')
        tr.feed({'code': 'Success', 'choices': [{'finish_reason': 'stop'}]})
        completion = tr.completion()
        self.assertEqual(completion['choices'][0]['message']['content'], '')
        self.assertEqual(completion['choices'][0]['finish_reason'], 'stop')


# ── 协议层 ──────────────────────────────────────────────────
class LingxiClientTest(unittest.TestCase):
    def setUp(self) -> None:
        client._runs.clear()
        self.addCleanup(client._runs.clear)

    def _account(self) -> dict:
        return {'uid': 'lx_1', 'cookie': _COOKIE, 'deviceId': 'dev-1'}

    def _patch_http(self, reply) -> None:
        patcher = mock.patch.object(
            config, 'http_client', lambda *a, **k: _client(reply))
        patcher.start()
        self.addCleanup(patcher.stop)

    # ── 错误归类 ─────────────────────────────────────────
    def test_classify_only_treats_clear_auth_failures_as_401(self) -> None:
        err = client.classify(401, {'message': 'unauthorized'}, '失败')
        self.assertEqual((err.status, err.code), (401, 'auth_expired'))
        err = client.classify(401, {'message': 'cookie expired'}, '失败')
        self.assertEqual(err.status, 401)
        err = client.classify(403, {'message': 'no permission'}, '失败')
        self.assertEqual((err.status, err.code), (404, 'model_not_found'))
        err = client.classify(400, {'code': 'InvalidArgument'}, '失败')
        self.assertEqual((err.status, err.code), (404, 'bad_request'))
        err = client.classify(503, {'message': 'busy'}, '失败')
        self.assertEqual((err.status, err.code), (503, 'upstream_error'))
        err = client.classify(418, {'message': 'teapot'}, '失败')
        self.assertEqual((err.status, err.code), (502, 'upstream_error'))

    def test_classify_maps_audit_to_400(self) -> None:
        err = client.classify(400, {'code': 'AuditFailed',
                                    'message': '内容被审核'}, '失败')
        self.assertEqual((err.status, err.code), (400, 'audit_blocked'))

    # ── 三步握手 ─────────────────────────────────────────
    def test_open_chat_runs_handshake_and_sends_agent_headers(self) -> None:
        seen: dict = {}

        def reply(request):
            path = request.url.path
            if path == C.SESSIONS_PATH:
                seen['session_body'] = json.loads(request.content)
                return httpx.Response(200, json={'data': {'session_id': 'sess-1'}})
            if path == C.RUNS_PATH:
                seen['run_body'] = json.loads(request.content)
                return httpx.Response(200, json={'data': {
                    'run_id': 'run-1', 'group_id': 'group-1'}})
            if path == C.CHAT_PATH:
                seen['headers'] = request.headers
                seen['chat_body'] = json.loads(request.content)
                return httpx.Response(
                    200, headers={'content-type': 'text/event-stream'},
                    content=_sse({'code': 'Success',
                                  'choices': [{'text': 'hi'}]},
                                 {'code': 'Success',
                                  'choices': [{'finish_reason': 'stop'}]}))
            return httpx.Response(404, json={'message': 'not found'})

        self._patch_http(reply)

        async def run():
            response = await client.open_chat(
                self._account(),
                {'stream': True, 'model': 'deepseek-v4.1-flash',
                 'messages': [{'role': 'user', 'content': '你好'}]},
                'deepseek-v4.1-flash')
            text = response.text
            await response.aclose()
            return text

        text = asyncio.run(run())
        self.assertIn('"hi"', text)

        self.assertEqual(seen['session_body']['session_type'], 'cowork_client')
        self.assertEqual(seen['session_body']['env']['device_id'], 'dev-1')
        self.assertEqual(seen['run_body']['session_id'], 'sess-1')
        self.assertEqual(seen['run_body']['model_provider'], 'lingxi')
        self.assertEqual(seen['run_body']['model_id'], 'deepseek-v4.1-flash')
        self.assertEqual(seen['run_body']['agent_input']['text'], '你好')

        headers = seen['headers']
        self.assertEqual(headers['x-cc-session-id'], 'sess-1')
        self.assertEqual(headers['x-cc-run-id'], 'run-1')
        self.assertEqual(headers['x-client-type'], C.CLIENT_TYPE)
        self.assertEqual(headers['x-cc-agent-type'], C.CLIENT_TYPE)
        self.assertIn('x-request-id', headers)
        cookie = headers['cookie']
        self.assertIn('wps_sid=abc123def456', cookie)
        self.assertIn('x-kso-app-name=win-lingxi-desktop', cookie)
        self.assertIn('x-kso-device-id=dev-1', cookie)
        self.assertEqual(seen['chat_body']['model'], 'deepseek-v4.1-flash')

    def test_run_is_cached_and_invalidated_on_stale_404(self) -> None:
        calls = {'session': 0, 'run': 0, 'chat': 0}

        def reply(request):
            path = request.url.path
            if path == C.SESSIONS_PATH:
                calls['session'] += 1
                return httpx.Response(200, json={'data': {'session_id': f's{calls["session"]}'}})
            if path == C.RUNS_PATH:
                calls['run'] += 1
                return httpx.Response(200, json={'data': {
                    'run_id': f'r{calls["run"]}', 'group_id': 'g'}})
            if path == C.CHAT_PATH:
                calls['chat'] += 1
                if calls['chat'] == 1:
                    return httpx.Response(404, json={'message': 'run expired'})
                return httpx.Response(
                    200, headers={'content-type': 'text/event-stream'},
                    content=_sse({'code': 'Success',
                                  'choices': [{'finish_reason': 'stop'}]}))
            return httpx.Response(404, json={'message': 'not found'})

        self._patch_http(reply)

        async def first():
            response = await client.open_chat(
                self._account(), {'messages': []}, 'deepseek-v4.1-flash')
            await response.aclose()

        asyncio.run(first())
        self.assertEqual(calls, {'session': 2, 'run': 2, 'chat': 2},
                         'run 失效要重建一次再试')

        async def second():
            response = await client.open_chat(
                self._account(), {'messages': []}, 'deepseek-v4.1-flash')
            await response.aclose()

        asyncio.run(second())
        self.assertEqual(calls, {'session': 2, 'run': 2, 'chat': 3},
                         'TTL 内第二次对话必须复用缓存')

    def test_open_chat_raises_401_for_login_error_and_never_retries(self) -> None:
        calls = {'session': 0}

        def reply(request):
            calls['session'] += 1
            return httpx.Response(401, json={'message': 'unauthorized'})

        self._patch_http(reply)

        with self.assertRaises(PlatformError) as ctx:
            asyncio.run(client.open_chat(
                self._account(), {'messages': []}, 'x'))
        self.assertEqual(ctx.exception.status, 401)
        self.assertEqual(ctx.exception.code, 'auth_expired')
        self.assertEqual(calls['session'], 1, '401 不换基址、不重试')

    def test_non_sse_response_is_classified(self) -> None:
        def reply(request):
            path = request.url.path
            if path == C.SESSIONS_PATH:
                return httpx.Response(200, json={'data': {'session_id': 's'}})
            if path == C.RUNS_PATH:
                return httpx.Response(200, json={'data': {'run_id': 'r'}})
            return httpx.Response(200, json={'code': 'InvalidArgument',
                                            'message': '参数错误'})

        self._patch_http(reply)

        with self.assertRaises(PlatformError) as ctx:
            asyncio.run(client.open_chat(
                self._account(), {'messages': []}, 'x'))
        self.assertEqual(ctx.exception.code, 'upstream_error')

    # ── 探活 / 余额 / 任务 ───────────────────────────────
    def test_probe_reports_ok_invalid_and_unknown(self) -> None:
        def make(payload, status=200):
            def reply(request):
                return httpx.Response(status, json=payload)
            return reply

        self._patch_http(make({'data': {'name': '小明'}, 'result': 'ok'}))
        out = asyncio.run(client.probe(self._account()))
        self.assertEqual((out['state'], out['name']), ('ok', '小明'))

        self._patch_http(make({'result': 'UserUnauthorized'}))
        out = asyncio.run(client.probe(self._account()))
        self.assertEqual(out['state'], 'invalid')

        self._patch_http(make({'message': 'boom'}, status=500))
        out = asyncio.run(client.probe(self._account()))
        self.assertEqual(out['state'], 'unknown')

    def test_parse_balance_uses_total_and_expiring_batches(self) -> None:
        now = 1_700_000_000
        payload = {'data': {
            'total_balance': 100.0,
            'bonus_summary': {'total_value': 50.0, 'total_consumed': 20.0,
                              'items': [
                                  {'value': 50.0, 'balance': 30.0,
                                   'expire_time': '2023-11-20 10:00:00'},
                              ]},
        }}
        out = client.parse_balance(payload, now=now)
        self.assertEqual(out['total'], 100.0)
        self.assertEqual(out['total_value'], 50.0)
        self.assertEqual(out['consumed'], 20.0)
        self.assertEqual(out['batches'][0]['consumed'], 20.0)
        self.assertEqual(out['expiring']['amount'], 30.0)
        self.assertGreater(out['expiring']['earliest_at'], now)

    def test_parse_balance_ignores_batches_beyond_fifteen_days(self) -> None:
        now = 1_700_000_000
        out = client.parse_balance({'data': {
            'balance': 10,
            'bonus_credits': [{'value': 10, 'balance': 10,
                               'expire_time': '2024-03-01 00:00:00'}],
        }}, now=now)
        self.assertNotIn('expiring', out)

    def test_parse_tasks_and_claim_outcomes(self) -> None:
        tasks = client.parse_tasks({'data': {'tasks': [
            {'task_key': 'daily_check_in', 'type': 'daily',
             'status': 'claimable', 'reward_amount': 10,
             'extra': {'consecutive_days': 3, 'check_in_days': [
                 {'date': '2023-11-14', 'status': 'available'}]}},
            {'task_key': 'unknown_key', 'status': 'locked'},
            {'no_key': True},
        ]}})
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0]['title'], '每日签到')
        self.assertEqual(tasks[0]['streak'], 3)
        self.assertEqual(tasks[0]['days'][0]['status'], 'available')
        self.assertEqual(tasks[1]['title'], 'unknown_key')

        self.assertEqual(client.claim_outcome({'result': 'ok',
                                              'data': {'extra': {
                                                  'today_reward': 5}}}),
                         {'ok': True, 'reward': 5.0, 'already': False,
                          'message': ''})
        already = client.claim_outcome({'result': 'failed',
                                        'hint': '今天已经签到过了'})
        self.assertTrue(already['ok'])
        self.assertTrue(already['already'])
        self.assertEqual(client.claim_outcome({})['ok'], True)

    def test_beijing_date_uses_utc_plus_eight(self) -> None:
        self.assertEqual(client.beijing_date(0), '1970-01-01')
        self.assertEqual(client.beijing_date(1_700_000_000), '2023-11-15')

    def test_list_plans_falls_back_when_upstream_is_broken(self) -> None:
        self._patch_http(lambda request: httpx.Response(500, json={}))
        self.assertEqual(asyncio.run(client.list_plans(self._account())), [])

    def test_tasks_propagate_401(self) -> None:
        self._patch_http(
            lambda request: httpx.Response(401, json={'message': 'cookie expired'}))
        with self.assertRaises(PlatformError) as ctx:
            asyncio.run(client.tasks(self._account()))
        self.assertEqual(ctx.exception.status, 401)


# ── 适配器 ──────────────────────────────────────────────────
class LingxiAdapterTest(unittest.TestCase):
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
        client._runs.clear()
        self.addCleanup(client._runs.clear)
        self.adapter = LingxiAdapter()

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db

    def _seed(self, **overrides) -> dict:
        uid = str(overrides.pop('uid', 'lx_1'))
        data = {'uid': uid, 'cookie': overrides.pop('cookie', _COOKIE)}
        data.update(overrides)
        store.save_account(data)
        return data

    # ── 导入 / 探活 ───────────────────────────────────────
    def test_import_account_derives_uid_and_hides_cookie(self) -> None:
        public = self.adapter.import_account({'cookie': 'wps_sid=abc123def456'})
        self.assertTrue(public['uid'].startswith('lx_'))
        self.assertTrue(public['has_token'])
        self.assertEqual(store.read_account(public['uid'])['cookie'],
                         'wps_sid=abc123def456')
        self.assertEqual(public['cookie_summary']['wps_sid_tail'], 'def456')
        self.assertEqual(public['login_state'], '正常')
        self.assertNotIn('abc123def456',
                         json.dumps(public, ensure_ascii=False))

    def test_import_rejects_cookie_without_login_state(self) -> None:
        with self.assertRaises(PlatformError) as ctx:
            self.adapter.import_account({'cookie': 'csrf=1; other=2'})
        self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(PlatformError):
            self.adapter.import_account({'nickname': 'x'})

    def test_refresh_account_records_probe_state(self) -> None:
        self._seed()

        async def fake_probe(account, **kwargs):
            return {'state': 'ok', 'message': '', 'name': '小明', 'base': C.BASE_URL}

        with mock.patch.object(client, 'probe', new=fake_probe):
            public = asyncio.run(self.adapter.refresh_account('lx_1'))
        self.assertEqual(public['login_state'], '正常')
        self.assertEqual(public['nickname'], '小明')
        self.assertEqual(store.read_account('lx_1')['base'], C.BASE_URL)

    def test_refresh_account_raises_401_when_cookie_invalid(self) -> None:
        self._seed()

        async def fake_probe(account, **kwargs):
            return {'state': 'invalid', 'message': 'Cookie 无效', 'name': '',
                    'base': ''}

        with mock.patch.object(client, 'probe', new=fake_probe):
            with self.assertRaises(PlatformError) as ctx:
                asyncio.run(self.adapter.refresh_account('lx_1'))
        self.assertEqual(ctx.exception.status, 401)
        self.assertEqual(store.read_account('lx_1')['loginState'], '登录已失效')

    # ── 模型目录 ──────────────────────────────────────────
    def test_list_models_falls_back_to_builtin_snapshot(self) -> None:
        models = asyncio.run(self.adapter.list_models())
        ids = {m['id'] for m in models}
        self.assertIn('lingxi/deepseek-v4.1-flash', ids)
        self.assertIn('lingxi/plan_pro', ids)
        self.assertTrue(all(m['realm'] == 'cn' for m in models))
        self.assertIn(f'lingxi/{C.DEFAULT_MODEL}',
                      self.adapter.cached_model_ids())
        self.assertEqual(self.adapter.upstream_model('lingxi/GLM-5.3'),
                         'GLM-5.3')

    # ── 选号 / 冷却 ───────────────────────────────────────
    def test_candidates_skip_disabled_checkin_only_and_cooling(self) -> None:
        self._seed(uid='lx_1')
        self._seed(uid='lx_2', disabled=True)
        self._seed(uid='lx_3', coolingUntil=time.time() + 600)
        self._seed(uid='lx_4', checkinOnly=True)
        ids = [a['uid'] for a in self.adapter._candidates()]
        self.assertEqual(ids, ['lx_1'])

    def test_weights_rotate_first_choice(self) -> None:
        self._seed(uid='lx_1', weight=1)
        self._seed(uid='lx_2', weight=1)
        firsts = [self.adapter._candidates()[0]['uid'] for _ in range(4)]
        self.assertEqual(sorted(firsts), ['lx_1', 'lx_1', 'lx_2', 'lx_2'])

    def test_429_cools_the_account(self) -> None:
        self._seed()
        db.set_setting('platforms.lingxi.cooldown_429', 60)

        async def fake_open(*args, **kwargs):
            raise PlatformError('busy', status=429, code='rate_limited')

        with mock.patch.object(client, 'open_chat', new=fake_open):
            with self.assertRaises(PlatformError) as ctx:
                asyncio.run(self.adapter.chat('default', {
                    'messages': [{'role': 'user', 'content': 'hi'}]}))
        self.assertEqual(ctx.exception.status, 429)
        raw = store.read_account('lx_1')
        self.assertGreater(raw.get('coolingUntil') or 0, time.time())

    def test_401_does_not_cool_but_switches_account(self) -> None:
        self._seed(uid='lx_1')
        self._seed(uid='lx_2')
        seen: list[str] = []

        async def fake_open(account, body, model, **kwargs):
            seen.append(str(account.get('uid')))
            if len(seen) == 1:
                raise PlatformError('登录已失效', status=401,
                                    code='auth_expired')
            return _FakeResponse(_sse(
                {'code': 'Success', 'choices': [{'text': 'ok'}]},
                {'code': 'Success', 'choices': [{'finish_reason': 'stop'}]}))

        with mock.patch.object(client, 'open_chat', new=fake_open):
            result = asyncio.run(self.adapter.chat('default', {
                'messages': [{'role': 'user', 'content': 'hi'}]}))
        self.assertEqual(result.payload['choices'][0]['message']['content'],
                         'ok')
        self.assertEqual(len(seen), 2)
        for uid in ('lx_1', 'lx_2'):
            self.assertFalse(store.read_account(uid).get('coolingUntil'))

    # ── 对话 ──────────────────────────────────────────────
    def test_chat_nonstream_aggregates_sse(self) -> None:
        self._seed()
        captured: dict = {}

        async def fake_open(account, body, model, **kwargs):
            captured['body'] = body
            return _FakeResponse(_sse(
                {'code': 'Success', 'choices': [{'text': '你'}]},
                {'code': 'Success',
                 'usage': {'prompt_tokens': 10, 'completion_tokens': 2,
                           'total_tokens': 99999},
                 'choices': [{'text': '好'}]},
                {'code': 'Success', 'choices': [{'finish_reason': 'stop'}]}))

        with mock.patch.object(client, 'open_chat', new=fake_open):
            result = asyncio.run(self.adapter.chat('kimi-k3', {
                'messages': [{'role': 'user', 'content': 'hi'}]}))
        self.assertEqual(result.payload['object'], 'chat.completion')
        self.assertEqual(result.payload['choices'][0]['message']['content'],
                         '你好')
        self.assertEqual(result.model, 'lingxi/kimi-k3')
        self.assertEqual(result.realm, 'cn')
        self.assertEqual(result.usage['total_tokens'], 12)
        self.assertEqual(captured['body']['model'], 'kimi-k3')
        self.assertEqual(captured['body']['messages'][0]['content'], 'hi')

    def test_chat_stream_yields_openai_chunks(self) -> None:
        self._seed()

        async def fake_open(account, body, model, **kwargs):
            return _FakeResponse(_sse(
                {'code': 'Success', 'choices': [{'text': 'hi'}]},
                {'code': 'Success', 'choices': [{'finish_reason': 'stop'}]}))

        async def collect():
            result = await self.adapter.chat('default', {
                'stream': True,
                'messages': [{'role': 'user', 'content': 'hi'}]})
            chunks = [c async for c in result.stream]
            return result, chunks

        with mock.patch.object(client, 'open_chat', new=fake_open):
            result, chunks = asyncio.run(collect())
        self.assertEqual(chunks[0]['choices'][0]['delta']['role'], 'assistant')
        text = ''.join(c['choices'][0]['delta'].get('content', '')
                       if c['choices'] else '' for c in chunks)
        self.assertEqual(text, 'hi')
        self.assertEqual(chunks[-2]['choices'][0]['finish_reason'], 'stop')
        self.assertEqual(chunks[-1]['choices'], [])
        self.assertIn('usage', chunks[-1])

    def test_chat_nonstream_empty_output_is_an_error(self) -> None:
        self._seed()

        async def fake_open(account, body, model, **kwargs):
            return _FakeResponse(_sse(
                {'code': 'Success', 'choices': [{'finish_reason': 'stop'}]}))

        with mock.patch.object(client, 'open_chat', new=fake_open):
            with self.assertRaises(PlatformError) as ctx:
                asyncio.run(self.adapter.chat('default', {
                    'messages': [{'role': 'user', 'content': 'hi'}]}))
        self.assertEqual(ctx.exception.code, 'empty_response')

    def test_chat_nonstream_truncated_stream_is_an_error(self) -> None:
        self._seed()

        async def fake_open(account, body, model, **kwargs):
            return _FakeResponse(_sse(
                {'code': 'Success', 'choices': [{'text': 'hi'}]}))

        with mock.patch.object(client, 'open_chat', new=fake_open):
            with self.assertRaises(PlatformError) as ctx:
                asyncio.run(self.adapter.chat('default', {
                    'messages': [{'role': 'user', 'content': 'hi'}]}))
        self.assertEqual(ctx.exception.code, 'truncated_stream')

    def test_chat_stream_audit_error_is_raised(self) -> None:
        self._seed()

        async def fake_open(account, body, model, **kwargs):
            return _FakeResponse(_sse(
                {'code': 'Success', 'choices': [{'text': 'hi'}]},
                {'code': 'AuditFailed', 'message': '内容被审核'}))

        async def collect():
            result = await self.adapter.chat('default', {
                'stream': True,
                'messages': [{'role': 'user', 'content': 'hi'}]})
            return [c async for c in result.stream]

        with mock.patch.object(client, 'open_chat', new=fake_open):
            with self.assertRaises(PlatformError) as ctx:
                asyncio.run(collect())
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(ctx.exception.code, 'audit_blocked')

    def test_stream_error_reaches_the_sse_layer_as_502(self) -> None:
        self._seed()

        async def fake_open(account, body, model, **kwargs):
            return _FakeResponse(_sse(
                {'code': 'Success', 'choices': [{'text': 'hi'}]},
                {'code': 'InternalError', 'msg': 'boom'}))

        calls: list[tuple] = []

        async def run():
            outcome = await self.adapter.chat('default', {
                'stream': True,
                'messages': [{'role': 'user', 'content': 'hi'}]})
            return [frame async for frame in openai_sse(
                outcome, started=0.0, on_finish=lambda *a: calls.append(a))]

        with mock.patch.object(client, 'open_chat', new=fake_open):
            frames = asyncio.run(run())
        self.assertNotIn(DONE, frames, '错误流不能发 [DONE]')
        error = json.loads(frames[-1][6:].decode('utf-8'))['error']
        self.assertIn('boom', error['message'])
        self.assertEqual(calls[0][0], 502)

    def test_stream_network_error_becomes_502(self) -> None:
        self._seed()

        class _BrokenResponse(_FakeResponse):
            async def aiter_lines(self):
                yield 'data: {"code":"Success","choices":[{"text":"hi"}]}'
                yield ''
                raise httpx.ReadError('peer closed')

        async def fake_open(account, body, model, **kwargs):
            return _BrokenResponse(b'')

        async def collect():
            result = await self.adapter.chat('default', {
                'stream': True,
                'messages': [{'role': 'user', 'content': 'hi'}]})
            return [c async for c in result.stream]

        with mock.patch.object(client, 'open_chat', new=fake_open):
            with self.assertRaises(PlatformError) as ctx:
                asyncio.run(collect())
        self.assertEqual(ctx.exception.status, 502)
        self.assertEqual(ctx.exception.code, 'network_error')

    def test_no_account_raises_503(self) -> None:
        with self.assertRaises(PlatformError) as ctx:
            asyncio.run(self.adapter.chat('default', {
                'messages': [{'role': 'user', 'content': 'hi'}]}))
        self.assertEqual(ctx.exception.status, 503)
        self.assertEqual(ctx.exception.code, 'no_account')

    # ── 签到 / 余额 ───────────────────────────────────────
    def test_checkin_records_snapshot_and_earned_credit(self) -> None:
        self._seed()

        async def fake_tasks(account, **kwargs):
            return [{'key': 'daily_check_in', 'streak': 3, 'reward': 10,
                     'days': [{'date': '2000-01-01',
                               'status': 'available'}]}]

        async def fake_claim(account, key, date, **kwargs):
            self.assertEqual(key, 'daily_check_in')
            return {'ok': True, 'already': False, 'reward': 10,
                    'message': '签到成功'}

        with mock.patch.object(client, 'tasks', new=fake_tasks), \
                mock.patch.object(client, 'claim', new=fake_claim):
            out = asyncio.run(self.adapter.checkin('lx_1'))
        self.assertTrue(out['ok'])
        self.assertEqual(out['earned_credit'], 10)
        raw = store.read_account('lx_1')
        self.assertEqual(raw['checkin']['last_result'], 'ok')
        self.assertEqual(raw['checkin']['continuous_days'], 3)

    def test_checkin_already_earns_nothing(self) -> None:
        self._seed()

        async def fake_tasks(account, **kwargs):
            return []

        async def fake_claim(account, key, date, **kwargs):
            return {'ok': True, 'already': True, 'reward': 0,
                    'message': '今日已签到'}

        with mock.patch.object(client, 'tasks', new=fake_tasks), \
                mock.patch.object(client, 'claim', new=fake_claim):
            out = asyncio.run(self.adapter.checkin('lx_1'))
        self.assertTrue(out['already'])
        self.assertEqual(out['earned_credit'], 0)

    def test_credits_exposes_remaining_and_expiries(self) -> None:
        self._seed()
        future = int(time.time()) + 86400

        async def fake_balance(account, **kwargs):
            return {'total': 100.0, 'total_value': 200.0, 'consumed': 5.0,
                    'batches': [{'balance': 20.0, 'value': 30.0,
                                 'consumed': 10.0,
                                 'expire_time': '2027-01-01 00:00:00',
                                 'expire_at': future * 1000}]}

        with mock.patch.object(client, 'balance', new=fake_balance):
            out = asyncio.run(self.adapter.credits('lx_1'))
        self.assertEqual(out['credits'], 100.0)
        self.assertEqual(out['used'], 5.0)
        self.assertEqual(out['next_expire_at'], future)
        self.assertEqual(out['expires'][0]['amount'], 20.0)
        self.assertEqual(store.read_account('lx_1')['balance']['remaining'],
                         100.0)

    def test_usage_reports_errors_without_raising(self) -> None:
        self._seed()

        async def fake_balance(account, **kwargs):
            raise PlatformError('boom', status=502, code='upstream_error')

        with mock.patch.object(client, 'balance', new=fake_balance):
            out = asyncio.run(self.adapter.usage('lx_1'))
        self.assertIsNone(out['session_usage'])
        self.assertEqual(out['errors'][0]['code'], 'upstream_error')


# ── 设置能力表 ──────────────────────────────────────────────
class LingxiSettingsTest(unittest.TestCase):
    def test_capabilities_match_the_adapter(self) -> None:
        caps = runtime_settings.capabilities('lingxi')
        self.assertTrue(caps['max_accounts_try'])
        self.assertTrue(caps['session_affinity'])
        self.assertTrue(caps['catalog_ttl'])
        self.assertTrue(caps['catalog_timeout'])
        self.assertTrue(caps['credits_ttl'])
        self.assertTrue(caps['credits_timeout'])
        self.assertTrue(caps['cooldown_429'])
        self.assertTrue(caps['cooldown_5xx'])
        # 灵犀没有保底积分选号，也没有桌面端回写：露出来就是假开关。
        self.assertFalse(caps['min_credits'])
        self.assertFalse(caps['desktop_sync'])


if __name__ == '__main__':
    unittest.main()