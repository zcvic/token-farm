"""多平台适配层。

`import server.platforms` 即完成所有内置平台的注册——网关与管理端只通过
`registry` 找到它们，不直接 import 具体平台，避免新增平台时改到路由代码。

当前平台：

  · WorkBuddy —— 不在本包里：它由随包的 `workbuddy2api`（Go）持有账号池，
    网关对它的转发保持原样（模型名不带平台前缀）。
  · Trae —— 原生实现，模型名以 `trae/` 为前缀。
"""
from __future__ import annotations

from . import registry
from .base import PlatformAdapter, PlatformChat, PlatformError
from .registry import all_adapters, get, names, register, route_model
from .sse import openai_sse
from .trae import adapter as trae_adapter

# 注册内置平台。放在 import 之后而不是模块顶层逐个 register，是为了让
# 「有哪些平台」集中在一处可见。
register(trae_adapter)

__all__ = [
    'PlatformAdapter', 'PlatformChat', 'PlatformError',
    'all_adapters', 'get', 'names', 'register', 'route_model', 'registry',
    'openai_sse',
]
