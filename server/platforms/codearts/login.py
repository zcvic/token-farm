"""CodeArts 网页登录：本机回环回调 + ticket 轮询双通道。

流程（2026-09 从官方客户端与真机抓包确认，见 `deploy/codearts-ref.txt`）：

  1. 生成 `ticket_id` / `secret`（各 16 字节 hex）、PKCE 的
     `(verifier, challenge)`、以及一把**长期复用**的 DPoP 私钥；
  2. 在本机 `127.0.0.1:<随机端口>` 起一个临时 HTTP 服务，回调路径
     `/oauth/callback`（与 `redirect_uri` 严格一致）；
  3. 让用户打开授权页 `{PortalHost}/authorize?...`；
  4. Portal **两次**回调本机：

       · 第一次带 `secret` + `redirect`，不带 `code` —— 按 307 跳去 `redirect`
         （华为登录页），把 `secret` 记到会话上；
       · 第二次**只带 `code`**（实测 2026-09-16：`has_code=true
         has_secret=false has_redirect=false`）。

  5. 用 `code` + 本机 `verifier` 调 STS `/v1/oauth2/tokens` 换 STS 临时凭据。

因为第二次回调**无法**自证属于哪个会话，候选顺序是：

  1. `secret` 命中（部分部署会带；带了但不命中即放弃，不再猜）；
  2. `ticket_id` 命中；
  3. 都没有判别键时，按创建时间**倒序**逐个试全部活跃会话。

第 3 种由调用方逐个尝试换取：授权码与 PKCE verifier 一对一校验，用错只回
`STS5.1805` 且**不消耗**授权码，所以逐个试是安全的；只挑其中一个，在服务重启
过或同时开过两次登录时必然错配。

远程部署时浏览器回不到服务器上的 `127.0.0.1`：面板仍会停在错误页，但地址栏
里的完整链接有效——把它粘回面板即可（`submit_callback`），走同一套校验与兑换。

另外保留 ticket 通道（`GET {snap-manager}/v1/login/ticket?ticket_id=&secret=`）：
只有收到过 `secret` 的会话才会去轮询，否则并行轮询会抢在授权码之前存下一份
**不含 refresh_token** 的临时凭据，把账号变成「一小时后必须重新登录」。
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from ..base import PlatformError
from . import client, constants as C, dpop, store

logger = logging.getLogger('workbuddy.platforms.codearts')

LOGIN_TTL = 15 * 60          # 会话有效期（秒）：与参考实现的 oauthSessionTTL 一致
_KEEP_DONE = 600             # 完成后保留多久供前端轮询
_MAX_PENDING = 8             # 同时挂起的会话上限（每个占一个本机端口 + 一条线程）
_MAX_CALLBACK_LEN = 16 * 1024
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
        logger.debug('codearts login server close failed', exc_info=True)


def _expire(login_id: str) -> None:
    with _lock:
        session = _sessions.get(login_id)
        if not session or session.get('state') != 'pending':
            return
        session['state'] = 'expired'
        session['updated'] = _now()
    _close_server(session)


# ── 授权 URL ────────────────────────────────────────────────
def build_authorize_url(ticket_id: str, challenge: str, port: int) -> str:
    """`{PortalHost}/authorize?...`；`code_challenge_method` 只认 "SHA-256"。"""
    query = {
        'theme': '2',
        'locale': 'zh-cn',
        'uri_scheme': C.CLIENT_ID,
        'client_id': C.CLIENT_ID,
        'port': str(int(port)),
        'code_challenge': challenge,
        'code_challenge_method': C.CODE_CHALLENGE_METHOD,
        'ticket_id': ticket_id,
        'plugin-name': C.PLUGIN_NAME,
        'plugin-version': C.PLUGIN_VERSION,
    }
    return (C.PORTAL_HOST + '/authorize?'
            + urllib.parse.urlencode(query, quote_via=urllib.parse.quote))


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


# ── 会话 ────────────────────────────────────────────────────
def _pending_sessions() -> list[dict[str, Any]]:
    with _lock:
        _prune(_now())
        return [sess for sess in _sessions.values()
                if str(sess.get('state') or '') == 'pending']


def candidates(secret: str = '', ticket_id: str = '') -> list[dict[str, Any]]:
    """该授权码可能属于哪些会话（按可信度排序）。

    带了 `secret` 却不命中 → 会话已失效，返回空列表（**不**再往下猜）。
    """
    pending = _pending_sessions()
    if secret:
        return [s for s in pending if s.get('secret') == secret]
    if ticket_id:
        return [s for s in pending if s.get('ticketId') == ticket_id]
    pending.sort(key=lambda s: float(s.get('created') or 0.0), reverse=True)
    return pending


def _record_secret(login_id: str, secret: str) -> None:
    """Portal 第一次回调带来的 `secret`：记到会话上，之后可以走 ticket 轮询。"""
    if not secret:
        return
    with _lock:
        session = _sessions.get(login_id)
        if session is None or session.get('state') != 'pending':
            return
        session['portalSecret'] = secret
        session['ticketFallback'] = True
        session['updated'] = _now()


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


def _exchange_code(session: dict[str, Any], code: str) -> dict:
    """用某个会话的 verifier + DPoP 私钥换一次；失败抛 `PlatformError`。"""
    async def run() -> dict:
        token = await client.exchange_code(
            code, str(session.get('verifier') or ''), session.get('dpopJwk') or {},
            int(session.get('port') or 0))
        payload: dict[str, Any] = {
            'userId': token.get('user_id') or '',
            'userName': token.get('user_name') or '',
            'domainId': token.get('domain_id') or '',
            'accessKeyId': token.get('access_key_id') or '',
            'secretAccessKey': token.get('secret_access_key') or '',
            'securityToken': token.get('security_token') or '',
            'expiredAt': store.iso_from_epoch(token.get('expires_at'))
            or token.get('expiration') or '',
            'refreshToken': token.get('refresh_token') or '',
            'refreshExpiredAt': store.iso_from_epoch(
                _now() + C.REFRESH_TTL_SECONDS),
            'codeVerifier': str(session.get('verifier') or ''),
            'clientId': C.CLIENT_ID,
            'dpopJwk': session.get('dpopJwk') or None,
            'source': 'web',
            'loginState': '正常',
        }
        # 顺手补一次用户名 / 域：token 响应里没有时（旧 ticket 通道）靠它。
        if not payload['userName']:
            try:
                cred = client.Credential(payload['accessKeyId'],
                                         payload['secretAccessKey'],
                                         payload['securityToken'])
                info = await client.current_user(cred)
                payload['userId'] = payload['userId'] or info['user_id']
                payload['userName'] = info['user_name']
                payload['domainId'] = payload['domainId'] or info['domain_id']
            except PlatformError:
                pass
        from .. import registry  # noqa: PLC0415 —— 避免 adapter ↔ login 循环导入
        adapter = registry.get('codearts')
        if adapter is None:
            raise PlatformError('CodeArts 平台未注册', status=500,
                                code='platform_missing')
        return adapter.import_account(payload)

    return asyncio.run(run())


def _accept_code(code: str, *, secret: str = '',
                 ticket_id: str = '') -> dict[str, Any]:
    """把授权码交给候选会话逐个兑换，成功后把会话推进到 `success`。

    返回 `{status, account, message}`。所有候选都失败时抛最后一个错误。
    """
    code = str(code or '').strip()
    if not code:
        raise PlatformError('回调里没有授权码', status=400,
                            code='login_no_credential')
    pool = candidates(secret=secret, ticket_id=ticket_id)
    if not pool:
        raise PlatformError('没有匹配的登录会话，请重新发起登录', status=404,
                            code='login_not_found')

    last_error: PlatformError | None = None
    for session in pool:
        login_id = str(session.get('id') or '')
        with _lock:
            current = _sessions.get(login_id)
            if current is None or current.get('state') != 'pending':
                continue
            current['state'] = 'exchanging'
            current['updated'] = _now()
        try:
            account = _exchange_code(session, code)
        except PlatformError as exc:
            last_error = exc
            with _lock:
                current = _sessions.get(login_id)
                if current is not None and current.get('state') == 'exchanging':
                    current['state'] = 'pending'
                    current['updated'] = _now()
            # 鉴权码与 verifier 一对一：用错只是不匹配，可以继续试下一个。
            if exc.status in (400, 401, 404):
                continue
            _finish_error(login_id, exc.message)
            raise
        except Exception as exc:  # noqa: BLE001 —— 登录线程不能把异常抛给调用方
            last_error = PlatformError(f'登录失败：{exc}', status=502,
                                       code='login_failed')
            _finish_error(login_id, last_error.message)
            continue

        with _lock:
            current = _sessions.get(login_id)
            if current is not None:
                current['state'] = 'success'
                current['account'] = account
                current['error'] = ''
                current['updated'] = _now()
        _close_server(session)
        return {'status': 'success', 'account': account, 'message': '登录成功'}

    raise last_error or PlatformError('登录失败：授权码无法兑换', status=400,
                                      code='login_failed')


# ── 回环回调服务 ────────────────────────────────────────────
_PAGE = ('<!doctype html><meta charset="utf-8"><title>{title}</title>'
         '<body style="font-family:system-ui,sans-serif;padding:48px;line-height:1.6">'
         '<h2>{title}</h2><p>{message}</p></body>')


def _page(title: str, message: str) -> bytes:
    return _PAGE.format(title=title, message=message).encode('utf-8')


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
            if parts.path != C.REDIRECT_PATH:
                self._respond(404, '页面不存在',
                              f'该地址只接受 {C.REDIRECT_PATH} 回调。')
                return
            params = _query_params(parts.query)
            self._handle(params)

        def do_POST(self) -> None:  # noqa: N802
            if not self._origin_allowed():
                self._respond(403, '拒绝访问', '该地址只接受本机授权回调。')
                return
            length = int(self.headers.get('Content-Length') or 0)
            raw = self.rfile.read(min(length, 64 * 1024)).decode(
                'utf-8', errors='replace')
            self._handle(_query_params(raw))

        def _handle(self, params: dict[str, str]) -> None:
            secret = str(params.get('secret') or '').strip()
            _record_secret(login_id, secret)
            code = str(params.get('code') or '').strip()
            redirect = str(params.get('redirect') or '').strip()
            if not code and redirect:
                # Portal 第一次回调：跳到登录页，等它带着 code 再回来。
                self.send_response(307)
                self.send_header('Location', redirect)
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                return
            if not code:
                self._respond(400, '登录失败', '回调里缺少 code，请重试。')
                return
            try:
                result = _accept_code(code, secret=secret,
                                      ticket_id=str(params.get('ticket_id')
                                                    or ''))
            except PlatformError as exc:
                title = {
                    'login_not_found': '登录会话已失效',
                    'login_no_credential': '授权失败',
                }.get(exc.code, '授权失败')
                self._respond(exc.status if exc.status >= 400 else 400,
                              title, exc.message)
                return
            account = result.get('account') or {}
            self._respond(200, '授权完成',
                          f'已登录：{account.get("nickname") or account.get("uid") or ""}。'
                          '可以关闭此页面并回到面板。')

        def log_message(self, *args: object) -> None:  # noqa: D102
            pass

    return Handler


# ── 对外接口 ────────────────────────────────────────────────
def start(realm: str | None = None) -> dict[str, Any]:
    """发起一次网页登录，返回 `{login_id, url, expires_in}`。"""
    login_id = uuid.uuid4().hex
    ticket_id = secrets.token_hex(16)
    secret = secrets.token_hex(16)
    verifier, challenge = dpop.pkce_pair()
    jwk = dpop.generate_jwk()

    server = ThreadingHTTPServer(('127.0.0.1', 0),
                                 _handler_factory(login_id))
    server.daemon_threads = True
    port = int(server.server_address[1])
    url = build_authorize_url(ticket_id, challenge, port)

    now = _now()
    with _lock:
        _prune(now)
        pending = sum(1 for item in _sessions.values()
                      if str(item.get('state') or '')
                      in ('pending', 'exchanging'))
        reject = pending >= _MAX_PENDING
        if not reject:
            _sessions[login_id] = {
                'id': login_id,
                'ticketId': ticket_id,
                'secret': secret,
                'portalSecret': '',
                'ticketFallback': False,
                'verifier': verifier,
                'dpopJwk': jwk,
                'port': port,
                'state': 'pending',
                'error': '',
                'account': None,
                'server': server,
                'created': now,
                'updated': now,
            }
    if reject:
        # 每个挂起会话都占一个本机端口 + 一条线程；serve_forever 还没起来，
        # 只能 server_close()（shutdown() 会等一个永远不来的事件）。
        try:
            server.server_close()
        except Exception:  # noqa: BLE001
            logger.debug('codearts login server close failed', exc_info=True)
        raise PlatformError('同时进行的登录会话过多，请稍后重试',
                            status=429, code='too_many_sessions')

    threading.Thread(target=server.serve_forever,
                     name='codearts-login-server', daemon=True).start()
    timer = threading.Timer(LOGIN_TTL, _expire, args=(login_id,))
    timer.daemon = True
    timer.start()
    return {'login_id': login_id, 'url': url, 'expires_in': LOGIN_TTL}


def _poll_ticket(session: dict[str, Any]) -> dict | None:
    """ticket 通道兜底：只在收到过 portal `secret` 的会话上跑。"""
    if not session.get('ticketFallback'):
        return None
    secret = str(session.get('portalSecret') or session.get('secret') or '')

    async def run() -> dict:
        token = await client.poll_ticket(
            str(session.get('ticketId') or ''), secret)
        payload: dict[str, Any] = {
            'userId': token.get('user_id') or '',
            'userName': token.get('user_name') or '',
            'domainId': token.get('domain_id') or '',
            'accessKeyId': token.get('access_key_id') or '',
            'secretAccessKey': token.get('secret_access_key') or '',
            'securityToken': token.get('security_token') or '',
            'expiredAt': store.iso_from_epoch(token.get('expires_at'))
            or token.get('expiration') or '',
            'refreshToken': token.get('refresh_token') or '',
            'refreshExpiredAt': store.iso_from_epoch(
                _now() + C.REFRESH_TTL_SECONDS),
            'codeVerifier': str(session.get('verifier') or ''),
            'clientId': C.CLIENT_ID,
            'dpopJwk': session.get('dpopJwk') or None,
            'source': 'web',
            'loginState': '正常',
        }
        from .. import registry  # noqa: PLC0415
        adapter = registry.get('codearts')
        if adapter is None:
            raise PlatformError('CodeArts 平台未注册', status=500,
                                code='platform_missing')
        return adapter.import_account(payload)

    try:
        return asyncio.run(run())
    except PlatformError:
        return None


def poll(login_id: str) -> dict[str, Any]:
    """查询登录状态；`ticketFallback` 会话顺带试一次 ticket 通道。"""
    with _lock:
        session = _sessions.get(str(login_id or ''))
        if not session:
            raise PlatformError('登录会话不存在或已过期', status=404,
                                code='login_not_found')
        state = str(session.get('state') or 'pending')
        snapshot = dict(session)

    if state == 'pending':
        account = _poll_ticket(snapshot)
        if account is not None:
            with _lock:
                current = _sessions.get(str(login_id))
                if current is not None and current.get('state') == 'pending':
                    current['state'] = 'success'
                    current['account'] = account
                    current['error'] = ''
                    current['updated'] = _now()
            _close_server(snapshot)
            state = 'success'

    with _lock:
        session = _sessions.get(str(login_id or ''))
        if not session:
            raise PlatformError('登录会话不存在或已过期', status=404,
                                code='login_not_found')
        return {
            'login_id': str(login_id),
            'status': str(session.get('state') or 'pending'),
            'realm': store.REALM,
            'account': session.get('account'),
            'message': str(session.get('error') or ''),
        }


def submit_callback(login_id: str, callback: str) -> dict[str, Any]:
    """远程部署的兜底：接收用户粘贴的回调地址并完成兑换。"""
    params = parse_callback_url(callback)
    secret = str(params.get('secret') or '').strip()
    if secret:
        _record_secret(str(login_id or ''), secret)
    code = str(params.get('code') or '').strip()
    if not code:
        raise PlatformError('回调地址里没有 code（第一次跳转不是最终回调，请等页面'
                            '跳转完成后再复制）', status=400,
                            code='login_no_credential')
    _accept_code(code, secret=secret,
                 ticket_id=str(params.get('ticket_id') or ''))
    return poll(login_id)


def cancel(login_id: str) -> bool:
    with _lock:
        session = _sessions.pop(str(login_id or ''), None)
    if session is None:
        return False
    _close_server(session)
    return True
