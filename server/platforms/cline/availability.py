"""Cline 模型的可用性信号（只收真实流量的被动观测）。

## 为什么要有这一层

Cline 的免费档是一池模型，其中个别模型会间歇性坏掉（上游 500 空响应、
`Daily free limit`、`INFERENCE_CAP_ERROR`）。客户端选模型时通常点列表里的
第一个，于是「第一个恰好是坏模型」的账号会看起来完全不可用——而同一账号
换个模型就好。

上游 `cline2api` 的做法是：被动记录真实请求的成败（这里），再加 15 分钟一次
的主动补测（本仓**不取**：那要额外打付费/配额请求，且本仓没有平台级定时
探针的宿主）。只有被动信号时，顺序仍然比「固定池序」准确：某个模型刚失败过
就沉底，成功过的置顶，没信号的保持原序。

## 三条纪律

1. **只影响排序，不影响可用性判定**。模型没被标记成「不可用」，也不会因此
   拒绝请求——信号会过期（失败 30 分钟、成功 2 小时），过期即回到「未知」。
2. **只记模型级失败**。401 / 403 / 402 是账号或余额问题，与模型无关，记进来
   会把整个池子拖下水。
3. **纯内存**。进程重启后回到「全部未知」，顺序退化为原来的池序——这是安全
   的降级，不需要落盘。
"""
from __future__ import annotations

import time
from typing import Callable

# 失败 / 成功信号的保鲜期（与上游参考实现一致）。
FAIL_TTL = 30 * 60
OK_TTL = 2 * 3600

# 只有这些状态算「这个模型不行」；账号级错误（401/402/403）不在此列。
MODEL_FAILURE_STATUSES = frozenset({404, 429, 500, 502, 503, 504})


class ModelHealth:
    """`model_id → 最近一次信号` 的内存表。"""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._states: dict[str, tuple[bool, float, str]] = {}

    def record_ok(self, model: object) -> None:
        key = str(model or '').strip().lower()
        if key:
            self._states[key] = (True, self._clock(), '')

    def record_fail(self, model: object, status: int, reason: str = '') -> None:
        key = str(model or '').strip().lower()
        if not key or status not in MODEL_FAILURE_STATUSES:
            return
        self._states[key] = (False, self._clock(), str(reason or '')[:200])

    def record_unavailable(self, model: object, reason: str = '') -> None:
        """直接记一次失败（不经状态码过滤）。

        用于「HTTP 200 但流里说这个模型不行」这种上游把错误伪装成正常帧的
        情况——那种响应没有状态码可看，但模型确实没产出。
        """
        key = str(model or '').strip().lower()
        if key:
            self._states[key] = (False, self._clock(), str(reason or '')[:200])

    def state(self, model: object) -> tuple[bool | None, str]:
        """返回 `(可用?, 失败原因)`；`None` = 未知或信号已过期。"""
        key = str(model or '').strip().lower()
        entry = self._states.get(key)
        if not entry:
            return None, ''
        ok, stamped, reason = entry
        if self._clock() - stamped > (OK_TTL if ok else FAIL_TTL):
            self._states.pop(key, None)
            return None, ''
        return ok, reason

    def rank(self, model: object) -> int:
        """排序键：可用 0、未知 1、失败 2（越小越靠前）。"""
        ok, _reason = self.state(model)
        return 0 if ok else (2 if ok is False else 1)

    def sort(self, models: list[dict]) -> list[dict]:
        """按可用性稳定排序：可用 → 未知 → 失败。

        稳定排序保证「同等级内保持目录原序」，没有信号时顺序与改造前一致
        （向后兼容，不会让用户的下拉顺序莫名其妙地跳）。
        """
        return sorted(models,
                      key=lambda m: self.rank(m.get('id') or m.get('model_name')))
