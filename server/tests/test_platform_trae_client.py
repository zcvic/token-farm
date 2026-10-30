"""Trae 协议层：请求体归一、SSE 解析、事件翻译。

全部离线：SSE 用固定文本喂给 `iter_events`，不依赖真实账号或网络。
"""
from __future__ import annotations

import asyncio
import unittest
from typing import Any, AsyncIterator

from server.platforms.base import PlatformError
from server.platforms.trae import chat as chatmod
from server.platforms.trae import client


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
        self.assertEqual(body['temperature'], 0.3)
        self.assertNotIn('nonsense', body)


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
        self.assertEqual(events[2], ('message', {'finish_reason': 'stop'}))

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


if __name__ == '__main__':
    unittest.main()
