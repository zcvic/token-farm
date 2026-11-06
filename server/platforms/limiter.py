"""平台池级并发闸门（在途请求上限）。

## 为什么是「池级」而不是「单账号」

WorkBuddy 的上游只有池级上限（`pool.max_in_flight` / `max_in_flight_global`），
没有单账号字段。平台适配器要和它保持同一个心智模型：一个平台上所有账号加起来
最多同时跑 N 个请求，而不是给每个账号各设一个数字（后者在账号很多时等于没有
上限）。

## 生命周期：**流式请求要占到流结束**

并发上限的意义是「同一时刻有多少个上游请求在跑」。非流式在聚合完就能释放；
流式必须等流关闭（客户端断开也会走到 `PlatformChat.aclose`）。所以释放动作
由适配器包装 `closer` 完成，而不是 `chat()` 一返回就释放——那样上限形同虚设。

## 进程内计数

计数在**当前进程**里。面板是单进程部署，这与实际并发一致；将来多进程部署时
每个进程各算各的，这一点必须写在这里，避免有人以为它是全局限流。
"""
from __future__ import annotations

import asyncio
import time

from .. import db

# 与 WorkBuddy 的 `pool.max_in_flight` 同一区间（0 = 不限）。
LIMIT_MIN = 0
LIMIT_MAX = 64
# 等一个空位的上限：超过就明确拒绝（429），而不是把请求无限期挂着。
DEFAULT_WAIT = 20.0

_CACHE_TTL = 5.0


def setting_key(platform: str) -> str:
    return f'platforms.{platform}.max_in_flight'


def _clamp(value: object) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return LIMIT_MIN
    if isinstance(value, bool):
        return LIMIT_MIN
    return min(LIMIT_MAX, max(LIMIT_MIN, number))


def load_limit(platform: str) -> int:
    return _clamp(db.get_setting(setting_key(platform), LIMIT_MIN))


def save_limit(platform: str, value: object) -> int:
    """校验并落库；非法值抛 `ValueError`（路由转 400）。"""
    if isinstance(value, bool):
        raise ValueError('并发上限必须是整数')
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError('并发上限必须是整数') from exc
    if number < LIMIT_MIN or number > LIMIT_MAX:
        raise ValueError(f'并发上限需在 {LIMIT_MIN} 与 {LIMIT_MAX} 之间（0 = 不限）')
    db.set_setting(setting_key(platform), number)
    return number


class PoolLimiter:
    """一个平台一个实例；上限改了会自动换一把新的信号量。

    旧信号量上还有在途请求时，它们释放到旧对象上——无害：旧对象不会再被
    `acquire`，多出来的计数没人消费。新请求立刻按新上限计数。
    """

    def __init__(self, platform: str) -> None:
        self.platform = platform
        self._sem: asyncio.Semaphore | None = None
        self._limit = -1
        self._read_at = 0.0

    def limit(self) -> int:
        now = time.monotonic()
        if now - self._read_at >= _CACHE_TTL:
            try:
                value = load_limit(self.platform)
            except Exception:  # noqa: BLE001 —— 配置读不到时不能把对话打挂
                value = 0
            self._read_at = now
            if value != self._limit:
                # 上限变了就换一把新的：旧信号量上还有在途请求时，它们释放到
                # 旧对象上，无害；新请求立刻按新上限计数。
                self._limit = value
                self._sem = asyncio.Semaphore(value) if value > 0 else None
        return self._limit

    def invalidate(self) -> None:
        self._read_at = 0.0

    async def acquire(self, *, timeout: float = DEFAULT_WAIT):
        """占一个空位。

        返回 `None` = 未启用上限（调用方不需要释放）；返回信号量 = 已占位，
        必须把同一个对象交给 `release`。返回 `False` = 等待超时。
        """
        limit = self.limit()
        if limit <= 0:
            return None
        sem = self._sem
        if sem is None:
            sem = self._sem = asyncio.Semaphore(limit)
        try:
            await asyncio.wait_for(sem.acquire(), timeout)
        except asyncio.TimeoutError:
            return False
        return sem

    def release(self, sem) -> None:
        if sem is not None:
            sem.release()
