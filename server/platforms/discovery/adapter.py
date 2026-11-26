"""Discovery（Intern TokenPlan）平台适配器。

与 Loomy / DuMate 同构：本文件只负责**策略**，协议在 `client.py`。

  · 选号（权重 SWRR + LRU + 会话粘性）与单账号并发闸门；
  · API Key 的探活（上游没有余额接口，只能问「还能不能调」）；
  · 模型目录缓存（在线 → 内置快照）；
  · 流式（原始字节透传）/ 非流式两条出口的最后一步。

Discovery 是**单区域**平台（`realm_aware=False`），账号与模型统一 `realm='cn'`。

凭证是 **API Key**（约 6 个月有效），没有 refresh token：`refresh_account`
做的是「拿 Key 探一次活」，不是续期；Key 失效只能去控制台换一把。

## 关于 `force-model`

参考实现有一个 `force-model` 开关：因为上游只暴露一个聚合模型，它把请求里的
`model` 一律改写成配置值。这里**不做**这件事——静默改写会让「我要的是 A，
回来的是 B」变得无法解释。适配器只做两件事：空模型名补成 `auto`；目录里的
名字原样透传。真要固定模型，在客户端写 `discovery/auto` 即可。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator, Callable

from ... import db
from ...services.session_affinity import SessionAffinity, affinity_key
from .. import settings as runtime_settings
from ..base import ChatContext, PlatformAdapter, PlatformChat, PlatformError
from ..limiter import AccountLimiter
from . import catalog, client, constants as C, store

logger = logging.getLogger('workbuddy.platforms.discovery')

SETTING_MODELS = 'platforms.discovery.models'
SETTING_MODELS_ATTEMPT = 'platforms.discovery.models_attempt'

# 额度耗尽（402）的冷却时长。额度是按账户算的，同一账户下的其它 Key 也一样
# 耗尽；冻 1 小时足够让批量任务改投别的账户，也不至于把「明天恢复」的号冻死。
QUOTA_COOLDOWN_SECONDS = 3600

# Cluster TPM 429 是**账户级**上限：同一账户下的 Key 共享同一个 ceiling，
# 换号逃不掉。等窗口（上游约 1 分钟）比把整池挨个冻一遍便宜；一次请求最多
# 等两轮、每轮不超过 15 秒，避免客户端被挂死。
CLUSTER_WAIT_LIMIT = 2
CLUSTER_WAIT_MAX_SECONDS = 15


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


class DiscoveryAdapter(PlatformAdapter):
    name = 'discovery'
    label = 'Discovery（Intern TokenPlan）'
    # 上游没有签到。
    supports_checkin = False
    # 凭证是一把 API Key，没有网页登录流程。
    supports_web_login = False
    # 单区域平台。
    realm_aware = False

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_used: dict[str, float] = {}
        # uid -> (过期时刻, 探针结果)：探活结果只缓存在内存，落盘的是摘要。
        self._probes: dict[str, tuple[float, dict]] = {}
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
    def _key_of(account: dict) -> str:
        return str(account.get('apiKey') or '').strip()

    # ── 账号 ──────────────────────────────────────────────
    def list_accounts(self) -> list[dict]:
        return [store.public_account(a) for a in store.list_accounts()]

    def export_accounts(self) -> list[dict]:
        """导出含明文凭据的账号，仅供管理员导出接口调用。"""
        return store.list_accounts()

    def import_account(self, payload: dict) -> dict:
        """导入账号：粘贴 Intern TokenPlan 的 API Key。

        接受 `{account: {...}}` 与扁平对象两种形状；`api_key` / `apiKey` /
        `key` / `token` 都按 Key 处理。导入阶段**不联网**，探活在
        `refresh_account` 或第一次对话时发生。
        """
        if not isinstance(payload, dict):
            raise PlatformError('账号必须是 JSON 对象', status=400,
                                code='invalid_account')
        raw = payload.get('account') if isinstance(payload.get('account'), dict) \
            else payload

        api_key = store.normalize_key(
            _pick(raw, 'apiKey', 'api_key', 'key', 'token', 'secret'))
        if not api_key:
            raise PlatformError('需要提供 API Key', status=400,
                                code='invalid_account')
        try:
            uid = _pick(raw, 'uid') or store.derive_uid(api_key)
            uid = store.safe_id(uid)
        except ValueError as exc:
            raise PlatformError(str(exc), status=400,
                                code='invalid_account') from exc

        old = store.read_account(uid) or {}
        expires = store.epoch_from_iso(
            _pick(raw, 'expiresAt', 'expiredAt', 'expires_at'))
        if not expires:
            days = _num(raw.get('valid_days') or raw.get('validDays'))
            if days and days > 0:
                expires = time.time() + days * 86400
        if not expires and str(old.get('apiKey') or '') == api_key:
            expires = store.epoch_from_iso(old.get('expiredAt'))

        account = {
            **old,
            'uid': uid,
            'apiKey': api_key,
            'expiredAt': store.iso_from_epoch(expires) if expires else '',
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
        self._probes.pop(uid, None)
        return store.public_account(store.save_account(account))

    def delete_account(self, account_id: str) -> bool:
        self._probes.pop(str(account_id or ''), None)
        return store.delete_account(account_id)

    async def refresh_account(self, account_id: str, *,
                              force: bool = False) -> dict:
        """用 Key 探一次活（不是续期）：上游没有 refresh token。"""
        account = self._require(account_id)
        uid = str(account.get('uid') or '')
        api_key = self._key_of(account)
        if not api_key:
            raise PlatformError('Discovery 账号缺少 API Key', status=401,
                                code='login_expired')
        proxy = self._proxy_for(account)
        out = await client.probe(api_key, proxy=proxy,
                                 timeout=runtime_settings.credits_timeout(self.name))
        state = str(out.get('state') or 'unknown')
        patch: dict[str, Any] = {
            'quota': {'state': state, 'at': int(time.time()),
                      'message': str(out.get('message') or '')[:200]},
            'refreshedAt': store.iso_from_epoch(time.time()),
        }
        if state == 'invalid':
            patch['loginState'] = '登录已失效'
            patch['lastRefreshError'] = str(out.get('message') or 'API Key 无效')[:200]
        elif state in ('ok', 'quota_exhausted', 'rate_limited'):
            patch['loginState'] = '正常'
            patch['lastRefreshError'] = ''
        else:
            patch['lastRefreshError'] = str(out.get('message') or '')[:200]
        saved = store.patch_account(uid, patch)
        self._probes[uid] = (time.time() + runtime_settings.credits_ttl(self.name),
                             out)
        if state == 'invalid':
            raise PlatformError(str(out.get('message') or 'API Key 无效'),
                                status=401, code='login_expired')
        return store.public_account(saved)

    # ── 任务 ──────────────────────────────────────────────
    async def checkin(self, account_id: str) -> dict:
        raise PlatformError('Discovery 没有签到', status=400,
                            code='checkin_unsupported')

    async def usage(self, account_id: str) -> dict:
        """额度 / 用量快照：只有「还能不能调」，没有数字余额。"""
        account = self._require(account_id)
        uid = str(account.get('uid') or '')
        api_key = self._key_of(account)
        out: dict[str, Any] = {
            'uid': uid,
            'realm': store.REALM,
            'session_usage': None,
            'entitlement': None,
            'errors': [],
        }
        probe = await self._probe(account, force=True)
        state = str(probe.get('state') or 'unknown')
        out['session_usage'] = {
            'state': state,
            'message': str(probe.get('message') or ''),
            'at': int(time.time()),
            'models': len(probe.get('models') or []),
        }
        out['entitlement'] = {
            'quota_known': state == 'ok',
            'exhausted': state == 'quota_exhausted',
            'rate_limited': state == 'rate_limited',
            'api_key': store.mask_key(api_key),
        }
        if state in ('invalid', 'unknown'):
            out['errors'].append({'endpoint': 'models',
                                  'code': 'login_expired' if state == 'invalid'
                                  else 'probe_failed',
                                  'message': str(probe.get('message') or '')})
        return out

    async def _probe(self, account: dict, *, force: bool = False) -> dict:
        uid = str(account.get('uid') or '')
        cached = self._probes.get(uid)
        if cached and not force and cached[0] > time.time():
            return cached[1]
        api_key = self._key_of(account)
        out = await client.probe(
            api_key, proxy=self._proxy_for(account),
            timeout=runtime_settings.credits_timeout(self.name))
        self._probes[uid] = (time.time() + runtime_settings.credits_ttl(self.name),
                             out)
        return out

    # ── 模型目录 ──────────────────────────────────────────
    def _cached_models(self, *, allow_stale: bool = False) -> list[dict] | None:
        """已缓存的模型目录；`allow_stale=True` 时不看 TTL。

        陈旧兜底是刻意留的：上游抖一次、`/v1/models` 拉不到时，宁可继续用
        上一份真实目录，也不该退回内置快照——内置快照里的模型可能早就下线，
        客户端会照着一份过期的名单发请求。
        """
        try:
            cached = db.get_setting(SETTING_MODELS, None)
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(cached, dict):
            return None
        if not allow_stale and float(cached.get('fetched_at') or 0) \
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
                self._save_models(live)
                models = live
            else:
                # 拉不到就继续用上一份真实目录（哪怕已过 TTL）：模型页 / 客户端
                # 需要的是「这个名字现在还能不能用」，一份过期但真实的名字
                # 远比内置快照可靠。完全没有历史目录时才退到内置快照。
                models = self._cached_models(allow_stale=True)
        # 空表也要兜底：`_live_models` 在「一个可用账号都没有」时返回 `[]`
        # 而不是 None，只判 None 会让模型页在一把 Key 都没导入时整片空白。
        if not models:
            models = catalog.default_models()
        self._model_meta = {str(m.get('id') or '').strip().lower(): m
                            for m in models if m.get('id')}
        return [self._public_model(m) for m in models if m.get('id')]

    async def _live_models(self, *, refresh: bool = False) -> list[dict]:
        """用**任意一把可用 Key** 拉目录：目录是按账户给的，与具体 Key 无关。

        全部账号都失败时返回空表（调用方降级到内置快照），不抛——一次目录
        刷新失败不该让模型页整片空白。
        """
        timeout = 45 if refresh else runtime_settings.catalog_timeout(self.name)
        for account in store.list_accounts():
            api_key = self._key_of(account)
            if not api_key or store.is_disabled(account):
                continue
            try:
                models = await client.fetch_models(
                    api_key, proxy=self._proxy_for(account), timeout=timeout)
            except PlatformError:
                continue
            if models:
                return catalog.normalize(models)
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
            'tool_call': True,
            'attachment': 'image' in modalities,
            'credit_ratio': None,
        }

    def cached_model_ids(self) -> set[str] | None:
        """已缓存的对外模型 id（`discovery/...`），只读不联网。"""
        models = self._cached_models() or catalog.default_models()
        if not models:
            return None
        return {self.public_model_id(str(m.get('id') or ''), store.REALM)
                for m in models if m.get('id')}

    def upstream_model(self, model: str) -> str:
        """空模型名补成 `auto`；其余原样透传（见模块 docstring 的说明）。"""
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
                    and self._key_of(a)
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
        # 链路失败（DNS / 超时 / 代理断开）与 Cluster TPM 都不是该 Key 的健康
        # 信号：前者换号仍可能走另一条代理，后者是账户级上限。都不冷却。
        if exc.code in ('network_error', 'cluster_rate_limited'):
            return False
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

        # 方言归一放在选号之前：它是「上游认不认这个请求体」的问题，与用哪把
        # Key 无关，重试之间也不必再算一遍。标准体（已经是上游方言）原样
        # 通过，一个字节都不改。
        body, notes = client.normalize_body(body)
        if notes:
            logger.info('discovery 请求体已归一：%s', ','.join(notes))

        candidates = self._candidates(affinity)
        if not candidates:
            raise PlatformError('没有可用的 Discovery 账号（请先导入 API Key）',
                                status=503, code='no_account')

        last_error: PlatformError | None = None
        # 慢速 400 的专用预算：只有一次，且与 `max_accounts_try` 独立——把它
        # 混进普通重试会让「上游抖一下」直接吃掉本该留给真正坏号的尝试次数。
        transient_budget = 1
        # Cluster TPM 的等待预算：上限按请求算，重排同一个账号而不是换号。
        cluster_waits = 0
        queue = list(candidates[:runtime['max_accounts_try']])
        while queue:
            account = queue.pop(0)
            uid = str(account.get('uid') or '')
            lease = None
            release: Callable[[], None] = _noop
            started = time.monotonic()
            try:
                proxy = self._proxy_for(account)
                lease = await self._limiter.acquire(uid)
                if lease is False:
                    lease = None
                    last_error = PlatformError(
                        f'Discovery 账号并发已满（单账号上限 '
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
                # 慢速 400：上游收下请求、处理了十几秒才说「不收」，是后端
                # 抖动而不是客户端写错。此刻还没有向客户端写任何字节，换把
                # Key 重试是安全的；schema 级 400 都在 1 秒内回来，不满足
                # 耗时门槛，仍走原来的快速失败。
                elapsed = time.monotonic() - started
                if transient_budget > 0 and client.is_transient_rejection(
                        exc, elapsed):
                    transient_budget -= 1
                    logger.warning(
                        'discovery 上游慢速拒绝（%.1fs，key=%s），换号重试：%s',
                        elapsed, store.mask_key(self._key_of(account)),
                        str(exc.message)[:200])
                    continue
                # Cluster TPM 429：账户级上限，换号没用。等共享窗口重开，
                # 把同一个账号放回队首再试；超过次数就按 429 交给客户端。
                if exc.code == 'cluster_rate_limited' \
                        and cluster_waits < CLUSTER_WAIT_LIMIT:
                    cluster_waits += 1
                    wait = min(float(exc.retry_after or 10),
                               CLUSTER_WAIT_MAX_SECONDS)
                    logger.warning(
                        'discovery Cluster TPM 限流（key=%s），等待 %.0fs 后'
                        '重试同一账号（%d/%d）：%s',
                        store.mask_key(self._key_of(account)), wait,
                        cluster_waits, CLUSTER_WAIT_LIMIT,
                        str(exc.message)[:200])
                    queue.insert(0, account)
                    await asyncio.sleep(wait)
                    continue
                # 链路失败不是该 Key 的健康信号：没配代理时换号无处可去，
                # 但每个账号可以走不同代理，换下一个仍有机会，且不冷却。
                if exc.code == 'network_error':
                    continue
                # 401 是 Key 失效：冻了也不会自己好，只换号不冷却。
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
        raise last_error or PlatformError('Discovery 账号均不可用', status=503,
                                          code='no_account')

    async def _chat_once(self, account: dict, model: str, body: dict,
                         wrapper_model: str, *, proxy: str | None,
                         runtime: dict) -> PlatformChat:
        uid = str(account.get('uid') or '')
        api_key = self._key_of(account)
        if not api_key:
            raise PlatformError('Discovery 账号缺少 API Key', status=401,
                                code='login_expired')

        payload = self.chat_body(model, body)
        if not payload.get('stream'):
            data = await client.chat(api_key, payload, proxy=proxy,
                                     timeout=runtime['request_timeout'])
            usage = data.get('usage') if isinstance(data.get('usage'), dict) else {}
            return PlatformChat(payload=data, usage=usage, model=wrapper_model,
                                account_hint=uid, realm=store.REALM)

        started = time.monotonic()
        response = await client.open_chat(
            api_key, payload, proxy=proxy,
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


adapter = DiscoveryAdapter()
