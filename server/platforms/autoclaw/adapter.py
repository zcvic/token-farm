"""AutoClaw（智谱 / Z.ai）平台适配器。

与其它原生平台同构：本文件只负责**策略**，协议在 `client.py`。

  · 双区选号（权重 SWRR + LRU + 会话粘性）与单账号并发闸门；
  · 一次性 refresh token 的轮换与**先落盘再使用**；
  · 模型目录按区域独立缓存（没有离线快照）；
  · 签到 / 积分 / 任务；
  · 流式（OpenAI chunk）/ 非流式两条出口的最后一步。

`realm_aware=True`：国内站（`autoclawcn/`）与国际站（`autoclawcom/`）是两套
互相独立的账号体系，token 不通用、域名不同、版本号不同，账号按区域分池，
模型按区域加前缀；旧式 `autoclaw/` 仍可调用（按账号池里有什么就选什么）。

## 区域不猜

区域**只**来自账号记录与模型前缀，绝不从模型名或 token 猜（`protocol.
realm_hint` 只能认出「国内短信号」，认不出时按记录走）。把国内 token 发到
国际域只会得到 401，而 401 会让适配器把好号标成需重登。

## 单模型额度耗尽不是账号死亡

`810000` 是**单模型**额度耗尽：它只冷却「账号 + 模型」，不能冻整号（冻了会
把同一账号下还能用的其它模型一起误伤）。冷却表在内存里，不落盘——它是上游
配额的短期事实，进程重启后重新试一次没有代价。

## 有意不做

- 不写死离线模型目录：某区没有可用账号时目录就是空表，**不**借另一区的；
- 不改写 system / messages 去过 2.x 的 406，也不做 force-model 静默改写；
- 不内嵌阿里云滑块：国际 OAuth 只做「打开授权页 + 粘回调」。
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
from . import catalog, client, constants as C, protocol as P, store

logger = logging.getLogger('workbuddy.platforms.autoclaw')

#: 模型目录缓存按区域分开：两区的模型名单不同，共用一份会让国际站显示国内
#: 的模型名（发过去必然报错）。
SETTING_MODELS = 'platforms.autoclaw.models.{realm}'
SETTING_MODELS_ATTEMPT = 'platforms.autoclaw.models_attempt.{realm}'

# 积分 / 额度耗尽（402）：等充值或次日发放，冻 10 分钟足够让批量任务改投。
CREDITS_COOLDOWN_SECONDS = 600
# 单模型额度耗尽（810000）：只摘掉「这个账号的这个模型」，冷却 15 分钟。
MODEL_COOLDOWN_SECONDS = 900
# 刷新失败先冷一会儿：上游抖动时不该每个请求都轮换一次一次性 token。
REFRESH_FAIL_COOLDOWN = 60


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


class AutoclawAdapter(PlatformAdapter):
    name = 'autoclaw'
    label = 'AutoClaw（智谱 / Z.ai）'
    # 签到：国际站有 `daily_signin` 任务；国内站按任务列表判断（没有该任务时
    # 如实返回「没有可签到的任务」，不报错，见 `checkin`）。
    supports_checkin = True
    # 网页登录：国内短信验证码 / 国际 OAuth「打开授权页 + 粘回调」。
    supports_web_login = True
    # 凭证是 access + refresh 两个 token，不是整段网页 Cookie。
    supports_cookie_import = False
    # 国内 / 国际两套账号体系。
    realm_aware = True

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_used: dict[str, float] = {}
        # uid -> (access token 到期时刻, access token)。refresh token 永远以
        # 磁盘为准：它是一次性的，内存副本轮换后立刻过期。
        self._tokens: dict[str, tuple[float, str]] = {}
        # uid -> (过期时刻, credits 结果)
        self._credits: dict[str, tuple[float, dict]] = {}
        # (uid, 小写模型名) -> 冷却截止时刻（单模型额度耗尽用）
        self._model_cool: dict[tuple[str, str], float] = {}
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
        return str(account.get('accessToken') or account.get('token')
                   or '').strip()

    @staticmethod
    def _realm_of(account: dict) -> str:
        return store.normalize_realm(account.get('realm'))

    @staticmethod
    def _device_of(account: dict) -> str:
        return store.device_id_for(account)

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
            realm = self._realm_of(fresh)
            access = self._access_of(fresh)
            expires = store.epoch_from_iso(fresh.get('expiredAt')) \
                or store.jwt_exp(access)
            refresh = self._refresh_of(fresh)

            if access and not force and expires \
                    and expires - C.TOKEN_SKEW_SECONDS > time.time():
                self._tokens[uid] = (expires, access)
                return access

            if not refresh:
                if access and not force:
                    # 手工粘贴的 access token 没有 refresh：先用一次，401 再换号。
                    self._tokens[uid] = (
                        expires or time.time() + C.TOKEN_SKEW_SECONDS, access)
                    return access
                self._mark_expired(uid, 'refresh token 缺失')
                raise PlatformError(
                    'AutoClaw 账号缺少 refresh token，请重新登录或导入',
                    status=401, code='login_expired')

            device = self._device_of(fresh)
            try:
                data = await client.refresh(
                    realm, refresh, device_id=device, proxy=proxy,
                    timeout=self._timeout('request_timeout', C.TOKEN_TIMEOUT))
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
                        store.set_cooling(uid, time.time()
                                          + REFRESH_FAIL_COOLDOWN,
                                          'refresh failed')
                    except (FileNotFoundError, ValueError, OSError):
                        pass
                raise

            new_access = str(data.get('access_token') or '')
            new_refresh = str(data.get('refresh_token') or '') or refresh
            new_expires = float(data.get('expires_at') or 0) \
                or time.time() + C.TOKEN_FALLBACK_SECONDS
            refresh_expires = float(data.get('refresh_expires_at') or 0)
            # **先落盘再使用**：进程崩在中间时，至少新的 refresh 已经在文件
            # 里，不会把账号永久踢下线。新 refresh 没带寿命时清空旧值——旧值
            # 描述的是已经作废的那一枚（见 `services/renew.py`）。
            try:
                store.patch_account(uid, {
                    'accessToken': new_access,
                    'refreshToken': new_refresh,
                    'expiredAt': store.iso_from_epoch(new_expires),
                    'refreshExpiredAt': store.iso_from_epoch(refresh_expires)
                    if refresh_expires > 0 else '',
                    'refreshedAt': store.iso_from_epoch(time.time()),
                    'lastRefreshError': '',
                    'loginState': '正常',
                })
            except (FileNotFoundError, ValueError, OSError) as exc:
                raise PlatformError(f'保存 AutoClaw 新 token 失败：{exc}',
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
        """导入账号：access token +（推荐）refresh token，可带区域。

        接受 `{account: {...}}` 与扁平对象两种形状。凭据字段兼容
        `access_token` / `accessToken` / `token` 与 `refresh_token` /
        `refreshToken` / `refresh`；也接受整段 Cookie（`ac_token` /
        `access_token` 会被抽出来）。导入阶段**不联网**，第一次刷新 / 探活
        在 `refresh_account` 或第一次对话时发生。

        `realm` 缺省时按国内版处理：**不猜**国际——猜错只会让一份国际 token
        打到国内域，然后被 401 标成「登录失效」。
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
                '需要提供 AutoClaw 的 access token 或 refresh token',
                status=400, code='invalid_account')

        # uid 落在**稳定身份**上：同一手机号 / 同一用户重新登录应覆盖同一条
        # 账号，而不是因为换了一枚 refresh token 就多出一条。
        identity = _pick(raw, 'phone', 'email', 'user_id', 'userId', 'uid')
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
        if not access and str(old.get('refreshToken') or '') == refresh:
            access = self._access_of(old)
        access_exp = store.jwt_exp(access) \
            or store.epoch_from_iso(_pick(raw, 'expiresAt', 'expiredAt',
                                          'expires_at'))
        if not access_exp and access:
            access_exp = time.time() + C.TOKEN_FALLBACK_SECONDS
        refresh_exp = store.jwt_exp(refresh) \
            or store.epoch_from_iso(_pick(raw, 'refreshExpiresAt',
                                          'refresh_expires_at'))
        next_refresh = refresh or str(old.get('refreshToken') or '')
        if not refresh_exp and next_refresh == str(old.get('refreshToken') or ''):
            refresh_exp = store.epoch_from_iso(old.get('refreshExpiredAt'))

        device = _pick(raw, 'device_id', 'deviceId') \
            or str(old.get('deviceId') or '') \
            or store.device_id_for({'uid': uid, 'accessToken': access,
                                    'refreshToken': next_refresh})
        source = _pick(raw, 'source')
        if source not in ('manual', 'desktop', 'web', 'sms', 'oauth'):
            source = str(old.get('source') or 'manual')

        account = {
            **old,
            'uid': uid,
            'realm': realm,
            'accessToken': access or self._access_of(old),
            'refreshToken': next_refresh,
            'expiredAt': store.iso_from_epoch(access_exp)
            if access_exp else str(old.get('expiredAt') or ''),
            'refreshExpiredAt': store.iso_from_epoch(refresh_exp)
            if refresh_exp else '',
            'deviceId': device,
            'name': _pick(raw, 'name') or str(old.get('name') or ''),
            'nickname': _pick(raw, 'nickname', 'displayName')
            or str(old.get('nickname') or ''),
            'email': _pick(raw, 'email') or str(old.get('email') or ''),
            'phone': _pick(raw, 'phone', 'mobile') or str(old.get('phone') or ''),
            'proxy': _pick(raw, 'proxy', 'egress') or str(old.get('proxy') or ''),
            'source': source,
            'lastRefreshError': '',
            'loginState': '正常',
        }
        self._credits.pop(uid, None)
        self._drop_token(uid)
        self._forget_model_cool(uid)
        return store.public_account(store.save_account(account))

    def delete_account(self, account_id: str) -> bool:
        uid = str(account_id or '')
        self._credits.pop(uid, None)
        self._drop_token(uid)
        self._forget_model_cool(uid)
        return store.delete_account(account_id)

    async def refresh_account(self, account_id: str, *,
                              force: bool = False) -> dict:
        """轮换一次 token，并读一次 `user-info`（顺带探活、补昵称）。"""
        account = self._require(account_id)
        uid = str(account.get('uid') or '')
        realm = self._realm_of(account)
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
            'realm': realm,
        }
        try:
            info = await client.user_info(
                realm, access, device_id=self._device_of(account),
                proxy=proxy,
                timeout=self._timeout('credits_timeout', C.ACCOUNT_TIMEOUT))
        except PlatformError as exc:
            if exc.status == 401:
                self._mark_expired(uid, exc.message)
                raise
            # 账号信息读不到不影响「token 已经换好」这个结论。
            patch['lastRefreshError'] = exc.message[:200]
            info = {}
        if info.get('name'):
            patch['name'] = info['name']
        if info.get('email'):
            patch['email'] = info['email']
        if info.get('phone'):
            patch['phone'] = info['phone']
        self._credits.pop(uid, None)
        return store.public_account(store.patch_account(uid, patch))

    # ── 网页登录 ──────────────────────────────────────────
    def login_start(self, *, realm: str | None = None,
                    mode: str | None = None) -> dict:
        """发起一次登录：国内短信 / 国际 OAuth。

        `mode` 对国内无意义（恒为短信），对国际可传 `google` / `zai` 选提供方。
        """
        from . import login as loginmod  # noqa: PLC0415 —— login 反向依赖 adapter
        return loginmod.start(realm, mode or '')

    def login_poll(self, login_id: str) -> dict:
        from . import login as loginmod  # noqa: PLC0415
        return loginmod.poll(login_id)

    def login_cancel(self, login_id: str) -> bool:
        from . import login as loginmod  # noqa: PLC0415
        return loginmod.cancel(login_id)

    def login_submit_callback(self, login_id: str, callback: str) -> dict:
        """国际 OAuth：把授权页地址栏里的完整链接粘回来。"""
        from . import login as loginmod  # noqa: PLC0415
        return loginmod.submit_callback(login_id, callback)

    def login_send_code(self, login_id: str, phone: str) -> dict:
        """国内短信：下发验证码。"""
        from . import login as loginmod  # noqa: PLC0415
        return loginmod.send_code(login_id, phone)

    def login_submit_code(self, login_id: str, code: str) -> dict:
        """国内短信：提交验证码，成功后落盘账号。"""
        from . import login as loginmod  # noqa: PLC0415
        return loginmod.submit_code(login_id, code)

    # ── 签到 / 任务 ───────────────────────────────────────
    async def checkin(self, account_id: str) -> dict:
        """每日签到：按任务列表判断本区有没有 `daily_signin`。

        国际站的任务列表里固定有 `daily_signin`，重复调用幂等
        （`already_completed=true`）。国内站**可能没有**这个任务（参考实现互相
        矛盾，见 `deploy/autoclaw-ref.txt`），所以这里先读任务列表：没有该任务
        就如实回「没有可签到的任务」，而不是让每天的批量签到记一串失败。

        任务接口本身不可用（404 / 路径不对）同样按「本区没有签到」处理；
        只有鉴权 / 额度类错误才上抛。
        """
        account = self._require(account_id)
        uid = str(account.get('uid') or '')
        realm = self._realm_of(account)
        proxy = self._proxy_for(account)
        timeout = self._timeout('credits_timeout', C.ACCOUNT_TIMEOUT)
        access = await self._token_for(account, proxy=proxy)

        try:
            tasks = await client.task_list(
                realm, access, device_id=self._device_of(account),
                proxy=proxy, timeout=timeout)
        except PlatformError as exc:
            if exc.status in (401, 402) or client.is_path_miss(exc):
                if exc.status == 401:
                    raise
                return self._no_checkin(uid, realm,
                                        '本区没有可用的签到任务接口')
            raise

        task = _find_task(tasks, C.TASK_DAILY_SIGNIN)
        if task is None:
            return self._no_checkin(uid, realm, '本区没有每日签到任务')
        if _task_done(task):
            return await self._checkin_result(
                uid, realm, ok=True, already=True, reward=0.0,
                message='今日已签到', access=access, proxy=proxy,
                timeout=timeout)

        try:
            result = await client.checkin_task(
                realm, access, device_id=self._device_of(account),
                proxy=proxy, timeout=timeout)
        except PlatformError as exc:
            if exc.status in (401, 402) or client.is_path_miss(exc):
                if exc.status == 401:
                    raise
                return self._no_checkin(uid, realm, '本区没有每日签到任务')
            raise

        already = bool(result.get('already'))
        reward = float(_num(result.get('reward')) or 0.0)
        return await self._checkin_result(
            uid, realm, ok=True, already=already, reward=reward,
            message=str(result.get('message') or
                        ('今日已签到' if already else '签到成功')),
            access=access, proxy=proxy, timeout=timeout)

    def _no_checkin(self, uid: str, realm: str, message: str) -> dict:
        return {
            'ok': True,
            'checked_in': False,
            'already': False,
            'credits': 0,
            'earned_credit': 0,
            'message': message,
            'uid': uid,
            'realm': realm,
            'reward': 0,
            'continuous_days': None,
        }

    async def _checkin_result(self, uid: str, realm: str, *, ok: bool,
                              already: bool, reward: float, message: str,
                              access: str, proxy: str | None,
                              timeout: float) -> dict:
        total: float | None = None
        try:
            total = await self._wallet_total(realm, access, proxy=proxy,
                                             timeout=timeout)
        except PlatformError:
            total = None
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
        self._credits.pop(uid, None)
        return {
            'ok': ok,
            'checked_in': ok,
            'already': already,
            'credits': total if total is not None else 0,
            'earned_credit': 0 if already else reward,
            'message': message,
            'uid': uid,
            'realm': realm,
            'reward': reward,
            'continuous_days': None,
        }

    async def _wallet_total(self, realm: str, access: str, *,
                            proxy: str | None, timeout: float) -> float:
        out = await client.wallets(realm, access, proxy=proxy, timeout=timeout)
        return float(_num(out.get('total')) or 0.0)

    async def usage(self, account_id: str) -> dict:
        """额度 / 用量快照（积分钱包）。"""
        account = self._require(account_id)
        uid = str(account.get('uid') or '')
        realm = self._realm_of(account)
        proxy = self._proxy_for(account)
        out: dict[str, Any] = {
            'uid': uid,
            'realm': realm,
            'session_usage': None,
            'entitlement': None,
            'errors': [],
        }
        try:
            access = await self._token_for(account, proxy=proxy)
            total = await self._wallet_total(
                realm, access, proxy=proxy,
                timeout=self._timeout('credits_timeout', C.ACCOUNT_TIMEOUT))
        except PlatformError as exc:
            out['errors'].append({'endpoint': 'wallets',
                                  'code': exc.code, 'message': exc.message})
            return out
        out['session_usage'] = {
            'total': total,
            'used': None,
            'remaining': total,
            'at': int(time.time()),
        }
        out['entitlement'] = {'quota_known': True, 'remaining': total}
        return out

    async def credits(self, account_id: str) -> dict:
        """积分余额快照（默认 60 秒缓存，`platforms.autoclaw.credits_ttl`）。"""
        account = self._require(account_id)
        uid = str(account.get('uid') or '')
        cached = self._credits.get(uid)
        if cached and cached[0] > time.time():
            return cached[1]

        realm = self._realm_of(account)
        proxy = self._proxy_for(account)
        access = await self._token_for(account, proxy=proxy)
        total = await self._wallet_total(
            realm, access, proxy=proxy,
            timeout=self._timeout('credits_timeout', C.ACCOUNT_TIMEOUT))
        out = {
            'uid': uid,
            'realm': realm,
            'credits': total,
            'remain': total,
            'limit': None,
            'used': None,
            'unit': 'points',
            'expires': [],
            'next_expire_at': None,
            'source': 'agent-assetmgr/wallets',
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
        """缓存目录（过期也算）：给本地模型校验用。"""
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

    async def _live_models(self, realm: str, *,
                           timeout: float) -> list[dict]:
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
                    realm, access, device_id=self._device_of(account),
                    proxy=proxy, timeout=timeout)
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
        self._model_meta = {
            str(m.get('model_name') or '').strip().lower(): m for m in out
            if m.get('model_name')}
        return out

    def _public_model(self, raw: dict, realm: str) -> dict:
        model_id = str(raw.get('id') or '')
        modalities = raw.get('modalities')
        if not isinstance(modalities, list) or not modalities:
            modalities = ['text']
        levels = [str(item) for item in raw.get('efforts') or []
                  if str(item or '').strip()]
        credit = raw.get('credit_level')
        return {
            'id': self.public_model_id(model_id, realm),
            'object': 'model',
            'created': 0,
            'owned_by': self.name,
            'name': str(raw.get('name') or model_id),
            'platform': self.name,
            'realm': realm,
            'context_window': int(_num(raw.get('context_length')) or 0),
            'max_tokens': int(_num(raw.get('max_tokens')) or 0),
            'supports_reasoning': bool(raw.get('reasoning')),
            'reasoning_efforts': levels,
            'modalities': {'input': [str(x) for x in modalities]},
            'model_name': model_id,
            'tool_call': bool(raw.get('tool_call', True)),
            'attachment': bool(raw.get('attachment') or 'image' in modalities),
            'credit_ratio': _num(credit) if credit not in ('', None) else None,
            'free': False,
            'badge': '',
            'description': str(raw.get('tooltip') or ''),
        }

    def cached_model_ids(self) -> set[str] | None:
        """已缓存的对外模型 id（`autoclawcn/...`、`autoclawcom/...`）。

        只读不联网；两区都没缓存过时返回 None（调用方据此跳过白名单校验）。
        """
        out: set[str] = set()
        for realm in C.REALMS:
            for raw in self._stored_models(realm) or ():
                model_id = str(raw.get('id') or '')
                if model_id:
                    out.add(self.public_model_id(model_id, realm))
        return out or None

    def upstream_model(self, model: str) -> str:
        """成员名 → 上游模型名。

        AutoClaw 的模型名就是上游目录里的 `id`，没有别名映射；空名与
        `default` 都不发（上游没有「默认模型」这个概念，猜一个只会答非所问）。
        """
        text = str(model or '').strip()
        if text.lower() in ('', 'default'):
            return ''
        return text

    def _assert_known_model(self, model: str, realm: str) -> None:
        """本区缓存过目录时，按目录拒绝未知模型（大小写不敏感）。"""
        text = str(model or '').strip()
        if not text or text.lower() == 'default':
            return
        known = {str(item.get('id') or '').strip().lower()
                 for item in (self._stored_models(realm) or [])
                 if item.get('id')}
        if known and text.lower() not in known:
            raise PlatformError(f'AutoClaw 没有模型 {text}', status=400,
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

    def set_account_checkin_only(self, account_id: str, enabled: bool) -> dict:
        """只把账号从推理选号池里摘出去，签到与任务照常。"""
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
        """给账号绑定出口线路（空串 = 跟随号池，`__direct__` = 强制直连）。

        国际站账号常需要境外出口；绑好后下一次请求就用新线路。
        """
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

    # ── 单模型冷却 ────────────────────────────────────────
    def _model_key(self, uid: str, model: str) -> tuple[str, str]:
        return str(uid or ''), str(model or '').strip().lower()

    def _cool_model(self, uid: str, model: str,
                    seconds: float = MODEL_COOLDOWN_SECONDS) -> None:
        if seconds <= 0 or not uid or not model:
            return
        now = time.time()
        # 顺手清掉已经过期的键，表不会无限增长。
        for key, until in list(self._model_cool.items()):
            if until <= now:
                self._model_cool.pop(key, None)
        self._model_cool[self._model_key(uid, model)] = now + seconds

    def _model_cooling(self, uid: str, model: str) -> bool:
        until = self._model_cool.get(self._model_key(uid, model), 0.0)
        return until > time.time()

    def _forget_model_cool(self, uid: str) -> None:
        for key in [k for k in self._model_cool if k[0] == str(uid or '')]:
            self._model_cool.pop(key, None)

    # ── 选号 ──────────────────────────────────────────────
    def _candidates(self, realm: str | None = None,
                    affinity: str | None = None,
                    model: str = '') -> list[dict]:
        wanted = store.normalize_realm(realm) if realm else None
        accounts = [a for a in store.list_accounts()
                    if str(a.get('uid') or '').strip()
                    and (self._access_of(a) or self._refresh_of(a))
                    and not store.is_disabled(a)
                    and not store.is_checkin_only(a)
                    and not store.is_cooling(a)
                    and (wanted is None or self._realm_of(a) == wanted)
                    and not self._model_cooling(str(a.get('uid') or ''), model)]
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

    def _cool_down(self, uid: str, exc: PlatformError, model: str) -> bool:
        """按失败分级处理；返回 True = 应换下一个账号。"""
        failure = client.failure_of(exc)
        if failure is not None and failure.model_scoped:
            # 810000：只摘掉这个账号的这个模型，账号本身还能用别的模型。
            self._cool_model(uid, model)
            return True
        if exc.status == 402:
            seconds = CREDITS_COOLDOWN_SECONDS
        elif exc.status == 429:
            seconds = (exc.retry_after
                       or runtime_settings.cooldown_429(self.name) or 60)
        elif exc.status >= 500:
            seconds = runtime_settings.cooldown_5xx(self.name)
        elif exc.status == 403:
            # 风控 / 指纹错误：换号可能有效，但**不冻号**——`X-Tm` 写错也会 403，
            # 冻号会误伤一池健康账号。
            return True
        else:
            return False
        if seconds <= 0 or not uid:
            return False
        try:
            store.set_cooling(uid, time.time() + seconds,
                              f'chat {exc.status}')
        except (FileNotFoundError, ValueError, OSError):
            return False
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
        if not self.upstream_model(model):
            raise PlatformError('请指定 AutoClaw 模型（例如 autoclawcn/glm-4.6）',
                                status=400, code='model_required')

        candidates = self._candidates(realm_hint, affinity, model)
        if not candidates:
            detail = (f'（{store.realm_label(realm_hint)}）'
                      if realm_hint else '')
            raise PlatformError(f'没有可用的 AutoClaw 账号{detail}'
                                '（请在面板里登录或粘贴 token）',
                                status=503, code='no_account')

        last_error: PlatformError | None = None
        for account in candidates[:runtime['max_accounts_try']]:
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
                        f'AutoClaw 账号并发已满（单账号上限 '
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
                    # access token 失效：换一份再试一次（同一账号，不消耗选号次数）。
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
                if self._cool_down(uid, exc, model):
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
        raise last_error or PlatformError('AutoClaw 账号均不可用', status=503,
                                          code='no_account')

    async def _chat_once(self, account: dict, model: str, body: dict,
                         wrapper_model: str, *, proxy: str | None,
                         runtime: dict, affinity: str = '',
                         force_token: bool = False) -> PlatformChat:
        uid = str(account.get('uid') or '')
        realm = self._realm_of(account)
        upstream = self.upstream_model(model)
        # 本区缓存过目录时先做一次本地校验：上游对未知模型的行为不确定（可能
        # 静默回落），等到首帧再发现就晚了。缓存为空时该检查是 no-op。
        self._assert_known_model(upstream, realm)
        access = await self._token_for(account, proxy=proxy, force=force_token)
        device = self._device_of(account)
        # 会话 id 用亲和键的稳定派生值：既是同一段对话的粘性标识，又不会把
        # 原始 prompt / 用户输入直接塞进上游请求头。只有国内 2.x 会发它。
        session_id = (f'ses_{_stable_hex(affinity)}' if affinity else '')
        payload = self.chat_body(model, body)
        timeout = float(runtime['request_timeout'])

        if not payload.get('stream'):
            data = await client.chat(
                realm, access, payload, model=upstream, device_id=device,
                session_id=session_id, proxy=proxy, timeout=timeout)
            usage = data.get('usage') if isinstance(data.get('usage'), dict) else {}
            return PlatformChat(payload=data, usage=usage, model=wrapper_model,
                                account_hint=uid, realm=realm)

        response = await client.open_chat(
            realm, access, payload, model=upstream, device_id=device,
            session_id=session_id, proxy=proxy,
            idle_timeout=float(runtime['idle_timeout']))

        usage: dict = {}
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
            saw_content = False
            try:
                async for chunk in client.iter_chunks(response):
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
                    raise PlatformError('AutoClaw 没有返回内容', status=502,
                                        code='empty_response')
            finally:
                await _close()

        chat.stream = relay()
        # 即使网关还没开始迭代就收尾（客户端提前断开 / 上层报错），也要保证
        # 上游连接被关闭：`relay` 的 finally 与这里都调 `_close`，靠幂等收口。
        chat.closer = _close
        return chat


def _stable_hex(text: str) -> str:
    """亲和键 → 稳定的 24 位 hex（不把原文带进上游请求头）。"""
    import hashlib

    return hashlib.sha256(str(text or '').encode('utf-8')).hexdigest()[:24]


_TASK_ID_KEYS = ('task_id', 'taskId', 'id', 'key', 'code', 'name')
_TASK_DONE_KEYS = ('completed', 'is_completed', 'isCompleted', 'done',
                   'finished', 'status')


def _find_task(tasks: list[dict], task_id: str) -> dict | None:
    """任务列表里找 `daily_signin`；兼容 `task_id` / `id` / `key` 几种写法。"""
    wanted = str(task_id or '').strip().lower()
    for task in tasks:
        if not isinstance(task, dict):
            continue
        for key in _TASK_ID_KEYS:
            value = task.get(key)
            if value not in (None, '') and str(value).strip().lower() == wanted:
                return task
    return None


def _task_done(task: dict) -> bool:
    """任务是否已经完成：布尔、`"2"` / `"done"` 这类状态串都认。"""
    for key in _TASK_DONE_KEYS:
        value = task.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value >= 2
        if isinstance(value, str):
            text = value.strip().lower()
            if text in ('1', 'true', 'done', 'completed', 'finished'):
                return True
            if text in ('0', 'false', 'pending', 'todo'):
                return False
    # 兜底：`progress` / `finished_count` 达到总数也算完成。
    progress = _num(task.get('progress'))
    total = _num(task.get('total'))
    if progress is not None and total is not None and total > 0:
        return progress >= total
    return False


adapter = AutoclawAdapter()
