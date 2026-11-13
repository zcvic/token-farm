"""Trae 协议层：请求体归一、SSE 解析、事件翻译。

全部离线：SSE 用固定文本喂给 `iter_events`，不依赖真实账号或网络。
"""
from __future__ import annotations

import asyncio
import json
import unittest
from typing import Any, AsyncIterator
from unittest import mock

from server import config
from server.platforms.base import PlatformError
from server.platforms.trae import chat as chatmod
from server.platforms.trae import client
from server.platforms.trae import text_tools


class FakeResponse:
    """只实现 `iter_events` 用到的 `aiter_lines()`。"""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def aiter_lines(self) -> AsyncIterator[str]:
        for line in self._lines:
            yield line


def _events(lines: list[str]) -> AsyncIterator[tuple[str, Any]]:
    async def walk() -> AsyncIterator[tuple[str, Any]]:
        async for event, data in client.iter_events(FakeResponse(lines)):
            yield event, data
    return walk()


def _run(awaitable):  # noqa: ANN001
    return asyncio.run(awaitable)


class TimeHelperTest(unittest.TestCase):
    def test_parse_epoch(self) -> None:
        self.assertAlmostEqual(client.parse_epoch('2030-01-01T00:00:00.000Z'),
                               1893456000.0, places=3)
        self.assertEqual(client.parse_epoch(1893456000000), 1893456000.0)
        self.assertEqual(client.parse_epoch(1893456000), 1893456000.0)
        self.assertIsNone(client.parse_epoch('not-a-date'))
        self.assertIsNone(client.parse_epoch(''))

    def test_iso_from_ms(self) -> None:
        self.assertEqual(client.iso_from_ms(1893456000123),
                         '2030-01-01T00:00:00.123Z')
        self.assertEqual(client.iso_from_ms(None), '')


class HttpErrorTest(unittest.TestCase):
    """上游错误体 → 可读文案（界面上直接展示这份 message）。"""

    def test_plain_message_wins(self) -> None:
        err = client._http_error(400, '{"message": "model is unknown"}')
        self.assertEqual(err.message, 'model is unknown')

    def test_response_metadata_error(self) -> None:
        text = ('{"ResponseMetadata": {"Error": {"Code": "20101", '
                '"Message": "Token invalid: refresh token is invalid."}}}')
        err = client._http_error(401, text)
        self.assertEqual(err.message, 'Token invalid: refresh token is invalid.')
        self.assertEqual(err.code, '20101')

    def test_nested_legacy_message(self) -> None:
        text = ('{"ResponseMetadata": {"Error": {"Code": "20405", '
                '"Data": {"__Message.error": "device proof mismatch"}}}}')
        err = client._http_error(400, text)
        self.assertEqual(err.message, 'device proof mismatch')
        self.assertEqual(err.code, '20405')

    def test_non_json_keeps_raw_text(self) -> None:
        err = client._http_error(502, '<html>bad gateway</html>')
        self.assertEqual(err.message, '<html>bad gateway</html>')
        self.assertEqual(err.code, 'upstream_error')

    def test_json_without_known_fields_keeps_raw_text(self) -> None:
        text = '{"Result": {}}'
        err = client._http_error(400, text)
        self.assertEqual(err.message, text)


class BodyTest(unittest.TestCase):
    def test_normalize_content(self) -> None:
        self.assertEqual(client.normalize_content('hi'),
                         [{'type': 'text', 'text': 'hi'}])
        self.assertEqual(client.normalize_content(None),
                         [{'type': 'text', 'text': ''}])
        parts = client.normalize_content([{'text': 'a'}, {'type': 'text', 'text': 'b'}])
        self.assertEqual(parts, [{'type': 'text', 'text': 'a'},
                                 {'type': 'text', 'text': 'b'}])
        image = {'type': 'image_url', 'image_url': {'url': 'data:x'}}
        self.assertEqual(client.normalize_content([image]), [image])

    def test_normalize_messages(self) -> None:
        messages = client.normalize_messages([
            {'role': 'human', 'content': 'hi'},
            {'role': 'model', 'content': 'yo'},
            {'role': 'weird', 'content': 'x'},
        ])
        self.assertEqual([m['role'] for m in messages],
                         ['user', 'assistant', 'user'])
        with self.assertRaises(PlatformError):
            client.normalize_messages([])

    def test_build_chat_body_only_sends_known_options(self) -> None:
        body = client.build_chat_body('glm-5.2', [{'role': 'user', 'content': 'hi'}], {
            'temperature': 0.3, 'stream': False, 'nonsense': 'x',
        })
        self.assertEqual(body['model_name'], 'glm-5.2')
        self.assertTrue(body['stream'])          # 服务端始终 SSE
        # 采样参数挂在 parameters 下：顶层同名字段上游不读。
        self.assertEqual(body['parameters']['temperature'], 0.3)
        self.assertNotIn('temperature', body)
        self.assertNotIn('nonsense', body)

    def test_reasoning_effort_is_forwarded(self) -> None:
        """客户端档位 → Trae `reasoning_effort_level`（旧版完全不下发）。"""
        messages = [{'role': 'user', 'content': 'hi'}]
        low = client.build_chat_body('glm-5.2', messages,
                                     {'reasoning_effort': 'low'})
        self.assertEqual(low['parameters']['reasoning_effort_level'], 'light')
        solo = client.build_solo_body('deepseek-v4.1-flash', messages,
                                      {'reasoning_effort': 'xhigh'})
        self.assertEqual(solo['reasoning_effort_level'], 'extra_high')
        media = client.build_solo_body('deepseek-v4.1-flash', messages,
                                       {'reasoning': {'effort': 'medium'}})
        self.assertEqual(media['reasoning_effort_level'], 'high')
        nested = client.build_solo_body('deepseek-v4.1-flash', messages,
                                        {'thinking': {'level': 'minimal'}})
        self.assertEqual(nested['reasoning_effort_level'], 'light')
        disabled = client.build_solo_body(
            'deepseek-v4.1-flash', messages,
            {'thinking': {'type': 'disabled'}})
        self.assertEqual(disabled['reasoning_effort_level'], 'none')

    def test_thinking_false_requests_off(self) -> None:
        body = client.build_chat_body(
            'glm-5.2', [{'role': 'user', 'content': 'hi'}],
            {'thinking': False})
        self.assertEqual(body['parameters']['reasoning_effort_level'], 'none')

    def test_effort_can_be_disabled(self) -> None:
        with mock.patch.object(config, 'TRAE_REASONING_EFFORT', False):
            body = client.build_chat_body(
                'glm-5.2', [{'role': 'user', 'content': 'hi'}],
                {'reasoning_effort': 'low'})
        self.assertNotIn('reasoning_effort_level', body.get('parameters', {}))

    def test_solo_defaults_max_tokens(self) -> None:
        """客户端不传时也要给足输出预算，避免思考吃满后没有正文。"""
        messages = [{'role': 'user', 'content': 'hi'}]
        body = client.build_solo_body('deepseek-v4.1-flash', messages, {})
        self.assertEqual(body['max_tokens'], config.TRAE_SOLO_MAX_TOKENS)
        explicit = client.build_solo_body('deepseek-v4.1-flash', messages,
                                          {'max_tokens': 123})
        self.assertEqual(explicit['max_tokens'], 123)

    def test_tool_hint_includes_parameter_schema(self) -> None:
        """只给工具名不够：模型得知道参数名/类型/必填。"""
        tools = [{'type': 'function', 'function': {
            'name': 'bash',
            'parameters': {'type': 'object',
                           'properties': {'command': {'type': 'string'}},
                           'required': ['command']}}}]
        body = client.build_solo_body(
            'deepseek-v4.1-flash', [{'role': 'user', 'content': 'hi'}],
            {'tools': tools})
        text = ''.join(part.get('text', '')
                       for part in body['messages'][0]['content'])
        self.assertIn('bash(command*:string)', text)

    def test_tools_parameters_serialized_to_json_string(self) -> None:
        """`parameters` 必须是字符串：传对象会被上游判 4001（实测）。"""
        params = {'type': 'object', 'properties': {'city': {'type': 'string'}}}
        body = client.build_solo_body('x', [{'role': 'user', 'content': 'hi'}], {
            'tools': [{'type': 'function',
                       'function': {'name': 'get_weather', 'description': '天气',
                                    'parameters': params, 'strict': True}}],
        })
        tool = body['tools'][0]
        self.assertEqual(tool['function']['name'], 'get_weather')
        self.assertIsInstance(tool['function']['parameters'], str)
        self.assertEqual(json.loads(tool['function']['parameters']), params)
        self.assertNotIn('strict', tool['function'], '未实测的函数字段不该外发')

    def test_flat_tool_shape_is_normalized(self) -> None:
        body = client.build_chat_body('x', [{'role': 'user', 'content': 'hi'}], {
            'tools': [{'type': 'function', 'name': 'f',
                       'parameters': {'type': 'object'}}],
        })
        self.assertEqual(body['tools'][0]['function']['name'], 'f')

    def test_unknown_tools_are_dropped(self) -> None:
        body = client.build_chat_body('x', [{'role': 'user', 'content': 'hi'}], {
            'tools': [{'type': 'web_search'},
                      {'type': 'function', 'function': {}}],
        })
        self.assertNotIn('tools', body)

    def test_invalid_parameters_string_is_dropped(self) -> None:
        body = client.build_chat_body('x', [{'role': 'user', 'content': 'hi'}], {
            'tools': [{'type': 'function',
                       'function': {'name': 'f', 'parameters': '{not json'}}],
        })
        self.assertNotIn('parameters', body['tools'][0]['function'])

    def test_tool_choice_only_accepts_strings(self) -> None:
        """上游 `tool_choice` 是 string 类型，对象形状会被直接拒绝（实测）。"""
        messages = [{'role': 'user', 'content': 'hi'}]
        tools = [{'type': 'function', 'function': {'name': 'f'}}]
        auto = client.build_solo_body('x', messages, {'tools': tools, 'tool_choice': 'auto'})
        self.assertEqual(auto['tool_choice'], 'auto')
        forced = client.build_solo_body('x', messages, {
            'tools': tools,
            'tool_choice': {'type': 'function', 'function': {'name': 'f'}},
        })
        self.assertNotIn('tool_choice', forced)
        bogus = client.build_solo_body('x', messages, {'tools': tools, 'tool_choice': 'greedy'})
        self.assertNotIn('tool_choice', bogus)

    def test_tool_history_folded_into_text(self) -> None:
        """上游丢弃 `tool_calls` / `tool_call_id`，只能折成正文保住多轮上下文。"""
        messages = client.normalize_messages([
            {'role': 'user', 'content': '北京天气'},
            {'role': 'assistant', 'content': '',
             'tool_calls': [{'id': 'c1', 'type': 'function',
                             'function': {'name': 'get_weather',
                                          'arguments': '{"city":"北京"}'}}]},
            {'role': 'tool', 'tool_call_id': 'c1', 'content': '晴，25℃'},
        ])
        self.assertEqual([m['role'] for m in messages],
                         ['user', 'assistant', 'user'])
        self.assertIn('[历史调用] get_weather({"city":"北京"})',
                      messages[1]['content'][-1]['text'])
        result = messages[2]['content'][0]['text']
        self.assertTrue(result.startswith('[历史结果]'))
        self.assertIn('晴，25℃', result)

    def test_solo_preserves_native_tool_roundtrip(self) -> None:
        """SOLO 接受原生 tool_calls / tool 回执，不能像 IDE 通道那样折成正文。

        参考实现 v1.4.2 实测：折成正文后第二轮请求会被上游 4027 拒绝
        （`tool_call_id is not found ... tool name: ''`）。
        """
        body = client.build_solo_body('x', [
            {'role': 'user', 'content': '北京天气'},
            {'role': 'assistant', 'content': None,
             'tool_calls': [{'id': 'c1', 'type': 'function',
                             'function': {'name': 'get_weather',
                                          'arguments': '{"city":"北京"}'}}]},
            {'role': 'tool', 'tool_call_id': 'c1', 'content': '晴，25℃'},
        ], {})
        messages = body['messages']
        self.assertEqual([m['role'] for m in messages],
                         ['user', 'assistant', 'tool'])
        call = messages[1]['tool_calls'][0]
        # 上游历史字段是 `function_call`，不是 OpenAI 的 `function`。
        self.assertEqual(call['function_call']['name'], 'get_weather')
        self.assertEqual(call['function_call']['arguments'], '{"city":"北京"}')
        self.assertEqual(call['id'], 'c1')
        tool = messages[2]
        self.assertEqual(tool['tool_call_id'], 'c1')
        self.assertEqual(tool['name'], 'get_weather')
        self.assertEqual(tool['content'][0]['text'], '晴，25℃')

    def test_solo_tool_arguments_object_is_serialized(self) -> None:
        body = client.build_solo_body('x', [
            {'role': 'assistant', 'content': '',
             'tool_calls': [{'id': 'c1', 'type': 'function',
                             'function': {'name': 'f',
                                          'arguments': {'a': 1}}}]},
        ], {})
        call = body['messages'][0]['tool_calls'][0]
        self.assertEqual(json.loads(call['function_call']['arguments']), {'a': 1})

    def test_solo_tool_without_name_mapping_falls_back_to_text(self) -> None:
        """映射不到工具名时退回正文，避免上游 4027（空 name）。"""
        body = client.build_solo_body('x', [
            {'role': 'assistant', 'content': '查一下',
             'tool_calls': [{'id': 'c1', 'type': 'function',
                             'function': {'name': 'get_weather',
                                          'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'other', 'content': '晴'},
        ], {})
        tool = body['messages'][1]
        self.assertEqual(tool['role'], 'user')
        self.assertEqual(tool['content'][0]['text'], '[历史结果] 晴')

    def test_solo_empty_messages_raise(self) -> None:
        with self.assertRaises(PlatformError):
            client.build_solo_body('x', [], {})

    def test_tool_protocol_hint_merged_into_system(self) -> None:
        """声明了 tools 就要把「走原生通道、别自造标签」说死。"""
        tools = [{'type': 'function', 'function': {'name': 'bash',
                                                   'parameters': {}}}]
        body = client.build_solo_body(
            'x',
            [{'role': 'system', 'content': '你是编码助手'},
             {'role': 'user', 'content': 'hi'}],
            {'tools': tools})
        self.assertEqual(len(body['messages']), 2, '并进原有 system，不新增一条')
        system = body['messages'][0]
        self.assertEqual(system['role'], 'system')
        text = ''.join(part.get('text', '') for part in system['content'])
        self.assertIn('你是编码助手', text)
        self.assertIn('[工具调用协议]', text)
        self.assertIn('bash', text)
        self.assertIn('原生 function calling', text)
        # 提示不能再教模型写正文调用：模型会照抄示例里的 `...`，还原器再把它
        # 当成真调用执行（2026-11-02 实测）。
        self.assertNotIn('唯一允许的格式', text)
        self.assertNotIn('[tool_call] bash(', text)

    def test_tool_protocol_hint_skipped_without_tools(self) -> None:
        body = client.build_chat_body(
            'x', [{'role': 'user', 'content': 'hi'}], {})
        self.assertEqual([m['role'] for m in body['messages']], ['user'])

    def test_tool_protocol_hint_can_be_disabled(self) -> None:
        tools = [{'type': 'function', 'function': {'name': 'bash',
                                                   'parameters': {}}}]
        with mock.patch.object(config, 'TRAE_TOOL_PROTOCOL_HINT', False):
            body = client.build_solo_body(
                'x', [{'role': 'user', 'content': 'hi'}], {'tools': tools})
        self.assertEqual([m['role'] for m in body['messages']], ['user'])


class EventParseTest(unittest.TestCase):
    def test_multiline_data_and_done(self) -> None:
        lines = [
            'event: output',
            'data: {"response":',
            'data: "hi"}',
            '',
            'event: done',
            'data: {"finish_reason": "stop"}',
            '',
            'data: [DONE]',
            '',
        ]

        async def collect() -> list[tuple[str, Any]]:
            return [item async for item in _events(lines)]

        events = _run(collect())
        self.assertEqual(events[0], ('output', {'response': 'hi'}))
        self.assertEqual(events[1], ('done', {'finish_reason': 'stop'}))
        # `data: [DONE]` 必须自成一帧结束：不能沿用上一帧的 event 名
        # （`event: output` + `[DONE]` 会被当成正文丢掉，流看起来没有结束）。
        self.assertEqual(events[2], ('done', {'finish_reason': 'stop'}))

    def test_idle_timeout_aborts_stream(self) -> None:
        """上游卡住不能空等 300 秒：连续无数据要尽快中止并报错。"""
        class SlowResponse:
            async def aiter_lines(self) -> AsyncIterator[str]:
                for line in ('event: output', 'data: {"response":"你"}', ''):
                    await asyncio.sleep(0.05)
                    yield line

        async def collect() -> list[tuple[str, Any]]:
            return [item async for item in client.iter_events(
                SlowResponse(), idle_timeout=0.01)]

        events = _run(collect())
        self.assertEqual(events[-1][0], 'error')
        self.assertEqual(events[-1][1]['code'], 'idle_timeout')

    def test_comments_and_unknown_fields_ignored(self) -> None:
        lines = [': keep-alive', 'id: 1', 'retry: 5', '',
                 'event: output', 'data: hello', '']

        async def collect() -> list[tuple[str, Any]]:
            return [item async for item in _events(lines)]

        self.assertEqual(_run(collect()), [('output', 'hello')])


class TranslateTest(unittest.TestCase):
    def _lines(self) -> list[str]:
        return [
            'event: metadata', 'data: {"model":"x"}', '',
            'event: output', 'data: {"response":"你"}', '',
            'event: output', 'data: {"reasoning_content":"想"}', '',
            'event: token_usage',
            'data: {"prompt_tokens":3,"completion_tokens":2}', '',
            'event: done', 'data: {"finish_reason":"stop"}', '',
        ]

    @staticmethod
    def _sse(event: str, payload: object) -> list[str]:
        return [f'event: {event}',
                'data: ' + json.dumps(payload, ensure_ascii=False), '']

    @staticmethod
    def _tool_frame(index: int, *, call_id: str = '', name: str = '',
                    arguments: str = '') -> dict:
        """上游实际下发的 `function_call` 分片（注意不是 OpenAI 的 `function`）。"""
        return {'tool_calls': [{
            'index': index, 'id': call_id, 'type': 'function' if call_id else '',
            'function_call': {'name': name, 'arguments': arguments,
                              'partial_arguments': None, 'namespace': None},
        }]}

    def _run_translate(self, lines: list[str], **kwargs) -> list[dict]:
        state = chatmod.StreamState()

        async def collect() -> list[dict]:
            return [c async for c in chatmod.translate(
                _events(lines), model='trae/x', state=state, **kwargs)]

        chunks = _run(collect())
        self._state = state
        return chunks

    def test_translate_produces_openai_chunks(self) -> None:
        state = chatmod.StreamState()

        async def collect() -> list[dict]:
            return [c async for c in chatmod.translate(
                _events(self._lines()), model='trae/glm-5.2', state=state)]

        chunks = _run(collect())
        self.assertEqual(chunks[0]['object'], 'chat.completion.chunk')
        self.assertEqual(chunks[0]['choices'][0]['delta'], {'role': 'assistant'})
        contents = [c['choices'][0]['delta'].get('content') for c in chunks]
        self.assertIn('你', contents)
        reasonings = [c['choices'][0]['delta'].get('reasoning_content') for c in chunks]
        self.assertIn('想', reasonings)
        final = chunks[-1]
        self.assertEqual(final['choices'][0]['finish_reason'], 'stop')
        self.assertEqual(final['usage']['prompt_tokens'], 3)
        self.assertEqual(final['usage']['completion_tokens'], 2)
        self.assertTrue(state.saw_content)
        self.assertIsNone(state.error)
        self.assertEqual(state.usage['total_tokens'], 5)

    def test_usage_dict_is_updated_in_place(self) -> None:
        """`PlatformChat.usage` 持有的是 StreamState 初始化时那个 dict。

        如果翻译层用 `state.usage = {...}` 换对象，网关在流结束后读到的是空
        dict，token 记账会静默丢成 0——这条用例钉的是 dict 的**身份**。
        """
        state = chatmod.StreamState()
        ref = state.usage

        async def collect() -> None:
            async for _ in chatmod.translate(
                    _events(self._lines()), model='trae/x', state=state):
                pass

        _run(collect())
        self.assertIs(state.usage, ref)
        self.assertEqual(ref.get('total_tokens'), 5)

    def test_aggregate_builds_completion(self) -> None:
        state = chatmod.StreamState()

        async def collect() -> dict:
            return await chatmod.aggregate(
                _events(self._lines()), model='trae/glm-5.2', state=state)

        payload = _run(collect())
        self.assertEqual(payload['object'], 'chat.completion')
        message = payload['choices'][0]['message']
        self.assertEqual(message['content'], '你')
        self.assertEqual(message['reasoning_content'], '想')
        self.assertEqual(payload['usage']['total_tokens'], 5)

    def test_aggregate_raises_when_error_without_content(self) -> None:
        lines = ['event: error', 'data: {"code": 4015, "message": "风控"}', '']
        state = chatmod.StreamState()

        async def collect() -> dict:
            return await chatmod.aggregate(
                _events(lines), model='trae/x', state=state)

        with self.assertRaises(PlatformError) as ctx:
            _run(collect())
        self.assertIn('4015', str(ctx.exception))
        self.assertIn('风控', str(ctx.exception))

    def test_usage_aliases(self) -> None:
        self.assertEqual(chatmod.normalize_usage({'input_tokens': 2, 'output_tokens': 1}),
                         {'prompt_tokens': 2, 'completion_tokens': 1,
                          'total_tokens': 3})
        self.assertEqual(chatmod.normalize_usage({'usage': {'prompt_tokens': 4}})
                         ['total_tokens'], 4)
        self.assertEqual(chatmod.normalize_usage({'unexpected': 1}), {})

    def test_midstream_error_is_surfaced(self) -> None:
        """已经出过正文再报错，不能伪装成正常结束（旧行为是 finish=stop）。"""
        lines = (self._sse('output', {'response': '说到一半'})
                 + self._sse('error', {'code': 4015, 'message': '风控'}))
        chunks = self._run_translate(lines)
        self.assertTrue(any(c.get('error') for c in chunks),
                        '中途错误必须补一帧错误对象')
        self.assertEqual(chunks[-1]['choices'][0]['finish_reason'], 'error')
        text = ''.join(c['choices'][0]['delta'].get('content') or ''
                       for c in chunks if 'choices' in c)
        self.assertEqual(text, '说到一半')

    def test_missing_done_is_reported_as_truncated(self) -> None:
        """连接被掐断（没有 done）要如实标记，不能报成完整的 stop。"""
        lines = self._sse('output', {'response': '说到一半'})
        chunks = self._run_translate(lines)
        self.assertTrue(any((c.get('error') or {}).get('code') == 'stream_truncated'
                            for c in chunks))
        self.assertEqual(chunks[-1]['choices'][0]['finish_reason'], 'length')

    def test_strict_end_can_be_disabled(self) -> None:
        lines = self._sse('output', {'response': '说到一半'})
        with mock.patch.object(config, 'TRAE_STRICT_STREAM_END', False):
            chunks = self._run_translate(lines)
        self.assertFalse(any(c.get('error') for c in chunks))
        self.assertEqual(chunks[-1]['choices'][0]['finish_reason'], 'stop')


class ToolCallTranslateTest(unittest.TestCase):
    """上游工具调用是私有形状（`function_call` + 分片参数），必须转成 OpenAI。"""

    def _chunks(self, lines: list[str], **kwargs) -> list[dict]:
        state = chatmod.StreamState()

        async def collect() -> list[dict]:
            return [c async for c in chatmod.translate(
                _events(lines), model='trae/x', state=state, **kwargs)]

        return _run(collect())

    def test_function_call_becomes_openai_tool_calls(self) -> None:
        lines = TranslateTest._sse('output', {
            'tool_calls': [{'index': 0, 'id': 'call_a', 'type': 'function',
                            'function_call': {'name': 'get_weather',
                                              'arguments': '',
                                              'partial_arguments': None,
                                              'namespace': None}}],
        }) + TranslateTest._sse('output', {
            'tool_calls': [{'index': 0, 'id': '', 'type': '',
                            'function_call': {'name': '',
                                              'arguments': '{"city": "北京"}',
                                              'partial_arguments': None,
                                              'namespace': None}}],
        }) + TranslateTest._sse('done', {'finish_reason': 'stop'})

        chunks = self._chunks(lines)
        deltas = [c['choices'][0]['delta'] for c in chunks]
        calls = [d['tool_calls'][0] for d in deltas if d.get('tool_calls')]
        self.assertEqual(len(calls), 2)

        first, second = calls
        self.assertEqual(first['id'], 'call_a')
        self.assertEqual(first['type'], 'function')
        self.assertEqual(first['function']['name'], 'get_weather')
        self.assertNotIn('function_call', first)
        self.assertNotIn('partial_arguments', first['function'])
        self.assertNotIn('namespace', first['function'])
        # 空串的 id/type/name 不能下发：OpenAI 客户端按 index 合并，空串会覆盖
        self.assertNotIn('id', second)
        self.assertNotIn('type', second)
        self.assertNotIn('name', second['function'])
        self.assertEqual(second['function']['arguments'], '{"city": "北京"}')
        # 上游即便报 stop，带工具调用也要改写成 tool_calls
        self.assertEqual(chunks[-1]['choices'][0]['finish_reason'], 'tool_calls')

    def test_partial_arguments_are_used(self) -> None:
        """`arguments` 为空时回退读 `partial_arguments`，否则客户端拿到空参数。"""
        lines = (TranslateTest._sse('output', {'tool_calls': [{
            'index': 0, 'id': 'c1', 'type': 'function',
            'function_call': {'name': 'write', 'arguments': '',
                              'partial_arguments': '{"path": "a"}'}}]})
            + TranslateTest._sse('done', {'finish_reason': 'stop'}))
        chunks = self._chunks(lines)
        calls = [d['tool_calls'][0] for d in
                 (c['choices'][0]['delta'] for c in chunks) if d.get('tool_calls')]
        self.assertEqual(calls[0]['function']['arguments'], '{"path": "a"}')

    def test_dict_arguments_are_json_encoded(self) -> None:
        """参数是对象时要序列化成 JSON 字符串，不能下发 Python repr。"""
        lines = (TranslateTest._sse('output', {'tool_calls': [{
            'index': 0, 'id': 'c1', 'type': 'function',
            'function_call': {'name': 'write', 'arguments': {'path': 'a'}}}]})
            + TranslateTest._sse('done', {'finish_reason': 'stop'}))
        chunks = self._chunks(lines)
        args = next(d['tool_calls'][0]['function']['arguments'] for d in
                    (c['choices'][0]['delta'] for c in chunks)
                    if d.get('tool_calls'))
        self.assertEqual(json.loads(args), {'path': 'a'})

    def test_tool_calls_dict_is_accepted(self) -> None:
        """上游偶尔把 `tool_calls` 发成单个对象而不是数组。"""
        lines = (TranslateTest._sse('output', {'tool_calls': {
            'index': 0, 'id': 'c1', 'type': 'function',
            'function_call': {'name': 'read', 'arguments': '{"path": "a"}'}}})
            + TranslateTest._sse('done', {'finish_reason': 'stop'}))
        chunks = self._chunks(lines)
        calls = [d['tool_calls'][0] for d in
                 (c['choices'][0]['delta'] for c in chunks) if d.get('tool_calls')]
        self.assertEqual(calls[0]['function']['name'], 'read')

    def test_reasoning_backfilled_when_only_thinking(self) -> None:
        """只有思考、没有正文 = 用户看到的「只有 Thought，没有回答」。"""
        lines = (TranslateTest._sse('output', {'reasoning_content': '我不想'})
                 + TranslateTest._sse('output', {'reasoning_content': '查天气'})
                 + TranslateTest._sse('done', {'finish_reason': 'stop'}))
        chunks = self._chunks(lines)
        texts = [c['choices'][0]['delta'].get('content') for c in chunks]
        self.assertEqual(''.join(t for t in texts if t), '我不想查天气')
        # 帧序：正文必须早于 finish_reason，否则客户端已经停止累积
        finish_at = next(i for i, c in enumerate(chunks)
                         if c['choices'][0].get('finish_reason'))
        content_at = next(i for i, c in enumerate(chunks)
                          if c['choices'][0]['delta'].get('content'))
        self.assertLess(content_at, finish_at)
        self.assertEqual(chunks[-1]['choices'][0]['finish_reason'], 'stop')

    def test_no_backfill_when_tool_calls_present(self) -> None:
        """有工具调用时绝不能回填：那段文字会被当成普通回答，工具链就断了。"""
        lines = (TranslateTest._sse('output', {
            'reasoning_content': '要查天气',
            'tool_calls': [{'index': 0, 'id': 'call_a', 'type': 'function',
                            'function_call': {'name': 'get_weather',
                                              'arguments': '{}'}}],
        }) + TranslateTest._sse('done', {'finish_reason': 'stop'}))
        chunks = self._chunks(lines)
        self.assertFalse(any(c['choices'][0]['delta'].get('content') for c in chunks))

    def test_no_backfill_on_error(self) -> None:
        """上游报错时补正文会把错误伪装成正常回答。"""
        lines = (TranslateTest._sse('output', {'reasoning_content': '想到一半'})
                 + TranslateTest._sse('error', {'code': 4015, 'message': '风控'}))
        chunks = self._chunks(lines)
        data_chunks = [c for c in chunks if 'choices' in c]
        self.assertFalse(any(c['choices'][0]['delta'].get('content')
                             for c in data_chunks))
        self.assertTrue(any(c.get('error') for c in chunks))

    def test_backfill_can_be_disabled(self) -> None:
        """关掉兜底后不再伪造正文；整轮只有思考时按空产出报错。"""
        lines = (TranslateTest._sse('output', {'reasoning_content': '只想不说'})
                 + TranslateTest._sse('done', {'finish_reason': 'stop'}))
        chunks = self._chunks(lines, reasoning_as_content=False)
        self.assertFalse(any(
            c['choices'][0]['delta'].get('content')
            for c in chunks if 'choices' in c))
        self.assertTrue(any((c.get('error') or {}).get('code') == 'reasoning_only'
                            for c in chunks))

    def test_aggregate_merges_interleaved_arguments(self) -> None:
        """一帧里可能同时出现多个 index，参数也是交叉分片下发的。"""
        lines = (
            TranslateTest._sse('output', {'tool_calls': [
                {'index': 0, 'id': 'c0', 'type': 'function',
                 'function_call': {'name': 'f', 'arguments': ''}}]})
            + TranslateTest._sse('output', {'tool_calls': [
                {'index': 0, 'id': '', 'type': '',
                 'function_call': {'name': '', 'arguments': '{"a"'}}]})
            + TranslateTest._sse('output', {'tool_calls': [
                {'index': 0, 'id': '', 'type': '',
                 'function_call': {'name': '', 'arguments': ': 1}'}},
                {'index': 1, 'id': 'c1', 'type': 'function',
                 'function_call': {'name': 'g', 'arguments': '{}'}}]})
            + TranslateTest._sse('done', {'finish_reason': 'stop'})
        )
        state = chatmod.StreamState()

        async def collect() -> dict:
            return await chatmod.aggregate(
                _events(lines), model='trae/x', state=state)

        payload = _run(collect())
        message = payload['choices'][0]['message']
        calls = message['tool_calls']
        self.assertEqual([c['id'] for c in calls], ['c0', 'c1'])
        self.assertEqual(calls[0]['function']['arguments'], '{"a": 1}')
        self.assertEqual(calls[1]['function']['arguments'], '{}')
        self.assertEqual(payload['choices'][0]['finish_reason'], 'tool_calls')
        self.assertIsNone(message['content'])

    def test_aggregate_backfills_reasoning(self) -> None:
        lines = (TranslateTest._sse('output', {'reasoning_content': '只在思考'})
                 + TranslateTest._sse('done', {'finish_reason': 'stop'}))
        state = chatmod.StreamState()

        async def collect() -> dict:
            return await chatmod.aggregate(
                _events(lines), model='trae/x', state=state)

        payload = _run(collect())
        message = payload['choices'][0]['message']
        self.assertEqual(message['content'], '只在思考')
        self.assertEqual(message['reasoning_content'], '只在思考')


class TextToolRescueTest(unittest.TestCase):
    """模型把调用写成正文（历史折叠的副作用）时要还原成 `tool_calls`。

    线上样本（2026-11-01）：deepseek-v4.1-flash 把 `edit` 的参数写进 `response`，
    客户端收到一条普通回复加 `finish_reason: "stop"`，Agent 循环当场停住，
    文件一个字都没改。
    """

    CALL = ('tool_call edit({"path": "web/a.tsx", "oldString": "x", '
            '"newString": "y"})')

    def _translate(self, *frames: str, **kwargs) -> tuple[list[dict], Any]:
        lines: list[str] = []
        for frame in frames:
            lines += TranslateTest._sse('output', {'response': frame})
        lines += TranslateTest._sse('done', {'finish_reason': 'stop'})
        state = chatmod.StreamState()

        async def collect() -> list[dict]:
            return [chunk async for chunk in chatmod.translate(
                _events(lines), model='trae/x', state=state, **kwargs)]

        return _run(collect()), state

    @staticmethod
    def _aggregate(*frames: str, **kwargs) -> tuple[dict, Any]:
        """同一段正文走非流式接口，用来钉住与流式同口径。"""
        lines: list[str] = []
        for frame in frames:
            lines += TranslateTest._sse('output', {'response': frame})
        lines += TranslateTest._sse('done', {'finish_reason': 'stop'})
        state = chatmod.StreamState()

        async def collect() -> dict:
            return await chatmod.aggregate(
                _events(lines), model='trae/x', state=state, **kwargs)

        return _run(collect()), state

    @staticmethod
    def _parts(chunks: list[dict]) -> tuple[str, list[dict]]:
        contents: list[str] = []
        calls: list[dict] = []
        for chunk in chunks:
            if 'choices' not in chunk:      # 错误帧没有 choices
                continue
            delta = chunk['choices'][0]['delta']
            if delta.get('content'):
                contents.append(delta['content'])
            calls.extend(delta.get('tool_calls') or [])
        return ''.join(contents), calls

    def test_text_call_becomes_tool_calls(self) -> None:
        chunks, state = self._translate(self.CALL)
        text, calls = self._parts(chunks)
        self.assertEqual(text, '', '被还原的调用不能再作为正文下发')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['type'], 'function')
        self.assertTrue(calls[0]['id'].startswith('call_rescue_'))
        self.assertEqual(calls[0]['function']['name'], 'edit')
        self.assertEqual(json.loads(calls[0]['function']['arguments']),
                         {'path': 'web/a.tsx', 'oldString': 'x', 'newString': 'y'})
        self.assertEqual(chunks[-1]['choices'][0]['finish_reason'], 'tool_calls')
        self.assertTrue(state.saw_content)

    def test_call_split_across_frames(self) -> None:
        """marker 和参数都可能被帧切开，不能半路就把文本放行。"""
        chunks, _ = self._translate('tool_ca', 'll edit({"path": "a"', '})')
        text, calls = self._parts(chunks)
        self.assertEqual(text, '')
        self.assertEqual(len(calls), 1)
        self.assertEqual(json.loads(calls[0]['function']['arguments']),
                         {'path': 'a'})

    def test_text_before_call_is_kept(self) -> None:
        chunks, _ = self._translate('我先改一下：\n' + self.CALL)
        text, calls = self._parts(chunks)
        self.assertEqual(text, '我先改一下：\n')
        self.assertEqual(len(calls), 1)

    def test_prose_mentioning_tool_call_passes_through(self) -> None:
        prose = '这里的 tool_call 字段是上游私有形状，不要手写。'
        chunks, _ = self._translate(prose)
        text, calls = self._parts(chunks)
        self.assertEqual(text, prose)
        self.assertEqual(calls, [])
        self.assertEqual(chunks[-1]['choices'][0]['finish_reason'], 'stop')

    def test_invalid_arguments_end_as_error(self) -> None:
        """参数不是合法 JSON 就不猜，但也不能伪装成正常回答。

        旧行为把这段当正文下发 + `stop`，客户端 Agent 循环当场停住。
        """
        broken = '[tool_call] edit({path: "a"})'
        chunks, _ = self._translate(broken)
        _text, calls = self._parts(chunks)
        self.assertEqual(calls, [])
        self.assertTrue(any((c.get('error') or {}).get('code') == 'degenerate_tool_call'
                            for c in chunks))
        self.assertEqual(chunks[-1]['choices'][0]['finish_reason'], 'error')

    def test_truncated_call_is_not_executed(self) -> None:
        """被 max_tokens 截断的半条调用不能变成工具调用，也不该静默结束。"""
        chunks, _ = self._translate('tool_call edit({"path": "a"')
        text, calls = self._parts(chunks)
        self.assertEqual(calls, [])
        self.assertEqual(text, '', '整轮只有半截调用时不应把残句当正文下发')
        self.assertTrue(any((c.get('error') or {}).get('code') == 'degenerate_tool_call'
                            for c in chunks))
        self.assertEqual(chunks[-1]['choices'][0]['finish_reason'], 'error')

    def test_degenerate_call_close_is_reported(self) -> None:
        """线上样本：`<tool_call>\\n</call>`（模型自造的残缺闭合标签）。"""
        chunks, state = self._translate('<tool_call>\n</call>')
        text, calls = self._parts(chunks)
        self.assertEqual(text, '')
        self.assertEqual(calls, [])
        self.assertTrue(any((c.get('error') or {}).get('code') == 'degenerate_tool_call'
                            for c in chunks))
        self.assertEqual(chunks[-1]['choices'][0]['finish_reason'], 'error')
        self.assertIsNotNone(state.error)

    def test_orphan_degenerate_close_is_reported(self) -> None:
        """整轮只剩一个 `</call>` / `</toolcall>` 也是退化，不是正常空回复。"""
        for body in ('</call>', '</toolcall>', '<toolcall>'):
            with self.subTest(body=body):
                chunks, _ = self._translate(body)
                _text, calls = self._parts(chunks)
                self.assertEqual(calls, [])
                self.assertTrue(any(
                    (c.get('error') or {}).get('code') == 'degenerate_tool_call'
                    for c in chunks))

    def test_degenerate_close_split_across_frames(self) -> None:
        """`</call>` 被帧切开时也要先扣住，不能先把开标签漏成正文。"""
        chunks, _ = self._translate('<tool_call>\n</ca', 'll>')
        text, calls = self._parts(chunks)
        self.assertEqual(text, '')
        self.assertEqual(calls, [])
        self.assertTrue(any((c.get('error') or {}).get('code') == 'degenerate_tool_call'
                            for c in chunks))

    def test_degenerate_error_can_be_disabled(self) -> None:
        """开关关掉后回到旧行为（空正文 + stop），给误判留退路。"""
        with mock.patch.object(config, 'TRAE_DEGENERATE_CALL_ERROR', False):
            chunks, _ = self._translate('<tool_call>\n</call>')
        _text, calls = self._parts(chunks)
        self.assertEqual(calls, [])
        self.assertFalse(any(c.get('error') for c in chunks))
        self.assertEqual(chunks[-1]['choices'][0]['finish_reason'], 'stop')

    def test_degenerate_error_requires_rescue(self) -> None:
        """关掉正文还原后不再判退化：那段文字按普通正文走（旧行为）。"""
        chunks, _ = self._translate('<tool_call>\n</call>',
                                    text_tool_rescue=False)
        _text, calls = self._parts(chunks)
        self.assertEqual(calls, [])
        self.assertFalse(any(c.get('error') for c in chunks))
        self.assertEqual(chunks[-1]['choices'][0]['finish_reason'], 'stop')

    def test_degenerate_aggregate_raises(self) -> None:
        """非流式同样不能返回空正文 + stop：直接报错。"""
        lines = (TranslateTest._sse('output', {'response': '<tool_call>\n</call>'})
                 + TranslateTest._sse('done', {'finish_reason': 'stop'}))
        state = chatmod.StreamState()

        async def collect() -> dict:
            return await chatmod.aggregate(
                _events(lines), model='trae/x', state=state)

        with self.assertRaises(PlatformError) as ctx:
            _run(collect())
        self.assertEqual(ctx.exception.code, 'degenerate_tool_call')

    def test_prose_with_orphan_close_still_completes(self) -> None:
        """有可读正文时只删标签噪音，不能误报成退化。"""
        lines = (TranslateTest._sse('output', {'response': '好。\n</call>'})
                 + TranslateTest._sse('done', {'finish_reason': 'stop'}))
        state = chatmod.StreamState()

        async def collect() -> dict:
            return await chatmod.aggregate(
                _events(lines), model='trae/x', state=state)

        payload = _run(collect())
        message = payload['choices'][0]['message']
        self.assertEqual(message['content'].strip(), '好。')
        self.assertEqual(payload['choices'][0]['finish_reason'], 'stop')

    def test_rescue_can_be_disabled(self) -> None:
        chunks, _ = self._translate(self.CALL, text_tool_rescue=False)
        text, calls = self._parts(chunks)
        self.assertEqual(text, self.CALL)
        self.assertEqual(calls, [])

    def test_aggregate_rescues_text_call(self) -> None:
        lines = (TranslateTest._sse('output', {'response': '先看一下\n' + self.CALL})
                 + TranslateTest._sse('done', {'finish_reason': 'stop'}))
        state = chatmod.StreamState()

        async def collect() -> dict:
            return await chatmod.aggregate(
                _events(lines), model='trae/x', state=state)

        payload = _run(collect())
        message = payload['choices'][0]['message']
        self.assertEqual(message['content'], '先看一下')
        self.assertEqual(message['tool_calls'][0]['function']['name'], 'edit')
        self.assertEqual(payload['choices'][0]['finish_reason'], 'tool_calls')

    def test_parser_handles_wrappers_and_empty_arguments(self) -> None:
        body = ('<tool_call>read({"path": "a"})</tool_call>\n'
                '[tool_call] write({"path": "b"})\n'
                'tool_call ping()')
        calls, rest = text_tools.parse_calls(body)
        self.assertEqual([call['name'] for call in calls],
                         ['read', 'write', 'ping'])
        self.assertEqual(calls[-1]['arguments'], '{}')
        self.assertEqual(rest.strip(), '')

    # ── 宽容开头与标签噪音 ────────────────────────────────
    def test_mixed_bracket_openers_are_rescued(self) -> None:
        """线上样本 `<tool_call] read({...})`：括号混用也要认成调用。"""
        body = ('<tool_call] read({"path": "a"})\n'
                '[tool_call> grep({"pattern": "x"})')
        chunks, _ = self._translate(body)
        text, calls = self._parts(chunks)
        self.assertEqual([call['function']['name'] for call in calls],
                         ['read', 'grep'])
        self.assertEqual(text.strip(), '')
        self.assertEqual(chunks[-1]['choices'][0]['finish_reason'], 'tool_calls')

    def test_mixed_bracket_opener_split_across_frames(self) -> None:
        """`<tool_call` 与 `]` 分属两帧时，尾巴要被扣住，不能先漏成正文。"""
        chunks, _ = self._translate('<tool_call', '] read({"path": "a"})')
        text, calls = self._parts(chunks)
        self.assertEqual(calls[0]['function']['name'], 'read')
        self.assertEqual(text, '')

    def test_empty_pair_is_dropped(self) -> None:
        chunks, _ = self._translate('<tool_call>\n</tool_call>')
        text, calls = self._parts(chunks)
        self.assertEqual(calls, [])
        self.assertEqual(text.strip(), '')
        self.assertTrue(any((c.get('error') or {}).get('code') == 'degenerate_tool_call'
                            for c in chunks))
        self.assertEqual(chunks[-1]['choices'][0]['finish_reason'], 'error')

    def test_orphan_closing_tag_is_dropped(self) -> None:
        chunks, _ = self._translate('好。\n</tool_call>')
        text, calls = self._parts(chunks)
        self.assertEqual(calls, [])
        self.assertEqual(text, '好。\n')

    def test_orphan_closing_tag_split_across_frames(self) -> None:
        chunks, _ = self._translate('好。\n</tool_ca', 'll>')
        text, calls = self._parts(chunks)
        self.assertEqual(calls, [])
        self.assertEqual(text, '好。\n')

    def test_invalid_call_still_passes_through(self) -> None:
        """「宁可漏，不可错」不变：解析不了的调用原样放行。"""
        broken = '[tool_call] edit({path: "a"})'
        calls, rest = text_tools.parse_calls(broken)
        self.assertEqual(calls, [])
        self.assertEqual(rest, broken)

    # ── JSON 载荷形态 ──────────────────────────────────────
    def test_json_payload_becomes_tool_calls(self) -> None:
        payload = '[tool_call] {"name": "edit", "arguments": {"path": "a"}}'
        chunks, _ = self._translate(payload)
        text, calls = self._parts(chunks)
        self.assertEqual(text, '')
        self.assertEqual(calls[0]['function']['name'], 'edit')
        self.assertEqual(json.loads(calls[0]['function']['arguments']),
                         {'path': 'a'})
        self.assertEqual(chunks[-1]['choices'][0]['finish_reason'], 'tool_calls')

    def test_json_payload_accepts_function_wrapper(self) -> None:
        payload = ('<tool_call>{"function": {"name": "read", '
                   '"arguments": "{\\"path\\": \\"a\\"}"}}</tool_call>')
        calls, rest = text_tools.parse_calls(payload)
        self.assertEqual(calls, [{'name': 'read', 'arguments': '{"path": "a"}'}])
        self.assertEqual(rest, '')

    # ── 标签形态：只认客户端声明过的工具 ──────────────────
    def test_tag_form_uses_declared_tool_name(self) -> None:
        chunks, _ = self._translate('<execute code="ls -la"></execute>',
                                    tool_names=['execute'])
        text, calls = self._parts(chunks)
        self.assertEqual(text, '')
        self.assertEqual(calls[0]['function']['name'], 'execute')
        self.assertEqual(json.loads(calls[0]['function']['arguments']),
                         {'code': 'ls -la'})

    def test_tag_alias_maps_to_declared_shell_tool(self) -> None:
        """opencode 只声明 `bash`：`<execute>` 要落到 bash，参数改名成 command。"""
        chunks, _ = self._translate('<execute code="ls -la"></execute>',
                                    tool_names=['bash', 'edit', 'read'])
        _text, calls = self._parts(chunks)
        self.assertEqual(calls[0]['function']['name'], 'bash')
        self.assertEqual(json.loads(calls[0]['function']['arguments']),
                         {'command': 'ls -la'})

    def test_tag_body_fills_primary_argument(self) -> None:
        chunks, _ = self._translate('<shell><command>git status</command></shell>',
                                    tool_names=['bash'])
        _text, calls = self._parts(chunks)
        self.assertEqual(calls[0]['function']['name'], 'bash')
        self.assertEqual(json.loads(calls[0]['function']['arguments']),
                         {'command': 'git status'})

    def test_tag_body_uses_command_for_shell_tool(self) -> None:
        """`<bash>ls -la</bash>` 的正文要落到 `command`，不是 `input`。"""
        calls, _rest = text_tools.parse_calls('<bash>ls -la</bash>', ['bash'])
        self.assertEqual(calls[0]['name'], 'bash')
        self.assertEqual(json.loads(calls[0]['arguments']), {'command': 'ls -la'})

    def test_tag_split_across_frames(self) -> None:
        chunks, _ = self._translate('先看：\n<exe', 'cute code="ls"',
                                    '></execute>', tool_names=['bash'])
        text, calls = self._parts(chunks)
        self.assertEqual(text, '先看：\n')
        self.assertEqual(calls[0]['function']['name'], 'bash')

    def test_tag_rescue_needs_declared_tool_names(self) -> None:
        """没声明工具（或没给白名单）时，标签就是正文，不能猜。"""
        source = '<execute code="ls"></execute>'
        chunks, _ = self._translate(source)
        text, calls = self._parts(chunks)
        self.assertEqual(calls, [])
        self.assertEqual(text, source)

    def test_unknown_tag_passes_through(self) -> None:
        source = '<unknown code="x"></unknown>'
        chunks, _ = self._translate(source, tool_names=['bash'])
        text, calls = self._parts(chunks)
        self.assertEqual(calls, [])
        self.assertEqual(text, source)

    def test_plain_less_than_in_prose_is_not_held_forever(self) -> None:
        chunks, _ = self._translate('当 a < b 且 c > d 时成立。',
                                    tool_names=['bash'])
        text, calls = self._parts(chunks)
        self.assertEqual(text, '当 a < b 且 c > d 时成立。')
        self.assertEqual(calls, [])

    def test_native_call_flushes_pending_text_in_order(self) -> None:
        """原生调用到达时，缓冲区里的半截正文要先补发，不能排到后面去。"""
        lines = (TranslateTest._sse('output', {'response': '先看看 tool_ca'})
                 + TranslateTest._sse('output', {'tool_calls': [
                     {'index': 0, 'id': 'c1', 'type': 'function',
                      'function_call': {'name': 'bash',
                                        'arguments': '{"command": "ls"}'}}]})
                 + TranslateTest._sse('output', {'response': '然后继续'})
                 + TranslateTest._sse('done', {'finish_reason': 'stop'}))
        state = chatmod.StreamState()

        async def collect() -> list[dict]:
            return [chunk async for chunk in chatmod.translate(
                _events(lines), model='trae/x', state=state)]

        chunks = _run(collect())
        text, calls = self._parts(chunks)
        self.assertEqual(text, '先看看 tool_ca然后继续')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['function']['name'], 'bash')

    # ── 第二轮：收窄误判面（2026-11-02）──────────────────────
    def test_html_closing_tag_is_not_degenerate(self) -> None:
        """普通 HTML 闭合标签是正文，不是工具脚手架。

        线上样本：只让模型输出 `</div>`，上一版返回正文 + `degenerate_tool_call`
        错误帧，客户端把一条正常回答当成失败。
        """
        for body in ('</div>', '</a>', '</span>', '</p>'):
            with self.subTest(body=body):
                chunks, _ = self._translate(body)
                text, calls = self._parts(chunks)
                self.assertEqual(text, body)
                self.assertEqual(calls, [])
                self.assertFalse(any(c.get('error') for c in chunks))
                self.assertEqual(chunks[-1]['choices'][0]['finish_reason'], 'stop')

    def test_fenced_example_is_not_rescued(self) -> None:
        """代码块里演示的调用是「讲解格式」，不能真的执行。"""
        body = '格式是这样：\n```\n[tool_call] bash({"command": "ls"})\n```\n'
        chunks, _ = self._translate(body, tool_names=['bash'])
        text, calls = self._parts(chunks)
        self.assertEqual(calls, [])
        self.assertIn('[tool_call] bash', text)
        self.assertIn('```', text)
        self.assertEqual(chunks[-1]['choices'][0]['finish_reason'], 'stop')

    def test_inline_code_example_is_not_rescued(self) -> None:
        body = '写成 `[tool_call] bash({"command": "ls"})` 这样是不行的。'
        chunks, _ = self._translate(body, tool_names=['bash'])
        text, calls = self._parts(chunks)
        self.assertEqual(calls, [])
        self.assertEqual(text, body)

    def test_prose_example_with_tail_is_not_rescued(self) -> None:
        """调用后面还接着正文 → 散文里的举例，不能执行。"""
        body = '我建议写成 [tool_call] bash({"command": "ls"}) 这种，不过仅供参考。'
        chunks, _ = self._translate(body, tool_names=['bash'])
        text, calls = self._parts(chunks)
        self.assertEqual(calls, [])
        self.assertEqual(text, body)

    def test_tail_rule_can_be_disabled(self) -> None:
        """逃生开关：关掉尾部规则后回到「见 marker 就还原」的旧行为。"""
        body = '我建议写成 [tool_call] bash({"command": "ls"}) 这种，不过仅供参考。'
        chunks, _ = self._translate(body, tool_names=['bash'], strict_tail=False)
        _text, calls = self._parts(chunks)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['function']['name'], 'bash')

    def test_placeholder_arguments_are_not_rescued(self) -> None:
        """模型复述协议提示时抄下来的 `...` 是示例，不是命令。"""
        body = '[tool_call] bash({"command": "..."})'
        chunks, _ = self._translate(body, tool_names=['bash'])
        text, calls = self._parts(chunks)
        self.assertEqual(calls, [])
        self.assertEqual(text, body)

    def test_marker_alias_maps_tool_and_argument(self) -> None:
        """正文形态也走白名单：`execute` → `bash`，`code` → `command`。"""
        body = '[tool_call] execute({"code": "ls"})'
        chunks, _ = self._translate(body, tool_names=['bash', 'edit'])
        _text, calls = self._parts(chunks)
        self.assertEqual(calls[0]['function']['name'], 'bash')
        self.assertEqual(json.loads(calls[0]['function']['arguments']),
                         {'command': 'ls'})

    def test_marker_unknown_name_is_not_dropped(self) -> None:
        """对不上任何已声明工具时原样下发，客户端报 unknown tool，不要静默丢。"""
        body = '[tool_call] frobnicate({"x": 1})'
        chunks, _ = self._translate(body, tool_names=['bash'])
        _text, calls = self._parts(chunks)
        self.assertEqual(calls[0]['function']['name'], 'frobnicate')

    def test_unclosed_recognized_tag_is_degenerate(self) -> None:
        """只写出 `<execute code="ls">` 就停 → 退化错误，不要静默停住。"""
        chunks, _ = self._translate('<execute code="ls -la">', tool_names=['bash'])
        text, calls = self._parts(chunks)
        self.assertEqual((text, calls), ('', []))
        self.assertTrue(any((c.get('error') or {}).get('code') == 'degenerate_tool_call'
                            for c in chunks))
        self.assertEqual(chunks[-1]['choices'][0]['finish_reason'], 'error')

    def test_unclosed_tag_in_prose_is_not_degenerate(self) -> None:
        """散文里提到一个未闭合的已声明标签，不能把后文全判成退化。"""
        body = ('用 <read> 这种标签表示读取文件。' * 40) + '这就是说明。'
        chunks, _ = self._translate(body, tool_names=['bash', 'read'])
        text, calls = self._parts(chunks)
        self.assertEqual(calls, [])
        self.assertFalse(any(c.get('error') for c in chunks))
        self.assertIn('这就是说明。', text)

    def test_index_allocator_keeps_rescued_and_native_apart(self) -> None:
        allocator = chatmod._IndexAllocator()
        rescued = allocator.rescue(2)
        native = allocator.native(0)
        self.assertNotIn(native, (rescued, rescued + 1))
        self.assertEqual(allocator.native(0), native, '同一上游 index 要多帧复用')

    def test_text_and_native_calls_do_not_merge_arguments(self) -> None:
        """同轮里正文调用与原生调用不能落在同一个 index 上。

        线上样本：两条被客户端按 index 合并成
        `{"command":"ls"}{"path":"a"}`（非法 JSON）。正文调用现在在块闭合时
        就立刻下发，原生通道随后到达时由 `_IndexAllocator` 分到独立区间。
        """
        lines = (TranslateTest._sse('output', {
                     'response': '先看目录。\n[tool_call] bash({"command": "ls"})\n'})
                 + TranslateTest._sse('output', TranslateTest._tool_frame(
                     0, call_id='c_native', name='read', arguments='{"path": "a"}'))
                 + TranslateTest._sse('done', {'finish_reason': 'stop'}))
        state = chatmod.StreamState()

        async def collect() -> list[dict]:
            return [c async for c in chatmod.translate(
                _events(lines), model='trae/x', state=state,
                tool_names=['bash', 'read'])]

        chunks = _run(collect())
        text, calls = self._parts(chunks)
        self.assertNotIn('[tool_call] bash', text)
        self.assertEqual([c['function']['name'] for c in calls],
                         ['bash', 'read'])
        self.assertEqual(json.loads(calls[0]['function']['arguments']),
                         {'command': 'ls'})
        self.assertEqual(json.loads(calls[1]['function']['arguments']),
                         {'path': 'a'})
        self.assertEqual(len({c['index'] for c in calls}), 2,
                         '两条调用不能共用 index，否则客户端会合并参数')

    def test_truncated_native_arguments_are_reported(self) -> None:
        """原生参数被 max_tokens 截断：明确报 `truncated_tool_call` + length。"""
        lines = (TranslateTest._sse('output', TranslateTest._tool_frame(
                     0, call_id='c1', name='edit',
                     arguments='{"path": "a.ts", "oldSt'))
                 + TranslateTest._sse('done', {'finish_reason': 'length'}))
        state = chatmod.StreamState()

        async def collect() -> list[dict]:
            return [c async for c in chatmod.translate(
                _events(lines), model='trae/x', state=state)]

        chunks = _run(collect())
        self.assertTrue(any(
            (c.get('error') or {}).get('code') == 'truncated_tool_call'
            for c in chunks))
        self.assertEqual(chunks[-1]['choices'][0]['finish_reason'], 'length')

    def test_aggregate_truncated_native_call_raises(self) -> None:
        lines = (TranslateTest._sse('output', TranslateTest._tool_frame(
                     0, call_id='c1', name='edit',
                     arguments='{"path": "a.ts", "oldSt'))
                 + TranslateTest._sse('done', {'finish_reason': 'length'}))
        state = chatmod.StreamState()

        async def collect() -> dict:
            return await chatmod.aggregate(
                _events(lines), model='trae/x', state=state)

        with self.assertRaises(PlatformError) as ctx:
            _run(collect())
        self.assertEqual(ctx.exception.code, 'truncated_tool_call')

    # ── 第十轮：判据改成「调用是否独占一行」（2026-11-04）──────
    def test_call_on_own_line_before_html_prose_is_rescued(self) -> None:
        """调用独占一行 → 执行；后面另起一段的 `<div>` 说明不影响判定。

        旧判据要求「调用之后到结尾只剩空白」，于是把「后面还有正文」当成
        散文里的举例。它必须看到整段文本才能定论，流式天然做不到——同一段
        输出在流式/非流式下结论不同。现在只看调用所在行的两侧。
        """
        body = ('先看目录：\n[tool_call] bash({"command": "ls"})'
                '\n<div>后面还有说明</div>')
        chunks, _state = self._translate(body, tool_names=['bash'])
        text, calls = self._parts(chunks)
        self.assertEqual([c['function']['name'] for c in calls], ['bash'])
        self.assertNotIn('[tool_call]', text)
        self.assertIn('先看目录：', text)
        self.assertIn('<div>后面还有说明</div>', text)

    def test_aggregate_call_on_own_line_before_html_prose_is_rescued(self) -> None:
        body = ('先看目录：\n[tool_call] bash({"command": "ls"})'
                '\n<div>后面还有说明</div>')
        result, _state = self._aggregate(body, tool_names=['bash'])
        message = result['choices'][0]['message']
        self.assertEqual(message['tool_calls'][0]['function']['name'], 'bash')
        self.assertNotIn('[tool_call]', message['content'])
        self.assertIn('<div>后面还有说明</div>', message['content'])

    def test_aggregate_placeholder_is_not_rescued(self) -> None:
        """非流式也认占位符：`{"command": "..."}` 是示例，不是命令。"""
        body = '[tool_call] bash({"command": "..."})'
        result, _state = self._aggregate(body, tool_names=['bash'])
        message = result['choices'][0]['message']
        self.assertNotIn('tool_calls', message)
        self.assertEqual(message['content'], body)
        self.assertEqual(result['choices'][0]['finish_reason'], 'stop')

    def test_aggregate_prose_plus_unclosed_tag_is_degenerate(self) -> None:
        """非流式也要报「未闭合标签」：只发正文 + stop 会让 Agent 静默停住。"""
        body = '先看目录。\n<execute code="ls -la">'
        with self.assertRaises(PlatformError) as ctx:
            self._aggregate(body, tool_names=['bash'])
        self.assertEqual(ctx.exception.code, 'degenerate_tool_call')

    def test_aggregate_prose_plus_truncated_text_call_is_degenerate(self) -> None:
        body = '先看目录。\n[tool_call] bash({"command": "ls"'
        with self.assertRaises(PlatformError) as ctx:
            self._aggregate(body, tool_names=['bash'])
        self.assertEqual(ctx.exception.code, 'degenerate_tool_call')

    def test_unclosed_tag_inside_unterminated_fence_is_not_degenerate(self) -> None:
        """围栏里的示例不判退化（围栏未闭合也一样）。"""
        body = '看这个例子：\n```\n<execute code="ls -la">'
        result, _state = self._aggregate(body, tool_names=['bash'])
        message = result['choices'][0]['message']
        self.assertEqual(message['content'], body)
        self.assertEqual(result['choices'][0]['finish_reason'], 'stop')


class _RescueHarness(unittest.TestCase):
    """`translate()` / `aggregate()` 的共用夹具（不含用例本身）。"""

    def _translate(self, *frames: str, **kwargs) -> tuple[list[dict], Any]:
        lines: list[str] = []
        for frame in frames:
            lines += TranslateTest._sse('output', {'response': frame})
        lines += TranslateTest._sse('done', {'finish_reason': 'stop'})
        state = chatmod.StreamState()

        async def collect() -> list[dict]:
            return [chunk async for chunk in chatmod.translate(
                _events(lines), model='trae/x', state=state, **kwargs)]

        return _run(collect()), state

    def _aggregate(self, *frames: str, **kwargs) -> tuple[dict, Any]:
        lines: list[str] = []
        for frame in frames:
            lines += TranslateTest._sse('output', {'response': frame})
        lines += TranslateTest._sse('done', {'finish_reason': 'stop'})
        state = chatmod.StreamState()

        async def collect() -> dict:
            return await chatmod.aggregate(
                _events(lines), model='trae/x', state=state, **kwargs)

        return _run(collect()), state

    @staticmethod
    def _parts(chunks: list[dict]) -> tuple[str, list[dict]]:
        contents: list[str] = []
        calls: list[dict] = []
        for chunk in chunks:
            if 'choices' not in chunk:
                continue
            delta = chunk['choices'][0]['delta']
            if delta.get('content'):
                contents.append(delta['content'])
            calls.extend(delta.get('tool_calls') or [])
        return ''.join(contents), calls

    def _errors(self, chunks: list[dict]) -> list[str]:
        return [chunk['error']['code'] for chunk in chunks if 'error' in chunk]


class TraeMarkerTest(_RescueHarness):
    """Trae 原生文本工具标记（`<|FunctionCallBegin|>…<|FunctionCallEnd|>`）。

    参考实现 trae2api-hub 的 `ToolMarkerStream`。旧版本项目不认这个格式，
    整段标记会当正文透传，客户端拿到普通回复 + `stop`，Agent 循环停住。
    """

    MARKER = ('<|FunctionCallBegin|>[{"name":"bash",'
              '"parameters":{"command":"ls"}}]<|FunctionCallEnd|>')

    def test_stream_marker_becomes_tool_calls(self) -> None:
        chunks, _ = self._translate(self.MARKER)
        text, calls = self._parts(chunks)
        self.assertEqual(text, '')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['function']['name'], 'bash')
        self.assertEqual(json.loads(calls[0]['function']['arguments']),
                         {'command': 'ls'})

    def test_marker_split_across_frames(self) -> None:
        chunks, _ = self._translate(
            '<|FunctionCallBegin|>[{"name":"ba',
            'sh","parameters":{"command":"ls"}}]<|FunctionCallEnd|>')
        text, calls = self._parts(chunks)
        self.assertEqual(text, '')
        self.assertEqual([c['function']['name'] for c in calls], ['bash'])

    def test_marker_can_hold_multiple_calls(self) -> None:
        marker = ('<|FunctionCallBegin|>['
                  '{"name":"bash","parameters":{"command":"ls"}},'
                  '{"name":"read","parameters":{"path":"a.py"}}'
                  ']<|FunctionCallEnd|>')
        chunks, _ = self._translate(marker, tool_names=('bash', 'read'))
        _text, calls = self._parts(chunks)
        self.assertEqual([c['function']['name'] for c in calls],
                         ['bash', 'read'])

    def test_stacked_marker_begin_is_skipped(self) -> None:
        """模型把开头连写两遍时，不能把 JSON 载荷打断。"""
        chunks, _ = self._translate(
            '<|FunctionCallBegin|>\n<|FunctionCallBegin|>[{"name":"bash",'
            '"parameters":{"command":"ls"}}]<|FunctionCallEnd|>')
        _text, calls = self._parts(chunks)
        self.assertEqual([c['function']['name'] for c in calls], ['bash'])

    def test_marker_inside_fence_is_plain_text(self) -> None:
        """代码块里是在讲解格式，不能执行。"""
        chunks, _ = self._translate('```\n' + self.MARKER + '\n```')
        text, calls = self._parts(chunks)
        self.assertEqual(calls, [])
        self.assertIn('bash', text)

    def test_truncated_marker_reports_degenerate(self) -> None:
        chunks, _ = self._translate('<|FunctionCallBegin|>[{"name":"bash"')
        text, calls = self._parts(chunks)
        self.assertEqual((text, calls), ('', []))
        self.assertIn('degenerate_tool_call', self._errors(chunks))

    def test_aggregate_marker_becomes_tool_calls(self) -> None:
        payload, _ = self._aggregate(self.MARKER)
        message = payload['choices'][0]['message']
        self.assertIsNone(message['content'])
        self.assertEqual(message['tool_calls'][0]['function']['name'], 'bash')
        self.assertEqual(payload['choices'][0]['finish_reason'], 'tool_calls')


class DsmlToolCallTest(_RescueHarness):
    """DeepSeek DSML 形态的正文调用。

    2026-11-02 实测：Trae 侧完全不认这些令牌，既不会还原成 `tool_calls`，
    也不会判成退化——原样漏给客户端；「整轮只有 DSML」于是退化成
    空正文 + `stop`，Agent 循环静默停住。Qoder 侧早有
    `parse_dsml_tool_calls()`，Trae 这里补齐。
    """

    PIPE = '\uff5c\uff5c'          # 全角竖线 x2
    BAR = '|'
    DSML = 'DSML'

    @classmethod
    def _tok(cls, text: str, style: str = 'full') -> str:
        sep = cls.PIPE if style == 'full' else cls.BAR
        return '<' + sep + cls.DSML + sep + text + '>'

    @classmethod
    def _close(cls, text: str, style: str = 'full') -> str:
        sep = cls.PIPE if style == 'full' else cls.BAR
        return '</' + sep + cls.DSML + sep + text + '>'

    @classmethod
    def _block(cls, name: str, params: dict[str, str], *,
               wrapper: bool = True, style: str = 'full') -> str:
        parts: list[str] = []
        if wrapper:
            parts.append(cls._tok('tool_calls', style))
        parts.append(cls._tok(f'invoke name="{name}"', style))
        for key, value in params.items():
            parts.append(cls._tok(f'parameter name="{key}"', style)
                         + value + cls._close('parameter', style))
        parts.append(cls._close('invoke', style))
        if wrapper:
            parts.append(cls._close('tool_calls', style))
        return '\n'.join(parts)

    def test_full_width_call_is_rescued(self) -> None:
        body = self._block('bash', {'command': 'ls -la'})
        chunks, _state = self._translate(body, tool_names=['bash'])
        text, calls = self._parts(chunks)
        self.assertEqual(text, '', 'DSML 调用不能再作为正文下发')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['function']['name'], 'bash')
        self.assertEqual(json.loads(calls[0]['function']['arguments']),
                         {'command': 'ls -la'})
        self.assertEqual(chunks[-1]['choices'][0]['finish_reason'], 'tool_calls')

    def test_half_width_call_is_rescued(self) -> None:
        body = self._block('bash', {'command': 'ls -la'}, style='half')
        _chunks, calls = self._parts(self._translate(body, tool_names=['bash'])[0])
        self.assertEqual(len(calls), 1)
        self.assertEqual(json.loads(calls[0]['function']['arguments']),
                         {'command': 'ls -la'})

    def test_call_without_calls_wrapper_is_rescued(self) -> None:
        """线上出现过只有 invoke/parameter、没有 calls 包裹的形态。"""
        body = self._block('bash', {'command': 'ls'}, wrapper=False)
        _chunks, calls = self._parts(self._translate(body, tool_names=['bash'])[0])
        self.assertEqual(len(calls), 1)
        self.assertEqual(json.loads(calls[0]['function']['arguments']),
                         {'command': 'ls'})

    def test_two_invokes_become_two_calls(self) -> None:
        body = '\n'.join([
            self._tok('tool_calls'),
            self._tok('invoke name="read"'),
            self._tok('parameter name="path"') + 'a.txt' + self._close('parameter'),
            self._close('invoke'),
            self._tok('invoke name="bash"'),
            self._tok('parameter name="command"') + 'ls' + self._close('parameter'),
            self._close('invoke'),
            self._close('tool_calls'),
        ])
        _chunks, calls = self._parts(
            self._translate(body, tool_names=['bash', 'read'])[0])
        self.assertEqual([c['function']['name'] for c in calls], ['read', 'bash'])

    def test_call_split_across_frames(self) -> None:
        """令牌与参数都会被帧切开，不能半路就把文本放行。"""
        body = self._block('bash', {'command': 'ls -la'})
        frames = [body[i:i + 7] for i in range(0, len(body), 7)]
        _chunks, calls = self._parts(
            self._translate(*frames, tool_names=['bash'])[0])
        self.assertEqual(len(calls), 1)
        self.assertEqual(json.loads(calls[0]['function']['arguments']),
                         {'command': 'ls -la'})

    def test_alias_maps_to_declared_tool(self) -> None:
        """`<invoke name="execute">` 落到客户端声明的 `bash`，参数名一并改。"""
        body = self._block('execute', {'code': 'ls -la'})
        _chunks, calls = self._parts(self._translate(body, tool_names=['bash'])[0])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['function']['name'], 'bash')
        self.assertEqual(json.loads(calls[0]['function']['arguments']),
                         {'command': 'ls -la'})

    def test_placeholder_is_not_rescued(self) -> None:
        body = self._block('bash', {'command': '...'})
        chunks, _state = self._translate(body, tool_names=['bash'])
        text, calls = self._parts(chunks)
        self.assertEqual(calls, [])
        self.assertIn('...', text)

    def test_fenced_example_is_not_rescued(self) -> None:
        body = '格式是这样：\n```\n' + self._block('bash', {'command': 'ls'}) + '\n```'
        chunks, _state = self._translate(body, tool_names=['bash'])
        text, calls = self._parts(chunks)
        self.assertEqual(calls, [])
        self.assertIn('```', text)

    def test_prose_after_call_is_not_rescued(self) -> None:
        """调用后面还接着正文 → 散文里的举例，不执行。"""
        body = '我先看看目录。' + self._block('bash', {'command': 'ls'}) + '然后继续分析。'
        chunks, _state = self._translate(body, tool_names=['bash'])
        _text, calls = self._parts(chunks)
        self.assertEqual(calls, [])

    def test_prose_before_trailing_call_is_rescued(self) -> None:
        body = '我先看看目录。\n' + self._block('bash', {'command': 'ls'})
        chunks, _state = self._translate(body, tool_names=['bash'])
        text, calls = self._parts(chunks)
        self.assertEqual(len(calls), 1)
        self.assertIn('我先看看目录。', text)

    def test_close_residue_is_stripped_from_prose(self) -> None:
        """正文尾部漏一个 DSML 闭合残片：删掉残片，正文照常下发。"""
        body = '好的，我看看目录。' + self._close('parameter')
        chunks, _state = self._translate(body, tool_names=['bash'])
        text, calls = self._parts(chunks)
        self.assertEqual(calls, [])
        self.assertEqual(text, '好的，我看看目录。')

    def test_only_residue_is_degenerate(self) -> None:
        body = self._close('parameter')
        with self.assertRaises(PlatformError) as ctx:
            self._aggregate(body, tool_names=['bash'])
        self.assertEqual(ctx.exception.code, 'degenerate_tool_call')

    def test_truncated_invoke_is_degenerate(self) -> None:
        """块没闭合（参数被截断）：必须报错，不能把脚手架当正文。"""
        body = self._tok('invoke name="bash"') + '\n' + self._tok(
            'parameter name="command"') + 'ls'
        with self.assertRaises(PlatformError) as ctx:
            self._aggregate(body, tool_names=['bash'])
        self.assertEqual(ctx.exception.code, 'degenerate_tool_call')

    def test_aggregate_call_is_rescued(self) -> None:
        """非流式与流式同口径。"""
        body = self._block('bash', {'command': 'ls -la'})
        result, _state = self._aggregate(body, tool_names=['bash'])
        message = result['choices'][0]['message']
        self.assertEqual(len(message['tool_calls']), 1)
        self.assertEqual(
            json.loads(message['tool_calls'][0]['function']['arguments']),
            {'command': 'ls -la'})
        self.assertEqual(result['choices'][0]['finish_reason'], 'tool_calls')

    # ── 2026-11-03 线上样本：无名 invoke / 无引号 name ──────────────

    def _unnamed_block(self, params: list[tuple[str, str]]) -> str:
        """`invoke` 不写 name，闭合用 `</…tool_call>`（线上就是这种）。"""
        parts = [self._tok('tool_calls'), self._tok('tool_call'),
                 self._tok('invoke')]
        for key, value in params:
            parts.append(self._tok(f'parameter name="{key}"') + value
                         + self._close('parameter'))
        parts.append(self._close('tool_call'))
        parts.append(self._close('tool_calls'))
        return '\n'.join(parts)

    def test_unnamed_invoke_is_not_leaked(self) -> None:
        """线上原样：`invoke` 缺 name，开标签以前会整串漏进回复。"""
        body = self._unnamed_block([
            ('pattern', '操作成功'),
            ('path', r'D:\work\MES-dev\mes-server\src\common'),
            ('include', '*.ts'),
        ])
        chunks, _state = self._translate(body, tool_names=['grep'])
        text, calls = self._parts(chunks)
        self.assertEqual(calls, [])
        self.assertEqual(text, '', '无名调用块的标签/参数都不能当正文下发')
        self.assertIn('degenerate_tool_call', self._errors(chunks))

    def test_unnamed_invoke_is_degenerate_when_aggregated(self) -> None:
        body = self._unnamed_block([('command', 'ls -la')])
        with self.assertRaises(PlatformError) as ctx:
            self._aggregate(body, tool_names=['bash'])
        self.assertEqual(ctx.exception.code, 'degenerate_tool_call')

    def test_unnamed_invoke_split_across_frames(self) -> None:
        body = self._unnamed_block([('command', 'ls -la')])
        frames = [body[i:i + 5] for i in range(0, len(body), 5)]
        chunks, _state = self._translate(*frames, tool_names=['bash'])
        text, calls = self._parts(chunks)
        self.assertEqual(calls, [])
        self.assertEqual(text, '')
        self.assertIn('degenerate_tool_call', self._errors(chunks))

    def test_unnamed_invoke_keeps_prose(self) -> None:
        """前面有正文：只丢掉无名调用块，正文照常下发。"""
        body = '我先搜一下。\n' + self._unnamed_block([
            ('pattern', '操作成功'),
        ])
        chunks, _state = self._translate(body, tool_names=['grep'])
        text, calls = self._parts(chunks)
        self.assertEqual(calls, [])
        self.assertEqual(text, '我先搜一下。\n')
        self.assertEqual(self._errors(chunks), [])

    def test_unquoted_invoke_name_is_rescued(self) -> None:
        """`invoke name=grep>`（没有引号）同样是合法调用。"""
        body = '\n'.join([
            self._tok('tool_calls'),
            self._tok('invoke name=grep'),
            self._tok('parameter name="pattern"') + '操作成功'
            + self._close('parameter'),
            self._close('invoke'),
            self._close('tool_calls'),
        ])
        chunks, _state = self._translate(body, tool_names=['grep'])
        text, calls = self._parts(chunks)
        self.assertEqual(text, '')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['function']['name'], 'grep')
        self.assertEqual(json.loads(calls[0]['function']['arguments']),
                         {'pattern': '操作成功'})


class FlatParameterCallTest(_RescueHarness):
    """`<tool_call>` + `<parameter …>`：模型把 invoke 头丢掉后的形态。

    2026-11-03 线上样本（deepseek-v4.1-flash）：整段

        <tool_call>
        <parameter name="path">…/build_doc.py</parameter>
        <parameter name="offset">138</parameter>

    原样出现在回复里，工具没有执行。没有工具名就无法执行，但**绝不能当正文
    下发**：整块丢弃，整轮只剩它时报 `degenerate_tool_call`。
    """

    STATELESS = '\n'.join([
        '<tool_call>',
        r'<parameter name="path">C:\Users\1222\build_doc.py</parameter>',
        '<parameter name="offset">138</parameter>',
        '<parameter name="limit">25</parameter>',
        '</tool_call>',
    ])

    CMD = '\n'.join([
        '<tool_call>',
        '<parameter name="command">uv run build_doc.py</parameter>',
        r'<parameter name="workdir">D:\work\MES-dev</parameter>',
    ])

    def test_nameless_params_are_not_leaked(self) -> None:
        chunks, _state = self._translate(self.STATELESS, tool_names=['read'])
        text, calls = self._parts(chunks)
        self.assertEqual(calls, [])
        self.assertEqual(text, '')
        self.assertIn('degenerate_tool_call', self._errors(chunks))

    def test_nameless_command_is_not_leaked(self) -> None:
        chunks, _state = self._translate(self.CMD, tool_names=['bash'])
        text, calls = self._parts(chunks)
        self.assertEqual(calls, [])
        self.assertEqual(text, '')
        self.assertIn('degenerate_tool_call', self._errors(chunks))

    def test_nameless_params_are_degenerate_when_aggregated(self) -> None:
        with self.assertRaises(PlatformError) as ctx:
            self._aggregate(self.STATELESS, tool_names=['read'])
        self.assertEqual(ctx.exception.code, 'degenerate_tool_call')

    def test_nameless_params_split_across_frames(self) -> None:
        frames = [self.CMD[i:i + 3] for i in range(0, len(self.CMD), 3)]
        chunks, _state = self._translate(*frames, tool_names=['bash'])
        text, calls = self._parts(chunks)
        self.assertEqual(calls, [])
        self.assertEqual(text, '')
        self.assertIn('degenerate_tool_call', self._errors(chunks))

    def test_nameless_params_keep_prose(self) -> None:
        body = '我先读一下这个文件。\n' + self.STATELESS
        chunks, _state = self._translate(body, tool_names=['read'])
        text, calls = self._parts(chunks)
        self.assertEqual(calls, [])
        self.assertEqual(text, '我先读一下这个文件。\n')
        self.assertEqual(self._errors(chunks), [])

    def test_function_token_is_rescued(self) -> None:
        """`<function=read>` + `<parameter=path>`：名字在，照常还原。"""
        body = '\n'.join([
            '<tool_call>',
            '<function=read>',
            r'<parameter=path>C:\a.py</parameter>',
            '<parameter=offset>138</parameter>',
            '</tool_call>',
        ])
        chunks, _state = self._translate(body, tool_names=['read'])
        text, calls = self._parts(chunks)
        self.assertEqual(text, '')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['function']['name'], 'read')
        self.assertEqual(json.loads(calls[0]['function']['arguments']),
                         {'path': r'C:\a.py', 'offset': '138'})

    def test_bare_name_is_rescued(self) -> None:
        body = '\n'.join([
            '<tool_call>',
            'read',
            '<parameter name="path">a.py</parameter>',
            '</tool_call>',
        ])
        chunks, _state = self._translate(body, tool_names=['read'])
        text, calls = self._parts(chunks)
        self.assertEqual(text, '')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['function']['name'], 'read')
        self.assertEqual(json.loads(calls[0]['function']['arguments']),
                         {'path': 'a.py'})

    def test_aggregate_rescues_function_token(self) -> None:
        body = '\n'.join([
            '<tool_call>',
            '<function=bash>',
            '<parameter=command>ls -la</parameter>',
            '</tool_call>',
        ])
        result, _state = self._aggregate(body, tool_names=['bash'])
        message = result['choices'][0]['message']
        self.assertEqual(len(message['tool_calls']), 1)
        self.assertEqual(json.loads(message['tool_calls'][0]['function']['arguments']),
                         {'command': 'ls -la'})

    def test_nameless_params_without_closer_are_not_leaked(self) -> None:
        """块没闭合（模型直接停住）：同样不能把半截标签漏出去。"""
        body = '\n'.join([
            '<tool_call>',
            '<parameter name="command">uv run build_doc.py</parameter>',
        ])
        chunks, _state = self._translate(body, tool_names=['bash'])
        text, _calls = self._parts(chunks)
        self.assertEqual(text, '')
        self.assertIn('degenerate_tool_call', self._errors(chunks))


class BareMarkerSplitTest(unittest.TestCase):
    """裸标记 `tool_call` 恰好停在帧尾时不能急着放行。

    2026-11-02 线上复现：一次大参数 `tool_call write({...})` 在流式下整段漏成
    正文。原因是一帧正好停在 `tool_call`（后面还没到 `write(`），`_BARE` 的前瞻
    不成立，嗅探器找不到标记，就把这半截当正文发出去了——后面的参数再也接不
    回来。非流式因为能看到完整文本，同一段是能还原的。
    """

    BODY = ('tool_call write({"path": "big.py", "content": '
            '"import json\\ndef f(data):\\n'
            "    h = data['choices'][0]\\n"
            '    return raw[5:].strip()\\n"})')

    def _translate(self, *frames: str, **kwargs) -> tuple[list[dict], Any]:
        lines: list[str] = []
        for frame in frames:
            lines += TranslateTest._sse('output', {'response': frame})
        lines += TranslateTest._sse('done', {'finish_reason': 'stop'})
        state = chatmod.StreamState()

        async def collect() -> list[dict]:
            return [chunk async for chunk in chatmod.translate(
                _events(lines), model='trae/x', state=state, **kwargs)]

        return _run(collect()), state

    def _aggregate(self, body: str, **kwargs) -> tuple[dict, Any]:
        lines = (TranslateTest._sse('output', {'response': body})
                 + TranslateTest._sse('done', {'finish_reason': 'stop'}))
        state = chatmod.StreamState()

        async def collect() -> dict:
            return await chatmod.aggregate(
                _events(lines), model='trae/x', state=state, **kwargs)

        return _run(collect()), state

    @staticmethod
    def _parts(chunks: list[dict]) -> tuple[str, list[dict]]:
        contents: list[str] = []
        calls: list[dict] = []
        for chunk in chunks:
            if 'choices' not in chunk:
                continue
            delta = chunk['choices'][0]['delta']
            if delta.get('content'):
                contents.append(delta['content'])
            calls.extend(delta.get('tool_calls') or [])
        return ''.join(contents), calls

    def test_marker_alone_at_frame_end_is_rescued(self) -> None:
        chunks, _state = self._translate(
            'tool_call', self.BODY[len('tool_call'):], tool_names=['write'])
        text, calls = self._parts(chunks)
        self.assertEqual(text, '')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['function']['name'], 'write')
        args = json.loads(calls[0]['function']['arguments'])
        self.assertEqual(args['path'], 'big.py')
        self.assertIn("data['choices'][0]", args['content'])
        self.assertEqual(chunks[-1]['choices'][0]['finish_reason'], 'tool_calls')

    def test_marker_and_name_at_frame_end_is_rescued(self) -> None:
        head = 'tool_call write'
        chunks, _state = self._translate(
            head, self.BODY[len(head):], tool_names=['write'])
        _text, calls = self._parts(chunks)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['function']['name'], 'write')

    def test_prose_ending_with_literal_word_is_not_held(self) -> None:
        """正文恰好以 `tool_call` 这个词结尾：下一帧不是调用就照常放行。"""
        chunks, _state = self._translate(
            '历史折叠用的标记是 tool_call', '，后面还接着解释。',
            tool_names=['write'])
        text, calls = self._parts(chunks)
        self.assertEqual(calls, [])
        self.assertEqual(text, '历史折叠用的标记是 tool_call，后面还接着解释。')

    def test_aggregate_still_rescues_same_body(self) -> None:
        result, _state = self._aggregate(self.BODY, tool_names=['write'])
        message = result['choices'][0]['message']
        self.assertEqual(len(message['tool_calls']), 1)
        args = json.loads(message['tool_calls'][0]['function']['arguments'])
        self.assertEqual(args['path'], 'big.py')
        self.assertEqual(result['choices'][0]['finish_reason'], 'tool_calls')


class ContainerCallTest(_RescueHarness):
    """无名容器（`<call>` / `<tool_calls>` / `<invoke>`）+ 参数标签。

    2026-11-03 线上样本（deepseek-v4.1-flash）：模型把工具名整个丢掉，只留

        <call>
        <parameter name="path">…</parameter>
        <parameter name="oldString">…</parameter>
        </call>

    整段原样漏进回复，工具没有执行。`<tool_call>` 早就有扁平形态的解析，但这一族
    容器不在那套正则里：`_next_start` 找不到开头，`_strip_tags` 又只删 `</call>`，
    于是线上看到的是「开标签在、闭合标签没了」的残块。
    """

    NAMELESS = '\n'.join([
        '<call>',
        '<parameter name="path">a.md</parameter>',
        '<parameter name="oldString">x</parameter>',
        '<parameter name="newString">y</parameter>',
        '</call>',
    ])

    def test_nameless_container_is_not_leaked(self) -> None:
        """没有工具名：整块丢弃，整轮只剩它时报退化（不能当正文下发）。"""
        for tag in ('call', 'calls', 'tool_calls', 'toolcalls', 'invoke',
                    'function_call', 'function_calls'):
            with self.subTest(tag=tag):
                body = self.NAMELESS.replace('<call>', f'<{tag}>')
                body = body.replace('</call>', f'</{tag}>')
                chunks, _state = self._translate(body, tool_names=['edit'])
                text, calls = self._parts(chunks)
                self.assertEqual(calls, [])
                self.assertEqual(text, '')
                self.assertIn('degenerate_tool_call', self._errors(chunks))

    def test_nameless_container_is_degenerate_when_aggregated(self) -> None:
        with self.assertRaises(PlatformError) as ctx:
            self._aggregate(self.NAMELESS, tool_names=['edit'])
        self.assertEqual(ctx.exception.code, 'degenerate_tool_call')

    def test_invoke_name_attribute_is_rescued(self) -> None:
        body = '\n'.join([
            '<invoke name="bash">',
            '<parameter name="command">ls -la</parameter>',
            '</invoke>',
        ])
        chunks, _state = self._translate(body, tool_names=['bash'])
        text, calls = self._parts(chunks)
        self.assertEqual(text, '')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['function']['name'], 'bash')
        self.assertEqual(json.loads(calls[0]['function']['arguments']),
                         {'command': 'ls -la'})
        self.assertEqual(chunks[-1]['choices'][0]['finish_reason'], 'tool_calls')

    def test_invoke_unquoted_name_is_rescued(self) -> None:
        body = '\n'.join([
            '<invoke name=bash>',
            '<parameter name="command">ls</parameter>',
            '</invoke>',
        ])
        _chunks, calls = self._parts(self._translate(body, tool_names=['bash'])[0])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['function']['name'], 'bash')

    def test_call_with_bare_name_is_rescued(self) -> None:
        """`<call>` 后面跟裸工具名：名字在，照常还原。"""
        body = '\n'.join([
            '<call>',
            'read',
            '<parameter name="path">a.py</parameter>',
            '</call>',
        ])
        chunks, _state = self._translate(body, tool_names=['read'])
        text, calls = self._parts(chunks)
        self.assertEqual(text, '')
        self.assertEqual(calls[0]['function']['name'], 'read')
        self.assertEqual(json.loads(calls[0]['function']['arguments']),
                         {'path': 'a.py'})

    def test_call_with_function_token_is_rescued(self) -> None:
        body = '\n'.join([
            '<call>',
            '<function=read>',
            '<parameter name="path">a.py</parameter>',
            '</call>',
        ])
        _chunks, calls = self._parts(self._translate(body, tool_names=['read'])[0])
        self.assertEqual(calls[0]['function']['name'], 'read')

    def test_wrapped_invokes_become_separate_calls(self) -> None:
        """`<tool_calls>` 里套两条 `<invoke>`：各自还原，外层收尾清掉。"""
        body = '\n'.join([
            '<tool_calls>',
            '<invoke name="bash">',
            '<parameter name="command">ls</parameter>',
            '</invoke>',
            '<invoke name="read">',
            '<parameter name="path">a.py</parameter>',
            '</invoke>',
            '</tool_calls>',
        ])
        chunks, _state = self._translate(body, tool_names=['bash', 'read'])
        text, calls = self._parts(chunks)
        self.assertEqual(text, '')
        self.assertEqual([c['function']['name'] for c in calls],
                         ['bash', 'read'])

    def test_container_name_goes_through_alias_table(self) -> None:
        """`<call>execute<parameter name="code">` → `bash` + `command`。"""
        body = '\n'.join([
            '<call>',
            'execute',
            '<parameter name="code">ls</parameter>',
            '</call>',
        ])
        _chunks, calls = self._parts(
            self._translate(body, tool_names=['bash', 'edit'])[0])
        self.assertEqual(calls[0]['function']['name'], 'bash')
        self.assertEqual(json.loads(calls[0]['function']['arguments']),
                         {'command': 'ls'})

    def test_container_block_split_across_frames(self) -> None:
        body = '\n'.join([
            '<call>',
            '<parameter name="path">a.md</parameter>',
            '</call>',
        ])
        frames = [body[i:i + 3] for i in range(0, len(body), 3)]
        chunks, _state = self._translate(*frames, tool_names=['read'])
        text, calls = self._parts(chunks)
        self.assertEqual(calls, [])
        self.assertEqual(text, '')
        self.assertIn('degenerate_tool_call', self._errors(chunks))

    def test_prose_before_container_is_kept(self) -> None:
        body = '我先看目录。\n' + '\n'.join([
            '<call>',
            'read',
            '<parameter name="path">a.py</parameter>',
            '</call>',
        ])
        chunks, _state = self._translate(body, tool_names=['read'])
        text, calls = self._parts(chunks)
        self.assertEqual(text, '我先看目录。\n')
        self.assertEqual(len(calls), 1)

    def test_duplicate_invoke_open_tag_is_not_leaked(self) -> None:
        """`<invoke name=…>` 连写两次：外层开头也不能漏成正文。"""
        body = '\n'.join([
            '<invoke name="write">',
            '<invoke name="write">',
            '<parameter name="path">a.py</parameter>',
            '<parameter name="content">print(1)</parameter>',
            '</invoke>',
        ])
        chunks, _state = self._translate(body, tool_names=['write'])
        text, calls = self._parts(chunks)
        self.assertEqual(text, '')
        self.assertEqual(len(calls), 1)

    def test_wrapper_with_name_attribute_is_not_leaked(self) -> None:
        body = '\n'.join([
            '<tool_calls name="write">',
            '<invoke name="write">',
            '<parameter name="path">a.py</parameter>',
            '</invoke>',
            '</tool_calls>',
        ])
        chunks, _state = self._translate(body, tool_names=['write'])
        text, calls = self._parts(chunks)
        self.assertEqual(text, '')
        self.assertEqual(len(calls), 1)

    def test_container_with_name_but_no_parameters_is_degenerate(self) -> None:
        """`<call>read</call>`：有名字没参数，无法执行 —— 整块丢掉，不当正文。"""
        chunks, _state = self._translate('<call>\nread\n</call>',
                                         tool_names=['read'])
        text, calls = self._parts(chunks)
        self.assertEqual((text, calls), ('', []))
        self.assertIn('degenerate_tool_call', self._errors(chunks))

    def test_empty_container_pair_is_dropped(self) -> None:
        chunks, _state = self._translate('<call></call>', tool_names=['read'])
        text, calls = self._parts(chunks)
        self.assertEqual((text, calls), ('', []))
        self.assertIn('degenerate_tool_call', self._errors(chunks))

    def test_truncated_container_is_degenerate(self) -> None:
        body = '<call>\n<parameter name="path">a.py</parameter>'
        chunks, _state = self._translate(body, tool_names=['read'])
        text, calls = self._parts(chunks)
        self.assertEqual((text, calls), ('', []))
        self.assertIn('degenerate_tool_call', self._errors(chunks))

    def test_placeholder_container_is_not_rescued(self) -> None:
        """参数是占位符 → 示例，不执行；正文保留（孤立闭合标签按既有口径清掉）。"""
        body = '\n'.join([
            '<call>',
            'bash',
            '<parameter name="command">...</parameter>',
            '</call>',
        ])
        chunks, _state = self._translate(body, tool_names=['bash'])
        text, calls = self._parts(chunks)
        self.assertEqual(calls, [])
        self.assertIn('<parameter name="command">...</parameter>', text)
        self.assertEqual(chunks[-1]['choices'][0]['finish_reason'], 'stop')

    def test_fenced_container_is_not_rescued(self) -> None:
        body = ('示例：\n```\n<call>\nread\n'
                '<parameter name="path">a.py</parameter>\n</call>\n```')
        chunks, _state = self._translate(body, tool_names=['read'])
        text, calls = self._parts(chunks)
        self.assertEqual(calls, [])
        self.assertIn('<call>', text)

    def test_container_word_in_prose_is_not_a_call(self) -> None:
        body = '<call> 这个词在正文里出现一下。'
        chunks, _state = self._translate(body, tool_names=['read'])
        text, calls = self._parts(chunks)
        self.assertEqual(calls, [])
        self.assertEqual(text, body)
        self.assertFalse(any(c.get('error') for c in chunks))

    def test_html_still_passes_through(self) -> None:
        body = '<div>正常 HTML</div>'
        chunks, _state = self._translate(body, tool_names=['read'])
        text, calls = self._parts(chunks)
        self.assertEqual(calls, [])
        self.assertEqual(text, body)
        self.assertFalse(any(c.get('error') for c in chunks))

    def test_aggregate_rescues_named_container(self) -> None:
        body = '\n'.join([
            '<invoke name="bash">',
            '<parameter name="command">ls</parameter>',
            '</invoke>',
        ])
        result, _state = self._aggregate(body, tool_names=['bash'])
        message = result['choices'][0]['message']
        self.assertEqual(len(message['tool_calls']), 1)
        self.assertEqual(json.loads(message['tool_calls'][0]['function']['arguments']),
                         {'command': 'ls'})
        self.assertEqual(result['choices'][0]['finish_reason'], 'tool_calls')


class StackedMarkerTest(_RescueHarness):
    """重复的开头（`<tool_call>` 连写两次）+ 一条裸 marker 调用。

    2026-11-04 线上样本：模型把同一条调用的开头写了两遍，真正的内容跟在后面
    的裸 `tool_call write({…})` 里——

        <tool_call>
        <tool_call>
        tool_call write({"path": "…", "content": "…"})

    参数能解析、调用也执行了，但多出来的开头被当正文保留，客户端看到的回复
    一上来就是两行 `<tool_call>`。
    """

    CALL = ('tool_call write({"path": "a.py", "content": "print(1)\\n"})')
    DOUBLE = '<tool_call>\n<tool_call>\n' + CALL

    def test_double_marker_call_is_rescued(self) -> None:
        chunks, _state = self._translate(self.DOUBLE, tool_names=['write'])
        text, calls = self._parts(chunks)
        self.assertEqual(text, '')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['function']['name'], 'write')
        self.assertEqual(chunks[-1]['choices'][0]['finish_reason'], 'tool_calls')

    def test_double_marker_split_across_frames(self) -> None:
        frames = [self.DOUBLE[i:i + 3]
                  for i in range(0, len(self.DOUBLE), 3)]
        chunks, _state = self._translate(*frames, tool_names=['write'])
        text, calls = self._parts(chunks)
        self.assertEqual(text, '')
        self.assertEqual(len(calls), 1)

    def test_single_marker_does_not_leak(self) -> None:
        body = '<tool_call>\n' + self.CALL
        chunks, _state = self._translate(body, tool_names=['write'])
        text, calls = self._parts(chunks)
        self.assertEqual(text, '')
        self.assertEqual(len(calls), 1)

    def test_double_marker_with_invalid_json_is_degenerate(self) -> None:
        """参数不是合法 JSON → 不执行，也不把调用文本漏成正文。"""
        body = ("<tool_call>\n<tool_call>\n"
                "tool_call write({'path': 'a.py', 'content': 'x'})")
        chunks, _state = self._translate(body, tool_names=['write'])
        text, calls = self._parts(chunks)
        self.assertEqual((text, calls), ('', []))
        self.assertIn('degenerate_tool_call', self._errors(chunks))

    def test_double_marker_with_invalid_json_is_degenerate_when_aggregated(self) -> None:
        body = ("<tool_call>\n<tool_call>\n"
                "tool_call write({'path': 'a.py', 'content': 'x'})")
        with self.assertRaises(PlatformError) as ctx:
            self._aggregate(body, tool_names=['write'])
        self.assertEqual(ctx.exception.code, 'degenerate_tool_call')

    def test_truncated_arguments_are_degenerate(self) -> None:
        body = '<tool_call>\n<tool_call>\ntool_call write({"path": "a.py"'
        chunks, _state = self._translate(body, tool_names=['write'])
        text, calls = self._parts(chunks)
        self.assertEqual((text, calls), ('', []))
        self.assertIn('degenerate_tool_call', self._errors(chunks))

    def test_prose_after_markers_is_kept(self) -> None:
        """marker 后面跟着句子 → 那是正文里提到这个词，原样放行。"""
        body = '<tool_call>\n<tool_call>\n先看目录。'
        chunks, _state = self._translate(body, tool_names=['write'])
        text, calls = self._parts(chunks)
        self.assertEqual(calls, [])
        self.assertIn('先看目录。', text)
        self.assertFalse(self._errors(chunks))

    def test_aggregate_rescues_double_marker_call(self) -> None:
        result, _state = self._aggregate(self.DOUBLE, tool_names=['write'])
        message = result['choices'][0]['message']
        self.assertFalse(message.get('content'))
        self.assertEqual(len(message['tool_calls']), 1)
        self.assertEqual(result['choices'][0]['finish_reason'], 'tool_calls')

    def test_frame_cut_right_after_marker_space(self) -> None:
        """帧恰好切在 `tool_call `（标记 + 前导空格）：必须扣住，不能先发正文。"""
        body = self.CALL
        chunks, _state = self._translate(
            body[:len('tool_call ')], body[len('tool_call '):],
            tool_names=['write'])
        text, calls = self._parts(chunks)
        self.assertEqual(text, '')
        self.assertEqual(len(calls), 1)

    def test_frame_cut_right_after_marker_name_space(self) -> None:
        body = self.CALL
        cut = len('tool_call write ')
        chunks, _state = self._translate(
            body[:cut], body[cut:], tool_names=['write'])
        text, calls = self._parts(chunks)
        self.assertEqual(text, '')
        self.assertEqual(len(calls), 1)

    def test_restated_history_is_not_executed(self) -> None:
        """历史折叠用的 `[历史调用]` 不是调用语法：复述它不会被执行。

        2026-11-04 线上样本：模型把上一轮 Agent 记录连同一句说明一起复述出来，
        旧折叠格式 `[tool_call] name(…)` 正好是还原器的语法，于是「记录」被当成
        新请求执行。现在历史用 `[历史调用]`，解析器根本认不出来。
        """
        body = '\n'.join([
            '[历史调用] write({"path": "a.py", "content": "print(1)\\n"})',
            '← Edit a.py',
            '主界面自上而下分五块。',
        ])
        chunks, _state = self._translate(body, tool_names=['write'])
        text, calls = self._parts(chunks)
        self.assertEqual(calls, [])
        self.assertIn('[历史调用]', text)
        self.assertIn('← Edit a.py', text)
        result, _state = self._aggregate(body, tool_names=['write'])
        message = result['choices'][0]['message']
        self.assertNotIn('tool_calls', message)
        self.assertIn('[历史调用]', message['content'])

    def test_standalone_call_line_before_prose_is_rescued(self) -> None:
        """独占一行的调用后面接正文：按新判据仍要执行（行内夹着的才算举例）。

        这是 2026-11-04 有意接受的取舍：判据只看调用**所在行**的两侧，所以
        后面另起一段解释不影响结论。真正防止「复述历史被执行」的是历史折叠
        格式（`[历史调用]`），而不是再看后文猜意图。
        """
        body = '\n'.join([
            self.CALL,
            '← Edit a.py',
            '主界面自上而下分五块。',
        ])
        chunks, _state = self._translate(body, tool_names=['write'])
        text, calls = self._parts(chunks)
        self.assertEqual([c['function']['name'] for c in calls], ['write'])
        self.assertIn('← Edit a.py', text)
        result, _state = self._aggregate(body, tool_names=['write'])
        self.assertEqual(
            len(result['choices'][0]['message']['tool_calls']), 1)

    def test_consecutive_calls_are_still_rescued(self) -> None:
        """两条调用紧挨着（中间只有空白）仍算「连续调用」，两种模式都要执行。"""
        body = (self.CALL + '\n'
                + 'tool_call write({"path": "b.py", "content": "x"})')
        chunks, _state = self._translate(body, tool_names=['write'])
        _text, calls = self._parts(chunks)
        self.assertEqual([c['function']['name'] for c in calls],
                         ['write', 'write'])
        result, _state = self._aggregate(body, tool_names=['write'])
        self.assertEqual(len(result['choices'][0]['message']['tool_calls']), 2)


class LargeToolCallTest(_RescueHarness):
    """参数很大的调用（一次 `write` 把整个文件塞进去）。

    嗅探器给普通正文 64 KB 的缓冲区上限；调用块如果也用这个上限，超过它的
    调用会被当成正文整段放行，而非流式却能还原（2026-11-04 实测）。
    """

    def _body(self, size: int) -> str:
        payload = 'x' * size
        return ('<call>\nwrite\n<parameter name="path">a.py</parameter>\n'
                f'<parameter name="content">{payload}</parameter>\n</call>')

    def test_large_call_beyond_plain_limit_is_rescued(self) -> None:
        body = self._body(80 * 1024)
        frames = [body[i:i + 4096] for i in range(0, len(body), 4096)]
        chunks, _state = self._translate(*frames, tool_names=['write'])
        text, calls = self._parts(chunks)
        self.assertEqual(text, '')
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(json.loads(calls[0]['function']['arguments'])['content']),
                         80 * 1024)

    def test_large_call_is_rescued_when_aggregated(self) -> None:
        body = self._body(80 * 1024)
        result, _state = self._aggregate(body, tool_names=['write'])
        message = result['choices'][0]['message']
        self.assertEqual(len(message['tool_calls']), 1)


class FetchConsumedTest(unittest.TestCase):
    """`fetch_consumed` 是「每请求实扣积分」的唯一读数来源（entitlement 口径）。"""

    def test_prefers_four_decimal_credits_amount(self) -> None:
        """`pack.usage.credits_amount` 有 4 位小数，明细增量能和它对上。"""
        payload = {
            'usage_summary': {'consumed_amount': 16.43},
            'user_entitlement_pack_list': [
                {'usage': {}},
                {'usage': {'credits_amount': 16.4284}},
            ],
        }
        with mock.patch.object(client, 'request_json',
                               new=mock.AsyncMock(return_value=payload)):
            got = _run(client.fetch_consumed({'realm': 'cn'}))
        self.assertAlmostEqual(got, 16.4284)

    def test_falls_back_to_consumed_amount(self) -> None:
        for payload in (
            {'usage_summary': {'consumed_amount': 1.66}},
            {'usage_summary': {'consumed_amount': 1.66},
             'user_entitlement_pack_list': [{'usage': {}}, {'usage': {'credits_amount': 'x'}}]},
        ):
            with mock.patch.object(client, 'request_json',
                                   new=mock.AsyncMock(return_value=payload)):
                got = _run(client.fetch_consumed({'realm': 'cn'}))
            self.assertAlmostEqual(got, 1.66, msg=payload)

    def test_returns_none_when_field_missing(self) -> None:
        for payload in ({}, {'usage_summary': None}, {'usage_summary': {}},
                        {'usage_summary': {'consumed_amount': 'x'}},
                        {'usage_summary': {'consumed_amount': True}},
                        {'user_entitlement_pack_list': None}):
            with mock.patch.object(client, 'request_json',
                                   new=mock.AsyncMock(return_value=payload)):
                self.assertIsNone(_run(client.fetch_consumed({'realm': 'cn'})),
                                  payload)

    def test_intl_uses_v1_path(self) -> None:
        calls: list[str] = []

        async def fake(account, path, body=None, **kw):  # noqa: ANN001, ANN202
            calls.append(path)
            return {'usage_summary': {'consumed_amount': 0}}

        with mock.patch.object(client, 'request_json', new=fake):
            _run(client.fetch_consumed({'realm': 'intl'}))
        self.assertEqual(calls, [client.PATH_ENT_USAGE_V1])


class UsageDetailTest(unittest.TestCase):
    """用量明细：`usage_type=7` + `session_id` 才是逐条实付的口径。"""

    def test_group_query_always_sends_credits_usage_type(self) -> None:
        seen: list[dict] = []

        async def fake(account, path, body=None, **kw):  # noqa: ANN001, ANN202
            seen.append({'path': path, 'body': body, 'base': kw.get('base')})
            return {'total': 0, 'user_usage_group_by_sessions': []}

        with mock.patch.object(client, 'request_json', new=fake):
            _run(client.fetch_usage_group({'realm': 'cn'}, start_time=100,
                                          end_time=200))
        self.assertEqual(seen[0]['path'], client.PATH_USAGE_GROUP)
        self.assertEqual(seen[0]['base'], 'pay')
        self.assertEqual(seen[0]['body']['usage_type'], [7])

    def test_request_credit_matches_session_and_retries(self) -> None:
        rows = [
            {'session_id': 'other', 'amount_float': 9.9},
            {'session_id': 'mine', 'amount_float': 0.028},
        ]
        calls: list[dict] = []

        async def fake(account, path, body=None, **kw):  # noqa: ANN001, ANN202
            calls.append(body)
            return {'user_usage_group_by_sessions': rows if len(calls) > 1 else []}

        with mock.patch.object(client, 'request_json', new=fake):
            row = _run(client.fetch_request_credit({'realm': 'cn'}, 'mine', 1000,
                                                   delays=(0, 0)))
        self.assertEqual(row['amount_float'], 0.028)
        self.assertEqual(len(calls), 2, '第一条空结果要继续轮询')
        self.assertEqual(calls[0]['usage_type'], [7])

    def test_request_credit_gives_up_without_rows(self) -> None:
        with mock.patch.object(client, 'request_json',
                               new=mock.AsyncMock(return_value={'total': 0})):
            row = _run(client.fetch_request_credit({'realm': 'cn'}, 'mine', 1000,
                                                   delays=(0, 0, 0)))
        self.assertIsNone(row)

    def test_default_retry_budget_is_under_one_second(self) -> None:
        """实测落账是同步的，默认重试预算必须短，否则会白白拖慢流结束。"""
        self.assertLessEqual(sum(client._CREDIT_POLL_DELAYS), 0.8)
        self.assertLessEqual(len(client._CREDIT_POLL_DELAYS), 3)

    def test_body_builders_carry_session_id(self) -> None:
        messages = [{'role': 'user', 'content': 'hi'}]
        chat = client.build_chat_body('seed_m8', messages, {}, session_id='abc')
        self.assertEqual(chat['session_id'], 'abc')
        self.assertEqual(chat['chat_session_id'], 'abc')
        solo = client.build_solo_body('deepseek-v4.1-flash', messages, {},
                                      session_id='abc')
        self.assertEqual(solo['session_id'], 'abc')
        self.assertNotIn('session_id', client.build_chat_body('seed_m8', messages, {}))


class SoloEventTest(unittest.TestCase):
    """Agent 通道没有 `event:` 行，`code` 只是业务码，不能一律当错误。

    旧判据 `code not in (None, 0)` 会把 `"code": "0"` / `"code": 200` 的
    正常帧判成 error；`translate` 收到 error 就 break，客户端看到的是
    「回复到一半没了」。
    """

    @staticmethod
    def _collect(lines: list[str], **kwargs) -> list[tuple[str, Any]]:
        async def walk() -> list[tuple[str, Any]]:
            return [item async for item in
                    client.iter_solo_events(FakeResponse(lines), **kwargs)]
        return _run(walk())

    def test_string_zero_code_is_not_an_error(self) -> None:
        events = self._collect(['data: {"code": "0", "response": "你"}', ''])
        self.assertEqual(events, [('output', {'code': '0', 'response': '你'})])

    def test_200_code_with_content_is_not_an_error(self) -> None:
        events = self._collect(['data: {"code": 200, "response": "好"}', ''])
        self.assertEqual(events[0][0], 'output')

    def test_nonzero_code_without_payload_is_an_error(self) -> None:
        events = self._collect(['data: {"code": 500, "message": "boom"}', ''])
        self.assertEqual(events[0][0], 'error')

    def test_done_sentinel(self) -> None:
        events = self._collect(['data: [DONE]', ''])
        self.assertEqual(events, [('done', {'finish_reason': 'stop'})])


if __name__ == '__main__':
    unittest.main()
