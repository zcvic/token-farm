"""AutoClaw 上游协议层（登录 / 刷新 / 目录 / 对话 / 积分 / 任务）。

边界与其它平台一致：本模块只做「协议翻译」，不含选号、冷却、会话粘性；那些
策略在 `adapter.py`。

## 三件必须记住的事

1. **信封**：账号面几乎所有接口都回 `{code,msg,time,trace,data}`，`code=0`
   才是成功。HTTP 200 + 非零 `code` 是常态，不能只看状态码。
2. **refresh token 一次一换**：调用方必须先落盘再使用（`adapter._token_for`）。
   刷新失败时的分级也不一样——`400000` 只说明「这个域名不认」，要换下一个
   host 再试，**不能**直接判会话失效（见 `refresh()`）。
3. **区域决定域名与版本**：国内账号面 / 推理面是两个域名，国际只有一个。
   跨区发 token 只有 401，所以 host 回退**只在同一区域内**做。

## 有意不做

- 不改写 system / messages 去绕 2.x 的 406（那是改写用户请求）；
- 不做 force-model / suggested_model 静默改写；
- 不内嵌阿里云滑块：国际 OAuth 只做「打开授权页 + 粘回调」。
"""
from __future__ import annotations

import json
import time
import urllib.parse
from typing import Any, AsyncIterator

import httpx

from ... import config
from ..base import PlatformError
from . import catalog, constants as C, protocol as P

__all__ = [
    'send_code', 'login', 'refresh', 'user_info',
    'fetch_catalog', 'prepare_chat_body', 'chat', 'open_chat', 'iter_chunks',
    'wallets', 'wallet_instances', 'ledgers', 'points_expiring',
    'task_list', 'task_complete', 'checkin_task',
    'oauth_captcha_config', 'oauth_url', 'oauth_login',
    'failure_of', 'is_path_miss',
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


def _data(payload: object) -> dict:
    if isinstance(payload, dict):
        node = payload.get('data')
        if isinstance(node, dict):
            return node
    return {}


def _pick(raw: dict, *keys: str) -> str:
    for key in keys:
        value = raw.get(key)
        if value not in (None, ''):
            return str(value).strip()
    return ''


def _num(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).replace(',', '').strip()
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _int(value: object) -> int:
    number = _num(value)
    return int(number) if number is not None else 0


# ── 错误 ────────────────────────────────────────────────────
def _with_failure(exc: PlatformError,
                  failure: P.Failure) -> PlatformError:
    """把分级结果挂在异常上，供适配器判断「模型级冷却」等。"""
    exc.failure = failure          # type: ignore[attr-defined]
    return exc


def failure_of(exc: PlatformError) -> P.Failure | None:
    failure = getattr(exc, 'failure', None)
    return failure if isinstance(failure, P.Failure) else None


def _error(status: int, payload: object, what: str,
           retry_after: object = None) -> PlatformError | None:
    failure = P.classify_error(status, body=payload, retry_after=retry_after)
    if failure is None:
        return None
    message = failure.message
    exc = PlatformError(f'{what}：{message}' if what else message,
                        status=failure.status, code=failure.code,
                        retry_after=failure.retry_after, payload=payload)
    return _with_failure(exc, failure)


def _check_envelope(payload: object, what: str) -> dict:
    """校验业务码并返回信封根（调用方再按需取 `data`）。

    `code` 非零时先交给 `protocol.classify_error` 分级（会话失效 / 单模型额度 /
    风控），认不出的再按 400 客户端错误上抛——**不能**把 `{code: 100001}` 当成
    成功响应继续解析，那只会得到「缺少字段」这种指不到上游的报错。
    """
    if not isinstance(payload, dict):
        raise PlatformError(f'{what}：上游返回不是 JSON 对象', status=502,
                            code='bad_response', payload=payload)
    code = P.biz_code_of(payload)
    if code and code != '0':
        exc = _error(200, payload, what)
        if exc is not None:
            raise exc
        message = P.message_of(payload, f'业务码 {code}')
        failure = P.Failure(P.KIND_CLIENT, 400, 'biz_error', message,
                            biz_code=code)
        raise _with_failure(
            PlatformError(f'{what}：{message}', status=400, code='biz_error',
                          payload=payload), failure)
    return payload


def _response_error(response: httpx.Response, payload: object,
                    what: str) -> PlatformError:
    retry_after = response.headers.get('retry-after')
    exc = _error(response.status_code, payload, what, retry_after=retry_after)
    if exc is not None:
        return exc
    return PlatformError(f'{what}：上游返回 HTTP {response.status_code}',
                         status=502, code='upstream_error', payload=payload)


def _network_error(exc: Exception) -> PlatformError:
    return PlatformError(f'连接 AutoClaw 失败：{exc}', status=502,
                         code='network_error')


def is_path_miss(exc: PlatformError) -> bool:
    """路径 / 版本不对（而不是账号或额度问题）：可以换下一个候选地址再试。"""
    if exc.status in (404, 405):
        return True
    text = str(exc.message or '').lower()
    return 'not found' in text or 'no route' in text


# ── HTTP ────────────────────────────────────────────────────
def _headers(realm: object, *, access: str = '', extra: dict | None = None,
             accept: str = 'application/json') -> dict[str, str]:
    """签名头 + 鉴权头 + 业务头。

    鉴权头两个都发（`Authorization` 与 `X-Authorization`）：国内 2.x 有「只认
    `X-Authorization`」的说法，国际 1.x 两个都收；多带一个标准头不会让国际版
    报错，少带则可能直接 401。最终收敛成哪种由联调结论决定，见
    `deploy/autoclaw-ref.txt`。
    """
    out = P.signed_headers(realm=realm)
    out['Accept'] = accept
    out['Content-Type'] = 'application/json'
    if access:
        out.update(P.bearer_headers(access))
    if extra:
        out.update({str(k): str(v) for k, v in extra.items() if v is not None})
    return out


async def _request(method: str, url: str, *, realm: object, access: str = '',
                   body: object = None, extra: dict | None = None,
                   accept: str = 'application/json',
                   proxy: str | None = None, timeout: float,
                   stream: bool = False) -> httpx.Response:
    headers = _headers(realm, access=access, extra=extra, accept=accept)
    content = (json.dumps(body, ensure_ascii=False).encode('utf-8')
               if body is not None else None)
    try:
        client_ctx = config.http_client(_timeout(timeout),
                                        proxy=_selected_proxy(proxy),
                                        lane='autoclaw')
        async with client_ctx as client:
            request = client.build_request(method, url, headers=headers,
                                           content=content)
            return await client.send(request, stream=stream)
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc


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


# ── 国内短信登录 ────────────────────────────────────────────
def _account_url(realm: object, path: str, host: str | None = None) -> str:
    base = host or P.account_hosts(realm)[0]
    return P.join(base, path)


def _token_payload(payload: object) -> dict:
    """登录 / 刷新响应 → 统一 token 结构（含到期时刻）。"""
    data = _data(payload)
    raw = data or (payload if isinstance(payload, dict) else {})
    access = _pick(raw, 'access_token', 'accessToken', 'token')
    if not access:
        raise PlatformError('AutoClaw 响应里没有 access_token', status=502,
                            code='oauth_bad_response', payload=payload)
    refresh = _pick(raw, 'refresh_token', 'refreshToken')
    expires_in = _int(raw.get('expires_in') or raw.get('expiresIn'))
    access_exp = P.token_expiry(access)
    if not access_exp:
        access_exp = time.time() + (expires_in
                                    if expires_in > 0
                                    else C.TOKEN_FALLBACK_SECONDS)
    refresh_expires_in = _int(raw.get('refresh_expires_in')
                              or raw.get('refreshExpiresIn'))
    refresh_exp = P.token_expiry(refresh) if refresh else 0
    if not refresh_exp and refresh_expires_in > 0:
        refresh_exp = time.time() + refresh_expires_in
    return {
        'access_token': access,
        'refresh_token': refresh,
        'expires_at': float(access_exp),
        'refresh_expires_at': float(refresh_exp),
        'expires_in': expires_in,
        'refresh_expires_in': refresh_expires_in,
        'device_id': _pick(raw, 'device_id', 'deviceId'),
        'raw': raw,
    }


async def send_code(realm: object, phone: object, *, device_id: object = '',
                    proxy: str | None = None,
                    timeout: float = C.ACCOUNT_TIMEOUT) -> dict:
    """国内短信发码。返回 `{device_id, message}`；设备号必须与下一步复用。"""
    region = P.normalize_realm(realm)
    if region != 'cn':
        raise PlatformError('AutoClaw 国际版用 OAuth 登录，没有短信验证码',
                            status=400, code='sms_unsupported')
    device = str(device_id or '').strip()
    last: PlatformError | None = None
    for host in P.account_hosts(region):
        try:
            response = await _request(
                'POST', _account_url(region, C.SEND_CODE_PATH, host),
                realm=region, body={
                    'phone': str(phone or '').strip(),
                    'source_id': 'autoclaw',
                    'device_id': device,
                }, proxy=proxy, timeout=timeout)
        except PlatformError as exc:
            last = exc
            continue
        payload = _json(response)
        if response.status_code >= 400:
            exc = _response_error(response, payload, '发送验证码失败')
            if is_path_miss(exc):
                last = exc
                continue
            raise exc
        root = _check_envelope(payload, '发送验证码失败')
        data = _data(root)
        return {
            'device_id': _pick(data, 'device_id', 'deviceId') or device,
            'message': P.message_of(root, '验证码已发送'),
        }
    raise last or PlatformError('发送验证码失败', status=502,
                                code='upstream_error')


async def login(realm: object, phone: object, code: object, *,
                device_id: object = '', proxy: str | None = None,
                timeout: float = C.ACCOUNT_TIMEOUT) -> dict:
    """国内短信登录 → token 结构。

    两个易错点：`agent-login/` 的**末尾斜杠必须保留**（少了就是另一个路由）；
    `code` 必须以 **JSON 数字**提交（字符串会被判参数错误）。
    """
    region = P.normalize_realm(realm)
    if region != 'cn':
        raise PlatformError('AutoClaw 国际版用 OAuth 登录，没有短信验证码',
                            status=400, code='sms_unsupported')
    text = str(code or '').strip()
    if not (len(text) == 6 and text.isdigit()):
        raise PlatformError('验证码必须是 6 位数字', status=400,
                            code='invalid_code')
    last: PlatformError | None = None
    for host in P.account_hosts(region):
        try:
            response = await _request(
                'POST', _account_url(region, C.LOGIN_PATH, host),
                realm=region, body={
                    'phone': str(phone or '').strip(),
                    'code': int(text),
                    'platform': 'web',
                    'source_id': 'autoclaw',
                    'device_id': str(device_id or '').strip(),
                }, proxy=proxy, timeout=timeout)
        except PlatformError as exc:
            last = exc
            continue
        payload = _json(response)
        if response.status_code >= 400:
            exc = _response_error(response, payload, '登录失败')
            if is_path_miss(exc):
                last = exc
                continue
            raise exc
        root = _check_envelope(payload, '登录失败')
        out = _token_payload(root)
        out['device_id'] = (out.get('device_id')
                            or str(device_id or '').strip())
        return out
    raise last or PlatformError('登录失败', status=502, code='upstream_error')


# ── 刷新 ────────────────────────────────────────────────────
async def _refresh_once(region: str, host: str, path: str, refresh_token: str,
                        device_id: str, *, proxy: str | None,
                        timeout: float) -> dict:
    response = await _request(
        'POST', _account_url(region, path, host), realm=region, body={
            'source_id': 'autoclaw',
            'device_id': device_id,
            'refresh_token': refresh_token,
        }, proxy=proxy, timeout=timeout)
    payload = _json(response)
    if response.status_code >= 400:
        raise _response_error(response, payload, '刷新 AutoClaw token 失败')
    root = _check_envelope(payload, '刷新 AutoClaw token 失败')
    out = _token_payload(root)
    if not out.get('refresh_token'):
        out['refresh_token'] = refresh_token
    out['device_id'] = out.get('device_id') or device_id
    return out


async def refresh(realm: object, refresh_token: str, *,
                  device_id: object = '', proxy: str | None = None,
                  timeout: float = C.TOKEN_TIMEOUT) -> dict:
    """用 refresh token 换新 token（旧的会被上游作废）。

    两条与对话不同的分级规则：

      · 业务码 `400002` 表示「这个接口不认，去试 agent-refresh」，不是错误；
      · `400000` 在刷新流程里可能只是「这个域名不认这份 refresh token」，参考
        实现的做法是换下一个 host 再试，全部 host 都 `400000` 才判失效。

    所以这里不套用对话的「400000 = 会话死」分级——那会让一次域名切换把整号
    标成需重登。
    """
    region = P.normalize_realm(realm)
    key = str(refresh_token or '').strip()
    if not key:
        raise PlatformError('缺少 refresh token', status=401,
                            code='login_expired')
    device = str(device_id or '').strip()
    last: PlatformError | None = None
    for host in P.account_hosts(region):
        try:
            return await _refresh_once(region, host, C.REFRESH_PATH, key,
                                       device, proxy=proxy, timeout=timeout)
        except PlatformError as exc:
            failure = failure_of(exc)
            biz = failure.biz_code if failure is not None else ''
            if biz == C.BIZ_REFRESH_FALLBACK:
                try:
                    return await _refresh_once(
                        region, host, C.AGENT_REFRESH_PATH, key, device,
                        proxy=proxy, timeout=timeout)
                except PlatformError as inner:
                    last = inner
                    inner_failure = failure_of(inner)
                    if (inner_failure is not None
                            and inner_failure.biz_code == C.BIZ_AUTH_EXPIRED):
                        continue
                    raise
            if biz == C.BIZ_AUTH_EXPIRED:
                # 这个域名不认，换下一个 host；全部都不认才算失效。
                last = exc
                continue
            if is_path_miss(exc):
                last = exc
                continue
            raise
    raise last or PlatformError('AutoClaw 登录已失效，请重新登录', status=401,
                                code='login_expired')


# ── 账号信息 ────────────────────────────────────────────────
async def user_info(realm: object, access: str, *, device_id: object = '',
                    proxy: str | None = None,
                    timeout: float = C.ACCOUNT_TIMEOUT) -> dict:
    """读一次 `user-info`：客户端启动那次调用还会触发登录奖励入账。"""
    region = P.normalize_realm(realm)
    last: PlatformError | None = None
    for host in P.account_hosts(region):
        try:
            response = await _request(
                'POST', _account_url(region, C.USER_INFO_PATH, host),
                realm=region, access=access, body={
                    'source_id': 'autoclaw',
                    'device_id': str(device_id or '').strip(),
                }, proxy=proxy, timeout=timeout)
        except PlatformError as exc:
            last = exc
            continue
        payload = _json(response)
        if response.status_code >= 400:
            exc = _response_error(response, payload, '读取账号信息失败')
            if is_path_miss(exc):
                last = exc
                continue
            raise exc
        root = _check_envelope(payload, '读取账号信息失败')
        data = _data(root) or root
        return {
            'user_id': _pick(data, 'user_id', 'userId', 'uid', 'id'),
            'name': _pick(data, 'name', 'nickname', 'user_name', 'userName'),
            'email': _pick(data, 'email', 'user_email', 'userEmail'),
            'phone': _pick(data, 'phone', 'mobile', 'phone_number'),
            'raw': data,
        }
    raise last or PlatformError('读取账号信息失败', status=502,
                                code='upstream_error')


# ── 模型目录 ────────────────────────────────────────────────
async def fetch_catalog(realm: object, access: str = '', *,
                        device_id: object = '', proxy: str | None = None,
                        timeout: float = C.MODELS_TIMEOUT) -> list[dict]:
    """读实时模型目录；按「推理面 → 账号面」的候选基址逐个试。

    目录是实时事实源，没有离线快照：全部候选都失败时抛错，由适配器决定退回
    上一份真实目录（连历史都没有就返回空表）。
    """
    region = P.normalize_realm(realm)
    last: PlatformError | None = None
    for host in P.catalog_hosts(region):
        try:
            response = await _request(
                'GET', P.model_config_url(host), realm=region, access=access,
                extra={'X-Request-Model': ''}, proxy=proxy, timeout=timeout)
        except PlatformError as exc:
            last = exc
            continue
        payload = _json(response)
        if response.status_code >= 400:
            exc = _response_error(response, payload, '拉取模型目录失败')
            if is_path_miss(exc):
                last = exc
                continue
            raise exc
        root = _check_envelope(payload, '拉取模型目录失败')
        models = catalog.parse(root, realm=region)
        if models:
            return models
        last = PlatformError('AutoClaw 返回的模型目录为空', status=502,
                             code='empty_catalog', payload=payload)
    raise last or PlatformError('拉取模型目录失败', status=502,
                                code='upstream_error')


# ── 对话 ────────────────────────────────────────────────────
#: 国内 2.x 明确禁发的输出预算字段（406）。国际 1.x 可以带，所以按区域剥。
_MAX_TOKEN_FIELDS = ('max_tokens', 'max_completion_tokens', 'max_output_tokens')


def prepare_chat_body(realm: object, body: dict) -> dict:
    """按区域契约整理请求体。

    · 国内 2.x 剥掉 `max_tokens` 族：上游对这几个字段回 406，带上只会让整次
      请求失败；输出预算由上游默认值决定。
    · 流式补 `stream_options.include_usage=true`，否则拿不到 usage。
    · `reasoning_effort` 原样保留：两代契约对它的说法互相矛盾，剥掉会静默
      改变客户端语义，交给上游判定并原样暴露错误。
    """
    payload = dict(body or {})
    region = P.normalize_realm(realm)
    if P.site(region).get('contract') == 'v2':
        for field in _MAX_TOKEN_FIELDS:
            payload.pop(field, None)
    if payload.get('stream'):
        options = payload.get('stream_options')
        options = dict(options) if isinstance(options, dict) else {}
        options.setdefault('include_usage', True)
        payload['stream_options'] = options
    return payload


def chat_headers(realm: object, access: str, model: str, *,
                 device_id: object = '', session_id: str = '',
                 agent_id: str = '', stream: bool = False) -> dict[str, str]:
    """对话头。

    `X-Request-Model` 是模型名的权威位置（部分版本也读 `body.model`，所以两处
    都写）。国内 2.x 另带 `X-Session-Id` / `X-Agent-Id`；`X-Harness-Type` 在
    对话里**禁发**（406），本函数从不发它。
    """
    extra: dict[str, str] = {'X-Request-Model': str(model or '')}
    device = str(device_id or '').strip()
    if device:
        extra['X-Device-Id'] = device
    region = P.normalize_realm(realm)
    if P.site(region).get('contract') == 'v2':
        if session_id:
            extra['X-Session-Id'] = session_id
        if agent_id:
            extra['X-Agent-Id'] = agent_id
    return _headers(region, access=access, extra=extra,
                    accept='text/event-stream' if stream else 'application/json')


async def chat(realm: object, access: str, body: dict, *, model: str,
               device_id: object = '', session_id: str = '',
               agent_id: str = '', proxy: str | None = None,
               timeout: float = 300) -> dict:
    """非流式对话：按「推理面基址 × 区域首选路径」逐个试，404 才换下一个。"""
    region = P.normalize_realm(realm)
    payload = {**prepare_chat_body(region, body), 'stream': False}
    payload['model'] = str(model or payload.get('model') or '')
    last: PlatformError | None = None
    for host in P.inference_hosts(region):
        for path in P.chat_paths(region):
            try:
                response = await _request(
                    'POST', P.join(host, path), realm=region, access=access,
                    body=payload,
                    extra=_chat_extra(region, model, device_id, session_id,
                                      agent_id),
                    proxy=proxy, timeout=timeout)
            except PlatformError as exc:
                last = exc
                continue
            raw = _json(response)
            if response.status_code >= 400:
                exc = _response_error(response, raw, 'AutoClaw 对话失败')
                if is_path_miss(exc):
                    last = exc
                    continue
                raise exc
            root = _check_envelope(raw, 'AutoClaw 对话失败')
            choices = root.get('choices')
            if not isinstance(choices, list) or not choices:
                raise PlatformError('AutoClaw 对话响应缺少 choices', status=502,
                                    code='bad_upstream', payload=root)
            return root
    raise last or PlatformError('AutoClaw 对话失败', status=502,
                                code='upstream_error')


def _chat_extra(realm: str, model: str, device_id: object,
                session_id: str, agent_id: str) -> dict[str, str]:
    extra: dict[str, str] = {'X-Request-Model': str(model or '')}
    device = str(device_id or '').strip()
    if device:
        extra['X-Device-Id'] = device
    if P.site(realm).get('contract') == 'v2':
        if session_id:
            extra['X-Session-Id'] = session_id
        if agent_id:
            extra['X-Agent-Id'] = agent_id
    return extra


async def open_chat(realm: object, access: str, body: dict, *, model: str,
                    device_id: object = '', session_id: str = '',
                    agent_id: str = '', proxy: str | None = None,
                    idle_timeout: float = 120,
                    connect_timeout: float = 10) -> httpx.Response:
    """流式对话：返回**未读取**的上游响应（调用方负责关闭）。

    上游有时把错误 / 风控伪装成 HTTP 200 + JSON（不是 SSE）。这里先看
    `content-type`：不是事件流就一次性读完、按信封分级，绝不把 JSON 当 SSE
    发给客户端。
    """
    region = P.normalize_realm(realm)
    payload = {**prepare_chat_body(region, body), 'stream': True}
    payload['model'] = str(model or payload.get('model') or '')
    last: PlatformError | None = None
    for host in P.inference_hosts(region):
        for path in P.chat_paths(region):
            url = P.join(host, path)
            try:
                response = await _request(
                    'POST', url, realm=region, access=access, body=payload,
                    extra=_chat_extra(region, model, device_id, session_id,
                                      agent_id),
                    accept='text/event-stream', proxy=proxy,
                    timeout=idle_timeout, stream=True)
            except PlatformError as exc:
                last = exc
                continue
            if response.status_code >= 400:
                _, raw = await _read_error(response)
                exc = _response_error(response, raw, 'AutoClaw 对话失败')
                if is_path_miss(exc):
                    last = exc
                    continue
                raise exc
            ctype = str(response.headers.get('content-type') or '').lower()
            if 'text/event-stream' not in ctype:
                text, raw = await _read_error(response)
                if isinstance(raw, dict):
                    _check_envelope(raw, 'AutoClaw 对话失败')
                    if isinstance(raw.get('choices'), list):
                        raise PlatformError(
                            'AutoClaw 忽略了 stream，返回了完整响应', status=502,
                            code='not_sse', payload=raw)
                raise PlatformError(
                    text[:200] or 'AutoClaw 对话接口未返回 SSE', status=502,
                    code='bad_upstream', payload=raw)
            return response
    raise last or PlatformError('AutoClaw 对话失败', status=502,
                                code='upstream_error')


async def iter_chunks(response: httpx.Response) -> AsyncIterator[dict]:
    """把上游 SSE 读成 OpenAI chunk dict；跳过保活 / `[DONE]`。"""
    buffer: list[str] = []
    async for line in response.aiter_lines():
        if line == '':
            if not buffer:
                continue
            text = '\n'.join(buffer).strip()
            buffer = []
            if not text or text == '[DONE]':
                continue
            try:
                obj = json.loads(text)
            except ValueError:
                continue
            if isinstance(obj, dict):
                yield obj
            continue
        if line.startswith('data:'):
            buffer.append(line[5:].strip())
        elif line.startswith(':') or line.startswith('event:') \
                or line.startswith('id:') or line.startswith('retry:'):
            continue
        elif line.strip():
            buffer.append(line.strip())
    if buffer:
        text = '\n'.join(buffer).strip()
        if text and text != '[DONE]':
            try:
                obj = json.loads(text)
            except ValueError:
                obj = None
            if isinstance(obj, dict):
                yield obj


# ── 积分 / 账单 ────────────────────────────────────────────
async def _account_get(realm: object, path: str, access: str, *,
                       device_id: object, proxy: str | None,
                       timeout: float, what: str) -> dict:
    region = P.normalize_realm(realm)
    last: PlatformError | None = None
    extra = {}
    device = str(device_id or '').strip()
    if device:
        extra['X-Device-Id'] = device
    for host in P.account_hosts(region):
        try:
            response = await _request(
                'GET', P.join(host, path), realm=region, access=access,
                extra=extra, proxy=proxy, timeout=timeout)
        except PlatformError as exc:
            last = exc
            continue
        payload = _json(response)
        if response.status_code >= 400:
            exc = _response_error(response, payload, what)
            if is_path_miss(exc):
                last = exc
                continue
            raise exc
        return _check_envelope(payload, what)
    raise last or PlatformError(f'{what}：没有可用地址', status=502,
                                code='upstream_error')


async def wallets(realm: object, access: str, *, device_id: object = '',
                  proxy: str | None = None,
                  timeout: float = C.ACCOUNT_TIMEOUT) -> dict:
    """总积分：`data.total_balance`（兼容 `wallets[]` 分钱包求和）。"""
    root = await _account_get(realm, C.WALLETS_PATH, access,
                              device_id=device_id, proxy=proxy,
                              timeout=timeout, what='查询积分失败')
    data = _data(root)
    total = _num(data.get('total_balance'))
    if total is None:
        total = _num(data.get('totalBalance'))
    wallets_list = data.get('wallets')
    if not isinstance(wallets_list, list):
        wallets_list = root.get('wallets')
    items = [item for item in wallets_list if isinstance(item, dict)] \
        if isinstance(wallets_list, list) else []
    if total is None and items:
        total = sum(_num(item.get('balance')) or 0.0 for item in items)
    return {
        'total': total if total is not None else 0.0,
        'wallets': items,
        'raw': data or root,
    }


async def wallet_instances(realm: object, access: str, *,
                           device_id: object = '', proxy: str | None = None,
                           timeout: float = C.ACCOUNT_TIMEOUT) -> list[dict]:
    """按用途分类的余额（daily / reward / subscription …）。"""
    root = await _account_get(realm, C.WALLET_INSTANCES_PATH, access,
                              device_id=device_id, proxy=proxy,
                              timeout=timeout, what='查询钱包明细失败')
    data = _data(root)
    items = data.get('wallet_instances') or data.get('instances') \
        or root.get('wallet_instances')
    return [item for item in items if isinstance(item, dict)] \
        if isinstance(items, list) else []


async def ledgers(realm: object, access: str, *, device_id: object = '',
                  query: dict | None = None, proxy: str | None = None,
                  timeout: float = C.ACCOUNT_TIMEOUT) -> list[dict]:
    """逐条账单；筛选参数按上游原样透传（调用方负责拼 `model` / 时间窗）。"""
    path = C.LEDGERS_PATH
    params = {str(k): str(v) for k, v in (query or {}).items()
              if v not in (None, '')}
    if params:
        path = f'{path}?{urllib.parse.urlencode(params)}'
    root = await _account_get(realm, path, access, device_id=device_id,
                              proxy=proxy, timeout=timeout, what='查询积分账单失败')
    data = _data(root)
    items = data.get('items') or data.get('ledgers') or root.get('items')
    return [item for item in items if isinstance(item, dict)] \
        if isinstance(items, list) else []


async def points_expiring(realm: object, access: str, *,
                          device_id: object = '', proxy: str | None = None,
                          timeout: float = C.ACCOUNT_TIMEOUT) -> list[dict]:
    """临期积分列表（展示用）。"""
    root = await _account_get(realm, C.POINTS_EXPIRING_PATH, access,
                              device_id=device_id, proxy=proxy,
                              timeout=timeout, what='查询临期积分失败')
    data = _data(root)
    items = data.get('items') or data.get('points') or root.get('items')
    return [item for item in items if isinstance(item, dict)] \
        if isinstance(items, list) else []


# ── 任务 / 签到 ────────────────────────────────────────────
async def task_list(realm: object, access: str, *, device_id: object = '',
                    proxy: str | None = None,
                    timeout: float = C.ACCOUNT_TIMEOUT) -> list[dict]:
    """任务列表（用来判断今日是否已签）。"""
    root = await _account_get(realm, C.TASK_LIST_PATH, access,
                              device_id=device_id, proxy=proxy,
                              timeout=timeout, what='查询任务列表失败')
    data = _data(root)
    items = data.get('tasks') or data.get('items') or root.get('tasks')
    return [item for item in items if isinstance(item, dict)] \
        if isinstance(items, list) else []


async def task_complete(realm: object, access: str, task_id: object, *,
                        device_id: object = '', proxy: str | None = None,
                        timeout: float = C.ACCOUNT_TIMEOUT) -> dict:
    """完成一个任务（签到用 `daily_signin`）。服务端幂等。"""
    region = P.normalize_realm(realm)
    last: PlatformError | None = None
    extra = {'X-Device-Id': str(device_id or '').strip()} \
        if str(device_id or '').strip() else {}
    for host in P.inference_hosts(region):
        try:
            response = await _request(
                'POST', P.join(host, C.TASK_COMPLETE_PATH), realm=region,
                access=access, body={'task_id': str(task_id or ''),
                                     'source_id': 'autoclaw'},
                extra=extra, proxy=proxy, timeout=timeout)
        except PlatformError as exc:
            last = exc
            continue
        payload = _json(response)
        if response.status_code >= 400:
            exc = _response_error(response, payload, '完成任务失败')
            if is_path_miss(exc):
                last = exc
                continue
            raise exc
        root = _check_envelope(payload, '完成任务失败')
        data = _data(root)
        already = data.get('already_completed')
        if already is None:
            already = data.get('alreadyCompleted')
        return {
            'ok': True,
            'already': bool(already),
            'reward': _num(data.get('reward') or data.get('points')),
            'message': P.message_of(root, '任务已完成'),
            'raw': data or root,
        }
    raise last or PlatformError('完成任务失败', status=502,
                                code='upstream_error')


async def checkin_task(realm: object, access: str, *, device_id: object = '',
                       task_id: str = C.TASK_DAILY_SIGNIN,
                       proxy: str | None = None,
                       timeout: float = C.ACCOUNT_TIMEOUT) -> dict:
    return await task_complete(realm, access, task_id, device_id=device_id,
                               proxy=proxy, timeout=timeout)


# ── 国际 OAuth（只做「打开授权页 + 粘回调」）────────────────
async def oauth_captcha_config(realm: object, *, proxy: str | None = None,
                               timeout: float = C.ACCOUNT_TIMEOUT) -> dict:
    """读滑块配置。第一版不内嵌滑块，只用于判断「上游是否要求人机校验」。"""
    region = P.normalize_realm(realm)
    host = P.account_hosts(region)[0]
    response = await _request('POST', P.join(host, C.OAUTH_CAPTCHA_PATH),
                              realm=region, body={'source_id': 'autoclaw'},
                              proxy=proxy, timeout=timeout)
    payload = _json(response)
    if response.status_code >= 400:
        raise _response_error(response, payload, '读取 OAuth 校验配置失败')
    root = _check_envelope(payload, '读取 OAuth 校验配置失败')
    return _data(root) or root


async def oauth_url(realm: object, vendor: object, *, device_id: object = '',
                    navigate_uri: str = '', captcha_param: object = None,
                    proxy: str | None = None,
                    timeout: float = C.ACCOUNT_TIMEOUT) -> str:
    """申请授权页地址；`vendor` 只能是 `zai` / `google`。"""
    region = P.normalize_realm(realm)
    name = str(vendor or '').strip().lower()
    if name not in C.OAUTH_VENDORS:
        raise PlatformError(f'未知的 OAuth 提供方：{vendor!r}', status=400,
                            code='invalid_vendor')
    body: dict[str, Any] = {
        'source_id': 'autoclaw',
        'device_id': str(device_id or '').strip(),
        'navigate_uri': str(navigate_uri or ''),
    }
    if captcha_param not in (None, '', {}):
        body['ali_captcha_verify_param'] = captcha_param
    host = P.account_hosts(region)[0]
    response = await _request(
        'POST', P.join(host, C.OAUTH_URL_PATH.format(vendor=name)),
        realm=region, body=body, proxy=proxy, timeout=timeout)
    payload = _json(response)
    if response.status_code >= 400:
        raise _response_error(response, payload, '申请 OAuth 授权页失败')
    root = _check_envelope(payload, '申请 OAuth 授权页失败')
    data = _data(root) or root
    url = _pick(data, 'oauth_url', 'url', 'login_url', 'auth_url', 'verify_url')
    if not url:
        raise PlatformError('AutoClaw 没有返回 OAuth 授权页地址', status=502,
                            code='oauth_bad_response', payload=payload)
    return url


async def oauth_login(realm: object, vendor: object, code: object, *,
                      device_id: object = '', state: object = '',
                      navigate_uri: str = '', proxy: str | None = None,
                      timeout: float = C.ACCOUNT_TIMEOUT) -> dict:
    """粘回来的 `code` / `state` → token 结构。"""
    region = P.normalize_realm(realm)
    name = str(vendor or '').strip().lower()
    if name not in C.OAUTH_VENDORS:
        raise PlatformError(f'未知的 OAuth 提供方：{vendor!r}', status=400,
                            code='invalid_vendor')
    text = str(code or '').strip()
    if not text:
        raise PlatformError('回调地址里没有 code', status=400,
                            code='missing_code')
    host = P.account_hosts(region)[0]
    response = await _request(
        'POST', P.join(host, C.OAUTH_LOGIN_PATH.format(vendor=name)),
        realm=region, body={
            'source_id': 'autoclaw',
            'device_id': str(device_id or '').strip(),
            'code': text,
            'state': str(state or '').strip(),
            'navigate_uri': str(navigate_uri or ''),
        }, proxy=proxy, timeout=timeout)
    payload = _json(response)
    if response.status_code >= 400:
        raise _response_error(response, payload, 'OAuth 登录失败')
    root = _check_envelope(payload, 'OAuth 登录失败')
    out = _token_payload(root)
    out['device_id'] = out.get('device_id') or str(device_id or '').strip()
    return out
