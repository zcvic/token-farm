"""Kilo（kilo.ai）协议常量。

协议事实来自官方客户端 `Kilo-Org/kilocode` 的 `packages/kilo-gateway`（2026-11
核对）：`api/constants.ts`、`auth/device.ts`、`api/profile.ts`、`headers.ts`。

  · **两套域名**，职责不同：
      - `api.kilo.ai` —— 账号侧（设备授权码、资料、余额、默认模型）；
      - `ai-gateway.kilo.ai/api/v1` —— 模型侧（对话、模型目录，OpenRouter
        schema 的 OpenAI 兼容层）。
  · 凭证是设备授权码换来的**一年期 API token**（`TOKEN_EXPIRATION_MS`），
    没有 refresh token：过期只能重新登录；
  · 对话只要 `Authorization: Bearer <token>`，再带两个客户端标记头
    （`User-Agent` / `X-KILOCODE-EDITORNAME`）；
  · 模型目录免鉴权可读，`kilo-auto/free` 是免费档的默认模型；
  · 余额接口回的是**美元**（`{balance: number}`），与积分不是同一口径。

这里只放地址与稳定常量；协议细节在 `client.py`，账号存储策略在 `store.py`。
"""
from __future__ import annotations

import os

# ── 上游 ────────────────────────────────────────────────────
# 账号侧：设备码 / 资料 / 余额。
API_BASE = 'https://api.kilo.ai'
DEVICE_PATH = '/api/device-auth/codes'
PROFILE_PATH = '/api/profile'
BALANCE_PATH = '/api/profile/balance'
DEFAULTS_PATH = '/api/defaults'

# 模型侧：OpenAI 兼容对话与模型目录。
DEFAULT_GATEWAY_BASE = 'https://ai-gateway.kilo.ai/api/v1'


def resolve_gateway_base(env: dict | None = None) -> str:
    """解析网关地址，对齐官方客户端的 `resolveKiloAiGatewayBase`。

    `KILO_AI_GATEWAY_URL` 优先；只配了 legacy 的 `KILO_API_URL` 时，网关流量
    走 `${KILO_API_URL}/api/gateway/v1`（该 API 会把这条路径改写到 AI 网关的
    `/api/v1`）。两者都没配才是官方默认值。末尾斜杠统一剥掉，调用方可以直接
    拼 `/chat/completions`。

    自建 / 代理网关因此不用改代码：设一个环境变量即可。
    """
    source = os.environ if env is None else env
    value = str(source.get('KILO_AI_GATEWAY_URL') or '').strip()
    if value:
        return value.rstrip('/')
    api = str(source.get('KILO_API_URL') or '').strip()
    if api:
        return f'{api.rstrip("/")}/api/gateway/v1'
    return DEFAULT_GATEWAY_BASE


GATEWAY_BASE = resolve_gateway_base()

CHAT_PATH = '/chat/completions'
MODELS_PATH = '/models'

# 上游只暴露一个聚合模型时的默认值（与官方 `DEFAULT_MODEL` 一致）。
DEFAULT_MODEL = 'kilo-auto/free'

# 客户端标记：上游按这两项识别「这是官方客户端」，缺了会被当第三方流量。
UA = 'opencode-kilo-provider'
EDITOR_NAME = 'Kilo CLI'

# ── 超时（秒）──────────────────────────────────────────────
MODELS_TIMEOUT = 20.0
PROFILE_TIMEOUT = 15.0
CHAT_TIMEOUT = 600.0
CHAT_IDLE_TIMEOUT = 120.0
DEVICE_TIMEOUT = 20.0

# 设备码有效期兜底（上游 `expiresIn` 实测 599 秒）。
DEVICE_TTL = 600
# 轮询间隔（官方 `POLL_INTERVAL_MS = 3000`）。
DEVICE_INTERVAL = 3.0

# ── 模型目录（离线兜底）────────────────────────────────────
# 上游目录拉不到时用它，保证「模型页不是空白」。只保留免费档与自动路由，
# 付费模型的可用性取决于账号余额，不在离线兜底里猜。
DEFAULT_MODELS: list[dict] = [
    {'id': 'kilo-auto/free', 'name': 'Auto Free', 'context_length': 256000,
     'max_tokens': 0, 'modalities': ['text'], 'reasoning': False},
    {'id': 'kilo-auto/balanced', 'name': 'Auto Balanced',
     'context_length': 1000000, 'max_tokens': 0,
     'modalities': ['text'], 'reasoning': False},
    {'id': 'kilo-auto/efficient', 'name': 'Auto Efficient',
     'context_length': 1000000, 'max_tokens': 0,
     'modalities': ['text'], 'reasoning': False},
]
