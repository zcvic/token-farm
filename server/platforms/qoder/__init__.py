"""Qoder 平台实现：内置网关（vendor 自 qoder2api-hub）。导入即注册。"""
from __future__ import annotations

from .adapter import QoderAdapter, adapter

__all__ = ['QoderAdapter', 'adapter']
