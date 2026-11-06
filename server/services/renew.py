"""token 自动续期（issue #40）。

## 背景：为什么需要这个模块

用户报的是「登录的账号那个 token 不会自动续期，到期了需要重新扫码或登录」。
排查后确认这不是我们的 bug，而是**没有被任何一方覆盖到的空档**：

上游 workbuddy2api **有**刷新能力，但只在三个**内部**时机触发：

  · 保活排程：`schedule.keepalive_hours`，**默认每天只有 22 点一次**；
  · 每次 chat 选号后、token 距到期不足 `RefreshSkew`（默认 10 分钟）时；
  · 签到前 token 临近过期时。

后两条都要求**真的有人在用这个号**。于是下面这几种情况下 token 会一路走到
过期而无人续期：

  · 账号长期闲置、没有对话流量（第二、三条永不触发）；
  · 上游容器停机、或调度器没跑（第一条也断了）；
  · 保活时刻那一刻网络抖动，而下次要等一整天。

而 accessToken 的有效期是**有限的**（实测签发 60 天；短的可到几天）。一旦过期，
该账号就彻底不可用了——只能重新扫码。用户看到的正是这个。

## 本模块做什么

进程内定时巡检，**只做上游没覆盖的那部分**：账号的 token 剩余时间低于阈值时
主动调腾讯的刷新接口续期，并把新 token 原子写回账号文件（上游热加载会自己
捡起来）。

刻意**不重复上游的保活排程**：
  · 不按整点跑（上游已有 keepalive_hours），而是按**剩余寿命**判断——
    「快过期了才刷」，与「到点就刷一遍」是两件事，前者才能覆盖长期闲置的号；
  · 不看上游的开关（`keepalive_enabled=false` 是用户对**上游排程**的选择，
    而 token 过期会让账号彻底报废，属于我们不能放任的那类问题）；
  · 不做签到、不做任何业务动作，只续期。

## 阈值为什么是「剩余 3 天」

太短（比如上游选号路径的 10 分钟）要求巡检足够频繁才赶得上，而巡检间隔本身
有几小时的粒度；太长会在 token 刚签发时白白多刷几次。3 天给了足够余量：
巡检间隔（1 小时）远小于它，且一个 60 天有效期的令牌只会在最后 5% 寿命里被刷。

刷新失败时**不做惩罚**（不冷却、不禁用账号）：续期是尽力而为的辅助动作，
失败可能只是网络抖动；真正的可用性判断仍由上游的选号与保活负责。失败会记在
任务日志里（kind=keepalive），用户能看见。

## 平台账号（Trae / Qoder）

平台账号**不在**上面那套目录里（`platforms/<name>/accounts/`），刷新协议也各不
相同：Trae 是轮换式 `ExchangeToken`，Qoder 是内置网关里的 OAuth token 刷新。
两边各自由 `refresh_trae_all` / `refresh_qoder_all` 单独巡检，接在同一条循环上，
但状态互不影响（一段抛错不让另一段整小时都跑不到）。续期一律走适配器的
`refresh_account`：

  · Trae：适配器负责「先看客户端是否已刷新」「刷新后原子落盘并回写客户端」，
    绕开它会因为轮换把用户的 IDE 登出；
  · Qoder：适配器转给内置网关，网关负责 token 落盘与 COSY 会话失效重建；
    直接打回环 HTTP 会漏掉这两步。

Qoder 的 accessToken 寿命比 WorkBuddy 短得多（jobToken 约 24 小时、deviceToken
约 30 天），所以阈值单独取 6 小时，见 `_QODER_RENEW_BEFORE_SECONDS`。

没有账号的平台会被 `has_accounts_hint()` 直接跳过——Qoder 的内置网关不该因为
一条令牌巡检就被拉起来。
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import time
from pathlib import Path

from .. import config, db, upstreamsvc
from . import taskpacing
from . import tencent, wb2api
from .errtext import err_text


logger = logging.getLogger(__name__)

# 巡检间隔（秒）。1 小时：与「剩余 3 天」的阈值配合，最多晚 1 小时发现
# 待续期账号，仍远早于过期。
_CHECK_INTERVAL_SECONDS = 3600

# 剩余寿命低于此值即续期（秒）。见模块文档「阈值为什么是剩余 3 天」。
_RENEW_BEFORE_SECONDS = 3 * 86400

# Qoder 的续期窗口（秒）。**不能**直接复用 3 天：它的 jobToken 寿命约 24 小时、
# deviceToken 约 30 天，按 3 天判会让每个刚签发的 jobToken 立刻落进窗口，等于
# 每小时都去换一遍 token（还可能把上游风控喂饱）。取 6 小时：巡检一小时一跳，
# 最长晚 1 小时发现，仍远早于过期。
_QODER_RENEW_BEFORE_SECONDS = 6 * 3600

# 连续写入失败（磁盘只读等）时不刷屏的间隔
_LOG_THROTTLE_SECONDS = 6 * 3600

_loop_task: asyncio.Task | None = None
_last_fail_log: dict[str, float] = {}


def _needs_renew(expires_at: int, now: int | None = None) -> bool:
    """该到期时刻是否已进入续期窗口。"""
    if expires_at <= 0:
        return False          # 解不出到期时间：不臆测，交给上游
    now = int(time.time()) if now is None else now
    return expires_at - now <= _RENEW_BEFORE_SECONDS


def _account_payload(raw: dict) -> dict:
    """账号文件 → `tencent.refresh_token` 需要的扁平结构。"""
    acct = raw.get('account') or {}
    auth = raw.get('auth') or {}
    return {
        'access_token': auth.get('accessToken', ''),
        'refresh_token': auth.get('refreshToken', ''),
        'uid': acct.get('uid', ''),
        'enterprise_id': acct.get('enterpriseId', ''),
        'domain': auth.get('domain', ''),
        'realm': auth.get('realm'),
        'device_token': str(raw.get('device_token') or ''),
        'proxy': str(raw.get('proxy') or ''),
    }


def _iter_accounts():
    """续期巡检覆盖的账号：默认目录 + 各分组登记的账号目录。

    产出 `(目录, 账号)` 对——目录必须一路带着：读文件与回写必须落在同一个
    目录里，否则会把 A 组的凭证写进 B 组（或读错对象）；两边文件名相同时
    这种错误完全看不出来。
    """
    dirs: list[Path | None] = [None]
    seen: set[str] = set()
    for up in upstreamsvc.list_upstreams(include_default=False):
        raw = str(up.get('auth_dir') or '').strip()
        if not raw or raw in seen:
            continue
        seen.add(raw)
        dirs.append(Path(raw))
    for d in dirs:
        for acct in wb2api.list_auth_accounts(d):
            yield d, acct


async def refresh_all(force: bool = False, now: int | None = None) -> dict:
    """巡检一遍所有账号并续期，返回统计（`{checked, renewed, failed, skipped}`）。

    `force=False`（默认）：只处理进入续期窗口（剩余 < `_RENEW_BEFORE_SECONDS`）
    的账号——自动续期的主路径。

    `force=True`：**无条件**刷新所有账号，与剩余寿命无关。供「令牌刷新计划」
    及其「立即执行一次」用——对应「每 N 天全量刷一次」的诉求，也覆盖那些距
    过期还早、但用户就是想定期换一遍令牌的场景。

    逐个串行：账号数是个位数到几十，且腾讯那边对高频请求有风控——并发刷新
    得不偿失（上游保活也是串行）。分组（多账号池）登记的目录一并巡检。
    """
    stats = {'checked': 0, 'renewed': 0, 'failed': 0, 'skipped': 0}
    pacer = taskpacing.AccountPacer('keepalive')
    for base_dir, acct in _iter_accounts():
        # 面板停用（改名）的账号不续期：它已退出账号池，用户明确表示不用它。
        # 但**状态位停用（manual_disabled）要照常续期**——那条路的语义是
        # 「只摘对话流量，凭证与积分保持活跃」（issue #45）。
        if acct.get('disabled_by_panel'):
            stats['skipped'] += 1
            continue
        if not force and not _needs_renew(int(acct.get('expires_at') or 0), now):
            continue
        await pacer.wait()
        stats['checked'] += 1
        name = acct['file']
        # 日志与限频用的标签：不同分组的同名文件要区分开，否则两组的同名账号
        # 会共用同一条限频记录，其中一组的问题被另一组静默掩盖。
        label = name if base_dir is None else f'{base_dir.name}/{name}'
        try:
            raw = wb2api.read_account_file_any(name, base_dir)
        except Exception as exc:  # noqa: BLE001
            _note_failure(label, f'读取失败: {err_text(exc)}')
            stats['failed'] += 1
            continue
        payload = _account_payload(raw)
        payload['_risk_auth_dir'] = str(base_dir or config.AUTH_DIR)
        if not payload['refresh_token']:
            # 没有 refreshToken 就是真的续不了（要么重新扫码，要么它本就只有
            # 一个长期令牌）。记一次日志，别静默——用户看任务记录能明白原因。
            _note_failure(label, '缺少 refreshToken，无法续期（需重新扫码或登录）')
            stats['failed'] += 1
            continue
        ok, message, fields = await tencent.refresh_token(payload)
        if not ok:
            _note_failure(label, message)
            stats['failed'] += 1
            continue
        try:
            tencent.update_auth_tokens(name, fields, base_dir)
        except Exception as exc:  # noqa: BLE001
            _note_failure(label, f'刷新成功但写入失败: {err_text(exc)}')
            stats['failed'] += 1
            continue
        stats['renewed'] += 1
        _last_fail_log.pop(label, None)
        logger.info('token 已续期: %s（%s）', label, message)
        _record(acct, 1, f'token 自动续期成功（{message}）')
    return stats


async def renew_once(now: int | None = None) -> dict:
    """按剩余寿命巡检一遍（保留原入口，等价于 `refresh_all(force=False)`）。"""
    return await refresh_all(force=False, now=now)


# ── 令牌刷新计划：「每 N 天全量刷一次」────────────────────────
#
# 与上面的「按剩余寿命续期」并列的第二条路径：用户显式要求「每 5 天把全部账号
# 刷一遍」，不看剩多少天。配置存 db 的 settings 表（与「定时领奖」同一套），
# 因为它是**本端**的调度意图，不该写进上游 config.json（那是上游自己的排程）。
_PLAN_KEY = 'token_refresh_plan'
# **默认开启**：令牌过期是不可逆的（只能重新扫码 / 重新登录），而「每 N 天全量刷
# 一遍」本身幂等、又不看剩余寿命，正好补上「剩下的 3 天窗口巡检」覆盖不到的那种
# 「到期时间解不出来 / 时钟漂移」的账号。每 5 天凌晨 3 点一次，成本可忽略。
_PLAN_DEFAULTS = {'enabled': True, 'interval_days': 5, 'hour': 3, 'last_run': 0}


def _normalise_plan(raw: object) -> dict:
    """把库里的值收敛成合法计划：字段缺失/被手改成非法值时逐项兜底。"""
    plan = dict(_PLAN_DEFAULTS)
    if isinstance(raw, dict):
        plan.update({k: raw[k] for k in _PLAN_DEFAULTS if k in raw})
    # 脏值一律**关掉**，不做 `bool(...)` 强转：把 'yes' / 1 之类的非布尔值
    # 当开启，会在用户不知情时开始定时打腾讯接口。宁可默认关，让它显式打开。
    if not isinstance(plan['enabled'], bool):
        plan['enabled'] = False
    for key, lo, hi, default in (('interval_days', 1, 365, 5), ('hour', 0, 23, 3)):
        val = plan.get(key)
        if isinstance(val, bool) or not isinstance(val, int) or not lo <= val <= hi:
            plan[key] = default
    last = plan.get('last_run')
    plan['last_run'] = int(last) if (isinstance(last, (int, float))
                                     and not isinstance(last, bool) and last > 0) else 0
    return plan


def get_plan() -> dict:
    """当前计划（含 `last_run`，可能为 0 = 从未跑过）。"""
    return _normalise_plan(db.get_setting(_PLAN_KEY))


def set_plan(enabled: object, interval_days: object, hour: object) -> dict:
    """保存计划；校验失败抛 `ValueError` / `TypeError`（路由转 400）。

    参数声明为 `object` 而不是 `bool`/`int`：值与类型都由本函数在运行时校验
    （输入来自 HTTP JSON，类型本就不受信），调用方不必先强转。

    `last_run` **保持不变**：改间隔不该把「上次跑过」这件事抹掉，否则调完设置
    会被判成逾期、立刻又触发一轮全量刷新。
    """
    if not isinstance(enabled, bool):
        raise TypeError('enabled 必须是布尔值')
    if isinstance(interval_days, bool) or not isinstance(interval_days, int):
        raise TypeError('interval_days 必须是整数')
    if not 1 <= interval_days <= 365:
        raise ValueError('interval_days 必须是 1-365 的整数')
    if isinstance(hour, bool) or not isinstance(hour, int):
        raise TypeError('hour 必须是整数')
    if not 0 <= hour <= 23:
        raise ValueError('hour 必须是 0-23 的整点')
    plan = get_plan()
    plan.update({'enabled': enabled, 'interval_days': interval_days, 'hour': hour})
    db.set_setting(_PLAN_KEY, plan)
    return plan


def plan_is_due(plan: dict | None = None, now: int | None = None) -> bool:
    """是否到点该跑全量刷新。

    两条同时成立：①距上次执行已满 `interval_days` 天；②当前整点等于 `hour`。
    按整点对齐而不是精确到秒——巡检本就是一小时一跳，要求秒级对齐只会永远
    命中不了。

    从未跑过（`last_run=0`）时不算「逾期」，只等下一个配置整点：避免刚打开
    开关就立刻打一轮腾讯（用户可能只是想先配好、到点再跑）。
    """
    plan = get_plan() if plan is None else _normalise_plan(plan)
    if not plan['enabled']:
        return False
    now = int(time.time()) if now is None else now
    if time.localtime(now).tm_hour != plan['hour']:
        return False
    last = plan['last_run']
    if last <= 0:
        return True
    return now - last >= plan['interval_days'] * 86400


def plan_next_run(plan: dict | None = None, now: int | None = None) -> int | None:
    """下一次执行时刻（epoch 秒）；未启用返回 None。

    供界面显示「下次执行」——用户据此确认计划真的排上了，而不是只能信任开关。
    """
    plan = get_plan() if plan is None else _normalise_plan(plan)
    if not plan['enabled']:
        return None
    now = int(time.time()) if now is None else now
    last = plan['last_run']
    base = max(now, last + plan['interval_days'] * 86400) if last > 0 else now
    day = datetime.date(*time.localtime(base)[:3])
    for i in range(400):
        cand = int(datetime.datetime.combine(
            day + datetime.timedelta(days=i),
            datetime.time(hour=plan['hour']),
        ).timestamp())
        if cand >= base:
            return cand
    return None


def mark_plan_ran(now: int | None = None) -> dict:
    """记录计划刚跑过（重置计时）。返回更新后的计划。"""
    plan = get_plan()
    plan['last_run'] = int(time.time()) if now is None else int(now)
    db.set_setting(_PLAN_KEY, plan)
    return plan


def _record_plan(stats: dict) -> None:
    """把计划执行结果落一条任务记录（kind=keepalive，与续期同类别，用户看得见）。"""
    _record(
        {'uid': '', 'nickname': 'token-refresh-plan', 'file': 'token-refresh-plan'},
        1 if stats.get('renewed') and not stats.get('failed') else 0,
        f"计划任务全量刷新：成功 {stats.get('renewed', 0)}，失败 {stats.get('failed', 0)}，"
        f"跳过 {stats.get('skipped', 0)}",
    )


def _note_failure(name: str, message: str) -> None:
    """记失败：日志与任务记录都写，但**限频**避免每小时刷屏。"""
    now = time.time()
    last = _last_fail_log.get(name, 0.0)
    if now - last < _LOG_THROTTLE_SECONDS:
        return
    _last_fail_log[name] = now
    logger.warning('token 续期失败: %s（%s）', name, message)
    _record({'uid': '', 'nickname': name, 'file': name}, 0, f'token 自动续期失败：{message}')


# ── 平台账号（Trae）的 token 续期 ─────────────────────────────
#
# 与上面 WorkBuddy 那段**并行**：两边账号文件格式、刷新协议、失效判断都不同，
# 共用同一条巡检循环但互不影响——任何一边抛错都不该拖住另一边。
#
# Trae 的 accessToken 由 `ExchangeToken` 轮换续期（refreshToken 同时换新）。
# 这条链路适配器已经实现好了（`_ensure_token`：先看客户端是否已刷新、刷新后
# 写回客户端），这里只是把「快过期了」这个触发时机补上——否则账号只有在
# 被用到或手动点刷新时才续，长期闲置会一路走到过期。
#
# 不能做的事：不能在这里绕过适配器直接调 `client.exchange_token`。轮换是单向
# 的，写回客户端与原子落盘都由适配器负责，漏一步就会把用户的 IDE 登出。


def _platform_adapter(name: str):
    """平台注册表里的适配器；未注册（被裁掉的部署）返回 None。"""
    from .. import platforms

    try:
        return platforms.get(name)
    except Exception:  # noqa: BLE001 —— 平台层异常不该打死续期巡检
        return None


def _trae_adapter():
    return _platform_adapter('trae')


def _qoder_adapter():
    return _platform_adapter('qoder')


def _trae_expires_at(account: dict) -> int | None:
    """账号的 accessToken 到期时刻（epoch 秒）；解不出返回 None。"""
    from ..platforms.trae.client import parse_epoch

    return parse_epoch(account.get('expiredAt'))


async def refresh_trae_all(force: bool = False, now: int | None = None) -> dict:
    """巡检一遍 Trae 账号并按需续期。

    与 `refresh_all` 同口径：`force=False` 只处理进入续期窗口（剩余 < 3 天）
    的账号；`force=True` 无条件全刷（「令牌刷新计划」用）。

    返回 `{checked, renewed, failed, skipped}`，与 WorkBuddy 那份形状一致，
    便于调用方统一打日志。
    """
    stats = {'checked': 0, 'renewed': 0, 'failed': 0, 'skipped': 0}
    adapter = _trae_adapter()
    if adapter is None:
        return stats
    now_ts = int(time.time()) if now is None else int(now)
    for account in adapter.list_accounts():
        uid = str(account.get('uid') or '').strip()
        if not uid:
            continue
        expires = _trae_expires_at(account)
        if expires is None:
            # 解不出到期时间：不臆测。交给面板的「刷新」按钮，避免每小时
            # 对一批「不知道什么时候过期」的账号发起无效轮换。
            stats['skipped'] += 1
            continue
        if not force and expires - now_ts > _RENEW_BEFORE_SECONDS:
            continue
        if not account.get('has_refresh_token'):
            _note_failure(f'trae/{uid}', '缺少 refreshToken，无法自动续期（需重新登录或导入）')
            stats['failed'] += 1
            continue
        stats['checked'] += 1
        try:
            # 适配器内部串行（按 uid 加锁）、失败退避、成功后原子落盘并回写客户端。
            await adapter.refresh_account(uid, force=True)
        except Exception as exc:  # noqa: BLE001
            _note_failure(f'trae/{uid}', err_text(exc))
            stats['failed'] += 1
            continue
        stats['renewed'] += 1
        _last_fail_log.pop(f'trae/{uid}', None)
        logger.info('Trae token 已续期: %s', uid)
        _record({'uid': uid, 'nickname': str(account.get('nickname') or ''),
                 'file': uid}, 1, 'token 自动续期成功（Trae）')
    return stats


def _parse_expiry(value: object) -> int | None:
    """ISO8601 / epoch（秒或毫秒）→ 秒级时间戳；解析不出返回 None。

    Qoder 的账号到期时间来自网关的 `expiresAt`（mapping 转成 UTC ISO 文本），
    形如 `2026-12-01T08:00:00+00:00`；也接受纯数字以防上游换回 epoch。
    """
    if value in (None, ''):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
        return int(seconds / 1000) if seconds > 1e11 else int(seconds)
    text = str(value).strip()
    if not text:
        return None
    iso = text[:-1] + '+00:00' if text.endswith('Z') else text
    try:
        parsed = datetime.datetime.fromisoformat(iso)
    except ValueError:
        try:
            seconds = float(text)
        except ValueError:
            return None
        return int(seconds / 1000) if seconds > 1e11 else int(seconds)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return int(parsed.timestamp())


async def refresh_qoder_all(force: bool = False, now: int | None = None) -> dict:
    """巡检一遍 Qoder 账号并按需续期。

    与 Trae 同口径，只有两点不同：

      · 阈值是 6 小时（`_QODER_RENEW_BEFORE_SECONDS`），因为它的令牌寿命短；
      · 账号到期时间来自内置网关的 `expiresAt`，mapping 已转成 ISO 文本。

    国区账号会用 `9999-12-31` 占位表示「永不过期」，解析出来是个极大的时间戳，
    自然落在窗口外——不需要特判。`force=True` 时仍然全刷（计划任务用）。
    """
    stats = {'checked': 0, 'renewed': 0, 'failed': 0, 'skipped': 0}
    adapter = _qoder_adapter()
    if adapter is None:
        return stats
    hint = getattr(adapter, 'has_accounts_hint', None)
    if hint is not None and not hint():
        # 没有 Qoder 账号：别为了巡检把内置网关拉起来（回环服务 + 后台线程）。
        return stats
    now_ts = int(time.time()) if now is None else int(now)
    for account in adapter.list_accounts():
        uid = str(account.get('uid') or '').strip()
        if not uid:
            continue
        # 冻结 / 停用的号不续期：它已退出调度，用户明确表示不用它。
        if account.get('disabled'):
            stats['skipped'] += 1
            continue
        expires = _parse_expiry(account.get('expiredAt'))
        if expires is None:
            # 解不出到期时间：不臆测（与 Trae 同口径，避免每小时打一批无效上游）。
            stats['skipped'] += 1
            continue
        if not force and expires - now_ts > _QODER_RENEW_BEFORE_SECONDS:
            continue
        if not account.get('has_refresh_token'):
            _note_failure(f'qoder/{uid}',
                          '缺少 refreshToken，无法自动续期（需重新登录或导入 PAT）')
            stats['failed'] += 1
            continue
        stats['checked'] += 1
        try:
            # 适配器转给内置网关：token 落盘与 COSY 会话失效重建都在网关里，
            # 绕开它直接打回环 HTTP 会漏掉这两步。它还会顺带强刷一次余额快照。
            await adapter.refresh_account(uid, force=True)
        except Exception as exc:  # noqa: BLE001
            _note_failure(f'qoder/{uid}', err_text(exc))
            stats['failed'] += 1
            continue
        stats['renewed'] += 1
        _last_fail_log.pop(f'qoder/{uid}', None)
        logger.info('Qoder token 已续期: %s', uid)
        _record({'uid': uid, 'nickname': str(account.get('nickname') or ''),
                 'file': uid}, 1, 'token 自动续期成功（Qoder）')
    return stats


#: 平台账号的续期入口：`(平台名, 日志标签, 巡检函数)`。新增平台只加一行——
#: 巡检循环、计划任务与「立即执行一次」都从这里遍历，避免三处各写一份清单。
_PLATFORM_RENEWERS = (
    ('trae', 'Trae', refresh_trae_all),
    ('qoder', 'Qoder', refresh_qoder_all),
)


async def refresh_all_platforms(force: bool = False, now: int | None = None) -> dict:
    """**全部来源**的全量刷新：WorkBuddy + 平台账号（Trae / Qoder）。

    「立即执行一次」用它，而不是只调 `refresh_all`：用户按下的是「把所有令牌
    刷一遍」，只刷 WorkBuddy 会让他以为计划对平台账号没生效（计划里的自动执行
    本来就是各段都跑，手动入口不该更弱）。

    每段各自兜底：一段抛错不让其他段整轮不跑——与巡检循环同一原则。
    返回合并后的统计，并带 `platforms` 明细，界面能说明这一轮到底刷了什么。
    """
    stats = await refresh_all(force=force, now=now)
    platform_stats: dict[str, dict] = {}
    for name, _label, runner in _PLATFORM_RENEWERS:
        try:
            platform_stats[name] = await runner(force=force, now=now)
        except Exception as exc:  # noqa: BLE001 —— 平台段异常不该吞掉 WorkBuddy 的结果
            logger.warning('%s 全量刷新异常: %s', name, exc)
            platform_stats[name] = {'checked': 0, 'renewed': 0,
                                    'failed': 0, 'skipped': 0}
    merged = dict(stats)
    for key in ('checked', 'renewed', 'failed', 'skipped'):
        merged[key] = int(stats.get(key, 0)) + sum(
            int(part.get(key, 0)) for part in platform_stats.values())
    merged['platforms'] = platform_stats
    return merged


def _record(acct: dict, credits: float, message: str) -> None:
    """落一条任务日志（复用「令牌保活」这一类，用户能在任务页看到）。

    为什么单独记一条而不是只打日志：用户排查「为什么这个号掉线了」时看的是
    任务记录页，不是容器日志（容器日志重建即丢）。续期是账号存活的关键动作，
    必须留痕。
    """
    try:
        uid = str(acct.get('uid') or '') or str(acct.get('file') or '')
        db.add_task_logs([{
            'ts': int(time.time()),
            'uid': uid,
            'kind': 'keepalive',
            'level': 'credit' if credits else 'warn',
            'credits': credits,
            'message': message,
            # 同一账号同一秒内重复写会被去重；续期事件天然低频，加时间戳即可
            'dedup_key': f'renew:{uid}:{int(time.time())}',
        }])
    except Exception as exc:  # noqa: BLE001
        logger.warning('写续期任务日志失败: %s', exc)


async def _loop() -> None:
    """定时巡检（首轮延迟一点，避免与启动期的其他初始化抢资源）。"""
    await asyncio.sleep(30)
    while True:
        try:
            with taskpacing.scheduled_execution():
                stats = await renew_once()
            if any(stats[k] for k in ('renewed', 'failed')):
                logger.info('token 续期巡检：%s', stats)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning('token 续期巡检异常: %s', exc)
        # 平台账号（Trae / Qoder）各单独一段：账号目录与刷新协议都与 WorkBuddy
        # 不同，一段抛错不该让其他段整小时都跑不到。
        for _name, label, runner in _PLATFORM_RENEWERS:
            try:
                pstats = await runner()
                if any(pstats[k] for k in ('renewed', 'failed')):
                    logger.info('%s token 续期巡检：%s', label, pstats)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning('%s token 续期巡检异常: %s', label, exc)
        # 令牌刷新计划：到点就无条件全量刷一轮（与上面的寿命巡检独立，
        # 一段出错不该拖住另一段）。
        try:
            plan = get_plan()
            if plan_is_due(plan):
                logger.info('令牌刷新计划触发（每 %d 天，%02d 点档）',
                            plan['interval_days'], plan['hour'])
                with taskpacing.scheduled_execution():
                    pstats = await refresh_all(force=True)
                    # 平台账号也吃这份「每 N 天全量刷一次」的排程：用户设的是
                    # 「把所有令牌刷一遍」，只刷 WorkBuddy 会让他以为计划没生效。
                    for _name, label, runner in _PLATFORM_RENEWERS:
                        try:
                            s = await runner(force=True)
                            if any(s[k] for k in ('renewed', 'failed')):
                                logger.info('令牌刷新计划（%s）：%s', label, s)
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:  # noqa: BLE001
                            logger.warning('令牌刷新计划（%s）异常：%s', label, exc)
                logger.info('令牌刷新计划完成：%s', pstats)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning('令牌刷新计划异常: %s', exc)
        await asyncio.sleep(_CHECK_INTERVAL_SECONDS)


def start_scheduler() -> bool:
    """启动续期巡检（幂等）。无事件循环时返回 False（如 CLI 场景）。"""
    global _loop_task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    if _loop_task is None or _loop_task.done():
        _loop_task = loop.create_task(_loop())
    return True


def stop_scheduler() -> None:
    global _loop_task
    if _loop_task and not _loop_task.done():
        _loop_task.cancel()
