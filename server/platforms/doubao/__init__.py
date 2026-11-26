"""豆包（Doubao）平台实现。导入即注册。

协议参考社区实现 `yyydsz/doubao2api`（MIT）：凭证是**网页 Cookie**，对话走
`POST https://www.doubao.com/chat/completion`（固定 SSE，没有 `stream` 开关），
签到与额度走 `alice/*` 两个网页接口。本包不引入第三方依赖，也没有浏览器 /
签名依赖：上游不校验 `a_bogus`，纯 HTTP 即可（扫码登录才需要 Passport 签名，
因此本平台只支持抓包导入 Cookie）。
"""
from __future__ import annotations

from .adapter import DoubaoAdapter, adapter

__all__ = ['DoubaoAdapter', 'adapter']
