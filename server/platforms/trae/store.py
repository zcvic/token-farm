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

# 账号权重与 WorkBuddy 的 `auth.weight` **同一口径**（issue #187）：
# 0.1–100，0 / 缺省 = 默认 1.0。两处界面上的「权重」含义必须一致，否则用户
# 在 WorkBuddy 账号上学会的直觉，到了平台账号就不成立了。
WEIGHT_MIN = 0.1
WEIGHT_MAX = 100.0


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


def weight_of(account: dict) -> float:
    """读权重：非法 / 缺省 / <=0 一律按 0 返回（0 = 未设置，等价 1.0）。

    不做钳制：列表要回显文件里的真实值，钳制只发生在选号时（见适配器）。
    与 `wb2api._weight_of` 同一口径。
    """
    try:
        value = float(account.get('weight') or 0)
    except (TypeError, ValueError):
        return 0.0
    if value != value or value in (float('inf'), float('-inf')) or value < 0:
        return 0.0
    return value


def cooling_until(account: dict, now: float | None = None) -> float:
    """冷却截止时刻（epoch 秒）。缺省 / 非法 / 已过期一律返回 0。

    **惰性过期**：到期不写文件，只在这里按「截止时刻是否在未来」判定。
    读取账号列表很频繁，为了清一个过期字段而写盘既没必要，也会让每个账号
    文件在无操作时被改动。
    """
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
    """本面板的临时冻结（停用）位：只影响调度与批量任务，不动凭据。"""
    return bool(account.get('disabled'))


def _update(account_id: object, mutate) -> dict:
    """在文件锁内读出 → 修改 → 原子写回，返回脱敏后的账号。"""
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


# ── 面板设备身份（国际版）────────────────────────────────────
def device_identity_path() -> Path:
    """`data/platforms/trae/device.json`：面板自己生成的设备身份。"""
    folder = config.DATA_DIR / 'platforms' / 'trae'
    folder.mkdir(parents=True, exist_ok=True)
    if os.name != 'nt':
        try:
            os.chmod(folder, 0o700)
        except OSError:
            pass
    return folder / 'device.json'


def read_device_identity() -> dict:
    """读面板设备身份；不存在或不完整返回 `{}`。

    文件里是 P-256 私钥，与账号文件同级敏感：只允许登录 / 刷新路径内部
    使用，不进入任何接口响应，也不打日志。
    """
    path = device_identity_path()
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    pem = str(raw.get('deviceKeyPEM') or raw.get('privateKeyPEM') or '').strip()
    device_id = str(raw.get('deviceId') or '').strip()
    machine_id = str(raw.get('machineId') or '').strip()
    if not pem or not device_id or not machine_id:
        return {}
    return {
        'deviceId': device_id,
        'machineId': machine_id,
        'deviceKeyPEM': pem,
        'devicePublicKeyPEM': str(raw.get('devicePublicKeyPEM')
                                 or raw.get('publicKeyPEM') or ''),
        'createdAt': int(raw.get('createdAt') or 0),
    }


def save_device_identity(identity: dict) -> dict:
    """原子落盘面板设备身份；返回归一化后的结构。"""
    if not isinstance(identity, dict):
        raise ValueError('设备身份必须是对象')
    pem = str(identity.get('deviceKeyPEM')
              or identity.get('privateKeyPEM') or '').strip()
    device_id = str(identity.get('deviceId') or '').strip()
    machine_id = str(identity.get('machineId') or '').strip()
    if not pem or not device_id or not machine_id:
        raise ValueError('设备身份缺少私钥 / 设备号 / 机器号')
    payload = {
        'realm': 'intl',
        'deviceId': device_id,
        'machineId': machine_id,
        'deviceKeyPEM': pem,
        'devicePublicKeyPEM': str(identity.get('devicePublicKeyPEM')
                                  or identity.get('publicKeyPEM') or ''),
        'createdAt': int(identity.get('createdAt') or time.time()),
    }
    path = device_identity_path()
    with file_lock(path):
        _atomic_write(path, payload)
    return payload


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
    """只改权重，保留凭据与其他字段。

    `weight` 传 0 = **清除**（恢复默认 1.0），与 WorkBuddy 的处理刻意区分：
    默认值不该出现在每个账号文件里，否则手改过的文件会被悄悄加上一个键。
    非法值抛 `ValueError`（路由转 400）。
    """
    try:
        value = float(weight)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError('权重必须是数字') from exc
    if value != value or value in (float('inf'), float('-inf')):
        raise ValueError('权重必须是有限数字')
    if value != 0 and not (WEIGHT_MIN <= value <= WEIGHT_MAX):
        raise ValueError(f'权重需在 {WEIGHT_MIN:g} 与 {WEIGHT_MAX:g} 之间（0 = 恢复默认）')

    def mutate(raw: dict) -> None:
        if value == 0:
            raw.pop('weight', None)
        else:
            raw['weight'] = value

    return _update(account_id, mutate)


def set_disabled(account_id: object, disabled: bool, reason: str = '') -> dict:
    """临时冻结 / 恢复一个账号。

    `disabled=True` 只影响**调度与批量任务**：账号仍留在列表里，令牌照常
    可刷新，积分照常可查——排障时需要这些入口。`disabled=False` 会连同
    冻结原因一起清掉，避免界面继续显示上次的理由。
    """
    def mutate(raw: dict) -> None:
        if disabled:
            raw['disabled'] = True
            raw['disabledReason'] = str(reason or '').strip()[:200]
            raw['disabledAt'] = int(time.time())
        else:
            raw.pop('disabled', None)
            raw.pop('disabledReason', None)
            raw.pop('disabledAt', None)

    try:
        return _update(account_id, mutate)
    except FileNotFoundError as exc:
        raise FileNotFoundError('账号不存在') from exc


def set_cooling(account_id: object, until: float, reason: str = '') -> dict:
    """强制冷却到某个时刻（epoch 秒）。`until` 必须在未来。"""
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
    """立即解除冷却（含永久冷却）。"""
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
    """对外展示形态：**不含** token / refreshToken，只留过期与指纹摘要。

    管理端接口一律走这里，避免哪天不小心把整份账号 JSON 直接返回给前端。
    """
    from . import fingerprint

    uid = str(account.get('uid') or '')
    realm = str(account.get('realm') or 'cn')
    expired_at = str(account.get('expiredAt') or '')
    now = time.time()
    cool_until = cooling_until(account, now)
    cool_remaining = max(0, int(cool_until - now)) if cool_until else 0
    disabled = is_disabled(account)
    # 来源：三类写入路径各自显式标注，老文件没有该字段时按 desktopPath 推断。
    # `web` = 网页登录（浏览器授权后兑换落盘）。
    source = str(account.get('source') or '')
    if source not in ('desktop', 'manual', 'web'):
        source = 'desktop' if account.get('desktopPath') else 'manual'
    return {
        'platform': 'trae',
        'uid': uid,
        'nickname': str(account.get('nickname') or ''),
        'email': str(account.get('email') or ''),
        'realm': realm,
        'realm_label': _REALM_LABEL.get(realm, realm),
        'proxy': str(account.get('proxy') or ''),
        # 手写权重（0 = 未设置，界面据此显示「默认」）。与 WorkBuddy 的
        # `auth.weight` 同一口径，但存在本端账号文件里——平台账号的选号在
        # 适配器内部完成，不经过上游池。
        'weight': weight_of(account),
        # 本面板的调度开关（与 WorkBuddy 的「停用 / 冷却」同一套用户语义，
        # 但存在平台自己的账号文件里）。`disabled` 让账号退出选号与批量任务；
        # `cooling` 只暂停选号，到期自动恢复。
        'disabled': disabled,
        'disabled_reason': str(account.get('disabledReason') or ''),
        'disabled_at': int(account.get('disabledAt') or 0),
        'cooling': cool_until > 0,
        'cooling_until': int(cool_until) if cool_until else 0,
        'cool_remaining_sec': cool_remaining,
        'cooling_reason': str(account.get('coolingReason') or '') if cool_until else '',
        'expiredAt': expired_at,
        'refreshExpiredAt': str(account.get('refreshExpiredAt') or ''),
        'has_token': bool(account.get('token')),
        'has_refresh_token': bool(account.get('refreshToken')),
        # 设备私钥只报「有没有」，不给任何形状（长度/前缀都会成为指纹）。
        'has_device_key': bool(account.get('deviceKeyPEM')),
        'token_preview': mask_token(account.get('token')),
        # 来源：三类写入路径各自显式标注，老文件没有该字段时按 desktopPath 推断。
        # `web` = 网页登录（浏览器授权后兑换落盘）。
        'source': source,
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
