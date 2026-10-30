"""Trae 平台适配器：把协议层与存储层组装成平台能力。

这里负责「策略」而非「协议」：

  · 选哪个账号（按模型所属区域 + 最近最少使用）；
  · 过期先刷新、刷新串行且失败退避；
  · 模型目录的缓存与兜底；
  · 流式 / 非流式两条出口的最后一步。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from ... import db
from ..base import PlatformAdapter, PlatformChat, PlatformError
from . import catalog, chat as chatmod, client, desktop, store
from .constants import DEFAULT_REALM, REALMS, normalize_realm

SETTING_MODELS = 'platforms.trae.models'
SETTING_MODELS_ATTEMPT = 'platforms.trae.models_attempt'
_TTL = 6 * 3600              # 目录缓存 6 小时
_IMPLICIT_TTL = 600          # 隐式拉取（/v1/models）失败后的冷却，避免每次请求都等超时
_IMPLICIT_TIMEOUT = 10       # 隐式拉取的超时：客户端启动时等 45 秒是不行的
_REFRESH_BASE = 30           # 刷新失败退避起点（秒）
_REFRESH_MAX = 1800          # 退避封顶（秒）
_MAX_ACCOUNTS_TRY = 3        # 单次对话最多换几个账号（仅 401/403 才换）


def _pick(raw: dict, *keys: str) -> str:
    for key in keys:
        value = raw.get(key)
        if value not in (None, ''):
            return str(value)
    return ''


def _normalize_realm_value(value: object) -> str:
    text = str(value or '').strip().lower()
    if text in ('intl', 'international', 'global', 'us', '海外', '国际'):
        return 'intl'
    if text in ('cn', 'china', 'domestic', '国内'):
        return 'cn'
    return DEFAULT_REALM


class TraeAdapter(PlatformAdapter):
    name = 'trae'
    label = 'Trae'
    supports_checkin = True

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._failures: dict[str, tuple[int, float]] = {}
        self._last_used: dict[str, float] = {}

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

    def _backoff_remaining(self, uid: str) -> float:
        entry = self._failures.get(uid)
        if not entry:
            return 0.0
        return max(0.0, entry[1] - time.time())

    def _note_failure(self, uid: str) -> None:
        count = (self._failures.get(uid) or (0, 0.0))[0] + 1
        delay = min(_REFRESH_MAX, _REFRESH_BASE * (2 ** min(count - 1, 6)))
        self._failures[uid] = (count, time.time() + delay)

    def _clear_failure(self, uid: str) -> None:
        self._failures.pop(uid, None)

    async def _ensure_token(self, account: dict, *, force: bool = False) -> dict:
        """确保 access token 可用；需要时刷新并落盘。

        两个与客户端共存的细节：

          · **刷新前**先看客户端是不是已经刷新过（`desktop.fresher`）。用户开着
            IDE 时，它会在我们之前轮换 refreshToken，我们手里的那份已经作废；
            直接拿去换只会拿到 401，还会顺手把账号打进退避。
          · **刷新后**把新凭据写回客户端（`desktop.sync`）。轮换是单向的：
            不写回，用户下次打开 IDE 就会被登出。
        """
        uid = str(account.get('uid') or '')
        if not force and not store.expired(account):
            return account
        if not force:
            remaining = self._backoff_remaining(uid)
            if remaining > 0:
                raise PlatformError(
                    f'令牌刷新失败冷却中（约 {int(remaining)} 秒后可重试）',
                    status=429, code='refresh_backoff', retry_after=int(remaining))
        async with self._lock(uid):
            # 等锁期间别的请求可能已经刷好了
            fresh_read = store.read_account(uid) or account
            if not force and not store.expired(fresh_read):
                return fresh_read

            adopted = desktop.fresher(fresh_read)
            if adopted:
                fresh_read = store.save_account({**fresh_read, **adopted})
                if not store.expired(fresh_read):
                    return fresh_read

            try:
                fresh = await client.exchange_token(fresh_read)
            except PlatformError as exc:
                # 客户端可能刚好在我们重试之前完成了轮换
                adopted = desktop.fresher(fresh_read)
                if adopted:
                    saved = store.save_account({**fresh_read, **adopted})
                    if not store.expired(saved):
                        self._clear_failure(uid)
                        return saved
                self._note_failure(uid)
                raise
            except Exception as exc:  # noqa: BLE001 —— 网络类异常也要退避
                self._note_failure(uid)
                raise PlatformError(f'刷新令牌失败：{exc}', status=502,
                                    code='refresh_failed') from exc
            self._clear_failure(uid)
            saved = store.save_account({**fresh_read, **fresh})
            if saved.get('desktopPath'):
                desktop.sync(saved)
            return saved

    # ── 账号 ──────────────────────────────────────────────
    def list_accounts(self) -> list[dict]:
        return [store.public_account(a) for a in store.list_accounts()]

    def import_account(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise PlatformError('账号必须是 JSON 对象', status=400, code='invalid_account')
        raw = payload.get('account') if isinstance(payload.get('account'), dict) else payload
        uid = _pick(raw, 'uid', 'user_id', 'userId')
        if not uid:
            raise PlatformError('缺少 uid', status=400, code='invalid_account')
        token = _pick(raw, 'token', 'accessToken', 'access_token')
        refresh = _pick(raw, 'refreshToken', 'refresh_token')
        if not token and not refresh:
            raise PlatformError('缺少 token 或 refreshToken', status=400,
                                code='invalid_account')

        old = store.read_account(uid) or {}
        expired_raw = raw.get('expiredAt', raw.get('expiresAt', raw.get('expired_at')))
        expired_at = client.iso_from_ms(expired_raw) if isinstance(expired_raw, (int, float)) \
            else (str(expired_raw) if expired_raw not in (None, '') else old.get('expiredAt', ''))

        account = {
            'uid': uid,
            'realm': _normalize_realm_value(raw.get('realm') or old.get('realm')),
            'token': token or old.get('token') or '',
            'refreshToken': refresh or old.get('refreshToken') or '',
            'expiredAt': expired_at or '',
            'refreshExpiredAt': str(
                client.iso_from_ms(raw.get('refreshExpiredAt'))
                if isinstance(raw.get('refreshExpiredAt'), (int, float))
                else (raw.get('refreshExpiredAt') or old.get('refreshExpiredAt') or '')),
            'nickname': _pick(raw, 'nickname', 'name', 'displayName') or old.get('nickname', ''),
            'email': _pick(raw, 'email') or old.get('email', ''),
            'proxy': _pick(raw, 'proxy', 'egress') or old.get('proxy', ''),
            # 来自本机客户端的账号才做「回写」；手工粘贴的 token 不该去改 IDE 的
            # storage.json（路径可能根本不属于这个账号）。
            'desktopPath': _pick(raw, 'desktopPath') or old.get('desktopPath', ''),
        }
        return store.public_account(store.save_account(account))

    def import_from_desktop(self, *, realm: str | None = None) -> dict:
        """一键导入本机 Trae 客户端里已登录的账号。

        单个账号失败不影响其他账号：客户端可能同时装了国内版与国际版，其中
        一个的 blob 坏掉时，另一个仍然应该能导进来。
        """
        wanted = str(realm or '').strip().lower() or None
        imported: list[dict] = []
        failed: list[dict] = []
        for account in desktop.read_accounts():
            if wanted and account.get('realm') != wanted:
                continue
            try:
                imported.append(self.import_account(account))
            except (PlatformError, ValueError) as exc:
                failed.append({'uid': str(account.get('uid') or ''),
                               'error': getattr(exc, 'message', str(exc))})
        return {
            'platform': self.name,
            'sources': [str(e['path']) for e in desktop.storage_paths()],
            'imported': imported,
            'failed': failed,
        }

    def delete_account(self, account_id: str) -> bool:
        return store.delete_account(account_id)

    async def refresh_account(self, account_id: str, *, force: bool = False) -> dict:
        account = self._require(account_id)
        if not force and not store.expired(account):
            return store.public_account(account)
        fresh = await self._ensure_token(account, force=True)
        return store.public_account(fresh)

    def fingerprint(self, account_id: str) -> dict:
        account = self._require(account_id)
        from . import fingerprint

        return fingerprint.summary(str(account.get('uid') or ''))

    # ── 任务 ──────────────────────────────────────────────
    async def checkin(self, account_id: str) -> dict:
        account = self._require(account_id)
        realm = normalize_realm(account.get('realm'))
        if not REALMS[realm]['supports_checkin']:
            raise PlatformError(f'{REALMS[realm]["label"]}不支持每日签到',
                                status=400, code='checkin_unsupported')
        account = await self._ensure_token(account)
        result = await client.claim_checkin(account, realm=realm)
        result.pop('raw', None)
        return {**result, 'uid': str(account.get('uid') or ''), 'realm': realm}

    async def usage(self, account_id: str) -> dict:
        account = self._require(account_id)
        realm = normalize_realm(account.get('realm'))
        account = await self._ensure_token(account)
        out: dict[str, Any] = {
            'uid': str(account.get('uid') or ''),
            'realm': realm,
            'session_usage': None,
            'entitlement': None,
            'errors': [],
        }
        try:
            out['session_usage'] = await client.fetch_usage(account, realm=realm)
        except PlatformError as exc:
            out['errors'].append({'endpoint': 'session_usage', 'code': exc.code,
                                  'message': exc.message})
        try:
            out['entitlement'] = await client.fetch_entitlement(account, realm=realm)
        except PlatformError as exc:
            out['errors'].append({'endpoint': 'entitlement', 'code': exc.code,
                                  'message': exc.message})
        return out

    # ── 模型目录 ──────────────────────────────────────────
    def _cached_models(self) -> list[dict] | None:
        try:
            cached = db.get_setting(SETTING_MODELS, None)
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(cached, dict):
            return None
        if float(cached.get('fetched_at') or 0) + _TTL < time.time():
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

    @staticmethod
    def _implicit_live_allowed() -> bool:
        """首次启动（还没有缓存）时允许拉一次线上目录。

        为什么要允许：本地目录里有相当一部分模型对当前账号是**调不通**的
        （上游回 `4023 model is unknown`），客户端照着列表选一个就失败，比
        「列表略少」糟糕得多。只在**完全没有缓存**时尝试，且失败后冷却
        10 分钟，避免每次 `/v1/models` 都等超时。
        """
        try:
            last = float(db.get_setting(SETTING_MODELS_ATTEMPT, 0) or 0)
        except Exception:  # noqa: BLE001
            return False
        return time.time() - last > _IMPLICIT_TTL

    @staticmethod
    def _note_implicit_attempt() -> None:
        try:
            db.set_setting(SETTING_MODELS_ATTEMPT, int(time.time()))
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _from_live(raw: dict) -> dict | None:
        if raw.get('chat') is False:
            return None
        model_id = raw.get('key') or raw.get('name') or raw.get('id')
        if not model_id:
            return None
        # 国内的 `model_list` 只回 `{name, display_name}`（没有上下文长度等），
        # 而本地目录里有这些元数据：按 id 补上，客户端才不会把模型当成 0 上下文。
        known = catalog.find(str(model_id)) or {}
        thinking = raw.get('thinking') if isinstance(raw.get('thinking'), dict) else {}
        return {
            'id': str(model_id),
            'name': str(raw.get('display_name') or raw.get('name')
                        or known.get('name') or model_id),
            'realm': 'cn',
            'context_window': int(raw.get('context_window')
                                  or known.get('context_window') or 0),
            'max_tokens': int(raw.get('max_tokens') or known.get('max_tokens') or 0),
            'multimodal': bool(raw.get('multimodal', known.get('multimodal'))),
            'thinking': bool(thinking.get('supported', known.get('thinking'))),
        }

    async def _live_models(self, *, timeout: float = 45) -> list[dict]:
        """向国内版账号要一次实时目录；拿不到就返回空（调用方兜底）。

        **不刷新令牌**：目录是只读信息，不值得为它阻塞一次可能 30 秒的刷新，
        更不该因为「客户端问了一下模型列表」就轮换掉用户的 refreshToken。
        过期的账号直接跳过，交给管理端的「刷新」按钮处理。
        """
        for account in store.list_accounts():
            if normalize_realm(account.get('realm')) != 'cn':
                continue
            if store.expired(account):
                continue
            try:
                raw = await client.fetch_model_list(account, realm='cn',
                                                    timeout=timeout)
            except PlatformError:
                continue
            models = [m for m in (self._from_live(x) for x in raw) if m]
            if models:
                return models
        return []

    async def list_models(self, *, refresh: bool = False) -> list[dict]:
        """模型目录。

        默认**只读缓存**（发请求会让客户端启动时等上游超时），但缓存为空时
        允许尝试一次线上目录——本地兜底目录里有一部分模型对这个账号根本调
        不通（`4023 model is unknown`），照着它选模型必然失败。
        """
        models = self._cached_models() if not refresh else None
        if models is None and (refresh or self._implicit_live_allowed()):
            live = await self._live_models(
                timeout=45 if refresh else _IMPLICIT_TIMEOUT)
            self._note_implicit_attempt()
            if live:
                self._save_models(live)
                models = live
        if models is None:
            models = catalog.default_models()
        return [self._public_model(m) for m in models if m.get('id')]

    def _public_model(self, raw: dict) -> dict:
        model_id = str(raw.get('id') or '')
        inputs = ['text']
        if raw.get('multimodal'):
            inputs.append('image')
        return {
            'id': f'{self.model_prefix}{model_id}',
            'object': 'model',
            'created': 0,
            'owned_by': self.name,
            'name': str(raw.get('name') or model_id),
            'platform': self.name,
            'realm': str(raw.get('realm') or DEFAULT_REALM),
            'context_window': int(raw.get('context_window') or 0),
            'max_tokens': int(raw.get('max_tokens') or 0),
            'supports_reasoning': bool(raw.get('thinking')),
            'modalities': {'input': inputs},
            'model_name': model_id,
        }

    @staticmethod
    def _model_realm(model: str) -> str | None:
        found = catalog.find(model)
        return normalize_realm(found['realm']) if found else None

    # ── 对话 ──────────────────────────────────────────────
    def _candidates(self, model: str) -> list[dict]:
        accounts = [a for a in store.list_accounts()
                    if str(a.get('uid') or '').strip()
                    and (a.get('token') or a.get('refreshToken'))]
        if not accounts:
            return []
        want = self._model_realm(model)
        if want:
            matched = [a for a in accounts
                       if normalize_realm(a.get('realm')) == want]
            if matched:
                accounts = matched
        # 最近最少使用优先：连续请求会自然分摊到多个账号
        accounts.sort(key=lambda a: self._last_used.get(str(a.get('uid')), 0.0))
        return accounts

    async def chat(self, model: str, body: dict) -> PlatformChat:
        wrapper_model = f'{self.model_prefix}{model}'
        candidates = self._candidates(model)
        if not candidates:
            raise PlatformError('没有可用的 Trae 账号（请先导入并刷新令牌）',
                                status=503, code='no_account')
        payload = client.build_chat_body(model, body.get('messages'), body)
        last_error: PlatformError | None = None
        for account in candidates[:_MAX_ACCOUNTS_TRY]:
            uid = str(account.get('uid') or '')
            try:
                ready = await self._ensure_token(account)
                result = await self._chat_once(ready, payload, wrapper_model,
                                               stream=bool(body.get('stream')))
            except PlatformError as exc:
                last_error = exc
                # 只有「凭据不可用」才换号；429/5xx 换号往往只是把限流扩散到更多账号
                if exc.status in (401, 403):
                    continue
                raise
            self._last_used[uid] = time.time()
            return result
        raise last_error or PlatformError('Trae 账号均不可用', status=503,
                                          code='no_account')

    async def _chat_once(self, account: dict, payload: dict, wrapper_model: str,
                         *, stream: bool) -> PlatformChat:
        response = await client.open_chat(account, payload)
        events = client.iter_events(response)

        # 先读一帧：上游的鉴权/风控错误通常就是第一帧。此时还没向客户端写
        # 任何字节，可以干净地改成 JSON 错误体，而不是发出半条流再断掉。
        prefetched: list[tuple[str, Any]] = []
        try:
            first = await events.__anext__()
        except StopAsyncIteration:
            first = None
        if first is not None:
            if first[0] == 'error':
                await response.aclose()
                raise PlatformError(chatmod._error_text(first[1]), status=502,
                                    code='upstream_error', payload=first[1])
            prefetched.append(first)

        async def chained():
            for item in prefetched:
                yield item
            async for item in events:
                yield item

        state = chatmod.StreamState()
        if not stream:
            try:
                result = await chatmod.aggregate(chained(), model=wrapper_model,
                                                 state=state)
            finally:
                await response.aclose()
            return PlatformChat(payload=result, usage=state.usage,
                                model=wrapper_model,
                                account_hint=str(account.get('uid') or ''),
                                error=state.error)

        generator = chatmod.translate(chained(), model=wrapper_model, state=state)

        async def closer() -> None:
            try:
                await generator.aclose()  # type: ignore[attr-defined]
            finally:
                await response.aclose()

        return PlatformChat(stream=generator, usage=state.usage, model=wrapper_model,
                            closer=closer, account_hint=str(account.get('uid') or ''))


adapter = TraeAdapter()
