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

import json
import time
import uuid
from typing import Any, AsyncIterator

from ..base import PlatformError
from ... import config
from . import text_tools


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
                               ('prompt_cache_miss_tokens', 'prompt_cache_miss_tokens'),
                               ('prompt_cache_write_tokens', 'prompt_cache_write_tokens'),
                               ('credit', 'credit')):
        value = source.get(src_name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            usage[dst_name] = int(value) if dst_name != 'credit' else float(value)
    return usage


# ── 工具调用：上游私有形状 → OpenAI 形状 ─────────────────────
#
# 实测（2026-11-01，`llm_utils_chat` / deepseek-v4.1-flash）上游是这样下发的：
#
#   {"index":0,"id":"call_x","type":"function",
#    "function_call":{"name":"get_weather","arguments":"",
#                     "partial_arguments":null,"namespace":null}}
#
# 与 OpenAI 的三处差异，全部在这里抹平：
#   · 字段叫 `function_call` 而不是 `function`；
#   · 参数**按 index 分片**增量下发，一帧里可能同时出现多个 index；
#   · 空帧里的 `id` / `type` / `name` 是空串（OpenAI 客户端按 index 合并，
#     空串会把已收到的值覆盖掉，所以只在非空时才写）。

def _tool_call_delta(raw: object) -> list[dict]:
    """把一帧 `tool_calls` 转成 OpenAI 的 `delta.tool_calls` 片段。"""
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[dict] = []
    for fallback, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get('index', fallback))
        except (TypeError, ValueError):
            index = fallback
        piece: dict[str, Any] = {'index': index}
        call_id = item.get('id')
        if isinstance(call_id, str) and call_id:
            piece['id'] = call_id
        kind = item.get('type')
        if isinstance(kind, str) and kind:
            piece['type'] = kind
        fn = item.get('function_call')
        if not isinstance(fn, dict):
            fn = item.get('function') if isinstance(item.get('function'), dict) else {}
        function: dict[str, Any] = {}
        name = fn.get('name')
        if isinstance(name, str) and name:
            function['name'] = name
        args = fn.get('arguments')
        if isinstance(args, (dict, list)):
            args = json.dumps(args, ensure_ascii=False)
        if not (isinstance(args, str) and args):
            # 上游少数帧把增量参数放在 `partial_arguments` 里（实测样本为
            # null，但字段存在）；只读 `arguments` 会下发「有名字没参数」的
            # 调用，客户端执行 `write` / `edit` 直接报缺参。
            args = fn.get('partial_arguments')
            if isinstance(args, (dict, list)):
                args = json.dumps(args, ensure_ascii=False)
        if isinstance(args, str) and args:
            function['arguments'] = args
        if function:
            piece['function'] = function
        # 只有 index、什么内容都没有的帧没有意义，丢掉
        if len(piece) > 1:
            out.append(piece)
    return out


def _merge_tool_call(acc: dict[int, dict], piece: dict) -> None:
    """把一帧片段并进聚合结果（非流式用）。"""
    index = int(piece.get('index') or 0)
    entry = acc.setdefault(index, {'id': '', 'name': '', 'arguments': ''})
    if piece.get('id') and not entry['id']:
        entry['id'] = str(piece['id'])
    fn = piece.get('function') or {}
    if fn.get('name') and not entry['name']:
        entry['name'] = str(fn['name'])
    args = fn.get('arguments')
    if not args:
        args = fn.get('partial_arguments')
    if isinstance(args, (dict, list)):
        # `str(dict)` 是 Python repr（单引号），不是合法 JSON，客户端解析必失败。
        args = json.dumps(args, ensure_ascii=False)
    if args:
        entry['arguments'] += str(args)


def _tool_calls_final(acc: dict[int, dict]) -> list[dict]:
    """聚合结果 → OpenAI `message.tool_calls`。"""
    out: list[dict] = []
    for index in sorted(acc):
        entry = acc[index]
        name = entry['name']
        if not name:
            continue  # 只有参数没有名字的残缺片段，交给客户端只会报解析错
        out.append({
            'id': entry['id'] or f'call_{index}',
            'type': 'function',
            'function': {'name': name,
                         'arguments': entry['arguments'] or '{}'},
        })
    return out


def _valid_arguments(raw: object) -> bool:
    """原生 `arguments` 是否是一段完整、合法的 JSON。

    空串按 `{}` 处理（不算截断）；对象/数组原样算合法（上游偶尔直接给结构）。
    """
    if isinstance(raw, (dict, list)):
        return True
    if not isinstance(raw, str) or not raw.strip():
        return True
    try:
        json.loads(raw)
    except ValueError:
        return False
    return True


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
        # 是否真的收到了上游的结束帧（`done` / `[DONE]`）。断流时它是 False，
        # 用来区分「正常说完」和「连接被掐断」——见 `translate()` 收尾。
        self.saw_done = False


class _IndexAllocator:
    """给「正文还原」和「原生调用」分配互不冲突的 `delta.tool_calls` index。

    上一版正文还原用「已还原条数」当起点、原生调用直接沿用上游 index（都从 0
    开始），同一轮里既有正文调用又有原生调用时两条落在同一个 index 上，客户端
    按 index 合并 → 参数被拼成非法 JSON（2026-11-02 实测：
    `{"command":"ls"}{"path":"a"}`）。

    上游同一个 index 会分多帧到达，所以必须记住 `upstream -> 分配` 的映射，
    不能每次自增。
    """

    def __init__(self) -> None:
        self._next = 0
        self._native: dict[int, int] = {}

    def rescue(self, count: int = 1) -> int:
        """分配 `count` 个连续下标，返回起始值。"""
        index = self._next
        self._next += max(1, count)
        return index

    def native(self, upstream: object) -> int:
        try:
            key = int(upstream or 0)
        except (TypeError, ValueError):
            key = 0
        index = self._native.get(key)
        if index is None:
            index = self._native[key] = self._next
            self._next += 1
        return index


def _rescue_tool_calls(calls: list[dict], index: int) -> list[dict]:
    """把正文里捞出来的调用做成 OpenAI `delta.tool_calls` 片段。

    `index` 是**已经分配好**的起始下标；`id` 用固定前缀——这类调用没有上游 id，
    客户端只需要一个能配对的唯一值（回填历史时 `_fold_tool_history` 也只用到
    名字和参数）。
    """
    out: list[dict] = []
    for offset, call in enumerate(calls):
        slot = index + offset
        out.append({
            'index': slot,
            'id': f'call_rescue_{slot}',
            'type': 'function',
            'function': {'name': call['name'], 'arguments': call['arguments']},
        })
    return out


async def translate(events: AsyncIterator[tuple[str, Any]], *, model: str,
                    state: StreamState,
                    reasoning_as_content: bool = True,
                    text_tool_rescue: bool = True,
                    tool_names: object = (),
                    strict_tail: bool = True) -> AsyncIterator[dict]:
    """把 Trae 事件流翻译成 OpenAI chunk 流。

    `state` 由调用方持有，流结束后从这里读 usage / 错误 / 首字时间。

    `reasoning_as_content`（默认关闭）：仅当整轮既没有正文也没有工具调用时，把
    思考原文补一帧正文。正常回合一定会有正文，唯一合法的「无正文」是工具调用轮
    ——那种轮次带 `tool_calls`，不会走到这里。该开关只是上游异常时的保险。

    `text_tool_rescue`（默认开启）：模型把调用写成正文（`[tool_call] name(args)`）
    时还原成 `tool_calls`，见 `text_tools`。上游一旦给出结构化调用就自动让位——
    那条通道是准的，正文不该再猜。整轮只剩空标签/半截调用时以错误收尾，不再
    下发空正文 + `stop`（见 `text_tools.is_degenerate_call`）。

    `tool_names` 是客户端本轮声明的工具名：给了才启用**标签形态**的还原
    （`<execute code="...">`），并且只在标签能对上已声明工具时才动手。
    """
    chunk_id = new_id()
    created = int(time.time())
    yield _delta_chunk(chunk_id, created, model, delta={'role': 'assistant'})

    reasoning_parts: list[str] = []
    raw_parts: list[str] = []
    saw_tools = False
    sniffer = text_tools.Sniffer(enabled=text_tool_rescue,
                                 tool_names=tool_names,
                                 strict_tail=strict_tail)
    indexes = _IndexAllocator()
    # 原生调用的参数原文按「分配后的 index」累计，只用于收尾的 JSON 完整性
    # 校验（截断的半条调用不能下发，客户端 `JSON.parse` 会直接抛错）。
    native_args: dict[int, str] = {}
    async for event, data in events:
        if event == 'metadata':
            continue
        if event == 'output' and isinstance(data, dict):
            reasoning = data.get('reasoning_content')
            if isinstance(reasoning, str) and reasoning:
                reasoning_parts.append(reasoning)
                yield _delta_chunk(chunk_id, created, model,
                                   delta={'reasoning_content': reasoning})
            calls = _tool_call_delta(data.get('tool_calls'))
            if calls:
                saw_tools = True
                # 原生通道已经给出准的调用：缓冲区里的文本先按原顺序补发，
                # 再关掉嗅探——否则它会排到后面的正文之后（顺序错乱）。
                for kind, payload in sniffer.disable():
                    state.saw_content = True
                    yield _delta_chunk(chunk_id, created, model,
                                       delta={'content': payload})
                state.saw_content = True
                mapped = []
                for piece in calls:
                    slot = indexes.native(piece.get('index'))
                    args = (piece.get('function') or {}).get('arguments')
                    if isinstance(args, str):
                        native_args[slot] = native_args.get(slot, '') + args
                    mapped.append({**piece, 'index': slot})
                yield _delta_chunk(chunk_id, created, model,
                                   delta={'tool_calls': mapped})
            text = data.get('response')
            if isinstance(text, str) and text:
                raw_parts.append(text)
                for kind, payload in sniffer.feed(text):
                    if kind == 'tool_calls':
                        delta = {'tool_calls': _rescue_tool_calls(
                            payload, indexes.rescue(len(payload)))}
                        saw_tools = True
                    else:
                        delta = {'content': payload}
                        if state.first_token_at is None:
                            state.first_token_at = time.time()
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
            state.saw_done = True
            if isinstance(data, dict):
                state.finish_reason = str(data.get('finish_reason') or 'stop')
            else:
                state.finish_reason = 'stop'
            continue
        if event == 'error':
            state.error = _error_text(data)
            break

    # 收尾：被扣在缓冲区里的残句（多数是截断的半条调用）原样当正文放出去，
    # 已经凑齐的调用照旧捞成 `tool_calls`。
    for kind, payload in sniffer.flush():
        if kind == 'tool_calls':
            yield _delta_chunk(chunk_id, created, model,
                               delta={'tool_calls': _rescue_tool_calls(
                                   payload, indexes.rescue(len(payload)))})
            saw_tools = True
        else:
            if state.first_token_at is None:
                state.first_token_at = time.time()
            yield _delta_chunk(chunk_id, created, model, delta={'content': payload})
        state.saw_content = True

    # 整轮只剩调用脚手架（空标签 / `</call>` / 参数被截断的半条调用）时，
    # 既没有可执行的调用，也没有可读正文。旧行为是下发空正文 + `stop`，
    # 客户端把它当成完整回复，Agent 循环静默停住；这里改成错误收尾。
    degenerate = False
    if (state.error is None and not saw_tools and text_tool_rescue
            and config.TRAE_DEGENERATE_CALL_ERROR
            and (sniffer.degenerate
                 or text_tools.is_degenerate_call(''.join(raw_parts), tool_names))):
        degenerate = True
        state.error = ('模型只给出了工具调用标签/半截调用，没有可执行的调用或正文；'
                       '本轮已中止，请重试')

    # 上游没给 done 就断流（代理重置 / 连接被掐 / 上游提前关闭）不能伪装成
    # 正常结束：客户端会当成一条完整回复，用户看到的就是「说到一半没了」。
    truncated = False
    if state.error is None and not state.saw_done and config.TRAE_STRICT_STREAM_END:
        truncated = True
        state.error = '上游流提前结束（未收到 done 帧）'

    # 原生调用的参数被 max_tokens 截断时，上游下发的 `arguments` 是残缺 JSON。
    # 客户端 `JSON.parse` 会直接抛错，Agent 循环断在「解析失败」而不是「被截断」。
    # 收尾按累计值校验一次，残缺就补一帧明确的错误并退回 `length`。
    broken_call = False
    if state.error is None and native_args:
        if any(not _valid_arguments(args) for args in native_args.values()):
            broken_call = True
            truncated = True
            state.error = '工具调用参数被截断（不是合法 JSON），本轮已中止，请重试'

    if state.error:
        # 头已经发出去了，只能补一帧错误对象。客户端会如实报错，而不是把
        # 半条回复当成完整回答继续往下走。
        code = ('degenerate_tool_call' if degenerate
                else 'truncated_tool_call' if broken_call
                else 'stream_truncated' if truncated else 'upstream_error')
        yield {'error': {'message': state.error, 'type': 'upstream_error',
                         'code': code}}

    # 思考兜底：帧序必须是「正文 → finish_reason → usage → [DONE]」——
    # 补在 finish 之后，很多客户端已经停止累积，等于白补。
    if (reasoning_as_content and reasoning_parts and not state.saw_content
            and not state.error):
        text = ''.join(reasoning_parts)
        if text.strip():
            yield _delta_chunk(chunk_id, created, model, delta={'content': text})
            state.saw_content = True
            if state.first_token_at is None:
                state.first_token_at = time.time()

    if state.error:
        # 明确报错用 `error`；纯断流用 `length`（标准里最接近「没说完」的值，
        # 客户端据此提示「回复被截断」）。
        finish = 'length' if truncated else 'error'
    else:
        finish = state.finish_reason or (
            'stop' if (state.saw_content or state.saw_done) else 'error')
    # 上游带工具调用时仍报 `stop`（实测），OpenAI 客户端靠 `tool_calls` 这个
    # finish_reason 决定要不要执行工具，所以这里按实际产出改写。
    if saw_tools and not broken_call and finish not in ('length', 'error'):
        finish = 'tool_calls'
    yield _delta_chunk(chunk_id, created, model, delta={}, finish_reason=finish,
                       usage=state.usage or None)


async def aggregate(events: AsyncIterator[tuple[str, Any]], *, model: str,
                    state: StreamState,
                    reasoning_as_content: bool = True,
                    text_tool_rescue: bool = True,
                    tool_names: object = (),
                    strict_tail: bool = True) -> dict:
    """把 Trae 事件流聚合成一个 OpenAI `chat.completion`。

    `text_tool_rescue` 与 `translate` 同义：正文里写出来的调用还原成
    `message.tool_calls`。上游给了结构化调用时以结构化为准，正文保持原样。
    整轮只剩空标签/半截调用时抛 `PlatformError`（502），不返回空正文 + `stop`。

    `tool_names` 与 `translate` 同义：标签形态的白名单。
    """
    content: list[str] = []
    reasoning: list[str] = []
    merged: dict[int, dict] = {}
    # 保留 error 帧的原始结构：判 4008 要看 code / message，只留字符串就丢了依据。
    error_data: Any = None
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
            for piece in _tool_call_delta(data.get('tool_calls')):
                _merge_tool_call(merged, piece)
                state.saw_content = True
            continue
        if event == 'token_usage':
            usage = normalize_usage(data)
            if usage:
                state.usage.clear()
                state.usage.update(usage)
            continue
        if event in ('done', 'message'):
            state.saw_done = True
            if isinstance(data, dict):
                state.finish_reason = str(data.get('finish_reason') or 'stop')
            continue
        if event == 'error':
            state.error = _error_text(data)
            error_data = data
            break

    truncated = False
    if state.error is None and not state.saw_done and config.TRAE_STRICT_STREAM_END:
        truncated = True
        state.error = '上游流提前结束（未收到 done 帧）'

    if state.error and not state.saw_content:
        raise error_from_event(error_data if error_data is not None else state.error)

    thinking = ''.join(reasoning)
    tools = _tool_calls_final(merged)
    body = ''.join(content)
    raw_body = body
    if text_tool_rescue and not tools and body:
        rescued, rest = text_tools.parse_calls(
            body, tool_names, strict_tail=strict_tail)
        if rescued:
            tools = [{'id': f'call_rescue_{index}', 'type': 'function',
                      'function': {'name': call['name'],
                                   'arguments': call['arguments']}}
                     for index, call in enumerate(rescued)]
            body = rest.strip()
            state.saw_content = True
            if body and state.first_token_at is None:
                state.first_token_at = time.time()
        else:
            # 没捞到调用也要清掉空标签 / 孤立闭合标签（`</call>` 等噪音）。
            body = rest
    # 原生调用参数残缺（被 max_tokens 截断）：这条调用不能执行，客户端
    # `JSON.parse` 会直接抛错。丢掉它；如果因此什么都不剩，就报截断错误。
    broken_call = False
    if tools:
        kept = [call for call in tools
                if _valid_arguments(call['function']['arguments'])]
        if len(kept) != len(tools):
            broken_call = True
            tools = kept
            if not tools:
                raise PlatformError(
                    '工具调用参数被截断（不是合法 JSON），本轮已中止，请重试',
                    status=502, code='truncated_tool_call')
    if (text_tool_rescue and config.TRAE_DEGENERATE_CALL_ERROR and not tools
            and not state.error
            and text_tools.is_degenerate_call(raw_body, tool_names)):
        raise PlatformError(
            '模型只给出了工具调用标签/半截调用，没有可执行的调用或正文；'
            '本轮已中止，请重试',
            status=502, code='degenerate_tool_call')
    # 与流式同口径：既无正文也无工具调用时，思考原文兼作正文。
    if reasoning_as_content and not body and not tools and not state.error:
        if thinking.strip():
            body = thinking
            state.saw_content = True
            if state.first_token_at is None:
                state.first_token_at = time.time()

    message: dict[str, Any] = {'role': 'assistant', 'content': body or None}
    if thinking:
        message['reasoning_content'] = thinking
    if tools:
        message['tool_calls'] = tools
    if broken_call:
        # 丢掉残缺调用后可能还有正文或其它完整调用：如实标记「被截断」。
        state.finish_reason = 'length'
    elif tools and state.finish_reason not in ('length', 'error'):
        state.finish_reason = 'tool_calls'
    if truncated:
        # 断流的半条回答不能报 `stop`：`length` 是标准里最接近「没说完」的值。
        state.finish_reason = 'length'
    elif state.error:
        # 中途出错但已经有正文：保留内容，同时如实标记，别伪装成完整回答。
        state.finish_reason = 'error'
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


# 上游「免费额度用尽」的业务码。它与 4015 / 4019 的**风控**完全不是一回事：
# 额度会随套餐重置恢复，账号本身没坏，所以既不冷却也不换号 —— 只要把结论
# 如实回给客户端（429 + Retry-After），让它按限流退避而不是当成网关故障重试。
QUOTA_CODE = '4008'
QUOTA_RETRY_AFTER = 60


def is_quota_error(data: Any) -> bool:
    """错误体是否表示 4008 quota exceeded。

    上游各接口放这个码的位置不一致：有时在 `code` 字段，有时只写进
    `message`（`[4008] quota exceeded`），所以两处都认。关键词范围与
    参考实现一致：`4008` / `quota exceeded` / `exceeded the quota`。
    """
    if isinstance(data, dict):
        code = data.get('code')
        if code is None:
            code = data.get('Code')
        if str(code if code is not None else '').strip() == QUOTA_CODE:
            return True
        text = ' '.join(str(data.get(key) or '')
                        for key in ('message', 'msg', 'error', 'Message'))
    else:
        text = str(data or '')
    lowered = text.lower()
    return (QUOTA_CODE in text or 'quota exceeded' in lowered
            or 'exceeded the quota' in lowered)


def error_from_event(data: Any, *, payload: Any = None) -> PlatformError:
    """把一帧 `error` 事件（或一段上游错误）翻成 `PlatformError`。

    4008 单独改判成 429：客户端会按限流退避，而不是把「额度用尽」当成
    「网关坏了」反复重试；同时它不会被误认成 4015/4019 风控（那两条才需要
    冷却或换号）。
    """
    if is_quota_error(data):
        return PlatformError(
            'Trae 免费额度已用尽（4008 quota exceeded），额度会随套餐重置恢复；'
            '这不是风控，账号本身可用。',
            status=429, code='quota_exceeded',
            retry_after=QUOTA_RETRY_AFTER,
            payload=payload if payload is not None else data)
    return PlatformError(_error_text(data), status=502, code='upstream_error',
                         payload=payload if payload is not None else data)
