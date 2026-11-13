"""平台运行参数：请求超时、换号次数与平台专属开关。

## 和另外两个「平台设置」模块的分工

  · `limiter.py`  —— 池级**推理并发**（同时有多少个上游请求在跑）；
  · `checkinpacing.py` —— 批量签到的并发与账号间隔（一次性任务的节奏）；
  · 本模块 —— 单次请求的**时间预算**与适配器自己的选号 / 兼容开关。

三者都按平台分开存（`platforms.<name>.<field>`），但口径互不相干。放在同一个
`/api/platforms/{name}/settings` 里返回，是因为界面把它们放在同一张「平台配置」
表单上；后端仍各自校验、各自落库，改一个不会把另一个写回默认值。

## 为什么不缓存

`db.get_setting` 是一次本地 SQLite 主键查询；一次对话最多读 6 个值，量级远小于
上游网络往返。之前给 `limiter.py` 加缓存是因为它每次请求都要读、而且改了要
立刻生效；这里字段更多、改动更少，直接读库反而不会出现「保存了但界面 / 请求
还看到旧值」的问题。

## 取值坏了怎么办

读路径一律回默认值，不抛异常：一条坏设置不该让整个平台的对话不可用。写路径
（`save`）严格校验并抛 `ValueError`，由路由转成 400。
"""
from __future__ import annotations

import logging
from typing import Any

from .. import db

logger = logging.getLogger('workbuddy.platforms.settings')

DEFAULT_REQUEST_TIMEOUT = 300
DEFAULT_IDLE_TIMEOUT = 60
DEFAULT_MAX_ACCOUNTS_TRY = 3
DEFAULT_SESSION_AFFINITY = True
DEFAULT_SESSION_AFFINITY_TTL = 30 * 60
DEFAULT_DESKTOP_SYNC = True

REQUEST_TIMEOUT_MIN, REQUEST_TIMEOUT_MAX = 10, 900
IDLE_TIMEOUT_MIN, IDLE_TIMEOUT_MAX = 10, 600
MAX_ACCOUNTS_TRY_MIN, MAX_ACCOUNTS_TRY_MAX = 1, 10
SESSION_AFFINITY_TTL_MIN, SESSION_AFFINITY_TTL_MAX = 60, 12 * 3600

# name -> (默认值, 下限, 上限, 界面文案用的中文名)
_INT_FIELDS: dict[str, tuple[int, int, int, str]] = {
    'request_timeout': (
        DEFAULT_REQUEST_TIMEOUT, REQUEST_TIMEOUT_MIN, REQUEST_TIMEOUT_MAX, '请求超时'),
    'idle_timeout': (
        DEFAULT_IDLE_TIMEOUT, IDLE_TIMEOUT_MIN, IDLE_TIMEOUT_MAX, '流空闲超时'),
    'max_accounts_try': (
        DEFAULT_MAX_ACCOUNTS_TRY, MAX_ACCOUNTS_TRY_MIN, MAX_ACCOUNTS_TRY_MAX, '换号次数'),
    'session_affinity_ttl': (
        DEFAULT_SESSION_AFFINITY_TTL, SESSION_AFFINITY_TTL_MIN,
        SESSION_AFFINITY_TTL_MAX, '会话亲和时长'),
}

# name -> (默认值, 中文名)
_BOOL_FIELDS: dict[str, tuple[bool, str]] = {
    'session_affinity': (DEFAULT_SESSION_AFFINITY, '会话亲和'),
    'desktop_sync': (DEFAULT_DESKTOP_SYNC, '回写桌面端'),
}

# 每个平台真正会用到的字段。不在表里的平台（含测试里的假平台）只拿到通用两项：
# 任何适配器都有「发请求」这一步，超时对它们都有意义；换号 / 亲和 / 回写则依赖
# 具体的选号实现，不能凭空显示给用户。
_GENERIC = frozenset({'request_timeout', 'idle_timeout'})
_TRAE = frozenset({
    'request_timeout', 'idle_timeout', 'max_accounts_try',
    'session_affinity', 'session_affinity_ttl', 'desktop_sync',
})
_QODER = frozenset({'request_timeout', 'idle_timeout'})
_BY_PLATFORM: dict[str, frozenset[str]] = {
    'trae': _TRAE,
    'qoder': _QODER,
}


def capabilities(platform: str) -> dict[str, bool]:
    """该平台支持哪些字段。前端据此决定显示哪几组，而不是用平台名硬编码。"""
    supported = _BY_PLATFORM.get(platform, _GENERIC)
    return {name: name in supported for name in (*_INT_FIELDS, *_BOOL_FIELDS)}


def _key(platform: str, field: str) -> str:
    return f'platforms.{platform}.{field}'


def _read_int(platform: str, field: str) -> int:
    default, low, high, _ = _INT_FIELDS[field]
    raw = db.get_setting(_key(platform, field), default)
    if isinstance(raw, bool):
        return default
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return value if low <= value <= high else default


def _read_bool(platform: str, field: str) -> bool:
    default, _ = _BOOL_FIELDS[field]
    raw = db.get_setting(_key(platform, field), default)
    return raw if isinstance(raw, bool) else default


def configured(platform: str, field: str) -> bool:
    """该字段是否**被显式设置过**（不是「读出来的值」）。

    用于「没设置时沿用客户端 / 环境变量的默认值」：Qoder 的读超时默认来自
    `QODER_TIMEOUT`，如果这里无脑把 300 传下去，运维改过的环境变量就被悄悄
    覆盖了。显式设置过才用这里的值。
    """
    return db.get_setting(_key(platform, field), None) is not None


def load(platform: str) -> dict[str, Any]:
    """当前生效的运行参数 + 可填范围 + 能力表。"""
    values: dict[str, Any] = {
        field: _read_int(platform, field) for field in _INT_FIELDS
    }
    values.update({field: _read_bool(platform, field) for field in _BOOL_FIELDS})
    return {
        **values,
        'capabilities': capabilities(platform),
        'runtime_limits': {
            field: {'min': low, 'max': high}
            for field, (_, low, high, _) in _INT_FIELDS.items()
        },
    }


def _coerce_int(field: str, raw: object) -> int:
    default, low, high, label = _INT_FIELDS[field]
    if isinstance(raw, bool):
        raise ValueError(f'{label}必须是整数')
    if isinstance(raw, float) and not raw.is_integer():
        raise ValueError(f'{label}必须是整数')
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{label}必须是整数') from exc
    if not low <= value <= high:
        raise ValueError(f'{label}需在 {low} 与 {high} 之间')
    return value


def _coerce_bool(field: str, raw: object) -> bool:
    if not isinstance(raw, bool):
        raise ValueError(f'{_BOOL_FIELDS[field][1]}必须是布尔值')
    return raw


def save(platform: str, payload: dict) -> dict[str, Any]:
    """保存运行参数；只更新 payload 里出现的字段，非法值抛 `ValueError`。

    路由会把整个请求体一起传进来（里面还有 `max_in_flight` 与 `checkin_*`），
    这里只认自己的字段，其余原样忽略。反过来，如果 payload 带了**本平台不
    支持**的字段就明确报错：静默丢弃会让界面以为「保存成功了」，实际什么都没改。
    """
    payload = payload if isinstance(payload, dict) else {}
    caps = capabilities(platform)
    updates: dict[str, Any] = {}
    for field in _INT_FIELDS:
        if field not in payload or payload[field] is None:
            continue
        if not caps[field]:
            raise ValueError(f'{platform} 不支持「{_INT_FIELDS[field][3]}」设置')
        updates[field] = _coerce_int(field, payload[field])
    for field in _BOOL_FIELDS:
        if field not in payload or payload[field] is None:
            continue
        if not caps[field]:
            raise ValueError(f'{platform} 不支持「{_BOOL_FIELDS[field][1]}」设置')
        updates[field] = _coerce_bool(field, payload[field])
    for field, value in updates.items():
        db.set_setting(_key(platform, field), value)
    return load(platform)


# ── 适配器用的单值读取 ────────────────────────────────────
# 适配器在热路径上只需要一两个字段时，用这些函数比 `load()` 少读几行库。
# 需要一次读多个（例如 Trae 的对话路径）时仍建议 `load()` 一次拿全。
def request_timeout(platform: str) -> int:
    return _read_int(platform, 'request_timeout')


def idle_timeout(platform: str) -> int:
    return _read_int(platform, 'idle_timeout')


def max_accounts_try(platform: str) -> int:
    return _read_int(platform, 'max_accounts_try')


def session_affinity(platform: str) -> bool:
    return _read_bool(platform, 'session_affinity')


def session_affinity_ttl(platform: str) -> int:
    return _read_int(platform, 'session_affinity_ttl')


def desktop_sync(platform: str) -> bool:
    return _read_bool(platform, 'desktop_sync')
