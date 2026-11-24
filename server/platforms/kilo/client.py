"""Kilo 上游协议层（设备授权码 + Bearer token）。

边界与其它平台一致：本模块只做「协议翻译」，不含选号、冷却、会话粘性；
那些策略在 `adapter.py`。

三条链路，两套域名：

  · `api.kilo.ai`（账号侧）
      - `POST /api/device-auth/codes` —— 申请设备码；
      - `GET  /api/device-auth/codes/{code}` —— 轮询授权结果
        （202 等待、403 拒绝、410 过期、200 通过并带 `token`）；
      - `GET  /api/profile` —— 邮箱 / 昵称 / 组织；
      - `GET  /api/profile/balance` —— 余额（**美元**，`{balance: number}`）；
  · `ai-gateway.kilo.ai/api/v1`（模型侧）
      - `GET  /models` —— 模型目录（OpenRouter schema，免鉴权）；
      - `POST /chat/completions` —— OpenAI 兼容对话，流式原样透传。

代理口径与项目其余部分一致：`None` = 用全局代理，`''` = 强制直连
（`trust_env=False`，不读环境代理）。
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from ... import config
from ..base import PlatformError
from . import constants as C

__all__ = [
    'start_device', 'poll_device', 'fetch_profile', 'fetch_balance',
    'fetch_models', 'parse_models', 'chat', 'open_chat',
    'start_device_sync', 'poll_device_sync', 'fetch_profile_sync',
]


# ── 通用 ────────────────────────────────────────────────────
def _selected_proxy(proxy: str | None) -> str:
    if proxy is None:
        return config.effective_http_proxy()
    return str(proxy).strip()


def _headers(token: str = '', *, accept: str = 'application/json') -> dict[str, str]:
    """对话 / 目录的公共头。

    两个客户端标记是**必须**的：上游按它们识别「这是官方客户端」，缺了会被
    当第三方流量。`X-KILOCODE-EDITORNAME` 与 `User-Agent` 各自独立判断。
    """
    out = {
        'Accept': accept,
        'Content-Type': 'application/json',
        'User-Agent': C.UA,
        'X-KILOCODE-EDITORNAME': C.EDITOR_NAME,
    }
    if token:
        out['Authorization'] = f'Bearer {token}'
    return out


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

    401 / 403 = token 失效（凭证问题，冻了也不会自己好）；
    402 = 余额不足（免费档之外的模型要钱）；
    429 = 限流；
    5xx = 上游故障。
    """
    message = _error_text(payload, text[:200] or f'上游 HTTP {response.status_code}')
    status = response.status_code
    if status in (401, 403):
        return PlatformError(message, status=401, code='login_expired',
                             payload=payload)
    if status == 402:
        return PlatformError(message, status=402, code='insufficient_balance',
                             payload=payload)
    if status == 429:
        return PlatformError(message, status=429, code='rate_limited',
                             retry_after=_retry_after(response), payload=payload)
    if status >= 500:
        return PlatformError(message, status=502, code='upstream_error',
                             payload=payload)
    return PlatformError(message, status=status, code='bad_request',
                         payload=payload)


def _network_error(exc: Exception) -> PlatformError:
    return PlatformError(f'连接 Kilo 失败：{exc}', status=502,
                         code='network_error')


# ── 设备授权码 ──────────────────────────────────────────────
async def start_device(*, proxy: str | None = None,
                       timeout: float = C.DEVICE_TIMEOUT) -> dict:
    """申请设备码，返回 `{code, user_code, url, interval, expires_in}`。

    注意**用 `code` 字段轮询**，不是 `device_code`：实测拿 `device_code` 去轮询
    会直接 410。两者都在响应里，别拿错。
    """
    try:
        async with config.http_client(
                httpx.Timeout(timeout, connect=10),
                proxy=_selected_proxy(proxy), lane='kilo') as client:
            response = await client.post(
                C.API_BASE + C.DEVICE_PATH,
                headers={'Content-Type': 'application/json'})
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc
    payload = _json(response)
    if response.status_code >= 400:
        if response.status_code == 429:
            raise PlatformError('Kilo 待授权的登录请求过多，请稍后再试',
                                status=429, code='too_many_logins',
                                retry_after=_retry_after(response),
                                payload=payload)
        raise _response_error(response, response.text, payload)
    body = payload if isinstance(payload, dict) else {}
    code = str(body.get('code') or '').strip()
    user_code = str(body.get('user_code') or '').strip()
    url = str(body.get('verificationUrl') or body.get('verification_url')
              or '').strip()
    if not code or not url:
        raise PlatformError('Kilo 设备码响应不完整', status=502,
                            code='oauth_bad_response', payload=payload)
    try:
        expires = float(body.get('expiresIn') or C.DEVICE_TTL)
    except (TypeError, ValueError):
        expires = C.DEVICE_TTL
    return {
        'code': code,
        'user_code': user_code,
        'url': url,
        'interval': C.DEVICE_INTERVAL,
        'expires_in': int(max(30.0, min(3600.0, expires))),
    }


async def poll_device(code: str, *, proxy: str | None = None,
                      timeout: float = C.DEVICE_TIMEOUT) -> dict:
    """轮询一次设备码，返回 `{status, token, email}`。

    `status` ∈ `pending` / `done` / `denied` / `expired`。
    """
    key = str(code or '').strip()
    if not key:
        return {'status': 'expired'}
    try:
        async with config.http_client(
                httpx.Timeout(timeout, connect=10),
                proxy=_selected_proxy(proxy), lane='kilo') as client:
            response = await client.get(f'{C.API_BASE}{C.DEVICE_PATH}/{key}')
    except httpx.HTTPError:
        # 单次网络抖动不应结束登录会话：交给调用方按 pending 重试。
        return {'status': 'pending'}
    if response.status_code == 202:
        return {'status': 'pending'}
    if response.status_code == 403:
        return {'status': 'denied'}
    if response.status_code == 410:
        return {'status': 'expired'}
    if response.status_code >= 500:
        return {'status': 'pending'}
    if response.status_code >= 400:
        raise _response_error(response, response.text, _json(response))
    payload = _json(response)
    body = payload if isinstance(payload, dict) else {}
    status = str(body.get('status') or '').strip().lower()
    token = str(body.get('token') or '').strip()
    if status == 'approved' or token:
        if not token:
            raise PlatformError('Kilo 授权通过但没有下发 token', status=502,
                                code='oauth_bad_response', payload=payload)
        return {'status': 'done', 'token': token,
                'email': str(body.get('userEmail') or body.get('email') or '')}
    if status == 'denied':
        return {'status': 'denied'}
    if status == 'expired':
        return {'status': 'expired'}
    return {'status': 'pending'}


# ── 资料 / 余额 ─────────────────────────────────────────────
def parse_profile(payload: object) -> dict:
    """`/api/profile` → `{email, name, organizationId}`。

    上游把用户包在 `user` 里，也见过平铺的写法，两种都认。
    """
    body = payload if isinstance(payload, dict) else {}
    user = body.get('user') if isinstance(body.get('user'), dict) else {}
    orgs = body.get('organizations') if isinstance(body.get('organizations'),
                                                  list) else []
    selected = str(body.get('selectedOrganizationId') or '').strip()
    org_id = ''
    if selected and any(isinstance(o, dict) and str(o.get('id') or '') == selected
                        for o in orgs):
        org_id = selected
    elif orgs and body.get('hasPersonalAccount') is False:
        first = orgs[0] if isinstance(orgs[0], dict) else {}
        org_id = str(first.get('id') or '').strip()
    return {
        'email': str(user.get('email') or body.get('email') or ''),
        'name': str(user.get('name') or body.get('name') or ''),
        'organizationId': org_id,
    }


async def fetch_profile(token: str, *, proxy: str | None = None,
                        timeout: float = C.PROFILE_TIMEOUT) -> dict:
    try:
        async with config.http_client(
                httpx.Timeout(timeout, connect=10),
                proxy=_selected_proxy(proxy), lane='kilo') as client:
            response = await client.get(C.API_BASE + C.PROFILE_PATH,
                                        headers=_headers(token))
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc
    payload = _json(response)
    if response.status_code >= 400:
        raise _response_error(response, response.text, payload)
    return parse_profile(payload)


def parse_balance(payload: object) -> float | None:
    """`/api/profile/balance` → 美元余额；形状不对返回 None。"""
    if not isinstance(payload, dict):
        return None
    raw = payload.get('balance')
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if value != value or value in (float('inf'), float('-inf')):
        return None
    return value


async def fetch_balance(token: str, *, organization_id: str = '',
                        proxy: str | None = None,
                        timeout: float = C.PROFILE_TIMEOUT) -> float | None:
    headers = _headers(token)
    if organization_id:
        headers['x-kilocode-organizationid'] = organization_id
    try:
        async with config.http_client(
                httpx.Timeout(timeout, connect=10),
                proxy=_selected_proxy(proxy), lane='kilo') as client:
            response = await client.get(C.API_BASE + C.BALANCE_PATH,
                                        headers=headers)
    except httpx.HTTPError:
        return None
    if response.status_code >= 400:
        return None
    return parse_balance(_json(response))


# ── 模型目录 ────────────────────────────────────────────────
def parse_models(payload: object) -> list[dict]:
    """解析 OpenRouter schema 的模型目录。

    `pricing` 按 token 计价（字符串），这里只用来判断「是不是免费」；
    真正要花的钱以 `/api/profile/balance` 为准，不在这里换算。
    """
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
        seen.add(model_id.lower())
        top = raw.get('top_provider') if isinstance(raw.get('top_provider'),
                                                    dict) else {}
        architecture = raw.get('architecture') \
            if isinstance(raw.get('architecture'), dict) else {}
        modalities = architecture.get('input_modalities')
        if not isinstance(modalities, list) or not modalities:
            modalities = ['text']
        context = raw.get('context_length') or top.get('context_length') or 0
        max_out = top.get('max_completion_tokens') or 0
        pricing = raw.get('pricing') if isinstance(raw.get('pricing'), dict) else {}
        free = bool(raw.get('isFree'))
        if not free:
            prompt = _price(pricing.get('prompt'))
            completion = _price(pricing.get('completion'))
            free = prompt == 0 and completion == 0 and 'free' in model_id.lower()
        out.append({
            'id': model_id,
            'name': str(raw.get('name') or model_id),
            'context_length': _int(context),
            'max_tokens': _int(max_out),
            'modalities': [str(x) for x in modalities],
            'reasoning': 'reasoning' in (raw.get('supported_parameters') or []),
            # 上游用 `opencode.reasoning_options` 描述档位（旧的 `variants` 只是
            # legacy 兜底，本仓不认——它的语义是「变体名 → 参数覆盖」，与档位
            # 不是一回事）。认不出 / 缺失时留空，不编造。
            'efforts': _efforts(raw),
            'tool_call': 'tools' in (raw.get('supported_parameters') or []),
            'free': free,
            'type': 'chat',
        })
    return out


def _efforts(raw: dict) -> list[str]:
    from . import catalog  # noqa: PLC0415 —— 避免 client ↔ catalog 循环导入

    opencode = raw.get('opencode')
    if not isinstance(opencode, dict):
        return []
    return catalog.reasoning_efforts(opencode.get('reasoning_options'))


def _price(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if number != number or number in (float('inf'), float('-inf')):
        return None
    return number


def _int(value: object) -> int:
    try:
        return int(value or 0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


async def fetch_models(*, proxy: str | None = None,
                       timeout: float = C.MODELS_TIMEOUT) -> list[dict]:
    """模型目录**免鉴权**可读：没有账号时也能列模型。"""
    try:
        async with config.http_client(
                httpx.Timeout(timeout, connect=10),
                proxy=_selected_proxy(proxy), lane='kilo') as client:
            response = await client.get(C.GATEWAY_BASE + C.MODELS_PATH,
                                        headers=_headers())
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc
    payload = _json(response)
    if response.status_code >= 400:
        raise _response_error(response, response.text, payload)
    return parse_models(payload)


# ── 对话 ────────────────────────────────────────────────────
def chat_body(body: dict, model: str = '') -> dict:
    """整理发往上游的请求体：剥掉 Kilo 不认识的字段，补上默认模型。

    Kilo 网关是 OpenRouter 形状，认 `stream_options` / `metadata`；只剥
    OpenAI 专有的 `store` / `service_tier`，免得上游把整条请求判成 400。
    `model` 已由适配器的 `chat_body()` 钩子写入，这里只在为空时兜底。
    """
    payload = dict(body or {})
    if model:
        payload['model'] = str(model).strip() or C.DEFAULT_MODEL
    elif not str(payload.get('model') or '').strip():
        payload['model'] = C.DEFAULT_MODEL
    for key in ('store', 'service_tier'):
        payload.pop(key, None)
    return payload


async def chat(token: str, body: dict, *, proxy: str | None = None,
               timeout: float = C.CHAT_TIMEOUT) -> dict:
    """非流式对话，返回上游 JSON。"""
    async with config.http_client(
            httpx.Timeout(timeout, connect=10),
            proxy=_selected_proxy(proxy), lane='kilo') as client:
        response = await client.post(
            C.GATEWAY_BASE + C.CHAT_PATH, headers=_headers(token),
            content=json.dumps(body, ensure_ascii=False).encode('utf-8'))
    text = response.text
    if response.status_code >= 400:
        raise _response_error(response, text, _json(response))
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise PlatformError('Kilo 对话响应不是 JSON', status=502,
                            code='bad_upstream', payload=text[:300]) from exc
    if not isinstance(payload, dict):
        raise PlatformError('Kilo 对话响应形状异常', status=502,
                            code='bad_upstream', payload=payload)
    return payload


async def open_chat(token: str, body: dict, *, proxy: str | None = None,
                    idle_timeout: float = C.CHAT_IDLE_TIMEOUT,
                    connect_timeout: float = 10) -> httpx.Response:
    """流式对话：返回**未读取**的上游响应（调用方负责关闭）。

    上游正常情况下回 `text/event-stream`；凭证 / 余额出问题时可能回 HTTP 200
    + JSON。照原样透传会把一段 JSON 当成 SSE 发给客户端，所以非 SSE 的响应
    在这里一次性读完并按错误归类。
    """
    async with config.http_client(
            httpx.Timeout(idle_timeout, connect=connect_timeout),
            proxy=_selected_proxy(proxy), lane='kilo') as client:
        request = client.build_request(
            'POST', C.GATEWAY_BASE + C.CHAT_PATH,
            headers=_headers(token, accept='text/event-stream'),
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
            raise PlatformError(
                _error_text(payload, text[:200] or '对话接口未返回 SSE'),
                status=502, code='bad_upstream', payload=payload)
        return response


# ── 同步版（登录线程用）──────────────────────────────────────
# 登录线程不是事件循环，而 `config.http_client` 的共享池绑定在创建它的循环上。
# 这里用一次性的 `httpx.Client`，与 MiniMax / Loomy 的登录线程同一口径。
def _sync_client(proxy: str | None, timeout: float) -> httpx.Client:
    selected = _selected_proxy(proxy)
    kwargs: dict[str, Any] = {'timeout': timeout, 'trust_env': False}
    if selected:
        kwargs['proxy'] = selected
    return httpx.Client(**kwargs)


def start_device_sync(*, proxy: str | None = None,
                      timeout: float = C.DEVICE_TIMEOUT) -> dict:
    try:
        with _sync_client(proxy, timeout) as http:
            response = http.post(C.API_BASE + C.DEVICE_PATH,
                                 headers={'Content-Type': 'application/json'})
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc
    payload = _json(response)
    if response.status_code >= 400:
        raise _response_error(response, response.text, payload)
    body = payload if isinstance(payload, dict) else {}
    code = str(body.get('code') or '').strip()
    url = str(body.get('verificationUrl') or '').strip()
    if not code or not url:
        raise PlatformError('Kilo 设备码响应不完整', status=502,
                            code='oauth_bad_response', payload=payload)
    try:
        expires = float(body.get('expiresIn') or C.DEVICE_TTL)
    except (TypeError, ValueError):
        expires = C.DEVICE_TTL
    return {
        'code': code,
        'user_code': str(body.get('user_code') or '').strip(),
        'url': url,
        'interval': C.DEVICE_INTERVAL,
        'expires_in': int(max(30.0, min(3600.0, expires))),
    }


def poll_device_sync(code: str, *, proxy: str | None = None,
                     timeout: float = C.DEVICE_TIMEOUT) -> dict:
    key = str(code or '').strip()
    if not key:
        return {'status': 'expired'}
    try:
        with _sync_client(proxy, timeout) as http:
            response = http.get(f'{C.API_BASE}{C.DEVICE_PATH}/{key}')
    except httpx.HTTPError:
        return {'status': 'pending'}
    if response.status_code == 202:
        return {'status': 'pending'}
    if response.status_code == 403:
        return {'status': 'denied'}
    if response.status_code == 410:
        return {'status': 'expired'}
    if response.status_code >= 500:
        return {'status': 'pending'}
    if response.status_code >= 400:
        raise _response_error(response, response.text, _json(response))
    payload = _json(response)
    body = payload if isinstance(payload, dict) else {}
    status = str(body.get('status') or '').strip().lower()
    token = str(body.get('token') or '').strip()
    if status == 'approved' or token:
        if not token:
            raise PlatformError('Kilo 授权通过但没有下发 token', status=502,
                                code='oauth_bad_response', payload=payload)
        return {'status': 'done', 'token': token,
                'email': str(body.get('userEmail') or body.get('email') or '')}
    if status == 'denied':
        return {'status': 'denied'}
    if status == 'expired':
        return {'status': 'expired'}
    return {'status': 'pending'}


def fetch_profile_sync(token: str, *, proxy: str | None = None,
                       timeout: float = C.PROFILE_TIMEOUT) -> dict:
    try:
        with _sync_client(proxy, timeout) as http:
            response = http.get(C.API_BASE + C.PROFILE_PATH,
                                headers=_headers(token))
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc
    payload = _json(response)
    if response.status_code >= 400:
        raise _response_error(response, response.text, payload)
    return parse_profile(payload)
