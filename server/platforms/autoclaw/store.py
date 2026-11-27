"""AutoClaw 账号文件存储。

位置：`<DATA_DIR>/platforms/autoclaw/accounts/<uid>.json`

凭证是「access + refresh」两个 token：

  · `accessToken` 寿命按上游签发（参考实现观测到的 JWT `exp` 是小时级）；
    所有请求都带 `Authorization` / `X-Authorization`；
  · `refreshToken` **一次一换**：刷新成功会同时下发新的 refresh，旧的立即作废。
    所以刷新必须先原子落盘再拿去发请求，否则进程崩在中间会把账号永久踢下线。

与其它平台不同，AutoClaw **必须持久化 `deviceId`**：登录时服务端签发一个设备号，
刷新 / 钱包 / 任务 / 签到都要求同一个 `device_id`；每次请求现造一个新设备号会被
当成换设备，轻则数据对不上，重则触发风控。JWT 里也带 `device_id` claim，可兜底。

国内 / 国际是两套独立账号体系，`realm` 写在账号记录里而不是从 token 猜：
`protocol.realm_hint()` 只能认出「国内短信号」这一种，认不出时按记录值走，
**不要**猜成另一区（那只会得到 401）。

写入与其它平台一致：文件锁 + 临时文件 + `os.replace` 原子替换。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ... import config
from ...services.filelock import file_lock
from . import constants as C, protocol as P

# uid 会成为文件名，必须限制成最小安全字符集。
_SAFE_ID = re.compile(r'^[0-9A-Za-z_-]{1,128}$')

# 与 WorkBuddy / Trae / Loomy 的账号权重同一口径。
WEIGHT_MIN = 0.1
WEIGHT_MAX = 100.0

# 各版本 JWT 里可能出现的用户 id 字段。
_UID_KEYS = ('user_id', 'userId', 'uid', 'userID', 'sub', 'id')

# 账号 id 前缀：`ac_` = AutoClaw。
UID_PREFIX = 'ac_'


def normalize_realm(value: object) -> str:
    """任意写法 → 协议口径（`cn` / `global`）。"""
    return P.normalize_realm(value)


def realm_label(realm: object) -> str:
    return C.REALM_LABELS.get(normalize_realm(realm), '')


def accounts_dir() -> Path:
    path = config.DATA_DIR / 'platforms' / 'autoclaw' / 'accounts'
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
_COOKIE_KEYS = ('ac_token', 'autoclaw_token', 'access_token', 'token')


def normalize_token(raw: object) -> str:
    """清洗用户粘贴的 token / Cookie 片段。

    支持：裸 JWT、`Bearer xxx`、`authorization: Bearer xxx`、整段 Cookie 里的
    `ac_token=...` / `access_token=...`。
    """
    text = str(raw or '').strip()
    text = re.sub(r'^authorization:\s*', '', text, flags=re.I)
    text = re.sub(r'^bearer\s+', '', text, flags=re.I)
    if ';' in text or '=' in text:
        for part in text.split(';'):
            key, _, value = part.strip().partition('=')
            if key.strip().lower() in _COOKIE_KEYS:
                text = value.strip()
                break
    return text.strip().strip('"').strip("'").strip()


def normalize_refresh(raw: object) -> str:
    return normalize_token(raw)


# ── JWT（只读 payload，不验签）──────────────────────────────
def jwt_exp(token: object) -> float:
    return float(P.token_expiry(token) or 0)


def jwt_device_id(token: object) -> str:
    claims = P.jwt_claims(token)
    for key in ('device_id', 'deviceId'):
        value = claims.get(key)
        if value not in (None, ''):
            return str(value).strip()
    return ''


def jwt_uid(token: object) -> str:
    claims = P.jwt_claims(token)
    for key in _UID_KEYS:
        value = claims.get(key)
        if value not in (None, ''):
            return str(value).strip()
    return ''


def derive_uid(realm: object, identity: object = '',
               access: object = '', refresh: object = '') -> str:
    """派生稳定 uid：`区域 + 用户标识`。

    优先用调用方给的稳定身份（手机号 / 用户 id / 邮箱），其次 JWT 里的 user id，
    最后才退回凭证哈希。**不能用 refresh token 的哈希**：它一次一换，重新登录
    同一个人会变成两条账号。

    区域进 seed 是必须的：同一个手机号在国内 / 国际是两套账号，uid 撞车会让
    一次登录覆盖掉另一区的记录。
    """
    region = normalize_realm(realm)
    explicit = str(identity or '').strip()
    user = explicit or jwt_uid(access) or jwt_uid(refresh)
    seed = f'{region}:user:{user}' if user else ''
    if not seed:
        credential = normalize_refresh(refresh) or normalize_token(access)
        if not credential:
            raise ValueError('缺少 access token 或 refresh token')
        seed = f'{region}:token:{credential}'
    return UID_PREFIX + hashlib.sha256(seed.encode('utf-8')).hexdigest()[:24]


def new_device_id() -> str:
    """登录前现造一个设备号（上游要求 `device_id` 是客户端生成的稳定串）。"""
    return secrets.token_hex(16)


def device_id_for(account: dict) -> str:
    """账号的设备号：记录值 → JWT claim → 稳定派生值。

    最后一步的派生是为了兼容「手工粘 token」导入：没有记录值也不能每次请求
    现造，否则同一账号每轮都像换了台机器。
    """
    for key in ('deviceId', 'device_id'):
        value = str(account.get(key) or '').strip()
        if value:
            return value
    for key in ('accessToken', 'token', 'refreshToken'):
        value = jwt_device_id(account.get(key))
        if value:
            return value
    seed = str(account.get('uid') or account.get('refreshToken')
               or account.get('accessToken') or 'autoclaw')
    return 'ac-' + hashlib.sha256(seed.encode('utf-8')).hexdigest()[:32]


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
    fd, tmp = tempfile.mkstemp(prefix='.autoclaw-', suffix='.json.tmp',
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
        merged['realm'] = normalize_realm(
            account.get('realm') or old.get('realm'))
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


# ── 调度字段写入 ────────────────────────────────────────────
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
    """开启 / 关闭「仅签到」：只摘推理流量，签到与平台任务照常。"""
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
    realm = normalize_realm(account.get('realm'))
    now = time.time()
    cool_until = cooling_until(account, now)
    cool_remaining = max(0, int(cool_until - now)) if cool_until else 0
    access = str(account.get('accessToken') or account.get('token') or '')
    refresh = str(account.get('refreshToken') or '')
    checkin = account.get('checkin') if isinstance(account.get('checkin'), dict) else {}
    balance = account.get('balance') if isinstance(account.get('balance'), dict) else {}
    source = str(account.get('source') or '')
    if source not in ('manual', 'desktop', 'web', 'sms', 'oauth'):
        source = 'manual'
    return {
        'platform': 'autoclaw',
        'uid': uid,
        'name': str(account.get('name') or ''),
        'nickname': str(account.get('nickname') or ''),
        'email': str(account.get('email') or ''),
        'phone': str(account.get('phone') or ''),
        'realm': realm,
        'realm_label': realm_label(realm),
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
        # access token 由适配器按需轮换，到期不代表账号不可用（真正会死的是
        # refresh token）。前端据此不把「access 过期」标成需处理。
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
