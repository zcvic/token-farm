"""灵犀实扣积分的延迟补记（`credit_meter` 的 `detail` / `delta` 收尾）。

灵犀上游是**延迟归账**：对话结束后立刻查余额或 `credits/transaction/*` 通常
还是空的，真实流水要过几分钟才出现（参考实现 README 与实测口径一致）。所以
不能在请求收尾里等——那会把 SSE 连接挂上几分钟。这里的做法是：

  1. 请求收尾先上报 `completed`（结算触发点），再注册一个后台探针；
  2. 探针按会话号 / 账户增量轮询，查到后调 `db.apply_deferred_credit` 一次
     性补齐日志、统计与密钥额度；
  3. 窗口内始终查不到就放弃，那一格保持 NULL（界面显示「—」）。

**绝不写估算值**：这里只搬运上游流水里的真实金额，认不出就空着。

状态只在内存里：进程重启会丢掉未结算的探测，那些请求的 `credit` 继续是
NULL。这是有意的取舍——持久化需要每次请求写一次库，而丢掉的只是展示值，
不是账（上游自己的账不会因为本端重启而丢）。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from ... import db

logger = logging.getLogger('workbuddy.platforms.lingxi')

# 探测节奏（秒）：上游延迟是「几分钟」量级，前密后疏，合计约 12 分钟。
# 多数请求会在第一、二次就拿到；再往后多半是上游根本没结算。
_DELAYS: tuple[float, ...] = (20, 40, 60, 120, 180, 300)

# 探针返回值：金额 = 已拿到；None = 还没出来，下次再试；
# False = 放弃本轮（例如已被更新的请求覆盖，再查会重复计算）。
Probe = Callable[[], Awaitable[float | None | bool]]

_pending: set[str] = set()
_tasks: set[asyncio.Task] = set()


def credit_of(rows: list[dict], group_id: str = '',
              session_id: str = '') -> float | None:
    """一轮会话的消耗合计；没有消耗行返回 None。

    会话在 `detail` 口径下是本次请求专用的，所以所有负数行都属于它；带上
    `session_id` / `group_id` 时再过滤一遍：上游万一没按参数过滤（或返回了
    同账号的其它记录），也不会把别人的消耗算进来。行里缺字段时不做排除。
    """
    total = 0.0
    for row in rows:
        if not isinstance(row, dict):
            continue
        amount = row.get('amount')
        if not isinstance(amount, (int, float)) or isinstance(amount, bool):
            continue
        if amount >= 0:
            continue
        if session_id and row.get('session_id') and row['session_id'] != session_id:
            continue
        if group_id and row.get('group_id') and row['group_id'] != group_id:
            continue
        total += -float(amount)
    return round(total, 6) if total > 0 else None


def schedule(key: str, request_id: str, probe: Probe) -> bool:
    """注册一个延迟探测；`key` 只用于去重（每个请求一个唯一值）。

    没有 request_id 时不注册：没有日志行可补，探测也没有意义（例如测试
    或后台脚本直接调适配器）。返回是否真的起了任务。
    """
    rid = str(request_id or '').strip()
    dedupe = str(key or rid)
    if not rid or not dedupe or dedupe in _pending:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # 不在事件循环里（同步调用）—— 没有后台可挂
        return False
    _pending.add(dedupe)
    task = loop.create_task(_run(dedupe, rid, probe))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return True


async def _run(dedupe: str, request_id: str, probe: Probe) -> None:
    """按节奏探测 → 拿到金额就补记 → 补上就收工。

    两处「还没成」要分开看：
      · 上游还没出流水（探针返回 None）→ 下一轮继续查上游；
      · 金额有了但日志行还没写进来（写库返回 None）→ **不必再查上游**，
        记着这个金额下一轮直接补写就行（日志行通常在流结束瞬间写好，
        探针的第一次等待就够它落地）。
    """
    pending: float | None = None
    try:
        for delay in _DELAYS:
            await asyncio.sleep(delay)
            if pending is not None:
                # 上一轮已经拿到金额，只差日志行：直接补写，别再打扰上游。
                state = _write(request_id, pending)
                if state is not None:
                    return
                continue
            try:
                credit = await probe()
            except Exception:  # noqa: BLE001 —— 旁路探测，失败只影响这一格
                logger.debug('lingxi: 实扣积分探测失败', exc_info=True)
                continue
            if credit is False:
                return
            if credit is None:
                continue
            state = _write(request_id, float(credit))
            if state is True:
                return
            if state is False:
                # 已经有值（或这一行已不唯一）：不能再记第二遍。
                return
            # None = 日志行还没写进来：留着金额，下一轮直接补写。
            pending = float(credit)
    finally:
        _pending.discard(dedupe)


def _write(request_id: str, credit: float) -> bool | None:
    """补记一次；返回 True 成功 / False 不该再补 / None 还得再等。"""
    try:
        state = db.apply_deferred_credit(request_id, credit)
    except Exception:  # noqa: BLE001 —— 写库抖动也留给下一轮
        logger.debug('lingxi: 实扣积分补记写库失败', exc_info=True)
        return None
    if state is True:
        logger.debug('lingxi: 实扣积分已补记 request=%s credit=%s',
                     request_id, credit)
    return state