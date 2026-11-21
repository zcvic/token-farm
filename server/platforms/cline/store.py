"""Cline 账号文件存储。

位置：`<DATA_DIR>/platforms/cline/accounts/<uid>.json`

凭证是 WorkOS 设备授权码换来的 **refresh token**，它有两个致命性质：

  · **一次性轮换**：刷新时上游会同时下发新的 refresh token，旧的立即作废。
    所以刷新成功必须**先落盘**再拿去发请求（失败重试时不能拿旧 token 再换）。
  · access token 只有约 10 分钟，且上游不一定回到期时间；适配器在每次使用前
    按余量自己换，所以 `expiredAt` 过期不代表账号不可用（`token_auto_refresh`）。

两份 token 都属高敏，对外只通过 `public_account` 给脱敏摘要。

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

# uid 会成为文件名，必须限制成最小安全字符集。
_SAFE_ID = re.compile(r'^[0-9A-Za-z_-]{1,128}$')

# 与 WorkBuddy / Trae / Loomy 的账号权重同一口径。
WEIGHT_MIN = 0.1
WEIGHT_MAX = 100.0

# 单区域平台：上游没有国内 / 国际分站。
REALM = 'cn'


def accounts_dir() -> Path:
    path = config.DATA_DIR / 'platforms' / 'cline' / 'accounts'
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


# ── token ──────────────────────────────────────────────────
def normalize_refresh(raw: object) -> str:
    """清洗用户粘贴的 refresh token：去掉 `Bearer ` 前缀、引号与空白。"""
    text = str(raw or '').strip()
    text = re.sub(r'^authorization:\s*', '', text, flags=re.I)
    text = re.sub(r'^bearer\s+', '', text, flags=re.I)
    return text.strip().strip('"').strip("'").strip()


def derive_uid(refresh: object) -> str:
    """由 refresh token 派生稳定 uid：同一份凭证重复导入覆盖同一条账号。"""
    key = normalize_refresh(refresh)
    if not key:
        raise ValueError('缺少 refresh token')
    return 'cl_' + hashlib.sha256(key.encode('utf-8')).hexdigest()[:24]


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
    # 上游 `expiresAt` 可能给 epoch 毫秒（数字）也可能给 ISO 串。
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
    """「仅签到」位：Cline 没有签到，但保留同一个位用于「只探活不用」。"""
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
    fd, tmp = tempfile.mkstemp(prefix='.cline-', suffix='.json.tmp',
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
            # 完全停用压过「仅签到」：两个位同时为真会让界面说不清停到哪一步。
            raw.pop('checkinOnly', None)
        else:
            raw.pop('disabled', None)
            raw.pop('disabledReason', None)
            raw.pop('disabledAt', None)

    return _update(account_id, mutate)


def set_checkin_only(account_id: object, enabled: bool) -> dict:
    """开启 / 关闭「仅签到」：只摘推理流量，探活照常。"""
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
    return {
        'platform': 'cline',
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
        # access token 只有约 10 分钟，且**由适配器按需刷新**（`_token_for` 在
        # 使用前发现临近过期就换一份）。所以 `expiredAt` 过期并不代表账号不可用，
        # 界面不能把它当成「需处理」——否则闲置十分钟的号全被标红。
        'token_auto_refresh': True,
        'source': source,
        'createdAt': int(account.get('createdAt') or 0),
        'updatedAt': int(account.get('updatedAt') or 0),
        'refreshedAt': str(account.get('refreshedAt') or ''),
        'lastRefreshError': str(account.get('lastRefreshError') or ''),
        'fingerprint': {},
        'login_state': str(account.get('loginState') or ''),
    }
