"""商汤小浣熊（Raccoon / xiaohuanxiong.com）平台实现。导入即注册。

协议参考社区实现 `haoxinyu1/xiaohuanxiong2api`（Go）与真机抓包：网页 /
桌面客户端共用 `https://xiaohuanxiong.com` 后端，JWT access + 一次性 refresh，
OpenAI Chat Completions 形状的对话接口。本包为**原生重写**，不引入第三方依赖，
也没有 vendor 任何参考仓源码。
"""
from __future__ import annotations

from .adapter import RaccoonAdapter, adapter

__all__ = ['RaccoonAdapter', 'adapter']
