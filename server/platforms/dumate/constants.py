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
# 签 token 的候选域。三个域是同一签发服务的三条入口，token 与域无关：
# 主域被墙 / 换域名时切另一条，不改变凭证语义。
WEB_HOSTS = (
    'https://www.dumate.cn',
    'https://dumate.baidu.com',
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
PRIZE_CLAIM_PATH = '/api/dumate/activity/growth-plan/prize/claim'
ACTIVITY_STATUS_PATH = '/api/dumate/activity/status'

# 「成长计划」当前活动 id / 版本（前端 bundle 与参考实现同口径）。
ACTIVITY_ID = 'growth_plan_2026'
ACTIVITY_VERSION = 2
# 任务列表要带 device，否则上游会按未知终端裁掉一部分任务。
TASKS_QUERY = '?device=web'
# 抽奖 / 领取接口的来源标记（与网页客户端一致）。
ACTIVITY_SOURCE = 'web'

# 邀请码（「邀请有礼」）：网页端 5090 bundle 的 `/api/dumate/invitation_code/*`。
#   · GET  /get?activityId=        → {invitationCode, type, createdAt, inviteMessage}
#   · GET  /invited/count?activityId= → {invitedCount, totalPoints}
#   · POST /redeem                → body {invitationCode, timezone, source?, activityId?}
# 邀请码区分大小写；新用户 7 天内可填一次，不能填自己的码。
INVITATION_CODE_BASE = '/api/dumate/invitation_code'
INVITATION_CODE_GET_PATH = INVITATION_CODE_BASE + '/get'
INVITATION_CODE_REDEEM_PATH = INVITATION_CODE_BASE + '/redeem'
INVITATION_CODE_INVITED_COUNT_PATH = INVITATION_CODE_BASE + '/invited/count'
INVITATION_TIMEZONE = 'Asia/Shanghai'
# 完成任务时上游用这个业务码表示「该任务次数已发放」（不是失败）。
TASK_ALREADY_GRANTED_CODE = 410121
# 客户端里能自动完成的任务类型（见参考实现 task-runner.js）。
AUTO_TASK_TYPES = ('QUERY_INPUT', 'USE_SKILL')

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

# ── 扫码登录（百度 Passport）─────────────────────────────────
# DuMate 的网页登录就是百度账号登录；官方页走 passport 的二维码协议
# （`qrloginfrom=pc` 会给同一套「百度 App / 百度智能云 App」二维码）。
# 扫码确认后 `channel/unicast` 回一次性 bduss，再在 `qrbdusslogin` 换正式
# BDUSS / STOKEN。所有请求都带同一份 cookie（BAIDUID 等由 passport 种下）。
PASSPORT_BASE = 'https://passport.baidu.com'
QRCODE_PATH = '/v2/api/getqrcode'
QRCODE_QUERY = '?lp=pc&qrloginfrom=pc'
QR_POLL_PATH = '/channel/unicast'
QR_LOGIN_PATH = '/v3/login/main/qrbdusslogin'

# 扫码确认后要落到 console 域才会种下 `bce-long-term-sessionid`：
# `/api/dumate/*` 的鉴权依赖它，只拿 passport 的 BDUSS 会一直 401。
# 与参考实现 `login-browser.js` 的 SETTLE_URLS 同口径。
CONSOLE_BASE = 'https://console.bce.baidu.com'
CONSOLE_SETTLE_URLS = (
    'https://console.bce.baidu.com/',
    'https://console.bce.baidu.com/ai/',
    'https://dumate.baidu.com/app',
)
# 只带着 passport 的 BDUSS 直接打 console 不一定能拿到会话；官方登录页
# 在拿到 passport 登录态后会走 `/postlogin/passport/wap` 做一次 SSO，
# 由服务端种下 `.bce.baidu.com` 域下的 `bce-long-term-sessionid`。
# 这两步都是普通 GET，用 httpx 就能复现（参考页面 bundle 的 ev()）。
BCE_LOGIN_BASE = 'https://login.bce.baidu.com'
BCE_LOGIN_REDIRECT = 'http://console.bce.baidu.com/'
BCE_POSTLOGIN_PATH = '/postlogin/passport/wap'
BCE_POSTLOGIN_QUERY = {
    'activate': 'true',
    'passportShared': 'true',
    'redirect': BCE_LOGIN_REDIRECT,
    'from': 'dumate',
}
# 落盘时只保留这些域下的 cookie（passport 自己的 PASSID / UBI 没有用）。
COOKIE_DOMAINS = ('baidu.com', 'dumate.cn')

# 扫码状态：unicast 是长轮询，但服务端也可能立刻回 `{errno:1}`；
# 两次查询之间留一点间隔，避免空转打满上游。
QR_WAIT_DELAY = 1.5
QR_POLL_TIMEOUT = 40.0
QR_CODE_TIMEOUT = 20.0
QR_LOGIN_TIMEOUT = 30.0
# 二维码有效期（百度页面约 5 分钟），登录会话按同一时长收尾。
QR_TTL = 300

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
