"""MiniMax Code（mcode）协议的常量。

MiniMax Code 是**订阅制**平台：用订阅了 M Plan 的账号登录，模型走
`agent.minimax.cn` 上的 Anthropic Messages 兼容端点，不需要 API Key。

两套站点（`realm`）：

  · `cn`     —— 国内站（account.minimax.cn / agent.minimax.cn）；
  · `global` —— 国际站（account.minimax.io / agent.minimax.io）。

两边的 token **不通用**：国际站会 307 到国内站，而国内站拒绝国际 token。

凭证是 OAuth 的 `refresh_token`（设备码登录发放）。refresh token 是**一次性**
的：换过一次就作废，必须把新的一起落盘（见 `store.py` 的说明）。
"""
from __future__ import annotations

from typing import Any

# ── 站点 ────────────────────────────────────────────────────
SITES: dict[str, dict[str, str]] = {
    'cn': {
        'realm': 'cn',
        'label': '国内版',
        'account': 'https://account.minimax.cn',
        'llm': 'https://agent.minimax.cn',
        'agent': 'https://agent.minimaxi.com',
        'agent_fallback': 'https://agent.minimax.cn',
        'platform': 'https://www.minimaxi.com',
        'region': 'cn',
        'lang': 'zh',
    },
    'global': {
        'realm': 'global',
        'label': '国际版',
        'account': 'https://account.minimax.io',
        'llm': 'https://agent.minimax.io',
        'agent': 'https://agent.minimax.io',
        'agent_fallback': '',
        'platform': 'https://platform.minimax.io',
        'region': 'en',
        'lang': 'en',
    },
}
DEFAULT_REALM = 'cn'
REALMS = ('cn', 'global')

# ── OAuth（设备码，PKCE）────────────────────────────────────
CLIENT_ID = 'mcode-public'
SCOPE = 'agent.default'
AUDIENCE = 'agent-backend'
DEVICE_GRANT = 'urn:ietf:params:oauth:grant-type:device_code'
DEVICE_CODE_PATH = '/oauth2/device/code'
TOKEN_PATH = '/oauth2/token'

# ── 账号接口签名 ────────────────────────────────────────────
# MiniMax Code 自己的 `x-signature` / `yy` 签名密钥（客户端常量）。
SIGN_SECRET = 'I*7Cf%WZ#S&%1RlZJ&C2'
SIGN_VERSION_CODE = '22201'
SIGN_BIZ_ID = '3'
SIGN_APP_ID = '3001'

# 签到 / 积分明细等 minimax-cloud 端点要求的查询参数（桌面端 0.6.12 口径）。
CHECKIN_QUERY = {
    'device_platform': 'web',
    'is_desktop': '1',
    'desktop_version': '0.6.12',
}

# ── 路径 ────────────────────────────────────────────────────
IDENTITY_PATH = '/v1/api/user/info'
WORKSPACE_PATH = '/matrix/api/v1/user/get_user_extra_info'
MEMBERSHIP_PATH = '/matrix/api/v1/commerce/get_membership_info'
CHECKIN_STATUS_PATH = '/minimax-cloud/api/v1/signin/status'
CHECKIN_CLAIM_PATH = '/minimax-cloud/api/v1/signin/claim'
CREDIT_DETAILS_PATH = '/minimax-cloud/api/v1/credit/details'
PLAN_QUOTA_PATH = '/backend/account/token_plan/remains_percent'
PLAN_QUOTA_FALLBACK = '/v1/api/openplatform/coding_plan/remains'
MODELS_PATH = '/mavis/api/v1/models'
CHAT_PATH = '/mavis/api/v1/llm/v1/messages'

# ── 超时 ────────────────────────────────────────────────────
TOKEN_TIMEOUT = 30
ACCOUNT_TIMEOUT = 15
MODELS_TIMEOUT = 10
# token 提前这么久视为过期（与参考实现一致的 2 分钟）。
TOKEN_SKEW_SECONDS = 120
# 刷新响应没给 `expires_in` 时的兜底寿命。
TOKEN_FALLBACK_SECONDS = 3600

# 设备码登录：没有上限时的兜底（10 分钟）。
DEVICE_TTL_SECONDS = 600

# ── 模型快照（离线兜底）──────────────────────────────────────
# 与参考实现 0.2.9 的 MiniMax Code 列表一致；在线目录拿不到时用它。
# `limit.output` 是客户端展示值，实测上限见 OUTPUT_CAPS。
DEFAULT_MODELS: list[dict[str, Any]] = [
    {
        'id': 'MiniMax-M3.1-Flash-Preview', 'name': 'M3.1-Flash-Preview',
        'context_length': 1048576, 'max_tokens': 524288,
        'modalities': ['text', 'image', 'video'],
        'reasoning': True, 'efforts': ['low', 'medium', 'high', 'xhigh', 'max'],
    },
    {
        'id': 'MiniMax-M3', 'name': 'MiniMax-M3',
        'context_length': 1048576, 'max_tokens': 524288,
        'modalities': ['text', 'image', 'video'],
        'reasoning': True, 'efforts': [],
    },
    {
        'id': 'MiniMax-M2.7', 'name': 'MiniMax-M2.7',
        'context_length': 200000, 'max_tokens': 128000,
        'modalities': ['text'], 'reasoning': True, 'efforts': [],
    },
    {
        'id': 'MiniMax-M2.7-highspeed', 'name': 'MiniMax-M2.7-highspeed',
        'context_length': 200000, 'max_tokens': 128000,
        'modalities': ['text'], 'reasoning': True, 'efforts': [],
    },
]

# 端点真正接受的上限（参考实现里用大请求实测过：M3 家族 1,048,576 / 524,288）。
# 模型目录里的 512K/128K 是客户端的保守声明，照着它钳会把输出预算砍掉一半。
OUTPUT_CAPS: dict[str, int] = {
    'MiniMax-M3.1-Flash-Preview': 524288,
    'MiniMax-M3.1-Flash': 524288,
    'MiniMax-M3': 524288,
}
CONTEXT_CAPS: dict[str, int] = {
    'MiniMax-M3.1-Flash-Preview': 1048576,
    'MiniMax-M3.1-Flash': 1048576,
    'MiniMax-M3': 1048576,
}
