"""Accio（阿里国际站 AI 电商工作台）平台适配器。

与其它原生平台同构：本文件只负责**策略**，协议在 `client.py` / `protocol.py`。

  · 双区选号（权重 SWRR + LRU + 会话粘性）与单账号并发闸门；
  · OAuth access token 的按需续期（**先落盘再使用**）；
  · 模型目录按区域独立缓存（没有离线快照）；
  · 积分（三类桶）与配额倒计时；
  · 流式（OpenAI chunk）/ 非流式两条出口的最后一步。

`realm_aware=True`：国内站（`acciocn/`）与国际站（`acciocom/`）的**登录页
域名不同**，账号按区域分池、模型按区域加前缀；网关与 token 两区通用，实际
区域由服务端按账号下发（`userinfo.routeRegion`）。旧式 `accio/` 仍可调用
（按账号池里有什么就选什么）。

## 没有签到

Accio **没有**「每日签到」端点：每日积分由**登录**触发服务端发放，没有可
点的按钮。所以 `supports_checkin = False`，`checkin()` 明确报「不支持」，
不伪造一个点一下加积分的动作。

## 失败分级（与参考实现 `accioFailureClassOf` 同口径）

  · `402` / 流内 `402` —— 额度耗尽：换号 + 冻结到 UTC+8 当日 24:00；
  · `429` —— 限流：换号 + 冻结 1 小时；
  · `401 / 403` —— 会话失效：**只换号，绝不写冷却**；
  · `403 + text/html` —— 风控 / 权限拒绝：不换号、不写冷却；
  · `400` / `5xx` / 风控页 —— 不换号。

单次请求最多试 8 个账号（`constants.MAX_ACCOUNTS_TRY`）。

## 有意不做

- 不写死离线模型目录：某区没有可用账号时目录就是空表，**不**借另一区的；
- 不改写 system prompt 过风控，也不做 force-model 静默改写；
- 不内嵌阿里云滑块：登录只做「打开授权页 + 粘回调」。
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Callable

from ... import db
from ...services.session_affinity import SessionAffinity, affinity_key
from .. import settings as runtime_settings
from ..base import ChatContext, PlatformAdapter, PlatformChat, PlatformError
from ..limiter import AccountLimiter
from . import catalog, chat as chatmod, client, constants as C, protocol as P, store

logger = logging.getLogger('workbuddy.platforms.accio')

#: 模型目录缓存按区域分开：两区的模型名单不同，共用一份会让国际站显示国内
#: 的模型名（发过去必然报错）。
SETTING_MODELS = 'platforms.accio.models.{realm}'
SETTING_MODELS_ATTEMPT = 'platforms.accio.models_attempt.{realm}'

# UTC+8 时区：Accio 的每日额度按北京时间 24:00 重置。
_UTC8 = timezone(timedelta(hours=8))


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


def _seconds_until_utc8_midnight(now: float | None = None) -> float:
    """距离 UTC+8 当日 24:00 的秒数（至少 1 秒）。"""
    current = datetime.now(tz=_UTC8) if now is None \
        else datetime.fromtimestamp(now, tz=_UTC8)
    tomorrow = (current + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return max(1.0, (tomorrow - current).total_seconds())


def _max_tokens_of(body: dict) -> object:
    for key in ('max_tokens', 'max_completion_tokens', 'max_output_tokens'):
        value = body.get(key)
        if value not in (None, ''):
            return value
    return None


def _kind_of(exc: PlatformError) -> str:
    failure = client.failure_of(exc)
    if failure is not None:
        return failure.kind
    return P.failure_class_of(P.http_error_code(exc.status))


def _bucket_total(data: dict) -> float | None:
    """服务端自己算的 `data.remaining`；缺席返回 None。"""
    return _num(data.get('remaining'))


def _read_buckets(data: dict) -> list[dict]:
    """`entitlement.{daily,monthly,referral}` → 固定顺序的桶行。

    桶对象存在但 `remaining` 读不到时**跳过**该桶（数据不足，不是 0）；
    `total` / `used` 读不到才退回 0（它们不是主读数）。
    """
    entitlement = data.get('entitlement')
    if not isinstance(entitlement, dict):
        return []
    rows: list[dict] = []
    for key, label in C.CREDIT_BUCKETS:
        raw = entitlement.get(key)
        if not isinstance(raw, dict):
            continue
        remaining = _num(raw.get('remaining'))
        if remaining is None:
            continue
        rows.append({
            'key': key,
            'label': label,
            'total': _num(raw.get('total')) or 0.0,
            'used': _num(raw.get('used')) or 0.0,
            'remaining': remaining,
        })
    return rows


def _sum_of(rows: list[dict], key: str) -> float:
    return float(sum(float(row.get(key) or 0.0) for row in rows))


class AccioAdapter(PlatformAdapter):
    name = 'accio'
    label = 'Accio（阿里国际站 AI 工作台）'
    # Accio 没有签到端点：每日积分由登录触发服务端发放，没有可点的按钮。
    supports_checkin = False
    # 网页登录：OAuth 2.0 + PKCE，打开授权页 + 本机回环 / 粘回调。
    supports_web_login = True
    # 凭证是 access + refresh 两个 token，不是整段网页 Cookie。
    supports_cookie_import = False
    # 国内 / 国际两套登录页域名（网关与 token 通用）。
    realm_aware = True

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_used: dict[str, float] = {}
        # uid -> (access token 到期时刻, access token)。
        self._tokens: dict[str, tuple[float, str]] = {}
        # uid -> (过期时刻, credits 结果)
        self._credits: dict[str, tuple[float, dict]] = {}
        self._limiter = AccountLimiter(self.name)
        self._swrr: dict[str, float] = {}
        self._affinity = SessionAffinity()
        # slug（对外裸名）→ 目录条目
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
    def _access_of(account: dict) -> str:
        return str(account.get('accessToken') or account.get('token')
                   or '').strip()

    @staticmethod
    def _refresh_of(account: dict) -> str:
        return str(account.get('refreshToken') or '').strip()

    @staticmethod
    def _realm_of(account: dict) -> str:
        return store.normalize_realm(account.get('realm'))

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
        """取（或续期）access token。

        Accio 的 access token 是**不透明 32 位串**（不是 JWT），过期时刻只能
        来自 `expiredAt`（上游 `expiresAt` 是秒，落盘前已归一成 ISO）。续期
        成功必须**先把新 token 落盘**再返回：进程崩在中间时至少磁盘上是新的。
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
            refresh = self._refresh_of(fresh)
            # 落盘的是 ISO（`expiredAt`），不是上游的秒值；两者都由
            # `store.epoch_from_iso` 统一解析（纯数字也兼容）。
            expires = store.epoch_from_iso(fresh.get('expiredAt'))

            if access and not force and expires \
                    and expires - C.TOKEN_SKEW_SECONDS > time.time():
                self._tokens[uid] = (expires, access)
                return access

            if not refresh:
                if access and not force:
                    # 手工粘贴的 access token 没有 refresh：先用一次，401 再换号。
                    self._tokens[uid] = (time.time() + C.TOKEN_SKEW_SECONDS,
                                         access)
                    return access
                self._mark_expired(uid, 'refresh token 缺失')
                raise PlatformError(
                    'Accio 账号缺少 refresh token，请重新登录或导入',
                    status=401, code='login_expired')

            try:
                data = await client.refresh(
                    fresh, proxy=proxy,
                    timeout=self._timeout('request_timeout',
                                          C.REQUEST_TIMEOUT))
            except PlatformError as exc:
                if exc.status == 401 or P.is_terminal_refresh_error(exc.message):
                    # refresh token 已终态（`invalid_grant` / `NOT_LOGIN`）：
                    # 标成登录失效，界面提示重新登录。
                    self._mark_expired(uid, exc.message)
                else:
                    try:
                        store.patch_account(uid, {
                            'lastRefreshError': exc.message[:200]})
                    except (FileNotFoundError, ValueError, OSError):
                        pass
                    # 刷新失败先冷一会儿：上游抖动时不该每个请求都去轮换。
                    try:
                        store.set_cooling(uid, time.time()
                                          + C.REFRESH_FAIL_COOLDOWN,
                                          'refresh failed')
                    except (FileNotFoundError, ValueError, OSError):
                        pass
                raise

            new_access = str(data.get('access_token') or '')
            new_refresh = str(data.get('refresh_token') or '')
            new_expires = float(data.get('expires_at') or 0) / 1000.0
            patch: dict[str, Any] = {
                'accessToken': new_access,
                'refreshToken': new_refresh,
                'expiredAt': store.iso_from_epoch(new_expires)
                if new_expires > 0 else '',
                'refreshedAt': store.iso_from_epoch(time.time()),
                'lastRefreshError': '',
                'loginState': '正常',
            }
            sgcookie = str(data.get('sgcookie') or '').strip()
            if sgcookie:
                patch['sgcookie'] = sgcookie
            try:
                store.patch_account(uid, patch)
            except (FileNotFoundError, ValueError, OSError) as exc:
                raise PlatformError(f'保存 Accio 新 token 失败：{exc}',
                                    status=500, code='store_error') from exc
            self._tokens[uid] = (new_expires or
                                 time.time() + C.TOKEN_SKEW_SECONDS, new_access)
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
        """导入账号：access token +（推荐）refresh token，可带区域。

        接受 `{account: {...}}` 与扁平对象两种形状。凭据字段兼容
        `access_token` / `accessToken` / `token` 与 `refresh_token` /
        `refreshToken` / `refresh`。区域缺省按国内版处理：**不猜**国际——
        猜错只会让一份国际 token 打到国内登录页，然后被 401 标成登录失效。
        """
        if not isinstance(payload, dict):
            raise PlatformError('账号必须是 JSON 对象', status=400,
                                code='invalid_account')
        raw = payload.get('account') if isinstance(payload.get('account'), dict) \
            else payload

        realm = store.normalize_realm(_pick(raw, 'realm', 'site', 'region'))
        access = store.normalize_token(
            _pick(raw, 'access_token', 'accessToken', 'token', 'auth_token',
                  'authToken', 'cookie', 'cookies', 'key'))
        refresh = store.normalize_refresh(
            _pick(raw, 'refresh_token', 'refreshToken', 'refresh'))
        if not access and not refresh:
            raise PlatformError(
                '需要提供 Accio 的 access token 或 refresh token',
                status=400, code='invalid_account')

        identity = _pick(raw, 'user_id', 'userId', 'userName', 'email',
                         'phone', 'nickname', 'uid')
        try:
            uid = _pick(raw, 'uid')
            if uid:
                uid = store.safe_id(uid)
            else:
                uid = store.derive_uid(realm, identity, access, refresh)
        except ValueError as exc:
            raise PlatformError(str(exc), status=400,
                                code='invalid_account') from exc

        old = store.read_account(uid) or {}
        next_access = access or self._access_of(old)
        next_refresh = refresh or str(old.get('refreshToken') or '')
        expires = P.expires_at_ms(_pick(raw, 'expires_at', 'expiresAt',
                                        'expiredAt'))
        source = _pick(raw, 'source')
        if source not in ('manual', 'desktop', 'web', 'oauth'):
            source = str(old.get('source') or 'manual')

        account = {
            **old,
            'uid': uid,
            'realm': realm,
            'accessToken': next_access,
            'refreshToken': next_refresh,
            'expiredAt': store.iso_from_epoch(expires / 1000.0)
            if expires else str(old.get('expiredAt') or ''),
            'utdid': _pick(raw, 'utdid', 'utdidValue')
            or str(old.get('utdid') or '') or store.new_utdid(),
            'sgcookie': _pick(raw, 'sgcookie', 'sgCookie')
            or str(old.get('sgcookie') or ''),
            'teamId': _pick(raw, 'team_id', 'teamId')
            or str(old.get('teamId') or ''),
            'teamName': _pick(raw, 'team_name', 'teamName')
            or str(old.get('teamName') or ''),
            'routeRegion': _pick(raw, 'route_region', 'routeRegion')
            or str(old.get('routeRegion') or ''),
            'name': _pick(raw, 'name') or str(old.get('name') or ''),
            'nickname': _pick(raw, 'nickname', 'displayName')
            or str(old.get('nickname') or ''),
            'userName': _pick(raw, 'user_name', 'userName', 'nickname')
            or str(old.get('userName') or ''),
            'email': _pick(raw, 'email') or str(old.get('email') or ''),
            'phone': _pick(raw, 'phone', 'mobile')
            or str(old.get('phone') or ''),
            'proxy': _pick(raw, 'proxy', 'egress') or str(old.get('proxy') or ''),
            'source': source,
            'lastRefreshError': '',
            'loginState': '正常',
        }
        self._credits.pop(uid, None)
        self._drop_token(uid)
        return store.public_account(store.save_account(account))

    def delete_account(self, account_id: str) -> bool:
        uid = str(account_id or '')
        self._credits.pop(uid, None)
        self._drop_token(uid)
        return store.delete_account(account_id)

    async def refresh_account(self, account_id: str, *,
                              force: bool = False) -> dict:
        """续期一次 token，并读一次 `userinfo`（顺带探活、补昵称 / 团队）。"""
        account = self._require(account_id)
        uid = str(account.get('uid') or '')
        proxy = self._proxy_for(account)
        try:
            access = await self._token_for(account, proxy=proxy, force=True)
        except PlatformError as exc:
            if exc.status != 401:
                try:
                    store.patch_account(uid, {
                        'lastRefreshError': exc.message[:200]})
                except (FileNotFoundError, ValueError, OSError):
                    pass
            raise

        patch: dict[str, Any] = {
            'refreshedAt': store.iso_from_epoch(time.time()),
            'lastRefreshError': '',
            'loginState': '正常',
        }
        try:
            info = await client.user_info(
                account, access, proxy=proxy,
                timeout=self._timeout('credits_timeout', C.REQUEST_TIMEOUT))
        except PlatformError as exc:
            if exc.status == 401:
                self._mark_expired(uid, exc.message)
                raise
            # 账号信息读不到不影响「token 已经换好」这个结论。
            patch['lastRefreshError'] = exc.message[:200]
            info = {}
        if info.get('userName'):
            patch['userName'] = info['userName']
            patch['nickname'] = info['userName']
        if info.get('routeRegion'):
            patch['routeRegion'] = info['routeRegion']
        team = info.get('activeTeam')
        if isinstance(team, dict) and str(team.get('teamId') or '').strip():
            patch['teamId'] = str(team['teamId']).strip()
            patch['teamName'] = str(team.get('teamName') or '').strip()
        self._credits.pop(uid, None)
        return store.public_account(store.patch_account(uid, patch))

    # ── 网页登录 ──────────────────────────────────────────
    def login_start(self, *, realm: str | None = None,
                    mode: str | None = None) -> dict:
        """发起一次登录（`mode` 对 Accio 无意义，忽略）。"""
        from . import login as loginmod  # noqa: PLC0415 —— login 反向依赖 adapter
        return loginmod.start(realm)

    def login_poll(self, login_id: str) -> dict:
        from . import login as loginmod  # noqa: PLC0415
        return loginmod.poll(login_id)

    def login_cancel(self, login_id: str) -> bool:
        from . import login as loginmod  # noqa: PLC0415
        return loginmod.cancel(login_id)

    def login_submit_callback(self, login_id: str, callback: str) -> dict:
        """把授权页地址栏里的完整链接粘回来，完成兑换。"""
        from . import login as loginmod  # noqa: PLC0415
        return loginmod.submit_callback(login_id, callback)

    # ── 签到 ──────────────────────────────────────────────
    async def checkin(self, account_id: str) -> dict:
        """Accio 没有签到端点（每日积分由登录触发发放）。"""
        raise PlatformError(
            'Accio 没有签到端点：每日积分由登录时由服务端发放，'
            '没有可调用的签到接口',
            status=400, code='checkin_unsupported')

    # ── 积分 / 配额 ───────────────────────────────────────
    async def _snapshot(self, account: dict) -> tuple[dict, dict]:
        """拉一次 `currentSubscription` + `quota`（quota 失败可容忍）。"""
        uid = str(account.get('uid') or '')
        proxy = self._proxy_for(account)
        access = await self._token_for(account, proxy=proxy)
        timeout = self._timeout('credits_timeout', C.REQUEST_TIMEOUT)
        data = await client.subscription(account, access, proxy=proxy,
                                         timeout=timeout)
        quota: dict = {}
        try:
            quota = await client.quota(account, access, proxy=proxy,
                                       timeout=timeout)
        except PlatformError:
            quota = {}
        return data, quota

    async def usage(self, account_id: str) -> dict:
        """额度 / 用量快照（三类积分桶 + 每日重置倒计时）。"""
        account = self._require(account_id)
        uid = str(account.get('uid') or '')
        realm = self._realm_of(account)
        out: dict[str, Any] = {
            'uid': uid,
            'realm': realm,
            'session_usage': None,
            'entitlement': None,
            'errors': [],
        }
        try:
            data, quota = await self._snapshot(account)
        except PlatformError as exc:
            out['errors'].append({'endpoint': 'entitlement/currentSubscription',
                                  'code': exc.code, 'message': exc.message})
            return out
        buckets = _read_buckets(data)
        total = _bucket_total(data)
        if total is None:
            total = _sum_of(buckets, 'remaining')
        out['session_usage'] = {
            'total': total,
            'used': _sum_of(buckets, 'used'),
            'remaining': total,
            'usage_percent': _num(quota.get('usagePercent')),
            'refresh_countdown_seconds': _num(
                quota.get('refreshCountdownSeconds')),
            'at': int(time.time()),
        }
        out['entitlement'] = {
            'quota_known': True,
            'remaining': total,
            'buckets': buckets,
        }
        return out

    async def credits(self, account_id: str) -> dict:
        """积分余额快照（默认 60 秒缓存，`platforms.accio.credits_ttl`）。"""
        account = self._require(account_id)
        uid = str(account.get('uid') or '')
        cached = self._credits.get(uid)
        if cached and cached[0] > time.time():
            return cached[1]

        data, quota = await self._snapshot(account)
        buckets = _read_buckets(data)
        total = _bucket_total(data)
        if total is None:
            total = _sum_of(buckets, 'remaining')
        if not buckets and _bucket_total(data) is None:
            # 查询「成功」但形状不认识：不编造 0（0 会被读成额度已用光）。
            raise PlatformError('Accio 积分查询失败：响应未包含余额字段',
                                status=502, code='bad_response')

        countdown = _num(quota.get('refreshCountdownSeconds'))
        next_expire = (int(time.time() + countdown)
                       if countdown is not None and countdown > 0 else None)
        # 只有每日桶知道周期何时结束（quota 的倒计时）；其余桶不臆造到期时刻。
        expires = []
        if next_expire is not None:
            daily = next((row for row in buckets if row['key'] == 'daily'), None)
            if daily is not None:
                expires.append({
                    'amount': daily['remaining'],
                    'expire_at': next_expire,
                    'label': daily['label'],
                })
        realm = self._realm_of(account)
        out = {
            'uid': uid,
            'realm': realm,
            'credits': total,
            'remain': total,
            'limit': _sum_of(buckets, 'total') if buckets else None,
            'used': _sum_of(buckets, 'used') if buckets else None,
            'unit': C.CREDIT_UNIT,
            'expires': expires,
            'next_expire_at': next_expire,
            'buckets': buckets,
            'source': 'entitlement/currentSubscription',
        }
        self._credits[uid] = (
            time.time() + runtime_settings.credits_ttl(self.name), out)
        try:
            store.patch_account(uid, {'balance': {
                'state': 'ok', 'at': int(time.time()), 'message': '',
                'remaining': total,
            }})
        except (FileNotFoundError, ValueError, OSError):
            pass
        return out

    # ── 模型目录 ──────────────────────────────────────────
    @staticmethod
    def _models_key(realm: str) -> str:
        return SETTING_MODELS.format(realm=store.normalize_realm(realm))

    @staticmethod
    def _attempt_key(realm: str) -> str:
        return SETTING_MODELS_ATTEMPT.format(realm=store.normalize_realm(realm))

    def _cached_models(self, realm: str) -> list[dict] | None:
        try:
            cached = db.get_setting(self._models_key(realm), None)
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(cached, dict):
            return None
        if float(cached.get('fetched_at') or 0) \
                + runtime_settings.catalog_ttl(self.name) < time.time():
            return None
        models = catalog.normalize(cached.get('models'), realm=realm)
        return models or None

    def _stored_models(self, realm: str) -> list[dict] | None:
        """缓存目录（过期也算）：给本地模型校验 / 反查用。"""
        try:
            cached = db.get_setting(self._models_key(realm), None)
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(cached, dict):
            return None
        models = catalog.normalize(cached.get('models'), realm=realm)
        return models or None

    def _save_models(self, realm: str, models: list[dict]) -> None:
        try:
            db.set_setting(self._models_key(realm),
                           {'fetched_at': int(time.time()), 'models': models})
        except Exception:  # noqa: BLE001 —— 缓存写不进去不影响本次返回
            pass

    def _implicit_live_allowed(self, realm: str) -> bool:
        try:
            last = float(db.get_setting(self._attempt_key(realm), 0) or 0)
        except Exception:  # noqa: BLE001
            return False
        return time.time() - last > runtime_settings.catalog_retry_ttl(
            self.name)

    def _note_implicit_attempt(self, realm: str) -> None:
        try:
            db.set_setting(self._attempt_key(realm), int(time.time()))
        except Exception:  # noqa: BLE001
            pass

    async def _live_models(self, realm: str, *, timeout: float) -> list[dict]:
        """用一个本区可用账号拉一次上游目录；失败就换下一个。

        **只在本区内换号**：跨区拿到的是另一份名单，混进来等于谎报可用模型。
        """
        for account in store.list_accounts():
            if self._realm_of(account) != realm:
                continue
            if store.is_disabled(account):
                continue
            if not (self._access_of(account) or self._refresh_of(account)):
                continue
            proxy = None
            try:
                proxy = self._proxy_for(account)
                access = await self._token_for(account, proxy=proxy)
                models = await client.fetch_catalog(
                    account, access, proxy=proxy, timeout=timeout)
            except PlatformError:
                continue
            if models:
                return models
        return []

    async def list_models(self, *, refresh: bool = False) -> list[dict]:
        """两个区域的目录各拉各的，最后合并成一份带区域前缀的清单。"""
        out: list[dict] = []
        for realm in C.REALMS:
            models = None if refresh else self._cached_models(realm)
            if models is None and (refresh or self._implicit_live_allowed(realm)):
                live = await self._live_models(
                    realm,
                    timeout=45 if refresh
                    else runtime_settings.catalog_timeout(self.name))
                self._note_implicit_attempt(realm)
                if live:
                    self._save_models(realm, live)
                    models = live
            # 没有离线快照：本区没拉到就是空表，不借另一区的名单。
            for raw in models or ():
                out.append(self._public_model(raw, realm))
        # 反查表同时挂裸 slug 与 `区域:slug`：两个区域可能有同名模型但
        # `modelCode` 不同，对话时按账号区域优先命中。
        meta: dict[str, dict] = {}
        for model in out:
            slug = str(model.get('model_name') or '').strip().lower()
            if not slug:
                continue
            meta.setdefault(slug, model)
            meta[f'{model.get("realm") or ""}:{slug}'] = model
        self._model_meta = meta
        return out

    def _public_model(self, raw: dict, realm: str) -> dict:
        slug = str(raw.get('slug') or raw.get('id') or '')
        modalities = raw.get('modalities')
        if not isinstance(modalities, list) or not modalities:
            modalities = ['text']
        levels = [str(item) for item in raw.get('efforts') or []
                  if str(item or '').strip()]
        credit = raw.get('multiple')
        return {
            'id': self.public_model_id(slug, realm),
            'object': 'model',
            'created': 0,
            'owned_by': self.name,
            'name': str(raw.get('label') or raw.get('name') or slug),
            'platform': self.name,
            'realm': realm,
            'context_window': int(_num(raw.get('context_length')) or 0),
            'max_tokens': int(_num(raw.get('max_tokens')) or 0),
            'supports_reasoning': bool(raw.get('reasoning')),
            'reasoning_efforts': levels,
            'default_reasoning_effort': raw.get('default_effort') or None,
            'modalities': {'input': [str(x) for x in modalities]},
            'model_name': slug,
            'upstream_model': str(raw.get('id') or ''),
            'tool_call': bool(raw.get('tool_call', True)),
            'attachment': bool(raw.get('attachment') or 'image' in modalities),
            'credit_ratio': _num(credit) if credit not in ('', None) else None,
            'free': False,
            'badge': '',
            'description': '',
        }

    def cached_model_ids(self) -> set[str] | None:
        """已缓存的对外模型 id（`acciocn/...`、`acciocom/...`），只读不联网。"""
        out: set[str] = set()
        for realm in C.REALMS:
            for raw in self._stored_models(realm) or ():
                slug = str(raw.get('slug') or '').strip()
                if slug:
                    out.add(self.public_model_id(slug, realm))
        return out or None

    def upstream_model(self, model: str, realm: str | None = None) -> str:
        """成员名（slug）→ 上游 `modelCode`；反查不到原样返回。

        目录里的 `modelCode` 是形如 `1Drift-N8kP2Q7wR5mX` 的内部名，**不能**
        凭展示名维护；所以只能靠目录反查，查不到就原样发（由上游报错）。
        """
        text = str(model or '').strip()
        if text.lower() in ('', 'default'):
            return ''
        key = text.lower()
        region = store.normalize_realm(realm) if realm else ''
        if region:
            meta = self._model_meta.get(f'{region}:{key}')
            if meta is not None:
                return str(meta.get('upstream_model') or text)
        meta = self._model_meta.get(key)
        if meta is not None:
            return str(meta.get('upstream_model') or text)
        for item_realm in ([region] if region else C.REALMS):
            for raw in self._stored_models(item_realm) or ():
                if str(raw.get('slug') or '').strip().lower() == key:
                    return str(raw.get('id') or text)
        return text

    def _assert_known_model(self, model: str, realm: str) -> None:
        """本区缓存过目录时，按目录拒绝未知模型（大小写不敏感）。"""
        text = str(model or '').strip()
        if not text or text.lower() == 'default':
            return
        known = {str(item.get('slug') or '').strip().lower()
                 for item in (self._stored_models(realm) or ())
                 if item.get('slug')}
        if known and text.lower() not in known:
            raise PlatformError(f'Accio 没有模型 {text}', status=400,
                                code='model_not_found')

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
    def _candidates(self, realm: str | None = None,
                    affinity: str | None = None) -> list[dict]:
        wanted = store.normalize_realm(realm) if realm else None
        accounts = [a for a in store.list_accounts()
                    if str(a.get('uid') or '').strip()
                    and (self._access_of(a) or self._refresh_of(a))
                    and not store.is_disabled(a)
                    and not store.is_cooling(a)
                    and (wanted is None or self._realm_of(a) == wanted)]
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
        """按失败分级处理；返回 True = 应换下一个账号。"""
        kind = _kind_of(exc)
        if not P.should_rotate(kind):
            return False
        if not P.records_cooldown(kind):
            # 401 / 403：只换号，绝不写冷却（写冷却会误伤一池健康账号）。
            return True
        if kind == P.KIND_QUOTA:
            seconds = _seconds_until_utc8_midnight()
        else:  # KIND_RATE
            seconds = float(exc.retry_after or C.RATE_LIMIT_COOLDOWN)
        if seconds <= 0 or not uid:
            return True
        try:
            store.set_cooling(uid, time.time() + seconds, f'chat {exc.status}')
        except (FileNotFoundError, ValueError, OSError):
            return True
        return True

    # ── 对话 ──────────────────────────────────────────────
    async def chat(self, model: str, body: dict,
                   ctx: ChatContext | None = None) -> PlatformChat:
        return await self._chat_accounts(model, body, ctx)

    async def _chat_accounts(self, model: str, body: dict,
                             ctx: ChatContext | None) -> PlatformChat:
        runtime = runtime_settings.load(self.name)
        affinity = affinity_key(body) if runtime['session_affinity'] else ''
        if affinity:
            self._affinity.set_ttl(runtime['session_affinity_ttl'])

        realm_hint = str(getattr(ctx, 'realm', '') or '').strip() or None
        if not str(model or '').strip():
            raise PlatformError('请指定 Accio 模型（例如 acciocn/deepseek-v4-1-flash）',
                                status=400, code='model_required')

        candidates = self._candidates(realm_hint, affinity)
        if not candidates:
            detail = (f'（{store.realm_label(realm_hint)}）'
                      if realm_hint else '')
            raise PlatformError(f'没有可用的 Accio 账号{detail}'
                                '（请在面板里登录或粘贴 token）',
                                status=503, code='no_account')

        last_error: PlatformError | None = None
        for account in candidates[:C.MAX_ACCOUNTS_TRY]:
            uid = str(account.get('uid') or '')
            realm = self._realm_of(account)
            wrapper_model = self.public_model_id(model, realm)
            lease = None
            release: Callable[[], None] = _noop
            try:
                proxy = self._proxy_for(account)
                lease = await self._limiter.acquire(uid)
                if lease is False:
                    lease = None
                    last_error = PlatformError(
                        f'Accio 账号并发已满（单账号上限 '
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
                    # access token 失效：续期一次再试（同一账号，不消耗选号次数）。
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
        raise last_error or PlatformError('Accio 账号均不可用', status=503,
                                          code='no_account')

    def _build_upstream_body(self, model: str, body: dict,
                             access: str, account: dict) -> dict:
        try:
            return P.build_chat_body(
                model=self.upstream_model(model, self._realm_of(account)),
                token=access,
                messages=body.get('messages'), tools=body.get('tools'),
                temperature=body.get('temperature'),
                max_output_tokens=_max_tokens_of(body),
                utdid=store.utdid_for(account))
        except P.UnsupportedContent as exc:
            raise PlatformError(str(exc), status=400,
                                code='unsupported_content') from exc

    async def _chat_once(self, account: dict, model: str, body: dict,
                         wrapper_model: str, *, proxy: str | None,
                         runtime: dict, affinity: str = '',
                         force_token: bool = False) -> PlatformChat:
        uid = str(account.get('uid') or '')
        realm = self._realm_of(account)
        # 本区缓存过目录时先做一次本地校验：上游对未知模型的行为不确定（可能
        # 静默回落），等到首帧再发现就晚了。缓存为空时该检查是 no-op。
        self._assert_known_model(model, realm)
        access = await self._token_for(account, proxy=proxy, force=force_token)
        payload = self._build_upstream_body(model, body, access, account)

        if not body.get('stream'):
            response = await client.open_chat(
                account, access, payload, proxy=proxy,
                idle_timeout=float(runtime['idle_timeout']))
            frames: list[dict] = []
            try:
                async for frame in client.iter_frames(response):
                    frames.append(frame)
            finally:
                try:
                    await response.aclose()
                except Exception:  # noqa: BLE001 —— 收尾失败不该影响记账
                    pass
            data = chatmod.aggregate(frames, wrapper_model)
            return PlatformChat(payload=data, usage=dict(data.get('usage') or {}),
                                model=wrapper_model, account_hint=uid,
                                realm=realm)

        response = await client.open_chat(
            account, access, payload, proxy=proxy,
            idle_timeout=float(runtime['idle_timeout']))

        usage: dict = {}
        translator = chatmod.StreamTranslator(wrapper_model)
        chat = PlatformChat(usage=usage, model=wrapper_model,
                            account_hint=uid, realm=realm)
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
            try:
                for chunk in translator.start():
                    yield chunk
                async for frame in client.iter_frames(response):
                    for chunk in translator.feed(frame):
                        yield chunk
                for chunk in translator.finish():
                    yield chunk
                usage.update(translator.usage_dict())
                if not translator.has_content:
                    raise PlatformError('Accio 没有返回内容', status=502,
                                        code='empty_response')
            finally:
                await _close()

        chat.stream = relay()
        # 即使网关还没开始迭代就收尾（客户端提前断开 / 上层报错），也要保证
        # 上游连接被关闭：`relay` 的 finally 与这里都调 `_close`，靠幂等收口。
        chat.closer = _close
        return chat


adapter = AccioAdapter()
