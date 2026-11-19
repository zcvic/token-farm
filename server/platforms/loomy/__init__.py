"""Loomy（讯飞星火 Loomy）平台实现。导入即注册。

协议参考 `loomy2api`（MIT）：账号服务 `account.xfinfr.com` + 模型网关
`loomyad.xunfei.cn/api/v1`。两者都按客户端行为实现，本包不引入第三方
依赖（RSA 用纯标准库 `pow()`，签名用 `hmac`/`hashlib`）。
"""
from __future__ import annotations

from .adapter import LoomyAdapter, adapter

__all__ = ['LoomyAdapter', 'adapter']
