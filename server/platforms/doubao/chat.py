"""豆包 `/chat/completion` 的请求体构造与 SSE → OpenAI 翻译。

两个方向都只覆盖**文本对话**：

  · 请求：OpenAI `messages` → 豆包 `content_block[block_type=10000]`。豆包没有
    「多轮消息数组」这个概念，历史只能压成一段文本（参考实现的
    `flatten_history`）。工具 / 图片本轮不支持，`tools` 等字段不会下发。
  · 响应：豆包 SSE 事件 → OpenAI chunk / Chat Completion。正文取
    `CHUNK_DELTA.text`，没有它时退回 `STREAM_CHUNK` 里 `block_type=10000` 的
    `text_block.text`；`block_type=10040` 是思考过程（标题级）。

上游**不返回 usage**，参考实现按字符 ÷1.7 估算。这里沿用同一口径，并把这个
事实写进模块注释：统计页上的数字是估算值，不是上游账单。
"""
from __future__ import annotations

import json
import random
import socket
import time
import uuid
from typing import Any

from ..base import PlatformError
from . import constants as C

# 豆包客户端 block 类型
_BLOCK_TEXT = 10000
_BLOCK_THINKING = 10040


# ── 小工具 ──────────────────────────────────────────────────
def _uuid() -> str:
    return str(uuid.uuid4())


def _random_digits(n: int = 16) -> str:
    return ''.join(str(random.SystemRandom().randrange(10)) for _ in range(n))


def _text_of(content: object) -> str:
    """OpenAI message.content → 纯文本（兼容字符串与 part 数组）。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ''
    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict):
            text = part.get('text')
            if isinstance(text, str):
                parts.append(text)
    return '\n'.join(p for p in parts if p)


def normalize_effort(raw: object) -> str:
    """思考强度：只接受数字（上游要 `"4"` 这类值），其余用默认。

    OpenAI 的 `low/medium/high` 与豆包的档位没有公开映射，猜一个数字比忽略
    更难解释；所以只在客户端显式给了数字时透传。
    """
    text = str(raw or '').strip()
    return text if text.isdigit() else C.REASONING_EFFORT


def flatten_messages(messages: object) -> str:
    """把 OpenAI 消息数组压成一段文本，返回**最后一条 user 的内容**。

    历史消息按「用户：/ 助手：/ 系统：」前缀拼在最后一条 user 之前；上游
    没有多轮数组，这样做能保住上下文。最后一条 user 缺失或为空时抛 400：
    继续发一个空问题只会拿到上游的另一个错误。
    """
    items = [m for m in messages if isinstance(m, dict)] \
        if isinstance(messages, list) else []
    last_index = -1
    for index in range(len(items) - 1, -1, -1):
        if str(items[index].get('role') or '').strip().lower() == 'user':
            last_index = index
            break
    if last_index < 0:
        raise PlatformError('messages 缺少 user 消息', status=400,
                            code='empty_messages')
    last_text = _text_of(items[last_index].get('content')).strip()
    if not last_text:
        raise PlatformError('最后一条 user 消息为空', status=400,
                            code='empty_messages')

    lines: list[str] = []
    for item in items[:last_index]:
        text = _text_of(item.get('content')).strip()
        if not text:
            continue
        role = str(item.get('role') or '').strip().lower()
        label = {'system': '系统', 'developer': '系统',
                 'user': '用户', 'assistant': '助手',
                 'tool': '工具', 'function': '工具'}.get(role, role or '用户')
        lines.append(f'{label}：{text}')
    if not lines:
        return last_text
    return '\n'.join(lines) + '\n用户：' + last_text


def _agent_task_param(account: dict) -> dict:
    app_id = str(account.get('localAppId') or '').strip() \
        or C.DEFAULT_LOCAL_APP_ID
    device_id = str(account.get('deviceId') or '').strip()
    return {
        'runtime_type': 2,
        'sandbox_auth_type': 1,
        'device_name': str(account.get('deviceName') or '').strip()
        or (socket.gethostname() or 'token-farm'),
        'folder_name': '',
        'local_app_id': app_id,
        'local_device_id': device_id,
        'workspace': '',
    }


def build_body(messages: object, model_key: str, entry: dict, *,
               account: dict, effort: object = None) -> dict:
    """OpenAI 请求体 → 豆包 `/chat/completion` 请求体。

    与参考实现逐字段对齐（`internal/doubao/doubao.go:BuildBody`）。三个地方
    必须同时带同一个 `model_item_key`：`conversation_init_ext` /
    `model_config` / `aggregate_params`——只改其中一处，上游会按默认模型回答，
    或者直接报参数错误。
    """
    text = flatten_messages(messages)
    message_id = str(uuid.uuid4())
    local_conversation = 'local_' + _random_digits(16)
    now_ms = int(time.time() * 1000)
    effort_text = normalize_effort(effort)
    memory = str(entry.get('memory_profile') or C.MEMORY_PROFILE)
    window = str(entry.get('total_window_size') or C.TOTAL_WINDOW_SIZE)

    block = {
        'block_type': _BLOCK_TEXT,
        'content': {
            'text_block': {'text': text, 'icon_url': '',
                           'icon_url_dark': '', 'summary': ''},
            'pc_event_block': '',
        },
        'block_id': _uuid(),
        'parent_id': '',
        'meta_info': [],
        'append_fields': [],
    }

    body: dict[str, Any] = {
        'client_meta': {
            'local_conversation_id': local_conversation,
            'conversation_id': '',
            'bot_id': C.BOT_ID,
            'last_section_id': '',
            'last_message_index': None,
            'local_permissions': [
                {'permission_name': 'ACCESS_COARSE_LOCATION', 'status': 1},
                {'permission_name': 'ACCESS_FINE_LOCATION', 'status': 1},
                {'permission_name': 'ACCESS_BACKGROUND_LOCATION', 'status': 1},
            ],
        },
        'messages': [{
            'local_message_id': message_id,
            'content_block': [block],
            'message_status': 0,
        }],
    }

    task_param = _agent_task_param(account)
    init_ext = {
        'model_item_key': model_key,
        'reasoning_effort': effort_text,
        'mode_id': C.MODE_ID,
        'agent_task_param': json.dumps(task_param, ensure_ascii=False),
    }
    option: dict[str, Any] = {
        'create_time_ms': now_ms,
        'answer_with_suggest': False,
        'agent_mode': 1 if C.WORK_MODE_ENABLED else 0,
        'need_deep_think': 10001,
        'unique_key': _uuid(),
        'start_seq': 0,
        'need_create_conversation': True,
        'conversation_init_option': {'need_ack_conversation': True},
        'sse_recv_event_options': {'support_chunk_delta': True},
        'support_lazy_fetch_stream': True,
        'is_old_user': True,
        'message_storage_type': 0,
        'conversation_mode': 2 if C.WORK_MODE_ENABLED else 0,
        'conversation_init_ext': init_ext,
        'model_config': {
            'model_item_key': model_key,
            'model_extra_params': {
                'memory_profile': memory,
                'total_window_size': window,
            },
            'reasoning_effort': int(effort_text),
        },
        'aggregate_params': {
            'mention_skill_list': '[]',
            'mention_plugin_list': '[]',
            'mention_ext': '[{}]',
            'conversation_mode': '2' if C.WORK_MODE_ENABLED else '0',
            'mode_id': C.MODE_ID,
            'model_item_key': model_key,
            'agent_mode': '1' if C.WORK_MODE_ENABLED else '0',
            'reasoning_effort': effort_text,
            'provider_id': '',
        },
    }

    if C.WORK_MODE_ENABLED:
        client_option = {
            'enable_sandbox': C.ENABLE_SANDBOX,
            'os': 'Windows',
            'shared_folder_path': [],
            'agent_workspace': {
                'agent_workspace': '',
                'local_skill_paths': [],
            },
            'client_env_id': _uuid(),
            'sandbox_id': 'sandbox-' + _uuid(),
            'sandbox_auth_type': 1,
        }
        general_task = {
            'action': 0,
            'thread_local_message_id': [message_id],
            'client_option': client_option,
            'runtime_type': 2,
            'agent_task_param': task_param,
            'agent_task_param_change': {
                'runtime_changed': False,
                'device_changed': False,
                'sandbox_auth_type_changed': False,
            },
            'need_modify_conversation': False,
            'task_input_json': json.dumps({
                'agents_md': {'files': [], 'state': 2},
                'schema_version': 1,
                'home_dir': '',
                'project_context': {},
                'localConnectors': [],
            }, ensure_ascii=False),
        }
        option['general_task_param'] = general_task
        option['ext'] = {
            'general_task_param': json.dumps(general_task, ensure_ascii=False),
            'use_deep_think': '10001',
            'agent_mode': '1',
            'is_finish': '1',
            'conversation_init_option': '{"need_ack_conversation":true}',
            'commerce_credit_config_enable': '0',
        }

    body['option'] = option
    body['user_context'] = []
    if 'ext' not in option:
        body['ext'] = {'use_deep_think': '10001', 'is_finish': '1'}
    return body


# ── SSE → OpenAI ────────────────────────────────────────────
class StreamError(Exception):
    def __init__(self, message: str, *, code: str = 'upstream_error') -> None:
        super().__init__(message)
        self.message = str(message)
        self.code = str(code)


def _estimate_tokens(chars: int) -> int:
    if chars <= 0:
        return 0
    # 与参考实现一致：截断而不是四舍五入。
    return int(chars / C.CHARS_PER_TOKEN)


class StreamTranslator:
    """豆包 SSE 事件 → OpenAI chunk / Chat Completion。

    用法：
      · 流式：`start()` 先发 role 帧，之后每个事件交给 `feed()`，流结束调
        `finish()`；
      · 非流式：照常 `feed()`，最后用 `completion()` 取聚合结果。
    """

    def __init__(self, model: str, usage: dict | None = None,
                 prompt_chars: int = 0) -> None:
        self.model = model
        self.usage = usage if isinstance(usage, dict) else {}
        self.prompt_chars = max(0, int(prompt_chars or 0))
        self.id = f'chatcmpl-{uuid.uuid4().hex[:24]}'
        self.created = int(time.time())
        self._text: list[str] = []
        self._reasoning: list[str] = []
        self._brief = ''
        self._role_sent = False
        self._saw_chunk_delta = False
        self._last_snapshot = ''
        self._last_title = ''
        self.finish_reason: str | None = None

    # ── 状态 ────────────────────────────────────────────
    @property
    def text(self) -> str:
        return ''.join(self._text)

    @property
    def reasoning(self) -> str:
        return ''.join(self._reasoning)

    def _append_snapshot(self, value: str) -> str:
        """`STREAM_CHUNK` 可能是累计快照也可能是增量：两种形状都兼容。

        参考实现只在没有 `CHUNK_DELTA` 时使用 `text_block.text`，但没说明它是
        增量还是快照。这里用「上一份快照的公共前缀」判断：如果新文本以旧文本
        开头，就只发新增部分；否则当作增量原样发。
        """
        if not value:
            return ''
        if value == self._last_snapshot:
            return ''
        delta = value
        if self._last_snapshot and value.startswith(self._last_snapshot):
            delta = value[len(self._last_snapshot):]
        self._last_snapshot = value
        if not delta:
            return ''
        self._text.append(delta)
        return delta

    # ── chunk ───────────────────────────────────────────
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

    def text_chunk(self, text: str) -> dict:
        """额外补一段正文（只用于「上游一个字都没给」时的兜底提示）。"""
        return self._chunk({'content': text})

    def usage_dict(self) -> dict:
        prompt = _estimate_tokens(self.prompt_chars)
        completion = _estimate_tokens(len(self.text))
        data = {
            'prompt_tokens': prompt,
            'completion_tokens': completion,
            'total_tokens': prompt + completion,
        }
        self.usage.update(data)
        return data

    # ── 事件 ────────────────────────────────────────────
    def feed(self, event: str, payload: object) -> list[dict]:
        event = str(event or '').strip()
        if not isinstance(payload, dict):
            return []
        error = payload.get('error')
        if error:
            if isinstance(error, dict):
                message = str(error.get('message') or error.get('msg')
                              or error.get('code') or '上游返回错误')
                code = str(error.get('code') or error.get('type')
                           or 'upstream_error')
            else:
                message, code = str(error), 'upstream_error'
            raise StreamError(message, code=code)
        if event.upper() in ('SSE_ERROR', 'ERROR', 'SSE_FAIL', 'FAIL'):
            message = str(payload.get('message') or payload.get('msg')
                          or '上游返回错误')
            raise StreamError(message)

        if event == 'CHUNK_DELTA':
            text = payload.get('text')
            if isinstance(text, str) and text:
                self._saw_chunk_delta = True
                self._text.append(text)
                return [self._chunk({'content': text})]
            return []

        if event == 'STREAM_CHUNK':
            chunks: list[dict] = []
            for op in payload.get('patch_op') or []:
                if not isinstance(op, dict):
                    continue
                patch = op.get('patch_value')
                if not isinstance(patch, dict):
                    continue
                for block in patch.get('content_block') or []:
                    if not isinstance(block, dict):
                        continue
                    kind = block.get('block_type')
                    content = block.get('content')
                    content = content if isinstance(content, dict) else {}
                    if kind == _BLOCK_TEXT and not self._saw_chunk_delta:
                        text_block = content.get('text_block')
                        if isinstance(text_block, dict):
                            delta = self._append_snapshot(
                                str(text_block.get('text') or ''))
                            if delta:
                                chunks.append(self._chunk({'content': delta}))
                    elif kind == _BLOCK_THINKING:
                        thinking = content.get('thinking_block')
                        if isinstance(thinking, dict):
                            title = str(thinking.get('streaming_title')
                                        or thinking.get('finish_title') or '')
                            if title and title != self._last_title:
                                self._last_title = title
                                self._reasoning.append(title)
                                chunks.append(self._chunk(
                                    {'reasoning_content': title}))
            return chunks

        if event == 'SSE_REPLY_END':
            try:
                end_type = int(payload.get('end_type') or 0)
            except (TypeError, ValueError):
                end_type = 0
            attr = payload.get('msg_finish_attr')
            attr = attr if isinstance(attr, dict) else {}
            self._brief = str(attr.get('brief') or '')
            self.finish_reason = 'stop'
            # 只有没拿到正文时才用 brief 兜底：它是完整答案，但正常路径上
            # CHUNK_DELTA 已经发过了，再发一遍就会重复。
            if not self._text and self._brief:
                self._text.append(self._brief)
                return [self._chunk({'content': self._brief})]
            _ = end_type  # 保留解析，便于将来区分异常结束
            return []

        if event == 'SSE_ACK':
            # 会话 id 在这里；本轮不做上游会话复用（见 adapter 模块注释）。
            return []

        return []

    def finish(self) -> list[dict]:
        out: list[dict] = []
        if not self._role_sent:
            out.extend(self.start())
        out.append(self._chunk({}, self.finish_reason or 'stop'))
        out.append(self._chunk({}, usage=self.usage_dict()))
        return out

    def completion(self) -> dict:
        """非流式聚合结果（OpenAI Chat Completion）。"""
        message: dict[str, Any] = {
            'role': 'assistant',
            'content': self.text,
        }
        if self.reasoning:
            message['reasoning_content'] = self.reasoning
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
