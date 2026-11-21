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
from typing import Any

import httpx

from ... import config
from ..base import PlatformError
from . import constants as C

__all__ = [
    'refresh_token', 'parse_refresh', 'fetch_models', 'fetch_recommended',
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


def register_sync(workos: dict, *, proxy: str | None = None,
                  timeout: float = C.DEVICE_TIMEOUT) -> dict:
    """用 WorkOS token 在 Cline 换 refresh token。

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
    data = payload.get('data') if isinstance(payload, dict) \
        and isinstance(payload.get('data'), dict) else {}
    refresh = str(data.get('refreshToken') or data.get('refresh_token')
                  or '').strip()
    if not refresh:
        raise PlatformError('Cline 注册响应里没有 refreshToken', status=502,
                            code='oauth_bad_response', payload=payload)
    user = data.get('userInfo') if isinstance(data.get('userInfo'), dict) else {}
    return {'refresh_token': refresh,
            'email': str(user.get('email') or data.get('email') or '')}


def device_exchange_sync(workos: dict, *, proxy: str | None = None,
                         timeout: float = C.DEVICE_TIMEOUT) -> dict:
    return register_sync(workos, proxy=proxy, timeout=timeout)
