"""平台 SSE 拼帧：OpenAI chunk → `data: ...`，以及收尾记账。"""
from __future__ import annotations

import asyncio
import json
import unittest

from server.platforms.base import PlatformChat
from server.platforms.sse import DONE, openai_sse


def _chunk(*, delta: dict | None = None, finish: str | None = None,
           usage: dict | None = None) -> dict:
    payload = {'id': 'c1', 'object': 'chat.completion.chunk', 'model': 'trae/x',
               'choices': [{'index': 0, 'delta': delta or {}, 'finish_reason': finish}]}
    if usage:
        payload['usage'] = usage
    return payload


def _outcome(chunks, closer=None):
    async def walk():
        for item in chunks:
            if isinstance(item, BaseException):
                raise item
            yield item

    async def default_closer() -> None:
        return None

    return PlatformChat(stream=walk(), closer=closer or default_closer)


def _collect(outcome, **kw):
    async def run() -> list[bytes]:
        return [frame async for frame in openai_sse(outcome, **kw)]
    return asyncio.run(run())


class SseTest(unittest.TestCase):
    def test_frames_and_done(self) -> None:
        outcome = _outcome([_chunk(delta={'role': 'assistant'}),
                            _chunk(delta={'content': '你'}),
                            _chunk(finish='stop', usage={'completion_tokens': 1})])
        frames = _collect(outcome, started=0.0)
        self.assertEqual(frames[-1], DONE)
        payloads = [json.loads(f[6:].decode('utf-8')) for f in frames[:-1]]
        self.assertEqual(payloads[0]['choices'][0]['delta'], {'role': 'assistant'})
        self.assertEqual(payloads[1]['choices'][0]['delta']['content'], '你')
        self.assertEqual(payloads[2]['usage'], {'completion_tokens': 1})

    def test_on_finish_reports_success_and_first_token(self) -> None:
        calls: list[tuple] = []
        outcome = _outcome([_chunk(delta={'role': 'assistant'}),
                            _chunk(delta={'content': 'x'})])
        _collect(outcome, started=0.0, on_finish=lambda *a: calls.append(a))
        self.assertEqual(len(calls), 1)
        status, first_token, error = calls[0]
        self.assertEqual(status, 200)
        self.assertIsNone(error)
        self.assertIsInstance(first_token, int)
        self.assertGreaterEqual(first_token, 0)

    def test_error_midstream_becomes_error_frame(self) -> None:
        calls: list[tuple] = []
        outcome = _outcome([_chunk(delta={'content': 'x'}),
                            RuntimeError('上游断了')])
        frames = _collect(outcome, started=0.0,
                          on_finish=lambda *a: calls.append(a))
        # 错误流不发 [DONE]：客户端看到 [DONE] 会当成正常结束并丢掉错误
        self.assertNotIn(DONE, frames)
        error = json.loads(frames[-1][6:].decode('utf-8'))['error']
        self.assertIn('上游断了', error['message'])
        self.assertEqual(calls[0][0], 502)
        self.assertIn('上游断了', calls[0][2])

    def test_closer_runs_on_error(self) -> None:
        closed: list[bool] = []

        async def closer() -> None:
            closed.append(True)

        outcome = _outcome([RuntimeError('boom')], closer=closer)
        _collect(outcome, started=0.0)
        self.assertEqual(closed, [True])

    def test_client_disconnect_propagates(self) -> None:
        """客户端断开：不补错误帧，但依然要释放上游连接。

        用 `__anext__` 手动推进：`CancelledError` 在 `asyncio.run` 里会被
        当成「主任务被取消」而吞掉，断言不到「有没有补错误帧」。
        """
        closed: list[bool] = []

        async def closer() -> None:
            closed.append(True)

        outcome = _outcome([_chunk(delta={'content': 'x'}),
                            asyncio.CancelledError()], closer=closer)

        async def run() -> list[bytes]:
            frames: list[bytes] = []
            generator = openai_sse(outcome, started=0.0)
            try:
                while True:
                    frames.append(await generator.__anext__())
            except (StopAsyncIteration, asyncio.CancelledError):
                pass
            return frames

        frames = asyncio.run(run())
        self.assertEqual(closed, [True])
        for item in frames:
            self.assertNotIn(b'"error"', item)

    def test_idle_timeout_stops_stream_and_reports_error(self) -> None:
        """内容空闲看门狗（issue #485）：保活/空帧不续命，超时就中止。"""
        calls: list[tuple] = []
        closed: list[bool] = []

        async def slower():
            yield _chunk(delta={'content': 'x'})
            await asyncio.sleep(0.05)
            yield _chunk(delta={})              # 空帧：不续命
            yield _chunk(delta={'content': 'never'})

        async def closer() -> None:
            closed.append(True)

        outcome = PlatformChat(stream=slower(), closer=closer)
        frames = _collect(outcome, started=0.0, idle_timeout=0.01,
                          on_finish=lambda *a: calls.append(a))
        self.assertNotIn(DONE, frames)
        error = json.loads(frames[-1][6:].decode('utf-8'))['error']
        self.assertIn('idle', error['message'])
        self.assertEqual(closed, [True])
        self.assertEqual(calls[0][0], 502)

    def test_tool_call_only_stream_is_not_idle(self) -> None:
        """只有工具调用增量、没有正文的流不算空闲。

        长工具调用（生成一个大文件的参数）可能连续几分钟没有 `content` delta；
        按正文判活跃会把这类合法请求误杀。看门狗必须按「有产出」判。
        """
        calls: list[tuple] = []

        async def tool_stream():
            yield _chunk(delta={'role': 'assistant'})
            for _ in range(4):
                await asyncio.sleep(0.01)
                yield _chunk(delta={'tool_calls': [
                    {'index': 0, 'function': {'arguments': 'x'}}]})

        outcome = PlatformChat(stream=tool_stream(), closer=lambda: asyncio.sleep(0))
        frames = _collect(outcome, started=0.0, idle_timeout=0.02,
                          on_finish=lambda *a: calls.append(a))
        self.assertEqual(frames[-1], DONE)
        self.assertNotIn(b'"error"', b''.join(frames))
        self.assertEqual(calls[0][0], 200)
        self.assertIsNone(calls[0][2])

    def test_disconnected_stops_stream(self) -> None:
        """客户端已断开：停止读取上游，且不伪造 [DONE]。"""
        calls: list[tuple] = []
        probes: list[bool] = []

        async def walk():
            yield _chunk(delta={'content': 'a'})
            yield _chunk(delta={'content': 'b'})
            yield _chunk(delta={'content': 'c'})

        async def is_disconnected() -> bool:
            probes.append(True)
            return len(probes) > 1   # 第一块之后客户端走人

        async def closer() -> None:
            return None

        outcome = PlatformChat(stream=walk(), closer=closer)
        frames = _collect(outcome, started=0.0, disconnected=is_disconnected,
                          on_finish=lambda *a: calls.append(a))
        self.assertNotIn(DONE, frames)
        bodies = [f for f in frames if b'"content"' in f]
        self.assertEqual(len(bodies), 1, '断开后不应继续转发 chunk')
        self.assertEqual(calls[0][0], 502)
        self.assertIn('disconnected', calls[0][2])

    def test_on_finish_failure_is_swallowed(self) -> None:
        def broken(*_a) -> None:
            raise ValueError('记账炸了')

        outcome = _outcome([_chunk(delta={'content': 'x'})])
        frames = _collect(outcome, started=0.0, on_finish=broken)
        self.assertEqual(frames[-1], DONE)


if __name__ == '__main__':
    unittest.main()
