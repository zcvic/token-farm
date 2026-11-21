"""Kilo（kilo.ai）平台实现。导入即注册。

协议参考官方客户端 `Kilo-Org/kilocode`（`packages/kilo-gateway`）：设备授权码
登录拿到一年期的 API token，对话走 `ai-gateway.kilo.ai/api/v1`（OpenAI 兼容，
模型目录是 OpenRouter schema）。本包不引入第三方依赖。
"""
from __future__ import annotations

from .adapter import KiloAdapter, adapter

__all__ = ['KiloAdapter', 'adapter']
