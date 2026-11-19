"""Loomy 网页登录：微信扫码、手机验证码，以及「粘链接」兜底。

三条入口最终都汇到 `_finish`（`session` → 落盘账号）：

  · **微信扫码**（主路径）：`wechat.fetch_login_qr` 把授权页拉下来，从 HTML
    里抠出 `uuid`；二维码图片交给面板内嵌，后台线程长轮询 `l/qrconnect`，
    `wx_errcode=405` 的响应里带 `wx_code`。用户只扫码，不用切窗口、不用
    复制地址。
  · **手机验证码**：`/login/phone/sendMsgCode` 下发短信 → `/login/phone/
    checkCode` 换 session。没有密码，`has_password=False`。
  · **粘链接**（兜底）：服务端连不上微信时，面板打开授权页，用户扫码后
    微信 302 到 `loomy.xunfei.cn/oauth/wechat/callback`（线上是 404 页），
    把地址栏整条链接粘回来，由 `wechat.parse_callback` 解出 `code`。

微信还有一条**补绑**支线：`bind/auth` 返回 `bind=0`（该微信没绑手机号）时，
`client` 抛 `wechat_needs_bind` 并带上 `rcode`；会话转成 `needs_bind`，前端
就地让用户填手机号，`bind/sendMsg` + `bind/checkCode` 完成「绑定 + 登录」。
注意绑定支线只能带 `rcode`，验证码登录支线只能带手机号，两者不可混用。

回调域被微信白名单锁死在 `loomy.xunfei.cn`，所以**不能**像 Trae 那样在
`127.0.0.1` 起临时服务自动收回调 —— 这也是为什么主路径走「服务端自己的
二维码会话」。

登录态只存在内存（`_sessions`）：进程重启后要重新扫码 / 重新收码，与 Trae
一致。每个会话单次使用，成功后即 expire；二维码有效期（`LOGIN_TTL`）与微信
页面一致按 4 分钟算，验证码阶段放宽到 `CODE_TTL`。
"""
from __future__ import annotations

import math
import threading
import time
import uuid
from typing import Any

from .. import registry
from ..base import PlatformError
from . import client, wechat

__all__ = ['start', 'poll', 'cancel', 'submit_callback', 'send_code',
           'submit_code', 'LOGIN_TTL', 'CODE_TTL']

LOGIN_TTL = 240                 # 二维码 / 授权页有效期（秒）
CODE_TTL = 900                  # 验证码阶段（等短信 / 补绑）的有效期（秒）
_KEEP_DONE = 300                # 终态后保留多久供前端轮询（秒）
_MAX_PENDING = 8                # 同时挂起的登录会话上限（每个占一条线程）
_LONG_POLL_TIMEOUT = 25         # 单次微信长轮询超时；到点视为「未扫码」
_RETRY_DELAY = 3                # 长轮询网络失败后的重试间隔
_SMS_COOLDOWN = 60              # 同一会话两次发码的最小间隔（秒）
_SMS_MAX_SENDS = 5              # 同一会话最多发几条短信
_CODE_MAX_FAILURES = 5          # 连续输错几次后作废会话

_sessions: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


def _now() -> float:
    return time.time()


def _prune(now: float) -> None:
    """清掉终态很久的会话，避免 `_sessions` 无限增长（调用方持锁）。"""
    for key, session in list(_sessions.items()):
        status = str(session.get('status') or '')
        if status in ('pending', 'exchanging', 'needs_bind'):
            # 没人轮询 / 用户直接关页面时不会触发惰性过期：超过
            # TTL + 保留期就整体丢掉（二维码线程到点也会自己退出）。
            if now - float(session.get('created') or now) \
                    > _session_ttl(session) + _KEEP_DONE:
                stop = session.get('stop')
                if stop is not None:
                    stop.set()
                _sessions.pop(key, None)
            continue
        if now - float(session.get('updated') or now) > _KEEP_DONE:
            _sessions.pop(key, None)


def _mask_phone(phone: str) -> str:
    phone = str(phone or '')
    if len(phone) < 7:
        return phone
    return f'{phone[:3]}****{phone[-4:]}'


def _normalize_phone(phone: object) -> str:
    """只支持大陆手机号（上游固定 `ccode=86`）：去掉空白 / 连字符 / +86。"""
    text = ''.join(ch for ch in str(phone or '').strip() if ch not in ' -()')
    for prefix in ('+86', '0086', '86'):
        if text.startswith(prefix) and len(text) > len(prefix):
            text = text[len(prefix):]
            break
    if not text:
        raise PlatformError('请先填写手机号', status=400, code='missing_phone')
    if len(text) != 11 or not text.isdigit() or text[0] != '1':
        raise PlatformError('手机号格式不正确（需要 11 位大陆手机号）',
                            status=400, code='invalid_phone')
    return text


def _session_ttl(session: dict) -> float:
    """二维码阶段按 4 分钟算；验证码 / 补绑阶段给用户留足收短信的时间。"""
    if str(session.get('status') or '') == 'needs_bind' \
            or str(session.get('mode') or 'qr') == 'sms':
        return CODE_TTL
    return LOGIN_TTL


def _view(session: dict) -> dict:
    now = _now()
    sent_at = float(session.get('code_sent_at') or 0)
    cooldown = 0
    if sent_at:
        cooldown = max(0, int(math.ceil(sent_at + _SMS_COOLDOWN - now)))
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
        # 验证码链路：`mode=sms`（直接登录）或 `needs_bind`（微信补绑）。
        'mode': str(session.get('mode') or 'qr'),
        'needs_bind': str(session.get('status') or '') == 'needs_bind',
        'code_sent': bool(session.get('msgid')),
        'retry_after': cooldown,
        'phone': _mask_phone(str(session.get('phone') or '')),
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


# ── 收尾 ────────────────────────────────────────────────────
def _finish(login_id: str, info: dict) -> dict:
    """`{session, userid, phone}` → 落盘账号（扫码 / 验证码 / 补绑共用）。"""
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
            # 扫码 / 验证码登录都拿不到密码：会话失效后只能重新登录
            # （`has_password=False`）。
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


# ── 微信兑换 ────────────────────────────────────────────────
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
    except client.AccountError as exc:
        if exc.code == 'wechat_needs_bind':
            # 该微信没绑手机号：把 rcode 留在服务端，前端就地收手机号 + 验证码。
            # rcode 不回传前端（它等价于一次性的绑定票据）。
            rcode = ''
            if isinstance(exc.payload, dict):
                rcode = str(exc.payload.get('rcode') or '')
            with _lock:
                session = _sessions.get(login_id)
                if session is not None:
                    session['bind_rcode'] = rcode
                    session['status'] = 'needs_bind'
                    session['message'] = ('该微信还没绑定手机号：'
                                          '填写手机号并完成短信验证即可绑定登录')
                    session['updated'] = _now()
                    return _view(session)
            raise
        _set_status(login_id, 'error', exc.message)
        raise
    except PlatformError as exc:
        _set_status(login_id, 'error', exc.message)
        raise

    return _finish(login_id, info)


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
                # 失败原因已经写进会话（`_exchange` 负责），这里只结束线程；
                # `needs_bind` 也是在这里返回的，会话转入验证码阶段。
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


# ── 验证码 ──────────────────────────────────────────────────
def _require_code_session(login_id: str) -> dict:
    """取出可用于验证码流程的会话：`mode=sms` 或微信 `needs_bind`。"""
    with _lock:
        session = _sessions.get(login_id)
        if session is None:
            raise PlatformError('登录会话不存在或已过期，请重新发起',
                                status=404, code='login_not_found')
        status = str(session.get('status') or '')
        mode = str(session.get('mode') or 'qr')
        if status in ('success', 'error', 'expired'):
            raise PlatformError('登录会话已失效，请重新发起', status=409,
                                code='login_not_pending')
        if status == 'exchanging':
            raise PlatformError('正在处理上一步，请稍候', status=429,
                                code='login_busy')
        if mode != 'sms' and status != 'needs_bind':
            raise PlatformError(
                '当前登录方式不需要验证码，请先扫码或改用验证码登录',
                status=400, code='code_not_supported')
        return session


def send_code(login_id: str, phone: object) -> dict:
    """下发短信验证码。

    · `mode=sms`：`/login/phone/sendMsgCode`，用于验证码直接登录；
    · `needs_bind`：`bind/sendMsg`，用于微信补绑手机号（带 `rcode`）。
    """
    login_id = str(login_id or '').strip()
    session = _require_code_session(login_id)
    needs_bind = str(session.get('status') or '') == 'needs_bind'
    with _lock:
        session = _sessions.get(login_id)
        if session is None:
            raise PlatformError('登录会话已过期，请重新发起', status=404,
                                code='login_not_found')
        if session.get('sending'):
            raise PlatformError('验证码正在发送，请稍候', status=429,
                                code='login_busy')
        now = _now()
        sent_at = float(session.get('code_sent_at') or 0)
        if sent_at and now - sent_at < _SMS_COOLDOWN:
            wait = int(math.ceil(sent_at + _SMS_COOLDOWN - now))
            raise PlatformError(f'请 {wait} 秒后再获取验证码', status=429,
                                code='sms_cooldown', retry_after=wait)
        if int(session.get('sms_sends') or 0) >= _SMS_MAX_SENDS:
            raise PlatformError('验证码发送次数过多，请重新发起登录',
                                status=429, code='sms_limit')
        normalized = _normalize_phone(phone or session.get('phone'))
        rcode = str(session.get('bind_rcode') or '')
        session['sending'] = True
        session['updated'] = now

    try:
        if needs_bind:
            if not rcode:
                raise PlatformError('微信授权已过期，请重新扫码', status=409,
                                    code='login_not_pending')
            info = client.bind_send_msg_sync(rcode, normalized)
        else:
            info = client.send_sms_code_sync(normalized)
    except PlatformError:
        with _lock:
            session = _sessions.get(login_id)
            if session is not None:
                session['sending'] = False
                session['updated'] = _now()
        raise

    with _lock:
        session = _sessions.get(login_id)
        if session is None:
            raise PlatformError('登录会话已过期，请重新发起', status=404,
                                code='login_not_found')
        session['sending'] = False
        session['phone'] = normalized
        session['msgid'] = str(info.get('msgid') or '')
        session['code_sent_at'] = _now()
        session['sms_sends'] = int(session.get('sms_sends') or 0) + 1
        session['code_failures'] = 0
        session['message'] = (
            '验证码已发送，请查收短信' if session['msgid']
            else '验证码请求已提交；若没收到短信，请稍后重试')
        session['updated'] = _now()
        return _view(session)


def submit_code(login_id: str, code: object) -> dict:
    """提交短信验证码：验证码登录或微信补绑，成功即落盘账号。"""
    login_id = str(login_id or '').strip()
    code = str(code or '').strip()
    if not code:
        raise PlatformError('请输入验证码', status=400, code='missing_code')
    session = _require_code_session(login_id)
    needs_bind = str(session.get('status') or '') == 'needs_bind'
    with _lock:
        session = _sessions.get(login_id)
        if session is None:
            raise PlatformError('登录会话已过期，请重新发起', status=404,
                                code='login_not_found')
        if int(session.get('code_failures') or 0) >= _CODE_MAX_FAILURES:
            raise PlatformError('验证码错误次数过多，请重新发起登录',
                                status=429, code='code_failures')
        phone = str(session.get('phone') or '')
        msgid = str(session.get('msgid') or '')
        rcode = str(session.get('bind_rcode') or '')
        if not phone:
            raise PlatformError('请先填写手机号并获取验证码', status=400,
                                code='missing_phone')
        if not msgid:
            raise PlatformError('还没有发送验证码，请先点「获取验证码」',
                                status=400, code='code_not_sent')
        if needs_bind and not rcode:
            raise PlatformError('微信授权已过期，请重新扫码', status=409,
                                code='login_not_pending')
        session['status'] = 'exchanging'
        session['message'] = '正在校验验证码…'
        session['updated'] = _now()

    try:
        if needs_bind:
            info = client.bind_check_code_sync(rcode, code, msgid)
        else:
            info = client.login_by_sms_sync(phone, code, msgid)
    except PlatformError as exc:
        with _lock:
            session = _sessions.get(login_id)
            if session is not None:
                failures = int(session.get('code_failures') or 0) + 1
                session['code_failures'] = failures
                if failures >= _CODE_MAX_FAILURES:
                    session['status'] = 'error'
                    session['message'] = '验证码错误次数过多，请重新发起登录'
                else:
                    session['status'] = 'needs_bind' if needs_bind else 'pending'
                    session['message'] = exc.message or '验证码不正确，请重试'
                session['updated'] = _now()
        raise

    return _finish(login_id, info)


# ── 对外 ────────────────────────────────────────────────────
def start(mode: str = 'qr') -> dict:
    """发起一次登录，返回 `{login_id, url, qr, expires_in}`。

    `mode='qr'`（默认）：立刻返回（`qr` 为空、状态 `pending`），取二维码放在
    后台线程里做，前端轮询第一次就能拿到 `qr`。取码失败也不让整个登录失败
    ——照旧返回授权页 URL，用户可以在浏览器里扫码后把回调链接粘回来。

    `mode='sms'`：只建会话，不取码、不开线程；前端随后调 `send_code`。
    """
    mode = 'sms' if str(mode or '').strip().lower() == 'sms' else 'qr'
    now = _now()
    login_id = uuid.uuid4().hex        # 扫码时同时就是微信原样回传的 state
    url = wechat.build_auth_url(login_id) if mode == 'qr' else ''
    with _lock:
        _prune(now)
        pending = sum(1 for item in _sessions.values()
                      if str(item.get('status') or '')
                      in ('pending', 'exchanging', 'needs_bind'))
        if pending >= _MAX_PENDING:
            raise PlatformError('同时进行的登录会话过多，请稍后重试',
                                status=429, code='too_many_sessions')
        _sessions[login_id] = {
            'id': login_id,
            'mode': mode,
            'status': 'pending',
            'message': ('正在获取二维码…' if mode == 'qr'
                        else '填写手机号并获取验证码'),
            'uuid': '',
            'qr': '',
            'url': url,
            'phone': '',
            'msgid': '',
            'bind_rcode': '',
            'code_sent_at': 0.0,
            'sms_sends': 0,
            'code_failures': 0,
            'sending': False,
            'account': None,
            'created': now,
            'updated': now,
            'stop': threading.Event(),
        }

    if mode == 'qr':
        threading.Thread(target=_run, args=(login_id,),
                         name='loomy-wechat-login', daemon=True).start()
    with _lock:
        session = _sessions.get(login_id) or {}
        return {**_view(session), 'expires_in': _session_ttl(session)}


def poll(login_id: str) -> dict:
    """查询登录状态（惰性过期：只有进行中的状态才会超时）。"""
    login_id = str(login_id or '').strip()
    with _lock:
        session = _sessions.get(login_id)
        if session is None:
            raise PlatformError('登录会话不存在或已过期', status=404,
                                code='login_not_found')
        if str(session.get('status') or '') in ('pending', 'exchanging',
                                               'needs_bind') \
                and _now() - float(session.get('created') or 0) \
                > _session_ttl(session):
            session['status'] = 'expired'
            session['message'] = ('二维码已过期，请重新发起'
                                  if str(session.get('mode') or 'qr') == 'qr'
                                  else '登录已超时，请重新发起')
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
