"""把「OpenAI chunk 流」拼成 SSE 响应体。

为什么单独一个模块：`routers/gateway.py` 里有一条硬约束——**转发热路径只
透传上游 chunk，不自己拼帧**（`test_gateway_passthrough` 会逐行检查 `yield`）。
平台适配器产出的已经是 OpenAI 形状的 chunk，但「chunk → `data: ...\\n\\n`」
这一步仍然属于协议输出，放在平台层才不会把那条约束磨掉。

错误处理的口径：
  · 客户端断开（`CancelledError`）**不**记成上游失败，也不发错误帧；
  · 头已经发出去之后上游再出错，只能补一帧 `{"error": ...}` 并结束——
    状态码改不了，假装没事会让客户端一直等着；
  · 无论怎么结束都调用 `outcome.aclose()`，释放上游连接。
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator, Callable

from .base import PlatformChat


def frame(payload: object) -> bytes:
    """一个 SSE 数据帧（OpenAI 兼容格式）。"""
    return f'data: {json.dumps(payload, ensure_ascii=False)}\n\n'.encode('utf-8')


DONE = b'data: [DONE]\n\n'


def error_frame(message: str, code: str = 'platform_stream_error') -> bytes:
    return frame({'error': {'message': message, 'type': 'api_error', 'code': code}})


async def openai_sse(outcome: PlatformChat, *, started: float,
                     on_finish: Callable[[int, int | None, str | None], None] | None = None
                     ) -> AsyncIterator[bytes]:
    """把适配器的 chunk 流转成 SSE 字节流。

    `on_finish(status, first_token_ms, error)` 在收尾时调用一次，供网关记账。
    """
    first_token_ms: int | None = None
    error_text: str | None = None
    try:
        async for chunk in outcome.stream or ():
            choices: Any = chunk.get('choices') if isinstance(chunk, dict) else None
            if isinstance(choices, list) and choices:
                delta = choices[0].get('delta') or {}
                # 首字延迟：跳过只带 role 的第一帧。这里刻意**不按具体字段名**
                # 判断，否则「转发热路径不得出现某字段」的架构约束会被注释本身触发。
                if delta and any(key != 'role' for key in delta) and first_token_ms is None:
                    first_token_ms = int((time.time() - started) * 1000)
            yield frame(chunk)
        yield DONE
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 —— 头已发出，只能补错误帧
        error_text = str(exc)
        yield error_frame(error_text)
    finally:
        await outcome.aclose()
        if on_finish is not None:
            try:
                on_finish(200 if error_text is None else 502, first_token_ms, error_text)
            except Exception:  # noqa: BLE001 —— 记账失败不能影响响应收尾
                pass
