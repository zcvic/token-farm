"""Qoder 平台适配器：把内置网关（qoder2api-hub）包装成 token-farm 的平台能力。

## 为什么内置

Qoder 的上游协议包含 COSY 签名、设备指纹、OAuth 设备授权、模型目录与签到
风控，`qoder2api-hub` 已经把它们实现成一个完整的 Python 网关。token-farm
把那份代码**内置**在 `_vendor/`，由 `embedded.py` 在回环地址上拉起，本适配器
只做：账号管理、模型前缀、realm 口径、SSE 透传与记账。

## 与 Trae 的差异

  · 账号池在 `_vendor/qoder_accounts.py`（内置网关）里，这里通过回环 HTTP
    调它的 `/accounts*`，不直接持有凭据；
  · 有账号权重（`/accounts/weight`），但没有强制冷却 API：
    `set_account_cooling` 沿用基类的「明确不支持」，前端按能力开关隐藏按钮；
  · 网页登录是**设备授权轮询**，没有本机回环回调，所以不实现
    `login_submit_callback`（前端据此隐藏回调输入框）。

## 模型名

对外按区域加前缀：`qodercn/<官方模型名>`（国内版）/ `qodercom/<官方模型名>`
（国际版）。`/v1/models` 对双区同名模型按区域各列一条；单区模型只列所属区域。
旧式 `qoder/<官方模型名>` 继续兼容（不带区域提示，由内置网关自动选区）；
需要显式指定区域时也可以写成 `qoder/intl:<模型>` 或 `qoder/cn:<模型>`，
适配器在调用网关时剥掉前缀并选中对应区域。密钥绑定的国际版（`global`）也
作为区域提示，但优先级低于区域前缀。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from ..base import ChatContext, PlatformAdapter, PlatformChat, PlatformError
from ..limiter import PoolLimiter
from . import embedded, mapping
from .client import QoderClient

logger = logging.getLogger(__name__)

_MODELS_TTL = 300          # 模型目录缓存 5 分钟
_MODELS_FAIL_TTL = 30      # 目录拉取失败后短时间内不再重试（避免 /v1/models 干等）
_ACCOUNTS_TTL = 5          # 积分批量查询共享同一份账号快照
_ACCOUNTS_FAIL_TTL = 10    # 网关起不来时短暂记住失败，避免每次开页都等超时


def _split_realm_hint(model: str) -> tuple[str | None, str]:
    """`intl:Qwen3.8-Max` → `('intl', 'Qwen3.8-Max')`；无前缀返回 `(None, 原名)`。"""
    text = str(model or '').strip()
    for prefix, realm in (('intl:', 'intl'), ('global:', 'intl'), ('cn:', 'cn')):
        if text.lower().startswith(prefix):
            rest = text[len(prefix):].strip()
            if rest:
                return realm, rest
    return None, text


def _usage(raw: Any) -> dict:
    """把上游 usage 归一成 token-farm 的记账口径。

    Qoder 上游给的是**复数** `credits`（内置网关自己的 `_extract_usage` 也是这么
    处理的，见 `_vendor/qoder_proxy.py` 里 issue #22 的注释）。网关侧的
    `_usage_credit` 已经兜了复数写法，这里再补一份是为了让 `PlatformCall.usage`
    的读者（Responses / Anthropic 两条协议层）也拿到同一个键名，不必各自判断。
    """
    out = dict(raw) if isinstance(raw, dict) else {}
    if out.get('credit') is None and out.get('credits') is not None:
        out['credit'] = out['credits']
    return out


class QoderAdapter(PlatformAdapter):
    name = mapping.PLATFORM
    label = mapping.LABEL
    supports_checkin = True
    supports_web_login = True
    realm_aware = True
    ui_visible = True
    routable = True

    def __init__(self, client: QoderClient | None = None) -> None:
        self.client = client or QoderClient()
        self._limiter = PoolLimiter(self.name)
        self._models: list[dict] = []
        self._models_at = 0.0
        self._models_error: PlatformError | None = None
        self._models_error_at = 0.0
        self._accounts: list[dict] | None = None
        self._accounts_at = 0.0
        self._accounts_error: PlatformError | None = None
        self._accounts_error_at = 0.0

    # ── 账号快照 ────────────────────────────────────────────
    def has_accounts_hint(self) -> bool:
        """不启动内置网关的前提下，磁盘上是否有账号文件。

        定时签到 / 令牌续期在**每个**部署上都会跑；若这里直接走 `list_accounts()`
        就会把网关拉起来（回环服务 + 余额刷新线程）。账号就是一个 `<uid>.json`，
        数一下文件即可——`settings.json` / `active_realm.json` / 机器身份不是账号。
        """
        try:
            folder = embedded.accounts_dir()
            return any(
                p.is_file() and p.suffix.lower() == '.json'
                and p.name not in ('settings.json', 'active_realm.json',
                                   'machine_identity.json')
                for p in folder.iterdir()
            )
        except OSError:
            return False

    def _invalidate_accounts(self) -> None:
        self._accounts = None
        self._accounts_at = 0.0
        self._accounts_error = None
        self._accounts_error_at = 0.0

    def _accounts_snapshot(self, max_age: float = 0) -> list[dict]:
        """网关账号列表；`max_age` > 0 时允许复用短缓存（批量积分查询用）。

        失败也缓存 10 秒：`/api/platforms` 每次开页都会问一遍账号数，内置网关
        起不来（缺依赖 / 数据目录不可写）时若每次都去连，页面会被拖住。失败后
        直接抛同一句错误，网关恢复后最多 10 秒重新可用。
        """
        now = time.time()
        if self._accounts_error is not None \
                and now - self._accounts_error_at < _ACCOUNTS_FAIL_TTL:
            raise self._accounts_error
        if max_age > 0 and self._accounts is not None \
                and now - self._accounts_at <= max_age:
            return self._accounts
        try:
            data = self.client.list_accounts('all')
        except PlatformError as exc:
            self._accounts_error = exc
            self._accounts_error_at = time.time()
            raise
        self._accounts_error = None
        self._accounts_error_at = 0.0
        self._accounts = data
        self._accounts_at = time.time()
        return data

    def _require(self, account_id: str) -> dict:
        target = str(account_id or '')
        for account in self._accounts_snapshot():
            if str(account.get('uid') or '') == target:
                return account
        raise PlatformError('账号不存在', status=404, code='account_not_found')

    def _require_cached(self, account_id: str) -> dict:
        target = str(account_id or '')
        for account in self._accounts_snapshot(_ACCOUNTS_TTL):
            if str(account.get('uid') or '') == target:
                return account
        raise PlatformError('账号不存在', status=404, code='account_not_found')

    # ── 账号 ────────────────────────────────────────────────
    def list_accounts(self) -> list[dict]:
        return [mapping.public_account(a) for a in self._accounts_snapshot()]

    def import_account(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise PlatformError('账号必须是 JSON 对象', status=400,
                                code='invalid_account')
        raw = self.client.import_account(payload)
        self._invalidate_accounts()
        return mapping.public_account(raw)

    def import_from_desktop(self, *, realm: str | None = None) -> dict:
        """从本机 Qoder 客户端（桌面 App + CLI）一键导入。

        内置网关与面板在同一个进程，扫描的就是 token-farm 所在机器的客户端。
        一次导入双区全部有效凭据，前端按当前版本过滤展示。
        """
        try:
            data = self.client.import_desktop()
        except PlatformError as exc:
            # 上游把「某个客户端的凭据读失败」整体报成 400；这里翻译成前端的
            # failed 列表（和手工导入一致），而不是让整页弹一个红条。
            # 网关不可达（502）仍然抛出去，那是另一类问题。
            if exc.status != 400:
                raise
            return {
                'platform': self.name,
                'sources': [],
                'imported': [],
                'failed': [{'error': exc.message,
                            'code': exc.code or 'qoder_import_failed'}],
            }
        imported = [a for a in (data.get('imported') or []) if isinstance(a, dict)]
        self._invalidate_accounts()
        return {
            'platform': self.name,
            'sources': [],
            'imported': [mapping.public_account(a) for a in imported],
            'failed': [],
        }

    def delete_account(self, account_id: str) -> bool:
        removed = self.client.delete_account(str(account_id or ''))
        if removed:
            self._invalidate_accounts()
        return removed

    async def refresh_account(self, account_id: str, *, force: bool = False) -> dict:
        uid = str(account_id or '')
        raw = await asyncio.to_thread(self.client.refresh_account, uid)
        # 刷新 token 不会顺带更新余额快照；账号页的「刷新」按钮同时也是用户
        # 获取实时余额的入口，这里补一次积分强刷。失败不影响账号刷新结果。
        try:
            raw = await asyncio.to_thread(self.client.refresh_credits, uid)
        except Exception as exc:  # noqa: BLE001 —— 余额查询失败不该让换 token 失败
            logger.warning('qoder: credits refresh failed for %s: %s', uid, exc)
        self._invalidate_accounts()
        return mapping.public_account(raw)

    # ── 网页登录（设备授权轮询）──────────────────────────────
    def login_start(self, *, realm: str | None = None) -> dict:
        data = self.client.login_start(mapping.normalize_realm(realm))
        return {
            'login_id': str(data.get('state') or ''),
            'url': str(data.get('authUrl') or ''),
            'realm': mapping.normalize_realm(data.get('realm') or realm),
            'expires_in': 600,
        }

    def login_poll(self, login_id: str) -> dict:
        data = self.client.login_poll(str(login_id or ''))
        raw_status = str(data.get('status') or '').strip().lower()
        status = {
            'ok': 'success',
            'pending': 'pending',
            'expired': 'expired',
            'error': 'error',
            'unknown': 'error',
        }.get(raw_status, 'error')
        account = data.get('account')
        return {
            'login_id': str(login_id or ''),
            'status': status,
            'realm': mapping.normalize_realm(
                (account or {}).get('realm') if isinstance(account, dict) else None),
            'account': mapping.public_account(account)
            if isinstance(account, dict) else None,
            'message': str(data.get('message') or ''),
        }

    def login_cancel(self, login_id: str) -> bool:
        return bool(self.client.login_cancel(str(login_id or '')))

    # ── 任务 ────────────────────────────────────────────────
    async def checkin(self, account_id: str) -> dict:
        # 用短缓存：批量签到前刚读过账号列表，没必要每个号再拉一次网关。
        # 签到只改 credits / 签到状态，列表本身由前端重新加载，不用在这里失效。
        raw = await asyncio.to_thread(self._require_cached, str(account_id or ''))
        realm = mapping.normalize_realm(raw.get('realm'))
        result = await asyncio.to_thread(self.client.checkin,
                                         str(account_id or ''))
        return mapping.checkin_of(result, uid=str(account_id or ''), realm=realm)

    async def usage(self, account_id: str) -> dict:
        raw = await asyncio.to_thread(self._require_cached, str(account_id or ''))
        return mapping.usage_of(raw)

    async def credits(self, account_id: str) -> dict:
        raw = await asyncio.to_thread(self._require_cached, str(account_id or ''))
        return mapping.credits_of(raw)

    def set_account_options(self, account_id: str, *, weight=None) -> dict:
        """修改账号权重（Qoder 网关的 `/accounts/weight`）。

        Qoder 目前只支持这一个调度参数：并发上限由网关整池控制，不像 Trae
        有按账号的 in-flight 概念。`weight` 传 0 = 恢复默认 1.0。
        """
        if weight is None:
            raise PlatformError('没有要修改的项', status=400,
                                code='nothing_to_update')
        raw = self.client.set_weight(str(account_id or ''), weight)
        self._invalidate_accounts()
        if not isinstance(raw, dict) or not raw:
            raw = self._require(str(account_id or ''))
        return mapping.public_account(raw)

    def set_account_disabled(self, account_id: str, disabled: bool,
                             reason: str = '') -> dict:
        raw = self.client.set_enabled(str(account_id or ''), not bool(disabled))
        self._invalidate_accounts()
        if not isinstance(raw, dict) or not raw:
            # 网关只回 `{account}`；拿不到就重新读一次，别返回半截结构。
            raw = self._require(str(account_id or ''))
        return mapping.public_account(raw)

    # ── 模型 ────────────────────────────────────────────────
    def _cached_realms(self, model: str) -> list[str]:
        """该裸模型在缓存里可用的全部区域。

        双区同名模型在列表中是**按区域各一条**（`qodercn/x`、`qodercom/x`），
        所以这里要把同 `model_name` 的所有条目并起来，而不是只看第一条——
        否则会把双区模型误判成单区，自动推断时选错账号池。
        """
        target = str(model or '').strip().lower()
        out: list[str] = []
        for item in self._models:
            if str(item.get('model_name') or '').strip().lower() != target:
                continue
            realms = item.get('realms')
            if not isinstance(realms, list):
                continue
            for realm in realms:
                text = str(realm)
                if text and text not in out:
                    out.append(text)
        return out

    async def list_models(self, *, refresh: bool = False) -> list[dict]:
        now = time.time()
        if not refresh and self._models and now - self._models_at < _MODELS_TTL:
            return list(self._models)
        # 目录拉取失败后 30 秒内直接复用结论：`/v1/models` 与模型页都会调这里，
        # 网关起不来时不能每次都等超时。`refresh=True` 是用户显式重试，放行。
        if not refresh and self._models_error is not None \
                and now - self._models_error_at < _MODELS_FAIL_TTL:
            if self._models:
                return list(self._models)
            raise self._models_error
        try:
            raw = await asyncio.to_thread(self.client.list_models)
        except PlatformError as exc:
            self._models_error = exc
            self._models_error_at = time.time()
            if self._models:
                return list(self._models)
            raise
        self._models_error = None
        self._models_error_at = 0.0
        models: list[dict] = []
        seen: set[str] = set()
        for item in raw:
            base = mapping.public_model(item)
            if not base.get('model_name'):
                continue
            # 双区同名模型在列表里**按区域各列一条**：ID 带 `qodercn/` /
            # `qodercom/` 前缀，客户端选哪个就直接锁定哪个区域的账号池。
            # 单区模型只列它自己所属的区域。
            realms = base.get('realms') or [base.get('realm') or 'cn']
            for realm in realms:
                if realm not in ('cn', 'intl'):
                    continue
                model = {
                    **base,
                    'id': self.public_model_id(base['model_name'],
                                              mapping.db_realm(realm)),
                    'realm': realm,
                    'realms': [realm],
                }
                if model['id'] in seen:
                    continue
                seen.add(model['id'])
                models.append(model)
        if models:
            self._models = models
            self._models_at = time.time()
        return models or list(self._models)

    def realm_of_model(self, model: str) -> str | None:
        hint, bare = _split_realm_hint(model)
        if hint:
            return mapping.db_realm(hint)
        realms = self._cached_realms(bare)
        if len(realms) == 1:
            return mapping.db_realm(realms[0])
        return None

    def _realm_for(self, model: str, ctx: ChatContext | None) -> tuple[str | None, str]:
        hint, bare = _split_realm_hint(model)
        if hint:
            return hint, bare
        # 区域化前缀（`qodercn/` / `qodercom/`）解析出的提示优先于目录与
        # 密钥版本：客户端已经明确选了区域，就不该再被自动推断改掉。
        if ctx is not None and ctx.realm:
            return mapping.normalize_realm(ctx.realm), bare
        realms = self._cached_realms(bare)
        key_realm = ''
        if ctx is not None and isinstance(ctx.key, dict):
            key_realm = mapping.normalize_realm(ctx.key.get('realm'))
        # 只有「显式绑定国际版」的密钥才作为提示；默认 cn 的密钥不强制，
        # 否则双区模型在国际版视图里也会被拉回国内版。
        if key_realm == 'intl' and (not realms or 'intl' in realms):
            return 'intl', bare
        if len(realms) == 1:
            return realms[0], bare
        return None, bare

    # ── 对话 ────────────────────────────────────────────────
    async def chat(self, model: str, body: dict,
                   ctx: ChatContext | None = None) -> PlatformChat:
        lease = await self._limiter.acquire()
        if lease is False:
            raise PlatformError(
                'Qoder 并发已满（平台池级上限），请稍后重试',
                status=429, code='pool_busy', retry_after=1)
        released = False

        def release() -> None:
            nonlocal released
            if released:
                return
            released = True
            self._limiter.release(lease)

        realm, bare = self._realm_for(model, ctx)
        wrapper_model = f'{self.model_prefix}{model}'
        payload_body = {**body, 'model': bare}
        started = time.time()
        # `account_hint` **不填区域**：它的契约是「本次实际使用的账号」（见
        # `PlatformChat.account_hint`）。区域已经在 `realm` 字段里了，把它写成
        # `qoder:cn` 会同时坏两件事：
        #   ① 日志页的「账号」列显示一个既不是 uid 也不是昵称的字符串；
        #   ② `db.attach_request_accounts` 只匹配 `account IS NULL` 的行，
        #      被写成非空之后**永远不会再回填**成真实账号。
        # 真实账号由内置网关在它自己的进程里选出，适配器这层拿不到，只能留空等
        # `services/qoderaccountlog` 从 usage.jsonl 回填（见 docs/qoder-usage-*.md）。
        try:
            if not body.get('stream'):
                data, actual_realm = await self.client.post_chat(payload_body, realm)
                data['model'] = wrapper_model
                release()
                return PlatformChat(
                    status=200,
                    payload=data,
                    usage=_usage(data.get('usage')),
                    model=self.record_name(model),
                    realm=mapping.db_realm(actual_realm or realm)
                    if (actual_realm or realm) else None,
                )
            resp = await self.client.open_chat(payload_body, realm)
        except BaseException:
            release()
            raise

        actual_realm = str(resp.headers.get('x-qoder-realm') or realm or '')
        outcome = PlatformChat(
            status=200,
            model=self.record_name(model),
            realm=mapping.db_realm(actual_realm) if actual_realm else None,
        )

        async def stream():
            try:
                async for line in resp.aiter_lines():
                    text = (line or '').strip()
                    if not text or text.startswith(':'):
                        continue
                    if not text.startswith('data:'):
                        continue
                    data = text[5:].strip()
                    if not data or data == '[DONE]':
                        if data == '[DONE]':
                            break
                        continue
                    try:
                        chunk = json.loads(data)
                    except Exception:  # noqa: BLE001 —— 跳过无法解析的心跳帧
                        continue
                    if not isinstance(chunk, dict):
                        continue
                    if chunk.get('error'):
                        err = chunk['error']
                        message = err.get('message') if isinstance(err, dict) else str(err)
                        raise PlatformError(f'Qoder 上游流错误：{message}',
                                            status=502, code='qoder_stream_error',
                                            payload=chunk)
                    if isinstance(chunk.get('usage'), dict):
                        # **就地更新**，不要 `outcome.usage = _usage(...)`：网关在开流
                        # 前可能已经持有这个 dict 的引用（`_platform_chat` 的收尾记账），
                        # 重新绑定会让它读到最初那个空 dict，于是 token / credit 恒为 0。
                        # 这与 Trae 适配器的写法（`usage['credit'] = ...`）保持一致。
                        outcome.usage.clear()
                        outcome.usage.update(_usage(chunk['usage']))
                    # 首字延迟：跳过只带 role 的第一帧（判据与 `platforms/sse.py`
                    # 一致，那里只管 `/v1/chat/completions`，Responses / Anthropic
                    # 两条协议层读的是本字段）。
                    if outcome.first_token_ms is None:
                        choices = chunk.get('choices')
                        if isinstance(choices, list) and choices:
                            delta = (choices[0] or {}).get('delta') or {}
                            if isinstance(delta, dict) \
                                    and any(k != 'role' for k in delta):
                                outcome.first_token_ms = int(
                                    (time.time() - started) * 1000)
                    chunk['model'] = wrapper_model
                    yield chunk
            finally:
                try:
                    await resp.aclose()
                except Exception:  # noqa: BLE001
                    pass
                release()

        outcome.stream = stream()

        async def closer() -> None:
            try:
                await resp.aclose()
            finally:
                release()

        outcome.closer = closer
        return outcome

    # ── 平台设置 ────────────────────────────────────────────
    def invalidate_limit(self) -> None:
        self._limiter.invalidate()


adapter = QoderAdapter()
