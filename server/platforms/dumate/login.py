"""DuMate 网页登录：百度 Passport 扫码（二维码 + 长轮询 + BDUSS 兑换）。

为什么是扫码：DuMate 的网页登录本来就是百度账号登录，官方登录页
（`login.bce.baidu.com`）走 Passport 的二维码协议；它不需要密码、验证码
或本机浏览器，因此面板部署在远端也能完成。手工粘贴 Cookie 仍然保留，
作为扫码不可用时的兜底。

链路（扫码部分与 `wooluo/BDpan` 的 `app/baidu/auth.js` 同口径；DuMate 侧的
落域按官方登录页的 SSO 链路复现）：

  1. `GET passport.baidu.com/v2/api/getqrcode?lp=pc&qrloginfrom=pc`
     → `{sign, imgurl}`；后台线程把 `imgurl` 取回来内联成 data URL
     （面板是同源 CSP，且浏览器不一定能直连百度），前端直接显示；
  2. `GET passport.baidu.com/channel/unicast?channel_id=<sign>` 长轮询
     → 30 秒一次：未扫码回 `{errno:1}`；确认后 `channel_v` 里带一次性
     `v`（temp bduss，`status=0`）；
  3. `GET passport.baidu.com/v3/login/main/qrbdusslogin?bduss=<v>`
     → 一段单引号 + 双引号混用的 JSON，真正的 `BDUSS` / `STOKEN` 在
     `data.session` 里（不能按标准 JSON 整体解析）；
  4. 带着 BDUSS 走 `login.bce.baidu.com` 的 `/postlogin/passport/wap`，
     再落到 `console.bce.baidu.com`，让服务端种下 `bce-long-term-sessionid`。
     **这一步不能省**：`/api/dumate/*` 的鉴权依赖 console 会话，只有
     Passport 的 BDUSS 会一直回 10001；
  5. 用组装好的 Cookie 换一次模型网关 token 探活，成功才落盘。

登录态只在内存里（`_sessions`），进程重启后重新扫码，与 Trae / Loomy /
MiniMax 一致。二维码有效期按 5 分钟算。
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import threading
import time
import uuid
from typing import Any

import httpx

from ... import config
from .. import registry
from ..base import PlatformError
from . import client, constants as C

__all__ = ['start', 'poll', 'cancel', 'parse_exchange', 'cookie_header', 'LOGIN_TTL']

LOGIN_TTL = C.QR_TTL
_KEEP_DONE = 300                # 终态后保留多久供前端轮询（秒）
_MAX_PENDING = 6                # 同时挂起的登录会话上限
_RETRY_DELAY = 2                # 长轮询网络失败后的重试间隔

_HEADERS = {
    'User-Agent': C.UA,
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'zh-CN,zh;q=0.9',
    'Referer': 'https://passport.baidu.com/',
}

_sessions: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


def _now() -> float:
    return time.time()


def _set(login_id: str, status: str, message: str = '',
         account: dict | None = None) -> None:
    with _lock:
        session = _sessions.get(login_id)
        if session is None:
            return
        session['status'] = status
        session['message'] = message
        session['updated'] = _now()
        if account is not None:
            session['account'] = account


def _view(session: dict) -> dict:
    return {
        'login_id': str(session.get('id') or ''),
        'status': str(session.get('status') or 'pending'),
        'message': str(session.get('message') or ''),
        'account': session.get('account'),
        'realm': 'cn',
        # 二维码是 passport 的图片地址，前端直接当 <img src> 用。
        'qr': str(session.get('qr') or ''),
        'url': '',
        'user_code': '',
        'expires_in': max(0, int(float(session.get('deadline') or 0) - _now())),
    }


def _prune(now: float) -> None:
    for key, session in list(_sessions.items()):
        status = str(session.get('status') or '')
        if status in ('pending', 'exchanging'):
            if now - float(session.get('created') or now) > LOGIN_TTL + _KEEP_DONE:
                stop = session.get('stop')
                if stop is not None:
                    stop.set()
                _sessions.pop(key, None)
            continue
        if now - float(session.get('updated') or now) > _KEEP_DONE:
            _sessions.pop(key, None)


# ── HTTP 小工具 ─────────────────────────────────────────────
def _http_client(timeout: float = C.QR_CODE_TIMEOUT) -> httpx.Client:
    """扫码链路专用客户端：同一份 cookie 贯穿取码 / 轮询 / 兑换 / 落域。

    与项目其余部分同口径：默认不读环境代理，需要时显式走全局代理。
    """
    kwargs: dict[str, Any] = {
        'timeout': httpx.Timeout(timeout, connect=10),
        'trust_env': False,
        'follow_redirects': True,
    }
    proxy = config.effective_http_proxy()
    if proxy:
        kwargs['proxy'] = proxy
    return httpx.Client(**kwargs)


def _qr_image_url(raw: object) -> str:
    text = str(raw or '').strip()
    if not text:
        return ''
    if text.startswith('//'):
        return 'https:' + text
    if text.startswith(('http://', 'https://')):
        return text
    return 'https://' + text


def _inline_qr(http: httpx.Client, url: str) -> str:
    """把二维码图片取回来内联成 data URL。

    面板是静态导出，并且带 `img-src 'self' data: blob:` 的 CSP，直接给
    passport 的外链会被浏览器拦掉；另外「浏览器能到面板、到不了百度」的
    网络也很常见。图片只有几 KB，内联最稳。取不到时退回外链。
    """
    if not url:
        return ''
    try:
        response = http.get(url, headers=_HEADERS)
    except httpx.HTTPError:
        return url
    if response.status_code != 200 or not response.content:
        return url
    ctype = str(response.headers.get('content-type') or 'image/png')
    ctype = ctype.split(';')[0].strip().lower()
    if not ctype.startswith('image/'):
        ctype = 'image/png'
    data = base64.b64encode(response.content).decode()
    return f'data:{ctype};base64,{data}'


def fetch_qrcode(http: httpx.Client) -> dict:
    """取一次登录二维码：`{sign, qr}`；`qr` 优先是内联的 data URL。"""
    try:
        response = http.get(C.PASSPORT_BASE + C.QRCODE_PATH + C.QRCODE_QUERY,
                            headers=_HEADERS)
    except httpx.HTTPError as exc:
        raise PlatformError(f'百度登录二维码获取失败：{exc}', status=502,
                            code='qr_unreachable') from exc
    try:
        data = response.json()
    except ValueError as exc:
        raise PlatformError(
            f'百度登录二维码接口返回非 JSON（HTTP {response.status_code}）',
            status=502, code='qr_bad_response') from exc
    sign = str((data or {}).get('sign') or '').strip() if isinstance(data, dict) else ''
    raw = (data or {}).get('imgurl') if isinstance(data, dict) else ''
    qr = _qr_image_url(raw)
    if response.status_code != 200 or not sign or not qr:
        raise PlatformError(
            str((data or {}).get('prompt') if isinstance(data, dict) else '')
            or f'百度登录二维码接口异常（HTTP {response.status_code}）',
            status=502, code='qr_unavailable')
    return {'sign': sign, 'qr': _inline_qr(http, qr)}


def poll_qrcode(http: httpx.Client, sign: str) -> dict:
    """一次扫码长轮询。

    返回 `{'status': 'waiting'|'confirmed'|'expired'|'cancelled', 'token': str}`。
    超时（30 秒内没状态变化）按 `waiting` 处理，不是错误。
    """
    try:
        response = http.get(
            C.PASSPORT_BASE + C.QR_POLL_PATH,
            params={'channel_id': sign},
            headers=_HEADERS,
            timeout=httpx.Timeout(C.QR_POLL_TIMEOUT, connect=10),
        )
    except httpx.TimeoutException:
        return {'status': 'waiting', 'token': ''}
    except httpx.HTTPError as exc:
        raise PlatformError(f'百度扫码状态查询失败：{exc}', status=502,
                            code='qr_poll_failed') from exc

    text = (response.text or '').strip()
    # 上游也支持 JSONP（带 `callback` 时回 `cb({...})`）；这里不传 callback，
    # 但兼容一下，免得某天被默认包一层就开始一直「等待扫码」。
    if text and not text.startswith('{'):
        start, end = text.find('('), text.rfind(')')
        if 0 <= start < end:
            text = text[start + 1:end].strip()
    try:
        payload = json.loads(text) if text else {}
    except ValueError:
        return {'status': 'waiting', 'token': ''}
    if not isinstance(payload, dict):
        return {'status': 'waiting', 'token': ''}
    # 长轮询空转：errno=1 表示「还没事件」。
    try:
        errno = int(payload.get('errno'))
    except (TypeError, ValueError):
        errno = 0
    if errno != 0:
        return {'status': 'waiting', 'token': ''}

    channel = payload.get('channel_v')
    event: object = channel
    if isinstance(channel, str):
        try:
            event = json.loads(channel)
        except ValueError:
            event = None
    if not isinstance(event, dict):
        return {'status': 'waiting', 'token': ''}

    token = str(event.get('v') or '').strip()
    try:
        status = int(event.get('status')) if event.get('status') is not None else None
    except (TypeError, ValueError):
        status = None
    if token:
        # 参考实现（BDpan）只在 status==0 时把 `v` 当成一次性 bduss；
        # 其它 status 是「已扫码、待确认」之类的中间态，继续等。
        if status in (2, 3):
            return {'status': 'cancelled', 'token': ''}
        if status == 0:
            return {'status': 'confirmed', 'token': token}
        return {'status': 'waiting', 'token': ''}
    if status in (2, 3):
        return {'status': 'expired', 'token': ''}
    return {'status': 'waiting', 'token': ''}


# ── 兑换登录态 ──────────────────────────────────────────────
def _grab(body: str, key: str) -> str:
    match = re.search(
        r'["\']' + re.escape(key) + r'["\']\s*:\s*["\']([^"\']*)["\']', body or '')
    return match.group(1) if match else ''


def parse_exchange(body: str) -> dict:
    """解析 `qrbdusslogin` 的混引号 JSON 响应。

    成功形状（节选）::

        {"errInfo": {"no": "0", "msg": ""}, "data": {"session": {
            "bduss": "...", "stoken": "...", "ptoken": "..." }}}

    **不能 `json.loads`**：顶层 `'data'` 用的是单引号。逐个字段抠更稳。
    """
    text = str(body or '')
    no = _grab(text, 'no')
    message = _grab(text, 'msg') or _grab(text, 'message')
    bduss = _grab(text, 'bduss')
    if not bduss:
        detail = message or (f'错误码 {no}' if no else '上游未返回 BDUSS')
        raise PlatformError(f'扫码登录换取凭证失败：{detail}', status=401,
                            code='qr_login_failed', payload=text[:500])
    return {
        'bduss': bduss,
        'stoken': _grab(text, 'stoken'),
        'ptoken': _grab(text, 'ptoken'),
        'nickname': _grab(text, 'displayName'),
    }


def exchange(http: httpx.Client, temp_bduss: str) -> dict:
    """用一次性 bduss 换正式 BDUSS / STOKEN。

    正常响应把凭证放在 body；浏览器上下文里也可能只靠 `Set-Cookie` 下发。
    两条都认：body 里没有 `bduss` 时从 cookie jar 里取（`Set-Cookie` 已经
    被 httpx 收进 jar）。
    """
    try:
        response = http.get(
            C.PASSPORT_BASE + C.QR_LOGIN_PATH,
            params={'bduss': temp_bduss},
            headers=_HEADERS,
            timeout=httpx.Timeout(C.QR_LOGIN_TIMEOUT, connect=10),
        )
    except httpx.HTTPError as exc:
        raise PlatformError(f'百度扫码兑换登录态失败：{exc}', status=502,
                            code='qr_exchange_failed') from exc
    try:
        return parse_exchange(response.text)
    except PlatformError:
        jar = {c.name: c.value for c in http.cookies.jar if c.value}
        if jar.get('BDUSS'):
            return {
                'bduss': jar['BDUSS'],
                'stoken': jar.get('STOKEN', ''),
                'ptoken': jar.get('PTOKEN', ''),
                'nickname': '',
            }
        raise


def cookie_header(cookies: httpx.Cookies) -> str:
    """从 cookie jar 里挑出百度域下的凭证，拼成 `k=v; k=v`。

    同名 cookie 取域名更具体的那个（`.console.bce.baidu.com` 优先于
    `.baidu.com`）；passport 自己的 PASSID / UBI 不发给业务域。
    """
    best: dict[str, Any] = {}
    for cookie in cookies.jar:
        name = str(getattr(cookie, 'name', '') or '')
        value = str(getattr(cookie, 'value', '') or '')
        if not name or not value:
            continue
        domain = str(getattr(cookie, 'domain', '') or '').lstrip('.').lower()
        if not any(domain == item or domain.endswith('.' + item)
                   for item in C.COOKIE_DOMAINS):
            continue
        prev = best.get(name)
        if prev is None or len(str(prev.domain or '')) < len(str(cookie.domain or '')):
            best[name] = cookie
    return '; '.join(f'{name}={best[name].value}' for name in sorted(best))


def settle_console(http: httpx.Client, exchange_info: dict) -> str:
    """把 BDUSS 落到 console / dumate 域，拿到 console 会话 cookie。

    返回可直接保存的整段 Cookie。任何一次落域失败都不算致命：后面的
    token 探活会给出最终结论。
    """
    jar = http.cookies
    bduss = str(exchange_info.get('bduss') or '')
    if bduss:
        jar.set('BDUSS', bduss, domain='.baidu.com', path='/')
    stoken = str(exchange_info.get('stoken') or '')
    if stoken:
        jar.set('STOKEN', stoken, domain='.baidu.com', path='/')
    ptoken = str(exchange_info.get('ptoken') or '')
    if ptoken:
        jar.set('PTOKEN', ptoken, domain='.baidu.com', path='/')

    # ① 进入官方登录页（建立 BCEC 侧上下文；未登录时这里只是登录页）。
    try:
        http.get(C.BCE_LOGIN_BASE + '/',
                 params={'redirect': C.BCE_LOGIN_REDIRECT},
                 headers=_HEADERS,
                 timeout=httpx.Timeout(C.QR_CODE_TIMEOUT, connect=10))
    except httpx.HTTPError:
        pass

    # ② 带 passport 登录态走一次 SSO：成功时 302 回 console 并种下
    #    `bce-long-term-sessionid`；失败时 302 回登录页（后面探活会报错）。
    try:
        http.get(C.BCE_LOGIN_BASE + C.BCE_POSTLOGIN_PATH,
                 params=C.BCE_POSTLOGIN_QUERY,
                 headers=_HEADERS,
                 timeout=httpx.Timeout(C.QR_CODE_TIMEOUT, connect=10))
    except httpx.HTTPError:
        pass

    for url in C.CONSOLE_SETTLE_URLS:
        try:
            http.get(url, headers=_HEADERS,
                     timeout=httpx.Timeout(C.QR_CODE_TIMEOUT, connect=10))
        except httpx.HTTPError:
            continue
    return cookie_header(jar)


# ── 后台线程 ────────────────────────────────────────────────
def _await_on(session: dict, coro, *, timeout: float):
    """在发起登录的事件循环上跑协程；没有循环时退回 `asyncio.run`。

    登录流程在后台线程里跑，而 `config.http_client` 的客户端池按事件循环
    归属。用 `asyncio.run` 每调一次就新建/丢弃一个循环，会留下绑在已关闭
    循环上的连接池；把协程投回主循环更干净。
    """
    loop = session.get('loop')
    if loop is not None and not loop.is_closed():
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)
    return asyncio.run(coro)


def _run(login_id: str) -> None:
    deadline = _now() + LOGIN_TTL
    try:
        with _http_client(C.QR_POLL_TIMEOUT) as http:
            qr = fetch_qrcode(http)
            with _lock:
                session = _sessions.get(login_id)
                if session is None or session.get('status') != 'pending':
                    return
                session['sign'] = qr['sign']
                session['qr'] = qr['qr']
                session['message'] = '请用百度 App / 百度智能云 App 扫码登录'
                session['updated'] = _now()

            while _now() < deadline:
                with _lock:
                    session = _sessions.get(login_id)
                    if session is None or session.get('status') != 'pending':
                        return
                    sign = str(session.get('sign') or '')
                    stop = session.get('stop')
                if not sign or stop is None or stop.is_set():
                    return
                try:
                    out = poll_qrcode(http, sign)
                except PlatformError as exc:
                    with _lock:
                        session = _sessions.get(login_id)
                        if session and session.get('status') == 'pending':
                            session['message'] = f'等待扫码（{exc.message}）'
                            session['updated'] = _now()
                    stop.wait(_RETRY_DELAY)
                    continue

                status = str(out.get('status') or 'waiting')
                if status == 'waiting':
                    stop.wait(C.QR_WAIT_DELAY)
                    continue
                if status == 'expired':
                    _set(login_id, 'expired', '二维码已过期，请重新发起登录')
                    return
                if status == 'cancelled':
                    _set(login_id, 'expired', '已在手机上取消登录')
                    return
                if status == 'confirmed':
                    token = str(out.get('token') or '')
                    try:
                        _finish(login_id, http, token)
                        return
                    except PlatformError as exc:
                        # 扫码确认事件偶尔比真正的确认早一步（兑换回「验证信息
                        # 已过期」）：继续等下一个事件，别把用户直接踢出去。
                        if exc.code == 'qr_login_failed' and '过期' in exc.message:
                            stop.wait(_RETRY_DELAY)
                            continue
                        _set(login_id, 'error', exc.message)
                        return
    except PlatformError as exc:
        _set(login_id, 'error', exc.message)
        return
    except Exception as exc:  # noqa: BLE001 —— 线程异常也要让前端看到
        _set(login_id, 'error', f'扫码登录失败：{exc}')
        return

    with _lock:
        session = _sessions.get(login_id)
        if session and session.get('status') == 'pending':
            session['status'] = 'expired'
            session['message'] = '二维码已过期，请重新发起登录'
            session['updated'] = _now()


def _finish(login_id: str, http: httpx.Client, temp_bduss: str) -> None:
    _set(login_id, 'exchanging', '扫码成功，正在换取登录凭证…')
    with _lock:
        session = dict(_sessions.get(login_id) or {})
    info = exchange(http, temp_bduss)
    cookie = settle_console(http, info)

    # 探活：token 换得到才算一份可用的 DuMate 凭证。网络类 5xx 不否定
    # 这次登录（cookie 本身已经拿到），凭证问题（401/403）才判失败。
    warning = ''
    try:
        _await_on(session, client.fetch_token(cookie),
                  timeout=C.TOKEN_TIMEOUT + 15)
    except PlatformError as exc:
        if exc.status in (500, 502, 503, 504):
            warning = exc.message[:200]
        else:
            hint = ''
            if 'bce-long-term-sessionid=' not in cookie:
                hint = ('（没能取得 console 会话 cookie，'
                        '可改用「粘贴 Cookie」：在浏览器登录 dumate.cn 后复制整行 Cookie）')
            raise PlatformError(
                f'扫码成功，但这份凭证换不到 DuMate token：{exc.message}{hint}',
                status=exc.status, code='login_not_usable',
                payload=exc.payload) from exc
    except Exception as exc:  # noqa: BLE001 —— 网络异常按可保存处理
        warning = str(exc)[:200]

    nickname = str(info.get('nickname') or '').strip()
    if not nickname:
        try:
            user = _await_on(session, client.fetch_user_info(cookie),
                             timeout=C.TOKEN_TIMEOUT + 15)
            nickname = str(user.get('nickname') or '').strip()
        except Exception:  # noqa: BLE001 —— 昵称只是展示，拿不到不影响登录
            nickname = ''

    adapter = registry.get('dumate')
    if adapter is None:
        _set(login_id, 'error', 'DuMate 平台未注册')
        return
    payload = {
        'cookie': cookie,
        'source': 'web',
        'loginState': '正常' if not warning else '待验证',
        'lastRefreshError': warning,
    }
    if nickname:
        payload['nickname'] = nickname
    account = adapter.import_account(payload)
    # 登录即探活成功：记一次刷新时间，避免账号页显示「从未刷新」。
    if not warning:
        try:
            from . import store  # noqa: PLC0415 —— 避免循环导入
            store.patch_account(account['uid'], {'refreshedAt': int(_now())})
        except Exception:  # noqa: BLE001
            pass
    _set(login_id, 'success', '登录成功', account=account)


# ── 对外 ────────────────────────────────────────────────────
def start() -> dict:
    """发起一次扫码登录；二维码由后台线程取，前端轮询第一次就能看到。"""
    now = _now()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:              # 同步调用（脚本 / 测试）时没有循环
        loop = None
    with _lock:
        _prune(now)
        pending = sum(1 for s in _sessions.values()
                      if s.get('status') in ('pending', 'exchanging'))
        if pending >= _MAX_PENDING:
            raise PlatformError('登录会话过多，请稍后再试', status=429,
                                code='too_many_logins')
        login_id = uuid.uuid4().hex
        _sessions[login_id] = {
            'id': login_id,
            'status': 'pending',
            'message': '正在获取百度登录二维码…',
            'qr': '',
            'sign': '',
            'account': None,
            'loop': loop,
            'created': now,
            'updated': now,
            'deadline': now + LOGIN_TTL,
            'stop': threading.Event(),
        }
    threading.Thread(target=_run, args=(login_id,), daemon=True,
                     name=f'dumate-login-{login_id[:8]}').start()
    with _lock:
        return _view(_sessions.get(login_id) or {})


def poll(login_id: str) -> dict:
    login_id = str(login_id or '').strip()
    with _lock:
        _prune(_now())
        session = _sessions.get(login_id)
        if session is None:
            raise PlatformError('登录会话不存在或已过期，请重新发起',
                                status=404, code='login_not_found')
        if session.get('status') in ('pending', 'exchanging') \
                and _now() - float(session.get('created') or 0) > LOGIN_TTL:
            session['status'] = 'expired'
            session['message'] = '二维码已过期，请重新发起登录'
            session['updated'] = _now()
        return _view(session)


def cancel(login_id: str) -> bool:
    login_id = str(login_id or '').strip()
    with _lock:
        session = _sessions.pop(login_id, None)
    if session is None:
        return False
    stop = session.get('stop')
    if stop is not None:
        stop.set()
    return True
