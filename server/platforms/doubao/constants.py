"""豆包（www.doubao.com）协议常量。

协议事实来自社区实现 `yyydsz/doubao2api`（MIT）的 `internal/doubao`、
`internal/config`、`internal/server/{handler,checkin,quota}.go` 与
`config.example.json`（2026-11 实抓）：

  · 凭证是**网页 Cookie**（`sessionid` / `sid_tt` / `ttwid` / `s_v_web_id` /
    `odin_tt` / `passport_csrf_token`），没有 OAuth、没有 refresh token；
  · 对话走 `POST https://www.doubao.com/chat/completion`，请求体是豆包自己的
    「客户端消息 + option」结构，响应固定为 SSE（没有 `stream` 开关）；
  · 上游不返回 usage，参考实现按输出字符 ÷1.7 估算 token；
  · 签到：`POST /alice/ug/campaign/landing/checkin`，body `{"scene": ...}`；
  · 额度：`POST /alice/commerce/sale/subscription/quota/summary/`，
    `window_limits[].{total_amount,used_amount,used_percent}`，
    `total_amount` 可能带千分位逗号。

这里只放地址、稳定参数与模型表；协议细节在 `client.py` / `chat.py`，账号存储
在 `store.py`。
"""
from __future__ import annotations

# ── 上游 ────────────────────────────────────────────────────
BASE_URL = 'https://www.doubao.com'
CHAT_PATH = '/chat/completion'
CHECKIN_PATH = '/alice/ug/campaign/landing/checkin'
# 只读的签到活动面板（`daily_signin` 任务、今日是否已签、连签天数、待领额度）。
# 写接口被拒（710010702）时用它回查真实状态，避免把「上游系统错误」误报成
# 「没有活动」。
CHECKIN_DETAIL_PATH = '/alice/ug/campaign/v3/detail/'
QUOTA_PATH = '/alice/commerce/sale/subscription/quota/summary/'

# 上游不认 OpenAI 的 `model`，只认豆包内部的 `model_item_key`。空模型名补
# 这个（参考实现的 `default_model`）。
DEFAULT_MODEL = 'deepseek-v4-flash'

# 客户端版本：活动 / 模型按版本放行。参考仓 README 记录的实测值为
# 2.32.6 / 3.42.0（config.example.json 里的 2.30.1 / 3.37.4 是旧值）。
PC_VERSION = '2.32.6'
RUNTIME_VERSION = '3.42.0'

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36 SamanthaDoubao/2.32.6')

# `/chat/completion` 与两个 alice 接口共用的查询参数。参考实现从
# `upstream.query` 原样带过来；缺少这些参数时上游会按「网页匿名会话」处理。
QUERY: dict[str, str] = {
    'aid': '582478',
    'real_aid': '582478',
    'channel': 'win',
    'client_platform': 'pc_client',
    'device_platform': 'web',
    'doubao_device_platform': 'desktop',
    'doubao_pc_version': PC_VERSION,
    'pc_version': PC_VERSION,
    'pkg_type': 'release_version',
    'language': 'zh',
    'region': 'CN',
    'sys_region': 'CN',
    'runtime': 'web',
    'runtime_version': RUNTIME_VERSION,
    'samantha_web': '1',
    'use-olympus-account': '1',
    'version_code': '20800',
    'web_platform': 'desktop',
    'chromium_version': '147.0.7727.149',
}

# 工作模式（桌面端「Agent」）。这些值决定上游是否把这些模型当作可用的
# 客户端会话：`agent_mode=1` / `conversation_mode=2` / `mode_id="3"`，且
# headless 场景必须 `enable_sandbox=false`（否则上游要求本地沙箱）。
WORK_MODE_ENABLED = True
BOT_ID = '7338286299411103781'
MODE_ID = '3'
REASONING_EFFORT = '4'
MEMORY_PROFILE = '300k_230'
TOTAL_WINDOW_SIZE = '300000'
ENABLE_SANDBOX = False

# 签到活动场景（实测：桌面端「每日签到」）。
CHECKIN_SCENE = 'pc_incentive_task_mvp_v3'

# Cookie 里必须至少有一个登录态字段，否则请求必然 401。
COOKIE_FIELDS = ('sessionid', 'sid_tt', 'session_tt', 'ttwid', 's_v_web_id',
                 'odin_tt', 'passport_csrf_token', 'passport_csrf_token_default')

# 默认本地应用 id（与桌面端一致）。
DEFAULT_LOCAL_APP_ID = '582478'

# ── 超时（秒）──────────────────────────────────────────────
CHAT_TIMEOUT = 600.0
CHAT_IDLE_TIMEOUT = 120.0
CHECKIN_TIMEOUT = 20.0
QUOTA_TIMEOUT = 20.0

# ── 模型表 ─────────────────────────────────────────────────
# `model_item_key` 是上游真正的模型 id；`memory_profile` / `total_window_size`
# 随模型不同（写错窗口上游会报错）。`context_length` 只用于 `/v1/models`
# 展示与面板提示。
#
# 已知上游报错的两个 GLM 模型（`person-glm-5-3-*`）**不放进目录**：列出来
# 只会让用户选中一个必然失败的模型。需要直连时仍可显式发
# `doubao/glm-5.3`，`upstream_model()` 会把它原样交给上游。
DEFAULT_MODELS: list[dict] = [
    {'id': 'auto', 'name': '自动（豆包）',
     'model_item_key': '9',
     'memory_profile': '256k', 'total_window_size': '256000',
     'context_length': 256000, 'reasoning': True},
    {'id': 'doubao-2.1-lite', 'name': '豆包 2.1 Lite',
     'model_item_key': 'seed-lite-7b',
     'memory_profile': '256k', 'total_window_size': '256000',
     'context_length': 256000, 'reasoning': True},
    {'id': 'doubao-2.1-pro', 'name': '豆包 2.1 Pro',
     'model_item_key': '5',
     'memory_profile': '256k', 'total_window_size': '256000',
     'context_length': 256000, 'reasoning': True},
    {'id': 'deepseek-v4-flash', 'name': 'DeepSeek V4.1 Flash',
     'model_item_key': 'person-deepseek-v4-1-flash-261017',
     'memory_profile': '300k_230', 'total_window_size': '300000',
     'context_length': 300000, 'reasoning': True},
    {'id': 'deepseek-v4-pro', 'name': 'DeepSeek V4 Pro',
     'model_item_key': 'person-deepseek-v4-pro-ga-260813',
     'memory_profile': '300k_230', 'total_window_size': '300000',
     'context_length': 300000, 'reasoning': True},
    {'id': 'kimi-k3', 'name': 'Kimi K3',
     'model_item_key': 'person-kimi-k3-261111',
     'memory_profile': '300k_230', 'total_window_size': '300000',
     'context_length': 300000, 'reasoning': True},
    {'id': 'kimi-k3.1-pro', 'name': 'Kimi K3.1 Pro',
     'model_item_key': 'person-kimi-k3-1-pro-261111',
     'memory_profile': '300k_230', 'total_window_size': '300000',
     'context_length': 300000, 'reasoning': True},
    {'id': 'kimi-k3.1-pro-fast', 'name': 'Kimi K3.1 Pro 高速',
     'model_item_key': 'person-kimi-k3-1-pro-highspeed-261111',
     'memory_profile': '300k_230', 'total_window_size': '300000',
     'context_length': 300000, 'reasoning': True},
]

# 兼容别名（参考实现的 `model_aliases`）：客户端写 `doubao` / `deepseek`
# 时都落到默认的 DeepSeek Flash。
MODEL_ALIASES: dict[str, str] = {
    'doubao': 'person-deepseek-v4-1-flash-261017',
    'deepseek': 'person-deepseek-v4-1-flash-261017',
}

# 上游没有 usage，参考实现按「字符 ÷ 1.7」估算。这里沿用同一口径，并在
# 文档 / 代码注释里标明是估算值，而不是让统计页把 0 当成真实用量。
CHARS_PER_TOKEN = 1.7


def model_entry(name: object) -> dict:
    """模型名 → 目录条目；未知名字按默认窗口兜底。"""
    text = str(name or '').strip().lower()
    for entry in DEFAULT_MODELS:
        if entry['id'].lower() == text:
            return entry
    if text in MODEL_ALIASES:
        key = MODEL_ALIASES[text]
        for entry in DEFAULT_MODELS:
            if entry['model_item_key'] == key:
                return entry
    default = next(e for e in DEFAULT_MODELS if e['id'] == DEFAULT_MODEL)
    return default


def upstream_model_key(name: object) -> str:
    """成员名 → 上游 `model_item_key`。

    空名补默认模型；目录里的名字查表；兼容别名查别名表；其余原样透传
    （支持用户在参考仓更新模型后不改代码直接试新 key）。
    """
    text = str(name or '').strip()
    if not text:
        text = DEFAULT_MODEL
    lowered = text.lower()
    for entry in DEFAULT_MODELS:
        if entry['id'].lower() == lowered:
            return str(entry['model_item_key'])
    alias = MODEL_ALIASES.get(lowered)
    if alias:
        return alias
    return text
