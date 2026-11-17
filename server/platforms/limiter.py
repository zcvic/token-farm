"""平台并发闸门（**单账号**在途请求上限）。

## 口径：设置是「每个账号」的上限，不是整池

`platforms.<name>.max_in_flight` 表示**一个账号**最多同时跑几个上游请求，
0 = 不限。总吞吐随账号数增长，而不是所有账号共享一个数字——后者在账号很多
时等于没有上限，也分不清「是池子满了」还是「这个号被上游限流了」。

两种执行方式，取决于适配器能不能自己选号：

  · `AccountLimiter` —— **严格按账号计数**（Trae）。适配器先选出账号，再占
    这个账号自己的名额；两个账号之间互不影响。
  · `PoolLimiter` —— **按账号数折算总闸门**（Qoder）。内置网关自己选号，
    适配器拿不到「本次用了哪个账号」，所以只能把上限算成
    `单账号上限 × 可用账号数`，整池共享这一个名额池。它不保证单个账号不超
    上限，但总量与「每个号各跑 N 个」一致。

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
from typing import Callable

from .. import db

# 单账号并发上限的可填区间（0 = 不限）。上限给 64：再高已经不是「保护账号」，
# 而是把上游当成无限并发的通道；真要放开，填 0 更诚实。
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
    """当前生效的**单账号**并发上限（0 = 不限）。"""
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


class AccountLimiter:
    """按账号计数的在途闸门（Trae）。

    一个账号一把信号量，上限改了会在下一次 `acquire` 时换成新的一把。旧信号量
    上还有在途请求时，它们释放到旧对象上——无害：旧对象不会再被 `acquire`，
    多出来的计数没人消费。
    """

    def __init__(self, platform: str) -> None:
        self.platform = platform
        self._limit = -1
        self._read_at = 0.0
        # uid -> (建这把信号量时的上限, 信号量)。存上限是为了「上限变了就重建」，
        # 不用在配置变更时遍历所有账号。
        self._sems: dict[str, tuple[int, asyncio.Semaphore]] = {}

    def limit(self) -> int:
        now = time.monotonic()
        if now - self._read_at >= _CACHE_TTL:
            try:
                value = load_limit(self.platform)
            except Exception:  # noqa: BLE001 —— 配置读不到时不能把对话打挂
                value = 0
            self._read_at = now
            if value != self._limit:
                self._limit = value
                # 旧信号量上的在途请求释放到旧对象上，无害；新请求立刻按新上限计数。
                self._sems.clear()
        return self._limit

    def invalidate(self) -> None:
        """上限改了之后立刻生效，不用等 5 秒缓存。"""
        self._read_at = 0.0
        self._sems.clear()

    def _sem(self, key: str) -> asyncio.Semaphore | None:
        limit = self.limit()
        if limit <= 0:
            return None
        entry = self._sems.get(key)
        if entry is None or entry[0] != limit:
            entry = (limit, asyncio.Semaphore(limit))
            self._sems[key] = entry
        return entry[1]

    async def acquire(self, key: object, *, timeout: float = DEFAULT_WAIT):
        """占某个账号的一个空位。

        返回 `None` = 未启用上限（调用方不需要释放）；返回信号量 = 已占位，
        必须把同一个对象交给 `release`。返回 `False` = 等待超时。
        """
        sem = self._sem(str(key or ''))
        if sem is None:
            return None
        try:
            await asyncio.wait_for(sem.acquire(), timeout)
        except asyncio.TimeoutError:
            return False
        return sem

    def release(self, sem) -> None:
        if sem is not None:
            sem.release()


class PoolLimiter:
    """按「单账号上限 × 账号数」折算的整池闸门（Qoder）。

    `scale` 返回当前**可用账号数**：内置网关自己选号，适配器无法在请求前知道
    落在哪个账号上，只能用总闸门逼近「每个账号各跑 N 个」。账号数变化时下一次
    刷新就会换一把新信号量（与上限变更同一处理）。

    `scale` 缺省 / 抛异常 / 返回 0 时按 1 个账号算——总闸门退化成单账号上限，
    至少不会因为读不到账号数而变成「不限」。
    """

    def __init__(self, platform: str,
                 scale: Callable[[], int] | None = None) -> None:
        self.platform = platform
        self._scale = scale
        self._sem: asyncio.Semaphore | None = None
        self._limit = -1
        self._read_at = 0.0

    def per_account(self) -> int:
        """当前生效的单账号上限（0 = 不限），供调用方决定要不要算账号数。"""
        try:
            return load_limit(self.platform)
        except Exception:  # noqa: BLE001 —— 配置读不到时按「不限」处理
            return 0

    def limit(self) -> int:
        now = time.monotonic()
        if now - self._read_at >= _CACHE_TTL:
            per_account = self.per_account()
            factor = 1
            if per_account > 0 and self._scale is not None:
                try:
                    factor = max(1, int(self._scale()))
                except Exception:  # noqa: BLE001 —— 账号数读不到就按 1 个算
                    factor = 1
            value = per_account * factor if per_account > 0 else 0
            self._read_at = now
            if value != self._limit:
                self._limit = value
                self._sem = asyncio.Semaphore(value) if value > 0 else None
        return self._limit

    def invalidate(self) -> None:
        """上限 / 账号数变了之后立刻生效，不用等 5 秒缓存。"""
        self._read_at = 0.0

    async def acquire(self, *, timeout: float = DEFAULT_WAIT):
        """占整池的一个空位（返回值语义与 `AccountLimiter.acquire` 相同）。"""
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
