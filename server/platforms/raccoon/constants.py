"""商汤小浣熊（Raccoon / xiaohuanxiong.com）协议常量。

协议事实来自社区实现与真机抓包（见 `deploy/raccoon-ref.txt`）：

  · 网页 / 桌面客户端共用同一套后端：`https://xiaohuanxiong.com`；
  · 鉴权是 JWT：`access_token` 约 3 小时、`refresh_token` 约 30 天，
    刷新接口**同时轮换**两者，且 refresh 是**单次使用**的；
  · LLM 网关 `GET /api/web/llm/v2/model_catalog` 与
    `POST /api/web/llm/v2/chat/completions`，OpenAI Chat Completions 形状；
  · 积分接口 `GET /api/web/points/v1/balance`，账单 `GET .../bills`；
  · 每日积分没有独立领取接口，桌面端读一次
    `GET /api/web/office/v3/setting_info` 就会触发发放。

这里只放地址、稳定常量与离线兜底；协议解析在 `client.py`，账号存储策略在
`store.py`，选号 / 冷却在 `adapter.py`。
"""
from __future__ import annotations

# ── 上游 ────────────────────────────────────────────────────
ORIGIN = 'https://xiaohuanxiong.com'
LLM_V2 = f'{ORIGIN}/api/web/llm/v2'
LLM_V1 = f'{ORIGIN}/api/web/llm/v1'
AUTH_BASE = f'{ORIGIN}/api/web/auth/v1'
POINTS_BASE = f'{ORIGIN}/api/web/points/v1'
OFFICE_BASE = f'{ORIGIN}/api/web/office/v3'

CATALOG_PATH = '/model_catalog'
CHAT_PATH = '/chat/completions'
REFRESH_PATH = '/refresh'
ENTITLEMENT_PATH = '/entitlement_info'
BALANCE_PATH = '/balance'
BILLS_PATH = '/bills'
SETTING_INFO_PATH = '/setting_info'

# 浏览器的 UA 是上游风控的判据之一：非浏览器 / 非官方客户端更容易被
# 200108 拦。这里用 Windows Chrome 的常见值，与参考实现一致。
USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
              'AppleWebKit/537.36 (KHTML, like Gecko) '
              'Chrome/131.0.0.0 Safari/537.36')

# 桌面端接口（每日积分触发等）会校验客户端头。
CLIENT_PLATFORM = 'desktop-windows'
CLIENT_VERSION = 'v1.0.0'
RACCOON_LANGUAGE = 'zh'

# ── 令牌寿命（上游不给到期时间时兜底）──────────────────────
ACCESS_TTL_SECONDS = 3 * 60 * 60
REFRESH_TTL_SECONDS = 30 * 24 * 60 * 60
# 提前 5 分钟换新：避免请求刚好卡在 JWT 过期边界。
TOKEN_SKEW_SECONDS = 5 * 60
# 刷新失败（非鉴权类）后的冷却：一次性 refresh token 不能每个请求都打。
REFRESH_FAIL_COOLDOWN = 60

# ── 超时（秒）──────────────────────────────────────────────
CATALOG_TIMEOUT = 20.0
REFRESH_TIMEOUT = 20.0
CHAT_TIMEOUT = 600.0
CHAT_IDLE_TIMEOUT = 120.0
BALANCE_TIMEOUT = 20.0
BILLS_TIMEOUT = 20.0

# ── 对话默认值 ─────────────────────────────────────────────
# 参考实现实测：给上游预留过大的输出预算更容易触发风控，默认 16k 是折中；
# 客户端显式传 `max_tokens` 时仍以客户端为准。
DEFAULT_MAX_TOKENS = 16_384
DEFAULT_CONTEXT_WINDOW = 128_000
# 上游没有目录时（新部署、目录接口失败）的兜底模型。
DEFAULT_MODEL = 'sn-sensenova-6-8-flash-lite'

# 语义上等于「额度耗尽 / 账号被风控」的业务码。
RATE_LIMIT_CODES = {200108}
AUTH_CODES = {200003}
LOGIN_EXPIRED_CODES = {200035, 200822}

# 200108 是账号级风控，不是普通限流：恢复中位约 25 分钟。
RISK_CONTROL_COOLDOWN = 25 * 60

# ── 模型目录（离线兜底）────────────────────────────────────
# 2026-09 真机可见模型；新部署在第一次成功拉目录后会被线上目录覆盖。
DEFAULT_MODELS: list[dict] = [
    {'id': 'sn-sensenova-6-8-flash-lite',
     'name': 'SenseNova-6.8-Flash-Lite', 'context_length': 256000,
     'max_tokens': 63999, 'tags': ['general', 'chat', 'vision'],
     'visible': True, 'reasoning': False, 'free': True},
    {'id': 'sn-glm-5-3', 'name': 'GLM-5-3', 'context_length': 1000000,
     'max_tokens': 100000, 'tags': ['code', 'reasoning'], 'visible': True,
     'reasoning': True, 'free': False},
    {'id': 'sn-kimi-k3', 'name': 'Kimi-K3', 'context_length': 1000000,
     'max_tokens': 100000, 'tags': ['general', 'vision', 'reasoning'],
     'visible': True, 'reasoning': True, 'free': False},
    {'id': 'sn-glm-5-3-flash', 'name': 'GLM-5-3-Flash',
     'context_length': 1000000, 'max_tokens': 100000,
     'tags': ['chat', 'fast'], 'visible': True, 'reasoning': True,
     'free': False},
    {'id': 'sn-deepseek-v4-pro', 'name': 'DeepSeek-V4-Pro',
     'context_length': 1000000, 'max_tokens': 100000,
     'tags': ['code', 'reasoning'], 'visible': True, 'reasoning': True,
     'free': False},
    {'id': 'sn-deepseek-v4-1-flash', 'name': 'DeepSeek-V4.1-Flash',
     'context_length': 1000000, 'max_tokens': 100000,
     'tags': ['general', 'fast'], 'visible': True, 'reasoning': True,
     'free': False},
]

# 识图能力**不能只看目录里的 `vision` 标签**：上游实测两个方向都错过。这份
# 表是 2026-09 的真机结论，未测过的模型回退到目录标签。
MEASURED_VISION: dict[str, bool] = {
    'sn-sensenova-6-8-flash-lite': True,
    'sn-glm-5-3': False,
    'sn-kimi-k3': True,
    'sn-glm-5-3-flash': True,
    'sn-deepseek-v4-1-flash': True,
}
