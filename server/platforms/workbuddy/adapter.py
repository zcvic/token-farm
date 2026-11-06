"""WorkBuddy 作为「模型组合」成员的适配器。

## 它为什么存在

组合的成员必须是"平台适配器"（`combo.adapter` 只认 `registry.get(name)`）。
而 WorkBuddy 在网关里从来不是适配器：它走的是 `_chat` 里的直连反代分支，账号
选择、冷却、粘性会话、账号权重全在随包的 `workbuddy2api` 进程里。于是"能不能
把 WorkBuddy 当成员"就变成"能不能给它补一个适配器"。

能，但**不能**让这个适配器自己猜上游：那样会绕开密钥的分组绑定（一个绑了 A 组
的密钥，请求跑到全池，隔离静默失效）。所以它复用网关已经解析好的请求上下文
（`ChatContext`：key / ip / ua / 原始路径），并且**复用同一条传输实现**
（`services/wbforward`）——与直连 WorkBuddy 的字节级行为保持一致。

## 三条"故意不做"

  · `ui_visible = False`：没有自己的账号页。账号仍由上游池管理。
  · `routable = False`：模型名前缀不生效。否则会凭空多出一个
    `workbuddy/<model>` 入口，与直连语义重复，且绕开上面那条上下文。
  · **不记账、不鉴权**：记账由网关 `_platform_chat` 做一次（用成员真名），
    鉴权（密钥 / IP / 版本 / 白名单 / 限流）由 `_authorize` 做一次。成员调用
    若把这两步再走一遍，就会"一次请求两条日志、配额双倍消耗"。
"""
from __future__ import annotations

from ... import upstreamsvc
from ...services import modelcatalog, wbforward
from ..base import (ChatContext, PlatformAdapter, PlatformChat, PlatformError)

# 兼容上游的两条对话路径（v1 / v2）：成员请求沿用客户端打的那一条。
_DEFAULT_PATH = '/v1/chat/completions'


class WorkBuddyMemberAdapter(PlatformAdapter):
    name = 'workbuddy'
    label = 'WorkBuddy'
    supports_checkin = False
    # 只在组合编辑页出现（见 `combo_member`），不进平台下拉。
    ui_visible = False
    combo_member = True
    routable = False
    # 账号权重由上游池按 `auth.weight` 调度：成员权重只决定组合内选到它的概率。
    pool_weight = True

    def record_name(self, bare_model: str) -> str:
        """记账用**裸模型名**（`glm-5.2` / `global:gpt-5.6-sol`）。

        WorkBuddy 在上游侧的模型名就是裸名，没有 `workbuddy/` 前缀。加上前缀会让
        `_record` 的 `platform` 列与 `realm` 判定都错位：前者会去找一个不存在的
        平台（落在空串上是对的），后者会按「非 global: 就是 cn」把国际版成员记成
        国内版——统计里那一栏从此是错的，而且很难看出来。
        """
        return str(bare_model or '').strip()

    # ── 账号：由上游池管理，不在本端 ──────────────────────
    def _pool_only(self) -> PlatformError:
        return PlatformError(
            'WorkBuddy 的账号在它自己的账号池里（上游 workbuddy2api），'
            '请到 WorkBuddy 模式下的「账号」页管理',
            status=400, code='workbuddy_pool')

    def list_accounts(self) -> list[dict]:
        return []

    def import_account(self, payload: dict) -> dict:
        raise self._pool_only()

    def delete_account(self, account_id: str) -> bool:
        raise self._pool_only()

    async def refresh_account(self, account_id: str, *, force: bool = False) -> dict:
        raise self._pool_only()

    async def checkin(self, account_id: str) -> dict:
        raise self._pool_only()

    async def usage(self, account_id: str) -> dict:
        raise self._pool_only()

    async def credits(self, account_id: str) -> dict:
        raise self._pool_only()

    def set_account_options(self, account_id: str, *, weight=None) -> dict:
        raise self._pool_only()

    # ── 模型：给组合编辑页挑成员用 ────────────────────────
    async def list_models(self, *, refresh: bool = False) -> list[dict]:
        """WorkBuddy 的模型清单，**按版本限定**。

        为什么成员模型要带 `global:` 前缀：上游按模型名前缀选账号池
        （`db.realm_of_model`）。国际版的裸名（`gpt-5.6-sol`）如果原样发给上游，
        会被当成国内版模型去查，结果是 11102「模型不存在」——而不是报"版本不对"，
        排查时几乎看不出来。所以这里把国际版条目补成 `global:<name>`。

        清单来自 `modelcatalog`（5 分钟缓存、含腾讯接口的显示名）。缓存为空时它
        会去拉一次，与 Trae 的 `list_models` 同样是"打开编辑页时取一份"。
        """
        out: list[dict] = []
        seen: set[str] = set()
        for realm in ('cn', 'global'):
            try:
                payload = await modelcatalog.catalog(realm, force=refresh)
            except Exception:  # noqa: BLE001 —— 单版本取不到不影响另一版本
                continue
            for item in payload.get('models') or []:
                bare = str(item.get('id') or '').strip()
                if not bare:
                    continue
                name = bare if realm == 'cn' or bare.lower().startswith('global:') \
                    else f'global:{bare}'
                if name in seen:
                    continue
                seen.add(name)
                out.append({
                    # `id` 只是这份清单里的唯一键；**不能**当成可调用模型名
                    # （`routable=False`，`workbuddy/` 前缀不参与路由）。
                    'id': f'{self.model_prefix}{name}',
                    'object': 'model',
                    'created': 0,
                    'owned_by': self.name,
                    'name': str(item.get('name') or bare),
                    'platform': self.name,
                    'realm': realm,
                    'model_name': name,
                    'context_window': int(item.get('context_length') or 0),
                    'max_tokens': int(item.get('max_output_tokens') or 0),
                    'supports_reasoning': bool(item.get('supports_reasoning')),
                    'modalities': {'input': ['text']},
                })
        return out

    # ── 网关 ────────────────────────────────────────────────
    async def chat(self, model: str, body: dict,
                   ctx: ChatContext | None = None) -> PlatformChat:
        """按**调用方的**密钥与来源反代一次 WorkBuddy 请求。

        `model` 是成员里写的模型名（`glm-5.2` / `global:gpt-5.6-sol`），已经由
        组合适配器剥掉平台名。它会覆盖 body 里的 `model`——那里是外层的组合名
        （`combo/fast`），上游不认识。
        """
        if ctx is None:
            # 只该由组合调用；直接调用没有 key/ip，无法确定分组与粘性会话。
            raise PlatformError(
                'WorkBuddy 只能作为「模型组合」的成员被调用（缺少请求上下文）',
                status=400, code='workbuddy_member_only')
        if not str(model or '').strip():
            raise PlatformError('组合成员没有指定 WorkBuddy 模型', status=400,
                                code='invalid_request')

        # 按**这把密钥**解析上游：绑定分组优先，未绑定才走默认。
        # 不在这里回落到默认上游——那会让分组隔离静默失效（见 upstreamsvc）。
        try:
            upstream = upstreamsvc.resolve_for_key(ctx.key)
        except upstreamsvc.UpstreamUnavailable as exc:
            raise PlatformError(str(exc), status=503, code='upstream_unavailable') from exc

        payload = {**body, 'model': str(model).strip()}
        stream = bool(payload.get('stream'))
        try:
            outcome = await wbforward.forward(
                upstream=upstream,
                upstream_path=ctx.upstream_path or _DEFAULT_PATH,
                body=payload,
                stream=stream,
            )
        except Exception as exc:  # noqa: BLE001
            # 文案与直连路径同源（`upstream_hint` 说明这一跳打的是谁、会不会走代理），
            # 否则同一次故障在两条路径上给出两种说法。
            raise PlatformError(
                f'上游不可用: {exc}{wbforward.upstream_hint(upstream)}',
                status=502, code='upstream_unavailable') from exc

        chat = PlatformChat(
            status=outcome.status,
            payload=outcome.payload,
            content_type=outcome.content_type,
            usage=outcome.usage,
            model=str(model).strip(),
            error=outcome.error_text,
        )
        if outcome.stream is None:
            return chat

        async def relay():
            """原样透传上游字节，并在收尾时把遥测交回给 `chat`。

            `first_token_ms` 只在上面的转发层里才知道（它扫的是上游帧），而网关
            记账发生在**流结束之后**——所以这里用一层 wrapper 把结束时刻的值同步
            到 `chat` 上，而不是在构造时就拍一个 None 下去（那会让首字延迟永远为空）。
            """
            try:
                async for chunk in outcome.stream:
                    yield chunk
            finally:
                chat.first_token_ms = outcome.first_token_ms
                chat.error = outcome.error_text

        chat.raw_stream = relay()
        return chat


adapter = WorkBuddyMemberAdapter()
