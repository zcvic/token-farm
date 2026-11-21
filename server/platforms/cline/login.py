"""Cline 网页登录：WorkOS 设备授权码 → Cline refresh token。

流程与社区实现 `luawei1/cline2api` 一致（见 `deploy/cline-ref.txt`）：

  1. `POST api.workos.com/user_management/authorize/device`（`client_id` 公开）
     拿 `device_code` / `user_code` / `verification_uri_complete`；
  2. 用户在浏览器里登录并授权；
  3. 后端每 `interval` 秒 `POST .../authenticate`
     （`grant_type=urn:ietf:params:oauth:grant-type:device_code`）：
     `authorization_pending` = 等待、`slow_down` = 放慢、`access_denied` = 拒绝、
     `expired_token` = 过期、带 `access_token` = 通过；
  4. 用 WorkOS token 调 `POST api.cline.bot/api/v1/auth/register` 换 Cline 的
     **refresh token**（后续对话全靠它，WorkOS token 不进账号文件）。

与其它平台的网页登录一样：会话只在内存里，进程重启后要重新发起。设备码请求
（`start`）在路由线程里同步完成，先拿到 `url` 再返回；真正的轮询在后台线程。
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
            'message': '正在向 Cline 申请设备码…',
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
        device = client.start_device()
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
                     name=f'cline-login-{login_id[:8]}').start()
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
            out = client.poll_device(str(device.get('device_code') or ''))
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
        tokens = out.get('tokens') if isinstance(out.get('tokens'), dict) else {}
        if status == 'done' and tokens:
            _finish(login_id, tokens)
            return

    _set(login_id, 'expired', '设备码已过期，请重新发起登录')


def _finish(login_id: str, workos: dict) -> None:
    _set(login_id, 'exchanging', '正在换取 Cline 登录态…')
    try:
        registered = client.register_sync(workos)
    except PlatformError as exc:
        _set(login_id, 'error', exc.message)
        return
    except Exception as exc:  # noqa: BLE001
        _set(login_id, 'error', f'换取 Cline 登录态失败：{exc}')
        return
    adapter = registry.get('cline')
    if adapter is None:
        _set(login_id, 'error', 'Cline 平台未注册')
        return
    payload = {
        'refresh_token': str(registered.get('refresh_token') or ''),
        'email': str(registered.get('email') or ''),
        'nickname': str(registered.get('email') or '').split('@')[0],
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
