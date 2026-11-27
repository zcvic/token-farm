"""华为云 CodeArts 码道（CodeArts Agent）平台实现。导入即注册。

协议事实来自官方客户端（VS Code 系 Electron 应用）逆向与真机抓包，参考社区
实现 `codearts2api`（MIT，只作协议事实来源，未 vendor 任何源码）：

  · 登录是华为云 OAuth2 + PKCE + **DPoP**，token 端点在
    `sts.cn-north-4.myhuaweicloud.com/v1/oauth2/tokens`；
  · 换到的是 STS 临时凭证（AK/SK + `security_token`，约 1 小时），每个业务
    请求都要再做一次 `SDK-HMAC-SHA256` 签名；
  · `refresh_token` 一次性轮换，与 `client_id` + DPoP 私钥三者绑定；
  · 对话走 `/api/v2/chat/completions`（原生 OpenAI 形状），SSE 里的 `text`
    是**累计全文**；
  · 模型目录三路（agent-center / builtin / 限时福利），福利模型按账号判定，
    对话时要带 `maas_type: benefit`。

本包为**原生重写**，只依赖 httpx 与标准库。
"""
from __future__ import annotations

from .adapter import CodeArtsAdapter, adapter

__all__ = ['CodeArtsAdapter', 'adapter']
