"""华为云 CodeArts 码道（CodeArts Agent）协议常量。

协议事实来自官方客户端逆向与真机抓包（见 `deploy/codearts-ref.txt`）：

  · 登录是华为云 OAuth2（PKCE），token 端点 `sts.cn-north-4` 的
    `/v1/oauth2/tokens`，请求必须带 **DPoP** 证明；
  · 拿到的是 STS 临时凭证：`access_key_id` / `secret_access_key` /
    `security_token`（= 对话用的 `x-auth-token`），有效期约 1 小时；
  · 所有业务请求都要再做一次华为云 `SDK-HMAC-SHA256` 的 AK/SK 签名；
  · `refresh_token` 与 `client_id`、DPoP 私钥三者绑定，且**一次性轮换**；
  · 对话走 `/api/v2/chat/completions`（原生 OpenAI 形状），SSE 里的 `text`
    字段是**累计全文**，不是增量；
  · 模型来自三路：agent-center、`/v1/model/builtin`、限时福利网关；
  · 每日积分靠 `/v1/ops/delivery|claim|confirm` 三步领取。

这里只放地址、稳定常量与离线兜底；协议解析在 `client.py`，签名在
`signer.py`，DPoP 在 `dpop.py`，账号存储策略在 `store.py`。
"""
from __future__ import annotations

# ── 上游 ────────────────────────────────────────────────────
# 商业版 cn-north-4：product.json 里 chatDomain = codeGenDomain = snapEngineDomain。
SNAP_HOST = 'https://snap-access.cn-north-4.myhuaweicloud.com'
SNAP_MANAGER = f'{SNAP_HOST}/snap-manager'
PORTAL_HOST = 'https://codearts.huaweicloud.com/portal'
BENEFIT_HOST = 'https://opengw.developer.huaweicloud.com'
STS_HOST = 'https://sts.cn-north-4.myhuaweicloud.com'

# 端点（都相对上面各自的主机）。
EP_CHAT_V2 = '/api/v2/chat/completions'
EP_AGENT_LIST = '/v1/agent-center/agents/useragents'
EP_AGENT_DETAIL = '/v1/agent-center/agents/detail'
EP_MODEL_BUILTIN = '/v1/model/builtin'
EP_LOGIN_TICKET = '/v1/login/ticket'
EP_OAUTH_TOKENS = '/v1/oauth2/tokens'
EP_CURRENT_USER = '/v1/current/user'
EP_CALLER_IDENTITY = '/v5/caller-identity'
EP_BENEFIT_CONFIG = '/api/v1/gateway/config'
EP_BENEFIT_CLAIM = '/api/v1/benefit/claim'
EP_CREDIT_DELIVERY = '/v1/ops/delivery'
EP_CREDIT_CLAIM = '/v1/ops/claim'
EP_CREDIT_CONFIRM = '/v1/ops/confirm'

TOKEN_URL = STS_HOST + EP_OAUTH_TOKENS

# 官方 CodeArts Agent 插件的 uri_scheme。refresh_token 与它绑定：换一个
# client_id 刷新会被 STS 拒（`invalid client id`）。
CLIENT_ID = 'codearts-agent'
# 授权页要求这两个头 / 参数，且 `code_challenge_method` 只认 "SHA-256"
# （不是标准写法 `S256`）。
PLUGIN_NAME = 'snap_AIIDE'
PLUGIN_VERSION = '5.2.0'
REDIRECT_PATH = '/oauth/callback'
CODE_CHALLENGE_METHOD = 'SHA-256'

# 对话 / 目录接口的客户端标识。
APP_ID = 'CodeAgent3.0'
LANGUAGE = 'zh-cn'
AGENT_TYPE_PROMPT_CENTER = 'PromptCenter'
AGENT_TYPE_AGENT_CENTER = 'AgentCenter'
HEADER_MAAS_TYPE = 'maas_type'
MAAS_BENEFIT = 'benefit'
# 积分活动只走 IDE 渠道。
CREDIT_CHANNEL = 'IDE'
# 只处理这两个活动：1 = 每日签到，4 = 新用户。
CREDIT_CAMPAIGNS = (1, 4)

# ── 令牌寿命（上游不给到期时间时兜底）──────────────────────
# STS 临时凭证约 1 小时；refresh token 的寿命上游不回，按 30 天保守估算
# （只用于「按剩余寿命巡检」，不参与对话选路）。
ACCESS_TTL_SECONDS = 60 * 60
REFRESH_TTL_SECONDS = 30 * 24 * 60 * 60
# 提前 10 分钟换新：签名请求的往返 + 时钟偏差都在这个窗口内消化。
TOKEN_SKEW_SECONDS = 10 * 60
# 刷新失败（非鉴权类）后的冷却：一次性 refresh token 不能每个请求都打一次。
REFRESH_FAIL_COOLDOWN = 60

# ── 超时（秒）──────────────────────────────────────────────
TOKEN_TIMEOUT = 20.0
CATALOG_TIMEOUT = 20.0
CHAT_TIMEOUT = 600.0
CHAT_IDLE_TIMEOUT = 120.0
CREDITS_TIMEOUT = 20.0

# ── 模型目录 ───────────────────────────────────────────────
# 目录按账号发现、按账号隔离（福利是授予到账号的），单账号缓存 1 小时。
CATALOG_TTL_SECONDS = 3600
# 某一路来源连续失败后的退避窗口。
CATALOG_SOURCE_BACKOFF = 5 * 60

# 冷启动种子：首次发现前也要能给出模型列表；福利种子决定首次对话是否带
# `maas_type: benefit`（少带一次就是一次 `model is not registered`）。
SEED_MODELS: tuple[str, ...] = (
    'GLM-5.2',
    'GLM-5.2-ArkTS-SPARK',
    'GLM-5.1',
    'GLM-4.7',
    'OpenPangu-2.0-Pro',
    'OpenPangu-2.0-Flash',
    'Qwen3-VL-235B',
    'Qwen3.5-397B-A17B-VL',
    'Qwen3.6-27B-VL',
)
SEED_BENEFIT_MODELS: tuple[str, ...] = (
    'deepseek-v4-flash-0731',
    'deepseek-v4-pro-0813',
    'glm-5.3-flash',
)
# 旧版小写别名 → 上游注册名。上游按注册名**精确匹配（区分大小写）**，
# 而客户端习惯写小写。
STATIC_ALIASES: dict[str, str] = {
    'snap-chat': 'GLM-5.2',
    'glm-5.2': 'GLM-5.2',
    'glm-5.1': 'GLM-5.1',
    'glm-4.7': 'GLM-4.7',
    'qwen3-vl-235b': 'Qwen3-VL-235B',
    'qwen3.5-397b-a17b-vl': 'Qwen3.5-397B-A17B-VL',
    'qwen3.6-27b-vl': 'Qwen3.6-27B-VL',
}
# 目录里没有模型时的兜底默认（与种子第一项一致）。
DEFAULT_MODEL = 'GLM-5.2'
DEFAULT_CONTEXT_WINDOW = 128_000
DEFAULT_MAX_TOKENS = 32_768

# ── 错误码 ─────────────────────────────────────────────────
# 账号级并发 / 限流。
RATE_LIMIT_MARKERS = ('tm.00001041', 'tpm', '并发会话', '429')
# 「该账号用不了这个模型」：客户端问题，不是账号故障。
MODEL_ERROR_MARKERS = (
    'inferhub.002002009', 'not registered', '4004.200', 'benefit not found',
    'tm.000010051', '无权调用该模型',
)
# 请求参数无效（客户端问题）。
BAD_REQUEST_MARKERS = ('inferhub.001001005', 'request param is invalid')
# refresh token 终态失效：继续重试没有意义，必须重新登录。
REFRESH_EXPIRED_MARKERS = (
    'invalid_grant', 'expiredrefreshtoken', 'invaliddpopheader',
    'invalid client id', 'has been used',
)


def is_model_error(text: object) -> bool:
    """是否是「模型 ID / 权限」类错误（可换一种拼写重试一次）。"""
    low = str(text or '').lower()
    return any(marker in low for marker in MODEL_ERROR_MARKERS)


def is_rate_limited(text: object) -> bool:
    low = str(text or '').lower()
    return any(marker in low for marker in RATE_LIMIT_MARKERS)


def is_bad_request(text: object) -> bool:
    low = str(text or '').lower()
    return any(marker in low for marker in BAD_REQUEST_MARKERS)


def is_refresh_expired(text: object) -> bool:
    low = str(text or '').lower()
    return any(marker in low for marker in REFRESH_EXPIRED_MARKERS)
