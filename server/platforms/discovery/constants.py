"""Discovery（Intern TokenPlan）协议常量。

协议事实来自社区实现 `ZFXing-lite/discovery2api`（MIT）的 `internal/keypool`、
`internal/credits`、`internal/config` 与 `server.go` 的实测记录（2026-11）：

  · 凭证是 Intern TokenPlan 的 **API Key**（约 6 个月有效），直接
    `Authorization: Bearer <key>` 打上游，没有 OAuth / refresh token；
  · 对话 / 模型列表都在同一个 base 下（`/v1/chat/completions`、`/v1/models`），
    OpenAI 兼容，流式 / 非流式都支持；
  · **按账户限流**：同一账户下的多把 Key 共享余额与配额，多 Key 不叠加；
  · 没有余额查询接口。唯一可靠的额度探针是 `GET /v1/models`：
    200 = 还有额度，429 `quota_exceeded` = 耗尽，429 `rate_limit_exceeded`
    = 只是被限流（额度还在）。

这里只放地址与稳定常量；协议细节在 `client.py`，账号存储策略在 `store.py`。
"""
from __future__ import annotations

# ── 上游 ────────────────────────────────────────────────────
BASE_URL = 'https://discovery-api.intern-ai.org.cn'
CHAT_PATH = '/v1/chat/completions'
MODELS_PATH = '/v1/models'

# 参考实现的 `default-model`：上游只暴露一个聚合模型时，这个值永远可用。
DEFAULT_MODEL = 'auto'

# 与网页控制台一致的来源标记。上游对 UA 不敏感，但空 UA 会被网关当脚本流量。
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0 Safari/537.36')

# ── 超时（秒）──────────────────────────────────────────────
MODELS_TIMEOUT = 15.0
CHAT_TIMEOUT = 600.0
CHAT_IDLE_TIMEOUT = 120.0

# ── 错误码 ──────────────────────────────────────────────────
# 上游错误体是 `{error: {code, message}, trace_id, request_id}`。
CODE_INVALID_KEY = 'invalid_api_key'
CODE_QUOTA = 'quota_exceeded'
CODE_RATE = 'rate_limit_exceeded'

# ── 模型目录（离线兜底）────────────────────────────────────
# 与 `server.go` 的 `knownDiscoveryModels` 同源（上游目录拉不到时用它）。
# `max_tokens` 留 0：上游没给逐模型输出上限，填一个猜的值会让适配器把
# 客户端合法的 `max_tokens` 误钳掉。
DEFAULT_MODELS: list[dict] = [
    {'id': 'auto', 'name': 'Auto', 'context_length': 256000,
     'modalities': ['text'], 'reasoning': True},
    {'id': 'deepseek-v3', 'name': 'DeepSeek V3', 'context_length': 64000,
     'modalities': ['text']},
    {'id': 'deepseek-r1', 'name': 'DeepSeek R1', 'context_length': 64000,
     'modalities': ['text'], 'reasoning': True},
    {'id': 'glm-4', 'name': 'GLM-4', 'context_length': 128000,
     'modalities': ['text']},
    {'id': 'glm-4-flash', 'name': 'GLM-4-Flash', 'context_length': 128000,
     'modalities': ['text']},
    {'id': 'qwen-plus', 'name': 'Qwen Plus', 'context_length': 131072,
     'modalities': ['text']},
    {'id': 'qwen-turbo', 'name': 'Qwen Turbo', 'context_length': 8192,
     'modalities': ['text']},
    {'id': 'kimi-k1.5', 'name': 'Kimi K1.5', 'context_length': 128000,
     'modalities': ['text']},
]
