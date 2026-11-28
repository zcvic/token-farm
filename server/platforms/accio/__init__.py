"""Accio（阿里国际站 AI 电商工作台）平台实现。导入即注册。

`realm_aware=True`：国内站（`acciocn/`）与国际站（`acciocom/`）的登录页域名
不同，旧式 `accio/` 仍可调用。

  · `constants.py` —— 常量（网关 / 登录页 / 指纹 / 超时）；
  · `protocol.py`  —— 纯函数（PKCE / 信封 / 错误分级 / 消息序列化）；
  · `catalog.py`   —— 模型目录归一（展示名 → slug，倍率排序）；
  · `store.py`     —— 账号文件（含必须持久化的 `utdid` / `sgcookie` / 团队）；
  · `client.py`    —— 上游 HTTP（兑换 / 续期 / 身份 / 目录 / 对话 / 积分）；
  · `login.py`     —— OAuth 2.0 + PKCE 的「打开授权页 + 本机回环 / 粘回调」；
  · `chat.py`      —— Gemini 风格帧 → OpenAI chunk（含工具分片合并）；
  · `adapter.py`   —— 选号、续期、冷却、缓存与对话出口。

**没有签到**：Accio 的每日积分由登录触发服务端发放，没有可调用的签到端点。

协议事实与待实测项见 `deploy/accio-ref.txt`，接入说明见 `docs/accio.md`。
"""
from __future__ import annotations

from .adapter import AccioAdapter, adapter

__all__ = ['AccioAdapter', 'adapter']
