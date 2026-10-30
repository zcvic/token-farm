"""Trae 账号文件存储。

位置：`<DATA_DIR>/platforms/trae/accounts/<uid>.json`

与 WorkBuddy 的 `auths/` 严格分开——两边文件格式、刷新语义、失效判断都不同，
混放会让「面板显示一个、上游池里是另一个」这类不一致重新出现。

写入必须是**原子替换 + 文件锁**：Trae 的 refreshToken 是轮换式的，一次刷新
会作废旧值；如果落盘用 `write_text`（先截断再写），任何并发读或中途崩溃都会
留下半截文件，下一次刷新就再也拿不到正确凭据。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from ... import config
from ...services.filelock import file_lock

# uid 会成为文件名，必须限制成最小安全字符集：拒绝 `..`、路径分隔符、NUL。
_SAFE_ID = re.compile(r'^[0-9A-Za-z_-]{1,128}$')

_REALM_LABEL = {'cn': '国内版', 'intl': '国际版'}


def accounts_dir() -> Path:
    path = config.DATA_DIR / 'platforms' / 'trae' / 'accounts'
    path.mkdir(parents=True, exist_ok=True)
    if os.name != 'nt':
        # 目录里是明文凭据：POSIX 上只给属主读写（Windows 由 ACL 决定，chmod 无意义）
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    return path


def safe_id(account_id: object) -> str:
    """校验并返回可用作文件名的账号 id。"""
    value = str(account_id or '').strip()
    if not _SAFE_ID.match(value):
        raise ValueError('非法的账号 id')
    return value


def account_path(account_id: object) -> Path:
    return accounts_dir() / f'{safe_id(account_id)}.json'


# ── 读 ───────────────────────────────────────────────────────
def list_accounts() -> list[dict]:
    """全部账号（**含**明文，仅限内部使用）。

    单个文件损坏时跳过而不是整体抛错：一个坏文件不该让整个账号页打不开，
    否则管理员连删掉它的入口都没有。
    """
    out: list[dict] = []
    for path in sorted(accounts_dir().glob('*.json')):
        if not _SAFE_ID.match(path.stem):
            continue
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and str(data.get('uid') or '').strip():
            out.append(data)
    out.sort(key=lambda a: (str(a.get('realm') or ''), str(a.get('uid') or '')))
    return out


def read_account(account_id: object) -> dict | None:
    path = account_path(account_id)
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# ── 写 ───────────────────────────────────────────────────────
def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    """临时文件 + os.replace：任何时刻磁盘上都是一份完整 JSON。"""
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(prefix='.trae-', suffix='.json.tmp', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != 'nt':
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_account(account: dict) -> dict:
    """保存账号（合并旧字段），返回落盘后的完整结构。

    **合并而不是覆盖**：刷新只带回 token 三件套，昵称 / 邮箱 / 代理是导入时
    写的；覆盖会把它们清空，界面上就变成"刷新后昵称丢了"。
    """
    if not isinstance(account, dict):
        raise ValueError('账号必须是对象')
    uid = safe_id(account.get('uid'))
    now = int(time.time())
    path = account_path(uid)
    with file_lock(path):
        old: dict = {}
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
            if isinstance(raw, dict):
                old = raw
        except (OSError, ValueError):
            old = {}
        merged = {**old, **account, 'uid': uid}
        merged['createdAt'] = int(old.get('createdAt') or account.get('createdAt') or now)
        merged['updatedAt'] = now
        _atomic_write(path, merged)
    return merged


def delete_account(account_id: object) -> bool:
    path = account_path(account_id)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ValueError(f'删除账号失败: {exc}') from exc


# ── 脱敏 ─────────────────────────────────────────────────────
def mask_token(token: object) -> str:
    text = str(token or '')
    if not text:
        return ''
    if len(text) <= 12:
        return '*' * len(text)
    return f'{text[:6]}…{text[-4:]}'


def public_account(account: dict) -> dict:
    """对外展示形态：**不含** token / refreshToken，只留过期与指纹摘要。

    管理端接口一律走这里，避免哪天不小心把整份账号 JSON 直接返回给前端。
    """
    from . import fingerprint

    uid = str(account.get('uid') or '')
    realm = str(account.get('realm') or 'cn')
    expired_at = str(account.get('expiredAt') or '')
    return {
        'platform': 'trae',
        'uid': uid,
        'nickname': str(account.get('nickname') or ''),
        'email': str(account.get('email') or ''),
        'realm': realm,
        'realm_label': _REALM_LABEL.get(realm, realm),
        'proxy': str(account.get('proxy') or ''),
        'expiredAt': expired_at,
        'refreshExpiredAt': str(account.get('refreshExpiredAt') or ''),
        'has_token': bool(account.get('token')),
        'has_refresh_token': bool(account.get('refreshToken')),
        'token_preview': mask_token(account.get('token')),
        # 是否「一键导入自本机客户端」：决定刷新后要不要把新凭据写回 IDE
        'source': 'desktop' if account.get('desktopPath') else 'manual',
        'createdAt': int(account.get('createdAt') or 0),
        'updatedAt': int(account.get('updatedAt') or 0),
        'refreshedAt': str(account.get('refreshedAt') or ''),
        'lastRefreshError': str(account.get('lastRefreshError') or ''),
        'fingerprint': fingerprint.summary(uid),
    }


def expired(account: dict, skew: int = 120) -> bool:
    """access token 是否已过期（默认提前 120 秒视为过期）。"""
    from .client import parse_epoch

    expires = parse_epoch(account.get('expiredAt'))
    if expires is None:
        return False
    return time.time() + max(0, skew) >= expires
