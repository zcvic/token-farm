"""Cline（cline.bot）平台适配器。

与 Loomy / DuMate / Kilo 同构：本文件只负责**策略**，协议在 `client.py`。

  · 选号（权重 SWRR + LRU + 会话粘性）与单账号并发闸门；
  · refresh token 轮换（**先落盘再使用**，账号级锁串行）；
  · 模型目录缓存（两个免鉴权接口合并 → 内置免费档兜底）；
  · 流式（原始字节透传）/ 非流式两条出口的最后一步。

Cline 是**单区域**平台（`realm_aware=False`），账号与模型统一 `realm='cn'`。

两处协议特性直接决定了这里的写法：

  · **免费档只认流式**：`deepseek/` / `cline-free/` / `cline-pass/` 前缀在
    非流式请求上会被上游回 500 `empty response content`。所以客户端要非流式时，
    适配器仍按流式发、再把 SSE 聚合成 `chat.completion`。
  · **限流会被伪装成正常响应**：HTTP 200 的 SSE 里只有 reasoning、正文为空，
    或正文写着 `Daily free limit` / `INFERENCE_CAP_ERROR`。这类响应必须判成
    限流（换号 + 冷却），不能当成功回复交给用户。

没有余额接口：`credits` 沿用基类的「不支持」，余额不足由 402 表达。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, AsyncIterator, Callable

from ... import db
from ...services.session_affinity import SessionAffinity, affinity_key
from .. import settings as runtime_settings
from ..base import ChatContext, PlatformAdapter, PlatformChat, PlatformError
from ..limiter import AccountLimiter
from . import catalog, client, constants as C, store
from .availability import ModelHealth

logger = logging.getLogger('workbuddy.platforms.cline')

SETTING_MODELS = 'platforms.cline.models'
SETTING_MODELS_ATTEMPT = 'platforms.cline.models_attempt'

# 余额不足（402）的冷却时长：免费档不受影响，冻 10 分钟足够让批量任务改投
# 别的账号，也不至于把「充值后立刻恢复」的号冻太久。
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

    除了标准的 `{"error": ...}` 帧，这里还认「伪装成正常帧的限流」：正文里
    出现 `empty response content` / `Daily free limit` / `INFERENCE_CAP_ERROR`
    时同样判成错误，交给上层换号。
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
                body = delta.get('content')
                if isinstance(body, str) and body:
                    saw_content = True
                    if client.limit_signal(body):
                        error = body.strip()[:200]
                if delta.get('reasoning_content'):
                    saw_content = True
    return pending, saw_content, error


async def _collect_sse(response) -> list[object]:
    """把上游 SSE 读成事件对象列表（非流式请求被强制走流式时用）。"""
    events: list[object] = []
    buffer: list[str] = []
    async for line in response.aiter_lines():
        if line == '':
            if not buffer:
                continue
            text = '\n'.join(buffer).strip()
            buffer = []
            if not text or text == '[DONE]':
                continue
            try:
                events.append(json.loads(text))
            except ValueError:
                continue
            continue
        if line.startswith('data:'):
            buffer.append(line[5:].strip())
        elif line.startswith('event:') or line.startswith(':'):
            continue
        elif line.strip():
            buffer.append(line.strip())
    if buffer:
        text = '\n'.join(buffer).strip()
        if text and text != '[DONE]':
            try:
                events.append(json.loads(text))
            except ValueError:
                pass
    return events


class ClineAdapter(PlatformAdapter):
    name = 'cline'
    label = 'Cline'
    # 上游没有签到。
    supports_checkin = False
    # WorkOS 设备授权码登录。
    supports_web_login = True
    # 单区域平台。
    realm_aware = False

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_used: dict[str, float] = {}
        # uid -> (access token 到期时刻, access token)。只缓存 access，
        # refresh token 永远以磁盘为准（一次性轮换，内存副本会过期）。
        self._tokens: dict[str, tuple[float, str]] = {}
        self._limiter = AccountLimiter(self.name)
        self._swrr: dict[str, float] = {}
        self._affinity = SessionAffinity()
        self._model_meta: dict[str, dict] = {}
        self._health = ModelHealth()

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

    def _session_timeout(self) -> float:
        try:
            return float(max(8, min(30, runtime_settings.request_timeout(self.name))))
        except Exception:  # noqa: BLE001
            return C.REFRESH_TIMEOUT

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
            if cached and not force \
                    and cached[0] - C.TOKEN_SKEW_SECONDS > time.time():
                return cached[1]

            fresh = store.read_account(uid) or account
            access = self._access_of(fresh)
            expires = store.epoch_from_iso(fresh.get('expiredAt'))
            refresh = self._refresh_of(fresh)

            if access and not force and expires \
                    and expires - C.TOKEN_SKEW_SECONDS > time.time():
                self._tokens[uid] = (expires, access)
                return access

            if not refresh:
                if access and not force:
                    # 没有 refresh token（手工粘贴的 access）：先用一次，401 再换号。
                    self._tokens[uid] = (time.time() + 60, access)
                    return access
                self._mark_expired(uid, 'refresh token 缺失')
                raise PlatformError('Cline 账号缺少 refresh token，请重新登录',
                                    status=401, code='login_expired')

            try:
                data = await client.refresh_token(
                    refresh, proxy=proxy, timeout=self._session_timeout())
            except PlatformError as exc:
                # `/auth/refresh` 被拒 ≠ 账号真失效：refresh token 本质是
                # WorkOS token，那条封装回 400 时 WorkOS 通道往往仍可自愈，
                # 用户不必重新走设备授权。只有 WorkOS 也回 invalid_grant
                # （401）才标失效。
                healed = await self._heal_via_workos(uid, refresh, proxy, exc)
                if healed is not None:
                    return healed
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
                or time.time() + C.TOKEN_FALLBACK_SECONDS
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
                raise PlatformError(f'保存 Cline 新 token 失败：{exc}',
                                    status=500, code='store_error') from exc
            self._tokens[uid] = (new_expires, new_access)
            return new_access

    async def _heal_via_workos(self, uid: str, refresh: str,
                              proxy: str | None,
                              cause: PlatformError) -> str | None:
        """`/auth/refresh` 失败后试一次 WorkOS 通道，成功返回新 access token。

        两步：WorkOS `authenticate`（grant_type=refresh_token）换出新的一对
        token → 回 `/auth/register` 换成 Cline 正式令牌。注册这步失败不致命：
        Cline 上游也认 `workos:` 前缀的 WorkOS access token，直接用它顶上
        （请求头本来就带 `workos:` 前缀）。

        只有 WorkOS 也明确 `invalid_grant` 才返回 None（交给调用方标失效）；
        网络抖动 / 5xx 同样返回 None，但保留原错误，账号只冷一会儿。
        """
        if not refresh:
            return None
        # 只有「上游明确拒绝」才值得走 WorkOS 通道；5xx / 网络错误是抖动，
        # 再打一次别的域名只是把故障放大。
        if cause.status not in (400, 401, 403):
            return None
        try:
            wos = await client.refresh_via_workos(
                refresh, proxy=proxy, timeout=self._session_timeout())
        except PlatformError:
            return None
        new_refresh = str(wos.get('refresh_token') or '') or refresh
        access = str(wos.get('access_token') or '')
        expires = 0.0
        try:
            registered = await client.register(
                wos, proxy=proxy, timeout=self._session_timeout())
        except PlatformError:
            registered = {}
        else:
            access = str(registered.get('access_token') or '') or access
            new_refresh = str(registered.get('refresh_token') or '') or new_refresh
            expires = float(registered.get('expires_at') or 0)
        if not access:
            return None
        if not expires:
            # WorkOS access token 实测 1 小时；留 60 秒余量，到点再刷。
            expires = time.time() + 3600 - 60
        try:
            store.patch_account(uid, {
                'token': access,
                'refreshToken': new_refresh,
                'expiredAt': store.iso_from_epoch(expires),
                'refreshedAt': store.iso_from_epoch(time.time()),
                'lastRefreshError': '',
                'loginState': '正常',
            })
        except (FileNotFoundError, ValueError, OSError) as exc:
            raise PlatformError(f'保存 Cline 新 token 失败：{exc}',
                                status=500, code='store_error') from exc
        self._tokens[uid] = (expires, access)
        logger.info('cline 账号 %s 经 WorkOS 通道自愈（原错误：%s）',
                    uid, str(cause.message)[:120])
        return access

    def _drop_token(self, uid: str) -> None:
        self._tokens.pop(str(uid or ''), None)

    # ── 账号 ──────────────────────────────────────────────
    def list_accounts(self) -> list[dict]:
        return [store.public_account(a) for a in store.list_accounts()]

    def import_account(self, payload: dict) -> dict:
        """导入账号：粘贴 Cline 的 refresh token（或设备码登录的返回值）。

        接受 `{account: {...}}` 与扁平对象两种形状；`refresh_token` /
        `refreshToken` / `token` / `key` 都按 refresh token 处理。导入阶段
        **不联网**，首次刷新在 `refresh_account` 或第一次对话时发生。
        """
        if not isinstance(payload, dict):
            raise PlatformError('账号必须是 JSON 对象', status=400,
                                code='invalid_account')
        raw = payload.get('account') if isinstance(payload.get('account'), dict) \
            else payload

        refresh = store.normalize_refresh(
            _pick(raw, 'refreshToken', 'refresh_token', 'refresh', 'token',
                  'key', 'CLINE_REFRESH_TOKEN'))
        if not refresh:
            raise PlatformError('需要提供 Cline refreshToken', status=400,
                                code='invalid_account')
        try:
            uid = _pick(raw, 'uid') or store.derive_uid(refresh)
            uid = store.safe_id(uid)
        except ValueError as exc:
            raise PlatformError(str(exc), status=400,
                                code='invalid_account') from exc

        old = store.read_account(uid) or {}
        access = _pick(raw, 'accessToken', 'access_token')
        if not access and str(old.get('refreshToken') or '') == refresh:
            access = str(old.get('token') or '')
        expires = store.epoch_from_iso(
            _pick(raw, 'expiresAt', 'expiredAt', 'expires_at'))
        if not expires:
            expires_in = _num(raw.get('expires_in'))
            if expires_in and expires_in > 0:
                expires = time.time() + expires_in
        if not expires and access:
            # 上游不给到期时间时按 10 分钟兜底：适配器会用余量自己换。
            expires = time.time() + C.TOKEN_FALLBACK_SECONDS

        account = {
            **old,
            'uid': uid,
            'refreshToken': refresh,
            'token': access,
            'expiredAt': store.iso_from_epoch(expires) if expires else '',
            'email': _pick(raw, 'email') or str(old.get('email') or ''),
            'name': _pick(raw, 'name') or str(old.get('name') or ''),
            'nickname': _pick(raw, 'nickname', 'displayName')
            or str(old.get('nickname') or ''),
            'proxy': _pick(raw, 'proxy', 'egress') or str(old.get('proxy') or ''),
            'proxy_session': _pick(raw, 'proxy_session', 'proxySession')
            or str(old.get('proxy_session') or ''),
            'source': _pick(raw, 'source') or str(old.get('source') or 'manual'),
            'lastRefreshError': '',
            'loginState': '正常',
        }
        self._drop_token(uid)
        return store.public_account(store.save_account(account))

    def delete_account(self, account_id: str) -> bool:
        self._drop_token(str(account_id or ''))
        return store.delete_account(account_id)

    async def refresh_account(self, account_id: str, *,
                              force: bool = False) -> dict:
        """轮换一次 token（顺便验证凭证）；没有余额接口可查。"""
        account = self._require(account_id)
        uid = str(account.get('uid') or '')
        proxy = self._proxy_for(account)
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
        return store.public_account(store.patch_account(uid, {
            'refreshedAt': store.iso_from_epoch(time.time()),
            'lastRefreshError': '',
            'loginState': '正常',
        }))

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
        raise PlatformError('Cline 没有签到', status=400,
                            code='checkin_unsupported')

    async def usage(self, account_id: str) -> dict:
        """额度 / 用量快照：Cline 没有余额接口，只能验证凭证是否可用。"""
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
            await self._token_for(account, proxy=self._proxy_for(account))
        except PlatformError as exc:
            out['errors'].append({'endpoint': 'auth/refresh',
                                  'code': exc.code, 'message': exc.message})
            return out
        out['session_usage'] = {
            'checked_at': int(time.time()),
            'message': 'Cline 不提供余额接口；免费额度用尽时对话会返回 429',
        }
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
            models = await self._live_models(refresh=refresh)
            self._note_implicit_attempt()
            if models:
                self._save_models(models)
        # `not models` 而不是 `is None`：`_live_models` 在**两个接口都失败**时按
        # 设计返回空表（它的 docstring 就写着「调用方降级到内置免费档」），只判
        # None 会让那条兜底永远走不到 —— 现场表现是模型页整片空白，而日志里只有
        # 两条抓取失败，看不出「本该有内置免费档」。
        if not models:
            models = catalog.default_models()
        # 按被动可用性信号排序：可用置顶、未知居中、失败沉底。稳定排序，
        # 没有信号时顺序与目录原序完全一致（不会让下拉莫名跳动）。
        models = self._health.sort(list(models))
        self._model_meta = {str(m.get('id') or '').strip().lower(): m
                            for m in models if m.get('id')}
        return [self._public_model(m) for m in models if m.get('id')]

    async def _live_models(self, *, refresh: bool = False) -> list[dict]:
        """目录**免鉴权**可读：没有账号也照样能列模型。

        两个接口各补一块，任一失败都只用另一半；都失败时返回空表（调用方降级
        到内置免费档），不抛——一次目录刷新失败不该让模型页整片空白。
        """
        timeout = 45 if refresh else runtime_settings.catalog_timeout(self.name)
        proxy: str | None = None
        for account in store.list_accounts():
            if store.is_disabled(account):
                continue
            try:
                proxy = self._proxy_for(account)
            except PlatformError:
                proxy = None
            break
        free: list[dict] = []
        live: list[dict] = []
        try:
            free = await client.fetch_recommended(proxy=proxy, timeout=timeout)
        except PlatformError:
            free = []
        try:
            live = await client.fetch_models(proxy=proxy, timeout=timeout)
        except PlatformError:
            live = []
        return catalog.merge(free, live)

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
        """已缓存的对外模型 id（`cline/...`），只读不联网。"""
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
                    and self._refresh_of(a)
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
            seconds = (exc.retry_after
                       or runtime_settings.cooldown_429(self.name) or 60)
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
            raise PlatformError('没有可用的 Cline 账号（请先登录或粘贴 refreshToken）',
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
                        f'Cline 账号并发已满（单账号上限 '
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
                # 被动可用性信号：只记模型级失败（401/402/403 是账号或余额
                # 问题，记进来会把整个池子拖下水）。
                self._health.record_fail(self.upstream_model(model), exc.status,
                                         exc.message)
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
                # 非流式到这里已经拿到完整响应，可以判定这个模型可用；
                # 流式的成败要等 relay 走完才知道，见下面的 finally。
                self._health.record_ok(self.upstream_model(model))
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
        raise last_error or PlatformError('Cline 账号均不可用', status=503,
                                          code='no_account')

    async def _chat_once(self, account: dict, model: str, body: dict,
                         wrapper_model: str, *, proxy: str | None,
                         runtime: dict, affinity: str = '',
                         force_token: bool = False) -> PlatformChat:
        uid = str(account.get('uid') or '')
        access = await self._token_for(account, proxy=proxy, force=force_token)
        upstream = self.upstream_model(model)
        # 会话 id 用亲和键的稳定派生值：同一段对话落在同一个 session，
        # 又不会把原始 prompt 哈希直接塞进上游请求体。
        session_id = (f'sess_{uuid.uuid5(uuid.NAMESPACE_URL, affinity).hex[:16]}'
                      if affinity else f'sess_{uuid.uuid4().hex[:16]}')
        payload = client.chat_body(self.chat_body(model, body),
                                   session_id=session_id)
        # Meta 后端硬限 64 字符工具名：超长请求会间歇性 400（取决于轮询到的
        # 后端）。入口处一次性截短并记下映射，回程再还原成客户端原始名。
        tool_names = client.clamp_tool_names(payload)

        if not payload.get('stream'):
            if client.needs_force_stream(upstream):
                # 这些前缀只认流式：按流式发，再把 SSE 聚合成非流式响应。
                payload['stream'] = True
                response = await client.open_chat(
                    access, payload, proxy=proxy,
                    idle_timeout=runtime['idle_timeout'],
                    session_id=session_id)
                try:
                    events = await _collect_sse(response)
                finally:
                    try:
                        await response.aclose()
                    except Exception:  # noqa: BLE001
                        pass
                data = client.sse_to_completion(events, wrapper_model)
                client.restore_tool_names(data, tool_names)
                usage = data.get('usage') if isinstance(data.get('usage'),
                                                        dict) else {}
                return PlatformChat(payload=data, usage=usage,
                                    model=wrapper_model, account_hint=uid,
                                    realm=store.REALM)
            data = await client.chat(access, payload, proxy=proxy,
                                     timeout=runtime['request_timeout'],
                                     session_id=session_id)
            client.restore_tool_names(data, tool_names)
            usage = data.get('usage') if isinstance(data.get('usage'), dict) else {}
            return PlatformChat(payload=data, usage=usage, model=wrapper_model,
                                account_hint=uid, realm=store.REALM)

        started = time.monotonic()
        response = await client.open_chat(
            access, payload, proxy=proxy,
            idle_timeout=runtime['idle_timeout'], session_id=session_id)
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
            rewriter = client.SseToolNameRewriter(tool_names)
            try:
                async for chunk in response.aiter_bytes():
                    if not chunk:
                        continue
                    # 工具名还原只影响含超长名的帧；没有映射时原样返回同一块。
                    out = rewriter.feed(chunk)
                    if out:
                        yield out
                    pending, saw_content, error = _scan_sse(pending + chunk, usage)
                    if error:
                        state['error'] = error
                    if saw_content and chat.first_token_ms is None:
                        chat.first_token_ms = int(
                            (time.monotonic() - started) * 1000)
                tail = rewriter.flush()
                if tail:
                    yield tail
            finally:
                if state['error']:
                    chat.error = state['error']
                # 流式要等走完才知道结果：中途报错（含伪装成正常帧的限流）
                # 记一次模型失败，正常收尾记一次成功。
                if state['error']:
                    self._health.record_unavailable(upstream, state['error'])
                elif chat.first_token_ms is not None:
                    self._health.record_ok(upstream)
                await _close()

        chat.raw_stream = relay()
        return chat


adapter = ClineAdapter()
