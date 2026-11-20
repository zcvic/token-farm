"""DuMate（百度搭子）协议常量。

协议事实来自 `wearetheone777/dumate2api`（MIT）的 `src/web-pool.js` 与
`src/dumate-web.js` 的真机实测记录（2026-09 ~ 2026-11）：

  · 网页 cookie（必须含 `BDUSS`）→ `GET /api/dumate/app/token/get`
    → `{result: {token, expired_at}}`，token 约 1 小时；
  · 对话走 `POST https://dumate-svc.baidu.com/gateway/apis/v1/chat/completions`，
    `Authorization: Bearer <token>`，标准 OpenAI 形状（流式 / 非流式都支持）；
  · 签到、积分、用户信息在 `https://www.dumate.cn/api/dumate/*`，同样是网页
    cookie 鉴权。

这里只放地址与稳定常量；协议细节在 `client.py`，账号存储策略在 `store.py`。
"""
from __future__ import annotations

import json
from pathlib import Path

# ── 网页端（cookie 鉴权）────────────────────────────────────
WEB_BASE = 'https://www.dumate.cn'
# 签 token 的候选域。两个域是同一签发服务的两条入口，token 与域无关：
# 主域被墙 / 换域名时切另一条，不改变凭证语义。
WEB_HOSTS = (
    'https://www.dumate.cn',
    'https://console.bce.baidu.com',
)

TOKEN_PATH = '/api/dumate/app/token/get'
USER_INFO_PATH = '/api/dumate/user/info'
LOGIN_BONUS_PATH = '/api/dumate/points/loginBonus'
LOGIN_BONUS_INFO_PATH = '/api/dumate/points/loginBonusInfo'
QUOTA_OVERVIEW_PATH = '/api/dumate/points/quota_overview'
DRAW_STATUS_PATH = '/api/dumate/activity/growth-plan/draw/status'
DRAW_PATH = '/api/dumate/activity/growth-plan/draw'
TASKS_PATH = '/api/dumate/activity/growth-plan/tasks'
TASK_COMPLETE_PATH = '/api/dumate/activity/growth-plan/task/complete'

# 积分概览要带 clientType/timezone，否则上游按网页默认口径回（时区不同，
# 「今日」边界会漂）。
QUOTA_QUERY = '?clientType=desktop&timezone=Asia%2FShanghai'

# ── 模型网关（Bearer token 鉴权）────────────────────────────
GATEWAY_BASE = 'https://dumate-svc.baidu.com/gateway/apis/v1'
CHAT_PATH = '/chat/completions'

# 与桌面 / 网页客户端一致的来源标记。缺 `X-Dumate-Client-Type` 会被当成
# 非浏览器来源，部分账号直接被拒。
CLIENT_TYPE = 'web'
ACCOUNT_ID = 'global'

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0 Safari/537.36')

# token 提前刷新的余量：留 5 分钟，避免请求刚好压在过期点上。
TOKEN_SKEW_SECONDS = 5 * 60
# 上游正常返回 expired_at；解析失败时按 50 分钟兜底（宁可早刷不要晚刷）。
TOKEN_FALLBACK_SECONDS = 50 * 60

# 默认超时（秒）。对话本身可能很长，`idle_timeout` 才是流式的读超时。
TOKEN_TIMEOUT = 15.0
API_TIMEOUT = 20.0
CHAT_TIMEOUT = 600.0
CHAT_IDLE_TIMEOUT = 120.0

# 账号里没有 BDUSS 时调任何接口都会 401，导入阶段就拒绝。
REQUIRED_COOKIE = 'BDUSS'

# 业务码：登录态失效（网页接口 HTTP 200 也可能带这个码）。
EXPIRED_CODES = {10001, 10006}

# ── 模型目录 ────────────────────────────────────────────────
# 按模型的 `max_tokens` 真值（上游 400 错误体实测）。目录里的 `limit.output`
# 是客户端展示值，可能与上游不同（model-text：展示 384000 / 真值 393216）。
# 客户端给的上限超过这里时钳到真值，避免整轮 400。
OUTPUT_CAPS = {
    'dm-auto-model/text.L0': 393216,
    'model-text': 393216,
    'dm-auto-model/mm.L0': 393216,
    'dm-auto-model/mm.L2': 393216,
    'dm-auto-model/text.L1': 2000000,
    'dm-auto-model/text.L2': 131072,
    'glm-5': 131072,
}

_CATALOG_FILE = Path(__file__).with_name('catalog.json')


def _load_catalog() -> list[dict]:
    """内置模型目录；文件损坏时退回空表（账号页仍可用，只是列不出模型）。"""
    try:
        data = json.loads(_CATALOG_FILE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    models = data.get('models') if isinstance(data, dict) else None
    if not isinstance(models, list):
        return []
    return [m for m in models if isinstance(m, dict) and str(m.get('id') or '')]


CATALOG = _load_catalog()
CATALOG_AS_OF = '2026-11-13'
