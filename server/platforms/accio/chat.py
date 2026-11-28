"""Accio（Gemini 风格）帧 → OpenAI chunk / Chat Completion。

上游与 Gemini 是同一族协议，但有两处必须在本层消化的差异：

  · 帧是**顶层** `{content:{parts}, usage_metadata, finish_reason}`，不是
    `{candidates:[{content}]}`；
  · 工具调用是 `parts[].function_call`（snake），参数在 **`args_json` 字符串**里。

## 工具调用必须跨帧合并

实测形状（三轮取样一致）：

    frame#8  {"function_call":{"id":"call_x","name":"read_file"}}            ← 无参数
    frame#9  {"function_call":{"id":"call_x","name":"read_file","args_json":"{\"file_path\":\"a.txt\"}"}}
    frame#10 {"function_call":{"id":"call_x","name":"read_file","args_json":"{\"file_path\":\"a.txt\"}"}}

不合并会产出 **3 个** 工具调用（首个参数为空），客户端会把同一个工具执行
N 次。合并规则与参考实现 `mergeAccioToolArgs` 一致：后一份以前一份为前缀 ⇒
取最后一份（重复全量）；否则按增量拼接。

## 其它硬约束

  · `error_code === '200'` 是成功哨兵，必须先于错误判据；
  · 流内 `error_code === '402'` ⇒ 额度耗尽（换号 + 冷却），其余非 200 码按
    服务器错误上抛；
  · 同一帧里的正文 / `thought` **绝不**因为缓冲工具而丢弃；
  · usage 取 **total 最大** 的那一帧（上游会重复播报，早期帧偏小）。
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any

from ..base import PlatformError
from . import protocol as P

_FINISH_MAP = {
    'STOP': 'stop',
    'MAX_TOKENS': 'length',
    'SAFETY': 'content_filter',
    'RECITATION': 'content_filter',
    'BLOCKLIST': 'content_filter',
    'PROHIBITED_CONTENT': 'content_filter',
    'SPII': 'content_filter',
    'MALFORMED_FUNCTION_CALL': 'tool_calls',
    'OTHER': 'stop',
    'FINISH_REASON_UNSPECIFIED': 'stop',
}
_OPENAI_FINISH = {'stop', 'length', 'tool_calls', 'content_filter'}


def _map_finish(value: object) -> str | None:
    text = str(value or '').strip()
    if not text:
        return None
    if text.lower() in _OPENAI_FINISH:
        return text.lower()
    return _FINISH_MAP.get(text.upper(), 'stop')


def _num(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float('inf') else None


def _pick_usage(*values: object) -> dict:
    for value in values:
        if isinstance(value, dict):
            return value
    return {}


def _usage_total(raw: object) -> float:
    if not isinstance(raw, dict):
        return -1.0
    for key in ('totalTokenCount', 'total_token_count'):
        number = _num(raw.get(key))
        if number is not None:
            return number
    return -1.0


def usage_openai(raw: object) -> dict:
    """Gemini `usageMetadata` → OpenAI `usage`（prompt / completion / total）。"""
    if not isinstance(raw, dict):
        return {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}
    prompt = _num(raw.get('promptTokenCount', raw.get('prompt_token_count'))) or 0
    candidates = _num(raw.get('candidatesTokenCount',
                              raw.get('candidates_token_count'))) or 0
    thoughts = _num(raw.get('thoughtsTokenCount',
                            raw.get('thoughts_token_count'))) or 0
    total = _num(raw.get('totalTokenCount', raw.get('total_token_count')))
    completion = int(candidates + thoughts)
    prompt = int(prompt)
    if total is None:
        total = prompt + completion
    out: dict[str, Any] = {
        'prompt_tokens': prompt,
        'completion_tokens': completion,
        'total_tokens': int(total),
    }
    if thoughts:
        out['completion_tokens_details'] = {'reasoning_tokens': int(thoughts)}
    return out


# ── 帧解析 ──────────────────────────────────────────────────
def _content_of(frame: dict) -> tuple[object, object, object]:
    """`(content, finish, usage)`；兼容 Accio 顶层与标准 Gemini 形状。"""
    candidates = frame.get('candidates')
    if isinstance(candidates, list) and candidates:
        cand = candidates[0] if isinstance(candidates[0], dict) else {}
        content = cand.get('content')
        finish = cand.get('finishReason', cand.get('finish_reason'))
        usage = _pick_usage(frame.get('usageMetadata'),
                            frame.get('usage_metadata'))
        return content, finish, usage
    content = frame.get('content')
    finish = frame.get('finish_reason', frame.get('finishReason'))
    usage = _pick_usage(frame.get('usage_metadata'),
                        frame.get('usageMetadata'))
    return content, finish, usage


def _args_text(call: dict) -> str:
    """参数串：`args_json` / `argsJson` → 对象 `args`（空串必须继续回退）。"""
    raw = call.get('args_json', call.get('argsJson'))
    if isinstance(raw, str) and raw != '':
        return raw
    data = call.get('args')
    if isinstance(data, dict):
        try:
            return json.dumps(data, ensure_ascii=False)
        except (TypeError, ValueError):
            return ''
    return ''


def _tool_part(part: dict) -> dict | None:
    call = part.get('functionCall')
    if not isinstance(call, dict):
        call = part.get('function_call')
    if not isinstance(call, dict):
        return None
    name = str(call.get('name') or '').strip()
    if not name:
        # 无名工具调用整块丢弃（产出无名调用会让客户端 400），但不连坐同帧
        # 的正文 / usage。
        return None
    return {
        'kind': 'tool',
        'id': str(call.get('id') or '').strip(),
        'name': name,
        'args': _args_text(call),
    }


def parts_of(frame: object) -> list[dict]:
    """一帧里的语义块：正文 / 思考 / 工具调用（顺序保持上游顺序）。"""
    if not isinstance(frame, dict):
        return []
    content, _finish, _usage = _content_of(frame)
    raw_parts: list[object]
    if isinstance(content, dict):
        parts = content.get('parts')
        raw_parts = parts if isinstance(parts, list) else []
    elif isinstance(content, str):
        raw_parts = [{'text': content}]
    else:
        raw_parts = []
    # 顶层 function_call（无 content 信封）绝不能丢。
    for key in ('function_call', 'functionCall'):
        if key in frame:
            raw_parts = [*raw_parts, {key: frame.get(key)}]
            break

    out: list[dict] = []
    for raw in raw_parts:
        if not isinstance(raw, dict):
            continue
        tool = _tool_part(raw)
        if tool is not None:
            out.append(tool)
            continue
        text = raw.get('text')
        if isinstance(text, str) and text != '':
            kind = 'reasoning' if raw.get('thought') is True else 'text'
            out.append({'kind': kind, 'text': text})
    return out


def merge_args(fragments: list[str]) -> str:
    """合并参数分片（重复全量取最后一份，增量则依次拼接）。"""
    values = [item for item in fragments if item]
    if not values:
        return ''
    cumulative = True
    for index in range(1, len(values)):
        if not values[index].startswith(values[index - 1]):
            cumulative = False
            break
    if cumulative:
        return values[-1]
    merged = ''
    for fragment in values:
        merged = fragment if fragment.startswith(merged) else merged + fragment
    return merged


def stream_error(code: str, message: str) -> PlatformError:
    failure = P.failure_for_stream(code, message)
    exc = PlatformError(f'Accio 对话失败：{failure.message}',
                        status=failure.status, code=failure.code)
    exc.failure = failure          # type: ignore[attr-defined]
    return exc


def _check_error(frame: object) -> None:
    if not isinstance(frame, dict):
        return
    code, message = P.frame_error_of(frame)
    if code:
        raise stream_error(code, message)


# ── 翻译器 ──────────────────────────────────────────────────
class StreamTranslator:
    """Accio 帧 → OpenAI chunk / Chat Completion。"""

    def __init__(self, model: str, usage: dict | None = None) -> None:
        self.model = model
        self.usage = usage if isinstance(usage, dict) else {}
        self.id = f'chatcmpl-{uuid.uuid4().hex[:24]}'
        self.created = int(time.time())
        self._text: list[str] = []
        self._reasoning: list[str] = []
        self._pending: dict[str, dict] = {}
        self._order: list[str] = []
        self._has_tools = False
        self._all_calls: list[dict] = []
        self._role_sent = False
        self._finish: str | None = None
        self._usage_raw: dict | None = None

    # ── 状态 ────────────────────────────────────────────
    @property
    def text(self) -> str:
        return ''.join(self._text)

    @property
    def reasoning(self) -> str:
        return ''.join(self._reasoning)

    @property
    def has_content(self) -> bool:
        return bool(self._text or self._reasoning or self._has_tools
                    or self._pending)

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

    def start(self) -> list[dict]:
        if self._role_sent:
            return []
        self._role_sent = True
        return [self._chunk({'role': 'assistant', 'content': ''})]

    # ── 工具调用缓冲 ────────────────────────────────────
    def _key_for(self, part: dict) -> str:
        call_id = str(part.get('id') or '')
        if call_id:
            return f'id:{call_id}'
        name = str(part.get('name') or '')
        for key in self._order:
            if self._pending[key].get('name') == name:
                return key
        return f'anon-{len(self._order) + 1}'

    def _buffer(self, part: dict) -> None:
        key = self._key_for(part)
        item = self._pending.get(key)
        if item is None:
            item = {'id': str(part.get('id') or ''), 'name': part['name'],
                    'fragments': []}
            self._pending[key] = item
            self._order.append(key)
        args = str(part.get('args') or '')
        if args:
            item['fragments'].append(args)

    def _flush_tools(self) -> list[dict]:
        if not self._order:
            return []
        calls: list[dict] = []
        for index, key in enumerate(self._order):
            item = self._pending[key]
            arguments = merge_args(item['fragments']) or '{}'
            call_id = item['id'] or key
            self._all_calls.append({'id': call_id, 'name': item['name'],
                                    'arguments': arguments})
            calls.append({
                'index': index,
                'id': call_id,
                'type': 'function',
                'function': {'name': item['name'], 'arguments': arguments},
            })
        self._pending.clear()
        self._order.clear()
        self._has_tools = True
        return [self._chunk({'tool_calls': calls})]

    # ── 帧 ──────────────────────────────────────────────
    def feed(self, frame: object) -> list[dict]:
        _check_error(frame)
        if not isinstance(frame, dict):
            return []
        parts = parts_of(frame)
        tool_parts = [item for item in parts if item.get('kind') == 'tool']
        chunks: list[dict] = []
        # flush 必须排在当前行之前：工具调用仍出现在后面的 usage / finish 之前。
        if not tool_parts:
            chunks.extend(self._flush_tools())

        for part in parts:
            if part.get('kind') == 'tool':
                self._buffer(part)
                continue
            text = str(part.get('text') or '')
            if not text:
                continue
            if part.get('kind') == 'reasoning':
                self._reasoning.append(text)
                chunks.append(self._chunk({'reasoning_content': text}))
            else:
                self._text.append(text)
                chunks.append(self._chunk({'content': text}))

        _content, finish, usage = _content_of(frame)
        mapped = _map_finish(finish)
        if mapped is not None:
            self._finish = mapped
        if isinstance(usage, dict) and usage:
            if _usage_total(usage) >= _usage_total(self._usage_raw):
                self._usage_raw = usage
        return chunks

    def usage_dict(self) -> dict:
        data = usage_openai(self._usage_raw)
        self.usage.update(data)
        return data

    def _finish_reason(self) -> str:
        if self._has_tools:
            return 'tool_calls'
        return self._finish or 'stop'

    def finish(self) -> list[dict]:
        chunks: list[dict] = []
        if not self._role_sent:
            chunks.extend(self.start())
        chunks.extend(self._flush_tools())
        chunks.append(self._chunk({}, self._finish_reason()))
        chunks.append(self._chunk({}, usage=self.usage_dict()))
        return chunks

    def completion(self) -> dict:
        if self._order:
            self._flush_tools()
        message: dict[str, Any] = {
            'role': 'assistant',
            'content': self.text,
        }
        if self.reasoning:
            message['reasoning_content'] = self.reasoning
        if self._all_calls:
            message['tool_calls'] = [{
                'id': call['id'],
                'type': 'function',
                'function': {'name': call['name'],
                             'arguments': call['arguments']},
            } for call in self._all_calls]
        return {
            'id': self.id,
            'object': 'chat.completion',
            'created': self.created,
            'model': self.model,
            'choices': [{
                'index': 0,
                'message': message,
                'finish_reason': self._finish_reason(),
            }],
            'usage': self.usage_dict(),
        }


def aggregate(frames: list[dict], model: str,
              usage: dict | None = None) -> dict:
    """非流式：把整条帧列表聚合成一份 Chat Completion。"""
    translator = StreamTranslator(model, usage)
    for frame in frames:
        translator.feed(frame)
    if not translator.has_content:
        raise PlatformError('Accio 没有返回内容', status=502,
                            code='empty_response')
    return translator.completion()
