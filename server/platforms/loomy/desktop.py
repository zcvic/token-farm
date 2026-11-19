"""从本机 Loomy 客户端导入已登录的会话（**只读**）。

官方客户端把登录态明文写在公共目录里（Windows 是
`C:\\Users\\Public\\Loomy\\<hash>\\userData\\auth-session.json`，macOS 是
`/Users/Shared/Loomy/...`；`<hash>` 是 `sha256(用户名)[:12]`），一台机器上
可能有多份（多用户 / 多个安装）。这里按通配符全部扫出来，凭据只从磁盘读到
内存、再落进本平台的账号目录。

与 Trae 的桌面导入不同，这里**不写回**客户端文件：

  · Loomy 没有 refresh token，会话失效后是我们自己用手机号 / 密码重新登录，
    拿到的是**另一个**会话，写回客户端反而会把它正在用的那份顶掉；
  · 客户端文件由客户端自己维护，我们只借它的登录态一用。

`auth-session.json` 形状（客户端自己写的）::

    {"session": "...", "userid": "...", "phone": "...", "updatedAt": 1763...000}

`updatedAt` 是毫秒时间戳。账号有没有密码决定它能不能续期：这份文件只有
会话，所以导入后的账号与「粘 session」同口径。
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

from . import constants as C

logger = logging.getLogger('workbuddy.platforms.loomy.desktop')

#: 会话文件名（客户端 userData 目录下）
SESSION_FILE = 'auth-session.json'

#: Windows 上客户端的公共数据根目录
PUBLIC_ROOT_WIN = r'C:\Users\Public\Loomy'
#: macOS 上对应的公共目录（`/Users/Shared` 是 Public 的同位目录）
PUBLIC_ROOT_MAC = '/Users/Shared/Loomy'


def client_root() -> Path:
    """客户端数据根目录；`LOOMY_CLIENT_ROOT` 可覆盖（测试与便携版安装用）。"""
    override = str(os.environ.get('LOOMY_CLIENT_ROOT') or '').strip()
    if override:
        return Path(override).expanduser()
    if sys.platform == 'darwin':
        return Path(PUBLIC_ROOT_MAC)
    return Path(PUBLIC_ROOT_WIN)


def storage_paths() -> list[dict]:
    """本机存在的会话文件：`[{path, user}]`，`user` 是 `<hash>` 目录名。"""
    root = client_root()
    if not root.is_dir():
        return []
    out: list[dict] = []
    try:
        entries = sorted(root.iterdir())
    except OSError as exc:  # 权限不足等：当作没装客户端，不阻断导入按钮
        logger.warning('读取 Loomy 客户端目录失败（%s）：%s', root, exc)
        return []
    for user_dir in entries:
        if not user_dir.is_dir():
            continue
        path = user_dir / 'userData' / SESSION_FILE
        if path.is_file():
            out.append({'path': path, 'user': user_dir.name})
    return out


def _read_session(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as exc:
        logger.warning('读取 Loomy 登录态失败（%s）：%s', path, exc)
        return None
    if not isinstance(data, dict):
        return None
    return data


def read_accounts() -> list[dict]:
    """读出本机所有已登录的 Loomy 会话（含明文 session，仅供导入流程）。"""
    accounts: list[dict] = []
    seen: set[str] = set()
    for entry in storage_paths():
        data = _read_session(entry['path'])
        if data is None:
            continue
        session = str(data.get('session') or '').strip()
        if not session or session in seen:
            continue
        seen.add(session)
        phone = str(data.get('phone') or '').strip()
        userid = str(data.get('userid') or '').strip()
        # 客户端存的是毫秒；没有时间戳（0）时交给适配器按导入时刻算。
        created_ms = 0
        try:
            created_ms = int(data.get('updatedAt') or 0)
        except (TypeError, ValueError):
            created_ms = 0
        obtained_at = created_ms // 1000 if created_ms > 0 else 0
        # 客户端文件里没有昵称；用手机号尾号占位，避免导入时再为每个账号打
        # 一次 baseInfo（那样一个连不上的账号服务会让「一键导入」长时间转圈）。
        nickname = str(data.get('nickname') or '').strip()
        if not nickname:
            nickname = f'Loomy {phone[-4:]}' if phone else 'Loomy 桌面端'
        accounts.append({
            'phone': phone,
            'userid': userid,
            'session': session,
            'nickname': nickname,
            'source': 'desktop',
            'desktopPath': str(entry['path']),
            'obtainedAt': obtained_at,
            'expireAt': (obtained_at + C.SESSION_EXPIRE_SECONDS)
            if obtained_at else 0,
        })
    return accounts
