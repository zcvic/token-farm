"""灵犀 `chat/completions` 的请求体构造与 SSE → OpenAI 翻译。

上游请求体本身就是 **OpenAI 形状**，但实测有两条硬约束（`PROTOCOL.md` §3.3.1）：

  · `content` 只能是**字符串**。分段数组（`[{type:"text",text}]`）任何角色
    都返回 `400 InvalidArgument`，必须展平；
  · `tool_choice` 只能是**字符串**。对象形式（指定具体函数）同样 400，降级成
    `required` / `auto`。

响应侧与 OpenAI 有三处本质差异：

  1. 正文在 `choices[].text`，不是 `delta.content`；
  2. **没有 `[DONE]`**，靠 `choices[].finish_reason` 收尾；
  3. `usage.total_tokens` 是「当前上下文大小」而不是本轮用量，必须自己重算。

`code !== "Success"` 与审核标记都是**流内错误**，必须以异常形式抛给上层：
安静吞掉会让客户端看到一条正常结束的空流。
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from ..base import PlatformError
from . import constants as C


# ── 请求体归一 ──────────────────────────────────────────────
def _part_text(part: object) -> str | None:
    if isinstance(part, str):
        return part
    if isinstance(part, dict) and isinstance(part.get('text'), str):
        return part['text']
    return None


def flatten_content(content: object) -> tuple[str, int]:
    """`content` → `(文本, 丢弃的非文本分段数)`。

    `null` / 对象 / 缺失一律当空串：上游对 `content:null` 也返回
    `InvalidArgument`，只认字符串。
    """
    if isinstance(content, str):
        return content, 0
    if not isinstance(content, list):
        return '', 0
    texts: list[str] = []
    dropped = 0
    for part in content:
        text = _part_text(part)
        if text is None:
            dropped += 1
            continue
        texts.append(text)
    return '\n'.join(texts), dropped


def normalize_messages(messages: object) -> tuple[list[dict], int]:
    """逐条展平 `content`，`developer` 角色换成 `system`，其余键原样保留。"""
    items = messages if isinstance(messages, list) else []
    out: list[dict] = []
    dropped_parts = 0
    for message in items:
        if not isinstance(message, dict):
            continue
        role = str(message.get('role') or '').strip()
        if role == 'developer':
            role = 'system'
        content = message.get('content')
        text, dropped = flatten_content(content)
        if isinstance(content, list):
            dropped_parts += dropped
        entry = {**message, 'role': role or 'user', 'content': text}
        out.append(entry)
    return out, dropped_parts


def normalize_tool_choice(choice: object) -> str | None:
    """对象 `tool_choice` → 上游接受的字符串；`None` = 不必发送。"""
    if choice is None:
        return None
    if isinstance(choice, str):
        return choice
    if not isinstance(choice, dict):
        return 'auto'
    kind = str(choice.get('type') or '')
    if kind in ('none', 'auto', 'required'):
        return kind
    if kind == 'function':
        return 'required'
    return 'auto'


def _int_positive(value: object) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def build_body(body: dict, model: str) -> tuple[dict, int]:
    """OpenAI 请求体 → 灵犀请求体（上游固定流式，`stream` 一律 True）。

    返回 `(上游请求体, 被丢弃的非文本分段数)`；分段数只用于日志。
    """
    if not isinstance(body, dict):
        raise PlatformError('请求体必须是 JSON 对象', status=400,
                            code='bad_request')
    messages, dropped = normalize_messages(body.get('messages'))
    if not messages:
        raise PlatformError('messages 不能为空', status=400,
                            code='empty_messages')
    out: dict[str, Any] = {
        'stream': True,
        'model': C.upstream_model_id(model),
        'messages': messages,
    }

    tools = body.get('tools')
    if isinstance(tools, list) and tools:
        out['tools'] = tools
        choice = normalize_tool_choice(body.get('tool_choice'))
        if choice is not None:
            out['tool_choice'] = choice

    temperature = body.get('temperature')
    if isinstance(temperature, (int, float)) and not isinstance(temperature, bool):
        out['temperature'] = temperature
    top_p = body.get('top_p')
    if isinstance(top_p, (int, float)) and not isinstance(top_p, bool):
        out['top_p'] = top_p

    max_tokens = _int_positive(body.get('max_tokens')) \
        or _int_positive(body.get('max_completion_tokens'))
    if max_tokens:
        out['base_llm_arguments'] = {'max_tokens': max_tokens}

    effort = body.get('reasoning_effort')
    effort = effort.strip() if isinstance(effort, str) else ''
    if effort:
        # 上游同时读这两处；只写一处时行为不稳定。
        out['reasoning_effort'] = effort
        out['llm_arguments'] = {'reasoning_effort': effort}

    return out, dropped


# ── SSE → OpenAI ────────────────────────────────────────────
class StreamError(Exception):
    def __init__(self, message: str, *, code: str = 'upstream_error') -> None:
        super().__init__(message)
        self.message = str(message)
        self.code = str(code)


def _number(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return int(number) if number > 0 else 0


def _success_code(code: object) -> bool:
    if code in (None, ''):
        return True
    return isinstance(code, str) and code.lower() == 'success'


def normalize_usage(raw: object) -> dict | None:
    """上游用量归一，字段名对齐 OpenAI；`total_tokens` 自行重算。"""
    if not isinstance(raw, dict):
        return None
    prompt = _number(raw.get('prompt_tokens') if raw.get('prompt_tokens')
                     is not None else raw.get('input_tokens'))
    completion = _number(raw.get('completion_tokens')
                         if raw.get('completion_tokens') is not None
                         else raw.get('output_tokens'))
    if prompt == 0 and completion == 0:
        return None
    out: dict[str, Any] = {
        'prompt_tokens': prompt,
        'completion_tokens': completion,
        'total_tokens': prompt + completion,
    }
    prompt_details = raw.get('prompt_tokens_details')
    prompt_details = prompt_details if isinstance(prompt_details, dict) else {}
    cached = prompt_details.get('cached_tokens')
    if cached is None:
        cached = raw.get('cached_tokens')
    if cached is not None:
        out['prompt_tokens_details'] = {'cached_tokens': _number(cached)}
    completion_details = raw.get('completion_tokens_details')
    completion_details = completion_details if isinstance(completion_details,
                                                          dict) else {}
    reasoning = completion_details.get('reasoning_tokens')
    if reasoning is None:
        reasoning = raw.get('reasoning_tokens')
    if reasoning is not None:
        out['completion_tokens_details'] = {
            'reasoning_tokens': _number(reasoning)}
    return out


class StreamTranslator:
    """灵犀 SSE 帧 → OpenAI chunk / Chat Completion。

    用法：
      · 流式：`start()` 先发 role 帧，之后每帧交给 `feed()`，流结束调 `finish()`；
      · 非流式：照常 `feed()`，最后用 `completion()` 取聚合结果。
    """

    def __init__(self, model: str, usage: dict | None = None) -> None:
        self.model = model
        self.usage = usage if isinstance(usage, dict) else {}
        self.id = f'chatcmpl-lingxi-{uuid.uuid4().hex[:24]}'
        self.created = int(time.time())
        self._text: list[str] = []
        self._reasoning: list[str] = []
        self._calls: dict[int, dict] = {}
        self._role_sent = False
        # `_seen_finish` = 上游已经给过 `finish_reason`（流应到此结束）；
        # `_closed` = 翻译器已经收过尾（`finish()` 或非流式聚合已产出）。
        self._seen_finish = False
        self._closed = False
        self.finish_reason: str | None = None

    # ── 状态 ────────────────────────────────────────────
    @property
    def text(self) -> str:
        return ''.join(self._text)

    @property
    def reasoning(self) -> str:
        return ''.join(self._reasoning)

    @property
    def finished(self) -> bool:
        """上游是否已经给出 `finish_reason`（没有就是响应被截断）。"""
        return self._seen_finish

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def has_tool_calls(self) -> bool:
        return bool(self._calls)

    def _chunk(self, delta: dict, finish: str | None = None, *,
               index: int = 0, usage: dict | None = None) -> dict:
        out: dict[str, Any] = {
            'id': self.id,
            'object': 'chat.completion.chunk',
            'created': self.created,
            'model': self.model,
            'choices': [] if usage is not None else [{
                'index': index,
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

    def text_chunk(self, text: str) -> dict:
        """额外补一段正文（只用于「上游一个字都没给」时的兜底提示）。"""
        self._text.append(text)
        return self._chunk({'content': text})

    # ── 帧处理 ──────────────────────────────────────────
    def _fail(self, exc: StreamError) -> None:
        raise exc

    def _merge_call(self, index: int, raw: dict) -> None:
        current = self._calls.setdefault(
            index, {'id': '', 'name': '', 'arguments': ''})
        call_id = raw.get('id')
        if isinstance(call_id, str) and call_id:
            current['id'] = call_id
        function = raw.get('function')
        if isinstance(function, dict):
            name = function.get('name')
            if isinstance(name, str) and name:
                current['name'] = name
            arguments = function.get('arguments')
            if isinstance(arguments, str):
                current['arguments'] += arguments

    def _delta_calls(self, raw_calls: list) -> list[dict]:
        out: list[dict] = []
        for position, raw in enumerate(raw_calls):
            if not isinstance(raw, dict):
                continue
            index = raw.get('index')
            index = index if isinstance(index, int) and index >= 0 else position
            item: dict[str, Any] = {'index': index}
            call_id = raw.get('id')
            if isinstance(call_id, str) and call_id:
                item['id'] = call_id
                item['type'] = 'function'
            function = raw.get('function') if isinstance(raw.get('function'),
                                                          dict) else {}
            fn: dict[str, Any] = {}
            if isinstance(function.get('name'), str) and function['name']:
                fn['name'] = function['name']
            if isinstance(function.get('arguments'), str) and function['arguments']:
                fn['arguments'] = function['arguments']
            item['function'] = fn
            out.append(item)
            self._merge_call(index, raw)
        return out

    def feed(self, frame: object) -> list[dict]:
        if not isinstance(frame, dict) or self._closed or self._seen_finish:
            return []
        # 审核拦截：只匹配诊断字段，模型正文不参与（见 client.is_audit）。
        from .client import is_audit  # noqa: PLC0415 —— 避免循环导入
        if is_audit(frame):
            self._fail(StreamError('灵犀上游返回 AuditFailed：内容被审核拦截',
                                   code='audit_blocked'))
        if not _success_code(frame.get('code')):
            from .client import _text_of  # noqa: PLC0415
            self._fail(StreamError(
                f'灵犀上游返回 {frame.get("code")}：'
                f'{_text_of(frame) or "上游错误"}'))

        usage = normalize_usage(frame.get('usage'))
        if usage is not None:
            self.usage.update(usage)

        chunks: list[dict] = []
        choices = frame.get('choices')
        choices = choices if isinstance(choices, list) else []
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            index = choice.get('index')
            index = index if isinstance(index, int) and index >= 0 else 0
            text = choice.get('text')
            text = text if isinstance(text, str) else ''
            reasoning = choice.get('reasoning_content')
            reasoning = reasoning if isinstance(reasoning, str) else ''
            raw_calls = choice.get('tool_calls')
            raw_calls = raw_calls if isinstance(raw_calls, list) else []

            delta: dict[str, Any] = {}
            if text:
                self._text.append(text)
                delta['content'] = text
            if reasoning:
                self._reasoning.append(reasoning)
                delta['reasoning_content'] = reasoning
            if raw_calls:
                calls = self._delta_calls(raw_calls)
                if calls:
                    delta['tool_calls'] = calls
            if delta:
                if not self._role_sent:
                    chunks.extend(self.start())
                chunks.append(self._chunk(delta, index=index))

            finish = choice.get('finish_reason')
            if isinstance(finish, str) and finish.strip() and not self._seen_finish:
                # 收尾帧**留到 `finish()` 再发**：这样「上游一个字都没给」时
                # 适配器还有机会在 finish_reason 之前补一段说明，而不是把一个
                # 只有结束标记的空流交给客户端。
                self.finish_reason = finish.strip()
                self._seen_finish = True

        return chunks

    def usage_dict(self) -> dict:
        """把当前 usage 规整成网关认的键（同一 dict 对象，便于记账）。"""
        prompt = _number(self.usage.get('prompt_tokens'))
        completion = _number(self.usage.get('completion_tokens'))
        data = {
            'prompt_tokens': prompt,
            'completion_tokens': completion,
            'total_tokens': prompt + completion,
        }
        for key in ('prompt_tokens_details', 'completion_tokens_details'):
            value = self.usage.get(key)
            if isinstance(value, dict):
                data[key] = value
        self.usage.clear()
        self.usage.update(data)
        return data

    def finish(self) -> list[dict]:
        if self._closed:
            return []
        self._closed = True
        out: list[dict] = []
        if not self._role_sent:
            out.extend(self.start())
        out.append(self._chunk({}, self.finish_reason or 'stop'))
        out.append(self._chunk({}, usage=self.usage_dict()))
        return out

    def completion(self) -> dict:
        """非流式聚合结果（OpenAI Chat Completion）。"""
        self._closed = True
        message: dict[str, Any] = {
            'role': 'assistant',
            'content': self.text,
        }
        if self.reasoning:
            message['reasoning_content'] = self.reasoning
        if self._calls:
            ordered = [self._calls[index] for index in sorted(self._calls)]
            message['tool_calls'] = [
                {
                    'id': call['id'] or f'call_lingxi_{uuid.uuid4().hex[:8]}',
                    'type': 'function',
                    'function': {'name': call['name'],
                                 'arguments': call['arguments']},
                }
                for call in ordered
            ]
        return {
            'id': self.id,
            'object': 'chat.completion',
            'created': self.created,
            'model': self.model,
            'choices': [{
                'index': 0,
                'message': message,
                'finish_reason': self.finish_reason or 'stop',
            }],
            'usage': self.usage_dict(),
        }