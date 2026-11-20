"""MiniMax Code 上游协议层。

与其它平台一致：本模块只做协议，不做选号 / 冷却 / 缓存；那些在 `adapter.py`。

四组端点：

  · **OAuth**（`account.minimax.*`）——设备码登录与 refresh token 轮换；
  · **模型**（`agent.minimax.*`）——模型目录与 Anthropic Messages 对话；
  · **账号**（`agent.minimaxi.com` / `agent.minimax.io`）——带 `yy` /
    `x-signature` 签名的身份 / 套餐 / 签到 / 积分明细接口；
  · **控制台**（`www.minimaxi.com` / `platform.minimax.io`）——M Plan 的
    窗口余量。

代理口径与项目其余部分一致：`None` = 用全局代理，`''` = 强制直连。
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import sys
import time
import urllib.parse
import uuid
from typing import Any

import httpx

from ... import config
from ..base import PlatformError
from . import constants as C

_QUOTA = re.compile(
    r'insufficient|balance|credit|quota|exhaust|limit|余额|积分|额度|不足|用完|上限',
    re.I)

# 上游会用 3xx 把请求转到另一个站点：国际站（agent.minimax.io）在 Akamai
# 后面，大陆出口会被 307 到 agent.minimaxi.com，而国内站拒绝国际站 token。
# httpx 默认不跟重定向，所以我们自己认 Location：跨站就报清楚原因，别把
# 它当成 token 失效（更不能因此消耗一次性的 refresh token）。
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


def _redirect_target(response: httpx.Response, request_url: str) -> tuple[str, str] | None:
    """3xx 且 Location 换站时返回 `(from_host, to_host)`，否则 None。"""
    if response.status_code not in _REDIRECT_STATUSES:
        return None
    location = str(response.headers.get('location') or '').strip()
    if not location:
        return None
    try:
        target = urllib.parse.urljoin(str(request_url), location)
        source_host = urllib.parse.urlsplit(str(request_url)).netloc.lower()
        target_host = urllib.parse.urlsplit(target).netloc.lower()
    except ValueError:
        return None
    if not target_host or target_host == source_host:
        return None
    return source_host, target_host


def _guard_redirect(response: httpx.Response, request_url: str,
                    info: dict, what: str = '') -> None:
    """跨站 3xx → 明确的 PlatformError；同站 3xx 留给调用方。"""
    target = _redirect_target(response, request_url)
    if target is None:
        return
    source_host, target_host = target
    label = f'MiniMax Code{" " + what if what else ""}'
    if info.get('realm') == 'global':
        hint = ('国际站的请求被转到了国内站，国内站不接受国际站的登录态。'
                '请给这个账号绑定一个中国大陆以外的代理出口后重试。')
    else:
        hint = (f'请求被转到了 {target_host}，该站不接受本站的登录态。'
                '请检查这个账号绑定的代理出口。')
    raise PlatformError(
        f'{label} 被上游重定向：{source_host} → {target_host}'
        f'（HTTP {response.status_code}）。{hint}',
        status=502, code='cross_site_redirect',
        payload={'from': source_host, 'to': target_host,
                 'status': response.status_code})


# ── 站点 / 出口 ─────────────────────────────────────────────
def site(realm: object) -> dict[str, str]:
    key = str(realm or '').strip().lower()
    return C.SITES.get(key) or C.SITES[C.DEFAULT_REALM]


def _selected_proxy(proxy: str | None) -> str:
    if proxy is None:
        return config.effective_http_proxy()
    return str(proxy).strip()


def _timeout(timeout: float) -> httpx.Timeout:
    return httpx.Timeout(timeout, connect=min(10.0, max(3.0, timeout)))


# ── 错误 ────────────────────────────────────────────────────
def _error_text(payload: object, fallback: str) -> str:
    if isinstance(payload, dict):
        error = payload.get('error')
        if isinstance(error, dict) and error.get('message'):
            return str(error['message'])
        for key in ('message', 'msg', 'status_msg', 'detail'):
            if payload.get(key):
                return str(payload[key])
        base = payload.get('base_resp')
        if isinstance(base, dict) and base.get('status_msg'):
            return str(base['status_msg'])
    return fallback


def _response_error(response: httpx.Response, text: str,
                    payload: object = None) -> PlatformError:
    message = _error_text(payload, f'HTTP {response.status_code}')
    if response.status_code in (401, 403):
        if response.status_code == 403 and _QUOTA.search(text):
            return PlatformError(f'额度已用完：{message}', status=402,
                                 code='quota_exhausted', payload=payload)
        return PlatformError(f'登录已失效：{message}', status=401,
                             code='login_expired', payload=payload)
    if response.status_code == 402:
        return PlatformError(f'额度已用完：{message}', status=402,
                             code='quota_exhausted', payload=payload)
    if response.status_code == 404:
        return PlatformError(f'接口不存在：{message}', status=404,
                             code='not_found', payload=payload)
    if 300 <= response.status_code < 400:
        # 同站重定向到这里说明没有可用 Location（没跟）或跳转层数过多。
        return PlatformError(f'上游重定向（HTTP {response.status_code}）：{message}',
                             status=502, code='upstream_redirect',
                             payload=payload)
    if response.status_code == 429:
        retry = response.headers.get('retry-after')
        return PlatformError(f'限流：{message}', status=429,
                             code='rate_limited', payload=payload,
                             retry_after=int(retry) if str(retry or '').isdigit() else None)
    if response.status_code >= 500:
        return PlatformError(f'上游不可用：{message}', status=502,
                             code='upstream_error', payload=payload)
    return PlatformError(f'请求被上游拒绝：{message}', status=400,
                         code='invalid_request', payload=payload)


def _network_error(exc: Exception) -> PlatformError:
    return PlatformError(f'MiniMax Code 不可达：{exc}', status=502,
                         code='network_error')


def _json(response: httpx.Response) -> object:
    try:
        return response.json()
    except ValueError:
        return None


# ── OAuth ───────────────────────────────────────────────────
def _token_payload(raw: object) -> dict:
    payload = raw if isinstance(raw, dict) else {}
    access = str(payload.get('access_token') or '').strip()
    if not access:
        raise PlatformError('MiniMax 登录响应缺少 access_token', status=502,
                            code='oauth_bad_response', payload=raw)
    try:
        expires_in = int(payload.get('expires_in') or 0)
    except (TypeError, ValueError):
        expires_in = 0
    return {
        'access_token': access,
        'refresh_token': str(payload.get('refresh_token') or '').strip(),
        'expires_in': expires_in if expires_in > 0 else C.TOKEN_FALLBACK_SECONDS,
    }


async def _post_form(site_info: dict, path: str, fields: dict[str, str], *,
                     proxy: str | None, timeout: float) -> tuple[httpx.Response, object]:
    url = f"{site_info['account'].rstrip('/')}{path}"
    try:
        async with config.http_client(_timeout(timeout), proxy=_selected_proxy(proxy),
                                      lane='minimax') as client:
            response = await client.post(
                url, data=fields,
                headers={'Accept': 'application/json',
                         'Content-Type': 'application/x-www-form-urlencoded'})
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc
    _guard_redirect(response, url, site_info, '登录')
    payload = _json(response)
    return response, payload


async def start_device(realm: object, *, proxy: str | None = None,
                       timeout: float = C.TOKEN_TIMEOUT) -> dict:
    """发起设备码登录（PKCE）。返回轮询所需的一切。"""
    info = site(realm)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode('ascii')).digest()).rstrip(b'=').decode('ascii')
    response, payload = await _post_form(info, C.DEVICE_CODE_PATH, {
        'client_id': C.CLIENT_ID,
        'scope': C.SCOPE,
        'audience': C.AUDIENCE,
        'code_challenge': challenge,
        'code_challenge_method': 'S256',
    }, proxy=proxy, timeout=timeout)
    if response.status_code >= 400 or not isinstance(payload, dict) \
            or payload.get('error'):
        raise _response_error(response, response.text, payload)
    device_code = str(payload.get('device_code') or '').strip()
    user_code = str(payload.get('user_code') or '').strip()
    uri = str(payload.get('verification_uri_complete')
              or payload.get('verification_uri') or payload.get('verification_url')
              or '').strip()
    if not user_code or not uri:
        raise PlatformError('MiniMax 设备码响应不完整', status=502,
                            code='oauth_bad_response', payload=payload)
    try:
        interval = float(payload.get('interval') or 5)
    except (TypeError, ValueError):
        interval = 5.0
    # 上游的 user_code 变体把 interval 给成毫秒（如 5000）；>1000 按毫秒折回秒。
    if interval > 1000:
        interval /= 1000.0
    try:
        expires = float(payload.get('expires_in')
                        or payload.get('expired_in') or C.DEVICE_TTL_SECONDS)
    except (TypeError, ValueError):
        expires = C.DEVICE_TTL_SECONDS
    if expires > 1e12:      # 上游用绝对毫秒时间
        expires = max(1.0, (expires - time.time() * 1000) / 1000)
    return {
        'realm': info['realm'],
        'verifier': verifier,
        'device_code': device_code,
        'user_code': user_code,
        'url': uri,
        'interval': max(1.0, min(30.0, interval)),
        'expires_in': int(max(30.0, min(3600.0, expires))),
    }


async def poll_device(realm: object, device: dict, *,
                      proxy: str | None = None,
                      timeout: float = C.TOKEN_TIMEOUT) -> dict:
    """轮询设备码。`status` ∈ pending / slow_down / done / denied / expired。"""
    info = site(realm)
    fields = {
        'grant_type': C.DEVICE_GRANT,
        'client_id': C.CLIENT_ID,
        'code_verifier': str(device.get('verifier') or ''),
    }
    if device.get('device_code'):
        fields['device_code'] = str(device['device_code'])
    else:
        fields['user_code'] = str(device.get('user_code') or '')
    response, payload = await _post_form(info, C.TOKEN_PATH, fields,
                                         proxy=proxy, timeout=timeout)
    body = payload if isinstance(payload, dict) else {}
    error = str(body.get('error') or '').strip()
    status = str(body.get('status') or '').strip()
    if response.status_code < 400 and not error:
        if payload and body.get('access_token'):
            return {'status': 'done', 'token': _token_payload(payload)}
        if status in ('', 'pending'):
            return {'status': 'pending'}
        if status in ('denied', 'access_denied'):
            return {'status': 'denied'}
        if status in ('expired', 'expired_token'):
            return {'status': 'expired'}
    if error in ('authorization_pending', 'pending'):
        return {'status': 'pending'}
    if error == 'slow_down':
        return {'status': 'slow_down'}
    if error in ('access_denied', 'denied'):
        return {'status': 'denied'}
    if error in ('expired_token', 'expired', 'invalid_grant', 'invalid_request'):
        return {'status': 'expired'}
    if response.status_code >= 500:
        return {'status': 'pending'}      # 上游抖动，继续等
    raise _response_error(response, response.text, payload)


async def refresh_token(realm: object, refresh: str, *, proxy: str | None = None,
                        timeout: float = C.TOKEN_TIMEOUT) -> dict:
    """用 refresh token 换新 token（旧的会被上游作废）。"""
    info = site(realm)
    response, payload = await _post_form(info, C.TOKEN_PATH, {
        'grant_type': 'refresh_token',
        'refresh_token': str(refresh or ''),
        'client_id': C.CLIENT_ID,
        'scope': C.SCOPE,
        'audience': C.AUDIENCE,
    }, proxy=proxy, timeout=timeout)
    body = payload if isinstance(payload, dict) else {}
    error = str(body.get('error') or '').strip()
    if error in ('invalid_grant', 'invalid_token'):
        raise PlatformError('MiniMax Code 登录已失效，请重新登录', status=401,
                            code='login_expired', payload=payload)
    if response.status_code >= 400 or error:
        exc = _response_error(response, response.text, payload)
        if exc.status == 401:
            raise PlatformError('MiniMax Code 登录已失效，请重新登录', status=401,
                                code='login_expired', payload=payload) from exc
        raise exc
    return _token_payload(payload)


# ── 模型目录 ────────────────────────────────────────────────
async def fetch_models(realm: object, access: str = '', *, proxy: str | None = None,
                       timeout: float = C.MODELS_TIMEOUT) -> list[dict]:
    """读在线模型配置；没有可用 token 时也允许匿名读（客户端启动时如此）。"""
    info = site(realm)
    url = (f"{info['llm'].rstrip('/')}{C.MODELS_PATH}"
           f"?region={urllib.parse.quote(info['region'])}&buildEnv=prod")

    async def ask(token: str) -> httpx.Response:
        headers = {'Accept': 'application/json'}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        async with config.http_client(_timeout(timeout),
                                      proxy=_selected_proxy(proxy),
                                      lane='minimax') as client:
            return await client.get(url, headers=headers)

    try:
        response = await ask(str(access or ''))
        if response.status_code in (401, 403) and access:
            response = await ask('')
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc
    _guard_redirect(response, url, info, '模型目录')
    if response.status_code >= 400:
        raise _response_error(response, response.text, _json(response))
    payload = _json(response)
    from . import catalog  # noqa: PLC0415 —— catalog 反向依赖 constants，避免环
    return catalog.parse_models(payload)


# ── 签名账号接口 ────────────────────────────────────────────
def _encode_component(text: str) -> str:
    """JS `encodeURIComponent` 的等价物（只保留 A-Za-z0-9-_.!~*'()）。"""
    return urllib.parse.quote(text, safe="-_.!~*'()")


def _tz_offset() -> int:
    """本地时区相对 UTC 的秒数（东区为正，与 JS `getTimezoneOffset()*-60` 一致）。"""
    seconds = time.altzone if time.localtime().tm_isdst > 0 else time.timezone
    return -int(seconds)


def signed_request(info: dict, path: str, *, access: str, user_id: object = '',
                   body: object = None, query: dict | None = None,
                   now_ms: int | None = None) -> tuple[str, dict[str, str], str | None]:
    """构造一次带 `yy` / `x-signature` 的账号 API 请求。

    返回 `(url, headers, body_text)`；GET 的 body_text 为 None。
    """
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    params: dict[str, str] = {
        'device_platform': 'mcode',
        'biz_id': C.SIGN_BIZ_ID,
        'app_id': C.SIGN_APP_ID,
        'version_code': C.SIGN_VERSION_CODE,
    }
    params.update({str(k): str(v) for k, v in (query or {}).items()})
    params.update({
        'unix': str(now),
        'timezone_offset': str(_tz_offset()),
        'sys_language': info['lang'],
        'lang': info['lang'],
        'device_id': '0',
        'os_name': sys.platform,
        'browser_name': 'mcode',
        'user_id': str(user_id or '').strip() or '0',
        'client': 'mcode',
    })
    query_text = urllib.parse.urlencode(params)
    at = f'{path}?{query_text}'
    body_text = None if body is None else json.dumps(body, ensure_ascii=False,
                                                     separators=(',', ':'))
    encoded = _encode_component(at)
    stamp = hashlib.md5(str(now).encode('utf-8')).hexdigest()
    signed = f'{encoded}_{body_text if body_text is not None else "{}"}{stamp}ooui'
    ts = now // 1000
    signature = (f'{ts}{C.SIGN_SECRET}'
                 + (body_text if body_text is not None else ''))
    headers = {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'User-Agent': 'MiniMaxCode',
        'Authorization': f'Bearer {access}',
        'yy': hashlib.md5(signed.encode('utf-8')).hexdigest(),
        'x-timestamp': str(ts),
        'x-signature': hashlib.md5(signature.encode('utf-8')).hexdigest(),
    }
    return f"{info['agent'].rstrip('/')}{at}", headers, body_text


def _check_envelope(payload: object, what: str) -> dict:
    if not isinstance(payload, dict):
        raise PlatformError(f'{what}：上游返回不是 JSON 对象', status=502,
                            code='bad_response', payload=payload)
    status = payload.get('statusInfo')
    if isinstance(status, dict):
        code = status.get('code')
        if isinstance(code, (int, float)) and int(code) != 0:
            message = _error_text(status, f'状态 {code}')
            if int(code) == 1000048:
                raise PlatformError(f'{what}：登录已失效', status=401,
                                    code='login_expired', payload=payload)
            raise PlatformError(f'{what}：{message}', status=502,
                                code='account_error', payload=payload)
    base = payload.get('base_resp')
    if isinstance(base, dict):
        code = base.get('status_code')
        if isinstance(code, (int, float)) and int(code) != 0:
            raise PlatformError(
                f"{what}：{_error_text(base, f'状态 {code}')}", status=502,
                code='account_error', payload=payload)
    return payload


async def account_call(realm: object, path: str, *, access: str,
                       user_id: object = '', body: object = None,
                       query: dict | None = None, proxy: str | None = None,
                       timeout: float = C.ACCOUNT_TIMEOUT,
                       what: str = 'MiniMax 账号接口') -> dict:
    """一次签名账号 API 调用；主域名不可达时切到备用域名。"""
    info = site(realm)
    origins = [info['agent']]
    if info.get('agent_fallback') and info['agent_fallback'] != info['agent']:
        origins.append(info['agent_fallback'])
    last: Exception | None = None
    for origin in origins:
        local = {**info, 'agent': origin}
        url, headers, body_text = signed_request(
            local, path, access=access, user_id=user_id, body=body, query=query)
        try:
            async with config.http_client(_timeout(timeout),
                                          proxy=_selected_proxy(proxy),
                                          lane='minimax') as client:
                response = await client.request(
                    'POST' if body is not None else 'GET', url,
                    headers=headers,
                    content=body_text.encode('utf-8') if body_text is not None else None)
        except httpx.HTTPError as exc:
            last = exc
            continue
        _guard_redirect(response, url, local, what)
        payload = _json(response)
        if response.status_code in (401, 403):
            raise PlatformError('MiniMax Code 登录已失效', status=401,
                                code='login_expired', payload=payload)
        if response.status_code >= 400:
            raise _response_error(response, response.text, payload)
        return _check_envelope(payload, what)
    raise _network_error(last or RuntimeError('no origin'))


async def fetch_identity(realm: object, access: str, *, user_id: object = '',
                         proxy: str | None = None,
                         timeout: float = C.ACCOUNT_TIMEOUT) -> dict:
    payload = await account_call(realm, C.IDENTITY_PATH, access=access,
                                 user_id=user_id, proxy=proxy, timeout=timeout,
                                 what='账号信息')
    data = payload.get('data') if isinstance(payload.get('data'), dict) else {}
    user = (data.get('userInfo') or data.get('user_info')
            or payload.get('userInfo') or payload.get('user_info')
            or (data if any(k in data for k in
                            ('realUserID', 'real_user_id', 'userId', 'user_id'))
                else {})
            or {})
    user = user if isinstance(user, dict) else {}

    def pick(*keys: str) -> str:
        for key in keys:
            value = user.get(key)
            if value not in (None, ''):
                return str(value).strip()
        return ''

    real_id = pick('realUserID', 'real_user_id', 'userId', 'user_id')
    if not real_id:
        raise PlatformError('账号信息缺少 realUserID', status=502,
                            code='bad_response', payload=payload)
    return {
        'realUserID': real_id,
        'email': pick('userEmail', 'email', 'userMail', 'user_email'),
        'name': pick('name', 'userName', 'user_name', 'nickname'),
    }


async def fetch_membership(realm: object, access: str, user_id: str, *,
                           proxy: str | None = None,
                           timeout: float = C.ACCOUNT_TIMEOUT) -> dict:
    """套餐 / 积分摘要；识别不了时返回 `{}`（不把 Free 当结论）。

    国际站的这两个矩阵接口都返回 404（上游已知行为），此时套餐是「不知道」，
    不是 Free：返回空字典，由调用方把 entitlement 留空。
    """
    try:
        workspace = await account_call(
            realm, C.WORKSPACE_PATH, access=access, user_id=user_id, body={},
            proxy=proxy, timeout=timeout, what='工作区')
    except PlatformError as exc:
        if exc.status in (401, 402):
            raise
        if exc.status != 404:
            raise
        # 国际站没有工作区接口：直接问会员接口，两个都 404 就是「未知」。
        try:
            payload = await account_call(
                realm, C.MEMBERSHIP_PATH, access=access, user_id=user_id,
                body={}, proxy=proxy, timeout=timeout, what='套餐')
        except PlatformError as inner:
            if inner.status == 404:
                return {}
            raise
        return _membership_of(payload)
    data = workspace.get('data') if isinstance(workspace.get('data'), dict) else {}
    items = (workspace.get('workspaces') if isinstance(workspace.get('workspaces'), list)
             else data.get('workspaces') if isinstance(data.get('workspaces'), list)
             else [])
    target = None
    for item in items:
        if isinstance(item, dict) and str(item.get('workspace_type')) == '0':
            target = item
            break
    if target is None:
        return {}
    workspace_id = target.get('workspace_id')
    try:
        payload = await account_call(
            realm, C.MEMBERSHIP_PATH, access=access, user_id=user_id,
            body={'workspace_id': workspace_id}, proxy=proxy, timeout=timeout,
            what='套餐')
    except PlatformError as exc:
        if exc.status in (401, 402):
            raise
        return _membership_of(target)
    merged = {**_membership_of(target), **_membership_of(payload)}
    return merged


def _membership_of(payload: object) -> dict:
    env = payload if isinstance(payload, dict) else {}
    data = env.get('data') if isinstance(env.get('data'), dict) else {}

    def pick(key: str) -> object:
        value = env.get(key)
        if value is None:
            value = data.get(key)
        return value

    summary = env.get('op_credit_summary') or data.get('op_credit_summary') or {}
    summary = summary if isinstance(summary, dict) else {}
    balance = summary.get('total_remaining_amount')
    if balance in (None, ''):
        balance = pick('opcredit_balance')
    expires = _int_or_zero(pick('token_plan_expires_at'))
    if expires > 10 ** 12:
        expires = expires // 1000
    return {
        'subscribed': pick('has_token_plan') is True,
        'known': True,
        'tier': str(pick('token_plan_tier') or ''),
        'group_id': str(pick('op_group_id') or ''),
        'expires_at': expires,
        'balance': str(balance) if balance not in (None, '') else '',
    }


def _int_or_zero(value: object) -> int:
    try:
        return int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


# ── 签到 / 积分 / 套餐窗口 ──────────────────────────────────
async def checkin_status(realm: object, access: str, user_id: str, *,
                         proxy: str | None = None,
                         timeout: float = C.ACCOUNT_TIMEOUT) -> dict:
    payload = await account_call(
        realm, C.CHECKIN_STATUS_PATH, access=access, user_id=user_id,
        query=dict(C.CHECKIN_QUERY), proxy=proxy, timeout=timeout,
        what='签到面板')
    data = payload.get('data') if isinstance(payload.get('data'), dict) else {}
    days = data.get('days') if isinstance(data.get('days'), list) else []
    return {'days': [d for d in days if isinstance(d, dict)]}


async def claim_checkin(realm: object, access: str, user_id: str, *,
                        proxy: str | None = None,
                        timeout: float = C.ACCOUNT_TIMEOUT) -> dict:
    payload = await account_call(
        realm, C.CHECKIN_CLAIM_PATH, access=access, user_id=user_id, body={},
        query=dict(C.CHECKIN_QUERY), proxy=proxy, timeout=timeout,
        what='签到')
    data = payload.get('data') if isinstance(payload.get('data'), dict) else {}
    return {
        'claim_result': _int_or_zero(data.get('claim_result')),
        'points': _int_or_zero(data.get('points')),
    }


async def credit_details(realm: object, access: str, user_id: str, *,
                         proxy: str | None = None,
                         timeout: float = C.ACCOUNT_TIMEOUT,
                         page_size: int = 20, max_pages: int = 5) -> list[dict]:
    """积分明细（分批；只读前几页，够算余额与最早到期）。"""
    out: list[dict] = []
    for page in range(1, max_pages + 1):
        query = {**C.CHECKIN_QUERY, 'page_number': str(page),
                 'page_size': str(page_size), 'include_expired': 'true'}
        payload = await account_call(
            realm, C.CREDIT_DETAILS_PATH, access=access, user_id=user_id,
            query=query, proxy=proxy, timeout=timeout, what='积分明细')
        data = payload if isinstance(payload.get('details'), list) else payload.get('data')
        if not isinstance(data, dict) or not isinstance(data.get('details'), list):
            return []
        out.extend(item for item in data['details'] if isinstance(item, dict))
        total = _int_or_zero(data.get('total_count'))
        if len(out) >= total or not data['details']:
            return out
    return out


async def plan_quota(realm: object, access: str, group: str = '', *,
                     proxy: str | None = None,
                     timeout: float = C.ACCOUNT_TIMEOUT) -> list[dict]:
    """M Plan 的窗口余量；控制台失败时退回 coding_plan/remains。"""
    info = site(realm)
    headers = {'Accept': 'application/json',
               'Authorization': f'Bearer {access}'}
    if group:
        headers['X-Group-Id'] = group

    async def ask(path: str, budget: float) -> list[dict]:
        url = f"{info['platform'].rstrip('/')}{path}"
        async with config.http_client(_timeout(budget),
                                      proxy=_selected_proxy(proxy),
                                      lane='minimax') as client:
            response = await client.get(url, headers=headers)
        _guard_redirect(response, url, info, 'M Plan')
        if response.status_code >= 400:
            raise _response_error(response, response.text, _json(response))
        return parse_plan(_json(response))

    try:
        windows = await ask(C.PLAN_QUOTA_PATH, min(5.0, timeout))
        if windows:
            return windows
    except (PlatformError, httpx.HTTPError):
        pass
    try:
        return await ask(C.PLAN_QUOTA_FALLBACK, timeout)
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc


def _num(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _ms(value: object) -> int:
    raw = _num(value)
    if raw is None or raw <= 0:
        return 0
    return int(raw if raw >= 1e12 else raw * 1000)


def parse_plan(payload: object) -> list[dict]:
    """把 M Plan 的 `model_remains` 压成展示用的窗口列表。"""
    body = payload if isinstance(payload, dict) else {}
    base = body.get('base_resp')
    if isinstance(base, dict) and _int_or_zero(base.get('status_code')):
        raise PlatformError(f"套餐额度：{_error_text(base, '读取失败')}",
                            status=502, code='plan_error', payload=payload)
    items = body.get('model_remains')
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get('model_name') or '').strip() or 'M Plan'
        for kind, prefix, weekly in (('interval', 'current_interval', False),
                                     ('weekly', 'current_weekly', True)):
            status = _int_or_zero(raw.get(f'{prefix}_status'))
            used = _num(raw.get(f'{prefix}_used_percent'))
            remaining = _num(raw.get(f'{prefix}_remaining_percent'))
            total = _num(raw.get(f'{prefix}_total_percent'))
            if status == 3 and used is None and remaining is None:
                out.append({'name': name, 'kind': kind, 'unlimited': True})
                continue
            if used is None and remaining is None:
                continue
            record: dict[str, Any] = {
                'name': name,
                'kind': kind,
                'used_percent': used,
                'remaining_percent': (remaining if remaining is not None
                                      else max(0.0, 100.0 - (used or 0.0))),
                'total_percent': total,
                'exhausted': status == 2,
            }
            end = _ms(raw.get('weekly_end_time' if weekly else 'end_time'))
            if end:
                record['resets_at'] = end
            used_count = _num(raw.get(f'{prefix}_used_count'))
            total_count = _num(raw.get(f'{prefix}_total_count'))
            if total_count:
                record['used_count'] = int(used_count or 0)
                record['total_count'] = int(total_count)
            out.append(record)
    return out


# ── 对话 ────────────────────────────────────────────────────
def chat_headers(access: str, *, session_id: str = '') -> dict[str, str]:
    return {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {access}',
        'x-api-key': 'sk-xxx',
        'User-Agent': 'MiniMaxAgent',
        'X-Mavis-Agent-Id': 'main',
        'X-Mavis-Timezone-Offset': str(_tz_offset()),
        'X-Mavis-Session-Id': session_id or f'ses_{uuid.uuid4().hex[:24]}',
        'anthropic-version': '2023-06-01',
        # 要求不压缩：上游会等整段回答结束才 flush gzip，首字会被拖到最后。
        'Accept-Encoding': 'identity',
    }


def chat_url(realm: object) -> str:
    info = site(realm)
    return f"{info['llm'].rstrip('/')}{C.CHAT_PATH}"


async def chat(realm: object, access: str, body: dict, *, proxy: str | None = None,
               timeout: float = 120, session_id: str = '') -> dict:
    try:
        async with config.http_client(_timeout(timeout),
                                      proxy=_selected_proxy(proxy),
                                      lane='minimax') as client:
            response = await client.post(chat_url(realm), headers=chat_headers(
                access, session_id=session_id), json=body)
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc
    _guard_redirect(response, chat_url(realm), site(realm), '对话')
    payload = _json(response)
    if response.status_code >= 400:
        raise _response_error(response, response.text, payload)
    if not isinstance(payload, dict):
        raise PlatformError('MiniMax Code 返回的不是 JSON 对象', status=502,
                            code='bad_response', payload=payload)
    if payload.get('type') == 'error':
        raise _response_error(response, response.text, payload)
    return payload


async def open_chat(realm: object, access: str, body: dict, *,
                    proxy: str | None = None, idle_timeout: float = 120,
                    connect_timeout: float = 10,
                    session_id: str = '') -> httpx.Response:
    """流式对话：返回**未读取**的上游响应（调用方负责关闭）。"""
    try:
        async with config.http_client(
                httpx.Timeout(idle_timeout, connect=connect_timeout),
                proxy=_selected_proxy(proxy), lane='minimax') as client:
            request = client.build_request(
                'POST', chat_url(realm),
                headers=chat_headers(access, session_id=session_id),
                content=json.dumps(body, ensure_ascii=False).encode('utf-8'))
            response = await client.send(request, stream=True)
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc
    try:
        _guard_redirect(response, chat_url(realm), site(realm), '对话')
    except PlatformError:
        await response.aclose()
        raise
    if response.status_code >= 400:
        raw = (await response.aread()).decode('utf-8', errors='replace')
        await response.aclose()
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = None
        raise _response_error(response, raw, payload)
    # Anthropic 端点有时对**流式**请求也回 HTTP 200 + JSON 错误体（不是 SSE）。
    # 原样交给 relay 会被当成「没有 data: 的 SSE」解析，客户端只能看到一个
    # 语法错误；这里先读完并按认证 / 上游错误归类，与 Loomy 的口径一致。
    ctype = str(response.headers.get('content-type') or '').lower()
    if 'text/event-stream' not in ctype:
        raw = (await response.aread()).decode('utf-8', errors='replace')
        await response.aclose()
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            payload = {}
        payload = payload if isinstance(payload, dict) else {}
        error = payload.get('error') if isinstance(payload.get('error'), dict) else {}
        kind = str(error.get('type') or payload.get('type') or '').lower()
        if 'auth' in kind or 'permission' in kind or 'login' in kind:
            raise PlatformError(
                f'登录已失效：{_error_text(payload, "MiniMax Code 拒绝该 token")}',
                status=401, code='login_expired', payload=payload)
        raise PlatformError(
            f'MiniMax Code 未返回 SSE：{_error_text(payload, raw[:200] or "空响应")}',
            status=502, code='bad_upstream', payload=payload)
    return response


# ── 同步版本（设备码登录线程）────────────────────────────────
#
# 登录线程不是事件循环（`login_start` 是同步调用），而 `config.http_client`
# 的共享池绑定在创建它的循环上。这里用一次性的 `httpx.Client`，与 Loomy 的
# 扫码线程同一口径，避免把绑定在临时循环上的连接池留在全局池里。
def _sync_client(proxy: str | None, timeout: float) -> httpx.Client:
    selected = _selected_proxy(proxy)
    kwargs: dict[str, Any] = {'timeout': timeout, 'trust_env': False}
    if selected:
        kwargs['proxy'] = selected
    return httpx.Client(**kwargs)


def _post_form_sync(info: dict, path: str, fields: dict[str, str], *,
                    proxy: str | None, timeout: float) -> tuple[httpx.Response, object]:
    url = f"{info['account'].rstrip('/')}{path}"
    try:
        with _sync_client(proxy, timeout) as http:
            response = http.post(
                url, data=fields,
                headers={'Accept': 'application/json',
                         'Content-Type': 'application/x-www-form-urlencoded'})
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc
    _guard_redirect(response, url, info, '登录')
    return response, _json(response)


def start_device_sync(realm: object, *, proxy: str | None = None,
                      timeout: float = C.TOKEN_TIMEOUT) -> dict:
    info = site(realm)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode('ascii')).digest()).rstrip(b'=').decode('ascii')
    response, payload = _post_form_sync(info, C.DEVICE_CODE_PATH, {
        'client_id': C.CLIENT_ID,
        'scope': C.SCOPE,
        'audience': C.AUDIENCE,
        'code_challenge': challenge,
        'code_challenge_method': 'S256',
    }, proxy=proxy, timeout=timeout)
    if response.status_code >= 400 or not isinstance(payload, dict) \
            or payload.get('error'):
        raise _response_error(response, response.text, payload)
    device_code = str(payload.get('device_code') or '').strip()
    user_code = str(payload.get('user_code') or '').strip()
    uri = str(payload.get('verification_uri_complete')
              or payload.get('verification_uri') or payload.get('verification_url')
              or '').strip()
    if not user_code or not uri:
        raise PlatformError('MiniMax 设备码响应不完整', status=502,
                            code='oauth_bad_response', payload=payload)
    try:
        interval = float(payload.get('interval') or 5)
    except (TypeError, ValueError):
        interval = 5.0
    # 上游的 user_code 变体把 interval 给成毫秒（如 5000）；>1000 按毫秒折回秒。
    if interval > 1000:
        interval /= 1000.0
    try:
        expires = float(payload.get('expires_in')
                        or payload.get('expired_in') or C.DEVICE_TTL_SECONDS)
    except (TypeError, ValueError):
        expires = C.DEVICE_TTL_SECONDS
    if expires > 1e12:
        expires = max(1.0, (expires - time.time() * 1000) / 1000)
    return {
        'realm': info['realm'],
        'verifier': verifier,
        'device_code': device_code,
        'user_code': user_code,
        'url': uri,
        'interval': max(1.0, min(30.0, interval)),
        'expires_in': int(max(30.0, min(3600.0, expires))),
    }


def poll_device_sync(realm: object, device: dict, *,
                     proxy: str | None = None,
                     timeout: float = C.TOKEN_TIMEOUT) -> dict:
    info = site(realm)
    fields = {
        'grant_type': C.DEVICE_GRANT,
        'client_id': C.CLIENT_ID,
        'code_verifier': str(device.get('verifier') or ''),
    }
    if device.get('device_code'):
        fields['device_code'] = str(device['device_code'])
    else:
        fields['user_code'] = str(device.get('user_code') or '')
    response, payload = _post_form_sync(info, C.TOKEN_PATH, fields,
                                        proxy=proxy, timeout=timeout)
    body = payload if isinstance(payload, dict) else {}
    error = str(body.get('error') or '').strip()
    status = str(body.get('status') or '').strip()
    if response.status_code < 400 and not error:
        if body.get('access_token'):
            return {'status': 'done', 'token': _token_payload(body)}
        if status in ('', 'pending'):
            return {'status': 'pending'}
        if status in ('denied', 'access_denied'):
            return {'status': 'denied'}
        if status in ('expired', 'expired_token'):
            return {'status': 'expired'}
    if error in ('authorization_pending', 'pending'):
        return {'status': 'pending'}
    if error == 'slow_down':
        return {'status': 'slow_down'}
    if error in ('access_denied', 'denied'):
        return {'status': 'denied'}
    if error in ('expired_token', 'expired', 'invalid_grant', 'invalid_request'):
        return {'status': 'expired'}
    if response.status_code >= 500:
        return {'status': 'pending'}
    raise _response_error(response, response.text, payload)


def fetch_identity_sync(realm: object, access: str, *,
                        proxy: str | None = None,
                        timeout: float = C.ACCOUNT_TIMEOUT) -> dict:
    """设备码登录线程里读账号信息（签名逻辑与异步版共用 `signed_request`）。"""
    info = site(realm)
    url, headers, _ = signed_request(info, C.IDENTITY_PATH, access=access)
    try:
        with _sync_client(proxy, timeout) as http:
            response = http.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc
    _guard_redirect(response, url, info, '账号信息')
    payload = _json(response)
    if response.status_code in (401, 403):
        raise PlatformError('MiniMax Code 登录已失效', status=401,
                            code='login_expired', payload=payload)
    if response.status_code >= 400:
        raise _response_error(response, response.text, payload)
    payload = _check_envelope(payload, '账号信息')
    data = payload.get('data') if isinstance(payload.get('data'), dict) else {}
    user = (data.get('userInfo') or data.get('user_info')
            or payload.get('userInfo') or payload.get('user_info')
            or (data if any(k in data for k in
                            ('realUserID', 'real_user_id', 'userId', 'user_id'))
                else {})
            or {})
    user = user if isinstance(user, dict) else {}

    def pick(*keys: str) -> str:
        for key in keys:
            value = user.get(key)
            if value not in (None, ''):
                return str(value).strip()
        return ''

    return {
        'realUserID': pick('realUserID', 'real_user_id', 'userId', 'user_id'),
        'email': pick('userEmail', 'email', 'userMail', 'user_email'),
        'name': pick('name', 'userName', 'user_name', 'nickname'),
    }
