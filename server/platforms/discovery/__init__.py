"""Discovery（Intern TokenPlan）平台实现。导入即注册。

协议参考社区实现 `ZFXing-lite/discovery2api`（MIT）：凭证是一把 Intern
TokenPlan API Key，`Bearer` 直连 `discovery-api.intern-ai.org.cn`，OpenAI
兼容；上游没有余额接口，额度只能靠 `GET /v1/models` 探。本包不引入第三方依赖。
"""
from __future__ import annotations

from .adapter import DiscoveryAdapter, adapter

__all__ = ['DiscoveryAdapter', 'adapter']
