"""模型组合（跨平台路由组）的配置语义与加权选路。

## 它解决什么

一个模型名只能对应一个平台时，积分只能从那一侧扣。组合把一个**下游模型名**
挂到多个平台成员上，每个请求按权重挑一个发出去——整体上积分就从多个平台扣。
（单个请求仍然只走一个平台：上游的一次对话没有办法拆成两半计费。）

成员**不要求同名**：`combo/fast` 可以同时挂 `trae/deepseek-V3` 与
`other/deepseek-chat`。要求同名只会把「不同平台对同一个模型叫法不同」挡在
门外，而这恰恰是最常见的情况。

## 权重

权重是**相对比例**，不是优先级：两个成员 3:1，长期大约 3/4 的请求走第一个。
选路用平滑加权轮询（nginx 的 SWRR），不是纯随机——纯随机在小样本上抖动很大
（10 个请求按 6:3:1 可能落成 7:3:0），而 SWRR 在短序列上也均匀，并且**确定性
可测**。

权重范围与 WorkBuddy 账号权重同一口径（0.1–100），避免界面上两处「权重」含义
不同。

## 坏数据

设置里的值可能是历史写入、手工改库、或将来字段改名留下的。这里的口径与
`modelmap` 一致：**坏条目只影响它自己**，整体按「没有这一条」处理，绝不让网关
因为一份坏配置 5xx。
"""
from __future__ import annotations

import re

# 组合名会成为 `combo/<name>` 的后半段：限制成最小字符集，避免斜杠、空格与
# 大小写带来的「列表里有、调用时对不上」。
NAME_RE = re.compile(r'^[a-z0-9][a-z0-9._-]{0,31}$')

WEIGHT_MIN = 0.1
WEIGHT_MAX = 100.0
DEFAULT_WEIGHT = 1.0

# 成员模型名的长度上限。上游模型 id 通常很短；给 128 是为了容下自建网关的
# `owner/model` 写法，同时挡住能把日志/统计撑大的超长字符串。
MODEL_MAX = 128


def weight_of(value: object, default: float = DEFAULT_WEIGHT) -> float:
    """把任意输入归一成一个可用的权重。

    非法（非数字 / NaN / 无穷 / <= 0）→ 默认值；合法值钳到 0.1–100。
    钳制而不是拒绝：读路径上不该因为一个越界数字就让整份配置不可用。
    """
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if number != number or number in (float('inf'), float('-inf')) or number <= 0:
        return default
    return min(WEIGHT_MAX, max(WEIGHT_MIN, number))


def _member_key(member: dict) -> str:
    return f"{member['platform']}/{member['model']}"


def member_key(member: dict) -> str:
    """成员在选路游标里的稳定键（`平台/模型`）。"""
    return _member_key(member)


def _clean_member(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    platform = str(raw.get('platform') or '').strip().lower()
    model = str(raw.get('model') or '').strip()
    if not platform or not model or len(model) > MODEL_MAX:
        return None
    if '/' in platform or model.startswith('/'):
        return None
    return {
        'platform': platform,
        'model': model,
        'weight': weight_of(raw.get('weight')),
        'enabled': raw.get('enabled') is not False,
    }


def _clean_combo(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    name = str(raw.get('name') or '').strip().lower()
    if not NAME_RE.match(name):
        return None
    members = [_clean_member(item) for item in (raw.get('members') or [])] \
        if isinstance(raw.get('members'), list) else []
    clean = [m for m in members if m]
    if not clean:
        return None
    return {
        'name': name,
        # 默认启用：老配置没有这个字段时按「可用」读，而不是静默失效。
        'enabled': raw.get('enabled') is not False,
        'members': clean,
    }


def load(raw: object) -> list[dict]:
    """读路径：把数据库里的原始值归一成组合列表（坏条目跳过）。"""
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        combo = _clean_combo(item)
        if combo is None or combo['name'] in seen:
            continue
        seen.add(combo['name'])
        out.append(combo)
    return out


def validate(raw: object) -> list[dict]:
    """写路径：严格校验要落库的配置；非法值抛 `ValueError`（路由转 400）。

    与 `load` 的差别是**不静默丢弃**：保存时丢掉一条，用户会以为保存成功了，
    但组合里少了成员——这类「看起来成功」的失败比报错更难查。
    """
    if not isinstance(raw, list):
        raise ValueError('组合配置必须是一个数组')
    out: list[dict] = []
    seen: set[str] = set()
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ValueError(f'第 {index} 个组合必须是对象')
        name = str(item.get('name') or '').strip().lower()
        if not NAME_RE.match(name):
            raise ValueError(
                f'第 {index} 个组合的名字不合法：只能用 1–32 个字母、数字与 ._-，'
                '并且以字母或数字开头')
        if name in seen:
            raise ValueError(f'组合名重复：{name}')
        seen.add(name)
        members = item.get('members')
        if not isinstance(members, list) or not members:
            raise ValueError(f'组合「{name}」至少要有一个成员')
        clean_members: list[dict] = []
        keys: set[str] = set()
        for raw_member in members:
            member = _clean_member(raw_member)
            if member is None:
                raise ValueError(
                    f'组合「{name}」里有成员的平台或模型名不合法'
                    '（平台不能含斜杠，模型名不能以斜杠开头）')
            key = _member_key(member)
            if key in keys:
                raise ValueError(f'组合「{name}」里重复的成员：{key}')
            keys.add(key)
            clean_members.append(member)
        enabled = [m for m in clean_members if m['enabled']]
        if not enabled:
            raise ValueError(f'组合「{name}」没有启用的成员')
        if sum(m['weight'] for m in enabled) <= 0:
            raise ValueError(f'组合「{name}」启用成员的权重之和必须大于 0')
        out.append({'name': name, 'enabled': item.get('enabled') is not False,
                    'members': clean_members})
    return out


def find(raw: object, name: object) -> dict | None:
    """按名字查一个**启用**的组合；找不到或未启用返回 None。"""
    target = str(name or '').strip().lower()
    if not target:
        return None
    for combo in load(raw):
        if combo['name'] == target and combo['enabled']:
            return combo
    return None


def enabled_members(combo: dict) -> list[dict]:
    return [m for m in combo.get('members') or [] if m.get('enabled')]


def pick(members: list[dict], currents: dict[str, float]) -> tuple[dict | None, list[dict]]:
    """选一个成员，并给出失败时可依次尝试的其余成员。

    `currents` 是**每个成员**的 SWRR 游标（原地更新，只改被选中的那个）。
    其余成员按「权重降序 → 名字」排序：权重高的先作为重试候选，结果稳定可测。

    返回 `(选中的成员, 其余成员)`；`members` 为空时返回 `(None, [])`。
    """
    if not members:
        return None, []
    total = sum(m['weight'] for m in members)
    best = max(members, key=lambda m: currents.get(_member_key(m), 0.0) + m['weight'])
    key = _member_key(best)
    currents[key] = currents.get(key, 0.0) + best['weight'] - total
    return best, alternatives(members, best)


def alternatives(members: list[dict], chosen: dict) -> list[dict]:
    """除 `chosen` 外的其余成员，按「权重降序 → 成员键」排好。

    失败切换的候选顺序与选路一致；会话粘性命中时也要用它算重试候选，
    所以单独抽出来，避免两处排序规则漂移。
    """
    rest = [m for m in members if m is not chosen]
    rest.sort(key=lambda m: (-m['weight'], _member_key(m)))
    return rest


def names(raw: object) -> list[str]:
    """全部启用组合的名字（供 `/v1/models` 与界面使用）。"""
    return [c['name'] for c in load(raw) if c['enabled'] and enabled_members(c)]
