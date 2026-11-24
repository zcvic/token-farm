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


def _num(value: object) -> float | None:
    """数字字段可能是字符串 / None；认不出返回 None（**不是** 0）。"""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
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
    # 单区域平台（百度搭子国内站）。
    realm_aware = False

    def __init__(self) -> None:
        # uid -> 账号级锁（token 强刷串行，避免并发把同一账号打两次）。
        self._locks: dict[str, asyncio.Lock] = {}
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

        before = None
        try:
            snapshot = await client.quota_overview(cookie, proxy=proxy,
                                                   timeout=timeout)
            before = _num(snapshot.get('left'))
        except PlatformError:
            before = None

        info = await client.login_bonus_info(cookie, proxy=proxy, timeout=timeout)
        already = bool(info.get('has_issued'))
        message = '今日已签到' if already else ''
        if not already:
            try:
                await client.claim_login_bonus(cookie, proxy=proxy, timeout=timeout)
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

        after = None
        try:
            snapshot = await client.quota_overview(cookie, proxy=proxy,
                                                   timeout=timeout)
            after = _num(snapshot.get('left'))
        except PlatformError:
            after = None

        earned = 0
        if not already and before is not None and after is not None \
                and after > before:
            earned = int(round(after - before))

        self._credits.pop(uid, None)
        checkin = {
            'last_at': int(time.time()),
            'last_result': message,
            'total_times': info.get('total_times'),
            'sign_in_days': info.get('sign_in_days') or [],
            'month_points': info.get('month_points'),
        }
        patch: dict[str, Any] = {'checkin': checkin}
        if after is not None:
            patch['points'] = {'total': None, 'used': None, 'left': after,
                               'at': int(time.time())}
        try:
            store.patch_account(uid, patch)
        except (FileNotFoundError, ValueError, OSError):
            pass

        return {
            'ok': True,
            'checked_in': True,
            'already': already,
            'credits': after if after is not None else (before or 0),
            'earned_credit': earned,
            'message': message,
            'uid': uid,
            'realm': 'cn',
            'total_times': info.get('total_times'),
            'sign_in_days': info.get('sign_in_days') or [],
            'month_points': info.get('month_points'),
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
        return out

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
