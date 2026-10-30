"""Trae SSE → OpenAI 形状翻译。

Trae 的 `llm_raw_chat` **永远**返回 SSE，即使请求里 `stream=false`。因此
「非流式」不是另一条上游路径，而是把同一条流聚合起来再拼一个
`chat.completion`。这样做的好处是两种模式共用一套解析与错误处理。

事件形状（参考实现 `collect()`）：

    event: metadata       data: {"model":..., "session_id":...}
    event: output         data: {"response": "...", "reasoning_content": "...",
                                 "tool_calls": [...]}
    event: token_usage    data: {"prompt_tokens": n, "completion_tokens": n, ...}
    event: done           data: {"finish_reason": "stop"}
    event: error          data: {"code": ..., "message": ...}

字段名在不同区域/版本间可能漂移，所以取值一律「多别名 + 类型校验」，
拿不到就留空，**不猜**。
"""
from __future__ import annotations

import time
import uuid
from typing import Any, AsyncIterator

from ..base import PlatformError


def new_id(prefix: str = 'chatcmpl') -> str:
    return f'{prefix}-{uuid.uuid4().hex[:24]}'


def _pick_int(payload: dict, *names: str) -> int | None:
    for name in names:
        value = payload.get(name)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return max(0, int(value))
    return None


def normalize_usage(raw: object) -> dict:
    """把 Trae 的 token_usage 归一成 OpenAI usage。

    认不出的结构返回空 dict —— 记账侧把「没有 usage」与「用了 0 token」
    分得很开（前者存 NULL），编造一个 0 会让统计失真。
    """
    if not isinstance(raw, dict):
        return {}
    source = raw
    # 有些版本把真正的用量包了一层：{"usage": {...}} / {"token_usage": {...}}
    for wrapper in ('usage', 'token_usage', 'data'):
        inner = raw.get(wrapper)
        if isinstance(inner, dict):
            source = inner
            break
    prompt = _pick_int(source, 'prompt_tokens', 'input_tokens',
                       'promptTokens', 'inputTokens')
    completion = _pick_int(source, 'completion_tokens', 'output_tokens',
                           'completionTokens', 'outputTokens')
    if prompt is None and completion is None:
        return {}
    usage: dict[str, Any] = {
        'prompt_tokens': prompt or 0,
        'completion_tokens': completion or 0,
        'total_tokens': (prompt or 0) + (completion or 0),
    }
    for src_name, dst_name in (('prompt_cache_hit_tokens', 'prompt_cache_hit_tokens'),
                               ('cached_tokens', 'prompt_cache_hit_tokens'),
                               ('credit', 'credit')):
        value = source.get(src_name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            usage[dst_name] = int(value) if dst_name != 'credit' else float(value)
    return usage


def _delta_chunk(chunk_id: str, created: int, model: str, *,
                 delta: dict | None = None,
                 finish_reason: str | None = None,
                 usage: dict | None = None) -> dict:
    payload: dict[str, Any] = {
        'id': chunk_id,
        'object': 'chat.completion.chunk',
        'created': created,
        'model': model,
        'choices': [{'index': 0, 'delta': delta or {}, 'finish_reason': finish_reason}],
    }
    if usage:
        payload['usage'] = usage
    return payload


class StreamState:
    """一次翻译的中间状态：网关需要用它记账（usage / 错误 / 是否出过正文）。"""

    def __init__(self) -> None:
        self.usage: dict = {}
        self.error: str | None = None
        self.saw_content = False
        self.first_token_at: float | None = None
        self.finish_reason: str | None = None


async def translate(events: AsyncIterator[tuple[str, Any]], *, model: str,
                    state: StreamState) -> AsyncIterator[dict]:
    """把 Trae 事件流翻译成 OpenAI chunk 流。

    `state` 由调用方持有，流结束后从这里读 usage / 错误 / 首字时间。
    """
    chunk_id = new_id()
    created = int(time.time())
    yield _delta_chunk(chunk_id, created, model, delta={'role': 'assistant'})

    async for event, data in events:
        if event == 'metadata':
            continue
        if event == 'output' and isinstance(data, dict):
            delta: dict[str, Any] = {}
            text = data.get('response')
            if isinstance(text, str) and text:
                delta['content'] = text
            reasoning = data.get('reasoning_content')
            if isinstance(reasoning, str) and reasoning:
                delta['reasoning_content'] = reasoning
            tool_calls = data.get('tool_calls')
            if isinstance(tool_calls, list) and tool_calls:
                delta['tool_calls'] = tool_calls
            if delta:
                if 'content' in delta and state.first_token_at is None:
                    state.first_token_at = time.time()
                if 'content' in delta or 'tool_calls' in delta:
                    state.saw_content = True
                yield _delta_chunk(chunk_id, created, model, delta=delta)
            continue
        if event == 'token_usage':
            usage = normalize_usage(data)
            if usage:
                # **原地更新**：`PlatformChat.usage` 持有的是同一个 dict，
                # 换成新对象会让网关在流结束后读到空 usage（记账丢 token）。
                state.usage.clear()
                state.usage.update(usage)
            continue
        if event in ('done', 'message'):
            if isinstance(data, dict):
                state.finish_reason = str(data.get('finish_reason') or 'stop')
            else:
                state.finish_reason = 'stop'
            continue
        if event == 'error':
            state.error = _error_text(data)
            break

    finish = state.finish_reason or ('stop' if state.saw_content else 'error')
    yield _delta_chunk(chunk_id, created, model, delta={}, finish_reason=finish,
                       usage=state.usage or None)


async def aggregate(events: AsyncIterator[tuple[str, Any]], *, model: str,
                    state: StreamState) -> dict:
    """把 Trae 事件流聚合成一个 OpenAI `chat.completion`。"""
    content: list[str] = []
    reasoning: list[str] = []
    tool_calls: list[Any] = []
    async for event, data in events:
        if event == 'metadata':
            continue
        if event == 'output' and isinstance(data, dict):
            text = data.get('response')
            if isinstance(text, str) and text:
                content.append(text)
                if state.first_token_at is None:
                    state.first_token_at = time.time()
                state.saw_content = True
            part = data.get('reasoning_content')
            if isinstance(part, str) and part:
                reasoning.append(part)
            calls = data.get('tool_calls')
            if isinstance(calls, list) and calls:
                tool_calls.extend(calls)
                state.saw_content = True
            continue
        if event == 'token_usage':
            usage = normalize_usage(data)
            if usage:
                state.usage.clear()
                state.usage.update(usage)
            continue
        if event in ('done', 'message'):
            if isinstance(data, dict):
                state.finish_reason = str(data.get('finish_reason') or 'stop')
            continue
        if event == 'error':
            state.error = _error_text(data)
            break

    if state.error and not state.saw_content:
        raise PlatformError(state.error or '上游返回错误', status=502,
                            code='upstream_error')

    message: dict[str, Any] = {'role': 'assistant',
                               'content': ''.join(content) or None}
    if reasoning:
        message['reasoning_content'] = ''.join(reasoning)
    if tool_calls:
        message['tool_calls'] = tool_calls
    payload: dict[str, Any] = {
        'id': new_id(),
        'object': 'chat.completion',
        'created': int(time.time()),
        'model': model,
        'choices': [{
            'index': 0,
            'message': message,
            'finish_reason': state.finish_reason or 'stop',
        }],
    }
    if state.usage:
        payload['usage'] = state.usage
    return payload


def _error_text(data: Any) -> str:
    if isinstance(data, dict):
        message = data.get('message') or data.get('msg') or data.get('error')
        code = data.get('code') or data.get('Code')
        if message and code not in (None, '', 0, '0'):
            return f'[{code}] {message}'
        if message:
            return str(message)
        return str(data)[:300]
    return str(data or '上游返回错误')[:300]
