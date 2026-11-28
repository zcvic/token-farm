"""Accio 协议的纯函数层。

本模块**不联网、不读磁盘、不改全局状态**：PKCE、URL 拼接、时间归一、
OpenAI 消息 → Accio（Gemini 风格）信封、工具定义序列化与错误分级都在这里，
便于离线单测。HTTP、账号存储与调度分别在 `client.py`、`store.py`、
`adapter.py`。

设计原则与其它平台一致：只做协议翻译，不猜上游意图。参考实现里「改写
system prompt 过风控」「静默 force-model」那类行为不在这里，也不在适配器里。

## 三处最容易回归的硬约束

1. `expiresAt` 上游是**秒**，必须 ×1000 归一化（`>1e12` 视为已是毫秒）；
   量纲写错会让每个请求都触发续期，账号会被上游标死；
2. `token` 必须在**请求体**里（放 `Authorization` 头恒回 HTTP 200 + 流内
   402）；
3. `system_instruction` 是**字符串**，不是 `{parts:[{text}]}` 对象；空串时
   整个键都不产出。
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import secrets
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

from . import constants as C

# ── 区域 ────────────────────────────────────────────────────
_REALM_ALIASES = {
    'cn': 'cn', 'china': 'cn', 'domestic': 'cn', '国内': 'cn', '国内版': 'cn',
    'global': 'global', 'intl': 'global', 'international': 'global',
    'us': 'global', 'com': 'global', '海外': 'global', '国际': 'global',
    '国际版': 'global',
}


def normalize_realm(value: object, default: str = C.DEFAULT_REALM) -> str:
    """任意写法 → 协议口径（`cn` / `global`）；认不出用 `default`。"""
    text = str(value or '').strip().lower()
    return (_REALM_ALIASES.get(text)
            or _REALM_ALIASES.get(str(default or '').strip().lower(),
                                  C.DEFAULT_REALM))


def realm_label(realm: object) -> str:
    return C.REALM_LABELS.get(normalize_realm(realm), '')


def gateway_url(path: str) -> str:
    return f'{C.GATEWAY}{path}'


def login_url(realm: object) -> str:
    return f'{C.LOGIN_BASE[normalize_realm(realm)]}{C.LOGIN_PATH}'


# ── 时间 ────────────────────────────────────────────────────
def expires_at_ms(raw: object) -> int | None:
    """上游 `expiresAt` → **毫秒**；读不到 / 非正数返回 None。

    ⚠️ 上游是**秒**（`1767225600`）。`> 1e12` 视为调用方已经给了毫秒
    （防御性：容忍上游某天改单位，也容忍手工导入的毫秒值）。
    """
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        number = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return int(round(number if number > 1e12 else number * 1000))


def expires_at_seconds(raw: object) -> float:
    ms = expires_at_ms(raw)
    return ms / 1000.0 if ms is not None else 0.0


# ── 请求 id ─────────────────────────────────────────────────
def request_id(now_ms: int | None = None) -> str:
    """一次请求的 id（也用于 `sg_k` 与 `message_id` 的派生）。"""
    stamp = int(time.time() * 1000) if now_ms is None else int(now_ms)
    return f'req-{stamp}-{secrets.token_hex(4)}'


def sg_k(request: object) -> str:
    """`sg_k` = md5(requestId) 的 hex。"""
    return hashlib.md5(str(request or '').encode('utf-8')).hexdigest()


def message_id(request: object) -> str:
    """`message_id` 必填；同一 requestId 连续两次不能相同。"""
    seed = f'{request}:{secrets.token_hex(16)}'
    return hashlib.md5(seed.encode('utf-8')).hexdigest()


# ── OAuth 2.0 + PKCE ────────────────────────────────────────
def pkce_pair() -> tuple[str, str]:
    """`(verifier, challenge)`；verifier 是 48 随机字节的 base64url。"""
    verifier = base64.urlsafe_b64encode(
        secrets.token_bytes(48)).rstrip(b'=').decode('ascii')
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode('ascii')).digest()
    ).rstrip(b'=').decode('ascii')
    return verifier, challenge


def new_state() -> str:
    return secrets.token_hex(32)


def new_ttid() -> str:
    return f'{C.TTID_PREFIX}{secrets.token_hex(16)}'


def build_auth_url(realm: object, redirect_uri: str, state: str,
                   code_challenge: str) -> str:
    """授权页地址；参数缺一不可（缺 `code_challenge` 会命中 legacy 门禁）。"""
    query = {
        'return_url': str(redirect_uri or ''),
        'state': str(state or ''),
        'code_challenge': str(code_challenge or ''),
        'code_challenge_method': 'S256',
        'client_id': C.CLIENT_ID,
        'ttid': new_ttid(),
        'language': C.LANGUAGE,
    }
    return (login_url(realm) + '?'
            + urllib.parse.urlencode(query, quote_via=urllib.parse.quote))


# ── camelCase → snake_case ──────────────────────────────────
_CAMEL = re.compile(r'[A-Z]')


def to_snake(value: object) -> object:
    """递归 camel → snake；`None` 的键整个丢掉（与 `undefined` 同语义）。

    ⚠️ **不下钻字符串**：工具 JSON Schema 以字符串形式挂在
    `parameters_json` 上，参数名是**数据**，不能被改名。
    """
    if isinstance(value, list):
        return [to_snake(item) for item in value]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if item is None:
                continue
            name = _CAMEL.sub(lambda m: f'_{m.group(0).lower()}', str(key))
            out[name] = to_snake(item)
        return out
    return value


# ── 消息序列化 ──────────────────────────────────────────────
class UnsupportedContent(ValueError):
    """当前的 Accio 通路不支持的内容（图片）。"""


def flatten_text(content: object) -> str:
    """OpenAI `content` → 纯文本；数组时只取 `type == 'text'` 的块。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ''
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        text = block.get('text')
        if isinstance(text, str):
            parts.append(text)
    return ''.join(parts)


def _has_image(content: object) -> bool:
    if not isinstance(content, list):
        return False
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = str(block.get('type') or '').strip().lower()
        if kind in ('image', 'image_url', 'input_image'):
            return True
        if 'image_url' in block or 'inline_data' in block:
            return True
    return False


def _tool_args_text(function: object) -> str:
    if not isinstance(function, dict):
        return ''
    raw = function.get('arguments')
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        try:
            return json.dumps(raw, ensure_ascii=False)
        except (TypeError, ValueError):
            return ''
    return ''


def _response_text(content: object) -> str:
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content if content is not None else {},
                          ensure_ascii=False)
    except (TypeError, ValueError):
        return ''


def serialize_messages(messages: object) -> tuple[list[dict], str]:
    """OpenAI messages → `(contents, system_instruction)`。

    - system / developer 拼进 `system_instruction`（字符串）；
    - `role='tool'` → `{role:'user', parts:[{functionResponse:{...}}]}`；
    - assistant 文本 + `tool_calls` → `{role:'model', parts:[...]}`；
    - user → `{role:'user', parts:[{text}]}`。

    图片**不静默丢弃**：本通路未实测图片，遇到图片块直接抛
    `UnsupportedContent`，由调用方转成 400。
    """
    contents: list[dict] = []
    system: list[str] = []

    for raw in messages if isinstance(messages, list) else []:
        if not isinstance(raw, dict):
            continue
        role = str(raw.get('role') or 'user').strip().lower()
        content = raw.get('content')

        if role in ('system', 'developer'):
            text = flatten_text(content)
            if text.strip():
                system.append(text)
            continue

        if role == 'tool':
            call_id = str(raw.get('tool_call_id') or '').strip()
            name = str(raw.get('name') or '').strip()
            contents.append({
                'role': 'user',
                'parts': [{
                    'functionResponse': {
                        'id': call_id,
                        'name': name,
                        'responseJson': _response_text(content),
                    },
                }],
            })
            continue

        if _has_image(content):
            raise UnsupportedContent(
                'Accio 本轮暂不支持图片输入（上游图片通路尚未实测）')

        if role == 'assistant':
            parts: list[dict] = []
            text = flatten_text(content)
            if text != '':
                parts.append({'text': text})
            calls = raw.get('tool_calls')
            for call in calls if isinstance(calls, list) else []:
                if not isinstance(call, dict):
                    continue
                fn = call.get('function')
                if not isinstance(fn, dict):
                    continue
                name = str(fn.get('name') or '').strip()
                if not name:
                    continue
                args = _tool_args_text(fn) or '{}'
                parts.append({
                    'functionCall': {
                        'id': str(call.get('id') or '').strip(),
                        'name': name,
                        # 历史里必须带参数串：上游靠它重建「调用过什么」。
                        'argsJson': args,
                    },
                })
            if parts:
                contents.append({'role': 'model', 'parts': parts})
            continue

        text = flatten_text(content)
        if text != '':
            contents.append({'role': 'user', 'parts': [{'text': text}]})

    return contents, '\n\n'.join(system)


# ── 工具定义 ────────────────────────────────────────────────
def to_tools(tools: object) -> list[dict]:
    """OpenAI `tools` → Accio 的**扁平**形态；schema 字符串化。"""
    out: list[dict] = []
    for tool in tools if isinstance(tools, list) else []:
        if not isinstance(tool, dict):
            continue
        fn = tool.get('function')
        if not isinstance(fn, dict):
            continue
        name = str(fn.get('name') or '').strip()
        if not name:
            continue
        parameters = fn.get('parameters')
        if not isinstance(parameters, dict):
            parameters = {'type': 'object', 'properties': {}}
        out.append({
            'name': name,
            'description': str(fn.get('description') or ''),
            'parametersJson': json.dumps(parameters, ensure_ascii=False),
        })
    return out


# ── 请求体 ──────────────────────────────────────────────────
def _number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def build_chat_body(*, model: str, token: str, messages: object,
                    tools: object = None, temperature: object = None,
                    max_output_tokens: object = None,
                    utdid: str, request: str | None = None) -> dict:
    """构造对话请求体（已 snake_case 化，`properties` 子树单独覆盖）。"""
    req = str(request or request_id())
    contents, system = serialize_messages(messages)
    tool_list = to_tools(tools)
    camel: dict[str, Any] = {
        'model': str(model or ''),
        'empid': '',
        'tenant': '',
        'iaiTag': '',
        'requestId': req,
        'userRequestId': req,
        'contents': contents,
        # 空串时要**整个键都不存在**，不能写 null / 空串。
        'systemInstruction': system if system != '' else None,
        'tools': tool_list if tool_list else None,
        'temperature': _number(temperature),
        'maxOutputTokens': _number(max_output_tokens),
        'stopSequences': [],
        'properties': {
            'utdid': str(utdid or ''),
            'version': C.APP_VERSION,
            'appKey': C.APP_KEY,
            'normalizedResponse': 'true',
        },
        # ⚠️ token 必须在 body 里；放 Authorization 头恒回流内 402。
        'token': str(token or ''),
        'messageId': message_id(req),
        'incremental': True,
    }
    body = to_snake(camel)
    assert isinstance(body, dict)
    # properties 子树**不参与**通用转换：`appKey` 保持驼峰，
    # `normalized_response` 保持 snake（两种键形混用是实测成功形状）。
    body['properties'] = {
        'utdid': str(utdid or ''),
        'version': C.APP_VERSION,
        'appKey': C.APP_KEY,
        'normalized_response': 'true',
    }
    return body


# ── 错误分级 ────────────────────────────────────────────────
#: 额度耗尽（HTTP 402 或流内 402）：换号 + 冷却至 UTC+8 当日 24:00。
KIND_QUOTA = 'quota'
#: 限流（HTTP 429）：换号 + 冷却 1 小时。
KIND_RATE = 'rate'
#: 会话失效（HTTP 401/403）：只换号，**绝不**写冷却。
KIND_AUTH = 'auth'
#: 权限 / 风控拒绝（403 + text/html）：不换号、不写冷却。
KIND_PERMISSION = 'permission'
#: 请求本身有问题（400）：原样回给调用方。
KIND_INVALID = 'invalid'
#: 上游 5xx / 空响应：不换号。
KIND_SERVER = 'server'
#: 其它无法分类的失败。
KIND_UNKNOWN = 'unknown'


@dataclass(frozen=True)
class Failure:
    """一次上游失败的分类结果；适配器据此决定换不换号、冻不冻号。"""

    kind: str
    status: int
    code: str
    message: str
    retry_after: int | None = None


def http_error_code(status: int) -> str:
    """HTTP 状态 → 稳定错误码（与参考实现 `httpErrorCode` 同口径）。"""
    if status in (401, 403):
        return 'AUTH'
    if status == 402:
        return 'QUOTA_EXCEEDED'
    if status == 429:
        return 'RATE_LIMIT'
    if status == 400:
        return 'INVALID_REQUEST'
    if status >= 500:
        return 'SERVER'
    return 'HTTP_ERROR'


def failure_class_of(code: object) -> str:
    """稳定错误码 → 分级（与参考实现 `accioFailureClassOf` 同口径）。"""
    text = str(code or '').strip().upper()
    if text in ('QUOTA_EXCEEDED', 'QUOTA', '402'):
        return KIND_QUOTA
    if text in ('RATE_LIMIT', 'RATE', '429'):
        return KIND_RATE
    if text in ('AUTH', 'UNAUTHORIZED', '401', '403'):
        return KIND_AUTH
    if text in ('PERMISSION_DENIED', 'DENIED'):
        return KIND_PERMISSION
    if text in ('INVALID_REQUEST', 'BAD_REQUEST', '400'):
        return KIND_INVALID
    if text in ('SERVER', 'UPSTREAM_ERROR', 'EMPTY_RESPONSE'):
        return KIND_SERVER
    return KIND_UNKNOWN


def should_rotate(kind: str) -> bool:
    """失败后要不要换下一个账号。"""
    return kind in (KIND_QUOTA, KIND_RATE, KIND_AUTH)


def records_cooldown(kind: str) -> bool:
    """失败后要不要给账号写冷却。"""
    return kind in (KIND_QUOTA, KIND_RATE)


def _retry_after(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0, int(float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def media_type_of(value: object) -> str:
    """`content-type` → 主类型（小写、去参数、去空白）。

    ⚠️ 三步缺一不可：`split(';')[0].strip().lower()`。风控页是
    `text/html;charset=UTF-8`，少任何一步都会漏判。
    """
    return str(value or '').split(';')[0].strip().lower()


def is_html_denial(media_type: object) -> bool:
    return media_type_of(media_type) in ('text/html', 'application/xhtml+xml')


def frame_error_of(frame: object) -> tuple[str, str]:
    """流内错误帧 → `(code, message)`；不是错误帧返回 `('', '')`。

    `error_code === '200'` 是**成功哨兵**（正常帧也会带），必须先于错误判据
    返回；否则每一条正常响应都会被当成错误。
    """
    if not isinstance(frame, dict):
        return '', ''
    raw_code = frame.get('error_code', frame.get('errorCode'))
    raw_message = frame.get('error_message', frame.get('errorMessage'))
    code = ''
    if isinstance(raw_code, (str, int, float)) and not isinstance(raw_code, bool):
        code = str(raw_code).strip()
    message = str(raw_message).strip() if isinstance(raw_message, str) else ''
    if code == '' and message == '':
        return '', ''
    if code == C.ERROR_CODE_OK:
        return '', ''
    return code, message


def message_of(payload: object, fallback: str = '') -> str:
    """从各种响应体里取一句可读文案。"""
    if isinstance(payload, dict):
        error = payload.get('error')
        if isinstance(error, dict):
            for key in ('message', 'msg', 'detail'):
                if error.get(key):
                    return str(error[key]).strip()
        if isinstance(error, str) and error.strip():
            return error.strip()
        for key in ('message', 'msg', 'error_message', 'errorMessage',
                    'detail'):
            if payload.get(key):
                return str(payload[key]).strip()
        data = payload.get('data')
        if isinstance(data, dict):
            for key in ('message', 'msg', 'detail'):
                if data.get(key):
                    return str(data[key]).strip()
    elif isinstance(payload, str) and payload.strip():
        return payload.strip()
    return fallback


def payload_code_of(payload: object) -> str:
    """取信封里的 `code`（字符串或数字都认）。"""
    if not isinstance(payload, dict):
        return ''
    raw = payload.get('code')
    if isinstance(raw, bool) or raw is None:
        return ''
    if isinstance(raw, (int, float)):
        return str(int(raw))
    return str(raw).strip()


_TERMINAL_REFRESH_MARKERS = (
    'invalid_grant',
    'invalid refresh token',
    'invalid refresh_token',
    'refresh token has been used',
    'refresh_token has been used',
    'refresh token expired',
    'refresh_token expired',
    'expired refresh token',
    'not_login',
    'not login',
)


def is_terminal_refresh_error(message: object) -> bool:
    """续期失败是否**终态**（refresh token 真的不能用了）。

    只有服务端明确说 `invalid_grant` / `NOT_LOGIN` 这类才算；网络抖动、5xx
    只算可重试，不得标死账号。
    """
    text = str(message or '').lower()
    return any(marker in text for marker in _TERMINAL_REFRESH_MARKERS)


def classify_failure(status: int, *, body: object = None,
                     message: object = '', media_type: object = '',
                     retry_after: object = None) -> Failure | None:
    """HTTP 状态 + 响应体 → `Failure`；不是错误返回 None。"""
    text = str(message or '').strip() or message_of(body)
    code = payload_code_of(body)

    # 风控页：HTTP 200（网关包一层）或 403 都可能是 HTML 拒绝页。它不是账号
    # 错误，换号也绕不过，所以「不换号、不写冷却」。
    if is_html_denial(media_type) and status in (0, 200, 403):
        return Failure(KIND_PERMISSION, 403, 'PERMISSION_DENIED',
                       text or '上游返回权限 / 风控拦截页')

    if status == 0:
        return Failure(KIND_SERVER, 502, 'NETWORK_ERROR',
                       text or '连接 Accio 失败')

    if status in (401, 403):
        if status == 403:
            return Failure(KIND_AUTH, 403, 'AUTH',
                           text or 'Accio 会话已失效')
        return Failure(KIND_AUTH, 401, 'AUTH',
                       text or 'Accio 登录已失效')

    if status == 402:
        return Failure(KIND_QUOTA, 402, 'QUOTA_EXCEEDED',
                       text or 'Accio 积分 / 额度已用尽')

    if status == 429:
        return Failure(KIND_RATE, 429, 'RATE_LIMIT',
                       text or 'Accio 请求过于频繁',
                       retry_after=_retry_after(retry_after))

    if status == 400 or code.upper() == 'INVALID_REQUEST':
        return Failure(KIND_INVALID, 400, 'INVALID_REQUEST',
                       text or 'Accio 请求参数不正确')

    if status >= 500:
        return Failure(KIND_SERVER, 502, 'SERVER',
                       text or f'Accio 上游 HTTP {status}')

    if status >= 400:
        # 其它 4xx 一律按请求错误处理，不换号。
        return Failure(KIND_INVALID, status, http_error_code(status),
                       text or f'Accio 上游 HTTP {status}')

    return None


def failure_for_stream(code: object, message: object = '') -> Failure:
    """流内错误帧 → `Failure`。"""
    text = str(code or '').strip()
    detail = str(message or '').strip()
    if text == C.QUOTA_ERROR_CODE:
        return Failure(KIND_QUOTA, 402, 'QUOTA_EXCEEDED',
                       detail or 'Accio 积分 / 额度已用尽')
    # 其它错误帧归服务器错误：不换号、不写冷却。
    return Failure(KIND_SERVER, 502, 'SERVER',
                   detail or f'Accio 流内错误帧（{text or "?"}）')
