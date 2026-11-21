"""Cline（cline.bot）协议常量。

协议事实来自社区实现 `luawei1/cline2api` 的 Cloudflare Worker 版（2026-11
核对，见 `deploy/cline-ref.txt`）与上游实测：

  · 官方 API：`https://api.cline.bot/api/v1`，OpenAI 兼容；
  · **必须伪装 Cline 客户端头**（`User-Agent: Cline/…`、`X-CLIENT-TYPE`、
    `X-PLATFORM`、`X-Task-ID` 等）。缺了会被 403：
    `only available via Cline product surfaces`；
  · 凭证是 `refreshToken`（WorkOS 设备授权码换来的），**刷新时轮换**：
    换发后旧的立即作废，必须**先落盘再使用**；
  · 授权头是 `Bearer workos:<accessToken>`（注意 `workos:` 前缀）；
  · `/models` 与 `/ai/cline/recommended-models` 免鉴权可读；
  · 没有余额接口：余额不足由 402 `insufficient_credits` 表达；
  · 免费档（`cline-free/` 前缀）**请求体带 `max_tokens` 会 500**，非流式还要
    强制上游走 stream 再聚合——上游对这几类前缀只认流式。

这里只放地址与稳定常量；协议细节在 `client.py`，账号存储策略在 `store.py`。
"""
from __future__ import annotations

# ── 上游 ────────────────────────────────────────────────────
BASE_URL = 'https://api.cline.bot/api/v1'
CHAT_PATH = '/chat/completions'
MODELS_PATH = '/models'
RECOMMENDED_PATH = '/ai/cline/recommended-models'
REFRESH_PATH = '/auth/refresh'
REGISTER_PATH = '/auth/register'

# WorkOS 设备授权码（Cline 的 client_id 是公开的，与官方客户端一致）。
WORKOS_DEVICE_URL = 'https://api.workos.com/user_management/authorize/device'
WORKOS_AUTH_URL = 'https://api.workos.com/user_management/authenticate'
WORKOS_CLIENT_ID = 'client_01K3A541FN8TA3EPPHTD2325AR'

# 默认模型：官方免费档里最稳的一条。
DEFAULT_MODEL = 'cline-free/nemotron-3.5-super-vl'

# ── 伪装头 ─────────────────────────────────────────────────
# 这组值来自参考实现的实测；上游按它们判断「是不是 Cline 客户端」。
UA = 'Cline/3.0.47'
CLIENT_TYPE = 'cline-sdk'
CLIENT_VERSION = '3.0.47'
PLATFORM = 'terminal'
PLATFORM_VERSION = '3.0.47'
CORE_VERSION = '0.0.66'
REFERER = 'https://cline.bot'
TITLE = 'Cline'

# ── 超时（秒）──────────────────────────────────────────────
MODELS_TIMEOUT = 20.0
REFRESH_TIMEOUT = 20.0
CHAT_TIMEOUT = 600.0
CHAT_IDLE_TIMEOUT = 120.0
DEVICE_TIMEOUT = 20.0

# access token 默认寿命（上游不给 `expiresAt` 时按 10 分钟算）。
TOKEN_FALLBACK_SECONDS = 10 * 60
# 提前多少秒换新（避免卡在过期边缘）。
TOKEN_SKEW_SECONDS = 120
# 刷新失败后的冷却：与参考实现一致（60 秒）。
REFRESH_FAIL_COOLDOWN = 60

# 设备码有效期兜底（WorkOS 实测 300 秒）。
DEVICE_TTL = 300
DEVICE_INTERVAL = 5.0

# 需要「强制上游流式再聚合」的模型前缀（参考实现实测）。
FORCE_STREAM_PREFIXES = ('deepseek/', 'cline-free/', 'cline-pass/')

# 上游把限流伪装成 200 SSE 的几种信号（`empty response content` 等）。
SOFT_ERROR_MARKERS = ('empty response content', 'Daily free limit',
                      'INFERENCE_CAP_ERROR')

# ── 模型目录（离线兜底）────────────────────────────────────
# 上游目录拉不到时用它。只保留免费档——付费档的可用性取决于账号余额。
DEFAULT_MODELS: list[dict] = [
    {'id': 'cline-free/nemotron-3.5-super-vl',
     'name': 'Nemotron 3.5 Super VL', 'context_length': 256000,
     'max_tokens': 0, 'modalities': ['text', 'image'], 'free': True},
    {'id': 'cline-free/mimo-v2.6-flash', 'name': 'MiMo v2.6 Flash',
     'context_length': 128000, 'max_tokens': 0,
     'modalities': ['text'], 'free': True},
    {'id': 'cline-free/muse-spark-1.3-contributor',
     'name': 'Muse Spark 1.3 Contributor', 'context_length': 128000,
     'max_tokens': 0, 'modalities': ['text'], 'free': True},
    {'id': 'cline-free/solar-mini4', 'name': 'Solar Mini 4',
     'context_length': 128000, 'max_tokens': 0,
     'modalities': ['text'], 'free': True},
]
