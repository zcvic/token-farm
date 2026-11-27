"""AutoClaw 登录：国内短信验证码、国际 OAuth「打开授权页 + 粘回调」。

两个区域是两套完全不同的登录方式，本模块按区域分派：

  · **国内 `cn`** —— +86 手机号 + 短信验证码，没有浏览器：
    `login_start(mode='sms')` 只建会话，前端就地收手机号；
    `send_code` 下发短信，`submit_code` 换 token 并落盘。

  · **国际 `global`** —— Google / z.ai 网页 OAuth。官方客户端的回调固定落在
    `http://localhost:<白名单端口>/auth/callback-<vendor>`，端口是注册过的，
    随便挑会得到 `Redirect URI not registered for this client`。第一版**不内嵌
    阿里云滑块**（纯服务端做不了，内嵌浏览器自动化又违反项目红线），所以只做
    「申请授权页 → 用户在浏览器里完成 → 把地址栏里的完整链接粘回来」。

为什么国内不用二维码：上游只提供短信这一条网页登录链路，客户端本身也没有扫码。

登录态只存在内存（`_sessions`）：进程重启后要重新发起，与 Trae / Loomy /
MiniMax Code 一致。会话单次使用，成功后立即 expire。

发码冷却（60 秒）、单会话发码上限（5 次）、验证码错误上限（5 次）与 Loomy 的
短信模式同一套口径——这些是防误触 / 防爆破的界面级约束，不是上游契约。
"""
from __future__ import annotations

import asyncio
import math
import threading
import time
import urllib.parse
import uuid
from typing import Any

from .. import registry
from ..base import PlatformError
from . import client, constants as C, protocol as P, store

__all__ = ['start', 'poll', 'cancel', 'send_code', 'submit_code',
           'submit_callback', 'LOGIN_TTL', 'CODE_TTL']

LOGIN_TTL = 900                 # 授权页 / 验证码阶段的有效期（秒）
CODE_TTL = 900                  # 验证码阶段（等短信）的有效期（秒）
_KEEP_DONE = 300                # 终态后保留多久供前端轮询（秒）
_MAX_PENDING = 6                # 同时挂起的登录会话上限
_SMS_COOLDOWN = 60              # 同一会话两次发码的最小间隔（秒）
_SMS_MAX_SENDS = 5              # 同一会话最多发几条短信
_CODE_MAX_FAILURES = 5          # 连续输错几次后作废会话
_MAX_CALLBACK_LEN = 8 * 1024
# 国际 OAuth 的回调端口：官方 client 的白名单，**不能**随便换。
_OAUTH_PORT = C.OAUTH_CALLBACK_PORTS[0]

_sessions: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


def _now() -> float:
    return time.time()


def _prune(now: float) -> None:
    """清掉终态很久的会话（调用方持锁）。"""
    for key, session in list(_sessions.items()):
        if now - float(session.get('updated') or session.get('created') or now) \
                > _KEEP_DONE:
            _sessions.pop(key, None)


def _session_ttl(session: dict) -> float:
    return CODE_TTL if str(session.get('mode') or '') == 'sms' else LOGIN_TTL


def _mask_phone(phone: str) -> str:
    text = str(phone or '')
    if len(text) < 7:
        return text
    return f'{text[:3]}****{text[-4:]}'


def _normalize_phone(phone: object) -> str:
    """只支持大陆手机号（上游固定 +86）：去掉空白 / 连字符 / +86。"""
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
        'realm': str(session.get('realm') or C.DEFAULT_REALM),
        # AutoClaw 没有二维码；国际版给的是浏览器要打开的授权页。
        'qr': '',
        'url': str(session.get('url') or ''),
        'mode': str(session.get('mode') or 'sms'),
        'needs_bind': False,
        'code_sent': bool(session.get('msgid')),
        'retry_after': cooldown,
        'phone': _mask_phone(str(session.get('phone') or '')),
        'expires_in': max(0, int(float(session.get('created') or now)
                                 + _session_ttl(session) - now)),
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


# ── 事件循环 / 线程 ─────────────────────────────────────────
def _run_sync(coro):
    """在同步调用里跑一段协程。

    `login_start` 由路由**直接**调用（不是 `to_thread`），此时当前线程里有
    正在跑的事件循环，`asyncio.run` 会直接报错。国际版申请授权页是一次真实
    的上游请求，只能把它放到工作线程里跑——与 MiniMax Code 的「设备码在路由
    线程里同步请求」同一取舍，区别只是这里必须换线程。
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

    thread = threading.Thread(target=runner, name='autoclaw-login', daemon=True)
    thread.start()
    thread.join()
    if 'error' in box:
        raise box['error']
    return box.get('value')


# ── 收尾 ────────────────────────────────────────────────────
def _finish(login_id: str, token: dict, *, source: str) -> dict:
    """`token 结构` → 落盘账号（短信 / OAuth 共用）。"""
    adapter = registry.get('autoclaw')
    if adapter is None:
        message = 'AutoClaw 平台未注册'
        _set_status(login_id, 'error', message)
        raise PlatformError(message, status=500, code='platform_missing')

    with _lock:
        session = dict(_sessions.get(login_id) or {})
    realm = store.normalize_realm(session.get('realm'))
    try:
        # 复用导入路径：uid 推导、脱敏、调度字段清理都在那一处。
        account = adapter.import_account({
            'realm': realm,
            'access_token': str(token.get('access_token') or ''),
            'refresh_token': str(token.get('refresh_token') or ''),
            'expires_at': token.get('expires_at') or '',
            'refresh_expires_at': token.get('refresh_expires_at') or '',
            'device_id': str(token.get('device_id')
                             or session.get('device_id') or ''),
            'phone': str(session.get('phone') or ''),
            'source': source,
        })
    except PlatformError as exc:
        _set_status(login_id, 'error', exc.message)
        raise
    except Exception as exc:  # noqa: BLE001 —— 落盘异常也要让前端看到原因
        message = f'登录成功但账号保存失败：{exc}'
        _set_status(login_id, 'error', message)
        raise PlatformError(message, status=500,
                            code='login_save_failed') from exc

    # 顺手读一次 `user-info`：昵称 / 邮箱 / 手机号只有这里有，而且客户端启动
    # 那次调用会触发登录奖励入账。失败不影响登录成功。
    access = str(token.get('access_token') or '')
    if access:
        try:
            info = _run_sync(client.user_info(
                realm, access,
                device_id=str(token.get('device_id')
                              or session.get('device_id') or ''),
                timeout=C.ACCOUNT_TIMEOUT))
            patch: dict[str, Any] = {}
            if info.get('name'):
                patch['nickname'] = info['name']
            if info.get('email'):
                patch['email'] = info['email']
            if info.get('phone'):
                patch['phone'] = info['phone']
            if patch:
                account = store.public_account(store.patch_account(
                    str(account.get('uid') or ''), patch))
        except (PlatformError, ValueError, OSError, RuntimeError):
            pass

    _set_status(login_id, 'success', '登录成功', account=account)
    with _lock:
        return _view(_sessions.get(login_id) or {})


# ── 发起 ────────────────────────────────────────────────────
def _oauth_state_of(url: str) -> str:
    """从授权页地址里取 `state`（上游自己生成的，粘回来时要比对）。"""
    raw = str(url or '').strip()
    if not raw:
        return ''
    try:
        query = urllib.parse.urlsplit(raw).query
    except ValueError:
        return ''
    params = urllib.parse.parse_qs(query, keep_blank_values=True)
    return str((params.get('state') or [''])[0])


def _start_oauth(session: dict) -> None:
    """申请授权页；失败把会话推进到 `error`，不抛给路由。"""
    login_id = str(session.get('id') or '')
    realm = str(session.get('realm') or 'global')
    vendor = str(session.get('vendor') or 'zai')
    navigate = str(session.get('navigate_uri') or '')
    device = str(session.get('device_id') or '')
    try:
        url = _run_sync(client.oauth_url(
            realm, vendor, device_id=device, navigate_uri=navigate,
            timeout=C.ACCOUNT_TIMEOUT))
    except PlatformError as exc:
        hint = ''
        if 'captcha' in str(exc.code or '').lower() \
                or '验证' in str(exc.message or ''):
            hint = '（上游要求人机校验；第一版不支持内嵌滑块，请稍后重试）'
        _set_status(login_id, 'error',
                    f'申请授权页失败：{exc.message}{hint}')
        return
    except Exception as exc:  # noqa: BLE001 —— 会话不能挂在 pending
        _set_status(login_id, 'error', f'申请授权页失败：{exc}')
        return

    with _lock:
        current = _sessions.get(login_id)
        if current is None:
            return
        current['url'] = url
        current['state'] = _oauth_state_of(url)
        current['message'] = ('请在浏览器里完成授权；完成后把地址栏里的完整链接'
                              '粘贴到下方')
        current['updated'] = _now()


def start(realm: str | None = None, mode: str = '') -> dict:
    """发起一次登录，返回 `{login_id, url, mode, expires_in, ...}`。

    国内固定短信：`mode` 无论传什么都会被改成 `sms`，`url` 为空。
    国际固定 OAuth：`mode` 可以传 `google` / `zai` 选提供方，默认 `zai`。
    """
    region = store.normalize_realm(realm)
    requested = str(mode or '').strip().lower()
    vendor = 'google' if requested == 'google' else 'zai'
    login_id = uuid.uuid4().hex
    now = _now()

    if region == 'cn':
        entry: dict[str, Any] = {
            'id': login_id,
            'realm': region,
            'mode': 'sms',
            'vendor': '',
            'status': 'pending',
            'message': '填写手机号并获取验证码',
            'phone': '',
            'msgid': '',
            'device_id': store.new_device_id(),
            'code_sent_at': 0.0,
            'sms_sends': 0,
            'code_failures': 0,
            'sending': False,
            'account': None,
            'url': '',
            'state': '',
            'navigate_uri': '',
            'created': now,
            'updated': now,
        }
    else:
        entry = {
            'id': login_id,
            'realm': region,
            'mode': 'oauth',
            'vendor': vendor,
            'status': 'pending',
            'message': '正在申请授权页…',
            'phone': '',
            'msgid': '',
            'device_id': store.new_device_id(),
            'code_sent_at': 0.0,
            'sms_sends': 0,
            'code_failures': 0,
            'sending': False,
            'account': None,
            'url': '',
            'state': '',
            'navigate_uri': P.oauth_callback_url(_OAUTH_PORT, vendor),
            'created': now,
            'updated': now,
        }

    with _lock:
        _prune(now)
        pending = sum(1 for item in _sessions.values()
                      if str(item.get('status') or '')
                      in ('pending', 'exchanging'))
        if pending >= _MAX_PENDING:
            raise PlatformError('同时进行的登录会话过多，请稍后重试',
                                status=429, code='too_many_sessions')
        _sessions[login_id] = entry

    if region != 'cn':
        _start_oauth(entry)
    with _lock:
        return _view(_sessions.get(login_id) or {})


def poll(login_id: str) -> dict:
    """查询登录状态（惰性过期：只有进行中的状态才会超时）。"""
    login_id = str(login_id or '').strip()
    with _lock:
        session = _sessions.get(login_id)
        if session is None:
            raise PlatformError('登录会话不存在或已过期', status=404,
                                code='login_not_found')
        if str(session.get('status') or '') in ('pending', 'exchanging') \
                and _now() - float(session.get('created') or 0) \
                > _session_ttl(session):
            session['status'] = 'expired'
            session['message'] = '登录已超时，请重新发起'
            session['updated'] = _now()
        return _view(session)


def cancel(login_id: str) -> bool:
    with _lock:
        return _sessions.pop(str(login_id or '').strip(), None) is not None


# ── 国内短信 ────────────────────────────────────────────────
def _require_sms_session(login_id: str) -> dict:
    with _lock:
        session = _sessions.get(login_id)
        if session is None:
            raise PlatformError('登录会话不存在或已过期，请重新发起',
                                status=404, code='login_not_found')
        status = str(session.get('status') or '')
        if status in ('success', 'error', 'expired'):
            raise PlatformError('登录会话已失效，请重新发起', status=409,
                                code='login_not_pending')
        if status == 'exchanging':
            raise PlatformError('正在处理上一步，请稍候', status=429,
                                code='login_busy')
        if str(session.get('mode') or '') != 'sms':
            raise PlatformError('该登录方式不用短信验证码', status=400,
                                code='code_not_supported')
        return dict(session)


def send_code(login_id: str, phone: object) -> dict:
    """下发国内短信验证码。同会话 60 秒一次，最多 5 次。"""
    login_id = str(login_id or '').strip()
    session = _require_sms_session(login_id)
    with _lock:
        current = _sessions.get(login_id)
        if current is None:
            raise PlatformError('登录会话已过期，请重新发起', status=404,
                                code='login_not_found')
        if current.get('sending'):
            raise PlatformError('验证码正在发送，请稍候', status=429,
                                code='login_busy')
        now = _now()
        sent_at = float(current.get('code_sent_at') or 0)
        if sent_at and now - sent_at < _SMS_COOLDOWN:
            wait = int(math.ceil(sent_at + _SMS_COOLDOWN - now))
            raise PlatformError(f'请 {wait} 秒后再获取验证码', status=429,
                                code='sms_cooldown', retry_after=wait)
        if int(current.get('sms_sends') or 0) >= _SMS_MAX_SENDS:
            raise PlatformError('验证码发送次数过多，请重新发起登录',
                                status=429, code='sms_limit')
        normalized = _normalize_phone(phone or current.get('phone'))
        device = str(current.get('device_id') or '')
        current['sending'] = True
        current['updated'] = now

    try:
        out = _run_sync(client.send_code(
            session.get('realm'), normalized, device_id=device,
            timeout=C.ACCOUNT_TIMEOUT))
    except PlatformError:
        with _lock:
            current = _sessions.get(login_id)
            if current is not None:
                current['sending'] = False
                current['updated'] = _now()
        raise

    with _lock:
        current = _sessions.get(login_id)
        if current is None:
            raise PlatformError('登录会话已过期，请重新发起', status=404,
                                code='login_not_found')
        current['sending'] = False
        current['phone'] = normalized
        current['device_id'] = str(out.get('device_id') or device)
        current['msgid'] = str(out.get('message') or '')
        current['code_sent_at'] = _now()
        current['sms_sends'] = int(current.get('sms_sends') or 0) + 1
        current['code_failures'] = 0
        current['message'] = '验证码已发送，请查收短信'
        current['updated'] = _now()
        return _view(current)


def submit_code(login_id: str, code: object) -> dict:
    """提交短信验证码；成功即落盘账号。"""
    login_id = str(login_id or '').strip()
    text = str(code or '').strip()
    if not text:
        raise PlatformError('请输入验证码', status=400, code='missing_code')
    session = _require_sms_session(login_id)
    with _lock:
        current = _sessions.get(login_id)
        if current is None:
            raise PlatformError('登录会话已过期，请重新发起', status=404,
                                code='login_not_found')
        if int(current.get('code_failures') or 0) >= _CODE_MAX_FAILURES:
            raise PlatformError('验证码错误次数过多，请重新发起登录',
                                status=429, code='code_failures')
        phone = str(current.get('phone') or '')
        if not phone:
            raise PlatformError('请先填写手机号并获取验证码', status=400,
                                code='missing_phone')
        if not current.get('msgid'):
            raise PlatformError('还没有发送验证码，请先点「获取验证码」',
                                status=400, code='code_not_sent')
        device = str(current.get('device_id') or '')
        current['status'] = 'exchanging'
        current['message'] = '正在校验验证码…'
        current['updated'] = _now()

    try:
        token = _run_sync(client.login(
            session.get('realm'), phone, text, device_id=device,
            timeout=C.TOKEN_TIMEOUT))
    except PlatformError as exc:
        with _lock:
            current = _sessions.get(login_id)
            if current is not None:
                failures = int(current.get('code_failures') or 0) + 1
                current['code_failures'] = failures
                if failures >= _CODE_MAX_FAILURES:
                    current['status'] = 'error'
                    current['message'] = '验证码错误次数过多，请重新发起登录'
                else:
                    current['status'] = 'pending'
                    current['message'] = exc.message or '验证码不正确，请重试'
                current['updated'] = _now()
        raise

    return _finish(login_id, token, source='sms')


# ── 国际 OAuth ──────────────────────────────────────────────
def _params_of(text: object) -> dict[str, str]:
    raw = str(text or '').strip()
    if not raw:
        return {}
    if len(raw) > _MAX_CALLBACK_LEN:
        raise PlatformError('回调地址过长', status=400, code='callback_too_long')
    if '://' in raw:
        try:
            query = urllib.parse.urlsplit(raw).query
        except ValueError as exc:
            raise PlatformError('无法识别回调地址', status=400,
                                code='callback_invalid') from exc
    elif '?' in raw:
        query = raw.split('?', 1)[1]
    elif '=' in raw:
        query = raw.lstrip('?')
    else:
        raise PlatformError('无法识别回调地址，请复制浏览器地址栏里的完整链接',
                            status=400, code='callback_invalid')
    return {key: (values[0] if values else '')
            for key, values in urllib.parse.parse_qs(
                query, keep_blank_values=True).items()}


def submit_callback(login_id: str, callback: object) -> dict:
    """粘回授权页回调：解析 `code` / `state` 并换取 token。"""
    login_id = str(login_id or '').strip()
    params = _params_of(callback)
    with _lock:
        session = _sessions.get(login_id)
        if session is None:
            raise PlatformError('登录会话不存在或已过期，请重新发起',
                                status=404, code='login_not_found')
        status = str(session.get('status') or '')
        if status == 'success':
            return _view(session)
        if status not in ('pending', 'exchanging'):
            raise PlatformError('登录会话已失效，请重新发起', status=409,
                                code='login_not_pending')
        expected = str(session.get('state') or '')
        snapshot = dict(session)

    state = str(params.get('state') or '').strip()
    # state 是上游签发的：对不上说明粘的是上一次的链接。
    if expected and state and state != expected:
        raise PlatformError('回调链接与本次登录不匹配（多半是上一次的链接），'
                            '请重新发起授权', status=400,
                            code='login_state_mismatch')
    code = str(params.get('code') or '').strip()
    if not code:
        raise PlatformError('回调地址里没有 code，请等授权完成后再复制完整链接',
                            status=400, code='missing_code')

    with _lock:
        current = _sessions.get(login_id)
        if current is not None:
            current['status'] = 'exchanging'
            current['message'] = '正在换取登录态…'
            current['updated'] = _now()

    try:
        token = _run_sync(client.oauth_login(
            snapshot.get('realm'), snapshot.get('vendor') or 'zai', code,
            device_id=str(snapshot.get('device_id') or ''),
            state=state,
            navigate_uri=str(snapshot.get('navigate_uri') or ''),
            timeout=C.TOKEN_TIMEOUT))
    except PlatformError as exc:
        _set_status(login_id, 'error', exc.message)
        raise

    return _finish(login_id, token, source='oauth')
