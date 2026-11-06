"""WorkBuddy 的「组合成员」适配器（不对外暴露为平台）。

导入即注册（见 `platforms/__init__.py`）。它存在的唯一理由是：组合的成员必须是
适配器，而 WorkBuddy 原本走的是网关里的直连反代。细节见 `adapter.py` 的文件头。
"""
from __future__ import annotations

from .adapter import WorkBuddyMemberAdapter, adapter

__all__ = ['WorkBuddyMemberAdapter', 'adapter']
