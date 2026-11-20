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

一次请求最多尝试 3 个成员（含首选）。组合的意义是分摊积分，不是无限重试：
持续 5xx 时把每个成员都试一遍会让一次用户请求变成 N 次上游调用，既慢又可能
把成员平台一起打挂。不可切换的错误（请求本身有问题，如参数非法 / 上下文超长）
原样抛出——换一个平台同样会失败，只会把真实原因换成一句笼统的「全部失败」。

失败过的成员会进入**短冷却**：冷却期内选路直接跳过它，不再每轮先撞一次。
冷却按连续失败次数指数退避（15 秒起，封顶 10 分钟）；429 优先采用上游给的
`retry_after`。成功的成员立即解除冷却。3 个以上成员时也必须有第三次机会：
只试两个成员会让「前两个故障、第三个健康」的组合持续 503。

## 会话粘性（prompt cache 亲和）

上游的提示词缓存按**账号/平台**隔离，同一段对话换成员就等于缓存全部作废；
而纯 SWRR 在等权两成员上几乎每轮换一次。所以第一个请求按权重选出成员后，
把「会话键 → 成员」钉住（默认 TTL 30 分钟，可通过平台设置
`platforms.combo.session_affinity` / `session_affinity_ttl` 关掉或调整时长），
后续同一会话直接复用，不再推进 SWRR 游标——否则一个长会话会把全局分配比例
带偏。新会话才参与加权选路，整体比例仍由权重决定。会话键的提取见
`services/session_affinity.affinity_key`（`prompt_cache_key` → 会话 id →
首条 user 消息哈希），显式键未命中时还会用 `content_key` 的内容指纹兜底。

**绑定的成员只是「暂时」不可用时（429 / 5xx），本次请求会切到别的成员，但
不改写绑定**：冷却结束后会话自动回到原成员。否则一次限流就会把这段对话永久
搬到另一个平台；等新平台再出一次故障又会搬一次，之前建立的缓存反复作废，
积分全花在重新读上下文上。只有凭据 / 模型确定不可用（401 / 403 / 404）或
连续失败达到阈值，才解除绑定并改绑到本次能成功的成员。
"""
from __future__ import annotations

import logging
import time
from dataclasses import replace

from ... import db, keysvc
from ...services import combo as combo_svc
from ...services.session_affinity import (SessionAffinity, affinity_key,
                                          content_key)
from .. import settings as runtime_settings
from ..base import (ChatContext, PlatformAdapter, PlatformChat, PlatformError)
from ..registry import get as get_platform

logger = logging.getLogger('workbuddy.combo')

# 换一个成员**可能**成功的状态：凭据 / 模型下线 / 限流 / 上游故障。
_SWITCHABLE_STATUS = frozenset({401, 403, 404, 408, 409, 425, 429})

# 换平台一定也失败的错误码：请求本身不合法（参数、上下文、不支持的字段）。
# 文档里明确这几种不切换，避免把确定性的 400 变成「全部失败」的 503。
_NO_SWITCH_CODES = frozenset({
    'invalid_request',
    'context_length_exceeded',
    'unsupported_parameter',
})

# 确定「这个成员本身不可用」的状态：换一次不会好，继续探测只会白花积分。
_PERMANENT_STATUS = frozenset({401, 403, 404})

# 一次请求最多尝试几个成员（含首选）。
MAX_ATTEMPTS = 3

# 成员冷却（秒）：连续失败按 2 的幂退避，封顶 10 分钟。
_COOLDOWN_START = 15.0        # 5xx / 超时 / 网络类
_COOLDOWN_RATE_LIMIT = 30.0   # 429：通常要等配额窗口
_COOLDOWN_MAX = 600.0
# 绑定的成员连续失败到这个次数，认为它对当前会话已不可用：允许改绑，
# 避免「每轮探测一次坏成员 → 换走 → 冷却结束再探测」的长期抖动。
_BREAK_AFTER = 3


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
        # 成员级短冷却：(组合名, 成员键) → (恢复时间, 连续失败次数)。
        # 失败过的成员在一段时间内不参与选路，避免每次请求都先撞一次。
        self._cooldowns: dict[tuple[str, str], tuple[float, int]] = {}

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

    # ── 成员冷却 ────────────────────────────────────────────
    def _cooling(self, combo_name: str, member_key: str, now: float) -> bool:
        item = self._cooldowns.get((combo_name, member_key))
        return item is not None and item[0] > now

    def _cool(self, combo_name: str, member: dict,
              exc: PlatformError) -> int:
        """记一次失败并返回连续失败次数。429 优先用上游的 retry_after。"""
        key = (combo_name, combo_svc.member_key(member))
        now = time.monotonic()
        _, streak = self._cooldowns.get(key, (0.0, 0))
        streak += 1
        if exc.status in _PERMANENT_STATUS:
            delay = _COOLDOWN_MAX
        else:
            start = (_COOLDOWN_RATE_LIMIT if exc.status == 429
                     else _COOLDOWN_START)
            delay = min(_COOLDOWN_MAX, start * (2 ** (streak - 1)))
        hint = getattr(exc, 'retry_after', None)
        if isinstance(hint, int) and hint > 0:
            delay = min(_COOLDOWN_MAX, max(delay, float(hint)))
        self._cooldowns[key] = (now + delay, streak)
        return streak

    def _recover(self, combo_name: str, member: dict) -> None:
        self._cooldowns.pop((combo_name, combo_svc.member_key(member)), None)

    def _currents_for(self, combo_name: str,
                      members: list[dict]) -> dict[str, float]:
        """取该组合的 SWRR 游标，并清掉已被删除 / 改名的成员。"""
        currents = self._currents.setdefault(combo_name, {})
        live = {combo_svc.member_key(m) for m in members}
        for stale in [k for k in currents if k not in live]:
            currents.pop(stale, None)
        return currents

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

    def cached_model_ids(self) -> set[str] | None:
        """已定义的组合名（`combo/<name>`）；没有组合时返回空集合（= 都不认）。

        组合清单就在库里，读一次很便宜，所以这里**不返回 None**：没有组合时
        白名单里写 `combo/x` 确实是个无效名字，应当被点出来。
        """
        return {f'{self.model_prefix}{name}'
                for name in combo_svc.names(db.get_setting('model_combos', []))}

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
        candidates: list[str] = []
        # 粘性可在平台设置里关闭（`platforms.combo.session_affinity`）：
        # 有些客户端每轮都换会话键，关掉比让它随机钉在某个成员上更可控。
        if runtime_settings.session_affinity(self.name):
            self._affinity.set_ttl(
                float(runtime_settings.session_affinity_ttl(self.name)))
            explicit = affinity_key(body)
            if explicit:
                candidates.append(explicit)
            # 显式键可能每轮都变（客户端把随机串塞进 prompt_cache_key）。
            # 内容键只在显式键未命中时兜底；它命中时不写入新的显式键，
            # 否则「每请求一个随机键」会把绑定表撑爆。`messages:` 本身
            # 已经由内容派生，不再重复加一条。
            if not explicit or not explicit.startswith('messages:'):
                derived = content_key(body)
                if derived and derived not in candidates:
                    candidates.append(derived)

        pinned = None
        hit_key = ''
        for candidate in candidates:
            pinned = self._bound_member(combo_name, candidate, members)
            if pinned is not None:
                hit_key = candidate
                break
        had_pin = pinned is not None

        now = time.monotonic()
        available = [m for m in members
                     if not self._cooling(combo_name,
                                          combo_svc.member_key(m), now)]
        if not available:
            # 全部在冷却：冷却只是「优先跳过」，不该让组合彻底不可用。
            available = list(members)

        if had_pin and pinned in available:
            # 粘住的成员仍可用：直接用，**不推进 SWRR 游标**——长会话会把
            # 全局比例带偏，只有新会话才参与加权选路。
            chosen = pinned
            rest = combo_svc.alternatives(available, pinned)
        elif had_pin:
            # 粘住的成员只是暂时冷却：本次临时切到权重最高的可用成员，
            # **不推进游标、不改绑定**，冷却结束后自动回到原成员。
            ranked = combo_svc.alternatives(available, pinned)
            chosen = ranked[0] if ranked else None
            rest = ranked[1:]
        else:
            chosen, rest = combo_svc.pick(
                available, self._currents_for(combo_name, members))
            # 先绑定再调用：同一会话并发发出的两条请求也应落同一成员，等结果
            # 回来才绑定就晚了。首次请求若失败切走，会在成功分支改绑。
            if chosen is not None:
                for candidate in candidates:
                    self._bind(combo_name, candidate, chosen)
        attempts = ([chosen] + rest if chosen else rest)[:MAX_ATTEMPTS]
        logger.debug(
            'combo %s: affinity=%s pinned=%s chosen=%s attempts=%s',
            combo_name, 'yes' if candidates else 'no',
            combo_svc.member_key(pinned) if pinned else '-',
            combo_svc.member_key(chosen) if chosen else '-',
            [combo_svc.member_key(m) for m in attempts])

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
            # 成员可以指定版本（`cn` / `global`）。组合入口 `combo/<name>` 自身
            # 不属于任何版本，网关传来的 `ctx.realm` 是 None；若不在这里补上，
            # 双区同名模型只能由平台自己猜（MiniMax 会在两版之间按账号分摊，
            # Trae 会按目录推一个），用户无法指定「这个成员走国际版」。
            # 只对 `realm_aware` 平台透传：WorkBuddy 的版本写在模型名前缀里，
            # 不是在账号池维度上区分的。
            member_ctx = ctx
            member_realm = str(member.get('realm') or '').strip()
            if member_realm and getattr(adapter, 'realm_aware', False):
                member_ctx = (replace(ctx, realm=member_realm)
                              if ctx is not None
                              else ChatContext(realm=member_realm))
            try:
                # `ctx` 原样透传：WorkBuddy 成员要靠它拿到调用方的密钥与来源 IP
                # （分组绑定 / 粘性会话都在这两条上）。真实平台忽略这个参数。
                outcome = await adapter.chat(member['model'], body, member_ctx)
            except PlatformError as exc:
                failures.append(f'{key}: {exc.message}')
                if not _switchable(exc):
                    # 请求本身的问题：换平台也一样失败，把原始状态码与错误码
                    # 透给客户端，它才知道该改哪里。
                    raise
                streak = self._cool(combo_name, member, exc)
                member_key = combo_svc.member_key(member)
                bound_now = any(
                    self._affinity.peek(self._binding(combo_name, candidate))
                    == member_key
                    for candidate in candidates)
                if bound_now and (exc.status in _PERMANENT_STATUS
                                  or streak >= _BREAK_AFTER):
                    # 绑定的成员确定不可用 / 连续多次失败：解除绑定，
                    # 本次若切到别的成员并成功，就改绑过去。
                    for candidate in candidates:
                        self._affinity.unbind(
                            self._binding(combo_name, candidate))
                    had_pin = False
                logger.debug('combo %s: %s 失败（%s %s），连续第 %s 次',
                             combo_name, key, exc.status, exc.code, streak)
                continue
            # 成员适配器不一定填 model（WorkBuddy 那条老路径没有）。补成真实
            # 去向，网关记账才能记出「这次到底走了哪个平台」。
            if not outcome.model:
                outcome.model = key
            self._recover(combo_name, member)
            if candidates:
                same = (had_pin and pinned is not None
                        and combo_svc.member_key(member)
                        == combo_svc.member_key(pinned))
                if not had_pin:
                    # 新会话（或绑定已判定失效）：把本次成功的成员记到
                    # 所有候选键上，后续任何一条兜底路径都能命中。
                    for candidate in candidates:
                        self._bind(combo_name, candidate, member)
                elif same and hit_key:
                    # 已建立绑定：只续期**命中过的那个键**。显式键每轮都变时
                    # 不要顺手写入新键，否则绑定表会被随机键撑爆。
                    self._bind(combo_name, hit_key, member)
                # 已建立绑定 + 临时故障切换：保留原绑定。
            return outcome

        # 全部失败（或一个候选都没有）：503 让客户端知道可以稍后重试，
        # 而不是 4xx 暗示请求写错了。
        detail = '；'.join(failures) or '没有可尝试的成员'
        raise PlatformError(
            f'组合「{target["name"]}」全部失败：{detail}', status=503,
            code='combo_all_failed')


adapter = ComboAdapter()
