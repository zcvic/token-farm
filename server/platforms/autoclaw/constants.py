"""AutoClaw（智谱 / Z.ai）协议的常量。

协议事实来自 2026-11 的社区实现，逐项来源与「未实测」标注见
`deploy/autoclaw-ref.txt`。这里只放**稳定值**：地址、路径、客户端指纹与业务码；
签名 / URL 拼接 / 模型目录归一 / 错误分级在 `protocol.py`、`catalog.py`。

两个区域是两套互相独立的账号体系：

  · `cn`     —— +86 手机号 + 短信；账号面 `autoglm-api.zhipuai.cn`，
                推理面 `autoglm-acceleration-api.zhipuai.cn`；
  · `global` —— Google / z.ai OAuth；两件事都在 `autoglm-api.autoglm.ai`。

凭证不通用：国内 token 打国际域 401，反之亦然。国内为什么拆两个域名，见
`deploy/autoclaw-ref.txt` 第 1、2 节。

**国内账号面用哪个域名，参考实现互相矛盾**：VVVVVILK / ZFXing-lite 的全套
基址都是 `autoglm-acceleration-api`；hubo1989 的 CN_HOST 是 `autoglm-api`；
hj01857655 的实测结论是「账号面 `autoglm-api`、推理面 acceleration」。所以
这里把两处都保留成「主地址 + 回退地址」，不在常量层赌一个答案。接入真实账号
后只需调整 `SITES` 里的顺序。

国内 chat 路径的 `/v1` 同样有分歧：VVVVVILK / hubo 用带 `/v1` 的路径，hj 实测
国内 2.x 默认不带。`protocol.chat_paths()` 按 `contract` 给出「首选 + 回退」，
真正的顺序等联调后再收敛。
"""
from __future__ import annotations

from typing import Any

# ── 区域 ────────────────────────────────────────────────────
REALMS = ('cn', 'global')
DEFAULT_REALM = 'cn'
REALM_LABELS = {'cn': '国内版', 'global': '国际版'}

# ── 客户端常量（账号面与对话共用）───────────────────────────
APP_ID = '100003'
APP_SECRET = '38d2391985e2369a5fb8227d8e6cd5e5'
PRODUCT = 'autoclaw'
CLIENT_TYPE = 'pc'
DEFAULT_LANG = 'zh-CN'
# 服务端默认伪装成 Windows 客户端：`X-Tm` 写 `linux` 在部分实现里会被 403
# （参见 hj01857655 的兼容表）。需要别的值由调用方显式覆盖。
DEFAULT_TM = 'win'

#: 站点。每个字段都可能被参考实现中的一个子集使用，名称只表达用途：
#: `account` = 登录 / 刷新 / 用户信息 / 钱包 / 任务；
#: `inference` = `/autoclaw-proxy/proxy/*` 下的模型目录与对话。
SITES: dict[str, dict[str, Any]] = {
    'cn': {
        'realm': 'cn',
        'label': '国内版',
        'account': 'https://autoglm-api.zhipuai.cn',
        'account_fallbacks': ('https://autoglm-acceleration-api.zhipuai.cn',),
        'inference': 'https://autoglm-acceleration-api.zhipuai.cn',
        'inference_fallbacks': ('https://autoglm-api.zhipuai.cn',),
        'version': '2.1.0',
        'channel': 'AutoClaw26',
        'contract': 'v2',
        'login': 'sms',
    },
    'global': {
        'realm': 'global',
        'label': '国际版',
        'account': 'https://autoglm-api.autoglm.ai',
        'account_fallbacks': (),
        'inference': 'https://autoglm-api.autoglm.ai',
        'inference_fallbacks': (),
        'version': '1.18.1',
        'channel': 'AutoClaw4',
        'contract': 'v1',
        'login': 'oauth',
    },
}

# ── 路径 ────────────────────────────────────────────────────
# 国内短信登录。`agent-login/` 的末尾斜杠必须保留：少了就是另一个路由。
SEND_CODE_PATH = '/userapi/v1/agent-send-code'
LOGIN_PATH = '/userapi/v1/agent-login/'
# 刷新 / 续期。业务码 400002 时回落到 agent-refresh。
REFRESH_PATH = '/userapi/v1/refresh'
AGENT_REFRESH_PATH = '/userapi/v1/agent-refresh'
USER_INFO_PATH = '/userapi/v1/user-info'

# 国际 OAuth（z.ai / Google）。回调端口是官方 client 白名单，不能随便换。
OAUTH_CAPTCHA_PATH = '/userapi/overseasv1/oauth-captcha-config'
OAUTH_URL_PATH = '/userapi/overseasv1/{vendor}-oauth-url'
OAUTH_LOGIN_PATH = '/userapi/overseasv1/{vendor}-oauth-login'
OAUTH_VENDORS = ('zai', 'google')
OAUTH_CALLBACK_PORTS = (18432, 19654, 19723, 53699)
OAUTH_CALLBACK_TEMPLATE = 'http://localhost:{port}/auth/callback-{vendor}'

# 模型目录 / 对话（推理面）。
MODEL_CONFIG_PATH = '/autoclaw-proxy/proxy/autoclaw-model-config'
CHAT_PATH = '/autoclaw-proxy/proxy/autoclaw/v1/chat/completions'
CHAT_PATH_NO_V1 = '/autoclaw-proxy/proxy/autoclaw/chat/completions'

# 任务 / 福利（推理面的同域代理路径）。
TASK_COMPLETE_PATH = '/autoclaw-proxy/proxy/autoclaw-task-complete'
TASK_LIST_PATH = '/autoclaw-proxy/proxy/autoclaw-task-list'
TASK_DAILY_SIGNIN = 'daily_signin'
TASK_INSPIRATION = 'daily_inspiration_center'

# 积分 / 账单（账号面）。
WALLETS_PATH = '/agent-assetmgr/api/v2/wallets?biz_app_id=autoclaw'
WALLET_INSTANCES_PATH = ('/agent-assetmgr/api/v1/wallet-instances'
                         '?wallet_type=all&wallet_scope=all')
LEDGERS_PATH = '/agent-assetmgr/api/v1/ledgers_std'
POINTS_EXPIRING_PATH = '/agent-assetmgr/api/v1/points/expiring'

# ── 业务码 ──────────────────────────────────────────────────
# 有业务码的响应仍可能是 HTTP 200；分级逻辑在 `protocol.classify_error()`。
BIZ_NOT_LOGGED_IN = '410000'
BIZ_AUTH_EXPIRED = '400000'
BIZ_REFRESH_FALLBACK = '400002'
BIZ_MODEL_QUOTA = '810000'
SESSION_BIZ_CODES = frozenset({BIZ_NOT_LOGGED_IN, BIZ_AUTH_EXPIRED})

# ── 超时 / 寿命 ─────────────────────────────────────────────
TOKEN_TIMEOUT = 30
ACCOUNT_TIMEOUT = 15
MODELS_TIMEOUT = 15
# token 提前这么久视为过期（与其它平台一致的两分钟）。
TOKEN_SKEW_SECONDS = 120
# 响应既没有 JWT `exp` 也没有 `expires_in` 时的兜底寿命。
# 参考实现（hubo）用 5 小时；宁可早刷新，不要把过期 token 当成可用。
TOKEN_FALLBACK_SECONDS = 5 * 3600
