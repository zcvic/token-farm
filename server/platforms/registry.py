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


def route_model(model: object) -> tuple[str, str] | None:
    """把 `trae/glm-5.2` 解析成 `('trae', 'glm-5.2')`。

    **只认已注册的平台名**：模型名里出现斜杠并不罕见（例如某些自建网关的
    `owner/model` 写法），把任意前缀都当平台会误伤 WorkBuddy 的存量模型。
    没有命中就返回 None，调用方按原行为继续走 WorkBuddy。
    """
    raw = str(model or '').strip()
    if '/' not in raw:
        return None
    head, _, rest = raw.partition('/')
    adapter = get(head)
    if adapter is None or not rest.strip():
        return None
    return adapter.name, rest.strip()
