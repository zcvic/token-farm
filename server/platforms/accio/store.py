"""Accio 账号文件存储。

位置：`<DATA_DIR>/platforms/accio/accounts/<uid>.json`

## 必须持久化的东西

  · `accessToken` / `refreshToken` / `expiredAt` —— OAuth 凭据。access token 是
    不透明 32 位串（**不是 JWT**），过期时刻只能来自上游 `expiresAt`；上游给的
    是**秒**，本模块统一按秒落 `expiredAt`（ISO 8601），读取时再归一成 epoch 秒。
  · `realm`（`cn` / `global`）—— 登录页域名不同，但网关与 token 通用。区域由
    服务端按账号下发，登录前无法自动判断，所以必须记住用户选的那一个。
  · `utdid` —— 设备指纹。实测只在请求体 `properties.utdid` 里被校验，且上游
    并不核对取值，所以本地生成一份**稳定**的即可；每次请求现造会被当成换设备。
  · `sgcookie` —— 兑换响应偶尔下发，缺了不影响请求，顺手保存。
  · `routeRegion` / `teamId` —— 来自 `userinfo`，分别写进
    `x-accio-route-region` / `x-team-id`。空值不落盘、不发头。

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

# 与其它平台同一口径的账号权重区间。
WEIGHT_MIN = 0.1
WEIGHT_MAX = 100.0

# 账号 id 前缀：`ao_` = Accio OAuth。
UID_PREFIX = 'ao_'

_DEFAULT_UTDID_PREFIX = 'accio-'


def normalize_realm(value: object) -> str:
    return P.normalize_realm(value)


def realm_label(realm: object) -> str:
    return C.REALM_LABELS.get(normalize_realm(realm), '')


def accounts_dir() -> Path:
    path = config.DATA_DIR / 'platforms' / 'accio' / 'accounts'
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


# ── 凭据清洗 ────────────────────────────────────────────────
_COOKIE_KEYS = ('ac_token', 'access_token', 'accio_token', 'token')


def normalize_token(raw: object) -> str:
    """清洗用户粘贴的 token / Cookie 片段（`Bearer …`、整段 Cookie 都认）。"""
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


def derive_uid(realm: object, identity: object = '',
               access: object = '', refresh: object = '') -> str:
    """派生稳定 uid。

    优先用调用方给的稳定身份（`userId` / 用户名 / 邮箱），其次凭证哈希。
    access token 在续期后会变，**不能**只靠它：登录流程会带 `userId`，手工
    导入才退回凭证哈希（导入后 uid 已落盘，后续续期不会再变）。
    """
    region = normalize_realm(realm)
    explicit = str(identity or '').strip()
    seed = f'{region}:user:{explicit}' if explicit else ''
    if not seed:
        credential = normalize_refresh(refresh) or normalize_token(access)
        if not credential:
            raise ValueError('缺少 access token 或 refresh token')
        seed = f'{region}:token:{credential}'
    return UID_PREFIX + hashlib.sha256(seed.encode('utf-8')).hexdigest()[:24]


# ── utdid ───────────────────────────────────────────────────
def _base36(value: int) -> str:
    digits = '0123456789abcdefghijklmnopqrstuvwxyz'
    value = max(0, int(value))
    if value == 0:
        return '0'
    out: list[str] = []
    while value:
        value, rem = divmod(value, 36)
        out.append(digits[rem])
    return ''.join(reversed(out))


def new_utdid() -> str:
    """生成本地设备号：`accio-<8位36进制><时间36进制>`。

    实测上游不核对具体取值，只要**同一个账号稳定**即可。时间后缀让不同账号
    之间也不会撞车。
    """
    return (f'{_DEFAULT_UTDID_PREFIX}{_base36(secrets.randbits(32)):0>8}'
            f'{_base36(int(time.time() * 1000))}')


def utdid_for(account: dict) -> str:
    """账号的设备号：记录值 → 稳定派生值（绝不每次现造）。"""
    for key in ('utdid', 'utdidValue'):
        value = str(account.get(key) or '').strip()
        if value:
            return value
    seed = str(account.get('uid') or account.get('accessToken')
               or account.get('refreshToken') or 'accio')
    return f'{_DEFAULT_UTDID_PREFIX}{hashlib.sha256(seed.encode()).hexdigest()[:24]}'


def mask_token(value: object) -> str:
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
    """ISO / epoch（秒或毫秒）→ epoch 秒；读不到返回 0。"""
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


def expires_iso(raw: object) -> str:
    """上游 `expiresAt`（秒 / 毫秒）→ ISO 秒级时刻。"""
    ms = P.expires_at_ms(raw)
    return iso_from_epoch(ms / 1000.0) if ms is not None else ''


def expires_epoch(raw: object) -> float:
    """上游 `expiresAt`（秒 / 毫秒）→ epoch 秒；读不到 0。"""
    ms = P.expires_at_ms(raw)
    return ms / 1000.0 if ms is not None else 0.0


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
    fd, tmp = tempfile.mkstemp(prefix='.accio-', suffix='.json.tmp',
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
        merged['utdid'] = utdid_for(merged)
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
        raw['utdid'] = utdid_for(raw)
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
        else:
            raw.pop('disabled', None)
            raw.pop('disabledReason', None)
            raw.pop('disabledAt', None)

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
        raw['utdid'] = utdid_for(raw)
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
    access = str(account.get('accessToken') or '')
    refresh = str(account.get('refreshToken') or '')
    source = str(account.get('source') or '')
    if source not in ('manual', 'desktop', 'web', 'oauth'):
        source = 'manual'
    return {
        'platform': 'accio',
        'uid': uid,
        'name': str(account.get('name') or ''),
        'nickname': str(account.get('nickname') or ''),
        'user_name': str(account.get('userName') or ''),
        'email': str(account.get('email') or ''),
        'phone': str(account.get('phone') or ''),
        'realm': realm,
        'realm_label': realm_label(realm),
        'proxy': str(account.get('proxy') or ''),
        'weight': weight_of(account),
        'disabled': is_disabled(account),
        'disabled_reason': str(account.get('disabledReason') or ''),
        'disabled_at': int(account.get('disabledAt') or 0),
        'cooling': cool_until > 0,
        'cooling_until': int(cool_until) if cool_until else 0,
        'cool_remaining_sec': cool_remaining,
        'cooling_reason': str(account.get('coolingReason') or '') if cool_until else '',
        'expiredAt': str(account.get('expiredAt') or ''),
        'has_token': bool(access),
        'has_refresh_token': bool(refresh),
        'token_preview': mask_token(access or refresh),
        'token_auto_refresh': True,
        'team_id': str(account.get('teamId') or ''),
        'team_name': str(account.get('teamName') or ''),
        'route_region': str(account.get('routeRegion') or ''),
        'source': source,
        'createdAt': int(account.get('createdAt') or 0),
        'updatedAt': int(account.get('updatedAt') or 0),
        'refreshedAt': str(account.get('refreshedAt') or ''),
        'lastRefreshError': str(account.get('lastRefreshError') or ''),
        'fingerprint': {'utdid': utdid_for(account)},
        'login_state': str(account.get('loginState') or ''),
    }
