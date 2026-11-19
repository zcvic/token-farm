"""Loomy 协议常量。

来源：官方客户端 `resources/.env.prod` 与 `electron/xfyun/*`，以及参考实现
`loomy2api`（MIT）。这些都是**客户端常量**，不是服务端密钥；上游只要求请求头
里带上它们。
"""
from __future__ import annotations

# ── 上游 ────────────────────────────────────────────────────
#: 模型网关（OpenAI 兼容：/chat/completions、/models、/points/records…）
MODEL_BASE = 'https://loomyad.xunfei.cn/api/v1'
#: 讯飞账号服务（登录 / 用户信息）
ACCOUNT_BASE = 'https://account.xfinfr.com'
ACCOUNT_APPID = 'GM3LOOMY'

#: 客户端自带的账号服务签名 AK/SK（每个安装包里都有，属于客户端常量）。
ACCESS_KEY_ID = '2thryby66wxi53sk'
ACCESS_KEY_SECRET = 'zsak6eadrbawz683wf5r3m2snrwj868r'

#: `loomy-version` 只做「存在性」校验；0.9.39 是当前最新客户端。
LOOMY_VERSION = '0.9.39'

# ── 微信扫码登录 ────────────────────────────────────────────
#: 客户端唯一的「跳转授权」链路：微信开放平台「网站应用」扫码。
#: AppID 与回调域都是客户端 `resources/.env.prod` 里的常量；回调域被
#: 白名单锁死在 `loomy.xunfei.cn`，本机回环地址会被拒（见 docs/loomy.md）。
WECHAT_APP_ID = 'wx18d60be432287cf8'
WECHAT_REDIRECT_URI = 'https://loomy.xunfei.cn/oauth/wechat/callback'
WECHAT_AUTH_BASE = 'https://open.weixin.qq.com/connect/qrconnect'
WECHAT_SCOPE = 'snsapi_login'
WECHAT_THIRD_TYPE = 'wx'
#: 由 `uuid` 直接出的二维码图片（面板内嵌 `<img>` 用）
WECHAT_QRCODE_BASE = 'https://open.weixin.qq.com/connect/qrcode/'
#: 扫码状态长轮询；命中 405 时响应里带 `wx_code`
WECHAT_LONGPOLL_URL = 'https://long.open.weixin.qq.com/connect/l/qrconnect'

# ── 账号 ────────────────────────────────────────────────────
#: 客户端声称的会话时长。**仅用于展示**：登录响应里没有可靠过期字段，
#: 实测寿命更长，凭证死活的唯一权威是上游 401/403。
SESSION_EXPIRE_SECONDS = 14 * 24 * 3600
DEVICE_ID = 'web'
WEB_MODEL_ID = 'Web'
CLIENT_VERSION = '1.0.0'
CLIENT_UA = 'Loomy|Desktop|Electron|macOS'
CAMPUS_DEVICE_ID_PREFIX = 'loomy-campus-'

# ── 模型 ────────────────────────────────────────────────────
#: 对外模型 id → 上游线 id。上游有两处「名字对不上」：
#:
#:   · `DeepSeek-V4.1-Flash` 对外展示成官方小写 id `deepseek-v4.1-flash`；
#:   · 1017 快照上游 API 省略了 `.1`（`deepseek-v4-flash-1017`），
#:     对外仍用官方 id `deepseek-v4.1-flash-1017`。
#:
#: 请求时必须翻回上游 id，否则上游会静默回落到默认模型（实测 spark-x2.6）。
MODEL_ALIASES = {
    'deepseek-v4.1-flash': 'DeepSeek-V4.1-Flash',
    'deepseek-v4.1-flash-1017': 'deepseek-v4-flash-1017',
}
REVERSE_MODEL_ALIASES = {wire: outward for outward, wire in MODEL_ALIASES.items()}

DEFAULT_MODEL = 'deepseek-v4.1-flash-1017'

#: 上游目录拉取失败时的兜底。`multiplier` 是客户端展示的积分倍率
#: （`None` = 未知，不要当成 0）；`type` 不是 `chat` 的模型不会出现在
#: token-farm 的模型清单里（本仓库只有 chat/completions 一条上游通路）。
FALLBACK_MODELS = (
    {'id': 'spark-x2.6', 'name': 'Spark-X2.6 正式版(限时免费)(x0.0)',
     'type': 'chat', 'multiplier': 0.0, 'context_length': 1048576,
     'max_tokens': 131072, 'modalities': ['text']},
    {'id': 'deepseek-v4.1-flash', 'name': 'DeepSeek V4.1 Flash(x2.0)',
     'type': 'chat', 'multiplier': 2.0, 'context_length': 1048576,
     'max_tokens': 384000, 'modalities': ['text', 'image']},
    {'id': 'deepseek-v4.1-flash-1017', 'name': 'DeepSeek V4.1 Flash 1017(x2.0)',
     'type': 'chat', 'multiplier': 2.0, 'context_length': 1048576,
     'max_tokens': 384000, 'modalities': ['text', 'image']},
    {'id': 'GLM-5.3-Flash', 'name': 'GLM 5.3 Flash(x0.8)',
     'type': 'chat', 'multiplier': 0.8, 'context_length': 1048576,
     'max_tokens': 131072, 'modalities': ['text', 'image', 'video']},
    {'id': 'qwen3.8-flash', 'name': 'qwen 3.8 flash（x0.8）',
     'type': 'chat', 'multiplier': 0.8, 'context_length': 1000000,
     'max_tokens': 131072, 'modalities': ['text', 'image', 'video']},
    {'id': 'Hy-Image-3.5-preview', 'name': 'Hy image 3.5 preview',
     'type': 'image', 'multiplier': 0.0, 'context_length': 100000,
     'max_tokens': 0, 'modalities': ['text', 'image']},
    {'id': 'doubao-seedream-5-lite', 'name': 'Doubao seedream 5 lite',
     'type': 'image', 'multiplier': 0.0, 'context_length': 128000,
     'max_tokens': 0, 'modalities': ['text', 'image']},
    {'id': 'qwen-image-3.0-pro', 'name': 'qwen image 3.0 pro',
     'type': 'image', 'multiplier': 0.0, 'context_length': 0,
     'max_tokens': 0, 'modalities': ['text', 'image']},
    {'id': 'mimo-v2.5', 'name': 'MiMo V2.5（x3.3）',
     'type': 'chat', 'multiplier': 3.3, 'context_length': 1024000,
     'max_tokens': 128000, 'modalities': ['text', 'image', 'audio', 'video']},
    {'id': 'auto', 'name': '自动 · 均衡',
     'type': 'chat', 'multiplier': None, 'context_length': 0,
     'max_tokens': 0, 'modalities': ['text']},
)


def wire_model(model: object) -> str:
    """对外模型名 → 上游实际认的模型名。"""
    name = str(model or '').strip() or DEFAULT_MODEL
    return MODEL_ALIASES.get(name, name)


def outward_model(model: object) -> str:
    """上游模型名 → 对外模型名。"""
    name = str(model or '').strip()
    return REVERSE_MODEL_ALIASES.get(name, name)
