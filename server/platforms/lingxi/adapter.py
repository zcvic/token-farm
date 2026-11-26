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
import time
from typing import Any, AsyncIterator, Callable

from ... import config, db
from ...services.session_affinity import SessionAffinity, affinity_key
from .. import settings as runtime_settings
from ..base import ChatContext, PlatformAdapter, PlatformChat, PlatformError
from ..limiter import AccountLimiter
from . import catalog, chat as chatproto, client, constants as C, store

logger = logging.getLogger('workbuddy.platforms.lingxi')

SETTING_MODELS = 'platforms.lingxi.models'
SETTING_MODELS_ATTEMPT = 'platforms.lingxi.models_attempt'

# 429 / 额度的冷却口径。灵犀用 429 表达限流；与其它平台一致，默认 0 表示
# 「维持旧行为：直接上抛、不换号不冻结」，由设置项开启。
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
    # 单区域平台。
    realm_aware = False

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_used: dict[str, float] = {}
        self._probes: dict[str, tuple[float, dict]] = {}
        self._credits: dict[str, tuple[float, dict]] = {}
        self._limiter = AccountLimiter(self.name)
        self._swrr: dict[str, float] = {}
        self._affinity = SessionAffinity()
        self._model_meta: dict[str, dict] = {}

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

        response = await client.open_chat(
            account, payload, model, proxy=proxy,
            idle_timeout=runtime['idle_timeout'])
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
            try:
                async for frame in client.iter_frames(response):
                    translator.feed(frame)
            except chatproto.StreamError as exc:
                raise PlatformError(exc.message, status=400
                                    if exc.code == 'audit_blocked' else 502,
                                    code=exc.code) from exc
            finally:
                await _close()
            if not translator.finished:
                raise PlatformError(
                    '灵犀上游流在完成事件之前结束（响应被截断）', status=502,
                    code='truncated_stream')
            if not translator.text.strip() and not translator.reasoning \
                    and not translator.has_tool_calls:
                raise PlatformError('灵犀没有返回内容', status=502,
                                    code='empty_response')
            completion = translator.completion()
            return PlatformChat(payload=completion, usage=usage,
                                model=wrapper_model, account_hint=uid,
                                realm=store.REALM)

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
            # 错误在收尾之后继续抛：`platforms/sse.openai_sse` 会补一帧 error
            # 并让网关记 502；只设 `chat.error` 不抛，客户端会看到一条正常结束
            # 的空流，日志里也还是 200。
            if failure is not None:
                raise failure

        chat.stream = relay()
        return chat


adapter = LingxiAdapter()