"""Loomy 网页登录：微信扫码（服务端取码 + 长轮询）与「粘链接」兜底。

两条入口共用同一段「`code` → session」逻辑：

  · **扫码**（主路径）：`wechat.fetch_login_qr` 把授权页拉下来，从 HTML 里
    抠出 `uuid`；二维码图片交给面板内嵌，后台线程长轮询
    `l/qrconnect`，`wx_errcode=405` 的响应里带 `wx_code`。用户只扫码，
    不用切窗口、不用复制地址。
  · **粘链接**（兜底）：服务端连不上微信时，面板打开授权页，用户扫码后
    微信 302 到 `loomy.xunfei.cn/oauth/wechat/callback`（线上是 404 页），
    把地址栏整条链接粘回来，由 `wechat.parse_callback` 解出 `code`。

回调域被微信白名单锁死在 `loomy.xunfei.cn`，所以**不能**像 Trae 那样在
`127.0.0.1` 起临时服务自动收回调 —— 这也是为什么主路径走「服务端自己的
二维码会话」。

登录态只存在内存（`_sessions`）：进程重启后要重新扫码，与 Trae 一致。
每个会话单次使用，成功后即 expire；二维码有效期（`LOGIN_TTL`）与微信页面
一致按 4 分钟算。
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Any

from .. import registry
from ..base import PlatformError
from . import client, wechat

__all__ = ['start', 'poll', 'cancel', 'submit_callback', 'LOGIN_TTL']

LOGIN_TTL = 240                 # 二维码 / 授权页有效期（秒）
_KEEP_DONE = 300                # 终态后保留多久供前端轮询（秒）
_MAX_PENDING = 8                # 同时挂起的登录会话上限（每个占一条线程）
_LONG_POLL_TIMEOUT = 25         # 单次微信长轮询超时；到点视为「未扫码」
_RETRY_DELAY = 3                # 长轮询网络失败后的重试间隔

_sessions: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


def _now() -> float:
    return time.time()


def _prune(now: float) -> None:
    """清掉终态很久的会话，避免 `_sessions` 无限增长（调用方持锁）。"""
    for key, session in list(_sessions.items()):
        if str(session.get('status') or '') in ('pending', 'exchanging'):
            continue
        if now - float(session.get('updated') or now) > _KEEP_DONE:
            _sessions.pop(key, None)


def _view(session: dict) -> dict:
    return {
        'login_id': str(session.get('id') or ''),
        'status': str(session.get('status') or 'pending'),
        'message': str(session.get('message') or ''),
        'account': session.get('account'),
        'realm': 'cn',
        # 二维码与授权页随轮询一起返回：前端刷新页面 / 重渲染时不用再发起
        # 一次登录（重新发起会换一个 uuid，正在扫的码就废了）。
        'qr': str(session.get('qr') or ''),
        'url': str(session.get('url') or ''),
    }


def _set_status(login_id: str, status: str, message: str = '',
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


# ── 兑换 ────────────────────────────────────────────────────
def _exchange(login_id: str, code: str) -> dict:
    """`code` → session → 落盘账号。同一条路径供扫码与粘贴使用。"""
    login_id = str(login_id or '').strip()
    with _lock:
        session = _sessions.get(login_id)
        if session is None:
            raise PlatformError('登录会话不存在或已过期，请重新发起',
                                status=404, code='login_not_found')
        if session.get('status') == 'success':
            return _view(session)
        if session.get('status') not in ('pending', 'exchanging'):
            raise PlatformError('登录会话已失效，请重新发起', status=409,
                                code='login_not_pending')
        session['status'] = 'exchanging'
        session['message'] = '正在换取登录态…'
        session['updated'] = _now()

    try:
        info = client.login_by_wechat_sync(code)
    except PlatformError as exc:
        _set_status(login_id, 'error', exc.message)
        raise

    adapter = registry.get('loomy')
    if adapter is None:
        message = 'Loomy 平台未注册'
        _set_status(login_id, 'error', message)
        raise PlatformError(message, status=500, code='platform_missing')
    try:
        # 复用导入路径：uid 推导、脱敏、权重 / 冷却字段清理都在那一处。
        account = adapter.import_account({
            'session': str(info.get('session') or ''),
            'userid': str(info.get('userid') or ''),
            'phone': str(info.get('phone') or ''),
            'nickname': str(info.get('nickname') or ''),
            # 微信登录拿不到密码：会话失效后只能重新扫码（`has_password=False`）。
            'source': 'web',
        })
    except PlatformError as exc:
        _set_status(login_id, 'error', exc.message)
        raise
    except Exception as exc:  # noqa: BLE001 —— 落盘等异常也要让前端看到原因
        message = f'登录成功但账号保存失败：{exc}'
        _set_status(login_id, 'error', message)
        raise PlatformError(message, status=500,
                            code='login_save_failed') from exc

    _set_status(login_id, 'success', '登录成功', account=account)
    return _view(_sessions.get(login_id) or {})


# ── 扫码线程 ────────────────────────────────────────────────
def _run(login_id: str) -> None:
    """后台线程：先取二维码，再长轮询扫码结果。

    取码要打一次微信页面（可能十几秒 / 失败），所以**不能**放在
    `login_start` 里同步做——那个路由是 `async def`，同步网络调用会把整个
    服务的事件循环卡住。前端第一次 `poll` 就能拿到二维码（`qr` 字段）。
    """
    try:
        qr = wechat.fetch_login_qr(login_id)
    except PlatformError as exc:
        with _lock:
            session = _sessions.get(login_id)
            if session is None or session.get('status') != 'pending':
                return
            # 退化到「打开授权页 + 粘回调」：二维码取不到不是登录失败。
            session['message'] = (
                f'二维码获取失败（可打开授权页扫码后粘贴回调）：{exc.message}')
            session['updated'] = _now()
        return

    with _lock:
        session = _sessions.get(login_id)
        if session is None or session.get('status') != 'pending':
            return
        session['uuid'] = qr['uuid']
        session['qr'] = qr['qr_url']
        session['message'] = '等待微信扫码'
        session['updated'] = _now()
    _wait(login_id)


def _wait(login_id: str) -> None:
    """长轮询：拿 `wx_code` 就完成登录，取消 / 过期就收工。"""
    deadline = _now() + LOGIN_TTL
    while _now() < deadline:
        with _lock:
            session = _sessions.get(login_id)
            if session is None or session.get('status') != 'pending':
                return
            qr_uuid = str(session.get('uuid') or '')
            stop = session.get('stop')
        if not qr_uuid or stop is None or stop.is_set():
            return
        try:
            out = wechat.poll_login(qr_uuid, timeout=_LONG_POLL_TIMEOUT)
        except PlatformError as exc:
            if exc.code == 'wechat_timeout':
                continue
            # 网络抖动不当作登录失败：歇一会再试，用户还在扫码。
            with _lock:
                session = _sessions.get(login_id)
                if session and session.get('status') == 'pending':
                    session['message'] = f'等待扫码（微信连接异常：{exc.message}）'
            stop.wait(_RETRY_DELAY)
            continue

        err = out.get('errcode')
        if err == wechat.ERR_CONFIRMED:
            code = str(out.get('code') or '').strip()
            if not code:
                _set_status(login_id, 'error', '微信已确认但没返回 code，请重试')
                return
            try:
                _exchange(login_id, code)
            except PlatformError:
                # 失败原因已经写进会话（`_exchange` 负责），这里只结束线程。
                pass
            return
        if err == wechat.ERR_EXPIRED:
            _set_status(login_id, 'expired', '二维码已过期，请重新发起')
            return
        if err == wechat.ERR_CANCELLED:
            _set_status(login_id, 'expired', '已在手机上取消登录')
            return
        if err == wechat.ERR_SCANNED:
            with _lock:
                session = _sessions.get(login_id)
                if session and session.get('status') == 'pending':
                    session['message'] = '已扫码，请在手机上确认'
                    session['updated'] = _now()

    # 二维码到期：仍在 pending 的会话转成 expired，前端据此显示「重新发起」。
    with _lock:
        session = _sessions.get(login_id)
        if session and session.get('status') == 'pending':
            session['status'] = 'expired'
            session['message'] = '二维码已过期，请重新发起'
            session['updated'] = _now()


# ── 对外 ────────────────────────────────────────────────────
def start() -> dict:
    """发起一次扫码登录，返回 `{login_id, url, qr, expires_in}`。

    立刻返回（`qr` 为空、状态 `pending`）：取二维码放在后台线程里做，前端
    轮询第一次就能拿到 `qr`。取码失败也不让整个登录失败——照旧返回授权页
    URL，用户可以在浏览器里扫码后把回调链接粘回来。
    """
    now = _now()
    login_id = uuid.uuid4().hex        # 同时就是微信原样回传的 state
    url = wechat.build_auth_url(login_id)
    with _lock:
        _prune(now)
        pending = sum(1 for item in _sessions.values()
                      if str(item.get('status') or '') in ('pending', 'exchanging'))
        if pending >= _MAX_PENDING:
            raise PlatformError('同时进行的登录会话过多，请稍后重试',
                                status=429, code='too_many_sessions')
        _sessions[login_id] = {
            'id': login_id,
            'status': 'pending',
            'message': '正在获取二维码…',
            'uuid': '',
            'qr': '',
            'url': url,
            'account': None,
            'created': now,
            'updated': now,
            'stop': threading.Event(),
        }

    threading.Thread(target=_run, args=(login_id,),
                     name='loomy-wechat-login', daemon=True).start()
    with _lock:
        session = _sessions.get(login_id) or {}
        return {**_view(session), 'expires_in': LOGIN_TTL}


def poll(login_id: str) -> dict:
    """查询登录状态（惰性过期：只有 pending 才会超时）。"""
    login_id = str(login_id or '').strip()
    with _lock:
        session = _sessions.get(login_id)
        if session is None:
            raise PlatformError('登录会话不存在或已过期', status=404,
                                code='login_not_found')
        if session.get('status') == 'pending' \
                and _now() - float(session.get('created') or 0) > LOGIN_TTL:
            session['status'] = 'expired'
            session['message'] = '二维码已过期，请重新发起'
            session['updated'] = _now()
        return _view(session)


def submit_callback(login_id: str, callback: str) -> dict:
    """粘回调链接完成登录（服务端拿不到二维码时用，也可与扫码并行）。"""
    login_id = str(login_id or '').strip()
    parsed = wechat.parse_callback(callback)
    state = str(parsed.get('state') or '')
    with _lock:
        session = _sessions.get(login_id)
    if session is None:
        raise PlatformError('登录会话不存在或已过期，请重新发起',
                            status=404, code='login_not_found')
    # state 是我们自己签发的：对不上说明粘的是上一次的链接。
    if state and state != login_id:
        raise PlatformError('回调链接与本次登录不匹配（多半是上一次的链接），请重新扫码',
                            status=400, code='login_state_mismatch')
    return _exchange(login_id, str(parsed.get('code') or ''))


def cancel(login_id: str) -> bool:
    """丢弃会话并停下后台长轮询。"""
    login_id = str(login_id or '').strip()
    with _lock:
        session = _sessions.pop(login_id, None)
    if session is None:
        return False
    stop = session.get('stop')
    if stop is not None:
        stop.set()
    return True
