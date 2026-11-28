"""Accio 网页登录：OAuth 2.0 + PKCE（S256）+ 本机回环回调。

流程（2026-11 从官方客户端与真机抓包确认，见 `deploy/accio-ref.txt`）：

  1. 生成 PKCE `(verifier, challenge)`、`state`（32 字节 hex）、`ttid`；
  2. 在本机 `127.0.0.1:<随机端口>` 起一个临时 HTTP 服务，回调路径
     `/auth/callback`（与 `return_url` 严格一致）；
  3. 让用户打开授权页 `{登录域名}/login?return_url=…&state=…&code_challenge=…`；
  4. 授权完成后浏览器回到本机回调，用 `code` + verifier 调
     `POST /api/oauth/token` 换 `accessToken` / `refreshToken` / `expiresAt`。

## 三个容易踩的点

  · `client_id='accio-work'` 是**公开客户端**，没有 secret；
  · `return_url` **没有白名单**，所以用本机回环地址；没有 `code_challenge`
    会命中 legacy 门禁弹「请升级客户端」；
  · `state` 必须**原样比对**（缺失也拒绝），否则一个随机回调就能把别人的
    授权码塞进当前会话。

远程部署时浏览器回不到服务器上的 `127.0.0.1`：面板会停在错误页，但地址栏
里的完整链接有效——把它粘回面板即可（`submit_callback`）。

## 区域

两个区域只差登录页域名（`cn` → `www.accio-ai.com`，`global` →
`zh.accio.com`）；网关与 token 通用，但服务端按账号下发实际区域，**登录前
无法自动判断**，所以发起登录时必须由用户先选区域。
"""
from __future__ import annotations

import asyncio
import html
import logging
import secrets
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from ..base import PlatformError
from . import client, constants as C, protocol as P, store

logger = logging.getLogger('workbuddy.platforms.accio')

_MAX_PENDING = 8
_MAX_CALLBACK_LEN = 16 * 1024
_KEEP_DONE = 600
_LOOPBACK_HOSTS = {'127.0.0.1', 'localhost', '::1'}

_sessions: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


def _now() -> float:
    return time.time()


def _prune(now: float) -> None:
    for key, sess in list(_sessions.items()):
        if now - float(sess.get('updated') or sess.get('created') or now) \
                > _KEEP_DONE:
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
        logger.debug('accio login server close failed', exc_info=True)


def _expire(login_id: str) -> None:
    with _lock:
        session = _sessions.get(login_id)
        if not session or session.get('status') != 'pending':
            return
        session['status'] = 'expired'
        session['updated'] = _now()
    _close_server(session)


def _set_status(login_id: str, status: str, message: str = '',
                account: dict | None = None) -> None:
    with _lock:
        session = _sessions.get(login_id)
        if session is None:
            return
        session['status'] = status
        session['error'] = str(message or '')
        session['updated'] = _now()
        if account is not None:
            session['account'] = account


# ── 事件循环 / 线程 ─────────────────────────────────────────
def _run_sync(coro):
    """在同步调用里跑协程（当前线程可能已有事件循环）。

    `login_start` 由路由**直接**调用（不是 `to_thread`），此时当前线程里有
    正在跑的事件循环，`asyncio.run` 会报错；回环回调线程则没有事件循环。
    两处都必须能用，所以有循环时换到工作线程。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    box: dict[str, Any] = {}

    def runner() -> None:
        try:
            box['value'] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001 —— 原样回抛给调用方
            box['error'] = exc

    thread = threading.Thread(target=runner, name='accio-login', daemon=True)
    thread.start()
    thread.join()
    if 'error' in box:
        raise box['error']
    return box.get('value')


# ── 回调解析 ────────────────────────────────────────────────
def _query_params(query: str) -> dict[str, str]:
    return {key: (values[0] if values else '')
            for key, values in urllib.parse.parse_qs(
                query, keep_blank_values=True).items()}


def parse_callback_url(text: str) -> dict[str, str]:
    """从用户粘贴的地址里取出回调参数（只做形状校验）。"""
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
            raise PlatformError(
                '回调地址应来自 127.0.0.1（浏览器地址栏里的链接）',
                status=400, code='callback_host_invalid')
        query = parts.query or parts.fragment
    elif '?' in raw:
        before, _, query = raw.partition('?')
        authority = before.split('/', 1)[0]
        host = (authority.rsplit(':', 1)[0].strip('[]').lower()
                if ':' in authority else '')
        if host and host not in _LOOPBACK_HOSTS:
            raise PlatformError(
                '回调地址应来自 127.0.0.1（浏览器地址栏里的链接）',
                status=400, code='callback_host_invalid')
    elif '=' in raw:
        query = raw.lstrip('?')
    else:
        raise PlatformError('无法识别回调地址，请复制浏览器地址栏里的完整链接',
                            status=400, code='callback_invalid')

    params = _query_params(query)
    if not params:
        raise PlatformError('回调地址里没有参数，请复制包含 code 的完整链接',
                            status=400, code='callback_invalid')
    return params


def _state_matches(session: dict, value: object) -> bool:
    expected = str(session.get('oauthState') or '')
    got = str(value or '')
    return bool(expected) and secrets.compare_digest(expected, got)


# ── 兑换 ────────────────────────────────────────────────────
def _finish_login(login_id: str, code: str) -> dict:
    """用某个会话的 verifier 兑换 token，落盘账号，推进到 success。"""
    with _lock:
        session = dict(_sessions.get(login_id) or {})
        current = _sessions.get(login_id)
        if current is not None and current.get('status') == 'pending':
            current['status'] = 'exchanging'
            current['updated'] = _now()
    if not session:
        raise PlatformError('登录会话不存在或已过期', status=404,
                            code='login_not_found')

    realm = store.normalize_realm(session.get('realm'))
    proxy = _proxy_for()
    try:
        token = _run_sync(client.exchange_code(
            code, str(session.get('verifier') or ''),
            str(session.get('redirectUri') or ''),
            proxy=proxy, timeout=C.REQUEST_TIMEOUT))
    except PlatformError as exc:
        _set_status(login_id, 'error', exc.message)
        _close_server(session)
        raise
    except Exception as exc:  # noqa: BLE001 —— 登录线程不能把异常抛给调用方
        message = f'登录失败：{exc}'
        _set_status(login_id, 'error', message)
        _close_server(session)
        raise PlatformError(message, status=502, code='login_failed') from exc

    # 先探一次身份：`userId` 让 uid 落在稳定身份上（access token 会轮换，
    # 不能拿它当长期 uid），顺带拿 `routeRegion` / `activeTeam.teamId`。
    probe = {
        'realm': realm,
        'accessToken': token.get('access_token') or '',
        'refreshToken': token.get('refresh_token') or '',
        'sgcookie': token.get('sgcookie') or '',
    }
    info: dict = {}
    try:
        info = _run_sync(client.user_info(
            probe, str(token.get('access_token') or ''),
            proxy=proxy, timeout=C.REQUEST_TIMEOUT))
    except PlatformError:
        info = {}

    team = info.get('activeTeam') if isinstance(info.get('activeTeam'), dict) \
        else {}
    payload: dict[str, Any] = {
        'realm': realm,
        'access_token': token.get('access_token') or '',
        'refresh_token': token.get('refresh_token') or '',
        'expires_at': token.get('expires_at'),
        'sgcookie': token.get('sgcookie') or '',
        'user_id': info.get('userId') or '',
        'user_name': info.get('userName') or '',
        'nickname': info.get('userName') or '',
        'route_region': info.get('routeRegion') or '',
        'team_id': team.get('teamId') or '',
        'team_name': team.get('teamName') or '',
        'source': 'web',
        'loginState': '正常',
    }
    from .. import registry  # noqa: PLC0415 —— 避免 adapter ↔ login 循环导入
    adapter = registry.get('accio')
    if adapter is None:
        message = 'Accio 平台未注册'
        _set_status(login_id, 'error', message)
        raise PlatformError(message, status=500, code='platform_missing')
    try:
        account = adapter.import_account(payload)
    except PlatformError as exc:
        _set_status(login_id, 'error', exc.message)
        _close_server(session)
        raise
    _set_status(login_id, 'success', '', account)
    _close_server(session)
    return {'status': 'success', 'account': account, 'message': '登录成功'}


def _proxy_for() -> str | None:
    """登录阶段还没有账号，只能跟随全局出口设置。"""
    return None


# ── 回环回调服务 ────────────────────────────────────────────
_PAGE = ('<!doctype html><meta charset="utf-8"><title>{title}</title>'
         '<body style="font-family:system-ui,sans-serif;padding:48px;line-height:1.6">'
         '<h2>{title}</h2><p>{message}</p></body>')


def _page(title: str, message: str) -> bytes:
    return _PAGE.format(title=html.escape(title),
                        message=html.escape(message)).encode('utf-8')


def _handler_factory(login_id: str):
    class Handler(BaseHTTPRequestHandler):
        def _respond(self, status: int, title: str, message: str) -> None:
            body = _page(title, message)
            self.send_response(status)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(body)

        def _origin_allowed(self) -> bool:
            address = getattr(self.server, 'server_address', ('127.0.0.1', 0))
            port = int(address[1])
            host = str(self.headers.get('Host') or '').strip().lower()
            if host not in {f'127.0.0.1:{port}', f'localhost:{port}',
                            f'[::1]:{port}'}:
                return False
            origin = str(self.headers.get('Origin') or '').strip().lower()
            if origin and origin not in {f'http://127.0.0.1:{port}',
                                         f'http://localhost:{port}',
                                         f'http://[::1]:{port}'}:
                return False
            return True

        def do_GET(self) -> None:  # noqa: N802
            if not self._origin_allowed():
                self._respond(403, '拒绝访问', '该地址只接受本机授权回调。')
                return
            parts = urllib.parse.urlsplit(self.path)
            if parts.path != C.CALLBACK_PATH:
                self._respond(404, '页面不存在',
                              f'该地址只接受 {C.CALLBACK_PATH} 回调。')
                return
            self._handle(_query_params(parts.query))

        def _handle(self, params: dict[str, str]) -> None:
            with _lock:
                session = dict(_sessions.get(login_id) or {})
            if not session:
                self._respond(410, '登录会话已失效', '请回到面板重新发起登录。')
                return
            error = str(params.get('error') or '').strip()
            if error:
                detail = (str(params.get('error_description') or '').strip()
                          or error)
                _set_status(login_id, 'error', detail)
                self._respond(400, '授权失败', detail)
                return
            if not _state_matches(session, params.get('state')):
                _set_status(login_id, 'error', '回调 state 不匹配，请重新登录')
                self._respond(400, '授权失败', '回调 state 不匹配，请重新登录。')
                return
            code = str(params.get('code') or '').strip()
            if not code:
                self._respond(400, '登录失败', '回调里缺少 code，请重试。')
                return
            # 先回提示页，再在后台兑换：授权码兑换可能耗时数秒，让浏览器
            # 一直转圈体验很差；面板会轮询到 exchanging → success。
            self._respond(200, '授权完成',
                          '已收到授权，正在完成登录，请回到面板查看结果。')
            threading.Thread(target=self._background_exchange,
                             args=(code,), name='accio-login-exchange',
                             daemon=True).start()

        def _background_exchange(self, code: str) -> None:
            try:
                _finish_login(login_id, code)
            except Exception:  # noqa: BLE001 —— 状态已写进会话，这里只记日志
                logger.debug('accio login exchange failed', exc_info=True)

        def log_message(self, *args: object) -> None:  # noqa: D102
            pass

    return Handler


# ── 对外接口 ────────────────────────────────────────────────
def start(realm: str | None = None) -> dict[str, Any]:
    """发起一次网页登录，返回 `{login_id, url, realm, expires_in}`。"""
    region = store.normalize_realm(realm)
    login_id = uuid.uuid4().hex
    verifier, challenge = P.pkce_pair()
    oauth_state = P.new_state()

    server = ThreadingHTTPServer(('127.0.0.1', 0),
                                 _handler_factory(login_id))
    server.daemon_threads = True
    port = int(server.server_address[1])
    redirect_uri = f'http://127.0.0.1:{port}{C.CALLBACK_PATH}'
    url = P.build_auth_url(region, redirect_uri, oauth_state, challenge)

    now = _now()
    with _lock:
        _prune(now)
        pending = sum(1 for item in _sessions.values()
                      if str(item.get('status') or '')
                      in ('pending', 'exchanging'))
        reject = pending >= _MAX_PENDING
        if not reject:
            _sessions[login_id] = {
                'id': login_id,
                'realm': region,
                'verifier': verifier,
                'challenge': challenge,
                'oauthState': oauth_state,
                'redirectUri': redirect_uri,
                'port': port,
                'status': 'pending',
                'error': '',
                'account': None,
                'server': server,
                'created': now,
                'updated': now,
            }
    if reject:
        try:
            server.server_close()
        except Exception:  # noqa: BLE001
            logger.debug('accio login server close failed', exc_info=True)
        raise PlatformError('同时进行的登录会话过多，请稍后重试',
                            status=429, code='too_many_sessions')

    threading.Thread(target=server.serve_forever,
                     name='accio-login-server', daemon=True).start()
    timer = threading.Timer(C.LOGIN_TIMEOUT, _expire, args=(login_id,))
    timer.daemon = True
    timer.start()
    return {'login_id': login_id, 'url': url, 'realm': region,
            'expires_in': C.LOGIN_TIMEOUT}


def poll(login_id: str) -> dict[str, Any]:
    with _lock:
        session = _sessions.get(str(login_id or ''))
        if not session:
            raise PlatformError('登录会话不存在或已过期', status=404,
                                code='login_not_found')
        return {
            'login_id': str(login_id),
            'status': str(session.get('status') or 'pending'),
            'realm': str(session.get('realm') or C.DEFAULT_REALM),
            'account': session.get('account'),
            'message': str(session.get('error') or ''),
        }


def submit_callback(login_id: str, callback: str) -> dict[str, Any]:
    """远程部署的兜底：接收用户粘贴的回调地址并完成兑换。"""
    params = parse_callback_url(callback)
    error = str(params.get('error') or '').strip()
    if error:
        detail = str(params.get('error_description') or '').strip() or error
        _set_status(str(login_id or ''), 'error', detail)
        raise PlatformError(f'授权失败：{detail}', status=400,
                            code='login_failed')
    with _lock:
        session = _sessions.get(str(login_id or ''))
        if session is None:
            raise PlatformError('登录会话不存在或已过期', status=404,
                                code='login_not_found')
        snapshot = dict(session)
    if snapshot.get('status') not in ('pending', 'exchanging'):
        raise PlatformError('该登录会话已经结束，请重新发起登录', status=409,
                            code='login_finished')
    if not _state_matches(snapshot, params.get('state')):
        raise PlatformError('回调 state 不匹配，请重新发起登录', status=400,
                            code='callback_state_invalid')
    code = str(params.get('code') or '').strip()
    if not code:
        raise PlatformError('回调地址里没有 code（请等页面跳转完成后再复制）',
                            status=400, code='login_no_credential')
    _finish_login(str(login_id), code)
    return poll(login_id)


def cancel(login_id: str) -> bool:
    with _lock:
        session = _sessions.pop(str(login_id or ''), None)
    if session is None:
        return False
    _close_server(session)
    return True
