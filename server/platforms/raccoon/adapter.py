"""商汤小浣熊（Raccoon / xiaohuanxiong.com）平台适配器。

与其它原生平台同构：本文件只负责**策略**，协议在 `client.py`。

  · 选号（权重 SWRR + LRU + 会话粘性）与单账号并发闸门；
  · JWT 刷新（refresh token **一次性轮换**，先落盘再使用，账号级锁串行）；
  · 模型目录缓存（在线拉取，失败时退回内置快照）；
  · 每日积分（触发桌面端 `setting_info`，再按账单核对到账）；
  · 流式（OpenAI chunk）/ 非流式两条出口的最后一步。

小浣熊是**单区域**平台（`realm_aware=False`），账号与模型统一 `realm='cn'`。

## 端点降级

参考实现与真机抓包给出的事实是：

  · v2 流式是官方客户端的路径，最完整；
  · v1 **流式**会报 `invalid upstream stream response`，但 v1 **非流式**可用；
  · v2 非官网客户端偶尔会被风控 `429 code 200108`。

因此：客户端要流式时先走 v2 流式；失败（非鉴权类）就退回 v1 非流式，再由
`synthesize_stream` 合成 OpenAI chunk——客户端仍然拿到 SSE，只是正文一次性
到达。非流式请求同样先 v2、失败再 v1，不额外合成。

## 未知模型

上游对未知模型**静默回落默认模型并返回 200**。所以本地必须先按目录校验，
并在首帧比对上游回显的 `model`：两者不一致时判错，不能把「默认模型的回答」
当成用户请求的模型。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncIterator, Callable

from ... import db
from ...services.session_affinity import SessionAffinity, affinity_key
from .. import settings as runtime_settings
from ..base import ChatContext, PlatformAdapter, PlatformChat, PlatformError
from ..limiter import AccountLimiter
from . import catalog, client, constants as C, store

logger = logging.getLogger('workbuddy.platforms.raccoon')

SETTING_MODELS = 'platforms.raccoon.models'
SETTING_MODELS_ATTEMPT = 'platforms.raccoon.models_attempt'

# 余额不足 / 积分耗尽的冷却：小浣熊每请求扣 1 分，积分见底后要等人充值或
# 次日发放，冻 10 分钟足够让批量任务改投别的号。
CREDITS_COOLDOWN_SECONDS = 600


# ── 小工具 ──────────────────────────────────────────────────
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


class RaccoonAdapter(PlatformAdapter):
    name = 'raccoon'
    label = '商汤小浣熊（Raccoon）'
    # 每日积分：读一次桌面端 setting_info 触发发放，再按账单核对。
    supports_checkin = True
    # 登录是网页 OAuth / 桌面客户端授权码，适配层不内置回环回调；面板支持
    # 手工粘贴与「导入本机客户端」。
    supports_web_login = False
    # 凭证不是整段网页 Cookie，而是 access/refresh 两个 JWT；前端要求用户
    # 贴 JSON（或本机客户端一键导入）。
    supports_cookie_import = False
    # 单区域平台。
    realm_aware = False

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_used: dict[str, float] = {}
        # uid -> (access token 到期时刻, access token)。refresh token 永远以
        # 磁盘为准：它是一次性的，内存副本轮换后立刻过期。
        self._tokens: dict[str, tuple[float, str]] = {}
        # uid -> (过期时刻, 探活结果)
        self._probes: dict[str, tuple[float, dict]] = {}
        # uid -> (过期时刻, credits 结果)
        self._credits: dict[str, tuple[float, dict]] = {}
        self._limiter = AccountLimiter(self.name)
        self._swrr: dict[str, float] = {}
        self._affinity = SessionAffinity()
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
        from ... import config  # noqa: PLC0415 —— 与其它平台同一取法
        try:
            return config.resolve_account_proxy(account)
        except ValueError as exc:
            raise PlatformError(str(exc), status=503,
                                code='proxy_invalid') from exc

    @staticmethod
    def _refresh_of(account: dict) -> str:
        return str(account.get('refreshToken') or '').strip()

    @staticmethod
    def _access_of(account: dict) -> str:
        return str(account.get('token') or account.get('accessToken')
                   or '').strip()

    @staticmethod
    def _mark_expired(uid: str, message: str = '') -> None:
        try:
            store.patch_account(uid, {
                'loginState': '登录已失效',
                'lastRefreshError': str(message or '')[:200],
            })
        except (FileNotFoundError, ValueError, OSError):
            pass

    def _timeout(self, field: str, fallback: float) -> float:
        try:
            getter = getattr(runtime_settings, field)
            value = float(getter(self.name))
        except Exception:  # noqa: BLE001 —— 配置读不到时不能把请求打挂
            return fallback
        return value if value > 0 else fallback

    # ── token ─────────────────────────────────────────────
    async def _token_for(self, account: dict, *, proxy: str | None,
                         force: bool = False) -> str:
        """取（或轮换）access token。

        refresh token 是**一次性**的：上游换发后旧的立即作废。因此刷新成功
        必须先把新 token 落盘，再返回给调用方；并发刷新由账号级锁串行。
        """
        uid = str(account.get('uid') or '')
        now = time.time()
        cached = self._tokens.get(uid)
        if cached and not force and cached[0] - C.TOKEN_SKEW_SECONDS > now:
            return cached[1]

        async with self._lock(uid):
            cached = self._tokens.get(uid)
            if cached and not force \
                    and cached[0] - C.TOKEN_SKEW_SECONDS > time.time():
                return cached[1]

            fresh = store.read_account(uid) or account
            access = self._access_of(fresh)
            access_exp = store.epoch_from_iso(fresh.get('expiredAt'))
            if not access_exp:
                access_exp = store.jwt_exp(access)
            refresh = self._refresh_of(fresh)

            if access and not force and access_exp \
                    and access_exp - C.TOKEN_SKEW_SECONDS > time.time():
                self._tokens[uid] = (access_exp, access)
                return access

            if not refresh:
                if access and not force:
                    # 手工粘贴的 access token 没有 refresh：先用一次，401 再换号。
                    self._tokens[uid] = (time.time() + 60, access)
                    return access
                self._mark_expired(uid, 'refresh token 缺失')
                raise PlatformError(
                    '小浣熊账号缺少 refresh token，请重新登录或导入客户端登录态',
                    status=401, code='login_expired')

            try:
                data = await client.refresh(
                    refresh, proxy=proxy,
                    timeout=self._timeout('request_timeout',
                                          C.REFRESH_TIMEOUT))
            except PlatformError as exc:
                if exc.status == 401:
                    self._mark_expired(uid, exc.message)
                else:
                    try:
                        store.patch_account(uid, {
                            'lastRefreshError': exc.message[:200]})
                    except (FileNotFoundError, ValueError, OSError):
                        pass
                    # 刷新失败先冷一会儿：上游抖动时不该每个请求都去轮换一次
                    # 一次性 token（那会把「没落盘」的丢链窗口放大）。
                    try:
                        store.set_cooling(
                            uid, time.time() + C.REFRESH_FAIL_COOLDOWN,
                            'refresh failed')
                    except (FileNotFoundError, ValueError, OSError):
                        pass
                raise

            new_access = str(data.get('access_token') or '')
            new_refresh = str(data.get('refresh_token') or '') or refresh
            new_expires = float(data.get('expires_at') or 0) \
                or time.time() + C.ACCESS_TTL_SECONDS
            refresh_expires = float(data.get('refresh_expires_at') or 0) \
                or time.time() + C.REFRESH_TTL_SECONDS
            # **先落盘再使用**：进程崩在中间时，至少新的 refresh 已经在文件
            # 里，不会把账号永久踢下线。
            try:
                store.patch_account(uid, {
                    'token': new_access,
                    'refreshToken': new_refresh,
                    'expiredAt': store.iso_from_epoch(new_expires),
                    'refreshExpiredAt': store.iso_from_epoch(refresh_expires),
                    'refreshedAt': store.iso_from_epoch(time.time()),
                    'lastRefreshError': '',
                    'loginState': '正常',
                })
            except (FileNotFoundError, ValueError, OSError) as exc:
                raise PlatformError(f'保存小浣熊新 token 失败：{exc}',
                                    status=500, code='store_error') from exc
            self._tokens[uid] = (new_expires, new_access)
            return new_access

    def _drop_token(self, uid: str) -> None:
        self._tokens.pop(str(uid or ''), None)

    # ── 账号 ──────────────────────────────────────────────
    def list_accounts(self) -> list[dict]:
        return [store.public_account(a) for a in store.list_accounts()]

    def export_accounts(self) -> list[dict]:
        """导出含明文凭据的账号，仅供管理员导出接口调用。"""
        return store.list_accounts()

    def import_account(self, payload: dict) -> dict:
        """导入账号：access token +（推荐）refresh token。

        接受 `{account: {...}}` 与扁平对象两种形状。凭据字段兼容
        `access_token` / `accessToken` / `token` / `auth_token` 与
        `refresh_token` / `refreshToken` / `refresh`；也接受整段 Cookie（里面
        的 `xhx_token` / `access_token` 会被抽出来）。导入阶段**不联网**，
        第一次刷新 / 探活在 `refresh_account` 或第一次对话时发生。
        """
        if not isinstance(payload, dict):
            raise PlatformError('账号必须是 JSON 对象', status=400,
                                code='invalid_account')
        raw = payload.get('account') if isinstance(payload.get('account'), dict) \
            else payload

        access = store.normalize_token(
            _pick(raw, 'access_token', 'accessToken', 'token', 'auth_token',
                  'authToken', 'cookie', 'cookies', 'key'))
        refresh = store.normalize_refresh(
            _pick(raw, 'refresh_token', 'refreshToken', 'refresh'))
        if not access and not refresh:
            raise PlatformError(
                '需要提供小浣熊的 access token 或 refresh token',
                status=400, code='invalid_account')

        try:
            uid = _pick(raw, 'uid')
            if uid:
                uid = store.safe_id(uid)
            else:
                uid = store.derive_uid(access, refresh)
        except ValueError as exc:
            raise PlatformError(str(exc), status=400,
                                code='invalid_account') from exc

        old = store.read_account(uid) or {}
        if not access and str(old.get('refreshToken') or '') == refresh:
            access = str(old.get('token') or '')
        access_exp = store.jwt_exp(access) \
            or store.epoch_from_iso(_pick(raw, 'expiresAt', 'expiredAt',
                                          'expires_at'))
        if not access_exp and access:
            access_exp = time.time() + C.ACCESS_TTL_SECONDS
        refresh_exp = store.jwt_exp(refresh) \
            or store.epoch_from_iso(_pick(raw, 'refreshExpiresAt',
                                          'refresh_expires_at'))
        if not refresh_exp and refresh:
            refresh_exp = time.time() + C.REFRESH_TTL_SECONDS

        account = {
            **old,
            'uid': uid,
            'token': access,
            'refreshToken': refresh,
            'expiredAt': store.iso_from_epoch(access_exp),
            'refreshExpiredAt': store.iso_from_epoch(refresh_exp),
            'name': _pick(raw, 'name') or str(old.get('name') or ''),
            'nickname': _pick(raw, 'nickname', 'displayName')
            or str(old.get('nickname') or ''),
            'email': _pick(raw, 'email') or str(old.get('email') or ''),
            'orgCode': _pick(raw, 'orgCode', 'org_code')
            or str(old.get('orgCode') or ''),
            'office_identity': _pick(raw, 'office_identity', 'officeIdentity')
            or str(old.get('office_identity') or ''),
            'office_org_name': _pick(raw, 'office_org_name', 'officeOrgName')
            or str(old.get('office_org_name') or ''),
            'office_org_role': _pick(raw, 'office_org_role', 'officeOrgRole')
            or str(old.get('office_org_role') or ''),
            'proxy': _pick(raw, 'proxy', 'egress') or str(old.get('proxy') or ''),
            'source': _pick(raw, 'source') or str(old.get('source') or 'manual'),
            'lastRefreshError': '',
            'loginState': '正常',
        }
        self._drop_token(uid)
        self._probes.pop(uid, None)
        self._credits.pop(uid, None)
        return store.public_account(store.save_account(account))

    def import_from_desktop(self, *, realm: str | None = None) -> dict:
        """一键导入本机小浣熊客户端里已登录的会话。

        `realm` 只为符合路由的统一签名（小浣熊是单区域平台，忽略）。
        """
        from . import desktop  # noqa: PLC0415 —— desktop 只被这条路径使用

        imported: list[dict] = []
        failed: list[dict] = []
        for account in desktop.read_accounts():
            try:
                imported.append(self.import_account(account))
            except (PlatformError, ValueError) as exc:
                failed.append({
                    'uid': str(account.get('uid') or ''),
                    'error': getattr(exc, 'message', str(exc)),
                })
        return {
            'platform': self.name,
            'sources': [str(item['path']) for item in desktop.storage_paths()],
            'imported': imported,
            'failed': failed,
        }

    def delete_account(self, account_id: str) -> bool:
        uid = str(account_id or '')
        self._drop_token(uid)
        self._probes.pop(uid, None)
        self._credits.pop(uid, None)
        return store.delete_account(account_id)

    async def refresh_account(self, account_id: str, *,
                              force: bool = False) -> dict:
        """轮换一次 token（顺便验证凭证）；没有余额接口可查。"""
        account = self._require(account_id)
        uid = str(account.get('uid') or '')
        proxy = self._proxy_for(account)
        if self._refresh_of(account):
            try:
                await self._token_for(account, proxy=proxy, force=True)
            except PlatformError as exc:
                if exc.status != 401:
                    try:
                        store.patch_account(uid, {
                            'lastRefreshError': exc.message[:200]})
                    except (FileNotFoundError, ValueError, OSError):
                        pass
                raise
        else:
            # 只有 access token：用目录接口探活，能读就说明还没过期。
            out = await client.probe(
                self._access_of(account), proxy=proxy,
                timeout=self._timeout('credits_timeout',
                                      C.CATALOG_TIMEOUT))
            state = str(out.get('state') or 'unknown')
            patch: dict[str, Any] = {
                'refreshedAt': store.iso_from_epoch(time.time())}
            if state == 'invalid':
                patch['loginState'] = '登录已失效'
                patch['lastRefreshError'] = str(
                    out.get('message') or 'token 无效')[:200]
            elif state == 'ok':
                patch['loginState'] = '正常'
                patch['lastRefreshError'] = ''
            else:
                patch['lastRefreshError'] = str(out.get('message') or '')[:200]
            saved = store.patch_account(uid, patch)
            self._probes[uid] = (
                time.time() + runtime_settings.credits_ttl(self.name), out)
            if state == 'invalid':
                raise PlatformError(str(out.get('message') or 'token 无效'),
                                    status=401, code='auth_expired')
            return store.public_account(saved)
        return store.public_account(store.patch_account(uid, {
            'refreshedAt': store.iso_from_epoch(time.time()),
            'lastRefreshError': '',
            'loginState': '正常',
        }))

    # ── 签到 / 余额 ───────────────────────────────────────
    async def checkin(self, account_id: str) -> dict:
        """每日积分：触发桌面端发放，再按账单核对今天到账的正向积分。

        上游**没有**独立领取接口：客户端打开时读一次
        `GET /api/web/office/v3/setting_info`，服务端就会把当天积分发下来。
        所以这里先触发、再查 `bills`，只汇总 `points > 0` 且属于今天
        （北京时间，01:30 宽限）的条目。
        """
        account = self._require(account_id)
        uid = str(account.get('uid') or '')
        proxy = self._proxy_for(account)
        timeout = self._timeout('credits_timeout', C.BALANCE_TIMEOUT)
        access = await self._token_for(account, proxy=proxy)

        grant = await client.claim_daily_grant(
            account, access, proxy=proxy, timeout=timeout)
        items = await client.bills(
            access, limit=20, proxy=proxy, timeout=timeout)
        reward = client.daily_reward(items)
        granted = bool(grant.get('popups') or grant.get('toast'))
        ok = reward > 0
        # 上游在 00:00 会主动发放；用户点「签到」时积分可能已经在账上，
        # 只有这次触发真的弹了领取提示才算「本次发放」。
        already = ok and not granted
        message = (str(grant.get('toast') or '')
                   or ('今日已签到' if already else
                       ('签到成功' if ok else '今天还没有积分到账')))

        try:
            store.patch_account(uid, {
                'checkin': {
                    'last_at': int(time.time()),
                    'last_result': 'already' if already else (
                        'ok' if ok else 'error'),
                    'continuous_days': None,
                    'reward': reward,
                },
                'loginState': '正常',
            })
        except (FileNotFoundError, ValueError, OSError):
            pass

        return {
            'ok': ok,
            'checked_in': ok,
            'already': already,
            'credits': 0,
            'earned_credit': reward,
            'message': message,
            'uid': uid,
            'realm': store.REALM,
            'reward': reward,
            'continuous_days': None,
            'bills_scanned': len(items),
        }

    async def usage(self, account_id: str) -> dict:
        """额度 / 用量快照（积分余额接口）。"""
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
            access = await self._token_for(
                account, proxy=self._proxy_for(account))
            data = await client.balance(
                access, proxy=self._proxy_for(account),
                timeout=self._timeout('credits_timeout', C.BALANCE_TIMEOUT))
        except PlatformError as exc:
            out['errors'].append({'endpoint': 'points/balance',
                                  'code': exc.code, 'message': exc.message})
            return out
        out['session_usage'] = {
            'total': data['available_points'],
            'used': None,
            'remaining': data['available_points'],
            'daily_points': data.get('daily_points'),
            'monthly_points': data.get('monthly_points'),
            'at': int(time.time()),
        }
        out['entitlement'] = {
            'quota_known': True,
            'remaining': data['available_points'],
        }
        return out

    async def credits(self, account_id: str) -> dict:
        """积分余额快照（默认 60 秒缓存，`platforms.raccoon.credits_ttl`）。"""
        account = self._require(account_id)
        uid = str(account.get('uid') or '')
        cached = self._credits.get(uid)
        if cached and cached[0] > time.time():
            return cached[1]

        proxy = self._proxy_for(account)
        access = await self._token_for(account, proxy=proxy)
        data = await client.balance(
            access, proxy=proxy,
            timeout=self._timeout('credits_timeout', C.BALANCE_TIMEOUT))
        out = {
            'uid': uid,
            'realm': store.REALM,
            'credits': data['available_points'],
            'remain': data['available_points'],
            'limit': None,
            'used': None,
            'unit': 'points',
            'daily_points': data.get('daily_points'),
            'monthly_points': data.get('monthly_points'),
            'reward_points': data.get('reward_points'),
            'topup_points': data.get('topup_points'),
            'expires': [],
            'next_expire_at': None,
            'source': 'points/v1/balance',
        }
        self._credits[uid] = (
            time.time() + runtime_settings.credits_ttl(self.name), out)
        try:
            store.patch_account(uid, {'balance': {
                'state': 'ok', 'at': int(time.time()), 'message': '',
                'remaining': data['available_points'],
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

    def _all_models(self) -> list[dict]:
        """缓存目录（过期也算）→ 内置兜底。用于本地模型校验。"""
        try:
            cached = db.get_setting(SETTING_MODELS, None)
        except Exception:  # noqa: BLE001
            cached = None
        if isinstance(cached, dict):
            models = catalog.normalize(cached.get('models'))
            if models:
                return models
        return catalog.default_models()

    async def _live_models(self, *, timeout: float) -> list[dict]:
        """用一个可用账号拉一次上游目录；失败就换下一个。"""
        for account in store.list_accounts():
            if not (self._access_of(account) or self._refresh_of(account)):
                continue
            if store.is_disabled(account):
                continue
            try:
                access = await self._token_for(
                    account, proxy=self._proxy_for(account))
                models = await client.fetch_catalog(
                    access, proxy=self._proxy_for(account), timeout=timeout)
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
        model_id = str(raw.get('id') or '')
        modalities = raw.get('modalities')
        if not isinstance(modalities, list) or not modalities:
            modalities = ['text']
        thinking = raw.get('thinking') if isinstance(raw.get('thinking'),
                                                      dict) else {}
        levels = [str(item) for item in thinking.get('levels') or []
                  if str(item or '').strip()]
        multiplier = _num(raw.get('billing_effective_multiplier'))
        if multiplier is None:
            multiplier = _num(raw.get('billing_multiplier'))
        return {
            'id': self.public_model_id(model_id, store.REALM),
            'object': 'model',
            'created': 0,
            'owned_by': self.name,
            'name': str(raw.get('name') or model_id),
            'platform': self.name,
            'realm': store.REALM,
            'context_window': int(_num(raw.get('context_length')) or 0),
            'max_tokens': int(_num(raw.get('max_tokens')) or 0),
            'supports_reasoning': bool(raw.get('reasoning')),
            'reasoning_efforts': levels,
            'modalities': {'input': [str(x) for x in modalities]},
            'model_name': model_id,
            'tool_call': bool(raw.get('tool_call', True)),
            'attachment': 'image' in modalities,
            'credit_ratio': multiplier,
            'free': bool(raw.get('free')),
            'badge': '限免' if raw.get('free') else '',
            'description': str(raw.get('description') or ''),
            'thinking': thinking,
        }

    def cached_model_ids(self) -> set[str] | None:
        """已缓存的对外模型 id（`raccoon/...`），只读不联网。"""
        models = self._all_models()
        if not models:
            return None
        return {self.public_model_id(str(m.get('id') or ''), store.REALM)
                for m in models if m.get('id')}

    def upstream_model(self, model: str) -> str:
        """成员名 → 上游模型名；空名 / `default` 补默认模型，其余原样。"""
        text = str(model or '').strip()
        return C.DEFAULT_MODEL if text in ('', 'default') else text

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
                    and (self._access_of(a) or self._refresh_of(a))
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
            seconds = CREDITS_COOLDOWN_SECONDS
        elif exc.status == 429:
            # 200108 是账号级风控，恢复中位约 25 分钟；普通 429 才按设置。
            seconds = exc.retry_after or runtime_settings.cooldown_429(self.name) \
                or 60
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

    def _assert_known_model(self, model: str) -> None:
        """本地按目录校验模型名。

        上游对未知模型会静默回落默认模型并返回 200，等到首帧再发现已经晚了；
        这里在发请求前就明确拒绝，错误信息也指得到正确的模型名。
        """
        text = str(model or '').strip()
        if not text or text.lower() == 'default':
            return
        known = {str(item.get('id') or '').strip().lower()
                 for item in self._all_models() if item.get('id')}
        if known and text.lower() not in known:
            raise PlatformError(f'小浣熊没有模型 {text}', status=400,
                                code='model_not_found')

    async def _chat_accounts(self, model: str, body: dict) -> PlatformChat:
        wrapper_model = f'{self.model_prefix}{model}'
        runtime = runtime_settings.load(self.name)
        affinity = affinity_key(body) if runtime['session_affinity'] else ''
        if affinity:
            self._affinity.set_ttl(runtime['session_affinity_ttl'])

        self._assert_known_model(model)
        candidates = self._candidates(affinity)
        if not candidates:
            raise PlatformError(
                '没有可用的小浣熊账号（请粘贴 token 或导入本机客户端登录态）',
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
                        f'小浣熊账号并发已满（单账号上限 '
                        f'{self._limiter.limit()}）', status=429,
                        code='account_busy')
                    continue
                release = _lease_releaser(self._limiter, lease)
                try:
                    result = await self._chat_once(
                        account, model, body, wrapper_model,
                        proxy=proxy, runtime=runtime, affinity=affinity)
                except PlatformError as exc:
                    if exc.status != 401:
                        raise
                    # access token 失效：换一份再试一次（同一账号，不消耗
                    # 选号次数）。
                    self._drop_token(uid)
                    result = await self._chat_once(
                        account, model, body, wrapper_model,
                        proxy=proxy, runtime=runtime, affinity=affinity,
                        force_token=True)
            except PlatformError as exc:
                release()
                last_error = exc
                if affinity and (exc.status in (401, 403, 429)
                                 or exc.status >= 500):
                    self._affinity.unbind(affinity)
                if exc.status == 401:
                    self._mark_expired(uid, exc.message)
                    continue
                if self._cool_down(uid, exc):
                    continue
                if exc.status == 403:
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
        raise last_error or PlatformError('小浣熊账号均不可用', status=503,
                                          code='no_account')

    async def _complete(self, account: dict, access: str, payload: dict,
                        *, proxy: str | None, timeout: float) -> dict:
        """非流式对话：v2 优先，失败退回 v1（两者都可用）。"""
        body = {**payload, 'stream': False}
        try:
            return await client.chat(account, access, body, base=C.LLM_V2,
                                     proxy=proxy, timeout=timeout)
        except PlatformError as exc:
            if exc.status in (401, 402, 403):
                raise
            if exc.status == 429 and client.is_risk_control(exc):
                # 账号级风控：v1 非流式通常仍可用，试一次；不行再上抛。
                pass
            return await client.chat(account, access, body, base=C.LLM_V1,
                                     proxy=proxy, timeout=timeout)

    async def _chat_once(self, account: dict, model: str, body: dict,
                         wrapper_model: str, *, proxy: str | None,
                         runtime: dict, affinity: str = '',
                         force_token: bool = False) -> PlatformChat:
        uid = str(account.get('uid') or '')
        access = await self._token_for(account, proxy=proxy, force=force_token)
        upstream = self.upstream_model(model)
        payload = client.chat_body(self.chat_body(model, body))
        timeout = float(runtime['request_timeout'])

        if not payload.get('stream'):
            data = await self._complete(account, access, payload,
                                        proxy=proxy, timeout=timeout)
            usage = data.get('usage') if isinstance(data.get('usage'), dict) else {}
            return PlatformChat(payload=data, usage=usage,
                                model=wrapper_model, account_hint=uid,
                                realm=store.REALM)

        # ── 流式：v2 失败后退回 v1 非流式，再合成为 chunk 流 ──
        try:
            response = await client.open_chat(
                account, access, payload, base=C.LLM_V2, proxy=proxy,
                idle_timeout=float(runtime['idle_timeout']))
        except PlatformError as exc:
            if exc.status in (401, 402, 403):
                raise
            completion = await client.chat(
                account, access, {**payload, 'stream': False}, base=C.LLM_V1,
                proxy=proxy, timeout=timeout)
            usage = (completion.get('usage')
                     if isinstance(completion.get('usage'), dict) else {})
            return PlatformChat(
                stream=client.synthesize_stream(completion, wrapper_model),
                usage=usage, model=wrapper_model, account_hint=uid,
                realm=store.REALM)

        usage: dict = {}
        chat = PlatformChat(usage=usage, model=wrapper_model,
                            account_hint=uid, realm=store.REALM)
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

        async def relay() -> AsyncIterator[dict]:
            seen_model = False
            saw_content = False
            try:
                async for chunk in client.iter_chunks(response):
                    if not seen_model:
                        echoed = str(chunk.get('model') or '')
                        if echoed:
                            seen_model = True
                            if echoed != upstream:
                                # 上游静默回落默认模型：不能把别人的回答当成
                                # 用户请求的模型。
                                raise PlatformError(
                                    f'小浣熊把模型 {upstream} 回落成了 {echoed}',
                                    status=502, code='model_fallback')
                    data = chunk.get('usage')
                    if isinstance(data, dict):
                        usage.update(data)
                    choices = chunk.get('choices')
                    if isinstance(choices, list):
                        for choice in choices:
                            delta = choice.get('delta') if isinstance(
                                choice, dict) else None
                            if isinstance(delta, dict) and any(
                                    value for key, value in delta.items()
                                    if key != 'role'):
                                saw_content = True
                    yield chunk
                if not saw_content:
                    raise PlatformError('小浣熊没有返回内容', status=502,
                                        code='empty_response')
            finally:
                await _close()

        chat.stream = relay()
        # 即使网关还没开始迭代就收尾（客户端提前断开 / 上层报错），也要保证上游
        # 连接被关闭：`relay` 的 finally 与这里都调 `_close`，靠 `closed` 幂等。
        chat.closer = _close
        return chat


adapter = RaccoonAdapter()
