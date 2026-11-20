"""OpenAI Chat Completions ↔ Anthropic Messages 翻译。

MiniMax Code 的模型端点（`/mavis/api/v1/llm/v1/messages`）是 **Anthropic
Messages** 形状：OpenCode / magpie 正是按 `@ai-sdk/anthropic` 的请求体发的。
而 token-farm 的平台契约统一是 OpenAI：适配器必须在这里把两个方向都翻掉，
不能让路由层知道上游用的是哪套协议。

只覆盖对话需要的字段：文本 / 图片 / 工具调用 / 工具结果 / 采样参数 / 用量。
刻意**不**做「OpenAI 独有字段的尽力猜测」——猜错会让上游 400，比忽略更难查。
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any

_STOP_REASONS = {
    'end_turn': 'stop',
    'stop_sequence': 'stop',
    'pause_turn': 'stop',
    'max_tokens': 'length',
    'tool_use': 'tool_calls',
    'refusal': 'content_filter',
}


def _text_of(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ''
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get('type') in ('text', 'input_text'):
            parts.append(str(block.get('text') or ''))
    return '\n'.join(p for p in parts if p)


def _image_block(part: dict) -> dict | None:
    raw = part.get('image_url')
    if isinstance(raw, dict):
        url = str(raw.get('url') or '')
    else:
        url = str(raw or part.get('url') or '')
    if not url:
        return None
    if url.startswith('data:'):
        header, _, data = url.partition(',')
        media = header[5:].split(';', 1)[0] or 'image/png'
        if not data:
            return None
        return {'type': 'image',
                'source': {'type': 'base64', 'media_type': media, 'data': data}}
    return {'type': 'image', 'source': {'type': 'url', 'url': url}}


def _blocks(content: object) -> list[dict]:
    """OpenAI 的 message.content → Anthropic content blocks。"""
    if isinstance(content, str):
        return [{'type': 'text', 'text': content}] if content else []
    if not isinstance(content, list):
        return []
    out: list[dict] = []
    for part in content:
        if isinstance(part, str):
            if part:
                out.append({'type': 'text', 'text': part})
            continue
        if not isinstance(part, dict):
            continue
        kind = str(part.get('type') or '')
        if kind in ('text', 'input_text'):
            text = str(part.get('text') or '')
            if text:
                out.append({'type': 'text', 'text': text})
        elif kind in ('image_url', 'input_image', 'image'):
            if kind == 'image' and isinstance(part.get('source'), dict):
                out.append({'type': 'image', 'source': part['source']})
                continue
            block = _image_block(part)
            if block:
                out.append(block)
    return out


def _arguments(value: object) -> dict:
    """OpenAI 的 arguments 字符串 → Anthropic 的 input 对象。"""
    if isinstance(value, dict):
        return value
    text = str(value or '').strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except ValueError:
        return {'_raw': text}
    return parsed if isinstance(parsed, dict) else {'_value': parsed}


def _merge(messages: list[dict], role: str, blocks: list[dict]) -> None:
    """同角色连续消息合并（Anthropic 要求 user / assistant 交替）。"""
    if not blocks:
        return
    if messages and messages[-1].get('role') == role:
        merged = messages[-1].setdefault('content', [])
        merged.extend(blocks)
        return
    messages.append({'role': role, 'content': blocks})


def to_anthropic(body: dict, *, default_max_tokens: int = 8192,
                 output_cap: int = 0) -> dict:
    """OpenAI 请求体 → Anthropic Messages 请求体。"""
    body = body if isinstance(body, dict) else {}
    system_parts: list[str] = []
    messages: list[dict] = []
    tool_results: list[dict] = []

    def flush_tools() -> None:
        if tool_results:
            _merge(messages, 'user', list(tool_results))
            tool_results.clear()

    for raw in body.get('messages') or []:
        if not isinstance(raw, dict):
            continue
        role = str(raw.get('role') or '').strip().lower()
        content = raw.get('content')
        if role in ('system', 'developer'):
            text = _text_of(content)
            if text:
                system_parts.append(text)
            continue
        if role in ('tool', 'function'):
            text = _text_of(content)
            block = {
                'type': 'tool_result',
                'tool_use_id': str(raw.get('tool_call_id') or raw.get('name') or ''),
                'content': text if text else '[空结果]',
            }
            tool_results.append(block)
            continue
        flush_tools()
        if role == 'assistant':
            blocks = _blocks(content)
            for call in raw.get('tool_calls') or []:
                if not isinstance(call, dict):
                    continue
                fn = call.get('function') if isinstance(call.get('function'), dict) else {}
                name = str(fn.get('name') or call.get('name') or '').strip()
                if not name:
                    continue
                blocks.append({
                    'type': 'tool_use',
                    'id': str(call.get('id') or f'call_{uuid.uuid4().hex[:12]}'),
                    'name': name,
                    'input': _arguments(fn.get('arguments')),
                })
            _merge(messages, 'assistant', blocks)
            continue
        if role == 'user':
            _merge(messages, 'user', _blocks(content))
            continue

    flush_tools()

    out: dict[str, Any] = {'messages': messages}
    if system_parts:
        out['system'] = '\n\n'.join(system_parts)

    limit = body.get('max_tokens')
    if limit is None:
        limit = body.get('max_completion_tokens')
    try:
        max_tokens = int(limit) if limit is not None else int(default_max_tokens)
    except (TypeError, ValueError):
        max_tokens = int(default_max_tokens)
    if output_cap > 0:
        max_tokens = min(max_tokens, output_cap)
    max_tokens = max(1, max_tokens)
    out['max_tokens'] = max_tokens

    for src, dst in (('temperature', 'temperature'), ('top_p', 'top_p')):
        value = body.get(src)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[dst] = value

    stop = body.get('stop')
    if isinstance(stop, str) and stop:
        out['stop_sequences'] = [stop]
    elif isinstance(stop, list):
        values = [str(x) for x in stop if str(x or '')]
        if values:
            out['stop_sequences'] = values

    tools = body.get('tools')
    if isinstance(tools, list) and tools:
        converted = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            fn = tool.get('function') if isinstance(tool.get('function'), dict) else tool
            name = str(fn.get('name') or '').strip()
            if not name:
                continue
            schema = fn.get('parameters')
            converted.append({
                'name': name,
                'description': str(fn.get('description') or ''),
                'input_schema': schema if isinstance(schema, dict)
                else {'type': 'object', 'properties': {}},
            })
        if converted:
            out['tools'] = converted
            choice = body.get('tool_choice')
            if choice == 'required':
                out['tool_choice'] = {'type': 'any'}
            elif choice in ('auto', 'none'):
                # Anthropic 的 none 不是所有版本都认；「不选工具」等价于不给工具。
                if choice == 'auto':
                    out['tool_choice'] = {'type': 'auto'}
            elif isinstance(choice, dict):
                name = str((choice.get('function') or {}).get('name')
                           or choice.get('name') or '').strip()
                if name:
                    out['tool_choice'] = {'type': 'tool', 'name': name}

    if body.get('stream'):
        out['stream'] = True
    return out


def _usage(payload: dict) -> dict:
    raw = payload.get('usage') if isinstance(payload.get('usage'), dict) else {}
    input_tokens = _int(raw.get('input_tokens'))
    output_tokens = _int(raw.get('output_tokens'))
    cache_read = _int(raw.get('cache_read_input_tokens'))
    cache_write = _int(raw.get('cache_creation_input_tokens'))
    usage = {
        'prompt_tokens': input_tokens + cache_read + cache_write,
        'completion_tokens': output_tokens,
        'total_tokens': input_tokens + cache_read + cache_write + output_tokens,
    }
    if cache_read or cache_write:
        usage['prompt_tokens_details'] = {'cached_tokens': cache_read}
    return usage


def _int(value: object) -> int:
    try:
        return max(0, int(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def from_anthropic(payload: dict, model: str) -> dict:
    """Anthropic Messages 响应 → OpenAI Chat Completion。"""
    payload = payload if isinstance(payload, dict) else {}
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls: list[dict] = []
    for block in payload.get('content') or []:
        if not isinstance(block, dict):
            continue
        kind = block.get('type')
        if kind == 'text':
            text_parts.append(str(block.get('text') or ''))
        elif kind in ('thinking', 'redacted_thinking'):
            thinking_parts.append(str(block.get('thinking') or block.get('data') or ''))
        elif kind == 'tool_use':
            tool_calls.append({
                'id': str(block.get('id') or f'call_{uuid.uuid4().hex[:12]}'),
                'type': 'function',
                'function': {
                    'name': str(block.get('name') or ''),
                    'arguments': json.dumps(block.get('input') or {},
                                            ensure_ascii=False),
                },
            })
    content = ''.join(text_parts)
    message: dict[str, Any] = {
        'role': 'assistant',
        'content': content if (content or not tool_calls) else None,
    }
    if thinking_parts:
        message['reasoning_content'] = ''.join(thinking_parts)
    if tool_calls:
        message['tool_calls'] = tool_calls
    return {
        'id': str(payload.get('id') or f'chatcmpl-{uuid.uuid4().hex[:24]}'),
        'object': 'chat.completion',
        'created': int(time.time()),
        'model': model,
        'choices': [{
            'index': 0,
            'message': message,
            'finish_reason': _STOP_REASONS.get(
                str(payload.get('stop_reason') or ''), 'stop'),
        }],
        'usage': _usage(payload),
    }


def sse(payload: dict) -> bytes:
    return (b'data: ' + json.dumps(payload, ensure_ascii=False).encode('utf-8')
            + b'\n\n')


class StreamTranslator:
    """Anthropic SSE 事件 → OpenAI chunk。

    用法：每个 `data:` JSON 交给 `feed()`，返回要下发的 chunk 列表；流结束后
    调 `finish()`。`usage` 是适配器持有的同一个 dict，在这里就地填 OpenAI 口径。
    """

    def __init__(self, model: str, usage: dict) -> None:
        self.model = model
        self.usage = usage
        self.id = f'chatcmpl-{uuid.uuid4().hex[:24]}'
        self.created = int(time.time())
        self.finish_reason: str | None = None
        self.tools: dict[int, int] = {}
        self._role_sent = False
        self._input = 0
        self._cache_read = 0
        self._cache_write = 0
        self._output = 0

    # ── chunk ────────────────────────────────────────────
    def _chunk(self, delta: dict, finish: str | None = None,
               *, usage: dict | None = None) -> dict:
        out: dict[str, Any] = {
            'id': self.id,
            'object': 'chat.completion.chunk',
            'created': self.created,
            'model': self.model,
            'choices': [] if usage is not None else [{
                'index': 0,
                'delta': delta,
                'finish_reason': finish,
            }],
        }
        if usage is not None:
            out['usage'] = usage
        return out

    def _usage_out(self) -> dict:
        prompt = self._input + self._cache_read + self._cache_write
        usage = {
            'prompt_tokens': prompt,
            'completion_tokens': self._output,
            'total_tokens': prompt + self._output,
        }
        if self._cache_read or self._cache_write:
            usage['prompt_tokens_details'] = {'cached_tokens': self._cache_read}
        self.usage.update(usage)
        return usage

    # ── 事件 ────────────────────────────────────────────
    def feed(self, event: dict) -> list[dict]:
        if not isinstance(event, dict):
            return []
        kind = str(event.get('type') or '')
        if kind == 'error':
            error = event.get('error') if isinstance(event.get('error'), dict) else {}
            message = str(error.get('message') or error.get('type') or '上游返回错误')
            raise _StreamError(message, code=str(error.get('type') or 'upstream_error'))
        if kind == 'message_start':
            data = event.get('message') if isinstance(event.get('message'), dict) else {}
            if data.get('id'):
                self.id = str(data['id'])
            raw = data.get('usage') if isinstance(data.get('usage'), dict) else {}
            self._input = _int(raw.get('input_tokens'))
            self._cache_read = _int(raw.get('cache_read_input_tokens'))
            self._cache_write = _int(raw.get('cache_creation_input_tokens'))
            self._role_sent = True
            return [self._chunk({'role': 'assistant', 'content': ''})]
        if kind == 'content_block_start':
            block = event.get('content_block')
            if not isinstance(block, dict):
                return []
            if block.get('type') == 'tool_use':
                index = len(self.tools)
                self.tools[_int(event.get('index'))] = index
                return [self._chunk({'tool_calls': [{
                    'index': index,
                    'id': str(block.get('id') or f'call_{uuid.uuid4().hex[:12]}'),
                    'type': 'function',
                    'function': {'name': str(block.get('name') or ''),
                                 'arguments': ''},
                }]})]
            return []
        if kind == 'content_block_delta':
            delta = event.get('delta') if isinstance(event.get('delta'), dict) else {}
            dtype = str(delta.get('type') or '')
            if dtype == 'text_delta':
                text = str(delta.get('text') or '')
                return [self._chunk({'content': text})] if text else []
            if dtype == 'thinking_delta':
                text = str(delta.get('thinking') or '')
                return [self._chunk({'reasoning_content': text})] if text else []
            if dtype == 'input_json_delta':
                index = self.tools.get(_int(event.get('index')), 0)
                partial = str(delta.get('partial_json') or '')
                return [self._chunk({'tool_calls': [{
                    'index': index,
                    'function': {'arguments': partial},
                }]})] if partial else []
            return []
        if kind == 'message_delta':
            delta = event.get('delta') if isinstance(event.get('delta'), dict) else {}
            reason = delta.get('stop_reason')
            if reason:
                self.finish_reason = _STOP_REASONS.get(str(reason), 'stop')
            raw = event.get('usage') if isinstance(event.get('usage'), dict) else {}
            self._output = _int(raw.get('output_tokens')) or self._output
            return []
        return []

    def finish(self) -> list[dict]:
        out: list[dict] = []
        if not self._role_sent:
            out.append(self._chunk({'role': 'assistant', 'content': ''}))
        out.append(self._chunk({}, self.finish_reason or 'stop'))
        out.append(self._chunk({}, usage=self._usage_out()))
        return out


class _StreamError(Exception):
    def __init__(self, message: str, *, code: str = 'upstream_error') -> None:
        super().__init__(message)
        self.message = message
        self.code = code
