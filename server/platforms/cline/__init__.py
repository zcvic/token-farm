"""Cline（cline.bot）平台实现。导入即注册。

协议参考社区实现 `luawei1/cline2api`（Cloudflare Worker 版）：WorkOS 设备授权码
换 refresh token，`Bearer workos:<access>` 直连 `api.cline.bot/api/v1`，对话需
伪装 Cline 客户端头。本包不引入第三方依赖。
"""
from __future__ import annotations

from .adapter import ClineAdapter, adapter

__all__ = ['ClineAdapter', 'adapter']
