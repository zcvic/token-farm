"""模型组合平台（虚拟）。导入即注册（见 `platforms/__init__.py`）。

它没有账号、没有凭据，只是把 `combo/<name>` 这个模型名翻译成「按权重挑一个
真实成员平台」。因此它在界面上不可见（`ui_visible=False`）。
"""
from __future__ import annotations

from .adapter import ComboAdapter, adapter

__all__ = ['ComboAdapter', 'adapter']
