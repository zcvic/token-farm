"""Trae 上游协议层（异步 httpx）。

边界：本模块只做「协议翻译」，不含账号池策略、签到编排与网关形状转换。
这些分别属于 `store.py` / `adapter.py` / 路由层。

参考实现：`trae2api-hub/trae_client.py`（MIT，纯标准库 urllib）。这里改用
httpx 以复用项目已有的连接池、代理与超时策略（`config.http_client`），
协议常量与请求形状保持与之一致。

**国际版的设备证明**：官方客户端刷新国际版令牌时会附带 `DeviceInfo` /
`DeviceProof`（ECDSA-P256 签名，私钥来自客户端的 `iCubeAuthInfo://icube-dc`）。
本实现按客户端字段结构补齐（见 `deviceproof.py`），私钥优先取账号自带的
`deviceKeyPEM`，其次从本机 Trae 客户端导入；两处都没有时不带该字段——
国内版实测不需要，国际版会如实报上游错误，不会假装刷新成功。
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import time
from typing import Any, AsyncIterator

import httpx

from ... import config
from ..base import PlatformError
from . import chat as chatmod
from . import desktop, deviceproof, fingerprint
from .constants import (
    APP_ID,
    CLIENT_ID,
    CLIENT_SECRET,
    IDE_VERSION,
    INTL_CHAT_HEADERS,
    INTL_CHAT_UA,
    INTL_IDE_VERSION,
    PATH_AGENT_CHAT,
    PATH_CHAT,
    PATH_CHECKIN_CLAIM,
    PATH_CHECKIN_STATUS,
    PATH_ENT_USAGE_V1,
    PATH_ENT_USAGE_V2,
    PATH_EXCHANGE_TOKEN,
    PATH_GET_USER_INFO,
    PATH_INTL_ENTITLEMENT,
    PATH_INTL_EXCHANGE_TOKEN,
    PATH_MODEL_LIST,
    PATH_PAY_STATUS,
    PATH_SESSION_USAGE,
    PATH_USAGE_GROUP,
    PLUGIN_CHANNEL,
    REALMS,
    SOLO_FUNCTION,
    USAGE_TYPE_CREDITS,
    normalize_realm,
    realm_hosts,
)

DEFAULT_TIMEOUT = 60
CHAT_READ_TIMEOUT = 300
# 流式空闲超时：这么久一个字节都没收到就判定上游卡死。httpx 的 read timeout
# 是按「单次读」算的，300 秒的空等对客户端来说等于挂死；参考实现用 60 秒。
IDLE_TIMEOUT = 60

logger = logging.getLogger('workbuddy.platforms.trae')


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


def _error_fields(parsed: dict) -> tuple[str, str]:
    """从上游错误体里取 `(message, code)`。

    Trae 的错误体有三种形状，同一个网关的不同接口各写一种（都实测见过）：

    ```
    {"message": "..."}                                     # 网关通用
    {"ResponseMetadata": {"Error": {"Code", "Message"}}}   # 业务错误
    {"ResponseMetadata": {"Error": {"Code",                 # 更老的接口
                                   "Data": {"__Message.error": "..."}}}}
    ```

    以前只认顶层 `message`，于是设备证明 / refreshToken 失效这类业务错误会被
    原样塞进界面（一整串 JSON，用户没法看）。这里按优先级往下找，
    顶层字段仍然优先，与旧行为兼容。
    """
    message = str(parsed.get('message') or parsed.get('Message')
                  or parsed.get('msg') or '')
    code = str(parsed.get('code') or parsed.get('Code') or '')
    meta = parsed.get('ResponseMetadata')
    error = meta.get('Error') if isinstance(meta, dict) else None
    if isinstance(error, dict):
        message = message or str(error.get('Message') or '')
        code = code or str(error.get('Code') or '')
        data = error.get('Data')
        if not message and isinstance(data, dict):
            message = str(data.get('__Message.error') or '')
    return message.strip(), code.strip()


def _http_error(status: int, text: str) -> PlatformError:
    """把上游 HTTP 错误翻成带状态码的 PlatformError。"""
    message = (text or '').strip()[:500]
    payload: Any = None
    code = 'upstream_error'
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            payload = parsed
            found, raw_code = _error_fields(parsed)
            message = (found or message)[:500]
            if raw_code:
                code = raw_code
    except (ValueError, TypeError):
        parsed = None
    # 4008（免费额度用尽）在上游可能是 400/403，也可能藏在 body 里；统一改判
    # 成 429 + Retry-After，别让客户端把它当成鉴权/风控错误去换 token。
    if chatmod.is_quota_error({'code': code, 'message': message}):
        return chatmod.error_from_event({'code': code, 'message': message},
                                        payload=payload)
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
def _resolve_device(account: dict) -> dict | None:
    """国际版刷新用的设备私钥：账号自带优先，其次本机客户端（同区）。

    返回整条设备记录而不是只有 PEM：DeviceInfo 里的 `DeviceID` 必须用
    私钥的 holderId、`MachineID` 必须用 `telemetry.machineId`，从 uid 派生
    会被上游判成设备不匹配（20405）。
    """
    pem = str(account.get('deviceKeyPEM') or account.get('privateKeyPEM') or '')
    if pem.strip():
        return {
            'privateKeyPEM': pem.strip(),
            'publicKeyPEM': str(account.get('devicePublicKeyPEM') or ''),
            'holderId': str(account.get('deviceId')
                            or account.get('holderId') or ''),
            'machineId': str(account.get('machineId') or ''),
        }
    devices = [d for d in desktop.read_devices() if d.get('realm') == 'intl']
    if not devices:
        return None
    wanted = str(account.get('deviceId') or account.get('holderId') or '')
    if wanted:
        for item in devices:
            if str(item.get('holderId') or '') == wanted:
                return item
    # 账号里的 uid 与 holderId 是两套编号；客户端换过账号、uid 对不上是常见
    # 情况，直接用同区第一把（一台机器通常只有一个 icube-dc）。
    return devices[0]


def _device_proof(account: dict, refresh: str) -> dict[str, Any] | None:
    """国际版 ExchangeToken 的 DeviceInfo / DeviceProof；拿不到私钥返回 None。"""
    device = _resolve_device(account)
    if not device:
        return None
    pem = str(device.get('privateKeyPEM') or '')
    if not pem:
        return None
    try:
        return deviceproof.build_device_proof(
            account, path=PATH_INTL_EXCHANGE_TOKEN, client_id=CLIENT_ID,
            refresh_token=refresh, private_key_pem=pem,
            device_id=str(device.get('holderId') or ''),
            machine_id=str(device.get('machineId') or ''),
            public_key_pem=str(device.get('publicKeyPEM') or ''),
            ide_version=INTL_IDE_VERSION)
    except (ValueError, TypeError, IndexError) as exc:
        # 签名失败不阻断刷新：照着基础字段发一次，让上游给出真实原因。
        logger.warning('DeviceProof 签名失败：%s', exc)
        return None


def _token_result(parsed: Any, realm: str, fallback_refresh: str = '') -> dict:
    """把 ExchangeToken 的响应归一化成待落盘的凭据。"""
    result = parsed.get('Result') if isinstance(parsed, dict) else None
    if not isinstance(result, dict):
        result = parsed if isinstance(parsed, dict) else {}
    token = result.get('Token') or result.get('token')
    if not token:
        found, _ = _error_fields(parsed) if isinstance(parsed, dict) else ('', '')
        raise PlatformError(found or 'ExchangeToken 未返回 Token', status=401,
                            code='refresh_failed', payload=parsed)
    return {
        'token': str(token),
        'refreshToken': str(result.get('RefreshToken')
                            or result.get('refresh_token') or fallback_refresh),
        'expiredAt': iso_from_ms(result.get('TokenExpireAt')),
        'refreshExpiredAt': iso_from_ms(result.get('RefreshExpireAt')),
        'userJwt': str(result.get('UserJwt') or ''),
        'realm': realm,
        'refreshedAt': now_iso(),
    }


async def exchange_token(account: dict, timeout: float = 30) -> dict:
    """ExchangeToken 轮换刷新，返回**待落盘**的新凭据。

    上游每次返回新的 RefreshToken，旧的立即失效——调用方必须在返回后立刻
    原子落盘（见 `store.save_account`），否则下次刷新必然失败。

    两个区的请求形状不同（2026-11-01 用真实 intl 账号验过）：
      · 国内版：旧路径 `/cloudide/api/v3/trae/oauth/ExchangeToken`，
        `ClientSecret: "-"`，不需要设备证明；
      · 国际版：新路径 `/trae/api/v3/oauth/ExchangeToken`，
        `ClientSecret: ""`，必须带 `DeviceInfo` / `DeviceProof` /
        `IDEVersion`；签名原文用秒级时间戳 + `\\n` 连接。
    """
    realm = normalize_realm(account.get('realm'))
    refresh = str(account.get('refreshToken') or '').strip()
    if not refresh:
        raise PlatformError('账号缺少 refreshToken，无法刷新', status=400,
                            code='no_refresh_token')
    token = str(account.get('token') or '').strip()
    if realm == 'intl':
        payload: dict[str, Any] = {
            'ClientID': CLIENT_ID,
            'ClientSecret': '',
            'RefreshToken': refresh,
            'IDEVersion': INTL_IDE_VERSION,
        }
        proof = _device_proof(account, refresh)
        if proof:
            payload.update(proof)
        headers = {'x-cloudide-token': token} if token else {}
        parsed = await _request_hosts(account, realm, realm_hosts(realm, 'account'),
                                      PATH_INTL_EXCHANGE_TOKEN, payload, timeout,
                                      headers, auth=False)
    else:
        payload = {
            'ClientID': CLIENT_ID,
            'RefreshToken': refresh,
            'ClientSecret': CLIENT_SECRET,
            'UserID': str(account.get('uid') or ''),
        }
        proof = None
        parsed = await _request_hosts(account, realm, realm_hosts(realm, 'account'),
                                      PATH_EXCHANGE_TOKEN, payload, timeout, {},
                                      auth=False)
    return {**_token_result(parsed, realm, refresh), 'deviceProofUsed': bool(proof)}


async def exchange_token_by_auth_code(account: dict, auth_code: str,
                                      code_verifier: str,
                                      timeout: float = 30) -> dict:
    """用授权页回调里的 `AuthCode` + 本机 PKCE verifier 换令牌（国际版）。

    这就是国际版客户端自己的登录兑换：请求体只有
    `ClientID / AuthCode / CodeVerifier / DeviceInfo / IDEVersion`，
    **不带 `DeviceProof`**（客户端本身也不签），但 `DeviceInfo` 仍要带设备公钥，
    上游据此把这次登录绑到同一台设备上。

    `AuthCode` 一次性且很快过期，兑换必须紧接着回调完成。
    """
    realm = normalize_realm(account.get('realm'))
    if realm != 'intl':
        raise PlatformError('只有国际版支持授权码兑换', status=400,
                            code='auth_code_unsupported')
    code = str(auth_code or '').strip()
    verifier = str(code_verifier or '').strip()
    if not code or not verifier:
        raise PlatformError('授权码或 PKCE verifier 缺失，无法兑换', status=400,
                            code='no_auth_code')
    device = _resolve_device(account)
    if not device:
        raise PlatformError(
            '账号缺少国际版设备密钥（deviceKeyPEM），无法完成授权码兑换。'
            '请重新走一次网页登录，或导入带设备密钥的账号。',
            status=400, code='login_device_key_missing')
    payload: dict[str, Any] = {
        'ClientID': CLIENT_ID,
        'AuthCode': code,
        'CodeVerifier': verifier,
        'DeviceInfo': deviceproof.device_info(
            str(account.get('uid') or ''),
            device_id=str(device.get('holderId') or ''),
            machine_id=str(device.get('machineId') or ''),
            public_key_pem=str(device.get('publicKeyPEM') or ''),
            ide_version=INTL_IDE_VERSION),
        'IDEVersion': INTL_IDE_VERSION,
    }
    parsed = await _request_hosts(account, realm, realm_hosts(realm, 'account'),
                                  PATH_INTL_EXCHANGE_TOKEN, payload, timeout,
                                  {'x-cloudide-token': ''}, auth=False)
    fresh = _token_result(parsed, realm)
    if not fresh.get('refreshToken'):
        # 没有 refreshToken 的账号只能用到 access token 过期，等于登录白做。
        raise PlatformError('ExchangeToken 未返回 RefreshToken，无法保存账号',
                            status=502, code='no_refresh_token', payload=parsed)
    return {**fresh, 'deviceProofUsed': False, 'authCodeUsed': True}


def region_of(payload: object) -> str:
    """从 GetUserInfo / ExchangeToken 的响应里取账号所在区（`US`、`SG`…）。

    国际版的积分主机按区选择，但 `region` 藏在嵌套结构里（`Result`、
    `UserInfo`、`userRegion`），不同端点层级还不一样；这里统一做一次深度
    查找，找不到返回空串（调用方按 SG 处理）。
    """
    if not isinstance(payload, dict):
        return ''
    queue: list[dict] = [payload]
    seen: set[int] = set()
    while queue and len(seen) < 8:
        node = queue.pop(0)
        if id(node) in seen:
            continue
        seen.add(id(node))
        for key in ('Region', 'AIRegion', 'AiRegion', 'region', 'aiRegion',
                    'userRegion'):
            value = node.get(key)
            if isinstance(value, dict):
                value = value.get('region') or value.get('_aiRegion') \
                    or value.get('aiRegion')
            if isinstance(value, str) and value.strip():
                return value.strip()
        for key in ('Result', 'Data', 'UserInfo', 'result', 'data', 'userInfo'):
            child = node.get(key)
            if isinstance(child, dict):
                queue.append(child)
    return ''


async def fetch_user_info(account: dict, token: str, *, realm: str | None = None,
                          timeout: float = 30) -> dict:
    """GetUserInfo：用 access token 取账号资料（网页登录落盘时用）。

    上游形状：`{Result: {Data: {UserInfo: {...}}, HostInfo: {...}}}`。
    与 ExchangeToken 一样走 `ug` 主机（国内版 `api.trae.cn`）。
    """
    realm = normalize_realm(realm or account.get('realm'))
    headers = {
        'Content-Type': 'application/json',
        'x-cloudide-token': str(token or ''),
    }
    # 国际版客户端上报的是当前版本（3.6.x）；国内版保持历史值。
    ide_version = INTL_IDE_VERSION if realm == 'intl' else IDE_VERSION
    body = {'ReqSource': 'IDE', 'IDEVersion': ide_version}
    parsed = await _request_hosts(account, realm, realm_hosts(realm, 'ug'),
                                  PATH_GET_USER_INFO, body, timeout, headers,
                                  auth=False)
    return parsed if isinstance(parsed, dict) else {}


# ── 对话 ────────────────────────────────────────────────────
_TEXT_OPTIONS = ('temperature', 'top_p', 'max_tokens', 'stop',
                 'presence_penalty', 'frequency_penalty')

# 思考强度（reasoning effort）。
#
# 客户端常见的传法有四种：`reasoning_effort`、`reasoning.effort`、
# `thinking.effort|level`，以及 `thinking: false`（要求关闭思考）。Trae 官方
# 模型认的是 `reasoning_effort_level`，取值 light / high / extra_high / none
# （见 trae2api-hub `build_trae_body`）。外部档位（low / medium / xhigh …）
# 统一归一到这三档：客户端不可能精确知道上游有哪些档，强行原样透传只会被
# 上游按默认值处理，用户看到的就是「设了也没用」。
_EFFORT_OFF = frozenset({'none', 'off', 'disabled', 'disable', 'false',
                         'no', '0'})
_EFFORT_LEVELS = {
    'minimal': 'light', 'low': 'light', 'light': 'light', 'lite': 'light',
    'medium': 'high', 'mid': 'high', 'high': 'high', 'default': 'high',
    'xhigh': 'extra_high', 'extra-high': 'extra_high',
    'extra_high': 'extra_high', 'max': 'extra_high', 'maximum': 'extra_high',
}


def requested_effort(body: dict) -> str | None:
    """从请求体里取客户端声明的思考强度（未归一）。"""
    effort = body.get('reasoning_effort')
    if not effort and isinstance(body.get('reasoning'), dict):
        effort = body['reasoning'].get('effort')
    thinking = body.get('thinking')
    if not effort and isinstance(thinking, dict):
        # Anthropic 风格用 `{"type": "disabled"}`，OpenAI 兼容层常混用。
        effort = (thinking.get('effort') or thinking.get('level')
                  or thinking.get('type'))
    if not effort and thinking is False:
        effort = 'none'
    if isinstance(effort, str):
        effort = effort.strip()
    return str(effort) if effort else None


def normalize_effort(effort: object) -> str | None:
    """外部档位 → Trae `reasoning_effort_level`；认不出就返回 None（不发）。

    认不出的值一律丢弃，不猜：发一个上游不认识的档位等于让模型回默认值，
    反而掩盖了客户端配置问题。
    """
    low = str(effort or '').strip().lower()
    if not low:
        return None
    if low in _EFFORT_OFF:
        return 'none'
    return _EFFORT_LEVELS.get(low)


def _attach_effort(payload: dict, body: dict, *, nested: bool = False) -> None:
    """挂思考强度：raw chat 放 `parameters`，solo 是平铺字段。"""
    if not config.TRAE_REASONING_EFFORT:
        return
    level = normalize_effort(requested_effort(body))
    if not level:
        return
    if nested:
        payload.setdefault('parameters', {})['reasoning_effort_level'] = level
    else:
        payload['reasoning_effort_level'] = level


_ROLE_ALIASES = {'human': 'user', 'ai': 'assistant', 'model': 'assistant'}

# ── 工具调用（tools / tool_choice）──────────────────────────
#
# 形状是**实测**出来的（2026-11-01，国内版 Agent 通道 `llm_utils_chat`，
# deepseek-v4.1-flash），不是照着 OpenAI 文档猜的。三条硬约束：
#
#   1. `function.parameters` 必须是 **JSON 字符串**。传对象会被 Go 侧拒绝：
#      `bad request: json: cannot unmarshal object into Go struct field
#      FunctionDefinition.tools.function.parameters of type string`；
#   2. `tool_choice` 必须是 **字符串**（`auto` / `none` / `required`）。传
#      `{type:"function",...}` 会被拒绝：
#      `cannot unmarshal object into Go struct field LLMUtilsChatRequest.tool_choice
#      of type string`；
#   3. 消息层面**只认 `role` + `content` parts**：`tool_calls`、
#      `tool_call_id`、`function_call` 会被上游静默丢掉，`role:"tool"` 被当成
#      `user`。所以历史里的工具调用只能**折成正文**（见 `_fold_tool_history`），
#      否则第二轮请求里模型看不到自己调过什么。
#
# 返回侧同样是私有形状：`function_call`（不是 `function`）、参数按 `index`
# 分片增量下发，转换在 `chat.py`。

_MAX_TOOLS = 128


def normalize_tools(tools: object) -> list[dict]:
    """把 `tools` 归一成上游认的形状；认不出的一律丢掉。

    函数只保留 `name` / `description` / `parameters` 三个键，`parameters`
    统一序列化成字符串（见上）。`strict`、`additionalProperties` 这类客户端
    爱加、上游结构体里没有的键不往外发。
    """
    if isinstance(tools, dict):
        tools = [tools]
    if not isinstance(tools, (list, tuple)):
        return []
    out: list[dict] = []
    for raw in tools:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get('type') or 'function').strip().lower()
        if kind != 'function':
            continue
        fn = raw.get('function')
        if not isinstance(fn, dict):
            fn = raw  # 扁平形状：{type:"function", name, parameters}
        name = str(fn.get('name') or '').strip()
        if not name:
            continue
        item: dict[str, Any] = {'type': 'function', 'function': {'name': name}}
        description = fn.get('description')
        if isinstance(description, str) and description:
            item['function']['description'] = description
        parameters = _parameters_text(fn.get('parameters'))
        if parameters is not None:
            item['function']['parameters'] = parameters
        out.append(item)
        if len(out) >= _MAX_TOOLS:
            break
    return out


def _parameters_text(value: object) -> str | None:
    """`parameters` → JSON 字符串。已经是字符串就校验一次，坏 JSON 直接丢。

    宁可少一个工具定义，也不要发一段上游解析不了的字符串——那会让整轮
    请求变成 `4001`，而不是「这个工具不可用」。
    """
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            json.loads(text)
        except (TypeError, ValueError):
            return None
        return text
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return None
    return None


def normalize_tool_choice(choice: object) -> str | None:
    """只转发字符串档位；`{type:"function",...}` 这类对象上游不认（见上）。"""
    if not isinstance(choice, str):
        return None
    value = choice.strip().lower()
    return value if value in ('auto', 'none', 'required') else None


def tool_names(tools: object) -> list[str]:
    """本轮声明的工具名（归一后）。给正文还原的标签形态做白名单用。"""
    return [item['function']['name'] for item in normalize_tools(tools)]


def _fold_tool_history(message: dict) -> str:
    """把一条消息里的工具痕迹折成正文。

    上游丢弃 `tool_calls` / `tool_call_id`，多轮 Agent 请求因此会「失忆」：
    模型看不到自己上一轮调过哪个工具、结果是什么。折成纯文本是目前唯一
    能穿过这条通道的表达方式。

    前缀刻意用 `[历史调用]` 而不是 `[tool_call]`：后者正好是正文还原器认的
    调用语法，模型**复述历史**时会把这一行原样念出来，于是上一轮的调用被当成
    这一轮的新调用执行（2026-11-04 线上样本）。历史是记录，不是请求，格式上
    就不能长得像可执行调用。
    """
    lines: list[str] = []
    calls = message.get('tool_calls')
    if isinstance(calls, (list, tuple)):
        for raw in calls:
            if not isinstance(raw, dict):
                continue
            fn = raw.get('function')
            fn = fn if isinstance(fn, dict) else {}
            name = str(fn.get('name') or '').strip()
            if not name:
                continue
            args = fn.get('arguments')
            if not isinstance(args, str):
                try:
                    args = json.dumps(args if args is not None else {},
                                      ensure_ascii=False)
                except (TypeError, ValueError):
                    args = '{}'
            lines.append(f'[历史调用] {name}({args})')
    single = message.get('function_call')
    if isinstance(single, dict):
        name = str(single.get('name') or '').strip()
        if name:
            args = single.get('arguments')
            if not isinstance(args, str):
                try:
                    args = json.dumps(args if args is not None else {},
                                      ensure_ascii=False)
                except (TypeError, ValueError):
                    args = '{}'
            lines.append(f'[历史调用] {name}({args})')
    return '\n'.join(lines)


def _attach_tools(payload: dict, tools: list[dict], body: dict) -> None:
    """把归一后的工具字段挂到请求体上；没有工具就什么都不加。"""
    if not tools:
        return
    payload['tools'] = tools
    choice = normalize_tool_choice(body.get('tool_choice'))
    if choice is not None:
        payload['tool_choice'] = choice


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
    """统一成 `[{role, content:[parts]}]`；未知 role 归一到 user。

    工具痕迹按上游能理解的方式折叠（见 `_fold_tool_history`）：

      · `assistant.tool_calls` / `function_call` → 正文里的 `[历史调用] …`；
      · `role:"tool"` / `role:"function"` → 正文里的 `[历史结果] …`，role 归到 user。

    不折叠的话这些字段会被上游静默丢掉（实测），第二轮请求里模型看不到自己
    上一轮调过什么，工具链第二轮就答非所问。
    """
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
        parts = normalize_content(raw.get('content'))
        if role in ('tool', 'function'):
            name = str(raw.get('name') or '').strip()
            label = (f'[历史结果] {name}:'
                     if role == 'function' and name else '[历史结果]')
            parts = _prefix_text(parts, label)
            role = 'user'
        else:
            folded = _fold_tool_history(raw)
            if folded:
                parts.append(_text_part(folded))
        if role not in ('system', 'user', 'assistant'):
            role = 'user'
        item: dict[str, Any] = {'role': role, 'content': parts}
        if role == 'assistant':
            # 历史里的思考痕迹不能直接丢：DeepSeek 系模型要求 assistant
            # 历史带 reasoning_content（参考实现 backfill_reasoning_content）。
            # 上游对未知字段是静默忽略，带上不会破坏非思考模型。
            reasoning = raw.get('reasoning_content')
            if reasoning is None:
                reasoning = raw.get('reasoning')
            if reasoning:
                item['reasoning_content'] = (
                    reasoning if isinstance(reasoning, str)
                    else str(reasoning))
        out.append(item)
    return out


def _prefix_text(parts: list[dict], prefix: str) -> list[dict]:
    """给 parts 的正文加一行前缀；首个 part 不是文本时单独插一节。"""
    if parts and parts[0].get('type') == 'text':
        first = dict(parts[0])
        first['text'] = f'{prefix} {str(first.get("text") or "")}'.rstrip()
        return [first, *parts[1:]]
    return [_text_part(prefix), *parts]


def _solo_parts(content: object) -> list[dict]:
    """SOLO 消息的 content parts；空字符串 / null 返回空列表。

    与 IDE 通道不同，SOLO 允许 `content=null` 的 assistant 消息（只要带
    `tool_calls`）。上游对这种消息要求 parts 为空数组，而不是一个空文本块。
    """
    if content is None:
        return []
    if isinstance(content, str):
        return [_text_part(content)] if content else []
    return normalize_content(content)


def _parts_empty(parts: list[dict]) -> bool:
    """parts 里是否没有任何可读内容（空文本 / 空数组）。"""
    for part in parts:
        if not isinstance(part, dict):
            return False
        if part.get('type') == 'text':
            if str(part.get('text') or ''):
                return False
        else:
            # 图片等非文本 part 也是有内容的
            return False
    return True


def _solo_history_calls(raw_calls: object) -> list[dict]:
    """OpenAI `assistant.tool_calls` → SOLO 上游认的增量调用形状。

    上游历史的字段是 `function_call`（不是 OpenAI 的 `function`），并且要求
    `arguments` 为 JSON 字符串。参数是对象时在这里序列化；缺名字的残片丢掉，
    不让一条坏历史把整轮请求打成 400。
    """
    if isinstance(raw_calls, dict):
        raw_calls = [raw_calls]
    if not isinstance(raw_calls, (list, tuple)):
        return []
    out: list[dict] = []
    for raw in raw_calls:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get('type') or 'function').strip().lower()
        if kind != 'function':
            continue
        fn = raw.get('function') if isinstance(raw.get('function'), dict) else {}
        name = str(fn.get('name') or raw.get('name') or '').strip()
        if not name:
            continue
        arguments = fn.get('arguments')
        if isinstance(arguments, (dict, list)):
            try:
                arguments = json.dumps(arguments, ensure_ascii=False)
            except (TypeError, ValueError):
                arguments = ''
        if not isinstance(arguments, str):
            arguments = ''
        out.append({
            'index': len(out),
            'id': str(raw.get('id') or ''),
            'type': 'function',
            'function_call': {
                'name': name,
                'arguments': arguments,
                'partial_arguments': None,
                'namespace': None,
            },
        })
    return out


def normalize_solo_messages(messages: object) -> list[dict]:
    """SOLO 通道的消息归一：**保留原生工具往返**，不折成正文。

    两条通道的上游行为不同，不能共用 `normalize_messages`：

      · `llm_raw_chat` 会静默丢掉 `tool_calls` / `tool_call_id`，所以 IDE 通道
        只能把工具历史折成 `[历史调用]` / `[历史结果]` 纯文本；
      · `llm_utils_chat`（SOLO）接受 assistant 的 `tool_calls` 和 `tool` 回执，
        折成正文反而会让模型看不到真实的调用 id，多轮工具链在第二轮断掉。

    `tool` 回执需要 `tool_call_id` + `name`，而 OpenAI 的 tool 消息只有前者的
    情况很常见，因此先从历史 assistant.tool_calls 建 id → name 映射；映射不到
    时退回正文折叠（保住上下文，同时避免上游 4027）。
    """
    if isinstance(messages, dict):
        messages = [messages]
    if not isinstance(messages, (list, tuple)) or not messages:
        raise PlatformError('messages 不能为空', status=400, code='invalid_messages')

    id_to_name: dict[str, str] = {}
    for raw in messages:
        if not isinstance(raw, dict):
            continue
        if str(raw.get('role') or '').strip().lower() not in ('assistant', 'ai', 'model'):
            continue
        calls = raw.get('tool_calls')
        if not isinstance(calls, (list, tuple)):
            continue
        for call in calls:
            if not isinstance(call, dict):
                continue
            call_id = str(call.get('id') or '').strip()
            fn = call.get('function') if isinstance(call.get('function'), dict) else {}
            name = str(fn.get('name') or '').strip()
            if call_id and name:
                id_to_name[call_id] = name

    out: list[dict] = []
    for index, raw in enumerate(messages):
        if isinstance(raw, str):
            if raw:
                out.append({'role': 'user', 'content': [_text_part(raw)]})
            continue
        if not isinstance(raw, dict):
            raise PlatformError(f'messages[{index}] 不是对象', status=400,
                                code='invalid_messages')
        role = str(raw.get('role') or 'user').strip().lower()
        role = _ROLE_ALIASES.get(role, role)
        if role == 'developer':
            role = 'system'
        if role == 'function':
            role = 'tool'
        parts = _solo_parts(raw.get('content'))
        message: dict[str, Any] = {'role': role, 'content': parts}

        if role == 'assistant':
            calls = _solo_history_calls(raw.get('tool_calls'))
            if calls:
                message['tool_calls'] = calls
        elif role in ('tool', 'function'):
            call_id = str(raw.get('tool_call_id') or '').strip()
            name = id_to_name.get(call_id) or str(raw.get('name') or '').strip()
            if call_id and name:
                message['tool_call_id'] = call_id
                message['name'] = name
            else:
                # 缺 id / 映射不到名字：发空 name 会被上游 4027 拒绝，退回正文。
                label = f'[历史结果] {name}:' if name else '[历史结果]'
                message['role'] = 'user'
                message['content'] = _prefix_text(parts, label)
                parts = message['content']
        if role not in ('system', 'user', 'assistant', 'tool'):
            message['role'] = 'user'

        if _parts_empty(parts) and not message.get('tool_calls'):
            # 上游会直接丢掉空消息；在这里丢掉，顺便保证 messages 不为空。
            continue
        out.append(message)

    if not out:
        raise PlatformError('messages 不能为空', status=400, code='invalid_messages')
    return out


_TOOL_PROTOCOL_HEADER = '[工具调用协议]'


def _tool_signature(item: dict) -> str:
    """把一个工具压成 `name(required*, other:type)` 一行签名。

    上游 raw chat 在部分场景会丢结构化 `tools`，只给名字的提示等于让模型
    盲猜参数；把必填项和类型写进 system 是最小成本的补偿。
    """
    fn = item.get('function') if isinstance(item.get('function'), dict) else {}
    name = str(fn.get('name') or '').strip()
    if not name:
        return ''
    raw = fn.get('parameters')
    schema = raw
    if isinstance(raw, str):
        try:
            schema = json.loads(raw)
        except (ValueError, TypeError):
            schema = {}
    if not isinstance(schema, dict):
        schema = {}
    properties = schema.get('properties')
    if not isinstance(properties, dict):
        properties = {}
    required = schema.get('required')
    if not isinstance(required, list):
        required = []
    args: list[str] = []
    for key, spec in properties.items():
        kind = str(spec.get('type') or '').strip() if isinstance(spec, dict) else ''
        mark = '*' if key in required else ''
        args.append(f'{key}{mark}{":" + kind if kind else ""}')
    return f'{name}({", ".join(args)})'


def _tool_protocol_hint(tools: list[dict]) -> str:
    """声明了 tools 时追加的固定说明。

    上游这条通道**支持**原生 function calling（实测 `deepseek-v4.1-flash` 能
    正确下发 `tool_calls`），但上下文一长、历史里又带着折叠出来的
    `[历史调用] name(args)`，弱模型会改成「用正文念调用」。结果客户端收到一条
    普通回复加 `finish_reason: "stop"`，Agent 循环当场停住。

    这段提示把「走原生通道」「不要自造标签」「历史里的文本只是记录」三件事
    说死，是正文还原之外的第一道防线。

    **刻意不给示例**：上一版写着「如果只能写成文本，唯一允许的格式是一行，
    例如 `[tool_call] bash({"command": "..."})`」——模型会照抄这段示例（连同
    省略号），正文还原器再把它当成真调用下发，客户端执行一条 `...` 命令
    （2026-11-02 实测）。第一道防线不该教模型写第二道防线要拦的东西。
    """
    signatures = [signature for signature in
                  (_tool_signature(item) for item in tools) if signature]
    listed = '；'.join(signatures)[:1600]
    return (
        f'{_TOOL_PROTOCOL_HEADER}\n'
        f'本轮可用工具：{listed}。\n'
        '参数名后的 * 表示必填；调用参数必须符合上述结构。\n'
        '需要调用工具时，必须通过原生 function calling（tools 通道）发起；'
        '不要用 XML/HTML 标签、Markdown 代码块或任何自造语法书写调用。\n'
        '写成正文、标签或代码块的调用都不会被执行。如果无法调用工具，'
        '直接说明原因，不要伪造调用。\n'
        '对话历史里的 [历史调用] / [历史结果] 只是记录，不是可模仿的输出格式。'
    )


def _inject_tool_hint(messages: list[dict], tools: list[dict]) -> list[dict]:
    """把协议提示并进 system；没有 system 就补一条在最前面。"""
    hint = _tool_protocol_hint(tools)
    for message in messages:
        if message.get('role') == 'system':
            message['content'] = [*(message.get('content') or []), _text_part(hint)]
            return messages
    return [{'role': 'system', 'content': [_text_part(hint)]}, *messages]


def build_chat_body(model: str, messages: object, body: dict,
                    *, session_id: str | None = None) -> dict:
    """构造 llm_raw_chat 请求体。

    只透传**实测不会触发 4001** 的采样参数；未在协议里记录的字段一律不发。

    `session_id` 是「用量明细」的归属键：带上它，上游才会在这次请求结束后落一条
    按会话分组的扣费明细（见 `client.fetch_request_credit`）；不带则只累加账户
    总额，逐条实付就查不到了。
    """
    model_name = str(model or '').strip()
    if not model_name:
        raise PlatformError('model 不能为空', status=400, code='invalid_model')
    tools = normalize_tools(body.get('tools'))
    messages_norm = normalize_messages(messages)
    if tools and config.TRAE_TOOL_PROTOCOL_HINT:
        messages_norm = _inject_tool_hint(messages_norm, tools)
    payload: dict[str, Any] = {
        'model_name': model_name,
        'messages': messages_norm,
        'stream': True,          # 服务端始终 SSE，这里固定 true
    }
    if session_id:
        # 客户端两个字段同名下发，实测都认；只给一个也能落账，但照抄客户端最稳。
        payload['session_id'] = str(session_id)
        payload['chat_session_id'] = str(session_id)
    # 采样参数必须挂在 `parameters` 下：顶层同名字段上游不读（参考实现
    # trae2api-hub `build_trae_body` 也是这么发的）。旧版平铺在顶层，实测
    # temperature / max_tokens 全部无效。
    parameters: dict[str, Any] = {}
    for key in _TEXT_OPTIONS:
        value = body.get(key)
        if key == 'max_tokens' and value is None:
            # OpenAI 新字段名：网关已经会把它们钳到 int32，这里也要认。
            value = (body.get('max_completion_tokens')
                     or body.get('max_output_tokens'))
        if value is None:
            continue
        if key == 'stop' and not isinstance(value, (list, tuple)):
            value = [value]
        parameters[key] = value
    if parameters:
        payload['parameters'] = parameters
    _attach_effort(payload, body, nested=True)
    _attach_tools(payload, tools, body)
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


class _IdleTimeout(Exception):
    """上游流在 `idle_timeout` 秒内没有任何数据。"""


async def _iter_lines(response: httpx.Response,
                      idle_timeout: float = IDLE_TIMEOUT) -> AsyncIterator[str]:
    """逐行读 SSE，带空闲超时。

    `httpx` 的 read timeout 是「单次读」的，上游一直不回包时客户端会空等
    300 秒；这里改成「连续 N 秒没有新行就中止」，与参考实现一致。
    """
    iterator = response.aiter_lines().__aiter__()
    while True:
        try:
            if idle_timeout and idle_timeout > 0:
                line = await asyncio.wait_for(iterator.__anext__(), idle_timeout)
            else:
                line = await iterator.__anext__()
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError:
            raise _IdleTimeout(f'{int(idle_timeout)} 秒无数据') from None
        yield line


def _dispatch_event(event: str | None,
                    data_lines: list[str]) -> tuple[str, Any]:
    """一帧 SSE → `(event, data)`；`data: [DONE]` 一律当结束帧。

    不能沿用上一帧的 event 名：上游偶尔发 `event: output` + `data: [DONE]`，
    那样结束帧会被当成正文丢掉，流看起来就是「没有 done」。
    """
    raw = '\n'.join(data_lines)
    if raw.strip() == '[DONE]':
        return 'done', {'finish_reason': 'stop'}
    return event or 'message', _parse_event_data(raw)


async def iter_events(response: httpx.Response,
                      idle_timeout: float = IDLE_TIMEOUT
                      ) -> AsyncIterator[tuple[str, Any]]:
    """逐帧产出 `(event, data)`。

    兼容 CRLF、多行 data、注释行与 `data: [DONE]`。`data` 能解析成 JSON 就给
    dict，否则保留原始字符串——不要在解析失败时丢掉这一帧，正文可能就在里面。
    """
    event: str | None = None
    data_lines: list[str] = []
    try:
        async for line in _iter_lines(response, idle_timeout):
            if line == '':
                if data_lines:
                    yield _dispatch_event(event, data_lines)
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
    except _IdleTimeout as exc:
        yield 'error', {'code': 'idle_timeout', 'message': f'上游 {exc}'}
        return
    if data_lines:
        yield _dispatch_event(event, data_lines)


# ── SOLO / Agent 通道 ──────────────────────────────────────
def solo_headers(account: dict) -> dict[str, str]:
    """Agent 通道（`llm_utils_chat`）的鉴权头。

    与 IDE 数据面是两套画像：这条通道不认 `x-plugin-channel`，但要求
    `X-App-Version / X-Ide-Version-Code` 显式上报成 Agent 客户端，且
    `X-Uid` 必填。照抄客户端的版本号是安全的（它本来就是公开常量），
    设备号仍然按 uid 派生——随机设备号才是风控靶子。
    """
    token = str(account.get('token') or '').strip()
    if not token:
        raise PlatformError('账号缺少 access token（先导入或刷新）',
                            status=401, code='no_token')
    uid = str(account.get('uid') or '')
    headers = fingerprint.device_headers(uid)
    return {
        'Accept': 'text/event-stream',
        'Content-Type': 'application/json',
        'User-Agent': 'Trae/0.1.61 antigravity-cockpit-tools',
        'Authorization': f'Cloud-IDE-JWT {token}',
        'X-Cloudide-Token': token,
        'X-Ide-Token': token,
        'X-Uid': uid,
        'X-Machine-Id': headers['X-Machine-Id'],
        'X-Device-Id': headers['X-Device-Id'],
        'X-App-Id': APP_ID,
        'X-App-Version': 'default',
        'X-Ide-Version': '0.1.61',
        'X-Ide-Version-Code': '20260820',
        'X-App-Version-Code': '20260820',
        'X-Ide-Version-Type': 'stable',
        'X-Device-Type': 'windows',
        # Agent 通道的设备头是**原样**上报（实测不 URL 编码也能通），
        # 与 IDE 数据面那套 encoded 画像不同。
        'X-OS-Version': fingerprint.os_version(),
        'X-Device-Brand': '83DG',
        'Request-Traffic-Type': 'prod',
    }


def build_solo_body(model: str, messages: object, body: dict,
                    *, session_id: str | None = None) -> dict:
    """构造 `llm_utils_chat` 请求体（`function=solo_work_lite`）。

    `config_name` 与 `model` 要同时给：上游按 `config_name` 找模型配置、
    按 `model` 计费，缺一个就是 4023 / 4001。

    `session_id` 与 IDE 通道同样作用：它是「用量明细」的归属键。
    """
    model_name = str(model or '').strip()
    if not model_name:
        raise PlatformError('model 不能为空', status=400, code='invalid_model')
    tools = normalize_tools(body.get('tools'))
    # SOLO 与 IDE 通道对工具历史的处理不同：这里必须保留原生 tool_calls /
    # tool 回执，否则多轮工具链会在第二轮断掉（见 `normalize_solo_messages`）。
    messages_norm = normalize_solo_messages(messages)
    if tools and config.TRAE_TOOL_PROTOCOL_HINT:
        messages_norm = _inject_tool_hint(messages_norm, tools)
    payload: dict[str, Any] = {
        'messages': messages_norm,
        'function': SOLO_FUNCTION,
        'stream': True,
        'config_name': model_name,
        'model': model_name,
    }
    if session_id:
        payload['session_id'] = str(session_id)
        payload['chat_session_id'] = str(session_id)
    # 思考 token 与正文共享输出预算。客户端不传时不能空着——空着走上游
    # 默认值，思考一长就只剩 reasoning，正文和工具调用全被挤掉。
    max_tokens = (body.get('max_tokens')
                  or body.get('max_completion_tokens')
                  or body.get('max_output_tokens')
                  or config.TRAE_SOLO_MAX_TOKENS)
    try:
        payload['max_tokens'] = int(max_tokens)
    except (TypeError, ValueError):
        payload['max_tokens'] = int(config.TRAE_SOLO_MAX_TOKENS)
    _attach_effort(payload, body)
    _attach_tools(payload, tools, body)
    return payload


async def open_solo_chat(account: dict, body: dict, *,
                         timeout: float = CHAT_READ_TIMEOUT) -> httpx.Response:
    """POST `llm_utils_chat`，返回**未读取**的 SSE 响应（调用方负责关闭）。"""
    realm = normalize_realm(account.get('realm'))
    if realm != 'cn':
        raise PlatformError('Agent 通道仅国内版可用', status=400,
                            code='solo_unsupported')
    headers = solo_headers(account)
    proxy = _proxy_for(account)
    hosts = realm_hosts(realm, 'core')
    last_exc: Exception | None = None
    timeout_obj = httpx.Timeout(timeout, connect=5)
    async with config.http_client(timeout_obj, proxy=proxy) as client:
        for host in hosts:
            request = client.build_request(
                'POST', host + PATH_AGENT_CHAT, headers=headers,
                content=json.dumps(body, ensure_ascii=False).encode('utf-8'))
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
    raise PlatformError(f'Trae Agent 通道不可达：{last_exc or "无可用主机"}',
                        status=502, code='upstream_unreachable')


async def iter_solo_events(response: httpx.Response,
                           idle_timeout: float = IDLE_TIMEOUT
                           ) -> AsyncIterator[tuple[str, Any]]:
    """把 Agent 通道的 SSE 帧翻成与 `iter_events` 同名的事件。

    这条通道没有 `event:` 行，帧也不带类型，只能按字段判断：
    `response` / `reasoning_content` / `tool_calls` → output；
    `prompt_tokens` → token_usage；`finish_reason` → done；
    `code` 非 0 **且这一帧没有正文** → error；其余当 metadata 丢掉。

    为什么 `code` 判据要放宽：不同版本/区域把 `code` 当业务码用（`"0"` 字符串、
    `200` 都见过）。以前只要不是 int 0 就整帧判错，第一帧正文之后立刻 break，
    客户端看到的就是「回复到一半没了」。带正文的帧一律按正常帧处理，只记日志。
    """
    try:
        async for line in _iter_lines(response, idle_timeout):
            line = (line or '').strip()
            if not line.startswith('data:'):
                continue
            raw = line[5:].strip()
            if not raw:
                continue
            if raw == '[DONE]':
                yield 'done', {'finish_reason': 'stop'}
                continue
            try:
                event = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if not isinstance(event, dict):
                continue
            code = event.get('code')
            if code not in (None, 0, '0', 200, '200'):
                has_payload = any(event.get(key) for key in (
                    'response', 'reasoning_content', 'tool_calls',
                    'multimodal_contents', 'prompt_tokens', 'total_tokens',
                    'finish_reason'))
                if not has_payload:
                    yield 'error', event
                    continue
                logger.warning('Trae SOLO 帧带非零 code=%r 但同时有正文，按正常帧处理',
                               code)
            # 一帧里可能同时带正文和用量 / 结束标记，逐项判断而不是 `continue`，
            # 否则会漏掉 usage（记账丢 token）或漏掉 finish_reason。
            emitted = False
            if any(event.get(key) for key in ('response', 'reasoning_content',
                                              'tool_calls', 'multimodal_contents')):
                yield 'output', event
                emitted = True
            if ('prompt_tokens' in event or 'total_tokens' in event
                    or isinstance(event.get('usage'), dict)):
                yield 'token_usage', event
                emitted = True
            if event.get('finish_reason'):
                yield 'done', event
                emitted = True
            if not emitted:
                yield 'metadata', event
    except _IdleTimeout as exc:
        yield 'error', {'code': 'idle_timeout', 'message': f'上游 {exc}'}


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


def _is_us_account(account: dict) -> bool:
    """国际版账号是否落在 US 部署（`region` 形如 `US` / `US-east`）。"""
    region = str(account.get('region') or account.get('aiRegion') or '')
    region = region.strip().upper()
    if region == 'US' or region[:3] in ('US-', 'US_', 'US '):
        return True
    return 'api-us' in str(account.get('api') or '').lower()


def intl_entitlement_hosts(account: dict) -> tuple[str, ...]:
    """国际版用量接口的主机顺序：网页同款 → 账号所属区 → 另一个区。"""
    conf = REALMS['intl']
    sg = str(conf['entitlement_sg'])
    us = str(conf['entitlement_us'])
    ordered = [str(conf['entitlement'])]
    ordered += [us, sg] if _is_us_account(account) else [sg, us]
    out: list[str] = []
    for host in ordered:
        if host and host not in out:
            out.append(host)
    return tuple(out)


async def fetch_intl_entitlement(account: dict, *, realm: str | None = None,
                                 timeout: float = 45) -> dict:
    """国际版「Usage」页的美元额度（`user_current_entitlement_list`）。

    2026-11-01 用真实网页请求核对过：主机是 `ug-normal.trae.ai`，请求体
    必须带 `require_usage` 才会回 `usage.*_usage_amount`。一个主机失败时
    按账号区换下一个（US：api-us-east，其余：api-sg-central），全部失败才
    抛出**第一个**错误——它通常最能说明原因（401 过期 / 403 无权限）。
    """
    realm = normalize_realm(realm or account.get('realm'))
    first: PlatformError | None = None
    for host in intl_entitlement_hosts(account):
        try:
            data = await _request_hosts(
                account, realm, (host,), PATH_INTL_ENTITLEMENT,
                {'require_usage': True}, timeout, {})
        except PlatformError as exc:
            first = first or exc
            continue
        return data if isinstance(data, dict) else {}
    raise first or PlatformError('取不到国际版用量', status=502,
                                 code='entitlement_unavailable')


async def fetch_consumed(account: dict, *, realm: str | None = None,
                         timeout: float = 20) -> float | None:
    """当前**已消耗**积分（套餐积分池口径），取不到返回 None。

    为什么要单独一个函数：`fetch_entitlement` 会顺带打 `pay_status`，而「一次
    请求扣了多少积分」只需要一个累计数——前后各夹一次的时候，多打的那个端点
    就是白白的两倍流量。

    读数优先取 `user_entitlement_pack_list[].usage.credits_amount`：实测它有
    4 位小数（如 16.4284），与明细 `amount_float` 的增量完全相等；退回
    `usage_summary.consumed_amount` 时只有 2 位小数（16.43），0.004 这种量级
    的扣费会被抹平，所以只当兜底。
    """
    realm = normalize_realm(realm or account.get('realm'))
    path = PATH_ENT_USAGE_V2 if realm == 'cn' else PATH_ENT_USAGE_V1
    data = await request_json(account, path, {}, realm=realm, base='pay',
                              timeout=timeout)
    if not isinstance(data, dict):
        return None
    for pack in data.get('user_entitlement_pack_list') or []:
        usage = pack.get('usage') if isinstance(pack, dict) else None
        value = usage.get('credits_amount') if isinstance(usage, dict) else None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if value:
            return float(value)
    summary = data.get('usage_summary')
    value = summary.get('consumed_amount') if isinstance(summary, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


# ── 用量明细（逐条实付积分）─────────────────────────────────
# 实测（2026-11-01，CN Free / billing_version=2）：
#   · 只有 `usage_type` 里带上 7 才会返回记录，1..6 恒为 total=0；
#   · 记录按 `session_id` 分组，一条明细 = 一次请求（同会话多轮会累加到一行）；
#   · 扣费是**同步落账**的：流结束后立刻查就能命中（4/4 次首查即中，耗时
#     0.24–0.48s 基本就是一次网络往返）。之前的「1–4 秒」是 4 秒粗轮询造成的
#     假象，已推翻；
#   · 记录里同时给 credits（amount_float / credits_float）与美元（cost_money_float）
#     以及四段 token；`amount_float` 与账户累计增量完全相等，就是实扣积分；
#   · 请求体不带 `session_id` 时上游照样扣费（账户累计会涨），但**不会**产生
#     明细行——所以要拿到逐条实付，必须在请求里带上会话号。
# 因此只留 3 次极短重试（合计 <1 秒）：正常情况下第一次就命中，剩下的只是
# 给偶发抖动兜底，不再像以前那样把流结束硬拖慢好几秒。
_CREDIT_POLL_DELAYS = (0.0, 0.25, 0.5)

async def fetch_usage_group(account: dict, *, start_time: int, end_time: int,
                            usage_type: tuple[int, ...] = (USAGE_TYPE_CREDITS,),
                            page_size: int = 20, page_num: int = 1,
                            realm: str | None = None,
                            timeout: float = 20) -> dict:
    """按时间窗拉「用量明细」（按会话分组）。

    `page_size` 实测上限在 50 附近，给 100 会被上游按参数错误拒（9004）。
    """
    body = {
        'start_time': int(start_time),
        'end_time': int(end_time),
        'page_size': int(page_size),
        'page_num': int(page_num),
        'usage_type': [int(x) for x in usage_type],
    }
    data = await request_json(account, PATH_USAGE_GROUP, body, realm=realm,
                              base='pay', timeout=timeout)
    return data if isinstance(data, dict) else {}


async def fetch_request_credit(account: dict, session_id: str, started: float, *,
                               realm: str | None = None,
                               delays: tuple[float, ...] = _CREDIT_POLL_DELAYS,
                               timeout: float = 20) -> dict | None:
    """按 `session_id` 找到本次请求那条明细，取不到返回 None。

    `started` 是**发请求前**的时间戳（秒）。时间窗往前放 60 秒、往后取当前时刻，
    即使 NTP 有偏差或落账比预期慢一点也不会漏行。
    """
    sid = str(session_id or '').strip()
    if not sid:
        return None
    start = int(started) - 60
    for delay in delays:
        if delay:
            await asyncio.sleep(delay)
        try:
            data = await fetch_usage_group(account, start_time=start,
                                           end_time=int(time.time()) + 60,
                                           realm=realm, timeout=timeout)
        except PlatformError:
            continue
        for row in data.get('user_usage_group_by_sessions') or []:
            if isinstance(row, dict) and str(row.get('session_id') or '') == sid:
                return row
    return None


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
