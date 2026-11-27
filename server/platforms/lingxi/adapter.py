"""WPS 灵犀（lingxi.wps.cn）平台适配器。

与 Loomy / DuMate / Discovery 同构：本文件只负责**策略**，协议在 `client.py`
与 `chat.py`。

  · 选号（权重 SWRR + LRU + 会话粘性）与单账号并发闸门；
  · Cookie 探活（`GET /account/profile`，见 `client.probe`）；
  · 模型目录（在线 `/sessions/plans` → 内置快照）；
  · 每日签到 / 智点余额（任务中心接口）；
  · 流式（OpenAI chunk）/ 非流式两条出口的最后一步。

灵犀是**单区域**平台（`realm_aware=False`），账号与模型统一 `realm='cn'`。

## 凭证

只有 `wps_sid` Cookie，没有 OAuth / refresh token：`refresh_account` 做的是
「拿 Cookie 探一次活」，不是续期；失效只能重新登录网页版再抓 Cookie。

## 对话

每轮都带完整 messages（上游请求体是 OpenAI 形状，不依赖服务端记忆），但
**会话 + run 会按「账号 + 设备 + 模型」缓存 1 小时**（与参考实现一致）：
每轮重建会话会让上游不断新开 agent 会话，也丢掉 prompt cache 亲和。缓存
过期或 run 失效（404 + 会话/run 过期文案）时重建一次再试。
会话粘性只用于把**同一段对话钉到同一个账号**，与 run 缓存是两件事。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable

from ... import config, db
from ...services import requestctx
from ...services.session_affinity import SessionAffinity, affinity_key
from .. import settings as runtime_settings
from ..base import ChatContext, PlatformAdapter, PlatformChat, PlatformError
from ..limiter import AccountLimiter
from . import catalog, chat as chatproto, client, constants as C, settle, store

logger = logging.getLogger('workbuddy.platforms.lingxi')

SETTING_MODELS = 'platforms.lingxi.models'
SETTING_MODELS_ATTEMPT = 'platforms.lingxi.models_attempt'

# 429 / 额度的冷却口径。灵犀用 429 表达限流；与其它平台一致，默认 0 表示
# 「维持旧行为：直接上抛、不换号不冻结」，由设置项开启。
QUOTA_COOLDOWN_SECONDS = 3600

# 任务中心的状态词（桌面客户端）：可领取 / 已领取；其余（incomplete）需要
# 用户在客户端完成对应动作，本端只展示、不代做。
_TASK_DONE_STATUS = {'claimed', 'completed', 'done', 'finished'}
_TASK_CLAIMABLE_STATUS = {'claimable', 'available'}
# 每日签到在面板上有独立的签到卡片，不再出现在任务列表里（否则同一天
# 会有两个按钮能触发领取，绕过 checkin 的缓存刷新）。
_DAILY_TASK_KEY = 'daily_check_in'
# `referral/bind` 的业务失败原因 → HTTP 语义（上游通常仍返回 200）。
_INVITE_ERRORS: dict[str, tuple[int, str]] = {
    'already_bound': (409, 'already_activated'),
    'not_new_user': (400, 'invite_not_new_user'),
    'self_invite': (400, 'invite_self'),
    'invalid_code': (400, 'invite_invalid'),
    'activity_ended': (400, 'invite_activity_ended'),
    'activity_not_started': (400, 'invite_activity_upcoming'),
    'activity_paused': (400, 'invite_activity_paused'),
}

# 独立任务集合：取消之后补发的 `completed` 上报挂在这里，避免被 GC 提前回收
# （fire-and-forget 的任务必须自己保活，见 asyncio 文档）。
_BACKGROUND_TASKS: set[asyncio.Task] = set()


# ── 小工具 ──────────────────────────────────────────────────
@dataclass
class _CreditMeter:
    """一次请求的计量凭据（`credit_meter` 为 '' 时不会创建）。

    `mode` 决定收尾怎么取值：
      · `delta`  —— 请求前记下账户累计消耗，结算后再查一次求增量；
      · `detail` —— 每轮独立会话，结算后按 `session_id` 查上游流水取本次消耗。
    两种模式都会让请求走「新建 run + 上报生命周期」的计费路径。

    灵犀的账是**延迟归账**（几分钟后才出现在流水里），所以查询不在请求收尾
    里做，而是交给 `settle` 的后台探测（见该模块说明）。
    """

    mode: str
    token: object | None = None
    before: float | None = None
    session_id: str = ''
    run_id: str = ''
    group_id: str = ''
    uid: str = ''
    request_id: str = ''
    # delta 的窗口序号：同一账号有更新的计量请求进来时，旧的探测主动放弃，
    # 避免把后一笔消耗重复记到前一笔头上。
    seq: int = 0


def _pick(raw: dict, *keys: str) -> str:
    for key in keys:
        value = raw.get(key)
        if value not in (None, ''):
            return str(value).strip()
    return ''


def _num(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _account_weight(account: dict) -> float:
    value = _num(account.get('weight'))
    if value is None or value <= 0:
        return 1.0
    return min(store.WEIGHT_MAX, max(store.WEIGHT_MIN, value))


def _noop() -> None:
    """未占位时的空释放动作。"""


def _lease_releaser(limiter: AccountLimiter, lease) -> Callable[[], None]:
    released = False

    def release() -> None:
        nonlocal released
        if released:
            return
        released = True
        limiter.release(lease)

    return release


class LingxiAdapter(PlatformAdapter):
    name = 'lingxi'
    label = 'WPS 灵犀'
    # 每日签到 = 任务中心的 `daily_check_in`。
    supports_checkin = True
    # 没有开放平台、也没有稳定的网页登录流程：只支持抓包导入 Cookie。
    supports_web_login = False
    # 凭证是整段网页 Cookie（`wps_sid` 等），面板允许直接粘贴。
    supports_cookie_import = True
    # 单区域平台。
    realm_aware = False

    def supports_tasks(self) -> bool:
        """灵犀客户端里有任务中心（签到 / 一次性任务）与邀请有礼。"""
        return True

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        # 同一账号的任务写操作串行（领取 / 绑邀请码 / 兑换），避免连点重复提交。
        self._task_locks: dict[str, asyncio.Lock] = {}
        self._last_used: dict[str, float] = {}
        self._probes: dict[str, tuple[float, dict]] = {}
        self._credits: dict[str, tuple[float, dict]] = {}
        self._limiter = AccountLimiter(self.name)
        self._swrr: dict[str, float] = {}
        self._affinity = SessionAffinity()
        self._model_meta: dict[str, dict] = {}
        # 正在「前后夹一次累计消耗」的账号（uid -> token）。delta 口径并发在途
        # 时无法归属，后来的请求照常计费、只是不记账。
        self._metering: dict[str, object] = {}
        # delta 的窗口序号（uid -> int）：新请求进来就递增，旧的延迟探测据此
        # 判断「这段窗口已经被覆盖」，主动放弃而不是把别人的消耗算进来。
        self._meter_seq: dict[str, int] = {}

    # ── 内部工具 ──────────────────────────────────────────
    def _require(self, account_id: object) -> dict:
        account = store.read_account(account_id)
        if not account:
            raise PlatformError('账号不存在', status=404, code='account_not_found')
        return account

    def _proxy_for(self, account: dict) -> str | None:
        try:
            return config.resolve_account_proxy(account)
        except ValueError as exc:
            raise PlatformError(str(exc), status=503,
                                code='proxy_invalid') from exc

    @staticmethod
    def _cookie_of(account: dict) -> str:
        return str(account.get('cookie') or '').strip()

    # ── 账号 ──────────────────────────────────────────────
    def list_accounts(self) -> list[dict]:
        return [store.public_account(a) for a in store.list_accounts()]

    def export_accounts(self) -> list[dict]:
        """导出含明文 Cookie 的账号，仅供管理员导出接口调用。"""
        return store.list_accounts()

    def import_account(self, payload: dict) -> dict:
        """导入账号：粘贴网页 Cookie（也可只粘 `wps_sid` 的值）。

        接受 `{account: {...}}` 与扁平对象两种形状；`cookie` / `cookies` /
        `token` / `session` / `wps_sid` 都按 Cookie 处理。导入阶段**不联网**，
        探活在 `refresh_account` 或第一次对话时发生。
        """
        if not isinstance(payload, dict):
            raise PlatformError('账号必须是 JSON 对象', status=400,
                                code='invalid_account')
        raw = payload.get('account') if isinstance(payload.get('account'), dict) \
            else payload

        cookie = store.normalize_cookie(
            _pick(raw, 'cookie', 'cookies', 'token', 'session', 'wps_sid'))
        if not cookie:
            raise PlatformError('需要提供 WPS 灵犀网页 Cookie', status=400,
                                code='invalid_account')
        if not store.has_login(cookie):
            raise PlatformError(
                'Cookie 里没有登录态字段（需要 wps_sid），请从已登录的 '
                'lingxi.wps.cn 复制完整 Cookie',
                status=400, code='invalid_account')

        try:
            uid = store.derive_uid(cookie, _pick(raw, 'uid'))
        except ValueError as exc:
            raise PlatformError(str(exc), status=400,
                                code='invalid_account') from exc

        base = _pick(raw, 'base')
        if base not in (C.BASE_URL, C.KDOCS_BASE):
            base = ''
        old = store.read_account(uid) or {}
        account = {
            **old,
            'uid': uid,
            'cookie': cookie,
            'deviceId': _pick(raw, 'deviceId', 'device_id', 'device')
            or str(old.get('deviceId') or ''),
            'deviceName': _pick(raw, 'deviceName', 'device_name')
            or str(old.get('deviceName') or ''),
            'base': base or str(old.get('base') or ''),
            'name': _pick(raw, 'name') or str(old.get('name') or ''),
            'nickname': _pick(raw, 'nickname', 'displayName')
            or str(old.get('nickname') or ''),
            'email': _pick(raw, 'email') or str(old.get('email') or ''),
            'proxy': _pick(raw, 'proxy', 'egress') or str(old.get('proxy') or ''),
            'source': _pick(raw, 'source') or str(old.get('source') or 'manual'),
            'lastRefreshError': '',
            'loginState': '正常',
        }
        self._probes.pop(uid, None)
        self._credits.pop(uid, None)
        # 新 Cookie / 新设备可能对应新的上游会话，旧 run 缓存必须丢掉。
        client.forget_run({'uid': uid})
        return store.public_account(store.save_account(account))

    def delete_account(self, account_id: str) -> bool:
        uid = str(account_id or '')
        self._probes.pop(uid, None)
        self._credits.pop(uid, None)
        client.forget_run({'uid': uid})
        return store.delete_account(account_id)

    async def refresh_account(self, account_id: str, *,
                              force: bool = False) -> dict:
        """Cookie 探活（不是续期）：灵犀没有 refresh token。"""
        account = self._require(account_id)
        uid = str(account.get('uid') or '')
        if not self._cookie_of(account):
            raise PlatformError('WPS 灵犀账号缺少 Cookie', status=401,
                                code='auth_expired')
        out = await client.probe(
            account, proxy=self._proxy_for(account),
            timeout=runtime_settings.credits_timeout(self.name))
        state = str(out.get('state') or 'unknown')
        patch: dict[str, Any] = {
            'refreshedAt': store.iso_from_epoch(time.time()),
        }
        if out.get('base'):
            patch['base'] = str(out['base'])
        name = str(out.get('name') or '')
        if name and not account.get('nickname'):
            patch['nickname'] = name
        if state == 'invalid':
            patch['loginState'] = '登录已失效'
            patch['lastRefreshError'] = str(out.get('message') or 'Cookie 无效')[:200]
        elif state == 'ok':
            patch['loginState'] = '正常'
            patch['lastRefreshError'] = ''
        else:
            patch['lastRefreshError'] = str(out.get('message') or '')[:200]
        saved = store.patch_account(uid, patch)
        self._probes[uid] = (time.time()
                             + runtime_settings.credits_ttl(self.name), out)
        if state == 'invalid':
            raise PlatformError(str(out.get('message') or 'Cookie 无效'),
                                status=401, code='auth_expired')
        return store.public_account(saved)

    # ── 签到 / 余额 ───────────────────────────────────────
    async def checkin(self, account_id: str) -> dict:
        """每日签到：任务中心 `daily_check_in`（上游按自然日幂等）。"""
        account = self._require(account_id)
        uid = str(account.get('uid') or '')
        proxy = self._proxy_for(account)
        timeout = runtime_settings.credits_timeout(self.name)

        daily: dict | None = None
        try:
            task_list = await client.tasks(account, proxy=proxy, timeout=timeout)
            daily = next((t for t in task_list
                          if t.get('key') == 'daily_check_in'), None)
        except PlatformError:
            # 任务中心读不到不阻断签到本身：领取接口是独立的。
            daily = None

        today = client.beijing_date()
        date = today
        if daily:
            days = daily.get('days') or []
            hit = next((d for d in days if d.get('date') == today), None)
            if hit is not None:
                date = str(hit.get('date') or today)
            else:
                available = next((d for d in days
                                  if d.get('status') == 'available'), None)
                date = str(available.get('date') or today) if available else today

        out = await client.claim(account, 'daily_check_in', date,
                                 proxy=proxy, timeout=timeout)
        ok = bool(out.get('ok'))
        already = bool(out.get('already'))
        reward = int(round(_num(out.get('reward'))
                           or _num(daily.get('reward') if daily else 0) or 0))
        message = str(out.get('message') or '')

        try:
            store.patch_account(uid, {
                'checkin': {
                    'last_at': int(time.time()),
                    'last_result': 'already' if already else ('ok' if ok else 'error'),
                    'continuous_days': daily.get('streak') if daily else None,
                    'reward': reward,
                },
                'loginState': '正常' if ok else str(
                    account.get('loginState') or ''),
            })
        except (FileNotFoundError, ValueError, OSError):
            pass

        return {
            'ok': ok,
            'checked_in': ok,
            'already': already,
            'credits': 0,
            'earned_credit': 0 if already else reward,
            'message': message or ('今日已签到' if already else
                                   ('签到成功' if ok else '签到失败')),
            'uid': uid,
            'realm': store.REALM,
            'reward': reward,
            'continuous_days': daily.get('streak') if daily else None,
        }

    # ── 任务面板（任务中心 / 邀请有礼 / 兑换码）────────────
    def _task_lock(self, uid: str) -> asyncio.Lock:
        lock = self._task_locks.get(uid)
        if lock is None:
            lock = asyncio.Lock()
            self._task_locks[uid] = lock
        return lock

    @staticmethod
    def _task_item(task: dict) -> dict:
        """`client.parse_tasks` 的一条 → 前端通用任务条目。"""
        status = str(task.get('status') or '').strip()
        done = status in _TASK_DONE_STATUS
        claimable = status in _TASK_CLAIMABLE_STATUS
        reward = int(round(_num(task.get('reward')) or 0))
        return {
            'task_id': str(task.get('key') or ''),
            'task_type': str(task.get('type') or ''),
            'title': str(task.get('title') or ''),
            'sub_title': str(task.get('description') or ''),
            'reward_points': reward,
            'done': done,
            # 只有 claimable 才允许本端代领；incomplete 需要用户去客户端完成。
            'auto': claimable,
            'status': status,
        }

    @staticmethod
    def _checkin_view(account: dict) -> dict | None:
        """账号里缓存的签到记录 → 前端签到卡片（没有记录返回 None）。"""
        stored = account.get('checkin') if isinstance(
            account.get('checkin'), dict) else {}
        last_at = int(_num(stored.get('last_at')) or 0)
        if last_at <= 0:
            return None
        result = str(stored.get('last_result') or '')
        ok = result in ('ok', 'already')
        reward = int(round(_num(stored.get('reward')) or 0))
        balance = account.get('balance') if isinstance(
            account.get('balance'), dict) else {}
        remaining = _num(balance.get('remaining'))
        view: dict[str, Any] = {
            'ok': ok,
            'already': result == 'already',
            'message': '今日已签到' if result == 'already' else (
                '签到成功' if result == 'ok' else '签到失败'),
            'earned_credit': 0 if result == 'already' else reward,
            'earned_at': last_at,
            'continuous_days': stored.get('continuous_days'),
        }
        if remaining is not None:
            view['balance'] = remaining
        return view

    async def task_status(self, account_id: str) -> dict:
        """任务面板只读状态：任务中心 + 邀请有礼 + 缓存签到。

        四路并行、各自容错：任务中心或邀请接口单路失败时，面板仍显示其它
        分段，失败原因放进 `errors`。**不做任何写操作**（签到哪里由
        `run_task('checkin')` 显式触发）。
        """
        account = self._require(account_id)
        uid = str(account.get('uid') or '')
        proxy = self._proxy_for(account)
        timeout = runtime_settings.credits_timeout(self.name)
        errors: list[dict[str, Any]] = []

        async def read(endpoint: str, coro) -> Any:
            try:
                return await coro
            except PlatformError as exc:
                errors.append({'endpoint': endpoint, 'code': exc.code,
                               'message': exc.message})
                return {}

        task_list, activity, code, summary = await asyncio.gather(
            read('tasks', client.tasks(
                account, proxy=proxy, timeout=timeout)),
            read('referral/activity', client.referral_activity(
                account, proxy=proxy, timeout=timeout)),
            read('referral/code', client.referral_code(
                account, proxy=proxy, timeout=timeout)),
            read('referral/summary', client.referral_summary(
                account, proxy=proxy, timeout=timeout)),
        )
        raw_tasks = task_list if isinstance(task_list, list) else []
        activity = activity if isinstance(activity, dict) else {}
        code = code if isinstance(code, dict) else {}
        summary = summary if isinstance(summary, dict) else {}

        summary_error = next(
            (e for e in errors if e['endpoint'] == 'referral/summary'), None)
        if summary_error:
            activation: dict[str, Any] = {
                'error': summary_error['message'],
                'code': summary_error['code'],
            }
        else:
            activation = {
                'activated': summary.get('redeemed') is True,
                'raw': {'activity': activity, 'summary': summary},
            }

        invite_code = str(code.get('referral_code') or '').strip()
        success_count = int(_num(summary.get('success_count')) or 0)
        total_reward = int(round(_num(summary.get('total_reward')) or 0))
        limit = _num(summary.get('max_success_invites'))
        unlimited = summary.get('unlimited') is True
        max_uses = None
        if not unlimited and limit is not None and limit > 0:
            max_uses = int(limit)

        invite_codes: list[dict[str, Any]] = []
        if invite_code:
            entry: dict[str, Any] = {
                'inviteCode': invite_code,
                'status': 'active' if activity.get('campaign_status') == 'active'
                else str(activity.get('campaign_status') or ''),
            }
            # 只有拿到上限时才显示「已用 / 上限」，否则 0 会被读成「不能邀请」。
            if max_uses is not None:
                entry['usedCount'] = success_count
                entry['maxUses'] = max_uses
            invite_codes.append(entry)

        tasks = [self._task_item(item) for item in raw_tasks
                 if isinstance(item, dict) and item.get('key')
                 and item.get('key') != _DAILY_TASK_KEY]
        groups = [{'group_code': '', 'group_name': '', 'tasks': tasks}] \
            if tasks else []

        return {
            'uid': uid,
            'nickname': str(account.get('nickname') or ''),
            'realm': store.REALM,
            'activation': activation,
            'checkin': self._checkin_view(account),
            'invite_codes': invite_codes,
            'invite': {
                'invitation_code': invite_code,
                'invited_count': success_count,
                'total_points': total_reward,
            },
            'tasks': tasks,
            'task_groups': groups,
            'synced_at': int(time.time()),
            'errors': errors,
        }

    async def run_task(self, account_id: str, action: str, **params: Any) -> dict:
        """任务动作：`checkin` / `complete_task` / `bind_invite` / `redeem`。"""
        action = str(action or '').strip().lower()
        async with self._task_lock(str(account_id)):
            if action in ('checkin', 'sync'):
                return await self._task_checkin(account_id)
            if action in ('complete_task', 'claim_task', 'claim', 'task_claim'):
                return await self._claim_task(
                    account_id, str(params.get('task_id')
                                    or params.get('task_key') or ''))
            if action in ('bind_invite', 'invite', 'redeem_invite'):
                return await self._bind_invite(
                    account_id,
                    str(params.get('invite_code') or params.get('code') or ''))
            if action in ('redeem', 'redeem_code'):
                return await self._redeem(
                    account_id, str(params.get('code')
                                    or params.get('redeem_code') or ''))
        raise PlatformError(f'不支持的任务动作 {action!r}', status=400,
                            code='task_unsupported')

    async def _task_checkin(self, account_id: str) -> dict:
        """签到（复用 `checkin`）后刷新一次任务面板。"""
        result = await self.checkin(account_id)
        snapshot = await self.task_status(account_id)
        earned = int(_num(result.get('earned_credit')) or 0)
        checkin = dict(snapshot.get('checkin') or {})
        checkin.update({
            'ok': bool(result.get('ok')),
            'already': bool(result.get('already')),
            'message': str(result.get('message') or ''),
            'earned_credit': earned,
            'earned_this_run': earned,
            'continuous_days': result.get('continuous_days'),
        })
        snapshot.update({'ok': True, 'action': 'checkin', 'checkin': checkin})
        return snapshot

    async def _claim_task(self, account_id: str, task_key: str) -> dict:
        key = str(task_key or '').strip()
        if not key:
            raise PlatformError('缺少任务标识', status=400,
                                code='missing_task_key')
        account = self._require(account_id)
        # 只有每日签到按日期领取；一次性任务带上 `?date=` 没有意义。
        date = client.beijing_date() if key == _DAILY_TASK_KEY else ''
        out = await client.claim(
            account, key, date,
            proxy=self._proxy_for(account),
            timeout=runtime_settings.credits_timeout(self.name))
        already = bool(out.get('already'))
        if not out.get('ok') and not already:
            raise PlatformError(str(out.get('message') or '领取任务失败'),
                                status=400, code='task_claim_failed',
                                payload=out)
        reward = int(round(_num(out.get('reward')) or 0))
        self._credits.pop(str(account.get('uid') or ''), None)
        snapshot = await self.task_status(account_id)
        message = str(out.get('message') or '')
        snapshot.update({
            'ok': True,
            'action': 'complete_task',
            'task_id': key,
            'desc': message or (f'已领取 {reward} 智点' if reward else '已领取'),
            'done_count': 1,
            'already_count': 1 if already else 0,
            'fail_count': 0,
            'results': [{'task_id': key, 'title': key, 'ok': True,
                         'already': already}],
        })
        return snapshot

    async def _bind_invite(self, account_id: str, invite_code: str) -> dict:
        # 与 Web 活动页一致：邀请码可能被整段粘贴（「邀请码：AB12CD」），
        # 先去掉分隔符，只认其中的 6 位字母数字。
        raw = str(invite_code or '').strip()
        compact = re.sub(r'[^A-Za-z0-9]', '', raw)
        if re.fullmatch(r'[A-Za-z0-9]{6}', compact):
            invite = compact
        else:
            found = re.search(r'[A-Za-z0-9]{6}', raw)
            invite = found.group(0) if found else ''
        if not invite:
            raise PlatformError('邀请码是 6 位英文字母或数字',
                                status=400, code='invalid_invite_code')
        account = self._require(account_id)
        data = await client.bind_referral(
            account, invite, proxy=self._proxy_for(account),
            timeout=runtime_settings.credits_timeout(self.name))
        if data.get('bound') is not True:
            reason = str(data.get('reason') or '').strip()
            status, code = _INVITE_ERRORS.get(
                reason, (400, reason or 'invite_failed'))
            raise PlatformError(
                str(data.get('message') or '绑定邀请码失败'),
                status=status, code=code, payload=data)
        self._credits.pop(str(account.get('uid') or ''), None)
        snapshot = await self.task_status(account_id)
        snapshot.update({
            'ok': True,
            'action': 'bind_invite',
            'invite_code': invite,
            'desc': str(data.get('message') or '邀请关系已绑定'),
        })
        return snapshot

    async def _redeem(self, account_id: str, code: str) -> dict:
        value = str(code or '').strip()
        if not value:
            raise PlatformError('请填写兑换码', status=400,
                                code='missing_redeem_code')
        account = self._require(account_id)
        out = await client.redeem_gift_card(
            account, value, proxy=self._proxy_for(account),
            timeout=runtime_settings.credits_timeout(self.name))
        if not out.get('ok'):
            raise PlatformError(str(out.get('message') or '兑换失败'),
                                status=400, code='redeem_failed',
                                payload=out)
        reward = int(round(_num(out.get('reward')) or 0))
        self._credits.pop(str(account.get('uid') or ''), None)
        snapshot = await self.task_status(account_id)
        snapshot.update({
            'ok': True,
            'action': 'redeem',
            'desc': str(out.get('message') or '') or (
                f'已到账 {reward} 智点' if reward else '兑换成功'),
            'data': out.get('data') or {},
        })
        return snapshot

    async def usage(self, account_id: str) -> dict:
        """余额 / 用量快照（余额接口）。"""
        account = self._require(account_id)
        uid = str(account.get('uid') or '')
        out: dict[str, Any] = {
            'uid': uid,
            'realm': store.REALM,
            'session_usage': None,
            'entitlement': None,
            'errors': [],
        }
        try:
            data = await client.balance(
                account, proxy=self._proxy_for(account),
                timeout=runtime_settings.credits_timeout(self.name))
        except PlatformError as exc:
            out['errors'].append({'endpoint': 'balance', 'code': exc.code,
                                  'message': exc.message})
            return out
        out['session_usage'] = {
            'total': data['total'],
            'used': data.get('consumed'),
            'remaining': data['total'],
            'total_value': data.get('total_value'),
            'expiring': data.get('expiring'),
            'at': int(time.time()),
        }
        out['entitlement'] = {
            'quota_known': True,
            'remaining': data['total'],
        }
        return out

    async def credits(self, account_id: str) -> dict:
        """智点余额快照（默认 60 秒缓存，`platforms.lingxi.credits_ttl`）。"""
        account = self._require(account_id)
        uid = str(account.get('uid') or '')
        cached = self._credits.get(uid)
        if cached and cached[0] > time.time():
            return cached[1]

        data = await client.balance(
            account, proxy=self._proxy_for(account),
            timeout=runtime_settings.credits_timeout(self.name))
        now = time.time()
        expiries: list[dict] = []
        for batch in data.get('batches') or []:
            expire_at = batch.get('expire_at')
            if not isinstance(expire_at, (int, float)):
                continue
            seconds = int(expire_at // 1000)
            amount = float(batch.get('balance') or 0)
            if seconds <= now or amount <= 0:
                continue
            expiries.append({
                'amount': round(amount, 2),
                'expire_at': seconds,
                'label': str(batch.get('expire_time') or '赠送智点')[:120],
            })
        expiries.sort(key=lambda item: item['expire_at'])
        out = {
            'uid': uid,
            'realm': store.REALM,
            'credits': data['total'],
            'remain': data['total'],
            'limit': None,
            'used': data.get('consumed'),
            'unit': 'credits',
            'total_value': data.get('total_value'),
            'expiring': data.get('expiring'),
            'expires': expiries,
            'next_expire_at': expiries[0]['expire_at'] if expiries else None,
            'source': 'credits/balance',
        }
        self._credits[uid] = (time.time()
                              + runtime_settings.credits_ttl(self.name), out)
        try:
            store.patch_account(uid, {'balance': {
                'state': 'ok', 'at': int(time.time()), 'message': '',
                'remaining': data['total'],
                'total_value': data.get('total_value'),
            }})
        except (FileNotFoundError, ValueError, OSError):
            pass
        return out

    # ── 模型目录 ──────────────────────────────────────────
    def _cached_models(self) -> list[dict] | None:
        try:
            cached = db.get_setting(SETTING_MODELS, None)
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(cached, dict):
            return None
        if float(cached.get('fetched_at') or 0) \
                + runtime_settings.catalog_ttl(self.name) < time.time():
            return None
        models = cached.get('models')
        if not isinstance(models, list) or not models:
            return None
        return [m for m in models if isinstance(m, dict) and m.get('id')]

    def _save_models(self, models: list[dict]) -> None:
        try:
            db.set_setting(SETTING_MODELS, {'fetched_at': int(time.time()),
                                            'models': models})
        except Exception:  # noqa: BLE001
            pass

    def _implicit_live_allowed(self) -> bool:
        try:
            last = float(db.get_setting(SETTING_MODELS_ATTEMPT, 0) or 0)
        except Exception:  # noqa: BLE001
            return False
        return time.time() - last > runtime_settings.catalog_retry_ttl(self.name)

    @staticmethod
    def _note_implicit_attempt() -> None:
        try:
            db.set_setting(SETTING_MODELS_ATTEMPT, int(time.time()))
        except Exception:  # noqa: BLE001
            pass

    async def _live_models(self, *, timeout: float) -> list[dict]:
        """用一个可用账号拉一次上游目录；失败就换下一个。"""
        for account in store.list_accounts():
            if not self._cookie_of(account) or store.is_disabled(account):
                continue
            try:
                models = await client.list_plans(
                    account, proxy=self._proxy_for(account), timeout=timeout)
            except PlatformError:
                continue
            if models:
                return models
        return []

    async def list_models(self, *, refresh: bool = False) -> list[dict]:
        models = None if refresh else self._cached_models()
        if models is None and (refresh or self._implicit_live_allowed()):
            live = await self._live_models(
                timeout=45 if refresh
                else runtime_settings.catalog_timeout(self.name))
            self._note_implicit_attempt()
            if live:
                self._save_models(live)
                models = live
        if models is None:
            models = catalog.default_models()
        self._model_meta = {str(m.get('id') or '').strip().lower(): m
                            for m in models if m.get('id')}
        return [self._public_model(m) for m in models if m.get('id')]

    def _public_model(self, raw: dict) -> dict:
        return catalog.to_public(raw, self.name)

    def cached_model_ids(self) -> set[str] | None:
        """已缓存的对外模型 id（`lingxi/...`），只读不联网。"""
        models = self._cached_models() or catalog.default_models()
        if not models:
            return None
        return {self.public_model_id(str(m.get('id') or ''), store.REALM)
                for m in models if m.get('id')}

    def upstream_model(self, model: str) -> str:
        """成员名 → 上游 model key（剥前缀、`default` 回默认模型）。"""
        return C.upstream_model_id(model)

    # ── 账号调度参数 ──────────────────────────────────────
    def set_account_options(self, account_id: str, *, weight=None) -> dict:
        if weight is None:
            raise PlatformError('没有要修改的项', status=400,
                                code='nothing_to_update')
        try:
            return store.set_weight(account_id, weight)
        except FileNotFoundError as exc:
            raise PlatformError('账号不存在', status=404,
                                code='account_not_found') from exc
        except ValueError as exc:
            raise PlatformError(str(exc), status=400,
                                code='invalid_weight') from exc

    def set_account_disabled(self, account_id: str, disabled: bool,
                             reason: str = '') -> dict:
        try:
            return store.set_disabled(account_id, bool(disabled), reason)
        except FileNotFoundError as exc:
            raise PlatformError('账号不存在', status=404,
                                code='account_not_found') from exc
        except ValueError as exc:
            raise PlatformError(str(exc), status=400,
                                code='invalid_account_state') from exc

    def set_account_checkin_only(self, account_id: str, enabled: bool) -> dict:
        """只把账号从推理选号池里摘出去，签到照常。"""
        try:
            return store.set_checkin_only(account_id, bool(enabled))
        except FileNotFoundError as exc:
            raise PlatformError('账号不存在', status=404,
                                code='account_not_found') from exc
        except ValueError as exc:
            raise PlatformError(str(exc), status=400,
                                code='invalid_account_state') from exc

    def set_account_cooling(self, account_id: str, until: float,
                            reason: str = '') -> dict:
        try:
            return store.set_cooling(account_id, until, reason)
        except FileNotFoundError as exc:
            raise PlatformError('账号不存在', status=404,
                                code='account_not_found') from exc
        except ValueError as exc:
            raise PlatformError(str(exc), status=400,
                                code='invalid_cooling') from exc

    def clear_account_cooling(self, account_id: str) -> dict:
        try:
            return store.clear_cooling(account_id)
        except FileNotFoundError as exc:
            raise PlatformError('账号不存在', status=404,
                                code='account_not_found') from exc
        except ValueError as exc:
            raise PlatformError(str(exc), status=400,
                                code='invalid_cooling') from exc

    def set_account_proxy(self, account_id: str, proxy: str) -> dict:
        """给账号绑定出口线路（空串 = 跟随号池，`__direct__` = 强制直连）。"""
        try:
            return store.set_proxy(account_id, proxy)
        except FileNotFoundError as exc:
            raise PlatformError('账号不存在', status=404,
                                code='account_not_found') from exc
        except ValueError as exc:
            raise PlatformError(str(exc), status=400,
                                code='invalid_account_state') from exc

    def invalidate_limit(self) -> None:
        self._limiter.invalidate()

    # ── 选号 ──────────────────────────────────────────────
    def _candidates(self, affinity: str | None = None) -> list[dict]:
        accounts = [a for a in store.list_accounts()
                    if str(a.get('uid') or '').strip()
                    and self._cookie_of(a)
                    and not store.is_disabled(a)
                    and not store.is_checkin_only(a)
                    and not store.is_cooling(a)]
        if not accounts:
            return []

        def uid_of(account: dict) -> str:
            return str(account.get('uid') or '')

        accounts.sort(key=lambda a: self._last_used.get(uid_of(a), 0.0))

        if affinity:
            bound_uid = self._affinity.get(affinity)
            if bound_uid:
                bound = next((a for a in accounts if uid_of(a) == bound_uid), None)
                if bound is not None:
                    rest = [a for a in accounts if a is not bound]
                    rest.sort(key=lambda a: (-_account_weight(a),
                                             self._last_used.get(uid_of(a), 0.0)))
                    return [bound, *rest]
                self._affinity.unbind(affinity)

        total = sum(_account_weight(a) for a in accounts)
        first = max(accounts, key=lambda a: (
            self._swrr.get(uid_of(a), 0.0) + _account_weight(a)))
        first_uid = uid_of(first)
        self._swrr[first_uid] = (self._swrr.get(first_uid, 0.0)
                                 + _account_weight(first) - total)
        rest = [a for a in accounts if a is not first]
        rest.sort(key=lambda a: (-_account_weight(a),
                                 self._last_used.get(uid_of(a), 0.0)))
        if affinity:
            self._affinity.bind(affinity, first_uid)
        return [first, *rest]

    def _cool_down(self, uid: str, exc: PlatformError) -> bool:
        """按设置把出错的账号冻结一段时间；返回 True = 应换下一个账号。"""
        if exc.status == 402:
            seconds = QUOTA_COOLDOWN_SECONDS
        elif exc.status == 429:
            seconds = runtime_settings.cooldown_429(self.name) or 60
        elif exc.status >= 500:
            seconds = runtime_settings.cooldown_5xx(self.name)
        else:
            return False
        if seconds <= 0 or not uid:
            return False
        try:
            store.set_cooling(uid, time.time() + seconds, f'chat {exc.status}')
        except (FileNotFoundError, ValueError, OSError):
            return False
        return True

    # ── 对话 ──────────────────────────────────────────────
    async def chat(self, model: str, body: dict,
                   ctx: ChatContext | None = None) -> PlatformChat:
        return await self._chat_accounts(model, body)

    async def _chat_accounts(self, model: str, body: dict) -> PlatformChat:
        wrapper_model = f'{self.model_prefix}{model}'
        runtime = runtime_settings.load(self.name)
        affinity = affinity_key(body) if runtime['session_affinity'] else ''
        if affinity:
            self._affinity.set_ttl(runtime['session_affinity_ttl'])

        candidates = self._candidates(affinity)
        if not candidates:
            raise PlatformError('没有可用的 WPS 灵犀账号（请先导入网页 Cookie）',
                                status=503, code='no_account')

        last_error: PlatformError | None = None
        for account in candidates[:runtime['max_accounts_try']]:
            uid = str(account.get('uid') or '')
            lease = None
            release: Callable[[], None] = _noop
            try:
                proxy = self._proxy_for(account)
                lease = await self._limiter.acquire(uid)
                if lease is False:
                    lease = None
                    last_error = PlatformError(
                        f'灵犀账号并发已满（单账号上限 '
                        f'{self._limiter.limit()}）', status=429,
                        code='account_busy')
                    continue
                release = _lease_releaser(self._limiter, lease)
                result = await self._chat_once(
                    account, model, body, wrapper_model,
                    proxy=proxy, runtime=runtime)
            except PlatformError as exc:
                release()
                last_error = exc
                if affinity and (exc.status in (401, 403, 429)
                                 or exc.status >= 500):
                    self._affinity.unbind(affinity)
                # 401 是 Cookie 失效：冻了也不会自己好，只换号不冷却。
                if exc.status != 401 and self._cool_down(uid, exc):
                    continue
                if exc.status in (401, 403):
                    continue
                raise
            except BaseException:
                release()
                raise

            self._last_used[uid] = time.time()
            if affinity:
                self._affinity.bind(affinity, uid)
            if result.stream is None and result.raw_stream is None:
                release()
                return result
            inner = result.closer

            async def closer(inner=inner, release=release) -> None:
                try:
                    if inner is not None:
                        await inner()
                finally:
                    release()

            result.closer = closer
            return result

        if affinity:
            self._affinity.unbind(affinity)
        raise last_error or PlatformError('WPS 灵犀账号均不可用', status=503,
                                          code='no_account')

    # ── 每请求实扣积分（面板 credit_meter / TF_LINGXI_CREDIT_METER）──
    async def _meter_start(self, account: dict) -> _CreditMeter | None:
        """请求前准备计量凭据；`credit_meter` 为空时返回 None（旧行为）。

        `detail` 请求前零上游开销；`delta` 要查一次账户累计消耗当基线，并抢
        一把账号级锁——并发在途时分不清归属，后来的请求只计费不记账。
        基线拿不到就不占锁：这段窗口本来就记不了账，不必挡住别人。

        每次计量请求都会把账号的 `seq` 世代顶上去：在途窗口的探针发现世代
        变了就**主动放弃**（返回 False）。宁可两笔都不记，也不把两笔的消耗
        混成一笔记到别人头上。
        """
        mode = runtime_settings.credit_meter(self.name)
        if not mode:
            return None
        uid = str(account.get('uid') or '')
        meter = _CreditMeter(mode=str(mode), uid=uid,
                             request_id=requestctx.current())
        if meter.mode != 'delta':
            return meter
        seq = self._meter_seq.get(uid, 0) + 1
        self._meter_seq[uid] = seq
        meter.seq = seq
        if uid and uid in self._metering:
            # 同一账号还有在途窗口：这次只计费、不计账（分不清归属）。
            return meter
        token = object()
        if uid:
            self._metering[uid] = token
            meter.token = token
        try:
            meter.before = await client.account_consumed(
                account, proxy=self._proxy_for(account),
                timeout=runtime_settings.credits_timeout(self.name))
        except PlatformError:
            meter.before = None
        if meter.before is None:
            self._release_meter(uid, token)
            meter.token = None
        return meter

    def _release_meter(self, uid: str, token: object | None) -> None:
        """只释放自己那把锁（并发下 A 的收尾不能删掉 B 的锁）。"""
        if uid and token is not None and self._metering.get(uid) is token:
            self._metering.pop(uid, None)

    def _proxy_or_none(self, account: dict) -> str | None:
        try:
            return self._proxy_for(account)
        except PlatformError:
            return None

    def _detail_probe(self, account: dict, meter: _CreditMeter) -> settle.Probe:
        """延迟探测：按本次专用会话查上游流水。"""
        proxy = self._proxy_or_none(account)
        timeout = runtime_settings.credits_timeout(self.name)

        async def probe() -> float | None:
            rows = await client.session_expenses(
                account, meter.session_id, proxy=proxy, timeout=timeout)
            return settle.credit_of(rows, meter.group_id, meter.session_id)

        return probe

    def _delta_probe(self, account: dict, meter: _CreditMeter) -> settle.Probe:
        """延迟探测：账户累计消耗的前后差值。

        只在「这段窗口没有被更新的计量请求覆盖」时才算：`seq` 变了说明同一
        账号又发了新请求，继续相减会把别人的消耗记到这笔头上（宁少勿重）。
        """
        proxy = self._proxy_or_none(account)
        timeout = runtime_settings.credits_timeout(self.name)

        async def probe() -> float | None | bool:
            if self._meter_seq.get(meter.uid) != meter.seq:
                return False
            after = await client.account_consumed(
                account, proxy=proxy, timeout=timeout)
            # 查询途中也可能有新请求进来：它已经把 seq 顶掉的话，这个 after
            # 里就混进了别人的消耗，放弃比记错好。
            if self._meter_seq.get(meter.uid) != meter.seq:
                return False
            if after is None or meter.before is None or after < meter.before:
                return None
            delta = round(after - meter.before, 6)
            return delta if delta > 0 else None

        return probe

    async def _meter_finish(self, account: dict, meter: _CreditMeter | None, *,
                            failed: bool = False) -> None:
        """上报 completed（结算触发点），并注册本次实付积分的延迟补记。

        **必须扛得住取消**：客户端断开 / 网关收流时，请求作用域会被取消，
        而 `completed` 恰恰是上游结算扣费的唯一触发点——丢了它这次对话就
        白跑（上游不扣费，本端也拿不到流水）。所以这里把上报丢进一个
        **独立任务**：它挂在事件循环上，不受调用方取消的影响。
        """
        if meter is None:
            return
        try:
            # 先登记探针：它是纯本地动作，不能被上报失败 / 取消带跑——
            # 探针在，这次实付积分才有补记的机会。
            self._schedule_probe(account, meter)
            await self._report_completed(account, meter, failed=failed)
        except Exception:  # noqa: BLE001 —— 记账失败不能影响已经完成的对话
            logger.debug('lingxi: 实扣积分收尾失败', exc_info=True)
        finally:
            self._release_meter(str(account.get('uid') or ''), meter.token)

    def _schedule_probe(self, account: dict, meter: _CreditMeter) -> None:
        """注册本次实付积分的延迟探测（上游延迟归账，不在收尾里等）。"""
        if meter.mode == 'detail' and meter.session_id:
            key = f'{meter.request_id}|d|{meter.session_id}'
            settle.schedule(key, meter.request_id,
                            self._detail_probe(account, meter))
        elif meter.mode == 'delta' and meter.before is not None:
            key = f'{meter.request_id}|l|{meter.seq}'
            settle.schedule(key, meter.request_id,
                            self._delta_probe(account, meter))

    async def _report_completed(self, account: dict, meter: _CreditMeter,
                                *, failed: bool) -> None:
        """上报 `completed`；被取消时改用独立任务补发一次。"""
        try:
            await client.report_status(account, {
                'session_id': meter.session_id,
                'run_id': meter.run_id,
                'group_id': meter.group_id,
            }, 'completed', reason='failed' if failed else 'completed',
                proxy=self._proxy_or_none(account))
        except asyncio.CancelledError:
            # 客户端断开会取消这里：不补发就没有结算、这次对话白跑。
            # 独立任务不继承调用方的取消信号，能把这条上报送出去。
            task = asyncio.ensure_future(self._report_completed_late(
                account, meter, failed=failed))
            _BACKGROUND_TASKS.add(task)
            task.add_done_callback(_BACKGROUND_TASKS.discard)
            raise

    async def _report_completed_late(self, account: dict, meter: _CreditMeter,
                                     *, failed: bool) -> None:
        """取消之后补发的 `completed`（独立任务，不受原请求取消影响）。"""
        try:
            await client.report_status(account, {
                'session_id': meter.session_id,
                'run_id': meter.run_id,
                'group_id': meter.group_id,
            }, 'completed', reason='failed' if failed else 'completed',
                proxy=self._proxy_or_none(account))
        except Exception:  # noqa: BLE001 —— 补发失败只影响这一次的结算
            logger.debug('lingxi: 实扣积分补发 completed 失败', exc_info=True)

    def _meter_abort(self, account: dict, meter: _CreditMeter | None) -> None:
        """请求在拿到流之前就失败时，释放计量锁（run 由 open_chat 收尾）。"""
        if meter is not None:
            self._release_meter(str(account.get('uid') or ''), meter.token)

    async def _chat_once(self, account: dict, model: str, body: dict,
                         wrapper_model: str, *, proxy: str | None,
                         runtime: dict) -> PlatformChat:
        uid = str(account.get('uid') or '')
        if not self._cookie_of(account):
            raise PlatformError('WPS 灵犀账号缺少 Cookie', status=401,
                                code='auth_expired')

        try:
            payload, dropped = chatproto.build_body(body, model)
        except PlatformError:
            raise
        except ValueError as exc:
            raise PlatformError(str(exc), status=400,
                                code='bad_request') from exc
        if dropped:
            logger.info('lingxi: 丢弃 %s 段非文本 content（上游只接受字符串）',
                        dropped)

        meter = await self._meter_start(account)
        meter_ids: dict | None = (
            None if meter is None else {'mode': meter.mode})
        try:
            response = await client.open_chat(
                account, payload, model, proxy=proxy,
                idle_timeout=runtime['idle_timeout'], meter=meter_ids)
        except BaseException:
            self._meter_abort(account, meter)
            raise
        if meter is not None and meter_ids is not None:
            meter.session_id = str(meter_ids.get('session_id') or '')
            meter.run_id = str(meter_ids.get('run_id') or '')
            meter.group_id = str(meter_ids.get('group_id') or '')
        translator = chatproto.StreamTranslator(wrapper_model)
        usage = translator.usage
        closed = False

        async def _close() -> None:
            nonlocal closed
            if closed:
                return
            closed = True
            try:
                await response.aclose()
            except Exception:  # noqa: BLE001 —— 收尾失败不该影响记账
                pass

        # ── 非流式：把整条 SSE 读完再聚合 ──
        if not (isinstance(body, dict) and body.get('stream')):
            ok = False
            try:
                async for frame in client.iter_frames(response):
                    translator.feed(frame)
                if not translator.finished:
                    raise PlatformError(
                        '灵犀上游流在完成事件之前结束（响应被截断）', status=502,
                        code='truncated_stream')
                if not translator.text.strip() and not translator.reasoning \
                        and not translator.has_tool_calls:
                    raise PlatformError('灵犀没有返回内容', status=502,
                                        code='empty_response')
                completion = translator.completion()
                ok = True
                return PlatformChat(payload=completion, usage=usage,
                                    model=wrapper_model, account_hint=uid,
                                    realm=store.REALM)
            except chatproto.StreamError as exc:
                raise PlatformError(exc.message, status=400
                                    if exc.code == 'audit_blocked' else 502,
                                    code=exc.code) from exc
            finally:
                await _close()
                await self._meter_finish(account, meter, failed=not ok)

        # ── 流式：上游帧 → OpenAI chunk ──
        started = time.monotonic()
        chat = PlatformChat(usage=usage, model=wrapper_model,
                            account_hint=uid, realm=store.REALM)

        async def relay() -> AsyncIterator[dict]:
            failure: PlatformError | None = None
            try:
                for chunk in translator.start():
                    yield chunk
                async for frame in client.iter_frames(response):
                    for chunk in translator.feed(frame):
                        if chat.first_token_ms is None and (
                                chunk.get('choices')
                                and any(key != 'role' for key in
                                        (chunk['choices'][0].get('delta') or {}))):
                            chat.first_token_ms = int(
                                (time.monotonic() - started) * 1000)
                        yield chunk
                if not translator.finished:
                    raise PlatformError(
                        '灵犀上游流在完成事件之前结束（响应被截断）', status=502,
                        code='truncated_stream')
                if not translator.text.strip() and not translator.reasoning \
                        and not translator.has_tool_calls:
                    yield translator.text_chunk('(空响应)')
                for chunk in translator.finish():
                    yield chunk
            except chatproto.StreamError as exc:
                chat.error = exc.message
                failure = PlatformError(
                    exc.message,
                    status=400 if exc.code == 'audit_blocked' else 502,
                    code=exc.code)
            except PlatformError as exc:
                chat.error = exc.message
                failure = exc
            finally:
                await _close()
                await self._meter_finish(account, meter,
                                         failed=failure is not None)
            # 错误在收尾之后继续抛：`platforms/sse.openai_sse` 会补一帧 error
            # 并让网关记 502；只设 `chat.error` 不抛，客户端会看到一条正常结束
            # 的空流，日志里也还是 200。
            if failure is not None:
                raise failure

        chat.stream = relay()
        return chat


adapter = LingxiAdapter()