"""豆包（www.doubao.com）平台适配器。

与 Loomy / DuMate / Discovery 同构：本文件只负责**策略**，协议在 `client.py`
与 `chat.py`。

  · 选号（权重 SWRR + LRU + 会话粘性）与单账号并发闸门；
  · Cookie 探活（借额度接口，见 `client.probe`）；
  · 模型目录（**内置快照**：豆包没有公开的模型列表接口）；
  · 流式（OpenAI chunk）/ 非流式两条出口的最后一步。

豆包是**单区域**平台（`realm_aware=False`），账号与模型统一 `realm='cn'`。

## 与参考实现的取舍

参考实现（`yyydsz/doubao2api`）维护「会话 id 复用」：`SSE_ACK` 里的
`conversation_id` 会被记下来，下一轮带回去让上游接着同一段对话。这里**不做**：

  · 会话粘性是「同一段对话钉到同一个账号」（prompt cache 亲和），不是复用
    上游会话对象；复用上游会话会让「客户端重发全文」与「上游已有历史」叠在
    一起，上下文翻倍；
  · 上游会话一旦被外部删除 / 过期，带旧 id 的请求会静默失败，排查成本很高。

所以每轮都发「压平后的历史 + 最后一条 user」（见 `chat.flatten_messages`），
由客户端自己掌握上下文，行为与 OpenAI 兼容接口一致。

## 上游不返回 usage

豆包 SSE 里没有 token 计数。`chat.StreamTranslator` 按「字符 ÷ 1.7」估算，
统计页上的数字是**估算值**，不是上游账单。这一点在 `constants.CHARS_PER_TOKEN`
与模块注释里都写明，避免有人拿它对账。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncIterator, Callable

from ...services import credits as creditsvc
from ...services.session_affinity import SessionAffinity, affinity_key
from .. import settings as runtime_settings
from ..base import ChatContext, PlatformAdapter, PlatformChat, PlatformError
from ..limiter import AccountLimiter
from . import catalog, chat as chatproto, client, constants as C, store

logger = logging.getLogger('workbuddy.platforms.doubao')

# 额度耗尽 / Cookie 失效的冷却口径：
#   · 401 是 Cookie 失效，冻了也不会自己好——只换号，不冷却；
#   · 402 没有对应上游码，豆包用 429 + 文案表达「额度用完」，这里沿用
#     Discovery 的口径冻 1 小时：额度不会在几十秒内回来。
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


def _prompt_chars(body: dict) -> int:
    """请求体里最终发给上游的提示文本长度（用于估算 prompt tokens）。"""
    try:
        blocks = body['messages'][0]['content_block']
        text = blocks[0]['content']['text_block']['text']
    except (KeyError, IndexError, TypeError):
        return 0
    return len(text) if isinstance(text, str) else 0


class DoubaoAdapter(PlatformAdapter):
    name = 'doubao'
    label = '豆包（Doubao）'
    # 每日签到（`POST /alice/ug/campaign/landing/checkin`）。
    supports_checkin = True
    # 扫码登录需要 Passport 三层签名，社区实现也不稳（参考仓 README 明说）。
    # 这里只支持抓包导入 Cookie，不提供一个必然失败的登录入口。
    supports_web_login = False
    # 凭证就是已登录 www.doubao.com 的整段网页 Cookie，面板允许直接粘贴。
    supports_cookie_import = True
    # 单区域平台。
    realm_aware = False

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_used: dict[str, float] = {}
        # uid -> (过期时刻, 探活结果)：探活结果只缓存在内存，落盘的是摘要。
        self._probes: dict[str, tuple[float, dict]] = {}
        # uid -> (过期时刻, credits 结果)
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
        from ... import config  # noqa: PLC0415 —— 与其它平台同一取法
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
        """导入账号：粘贴网页 Cookie（可选设备字段）。

        接受 `{account: {...}}` 与扁平对象两种形状；`cookie` / `cookies` /
        `token` 都按 Cookie 处理。导入阶段**不联网**，探活在 `refresh_account`
        或第一次对话时发生。
        """
        if not isinstance(payload, dict):
            raise PlatformError('账号必须是 JSON 对象', status=400,
                                code='invalid_account')
        raw = payload.get('account') if isinstance(payload.get('account'), dict) \
            else payload

        cookie = store.normalize_cookie(
            _pick(raw, 'cookie', 'cookies', 'token', 'session'))
        if not cookie:
            raise PlatformError('需要提供豆包网页 Cookie', status=400,
                                code='invalid_account')
        if not store.has_login(cookie):
            raise PlatformError(
                'Cookie 里没有登录态字段（需要 sessionid / sid_tt / ttwid 之一），'
                '请从已登录的 www.doubao.com 复制完整 Cookie',
                status=400, code='invalid_account')

        try:
            uid = store.derive_uid(cookie, _pick(raw, 'uid'))
        except ValueError as exc:
            raise PlatformError(str(exc), status=400,
                                code='invalid_account') from exc

        old = store.read_account(uid) or {}
        account = {
            **old,
            'uid': uid,
            'cookie': cookie,
            'deviceId': _pick(raw, 'deviceId', 'device_id', 'device')
            or str(old.get('deviceId') or ''),
            'webId': _pick(raw, 'webId', 'web_id')
            or str(old.get('webId') or ''),
            'localAppId': _pick(raw, 'localAppId', 'local_app_id', 'appId')
            or str(old.get('localAppId') or C.DEFAULT_LOCAL_APP_ID),
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
        return store.public_account(store.save_account(account))

    def delete_account(self, account_id: str) -> bool:
        uid = str(account_id or '')
        self._probes.pop(uid, None)
        self._credits.pop(uid, None)
        return store.delete_account(account_id)

    async def refresh_account(self, account_id: str, *,
                              force: bool = False) -> dict:
        """Cookie 探活（不是续期）：豆包没有 refresh token。"""
        account = self._require(account_id)
        uid = str(account.get('uid') or '')
        if not self._cookie_of(account):
            raise PlatformError('豆包账号缺少 Cookie', status=401,
                                code='login_expired')
        out = await client.probe(
            account, proxy=self._proxy_for(account),
            timeout=runtime_settings.credits_timeout(self.name))
        state = str(out.get('state') or 'unknown')
        patch: dict[str, Any] = {
            'refreshedAt': store.iso_from_epoch(time.time()),
        }
        if state == 'invalid':
            patch['loginState'] = '登录已失效'
            patch['lastRefreshError'] = str(out.get('message') or 'Cookie 无效')[:200]
        elif state == 'ok':
            patch['loginState'] = '正常'
            patch['lastRefreshError'] = ''
        else:
            patch['lastRefreshError'] = str(out.get('message') or '')[:200]
        saved = store.patch_account(uid, patch)
        self._probes[uid] = (time.time() + runtime_settings.credits_ttl(self.name),
                             out)
        if state == 'invalid':
            raise PlatformError(str(out.get('message') or 'Cookie 无效'),
                                status=401, code='login_expired')
        return store.public_account(saved)

    # ── 任务 ──────────────────────────────────────────────
    async def checkin(self, account_id: str) -> dict:
        """每日签到。上游按自然日幂等，重复调用不会多发。"""
        account = self._require(account_id)
        uid = str(account.get('uid') or '')
        out = await client.checkin(
            account, proxy=self._proxy_for(account),
            timeout=runtime_settings.credits_timeout(self.name))
        ok = bool(out.get('ok'))
        already = bool(out.get('already'))
        reward = int(out.get('reward_amount') or 0)
        message = str(out.get('message') or '')
        # 记账口径：上游系统错误（710010702）与「没有活动」要分开，别都写成
        # error——前者是「活动在、这次没放行」，后者是账号本来就没这个任务。
        result_code = str(out.get('code') or '')
        if already:
            last_result = 'already'
        elif ok:
            last_result = 'ok'
        elif result_code in ('upstream_error', 'no_campaign'):
            last_result = result_code
        else:
            last_result = 'error'

        try:
            store.patch_account(uid, {
                'checkin': {
                    'last_at': int(time.time()),
                    'last_result': last_result,
                    'continuous_days': out.get('days'),
                    'reward': out.get('reward'),
                },
                'loginState': '正常' if ok or already else str(
                    account.get('loginState') or ''),
            })
        except (FileNotFoundError, ValueError, OSError):
            pass

        return {
            'ok': ok,
            'checked_in': ok,
            'already': already,
            'credits': 0,
            # 收入用上游给的奖励数额：没有余额差值可算，`reward_amount` 就是
            # 这次实际到账（幂等命中时是 0）。
            'earned_credit': 0 if already else reward,
            'message': message or ('今日已签到' if already else
                                   ('签到成功' if ok else '签到失败')),
            'uid': uid,
            'realm': store.REALM,
            'reward': out.get('reward') or '',
            'continuous_days': out.get('days'),
            # 上游拒绝时带回的只读面板状态（今日是否已签 / 连签 / 待领额度），
            # 排查时不用再去抓一次包。
            'upstream_code': out.get('upstream_code'),
            'status': out.get('status') or None,
        }

    async def usage(self, account_id: str) -> dict:
        """额度 / 用量快照（额度汇总接口）。"""
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
            data = await client.quota(
                account, proxy=self._proxy_for(account),
                timeout=runtime_settings.credits_timeout(self.name))
        except PlatformError as exc:
            out['errors'].append({'endpoint': 'quota', 'code': exc.code,
                                  'message': exc.message})
            return out
        out['session_usage'] = {
            'total': data['total'],
            'used': data['used'],
            'remaining': data['remaining'],
            'percent': data['percent'],
            'groups': data['groups'],
            'at': int(time.time()),
        }
        out['entitlement'] = {
            'quota_known': True,
            'total': data['total'],
            'used': data['used'],
            'remaining': data['remaining'],
            'percent': data['percent'],
        }
        return out

    async def credits(self, account_id: str) -> dict:
        """额度余额快照（默认 60 秒缓存，`platforms.doubao.credits_ttl`）。"""
        account = self._require(account_id)
        uid = str(account.get('uid') or '')
        cached = self._credits.get(uid)
        if cached and cached[0] > time.time():
            return cached[1]

        data = await client.quota(
            account, proxy=self._proxy_for(account),
            timeout=runtime_settings.credits_timeout(self.name))
        # 到期明细：每个额度窗口的 end_time 就是一个到期时刻。0 / 过去的时间
        # 不放进 `expires`——那是「已到期」而不是「将到期」，排不进倒计时。
        now = time.time()
        expiries: list[dict] = []
        for group in data.get('groups') or []:
            end = int(group.get('end_time') or 0)
            amount = float(group.get('total') or 0) - float(group.get('used') or 0)
            if end <= now or amount <= 0:
                continue
            expiries.append({'amount': round(amount, 2), 'expire_at': end,
                             'label': str(group.get('name') or '额度')[:120]})
        expiries.sort(key=lambda item: item['expire_at'])
        out = {
            'uid': uid,
            'realm': store.REALM,
            'credits': data['remaining'],
            'remain': data['remaining'],
            'limit': data['total'],
            'used': data['used'],
            'unit': 'credits',
            'expires': expiries,
            'next_expire_at': expiries[0]['expire_at'] if expiries else None,
            'source': 'quota/summary',
        }
        self._credits[uid] = (time.time()
                              + runtime_settings.credits_ttl(self.name), out)
        try:
            store.patch_account(uid, {'quota': {
                'state': 'ok', 'at': int(time.time()), 'message': '',
                'total': data['total'], 'used': data['used'],
                'remaining': data['remaining'],
            }})
        except (FileNotFoundError, ValueError, OSError):
            pass
        return out

    # ── 模型目录 ──────────────────────────────────────────
    def _cached_models(self) -> list[dict]:
        """内置目录（豆包没有在线目录接口，见 `catalog` 模块注释）。"""
        return catalog.default_models()

    async def list_models(self, *, refresh: bool = False) -> list[dict]:
        models = self._cached_models()
        self._model_meta = {str(m.get('id') or '').strip().lower(): m
                            for m in models if m.get('id')}
        return [self._public_model(m) for m in models if m.get('id')]

    def _public_model(self, raw: dict) -> dict:
        model_id = str(raw.get('id') or '')
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
            'modalities': {'input': ['text']},
            'model_name': model_id,
            # 豆包只吃文本：附件 / 图片本轮不支持，界面据此隐藏上传入口。
            'tool_call': False,
            'attachment': False,
            'credit_ratio': None,
        }

    def cached_model_ids(self) -> set[str] | None:
        """内置目录的对外模型 id（`doubao/...`），只读不联网。"""
        models = self._cached_models()
        if not models:
            return None
        return {self.public_model_id(str(m.get('id') or ''), store.REALM)
                for m in models if m.get('id')}

    def upstream_model(self, model: str) -> str:
        """成员名 → 上游 `model_item_key`（空名补默认模型）。"""
        return C.upstream_model_key(model)

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

        entry = C.model_entry(model)
        effort = body.get('reasoning_effort') if isinstance(body, dict) else None

        candidates = self._candidates(affinity)
        if not candidates:
            raise PlatformError('没有可用的豆包账号（请先导入网页 Cookie）',
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
                        f'豆包账号并发已满（单账号上限 '
                        f'{self._limiter.limit()}）', status=429,
                        code='account_busy')
                    continue
                release = _lease_releaser(self._limiter, lease)
                result = await self._chat_once(
                    account, model, body, wrapper_model, entry,
                    proxy=proxy, runtime=runtime, effort=effort)
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
        raise last_error or PlatformError('豆包账号均不可用', status=503,
                                          code='no_account')

    async def _chat_once(self, account: dict, model: str, body: dict,
                         wrapper_model: str, entry: dict, *, proxy: str | None,
                         runtime: dict, effort: object) -> PlatformChat:
        uid = str(account.get('uid') or '')
        if not self._cookie_of(account):
            raise PlatformError('豆包账号缺少 Cookie', status=401,
                                code='login_expired')

        messages = body.get('messages') if isinstance(body, dict) else None
        try:
            payload = chatproto.build_body(
                messages, self.upstream_model(model), entry,
                account=account, effort=effort)
        except PlatformError:
            raise
        except ValueError as exc:
            raise PlatformError(str(exc), status=400,
                                code='bad_request') from exc

        response = await client.open_chat(
            account, payload, proxy=proxy,
            idle_timeout=runtime['idle_timeout'])
        translator = chatproto.StreamTranslator(
            wrapper_model, {}, prompt_chars=_prompt_chars(payload))

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
                async for event, data in client.iter_events(response):
                    translator.feed(event, data)
            except chatproto.StreamError as exc:
                raise PlatformError(exc.message, status=502,
                                    code=exc.code) from exc
            finally:
                await _close()
            if not translator.text.strip():
                raise PlatformError('豆包没有返回内容', status=502,
                                    code='empty_response')
            completion = translator.completion()
            usage = completion.get('usage') if isinstance(
                completion.get('usage'), dict) else {}
            return PlatformChat(payload=completion, usage=usage,
                                model=wrapper_model, account_hint=uid,
                                realm=store.REALM)

        # ── 流式：事件 → OpenAI chunk ──
        chat = PlatformChat(usage=translator.usage, model=wrapper_model,
                            account_hint=uid, realm=store.REALM)

        async def relay() -> AsyncIterator[dict]:
            finished = False
            failure: PlatformError | None = None
            try:
                for chunk in translator.start():
                    yield chunk
                async for event, data in client.iter_events(response):
                    for chunk in translator.feed(event, data):
                        yield chunk
                finished = True
            except chatproto.StreamError as exc:
                chat.error = exc.message
                failure = PlatformError(exc.message, status=502,
                                        code=exc.code)
            except PlatformError as exc:
                chat.error = exc.message
                failure = exc
            finally:
                await _close()
            # 错误在收尾之后**继续抛**：`platforms/sse.openai_sse` 会把它翻成
            # 一帧 error 并让网关记 502。只设 `chat.error` 不抛，客户端会看到
            # 一条正常结束的空流，日志里也还是 200。
            if failure is not None:
                raise failure
            if not finished:
                return
            # 空产出也要让客户端看到「没有内容」，而不是一个只有
            # finish_reason 的流（与参考实现同一兜底）。
            if not translator.text.strip() and not translator.reasoning:
                yield translator.text_chunk('(空响应)')
            for chunk in translator.finish():
                yield chunk

        chat.stream = relay()
        return chat


adapter = DoubaoAdapter()
