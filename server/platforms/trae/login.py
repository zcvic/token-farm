"""Trae 网页登录：本机回环回调 + 浏览器授权。

两个区的授权流程不一样（2026-11-01 从真实客户端逆向确认）：

  · **国内版**：不带 `code_challenge`，授权页走 refreshToken 分支，回调直接
    给 `refreshToken`，复用 `client.exchange_token` 轮换即可，不需要设备证明。
  · **国际版**：客户端**始终**带 PKCE（S256）。授权页回调给的是
    `authCodeInfo`（内含一次性 `AuthCode`），本机用自己保存的 `code_verifier`
    调 `client.exchange_token_by_auth_code` 兑换；请求体带 `DeviceInfo`，
    **不带** `DeviceProof`。设备身份优先复用本机国际版客户端（`icube-dc`
    密钥 + `telemetry.machineId`）；本机没有客户端时，面板自己生成一套
    稳定的 P-256 密钥对 / 19 位设备号 / 64 位机器号并保存在
    `data/platforms/trae/device.json`，因此远程部署或全新机器也能直接登录。
    私钥只在本地读写，不落日志、不回传接口。

设计约束：
  · 授权页对 `auth_callback_url` 有强校验：必须严格匹配
    `^http://127\\.0\\.0\\.1:(\\d+)/authorize$`，所以只能由**本机**起临时
    HTTP 服务收回调——面板后端与浏览器同机时可用，远程部署时让用户粘贴
    地址栏里的完整链接（`authCodeInfo` 同样在链接里）。
  · 一次性凭据：国内版的 `refreshToken`、国际版的 `AuthCode` 都只能用一次，
    兑换后必须立刻用返回的新 refreshToken 落盘。
  · 每个登录会话独占一个随机端口，回调靠端口 + `loginTraceID` 双重要素
    归属到会话，避免别的页面误投；PKCE verifier 只存在内存会话里。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import secrets
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from ..base import PlatformError
from . import client, desktop, deviceproof, fingerprint, store
from .constants import (
    CLIENT_ID,
    IDE_VERSION,
    INTL_IDE_VERSION,
    INTL_LOGIN_PLUGIN_VERSION,
    LOGIN_PLUGIN_VERSION,
)

logger = logging.getLogger('workbuddy.platforms')

LOGIN_TTL = 300                     # 会话有效期（秒）：够用户登录 + 切浏览器
_KEEP_DONE = 600                    # 完成后保留多久供前端轮询（秒）
_LOGIN_BASE = {
    'cn': 'https://www.trae.cn/authorization',
    'intl': 'https://www.trae.ai/authorization',
}
# 两个区的授权页上报的客户端版本不同；沿用真实客户端 / 本机安装版本。
_LOGIN_CLIENT = {
    'cn': {'app_version': IDE_VERSION, 'plugin_version': LOGIN_PLUGIN_VERSION},
    'intl': {'app_version': INTL_IDE_VERSION,
             'plugin_version': INTL_LOGIN_PLUGIN_VERSION},
}

_sessions: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


def _now() -> float:
    return time.time()


def _prune(now: float) -> None:
    """清掉过期很久的会话，避免 `_sessions` 无限增长。"""
    for key, sess in list(_sessions.items()):
        if now - float(sess.get('updated') or sess.get('created') or now) > _KEEP_DONE:
            _sessions.pop(key, None)


def _close_server(session: dict[str, Any]) -> None:
    server = session.get('server')
    if server is None:
        return
    session['server'] = None
    try:
        server.shutdown()
        server.server_close()
    except Exception:  # noqa: BLE001 —— 关闭失败不该影响登录结果
        logger.debug('trae login server close failed', exc_info=True)


def _expire(login_id: str) -> None:
    with _lock:
        session = _sessions.get(login_id)
        if not session or session.get('state') != 'pending':
            return
        session['state'] = 'expired'
        session['updated'] = _now()
    _close_server(session)


def _pkce_pair() -> tuple[str, str]:
    """生成 PKCE 的 `(verifier, challenge)`，与客户端 `Mxe()` 同形。

    `verifier` = 48 字节随机数的 base64url（无填充，64 个字符）；
    `challenge` = base64url(sha256(verifier))。
    """
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode('ascii')).digest()
    challenge = base64.urlsafe_b64encode(digest).decode('ascii').rstrip('=')
    return verifier, challenge


def _build_url(port: int, trace: str, machine: str, device: str,
               realm: str, challenge: str = '') -> str:
    from .fingerprint import hardware_brand, os_version

    client_conf = _LOGIN_CLIENT[realm]
    query = {
        'login_version': '1',
        'auth_from': 'trae',
        'login_channel': 'native_ide',
        'plugin_version': client_conf['plugin_version'],
        'auth_type': 'local',
        'client_id': CLIENT_ID,
        'redirect': '0',
        'login_trace_id': trace,
        'auth_callback_url': f'http://127.0.0.1:{port}/authorize',
        'machine_id': machine,
        'device_id': device,
        'x_device_id': device,
        'x_machine_id': machine,
        'x_device_brand': hardware_brand(),
        'x_device_type': 'windows',
        'x_os_version': os_version(),
        'x_env': '',
        'x_app_version': client_conf['app_version'],
        'x_app_type': 'stable',
    }
    if challenge:
        # 国际版客户端始终走 PKCE：回调给 `authCodeInfo`，由本机 verifier 兑换。
        query['code_challenge'] = challenge
        query['code_challenge_method'] = 'S256'
    query['channel_name'] = 'common'
    return _LOGIN_BASE[realm] + '?' + urllib.parse.urlencode(
        query, quote_via=urllib.parse.quote)


_PAGE = ('<!doctype html><meta charset="utf-8"><title>{title}</title>'
         '<body style="font-family:system-ui,sans-serif;padding:48px;line-height:1.6">'
         '<h2>{title}</h2><p>{message}</p></body>')


def _page(title: str, message: str) -> bytes:
    return _PAGE.format(title=title, message=message).encode('utf-8')


def _query_params(query: str) -> dict[str, str]:
    return {
        key: (values[0] if values else '')
        for key, values in urllib.parse.parse_qs(
            query, keep_blank_values=True).items()
    }


_MAX_CALLBACK_LEN = 16 * 1024
_LOOPBACK_HOSTS = {'127.0.0.1', 'localhost', '::1'}


def parse_callback_url(text: str) -> dict[str, str]:
    """从用户粘贴的地址里取出回调参数。

    远程部署时浏览器访问不到服务器上的 `127.0.0.1`，授权页会停在错误页；
    但地址栏里的完整链接仍然带着 `refreshToken`。这里同时接受：

      · 完整 URL：`http://127.0.0.1:<port>/authorize?refreshToken=…`；
      · 省略协议的 URL：`127.0.0.1:<port>/authorize?refreshToken=…`；
      · 纯查询串：`refreshToken=…&loginTraceID=…`（含前导 `?` 也可以）。

    不解析 `refreshToken` 的语义，只做**形状**校验：回调主机必须是本机回环，
    避免把一段来路不明的链接当成有效回调。
    """
    raw = str(text or '').strip()
    if not raw:
        raise PlatformError('请粘贴浏览器地址栏里的完整回调地址', status=400,
                            code='callback_required')
    if len(raw) > _MAX_CALLBACK_LEN:
        raise PlatformError('回调地址过长', status=400, code='callback_too_long')

    if '://' in raw:
        parts = urllib.parse.urlsplit(raw)
        host = str(parts.hostname or '').strip().lower()
        if host and host not in _LOOPBACK_HOSTS:
            raise PlatformError('回调地址应来自 127.0.0.1（浏览器地址栏里的链接）',
                                status=400, code='callback_host_invalid')
        query = parts.query or parts.fragment
    elif '?' in raw:
        before, _, query = raw.partition('?')
        authority = before.split('/', 1)[0]
        host = authority.rsplit(':', 1)[0].strip('[]').lower() if ':' in authority else ''
        if host and host not in _LOOPBACK_HOSTS:
            raise PlatformError('回调地址应来自 127.0.0.1（浏览器地址栏里的链接）',
                                status=400, code='callback_host_invalid')
    elif '=' in raw:
        query = raw.lstrip('?')
    else:
        raise PlatformError('无法识别回调地址，请复制浏览器地址栏里的完整链接',
                            status=400, code='callback_invalid')

    params = _query_params(query)
    if not params:
        raise PlatformError('回调地址里没有参数，请复制包含 refreshToken 的完整链接',
                            status=400, code='callback_invalid')
    return params


def _auth_code_of(params: dict[str, str]) -> str:
    """从 `authCodeInfo`（授权页回传的 JSON 字符串）里取一次性 `AuthCode`。"""
    raw = str(params.get('authCodeInfo') or '').strip()
    if not raw:
        return ''
    try:
        body = json.loads(raw)
    except ValueError:
        logger.warning('trae login authCodeInfo 解析失败')
        return ''
    if isinstance(body, dict):
        return str(body.get('AuthCode') or '').strip()
    return ''


def _accept_callback(login_id: str, params: dict[str, str]) -> str:
    """校验一次回调并把会话推进到 `exchanging`；返回给用户看的结果文案。

    HTTP 回调与「粘贴地址」走同一条路径，校验口径不会分叉。
    """
    login_id = str(login_id or '')
    with _lock:
        session = _sessions.get(login_id)
        if session is None:
            raise PlatformError('登录会话不存在或已过期，请重新发起',
                                status=404, code='login_not_found')
        state = str(session.get('state') or 'pending')
        expected_trace = str(session.get('trace') or '')
    if state == 'success':
        return '登录已完成。'
    if state != 'pending':
        raise PlatformError('登录会话已失效，请重新发起',
                            status=409, code='login_not_pending')

    trace = str(params.get('loginTraceID') or '')
    if trace and trace != expected_trace:
        logger.warning('trae login callback trace mismatch')
        raise PlatformError('登录回调与请求不匹配，请重新发起。',
                            status=400, code='login_trace_mismatch')

    error_code = str(params.get('error_code') or '').strip()
    if error_code and error_code not in ('0', ''):
        message = str(params.get('error_msg') or '') or '授权被拒绝。'
        _finish_error(login_id, message)
        raise PlatformError(message, status=400, code='login_denied')

    refresh = str(params.get('refreshToken') or params.get('data') or '').strip()
    auth_code = _auth_code_of(params)
    if not refresh and not auth_code:
        _finish_error(login_id, '回调里没有 refreshToken / AuthCode')
        raise PlatformError('回调里没有 refreshToken 或 AuthCode，请重试。',
                            status=400, code='login_no_credential')

    with _lock:
        session = _sessions.get(login_id)
        # 两个请求同时提交同一链接时，只有第一个能推进状态。
        if session is None:
            raise PlatformError('登录会话不存在或已过期，请重新发起',
                                status=404, code='login_not_found')
        if session.get('state') != 'pending':
            raise PlatformError('登录会话已失效，请重新发起',
                                status=409, code='login_not_pending')
        session['state'] = 'exchanging'
        session['updated'] = _now()
        realm = str(session.get('realm') or 'cn')
        verifier = str(session.get('codeVerifier') or '')
        device = session.get('device') or {}

    threading.Thread(target=_finish,
                     args=(login_id, realm, refresh, auth_code, verifier, params,
                           device),
                     name='trae-login-finish', daemon=True).start()
    return '正在换取令牌，可以关闭此窗口并回到面板。'


def _handler_factory(login_id: str):
    class Handler(BaseHTTPRequestHandler):
        def _respond(self, status: int, title: str, message: str) -> None:
            body = _page(title, message)
            self.send_response(status)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            parts = urllib.parse.urlsplit(self.path)
            if parts.path != '/authorize':
                self._respond(404, '页面不存在', '该地址只接受 /authorize 回调。')
                return
            params = _query_params(parts.query)
            try:
                message = _accept_callback(login_id, params)
            except PlatformError as exc:
                title = {
                    'login_not_found': '登录会话已失效',
                    'login_not_pending': '登录会话已失效',
                    'login_trace_mismatch': '校验失败',
                }.get(exc.code, '授权失败')
                self._respond(exc.status, title, exc.message)
                return
            self._respond(200, '授权完成', message)

        def log_message(self, *args: object) -> None:  # noqa: D102
            pass

    return Handler


def _finish_error(login_id: str, message: str) -> None:
    with _lock:
        session = _sessions.get(login_id)
        if not session:
            return
        session['state'] = 'error'
        session['error'] = message
        session['updated'] = _now()
    if session:
        _close_server(session)


def _iso_ms(value: object) -> str:
    """毫秒时间戳（可能是字符串，回调参数就是字符串）→ ISO8601。"""
    text = str(value or '').strip()
    if not text:
        return ''
    return client.iso_from_ms(text) or text


def _finish(login_id: str, realm: str, refresh: str, auth_code: str,
            code_verifier: str, params: dict[str, str],
            device: dict[str, Any]) -> None:
    async def run() -> dict[str, Any]:
        account = {'realm': realm, 'refreshToken': refresh, 'uid': ''}
        # 设备身份既用于本次兑换的 DeviceInfo，也要随账号落盘供后续刷新签名。
        if device:
            account.update({
                'deviceId': str(device.get('deviceId') or ''),
                'machineId': str(device.get('machineId') or ''),
                'deviceKeyPEM': str(device.get('deviceKeyPEM') or ''),
                'devicePublicKeyPEM': str(device.get('devicePublicKeyPEM') or ''),
            })
        if auth_code:
            # 国际版：授权码 + 本机 PKCE verifier（客户端同款兑换）。
            fresh = await client.exchange_token_by_auth_code(
                account, auth_code, code_verifier)
        else:
            fresh = await client.exchange_token(account)
        info = await client.fetch_user_info(fresh, fresh['token'])
        result = info.get('Result') if isinstance(info, dict) else None
        if not isinstance(result, dict):
            result = {}
        uid = str(result.get('UserID') or '').strip()
        if not uid:
            raise PlatformError('GetUserInfo 未返回 UserID，无法落盘',
                                status=502, code='no_user_id')
        # 国际版用量接口按区选主机（US / SG）；从登录响应里能取到就落盘，
        # 取不到时后续按 SG 处理（见 client.intl_entitlement_hosts）。
        hints: list[object] = [result, info, params]
        for key in ('userInfo', 'authCodeInfo'):
            try:
                hints.append(json.loads(str(params.get(key) or '{}')))
            except ValueError:
                continue
        region = ''
        for hint in hints:
            region = client.region_of(hint)
            if region:
                break
        payload = {
            'uid': uid,
            'realm': realm,
            'region': region,
            'token': fresh.get('token') or '',
            'refreshToken': fresh.get('refreshToken') or refresh,
            'expiredAt': fresh.get('expiredAt') or '',
            'refreshExpiredAt': fresh.get('refreshExpiredAt')
            or _iso_ms(params.get('refreshExpireAt')),
            'nickname': str(result.get('ScreenName') or ''),
            'email': str(result.get('NonPlainTextEmail') or ''),
            'source': 'web',
        }
        if device:
            payload.update({
                'deviceId': str(device.get('deviceId') or ''),
                'machineId': str(device.get('machineId') or ''),
                'deviceKeyPEM': str(device.get('deviceKeyPEM') or ''),
                'devicePublicKeyPEM': str(device.get('devicePublicKeyPEM') or ''),
            })
        # 延迟导入：adapter 反向依赖 client/store，模块级导入会成环。
        from .. import registry  # noqa: PLC0415
        adapter = registry.get('trae')
        if adapter is None:
            raise PlatformError('Trae 平台未注册', status=500, code='platform_missing')
        return adapter.import_account(payload)

    error = ''
    account: dict[str, Any] | None = None
    try:
        account = asyncio.run(run())
    except PlatformError as exc:
        error = exc.message
        logger.warning('trae web login failed: %s', exc.message)
    except Exception as exc:  # noqa: BLE001 —— 登录线程不能把异常抛给调用方
        error = f'登录失败：{exc}'
        logger.warning('trae web login failed', exc_info=True)

    with _lock:
        session = _sessions.get(login_id)
        if not session:
            return
        if account is not None:
            session['state'] = 'success'
            session['account'] = account
        else:
            session['state'] = 'error'
            session['error'] = error or '登录失败'
        session['updated'] = _now()
    if session:
        _close_server(session)


def _panel_device_identity() -> dict[str, Any]:
    """面板级设备身份：读缓存，没有就生成一套并落盘。

    客户端启动时也是自己生成密钥对的，不依赖先登录；面板在服务器 / 新机器
    上同样需要一套稳定身份，否则每次登录都换设备会被上游判成异常。
    """
    cached = store.read_device_identity()
    if cached:
        return cached
    keys = deviceproof.generate_keypair()
    identity = {
        # 与客户端同形：19 位数字设备号 + 64 位十六进制机器号。
        'deviceId': str(10 ** 18 + secrets.randbelow(9 * 10 ** 18)),
        'machineId': secrets.token_hex(32),
        'deviceKeyPEM': keys['privateKeyPEM'],
        'devicePublicKeyPEM': keys['publicKeyPEM'],
        'createdAt': int(_now()),
    }
    try:
        store.save_device_identity(identity)
    except (OSError, ValueError):
        # 落盘失败不该阻断登录：本次仍能兑换，只是下次会换一套身份。
        logger.warning('面板设备身份保存失败', exc_info=True)
    return identity


def _login_device(realm: str) -> dict[str, Any] | None:
    """登录用的设备身份（只有国际版需要）。

    优先本机客户端：与用户自己打开 IDE 时是同一台设备，绑定关系最自然。
    没有客户端时退回面板自己生成的身份，不再要求先安装 Trae 国际版。
    """
    if realm != 'intl':
        return None
    for item in desktop.read_devices():
        if item.get('realm') != 'intl':
            continue
        pem = str(item.get('privateKeyPEM') or '').strip()
        holder = str(item.get('holderId') or '').strip()
        if not pem or not holder:
            continue
        machine = str(item.get('machineId') or '').strip() \
            or fingerprint.host_fingerprint()[0]
        return {
            'deviceId': holder,
            'machineId': machine,
            'deviceKeyPEM': pem,
            'devicePublicKeyPEM': str(item.get('publicKeyPEM') or ''),
        }
    return _panel_device_identity()


def start(realm: str = 'cn') -> dict[str, Any]:
    """发起一次网页登录，返回 `{login_id, url, expires_in}`。"""
    realm = str(realm or 'cn').strip().lower()
    if realm not in _LOGIN_BASE:
        realm = 'cn'

    device = _login_device(realm)
    if device:
        machine = str(device.get('machineId') or '')
        device_id = str(device.get('deviceId') or '')
    else:
        machine, device_id = fingerprint.host_fingerprint()
    login_id = uuid.uuid4().hex
    trace = str(uuid.uuid4())
    # 国际版客户端始终走 PKCE；verifier 只留在内存会话里，兑换时用。
    code_verifier, code_challenge = '', ''
    if realm == 'intl':
        code_verifier, code_challenge = _pkce_pair()

    server = ThreadingHTTPServer(('127.0.0.1', 0),
                                 _handler_factory(login_id))
    server.daemon_threads = True
    port = int(server.server_address[1])
    url = _build_url(port, trace, machine, device_id, realm, code_challenge)

    now = _now()
    session = {
        'id': login_id,
        'realm': realm,
        'trace': trace,
        'codeVerifier': code_verifier,
        'device': device or {},
        'state': 'pending',
        'error': '',
        'account': None,
        'server': server,
        'created': now,
        'updated': now,
    }
    with _lock:
        _prune(now)
        _sessions[login_id] = session

    threading.Thread(target=server.serve_forever, name='trae-login-server',
                     daemon=True).start()
    timer = threading.Timer(LOGIN_TTL, _expire, args=(login_id,))
    timer.daemon = True
    timer.start()
    return {'login_id': login_id, 'url': url, 'expires_in': LOGIN_TTL}


def poll(login_id: str) -> dict[str, Any]:
    """查询登录状态。只返回脱敏后的账号（`store.public_account`）。"""
    with _lock:
        session = _sessions.get(str(login_id or ''))
        if not session:
            raise PlatformError('登录会话不存在或已过期', status=404,
                                code='login_not_found')
        state = str(session.get('state') or 'pending')
        account = session.get('account')
        error = str(session.get('error') or '')
        realm = str(session.get('realm') or 'cn')
    return {
        'login_id': str(login_id),
        'status': state,
        'realm': realm,
        'account': account,
        'message': error,
    }


def submit_callback(login_id: str, callback: str) -> dict[str, Any]:
    """远程部署的兜底：接收用户粘贴的回调地址并完成兑换。

    适用场景：面板后端跑在服务器上，而 Trae 只肯回调浏览器所在电脑的
    `127.0.0.1:<port>`。授权页会显示「无法访问」，但地址栏里的链接仍然
    有效——把它交回后端，就能复用本机回调的同一套校验与兑换流程。
    """
    params = parse_callback_url(callback)
    _accept_callback(login_id, params)
    return poll(login_id)


def cancel(login_id: str) -> bool:
    with _lock:
        session = _sessions.pop(str(login_id or ''), None)
    if session is None:
        return False
    _close_server(session)
    return True
