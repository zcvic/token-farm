"""Accio 上游协议层（OAuth / 目录 / 对话 / 身份 / 积分）。

边界与其它平台一致：本模块只做「协议翻译」，不含选号、冷却、会话粘性；
那些策略在 `adapter.py`。

## 三件必须记住的事

1. **token 在请求体**：对话端点的 `token` 必须写进 body；放进
   `Authorization` 头会恒回 HTTP 200 + 流内 `error_code=402`。
2. **`expiresAt` 是秒**：兑换 / 续期响应里是秒，本模块统一转成毫秒
   （`protocol.expires_at_ms`），再交给适配器落盘成 ISO。
3. **`success !== true` 即失败**：OAuth 信封是 `{success, code, message, data}`；
   只看 HTTP 200 会把「业务失败」当成成功，然后得到「缺少字段」这种指不到
   上游的报错。

## 三类端点传参方式不同（实测，不要「统一好看」）

| 端点 | 方法 | token 位置 |
|---|---|---|
| `/api/oauth/token` | POST | body（`code` + PKCE） |
| `/api/auth/refresh_token` | POST | body（`accessToken` + `refreshToken`） |
| `/api/auth/userinfo` | POST | **body**（`{accessToken}`） |
| `/api/llm/config/v2` | POST | body（`{token}`） |
| `/api/entitlement/*` | GET | **query**（`?accessToken=…`） |

`userinfo` 用 GET 会 405；额度端点缺 query token 会 400。
"""
from __future__ import annotations

import json
import urllib.parse
from typing import Any, AsyncIterator

import httpx

from ... import config
from ..base import PlatformError
from . import catalog, constants as C, protocol as P, store

__all__ = [
    'exchange_code', 'refresh', 'user_info', 'fetch_catalog',
    'open_chat', 'iter_frames', 'subscription', 'quota',
    'failure_of', 'headers',
]


# ── 通用 ────────────────────────────────────────────────────
def _selected_proxy(proxy: str | None) -> str:
    if proxy is None:
        return config.effective_http_proxy()
    return str(proxy).strip()


def _timeout(timeout: float) -> httpx.Timeout:
    return httpx.Timeout(timeout, connect=min(10.0, max(3.0, timeout)))


def _json(response: httpx.Response) -> object:
    text = response.text
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _record(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _with_failure(exc: PlatformError, failure: P.Failure) -> PlatformError:
    exc.failure = failure          # type: ignore[attr-defined]
    return exc


def failure_of(exc: PlatformError) -> P.Failure | None:
    failure = getattr(exc, 'failure', None)
    return failure if isinstance(failure, P.Failure) else None


def _media_type(response: httpx.Response) -> str:
    return P.media_type_of(response.headers.get('content-type'))


def _response_failure(response: httpx.Response, payload: object,
                      what: str) -> PlatformError:
    retry_after = response.headers.get('retry-after')
    failure = P.classify_failure(
        response.status_code, body=payload,
        media_type=_media_type(response), retry_after=retry_after)
    if failure is None:
        failure = P.Failure(P.KIND_SERVER, 502, 'SERVER',
                            f'{what}：上游返回 HTTP {response.status_code}')
    exc = PlatformError(f'{what}：{failure.message}', status=failure.status,
                        code=failure.code, retry_after=failure.retry_after,
                        payload=payload)
    return _with_failure(exc, failure)


def _network_error(exc: Exception) -> PlatformError:
    return PlatformError(f'连接 Accio 失败：{exc}', status=502,
                         code='network_error')


async def _read_error(response: httpx.Response) -> tuple[str, object]:
    try:
        text = (await response.aread()).decode('utf-8', errors='replace')
    finally:
        try:
            await response.aclose()
        except Exception:  # noqa: BLE001
            pass
    payload: object = None
    if text:
        try:
            payload = json.loads(text)
        except ValueError:
            payload = None
    return text, payload


# ── 头 ──────────────────────────────────────────────────────
def headers(account: dict, *, accept: str = 'application/json') -> dict[str, str]:
    """请求头。

    `utdid` / `version` / `appKey` 是客户端指纹；`sgcookie` / `x-team-id` /
    `x-accio-route-region` 有值才发（空串一律不发）；`x-package-region` 两区都发。
    """
    realm = P.normalize_realm(account.get('realm'))
    out: dict[str, str] = {
        'Content-Type': 'application/json',
        'Accept': accept,
        'utdid': store.utdid_for(account),
        'version': C.APP_VERSION,
        'appKey': C.APP_KEY,
        'x-package-region': C.PACKAGE_REGION.get(realm, 'CN'),
    }
    sgcookie = str(account.get('sgcookie') or '').strip()
    if sgcookie:
        out['sgcookie'] = sgcookie
    team_id = str(account.get('teamId') or '').strip()
    if team_id:
        out['x-team-id'] = team_id
    route = str(account.get('routeRegion') or '').strip()
    if route:
        out['x-accio-route-region'] = route
    return out


def _credit_headers(account: dict) -> dict[str, str]:
    """只读额度端点的头：实测不需要 `utdid` / `version` / `appKey`。

    `x-team-id` / `x-accio-route-region` / `x-package-region` 仍要发——它们是
    「当前空间 / 区域」的声明，缺了会拿到个人空间的额度。
    """
    out: dict[str, str] = {'Accept': 'application/json'}
    realm = P.normalize_realm(account.get('realm'))
    out['x-package-region'] = C.PACKAGE_REGION.get(realm, 'CN')
    team_id = str(account.get('teamId') or '').strip()
    if team_id:
        out['x-team-id'] = team_id
    route = str(account.get('routeRegion') or '').strip()
    if route:
        out['x-accio-route-region'] = route
    return out


async def _request(method: str, url: str, *, headers_in: dict[str, str],
                   body: object = None, proxy: str | None = None,
                   timeout: float, stream: bool = False) -> httpx.Response:
    content = (json.dumps(body, ensure_ascii=False).encode('utf-8')
               if body is not None else None)
    try:
        client_ctx = config.http_client(_timeout(timeout),
                                       proxy=_selected_proxy(proxy),
                                       lane='accio')
        async with client_ctx as client:
            request = client.build_request(method, url, headers=headers_in,
                                           content=content)
            return await client.send(request, stream=stream)
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc


# ── 信封 ────────────────────────────────────────────────────
def _envelope_failure(status: int, payload: object, what: str,
                      *, retry_after: object = None) -> PlatformError | None:
    """非 2xx / 业务失败的统一出口；`success === true` 才算成功。"""
    failure = P.classify_failure(status, body=payload,
                                 media_type='', retry_after=retry_after)
    if failure is None:
        code = P.payload_code_of(payload)
        message = P.message_of(payload, '')
        failure = P.Failure(P.KIND_INVALID, status if status >= 400 else 400,
                            code or 'biz_error',
                            message or f'{what}失败')
    exc = PlatformError(f'{what}：{failure.message}', status=failure.status,
                        code=failure.code, retry_after=failure.retry_after,
                        payload=payload)
    return _with_failure(exc, failure)


def _check_token_envelope(payload: object, what: str) -> dict:
    if not isinstance(payload, dict):
        raise PlatformError(f'{what}：上游返回不是 JSON 对象', status=502,
                            code='bad_response', payload=payload)
    if payload.get('success') is not True:
        exc = _envelope_failure(200, payload, what)
        assert exc is not None
        raise exc
    data = payload.get('data')
    if not isinstance(data, dict):
        raise PlatformError(f'{what}：响应缺少 data', status=502,
                            code='bad_response', payload=payload)
    return data


def _token_of(data: dict, what: str) -> dict:
    """`data` → `{access_token, refresh_token, expires_at(ms), sgcookie?}`。

    三者缺一即无效：上游 / 官方客户端都按这个判据。
    """
    access = data.get('accessToken')
    refresh = data.get('refreshToken')
    expires_ms = P.expires_at_ms(data.get('expiresAt'))
    if not isinstance(access, str) or access == '':
        raise PlatformError(f'{what}：响应缺 accessToken', status=502,
                            code='bad_response')
    if not isinstance(refresh, str) or refresh == '':
        raise PlatformError(f'{what}：响应缺 refreshToken', status=502,
                            code='bad_response')
    if expires_ms is None:
        raise PlatformError(f'{what}：响应缺 expiresAt（或不是数字）',
                            status=502, code='bad_response')
    out = {'access_token': access, 'refresh_token': refresh,
           'expires_at': expires_ms}
    sgcookie = data.get('sgcookie')
    if isinstance(sgcookie, str) and sgcookie:
        out['sgcookie'] = sgcookie
    return out


# ── OAuth ───────────────────────────────────────────────────
async def exchange_code(code: str, code_verifier: str, redirect_uri: str, *,
                        proxy: str | None = None,
                        timeout: float = C.REQUEST_TIMEOUT) -> dict:
    """授权码 + PKCE → 凭据。"""
    url = P.gateway_url(C.TOKEN_EXCHANGE_PATH)
    body = {
        'code': str(code or ''),
        'codeVerifier': str(code_verifier or ''),
        'clientId': C.CLIENT_ID,
        'redirectUri': str(redirect_uri or ''),
    }
    response = await _request('POST', url, headers_in={
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    }, body=body, proxy=proxy, timeout=timeout)
    payload = _json(response)
    if response.status_code >= 400:
        raise _response_failure(response, payload, 'Accio 授权码兑换失败')
    return _token_of(_check_token_envelope(payload, 'Accio 授权码兑换失败'),
                     'Accio 授权码兑换失败')


async def refresh(account: dict, *, proxy: str | None = None,
                  timeout: float = C.REQUEST_TIMEOUT) -> dict:
    """续期：`accessToken` + `refreshToken` → 新凭据。"""
    access = str(account.get('accessToken') or '')
    refresh_token = str(account.get('refreshToken') or '')
    if not refresh_token:
        raise PlatformError('Accio 账号缺少 refresh token，请重新登录',
                            status=401, code='login_expired')
    url = P.gateway_url(C.REFRESH_PATH)
    body = {'accessToken': access, 'refreshToken': refresh_token}
    response = await _request('POST', url, headers_in={
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    }, body=body, proxy=proxy, timeout=timeout)
    payload = _json(response)
    if response.status_code >= 400:
        raise _response_failure(response, payload, 'Accio 续期失败')
    return _token_of(_check_token_envelope(payload, 'Accio 续期失败'),
                     'Accio 续期失败')


# ── 身份 ────────────────────────────────────────────────────
async def user_info(account: dict, access: str, *, proxy: str | None = None,
                    timeout: float = C.REQUEST_TIMEOUT) -> dict:
    """`POST /api/auth/userinfo`（token 在 body；GET 会 405）。"""
    url = P.gateway_url(C.USERINFO_PATH)
    response = await _request(
        'POST', url, headers_in=headers(account), body={'accessToken': access},
        proxy=proxy, timeout=timeout)
    payload = _json(response)
    if response.status_code >= 400:
        raise _response_failure(response, payload, 'Accio 账号信息查询失败')
    if not isinstance(payload, dict):
        raise PlatformError('Accio 账号信息查询失败：响应不是 JSON',
                            status=502, code='bad_response', payload=payload)
    if payload.get('success') is False:
        failure = _envelope_failure(200, payload, 'Accio 账号信息查询失败')
        assert failure is not None
        raise failure
    data = _record(payload.get('data'))
    team_raw = data.get('activeTeam')
    team = None
    if isinstance(team_raw, dict):
        team_id = str(team_raw.get('teamId') or '').strip()
        if team_id:
            team = {
                'teamId': team_id,
                'teamName': str(team_raw.get('teamName') or '').strip(),
                'role': str(team_raw.get('role') or '').strip(),
            }
    return {
        'userId': str(data.get('userId') or '').strip(),
        'userName': str(data.get('userName') or '').strip(),
        'routeRegion': str(data.get('routeRegion') or '').strip(),
        'userLevel': str(data.get('userLevel') or '').strip(),
        'maskedMobile': str(data.get('maskedMobile') or '').strip(),
        'activeTeam': team,
    }


# ── 目录 ────────────────────────────────────────────────────
async def fetch_catalog(account: dict, access: str, *,
                        proxy: str | None = None,
                        timeout: float = C.REQUEST_TIMEOUT) -> list[dict]:
    """`POST /api/llm/config/v2`，body `{token}`。"""
    url = P.gateway_url(C.CATALOG_PATH)
    response = await _request(
        'POST', url, headers_in=headers(account), body={'token': access},
        proxy=proxy, timeout=timeout)
    payload = _json(response)
    if response.status_code >= 400:
        raise _response_failure(response, payload, 'Accio 模型目录查询失败')
    if not isinstance(payload, dict):
        raise PlatformError('Accio 模型目录查询失败：响应不是 JSON',
                            status=502, code='bad_response', payload=payload)
    if payload.get('success') is False:
        failure = _envelope_failure(200, payload, 'Accio 模型目录查询失败')
        assert failure is not None
        raise failure
    return catalog.parse(payload, realm=account.get('realm'))


# ── 积分 ────────────────────────────────────────────────────
async def _entitlement_get(account: dict, access: str, path: str, what: str, *,
                           proxy: str | None = None,
                           timeout: float = C.REQUEST_TIMEOUT) -> dict:
    url = (P.gateway_url(path) + '?'
           + urllib.parse.urlencode({'accessToken': access}))
    response = await _request('GET', url, headers_in=_credit_headers(account),
                              proxy=proxy, timeout=timeout)
    payload = _json(response)
    if response.status_code >= 400:
        raise _response_failure(response, payload, what)
    if not isinstance(payload, dict):
        raise PlatformError(f'{what}：响应不是 JSON', status=502,
                            code='bad_response', payload=payload)
    if payload.get('success') is False:
        failure = _envelope_failure(200, payload, what)
        assert failure is not None
        raise failure
    data = payload.get('data')
    if not isinstance(data, dict):
        raise PlatformError(f'{what}：响应缺少 data', status=502,
                            code='bad_response', payload=payload)
    return data


async def subscription(account: dict, access: str, *, proxy: str | None = None,
                       timeout: float = C.REQUEST_TIMEOUT) -> dict:
    return await _entitlement_get(
        account, access, C.SUBSCRIPTION_PATH, 'Accio 积分查询失败',
        proxy=proxy, timeout=timeout)


async def quota(account: dict, access: str, *, proxy: str | None = None,
                timeout: float = C.REQUEST_TIMEOUT) -> dict:
    return await _entitlement_get(
        account, access, C.QUOTA_PATH, 'Accio 配额查询失败',
        proxy=proxy, timeout=timeout)


# ── 对话 ────────────────────────────────────────────────────
async def open_chat(account: dict, access: str, body: dict, *,
                    proxy: str | None = None,
                    idle_timeout: float = C.SSE_IDLE_TIMEOUT) -> httpx.Response:
    """打开对话 SSE。

    上游总以事件流返回（没有 `stream` 开关）。这里先校验状态 / `content-type`：
    风控拒绝页是 HTTP 200 + `text/html`，若当 SSE 读会得到「没有内容」这种
    指不到上游的报错。
    """
    request_id = str(body.get('request_id') or '')
    url = (P.gateway_url(C.CHAT_PATH) + '?sg_k=' + P.sg_k(request_id))
    response = await _request(
        'POST', url, headers_in=headers(account, accept='text/event-stream'),
        body=body, proxy=proxy, timeout=idle_timeout, stream=True)
    if response.status_code >= 400:
        text, payload = await _read_error(response)
        media = P.media_type_of(response.headers.get('content-type'))
        failure = P.classify_failure(response.status_code, body=payload,
                                     media_type=media,
                                     retry_after=response.headers.get('retry-after'))
        if failure is None:
            failure = P.Failure(P.KIND_SERVER, 502, 'SERVER',
                                text[:200] or 'Accio 对话失败')
        exc = PlatformError(f'Accio 对话失败：{failure.message}',
                            status=failure.status, code=failure.code,
                            retry_after=failure.retry_after, payload=payload)
        raise _with_failure(exc, failure)
    media = _media_type(response)
    if 'text/event-stream' not in media:
        text, payload = await _read_error(response)
        failure = P.classify_failure(response.status_code, body=payload,
                                     media_type=media)
        if failure is not None:
            exc = PlatformError(f'Accio 对话失败：{failure.message}',
                                status=failure.status, code=failure.code,
                                payload=payload)
            raise _with_failure(exc, failure)
        if isinstance(payload, dict):
            code, message = P.frame_error_of(payload)
            if code:
                raise _stream_failure(code, message)
        raise PlatformError(
            text[:200] or 'Accio 对话接口未返回事件流', status=502,
            code='bad_upstream', payload=payload)
    return response


def _stream_failure(code: str, message: str) -> PlatformError:
    failure = P.failure_for_stream(code, message)
    exc = PlatformError(f'Accio 对话失败：{failure.message}',
                        status=failure.status, code=failure.code)
    return _with_failure(exc, failure)


async def iter_frames(response: httpx.Response) -> AsyncIterator[dict]:
    """把上游事件流读成帧 dict。

    ⚠️ Accio 的 SSE **不一定用空行分隔事件**（实测上游把多帧拼成
    `data: {...}\\ndata: {...}\\n`，参考实现也是**逐行**处理）。所以这里对每条
    `data:` 行先单独尝试解析；只有解析失败（JSON 被拆成多行）才累积到下一空行。
    """
    buffer: list[str] = []

    def _parse(text: str) -> dict | None:
        raw = text.strip()
        if not raw or raw == '[DONE]':
            return None
        try:
            obj = json.loads(raw)
        except ValueError:
            return None
        return obj if isinstance(obj, dict) else None

    async for line in response.aiter_lines():
        stripped = line.strip()
        if stripped == '':
            if not buffer:
                continue
            obj = _parse('\n'.join(buffer))
            buffer = []
            if obj is not None:
                yield obj
            continue
        if stripped.startswith('data:'):
            payload = stripped[5:].strip()
            obj = _parse(payload)
            if obj is not None:
                # 单行就是完整 JSON：立刻产出（不依赖空行分隔）。
                if buffer:
                    pending = _parse('\n'.join(buffer))
                    buffer = []
                    if pending is not None:
                        yield pending
                yield obj
            elif payload and payload != '[DONE]':
                buffer.append(payload)
            continue
        if stripped.startswith((':', 'event:', 'id:', 'retry:')):
            continue
        obj = _parse(stripped)
        if obj is not None:
            yield obj
    if buffer:
        obj = _parse('\n'.join(buffer))
        if obj is not None:
            yield obj
