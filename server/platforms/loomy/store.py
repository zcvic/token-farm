"""Loomy 账号文件存储。

位置：`<DATA_DIR>/platforms/loomy/accounts/<uid>.json`

与其它平台一样，写入是**原子替换 + 文件锁**。账号文件里含明文密码
（用于会话过期后重新登录）与会话令牌，属于高敏文件：POSIX 上是 0600，
Windows 由 ACL 决定；管理端接口一律走 `public_account` 脱敏。

Loomy **没有 refresh token**：会话过期后只能重新登录。因此「账号有没有
密码」直接决定它能不能被刷新——只有 session 的账号只能重新导入。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from ... import config
from ...services.filelock import file_lock

# uid 会成为文件名，必须限制成最小安全字符集。
_SAFE_ID = re.compile(r'^[0-9A-Za-z_-]{1,128}$')

# 与 WorkBuddy / Trae 的账号权重同一口径（issue #187）。
WEIGHT_MIN = 0.1
WEIGHT_MAX = 100.0


def accounts_dir() -> Path:
    path = config.DATA_DIR / 'platforms' / 'loomy' / 'accounts'
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


def derive_uid(userid: object = '', phone: object = '',
               session: object = '') -> str:
    """账号 id：优先 userid，其次手机号，最后会话摘要。

    上游 userid 是纯数字；手机号也是数字。两者都没有时（只粘了一个
    session）用会话的 SHA-256 前缀，保证同一 session 重复导入会覆盖而不是
    产生第二份账号。
    """
    for value in (userid, phone):
        text = str(value or '').strip()
        if not text:
            continue
        cleaned = re.sub(r'[^0-9A-Za-z_-]', '_', text)[:128]
        if cleaned:
            return cleaned
    seed = str(session or '').strip().encode('utf-8')
    if not seed:
        raise ValueError('缺少 session / phone / userid，无法生成账号 id')
    return 's_' + hashlib.sha256(seed).hexdigest()[:24]


# ── 时间 ────────────────────────────────────────────────────
def iso_from_epoch(value: object) -> str:
    """epoch 秒 → ISO8601（UTC）；0 / 非法返回空串。"""
    try:
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ''
    if seconds <= 0:
        return ''
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(seconds))


# ── 调度字段 ────────────────────────────────────────────────
def weight_of(account: dict) -> float:
    """读权重：非法 / 缺省 / <=0 一律按 0（0 = 未设置，等价 1.0）。"""
    try:
        value = float(account.get('weight') or 0)
    except (TypeError, ValueError):
        return 0.0
    if value != value or value in (float('inf'), float('-inf')) or value < 0:
        return 0.0
    return value


def cooling_until(account: dict, now: float | None = None) -> float:
    """冷却截止时刻；缺省 / 非法 / 已过期一律 0（惰性过期）。"""
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
    """「仅签到」位：不参与对话（不消耗积分），签到与平台任务照常。"""
    return bool(account.get('checkinOnly'))


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


# ── 读 ───────────────────────────────────────────────────────
def list_accounts() -> list[dict]:
    """全部账号（**含**明文，仅限内部使用）。

    单个坏文件跳过而不是整体抛错：一个坏文件不该让账号页打不开。
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
    out.sort(key=lambda a: str(a.get('uid') or ''))
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
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(prefix='.loomy-', suffix='.json.tmp',
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


def delete_account(account_id: object) -> bool:
    path = account_path(account_id)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ValueError(f'删除账号失败: {exc}') from exc


def set_weight(account_id: object, weight: object) -> dict:
    """只改权重；传 0 = 清除（恢复默认 1.0）。"""
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

    try:
        return _update(account_id, mutate)
    except FileNotFoundError as exc:
        raise FileNotFoundError('账号不存在') from exc


def set_checkin_only(account_id: object, enabled: bool) -> dict:
    """开启 / 关闭「仅签到」：只摘推理流量，签到与平台任务照常。"""
    def mutate(raw: dict) -> None:
        if enabled:
            raw['checkinOnly'] = True
            raw.pop('disabled', None)
            raw.pop('disabledReason', None)
            raw.pop('disabledAt', None)
        else:
            raw.pop('checkinOnly', None)

    try:
        return _update(account_id, mutate)
    except FileNotFoundError as exc:
        raise FileNotFoundError('账号不存在') from exc


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

    try:
        return _update(account_id, mutate)
    except FileNotFoundError as exc:
        raise FileNotFoundError('账号不存在') from exc


def clear_cooling(account_id: object) -> dict:
    def mutate(raw: dict) -> None:
        for key in ('coolingUntil', 'coolingReason', 'coolingAt'):
            raw.pop(key, None)

    try:
        return _update(account_id, mutate)
    except FileNotFoundError as exc:
        raise FileNotFoundError('账号不存在') from exc


# ── 脱敏 ─────────────────────────────────────────────────────
def mask_token(token: object) -> str:
    text = str(token or '')
    if not text:
        return ''
    if len(text) <= 12:
        return '*' * len(text)
    return f'{text[:6]}…{text[-4:]}'


def public_account(account: dict) -> dict:
    """对外展示形态：**不含** session / password 明文。"""
    uid = str(account.get('uid') or '')
    now = time.time()
    cool_until = cooling_until(account, now)
    cool_remaining = max(0, int(cool_until - now)) if cool_until else 0
    obtained_at = int(account.get('obtainedAt') or 0)
    session_age = ((now - obtained_at) / 86400) if obtained_at else None
    expire_at = float(account.get('expireAt') or 0)
    stale = bool(expire_at and now >= expire_at)
    source = str(account.get('source') or '')
    if source not in ('desktop', 'manual', 'web'):
        source = 'manual'
    return {
        'platform': 'loomy',
        'uid': uid,
        'nickname': str(account.get('nickname') or ''),
        'email': str(account.get('email') or ''),
        'realm': 'cn',
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
        # Loomy 的到期时刻是**本地估计**（登录响应没有可靠字段），拿它判定
        # 「令牌失效」会造成假警报，所以对外一律留空；真实状态只看 has_token
        # 与 lastRefreshError。session_age_days / session_stale 仅作信息展示。
        'expiredAt': '',
        'refreshExpiredAt': '',
        'has_token': bool(account.get('session')),
        # Loomy 没有 refresh token；会话过期只能重新登录。
        'has_refresh_token': False,
        'token_preview': mask_token(account.get('session')),
        'source': source,
        'createdAt': int(account.get('createdAt') or 0),
        'updatedAt': int(account.get('updatedAt') or 0),
        'refreshedAt': iso_from_epoch(obtained_at),
        'lastRefreshError': str(account.get('lastRefreshError') or ''),
        'fingerprint': {},
        # 平台专属信息（前端会忽略未知字段，管理/排障用）。
        'phone': str(account.get('phone') or ''),
        'has_password': bool(account.get('password')),
        'session_age_days': round(session_age, 2) if session_age is not None else None,
        'session_stale': stale,
        'login_state': str(account.get('loginState') or ''),
        # 任务面板缓存（签到结果 / 自己名下的邀请码 / 激活状态）。
        # 只读展示用：真实状态由 `task_status` 联网刷新，这里是为了让列表
        # 打开时就能看到上次同步到的邀请码，不必先点一次「签到」。
        'task_cache': _task_cache_view(account.get('taskCache')),
    }


def _task_cache_view(raw: object) -> dict:
    """账号文件里的 `taskCache` → 对外形状（脏数据一律当空）。"""
    raw = raw if isinstance(raw, dict) else {}
    checkin = raw.get('checkin') if isinstance(raw.get('checkin'), dict) else None
    codes = raw.get('inviteCodes')
    return {
        'synced_at': int(raw.get('syncedAt') or 0),
        'checkin': checkin,
        'invite_codes': [c for c in codes if isinstance(c, dict)]
        if isinstance(codes, list) else [],
    }
