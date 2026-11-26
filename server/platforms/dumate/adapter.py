"""DuMate（百度搭子）平台适配器：把协议层与存储层组装成平台能力。

与 Loomy / Trae 一样，这里只负责「策略」，协议在 `client.py`：

  · 选哪个账号（权重 SWRR + 最近最少使用 + 会话粘性）；
  · 网页 cookie → Bearer token 的缓存与强刷；
  · 签到 / 积分快照；
  · 模型目录的缓存与降级；
  · 流式（原始字节透传）/ 非流式两条出口的最后一步。

DuMate 是**单区域**平台（百度搭子国内站），`realm_aware=False`，账号与模型
统一记 `realm='cn'`。

凭证是**网页 cookie**（必须含 `BDUSS`），没有 refresh token：cookie 失效后
只能重新登录。登录两条路都支持——面板里的百度 Passport 扫码
（`server/platforms/dumate/login.py`），以及手工粘贴整段 Cookie。
因此 `refresh_account` 做的是「用 cookie 换一次 token 并探活」，不是续期；
换不到就明确报 401。
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime
from typing import Any, AsyncIterator, Callable

from ... import config, db
from ...services.session_affinity import SessionAffinity, affinity_key
from .. import settings as runtime_settings
from ..base import ChatContext, PlatformAdapter, PlatformChat, PlatformError
from ..limiter import AccountLimiter
from . import catalog, client, constants as C, reasoning, store

# 模型目录缓存：与 Trae / Loomy 同构，存 `{fetched_at, models}`。
SETTING_MODELS = 'platforms.dumate.models'
SETTING_MODELS_ATTEMPT = 'platforms.dumate.models_attempt'

# 积分耗尽（上游 402）的冷却时长。签到窗口按天算，先冻 1 小时足够让批量
# 任务改投别的账号，也不至于把「明天签到后恢复」的账号冻死一整天。
QUOTA_COOLDOWN_SECONDS = 3600


# ── 小工具 ──────────────────────────────────────────────────
def _pick(raw: dict, *keys: str) -> str:
    for key in keys:
        value = raw.get(key)
        if value not in (None, ''):
            return str(value).strip()
    return ''


def _without_raw(value: Any) -> Any:
    """去掉协议层附带的 `raw`（仅供排障），避免任务缓存把账号文件撑大。"""
    if isinstance(value, list):
        return [_without_raw(item) for item in value]
    if isinstance(value, dict):
        return {key: _without_raw(item) for key, item in value.items()
                if key != 'raw'}
    return value


def _num(value: object) -> float | None:
    """数字字段可能是字符串 / None；认不出返回 None（**不是** 0）。"""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _int_or_zero(value: object) -> int:
    number = _num(value)
    return int(number) if number is not None else 0


def _today_login_bonus(packages: object) -> dict | None:
    """从额度包里找出**今天**那笔登录奖励（服务端每天 00:00 自动发放）。

    为什么不能只靠余额前后差：奖励通常在 00:00 就发了，手动签到只会看到
    `has_issued=true`，余额早已包含这笔，差值恒为 0 —— 于是「签到得了多少
    积分」永远是空的，积分收支里也没有收入。这里按 `source=login_bonus`
    与 `granted_at >= 今天零点` 认出那笔发放，金额用包的 `total`。
    """
    if not isinstance(packages, list):
        return None
    start_ms = int(db.day_start_ts() * 1000)
    for item in packages:
        if not isinstance(item, dict):
            continue
        if str(item.get('source') or '') != C.LOGIN_BONUS_SOURCE:
            continue
        granted = _num(item.get('granted_at'))
        total = _num(item.get('total'))
        if granted is None or total is None or total <= 0:
            continue
        if int(granted) >= start_ms:
            return item
    return None


def normalize_token_fields(body: dict) -> bool:
    """把 `max_completion_tokens` 收敛成唯一的 `max_tokens`，返回是否改写。

    上游（dumate-svc `/gateway/apis/v1`）只认 `max_tokens`：两个都发会被明文
    拒绝（400），只发 `max_completion_tokens` 则整字段被静默忽略。两值不等时
    取较大者——同一次请求里客户端意图本身不明，取大不会把用户要的长度砍短。
    """
    if not isinstance(body, dict):
        return False
    if body.get('max_completion_tokens') is None:
        return False
    mct = _num(body.get('max_completion_tokens'))
    mt = _num(body.get('max_tokens'))
    if mct is not None and (mt is None or mct > mt):
        body['max_tokens'] = int(mct) if mct.is_integer() else mct
    body.pop('max_completion_tokens', None)
    return True


def _account_weight(account: dict) -> float:
    """选号用的权重：缺省 / 非法 / <=0 → 默认 1，合法值钳到 0.1–100。"""
    value = _num(account.get('weight'))
    if value is None or value <= 0:
        return 1.0
    return min(store.WEIGHT_MAX, max(store.WEIGHT_MIN, value))


def _noop() -> None:
    """未占位时的空释放动作（失败路径复用同一段收尾）。"""


def _lease_releaser(limiter: AccountLimiter, lease) -> Callable[[], None]:
    """把一个已占到的名额包成**只释放一次**的闭包。"""
    released = False

    def release() -> None:
        nonlocal released
        if released:
            return
        released = True
        limiter.release(lease)

    return release


def _parse_expiry(value: object) -> int:
    """上游 `expired_at` 是 ISO 时间；解析失败返回 0（调用方按 50 分钟兜底）。"""
    text = str(value or '').strip()
    if not text:
        return 0
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return 0
    return int(parsed.timestamp())


def _scan_sse(pending: bytes, usage: dict) -> tuple[bytes, bool, str]:
    """扫描 SSE 字节片段：填 `usage`、判断是否见到正文、抓流内错误。

    返回 `(未处理完的残留字节, 本次是否见到正文 delta, 错误文案)`。
    **只读**：本层不参与转发，字节原样透传。
    """
    saw_content = False
    error = ''
    while b'\n' in pending:
        line, pending = pending.split(b'\n', 1)
        text = line.strip()
        if not text.startswith(b'data:'):
            continue
        payload = text[5:].strip()
        if not payload or payload == b'[DONE]':
            continue
        try:
            obj = json.loads(payload.decode('utf-8', 'replace'))
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        data = obj.get('usage')
        if isinstance(data, dict):
            usage.update(data)
        if obj.get('error'):
            error = _error_message(obj.get('error'))
        choices = obj.get('choices')
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                delta = choice.get('delta')
                if not isinstance(delta, dict):
                    delta = choice.get('message') if isinstance(choice.get('message'), dict) else {}
                content = delta.get('content')
                if content:
                    saw_content = True
    return pending, saw_content, error


def _error_message(error: object) -> str:
    if isinstance(error, dict):
        for key in ('message', 'msg', 'desc'):
            value = error.get(key)
            if value:
                return str(value)
        return '上游返回错误'
    return str(error or '上游返回错误')


class DumateAdapter(PlatformAdapter):
    name = 'dumate'
    label = 'DuMate（百度搭子）'
    # 每日签到（`POST /api/dumate/points/loginBonus`）。
    supports_checkin = True
    # 网页登录 = 百度 Passport 扫码：服务端取二维码，手机确认后换 cookie。
    # 手工粘贴 Cookie 仍然保留（扫码不可用时的兜底）。
    supports_web_login = True
    # 凭证就是整段网页 Cookie（`BDUSS` 等），面板允许直接粘贴。
    supports_cookie_import = True
    # 单区域平台（百度搭子国内站）。
    realm_aware = False

    def supports_tasks(self) -> bool:
        """DuMate 客户端里有成长任务 / 抽奖 / 邀请码。"""
        return True

    def __init__(self) -> None:
        # uid -> 账号级锁（token 强刷串行，避免并发把同一账号打两次）。
        self._locks: dict[str, asyncio.Lock] = {}
        # uid -> 任务写操作锁。**不能**与 `_locks` 合并：完成任务要先发一条
        # 对话，途中 `_token_for` 会拿 `_locks`，同一把锁不可重入会死锁。
        self._task_locks: dict[str, asyncio.Lock] = {}
        # uid -> 最近使用时刻（LRU 兜底排序）。
        self._last_used: dict[str, float] = {}
        # uid -> (过期时间, token)：纯运行态，不落盘（与参考实现同口径）。
        self._tokens: dict[str, tuple[float, str]] = {}
        # uid -> (过期时间, 积分结果)：积分是列表里的一列，TTL 内复用。
        self._credits: dict[str, tuple[float, dict]] = {}
        # 单账号并发闸门（`platforms.dumate.max_in_flight`，0 = 不限）。
        self._limiter = AccountLimiter(self.name)
        # 权重 SWRR 游标（uid -> current），重启后从头开始。
        self._swrr: dict[str, float] = {}
        # 会话粘性：上游 prompt cache 按账号隔离（见 services/session_affinity）。
        self._affinity = SessionAffinity()
        # outward 模型 id（小写）→ 目录条目。只用于 `max_tokens` 与类型判断，
        # 由 `list_models` 充填；为空时退回内置兜底。
        self._model_meta: dict[str, dict] = {}

    # ── 内部工具 ──────────────────────────────────────────
    def _lock(self, account_id: str) -> asyncio.Lock:
        lock = self._locks.get(account_id)
        if lock is None:
            lock = self._locks[account_id] = asyncio.Lock()
        return lock

    def _task_lock(self, account_id: str) -> asyncio.Lock:
        lock = self._task_locks.get(account_id)
        if lock is None:
            lock = self._task_locks[account_id] = asyncio.Lock()
        return lock

    def _require(self, account_id: object) -> dict:
        account = store.read_account(account_id)
        if not account:
            raise PlatformError('账号不存在', status=404, code='account_not_found')
        return account

    def _proxy_for(self, account: dict) -> str | None:
        """账号出口：绑定线路 → 代理池 → 全局代理 → 直连。"""
        try:
            return config.resolve_account_proxy(account)
        except ValueError as exc:
            raise PlatformError(str(exc), status=503,
                                code='proxy_invalid') from exc

    def _session_timeout(self) -> float:
        """换 token / 网页读接口的超时：不受对话超时（默认 300s）拖累。"""
        try:
            return float(max(8, min(30, runtime_settings.request_timeout(self.name))))
        except Exception:  # noqa: BLE001 —— 配置读不到就按内置值
            return C.TOKEN_TIMEOUT

    async def _token_for(self, account: dict, *, proxy: str | None,
                         force: bool = False) -> str:
        """取（或复用）模型网关 token；并发调用共享同一次刷新。"""
        uid = str(account.get('uid') or '')
        cookie = str(account.get('cookie') or '').strip()
        if not cookie:
            raise PlatformError('DuMate 账号缺少 Cookie', status=401,
                                code='login_expired')
        now = time.time()
        cached = self._tokens.get(uid)
        if cached and not force and cached[0] - C.TOKEN_SKEW_SECONDS > now:
            return cached[1]

        async with self._lock(uid):
            cached = self._tokens.get(uid)
            if cached and not force and cached[0] - C.TOKEN_SKEW_SECONDS > time.time():
                return cached[1]
            try:
                info = await client.fetch_token(
                    cookie, proxy=proxy, timeout=self._session_timeout())
            except PlatformError as exc:
                self._tokens.pop(uid, None)
                raise
            token = str(info.get('token') or '')
            if not token:
                self._tokens.pop(uid, None)
                raise PlatformError('DuMate 换 token 响应缺少 token', status=502,
                                    code='token_failed')
            expire = _parse_expiry(info.get('expired_at'))
            if expire <= 0:
                expire = int(time.time()) + C.TOKEN_FALLBACK_SECONDS
            self._tokens[uid] = (expire, token)
            return token

    def _drop_token(self, uid: str) -> None:
        self._tokens.pop(str(uid or ''), None)

    # ── 账号 ──────────────────────────────────────────────
    def list_accounts(self) -> list[dict]:
        return [store.public_account(a) for a in store.list_accounts()]

    def export_accounts(self) -> list[dict]:
        """导出含明文凭据的账号，仅供管理员导出接口调用。"""
        return store.list_accounts()

    def import_account(self, payload: dict) -> dict:
        """导入账号：粘贴网页 Cookie（`BDUSS` 必填）。

        接受 `{account: {...}}` 与扁平对象两种形状；`cookie` / `cookies` /
        `token` 都按整段 Cookie 处理，导入阶段不联网。
        """
        if not isinstance(payload, dict):
            raise PlatformError('账号必须是 JSON 对象', status=400,
                                code='invalid_account')
        raw = payload.get('account') if isinstance(payload.get('account'), dict) \
            else payload

        cookie = store.normalize_cookie(
            _pick(raw, 'cookie', 'cookies', 'cookieString', 'token', 'session'))
        if not cookie:
            raise PlatformError('需要提供网页 Cookie（含 BDUSS）', status=400,
                                code='invalid_account')
        try:
            uid = _pick(raw, 'uid') or store.derive_uid(cookie)
            uid = store.safe_id(uid)
        except ValueError as exc:
            raise PlatformError(str(exc), status=400,
                                code='invalid_account') from exc

        old = store.read_account(uid) or {}
        account = {
            **old,
            'uid': uid,
            'cookie': cookie,
            'name': _pick(raw, 'name') or str(old.get('name') or ''),
            'nickname': _pick(raw, 'nickname', 'displayName')
            or str(old.get('nickname') or ''),
            'proxy': _pick(raw, 'proxy', 'egress') or str(old.get('proxy') or ''),
            'proxy_session': _pick(raw, 'proxy_session', 'proxySession')
            or str(old.get('proxy_session') or ''),
            'source': _pick(raw, 'source') or str(old.get('source') or 'manual'),
            # 扫码登录可能只拿到 cookie、探活遇到 5xx：这时落盘但标「待验证」，
            # 让账号页能看出这份凭证还没真正换到 token。手工粘贴不带这两个字段，
            # 相当于一次新的凭证，旧状态要清掉。
            'loginState': _pick(raw, 'loginState', 'login_state'),
            'lastRefreshError': _pick(raw, 'lastRefreshError', 'last_refresh_error'),
        }
        self._credits.pop(uid, None)
        self._drop_token(uid)
        return store.public_account(store.save_account(account))

    def delete_account(self, account_id: str) -> bool:
        self._credits.pop(str(account_id or ''), None)
        self._drop_token(str(account_id or ''))
        return store.delete_account(account_id)

    async def refresh_account(self, account_id: str, *,
                              force: bool = False) -> dict:
        """用 Cookie 换一次 token 并读取用户信息（探活，不是续期）。"""
        account = self._require(account_id)
        uid = str(account.get('uid') or '')
        proxy = self._proxy_for(account)
        timeout = self._session_timeout()
        cookie = str(account.get('cookie') or '').strip()
        try:
            await self._token_for(account, proxy=proxy, force=True)
            try:
                info = await client.fetch_user_info(cookie, proxy=proxy,
                                                    timeout=timeout)
            except PlatformError:
                info = {}
        except PlatformError as exc:
            try:
                store.patch_account(uid, {
                    'lastRefreshError': exc.message[:200],
                    'loginState': '登录已失效' if exc.status == 401 else '异常',
                })
            except (FileNotFoundError, ValueError, OSError):
                pass
            raise

        patch: dict[str, Any] = {
            'refreshedAt': int(time.time()),
            'lastRefreshError': '',
            'loginState': '正常',
        }
        nickname = str(info.get('nickname') or '').strip()
        if nickname:
            patch['nickname'] = nickname
        self._credits.pop(uid, None)
        return store.public_account(store.patch_account(uid, patch))

    # ── 网页登录（百度 Passport 扫码）──────────────────────
    def login_start(self, *, realm: str | None = None) -> dict:
        from . import login as loginmod  # noqa: PLC0415 —— login 反向依赖 adapter
        return loginmod.start()

    def login_poll(self, login_id: str) -> dict:
        from . import login as loginmod  # noqa: PLC0415
        return loginmod.poll(login_id)

    def login_cancel(self, login_id: str) -> bool:
        from . import login as loginmod  # noqa: PLC0415
        return loginmod.cancel(login_id)

    # ── 任务 ──────────────────────────────────────────────
    async def checkin(self, account_id: str) -> dict:
        """每日签到：先查是否已签，未签再领（重复调用幂等）。"""
        account = self._require(account_id)
        uid = str(account.get('uid') or '')
        proxy = self._proxy_for(account)
        timeout = runtime_settings.credits_timeout(self.name)
        cookie = str(account.get('cookie') or '')

        before_snapshot: dict = {}
        before = None
        try:
            before_snapshot = await client.quota_overview(
                cookie, proxy=proxy, timeout=timeout)
            before = _num(before_snapshot.get('left'))
        except PlatformError:
            before_snapshot = {}
            before = None

        info = await client.login_bonus_info(cookie, proxy=proxy, timeout=timeout)
        already = bool(info.get('has_issued'))
        claimed = False
        message = '今日已签到' if already else ''
        if not already:
            try:
                await client.claim_login_bonus(cookie, proxy=proxy, timeout=timeout)
                claimed = True
                message = '签到成功'
            except PlatformError as exc:
                if exc.status == 401:
                    raise
                # 并发 / 重复领取：上游用业务错误表达，按「已签到」处理。
                if '已' in exc.message or '重复' in exc.message:
                    already = True
                    message = exc.message
                else:
                    raise

        after_snapshot: dict = {}
        after = None
        try:
            after_snapshot = await client.quota_overview(
                cookie, proxy=proxy, timeout=timeout)
            after = _num(after_snapshot.get('left'))
        except PlatformError:
            after_snapshot = {}
            after = None

        # 登录奖励在服务端**当天 00:00 已自动发放**，手动签到通常只会看到
        # `has_issued=true`；余额早已含这笔，前后差值恒为 0。这里也从额度包里
        # 认出今天那笔 `source=login_bonus` 的发放额，并用 `auto_grant_at`
        # 判重——反复点签到不能把同一笔奖励重复记成收入。
        prev_checkin = (account.get('checkin')
                        if isinstance(account.get('checkin'), dict) else {})
        auto_grant_at = _int_or_zero(prev_checkin.get('auto_grant_at'))
        grant = _today_login_bonus(
            after_snapshot.get('packages')
            if after_snapshot.get('packages') is not None
            else before_snapshot.get('packages'))
        grant_at = _int_or_zero(grant.get('granted_at')) if grant else 0

        earned = 0
        if claimed:
            if before is not None and after is not None and after > before:
                earned = int(round(after - before))
            elif grant:
                # 结算有延迟时余额还没变，退回用发放包的真实金额。
                earned = int(round(_num(grant.get('total')) or 0))
            if not grant_at:
                # 本次已经用前后差额记账；把「今日已记」标到此刻，防止结算后
                # 才出现的登录奖励包再被补记一遍。
                grant_at = int(time.time() * 1000)
        elif grant and grant_at > auto_grant_at:
            # 已签到：这次点击没有触发发放，但今天的自动奖励确实到账了。
            earned = int(round(_num(grant.get('total')) or 0))

        now = int(time.time())
        prev_earned = _int_or_zero(prev_checkin.get('earned_credit'))
        prev_earned_at = _int_or_zero(prev_checkin.get('earned_at'))
        if earned > 0:
            prev_earned = earned
            prev_earned_at = now
        self._credits.pop(uid, None)
        checkin = {
            'last_at': now,
            'last_result': message,
            'already': already,
            'total_times': info.get('total_times'),
            'total_points': info.get('total_points'),
            'sign_in_days': info.get('sign_in_days') or [],
            'month_points': info.get('month_points'),
            # 「上次真正到账的签到奖励」：再点一次已签到时仍保留旧值，
            # 不会把面板上的「签到获得」清成 0。
            'earned_credit': prev_earned,
            'earned_at': prev_earned_at or None,
            'auto_grant_at': grant_at or auto_grant_at or None,
        }
        patch: dict[str, Any] = {'checkin': checkin}
        if after is not None:
            patch['points'] = {'total': None, 'used': None, 'left': after,
                               'at': now}
        try:
            store.patch_account(uid, patch)
        except (FileNotFoundError, ValueError, OSError):
            pass

        return {
            'ok': True,
            'checked_in': True,
            'already': already,
            'credits': after if after is not None else (before or 0),
            # 本次动作的真实到账额（写签到记录 / 积分收支用）。
            'earned_credit': earned,
            # 上次真正到账额（界面展示用；repeat 点击不清零）。
            'last_earned': prev_earned,
            'earned_at': prev_earned_at or None,
            'message': message,
            'uid': uid,
            'realm': 'cn',
            'total_times': info.get('total_times'),
            'total_points': info.get('total_points'),
            'sign_in_days': info.get('sign_in_days') or [],
            'month_points': info.get('month_points'),
        }

    # ── 任务面板（成长任务 / 抽奖 / 邀请码）────────────────
    def _task_cache(self, account: dict) -> dict:
        cached = account.get('taskCache')
        return dict(cached) if isinstance(cached, dict) else {}

    def _write_task_cache(self, account_id: str, **fields: Any) -> None:
        """把任务面板的最新快照写进账号文件（失败不影响本次结果）。"""
        try:
            account = self._require(account_id)
            cache = self._task_cache(account)
            cache.update(_without_raw(fields))
            cache['syncedAt'] = int(time.time())
            store.patch_account(account_id, {'taskCache': cache})
        except (PlatformError, OSError, ValueError):
            pass

    @staticmethod
    async def _safe_read(coro) -> tuple[Any, PlatformError | None]:
        """任务面板是**尽力而为**：单个接口失败只记录，不拖垮整页。"""
        try:
            return await coro, None
        except PlatformError as exc:
            return None, exc
        except Exception as exc:  # noqa: BLE001 —— 网络层异常同样不该炸整页
            return None, PlatformError(str(exc) or '任务状态读取失败',
                                       status=502, code='task_read_failed')

    async def _task_snapshot(self, account: dict) -> dict:
        """并发读取成长任务 / 抽奖 / 邀请码，失败项回落到缓存。"""
        uid = str(account.get('uid') or '')
        proxy = self._proxy_for(account)
        timeout = self._session_timeout()
        cookie = str(account.get('cookie') or '')
        cached = self._task_cache(account)

        (tasks_live, tasks_err), (draw_live, draw_err), \
            (invite_live, invite_err), (count_live, count_err) = await asyncio.gather(
                self._safe_read(client.tasks(cookie, proxy=proxy, timeout=timeout)),
                self._safe_read(client.draw_status(cookie, proxy=proxy,
                                                   timeout=timeout)),
                self._safe_read(client.invitation_code(cookie, proxy=proxy,
                                                       timeout=timeout)),
                self._safe_read(client.invited_count(cookie, proxy=proxy,
                                                     timeout=timeout)),
            )

        errors: list[dict] = []
        for endpoint, exc in (('tasks', tasks_err), ('draw', draw_err),
                              ('invitation_code', invite_err),
                              ('invited_count', count_err)):
            if exc is not None:
                errors.append({'endpoint': endpoint, 'code': exc.code,
                               'message': exc.message})

        tasks_data = tasks_live if isinstance(tasks_live, dict) else {
            'groups': cached.get('taskGroups')
            if isinstance(cached.get('taskGroups'), list) else [],
            'tasks': cached.get('tasks')
            if isinstance(cached.get('tasks'), list) else [],
        }
        draw = draw_live if isinstance(draw_live, dict) else (
            cached.get('draw') if isinstance(cached.get('draw'), dict) else None)

        invite: dict[str, Any] = {}
        cached_invite = cached.get('invite') if isinstance(
            cached.get('invite'), dict) else {}
        invitation = invite_live if isinstance(invite_live, dict) else {}
        counts = count_live if isinstance(count_live, dict) else {}
        # 成功但返回空（例如账号还没生成邀请码）应视为权威结果，不能拿缓存
        # 里的旧码顶上；只有该路**失败**（None）时才回落缓存。
        code = str((invitation.get('invitation_code') if invite_live is not None
                    else cached_invite.get('invitation_code')) or '')
        created = (invitation.get('created_at') if invite_live is not None
                   else cached_invite.get('created_at'))
        invited = (counts.get('invited_count', 0) if count_live is not None
                   else cached_invite.get('invited_count'))
        points = (counts.get('total_points', 0) if count_live is not None
                  else cached_invite.get('total_points'))
        within = None
        if created:
            within = (time.time() - float(created)) <= 7 * 86400
        invite.update({
            'invitation_code': code,
            'type': str((invitation.get('type') if invite_live is not None
                         else cached_invite.get('type')) or ''),
            'created_at': int(created or 0),
            'invite_message': str(
                (invitation.get('invite_message') if invite_live is not None
                 else cached_invite.get('invite_message')) or ''),
            'invited_count': invited,
            'total_points': points,
            'within_7_days': within,
        })
        if invite_err is not None:
            invite['error'] = invite_err.message

        # 只有至少一路成功才刷新磁盘缓存；全失败时保留上一次快照。
        if any(source is not None for source in (tasks_live, draw_live,
                                                 invite_live, count_live)):
            self._write_task_cache(
                uid, tasks=tasks_data.get('tasks') or [],
                taskGroups=tasks_data.get('groups') or [],
                draw=draw, invite=invite)
            synced_at = int(time.time())
        else:
            synced_at = int(cached.get('syncedAt') or 0)

        codes: list[dict] = []
        if code:
            codes.append({
                'inviteCode': code,
                'status': 'active',
                'type': invite.get('type') or '',
                'createdAt': invite.get('created_at') or 0,
                'invitedCount': invited,
                'totalPoints': points,
            })
        # 签到也可能发生在账号页（批量签到），那时结果写在账号的 `checkin` /
        # `points` 上而不是 `taskCache`；面板照样要能显示上次签到。
        checkin = (cached.get('checkin')
                   if isinstance(cached.get('checkin'), dict) else None)
        if checkin is None and isinstance(account.get('checkin'), dict):
            last = account['checkin']
            points = (account.get('points')
                      if isinstance(account.get('points'), dict) else {})
            checkin = {
                'ok': True,
                'already': bool(last.get('already')),
                'balance': points.get('left'),
                'message': str(last.get('last_result') or ''),
                'last_at': last.get('last_at'),
                'earned_credit': last.get('earned_credit') or 0,
                'earned_at': last.get('earned_at'),
                'total_times': last.get('total_times'),
                'total_points': last.get('total_points'),
                'month_points': last.get('month_points'),
                'sign_in_days': last.get('sign_in_days') or [],
            }
        return {
            'uid': uid,
            'nickname': str(account.get('nickname') or ''),
            'realm': 'cn',
            'checkin': checkin,
            'invite_codes': codes,
            'invite': invite,
            'tasks': _without_raw(tasks_data.get('tasks') or []),
            'task_groups': _without_raw(tasks_data.get('groups') or []),
            'draw': draw,
            'errors': errors,
            'synced_at': synced_at,
        }

    async def task_status(self, account_id: str) -> dict:
        """任务面板的**只读**状态：成长任务 / 抽奖 / 邀请码 + 签到缓存。

        刻意不在这里做任何写操作（签到、抽奖、填邀请码都由 `run_task` 显式
        触发）；打开一个页面不该顺手替用户领奖。读失败时回落到上次缓存的
        快照，并把错误放进 `errors` 供界面提示。
        """
        return await self._task_snapshot(self._require(account_id))

    async def run_task(self, account_id: str, action: str, **params: Any) -> dict:
        """执行任务动作：`checkin` / `complete_task` / `draw` / `bind_invite` / `claim_prize`。"""
        action = str(action or '').strip().lower()
        # 同一账号的写操作串行：连点「抽奖」「完成任务」不该把上游打两次
        # （抽奖尤其——`request_id` 每次不同，上游不会替我们幂等）。
        async with self._task_lock(str(account_id)):
            if action in ('checkin', 'sync'):
                return await self._task_checkin(account_id)
            if action in ('complete_task', 'complete', 'task_complete'):
                return await self._complete_tasks(
                    account_id, str(params.get('task_id') or ''))
            if action in ('draw', 'lottery'):
                return await self._draw(account_id)
            if action in ('claim_prize', 'claim'):
                return await self._claim_prize(
                    account_id, str(params.get('draw_record_id') or ''),
                    str(params.get('contact') or ''))
            if action in ('bind_invite', 'invite', 'redeem', 'redeem_code'):
                return await self._bind_invite(
                    account_id,
                    str(params.get('invite_code') or params.get('code') or ''))
        raise PlatformError(f'不支持的任务动作 {action!r}', status=400,
                            code='task_unsupported')

    async def _task_checkin(self, account_id: str) -> dict:
        """签到（复用 `checkin`）后刷新一次任务面板状态。"""
        result = await self.checkin(account_id)
        account = self._require(account_id)
        uid = str(account.get('uid') or '')
        stored = (account.get('checkin')
                  if isinstance(account.get('checkin'), dict) else {})
        cached = self._task_cache(account)
        panel = {
            # `checkin()` 刚落盘了完整记录（含 earned_credit / month_points）；
            # 账号读失败时才退回 taskCache 里的旧值。
            **stored,
            'ok': bool(result.get('ok')),
            'already': bool(result.get('already')),
            'balance': result.get('credits'),
            'credits': result.get('credits'),
            'message': result.get('message') or '',
            'earned_credit': (result.get('last_earned')
                              or stored.get('earned_credit') or 0),
            'earned_this_run': result.get('earned_credit') or 0,
        }
        self._write_task_cache(uid, checkin=panel)

        snapshot = await self._task_snapshot(self._require(account_id))
        snapshot['checkin'] = panel
        snapshot['ok'] = True
        snapshot['action'] = 'checkin'
        return snapshot

    async def _send_task_query(self, account: dict, task: dict, *,
                               proxy: str | None) -> None:
        """QUERY_INPUT 任务：先用该账号发一条内容匹配的消息，再上报完成。"""
        query = str(task.get('query') or '').strip() \
            or str(task.get('title') or '').strip() or '你好'
        body = {
            'model': 'model-text',
            'messages': [{'role': 'user', 'content': query}],
            'max_tokens': 64,
        }
        token = await self._token_for(account, proxy=proxy)
        try:
            await client.chat(token, body, proxy=proxy,
                              timeout=self._session_timeout())
        except PlatformError as exc:
            if exc.status != 401:
                raise
            # token 刚好过期：强刷一次再发（与对话链路同口径）。
            token = await self._token_for(account, proxy=proxy, force=True)
            await client.chat(token, body, proxy=proxy,
                              timeout=self._session_timeout())

    @staticmethod
    def _task_already_granted(exc: PlatformError) -> bool:
        payload = exc.payload if isinstance(exc.payload, dict) else {}
        raw = payload.get('code')
        try:
            code = int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            code = 0
        return code == C.TASK_ALREADY_GRANTED_CODE or '已发放' in exc.message

    async def _complete_tasks(self, account_id: str, task_id: str = '') -> dict:
        """完成任务；`task_id` 为空时把当前所有可自动完成的任务跑一遍。"""
        account = self._require(account_id)
        uid = str(account.get('uid') or '')
        cookie = str(account.get('cookie') or '')
        proxy = self._proxy_for(account)
        timeout = self._session_timeout()

        data = await client.tasks(cookie, proxy=proxy, timeout=timeout)
        flat = [t for t in data.get('tasks') or [] if isinstance(t, dict)]
        if task_id:
            target = next((t for t in flat if t.get('task_id') == task_id), None)
            if target is None:
                raise PlatformError('任务不存在或已下线', status=404,
                                    code='task_not_found')
            targets = [target]
        else:
            targets = [t for t in flat
                       if not t.get('done') and t.get('auto')]

        results: list[dict] = []
        for task in targets:
            tid = str(task.get('task_id') or '')
            title = str(task.get('title') or '')
            if task.get('done'):
                results.append({'task_id': tid, 'title': title, 'ok': True,
                                'already': True})
                continue
            if not task.get('auto'):
                results.append({
                    'task_id': tid, 'title': title, 'ok': False, 'skipped': True,
                    'error': f"{task.get('task_type') or ''} 需在客户端完成",
                })
                continue
            try:
                if task.get('task_type') == 'QUERY_INPUT':
                    await self._send_task_query(account, task, proxy=proxy)
                payload = await client.complete_task(
                    cookie, tid, proxy=proxy, timeout=timeout)
                reward = payload.get('result') if isinstance(
                    payload.get('result'), dict) else {}
                results.append({
                    'task_id': tid, 'title': title, 'ok': True,
                    'reward_count': reward.get('reward_count'),
                    'reward_points': reward.get('reward_points'),
                })
            except PlatformError as exc:
                if self._task_already_granted(exc):
                    results.append({'task_id': tid, 'title': title, 'ok': True,
                                    'already': True})
                else:
                    results.append({'task_id': tid, 'title': title, 'ok': False,
                                    'error': exc.message, 'code': exc.code})

        refreshed, refresh_error = await self._safe_read(
            client.tasks(cookie, proxy=proxy, timeout=timeout))
        tasks_data = refreshed if isinstance(refreshed, dict) else data
        self._write_task_cache(
            uid, tasks=tasks_data.get('tasks') or [],
            taskGroups=tasks_data.get('groups') or [])

        done = sum(1 for item in results if item.get('ok'))
        return {
            'uid': uid,
            'nickname': str(account.get('nickname') or ''),
            'realm': 'cn',
            'ok': True,
            'action': 'complete_task',
            'task_id': task_id,
            'results': results,
            'done_count': done,
            'already_count': sum(1 for item in results if item.get('already')),
            'fail_count': sum(1 for item in results if not item.get('ok')),
            'tasks': _without_raw(tasks_data.get('tasks') or []),
            'task_groups': _without_raw(tasks_data.get('groups') or []),
            'synced_at': int(time.time()),
            'error': refresh_error.message if refresh_error else '',
        }

    async def _draw(self, account_id: str) -> dict:
        """抽一次奖。`request_id` 每次新生成，复用会被上游当成重复请求。"""
        account = self._require(account_id)
        uid = str(account.get('uid') or '')
        cookie = str(account.get('cookie') or '')
        proxy = self._proxy_for(account)
        timeout = self._session_timeout()

        status = await client.draw_status(cookie, proxy=proxy, timeout=timeout)
        remaining = status.get('remaining_draws')
        if isinstance(remaining, int) and remaining <= 0:
            raise PlatformError('没有可抽奖次数', status=409,
                                code='no_draws', payload=status)

        result = await client.draw(cookie, str(uuid.uuid4()), proxy=proxy,
                                   timeout=timeout)
        if not result.get('draw_record_id'):
            raise PlatformError('抽奖响应缺少 draw_record_id', status=502,
                                code='bad_draw_response', payload=result)
        prize = result.get('prize') if isinstance(result.get('prize'), dict) \
            else {}
        name = str(result.get('prize_name') or prize.get('prize_name')
                   or prize.get('name') or prize.get('title') or '')
        prize_type = str(result.get('prize_type')
                         or prize.get('prize_type') or '')
        return {
            'uid': uid,
            'nickname': str(account.get('nickname') or ''),
            'realm': 'cn',
            'ok': True,
            'action': 'draw',
            'draw_record_id': result.get('draw_record_id'),
            'remaining_draws': result.get('remaining_draws'),
            'prize': prize or None,
            'prize_name': name,
            'prize_type': prize_type,
            # 实物奖要用户填收货信息后调 `claim_prize`；积分 / 会员自动到账。
            'needs_claim': prize_type.upper() == 'PHYSICAL',
            'desc': name,
            'data': result.get('raw') or {},
        }

    async def _claim_prize(self, account_id: str, draw_record_id: str,
                           contact: str = '') -> dict:
        """领取实物奖品（积分 / 会员类奖品抽到即到账，不要调这个）。"""
        record = str(draw_record_id or '').strip()
        if not record:
            raise PlatformError('请提供抽奖记录 ID', status=400,
                                code='missing_draw_record_id')
        account = self._require(account_id)
        uid = str(account.get('uid') or '')
        result = await client.claim_prize(
            str(account.get('cookie') or ''), record,
            contact=str(contact or '').strip(),
            proxy=self._proxy_for(account), timeout=self._session_timeout())
        self._credits.pop(uid, None)
        return {
            'uid': uid,
            'nickname': str(account.get('nickname') or ''),
            'realm': 'cn',
            'ok': True,
            'action': 'claim_prize',
            'draw_record_id': record,
            'desc': '领取成功',
            'data': result.get('raw') or {},
        }

    async def _bind_invite(self, account_id: str, code: str) -> dict:
        """填写别人的邀请码（DuMate 的「邀请有礼」兑换）。"""
        value = str(code or '').strip()
        if not value:
            raise PlatformError('请填写邀请码', status=400,
                                code='missing_invite_code')
        account = self._require(account_id)
        uid = str(account.get('uid') or '')
        cookie = str(account.get('cookie') or '')
        proxy = self._proxy_for(account)
        timeout = self._session_timeout()

        # 先查自己的码：填自己的码上游也会拒，提前给出更明确的提示。
        try:
            own = await client.invitation_code(cookie, proxy=proxy,
                                               timeout=timeout)
        except PlatformError:
            own = {}
        if own.get('invitation_code') and own['invitation_code'] == value:
            raise PlatformError('不能填写自己的邀请码', status=400,
                                code='self_invite_code')

        result = await client.redeem_invitation_code(
            cookie, value, proxy=proxy, timeout=timeout)
        notice = result.get('notice')
        desc = str(result.get('message') or '')
        if not desc:
            if isinstance(notice, dict):
                desc = str(notice.get('content') or notice.get('message') or '')
            else:
                desc = str(notice or '')
        self._credits.pop(uid, None)
        return {
            'uid': uid,
            'nickname': str(account.get('nickname') or ''),
            'realm': 'cn',
            'ok': True,
            'action': 'bind_invite',
            'invite_code': value,
            'desc': desc,
            'data': result.get('raw') or {},
        }

    async def usage(self, account_id: str) -> dict:
        """额度 / 用量快照（用量弹窗展示）。"""
        account = self._require(account_id)
        uid = str(account.get('uid') or '')
        proxy = self._proxy_for(account)
        timeout = runtime_settings.credits_timeout(self.name)
        out: dict[str, Any] = {
            'uid': uid,
            'realm': 'cn',
            'session_usage': None,
            'entitlement': None,
            'errors': [],
        }
        try:
            snapshot = await client.quota_overview(
                str(account.get('cookie') or ''), proxy=proxy, timeout=timeout)
            out['session_usage'] = {
                'total': snapshot.get('total'),
                'used': snapshot.get('used'),
                'left': snapshot.get('left'),
                'packages': snapshot.get('packages') or [],
            }
            out['entitlement'] = {
                'subscribed': bool(snapshot.get('subscribed')),
                'throttled': bool(snapshot.get('throttled')),
            }
        except PlatformError as exc:
            out['errors'].append({'endpoint': 'quota_overview',
                                  'code': exc.code, 'message': exc.message})
        await self._add_consumption(account, out, proxy=proxy, timeout=timeout)
        return out

    async def _add_consumption(self, account: dict, out: dict, *,
                               proxy: str | None, timeout: float) -> None:
        """补一段「对话消耗」：今日 / 近 7 天各查一次积分消耗记录。

        上游没有「本次请求扣多少」的字段，真实扣费只体现在 `/points/records/usage`
        的 `consumedPoints`（按会话记账，计费规则在上游）。两次查询分别对应
        「今天」和「近 7 天」两个口径，失败只记进 `errors`，不影响余额显示。
        """
        cookie = str(account.get('cookie') or '')
        now = int(time.time())
        today = db.day_start_ts()
        try:
            day = await client.usage_records(
                cookie, start_at=today, end_at=now, limit=1,
                proxy=proxy, timeout=timeout)
            week = await client.usage_records(
                cookie, start_at=today - 6 * 86400, end_at=now, limit=20,
                proxy=proxy, timeout=timeout)
        except PlatformError as exc:
            out['errors'].append({'endpoint': 'usage_records',
                                  'code': exc.code, 'message': exc.message})
            return

        def amount(value: object) -> float:
            return abs(_num(value) or 0.0)

        ledger = runtime_settings.usage_ledger(self.name)
        out['consumed'] = {
            'today': amount(day.get('consumed_points')),
            'today_records': int(_num(day.get('total_count')) or 0),
            'week': amount(week.get('consumed_points')),
            'week_records': int(_num(week.get('total_count')) or 0),
            'recent': _without_raw(week.get('list') or []),
            'ledger': ledger,
            'ledger_days': (runtime_settings.usage_sync_days(self.name)
                            if ledger else 0),
        }
        if ledger:
            # 展示不依赖同步结果：失败只影响「积分收支」的补账，不影响弹窗。
            synced = await self.sync_usage_ledger(str(account.get('uid') or ''))
            for failure in synced.get('errors') or []:
                out['errors'].append({
                    'endpoint': 'usage_ledger',
                    'code': str(failure.get('code') or 'ledger_failed'),
                    'message': str(failure.get('message') or ''),
                })

    async def _usage_window_records(self, account: dict, *, days: int,
                                    proxy: str | None,
                                    timeout: float) -> list[dict]:
        """拉取最近 N 天的对话扣费明细（自动翻页，去掉 `raw`）。

        上游一条记录没有稳定 id，同步只能靠窗口 + 指纹去重；因此这里尽量把
        窗口内的 `list` 取全（`totalCount` 是窗口总数，`limit` 给 100）。翻页
        上限 20 页是防御：上游若忽略 `page` 反复返回同一页，不能无限打下去。
        """
        now = int(time.time())
        start = db.day_start_ts(
            db.day_of(now - max(0, int(days) - 1) * 86400))
        cookie = str(account.get('cookie') or '')
        rows: list[dict] = []
        total = 0
        page = 1
        while page <= 20:
            data = await client.usage_records(
                cookie, start_at=start, end_at=now, page=page, limit=100,
                proxy=proxy, timeout=timeout)
            chunk = data.get('list') or []
            rows.extend(chunk)
            total = max(total, int(_num(data.get('total_count')) or 0))
            if not chunk:
                break
            # `totalCount` 是权威总数：上游若把 limit 压得比 100 小，仅凭
            # `len(chunk) < limit` 会提前收手。没有 total 时才按页大小判断。
            if total and len(rows) >= total:
                break
            if not total and len(chunk) < 100:
                break
            page += 1
        return _without_raw(rows)

    async def sync_usage_ledger(self, account_id: str | None = None) -> dict:
        """把对话扣费同步进积分收支账本（幂等，只在开关打开时执行）。

        `account_id` 为空表示全部账号（每日签到任务调用）；否则只同步一个账号
        （用量弹窗打开时调用）。单个账号失败只记进 `errors`，不拖垮其余账号。
        """
        if not runtime_settings.usage_ledger(self.name):
            return {'enabled': False, 'days': 0, 'accounts': 0,
                    'records': 0, 'errors': []}
        days = runtime_settings.usage_sync_days(self.name)
        accounts = ([self._require(account_id)] if account_id
                    else store.list_accounts())
        timeout = runtime_settings.credits_timeout(self.name)
        stored = 0
        errors: list[dict] = []
        for account in accounts:
            uid = str(account.get('uid') or '')
            if not uid:
                continue
            try:
                rows = await self._usage_window_records(
                    account, days=days, proxy=self._proxy_for(account),
                    timeout=timeout)
                stored += db.add_dumate_usage_records(uid, rows)
            except PlatformError as exc:
                errors.append({'uid': uid, 'code': exc.code,
                               'message': exc.message})
            except Exception as exc:  # noqa: BLE001 —— 账本是旁路，不能拖垮弹窗
                errors.append({'uid': uid, 'code': 'ledger_failed',
                               'message': str(exc)})
        return {'enabled': True, 'days': days, 'accounts': len(accounts),
                'records': stored, 'errors': errors}

    async def credits(self, account_id: str) -> dict:
        """积分余额快照（默认 60 秒缓存，`platforms.dumate.credits_ttl`）。"""
        account = self._require(account_id)
        uid = str(account.get('uid') or '')
        cached = self._credits.get(uid)
        if cached and cached[0] > time.time():
            return cached[1]

        proxy = self._proxy_for(account)
        timeout = runtime_settings.credits_timeout(self.name)
        snapshot = await client.quota_overview(
            str(account.get('cookie') or ''), proxy=proxy, timeout=timeout)
        total = _num(snapshot.get('total'))
        used = _num(snapshot.get('used'))
        left = _num(snapshot.get('left'))
        if left is None and total is None:
            raise PlatformError('取不到积分余额', status=502,
                                code='credits_unavailable')

        remaining = left if left is not None else total
        expires = []
        for package in snapshot.get('packages') or []:
            if not isinstance(package, dict):
                continue
            # 上游给的是**毫秒**；`PlatformCreditsExpiry.expire_at` 的契约是
            # **epoch 秒**，差 1000 倍会让倒计时显示成几十万天。
            expire_ms = _num(package.get('points_expire_at')) \
                or _num(package.get('expire_at'))
            rest = _num(package.get('left'))
            if not expire_ms or rest is None or rest <= 0:
                continue
            expires.append({
                'amount': rest,
                'expire_at': int(expire_ms // 1000),
                'label': str(package.get('source')
                             or package.get('product') or ''),
            })
        expires.sort(key=lambda item: item['expire_at'])
        out = {
            'uid': uid,
            'realm': 'cn',
            'credits': int(remaining) if float(remaining).is_integer()
            else remaining,
            'remain': remaining,
            'limit': total,
            'used': used,
            'unit': 'points',
            'expires': expires,
            'next_expire_at': min((x['expire_at'] for x in expires), default=None),
            'source': 'quota_overview',
            'packages': snapshot.get('packages') or [],
            'subscribed': bool(snapshot.get('subscribed')),
            'throttled': bool(snapshot.get('throttled')),
        }
        self._credits[uid] = (time.time()
                              + runtime_settings.credits_ttl(self.name), out)
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
        models = catalog.normalize(cached.get('models'))
        return models or None

    @staticmethod
    def _save_models(models: list[dict]) -> None:
        try:
            db.set_setting(SETTING_MODELS, {'fetched_at': int(time.time()),
                                            'models': models})
        except Exception:  # noqa: BLE001 —— 缓存写不进去不影响本次返回
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

    async def list_models(self, *, refresh: bool = False) -> list[dict]:
        models = None if refresh else self._cached_models()
        if models is None and (refresh or self._implicit_live_allowed()):
            live = catalog.read_desktop_models()
            self._note_implicit_attempt()
            if live:
                models = catalog.normalize(live)
                self._save_models(models)
        if models is None:
            models = catalog.default_models()
        self._model_meta = {str(m.get('id') or '').strip().lower(): m
                            for m in models if m.get('id')}
        return [self._public_model(m) for m in models if m.get('id')]

    def _public_model(self, raw: dict) -> dict:
        model_id = str(raw.get('id') or '')
        modalities = raw.get('modalities')
        if not isinstance(modalities, list) or not modalities:
            modalities = ['text']
        return {
            'id': self.public_model_id(model_id, 'cn'),
            'object': 'model',
            'created': 0,
            'owned_by': self.name,
            'name': str(raw.get('name') or model_id),
            'platform': self.name,
            'realm': 'cn',
            'context_window': int(_num(raw.get('context_length')) or 0),
            'max_tokens': int(_num(raw.get('max_tokens')) or 0),
            'supports_reasoning': bool(raw.get('reasoning')),
            'efforts': reasoning.efforts_of(model_id),
            'modalities': {'input': [str(x) for x in modalities]},
            'model_name': model_id,
            'tool_call': bool(raw.get('tool_call', True)),
            'attachment': bool(raw.get('attachment')),
            'credit_ratio': None,
        }

    def cached_model_ids(self) -> set[str] | None:
        """已缓存的对外模型 id（`dumate/...`），只读不联网。"""
        models = self._cached_models() or catalog.default_models()
        if not models:
            return None
        return {self.public_model_id(str(m.get('id') or ''), 'cn')
                for m in models if m.get('id')}

    def _output_cap(self, model: str) -> int:
        target = str(model or '').strip()
        if target in C.OUTPUT_CAPS:
            return int(C.OUTPUT_CAPS[target])
        entry = self._model_meta.get(target.lower())
        if entry:
            value = _num(entry.get('max_tokens'))
            return int(value) if value and value > 0 else 0
        for item in catalog.default_models():
            if str(item.get('id') or '').strip().lower() == target.lower():
                value = _num(item.get('max_tokens'))
                return int(value) if value and value > 0 else 0
        return 0

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
        """只把账号从推理选号池里摘出去，签到与平台任务照常。"""
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
        """本次请求的账号顺序：权重 SWRR 决定首选，权重相同按 LRU。"""
        accounts = [a for a in store.list_accounts()
                    if str(a.get('uid') or '').strip()
                    and str(a.get('cookie') or '').strip()
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
            seconds = runtime_settings.cooldown_429(self.name)
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
        """一次对话调用；并发闸门占到流结束（见 `_chat_accounts`）。"""
        return await self._chat_accounts(model, body)

    async def _chat_accounts(self, model: str, body: dict) -> PlatformChat:
        wrapper_model = f'{self.model_prefix}{model}'
        runtime = runtime_settings.load(self.name)
        affinity = affinity_key(body) if runtime['session_affinity'] else ''
        if affinity:
            self._affinity.set_ttl(runtime['session_affinity_ttl'])

        entry = self._model_meta.get(str(model or '').strip().lower())
        if entry is not None and str(entry.get('type') or 'chat').lower() != 'chat':
            raise PlatformError(
                f'模型 {model} 不是对话模型，不支持 /v1/chat/completions',
                status=400, code='unsupported_model')

        candidates = self._candidates(affinity)
        if not candidates:
            raise PlatformError('没有可用的 DuMate 账号（请先导入账号）',
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
                        f'DuMate 账号并发已满（单账号上限 '
                        f'{self._limiter.limit()}）', status=429,
                        code='account_busy')
                    continue
                release = _lease_releaser(self._limiter, lease)
                try:
                    result = await self._chat_once(
                        account, model, body, wrapper_model,
                        proxy=proxy, runtime=runtime)
                except PlatformError as exc:
                    if exc.status != 401:
                        raise
                    # token 可能是自然过期：强制刷一次再试；仍 401 说明 cookie
                    # 本身失效，换下一个账号，不在这里冷却（凭证问题不是
                    # 上游故障，冻了也不会自己好）。
                    self._drop_token(uid)
                    result = await self._chat_once(
                        account, model, body, wrapper_model,
                        proxy=proxy, runtime=runtime)
            except PlatformError as exc:
                release()
                last_error = exc
                if affinity and (exc.status in (401, 403, 429)
                                 or exc.status >= 500):
                    self._affinity.unbind(affinity)
                if self._cool_down(uid, exc):
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
        raise last_error or PlatformError('DuMate 账号均不可用', status=503,
                                          code='no_account')

    async def _chat_once(self, account: dict, model: str, body: dict,
                         wrapper_model: str, *, proxy: str | None,
                         runtime: dict) -> PlatformChat:
        uid = str(account.get('uid') or '')
        if not str(account.get('cookie') or '').strip():
            raise PlatformError('DuMate 账号缺少 Cookie', status=401,
                                code='login_expired')
        token = await self._token_for(account, proxy=proxy)

        payload = self.chat_body(model, body)
        # 上游只认 `max_tokens` 一个键：
        #   · 两个都发 → 直接 400（`setting 'max_tokens' and
        #     'max_completion_tokens' at the same time is not supported`），
        #     客户端整轮失败；
        #   · 只发 `max_completion_tokens`（OpenAI 现行写法）→ 不报错，但该
        #     字段被静默忽略，预算退回默认值，用户设的长度失效。
        # 先收敛成单一的 `max_tokens`（两值不等取较大者，避免砍短用户要的
        # 长度），再做按模型上限的钳制——顺序不能反，否则钳的是被忽略的那个值。
        normalize_token_fields(payload)
        # 思考档位：只对同一后端的白名单模型生效（见 `reasoning.py`）。
        reasoning.apply(payload, model)
        # 上游按真实模型限制 max_tokens；超过真值会整轮 400，钳到实测上限。
        cap = self._output_cap(model)
        if cap > 0:
            value = _num(payload.get('max_tokens'))
            if value is not None and value > cap:
                payload['max_tokens'] = cap

        if not payload.get('stream'):
            data = await client.chat(token, payload, proxy=proxy,
                                     timeout=runtime['request_timeout'])
            usage = data.get('usage') if isinstance(data.get('usage'), dict) else {}
            return PlatformChat(payload=data, usage=usage, model=wrapper_model,
                                account_hint=uid, realm='cn')

        started = time.monotonic()
        response = await client.open_chat(
            token, payload, proxy=proxy,
            idle_timeout=runtime['idle_timeout'])
        usage: dict = {}
        chat = PlatformChat(usage=usage, model=wrapper_model,
                            account_hint=uid, realm='cn')
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

        async def relay() -> AsyncIterator[bytes]:
            """原样透传上游字节，同时扫描 usage / 首字 / 流内错误。"""
            pending = b''
            state = {'error': ''}
            try:
                async for chunk in response.aiter_bytes():
                    if not chunk:
                        continue
                    yield chunk
                    pending, saw_content, error = _scan_sse(pending + chunk, usage)
                    if error:
                        state['error'] = error
                    if saw_content and chat.first_token_ms is None:
                        chat.first_token_ms = int(
                            (time.monotonic() - started) * 1000)
            finally:
                if state['error']:
                    chat.error = state['error']
                await _close()

        chat.raw_stream = relay()
        return chat


adapter = DumateAdapter()
