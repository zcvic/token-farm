"""小浣熊账号文件存储。

位置：`<DATA_DIR>/platforms/raccoon/accounts/<uid>.json`

凭证是一对 JWT：

  · `access_token`（约 3 小时）放在 `token` 字段，所有请求用
    `Authorization: Bearer <token>`；
  · `refresh_token`（约 30 天）放在 `refreshToken` 字段，刷新时**单次使用**，
    上游会同时换发新的 access 与 refresh。刷新成功必须先原子落盘，再拿去
    发请求——否则进程崩在中间就会把账号永久踢下线。

外部只通过 `public_account` 拿脱敏摘要；明文只在本机账号文件里。

写入与其它平台一致：文件锁 + 临时文件 + `os.replace` 原子替换。
"""
from __future__ import annotations

import base64
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

# uid 会成为文件名，必须限制成最小安全字符集。
_SAFE_ID = re.compile(r'^[0-9A-Za-z_-]{1,128}$')

# 与 WorkBuddy / Trae / Loomy 的账号权重同一口径。
WEIGHT_MIN = 0.1
WEIGHT_MAX = 100.0

# 单区域平台：上游没有国内 / 国际分站。
REALM = 'cn'

# JWT 里可能出现的用户 id 字段（不同版本 / 不同登录来源写法不一）。
_UID_KEYS = ('user_id', 'userId', 'uid', 'userID', 'sub')


def accounts_dir() -> Path:
    path = config.DATA_DIR / 'platforms' / 'raccoon' / 'accounts'
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


# ── token 清洗 ──────────────────────────────────────────────
def normalize_token(raw: object) -> str:
    """清洗用户粘贴的 access token / Cookie 片段。

    支持：裸 JWT、`Bearer xxx`、`authorization: Bearer xxx`、整段 Cookie 里的
    `xhx_token=...` / `access_token=...`。
    """
    text = str(raw or '').strip()
    text = re.sub(r'^authorization:\s*', '', text, flags=re.I)
    text = re.sub(r'^bearer\s+', '', text, flags=re.I)
    # 整段 Cookie：按分号拆开找登录态字段。
    if ';' in text or '=' in text:
        for part in text.split(';'):
            key, _, value = part.strip().partition('=')
            if key.strip().lower() in ('xhx_token', 'access_token', 'token'):
                text = value.strip()
                break
    return text.strip().strip('"').strip("'").strip()


def normalize_refresh(raw: object) -> str:
    return normalize_token(raw)


# ── JWT（只读 payload，不验签）──────────────────────────────
def jwt_payload(token: object) -> dict:
    text = normalize_token(token)
    parts = text.split('.')
    if len(parts) < 2:
        return {}
    body = parts[1]
    body += '=' * (-len(body) % 4)
    try:
        raw = base64.urlsafe_b64decode(body.encode('ascii'))
        payload = json.loads(raw.decode('utf-8'))
    except (ValueError, TypeError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def jwt_exp(token: object) -> float:
    try:
        return float(jwt_payload(token).get('exp') or 0)
    except (TypeError, ValueError):
        return 0.0


def jwt_uid(token: object) -> str:
    payload = jwt_payload(token)
    for key in _UID_KEYS:
        value = payload.get(key)
        if value not in (None, ''):
            return str(value).strip()
    return ''


def jwt_name(token: object) -> str:
    payload = jwt_payload(token)
    for key in ('name', 'nickname', 'display_name'):
        value = payload.get(key)
        if value not in (None, ''):
            return str(value).strip()
    return ''


def derive_uid(access: object = '', refresh: object = '',
               explicit: object = '') -> str:
    """派生稳定 uid：优先 JWT 里的用户 id，其次凭证哈希。

    不能用 refresh token 的哈希：refresh 每次轮换都会变，重新导入同一个人会
    变成两条账号。JWT 的 user id 在同一账号内稳定，拿不到才退回哈希。
    """
    raw = str(explicit or '').strip()
    if raw:
        return safe_id(raw)
    user = jwt_uid(access) or jwt_uid(refresh)
    seed = f'user:{user}' if user else f'token:{normalize_refresh(refresh) or normalize_token(access)}'
    if seed.endswith(':'):
        raise ValueError('缺少 access token 或 refresh token')
    return 'rc_' + hashlib.sha256(seed.encode('utf-8')).hexdigest()[:24]


def device_id_for(account: dict) -> str:
    """稳定派生客户端设备号（≤64 字符）。

    上游部分桌面接口会校验 `X-Client-Device-ID`。同一账号必须稳定，否则每次
    请求都像一台新设备，容易触发设备风控。
    """
    seed = str(account.get('uid') or account.get('token')
               or account.get('refreshToken') or 'raccoon')
    return 'rc-' + hashlib.sha256(seed.encode('utf-8')).hexdigest()[:48]


def mask_token(value: object) -> str:
    """只露尾部，绝不把 token 明文回给浏览器。"""
    text = str(value or '')
    if not text:
        return ''
    tail = text[-6:] if len(text) > 8 else ''
    return f'…{tail}' if tail else '…'


# ── 时间 ────────────────────────────────────────────────────
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
        number = float(text)
    except ValueError:
        number = 0.0
    if number > 0:
        return number / 1000.0 if number > 1e11 else number
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


# ── 调度字段 ────────────────────────────────────────────────
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
    """全部账号（**含**明文 token，仅限内部使用）。坏文件跳过。"""
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
    fd, tmp = tempfile.mkstemp(prefix='.raccoon-', suffix='.json.tmp',
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
    """只改给定字段（token 轮换、探活结果等）。"""
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
    """改写账号绑定的出口线路（空串 = 跟随号池，`__direct__` = 强制直连）。"""
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
    """开启 / 关闭「仅签到」：只摘推理流量，签到与探活照常。"""
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
def public_account(account: dict) -> dict:
    uid = str(account.get('uid') or '')
    now = time.time()
    cool_until = cooling_until(account, now)
    cool_remaining = max(0, int(cool_until - now)) if cool_until else 0
    access = str(account.get('token') or account.get('accessToken') or '')
    refresh = str(account.get('refreshToken') or '')
    source = str(account.get('source') or '')
    if source not in ('manual', 'desktop', 'web'):
        source = 'manual'
    checkin = account.get('checkin') if isinstance(account.get('checkin'), dict) else {}
    balance = account.get('balance') if isinstance(account.get('balance'), dict) else {}
    return {
        'platform': 'raccoon',
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
        'refreshExpiredAt': str(account.get('refreshExpiredAt') or ''),
        'has_token': bool(access or refresh),
        'has_refresh_token': bool(refresh),
        'token_preview': mask_token(access or refresh),
        # access token 由适配器按需轮换，到期不代表账号不可用。
        'token_auto_refresh': True,
        'source': source,
        'createdAt': int(account.get('createdAt') or 0),
        'updatedAt': int(account.get('updatedAt') or 0),
        'refreshedAt': str(account.get('refreshedAt') or ''),
        'lastRefreshError': str(account.get('lastRefreshError') or ''),
        'fingerprint': {'device_id': device_id_for(account)},
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
        },
        'login_state': str(account.get('loginState') or ''),
    }
