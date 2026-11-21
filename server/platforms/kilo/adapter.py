"""Kilo（kilo.ai）平台适配器。

与 Loomy / DuMate / Discovery 同构：本文件只负责**策略**，协议在 `client.py`。

  · 选号（权重 SWRR + LRU + 会话粘性）与单账号并发闸门；
  · 设备码登录（`login.py`）与手工粘贴 token 两条入口；
  · 模型目录缓存（免鉴权的在线目录 → 内置兜底）；
  · 余额（**美元**）与资料探活；
  · 流式（原始字节透传）/ 非流式两条出口的最后一步。

Kilo 是**单区域**平台（`realm_aware=False`），账号与模型统一 `realm='cn'`。

凭证是设备授权码换来的一年期 API token，没有 refresh token：`refresh_account`
做的是「拿 token 读一次资料 + 余额」，不是续期；token 失效只能重新登录。
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator, Callable

from ... import config, db
from ...services.session_affinity import SessionAffinity, affinity_key
from .. import settings as runtime_settings
from ..base import ChatContext, PlatformAdapter, PlatformChat, PlatformError
from ..limiter import AccountLimiter
from . import catalog, client, constants as C, store

SETTING_MODELS = 'platforms.kilo.models'
SETTING_MODELS_ATTEMPT = 'platforms.kilo.models_attempt'

# 余额不足（402）的冷却时长：付费模型要钱，免费档不受影响，冻 10 分钟足够让
# 批量任务改投别的账号，也不至于把「充值后立刻恢复」的号冻太久。
BALANCE_COOLDOWN_SECONDS = 600

# 一年期 token：设备码登录拿不到精确到期时间，按官方 `TOKEN_EXPIRATION_MS` 推算。
TOKEN_LIFETIME_SECONDS = 365 * 24 * 60 * 60


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


def _error_message(error: object) -> str:
    if isinstance(error, dict):
        for key in ('message', 'msg', 'desc'):
            value = error.get(key)
            if value:
                return str(value)
        return '上游返回错误'
    return str(error or '上游返回错误')


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
                    delta = choice.get('message') if isinstance(
                        choice.get('message'), dict) else {}
                if delta.get('content') or delta.get('reasoning_content'):
                    saw_content = True
    return pending, saw_content, error


class KiloAdapter(PlatformAdapter):
    name = 'kilo'
    label = 'Kilo'
    # 上游没有签到。
    supports_checkin = False
    # 设备授权码登录（官方客户端同款）。
    supports_web_login = True
    # 单区域平台。
    realm_aware = False

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_used: dict[str, float] = {}
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

    @staticmethod
    def _token_of(account: dict) -> str:
        return str(account.get('token') or '').strip()

    # ── 账号 ──────────────────────────────────────────────
    def list_accounts(self) -> list[dict]:
        return [store.public_account(a) for a in store.list_accounts()]

    def import_account(self, payload: dict) -> dict:
        """导入账号：粘贴 Kilo 的 API token。

        接受 `{account: {...}}` 与扁平对象两种形状；`token` / `apiKey` /
        `api_key` / `key` 都按 token 处理。导入阶段**不联网**，探活在
        `refresh_account` 或第一次对话时发生。
        """
        if not isinstance(payload, dict):
            raise PlatformError('账号必须是 JSON 对象', status=400,
                                code='invalid_account')
        raw = payload.get('account') if isinstance(payload.get('account'), dict) \
            else payload

        token = store.normalize_token(
            _pick(raw, 'token', 'apiKey', 'api_key', 'key', 'kilocodeToken'))
        if not token:
            raise PlatformError('需要提供 Kilo token', status=400,
                                code='invalid_account')
        try:
            uid = _pick(raw, 'uid') or store.derive_uid(token)
            uid = store.safe_id(uid)
        except ValueError as exc:
            raise PlatformError(str(exc), status=400,
                                code='invalid_account') from exc

        old = store.read_account(uid) or {}
        expires = store.epoch_from_iso(
            _pick(raw, 'expiresAt', 'expiredAt', 'expires_at'))
        if not expires:
            expires_in = _num(raw.get('expires_in'))
            if expires_in and expires_in > 0:
                expires = time.time() + expires_in
        if not expires and str(old.get('token') or '') == token:
            expires = store.epoch_from_iso(old.get('expiredAt'))
        if not expires:
            # 设备码登录不回到期时间：按官方一年期推算，界面才有「何时该重登」。
            expires = time.time() + TOKEN_LIFETIME_SECONDS

        account = {
            **old,
            'uid': uid,
            'token': token,
            'expiredAt': store.iso_from_epoch(expires),
            'email': _pick(raw, 'email') or str(old.get('email') or ''),
            'name': _pick(raw, 'name') or str(old.get('name') or ''),
            'nickname': _pick(raw, 'nickname', 'displayName')
            or str(old.get('nickname') or ''),
            'organizationId': _pick(raw, 'organizationId', 'organization_id')
            or str(old.get('organizationId') or ''),
            'proxy': _pick(raw, 'proxy', 'egress') or str(old.get('proxy') or ''),
            'proxy_session': _pick(raw, 'proxy_session', 'proxySession')
            or str(old.get('proxy_session') or ''),
            'source': _pick(raw, 'source') or str(old.get('source') or 'manual'),
            'lastRefreshError': '',
            'loginState': '正常',
        }
        self._credits.pop(uid, None)
        return store.public_account(store.save_account(account))

    def delete_account(self, account_id: str) -> bool:
        self._credits.pop(str(account_id or ''), None)
        return store.delete_account(account_id)

    async def refresh_account(self, account_id: str, *,
                              force: bool = False) -> dict:
        """读一次资料 + 余额（顺带探活）；没有 refresh token 可续。"""
        account = self._require(account_id)
        uid = str(account.get('uid') or '')
        token = self._token_of(account)
        if not token:
            raise PlatformError('Kilo 账号缺少 token', status=401,
                                code='login_expired')
        proxy = self._proxy_for(account)
        timeout = runtime_settings.credits_timeout(self.name)
        try:
            info = await client.fetch_profile(token, proxy=proxy, timeout=timeout)
        except PlatformError as exc:
            if exc.status == 401:
                self._mark_expired(uid, exc.message)
            else:
                try:
                    store.patch_account(uid, {'lastRefreshError': exc.message[:200]})
                except (FileNotFoundError, ValueError, OSError):
                    pass
            raise

        patch: dict[str, Any] = {
            'refreshedAt': store.iso_from_epoch(time.time()),
            'lastRefreshError': '',
            'loginState': '正常',
        }
        if info.get('email'):
            patch['email'] = info['email']
        if info.get('name'):
            patch['name'] = info['name']
        if info.get('organizationId'):
            patch['organizationId'] = info['organizationId']
        balance = await client.fetch_balance(
            token, organization_id=str(info.get('organizationId')
                                       or account.get('organizationId') or ''),
            proxy=proxy, timeout=timeout)
        if balance is not None:
            patch['balance'] = {'usd': balance, 'at': int(time.time()),
                                'message': ''}
        self._credits.pop(uid, None)
        return store.public_account(store.patch_account(uid, patch))

    @staticmethod
    def _mark_expired(uid: str, message: str = '') -> None:
        try:
            store.patch_account(uid, {
                'loginState': '登录已失效',
                'lastRefreshError': str(message or '')[:200],
            })
        except (FileNotFoundError, ValueError, OSError):
            pass

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

    # ── 任务 ──────────────────────────────────────────────
    async def checkin(self, account_id: str) -> dict:
        raise PlatformError('Kilo 没有签到', status=400,
                            code='checkin_unsupported')

    async def usage(self, account_id: str) -> dict:
        """额度 / 用量快照：Kilo 只有账户余额（美元），没有逐模型用量。"""
        account = self._require(account_id)
        uid = str(account.get('uid') or '')
        token = self._token_of(account)
        out: dict[str, Any] = {
            'uid': uid,
            'realm': store.REALM,
            'session_usage': None,
            'entitlement': None,
            'errors': [],
        }
        try:
            balance = await client.fetch_balance(
                token, organization_id=str(account.get('organizationId') or ''),
                proxy=self._proxy_for(account),
                timeout=runtime_settings.credits_timeout(self.name))
        except PlatformError as exc:
            out['errors'].append({'endpoint': 'profile/balance',
                                  'code': exc.code, 'message': exc.message})
            return out
        if balance is None:
            out['errors'].append({'endpoint': 'profile/balance',
                                  'code': 'credits_unavailable',
                                  'message': '取不到账户余额'})
            return out
        out['session_usage'] = {
            'balance': balance,
            'at': int(time.time()),
        }
        out['entitlement'] = {
            'balance_usd': balance,
            'organization_id': str(account.get('organizationId') or ''),
        }
        return out

    async def credits(self, account_id: str) -> dict:
        """余额快照（**美元**，默认 60 秒缓存）。"""
        account = self._require(account_id)
        uid = str(account.get('uid') or '')
        cached = self._credits.get(uid)
        if cached and cached[0] > time.time():
            return cached[1]

        token = self._token_of(account)
        balance = await client.fetch_balance(
            token, organization_id=str(account.get('organizationId') or ''),
            proxy=self._proxy_for(account),
            timeout=runtime_settings.credits_timeout(self.name))
        if balance is None:
            raise PlatformError('取不到 Kilo 账户余额', status=502,
                                code='credits_unavailable')
        out = {
            'uid': uid,
            'realm': store.REALM,
            'credits': balance,
            'remain': balance,
            'limit': None,
            'used': None,
            'unit': 'usd',
            'expires': [],
            'next_expire_at': None,
            'source': 'profile/balance',
        }
        self._credits[uid] = (time.time()
                              + runtime_settings.credits_ttl(self.name), out)
        try:
            store.patch_account(uid, {'balance': {
                'usd': balance, 'at': int(time.time()), 'message': ''}})
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

    async def list_models(self, *, refresh: bool = False) -> list[dict]:
        models = None if refresh else self._cached_models()
        if models is None and (refresh or self._implicit_live_allowed()):
            live = await self._live_models(refresh=refresh)
            self._note_implicit_attempt()
            if live:
                models = catalog.normalize(live)
                self._save_models(models)
        if models is None:
            models = catalog.default_models()
        self._model_meta = {str(m.get('id') or '').strip().lower(): m
                            for m in models if m.get('id')}
        return [self._public_model(m) for m in models if m.get('id')]

    async def _live_models(self, *, refresh: bool = False) -> list[dict]:
        """目录**免鉴权**可读：没有账号也照样能列模型。"""
        timeout = 45 if refresh else runtime_settings.catalog_timeout(self.name)
        # 目录与账号无关，但出口线路仍按「第一个可用账号」走：没有账号时用
        # 全局代理（`None`）。这样国际站部署也能拉到目录。
        proxy: str | None = None
        for account in store.list_accounts():
            if store.is_disabled(account):
                continue
            try:
                proxy = self._proxy_for(account)
            except PlatformError:
                proxy = None
            break
        try:
            return await client.fetch_models(proxy=proxy, timeout=timeout)
        except PlatformError:
            return []

    def _public_model(self, raw: dict) -> dict:
        model_id = str(raw.get('id') or '')
        modalities = raw.get('modalities')
        if not isinstance(modalities, list) or not modalities:
            modalities = ['text']
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
            'modalities': {'input': [str(x) for x in modalities]},
            'model_name': model_id,
            'tool_call': bool(raw.get('tool_call', True)),
            'attachment': 'image' in modalities,
            'credit_ratio': None,
            'free': bool(raw.get('free')),
        }

    def cached_model_ids(self) -> set[str] | None:
        """已缓存的对外模型 id（`kilo/...`），只读不联网。"""
        models = self._cached_models() or catalog.default_models()
        if not models:
            return None
        return {self.public_model_id(str(m.get('id') or ''), store.REALM)
                for m in models if m.get('id')}

    def upstream_model(self, model: str) -> str:
        """空模型名补成免费档默认模型；其余原样透传。"""
        text = str(model or '').strip()
        return text or C.DEFAULT_MODEL

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
        """只把账号从推理选号池里摘出去，探活照常。"""
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
                    and self._token_of(a)
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
            seconds = BALANCE_COOLDOWN_SECONDS
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
            raise PlatformError('没有可用的 Kilo 账号（请先登录或粘贴 token）',
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
                        f'Kilo 账号并发已满（单账号上限 '
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
                # 401 是 token 失效：冻了也不会自己好，只换号不冷却。
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
        raise last_error or PlatformError('Kilo 账号均不可用', status=503,
                                          code='no_account')

    async def _chat_once(self, account: dict, model: str, body: dict,
                         wrapper_model: str, *, proxy: str | None,
                         runtime: dict) -> PlatformChat:
        uid = str(account.get('uid') or '')
        token = self._token_of(account)
        if not token:
            raise PlatformError('Kilo 账号缺少 token', status=401,
                                code='login_expired')

        payload = client.chat_body(self.chat_body(model, body))
        if not payload.get('stream'):
            data = await client.chat(token, payload, proxy=proxy,
                                     timeout=runtime['request_timeout'])
            usage = data.get('usage') if isinstance(data.get('usage'), dict) else {}
            return PlatformChat(payload=data, usage=usage, model=wrapper_model,
                                account_hint=uid, realm=store.REALM)

        started = time.monotonic()
        response = await client.open_chat(
            token, payload, proxy=proxy,
            idle_timeout=runtime['idle_timeout'])
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


adapter = KiloAdapter()
