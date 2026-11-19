"""MiniMax Code（mcode）平台适配器。

与 Loomy / DuMate 同构：本文件只负责**策略**，协议在 `client.py`。

  · 选号（权重 SWRR + LRU + 会话粘性）与单账号并发闸门；
  · 一次性 refresh token 的轮换与**先落盘再使用**；
  · 签到、积分明细、套餐窗口；
  · 模型目录缓存（在线 → 快照）；
  · OpenAI ↔ Anthropic 的请求 / 响应 / 流式翻译（`translate.py`）。

`realm_aware=True`：国内站（`minimaxcn/`）与国际站（`minimaxcom/`）的 token
不通用，账号按站点分池，模型按站点加前缀；旧式 `minimax/` 仍可调用。
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, AsyncIterator, Callable

from ... import config, db
from ...services.session_affinity import SessionAffinity, affinity_key
from .. import settings as runtime_settings
from ..base import ChatContext, PlatformAdapter, PlatformChat, PlatformError
from ..limiter import AccountLimiter
from . import catalog, client, constants as C, store, translate

SETTING_MODELS = 'platforms.minimax.models'
SETTING_MODELS_ATTEMPT = 'platforms.minimax.models_attempt'

QUOTA_COOLDOWN_SECONDS = 3600


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


def _amount(value: object, *, clamp: bool = False) -> float | None:
    """积分明细里的金额是带千分位的字符串（`"1,200.5"`）。"""
    if value is None or value == '':
        return None
    text = str(value).replace(',', '').strip()
    if clamp and text.startswith('-'):
        return 0.0
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _round(value: float, digits: int = 2) -> float | int:
    out = round(value, digits)
    return int(out) if float(out).is_integer() else out


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


class MinimaxAdapter(PlatformAdapter):
    name = 'minimax'
    label = 'MiniMax Code'
    supports_checkin = True
    supports_web_login = True
    realm_aware = True

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_used: dict[str, float] = {}
        self._tokens: dict[str, tuple[float, str]] = {}
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
        try:
            return config.resolve_account_proxy(account)
        except ValueError as exc:
            raise PlatformError(str(exc), status=503,
                                code='proxy_invalid') from exc

    def _session_timeout(self) -> float:
        try:
            return float(max(8, min(30, runtime_settings.request_timeout(self.name))))
        except Exception:  # noqa: BLE001
            return C.TOKEN_TIMEOUT

    @staticmethod
    def _mark_expired(uid: str, message: str = '') -> None:
        try:
            store.patch_account(uid, {
                'loginState': '登录已失效',
                'lastRefreshError': str(message or '')[:200],
            })
        except (FileNotFoundError, ValueError, OSError):
            pass

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
            if cached and not force and cached[0] - C.TOKEN_SKEW_SECONDS > time.time():
                return cached[1]

            fresh = store.read_account(uid) or account
            realm = store.normalize_realm(fresh.get('realm'))
            access = str(fresh.get('token') or '').strip()
            expires = store.epoch_from_iso(fresh.get('expiredAt'))
            refresh = str(fresh.get('refreshToken') or '').strip()

            if access and not force and expires \
                    and expires - C.TOKEN_SKEW_SECONDS > time.time():
                self._tokens[uid] = (expires, access)
                return access

            if refresh:
                try:
                    data = await client.refresh_token(
                        realm, refresh, proxy=proxy,
                        timeout=self._session_timeout())
                except PlatformError as exc:
                    if exc.status == 401:
                        self._mark_expired(uid, exc.message)
                    else:
                        try:
                            store.patch_account(uid, {'lastRefreshError': exc.message[:200]})
                        except (FileNotFoundError, ValueError, OSError):
                            pass
                    raise
                new_access = str(data.get('access_token') or '')
                new_refresh = str(data.get('refresh_token') or '') or refresh
                new_expires = time.time() + int(data.get('expires_in')
                                                or C.TOKEN_FALLBACK_SECONDS)
                try:
                    store.patch_account(uid, {
                        'token': new_access,
                        'refreshToken': new_refresh,
                        'expiredAt': store.iso_from_epoch(new_expires),
                        'refreshedAt': store.iso_from_epoch(time.time()),
                        'lastRefreshError': '',
                        'loginState': '正常',
                    })
                except (FileNotFoundError, ValueError, OSError) as exc:
                    raise PlatformError(f'保存 MiniMax 新 token 失败：{exc}',
                                        status=500, code='store_error') from exc
                self._tokens[uid] = (new_expires, new_access)
                return new_access

            if access and (not force or not refresh):
                # 没有 refresh token，也没有可靠过期时间：先用一次，401 再换号。
                self._tokens[uid] = (expires or time.time() + 60, access)
                return access

            self._mark_expired(uid, 'refresh token 缺失')
            raise PlatformError('MiniMax Code 账号缺少 refresh token，请重新登录',
                                status=401, code='login_expired')

    def _drop_token(self, uid: str) -> None:
        self._tokens.pop(str(uid or ''), None)

    async def _identity(self, account: dict, access: str, *,
                        proxy: str | None) -> dict:
        realm = store.normalize_realm(account.get('realm'))
        return await client.fetch_identity(
            realm, access, user_id=str(account.get('realUserID') or ''),
            proxy=proxy, timeout=self._session_timeout())

    # ── 账号 ──────────────────────────────────────────────
    def list_accounts(self) -> list[dict]:
        return [store.public_account(a) for a in store.list_accounts()]

    def import_account(self, payload: dict) -> dict:
        """导入账号：粘贴 refresh token / access token。

        支持 `{account: {...}}` 与扁平对象；也接受设备码登录的返回值
        （`access_token` / `refresh_token`）。导入阶段**不联网**，首次刷新
        在 `refresh_account` 或第一次对话时发生。
        """
        if not isinstance(payload, dict):
            raise PlatformError('账号必须是 JSON 对象', status=400,
                                code='invalid_account')
        raw = payload.get('account') if isinstance(payload.get('account'), dict) \
            else payload
        realm = store.normalize_realm(_pick(raw, 'realm', 'site', 'region'))

        refresh = _pick(raw, 'refresh_token', 'refreshToken', 'refresh')
        access = _pick(raw, 'access_token', 'accessToken', 'token', 'session')
        if not refresh and not access:
            raise PlatformError(
                '需要提供 refresh_token（或 access_token）', status=400,
                code='invalid_account')
        # uid 尽量落在**稳定身份**上：设备码登录会带 realUserID，同一用户重新
        # 登录应覆盖同一条账号，而不是因为换了 refresh token 就多出一条。
        # 手工只粘 token 时退回凭证派生（同一份凭证重复导入仍然幂等）。
        identity = _pick(raw, 'realUserID', 'real_user_id', 'userId', 'user_id')
        seed = f'user:{identity}' if identity else (refresh or access)
        try:
            uid = store.safe_id(_pick(raw, 'uid') or store.derive_uid(realm, seed))
        except ValueError as exc:
            raise PlatformError(str(exc), status=400,
                                code='invalid_account') from exc

        old = store.read_account(uid) or {}
        expires = store.epoch_from_iso(_pick(raw, 'expiresAt', 'expiredAt',
                                             'expires_at'))
        if not expires:
            expires_in = _num(raw.get('expires_in'))
            if expires_in and expires_in > 0:
                expires = time.time() + expires_in
        account = {
            **old,
            'uid': uid,
            'realm': realm,
            'token': access or str(old.get('token') or ''),
            'refreshToken': refresh or str(old.get('refreshToken') or ''),
            'expiredAt': store.iso_from_epoch(expires)
            if expires else str(old.get('expiredAt') or ''),
            'refreshExpiredAt': _pick(raw, 'refreshExpiresAt',
                                      'refresh_expires_at')
            or str(old.get('refreshExpiredAt') or ''),
            'realUserID': _pick(raw, 'realUserID', 'real_user_id', 'userId',
                                'user_id') or str(old.get('realUserID') or ''),
            'name': _pick(raw, 'name') or str(old.get('name') or ''),
            'nickname': _pick(raw, 'nickname', 'displayName')
            or str(old.get('nickname') or ''),
            'email': _pick(raw, 'email') or str(old.get('email') or ''),
            'proxy': _pick(raw, 'proxy', 'egress') or str(old.get('proxy') or ''),
            'proxy_session': _pick(raw, 'proxy_session', 'proxySession')
            or str(old.get('proxy_session') or ''),
            'source': _pick(raw, 'source') or str(old.get('source') or 'manual'),
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
        """刷新 token 并读取用户信息（顺带探活）。"""
        account = self._require(account_id)
        uid = str(account.get('uid') or '')
        realm = store.normalize_realm(account.get('realm'))
        proxy = self._proxy_for(account)
        try:
            access = await self._token_for(account, proxy=proxy, force=True)
            info = await self._identity(account, access, proxy=proxy)
        except PlatformError as exc:
            if exc.status == 401:
                self._mark_expired(uid, exc.message)
            try:
                store.patch_account(uid, {'lastRefreshError': exc.message[:200]})
            except (FileNotFoundError, ValueError, OSError):
                pass
            raise

        patch: dict[str, Any] = {
            'refreshedAt': store.iso_from_epoch(time.time()),
            'lastRefreshError': '',
            'loginState': '正常',
            'realm': realm,
        }
        if info.get('realUserID'):
            patch['realUserID'] = info['realUserID']
        if info.get('email'):
            patch['email'] = info['email']
        if info.get('name'):
            patch['name'] = info['name']
        self._credits.pop(uid, None)
        return store.public_account(store.patch_account(uid, patch))

    # ── 网页登录（设备码）────────────────────────────────
    def login_start(self, *, realm: str | None = None) -> dict:
        from . import login as loginmod  # noqa: PLC0415 —— login 反向依赖 adapter
        return loginmod.start(realm)

    def login_poll(self, login_id: str) -> dict:
        from . import login as loginmod  # noqa: PLC0415
        return loginmod.poll(login_id)

    def login_cancel(self, login_id: str) -> bool:
        from . import login as loginmod  # noqa: PLC0415
        return loginmod.cancel(login_id)

    # ── 签到 ──────────────────────────────────────────────
    async def checkin(self, account_id: str) -> dict:
        """每日签到：读 7 天面板，今天可领就领；重复调用幂等。"""
        account = self._require(account_id)
        uid = str(account.get('uid') or '')
        proxy = self._proxy_for(account)
        timeout = runtime_settings.credits_timeout(self.name)
        token = await self._token_for(account, proxy=proxy)
        try:
            user_id = await self._user_id(account, token, proxy=proxy)
        except PlatformError as exc:
            if exc.status != 401:
                raise
            token = await self._token_for(account, proxy=proxy, force=True)
            user_id = await self._user_id(account, token, proxy=proxy)

        realm = store.normalize_realm(account.get('realm'))
        try:
            panel = await client.checkin_status(realm, token, user_id,
                                                proxy=proxy, timeout=timeout)
        except PlatformError as exc:
            if exc.status != 401:
                raise
            token = await self._token_for(account, proxy=proxy, force=True)
            panel = await client.checkin_status(realm, token, user_id,
                                                proxy=proxy, timeout=timeout)

        days = sorted((d for d in panel.get('days') or []
                       if isinstance(d, dict)),
                      key=lambda d: int(_num(d.get('day_no')) or 0))
        today = next((d for d in days if d.get('is_today')), None)
        if today is None:
            return {
                'ok': True, 'checked_in': False, 'already': False,
                'credits': 0, 'earned_credit': 0,
                'message': '今天还没有可签到的任务', 'uid': uid, 'realm': realm,
            }

        status = int(_num(today.get('status')) or 0)
        already = status == 3
        claim_result = 0
        points = 0
        if status == 2:
            claim = await client.claim_checkin(realm, token, user_id,
                                               proxy=proxy, timeout=timeout)
            claim_result = int(claim.get('claim_result') or 0)
            points = int(claim.get('points') or 0)
            already = claim_result == 2
            message = '今日已签到' if already else '签到成功'
        elif status == 3:
            message = '今日已签到'
            points = int(_num(today.get('points')) or 0)
        elif status == 4:
            message = '今天不可签到'
            already = False
        else:
            message = '今天还没开始'
            already = False

        streak = 0
        for day in days:
            if int(_num(day.get('status')) or 0) == 3:
                streak += 1
            elif day is today:
                break

        balance = None
        try:
            ledger = await self._ledger(account, token, user_id, proxy=proxy,
                                        timeout=timeout)
            balance = ledger['left']
        except PlatformError:
            balance = None

        earned = points if status == 2 and not already else 0
        checkin = {
            'last_at': int(time.time()),
            'last_result': message,
            'streak': streak,
            'credits': balance if balance is not None else earned,
        }
        patch: dict[str, Any] = {'checkin': checkin}
        if balance is not None:
            patch['plan'] = {**(account.get('plan')
                                if isinstance(account.get('plan'), dict) else {}),
                             'credits': balance, 'at': int(time.time())}
        try:
            store.patch_account(uid, patch)
        except (FileNotFoundError, ValueError, OSError):
            pass
        self._credits.pop(uid, None)

        return {
            'ok': True,
            'checked_in': status == 2 or status == 3,
            'already': already,
            'credits': balance if balance is not None else earned,
            'earned_credit': earned,
            'message': message,
            'uid': uid,
            'realm': realm,
            'streak': streak,
            'claim_result': claim_result,
        }

    async def _user_id(self, account: dict, access: str, *,
                       proxy: str | None) -> str:
        current = str(account.get('realUserID') or '').strip()
        if current:
            return current
        info = await self._identity(account, access, proxy=proxy)
        user_id = str(info.get('realUserID') or '').strip()
        if not user_id:
            raise PlatformError('账号信息缺少 realUserID', status=502,
                                code='bad_response')
        patch: dict[str, Any] = {'realUserID': user_id}
        if info.get('email'):
            patch['email'] = info['email']
        if info.get('name'):
            patch['name'] = info['name']
        try:
            store.patch_account(str(account.get('uid') or ''), patch)
        except (FileNotFoundError, ValueError, OSError):
            pass
        return user_id

    async def _ledger(self, account: dict, access: str, user_id: str, *,
                      proxy: str | None, timeout: float) -> dict:
        realm = store.normalize_realm(account.get('realm'))
        batches = await client.credit_details(realm, access, user_id,
                                             proxy=proxy, timeout=timeout)
        now_ms = time.time() * 1000
        total = 0.0
        left = 0.0
        expires: list[dict] = []
        for batch in batches:
            kind = int(_num(batch.get('credit_type')) or 0)
            if kind not in (1, 2):
                continue
            granted = _amount(batch.get('granted_amount'))
            remaining = _amount(batch.get('remaining_amount'), clamp=True)
            expire = _num(batch.get('expire_at_ms'))
            if granted is None or remaining is None or expire is None:
                continue
            expire_ms = expire if expire >= 1e12 else expire * 1000
            if expire_ms <= now_ms:
                continue
            total += granted
            rest = min(remaining, granted)
            left += rest
            if rest > 0:
                expires.append({'credits': _round(rest),
                                'expire_at': int(expire_ms)})
        expires.sort(key=lambda x: x['expire_at'])
        return {
            'total': _round(total),
            'left': _round(left),
            'used': _round(max(0.0, total - left)),
            'expires': expires,
            'next_expire_at': expires[0]['expire_at'] if expires else None,
        }

    # ── 额度 / 积分 ───────────────────────────────────────
    async def usage(self, account_id: str) -> dict:
        account = self._require(account_id)
        uid = str(account.get('uid') or '')
        realm = store.normalize_realm(account.get('realm'))
        proxy = self._proxy_for(account)
        timeout = runtime_settings.credits_timeout(self.name)
        out: dict[str, Any] = {
            'uid': uid,
            'realm': realm,
            'session_usage': None,
            'entitlement': None,
            'errors': [],
        }
        try:
            token = await self._token_for(account, proxy=proxy)
            user_id = await self._user_id(account, token, proxy=proxy)
        except PlatformError as exc:
            out['errors'].append({'endpoint': 'identity',
                                  'code': exc.code, 'message': exc.message})
            return out

        try:
            ledger = await self._ledger(account, token, user_id, proxy=proxy,
                                        timeout=timeout)
            out['session_usage'] = {
                'total': ledger['total'],
                'used': ledger['used'],
                'left': ledger['left'],
                'packages': ledger['expires'],
                'unit': 'credits',
            }
        except PlatformError as exc:
            out['errors'].append({'endpoint': 'credit_details',
                                  'code': exc.code, 'message': exc.message})

        try:
            membership = await client.fetch_membership(
                realm, token, user_id, proxy=proxy, timeout=timeout)
            group = str(membership.get('group_id') or '')
            windows: list[dict] = []
            if membership.get('subscribed') and group:
                try:
                    windows = await client.plan_quota(
                        realm, token, group, proxy=proxy, timeout=timeout)
                except PlatformError as exc:
                    out['errors'].append({'endpoint': 'plan_quota',
                                          'code': exc.code,
                                          'message': exc.message})
            out['entitlement'] = {
                'subscribed': bool(membership.get('subscribed')),
                'tier': membership.get('tier') or '',
                'until': store.iso_from_epoch(
                    membership.get('expires_at') or 0),
                'group_id': group,
                'windows': windows,
            }
        except PlatformError as exc:
            out['errors'].append({'endpoint': 'membership',
                                  'code': exc.code, 'message': exc.message})
        return out

    async def credits(self, account_id: str) -> dict:
        """积分余额（默认 60 秒缓存）。"""
        account = self._require(account_id)
        uid = str(account.get('uid') or '')
        cached = self._credits.get(uid)
        if cached and cached[0] > time.time():
            return cached[1]

        realm = store.normalize_realm(account.get('realm'))
        proxy = self._proxy_for(account)
        timeout = runtime_settings.credits_timeout(self.name)
        token = await self._token_for(account, proxy=proxy)
        try:
            user_id = await self._user_id(account, token, proxy=proxy)
        except PlatformError as exc:
            if exc.status != 401:
                raise
            token = await self._token_for(account, proxy=proxy, force=True)
            user_id = await self._user_id(account, token, proxy=proxy)
        ledger = await self._ledger(account, token, user_id, proxy=proxy,
                                    timeout=timeout)
        out = {
            'uid': uid,
            'realm': realm,
            'credits': ledger['left'],
            'remain': ledger['left'],
            'limit': ledger['total'],
            'used': ledger['used'],
            'unit': 'credits',
            'expires': ledger['expires'],
            'next_expire_at': ledger['next_expire_at'],
            'source': 'credit_details',
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
        seen: set[str] = set()
        out: list[dict] = []
        accounts = [a for a in store.list_accounts()
                    if str(a.get('refreshToken') or a.get('token') or '').strip()]
        for realm in C.REALMS:
            access = ''
            account = next((a for a in accounts
                            if store.normalize_realm(a.get('realm')) == realm), None)
            account_proxy = None
            if account is not None:
                try:
                    account_proxy = self._proxy_for(account)
                    access = await self._token_for(account, proxy=account_proxy)
                except PlatformError:
                    access = ''
            try:
                models = await client.fetch_models(realm, access,
                                                   proxy=account_proxy,
                                                   timeout=timeout)
            except PlatformError:
                continue
            for model in models:
                key = str(model.get('id') or '').lower()
                if key and key not in seen:
                    seen.add(key)
                    out.append(model)
        return catalog.normalize(out)

    async def list_models(self, *, refresh: bool = False) -> list[dict]:
        models = None if refresh else self._cached_models()
        if models is None and (refresh or self._implicit_live_allowed()):
            live = await self._live_models(
                timeout=45 if refresh else runtime_settings.catalog_timeout(self.name))
            self._note_implicit_attempt()
            if live:
                models = live
                self._save_models(models)
        if models is None:
            models = catalog.default_models()
        self._model_meta = {str(m.get('id') or '').strip().lower(): m
                            for m in models if m.get('id')}
        out: list[dict] = []
        for realm in C.REALMS:
            out.extend(self._public_model(m, realm) for m in models if m.get('id'))
        return out

    def _public_model(self, raw: dict, realm: str) -> dict:
        model_id = str(raw.get('id') or '')
        modalities = raw.get('modalities')
        if not isinstance(modalities, list) or not modalities:
            modalities = ['text']
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
            'modalities': {'input': [str(x) for x in modalities]},
            'model_name': model_id,
            'tool_call': bool(raw.get('tool_call', True)),
            'attachment': bool(raw.get('attachment') or 'image' in modalities),
            'credit_ratio': None,
        }

    def cached_model_ids(self) -> set[str] | None:
        models = self._cached_models() or catalog.default_models()
        if not models:
            return None
        out: set[str] = set()
        for realm in C.REALMS:
            out.update(self.public_model_id(str(m.get('id') or ''), realm)
                       for m in models if m.get('id'))
        return out

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

    def invalidate_limit(self) -> None:
        self._limiter.invalidate()

    # ── 选号 ──────────────────────────────────────────────
    def _candidates(self, realm: str | None = None,
                    affinity: str | None = None) -> list[dict]:
        wanted = store.normalize_realm(realm) if realm else None
        accounts = [a for a in store.list_accounts()
                    if str(a.get('uid') or '').strip()
                    and str(a.get('refreshToken') or a.get('token') or '').strip()
                    and not store.is_disabled(a)
                    and not store.is_cooling(a)
                    and (wanted is None
                         or store.normalize_realm(a.get('realm')) == wanted)]
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
        return await self._chat_accounts(model, body, ctx)

    async def _chat_accounts(self, model: str, body: dict,
                             ctx: ChatContext | None) -> PlatformChat:
        runtime = runtime_settings.load(self.name)
        affinity = affinity_key(body) if runtime['session_affinity'] else ''
        if affinity:
            self._affinity.set_ttl(runtime['session_affinity_ttl'])

        realm_hint = str(getattr(ctx, 'realm', '') or '').strip() or None
        candidates = self._candidates(realm_hint, affinity)
        if not candidates:
            detail = (f'（{store.normalize_realm(realm_hint)} 站点）'
                      if realm_hint else '')
            raise PlatformError(f'没有可用的 MiniMax Code 账号{detail}',
                                status=503, code='no_account')

        last_error: PlatformError | None = None
        for account in candidates[:runtime['max_accounts_try']]:
            uid = str(account.get('uid') or '')
            realm = store.normalize_realm(account.get('realm'))
            wrapper_model = self.public_model_id(model, realm)
            lease = None
            release: Callable[[], None] = _noop
            try:
                proxy = self._proxy_for(account)
                lease = await self._limiter.acquire(uid)
                if lease is False:
                    lease = None
                    last_error = PlatformError(
                        f'MiniMax Code 账号并发已满（单账号上限 '
                        f'{self._limiter.limit()}）', status=429,
                        code='account_busy')
                    continue
                release = _lease_releaser(self._limiter, lease)
                try:
                    result = await self._chat_once(
                        account, model, body, wrapper_model, realm,
                        proxy=proxy, runtime=runtime, affinity=affinity)
                except PlatformError as exc:
                    if exc.status != 401:
                        raise
                    self._drop_token(uid)
                    result = await self._chat_once(
                        account, model, body, wrapper_model, realm,
                        proxy=proxy, runtime=runtime, affinity=affinity,
                        force_token=True)
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
        raise last_error or PlatformError('MiniMax Code 账号均不可用', status=503,
                                          code='no_account')

    async def _chat_once(self, account: dict, model: str, body: dict,
                         wrapper_model: str, realm: str, *, proxy: str | None,
                         runtime: dict, affinity: str,
                         force_token: bool = False) -> PlatformChat:
        uid = str(account.get('uid') or '')
        token = await self._token_for(account, proxy=proxy, force=force_token)
        cap = self._output_cap(model)
        payload = translate.to_anthropic(body, output_cap=cap)
        payload['model'] = model
        # 会话 id 用亲和键的稳定派生值：既是同一段对话的粘性标识，又不会把
        # 原始 prompt 哈希 / 用户输入直接塞进上游请求头。
        session_id = (f'ses_{uuid.uuid5(uuid.NAMESPACE_URL, affinity).hex[:24]}'
                      if affinity else f'ses_{uuid.uuid4().hex[:24]}')

        if not payload.get('stream'):
            data = await client.chat(
                realm, token, payload, proxy=proxy,
                timeout=runtime['request_timeout'], session_id=session_id)
            result = translate.from_anthropic(data, wrapper_model)
            usage = result.get('usage') if isinstance(result.get('usage'), dict) else {}
            return PlatformChat(payload=result, usage=usage, model=wrapper_model,
                                account_hint=uid, realm=realm)

        started = time.monotonic()
        response = await client.open_chat(
            realm, token, payload, proxy=proxy,
            idle_timeout=runtime['idle_timeout'], session_id=session_id)
        translator = translate.StreamTranslator(wrapper_model, {})
        chat = PlatformChat(usage=translator.usage, model=wrapper_model,
                            account_hint=uid, realm=realm)
        closed = False

        async def _close() -> None:
            nonlocal closed
            if closed:
                return
            closed = True
            try:
                await response.aclose()
            except Exception:  # noqa: BLE001
                pass

        def _has_delta(chunk: dict) -> bool:
            for choice in chunk.get('choices') or []:
                delta = choice.get('delta') if isinstance(choice, dict) else None
                if isinstance(delta, dict) and (
                        delta.get('content') or delta.get('reasoning_content')
                        or delta.get('tool_calls')):
                    return True
            return False

        async def relay() -> AsyncIterator[bytes]:
            data_lines: list[str] = []
            finished = False
            try:
                async for line in response.aiter_lines():
                    if line == '':
                        if not data_lines:
                            continue
                        text = '\n'.join(data_lines).strip()
                        data_lines = []
                        if not text or text == '[DONE]':
                            continue
                        try:
                            event = json.loads(text)
                        except ValueError:
                            continue
                        chunks = translator.feed(event)
                        for chunk in chunks:
                            if chat.first_token_ms is None and _has_delta(chunk):
                                chat.first_token_ms = int(
                                    (time.monotonic() - started) * 1000)
                            yield translate.sse(chunk)
                        continue
                    if line.startswith('data:'):
                        data_lines.append(line[5:].strip())
                    elif line.startswith('event:') or line.startswith(':'):
                        continue
                    elif line.strip():
                        data_lines.append(line.strip())
                finished = True
            except translate._StreamError as exc:
                chat.error = exc.message
            except PlatformError as exc:
                chat.error = exc.message
            finally:
                if finished and not chat.error:
                    for chunk in translator.finish():
                        if chat.first_token_ms is None and _has_delta(chunk):
                            chat.first_token_ms = int(
                                (time.monotonic() - started) * 1000)
                        yield translate.sse(chunk)
                    yield b'data: [DONE]\n\n'
                elif chat.error and not finished:
                    # 头已经发出去了，只能补一帧错误并结束（与 `platforms/sse.py`
                    # 的口径一致）。客户端断开不会走到这里：那是 BaseException。
                    yield translate.sse({'error': {
                        'message': chat.error, 'type': 'api_error',
                        'code': 'platform_stream_error'}})
                    yield b'data: [DONE]\n\n'
                await _close()

        chat.raw_stream = relay()
        return chat


adapter = MinimaxAdapter()
