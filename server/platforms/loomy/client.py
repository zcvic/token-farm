"""Loomy 上游协议层。

边界与 Trae / Qoder 一致：本模块只做「协议翻译」，不含账号池策略；选号、
冷却、签到编排在 `adapter.py`。

两套传输：

  · **账号服务**（`account.xfinfr.com`）——登录是同步调用（`import_account`
    本身是同步方法），刷新走异步 httpx。请求必须带 `signer.build_headers`
    生成的 HMAC-SHA1 签名。
  · **模型网关**（`loomyad.xunfei.cn/api/v1`）——异步 httpx，OpenAI 兼容。
    流式响应原样透传字节，便于网关走 `raw_stream` 路径。

代理口径与项目其余部分一致：`adapter` 先用 `config.resolve_account_proxy`
把账号线路解析成 URL，本模块只接受「已解析」的地址：`None` = 用全局代理，
`''` = 强制直连（`trust_env=False`，不读环境代理）。
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Optional

import httpx

from ... import config
from ..base import PlatformError
from . import constants as C
from .crypto import rsa_encrypt
from .signer import build_headers

__all__ = [
    'AccountError', 'login_by_password', 'login_by_password_sync',
    'login_by_wechat_sync', 'send_sms_code_sync', 'login_by_sms_sync',
    'bind_send_msg_sync', 'bind_check_code_sync', 'query_base_info_sync',
    'fetch_models', 'fetch_points', 'fetch_points_breakdown', 'first_login',
    'query_activation', 'bind_invite_code', 'redeem_code',
    'chat', 'open_chat', 'login_expired', 'model_headers', 'parse_expire_at',
]


# ── 通用 ────────────────────────────────────────────────────
def _selected_proxy(proxy: str | None) -> str:
    """账号出口：`None` = 用全局代理（与 `http_client(proxy=None)` 同口径），
    `''` = **强制直连**（账号绑定了 `__direct__` 时由 `resolve_account_proxy`
    返回空串）。其它值按 URL 使用。
    """
    if proxy is None:
        return config.effective_http_proxy()
    return str(proxy).strip()


def _account_headers(path: str, body_text: str) -> dict[str, str]:
    return build_headers(
        C.ACCESS_KEY_ID, C.ACCESS_KEY_SECRET,
        method='POST', path=path, body=body_text,
    )


def model_headers(session: str) -> dict[str, str]:
    """模型网关请求头。

    `Authorization` 与 `token` 都带上（客户端 `useSessionAuth` 两条都发）；
    `traceparent` 缺失时上游会一直挂到超时——必须每次现生成。
    """
    session = str(session or '').strip()
    return {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {session}',
        'token': session,
        'traceparent': f'00-{uuid.uuid4().hex}-{uuid.uuid4().hex[:16]}-01',
        'loomy-version': C.LOOMY_VERSION,
    }


def _biz_ok(code: object) -> bool:
    return code in (None, '', 0, '0', '000000', 200, '200')


def login_expired(payload: object) -> bool:
    """面板级登录失效：HTTP 200 但业务码 100002 / 描述含「登录已失效」。

    这**不等于**会话作废（签到等端点可能仍可用），调用方只做提示，不据此
    清 session。
    """
    if not isinstance(payload, dict):
        return False
    code = payload.get('code')
    if str(code) in ('100002',):
        return True
    desc = str(payload.get('desc') or payload.get('message') or '')
    return '登录已失效' in desc


def _error_text(payload: object, fallback: str) -> str:
    if isinstance(payload, dict):
        error = payload.get('error')
        if isinstance(error, dict):
            message = error.get('message')
            if message:
                return str(message)
        for key in ('message', 'desc', 'msg'):
            value = payload.get(key)
            if value:
                return str(value)
    return fallback


class AccountError(PlatformError):
    """账号服务业务错误（带上游 code）。"""

    def __init__(self, message: str, *, code: str = 'account_error',
                 status: int = 502, payload: Any = None) -> None:
        super().__init__(message, status=status, code=code, payload=payload)


# ── 账号服务（同步）────────────────────────────────────────
def _identity_envelope(identity: dict | None) -> dict:
    identity = identity or {}
    return {
        'appid': C.ACCOUNT_APPID,
        'modelid': str(identity.get('modelid') or C.WEB_MODEL_ID),
        'version': str(identity.get('version') or C.CLIENT_VERSION),
        # 风控：所有官方客户端的 devid 都是固定常量 'web'。
        'devid': 'web',
        'ua': str(identity.get('ua') or C.CLIENT_UA),
        # 真实客户端每个请求都重新生成 traceid。
        'traceid': uuid.uuid4().hex,
    }


def _account_payload(body: dict | None,
                     identity: dict | None) -> tuple[dict, str]:
    payload = {'base': _identity_envelope(identity)}
    if body:
        payload.update(body)
    return payload, json.dumps(payload, ensure_ascii=False)


def _account_call_sync(path: str, body: dict | None, *,
                       identity: dict | None = None,
                       proxy: str | None = None,
                       timeout: float = 30) -> dict:
    _payload, body_text = _account_payload(body, identity)
    headers = _account_headers(path, body_text)
    url = f'{C.ACCOUNT_BASE.rstrip("/")}{path}'
    selected = _selected_proxy(proxy)
    kwargs: dict[str, Any] = {'timeout': timeout, 'trust_env': False}
    if selected:
        kwargs['proxy'] = selected
    try:
        with httpx.Client(**kwargs) as client:
            response = client.post(url, headers=headers,
                                   content=body_text.encode('utf-8'))
    except Exception as exc:  # noqa: BLE001 —— 网络异常统一成可重试的上游错误
        raise AccountError(f'账号服务不可达：{exc}', code='network',
                           status=502) from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise AccountError(
            f'账号服务返回 HTTP {response.status_code}：'
            f'{response.text[:200]}', code=f'http_{response.status_code}',
            status=502) from exc
    code = data.get('code')
    if not _biz_ok(code):
        message = _error_text(data, f'{path} 失败')
        # 账号密码错误本质上仍是「请求不合法」，回 400 让界面可直接提示。
        raise AccountError(message, code=str(code or 'account_error'),
                           status=400, payload=data)
    if response.status_code >= 400 and not data.get('data'):
        raise AccountError(
            f'{path} HTTP {response.status_code}',
            code=f'http_{response.status_code}', status=502, payload=data)
    return data


#: 上游登录响应里**可能**出现的过期时间字段名（绝对秒 / 毫秒 / 相对秒）。
_EXPIRE_FIELD_KEYS = (
    'expireAt', 'expire_at', 'expireTime', 'expire_time',
    'sessionExpire', 'session_expire', 'expiresAt', 'expires_at',
    'expire',
)
_EXPIRE_IN_FIELD_KEYS = ('expiresIn', 'expires_in', 'expireIn', 'expire_in', 'ttl')


def parse_expire_at(node: dict, obtained_at: int) -> int:
    """从登录响应里尽量抠出绝对过期时间戳（秒）；抠不到返回 0。

    登录响应实测没有可靠字段，所以这里只是「有就用、没有就算了」：兜底值由
    调用方按客户端声称的 14 天补上。绝不据此判定凭证死活。
    """
    if not isinstance(node, dict):
        return 0
    for key in _EXPIRE_FIELD_KEYS:
        value = node.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool) \
                or value <= 0:
            continue
        value = int(value)
        if value > 10 ** 12:            # 毫秒级 epoch
            return value // 1000
        if value > 10 ** 9:             # 秒级 epoch
            return value
        if 60 <= value <= 365 * 86400:  # 相对秒数（如 1209600 = 14 天）
            return obtained_at + value
    for key in _EXPIRE_IN_FIELD_KEYS:
        value = node.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) \
                and 60 <= value <= 365 * 86400:
            return obtained_at + int(value)
    return 0


def _extract_session(payload: dict) -> dict:
    node = payload.get('data') if isinstance(payload.get('data'), dict) else payload
    if not isinstance(node, dict):
        raise AccountError('登录响应格式异常', code='invalid_login',
                           status=502, payload=payload)
    session = str(node.get('session') or '')
    if not session:
        raise AccountError('登录成功但响应里没有 session', code='no_session',
                           status=502, payload=payload)
    return {
        'session': session,
        'userid': str(node.get('userid') or node.get('userId') or ''),
        'phone': str(node.get('phone') or node.get('loginid') or ''),
        'nickname': str(node.get('nickname') or node.get('name') or ''),
        'expire_at': parse_expire_at(node, int(time.time())),
        'raw': node,
    }


def login_by_password_sync(phone: str, password: str, *,
                           identity: dict | None = None,
                           proxy: str | None = None,
                           timeout: float = 30) -> dict:
    """密码登录：`getPuKey` → RSA 加密 → `byPwd`。"""
    if not phone or not password:
        raise AccountError('缺少手机号或密码', code='missing_credentials',
                           status=400)
    key_payload = _account_call_sync(
        '/login/account/getPuKey', None, identity=identity,
        proxy=proxy, timeout=timeout)
    data = key_payload.get('data') if isinstance(key_payload.get('data'), dict) else {}
    pukey = str(data.get('pukey') or '')
    rcode = str(data.get('rcode') or '')
    if not pukey:
        raise AccountError('账号服务没有返回公钥', code='no_pukey', status=502,
                           payload=key_payload)
    try:
        encrypted = rsa_encrypt(pukey, password)
    except ValueError as exc:
        raise AccountError(f'密码加密失败：{exc}', code='crypto_error',
                           status=500) from exc
    payload = _account_call_sync('/login/account/byPwd', {
        'param': {
            'loginid': phone,
            'password': encrypted,
            'rcode': rcode,
            'type': 1,
            'expire': C.SESSION_EXPIRE_SECONDS,
        },
    }, identity=identity, proxy=proxy, timeout=timeout)
    return _extract_session(payload)


def login_by_wechat_sync(code: str, *, identity: dict | None = None,
                         proxy: str | None = None,
                         timeout: float = 30) -> dict:
    """微信扫码登录：`bind/auth` → `bind/skip`。

    `bind=1`（该微信已绑手机号）直接换 session；`bind=0` 时上游要求再走
    `bind/sendMsg` + `bind/checkCode` 绑一次手机号，这里抛
    `wechat_needs_bind` 并带上 `rcode`，由 `login.py` 引导用户补填手机号
    （`send_sms_code` / `login_by_sms` 是另一条「验证码直接登录」链路，
    两者不能混用：绑定必须用 `rcode`）。

    微信 `code` 只在 `bind/auth` 用一次，后续接口只用 `rcode`。
    """
    code = str(code or '').strip()
    if not code:
        raise AccountError('缺少微信授权 code', code='missing_code', status=400)

    auth = _account_call_sync('/login/thirdAccount/bind/auth', {
        'param': {'tcode': {'code': code}, 'type': C.WECHAT_THIRD_TYPE},
    }, identity=identity, proxy=proxy, timeout=timeout)
    data = auth.get('data') if isinstance(auth.get('data'), dict) else auth
    data = data if isinstance(data, dict) else {}
    rcode = str(data.get('rcode') or '')
    bind = data.get('bind')
    if bind is None:
        bind = data.get('bindStatus')
    bind = str(bind if bind is not None else '')
    if not rcode:
        raise AccountError('微信鉴权没有返回 rcode', code='wechat_no_rcode',
                           status=502, payload=auth)
    if bind not in ('1', 'true', 'True'):
        raise AccountError(
            '该微信还没有绑定手机号：填写手机号并完成短信验证即可绑定登录',
            code='wechat_needs_bind', status=409,
            payload={'rcode': rcode, 'bind': bind, 'data': data})

    payload = _account_call_sync('/login/thirdAccount/bind/skip', {
        'param': {'rcode': rcode, 'expire': C.SESSION_EXPIRE_SECONDS},
    }, identity=identity, proxy=proxy, timeout=timeout)
    return _extract_session(payload)


def _sms_msgid(payload: dict) -> str:
    """`sendMsgCode` / `bind/sendMsg` 的 msgid 在 `data` 里，个别版本在顶层。"""
    data = payload.get('data') if isinstance(payload.get('data'), dict) else {}
    return str(data.get('msgid') or payload.get('msgid') or '')


def send_sms_code_sync(phone: str, *, ccode: str = '86',
                       identity: dict | None = None,
                       proxy: str | None = None,
                       timeout: float = 30) -> dict:
    """手机号验证码登录 · 第 1 步：`/login/phone/sendMsgCode`。

    返回 `{msgid, phone, raw}`；`msgid` 是下一步 `checkCode` 的票据。上游
    没给 msgid 时仍然返回（验证码可能已发出），由调用方决定是否提示。
    """
    phone = str(phone or '').strip()
    if not phone:
        raise AccountError('缺少手机号', code='missing_phone', status=400)
    payload = _account_call_sync('/login/phone/sendMsgCode', {
        'param': {'ccode': str(ccode or '86'), 'phone': phone, 'expire': 300},
    }, identity=identity, proxy=proxy, timeout=timeout)
    return {'msgid': _sms_msgid(payload), 'phone': phone, 'raw': payload}


def login_by_sms_sync(phone: str, code: str, msgid: str, *,
                      ccode: str = '86', identity: dict | None = None,
                      proxy: str | None = None,
                      timeout: float = 30) -> dict:
    """手机号验证码登录 · 第 2 步：`/login/phone/checkCode` → session。

    验证码登录拿不到密码：落盘后 `has_password=False`，会话失效只能重新
    用验证码登录（与扫码 / 本机导入一致）。
    """
    phone = str(phone or '').strip()
    code = str(code or '').strip()
    if not phone:
        raise AccountError('缺少手机号', code='missing_phone', status=400)
    if not code:
        raise AccountError('缺少验证码', code='missing_code', status=400)
    payload = _account_call_sync('/login/phone/checkCode', {
        'param': {
            'ccode': str(ccode or '86'),
            'phone': phone,
            'mcode': code,
            'msgid': str(msgid or ''),
            'expire': C.SESSION_EXPIRE_SECONDS,
        },
    }, identity=identity, proxy=proxy, timeout=timeout)
    return _extract_session(payload)


def bind_send_msg_sync(rcode: str, phone: str, *, ccode: str = '86',
                       identity: dict | None = None,
                       proxy: str | None = None,
                       timeout: float = 30) -> dict:
    """微信未绑手机号（`bind=0`）· 第 2 步：`bind/sendMsg`。

    `rcode` 来自 `bind/auth`，**不能**用 `code`：微信授权码是一次性的。
    """
    rcode = str(rcode or '').strip()
    phone = str(phone or '').strip()
    if not rcode:
        raise AccountError('缺少微信绑定票据（请重新扫码）', code='missing_rcode',
                           status=400)
    if not phone:
        raise AccountError('缺少手机号', code='missing_phone', status=400)
    payload = _account_call_sync('/login/thirdAccount/bind/sendMsg', {
        'param': {
            'rcode': rcode,
            'phone': phone,
            'ccode': str(ccode or '86'),
            'expire': 300,
        },
    }, identity=identity, proxy=proxy, timeout=timeout)
    return {'msgid': _sms_msgid(payload), 'phone': phone, 'raw': payload}


def bind_check_code_sync(rcode: str, code: str, msgid: str, *,
                         identity: dict | None = None,
                         proxy: str | None = None,
                         timeout: float = 30) -> dict:
    """微信未绑手机号（`bind=0`）· 第 3 步：`bind/checkCode` → session。

    上游会顺手完成「绑定手机号」（等价注册），成功后返回 session / userid /
    phone，和密码登录的响应同形。
    """
    rcode = str(rcode or '').strip()
    code = str(code or '').strip()
    if not (rcode and code and msgid):
        raise AccountError('缺少 rcode / 验证码 / msgid，请重新发送验证码',
                           code='missing_param', status=400)
    payload = _account_call_sync('/login/thirdAccount/bind/checkCode', {
        'param': {
            'rcode': rcode,
            'mcode': code,
            'msgid': str(msgid or ''),
            'expire': C.SESSION_EXPIRE_SECONDS,
        },
    }, identity=identity, proxy=proxy, timeout=timeout)
    return _extract_session(payload)


def query_base_info_sync(session: str, *,
                         identity: dict | None = None,
                         proxy: str | None = None,
                         timeout: float = 20) -> dict:
    payload = _account_call_sync('/userinfo/query/baseInfo', {
        'param': {'session': session},
    }, identity=identity, proxy=proxy, timeout=timeout)
    node = payload.get('data') if isinstance(payload.get('data'), dict) else payload
    return node if isinstance(node, dict) else {}


# ── 账号服务（异步，供刷新用）────────────────────────────────
async def _account_call_async(path: str, body: dict | None, *,
                              identity: dict | None = None,
                              proxy: str | None = None,
                              timeout: float = 30) -> dict:
    _payload, body_text = _account_payload(body, identity)
    headers = _account_headers(path, body_text)
    url = f'{C.ACCOUNT_BASE.rstrip("/")}{path}'
    try:
        async with config.http_client(
                httpx.Timeout(timeout, connect=10),
                proxy=_selected_proxy(proxy), lane='loomy') as client:
            response = await client.post(
                url, headers=headers, content=body_text.encode('utf-8'))
    except Exception as exc:  # noqa: BLE001
        raise AccountError(f'账号服务不可达：{exc}', code='network',
                           status=502) from exc
    try:
        data = response.json()
    except ValueError as exc:
        raise AccountError(
            f'账号服务返回 HTTP {response.status_code}：'
            f'{response.text[:200]}', code=f'http_{response.status_code}',
            status=502) from exc
    code = data.get('code')
    if not _biz_ok(code):
        raise AccountError(_error_text(data, f'{path} 失败'),
                           code=str(code or 'account_error'), status=400,
                           payload=data)
    return data


async def login_by_password(phone: str, password: str, *,
                            identity: dict | None = None,
                            proxy: str | None = None,
                            timeout: float = 30) -> dict:
    if not phone or not password:
        raise AccountError('缺少手机号或密码', code='missing_credentials',
                           status=400)
    key_payload = await _account_call_async(
        '/login/account/getPuKey', None, identity=identity,
        proxy=proxy, timeout=timeout)
    data = key_payload.get('data') if isinstance(key_payload.get('data'), dict) else {}
    pukey = str(data.get('pukey') or '')
    rcode = str(data.get('rcode') or '')
    if not pukey:
        raise AccountError('账号服务没有返回公钥', code='no_pukey', status=502,
                           payload=key_payload)
    encrypted = rsa_encrypt(pukey, password)
    payload = await _account_call_async('/login/account/byPwd', {
        'param': {
            'loginid': phone,
            'password': encrypted,
            'rcode': rcode,
            'type': 1,
            'expire': C.SESSION_EXPIRE_SECONDS,
        },
    }, identity=identity, proxy=proxy, timeout=timeout)
    return _extract_session(payload)


# ── 模型网关 ────────────────────────────────────────────────
def _gateway_error(response: httpx.Response, text: str,
                   *, what: str) -> PlatformError:
    try:
        payload = json.loads(text) if text else {}
    except ValueError:
        payload = {}
    message = _error_text(payload, text[:200] or f'{what} HTTP {response.status_code}')
    status = response.status_code
    if status in (401, 403):
        code = 'session_invalid'
    elif status == 429:
        code = 'rate_limited'
    elif status in (400, 404, 422):
        code = 'upstream_rejected'
    elif status >= 500:
        code = 'upstream_error'
    else:
        status, code = 502, 'upstream_error'
    return PlatformError(message, status=status, code=code, payload=payload)


def _url(path: str) -> str:
    return f'{C.MODEL_BASE.rstrip("/")}/{path.lstrip("/")}'


async def fetch_models(session: str, *, proxy: str | None = None,
                       timeout: float = 30) -> list[dict]:
    async with config.http_client(
            httpx.Timeout(timeout, connect=10),
            proxy=_selected_proxy(proxy), lane='loomy') as client:
        response = await client.get(_url('models'),
                                    headers=model_headers(session))
    if response.status_code >= 400:
        raise _gateway_error(response, response.text, what='/models')
    try:
        payload = response.json()
    except ValueError as exc:
        raise PlatformError('/models 响应不是 JSON', status=502,
                            code='bad_upstream') from exc
    data = payload.get('data') if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict) and item.get('id')]


async def fetch_points(session: str, *, proxy: str | None = None,
                       timeout: float = 30, page_no: int = 1,
                       page_size: int = 1) -> dict:
    """`/points/records`：上游只认 camelCase 的 `pageNo`/`pageSize`。"""
    params = (f'record_type=all&pageNo={int(page_no)}'
              f'&pageSize={int(page_size)}')
    async with config.http_client(
            httpx.Timeout(timeout, connect=10),
            proxy=_selected_proxy(proxy), lane='loomy') as client:
        response = await client.get(_url(f'points/records?{params}'),
                                    headers=model_headers(session))
    if response.status_code >= 400:
        raise _gateway_error(response, response.text, what='/points/records')
    try:
        payload = response.json()
    except ValueError as exc:
        raise PlatformError('/points/records 响应不是 JSON', status=502,
                            code='bad_upstream') from exc
    # 业务码级「登录已失效」（HTTP 仍 200）**不在这里抛**：签到 / 对话等端点
    # 可能仍可用，按 401 处理会把还能用的账号踢掉。调用方用 `login_expired`
    # 自己决定是提示还是报错。
    return payload if isinstance(payload, dict) else {}


async def fetch_points_breakdown(session: str, *, proxy: str | None = None,
                                 timeout: float = 30,
                                 max_pages: int = 3) -> dict:
    """余额 + 最近流水（用量弹窗展示用）。"""
    records: list[dict] = []
    first_data: dict = {}
    total = None
    seen: set = set()
    for page_no in range(1, max(1, max_pages) + 1):
        payload = await fetch_points(session, proxy=proxy, timeout=timeout,
                                     page_no=page_no, page_size=100)
        if page_no == 1 and login_expired(payload) \
                and not (isinstance(payload.get('data'), dict)
                         and payload.get('data')):
            # 会话失效：HTTP 200 + code=100002 + 空 data。不要把它当成
            # 「0 条流水」。
            raise PlatformError(
                _error_text(payload, '登录已失效'), status=401,
                code='login_expired', payload=payload)
        data = payload.get('data') if isinstance(payload.get('data'), dict) else {}
        if page_no == 1:
            first_data = data
            total = data.get('total')
        page = (data.get('list') or data.get('records')
                or data.get('items') or [])
        for item in page:
            if not isinstance(item, dict):
                continue
            key = item.get('ledgerId')
            if key is None:
                key = (item.get('createdAt'), item.get('modelName'),
                       item.get('pointsActual'))
            if key in seen:
                continue
            seen.add(key)
            records.append({
                'direction': item.get('direction') or '',
                'source': item.get('consumeSource') or '',
                'model': item.get('modelName') or '',
                'points': item.get('pointsActual') or 0,
                'balance_after': item.get('balanceAfter'),
                'time': item.get('createdAt') or '',
                'desc': (item.get('description') or '')[:80],
            })
        if len(page) < 100:
            break
        if isinstance(total, int) and len(records) >= total:
            break
    return _balance_view(first_data, records=records, total_records=total)


def _balance_view(data: dict, *, records: list[dict] | None = None,
                  total_records: object = None) -> dict:
    out = {
        'balance': data.get('balance'),
        'daily_balance': data.get('dailyBalance'),
        'available': data.get('availableBalance'),
        'total': data.get('totalBalance'),
        'expiring': data.get('expiringBalance'),
        'daily_quota': data.get('dailyQuota'),
        'daily_consumed': data.get('dailyConsumed'),
        'cycle': data.get('dailyCycleDate'),
    }
    if records is not None:
        out['records'] = records
        out['records_total'] = total_records
    return out


async def first_login(session: str, *, proxy: str | None = None,
                      timeout: float = 30, device_id: str = '',
                      invite_code: str = '') -> dict:
    """每日签到（每天首次登录发奖；重复调用幂等返回 alreadyProcessed）。"""
    body: dict[str, Any] = {}
    if str(invite_code or '').strip():
        body['inviteCode'] = str(invite_code).strip()
        if str(device_id or '').strip():
            body['deviceId'] = str(device_id).strip()
    content = json.dumps(body, ensure_ascii=False).encode('utf-8') if body else None
    async with config.http_client(
            httpx.Timeout(timeout, connect=10),
            proxy=_selected_proxy(proxy), lane='loomy') as client:
        response = await client.post(_url('points/first-login'),
                                     headers=model_headers(session),
                                     content=content)
    if response.status_code >= 400:
        raise _gateway_error(response, response.text, what='/points/first-login')
    try:
        payload = response.json()
    except ValueError as exc:
        raise PlatformError('/points/first-login 响应不是 JSON', status=502,
                            code='bad_upstream') from exc
    # 同 `fetch_points`：业务码失效交给调用方判断，不据此丢弃会话。
    return payload if isinstance(payload, dict) else {}


async def _points_json(session: str, path: str, *, method: str = 'GET',
                       body: dict | None = None, proxy: str | None = None,
                       timeout: float = 30) -> dict:
    """积分域的小接口（激活状态 / 绑定邀请码 / 兑换码）共用一次调用。

    与 `first_login` 同一口径：HTTP >= 400 归成 `PlatformError`，业务码失效
    （HTTP 200 + `code=100002`）**不在这里抛**，交给调用方用 `login_expired`
    判断——这些端点在会话「面板级失效」时仍可能可用。
    """
    content = (json.dumps(body, ensure_ascii=False).encode('utf-8')
               if body is not None else None)
    async with config.http_client(
            httpx.Timeout(timeout, connect=10),
            proxy=_selected_proxy(proxy), lane='loomy') as client:
        if method.upper() == 'GET':
            response = await client.get(_url(path),
                                        headers=model_headers(session))
        else:
            response = await client.post(_url(path),
                                         headers=model_headers(session),
                                         content=content)
    if response.status_code >= 400:
        raise _gateway_error(response, response.text, what=f'/{path}')
    try:
        payload = response.json()
    except ValueError as exc:
        raise PlatformError(f'/{path} 响应不是 JSON', status=502,
                            code='bad_upstream') from exc
    return payload if isinstance(payload, dict) else {}


async def query_activation(session: str, *, proxy: str | None = None,
                           timeout: float = 30) -> dict:
    """激活状态：`GET /points/activation`。

    `data.activated` 为 true 表示该账号已经绑过邀请码（此时不能再绑）；
    `data.appliedInvitationCode` 是已绑定的那一个。
    """
    return await _points_json(session, 'points/activation', proxy=proxy,
                              timeout=timeout)


async def bind_invite_code(session: str, invite_code: str, *,
                           device_id: str = '',
                           proxy: str | None = None,
                           timeout: float = 30) -> dict:
    """绑定邀请码：`POST /points/activation` body `{inviteCode, deviceId?}`。

    与签到（`points/first-login`）是两个接口：签到只负责发每日奖励，绑定
    邀请码走这里。`deviceId` 是客户端上报的校园设备号，没有就不带。
    """
    invite = str(invite_code or '').strip()
    if not invite:
        raise PlatformError('请填写邀请码', status=400, code='missing_invite_code')
    body: dict[str, Any] = {'inviteCode': invite}
    if str(device_id or '').strip():
        body['deviceId'] = str(device_id).strip()
    return await _points_json(session, 'points/activation', method='POST',
                              body=body, proxy=proxy, timeout=timeout)


async def redeem_code(session: str, code: str, *, proxy: str | None = None,
                      timeout: float = 30) -> dict:
    """兑换兑换码：`POST /points/redemption-codes/redeem` body `{code}`。"""
    value = str(code or '').strip()
    if not value:
        raise PlatformError('请填写兑换码', status=400, code='missing_redeem_code')
    return await _points_json(session, 'points/redemption-codes/redeem',
                              method='POST', body={'code': value},
                              proxy=proxy, timeout=timeout)


async def chat(session: str, body: dict, *, proxy: str | None = None,
               timeout: float = 120) -> dict:
    """非流式对话，返回上游 JSON。"""
    async with config.http_client(
            httpx.Timeout(timeout, connect=10),
            proxy=_selected_proxy(proxy), lane='loomy') as client:
        response = await client.post(
            _url('chat/completions'), headers=model_headers(session),
            content=json.dumps(body, ensure_ascii=False).encode('utf-8'))
    text = response.text
    if response.status_code >= 400:
        raise _gateway_error(response, text, what='/chat/completions')
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise PlatformError('上游对话响应不是 JSON', status=502,
                            code='bad_upstream') from exc
    if isinstance(payload, dict) and login_expired(payload) \
            and not payload.get('choices'):
        raise PlatformError(_error_text(payload, '登录已失效'), status=401,
                            code='login_expired', payload=payload)
    return payload if isinstance(payload, dict) else {}


async def open_chat(session: str, body: dict, *, proxy: str | None = None,
                    idle_timeout: float = 120,
                    connect_timeout: float = 10) -> httpx.Response:
    """流式对话：返回**未读取**的上游响应（调用方负责关闭）。"""
    async with config.http_client(
            httpx.Timeout(idle_timeout, connect=connect_timeout),
            proxy=_selected_proxy(proxy), lane='loomy') as client:
        request = client.build_request(
            'POST', _url('chat/completions'),
            headers=model_headers(session),
            content=json.dumps(body, ensure_ascii=False).encode('utf-8'))
        response = await client.send(request, stream=True)
        if response.status_code >= 400:
            text = (await response.aread()).decode('utf-8', errors='replace')
            await response.aclose()
            raise _gateway_error(response, text, what='/chat/completions')
        # 实测：会话失效时上游对**流式**请求也回 HTTP 200 +
        # `application/json` 的 `{"code":"100002",…}`（不是 SSE）。照原样透传
        # 会把一段 JSON 当成 SSE 发给客户端，而且永远走不到重登分支，所以
        # 非 SSE 的响应在这里一次性读完并按错误归类。
        ctype = str(response.headers.get('content-type') or '').lower()
        if 'text/event-stream' not in ctype:
            text = (await response.aread()).decode('utf-8', errors='replace')
            await response.aclose()
            try:
                payload = json.loads(text) if text else {}
            except ValueError:
                payload = {}
            payload = payload if isinstance(payload, dict) else {}
            if login_expired(payload) and not payload.get('choices'):
                raise PlatformError(_error_text(payload, '登录已失效'), status=401,
                                    code='login_expired', payload=payload)
            raise PlatformError(
                _error_text(payload, text[:200] or '/chat/completions 未返回 SSE'),
                status=502, code='bad_upstream', payload=payload)
        return response
