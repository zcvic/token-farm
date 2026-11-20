"""DuMate（百度搭子）平台实现。导入即注册。

协议参考 `dumate2api`（MIT）：网页 Cookie 换 Bearer token，模型网关
`dumate-svc.baidu.com/gateway/apis/v1` OpenAI 兼容。本包不引入第三方依赖。
"""
from __future__ import annotations

from .adapter import DumateAdapter, adapter

__all__ = ['DumateAdapter', 'adapter']
