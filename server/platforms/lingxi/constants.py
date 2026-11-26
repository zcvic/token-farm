"""WPS 灵犀（lingxi.wps.cn）协议常量。

灵犀没有开放平台 API。这里的地址与请求形状来自其桌面客户端自己使用的私有
app-server 接口，协议事实核对自两份已验证的实现：

  · `xinstart-1/dsh-connect-lingxi`（MIT）—— `docs/PROTOCOL.md` 与
    `src/protocol/*`（2026-11 真机抓包）；
  · `xfyang728/dsh-router-wpslingxi`（MIT）—— 同源协议，签名 / 握手一致。

要点：

  · 凭证是**网页 Cookie**（`wps_sid`），没有 OAuth、没有 refresh token；
  · 一次对话 = 三次 HTTP 握手：建会话 → 领 run → SSE 对话；
  · SSE 正文在 `choices[].text`（不是 `delta.content`），没有 `[DONE]`，
    靠 `choices[].finish_reason` 收尾；
  · 上游 `usage.total_tokens` 是**上下文大小**，不是本轮用量，必须自己加。

这里只放地址、稳定参数与模型兜底表；协议细节在 `client.py` / `chat.py`，
账号存储在 `store.py`。
"""
from __future__ import annotations

import hashlib
import tempfile

# ── 上游 ────────────────────────────────────────────────────
BASE_URL = 'https://lingxi.wps.cn'
# 部分账号的会话只在登录域名下有效，握手时两个基址都要试。
KDOCS_BASE = 'https://lingxi.kdocs.cn'

SESSIONS_PATH = '/api/aioffice/v1/cowork/sessions'
RUNS_PATH = '/api/public/v1/credits/runs'
CHAT_PATH = '/api/public/v1/agent-runtime/chat/completions'
PLANS_PATH = '/api/aioffice/v1/sessions/plans'
BALANCE_PATH = '/api/public/v1/credits/balance'
PROFILE_PATH = '/api/public/v1/account/profile'
TASKS_PATH = '/api/public/v1/tasks'
# run 生命周期上报（running → completed 是上游结算 / 扣费的唯一触发点）。
EXECUTION_STATUS_PATH = '/api/public/v1/sessions/{session_id}/execution_status'
# 单会话消耗明细（`session_id` → 这一轮实际扣了多少智点）。
EXPENSE_PATH = '/api/public/v1/credits/transaction/expense'

# ── 客户端指纹 ──────────────────────────────────────────────
CLIENT_TYPE = 'lingxi_claw'
CLIENT_LANG = 'zh-CN'
# 与参考实现一致：灵犀桌面端是 Chromium 壳，UA 只到 WebKit 段。
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'

KSO_APP_NAME = 'win-lingxi-desktop'
KSO_APP_VERSION = '1.3.31'

# Cookie 里必须有的登录态字段。缺了必然 401，导入时就该拒绝。
COOKIE_FIELDS = ('wps_sid',)

# 设备号由「账号 Cookie」稳定派生：同一账号每次请求都是同一台设备。
DEVICE_ID_SALT = 'token-farm-lingxi-device'

# ── 模型 ────────────────────────────────────────────────────
DEFAULT_MODEL = 'deepseek-v4.1-flash'
MAX_PROMPT_TOKENS = 1_048_576

# 客户端里「模式」与「模型」是同一条目录：`plan_pro` / `plan_max` 不是思考
# 模型，`reasoning_efforts` 为空。
#
# 下列字段逐条抄自一次真实 `/sessions/plans` 响应（2026-11-10）。顺序必须
# 保持「先模式、后模型」，否则登录前后目录顺序会不一致。
DEFAULT_MODELS: list[dict] = [
    {'id': 'plan_pro', 'name': 'Pro 模式', 'context_length': 1048576,
     'max_tokens': 131072, 'multiplier': '', 'reasoning': False,
     'vision': True, 'description': '适用于日常任务，智点消耗较少'},
    {'id': 'plan_max', 'name': 'Max 模式', 'context_length': 1048576,
     'max_tokens': 131072, 'multiplier': '', 'reasoning': False,
     'vision': True, 'description': '为复杂任务设计，智点消耗较多'},
    {'id': 'deepseek-v4.1-flash', 'name': 'DeepSeek V4.1 Flash',
     'context_length': 1048576, 'max_tokens': 393216, 'multiplier': '~0.12x',
     'reasoning': True, 'vision': True, 'badge': 'NEW',
     'reasoning_efforts': ['low', 'high', 'max']},
    {'id': 'deepseek-v4-pro-0813', 'name': 'DeepSeek V4 Pro',
     'context_length': 1048576, 'max_tokens': 393216, 'multiplier': '0.40x',
     'reasoning': True, 'vision': False, 'badge': '正式版',
     'reasoning_efforts': ['low', 'high', 'max']},
    {'id': 'qwen3.8_flash', 'name': 'Qwen3.8-Flash',
     'context_length': 1048576, 'max_tokens': 131072, 'multiplier': '~0.08x',
     'reasoning': True, 'vision': True,
     'reasoning_efforts': ['low', 'high', 'max']},
    {'id': 'qwen3.8_max', 'name': 'Qwen3.8-Max',
     'context_length': 1048576, 'max_tokens': 131072, 'multiplier': '~0.80x',
     'reasoning': True, 'vision': True,
     'reasoning_efforts': ['low', 'high', 'max']},
    {'id': 'mimo-v2.6-pro', 'name': 'Xiaomi MiMo-V2.6 Pro',
     'context_length': 1048576, 'max_tokens': 131072, 'multiplier': '0.10x',
     'reasoning': False, 'vision': True},
    {'id': 'GLM-5.3-Flash', 'name': 'GLM-5.3-Flash',
     'context_length': 1048576, 'max_tokens': 131072, 'multiplier': '~0.11x',
     'reasoning': True, 'vision': True, 'badge': '首发接入',
     'reasoning_efforts': ['low', 'high', 'max']},
    {'id': 'GLM-5.3', 'name': 'GLM-5.3',
     'context_length': 1048576, 'max_tokens': 131072, 'multiplier': '~0.75x',
     'reasoning': True, 'vision': False, 'badge': '首发接入',
     'reasoning_efforts': ['low', 'high', 'max']},
    {'id': 'minimax_m3', 'name': 'MiniMax-M3',
     'context_length': 1048576, 'max_tokens': 524288, 'multiplier': '0.20x',
     'reasoning': False, 'vision': True},
    {'id': 'doubao_seed_2_1_pro_260628', 'name': 'Doubao-Seed-2.1 Pro',
     'context_length': 262144, 'max_tokens': 262144, 'multiplier': '~0.50x',
     'reasoning': True, 'vision': True,
     'reasoning_efforts': ['low', 'high', 'max']},
    {'id': 'kimi-k3', 'name': 'Kimi-K3',
     'context_length': 1048576, 'max_tokens': 1048576, 'multiplier': '1.60x',
     'reasoning': True, 'vision': True,
     'reasoning_efforts': ['low', 'high', 'max']},
    {'id': 'kimi-k3.1-pro', 'name': 'Kimi-K3.1 Pro',
     'context_length': 1048576, 'max_tokens': 1048576, 'multiplier': '~1.10x',
     'reasoning': True, 'vision': True, 'badge': 'NEW',
     'reasoning_efforts': ['low', 'high', 'max']},
]

# 兼容别名（客户端习惯写短名 / `default`）。
MODEL_ALIASES = {
    'default': DEFAULT_MODEL,
    'auto': DEFAULT_MODEL,
}

# ── 超时（秒）──────────────────────────────────────────────
REQUEST_TIMEOUT = 120.0
CHAT_TIMEOUT = 600.0
CHAT_IDLE_TIMEOUT = 180.0
WEB_TIMEOUT = 20.0

# run 缓存有效期（1 小时，与参考实现一致）。
RUN_TTL_SECONDS = 3600

# 设备名 / 工作目录用于建会话的 `env`，与参考实现同口径。
DEVICE_NAME = ''  # 空 = 取 socket.gethostname()
WORK_SPACE_DIR = tempfile.gettempdir()


def device_id_for(cookie: object) -> str:
    """由账号 Cookie 稳定派生设备号。

    随机设备号会让每次请求都像一台新机器，等于自己制造风控特征；参考实现用
    固定串派生，这里按账号 Cookie 派生，多个账号之间也不会共用同一台设备。
    """
    text = str(cookie or '').strip()
    return hashlib.sha256(f'{DEVICE_ID_SALT}:{text}'.encode('utf-8')).hexdigest()[:32]


def upstream_model_id(name: object) -> str:
    """成员名 → 上游真正认的 model key。

    `wps-lingxi/` / `lingxi/` / `lingxi-` 前缀剥掉；空名与 `default` 落到
    默认模型；其余原样透传（上游上架新模型时不必改代码）。
    """
    text = str(name or '').strip()
    for prefix in ('wps-lingxi/', 'lingxi/', 'lingxi-'):
        if text.lower().startswith(prefix):
            text = text[len(prefix):].strip()
            break
    if not text:
        text = DEFAULT_MODEL
    return MODEL_ALIASES.get(text.lower(), text)