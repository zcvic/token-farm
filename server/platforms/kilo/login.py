"""Kilo 网页登录：设备授权码（device flow）。

流程与官方客户端一致：

  1. `POST api.kilo.ai/api/device-auth/codes` 拿 `code` 与授权页地址；
  2. 用户在浏览器里打开 `verificationUrl` 并确认；
  3. 后端每 3 秒 `GET api.kilo.ai/api/device-auth/codes/{code}`：
     202 = 等待、403 = 拒绝、410 = 过期、200 = 通过并带 `token`；
  4. 拿到 token 后读一次资料，落盘。

⚠️ 轮询用的是响应里的 **`code`**，不是 `device_code`——实测后者会直接 410。

与其它平台的网页登录一样：会话只在内存里，进程重启后要重新发起。设备码请求
（`start`）在路由线程里同步完成，先拿到 `url` 再返回，前端可以立刻打开浏览器；
真正的轮询在后台线程里，`login_start` 不会等用户确认。
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Any

from .. import registry
from ..base import PlatformError
from . import client, constants as C, store

__all__ = ['start', 'poll', 'cancel', 'LOGIN_TTL']

LOGIN_TTL = C.DEVICE_TTL          # 设备码有效期兜底（秒）
_KEEP_DONE = 300                  # 终态后保留多久供前端轮询
_MAX_PENDING = 6                  # 同时挂起的登录会话上限
_RETRY_DELAY = 3                  # 网络失败后的重试间隔

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
        'realm': store.REALM,
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
            'realm': store.REALM,
            'status': 'pending',
            'message': '正在向 Kilo 申请设备码…',
            'created': _now(),
            'updated': _now(),
            'url': '',
            'user_code': '',
            'deadline': _now() + LOGIN_TTL,
            'stop': threading.Event(),
        }

    # 设备码请求同步做完再返回：前端拿到 `login_id` 后会立刻打开 `url`，
    # 如果这里交给后台线程，返回的 `url` 还是空串，浏览器会先开一个空白页。
    try:
        device = client.start_device_sync()
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

    threading.Thread(target=_run, args=(login_id, device, deadline),
                     daemon=True,
                     name=f'kilo-login-{login_id[:8]}').start()
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
def _run(login_id: str, device: dict, deadline: float) -> None:
    interval = float(device.get('interval') or C.DEVICE_INTERVAL)
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
            out = client.poll_device_sync(str(device.get('code') or ''))
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
        if status == 'denied':
            _set(login_id, 'error', '已在浏览器中拒绝登录')
            return
        if status == 'expired':
            _set(login_id, 'expired', '设备码已过期，请重新发起登录')
            return
        token = str(out.get('token') or '')
        if status == 'done' and token:
            _finish(login_id, token, str(out.get('email') or ''))
            return

    _set(login_id, 'expired', '设备码已过期，请重新发起登录')


def _finish(login_id: str, token: str, email_hint: str) -> None:
    _set(login_id, 'exchanging', '正在保存登录态…')
    info: dict = {}
    try:
        info = client.fetch_profile_sync(token)
    except PlatformError:
        # 拿不到资料不应该让整次登录失败：token 已经到手，uid 由 token 派生，
        # 首次对话 / 余额查询时会补齐邮箱。
        info = {}
    adapter = registry.get('kilo')
    if adapter is None:
        _set(login_id, 'error', 'Kilo 平台未注册')
        return
    payload = {
        'token': token,
        'email': str(info.get('email') or email_hint or ''),
        'name': str(info.get('name') or ''),
        'nickname': str(info.get('name') or ''),
        'organizationId': str(info.get('organizationId') or ''),
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
