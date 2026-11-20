"""MiniMax Code（mcode）平台实现。导入即注册。

协议参考 `@magpie-community/opencode-minimax-auth`（MIT）：OAuth 设备码登录、
refresh token 轮换、Anthropic Messages 对话端点、`minimax-cloud` 签到 / 积分。
本包不引入第三方依赖（PDF 不涉及，签名用标准库 `hashlib`）。
"""
from __future__ import annotations

from .adapter import MinimaxAdapter, adapter

__all__ = ['MinimaxAdapter', 'adapter']
