"""MiniMax Code 网页登录：OAuth 设备码（device flow + PKCE）。

流程与官方客户端一致：

  1. `POST <account>/oauth2/device/code` 拿 `user_code` 与授权页地址；
  2. 用户在浏览器里确认；
  3. 后端每 `interval` 秒 `POST <account>/oauth2/token`
     （`grant_type=urn:ietf:params:oauth:grant-type:device_code`）；
  4. 成功后用 token 读一次账号信息，落盘。

与 Trae / Qoder 的网页登录一样：会话只在内存里，进程重启后要重新发起。
设备码请求（`start`）在路由线程里同步完成，先拿到 `url` 再返回，前端可以
立刻打开浏览器；真正的轮询在后台线程里，`login_start` 不会等用户确认。
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Any

from .. import registry
from ..base import PlatformError
from . import client, store

__all__ = ['start', 'poll', 'cancel', 'LOGIN_TTL']

LOGIN_TTL = 600                 # 设备码有效期兜底（秒）
_KEEP_DONE = 300                # 终态后保留多久供前端轮询
_MAX_PENDING = 6                # 同时挂起的登录会话上限
_RETRY_DELAY = 3                # 网络失败后的重试间隔

_sessions: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


def _now() -> float:
    return time.time()


def _view(session: dict) -> dict:
    return {
        'login_id': str(session.get('id') or ''),
        'status': str(session.get('status') or 'pending'),
        'message': str(session.get('message') or ''),
        'account': session.get('account'),
        'realm': str(session.get('realm') or 'cn'),
        # 设备码登录没有二维码；`url` 是浏览器要打开的授权页。
        'qr': '',
        'url': str(session.get('url') or ''),
        'user_code': str(session.get('user_code') or ''),
        'expires_in': max(0, int(float(session.get('deadline') or 0) - _now())),
    }


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


def start(realm: str | None = None) -> dict:
    """发起一次设备码登录，返回 `{login_id, url, user_code, expires_in}`。"""
    realm = store.normalize_realm(realm)
    with _lock:
        _prune(_now())
        pending = sum(1 for s in _sessions.values()
                      if s.get('status') in ('pending', 'exchanging'))
        if pending >= _MAX_PENDING:
            raise PlatformError('登录会话过多，请稍后再试', status=429,
                                code='too_many_logins')
        login_id = uuid.uuid4().hex
        _sessions[login_id] = {
            'id': login_id,
            'realm': realm,
            'status': 'pending',
            'message': '正在向 MiniMax 申请设备码…',
            'created': _now(),
            'updated': _now(),
            'url': '',
            'user_code': '',
            'deadline': _now() + LOGIN_TTL,
            'stop': threading.Event(),
        }

    # 设备码请求同步做完再返回：前端拿到 `login_id` 后会立刻打开 `url`，
    # 如果这里交给后台线程，返回的 `url` 还是空串，浏览器会先开一个空白页。
    # 与 Qoder 的 `login_start` 一样，这一次请求在路由线程里完成。
    try:
        device = client.start_device_sync(realm)
    except PlatformError as exc:
        _set(login_id, 'error', exc.message)
        return poll(login_id)
    except Exception as exc:  # noqa: BLE001 —— 不能把会话挂在 pending
        _set(login_id, 'error', f'申请设备码失败：{exc}')
        return poll(login_id)

    deadline = _now() + float(device.get('expires_in') or LOGIN_TTL)
    with _lock:
        session = _sessions.get(login_id)
        if session is not None:
            session['url'] = device.get('url') or ''
            session['user_code'] = device.get('user_code') or ''
            session['deadline'] = deadline
            session['message'] = (f"请在浏览器中确认代码 {device.get('user_code')}；"
                                  '确认后本页会自动完成登录')
            session['updated'] = _now()

    threading.Thread(target=_run, args=(login_id, realm, device, deadline),
                     daemon=True,
                     name=f'minimax-login-{login_id[:8]}').start()
    return poll(login_id)


def poll(login_id: str) -> dict:
    with _lock:
        _prune(_now())
        session = _sessions.get(str(login_id or '').strip())
        if session is None:
            raise PlatformError('登录会话不存在或已过期，请重新发起',
                                status=404, code='login_not_found')
        return _view(session)


def cancel(login_id: str) -> bool:
    with _lock:
        session = _sessions.pop(str(login_id or '').strip(), None)
    if session is None:
        return False
    stop = session.get('stop')
    if stop is not None:
        stop.set()
    return True


# ── 后台线程 ────────────────────────────────────────────────
def _run(login_id: str, realm: str, device: dict, deadline: float) -> None:
    interval = float(device.get('interval') or 5)
    while _now() < deadline:
        with _lock:
            session = _sessions.get(login_id)
            if session is None or session.get('status') != 'pending':
                return
            stop = session.get('stop')
        if stop is not None and stop.wait(interval):
            return
        if _now() >= deadline:
            break
        try:
            out = client.poll_device_sync(realm, device)
        except PlatformError as exc:
            if exc.status in (401, 403):
                _set(login_id, 'error', exc.message)
                return
            interval = min(15.0, interval + _RETRY_DELAY)
            continue
        except Exception:  # noqa: BLE001 —— 单次轮询失败不应结束会话
            interval = min(15.0, interval + _RETRY_DELAY)
            continue
        status = str(out.get('status') or '')
        if status == 'pending':
            continue
        if status == 'slow_down':
            interval = min(20.0, interval + 5)
            continue
        if status == 'denied':
            _set(login_id, 'error', '已在浏览器中拒绝登录')
            return
        if status == 'expired':
            _set(login_id, 'expired', '设备码已过期，请重新发起登录')
            return
        token = out.get('token') if isinstance(out.get('token'), dict) else {}
        if status == 'done' and token:
            _finish(login_id, realm, token)
            return

    _set(login_id, 'expired', '设备码已过期，请重新发起登录')


def _finish(login_id: str, realm: str, token: dict) -> None:
    _set(login_id, 'exchanging', '正在保存登录态…')
    access = str(token.get('access_token') or '')
    refresh = str(token.get('refresh_token') or '')
    info: dict = {}
    try:
        info = client.fetch_identity_sync(realm, access)
    except PlatformError:
        # 拿不到账号信息不应该让整次登录失败：refresh token 已经到手，
        # `uid` 可以先由 token 派生，首次刷新对话时再补齐 realUserID。
        info = {}
    adapter = registry.get('minimax')
    if adapter is None:
        _set(login_id, 'error', 'MiniMax 平台未注册')
        return
    payload = {
        'realm': realm,
        'access_token': access,
        'refresh_token': refresh,
        'expires_in': token.get('expires_in'),
        'realUserID': str(info.get('realUserID') or ''),
        'email': str(info.get('email') or ''),
        'name': str(info.get('name') or ''),
        'nickname': str(info.get('name') or ''),
        'source': 'web',
    }
    try:
        account = adapter.import_account(payload)
    except PlatformError as exc:
        _set(login_id, 'error', exc.message)
        return
    except Exception as exc:  # noqa: BLE001
        _set(login_id, 'error', f'保存登录态失败：{exc}')
        return
    _set(login_id, 'success', '登录成功', account)
