"""Cline 上游协议层（refresh token + 伪装头）。

边界与其它平台一致：本模块只做「协议翻译」，不含选号、冷却、会话粘性；
那些策略在 `adapter.py`。

三条链路：

  · **鉴权**：`POST /auth/refresh`（`{refreshToken, grantType:"refresh_token"}`）
    → `{data:{accessToken, refreshToken, expiresAt}}`。**refresh token 轮换**，
    调用方必须先落盘再使用；
  · **目录**：`GET /models`、`GET /ai/cline/recommended-models`，都免鉴权；
  · **对话**：`POST /chat/completions`，OpenAI 兼容，但必须带 Cline 客户端
    伪装头，否则 403。

代理口径与项目其余部分一致：`None` = 用全局代理，`''` = 强制直连
（`trust_env=False`，不读环境代理）。
"""
from __future__ import annotations

import json
import time
import uuid
from hashlib import sha256
from typing import Any

import httpx

from ... import config
from ..base import PlatformError
from . import constants as C

__all__ = [
    'refresh_token', 'parse_refresh', 'refresh_via_workos', 'parse_register',
    'register', 'fetch_models', 'fetch_recommended',
    'parse_models', 'chat', 'open_chat', 'sse_to_completion',
    'start_device', 'poll_device', 'register_sync', 'device_exchange_sync',
]


# ── 通用 ────────────────────────────────────────────────────
def _selected_proxy(proxy: str | None) -> str:
    if proxy is None:
        return config.effective_http_proxy()
    return str(proxy).strip()


def chat_headers(access: str, *, session_id: str = '') -> dict[str, str]:
    """对话头：伪装成 Cline 客户端 + `Bearer workos:<access>`。

    `X-Task-ID` 用会话 id：上游拿它做任务归因，缺了不一定报错，但带上更接近
    官方客户端的流量形状。
    """
    task = str(session_id or '').strip() or f'sess_{uuid.uuid4().hex[:16]}'
    return {
        'Authorization': f'Bearer workos:{access}',
        'Content-Type': 'application/json',
        'Accept-Encoding': 'identity',
        'User-Agent': C.UA,
        'HTTP-Referer': C.REFERER,
        'X-Title': C.TITLE,
        'X-IS-MULTIROOT': 'false',
        'X-CLIENT-TYPE': C.CLIENT_TYPE,
        'X-CLIENT-VERSION': C.CLIENT_VERSION,
        'X-PLATFORM': C.PLATFORM,
        'X-PLATFORM-VERSION': C.PLATFORM_VERSION,
        'X-CORE-VERSION': C.CORE_VERSION,
        'X-Task-ID': task,
    }


def _catalog_headers() -> dict[str, str]:
    return {'Accept': 'application/json', 'User-Agent': C.UA}


def _json(response: httpx.Response) -> object:
    text = response.text
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _error_text(payload: object, fallback: str) -> str:
    if isinstance(payload, dict):
        error = payload.get('error')
        if isinstance(error, dict):
            for key in ('message', 'msg', 'detail'):
                if error.get(key):
                    return str(error[key])
        if isinstance(error, str) and error.strip():
            return error
        for key in ('message', 'msg', 'detail'):
            if payload.get(key):
                return str(payload[key])
    return fallback


def _retry_after(response: httpx.Response) -> int | None:
    raw = response.headers.get('retry-after')
    if not raw:
        return None
    try:
        return max(0, int(float(raw)))
    except (TypeError, ValueError):
        return None


def _response_error(response: httpx.Response, text: str,
                    payload: object = None) -> PlatformError:
    """把上游错误映射成带「该不该换号 / 冻多久」语义的状态码。

    401 = access token 失效（调用方换一份再试，不是凭证死）；
    402 / `insufficient_credits` = 余额不足（免费档之外要钱）；
    429 / 每日免费额度用尽 = 限流；
    403 `only available via Cline product surfaces` = 伪装头不对，按上游故障报。
    """
    message = _error_text(payload, text[:200] or f'上游 HTTP {response.status_code}')
    status = response.status_code
    code = ''
    if isinstance(payload, dict):
        raw = payload.get('error')
        if isinstance(raw, dict):
            code = str(raw.get('code') or raw.get('type') or '').strip()
        code = code or str(payload.get('code') or '').strip()
    if status == 401:
        return PlatformError(message, status=401, code='token_expired',
                             payload=payload)
    if status == 402 or code == 'insufficient_credits':
        return PlatformError(message, status=402, code='insufficient_credits',
                             payload=payload)
    if status == 429:
        return PlatformError(message, status=429, code='rate_limited',
                             retry_after=_retry_after(response), payload=payload)
    if status == 403:
        return PlatformError(message, status=403, code='forbidden',
                             payload=payload)
    if status >= 500:
        return PlatformError(message, status=502, code='upstream_error',
                             payload=payload)
    return PlatformError(message, status=status, code='bad_request',
                         payload=payload)


def _network_error(exc: Exception) -> PlatformError:
    return PlatformError(f'连接 Cline 失败：{exc}', status=502,
                         code='network_error')


# ── 鉴权 ────────────────────────────────────────────────────
def parse_refresh(payload: object) -> dict:
    """`/auth/refresh` → `{access_token, refresh_token, expires_at}`。

    上游把结果包在 `data` 里；`expiresAt` 可能是 epoch 毫秒或 ISO 串，
    这里统一成 epoch 秒（拿不到就是 0，调用方按 10 分钟兜底）。
    """
    body = payload if isinstance(payload, dict) else {}
    data = body.get('data') if isinstance(body.get('data'), dict) else body
    access = str(data.get('accessToken') or data.get('access_token') or '').strip()
    refresh = str(data.get('refreshToken') or data.get('refresh_token')
                  or '').strip()
    raw_expires = data.get('expiresAt') or data.get('expires_at') or 0
    expires = 0.0
    if isinstance(raw_expires, (int, float)) and not isinstance(raw_expires, bool):
        number = float(raw_expires)
        expires = number / 1000.0 if number > 1e11 else number
    elif isinstance(raw_expires, str) and raw_expires.strip():
        from .store import epoch_from_iso  # noqa: PLC0415 —— 复用同一套解析
        expires = epoch_from_iso(raw_expires)
    return {'access_token': access, 'refresh_token': refresh,
            'expires_at': expires}


async def refresh_token(refresh: str, *, proxy: str | None = None,
                        timeout: float = C.REFRESH_TIMEOUT) -> dict:
    key = str(refresh or '').strip()
    if not key:
        raise PlatformError('缺少 refresh token', status=401, code='login_expired')
    try:
        async with config.http_client(
                httpx.Timeout(timeout, connect=10),
                proxy=_selected_proxy(proxy), lane='cline') as client:
            response = await client.post(
                C.BASE_URL + C.REFRESH_PATH,
                headers={'Content-Type': 'application/json',
                         'User-Agent': C.UA},
                content=json.dumps({'refreshToken': key,
                                    'grantType': 'refresh_token'},
                                   ensure_ascii=False).encode('utf-8'))
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc
    payload = _json(response)
    if response.status_code >= 400:
        # `invalid_grant` = refresh token 已作废（被轮换过 / 撤销），必须重登。
        message = _error_text(payload, response.text[:200] or '刷新失败')
        if response.status_code in (400, 401, 403) and (
                'invalid_grant' in message.lower()
                or 'invalid_grant' in str(payload).lower()
                or response.status_code in (401, 403)):
            raise PlatformError(message, status=401, code='login_expired',
                                payload=payload)
        raise _response_error(response, response.text, payload)
    out = parse_refresh(payload)
    if not out['access_token']:
        raise PlatformError('Cline 刷新响应里没有 accessToken', status=502,
                            code='oauth_bad_response', payload=payload)
    return out


async def refresh_via_workos(refresh: str, *, proxy: str | None = None,
                             timeout: float = C.REFRESH_TIMEOUT) -> dict:
    """`/auth/refresh` 被拒时改走 WorkOS `authenticate` 通道自愈。

    Cline 的 refresh token 本质就是 WorkOS 的 refresh token，`/auth/refresh`
    只是换皮封装。上游那条封装调整（或 rotate 残留）时会回 400，但 WorkOS
    通道仍可用——用它换出新的一对 token，就不必让用户重新走设备授权。

    返回 `{access_token, refresh_token, expires_at}`（形状同 `parse_refresh`）。
    WorkOS 也回 `invalid_grant` 才是真的失效：这里原样抛 `PlatformError`，
    由调用方决定是否标 `expired`。
    """
    key = str(refresh or '').strip()
    if not key:
        raise PlatformError('缺少 refresh token', status=401, code='login_expired')
    try:
        async with config.http_client(
                httpx.Timeout(timeout, connect=10),
                proxy=_selected_proxy(proxy), lane='cline') as client:
            response = await client.post(
                C.WORKOS_AUTH_URL,
                headers={'Content-Type': 'application/json',
                         'Accept': 'application/json',
                         'User-Agent': C.UA},
                content=json.dumps({
                    'grant_type': 'refresh_token',
                    'client_id': C.WORKOS_CLIENT_ID,
                    'refresh_token': key,
                }, ensure_ascii=False).encode('utf-8'))
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc
    payload = _json(response)
    if response.status_code >= 400:
        message = _error_text(payload, response.text[:200] or 'WorkOS 刷新失败')
        # WorkOS 把「token 真作废」表达成 invalid_grant；其余 4xx/5xx 只是抖动。
        if response.status_code in (400, 401, 403) and (
                'invalid_grant' in message.lower()
                or 'invalid_grant' in str(payload).lower()):
            raise PlatformError(message, status=401, code='login_expired',
                                payload=payload)
        raise _response_error(response, response.text, payload)
    body = payload if isinstance(payload, dict) else {}
    access = str(body.get('access_token') or '').strip()
    if not access:
        raise PlatformError('WorkOS 刷新响应里没有 access_token', status=502,
                            code='oauth_bad_response', payload=payload)
    return {'access_token': access,
            'refresh_token': str(body.get('refresh_token') or '').strip(),
            'expires_at': 0.0}


# ── 目录 ────────────────────────────────────────────────────
def _model_entry(raw: dict, *, free: bool) -> dict:
    model_id = str(raw.get('id') or '').strip()
    top = raw.get('top_provider') if isinstance(raw.get('top_provider'), dict) \
        else {}
    context = raw.get('context_length') or top.get('context_length') or 0
    modalities = raw.get('modalities')
    if not isinstance(modalities, list) or not modalities:
        architecture = raw.get('architecture') \
            if isinstance(raw.get('architecture'), dict) else {}
        modalities = architecture.get('input_modalities')
    if not isinstance(modalities, list) or not modalities:
        modalities = ['text']
    return {
        'id': model_id,
        'name': str(raw.get('name') or model_id),
        'context_length': _int(context),
        'max_tokens': 0,
        'modalities': [str(x) for x in modalities],
        'reasoning': bool(raw.get('reasoning')),
        'tool_call': True,
        'free': free,
        'type': 'chat',
    }


def _int(value: object) -> int:
    try:
        return int(value or 0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def parse_models(payload: object, *, free: bool = False) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    data = payload.get('data')
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for raw in data:
        if not isinstance(raw, dict):
            continue
        model_id = str(raw.get('id') or '').strip()
        if not model_id or model_id.lower() in seen:
            continue
        if model_id.endswith(':batch'):
            continue
        seen.add(model_id.lower())
        out.append(_model_entry(raw, free=free))
    return out


async def fetch_models(*, proxy: str | None = None,
                       timeout: float = C.MODELS_TIMEOUT) -> list[dict]:
    try:
        async with config.http_client(
                httpx.Timeout(timeout, connect=10),
                proxy=_selected_proxy(proxy), lane='cline') as client:
            response = await client.get(C.BASE_URL + C.MODELS_PATH,
                                        headers=_catalog_headers())
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc
    payload = _json(response)
    if response.status_code >= 400:
        raise _response_error(response, response.text, payload)
    return parse_models(payload)


async def fetch_recommended(*, proxy: str | None = None,
                            timeout: float = C.MODELS_TIMEOUT) -> list[dict]:
    """`/ai/cline/recommended-models` 的 `free` 分组。

    只取 `free`：`clinePass` 要订阅、`clineCloud` 只允许 Cloud Agents
    （API 一律 403），放进来就是一堆点了必然失败的模型。
    """
    try:
        async with config.http_client(
                httpx.Timeout(timeout, connect=10),
                proxy=_selected_proxy(proxy), lane='cline') as client:
            response = await client.get(C.BASE_URL + C.RECOMMENDED_PATH,
                                        headers=_catalog_headers())
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc
    payload = _json(response)
    if response.status_code >= 400:
        raise _response_error(response, response.text, payload)
    if not isinstance(payload, dict):
        return []
    free = payload.get('free')
    return parse_models({'data': free if isinstance(free, list) else []},
                        free=True)


# ── 对话 ────────────────────────────────────────────────────
def chat_body(body: dict, model: str = '', *, session_id: str = '') -> dict:
    """整理发往上游的请求体。

    `session_id` 与 `reasoning_effort` 是 Cline 客户端自己的字段，官方客户端
    每条请求都带；`max_tokens` 对免费档是**毒药**（上游 500 `empty response
    content`），所以免费档前缀一律剥掉。

    `model` 为空时保留调用方写好的值（适配器的 `chat_body()` 钩子已经填过），
    只有整条缺失才兜底到默认模型。
    """
    payload = dict(body or {})
    if model:
        payload['model'] = str(model).strip() or C.DEFAULT_MODEL
    elif not str(payload.get('model') or '').strip():
        payload['model'] = C.DEFAULT_MODEL
    payload.setdefault('session_id',
                       str(session_id or '').strip()
                       or f'sess_{uuid.uuid4().hex[:16]}')
    payload.setdefault('reasoning_effort', 'high')
    if str(payload['model']).startswith('cline-free/'):
        payload.pop('max_tokens', None)
        payload.pop('max_completion_tokens', None)
    return payload


def needs_force_stream(model: object) -> bool:
    """这些前缀的上游只认流式：非流式会被回 500 / 空内容。"""
    text = str(model or '').strip()
    return any(text.startswith(prefix) for prefix in C.FORCE_STREAM_PREFIXES)


def limit_signal(text: object) -> bool:
    """上游把限流伪装成正常帧时的标记（`empty response content` 等）。"""
    body = str(text or '')
    if not body:
        return False
    lowered = body.lower()
    return any(marker.lower() in lowered for marker in C.SOFT_ERROR_MARKERS)


# ── 工具名截短（Meta 后端硬限 64）──────────────────────────
def shorten_tool_name(name: str) -> str:
    """确定性截短：前 55 字符 + `_` + sha256 前 8 位十六进制。

    确定性很关键：同一原始名在多轮对话里必须映射到同一个短名，否则历史
    `tool_calls` 里的名字会和本轮工具定义对不上，上游照样 400。
    """
    digest = sha256(name.encode('utf-8')).hexdigest()[:8]
    return f'{name[:55]}_{digest}'


def clamp_tool_names(body: dict) -> dict[str, str]:
    """把请求体里超过 64 字符的工具名就地截短，返回 `短名 → 原名` 映射。

    覆盖四处：`tools[].function.name`、历史 `messages[].tool_calls[].function.name`、
    `tool_choice.function.name`，以及 Responses 协议转出来的
    `messages[].function_call.name`。没有超长名时返回空表（调用方据此跳过回程
    还原，零开销）。
    """
    mapping: dict[str, str] = {}

    def fix(name: object) -> str | None:
        text = name if isinstance(name, str) else ''
        if not text or len(text) <= C.TOOL_NAME_MAX:
            return None
        short = shorten_tool_name(text)
        mapping[short] = text
        return short

    tools = body.get('tools')
    if isinstance(tools, list):
        copied: list[object] = []
        for tool in tools:
            if not isinstance(tool, dict):
                copied.append(tool)
                continue
            fn = tool.get('function')
            if isinstance(fn, dict):
                fn = dict(fn)
                tool = {**tool, 'function': fn}
            else:
                tool = dict(tool)
                fn = tool
            short = fix(fn.get('name'))
            if short is not None:
                fn['name'] = short
            copied.append(tool)
        body['tools'] = copied

    messages = body.get('messages')
    if isinstance(messages, list):
        copied_msgs: list[object] = []
        for message in messages:
            if not isinstance(message, dict) or not message.get('tool_calls'):
                copied_msgs.append(message)
                continue
            message = dict(message)
            calls = []
            for call in message.get('tool_calls') or []:
                if not isinstance(call, dict):
                    calls.append(call)
                    continue
                call = dict(call)
                for key in ('function', 'function_call'):
                    holder = call.get(key)
                    if isinstance(holder, dict):
                        holder = dict(holder)
                        short = fix(holder.get('name'))
                        if short is not None:
                            holder['name'] = short
                        call[key] = holder
                calls.append(call)
            message['tool_calls'] = calls
            copied_msgs.append(message)
        body['messages'] = copied_msgs

    choice = body.get('tool_choice')
    if isinstance(choice, dict):
        fn = choice.get('function')
        if isinstance(fn, dict):
            short = fix(fn.get('name'))
            if short is not None:
                body['tool_choice'] = {**choice, 'function': {**fn, 'name': short}}

    return mapping


def restore_tool_names(obj: object, mapping: dict[str, str]) -> object:
    """把上游响应里被截短的工具名还原成客户端原始名（就地改写）。

    `mapping` 为空时是 no-op。流式 delta 与非流式 message 两种形状都覆盖。
    """
    if not isinstance(obj, dict) or not mapping:
        return obj
    choices = obj.get('choices')
    if not isinstance(choices, list):
        return obj
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        for key in ('delta', 'message'):
            holder = choice.get(key)
            if not isinstance(holder, dict):
                continue
            calls = holder.get('tool_calls')
            if not isinstance(calls, list):
                continue
            for call in calls:
                if not isinstance(call, dict):
                    continue
                fn = call.get('function')
                if not isinstance(fn, dict):
                    continue
                name = fn.get('name')
                if isinstance(name, str) and name in mapping:
                    fn['name'] = mapping[name]
    return obj


class SseToolNameRewriter:
    """流式透传时还原被截短的工具名。

    只在**整行命中映射**时重写该帧；其余字节逐字节原样输出，所以没有超长
    工具名的请求（绝大多数）路径上开销只有一次 `in` 判断。
    """

    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self.mapping = dict(mapping or {})
        self._pending = b''
        self._needles = [name.encode('utf-8') for name in self.mapping]

    def feed(self, chunk: bytes) -> bytes:
        if not self._needles or not chunk:
            return chunk
        data = self._pending + chunk
        out = bytearray()
        while True:
            index = data.find(b'\n')
            if index < 0:
                break
            out += self._line(data[:index + 1])
            data = data[index + 1:]
        self._pending = data
        return bytes(out)

    def flush(self) -> bytes:
        """把最后一段没有换行结尾的残留（极少见）也处理掉。"""
        if not self._needles or not self._pending:
            return b''
        rest, self._pending = self._pending, b''
        return self._line(rest)

    def _line(self, line: bytes) -> bytes:
        stripped = line.strip()
        if not stripped.startswith(b'data:'):
            return line
        payload = stripped[5:].strip()
        if not payload or payload == b'[DONE]':
            return line
        if not any(needle in payload for needle in self._needles):
            return line
        try:
            obj = json.loads(payload.decode('utf-8', 'replace'))
        except ValueError:
            return line
        if not isinstance(obj, dict):
            return line
        restore_tool_names(obj, self.mapping)
        newline = b'\r\n' if line.endswith(b'\r\n') else b'\n'
        return b'data: ' + json.dumps(
            obj, ensure_ascii=False).encode('utf-8') + newline


async def chat(access: str, body: dict, *, proxy: str | None = None,
               timeout: float = C.CHAT_TIMEOUT,
               session_id: str = '') -> dict:
    """非流式对话，返回上游 JSON。"""
    async with config.http_client(
            httpx.Timeout(timeout, connect=10),
            proxy=_selected_proxy(proxy), lane='cline') as client:
        response = await client.post(
            C.BASE_URL + C.CHAT_PATH,
            headers=chat_headers(access, session_id=session_id),
            content=json.dumps(body, ensure_ascii=False).encode('utf-8'))
    text = response.text
    if response.status_code >= 400:
        raise _response_error(response, text, _json(response))
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise PlatformError('Cline 对话响应不是 JSON', status=502,
                            code='bad_upstream', payload=text[:300]) from exc
    if not isinstance(payload, dict):
        raise PlatformError('Cline 对话响应形状异常', status=502,
                            code='bad_upstream', payload=payload)
    return payload


async def open_chat(access: str, body: dict, *, proxy: str | None = None,
                    idle_timeout: float = C.CHAT_IDLE_TIMEOUT,
                    connect_timeout: float = 10,
                    session_id: str = '') -> httpx.Response:
    """流式对话：返回**未读取**的上游响应（调用方负责关闭）。

    上游正常情况下回 `text/event-stream`；凭证 / 余额出问题时可能回 HTTP 200
    + JSON。照原样透传会把一段 JSON 当成 SSE 发给客户端，所以非 SSE 的响应
    在这里一次性读完并按错误归类。
    """
    async with config.http_client(
            httpx.Timeout(idle_timeout, connect=connect_timeout),
            proxy=_selected_proxy(proxy), lane='cline') as client:
        request = client.build_request(
            'POST', C.BASE_URL + C.CHAT_PATH,
            headers=chat_headers(access, session_id=session_id),
            content=json.dumps(body, ensure_ascii=False).encode('utf-8'))
        response = await client.send(request, stream=True)
        if response.status_code >= 400:
            text = (await response.aread()).decode('utf-8', errors='replace')
            await response.aclose()
            payload: Any = None
            try:
                payload = json.loads(text) if text else None
            except ValueError:
                payload = None
            raise _response_error(response, text, payload)
        ctype = str(response.headers.get('content-type') or '').lower()
        if 'text/event-stream' not in ctype:
            text = (await response.aread()).decode('utf-8', errors='replace')
            await response.aclose()
            payload = None
            try:
                payload = json.loads(text) if text else None
            except ValueError:
                payload = None
            # 有些上游用 JSON 体表达「限流 / 空响应」，仍按错误上报。
            raise PlatformError(
                _error_text(payload, text[:200] or '对话接口未返回 SSE'),
                status=502, code='bad_upstream', payload=payload)
        return response


# ── SSE → 非流式（免费档强制流式后的聚合）───────────────────
def sse_to_completion(chunks: list[object], model: str) -> dict:
    """把一串 SSE 事件聚合成 `chat.completion`。

    上游偶尔回「200 + 只有 reasoning、没有 content」：这里把 reasoning 拼进
    content 兜底，避免客户端拿到一个空回复。同时识别上游伪装成正常帧的限流
    信号（`empty response content` / `Daily free limit` / `INFERENCE_CAP_ERROR`），
    有就抛 `PlatformError`，让适配器换号而不是把空回复交给用户。
    """
    content: list[str] = []
    reasoning: list[str] = []
    tool_calls: list[dict] = []
    finish_reason = ''
    usage: dict = {}
    ident = ''
    upstream_model = ''
    soft_error = ''
    for raw in chunks:
        if not isinstance(raw, dict):
            continue
        if raw.get('error'):
            message = _error_text(raw.get('error'), '')
            if message:
                soft_error = soft_error or message
        if raw.get('id'):
            ident = str(raw['id'])
        if raw.get('model'):
            upstream_model = str(raw['model'])
        if isinstance(raw.get('usage'), dict):
            usage.update(raw['usage'])
        for choice in raw.get('choices') or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get('delta')
            if not isinstance(delta, dict):
                delta = choice.get('message') if isinstance(
                    choice.get('message'), dict) else {}
            text = delta.get('content')
            if isinstance(text, str):
                content.append(text)
            think = delta.get('reasoning') or delta.get('reasoning_content')
            if isinstance(think, str):
                reasoning.append(think)
            for call in delta.get('tool_calls') or []:
                if isinstance(call, dict):
                    tool_calls.append(call)
            if choice.get('finish_reason'):
                finish_reason = str(choice['finish_reason'])
    body = ''.join(content)
    think = ''.join(reasoning)
    lowered = body.strip().lower()
    if lowered and any(marker.lower() in lowered
                       for marker in C.SOFT_ERROR_MARKERS):
        soft_error = soft_error or body.strip()[:200]
    if soft_error:
        raise PlatformError(soft_error, status=429, code='upstream_limited')
    message: dict[str, Any] = {'role': 'assistant', 'content': body}
    if think and not body:
        # 只有思考没有正文：当正文交出去，比空回复有用。
        message['content'] = think
        message['reasoning'] = think
    elif think:
        message['reasoning'] = think
    if tool_calls:
        message['tool_calls'] = tool_calls
    return {
        'id': ident or f'chatcmpl-{uuid.uuid4().hex[:24]}',
        'object': 'chat.completion',
        'created': int(time.time()),
        'model': model or upstream_model or C.DEFAULT_MODEL,
        'choices': [{
            'index': 0,
            'message': message,
            'finish_reason': finish_reason or 'stop',
        }],
        'usage': usage or {'prompt_tokens': 0, 'completion_tokens': 0,
                           'total_tokens': 0},
    }


# ── 设备授权码（登录线程用，同步）────────────────────────────
# 登录线程不是事件循环，而 `config.http_client` 的共享池绑定在创建它的循环上。
# 这里用一次性的 `httpx.Client`，与 MiniMax / Kilo 的登录线程同一口径。
def _sync_client(proxy: str | None, timeout: float) -> httpx.Client:
    selected = _selected_proxy(proxy)
    kwargs: dict[str, Any] = {'timeout': timeout, 'trust_env': False}
    if selected:
        kwargs['proxy'] = selected
    return httpx.Client(**kwargs)


def _post_form_sync(url: str, fields: dict[str, str], *, proxy: str | None,
                    timeout: float) -> tuple[httpx.Response, object]:
    try:
        with _sync_client(proxy, timeout) as http:
            response = http.post(url, data=fields,
                                 headers={'Accept': 'application/json'})
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc
    return response, _json(response)


def start_device(*, proxy: str | None = None,
                 timeout: float = C.DEVICE_TIMEOUT) -> dict:
    """WorkOS 设备授权：返回 `{device_code, user_code, url, interval, expires_in}`。"""
    response, payload = _post_form_sync(
        C.WORKOS_DEVICE_URL, {'client_id': C.WORKOS_CLIENT_ID},
        proxy=proxy, timeout=timeout)
    if response.status_code >= 400:
        raise _response_error(response, response.text, payload)
    body = payload if isinstance(payload, dict) else {}
    device_code = str(body.get('device_code') or '').strip()
    user_code = str(body.get('user_code') or '').strip()
    url = str(body.get('verification_uri_complete')
              or body.get('verification_uri') or '').strip()
    if not device_code or not url:
        raise PlatformError('Cline 设备码响应不完整', status=502,
                            code='oauth_bad_response', payload=payload)
    try:
        interval = float(body.get('interval') or C.DEVICE_INTERVAL)
    except (TypeError, ValueError):
        interval = C.DEVICE_INTERVAL
    try:
        expires = float(body.get('expires_in') or C.DEVICE_TTL)
    except (TypeError, ValueError):
        expires = C.DEVICE_TTL
    return {
        'device_code': device_code,
        'user_code': user_code,
        'url': url,
        'interval': max(1.0, min(30.0, interval)),
        'expires_in': int(max(30.0, min(3600.0, expires))),
    }


def poll_device(device_code: str, *, proxy: str | None = None,
                timeout: float = C.DEVICE_TIMEOUT) -> dict:
    """轮询一次 WorkOS 授权，返回 `{status, tokens}`。

    `status` ∈ `pending` / `slow_down` / `done` / `denied` / `expired`。
    """
    code = str(device_code or '').strip()
    if not code:
        return {'status': 'expired'}
    response, payload = _post_form_sync(C.WORKOS_AUTH_URL, {
        'grant_type': 'urn:ietf:params:oauth:grant-type:device_code',
        'device_code': code,
        'client_id': C.WORKOS_CLIENT_ID,
    }, proxy=proxy, timeout=timeout)
    body = payload if isinstance(payload, dict) else {}
    error = str(body.get('error') or '').strip().lower()
    if response.status_code < 400 and body.get('access_token'):
        return {'status': 'done', 'tokens': body}
    if error in ('authorization_pending', 'pending'):
        return {'status': 'pending'}
    if error == 'slow_down':
        return {'status': 'slow_down'}
    if error in ('access_denied', 'denied'):
        return {'status': 'denied'}
    if error in ('expired_token', 'expired', 'invalid_grant',
                 'invalid_request', 'invalid_client'):
        return {'status': 'expired'}
    if response.status_code >= 500:
        return {'status': 'pending'}
    raise _response_error(response, response.text, payload)


def parse_register(payload: object) -> dict:
    """`/auth/register` → `{access_token, refresh_token, expires_at, email}`。

    与 `parse_refresh` 同一个 `data` 外壳；`register` 正常会回 accessToken，
    但没有也不致命（调用方可以用 `workos:` 前缀令牌顶上）。
    """
    out = parse_refresh(payload)
    data = payload.get('data') if isinstance(payload, dict) \
        and isinstance(payload.get('data'), dict) else (
            payload if isinstance(payload, dict) else {})
    user = data.get('userInfo') if isinstance(data.get('userInfo'), dict) else {}
    out['email'] = str(user.get('email') or data.get('email') or '')
    return out


async def register(workos: dict, *, proxy: str | None = None,
                   timeout: float = C.DEVICE_TIMEOUT) -> dict:
    """用 WorkOS token 在 Cline 换正式令牌（异步版，供适配器自愈使用）。"""
    body = {
        'accessToken': str(workos.get('access_token') or ''),
        'refreshToken': str(workos.get('refresh_token') or ''),
    }
    try:
        async with config.http_client(
                httpx.Timeout(timeout, connect=10),
                proxy=_selected_proxy(proxy), lane='cline') as client:
            response = await client.post(
                C.BASE_URL + C.REGISTER_PATH,
                headers={'Content-Type': 'application/json',
                         'Accept': 'application/json',
                         'User-Agent': C.UA},
                content=json.dumps(body, ensure_ascii=False).encode('utf-8'))
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc
    payload = _json(response)
    if response.status_code >= 400:
        raise _response_error(response, response.text, payload)
    out = parse_register(payload)
    if not out['refresh_token']:
        raise PlatformError('Cline 注册响应里没有 refreshToken', status=502,
                            code='oauth_bad_response', payload=payload)
    return out


def register_sync(workos: dict, *, proxy: str | None = None,
                  timeout: float = C.DEVICE_TIMEOUT) -> dict:
    """用 WorkOS token 在 Cline 换 refresh token（设备码登录的同步路径）。

    返回 `{refresh_token, email}`；`refreshToken` 缺失即视为失败——后面所有
    对话都靠它，拿不到就没有可用凭证。
    """
    body = {
        'accessToken': str(workos.get('access_token') or ''),
        'refreshToken': str(workos.get('refresh_token') or ''),
    }
    try:
        with _sync_client(proxy, timeout) as http:
            response = http.post(C.BASE_URL + C.REGISTER_PATH, json=body,
                                 headers={'Content-Type': 'application/json',
                                          'User-Agent': C.UA})
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc
    payload = _json(response)
    if response.status_code >= 400:
        raise _response_error(response, response.text, payload)
    out = parse_register(payload)
    if not out['refresh_token']:
        raise PlatformError('Cline 注册响应里没有 refreshToken', status=502,
                            code='oauth_bad_response', payload=payload)
    return {'refresh_token': out['refresh_token'], 'email': out['email']}


def device_exchange_sync(workos: dict, *, proxy: str | None = None,
                         timeout: float = C.DEVICE_TIMEOUT) -> dict:
    return register_sync(workos, proxy=proxy, timeout=timeout)
