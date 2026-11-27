"""多平台适配层。

`import server.platforms` 即完成所有内置平台的注册——网关与管理端只通过
`registry` 找到它们，不直接 import 具体平台，避免新增平台时改到路由代码。

当前平台：

  · WorkBuddy —— 账号池由随包的 `workbuddy2api`（Go）持有，网关对它的转发保持
    原样（模型名不带平台前缀）。本包里的 `workbuddy` 适配器**只给组合用**：
    它不出现在平台下拉里，也不能被 `<name>/<model>` 前缀命中（`routable=False`）。
  · Trae —— 原生实现，模型名以 `traecn/` / `traecom/` 为区域前缀，旧式
    `trae/` 继续兼容。
  · Qoder —— 内置网关实现（vendor 自 `qoder2api-hub`），模型名以
    `qodercn/` / `qodercom/` 为区域前缀（旧式 `qoder/` 兼容），国内版 /
    国际版账号由内置网关持有。
  · Loomy —— 原生实现（讯飞星火 Loomy），单区域平台，模型名以 `loomy/`
    为前缀。账号服务 `account.xfinfr.com`，模型网关 OpenAI 兼容。
  · DuMate —— 原生实现（百度搭子），单区域平台，模型名以 `dumate/` 为前缀。
    凭证是网页 Cookie（`BDUSS`），登录支持百度 Passport 扫码或手工粘贴
    Cookie，模型网关 OpenAI 兼容。
  · MiniMax Code —— 原生实现（mcode），分国内 / 国际两站，模型名以
    `minimaxcn/` / `minimaxcom/` 为前缀。凭证是 OAuth refresh token，
    对话端点是 Anthropic Messages 兼容（适配器内部翻译成 OpenAI 形状）。
  · Discovery —— 原生实现（Intern TokenPlan），单区域平台，模型名以
    `discovery/` 为前缀。凭证是一把 API Key，`Bearer` 直连，OpenAI 兼容；
    上游没有余额接口，额度靠 `GET /v1/models` 探。
  · Kilo —— 原生实现（kilo.ai），单区域平台，模型名以 `kilo/` 为前缀。
    凭证是设备授权码换来的一年期 API token，对话走 `ai-gateway.kilo.ai`
    （OpenRouter schema 的 OpenAI 兼容层），余额是**美元**。
  · Cline —— 原生实现（cline.bot），单区域平台，模型名以 `cline/` 为前缀。
    凭证是 WorkOS 设备码换来的 refresh token（**一次性轮换**），对话要带
    Cline 客户端伪装头；免费档只认流式，适配器内部聚合。
  · 豆包（doubao）—— 原生实现（www.doubao.com），单区域平台，模型名以
    `doubao/` 为前缀。凭证是**网页 Cookie**（抓包导入，没有扫码登录）；
    对话端点固定返回 SSE，适配器内部翻译成 OpenAI chunk；上游不返回 usage，
    统计按「字符 ÷ 1.7」估算。
  · WPS 灵犀（lingxi）—— 原生实现（lingxi.wps.cn），单区域平台，模型名以
    `lingxi/` 为前缀。凭证是**网页 Cookie**（`wps_sid`，抓包导入，没有扫码
    登录）。一次对话是「建会话 → 领 run → SSE 对话」三步握手；正文在
    `choices[].text`、没有 `[DONE]`，适配器内部翻译成 OpenAI chunk。
  · 商汤小浣熊（raccoon）—— 原生实现（xiaohuanxiong.com），单区域平台，
    模型名以 `raccoon/` 为前缀。凭证是 JWT（access + **一次性 refresh**），
    对话是 OpenAI Chat Completions；目录在线拉取，签到靠触发桌面端
    `setting_info` 后按积分账单核对。
  · 华为云 CodeArts 码道（codearts）—— 原生实现（snap-access.cn-north-4），
    单区域平台，模型名以 `codearts/` 为前缀。凭证是 STS 临时 AK/SK +
    `security_token`（每个请求做 `SDK-HMAC-SHA256` 签名），`refresh_token`
    **一次性轮换**且与 DPoP 私钥绑定；对话是 OpenAI 形状，但 SSE 里的 `text`
    是**累计全文**，适配器内部改成增量 chunk。模型目录按账号三路合并，限时
    福利模型要带 `maas_type: benefit`。
  · 组合（combo）—— **虚拟平台**：没有账号，只把 `combo/<name>` 按权重
    分发给上面这些真实平台。因此不在界面里露出（`ui_visible=False`）。
"""
from __future__ import annotations

from . import registry
from .base import (ChatContext, PlatformAdapter, PlatformChat, PlatformError,
                   normalize_db_realm, realm_prefix_suffix)
from .registry import all_adapters, get, names, register, route_model
from .sse import openai_sse
from .workbuddy import adapter as workbuddy_adapter
from .trae import adapter as trae_adapter
from .qoder import adapter as qoder_adapter
from .loomy import adapter as loomy_adapter
from .dumate import adapter as dumate_adapter
from .minimax import adapter as minimax_adapter
from .discovery import adapter as discovery_adapter
from .kilo import adapter as kilo_adapter
from .cline import adapter as cline_adapter
from .doubao import adapter as doubao_adapter
from .lingxi import adapter as lingxi_adapter
from .raccoon import adapter as raccoon_adapter
from .codearts import adapter as codearts_adapter
from .combo import adapter as combo_adapter

# 注册内置平台。放在 import 之后而不是模块顶层逐个 register，是为了让
# 「有哪些平台」集中在一处可见。顺序 = 界面里的顺序（不可见的也按此排序）。
register(workbuddy_adapter)
register(trae_adapter)
register(qoder_adapter)
register(loomy_adapter)
register(dumate_adapter)
register(minimax_adapter)
register(discovery_adapter)
register(kilo_adapter)
register(cline_adapter)
register(doubao_adapter)
register(lingxi_adapter)
register(raccoon_adapter)
register(codearts_adapter)
register(combo_adapter)

__all__ = [
    'ChatContext', 'PlatformAdapter', 'PlatformChat', 'PlatformError',
    'all_adapters', 'get', 'names', 'register', 'route_model', 'registry',
    'openai_sse', 'normalize_db_realm', 'realm_prefix_suffix',
]
