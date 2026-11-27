"""AutoClaw（智谱 / Z.ai）平台实现。导入即注册。

`realm_aware=True`：国内站（`autoclawcn/`）与国际站（`autoclawcom/`）是两套
互相独立的账号体系，旧式 `autoclaw/` 仍可调用。

  · `constants.py` / `protocol.py` / `catalog.py` —— 常量、纯函数、目录归一；
  · `store.py` —— 账号文件（含必须持久化的 `deviceId`）；
  · `client.py` —— 上游 HTTP（登录 / 刷新 / 目录 / 对话 / 积分 / 任务）；
  · `login.py` —— 国内短信验证码、国际 OAuth「打开授权页 + 粘回调」；
  · `adapter.py` —— 选号、刷新、冷却、缓存与对话出口。

协议事实与待实测项见 `deploy/autoclaw-ref.txt`，接入说明见 `docs/autoclaw.md`。
"""
from __future__ import annotations

from .adapter import AutoclawAdapter, adapter

__all__ = ['AutoclawAdapter', 'adapter']
