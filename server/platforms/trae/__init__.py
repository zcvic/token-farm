"""Trae 平台实现。导入即注册（见 `platforms/__init__.py`）。"""
from __future__ import annotations

from .adapter import TraeAdapter, adapter

__all__ = ['TraeAdapter', 'adapter']
