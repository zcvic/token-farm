"""WPS 灵犀账号文件存储。

位置：`<DATA_DIR>/platforms/lingxi/accounts/<uid>.json`

凭证是**网页 Cookie**（`wps_sid`），没有 OAuth、没有 refresh token：Cookie
失效后只能重新登录网页版再抓一次。因此：

  · Cookie 必须明文落盘（上游要原样放进 `Cookie:` 头），文件权限按高敏处理；
  · 对外结构一律走 `public_account`，只回摘要（有没有 wps_sid / 尾部几位 /
    长度），绝不回明文；
  · 同一份登录态重复导入命中同一个 `uid`（由 `wps_sid` 派生），覆盖而不是新增。

写入与其它平台一致：文件锁 + 临时文件 + `os.replace` 原子替换。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ... import config
from ...services.filelock import file_lock
from . import constants as C

_SAFE_ID = re.compile(r'^[0-9A-Za-z_-]{1,128}$')

WEIGHT_MIN = 0.1
WEIGHT_MAX = 100.0

REALM = 'cn'


def accounts_dir() -> Path:
    path = config.DATA_DIR / 'platforms' / 'lingxi' / 'accounts'
    path.mkdir(parents=True, exist_ok=True)
    if os.name != 'nt':
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    return path


def safe_id(account_id: object) -> str:
    value = str(account_id or '').strip()
    if not _SAFE_ID.match(value):
        raise ValueError('非法的账号 id')
    return value


def account_path(account_id: object) -> Path:
    return accounts_dir() / f'{safe_id(account_id)}.json'


# ── Cookie ─────────────────────────────────────────────────
def parse_cookie(raw: object) -> dict[str, str]:
    """把 `a=1; b=2` 解析成字典；同名键先出现者胜出。"""
    out: dict[str, str] = {}
    for part in str(raw or '').split(';'):
        part = part.strip()
        if not part or '=' not in part:
            continue
        key, _, value = part.partition('=')
        key = key.strip()
        if key and key not in out:
            out[key] = value.strip()
    return out


def normalize_cookie(raw: object) -> str:
    """清洗用户粘贴的整段 Cookie（`Cookie:` 前缀、换行、重复分号、空白）。

    只粘 `wps_sid` 的值也接受，自动补回键名——面板里最常见的粘贴就是一段
    裸 SID。
    """
    text = str(raw or '').strip()
    text = re.sub(r'^cookie:\s*', '', text, flags=re.I)
    parts = [part.strip() for part in re.split(r'[;\r\n]+', text) if part.strip()]
    if not parts:
        return ''
    if len(parts) == 1 and '=' not in parts[0]:
        return f'wps_sid={parts[0]}'
    return '; '.join(parts)


def has_login(cookie: object) -> bool:
    jar = parse_cookie(cookie)
    return any(jar.get(field) for field in C.COOKIE_FIELDS)


def cookie_summary(cookie: object) -> dict:
    """脱敏摘要：只告诉界面「哪些字段存在」，不泄露值。"""
    jar = parse_cookie(cookie)
    sid = jar.get('wps_sid') or ''
    return {
        'has_wps_sid': bool(sid),
        'wps_sid_tail': sid[-6:] if sid else '',
        'cookie_len': len(str(cookie or '')),
        'fields': len(jar),
        'region': cookie_region(cookie),
    }


def cookie_region(cookie: object) -> str:
    """从 Cookie 里读灰度区域标记（客户端据此带 `X-Cc-Region`）。"""
    for part in str(cookie or '').split(';'):
        item = part.strip()
        prefix = 'copilot_server_branch='
        if item.startswith(prefix):
            return item[len(prefix):].strip()
    return ''


def derive_uid(cookie: object, explicit: object = None) -> str:
    """派生稳定 uid：同一份登录态重复导入覆盖同一条账号。"""
    text = str(explicit or '').strip()
    if text:
        return safe_id(text)
    jar = parse_cookie(cookie)
    basis = jar.get('wps_sid') or normalize_cookie(cookie)
    if not basis:
        raise ValueError('缺少 Cookie')
    return 'lx_' + hashlib.sha256(basis.encode('utf-8')).hexdigest()[:24]


def device_of(account: dict) -> str:
    """这个账号该用的设备号：显式填了就用，否则由 Cookie 稳定派生。"""
    explicit = str(account.get('deviceId') or '').strip()
    if explicit:
        return explicit
    return C.device_id_for(str(account.get('cookie') or ''))


def device_name_of(account: dict) -> str:
    """建会话 `env.device_name`：显式值 → 本机主机名 → token-farm。"""
    explicit = str(account.get('deviceName') or '').strip()
    if explicit:
        return explicit
    try:
        import socket
        return socket.gethostname() or 'token-farm'
    except OSError:
        return 'token-farm'


# ── 时间 / 调度字段 ─────────────────────────────────────────
def iso_from_epoch(value: object) -> str:
    try:
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ''
    if seconds <= 0:
        return ''
    return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime(
        '%Y-%m-%dT%H:%M:%SZ')


def epoch_from_iso(value: object) -> float:
    text = str(value or '').strip()
    if not text:
        return 0.0
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def weight_of(account: dict) -> float:
    try:
        value = float(account.get('weight') or 0)
    except (TypeError, ValueError):
        return 0.0
    if value != value or value in (float('inf'), float('-inf')) or value < 0:
        return 0.0
    return value


def cooling_until(account: dict, now: float | None = None) -> float:
    try:
        value = float(account.get('coolingUntil') or 0)
    except (TypeError, ValueError):
        return 0.0
    if value != value or value in (float('inf'), float('-inf')):
        return 0.0
    current = time.time() if now is None else now
    return value if value > current else 0.0


def is_cooling(account: dict, now: float | None = None) -> bool:
    return cooling_until(account, now) > 0


def is_disabled(account: dict) -> bool:
    return bool(account.get('disabled'))


def is_checkin_only(account: dict) -> bool:
    return bool(account.get('checkinOnly'))


# ── 读 ─────────────────────────────────────────────────────
def list_accounts() -> list[dict]:
    """全部账号（**含**明文 Cookie，仅限内部使用）。坏文件跳过。"""
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
    out.sort(key=lambda a: str(a.get('uid') or ''))
    return out


def read_account(account_id: object) -> dict | None:
    try:
        data = json.loads(account_path(account_id).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# ── 写 ─────────────────────────────────────────────────────
def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(prefix='.lingxi-', suffix='.json.tmp',
                               dir=str(path.parent))
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
    """保存账号（合并旧字段），返回落盘后的完整结构。"""
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
        merged['createdAt'] = int(old.get('createdAt')
                                  or account.get('createdAt') or now)
        merged['updatedAt'] = now
        _atomic_write(path, merged)
    return merged


def patch_account(account_id: object, patch: dict) -> dict:
    if not isinstance(patch, dict):
        raise ValueError('patch 必须是对象')
    path = account_path(account_id)
    with file_lock(path):
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            raise
        except (OSError, ValueError) as exc:
            raise ValueError('账号文件格式异常') from exc
        if not isinstance(raw, dict):
            raise ValueError('账号文件格式异常')
        raw.update(patch)
        raw['updatedAt'] = int(time.time())
        _atomic_write(path, raw)
    return raw


def delete_account(account_id: object) -> bool:
    try:
        account_path(account_id).unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ValueError(f'删除账号失败: {exc}') from exc


def set_weight(account_id: object, weight: object) -> dict:
    try:
        value = float(weight)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError('权重必须是数字') from exc
    if value != value or value in (float('inf'), float('-inf')):
        raise ValueError('权重必须是有限数字')
    if value != 0 and not (WEIGHT_MIN <= value <= WEIGHT_MAX):
        raise ValueError(
            f'权重需在 {WEIGHT_MIN:g} 与 {WEIGHT_MAX:g} 之间（0 = 恢复默认）')

    def mutate(raw: dict) -> None:
        if value == 0:
            raw.pop('weight', None)
        else:
            raw['weight'] = value

    return _update(account_id, mutate)


def set_proxy(account_id: object, proxy: object) -> dict:
    value = str(proxy or '').strip()

    def mutate(raw: dict) -> None:
        if value:
            raw['proxy'] = value
        else:
            raw.pop('proxy', None)

    return _update(account_id, mutate)


def set_disabled(account_id: object, disabled: bool, reason: str = '') -> dict:
    def mutate(raw: dict) -> None:
        if disabled:
            raw['disabled'] = True
            raw['disabledReason'] = str(reason or '').strip()[:200]
            raw['disabledAt'] = int(time.time())
            raw.pop('checkinOnly', None)
        else:
            raw.pop('disabled', None)
            raw.pop('disabledReason', None)
            raw.pop('disabledAt', None)

    return _update(account_id, mutate)


def set_checkin_only(account_id: object, enabled: bool) -> dict:
    def mutate(raw: dict) -> None:
        if enabled:
            raw['checkinOnly'] = True
            raw.pop('disabled', None)
            raw.pop('disabledReason', None)
            raw.pop('disabledAt', None)
        else:
            raw.pop('checkinOnly', None)

    return _update(account_id, mutate)


def set_cooling(account_id: object, until: float, reason: str = '') -> dict:
    try:
        value = float(until)
    except (TypeError, ValueError) as exc:
        raise ValueError('冷却截止时间必须是数字') from exc
    if value != value or value in (float('inf'), float('-inf')):
        raise ValueError('冷却截止时间必须是有限数字')
    if value <= time.time():
        raise ValueError('冷却截止时间必须在未来')

    def mutate(raw: dict) -> None:
        raw['coolingUntil'] = value
        raw['coolingReason'] = str(reason or '').strip()[:200]
        raw['coolingAt'] = int(time.time())

    return _update(account_id, mutate)


def clear_cooling(account_id: object) -> dict:
    def mutate(raw: dict) -> None:
        for key in ('coolingUntil', 'coolingReason', 'coolingAt'):
            raw.pop(key, None)

    return _update(account_id, mutate)


def _update(account_id: object, mutate) -> dict:
    path = account_path(account_id)
    with file_lock(path):
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            raise
        except (OSError, ValueError) as exc:
            raise ValueError('账号文件格式异常') from exc
        if not isinstance(raw, dict):
            raise ValueError('账号文件格式异常')
        mutate(raw)
        raw['updatedAt'] = int(time.time())
        _atomic_write(path, raw)
    return public_account(raw)


# ── 脱敏 ────────────────────────────────────────────────────
def mask_cookie(cookie: object) -> str:
    text = str(cookie or '')
    if not text:
        return ''
    tail = text[-6:] if len(text) > 6 else ''
    return f'…{tail}' if tail else '…'


def public_account(account: dict) -> dict:
    uid = str(account.get('uid') or '')
    now = time.time()
    cool_until = cooling_until(account, now)
    cool_remaining = max(0, int(cool_until - now)) if cool_until else 0
    cookie = str(account.get('cookie') or '')
    summary = cookie_summary(cookie)
    checkin = account.get('checkin') if isinstance(account.get('checkin'), dict) else {}
    balance = account.get('balance') if isinstance(account.get('balance'), dict) else {}
    source = str(account.get('source') or '')
    if source not in ('manual', 'desktop', 'web'):
        source = 'manual'
    return {
        'platform': 'lingxi',
        'uid': uid,
        'name': str(account.get('name') or ''),
        'nickname': str(account.get('nickname') or ''),
        'email': str(account.get('email') or ''),
        'realm': REALM,
        'realm_label': '国内版',
        'proxy': str(account.get('proxy') or ''),
        'weight': weight_of(account),
        'disabled': is_disabled(account),
        'disabled_reason': str(account.get('disabledReason') or ''),
        'disabled_at': int(account.get('disabledAt') or 0),
        'checkin_only': is_checkin_only(account),
        'cooling': cool_until > 0,
        'cooling_until': int(cool_until) if cool_until else 0,
        'cool_remaining_sec': cool_remaining,
        'cooling_reason': str(account.get('coolingReason') or '') if cool_until else '',
        'expiredAt': str(account.get('expiredAt') or ''),
        'refreshExpiredAt': '',
        'has_token': bool(cookie),
        'has_refresh_token': False,
        'token_preview': mask_cookie(cookie),
        'source': source,
        'createdAt': int(account.get('createdAt') or 0),
        'updatedAt': int(account.get('updatedAt') or 0),
        'refreshedAt': str(account.get('refreshedAt') or ''),
        'lastRefreshError': str(account.get('lastRefreshError') or ''),
        'fingerprint': {
            'device_id': str(account.get('deviceId') or ''),
            'region': summary['region'],
        },
        'cookie_len': summary['cookie_len'],
        'cookie_summary': summary,
        'checkin': {
            'last_at': int(checkin.get('last_at') or 0),
            'last_result': str(checkin.get('last_result') or ''),
            'continuous_days': checkin.get('continuous_days'),
            'reward': checkin.get('reward'),
        },
        'balance': {
            'state': str(balance.get('state') or ''),
            'at': int(balance.get('at') or 0),
            'message': str(balance.get('message') or ''),
            'remaining': balance.get('remaining'),
            'total_value': balance.get('total_value'),
        },
        'login_state': str(account.get('loginState') or ''),
    }