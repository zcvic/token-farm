"""多平台适配层。

`import server.platforms` 即完成所有内置平台的注册——网关与管理端只通过
`registry` 找到它们，不直接 import 具体平台，避免新增平台时改到路由代码。

当前平台：

  · WorkBuddy —— 账号池由随包的 `workbuddy2api`（Go）持有，网关对它的转发保持
    原样（模型名不带平台前缀）。本包里的 `workbuddy` 适配器**只给组合用**：
    它不出现在平台下拉里，也不能被 `<name>/<model>` 前缀命中（`routable=False`）。
  · Trae —— 原生实现，模型名以 `traecn/` / `traecom/` 为区域前缀，旧式
    `trae/` 继续兼容。
  · Qoder —— 内置网关实现（vendor 自 `qoder2api-hub`），模型名以
    `qodercn/` / `qodercom/` 为区域前缀（旧式 `qoder/` 兼容），国内版 /
    国际版账号由内置网关持有。
  · 组合（combo）—— **虚拟平台**：没有账号，只把 `combo/<name>` 按权重
    分发给上面这些真实平台。因此不在界面里露出（`ui_visible=False`）。
"""
from __future__ import annotations

from . import registry
from .base import (ChatContext, PlatformAdapter, PlatformChat, PlatformError,
                   normalize_db_realm, realm_prefix_suffix)
from .registry import all_adapters, get, names, register, route_model
from .sse import openai_sse
from .workbuddy import adapter as workbuddy_adapter
from .trae import adapter as trae_adapter
from .qoder import adapter as qoder_adapter
from .combo import adapter as combo_adapter

# 注册内置平台。放在 import 之后而不是模块顶层逐个 register，是为了让
# 「有哪些平台」集中在一处可见。顺序 = 界面里的顺序（不可见的也按此排序）。
register(workbuddy_adapter)
register(trae_adapter)
register(qoder_adapter)
register(combo_adapter)

__all__ = [
    'ChatContext', 'PlatformAdapter', 'PlatformChat', 'PlatformError',
    'all_adapters', 'get', 'names', 'register', 'route_model', 'registry',
    'openai_sse', 'normalize_db_realm', 'realm_prefix_suffix',
]
