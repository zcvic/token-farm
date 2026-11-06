"""平台注册表与模型前缀解析。

网关只需要两个问题的答案：

  1. 这个模型名属于哪个平台？（`route_model`）
  2. 这个平台有哪些模型？（`list_models` 时逐个询问）

注册在 `platforms/__init__.py` 里完成：导入即注册，避免「忘了调 register」
这种只在运行时才暴露的问题。
"""
from __future__ import annotations

from .base import PlatformAdapter

_ADAPTERS: dict[str, PlatformAdapter] = {}


def register(adapter: PlatformAdapter) -> PlatformAdapter:
    """注册（或替换）一个平台。返回入参，便于在模块末尾一行完成注册。"""
    key = str(adapter.name or '').strip().lower()
    if not key:
        raise ValueError('平台适配器必须有 name')
    if not all(ch.isalnum() or ch in '-_' for ch in key):
        raise ValueError(f'平台名 {key!r} 只能含字母数字与 -_')
    _ADAPTERS[key] = adapter
    return adapter


def get(name: str) -> PlatformAdapter | None:
    return _ADAPTERS.get(str(name or '').strip().lower())


def all_adapters() -> list[PlatformAdapter]:
    return list(_ADAPTERS.values())


def names() -> list[str]:
    return list(_ADAPTERS)


def route_model(model: object) -> tuple[str, str, str | None] | None:
    """把平台模型名解析成 `(平台, 裸模型, 版本提示)`。

    两种前缀都认：

      · 区域化：`traecn/glm-5.2` → `('trae', 'glm-5.2', 'cn')`，
        `traecom/glm-5.2` → `('trae', 'glm-5.2', 'global')`；
      · 旧式：`trae/glm-5.2` → `('trae', 'glm-5.2', None)`。

    版本提示是 db 口径（`cn` / `global`），交给适配器决定怎么选号。旧式前缀
    不携带版本，行为与改造前完全一致——这是给存量客户端 / 白名单留的兼容入口。

    **只认已注册的平台名**：模型名里出现斜杠并不罕见（例如某些自建网关的
    `owner/model` 写法），把任意前缀都当平台会误伤 WorkBuddy 的存量模型。
    没有命中就返回 None，调用方按原行为继续走 WorkBuddy。

    `routable=False` 的适配器（WorkBuddy 成员）**不参与解析**：它只给组合
    内部调用，若让前缀生效就等于凭空多出一个与直连语义重复的入口。
    """
    raw = str(model or '').strip()
    if '/' not in raw:
        return None
    head, _, rest = raw.partition('/')
    bare = rest.strip()
    if not bare:
        return None
    head = head.strip().lower()
    adapter = get(head)
    if adapter is not None:
        if not adapter.routable:
            return None
        return adapter.name, bare, None
    # 区域化前缀：平台名 + `cn` / `com`。放在精确匹配之后，注册名永远优先。
    for candidate in all_adapters():
        if not candidate.routable:
            continue
        realm = candidate.realm_of_prefix(head)
        if realm is not None:
            return candidate.name, bare, realm
    return None
