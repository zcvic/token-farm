"""平台批量签到的节奏：并发上限 + 账号间隔。

## 为什么需要

Trae / Qoder 的批量签到此前只用了一个硬编码的信号量（3）并发拉起全部账号，
账号之间**零间隔**：多个账号会在同一秒打到同一个站点。Trae 侧没有额外保护；
Qoder 的活动平台还会按「人 / 设备」去重（见 `qoder_tasks.run_checkin` 的
`blocked` 分支），并发太密时后面的账号会出现「同人已领取」。

## 设置口径

按平台分开存（`platforms.<name>.checkin_*`）：

  · `checkin_concurrency` —— 同时进行签到的账号数（1–8，默认 2）
  · `checkin_delay_min/max` —— 相邻两个账号**开始**签到之间的随机间隔
    （0–60 秒，默认 2–5 秒）

间隔为 0 表示「不间隔」，只受并发上限约束，与旧行为一致。

这些数字与平台的**推理**并发（`limiter.py` 的 `max_in_flight`）是两回事：
那个限制的是转发请求的在途数，这里限制的是批量签到这种一次性任务的节奏。
"""
from __future__ import annotations

import asyncio
import logging
import random
import time

from .. import db

logger = logging.getLogger('workbuddy.platforms.checkinpacing')

DEFAULT_CONCURRENCY = 2
DEFAULT_DELAY_MIN = 2
DEFAULT_DELAY_MAX = 5

CONCURRENCY_MIN = 1
CONCURRENCY_MAX = 8
DELAY_MIN = 0
DELAY_MAX = 60

_FIELDS = ('checkin_concurrency', 'checkin_delay_min', 'checkin_delay_max')
_DEFAULT = {
    'checkin_concurrency': DEFAULT_CONCURRENCY,
    'checkin_delay_min': DEFAULT_DELAY_MIN,
    'checkin_delay_max': DEFAULT_DELAY_MAX,
}
_RANGE = {
    'checkin_concurrency': (CONCURRENCY_MIN, CONCURRENCY_MAX, '签到并发'),
    'checkin_delay_min': (DELAY_MIN, DELAY_MAX, '签到间隔'),
    'checkin_delay_max': (DELAY_MIN, DELAY_MAX, '签到间隔'),
}


def _key(platform: str, field: str) -> str:
    return f'platforms.{platform}.{field}'


def _read(platform: str, field: str) -> int:
    """读一个字段；库里的值坏了（类型不对 / 越界）就回默认值。

    读路径不抛异常：一条坏设置不该让「全部签到」整个不可用。
    """
    raw = db.get_setting(_key(platform, field), _DEFAULT[field])
    if isinstance(raw, bool):
        return _DEFAULT[field]
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT[field]
    low, high, _ = _RANGE[field]
    return value if low <= value <= high else _DEFAULT[field]


def load(platform: str) -> dict:
    """当前生效的节奏 + 可填范围（界面据此校验）。"""
    values = {field: _read(platform, field) for field in _FIELDS}
    if values['checkin_delay_min'] > values['checkin_delay_max']:
        # 历史数据里出现过交叉就整组回默认，避免读出一个「永远等不到」的区间。
        values['checkin_delay_min'] = DEFAULT_DELAY_MIN
        values['checkin_delay_max'] = DEFAULT_DELAY_MAX
    return {
        **values,
        'checkin_limits': {
            'concurrency_min': CONCURRENCY_MIN,
            'concurrency_max': CONCURRENCY_MAX,
            'delay_min': DELAY_MIN,
            'delay_max': DELAY_MAX,
        },
    }


def _coerce(field: str, raw: object) -> int:
    low, high, label = _RANGE[field]
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


def save(platform: str, payload: dict) -> dict:
    """保存节奏设置；只更新 payload 里出现的字段，非法值抛 `ValueError`。"""
    payload = payload if isinstance(payload, dict) else {}
    current = load(platform)
    updates = {
        field: _coerce(field, payload[field])
        for field in _FIELDS
        if field in payload and payload[field] is not None
    }
    merged = {**{field: current[field] for field in _FIELDS}, **updates}
    if merged['checkin_delay_min'] > merged['checkin_delay_max']:
        raise ValueError('签到间隔下限不能大于上限')
    for field, value in updates.items():
        db.set_setting(_key(platform, field), value)
    return load(platform)


class StartPacer:
    """让相邻两个账号的**开始时刻**至少相隔一段随机间隔。

    调用方在拿到并发信号量之后、真正发起签到之前 `await wait()`：

      · **先占信号量再等间隔**：否则排队中的账号会在间隔结束时同时开跑，
        把「一次爆发」变成「一串爆发」，等于没加；
      · 用单调时钟排「下一个可开始时刻」：单次签到比间隔还慢时不再额外等待，
        间隔只是下限。
    """

    def __init__(self, low: int, high: int) -> None:
        self.low = max(DELAY_MIN, int(low))
        self.high = min(DELAY_MAX, max(self.low, int(high)))
        self._lock = asyncio.Lock()
        self._next = 0.0

    @property
    def enabled(self) -> bool:
        return self.high > 0

    async def wait(self) -> None:
        if not self.enabled:
            return
        delay = float(random.randint(self.low, self.high))
        async with self._lock:
            start = max(time.monotonic(), self._next)
            self._next = start + delay
        remaining = start - time.monotonic()
        if remaining > 0:
            logger.info('平台签到节奏：下一个账号 %.1fs 后开始', remaining)
            await asyncio.sleep(remaining)
