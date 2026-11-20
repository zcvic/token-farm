"""Loomy（讯飞星火 Loomy）平台适配器：把协议层与存储层组装成平台能力。

与 Trae / Qoder 一样，这里只负责「策略」，协议在 `client.py`：

  · 选哪个账号（权重 SWRR + 最近最少使用 + 会话粘性）；
  · 会话失效时用保存的手机号 / 密码重新登录；
  · 模型目录的缓存与兜底；
  · 流式（原始字节透传）/ 非流式两条出口的最后一步。

Loomy 是**单区域**平台（讯飞星火国内站），没有国内版 / 国际版之分，所以
`realm_aware=False`，账号与模型统一记 `realm='cn'`。

上游协议的一个实测特性：思考类模型在小 `max_tokens` 下会把预算全花在
reasoning 上、正文为空。官方客户端因此**始终**把输出上限设成该模型的最大
值（参考实现 loomy2api 同样如此）。这里沿用该口径，见 `_max_output_tokens`。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, AsyncIterator, Callable

from ... import config, db
from ...services import credits as creditsvc
from ...services.session_affinity import SessionAffinity, affinity_key
from .. import settings as runtime_settings
from ..base import ChatContext, PlatformAdapter, PlatformChat, PlatformError
from ..limiter import AccountLimiter
from . import client, constants as C, desktop, store

# 模型目录缓存：与 Trae 同构，存 `{fetched_at, models}`。
SETTING_MODELS = 'platforms.loomy.models'
SETTING_MODELS_ATTEMPT = 'platforms.loomy.models_attempt'


# ── 小工具 ──────────────────────────────────────────────────
def _pick(raw: dict, *keys: str) -> str:
    for key in keys:
        value = raw.get(key)
        if value not in (None, ''):
            return str(value).strip()
    return ''


def _num(value: object) -> float | None:
    """数字字段可能是字符串 / None；认不出返回 None（**不是** 0）。"""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


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


def _quota_fields(data: object) -> dict:
    """`points/records` 的 `data` → 统一口径的余额字段。"""
    data = data if isinstance(data, dict) else {}
    return {
        'balance': data.get('balance'),
        'daily_balance': data.get('dailyBalance'),
        'available': data.get('availableBalance'),
        'total': data.get('totalBalance'),
        'expiring': data.get('expiringBalance'),
        'daily_quota': data.get('dailyQuota'),
        'daily_consumed': data.get('dailyConsumed'),
        'cycle': data.get('dailyCycleDate'),
    }


def _points_of(data: object) -> float | None:
    """主余额：可用 > 总余额 > 当前余额。

    **不读** `currentBalance`：那是签到响应的字段，与 `points/records` 的
    `availableBalance` 不是同一口径，两者相减会算出假的「本次收入」。
    """
    data = data if isinstance(data, dict) else {}
    for key in ('availableBalance', 'totalBalance', 'balance'):
        value = _num(data.get(key))
        if value is not None:
            return value
    return None


def _multiplier_of(item: dict) -> float | None:
    """模型倍率：字段缺失 / 为 0 时从名称里取**最后**一个 `x数字`。

    为什么取最后一个：型号本身就带数字（「Spark-X2.6 …(x0.0)」取第一个会
    读成 2.6，实际倍率是末尾的 0.0）。
    """
    value = _num(item.get('multiplier'))
    if value:
        return value
    name = str(item.get('name') or '')
    found = re.findall(r'(?:x|×)([0-9]+(?:\.[0-9]+)?)', name, re.I)
    if found:
        return float(found[-1])
    return value


def _apply_points_credit(usage: dict) -> None:
    """把上游的 `points_consumed` 归一成网关认的 `usage.credit`。

    网关的 `_usage_credit` 只读 `credit` / `credits`；Loomy 的真实扣费在
    `points_consumed` 里。不转换的话，「实扣积分」永远是空的。
    """
    if not isinstance(usage, dict):
        return
    points = _num(usage.get('points_consumed'))
    if points is not None and points >= 0:
        usage['credit'] = points


def _error_message(error: object) -> str:
    if isinstance(error, dict):
        for key in ('message', 'msg', 'desc'):
            value = error.get(key)
            if value:
                return str(value)
        return '上游返回错误'
    return str(error or '上游返回错误')


def _wants_no_think(payload: dict) -> bool:
    """客户端是否**显式**要求关闭思考。

    认三种写法（与参考实现同口径）：`reasoning_effort="none"`、
    `enable_thinking=false`、`chat_template_kwargs.enable_thinking=false`。
    没传就当作没要求——思考类模型的默认行为不该被我们擅自改掉。
    """
    effort = str(payload.get('reasoning_effort') or '').strip().lower()
    if effort == 'none':
        return True
    if payload.get('enable_thinking') is False:
        return True
    kwargs = payload.get('chat_template_kwargs')
    return isinstance(kwargs, dict) and kwargs.get('enable_thinking') is False


def _no_think_messages(payload: dict) -> list | None:
    """「关闭思考」→ 首条 user 消息的 `/no_think` 前缀。

    上游没有开关字段（`reasoning_effort` / `enable_thinking` 传过去会被
    静默忽略），只认正文里的 `/no_think`，所以只能这样翻译。

    返回**新的** `messages`（不修改调用方传进来的请求体），不需要改写时返回
    None。已经带前缀的消息不重复加。
    """
    if not _wants_no_think(payload):
        return None
    messages = payload.get('messages')
    if not isinstance(messages, list):
        return None
    out: list = []
    changed = False
    for message in messages:
        if not changed and isinstance(message, dict) \
                and message.get('role') == 'user':
            content = message.get('content')
            if isinstance(content, str):
                if not content.lstrip().startswith('/no_think'):
                    message = {**message, 'content': '/no_think ' + content}
                    changed = True
            elif isinstance(content, list):
                # 多模态：前缀加在第一个文本片段上，图片等片段原样保留。
                parts: list = []
                handled = False
                for part in content:
                    if not handled and isinstance(part, dict) \
                            and part.get('type') == 'text' \
                            and isinstance(part.get('text'), str):
                        text = part['text']
                        handled = True
                        if not text.lstrip().startswith('/no_think'):
                            part = {**part, 'text': '/no_think ' + text}
                            changed = True
                    parts.append(part)
                if changed:
                    message = {**message, 'content': parts}
        out.append(message)
    return out if changed else None


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
            _apply_points_credit(usage)
        if obj.get('error'):
            error = _error_message(obj.get('error'))
        choices = obj.get('choices')
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                delta = choice.get('delta') or choice.get('message') or {}
                if isinstance(delta, dict) and (
                    delta.get('content') or delta.get('reasoning_content')
                ):
                    saw_content = True
    return pending, saw_content, error


class LoomyAdapter(PlatformAdapter):
    name = 'loomy'
    label = 'Loomy'
    supports_checkin = True
    # 网页登录 = 微信开放平台「网站应用」扫码（Loomy 唯一的跳转授权链路）。
    # 主路径由服务端取码 + 长轮询，连不上微信时退化成「打开授权页 + 粘回调」。
    supports_web_login = True
    # 单区域平台（讯飞星火国内站）。
    realm_aware = False

    def supports_tasks(self) -> bool:
        """Loomy 客户端里有「签到 / 绑定邀请码 / 兑换兑换码」这一组任务。"""
        return True

    def __init__(self) -> None:
        # uid -> 账号级锁（重新登录串行，避免并发把同一账号打两次）。
        self._locks: dict[str, asyncio.Lock] = {}
        # uid -> 最近使用时刻（LRU 兜底排序）。
        self._last_used: dict[str, float] = {}
        # uid -> (过期时间, 积分结果)：积分是列表里的一列，TTL 内复用。
        self._credits: dict[str, tuple[float, dict]] = {}
        # 单账号并发闸门（`platforms.loomy.max_in_flight`，0 = 不限）。
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

    async def _relogin(self, account: dict) -> dict:
        """用保存的手机号 / 密码重新登录并落盘。

        并发去重：拿到锁后若账号文件里的 session 已经变了，说明别的协程刚
        刷过，直接复用，不再打一次上游。
        """
        uid = str(account.get('uid') or '')
        if not uid:
            raise PlatformError('账号缺少 uid', status=400, code='invalid_account')
        async with self._lock(uid):
            fresh = store.read_account(uid) or account
            old_session = str(account.get('session') or '')
            current = str(fresh.get('session') or '')
            if current and current != old_session:
                return fresh
            phone = str(fresh.get('phone') or '').strip()
            password = str(fresh.get('password') or '')
            if not phone or not password:
                raise PlatformError(
                    'Loomy 没有 refresh token：该账号只保存了会话，失效后只能重新导入',
                    status=401, code='no_password')
            proxy = self._proxy_for(fresh)
            try:
                info = await client.login_by_password(
                    phone, password, proxy=proxy,
                    timeout=runtime_settings.request_timeout(self.name))
            except PlatformError as exc:
                try:
                    store.save_account({**fresh,
                                        'lastRefreshError': exc.message[:200]})
                except Exception:  # noqa: BLE001 —— 落盘失败不掩盖真正的登录错误
                    pass
                raise
            now = int(time.time())
            saved = store.save_account({
                **fresh,
                'session': info.get('session') or '',
                'userid': info.get('userid') or fresh.get('userid') or '',
                'phone': info.get('phone') or phone,
                'nickname': info.get('nickname') or fresh.get('nickname') or '',
                'obtainedAt': now,
                'expireAt': int(info.get('expire_at') or 0)
                or now + C.SESSION_EXPIRE_SECONDS,
                'lastRefreshError': '',
            })
            self._credits.pop(uid, None)
            return saved

    async def _ensure_session(self, account: dict) -> dict:
        """确保有会话：有就直接用；没有但有密码就重新登录。"""
        if str(account.get('session') or '').strip():
            return account
        return await self._relogin(account)

    # ── 账号 ──────────────────────────────────────────────
    def list_accounts(self) -> list[dict]:
        return [store.public_account(a) for a in store.list_accounts()]

    def import_account(self, payload: dict) -> dict:
        """导入账号：手机号 + 密码（服务端登录）或直接粘 session。

        两种形状都接受：`{account: {...}}` 与扁平对象。
        """
        if not isinstance(payload, dict):
            raise PlatformError('账号必须是 JSON 对象', status=400,
                                code='invalid_account')
        raw = payload.get('account') if isinstance(payload.get('account'), dict) \
            else payload

        phone = _pick(raw, 'phone', 'loginid', 'loginId', 'mobile', 'username')
        password = str(raw.get('password') or '')
        session = _pick(raw, 'session', 'token', 'accessToken', 'access_token')
        userid = _pick(raw, 'userid', 'userId', 'user_id', 'uid')
        nickname = _pick(raw, 'nickname', 'name')
        proxy = _pick(raw, 'proxy', 'egress')
        source = _pick(raw, 'source')
        now = int(time.time())

        # 重导入时沿用已绑定的线路与会话名：登录这一步也要从同一条出口走。
        # 否则代理池里带 `{account}` 的线路会在导入时先按 'acct' 建一次粘性
        # 会话，与后续对话用的出口不是同一个 IP。
        previous: dict = {}
        for key in (userid, phone):
            if not key:
                continue
            try:
                previous = store.read_account(key) or previous
            except ValueError:  # uid 形状不合法（手机号之外的值）
                continue
        route_hint = {
            'uid': _pick(raw, 'uid') or userid or phone,
            'nickname': nickname or str(previous.get('nickname') or ''),
            'proxy': proxy or str(previous.get('proxy') or ''),
            'proxy_session': _pick(raw, 'proxy_session', 'proxySession')
            or str(previous.get('proxy_session') or ''),
        }

        if not session:
            if not phone or not password:
                raise PlatformError(
                    '需要手机号 + 密码，或直接提供一个 session',
                    status=400, code='invalid_account')
            info = client.login_by_password_sync(
                phone, password, proxy=self._proxy_for(route_hint),
                timeout=runtime_settings.request_timeout(self.name))
            session = str(info.get('session') or '')
            userid = userid or str(info.get('userid') or '')
            phone = str(info.get('phone') or phone)
            nickname = nickname or str(info.get('nickname') or '')
            obtained_at = now
            expire_at = int(info.get('expire_at') or 0) \
                or now + C.SESSION_EXPIRE_SECONDS
        else:
            obtained_at = int(raw.get('obtainedAt') or 0) or now
            expire_at = int(raw.get('expireAt') or 0) \
                or obtained_at + C.SESSION_EXPIRE_SECONDS
            # 只给了 session 时尽量补全昵称 / userid；补不到不影响导入。
            if not userid or not nickname:
                try:
                    info = client.query_base_info_sync(
                        session, proxy=self._proxy_for(route_hint),
                        timeout=runtime_settings.request_timeout(self.name))
                    userid = userid or _pick(info, 'userid', 'userId', 'user_id')
                    nickname = nickname or _pick(info, 'nickname', 'name')
                    phone = phone or _pick(info, 'phone', 'loginid')
                except PlatformError:
                    pass

        try:
            uid = _pick(raw, 'uid') or store.derive_uid(userid, phone, session)
            uid = store.safe_id(uid)
        except ValueError as exc:
            raise PlatformError(str(exc), status=400,
                                code='invalid_account') from exc

        old = store.read_account(uid) or previous
        account = {
            **old,
            'uid': uid,
            'session': session,
            # 密码明文落盘：没有 refresh token，只能用密码续期。
            'password': password or old.get('password') or '',
            'phone': phone or old.get('phone') or '',
            'userid': userid or old.get('userid') or '',
            'nickname': nickname or old.get('nickname') or '',
            'proxy': proxy or old.get('proxy') or '',
            # 代理池线路里的 `{account}` 会话名：存下来，改昵称不会换出口 IP。
            'proxy_session': route_hint['proxy_session']
            or old.get('proxy_session') or '',
            'source': source or old.get('source') or 'manual',
            'obtainedAt': obtained_at,
            'expireAt': expire_at,
            'lastRefreshError': '',
        }
        self._credits.pop(uid, None)
        return store.public_account(store.save_account(account))

    def import_from_desktop(self, *, realm: str | None = None) -> dict:
        """一键导入本机 Loomy 客户端里已登录的会话。

        单个文件失败不影响其他文件：一台机器上可能装了多份客户端，其中一份
        写坏了，其余几份仍然应该能导进来。

        `realm` 只为符合路由的统一签名（Loomy 是单区域平台，忽略）。
        """
        imported: list[dict] = []
        failed: list[dict] = []
        for account in desktop.read_accounts():
            try:
                imported.append(self.import_account(account))
            except (PlatformError, ValueError) as exc:
                failed.append({
                    'uid': str(account.get('phone') or account.get('userid') or ''),
                    'error': getattr(exc, 'message', str(exc)),
                })
        return {
            'platform': self.name,
            'sources': [str(item['path']) for item in desktop.storage_paths()],
            'imported': imported,
            'failed': failed,
        }

    def delete_account(self, account_id: str) -> bool:
        self._credits.pop(str(account_id or ''), None)
        return store.delete_account(account_id)

    # ── 网页登录（微信扫码 / 手机验证码）──────────────────────
    def login_start(self, *, realm: str | None = None,
                    mode: str | None = None) -> dict:
        """发起一次登录，返回 `{login_id, url, qr, expires_in}`。

        `realm` 只为符合路由的统一签名（Loomy 是单区域平台，忽略）；
        `mode='sms'` 时只建会话，等待前端调用 `login_send_code`。
        """
        from . import login as loginmod  # noqa: PLC0415 —— login 反向依赖 adapter
        return loginmod.start(mode or 'qr')

    def login_poll(self, login_id: str) -> dict:
        from . import login as loginmod  # noqa: PLC0415
        return loginmod.poll(login_id)

    def login_cancel(self, login_id: str) -> bool:
        from . import login as loginmod  # noqa: PLC0415
        return loginmod.cancel(login_id)

    def login_submit_callback(self, login_id: str, callback: str) -> dict:
        """扫不到二维码时（服务端连不上微信）粘回调链接完成登录。"""
        from . import login as loginmod  # noqa: PLC0415
        return loginmod.submit_callback(login_id, callback)

    def login_send_code(self, login_id: str, phone: str) -> dict:
        """发短信验证码：验证码登录，或微信 `bind=0` 时的补绑。"""
        from . import login as loginmod  # noqa: PLC0415
        return loginmod.send_code(login_id, phone)

    def login_submit_code(self, login_id: str, code: str) -> dict:
        """提交短信验证码，成功后落盘账号。"""
        from . import login as loginmod  # noqa: PLC0415
        return loginmod.submit_code(login_id, code)

    async def refresh_account(self, account_id: str, *,
                              force: bool = False) -> dict:
        """重新登录。Loomy 没有 refresh token，有密码才有「刷新」这回事。

        `force` 不参与判断：本地没有可靠的过期时间（登录响应不带），任何一次
        显式刷新都直接换一个新会话。
        """
        account = self._require(account_id)
        if not account.get('password') or not account.get('phone'):
            raise PlatformError(
                'Loomy 没有 refresh token：该账号只保存了会话，失效后只能重新导入',
                status=400, code='no_password')
        return store.public_account(await self._relogin(account))

    # ── 任务 ──────────────────────────────────────────────
    async def checkin(self, account_id: str) -> dict:
        """每日签到：上游 `POST /points/first-login`（重复调用幂等）。"""
        account = self._require(account_id)
        uid = str(account.get('uid') or '')
        account = await self._ensure_session(account)
        proxy = self._proxy_for(account)
        timeout = runtime_settings.credits_timeout(self.name)
        session = str(account.get('session') or '')

        before = None
        try:
            snapshot = await client.fetch_points(session, proxy=proxy,
                                                 timeout=timeout, page_size=1)
            before = _points_of(snapshot.get('data'))
        except PlatformError:
            before = None

        payload = await client.first_login(session, proxy=proxy, timeout=timeout)
        if client.login_expired(payload):
            raise PlatformError(
                client._error_text(payload, '登录已失效'), status=401,
                code='login_expired', payload=payload)

        data = payload.get('data') if isinstance(payload.get('data'), dict) else {}
        code = payload.get('code')
        ok = code in (None, '', 0, '0', '000000', 200, '200')
        already = bool(data.get('alreadyProcessed'))

        after = None
        try:
            # 签到响应只给 currentBalance 一族；再查一次余额，字段才与 before
            # 同口径（`availableBalance`），相减得到的收入才可信。
            snapshot = await client.fetch_points(session, proxy=proxy,
                                                 timeout=timeout, page_size=1)
            after = _points_of(snapshot.get('data'))
        except PlatformError:
            after = None

        earned = 0
        if ok and not already and before is not None and after is not None \
                and after > before:
            earned = int(round(after - before))

        self._credits.pop(uid, None)
        # 顺手更新「最近一次登记的余额」：保底积分（`platforms.loomy.min_credits`）
        # 的选号判断读的就是这份快照；签到是每天都会发生的余额变化，不在这里
        # 登记的话，只有用户手动打开账号页刷新积分后保底才会看到新值。
        credits_balance = after if after is not None else before
        if credits_balance is not None:
            creditsvc.record_platform_balance(self.name, uid, credits_balance)
        description = str(payload.get('desc') or payload.get('message') or '')
        if not description:
            description = '今日已签到' if already else ('签到成功' if ok else '签到失败')

        return {
            'ok': ok,
            'checked_in': ok,
            'already': already,
            'credits': after if after is not None else (before or 0),
            'earned_credit': earned,
            'message': description,
            'uid': uid,
            'realm': 'cn',
            # 平台专属明细（前端忽略未知字段，排障用）
            'balance': data.get('currentBalance'),
            'permanent': data.get('permanentBalance'),
            'daily': data.get('dailyBalance'),
            'daily_quota': data.get('dailyQuota'),
            # 自己名下的邀请码只在这个响应里下发，任务页要展示就必须带出来。
            'invite_codes': data.get('invitationCodes') or [],
        }

    # ── 任务面板（激活状态 / 邀请码 / 兑换码）──────────────
    async def task_status(self, account_id: str) -> dict:
        """任务面板的**只读**状态：激活情况 + 上次同步的签到 / 邀请码缓存。

        刻意不在这里调 `points/first-login`：那是一次真实的签到写操作，
        打开一个页面不该顺手替用户领奖。签到由 `run_task('checkin')` 显式触发。
        """
        account = self._require(account_id)
        uid = str(account.get('uid') or '')
        cached = account.get('taskCache') if isinstance(
            account.get('taskCache'), dict) else {}

        activation: dict[str, Any]
        try:
            activation = self._activation_view(await self._query_activation(account))
        except PlatformError as exc:
            activation = {'error': exc.message, 'code': exc.code}

        checkin = cached.get('checkin') if isinstance(
            cached.get('checkin'), dict) else None
        codes = cached.get('inviteCodes')
        return {
            'uid': uid,
            'nickname': str(account.get('nickname') or ''),
            'realm': 'cn',
            'activation': activation,
            'checkin': checkin,
            'invite_codes': [c for c in codes if isinstance(c, dict)]
            if isinstance(codes, list) else [],
            'synced_at': int(cached.get('syncedAt') or 0),
        }

    async def run_task(self, account_id: str, action: str, **params) -> dict:
        """执行任务动作：`checkin`（签到 + 同步邀请码）/ `bind_invite` / `redeem`。"""
        action = str(action or '').strip().lower()
        if action in ('checkin', 'sync'):
            return await self._task_checkin(account_id)
        if action in ('bind_invite', 'invite'):
            return await self._bind_invite(
                account_id, str(params.get('invite_code') or ''))
        if action in ('redeem', 'redeem_code'):
            return await self._redeem(
                account_id, str(params.get('code') or ''))
        raise PlatformError(f'不支持的任务动作 {action!r}', status=400,
                            code='task_unsupported')

    def _activation_view(self, payload: dict) -> dict:
        """`/points/activation` → 统一形状（同时保留原始 data 便于排障）。"""
        data = payload.get('data') if isinstance(payload.get('data'), dict) else {}
        return {
            'activated': data.get('activated') is True,
            'applied_invitation_code': str(
                data.get('appliedInvitationCode') or ''),
            'raw': data,
        }

    async def _query_activation(self, account: dict) -> dict:
        account = await self._ensure_session(account)
        payload = await client.query_activation(
            str(account.get('session') or ''),
            proxy=self._proxy_for(account),
            timeout=runtime_settings.credits_timeout(self.name))
        if client.login_expired(payload):
            raise PlatformError(client._error_text(payload, '登录已失效'),
                                status=401, code='login_expired',
                                payload=payload)
        return payload

    async def _task_checkin(self, account_id: str) -> dict:
        """签到（复用 `checkin`）+ 拉一次激活状态，并把结果缓存进账号文件。"""
        account = self._require(account_id)
        result = await self.checkin(account_id)
        activation: dict[str, Any]
        try:
            activation = self._activation_view(
                await self._query_activation(account))
        except PlatformError as exc:
            activation = {'error': exc.message, 'code': exc.code}

        cache = {
            'syncedAt': int(time.time()),
            'checkin': {
                'ok': bool(result.get('ok')),
                'already': bool(result.get('already')),
                'balance': result.get('balance'),
                'permanent': result.get('permanent'),
                'daily': result.get('daily'),
                'daily_quota': result.get('daily_quota'),
                'message': result.get('message') or '',
            },
            'inviteCodes': [
                item for item in (result.get('invite_codes') or [])
                if isinstance(item, dict)
            ],
        }
        try:
            store.save_account({**self._require(account_id), 'taskCache': cache})
        except Exception:  # noqa: BLE001 —— 缓存写不进去不影响本次结果
            pass

        return {
            'uid': str(account.get('uid') or ''),
            'nickname': str(account.get('nickname') or ''),
            'realm': 'cn',
            'checkin': {
                **cache['checkin'],
                'earned_credit': result.get('earned_credit') or 0,
                'credits': result.get('credits'),
            },
            'activation': activation,
            'invite_codes': cache['inviteCodes'],
            'synced_at': cache['syncedAt'],
        }

    async def _bind_invite(self, account_id: str, invite_code: str) -> dict:
        invite = str(invite_code or '').strip()
        if not invite:
            raise PlatformError('请填写邀请码', status=400,
                                code='missing_invite_code')
        account = self._require(account_id)
        current = await self._query_activation(account)
        view = self._activation_view(current)
        if view['activated']:
            raise PlatformError(
                '该账号已绑定邀请码'
                + (f'：{view["applied_invitation_code"]}'
                   if view['applied_invitation_code'] else ''),
                status=409, code='already_activated', payload=current)

        account = await self._ensure_session(account)
        payload = await client.bind_invite_code(
            str(account.get('session') or ''), invite,
            device_id=str(account.get('deviceId') or ''),
            proxy=self._proxy_for(account),
            timeout=runtime_settings.credits_timeout(self.name))
        if client.login_expired(payload):
            raise PlatformError(client._error_text(payload, '登录已失效'),
                                status=401, code='login_expired',
                                payload=payload)
        code = payload.get('code')
        ok = code in (None, '', 0, '0', '000000', 200, '200')
        desc = str(payload.get('desc') or payload.get('message') or '')
        if not ok:
            raise PlatformError(desc or '绑定邀请码失败', status=400,
                                code='invite_failed', payload=payload)

        # 绑定成功后余额 / 奖励可能变化，顺手清掉积分缓存。
        self._credits.pop(str(account.get('uid') or ''), None)
        try:
            activation = self._activation_view(
                await self._query_activation(account))
        except PlatformError:
            activation = {'activated': True,
                          'applied_invitation_code': invite}
        return {
            'ok': True,
            'uid': str(account.get('uid') or ''),
            'account': str(account.get('uid') or ''),
            'desc': desc,
            'invite_code': invite,
            'activation': activation,
            'data': payload.get('data') or {},
        }

    async def _redeem(self, account_id: str, code: str) -> dict:
        value = str(code or '').strip()
        if not value:
            raise PlatformError('请填写兑换码', status=400,
                                code='missing_redeem_code')
        account = await self._ensure_session(self._require(account_id))
        payload = await client.redeem_code(
            str(account.get('session') or ''), value,
            proxy=self._proxy_for(account),
            timeout=runtime_settings.credits_timeout(self.name))
        if client.login_expired(payload):
            raise PlatformError(client._error_text(payload, '登录已失效'),
                                status=401, code='login_expired',
                                payload=payload)
        biz = payload.get('code')
        ok = biz in (None, '', 0, '0', '000000', 200, '200')
        desc = str(payload.get('desc') or payload.get('message') or '')
        if not ok:
            raise PlatformError(desc or '兑换失败', status=400,
                                code='redeem_failed', payload=payload)
        self._credits.pop(str(account.get('uid') or ''), None)
        return {
            'ok': True,
            'uid': str(account.get('uid') or ''),
            'account': str(account.get('uid') or ''),
            'desc': desc,
            'data': payload.get('data') or {},
        }

    async def usage(self, account_id: str) -> dict:
        """额度 / 用量快照（用量弹窗展示）。"""
        account = self._require(account_id)
        uid = str(account.get('uid') or '')
        account = await self._ensure_session(account)
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
            data = await client.fetch_points_breakdown(
                str(account.get('session') or ''), proxy=proxy,
                timeout=timeout, max_pages=3)
            out['session_usage'] = data
            out['entitlement'] = {
                key: data.get(key)
                for key in ('daily_quota', 'daily_consumed', 'cycle')
            }
        except PlatformError as exc:
            out['errors'].append({'endpoint': 'points', 'code': exc.code,
                                  'message': exc.message})
        return out

    async def credits(self, account_id: str) -> dict:
        """积分余额快照（默认 60 秒缓存，`platforms.loomy.credits_ttl`）。"""
        account = self._require(account_id)
        uid = str(account.get('uid') or '')
        cached = self._credits.get(uid)
        if cached and cached[0] > time.time():
            return cached[1]

        account = await self._ensure_session(account)
        proxy = self._proxy_for(account)
        timeout = runtime_settings.credits_timeout(self.name)
        payload = await client.fetch_points(
            str(account.get('session') or ''), proxy=proxy,
            timeout=timeout, page_no=1, page_size=1)
        data = payload.get('data') if isinstance(payload.get('data'), dict) else {}
        points = _points_of(data)
        if points is None:
            # 会话失效时 `/points/records` 也是 HTTP 200 + `code=100002`：
            # 归成 401，调用方才能提示「登录已失效」，而不是「取不到余额」。
            if client.login_expired(payload):
                raise PlatformError(
                    client._error_text(payload, '登录已失效'), status=401,
                    code='login_expired', payload=payload)
            raise PlatformError('取不到积分余额', status=502,
                                code='credits_unavailable')

        out = {
            'uid': uid,
            'realm': 'cn',
            'credits': int(points) if float(points).is_integer() else points,
            'remain': points,
            'limit': None,
            'used': None,
            'unit': 'credits',
            # Loomy 的积分没有逐包到期时间（只有每日额度日切），给空数组而不是
            # 编一个日期；`next_expire_at` 同理。
            'expires': [],
            'next_expire_at': None,
            'source': 'points',
            **_quota_fields(data),
        }
        if client.login_expired(payload):
            out['login_state'] = '登录已失效'
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
        models = cached.get('models')
        if not isinstance(models, list) or not models:
            return None
        return [m for m in models if isinstance(m, dict) and m.get('id')]

    def _save_models(self, models: list[dict]) -> None:
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

    @staticmethod
    def _from_live(raw: dict) -> dict | None:
        """上游 `/models` 条目 → 内部目录条目；非 chat 模型返回 None。"""
        model_type = str(raw.get('type') or 'chat').strip().lower()
        if model_type not in ('', 'chat'):
            return None
        model_id = str(raw.get('id') or '').strip()
        if not model_id:
            return None
        caps = raw.get('capabilities') if isinstance(raw.get('capabilities'), dict) else {}
        modalities = raw.get('modalities') or caps.get('input_modalities') or ['text']
        if not isinstance(modalities, list):
            modalities = ['text']
        modalities = [str(x) for x in modalities if str(x).strip()]
        if 'text' not in modalities:
            modalities.insert(0, 'text')
        context = _num(raw.get('context_length')
                       or caps.get('context_length'))
        max_out = _num(raw.get('max_output_tokens')
                       or caps.get('max_output_tokens'))
        return {
            'id': C.outward_model(model_id),
            'wire_id': model_id,
            'name': str(raw.get('name') or model_id),
            'type': 'chat',
            'multiplier': _multiplier_of(raw),
            'context_length': int(context) if context else 0,
            'max_tokens': int(max_out) if max_out else 0,
            'modalities': modalities,
            'reasoning': bool(caps.get('reasoning')),
        }

    async def _live_models(self, *, timeout: float) -> list[dict]:
        """用一个可用账号拉一次上游目录；失败就换下一个。"""
        for account in store.list_accounts():
            if not str(account.get('session') or '').strip():
                continue
            if store.is_disabled(account):
                continue
            try:
                raw = await client.fetch_models(
                    str(account.get('session') or ''),
                    proxy=self._proxy_for(account), timeout=timeout)
            except PlatformError:
                continue
            models = [m for m in (self._from_live(x) for x in raw if isinstance(x, dict))
                      if m]
            if models:
                return models
        return []

    @staticmethod
    def _merge_fallback(models: list[dict]) -> list[dict]:
        seen = {str(m.get('id') or '').lower() for m in models if m.get('id')}
        out = list(models)
        for item in C.FALLBACK_MODELS:
            key = str(item.get('id') or '').lower()
            if key and key not in seen:
                out.append(dict(item))
                seen.add(key)
        return out

    async def list_models(self, *, refresh: bool = False) -> list[dict]:
        models = None if refresh else self._cached_models()
        if models is None and (refresh or self._implicit_live_allowed()):
            live = await self._live_models(
                timeout=45 if refresh
                else runtime_settings.catalog_timeout(self.name))
            self._note_implicit_attempt()
            if live:
                live = self._merge_fallback(live)
                live = [m for m in live
                        if str(m.get('type') or 'chat').lower() == 'chat']
                self._save_models(live)
                models = live
        if models is None:
            models = [dict(m) for m in C.FALLBACK_MODELS]
        else:
            models = self._merge_fallback(models)
        models = [m for m in models
                  if str(m.get('type') or 'chat').lower() == 'chat']
        self._model_meta = {str(m.get('id') or '').strip().lower(): m
                            for m in models if m.get('id')}
        return [self._public_model(m) for m in models if m.get('id')]

    def _public_model(self, raw: dict) -> dict:
        model_id = str(raw.get('id') or '')
        modalities = raw.get('modalities')
        if not isinstance(modalities, list) or not modalities:
            modalities = ['text']
        multiplier = _multiplier_of(raw)
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
            'modalities': {'input': [str(x) for x in modalities]},
            'model_name': model_id,
            'credit_ratio': multiplier,
        }

    def cached_model_ids(self) -> set[str] | None:
        """已缓存的对外模型 id（`loomy/...`），只读不联网。"""
        models = self._cached_models()
        if models is None:
            models = [dict(m) for m in C.FALLBACK_MODELS]
        models = [m for m in models
                  if str(m.get('type') or 'chat').lower() == 'chat']
        if not models:
            return None
        return {self.public_model_id(str(m.get('id') or ''), 'cn')
                for m in models if m.get('id')}

    def _max_output_tokens(self, model: str) -> int:
        target = str(model or '').strip().lower()
        entry = self._model_meta.get(target)
        if entry is None:
            for item in C.FALLBACK_MODELS:
                if str(item.get('id') or '').strip().lower() == target:
                    entry = item
                    break
        if not entry:
            return 0
        value = _num(entry.get('max_tokens'))
        return int(value) if value and value > 0 else 0

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
    def _candidates(self, affinity: str | None = None,
                    floor: int | None = None) -> list[dict]:
        """本次请求的账号顺序：权重 SWRR 决定首选，权重相同按 LRU。

        命中会话绑定时沿用上次的账号，且**不推进** SWRR 游标（长会话会把全局
        比例带偏）；绑定账号被冻结 / 冷却 / 删除后重新选路。

        保底积分（`platforms.loomy.min_credits`，默认 5000）与 WorkBuddy 同
        语义：余额低于阈值的账号只作**最后候选**——池里还有别的号就先跳过它，
        全部低于保底时照常使用。余额取「最近一次登记的积分快照」；从未登记过
        的账号不参与判断，避免把新导入的号误判成 0 积分。低于保底的粘性号也
        会让位（否则会一直把同一个号吃穿），全部低于保底时粘性照常生效。
        """
        accounts = [a for a in store.list_accounts()
                    if str(a.get('uid') or '').strip()
                    and (a.get('session') or a.get('password'))
                    and not store.is_disabled(a)
                    and not store.is_checkin_only(a)
                    and not store.is_cooling(a)]
        if not accounts:
            return []

        def uid_of(account: dict) -> str:
            return str(account.get('uid') or '')

        limit = runtime_settings.min_credits(self.name) if floor is None \
            else int(floor)
        low: list[dict] = []
        if limit > 0:
            balances = {
                str(row.get('uid') or ''): row.get('credits')
                for row in creditsvc.platform_snapshot_entries(self.name)['accounts']
            }
            above: list[dict] = []
            for account in accounts:
                value = balances.get(uid_of(account))
                if isinstance(value, (int, float)) and not isinstance(value, bool) \
                        and value < limit:
                    low.append(account)
                else:
                    above.append(account)
            if above:
                # 有余额充足（或余额未知）的号时，低余额号排到队尾；全低于保底
                # 时保持原样（`low` 清空），不让保底把可用池变成 503。
                accounts = above
                low.sort(key=lambda a: (-_account_weight(a),
                                        self._last_used.get(uid_of(a), 0.0)))
            else:
                low = []

        accounts.sort(key=lambda a: self._last_used.get(uid_of(a), 0.0))

        if affinity:
            bound_uid = self._affinity.get(affinity)
            if bound_uid:
                bound = next((a for a in accounts if uid_of(a) == bound_uid), None)
                if bound is not None:
                    rest = [a for a in accounts if a is not bound]
                    rest.sort(key=lambda a: (-_account_weight(a),
                                             self._last_used.get(uid_of(a), 0.0)))
                    return [bound, *rest, *low]
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
        return [first, *rest, *low]

    def _cool_down(self, uid: str, exc: PlatformError) -> bool:
        """按设置把出错的账号冻结一段时间；返回 True = 应换下一个账号。"""
        if exc.status == 429:
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

        model_type = ''
        entry = self._model_meta.get(str(model or '').strip().lower())
        if entry is not None:
            model_type = str(entry.get('type') or '').lower()
        if model_type and model_type != 'chat':
            raise PlatformError(
                f'模型 {model} 是 {model_type} 类型，不支持 /v1/chat/completions',
                status=400, code='unsupported_model')

        candidates = self._candidates(affinity, runtime.get('min_credits'))
        if not candidates:
            raise PlatformError('没有可用的 Loomy 账号（请先导入账号）',
                                status=503, code='no_account')

        last_error: PlatformError | None = None
        for account in candidates[:runtime['max_accounts_try']]:
            uid = str(account.get('uid') or '')
            lease = None
            release: Callable[[], None] = _noop
            try:
                proxy = self._proxy_for(account)
                ready = await self._ensure_session(account)
                lease = await self._limiter.acquire(uid)
                if lease is False:
                    lease = None
                    last_error = PlatformError(
                        f'Loomy 账号并发已满（单账号上限 '
                        f'{self._limiter.limit()}），请稍后重试',
                        status=429, code='account_busy', retry_after=1)
                    # 与 Trae 同口径：这个号满了就试下一个号（名额是按账号算的），
                    # 全都满了才会把 account_busy 抛给客户端。
                    continue
                release = _lease_releaser(self._limiter, lease)
                try:
                    result = await self._chat_once(
                        ready, model, body, wrapper_model,
                        proxy=proxy, runtime=runtime)
                except PlatformError as exc:
                    # 会话被上游拒绝：用保存的密码换一个新会话再试一次。
                    if exc.status in (401, 403):
                        ready = await self._relogin(ready)
                        result = await self._chat_once(
                            ready, model, body, wrapper_model,
                            proxy=proxy, runtime=runtime)
                    else:
                        raise
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
        raise last_error or PlatformError('Loomy 账号均不可用', status=503,
                                          code='no_account')

    async def _chat_once(self, account: dict, model: str, body: dict,
                         wrapper_model: str, *, proxy: str | None,
                         runtime: dict) -> PlatformChat:
        session = str(account.get('session') or '').strip()
        if not session:
            raise PlatformError('Loomy 会话为空', status=401,
                                code='session_invalid')
        uid = str(account.get('uid') or '')

        payload = dict(body or {})
        payload['model'] = C.wire_model(model)
        # 「不思考」：上游只认正文里的 `/no_think`，没有开关字段。
        messages = _no_think_messages(payload)
        if messages is not None:
            payload['messages'] = messages
        # 上游在思考类模型上会先把预算花在 reasoning 上，小上限直接得到空正文；
        # 官方客户端始终按模型最大值下发（参考实现同口径）。
        cap = self._max_output_tokens(model)
        if cap > 0:
            payload['max_tokens'] = cap
            if 'max_completion_tokens' in payload:
                payload['max_completion_tokens'] = cap

        if not payload.get('stream'):
            data = await client.chat(session, payload, proxy=proxy,
                                     timeout=runtime['request_timeout'])
            usage = data.get('usage') if isinstance(data.get('usage'), dict) else {}
            _apply_points_credit(usage)
            return PlatformChat(payload=data, usage=usage, model=wrapper_model,
                                account_hint=uid, realm='cn')

        started = time.monotonic()
        response = await client.open_chat(
            session, payload, proxy=proxy,
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


adapter = LoomyAdapter()
