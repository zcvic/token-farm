"""Accio（阿里国际站 AI 电商工作台）协议的常量。

协议事实来自 2026-11 对官方客户端与社区实现的逆向 / 真机实测，逐项来源与
「未实测」标注见 `deploy/accio-ref.txt`。本文件只放**稳定值**：地址、路径、
客户端指纹与超时；签名 / 请求体 / 错误分级在 `protocol.py`，目录归一在
`catalog.py`。

## 两个区域

  · `cn`     —— 国内站，登录页 `https://www.accio-ai.com/login`；
  · `global` —— 国际站，登录页 `https://zh.accio.com/login`。

**只有登录页分区域**：两侧的网关都是 `https://phoenix-gw.alibaba.com`，
token 也通用，区域由服务端按账号下发（`userinfo.routeRegion`）。登录前无法
自动判断区域，所以发起登录时必须由用户先选区域。

## 积分与签到

Accio 没有「每日签到」端点。登录会触发按日发放、24 小时不累积的额度，新
用户另有一次性的 500 积分（7 天有效）——这些都是服务端行为，**没有**可以
点的按钮，因此 `supports_checkin = False`。
"""
from __future__ import annotations

# ── 区域 ────────────────────────────────────────────────────
REALMS = ('cn', 'global')
DEFAULT_REALM = 'cn'
REALM_LABELS = {'cn': '国内版', 'global': '国际版'}

# ── 网关 / 登录页 ───────────────────────────────────────────
# 两个区域共用同一个网关；区域只决定登录页域名。
GATEWAY = 'https://phoenix-gw.alibaba.com'
LOGIN_BASE = {
    'cn': 'https://www.accio-ai.com',
    'global': 'https://zh.accio.com',
}
LOGIN_PATH = '/login'
CALLBACK_PATH = '/auth/callback'

# ── 客户端指纹 ──────────────────────────────────────────────
CLIENT_ID = 'accio-work'      # 公开客户端：没有 client_secret
APP_KEY = 'OneChat'
APP_VERSION = '0.34.4'
LANGUAGE = 'zh'
TTID_PREFIX = 'ttid_'
PACKAGE_REGION = {'cn': 'CN', 'global': 'GLOBAL'}

# ── 路径 ────────────────────────────────────────────────────
TOKEN_EXCHANGE_PATH = '/api/oauth/token'
REFRESH_PATH = '/api/auth/refresh_token'
USERINFO_PATH = '/api/auth/userinfo'
CATALOG_PATH = '/api/llm/config/v2'
CHAT_PATH = '/api/adk/llm/generateContent'
SUBSCRIPTION_PATH = '/api/entitlement/currentSubscription'
QUOTA_PATH = '/api/entitlement/quota'

# ── 超时 / 寿命 ─────────────────────────────────────────────
# 单次请求上限（整个响应，不是段间空闲）。
REQUEST_TIMEOUT = 30
# SSE 空闲超时：生成大工具参数期间上游可能长时间不 flush，裸读会无限挂起。
SSE_IDLE_TIMEOUT = 120
# 登录回调窗口（10 分钟）。
LOGIN_TIMEOUT = 600
# token 提前这么多秒视为过期。
TOKEN_SKEW_SECONDS = 120
# 一次用户请求最多尝试的账号数（含首号），只为防病态池。
MAX_ACCOUNTS_TRY = 8
# 429 限流冷却：1 小时。
RATE_LIMIT_COOLDOWN = 3600
# 刷新失败先冷一会儿，避免上游抖动时每次请求都去轮换。
REFRESH_FAIL_COOLDOWN = 60

# ── 业务码 ──────────────────────────────────────────────────
# 流内错误帧的成功哨兵；其余非 200 码都算失败。
ERROR_CODE_OK = '200'
# 额度 / 认证失败的流内码（参考实现映射成 QUOTA_EXCEEDED）。
QUOTA_ERROR_CODE = '402'

# ── 积分桶（顺序固定：每日 → 每月 → 推荐）──────────────────
CREDIT_BUCKETS = (
    ('daily', '每日积分'),
    ('monthly', '每月积分'),
    ('referral', '推荐积分'),
)
CREDIT_UNIT = 'credit'
