"""从本机小浣熊客户端导入已登录的会话（**只读**）。

桌面客户端把登录态明文写在用户目录：

    %USERPROFILE%\\.box-agent\\config\\auth.json

`BOX_AGENT_CONFIG_DIR` 可覆盖这个目录（便携版安装、测试与自定义数据目录）。
文件里的有用字段：

    access_token / refresh_token / office_identity / office_org_name
    / office_org_role

也兼容 `token` / `auth_token` 作为 access 别名——不同版本客户端的写法不完全
一致。这里**只读不写**：客户端自己维护这份文件，我们借它的登录态用一次，
再落到本平台的账号目录；写回反而可能把它正在用的 refresh token 顶掉。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger('workbuddy.platforms.raccoon.desktop')

#: 客户端配置目录名（用户主目录下）
CONFIG_DIR = '.box-agent'
CONFIG_SUBDIR = 'config'
CONFIG_FILE = 'auth.json'


def client_root() -> Path:
    """客户端配置根目录；`BOX_AGENT_CONFIG_DIR` 可覆盖。"""
    override = str(os.environ.get('BOX_AGENT_CONFIG_DIR') or '').strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / CONFIG_DIR / CONFIG_SUBDIR


def storage_paths() -> list[dict]:
    """本机存在的登录态文件：`[{path, user}]`。"""
    root = client_root()
    path = root / CONFIG_FILE
    if not path.is_file():
        # 部分版本把 auth.json 直接放在 `.box-agent/` 下。
        alt = root.parent / CONFIG_FILE
        if alt.is_file():
            path = alt
        else:
            return []
    try:
        user = Path.home().name or os.environ.get('USERNAME') or ''
    except OSError:  # pragma: no cover —— 极少数无主目录环境
        user = ''
    return [{'path': path, 'user': user}]


def _read_auth(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as exc:
        logger.warning('读取小浣熊客户端登录态失败（%s）：%s', path, exc)
        return None
    if not isinstance(data, dict):
        return None
    # 有些版本把凭据包在 `auth` / `tokens` 里。
    for key in ('auth', 'tokens', 'credential', 'credentials'):
        nested = data.get(key)
        if isinstance(nested, dict):
            data = {**data, **nested}
    return data


def _pick(raw: dict, *keys: str) -> str:
    for key in keys:
        value = raw.get(key)
        if value not in (None, ''):
            return str(value).strip()
    return ''


def read_accounts() -> list[dict]:
    """读出本机已登录的小浣熊账号（含明文 token，仅供导入流程）。"""
    accounts: list[dict] = []
    seen: set[str] = set()
    for entry in storage_paths():
        data = _read_auth(entry['path'])
        if data is None:
            continue
        access = _pick(data, 'access_token', 'accessToken', 'token',
                       'auth_token', 'authToken')
        refresh = _pick(data, 'refresh_token', 'refreshToken', 'refresh')
        if not access and not refresh:
            continue
        stamp = access or refresh
        if stamp in seen:
            continue
        seen.add(stamp)
        accounts.append({
            'access_token': access,
            'refresh_token': refresh,
            'orgCode': _pick(data, 'org_code', 'orgCode', 'X-Org-Code'),
            'office_identity': _pick(data, 'office_identity', 'officeIdentity'),
            'office_org_name': _pick(data, 'office_org_name', 'officeOrgName'),
            'office_org_role': _pick(data, 'office_org_role', 'officeOrgRole'),
            'nickname': _pick(data, 'nickname', 'name', 'display_name'),
            'email': _pick(data, 'email'),
            'source': 'desktop',
            'desktopPath': str(entry['path']),
        })
    return accounts


__all__ = ['client_root', 'storage_paths', 'read_accounts']
