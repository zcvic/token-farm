"""华为云 CodeArts 码道（CodeArts Agent）平台适配器。

与其它原生平台同构：本文件只负责**策略**，协议在 `client.py`，签名 / DPoP 在
`signer.py` / `dpop.py`。

CodeArts 与其它平台最大的三处不同：

  1. **凭证是一组 STS 临时凭据**（AK/SK + `security_token`），约 1 小时有效。
     每个业务请求都要用 AK/SK 做一次 `SDK-HMAC-SHA256` 签名；
  2. **`refresh_token` 一次性轮换**，且与 `client_id` + DPoP 私钥三者绑定。
     刷新成功必须**先落盘再使用**（同 Cline / MiniMax / 小浣熊）；
  3. **模型目录按账号**：限时福利模型是授予到账号的，对话时必须带
     `maas_type: benefit`，漏带就是 `model is not registered`。

`realm_aware=False`：只有华为云商业版 `cn-north-4`，账号与模型统一 `cn`。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncIterator, Callable

from ... import config, db
from ...services.session_affinity import SessionAffinity, affinity_key
from .. import settings as runtime_settings
from ..base import ChatContext, PlatformAdapter, PlatformChat, PlatformError
from ..limiter import AccountLimiter
from . import catalog, client, constants as C, dpop, store

logger = logging.getLogger('workbuddy.platforms.codearts')

SETTING_MODELS_ATTEMPT = 'platforms.codearts.models_attempt'

# 积分耗尽 / 每日额度用尽：等次日发放或充值，冻 10 分钟足够让批量任务改投。
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


class CodeArtsAdapter(PlatformAdapter):
    name = 'codearts'
    label = '华为云 CodeArts 码道'
    # 每日签到 + 新用户积分（`/v1/ops/*`）。
    supports_checkin = True
    # 网页登录：本机回环回调 + ticket 轮询双通道。
    supports_web_login = True
    # 凭证是 AK/SK + security_token 一组 JSON，不是整段 Cookie。
    supports_cookie_import = False
    # 单区域（华为云商业版 cn-north-4）。
    realm_aware = False

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        # 签到必须串行（`/v1/ops/claim` 的幂等键按毫秒生成，并发会重复领取）。
        self._checkin_locks: dict[str, asyncio.Lock] = {}
        self._last_used: dict[str, float] = {}
        # uid -> (security_token 到期时刻, Credential)。refresh token 永远以
        # 磁盘为准：它是一次性的，内存副本轮换后立刻过期。
        self._creds: dict[str, tuple[float, client.Credential]] = {}
        # uid -> (过期时刻, 探活结果)
        self._probes: dict[str, tuple[float, dict]] = {}
        # uid -> (过期时刻, credits 结果)
        self._credits: dict[str, tuple[float, dict]] = {}
        # (小写模型 id, 是否福利) -> 上游确认可用的精确拼写
        self._model_ids: dict[tuple[str, bool], str] = {}
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

    def _checkin_lock(self, account_id: str) -> asyncio.Lock:
        lock = self._checkin_locks.get(account_id)
        if lock is None:
            lock = self._checkin_locks[account_id] = asyncio.Lock()
        return lock

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
    def _refresh_of(account: dict) -> str:
        return str(account.get('refreshToken') or '').strip()

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

    # ── 令牌 ─────────────────────────────────────────────
    async def _token_for(self, account: dict, *, proxy: str | None,
                         force: bool = False) -> client.Credential:
        """取（或轮换）一份可用的 STS 临时凭据。

        refresh token 是**一次性**的：上游换发后旧的立即作废。因此刷新成功
        必须先把新 token 落盘，再返回给调用方；并发刷新由账号级锁串行。
        """
        uid = str(account.get('uid') or '')
        now = time.time()
        cached = self._creds.get(uid)
        if cached and not force and cached[0] - C.TOKEN_SKEW_SECONDS > now:
            return cached[1]

        async with self._lock(uid):
            cached = self._creds.get(uid)
            if cached and not force \
                    and cached[0] - C.TOKEN_SKEW_SECONDS > time.time():
                return cached[1]

            fresh = store.read_account(uid) or account
            current = client.Credential.of(fresh)
            expires = store.epoch_from_iso(fresh.get('expiredAt'))
            if not force and current and expires - C.TOKEN_SKEW_SECONDS > now:
                self._creds[uid] = (expires, current)
                return current

            refresh_token = self._refresh_of(fresh)
            if not refresh_token:
                if current and (not expires or expires > now):
                    self._creds[uid] = (expires or now + C.TOKEN_SKEW_SECONDS,
                                        current)
                    return current
                self._mark_expired(uid, 'STS 临时凭证已过期且没有 refresh_token')
                raise PlatformError(
                    'CodeArts 凭证已过期，请重新登录（该账号没有 refresh_token）',
                    status=401, code='login_expired')

            jwk = fresh.get('dpopJwk')
            try:
                dpop.normalize_jwk(jwk)
            except ValueError as exc:
                self._mark_expired(uid, str(exc))
                raise PlatformError(
                    f'CodeArts DPoP 私钥不可用，请重新登录：{exc}',
                    status=401, code='login_expired') from exc

            try:
                token = await client.refresh(
                    refresh_token,
                    str(fresh.get('codeVerifier') or ''),
                    jwk,
                    client_id=str(fresh.get('clientId') or C.CLIENT_ID),
                    proxy=proxy,
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
                raise

            patch: dict[str, Any] = {
                'accessKeyId': str(token.get('access_key_id') or ''),
                'secretAccessKey': str(token.get('secret_access_key') or ''),
                'securityToken': str(token.get('security_token') or ''),
                'expiredAt': store.iso_from_epoch(token.get('expires_at'))
                or str(token.get('expiration') or ''),
                'refreshToken': str(token.get('refresh_token') or '')
                or refresh_token,
                'refreshedAt': store.iso_from_epoch(time.time()),
                'lastRefreshError': '',
                'loginState': '正常',
            }
            if not patch['securityToken'] or not patch['accessKeyId']:
                # 刷新接口回了 200 却没有凭据：旧 refresh token 可能已经轮换掉了，
                # 这时只剩「重新登录」一条路，不能把空凭据写进账号。
                self._mark_expired(uid, '刷新响应缺少 STS 凭据')
                raise PlatformError(
                    'CodeArts 刷新响应缺少 STS 凭据，请重新登录',
                    status=401, code='login_expired')
            if token.get('user_id') and not str(fresh.get('userId') or ''):
                patch['userId'] = token['user_id']
            if token.get('user_name') and not str(fresh.get('userName') or ''):
                patch['userName'] = token['user_name']
            if token.get('domain_id') and not str(fresh.get('domainId') or ''):
                patch['domainId'] = token['domain_id']
            try:
                store.patch_account(uid, patch)
            except (FileNotFoundError, ValueError, OSError) as exc:
                # 已经作废旧 refresh token，落盘失败就等于账号掉线：必须让
                # 调用方知道，不能假装成功继续用一份下次就失效的凭据。
                raise PlatformError(
                    f'CodeArts 新令牌落盘失败：{exc}', status=500,
                    code='store_error') from exc

            cred = client.Credential(patch['accessKeyId'],
                                     patch['secretAccessKey'],
                                     patch['securityToken'])
            new_expires = token.get('expires_at') or (
                time.time() + C.ACCESS_TTL_SECONDS)
            self._creds[uid] = (float(new_expires), cred)
            return cred

    def _drop_token(self, uid: str) -> None:
        self._creds.pop(str(uid or ''), None)

    # ── 账号 ──────────────────────────────────────────────
    def list_accounts(self) -> list[dict]:
        return [store.public_account(a) for a in store.list_accounts()]

    def export_accounts(self) -> list[dict]:
        """导出含明文凭据的账号，仅供管理员导出接口调用。"""
        return store.list_accounts()

    def import_account(self, payload: dict) -> dict:
        """导入账号：STS 临时凭据 +（推荐）refresh token + DPoP 私钥。

        接受 `{account: {...}}` 与扁平对象两种形状，也兼容参考实现的
        `auths/codearts-*.json`（`credentials` 嵌套 / `cloud_dragon_token` /
        `dpop_private_key`）。导入阶段**不联网**：第一次刷新 / 探活发生在
        `refresh_account` 或第一次对话时。
        """
        if not isinstance(payload, dict):
            raise PlatformError('账号必须是 JSON 对象', status=400,
                                code='invalid_account')
        try:
            normalized = store.normalize_credentials(payload)
        except ValueError as exc:
            raise PlatformError(str(exc), status=400,
                                code='invalid_account') from exc

        refresh = normalized['refreshToken']
        access = normalized['securityToken']
        if not refresh and not (access and normalized['accessKeyId']):
            raise PlatformError(
                '需要提供 CodeArts 的 security_token + AK/SK，或 refresh_token',
                status=400, code='invalid_account')

        try:
            uid = normalized['uid']
            if uid:
                uid = store.safe_id(uid)
            else:
                uid = store.derive_uid(
                    normalized['uid'], normalized['userName'])
        except ValueError as exc:
            raise PlatformError(str(exc), status=400,
                                code='invalid_account') from exc

        old = store.read_account(uid) or {}
        dpop_jwk = normalized.get('dpopJwk') or old.get('dpopJwk')
        if dpop_jwk is not None:
            try:
                dpop_jwk = dpop.normalize_jwk(dpop_jwk)
            except ValueError as exc:
                raise PlatformError(f'DPoP 私钥无效：{exc}', status=400,
                                    code='invalid_account') from exc

        expires = store.epoch_from_iso(normalized['expiredAt'])
        if not expires and access:
            expires = time.time() + C.ACCESS_TTL_SECONDS
        refresh_expires = store.epoch_from_iso(normalized['refreshExpiredAt'])
        if not refresh_expires and refresh:
            refresh_expires = time.time() + C.REFRESH_TTL_SECONDS

        account = {
            **old,
            'uid': uid,
            'userId': normalized['uid'] or str(old.get('userId') or ''),
            'userName': normalized['userName'] or str(old.get('userName') or ''),
            'domainId': normalized['domainId'] or str(old.get('domainId') or ''),
            'accessKeyId': normalized['accessKeyId'],
            'secretAccessKey': normalized['secretAccessKey'],
            'securityToken': access,
            'expiredAt': store.iso_from_epoch(expires),
            'refreshToken': refresh or str(old.get('refreshToken') or ''),
            'refreshExpiredAt': store.iso_from_epoch(refresh_expires),
            'codeVerifier': normalized['codeVerifier']
            or str(old.get('codeVerifier') or ''),
            'clientId': normalized['clientId'] or C.CLIENT_ID,
            'dpopJwk': dpop_jwk,
            'proxy': normalized['proxy'] or str(old.get('proxy') or ''),
            'source': normalized['source'] or str(old.get('source') or 'manual'),
            'lastRefreshError': '',
            'loginState': '正常',
        }
        self._drop_token(uid)
        self._probes.pop(uid, None)
        self._credits.pop(uid, None)
        return store.public_account(store.save_account(account))

    def delete_account(self, account_id: str) -> bool:
        uid = str(account_id or '')
        self._drop_token(uid)
        self._probes.pop(uid, None)
        self._credits.pop(uid, None)
        catalog.cache_drop(uid)
        return store.delete_account(account_id)

    # ── 网页登录 ──────────────────────────────────────────
    def login_start(self, *, realm: str | None = None) -> dict:
        """发起一次网页登录：本机回环回调 + ticket 轮询双通道。

        `realm` 只为路由统一签名：CodeArts 只有 `cn-north-4` 一个区域。
        """
        from . import login as loginmod  # noqa: PLC0415 —— 避免循环导入
        return loginmod.start(realm)

    def login_poll(self, login_id: str) -> dict:
        from . import login as loginmod  # noqa: PLC0415
        return loginmod.poll(login_id)

    def login_cancel(self, login_id: str) -> bool:
        from . import login as loginmod  # noqa: PLC0415
        return loginmod.cancel(login_id)

    def login_submit_callback(self, login_id: str, callback: str) -> dict:
        """远程部署兜底：提交浏览器地址栏里的完整回调 URL。"""
        from . import login as loginmod  # noqa: PLC0415
        return loginmod.submit_callback(login_id, callback)

    async def refresh_account(self, account_id: str, *,
                              force: bool = False) -> dict:
        """轮换一次令牌（顺便验证凭证）。"""
        account = self._require(account_id)
        uid = str(account.get('uid') or '')
        proxy = self._proxy_for(account)
        if self._refresh_of(account):
            await self._token_for(account, proxy=proxy, force=True)
            return store.public_account(store.patch_account(uid, {
                'refreshedAt': store.iso_from_epoch(time.time()),
                'lastRefreshError': '',
                'loginState': '正常',
            }))

        # 只有 STS 临时凭据：用身份接口探活，能读就说明还没过期。
        cred = await self._token_for(account, proxy=proxy)
        out = await client.probe(cred, proxy=proxy,
                                 timeout=self._timeout('catalog_timeout',
                                                       C.CATALOG_TIMEOUT))
        state = str(out.get('state') or 'unknown')
        patch: dict[str, Any] = {
            'refreshedAt': store.iso_from_epoch(time.time())}
        identity = out.get('identity') if isinstance(out.get('identity'),
                                                      dict) else {}
        if identity:
            patch['userId'] = str(identity.get('user_id')
                                  or account.get('userId') or '')
            patch['userName'] = str(identity.get('user_name')
                                    or account.get('userName') or '')
            patch['domainId'] = str(identity.get('domain_id')
                                    or account.get('domainId') or '')
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

    # ── 签到 / 积分 ───────────────────────────────────────
    async def checkin(self, account_id: str) -> dict:
        """领取每日签到（活动 1）与新用户（活动 4）积分。

        同一账号的领取必须串行：`/v1/ops/claim` 的幂等键按毫秒生成，两个并发
        请求会各自领一次。活动故障只记签到结果，不计入推理错误、不冷却、不停用。
        """
        account = self._require(account_id)
        uid = str(account.get('uid') or '')
        proxy = self._proxy_for(account)
        timeout = self._timeout('credits_timeout', C.CREDITS_TIMEOUT)
        async with self._checkin_lock(uid):
            cred = await self._token_for(account, proxy=proxy)
            result = await client.checkin(cred, proxy=proxy, timeout=timeout)
            reward = float(result.get('credits') or 0.0)
            ok = bool(result.get('ok'))
            previous = store.read_account(uid) or {}
            total = float(_num(previous.get('creditsAwarded')) or 0.0)
            if reward > 0:
                total += reward
            try:
                store.patch_account(uid, {
                    'checkin': {
                        'last_at': int(time.time()),
                        'last_result': 'ok' if ok else 'error',
                        'reward': reward,
                        'campaigns': result.get('campaigns') or [],
                    },
                    'creditsAwarded': total,
                    'creditsAt': int(time.time()),
                    'loginState': '正常',
                })
            except (FileNotFoundError, ValueError, OSError):
                pass
            self._credits.pop(uid, None)

        return {
            'ok': ok,
            'checked_in': ok,
            'already': ok and reward <= 0,
            'credits': total,
            'earned_credit': reward,
            'message': str(result.get('message') or ''),
            'uid': uid,
            'realm': store.REALM,
            'reward': result.get('campaigns') or [],
            'continuous_days': None,
        }

    async def usage(self, account_id: str) -> dict:
        """额度 / 用量快照。

        上游**没有**积分余额接口：这里给出的是「签到累计领取量」+ 只读的活动
        列表，绝不编一个看起来像余额的数字。
        """
        account = self._require(account_id)
        uid = str(account.get('uid') or '')
        proxy = self._proxy_for(account)
        out: dict[str, Any] = {
            'uid': uid,
            'realm': store.REALM,
            'session_usage': None,
            'entitlement': None,
            'errors': [],
        }
        try:
            cred = await self._token_for(account, proxy=proxy)
            campaigns = await client.fetch_campaigns(
                cred, proxy=proxy,
                timeout=self._timeout('credits_timeout', C.CREDITS_TIMEOUT))
        except PlatformError as exc:
            out['errors'].append({'endpoint': 'v1/ops/delivery',
                                  'code': exc.code, 'message': exc.message})
            return out
        awarded = float(_num(account.get('creditsAwarded')) or 0.0)
        out['session_usage'] = {
            'total': awarded,
            'used': None,
            'remaining': None,
            'at': int(time.time()),
        }
        out['entitlement'] = {'quota_known': False, 'remaining': None}
        out['campaigns'] = campaigns
        return out

    async def credits(self, account_id: str) -> dict:
        """积分快照（默认 60 秒缓存）。

        上游没有余额接口，所以给的是**累计签到领取量**（`source: checkin`），
        并明确 `remain` 未知。一次都没领过时如实报错，而不是回 0——0 会被
        读成「积分用完了」。
        """
        account = self._require(account_id)
        uid = str(account.get('uid') or '')
        cached = self._credits.get(uid)
        if cached and cached[0] > time.time():
            return cached[1]

        awarded = _num(account.get('creditsAwarded'))
        if awarded is None:
            checkin = account.get('checkin')
            if isinstance(checkin, dict):
                awarded = _num(checkin.get('reward'))
        if awarded is None:
            raise PlatformError(
                'CodeArts 没有积分余额接口；先跑一次签到，才能给出累计领取量',
                status=400, code='credits_unsupported')

        out = {
            'uid': uid,
            'realm': store.REALM,
            'credits': float(awarded),
            'remain': None,
            'limit': None,
            'used': None,
            'unit': 'credits',
            'expires': [],
            'next_expire_at': None,
            'source': 'checkin',
            'note': '累计签到领取量，不是账户余额（上游未提供余额接口）',
        }
        self._credits[uid] = (
            time.time() + runtime_settings.credits_ttl(self.name), out)
        return out

    # ── 模型目录 ──────────────────────────────────────────
    def _cached_models(self) -> list[dict] | None:
        models = catalog.all_models()
        return models or None

    def _implicit_live_allowed(self) -> bool:
        try:
            last = float(db.get_setting(SETTING_MODELS_ATTEMPT, 0) or 0)
        except Exception:  # noqa: BLE001
            return False
        return time.time() - last > runtime_settings.catalog_retry_ttl(
            self.name)

    @staticmethod
    def _note_implicit_attempt() -> None:
        try:
            db.set_setting(SETTING_MODELS_ATTEMPT, int(time.time()))
        except Exception:  # noqa: BLE001
            pass

    async def _refresh_catalog(self, account: dict, *, proxy: str | None,
                               timeout: float) -> bool:
        """刷新单个账号的目录；失败保留旧缓存，返回是否写入。"""
        uid = str(account.get('uid') or '')
        if not uid:
            return False
        try:
            cred = await self._token_for(account, proxy=proxy)
        except PlatformError:
            return False
        try:
            models = await catalog.discover(cred, uid, proxy=proxy,
                                            timeout=timeout)
        except PlatformError as exc:
            logger.info('codearts 目录刷新失败 uid=%s: %s', uid, exc.message)
            return False
        catalog.cache_put(uid, models)
        return True

    async def list_models(self, *, refresh: bool = False) -> list[dict]:
        ttl = runtime_settings.catalog_ttl(self.name)
        accounts = [a for a in store.list_accounts()
                    if store.has_credentials(a) and not store.is_disabled(a)]
        due = [a for a in accounts
               if refresh or catalog.cache_get(str(a.get('uid') or ''),
                                               ttl=ttl) is None]
        if due and (refresh or self._implicit_live_allowed()):
            timeout = (45.0 if refresh
                       else runtime_settings.catalog_timeout(self.name))
            for account in due:
                await self._refresh_catalog(
                    account, proxy=self._proxy_for(account), timeout=timeout)
            self._note_implicit_attempt()

        models = self._cached_models() or catalog.seed_models()
        self._model_meta = {str(m.get('id') or '').strip().lower(): m
                            for m in models if m.get('id')}
        return [self._public_model(m) for m in models if m.get('id')]

    def _public_model(self, raw: dict) -> dict:
        model_id = str(raw.get('id') or '')
        benefit = bool(raw.get('benefit'))
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
            'model_name': model_id,
            'supports_reasoning': True,
            'reasoning_efforts': [],
            'modalities': {'input': ['text']},
            'tool_call': True,
            'attachment': False,
            'free': benefit,
            'badge': '限免' if benefit else '',
            'description': str(raw.get('description') or ''),
        }

    def cached_model_ids(self) -> set[str] | None:
        """已缓存的对外模型 id（`codearts/...`），只读不联网。"""
        models = self._cached_models()
        if not models:
            return None
        return {self.public_model_id(str(m.get('id') or ''), store.REALM)
                for m in models if m.get('id')}

    def upstream_model(self, model: str) -> str:
        """成员名 → 上游注册名（大小写归一 + 静态别名）。

        上游按注册名**精确匹配（区分大小写）**，而客户端习惯写小写；这里先用
        目录 + 种子 + 静态别名归一，真的拼错了还有 `_chat_once` 的一次重试。
        """
        text = str(model or '').strip()
        if not text or text.lower() == 'default':
            return C.DEFAULT_MODEL
        return client.canonical_model(text, catalog.known_spellings())

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
                    and store.has_credentials(a)
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

        上游对未注册模型回 `InferHub.002002009.404 model is not registered`，
        但那要等一次完整往返；这里先按目录（大小写不敏感）拒绝，错误信息也
        指得到正确的模型名。

        只在**真的发现过目录**时启用：冷启动只有种子，拿种子去判「未知模型」
        会把账号其实有的模型误拒成 400。
        """
        text = str(model or '').strip()
        if not text or text.lower() == 'default':
            return
        if not catalog.has_live_catalog():
            return
        known = {str(item.get('id') or '').strip().lower()
                 for item in (self._cached_models() or [])
                 if item.get('id')}
        if known and text.lower() not in known:
            raise PlatformError(f'CodeArts 没有模型 {text}', status=400,
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
                '没有可用的 CodeArts 账号（请在面板里登录或粘贴凭据）',
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
                        f'CodeArts 账号并发已满（单账号上限 '
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
                    # STS 凭据失效：换一份再试一次（同一账号，不消耗选号次数）。
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
        raise last_error or PlatformError('CodeArts 账号均不可用', status=503,
                                          code='no_account')

    async def _open(self, cred: client.Credential, payload: dict, *,
                    chat_id: str, benefit: bool, proxy: str | None,
                    idle_timeout: float) -> client.ChatStream:
        return await client.open_chat(
            cred, payload, chat_id=chat_id,
            session_id=client.session_id_for(chat_id), benefit=benefit,
            proxy=proxy, idle_timeout=idle_timeout)

    async def _chat_once(self, account: dict, model: str, body: dict,
                         wrapper_model: str, *, proxy: str | None,
                         runtime: dict, affinity: str = '',
                         force_token: bool = False) -> PlatformChat:
        uid = str(account.get('uid') or '')
        cred = await self._token_for(account, proxy=proxy, force=force_token)
        upstream = self.upstream_model(model)
        benefit = catalog.is_benefit(uid, upstream)
        chat_id = client.new_chat_id()
        # 上游**永远**走流式（`/api/v2/chat/completions` 的 SSE 是唯一稳定
        # 出口）；客户端要非流式时在本地聚合，不要向上去要一份 JSON。
        want_stream = bool(body.get('stream'))
        payload = client.build_chat_body(
            list(body.get('messages') or []), upstream, chat_id,
            options=body)

        # 上游会把「拼写不匹配」报成权限错误（`tm.000010051`）。换一种拼写
        # 重试一次，成功就记住——同一账号后续请求直接命中。
        key = (upstream.lower(), benefit)
        first = self._model_ids.get(key) or upstream
        alternate = upstream if first != upstream else _other_spelling(upstream)

        try:
            stream = await self._open(
                cred, {**payload, 'model': first}, chat_id=chat_id,
                benefit=benefit, proxy=proxy,
                idle_timeout=float(runtime['idle_timeout']))
            self._model_ids[key] = first
        except PlatformError as exc:
            if alternate == first or not C.is_model_error(exc.message):
                raise
            stream = await self._open(
                cred, {**payload, 'model': alternate}, chat_id=chat_id,
                benefit=benefit, proxy=proxy,
                idle_timeout=float(runtime['idle_timeout']))
            self._model_ids[key] = alternate

        if not want_stream:
            events: list[tuple[str, object]] = []
            try:
                async for event in client.iter_events(stream):
                    events.append(event)
            finally:
                await stream.aclose()
            data = client.aggregate(events, wrapper_model)
            usage = data.get('usage') if isinstance(data.get('usage'),
                                                    dict) else {}
            return PlatformChat(payload=data, usage=usage,
                                model=wrapper_model, account_hint=uid,
                                realm=store.REALM)

        translator = client.StreamTranslator(wrapper_model)
        usage: dict = translator.usage
        chat = PlatformChat(usage=usage, model=wrapper_model,
                            account_hint=uid, realm=store.REALM)

        async def relay() -> AsyncIterator[dict]:
            finished = False
            try:
                async for event, payload_obj in client.iter_events(stream):
                    for chunk in translator.feed(event, payload_obj):
                        yield chunk
                finished = True
            finally:
                if finished:
                    for chunk in translator.finish_chunks():
                        yield chunk
                await stream.aclose()

        chat.stream = relay()
        # 即使网关还没开始迭代就收尾（客户端提前断开 / 上层报错），也要保证
        # 上游连接被关闭：`relay` 的 finally 与这里都调 `aclose`，httpx 幂等。
        async def _close() -> None:
            await stream.aclose()

        chat.closer = _close
        return chat


def _other_spelling(model: str) -> str:
    """给「拼写不匹配」重试用：全大写 / 全小写里挑一个不同的。"""
    text = str(model or '').strip()
    if not text:
        return ''
    swapped = text.lower() if text != text.lower() else text.upper()
    return swapped if swapped != text else ''


adapter = CodeArtsAdapter()
