"""平台运行参数：请求超时、换号次数与平台专属开关。

## 和另外两个「平台设置」模块的分工

  · `limiter.py`  —— **单账号**推理并发（每个账号同时有多少个上游请求在跑）；
  · `checkinpacing.py` —— 批量签到的并发与账号间隔（一次性任务的节奏）；
  · 本模块 —— 单次请求的**时间预算**、缓存节奏、重试冷却与适配器兼容开关。

三者都按平台分开存（`platforms.<name>.<field>`），但口径互不相干。放在同一个
`/api/platforms/{name}/settings` 里返回，是因为界面把它们放在同一张「平台配置」
表单上；后端仍各自校验、各自落库，改一个不会把另一个写回默认值。

## 三类默认值

  · **常量默认**（`_INT_FIELDS` / `_BOOL_FIELDS`）：以前就是模块里的硬编码常量，
    这里只是把它挪到可配置层，默认值与旧行为逐位相同；
  · **环境变量默认**（`solo_max_tokens` / `credit_meter` / `_PROTOCOL_BOOLS`）：
    Trae 协议层的 `TF_TRAE_*` / `config.TRAE_*` 先于本模块存在，运维仍在用。
    默认值**实时**读 `config`，只有用户显式保存过才用库里的值——这样
    `TF_TRAE_*` 环境变量照常生效，面板改了也能覆盖它；
  · **关闭即旧行为**（`cooldown_429` / `cooldown_5xx`）：0 表示维持改动前的
    「429/5xx 直接上抛、不换号也不冻结」，只有大于 0 才启用强制冷却。

## 为什么不缓存

`db.get_setting` 是一次本地 SQLite 主键查询；一次对话最多读 6 个值，量级远小于
上游网络往返。之前给 `limiter.py` 加缓存是因为它每次请求都要读、而且改了要
立刻生效；这里字段更多、改动更少，直接读库反而不会出现「保存了但界面 / 请求
还看到旧值」的问题。

**组合配置是例外**：`settings.model_combos` 在每个组合请求上都要读，而且
`services/combo.py:load()` 还要逐条归一化成员，开销不在同一个量级，所以它走
进程内快照（`combo.snapshot()`），失效挂在 `db` 的写入通知上。本模块的字段仍
直读库——它们的读次数少、单值校验便宜，多一层缓存只会多一个「什么时候失效」
的问题。

## 取值坏了怎么办

读路径一律回默认值，不抛异常：一条坏设置不该让整个平台的对话不可用。写路径
（`save`）严格校验并抛 `ValueError`，由路由转成 400。
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from .. import config, db

logger = logging.getLogger('workbuddy.platforms.settings')

DEFAULT_REQUEST_TIMEOUT = 300
DEFAULT_IDLE_TIMEOUT = 60
DEFAULT_MAX_ACCOUNTS_TRY = 3
DEFAULT_SESSION_AFFINITY = True
DEFAULT_SESSION_AFFINITY_TTL = 30 * 60
DEFAULT_DESKTOP_SYNC = True
# Loomy 保底积分：低于该值的账号只作最后候选。默认 5000，0 = 关闭。
DEFAULT_MIN_CREDITS = 5000
# DuMate 对话消耗回补窗口：入账/展示时回溯同步最近多少天。
DEFAULT_USAGE_SYNC_DAYS = 7

REQUEST_TIMEOUT_MIN, REQUEST_TIMEOUT_MAX = 10, 900
IDLE_TIMEOUT_MIN, IDLE_TIMEOUT_MAX = 10, 600
MAX_ACCOUNTS_TRY_MIN, MAX_ACCOUNTS_TRY_MAX = 1, 10
SESSION_AFFINITY_TTL_MIN, SESSION_AFFINITY_TTL_MAX = 60, 12 * 3600
MIN_CREDITS_MIN, MIN_CREDITS_MAX = 0, 10_000_000

# name -> (默认值 / 默认值工厂, 下限, 上限, 界面文案用的中文名)
# 工厂只用于「默认值来自环境变量」的项（`solo_max_tokens` 的 TF_TRAE_*）：每次
# 读取实时调用，所以测试里 `mock.patch.object(config, ...)` 与运维改环境变量
# 都照旧生效，不会被这里抄成一份静态默认值。
_IntDefault = int | Callable[[], int]
_INT_FIELDS: dict[str, tuple[_IntDefault, int, int, str]] = {
    'request_timeout': (
        DEFAULT_REQUEST_TIMEOUT, REQUEST_TIMEOUT_MIN, REQUEST_TIMEOUT_MAX, '请求超时'),
    'idle_timeout': (
        DEFAULT_IDLE_TIMEOUT, IDLE_TIMEOUT_MIN, IDLE_TIMEOUT_MAX, '流空闲超时'),
    'max_accounts_try': (
        DEFAULT_MAX_ACCOUNTS_TRY, MAX_ACCOUNTS_TRY_MIN, MAX_ACCOUNTS_TRY_MAX, '换号次数'),
    # ── Loomy：保底积分 ──
    # 与 WorkBuddy 的「保底积分」（上游 0048）同语义：低于阈值的账号只是**最后
    # 候选**，池里还有别的号就先跳过它；全部低于保底时照常使用，不会因此报错。
    # 余额来自 `credits_snapshot_platforms` 里最近一次登记的积分（账号页刷新积分
    # / 批量查询时写入），从未登记过的账号不参与判断。
    'min_credits': (
        DEFAULT_MIN_CREDITS, MIN_CREDITS_MIN, MIN_CREDITS_MAX, '保底积分'),
    'session_affinity_ttl': (
        DEFAULT_SESSION_AFFINITY_TTL, SESSION_AFFINITY_TTL_MIN,
        SESSION_AFFINITY_TTL_MAX, '会话亲和时长'),
    # ── DuMate：对话扣费入账的回补窗口 ──
    # 打开「对话消耗入账」后，每次同步会回溯最近 N 天的会话记录。窗口越大，
    # 首次补账越完整，但上游请求也越多；默认 7 天与用量弹窗的展示窗口一致。
    'usage_sync_days': (
        DEFAULT_USAGE_SYNC_DAYS, 1, 90, '消耗记录回补'),
    # ── Trae：模型目录缓存与刷新退避 ──
    # 默认值就是原先 `adapter.py` 里的那几个常量。目录刷新失败时的指数退避
    # 起点与封顶也一起放出，避免「等半小时才重试」只能改代码。
    'catalog_ttl': (6 * 3600, 600, 86400, '模型目录缓存'),
    'catalog_retry_ttl': (600, 60, 21600, '目录失败冷却'),
    'catalog_timeout': (10, 3, 60, '目录拉取超时'),
    'catalog_backoff_base': (30, 5, 600, '刷新退避起点'),
    'catalog_backoff_max': (1800, 60, 7200, '刷新退避封顶'),
    # ── Trae：积分 / 额度查询 ──
    # 账号列表一次问全部账号，TTL 太短等于每刷新一次页面就打一轮上游。
    'credits_ttl': (60, 15, 600, '积分缓存'),
    'credits_timeout': (20, 5, 60, '积分查询超时'),
    # ── Trae：限流 / 上游错误冷却（0 = 维持旧行为：直接上抛、不换号不冻结）──
    'cooldown_429': (0, 0, 600, '限流冷却'),
    'cooldown_5xx': (0, 0, 300, '上游错误冷却'),
    # ── Trae：SOLO 通道默认输出预算 ──
    # 思考 token 与正文共享这个上限；客户端显式传 max_tokens 时仍以客户端为准。
    'solo_max_tokens': (lambda: config.TRAE_SOLO_MAX_TOKENS, 1024, 2_000_000,
                        'SOLO 输出上限'),
}

# name -> (默认值, 中文名)
_BOOL_FIELDS: dict[str, tuple[bool, str]] = {
    'session_affinity': (DEFAULT_SESSION_AFFINITY, '会话亲和'),
    'desktop_sync': (DEFAULT_DESKTOP_SYNC, '回写桌面端'),
    'web_tools': (False, '本地 Web 工具'),
    # DuMate 的对话扣费只在上游会话记录里，网关的 usage 没有积分字段。打开后
    # 会把记录同步进积分收支的支出账本；默认关闭 = 维持旧行为（只在弹窗展示）。
    'usage_ledger': (False, '对话消耗入账'),
}

# Trae 协议兼容开关：name -> (config 属性, 中文名)。
# 这批开关原先只能靠 `TF_TRAE_*` 环境变量改，改一次要重启进程；现在面板可覆盖，
# 默认值仍实时取 config（所以环境变量与测试里的 patch 照旧生效）。
_PROTOCOL_BOOLS: dict[str, tuple[str, str]] = {
    'native_tools': ('TRAE_NATIVE_TOOLS', '原生工具透传'),
    'text_tool_rescue': ('TRAE_TEXT_TOOL_RESCUE', '正文调用还原'),
    'text_tool_tags': ('TRAE_TEXT_TOOL_TAGS', '标签调用还原'),
    'tool_protocol_hint': ('TRAE_TOOL_PROTOCOL_HINT', '工具协议提示'),
    'degenerate_call_error': ('TRAE_DEGENERATE_CALL_ERROR', '退化调用判错'),
    'rescue_tail_strict': ('TRAE_RESCUE_TAIL_STRICT', '尾部规则'),
    'reasoning_effort': ('TRAE_REASONING_EFFORT', '思考强度透传'),
    'reasoning_as_content': ('TRAE_REASONING_AS_CONTENT', '思考内容兜底'),
    'empty_output_error': ('TRAE_EMPTY_OUTPUT_ERROR', '空产出判错'),
    'strict_stream_end': ('TRAE_STRICT_STREAM_END', '严格结束判据'),
}
_BOOL_NAMES = (*_BOOL_FIELDS, *_PROTOCOL_BOOLS)

# 三态 / 多选开关：可选值里保留空串 =「跟随上游默认」（Qoder 是网关默认，
# Trae 的 credit_meter 是「不计量」）。这些开关由内置网关 / 适配器**每次请求
# 现读**，所以保存后立即生效，不需要重启。
# name -> (默认值, 可选值, 中文名)
_ENUM_FIELDS: dict[str, tuple[str, tuple[str, ...], str]] = {
    'tool_echo_mode': ('', ('', 'strict', 'lenient'), '工具残片收尾'),
    'anthropic_thinking': ('', ('', 'off', 'empty'), 'Anthropic 思考块'),
    'structured_tool_history': ('', ('', 'auto', 'on', 'off'), '结构化工具历史'),
    'image_transcode': ('', ('', 'auto', 'off'), '图片预处理'),
    # 空串 = 不计量（本次改动前的行为）。默认值跟随 TF_TRAE_CREDIT_METER，
    # 见 `_enum_default`：环境变量设了 detail/delta 时下拉框直接显示对应项，
    # 只有显式保存空串才会覆盖成「不计量」。
    'credit_meter': ('', ('', 'delta', 'detail'), '实扣积分模式'),
}

# 默认值跟随环境变量的枚举项：name -> config 属性。未设置过时用 config 的现值，
# 用户保存过（包括保存空串「关闭」）之后以库里的值为准。
_ENUM_CONFIG_DEFAULT: dict[str, str] = {'credit_meter': 'TRAE_CREDIT_METER'}
# 同名开关在不同平台对应不同的环境变量（灵犀的默认值跟随 TF_LINGXI_CREDIT_METER）。
_ENUM_CONFIG_DEFAULT_BY_PLATFORM: dict[tuple[str, str], str] = {
    ('lingxi', 'credit_meter'): 'LINGXI_CREDIT_METER',
}

# 每个平台真正会用到的字段。不在表里的平台（含测试里的假平台）只拿到通用两项：
# 任何适配器都有「发请求」这一步，超时对它们都有意义；换号 / 亲和 / 回写则依赖
# 具体的选号实现，不能凭空显示给用户。
_GENERIC = frozenset({'request_timeout', 'idle_timeout'})
_TRAE = frozenset({
    'request_timeout', 'idle_timeout', 'max_accounts_try',
    'session_affinity', 'session_affinity_ttl', 'desktop_sync',
    'catalog_ttl', 'catalog_retry_ttl', 'catalog_timeout',
    'catalog_backoff_base', 'catalog_backoff_max',
    'credits_ttl', 'credits_timeout',
    'cooldown_429', 'cooldown_5xx', 'solo_max_tokens', 'credit_meter',
    *_PROTOCOL_BOOLS,
})
# Qoder 的协议兼容开关：网关的对话管线里有一批「模型行为不合预期」时的兜底
# 策略，上游面板能改，token-farm 之前没有任何入口。它们都是网关**每次请求
# 现读**的值（见 embedded.apply_runtime_settings），保存即生效。
#
# 为什么不放 `max_accounts_try` / `catalog_*` / `cooldown_*`：Qoder 的账号
# 选路、目录与限流都在**内置网关进程**里，适配器只做一条 HTTP 透传（见
# `qoder/adapter.py`），本进程没有任何一处会读这些值。放出来只会得到
# 「保存成功但行为不变」——正是能力表要避免的那种假开关。
_QODER = frozenset({
    'request_timeout', 'idle_timeout',
    'tool_echo_mode', 'anthropic_thinking', 'structured_tool_history',
    'image_transcode', 'web_tools',
})
# Loomy 是原生实现（与 Trae 同构的选号 / 缓存 / 冷却策略），但没有桌面端会话
# 回写与协议兼容开关：不含 `desktop_sync` / `solo_max_tokens` / `credit_meter`。
# `min_credits` 只有 Loomy 有：余额低于阈值的号只作最后候选（见 `_INT_FIELDS`）。
_LOOMY = frozenset({
    'request_timeout', 'idle_timeout', 'max_accounts_try',
    'min_credits',
    'session_affinity', 'session_affinity_ttl',
    'catalog_ttl', 'catalog_retry_ttl', 'catalog_timeout',
    'credits_ttl', 'credits_timeout',
    'cooldown_429', 'cooldown_5xx',
})
# DuMate 与 Loomy 同一组策略（原生选号 / 目录缓存 / 冷却），也没有桌面端回写。
# 保底积分目前只在 Loomy 的选号里实现，不要顺手开给同构的 DuMate / MiniMax。
#
# 但 **没有 `catalog_timeout`**：DuMate 的模型目录读的是本机客户端写下的快照
# （`catalog.read_desktop_models()`），本进程根本不发目录请求，放出来就是一个
# 改了也不会生效的字段。
_DUMATE = (_LOOMY - {'min_credits', 'catalog_timeout'}) \
    | {'usage_ledger', 'usage_sync_days'}
# MiniMax Code 同为原生选号 / 目录缓存 / 冷却，且目录是**真的**在线拉取
# （`_live_models` 带 `catalog_timeout`），所以保留 Loomy 的整组字段。
# 额外支持网页设备码登录（路由不按能力表过滤登录，这里只是保持与适配器声明
# 一致）。
_MINIMAX = _LOOMY - {'min_credits'}
# Discovery / Kilo / Cline 与 Loomy 同一组策略（原生选号 / 目录缓存 / 冷却），
# 没有桌面端回写，也没有保底积分。三者的目录都是**在线**拉取，所以保留
# `catalog_timeout`；`min_credits` 只在 Loomy 的选号里实现，不要顺手开给它们。
#
# Kilo 的余额是美元、Cline 没有余额接口——但 `credits_ttl` / `credits_timeout`
# 仍保留：Kilo 用它缓存余额与给查询定超时，Cline 的 `credits()` 走基类「不支持」，
# token 轮换用的是 `request_timeout`（见 `_session_timeout`）。
_NATIVE_POOL = _LOOMY - {'min_credits'}
# 豆包也是原生选号 / 冷却，但**没有在线模型目录**（模型表是内置快照），所以
# 不能给 `catalog_*`：那会是「保存成功但行为不变」的假开关。它有签到与额度
# 查询，`credits_ttl` / `credits_timeout` 照常保留。
_DOUBAO = _NATIVE_POOL - {'catalog_ttl', 'catalog_retry_ttl', 'catalog_timeout'}
# WPS 灵犀是原生选号 / 在线目录（`/sessions/plans`）/ 签到 / 余额，与 Loomy
# 同一组策略；但没有「保底积分」的选号实现，也没有桌面端回写。
# `credit_meter` 是每请求实扣积分口径（见 config.LINGXI_CREDIT_METER）：灵犀
# 必须靠网关上报 run 生命周期才会扣费，所以这个开关同时决定「是否计费」。
_LINGXI = _NATIVE_POOL | {'credit_meter'}
# 组合是虚拟平台，没有自己的账号 / 目录，但**会话粘性**直接决定它会不会把
# 同一段对话拆到多个成员平台（缓存作废，上下文被反复重读）。它与 Trae /
# Loomy 用同一组开关，运维可以关掉或调整时长，而不必改代码 / 重启。
_COMBO = frozenset({'session_affinity', 'session_affinity_ttl'})
_BY_PLATFORM: dict[str, frozenset[str]] = {
    'trae': _TRAE,
    'qoder': _QODER,
    'loomy': _LOOMY,
    'dumate': _DUMATE,
    'minimax': _MINIMAX,
    'discovery': _NATIVE_POOL,
    'kilo': _NATIVE_POOL,
    'cline': _NATIVE_POOL,
    'doubao': _DOUBAO,
    'lingxi': _LINGXI,
    'combo': _COMBO,
}


def capabilities(platform: str) -> dict[str, bool]:
    """该平台支持哪些字段。前端据此决定显示哪几组，而不是用平台名硬编码。"""
    supported = _BY_PLATFORM.get(platform, _GENERIC)
    return {
        name: name in supported
        for name in (*_INT_FIELDS, *_BOOL_NAMES, *_ENUM_FIELDS)
    }


def _key(platform: str, field: str) -> str:
    return f'platforms.{platform}.{field}'


def _int_default(field: str) -> int:
    raw = _INT_FIELDS[field][0]
    if callable(raw):
        try:
            return int(raw())
        except (TypeError, ValueError):  # 环境变量写坏了：退回区间下限，仍可保存
            return _INT_FIELDS[field][1]
    return int(raw)


def _read_int(platform: str, field: str) -> int:
    default = _int_default(field)
    _, low, high, _ = _INT_FIELDS[field]
    raw = db.get_setting(_key(platform, field), default)
    if isinstance(raw, bool):
        return default
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return value if low <= value <= high else default


def _bool_default(field: str) -> bool:
    if field in _BOOL_FIELDS:
        return _BOOL_FIELDS[field][0]
    return bool(getattr(config, _PROTOCOL_BOOLS[field][0], False))


def _bool_label(field: str) -> str:
    if field in _BOOL_FIELDS:
        return _BOOL_FIELDS[field][1]
    return _PROTOCOL_BOOLS[field][1]


def _read_bool(platform: str, field: str) -> bool:
    default = _bool_default(field)
    raw = db.get_setting(_key(platform, field), default)
    return raw if isinstance(raw, bool) else default


def _enum_default(platform: str, field: str) -> str:
    """枚举默认值：优先跟随环境变量（config），读坏了退回表里的静态默认。"""
    static, choices, _ = _ENUM_FIELDS[field]
    attr = (_ENUM_CONFIG_DEFAULT_BY_PLATFORM.get((platform, field))
            or _ENUM_CONFIG_DEFAULT.get(field))
    if not attr:
        return static
    raw = getattr(config, attr, '')
    if raw is True:  # 布尔型环境变量（如 TF_TRAE_CREDIT_METER=1）
        value = 'delta'
    elif not raw:
        value = ''
    else:
        value = str(raw).strip().lower()
    return value if value in choices else static


def _read_enum(platform: str, field: str) -> str:
    default = _enum_default(platform, field)
    _, choices, _ = _ENUM_FIELDS[field]
    raw = db.get_setting(_key(platform, field), default)
    return raw if isinstance(raw, str) and raw in choices else default


def configured(platform: str, field: str) -> bool:
    """该字段是否**被显式设置过**（不是「读出来的值」）。

    用于「没设置时沿用客户端 / 环境变量的默认值」：Qoder 的读超时默认来自
    `QODER_TIMEOUT`，如果这里无脑把 300 传下去，运维改过的环境变量就被悄悄
    覆盖了。显式设置过才用这里的值。
    """
    return db.get_setting(_key(platform, field), None) is not None


def load(platform: str) -> dict[str, Any]:
    """当前生效的运行参数 + 可填范围 + 能力表。"""
    values: dict[str, Any] = {
        field: _read_int(platform, field) for field in _INT_FIELDS
    }
    values.update({field: _read_bool(platform, field) for field in _BOOL_NAMES})
    values.update({field: _read_enum(platform, field) for field in _ENUM_FIELDS})
    caps = capabilities(platform)
    return {
        **values,
        'capabilities': caps,
        'runtime_limits': {
            field: {'min': low, 'max': high}
            for field, (_, low, high, _) in _INT_FIELDS.items()
        },
        # 枚举字段的可选值（含空串 =「跟随网关默认」）。只下发本平台支持的，
        # 前端据此画下拉框，不用在界面里再抄一份取值表。
        'enum_choices': {
            field: list(choices)
            for field, (_, choices, _) in _ENUM_FIELDS.items()
            if caps[field]
        },
    }


def _coerce_int(field: str, raw: object) -> int:
    _, low, high, label = _INT_FIELDS[field]
    if isinstance(raw, bool):
        raise ValueError(f'{label}必须是整数')
    if isinstance(raw, float) and not raw.is_integer():
        raise ValueError(f'{label}必须是整数')
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{label}必须是整数') from exc
    if not low <= value <= high:
        raise ValueError(f'{label}需在 {low} 与 {high} 之间')
    return value


def _coerce_bool(field: str, raw: object) -> bool:
    if not isinstance(raw, bool):
        raise ValueError(f'{_bool_label(field)}必须是布尔值')
    return raw


def _coerce_enum(field: str, raw: object) -> str:
    _, choices, label = _ENUM_FIELDS[field]
    if not isinstance(raw, str):
        raise ValueError(f'{label}必须是字符串')
    value = raw.strip().lower()
    if value not in choices:
        allowed = ' / '.join(choice or '默认' for choice in choices)
        raise ValueError(f'{label}只能是 {allowed}')
    return value


def save(platform: str, payload: dict) -> dict[str, Any]:
    """保存运行参数；只更新 payload 里出现的字段，非法值抛 `ValueError`。

    路由会把整个请求体一起传进来（里面还有 `max_in_flight` 与 `checkin_*`），
    这里只认自己的字段，其余原样忽略。反过来，如果 payload 带了**本平台不
    支持**的字段就明确报错：静默丢弃会让界面以为「保存成功了」，实际什么都没改。
    """
    payload = payload if isinstance(payload, dict) else {}
    caps = capabilities(platform)
    updates: dict[str, Any] = {}
    for field in _INT_FIELDS:
        if field not in payload or payload[field] is None:
            continue
        if not caps[field]:
            raise ValueError(f'{platform} 不支持「{_INT_FIELDS[field][3]}」设置')
        updates[field] = _coerce_int(field, payload[field])
    for field in _BOOL_NAMES:
        if field not in payload or payload[field] is None:
            continue
        if not caps[field]:
            raise ValueError(f'{platform} 不支持「{_bool_label(field)}」设置')
        updates[field] = _coerce_bool(field, payload[field])
    for field in _ENUM_FIELDS:
        if field not in payload or payload[field] is None:
            continue
        if not caps[field]:
            raise ValueError(f'{platform} 不支持「{_ENUM_FIELDS[field][2]}」设置')
        updates[field] = _coerce_enum(field, payload[field])
    for field, value in updates.items():
        db.set_setting(_key(platform, field), value)
    return load(platform)


# ── 适配器用的单值读取 ────────────────────────────────────
# 适配器在热路径上只需要一两个字段时，用这些函数比 `load()` 少读几行库。
# 需要一次读多个（例如 Trae 的对话路径）时仍建议 `load()` 一次拿全。
def request_timeout(platform: str) -> int:
    return _read_int(platform, 'request_timeout')


def idle_timeout(platform: str) -> int:
    return _read_int(platform, 'idle_timeout')


def max_accounts_try(platform: str) -> int:
    return _read_int(platform, 'max_accounts_try')


def min_credits(platform: str) -> int:
    """保底积分（0 = 关闭）；仅 Loomy 的选号会读。"""
    return _read_int(platform, 'min_credits')


def usage_sync_days(platform: str) -> int:
    """DuMate 对话消耗的回补窗口（天）。"""
    return _read_int(platform, 'usage_sync_days')


def usage_ledger(platform: str) -> bool:
    """是否把 DuMate 对话消耗计入积分收支（默认关闭 = 旧行为）。"""
    return _read_bool(platform, 'usage_ledger')


def session_affinity(platform: str) -> bool:
    return _read_bool(platform, 'session_affinity')


def session_affinity_ttl(platform: str) -> int:
    return _read_int(platform, 'session_affinity_ttl')


def desktop_sync(platform: str) -> bool:
    return _read_bool(platform, 'desktop_sync')


# ── Trae：缓存 / 冷却 / 协议开关 ──────────────────────────
# 这一组只有 Trae 适配器会读（Qoder 的 capabilities 里没有这些字段）。
def catalog_ttl(platform: str) -> int:
    return _read_int(platform, 'catalog_ttl')


def catalog_retry_ttl(platform: str) -> int:
    return _read_int(platform, 'catalog_retry_ttl')


def catalog_timeout(platform: str) -> int:
    return _read_int(platform, 'catalog_timeout')


def catalog_backoff_base(platform: str) -> int:
    return _read_int(platform, 'catalog_backoff_base')


def catalog_backoff_max(platform: str) -> int:
    return _read_int(platform, 'catalog_backoff_max')


def credits_ttl(platform: str) -> int:
    return _read_int(platform, 'credits_ttl')


def credits_timeout(platform: str) -> int:
    return _read_int(platform, 'credits_timeout')


def cooldown_429(platform: str) -> int:
    return _read_int(platform, 'cooldown_429')


def cooldown_5xx(platform: str) -> int:
    return _read_int(platform, 'cooldown_5xx')


def solo_max_tokens(platform: str) -> int:
    return _read_int(platform, 'solo_max_tokens')


def credit_meter(platform: str) -> str:
    """实扣积分口径：'' / 'delta' / 'detail'（见 config.TRAE_CREDIT_METER）。"""
    return _read_enum(platform, 'credit_meter')


def protocol_switches(platform: str) -> dict[str, bool]:
    """Trae 协议兼容开关一次读全。

    协议层一次请求要判断好几个开关（工具透传 / 还原 / 判错 / 断流），逐个
    调单值函数会让调用点散成一片；这里返回的 dict 就是 `_PROTOCOL_BOOLS`
    的键，调用方按名字取，拼错会立刻 KeyError 而不是静默拿到 False。
    """
    return {field: _read_bool(platform, field) for field in _PROTOCOL_BOOLS}
