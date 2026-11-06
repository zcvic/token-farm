"""模型组合的适配器：把 `combo/<name>` 路由到真实成员平台。

## 为什么是一个「平台」

网关只认识「模型名 → 平台适配器」这一条路（见 `registry.route_model`）。
组合要能被调用，最省事也最一致的做法就是注册成一个平台：模型名变成
`combo/fast`，前缀解析、鉴权、记账全部沿用既有路径，不必在网关里加分支。

但它**不是真的平台**：没有账号、没有凭据、也没有自己的并发池。所以：

  · `ui_visible = False` —— 不出现在平台下拉与账号页；
  · 账号类方法一律抛 `combo_virtual`，而不是返回空列表（空列表会被读成
    「这个平台还没导入账号」，把配置问题说成数据问题）；
  · **不在这里加并发闸门**：每个成员适配器自己有限流（Trae 已实现），
    再加一层只会让两处上限互相掩盖。

## 失败切换

只换一次（最多两个成员）。组合的意义是分摊积分，不是无限重试：持续 5xx 时
把每个成员都试一遍会让一次用户请求变成 N 次上游调用，既慢又可能把成员平台
一起打挂。不可切换的错误（请求本身有问题，如参数非法 / 上下文超长）原样
抛出——换一个平台同样会失败，只会把真实原因换成一句笼统的「全部失败」。

## 会话粘性（prompt cache 亲和）

上游的提示词缓存按**账号/平台**隔离，同一段对话换成员就等于缓存全部作废；
而纯 SWRR 在等权两成员上几乎每轮换一次。所以第一个请求按权重选出成员后，
把「会话键 → 成员」钉住（TTL 30 分钟），后续同一会话直接复用，不再推进
SWRR 游标——否则一个长会话会把全局分配比例带偏。新会话才参与加权选路，
整体比例仍由权重决定。会话键的提取见 `services/session_affinity.affinity_key`
（`prompt_cache_key` → 会话 id → 首条 user 消息哈希）。
"""
from __future__ import annotations

from ... import db, keysvc
from ...services import combo as combo_svc
from ...services.session_affinity import SessionAffinity, affinity_key
from ..base import (ChatContext, PlatformAdapter, PlatformChat, PlatformError)
from ..registry import get as get_platform

# 换一个成员**可能**成功的状态：凭据 / 模型下线 / 限流 / 上游故障。
_SWITCHABLE_STATUS = frozenset({401, 403, 404, 408, 409, 425, 429})

# 换平台一定也失败的错误码：请求本身不合法（参数、上下文、不支持的字段）。
# 文档里明确这几种不切换，避免把确定性的 400 变成「全部失败」的 503。
_NO_SWITCH_CODES = frozenset({
    'invalid_request',
    'context_length_exceeded',
    'unsupported_parameter',
})

# 一次请求最多尝试几个成员（含首选）。
MAX_ATTEMPTS = 2


def _switchable(exc: PlatformError) -> bool:
    """这个失败换一个成员还值得再试吗？"""
    if exc.code in _NO_SWITCH_CODES:
        return False
    return exc.status in _SWITCHABLE_STATUS or exc.status >= 500


class ComboAdapter(PlatformAdapter):
    name = 'combo'
    label = '组合'
    # 虚拟平台：只借路由，不该出现在平台下拉 / 账号页。
    ui_visible = False

    def __init__(self) -> None:
        # 每个组合一份 SWRR 游标（成员键 → 当前权重）。进程内状态即可：
        # 重启后从 0 开始只影响第一批请求的分配顺序，不影响长期比例。
        self._currents: dict[str, dict[str, float]] = {}
        # 会话粘性：组合名 + 会话键 → 成员键。绑定表自带 TTL / 容量上限。
        self._affinity = SessionAffinity()

    # ── 会话粘性 ────────────────────────────────────────────
    def _binding(self, combo_name: str, key: str) -> str:
        # 同一会话键在不同组合里可以落不同成员，所以带组合名做命名空间。
        return f'{combo_name}\x00{key}'

    def _bound_member(self, combo_name: str, key: str,
                      members: list[dict]) -> dict | None:
        """取这个会话钉住的成员；成员已不在启用列表里则丢弃绑定。"""
        if not key:
            return None
        binding = self._binding(combo_name, key)
        bound = self._affinity.get(binding)
        if not bound:
            return None
        member = next((m for m in members
                       if combo_svc.member_key(m) == bound), None)
        if member is None:
            # 成员被停用或删掉了：留着绑定只会每次都白找一遍。
            self._affinity.unbind(binding)
        return member

    def _bind(self, combo_name: str, key: str, member: dict) -> None:
        if key:
            self._affinity.bind(self._binding(combo_name, key),
                                combo_svc.member_key(member))

    # ── 账号：虚拟平台没有账号 ────────────────────────────
    def list_accounts(self) -> list[dict]:
        return []

    def import_account(self, payload: dict) -> dict:
        raise PlatformError('组合是虚拟平台，没有账号可导入', status=400,
                            code='combo_virtual')

    def delete_account(self, account_id: str) -> bool:
        return False

    async def refresh_account(self, account_id: str, *, force: bool = False) -> dict:
        raise PlatformError('组合是虚拟平台，没有账号可刷新', status=400,
                            code='combo_virtual')

    async def checkin(self, account_id: str) -> dict:
        raise PlatformError('组合是虚拟平台，没有账号可签到', status=400,
                            code='combo_virtual')

    async def usage(self, account_id: str) -> dict:
        raise PlatformError('组合是虚拟平台，没有账号可查询用量', status=400,
                            code='combo_virtual')

    # ── 模型 ────────────────────────────────────────────────
    async def list_models(self, *, refresh: bool = False) -> list[dict]:
        """只列出**启用**的组合；停用的组合不出现在 `/v1/models` 里。"""
        return [self._public_model(name)
                for name in combo_svc.names(db.get_setting('model_combos', []))]

    def _public_model(self, name: str) -> dict:
        return {
            'id': f'{self.model_prefix}{name}',
            'object': 'model',
            'created': 0,
            'owned_by': self.name,
            'name': name,
            'platform': self.name,
            'realm': 'cn',
            'context_window': 0,
            'max_tokens': 0,
            'supports_reasoning': False,
            'modalities': {'input': ['text']},
            'model_name': name,
        }

    # ── 网关 ────────────────────────────────────────────────
    async def chat(self, model: str, body: dict,
                   ctx: ChatContext | None = None) -> PlatformChat:
        raw = db.get_setting('model_combos', [])
        target = combo_svc.find(raw, model)
        if target is None:
            raise PlatformError(f'未知组合 {model!r}', status=404,
                                code='combo_not_found')

        members = combo_svc.enabled_members(target)
        if not members:
            # `load` 允许「全部成员停用」的组合存在（用户可能只是临时关掉），
            # 但调用时它没有任何可用去向，必须明确报错而不是静默回落到别处。
            raise PlatformError(f'组合「{target["name"]}」没有启用的成员',
                                status=503, code='combo_all_failed')

        combo_name = target['name']
        affinity = affinity_key(body)
        chosen = self._bound_member(combo_name, affinity, members)
        if chosen is not None:
            # 粘住的成员仍启用：直接用，**不推进 SWRR 游标**——长会话会把
            # 全局比例带偏，只有新会话才参与加权选路。
            rest = combo_svc.alternatives(members, chosen)
        else:
            currents = self._currents.setdefault(combo_name, {})
            # 成员被删除 / 改名后旧游标会永久残留；顺手清掉，避免配置反复调整
            # 时这个 dict 只增不减。
            live = {combo_svc.member_key(m) for m in members}
            for stale in [k for k in currents if k not in live]:
                currents.pop(stale, None)
            chosen, rest = combo_svc.pick(members, currents)
            # 先绑定再调用：同一会话并发发出的两条请求也应落同一成员，等结果
            # 回来才绑定就晚了。失败切换成功时会被改绑到新成员。
            if affinity and chosen is not None:
                self._bind(combo_name, affinity, chosen)
        attempts = ([chosen] + rest if chosen else rest)[:MAX_ATTEMPTS]

        failures: list[str] = []
        for member in attempts:
            key = f'{member["platform"]}/{member["model"]}'
            adapter = get_platform(member['platform'])
            if adapter is None:
                # 成员平台被卸载 / 配置里写了不存在的平台：跳过它，继续下一个。
                failures.append(f'{key}: 未知平台')
                continue
            # 版本校验必须在**调用成员之前**做。
            #
            # 网关入口名是 `combo/fast`，它不属于任何一个版本，所以 `_authorize`
            # 传的是 `platform_model=True`，版本检查被跳过。成员却可能指向
            # `global:xxx`：若不在这一层补一刀，限定国内版的密钥就能借组合用到
            # 国际版模型，版本隔离静默失效。放在调用**之前**还有一层原因：等到
            # 拿到结果再拒绝，那次上游调用已经发出去、积分已经花掉了。
            #
            # 判据用 `routable`：真实平台成员的 `trae/xxx` 与直连时一样不受 realm
            # 约束；只有 WorkBuddy 这种裸模型名成员是版本相关的。
            if ctx is not None and ctx.key and not getattr(adapter, 'routable', True):
                reject = keysvc.realm_allows(ctx.key, member['model'], member=True)
                if reject is not None:
                    raise PlatformError(
                        str(reject), status=getattr(reject, 'status', 403),
                        code=getattr(reject, 'code', 'realm_mismatch'))
            try:
                # `ctx` 原样透传：WorkBuddy 成员要靠它拿到调用方的密钥与来源 IP
                # （分组绑定 / 粘性会话都在这两条上）。真实平台忽略这个参数。
                outcome = await adapter.chat(member['model'], body, ctx)
            except PlatformError as exc:
                failures.append(f'{key}: {exc.message}')
                if _switchable(exc):
                    continue
                # 请求本身的问题：换平台也一样失败，把原始状态码与错误码
                # 透给客户端，它才知道该改哪里。
                raise
            # 成员适配器不一定填 model（WorkBuddy 那条老路径没有）。补成真实
            # 去向，网关记账才能记出「这次到底走了哪个平台」。
            if not outcome.model:
                outcome.model = key
            if affinity:
                # 首轮绑定通常与这里相同；失败切到别的成员时必须改绑，
                # 否则这个会话每轮都会先撞一次坏成员。
                self._bind(combo_name, affinity, member)
            return outcome

        # 全部失败（或一个候选都没有）：503 让客户端知道可以稍后重试，
        # 而不是 4xx 暗示请求写错了。
        detail = '；'.join(failures) or '没有可尝试的成员'
        raise PlatformError(
            f'组合「{target["name"]}」全部失败：{detail}', status=503,
            code='combo_all_failed')


adapter = ComboAdapter()
