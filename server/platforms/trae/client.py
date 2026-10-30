"""Trae 上游协议层（异步 httpx）。

边界：本模块只做「协议翻译」，不含账号池策略、签到编排与网关形状转换。
这些分别属于 `store.py` / `adapter.py` / 路由层。

参考实现：`trae2api-hub/trae_client.py`（MIT，纯标准库 urllib）。这里改用
httpx 以复用项目已有的连接池、代理与超时策略（`config.http_client`），
协议常量与请求形状保持与之一致。

**已知限制**：国际版刷新令牌时官方客户端会附带设备证明（DeviceInfo /
DeviceProof，需要 iCube 私钥）。本实现没有该私钥，因此只发基础字段——
国内版实测不需要，国际版可能需要重新导出凭据才能刷新。
"""
from __future__ import annotations

import datetime
import json
import time
from typing import Any, AsyncIterator

import httpx

from ... import config
from ..base import PlatformError
from . import fingerprint
from .constants import (
    APP_ID,
    CLIENT_ID,
    CLIENT_SECRET,
    INTL_CHAT_HEADERS,
    INTL_CHAT_UA,
    PATH_CHAT,
    PATH_CHECKIN_CLAIM,
    PATH_CHECKIN_STATUS,
    PATH_ENT_USAGE_V1,
    PATH_ENT_USAGE_V2,
    PATH_EXCHANGE_TOKEN,
    PATH_MODEL_LIST,
    PATH_PAY_STATUS,
    PATH_SESSION_USAGE,
    PLUGIN_CHANNEL,
    REALMS,
    normalize_realm,
    realm_hosts,
)

DEFAULT_TIMEOUT = 60
CHAT_READ_TIMEOUT = 300


class _NetworkError(Exception):
    """仅表示「连不上 / 传输层失败」，用于触发备选主机重试。"""


# ── 时间 ────────────────────────────────────────────────────
def parse_epoch(value: object) -> float | None:
    """ISO8601 / epoch（秒或毫秒）→ 秒级时间戳；解析不了返回 None。"""
    if value in (None, ''):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
        return seconds / 1000.0 if seconds > 1e11 else seconds
    text = str(value).strip()
    if not text:
        return None
    iso = text[:-1] + '+00:00' if text.endswith('Z') else text
    try:
        parsed = datetime.datetime.fromisoformat(iso)
    except ValueError:
        try:
            seconds = float(text)
        except ValueError:
            return None
        return seconds / 1000.0 if seconds > 1e11 else seconds
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.timestamp()


def iso_from_ms(value: object) -> str:
    """毫秒时间戳 → ISO8601（UTC，毫秒精度）。失败返回空串。"""
    try:
        ms = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ''
    if ms <= 0:
        return ''
    dt = datetime.datetime.fromtimestamp(ms / 1000.0, datetime.timezone.utc)
    return dt.strftime('%Y-%m-%dT%H:%M:%S.') + f'{ms % 1000:03d}Z'


def now_iso() -> str:
    dt = datetime.datetime.now(datetime.timezone.utc)
    return dt.strftime('%Y-%m-%dT%H:%M:%S.') + f'{dt.microsecond // 1000:03d}Z'


# ── 请求头 ──────────────────────────────────────────────────
def auth_headers(account: dict, realm: str | None = None) -> dict[str, str]:
    """IDE 数据面鉴权头 + 设备头。

    `x-ide-token` 与 `Authorization` 携带**同一个** access token（实测结论，
    不要"顺手"换成两种 token）。
    """
    realm = normalize_realm(realm or account.get('realm'))
    token = str(account.get('token') or '').strip()
    if not token:
        raise PlatformError('账号缺少 access token（先导入或刷新）',
                            status=401, code='no_token')
    headers = {
        'X-App-Id': APP_ID,
        'x-plugin-channel': PLUGIN_CHANNEL,
        'x-ide-version-code': REALMS[realm]['version_code'],
        'x-ide-token': token,
        'Authorization': f'Cloud-IDE-JWT {token}',
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'User-Agent': REALMS[realm]['ua'],
    }
    headers.update(fingerprint.device_headers(str(account.get('uid') or '')))
    headers['X-Request-Id'] = fingerprint.request_id()
    return headers


def chat_headers(account: dict, realm: str | None = None) -> dict[str, str]:
    """对话专用头。

    国际版 raw_chat 用 `Bearer` 而不是 `Cloud-IDE-JWT`，且必须带当前客户端
    版本头，否则上游回 4015（风控）。这是两个区最容易踩错的一处差异。
    """
    realm = normalize_realm(realm or account.get('realm'))
    headers = auth_headers(account, realm)
    headers['Accept'] = 'text/event-stream'
    headers['Cache-Control'] = 'no-cache'
    if realm == 'intl':
        token = str(account.get('token') or '')
        headers['Authorization'] = f'Bearer {token}'
        headers.update(INTL_CHAT_HEADERS)
        headers['User-Agent'] = INTL_CHAT_UA
    return headers


def _proxy_for(account: dict) -> str | None:
    """账号出口：绑定线路 → 代理池 → 全局代理 → 直连（与 WorkBuddy 同规则）。"""
    try:
        return config.resolve_account_proxy(account)
    except ValueError as exc:
        raise PlatformError(str(exc), status=503, code='proxy_invalid') from exc


def _http_error(status: int, text: str) -> PlatformError:
    """把上游 HTTP 错误翻成带状态码的 PlatformError。"""
    message = (text or '').strip()[:500]
    payload: Any = None
    code = 'upstream_error'
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            payload = parsed
            message = str(parsed.get('message') or parsed.get('Message')
                          or parsed.get('msg') or message)[:500]
            raw_code = parsed.get('code') or parsed.get('Code')
            if raw_code not in (None, ''):
                code = str(raw_code)
    except (ValueError, TypeError):
        parsed = None
    if status in (401, 403):
        out_status = status
    elif status == 429:
        out_status = 429
    elif 400 <= status < 500:
        out_status = 400
    else:
        out_status = 502
    return PlatformError(message or f'上游返回 HTTP {status}', status=out_status,
                         code=code, payload=payload)


# ── 基础请求 ────────────────────────────────────────────────
async def _send(account: dict, realm: str, method: str, url: str, body: Any,
                timeout: float, headers: dict[str, str], *, auth: bool) -> tuple[int, str]:
    proxy = _proxy_for(account)
    send_headers = auth_headers(account, realm) if auth else {'Content-Type': 'application/json'}
    send_headers.update(headers or {})
    content = None if body is None else json.dumps(body, ensure_ascii=False).encode('utf-8')
    async with config.http_client(timeout, connect=5, proxy=proxy) as client:
        request = client.build_request(method, url, headers=send_headers, content=content)
        try:
            response = await client.send(request)
        except httpx.TransportError as exc:
            raise _NetworkError(str(exc)) from exc
        return response.status_code, response.text


async def _request_hosts(account: dict, realm: str, hosts: tuple[str, ...],
                         path: str, body: Any, timeout: float,
                         headers: dict[str, str], *, method: str = 'POST',
                         auth: bool = True) -> Any:
    last_exc: Exception | None = None
    for host in hosts:
        try:
            status, text = await _send(account, realm, method, host + path, body,
                                       timeout, headers, auth=auth)
        except _NetworkError as exc:
            last_exc = exc
            continue
        if status >= 400:
            # 业务错误（403/401/429）换主机重放没有意义，立刻抛出。
            raise _http_error(status, text)
        text = (text or '').strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except ValueError as exc:
            raise PlatformError(f'上游返回非 JSON：{text[:200]}', status=502,
                                code='bad_json', payload=text[:500]) from exc
    raise PlatformError(f'Trae 上游不可达：{last_exc or "无可用主机"}',
                        status=502, code='upstream_unreachable')


async def request_json(account: dict, path: str, body: Any = None, *,
                       realm: str | None = None, base: str = 'core',
                       timeout: float = DEFAULT_TIMEOUT, method: str = 'POST',
                       headers: dict[str, str] | None = None) -> Any:
    realm = normalize_realm(realm or account.get('realm'))
    return await _request_hosts(account, realm, realm_hosts(realm, base), path,
                                body, timeout, headers or {}, method=method)


# ── 令牌刷新 ────────────────────────────────────────────────
async def exchange_token(account: dict, timeout: float = 30) -> dict:
    """ExchangeToken 轮换刷新，返回**待落盘**的新凭据。

    上游每次返回新的 RefreshToken，旧的立即失效——调用方必须在返回后立刻
    原子落盘（见 `store.save_account`），否则下次刷新必然失败。
    """
    realm = normalize_realm(account.get('realm'))
    refresh = str(account.get('refreshToken') or '').strip()
    if not refresh:
        raise PlatformError('账号缺少 refreshToken，无法刷新', status=400,
                            code='no_refresh_token')
    payload = {
        'ClientID': CLIENT_ID,
        'RefreshToken': refresh,
        'ClientSecret': CLIENT_SECRET,
        'UserID': str(account.get('uid') or ''),
    }
    parsed = await _request_hosts(account, realm, realm_hosts(realm, 'account'),
                                  PATH_EXCHANGE_TOKEN, payload, timeout, {}, auth=False)
    result = parsed.get('Result') if isinstance(parsed, dict) else None
    if not isinstance(result, dict):
        result = parsed if isinstance(parsed, dict) else {}
    token = result.get('Token') or result.get('token')
    if not token:
        message = (str((parsed or {}).get('Message') or (parsed or {}).get('message')
                       or 'ExchangeToken 未返回 Token') if isinstance(parsed, dict)
                   else 'ExchangeToken 未返回 Token')
        raise PlatformError(message, status=401, code='refresh_failed', payload=parsed)
    return {
        'token': str(token),
        'refreshToken': str(result.get('RefreshToken')
                            or result.get('refresh_token') or refresh),
        'expiredAt': iso_from_ms(result.get('TokenExpireAt')),
        'refreshExpiredAt': iso_from_ms(result.get('RefreshExpireAt')),
        'userJwt': str(result.get('UserJwt') or ''),
        'realm': realm,
        'refreshedAt': now_iso(),
    }


# ── 对话 ────────────────────────────────────────────────────
_TEXT_OPTIONS = ('temperature', 'top_p', 'max_tokens', 'stop',
                 'presence_penalty', 'frequency_penalty')

_ROLE_ALIASES = {'human': 'user', 'ai': 'assistant', 'model': 'assistant'}


def _text_part(text: object) -> dict:
    return {'type': 'text', 'text': '' if text is None else str(text)}


def normalize_content(content: object) -> list[dict]:
    """把任意 content 变成 Trae 认的 parts 数组。

    上游只认 `[{"type":"text","text":...}]`；直接传字符串会 400（4001）。
    """
    if content is None:
        return [_text_part('')]
    if isinstance(content, str):
        return [_text_part(content)]
    if isinstance(content, dict):
        content = [content]
    if isinstance(content, (list, tuple)):
        parts: list[dict] = []
        for item in content:
            if isinstance(item, str):
                parts.append(_text_part(item))
            elif isinstance(item, dict):
                if 'text' in item and not item.get('type'):
                    parts.append(_text_part(item.get('text')))
                elif item.get('type') == 'text':
                    parts.append(_text_part(item.get('text')))
                else:
                    # 图片等非文本 part 原样透传（多模态由账号/模型决定）
                    parts.append(dict(item))
        return parts or [_text_part('')]
    return [_text_part(content)]


def normalize_messages(messages: object) -> list[dict]:
    """统一成 `[{role, content:[parts]}]`；未知 role 归一到 user。"""
    if isinstance(messages, dict):
        messages = [messages]
    if not isinstance(messages, (list, tuple)) or not messages:
        raise PlatformError('messages 不能为空', status=400, code='invalid_messages')
    out: list[dict] = []
    for index, raw in enumerate(messages):
        if isinstance(raw, str):
            out.append({'role': 'user', 'content': [_text_part(raw)]})
            continue
        if not isinstance(raw, dict):
            raise PlatformError(f'messages[{index}] 不是对象', status=400,
                                code='invalid_messages')
        role = str(raw.get('role') or 'user').strip().lower()
        role = _ROLE_ALIASES.get(role, role)
        if role not in ('system', 'user', 'assistant'):
            role = 'user'
        out.append({'role': role, 'content': normalize_content(raw.get('content'))})
    return out


def build_chat_body(model: str, messages: object, body: dict) -> dict:
    """构造 llm_raw_chat 请求体。

    只透传**实测不会触发 4001** 的采样参数；未在协议里记录的字段一律不发。
    """
    model_name = str(model or '').strip()
    if not model_name:
        raise PlatformError('model 不能为空', status=400, code='invalid_model')
    payload: dict[str, Any] = {
        'model_name': model_name,
        'messages': normalize_messages(messages),
        'stream': True,          # 服务端始终 SSE，这里固定 true
    }
    for key in _TEXT_OPTIONS:
        value = body.get(key)
        if value is not None:
            payload[key] = value
    return payload


async def open_chat(account: dict, body: dict, *, realm: str | None = None,
                    timeout: float = CHAT_READ_TIMEOUT) -> httpx.Response:
    """POST llm_raw_chat，返回**未读取**的流式响应（调用方负责关闭）。"""
    realm = normalize_realm(realm or account.get('realm'))
    headers = chat_headers(account, realm)
    proxy = _proxy_for(account)
    hosts = realm_hosts(realm, 'core')
    last_exc: Exception | None = None
    timeout_obj = httpx.Timeout(timeout, connect=5)
    async with config.http_client(timeout_obj, proxy=proxy) as client:
        for host in hosts:
            request = client.build_request('POST', host + PATH_CHAT, headers=headers,
                                           content=json.dumps(body, ensure_ascii=False)
                                           .encode('utf-8'))
            try:
                response = await client.send(request, stream=True)
            except httpx.TransportError as exc:
                last_exc = exc
                continue
            if response.status_code >= 400:
                text = (await response.aread()).decode('utf-8', errors='replace')
                await response.aclose()
                raise _http_error(response.status_code, text)
            return response
    raise PlatformError(f'Trae 对话上游不可达：{last_exc or "无可用主机"}',
                        status=502, code='upstream_unreachable')


def _parse_event_data(raw: str) -> Any:
    text = (raw or '').strip()
    if text == '[DONE]':
        return {'finish_reason': 'stop'}
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return text


async def iter_events(response: httpx.Response) -> AsyncIterator[tuple[str, Any]]:
    """逐帧产出 `(event, data)`。

    兼容 CRLF、多行 data、注释行与 `data: [DONE]`。`data` 能解析成 JSON 就给
    dict，否则保留原始字符串——不要在解析失败时丢掉这一帧，正文可能就在里面。
    """
    event: str | None = None
    data_lines: list[str] = []
    async for line in response.aiter_lines():
        if line == '':
            if data_lines:
                yield event or 'message', _parse_event_data('\n'.join(data_lines))
                event, data_lines = None, []
            continue
        if line.startswith(':'):
            continue
        field, _, value = line.partition(':')
        field = field.strip()
        if value.startswith(' '):
            value = value[1:]
        if field == 'event':
            event = value.strip()
        elif field == 'data':
            data_lines.append(value)
        # id / retry 与本协议无关，忽略
    if data_lines:
        yield event or 'message', _parse_event_data('\n'.join(data_lines))


# ── 任务 ────────────────────────────────────────────────────
def _checkin_req_source(realm: str) -> int:
    """客户端：国内版 1、国际版 2。"""
    return 2 if normalize_realm(realm) == 'intl' else 1


async def checkin_status(account: dict, *, realm: str | None = None,
                         timeout: float = 30) -> dict:
    realm = normalize_realm(realm or account.get('realm'))
    data = await request_json(account, PATH_CHECKIN_STATUS,
                              {'req_source': _checkin_req_source(realm)},
                              realm=realm, base='ug', timeout=timeout)
    return data if isinstance(data, dict) else {}


async def claim_checkin(account: dict, *, realm: str | None = None,
                        timeout: float = 30) -> dict:
    """领取签到（幂等），用领取前后的 credits 差额计算真实到账。"""
    realm = normalize_realm(realm or account.get('realm'))
    before = await checkin_status(account, realm=realm, timeout=timeout)
    before_credits = int(before.get('credits') or 0)
    if before.get('checked_in'):
        return {'ok': True, 'checked_in': True, 'already': True,
                'credits': before_credits, 'earned_credit': 0,
                'message': f'今日已签到（积分 {before_credits}）',
                'raw': before}
    raw = await request_json(account, PATH_CHECKIN_CLAIM,
                             {'req_source': _checkin_req_source(realm)},
                             realm=realm, base='ug', timeout=timeout)
    after = await checkin_status(account, realm=realm, timeout=timeout)
    after_credits = int(after.get('credits') or 0)
    ok = bool(after.get('checked_in')) and (
        not isinstance(raw, dict) or raw.get('code') in (0, None))
    if ok:
        message = f'签到成功（积分 {after_credits}）'
    else:
        # 「接口 200」不等于「已到账」：优先用状态接口的说明，其次领取响应，
        # 都没有就给一句中性的，绝不把失败说成成功。
        message = str(after.get('message') or
                      (raw.get('message') if isinstance(raw, dict) else '') or
                      '签到未生效')
    return {
        'ok': ok,
        'checked_in': bool(after.get('checked_in')),
        'already': False,
        'credits': after_credits,
        'earned_credit': max(0, after_credits - before_credits),
        'message': message,
        'raw': {'claim': raw, 'status': after},
    }


# ── 用量 / 权益 ─────────────────────────────────────────────
async def fetch_usage(account: dict, *, realm: str | None = None,
                      timeout: float = 45) -> dict:
    data = await request_json(account, PATH_SESSION_USAGE, {},
                              realm=realm, base='core', timeout=timeout)
    return data if isinstance(data, dict) else {}


async def fetch_entitlement(account: dict, *, realm: str | None = None,
                            timeout: float = 45, strict: bool = False) -> dict:
    """权益包 / 付费状态；任一失败只记进 errors，不打断主链路。"""
    realm = normalize_realm(realm or account.get('realm'))
    out: dict[str, Any] = {'realm': realm, 'ent_usage': None,
                           'pay_status': None, 'errors': []}
    ent_path = PATH_ENT_USAGE_V2 if realm == 'cn' else PATH_ENT_USAGE_V1
    for key, path in (('ent_usage', ent_path), ('pay_status', PATH_PAY_STATUS)):
        try:
            out[key] = await request_json(account, path, {}, realm=realm,
                                          base='pay', timeout=timeout)
        except PlatformError as exc:
            if strict:
                raise
            out['errors'].append({'endpoint': key, 'code': exc.code,
                                  'message': exc.message})
    return out


async def fetch_model_list(account: dict, *, realm: str | None = None,
                           timeout: float = 45) -> list[dict]:
    """model_list（仅国内版可用；国际版 403）。"""
    realm = normalize_realm(realm or account.get('realm'))
    if not REALMS[realm]['model_list_supported']:
        raise PlatformError(f'{REALMS[realm]["label"]}不支持 model_list',
                            status=403, code='model_list_unsupported')
    payload = await request_json(account, PATH_MODEL_LIST, {'type': 'chat'},
                                 realm=realm, base='core', timeout=timeout)
    models: Any = []
    if isinstance(payload, dict):
        models = payload.get('model_configs') or payload.get('models') or []
    elif isinstance(payload, list):
        models = payload
    return [m for m in models if isinstance(m, dict)]


def elapsed_ms(started: float) -> int:
    return int((time.time() - started) * 1000)
