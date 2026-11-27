"""AutoClaw 协议的纯函数层。

本模块**不联网、不读磁盘、不改全局状态**：区域归一、地址拼接、客户端签名、
JWT 解析与错误分级都放在这里，便于离线单测。HTTP、账号存储与调度分别在
`client.py`、`store.py`、`adapter.py`（按 `docs/autoclaw.md` 的阶段 3+ 落地）。

设计原则与其它平台一致：只做协议翻译，不猜上游意图。参考实现里「改写 system
prompt 过风控」「静默 force-model」那类行为不在这里，也不在适配器里。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any

from . import constants as C

# ── 区域与地址 ──────────────────────────────────────────────
_REALM_ALIASES = {
    'cn': 'cn', 'china': 'cn', 'domestic': 'cn', '国内': 'cn',
    'global': 'global', 'intl': 'global', 'international': 'global',
    'us': 'global', 'com': 'global', '海外': 'global', '国际': 'global',
}


def normalize_realm(value: object, default: str = C.DEFAULT_REALM) -> str:
    """任意写法 → 协议口径（`cn` / `global`）；认不出用 `default`。"""
    text = str(value or '').strip().lower()
    return (_REALM_ALIASES.get(text)
            or _REALM_ALIASES.get(str(default or '').strip().lower(),
                                  C.DEFAULT_REALM))


def site(realm: object) -> dict[str, Any]:
    """该区域的站点配置（常量表的只读视图；调用方不要原地修改）。"""
    return C.SITES[normalize_realm(realm)]


def _dedupe(values: object) -> tuple[str, ...]:
    out: list[str] = []
    for value in values if isinstance(values, (list, tuple)) else ():
        text = str(value or '').strip().rstrip('/')
        if text and text not in out:
            out.append(text)
    return tuple(out)


def account_hosts(realm: object) -> tuple[str, ...]:
    """账号面基址：主地址在前，回退地址在后。"""
    cfg = site(realm)
    return _dedupe((cfg['account'], *cfg.get('account_fallbacks', ())))


def inference_hosts(realm: object) -> tuple[str, ...]:
    """推理面基址：主地址在前，回退地址在后。"""
    cfg = site(realm)
    return _dedupe((cfg['inference'], *cfg.get('inference_fallbacks', ())))


def catalog_hosts(realm: object) -> tuple[str, ...]:
    """模型目录候选基址。

    `autoclaw-model-config` 挂在 `/autoclaw-proxy/proxy/` 下，归推理面；但国内
    两个域名的分工本身还有分歧，所以推理面试完再试账号面。去重后返回。
    """
    return _dedupe((*inference_hosts(realm), *account_hosts(realm)))


def join(base: object, path: object) -> str:
    """基址 + 路径，保证中间只有一个斜杠（路径里的 `?query` 原样保留）。"""
    text = str(base or '').strip().rstrip('/')
    tail = str(path or '').strip()
    if not text:
        return ''
    if not tail:
        return text
    return f'{text}/{tail.lstrip("/")}'


def chat_paths(realm: object) -> tuple[str, ...]:
    """对话路径候选。

    国际（`contract = v1`）只有带 `/v1` 的一条；国内 2.x 另有「不带 `/v1`」
    的写法。两边都保留，首选按区域契约排，联调后可以收敛成一条。
    """
    cfg = site(realm)
    if cfg.get('contract') == 'v2':
        return (C.CHAT_PATH_NO_V1, C.CHAT_PATH)
    return (C.CHAT_PATH,)


def chat_url(base: object, realm: object, *, path: str | None = None) -> str:
    """对话完整 URL；不传 `path` 时用该区域的首选路径。"""
    return join(base, path or chat_paths(realm)[0])


def model_config_url(base: object) -> str:
    return join(base, C.MODEL_CONFIG_PATH)


def oauth_callback_url(port: int, vendor: str) -> str:
    """官方 OAuth 回调地址；端口取自 `OAUTH_CALLBACK_PORTS` 白名单。"""
    name = str(vendor or '').strip().lower()
    if name not in C.OAUTH_VENDORS:
        raise ValueError(f'未知的 OAuth 提供方：{vendor!r}')
    return C.OAUTH_CALLBACK_TEMPLATE.format(port=int(port), vendor=name)


# ── 客户端签名 ──────────────────────────────────────────────
def sign(timestamp: object, *, app_id: str = C.APP_ID,
         secret: str = C.APP_SECRET) -> str:
    """`X-Auth-Sign = md5("<appid>&<ts>&<secret>")`。"""
    ts = str(timestamp).strip()
    return hashlib.md5(f'{app_id}&{ts}&{secret}'.encode('utf-8')).hexdigest()


_TM_ALIASES = {
    'win': 'win', 'win32': 'win', 'windows': 'win',
    'mac': 'mac', 'macos': 'mac', 'darwin': 'mac', 'osx': 'mac',
    'linux': 'linux', 'linux-x64': 'linux', 'linux64': 'linux',
}


def client_tm(platform: object = None) -> str:
    """平台名 → `X-Tm` 值（`win` / `mac` / `linux`）；认不出用默认值。

    参考实现里 `X-Tm: linux`（而不是 `linux-x64`）在部分场景会 403，所以默认
    伪装成 `win`；真要发 `linux` 时由调用方显式传 `platform='linux-x64'`。
    """
    text = str(platform or '').strip().lower()
    if not text:
        return C.DEFAULT_TM
    if text in _TM_ALIASES:
        return _TM_ALIASES[text]
    for prefix, value in (('darwin', 'mac'), ('mac', 'mac'),
                          ('linux', 'linux'), ('win', 'win')):
        if text.startswith(prefix):
            return value
    return C.DEFAULT_TM


def signed_headers(*, realm: object = C.DEFAULT_REALM,
                   timestamp: object = None, version: object = None,
                   channel: object = None, platform: object = None,
                   client_type: object = None, trace_id: object = None,
                   lang: object = None) -> dict[str, str]:
    """构造账号面 / 对话共用的客户端签名头。

    `timestamp` / `trace_id` 可注入，测试才能断言确定值；运行时不传即用当前
    秒与 16 位随机 hex。`version` / `channel` 默认取区域常量，但允许调用方
    覆盖——1.18.x 与 2.x 的请求体契约不同，版本必须与请求体成对出现。
    """
    cfg = site(realm)
    ts = str(int(timestamp)) if timestamp is not None else str(int(time.time()))
    ver = str(version or cfg['version']).strip()
    trace = str(trace_id or secrets.token_hex(8)).strip()
    return {
        'X-Auth-Appid': C.APP_ID,
        'X-Auth-TimeStamp': ts,
        'X-Auth-Sign': sign(ts),
        'X-Product': C.PRODUCT,
        'X-Client-Type': str(client_type or C.CLIENT_TYPE).strip(),
        'X-Version': ver,
        'X-Tm': client_tm(platform),
        'X-Channel': str(channel or cfg['channel']).strip(),
        'X-Trace-Id': trace,
        'X-Lang': str(lang or C.DEFAULT_LANG).strip(),
        'User-Agent': f'AutoClaw/{ver}',
    }


def bearer_headers(token: object, *, standard: bool = True) -> dict[str, str]:
    """Bearer 鉴权头。

    参考实现里国际版同时发 `Authorization` 与 `X-Authorization`，国内 2.x
    有「只认 `X-Authorization`」的说法。默认两个都发，`standard=False` 时只发
    后者；最终取哪种由联调结论决定，不在这里写死某一个区域。
    """
    text = str(token or '').strip()
    if text[:7].lower() == 'bearer ':
        text = text[7:].strip()
    if not text:
        return {}
    value = f'Bearer {text}'
    headers = {'X-Authorization': value}
    if standard:
        headers['Authorization'] = value
    return headers


# ── JWT ─────────────────────────────────────────────────────
def jwt_claims(token: object) -> dict[str, Any]:
    """解出 JWT 的 payload；**不验签**，解析失败返回空字典。

    只用于读 `exp` / `device_id` / `source_id` 这类客户端自己就知道的声明，
    不拿它做安全判定。
    """
    text = str(token or '').strip()
    if text[:7].lower() == 'bearer ':
        text = text[7:].strip()
    parts = text.split('.')
    if len(parts) != 3 or not parts[1]:
        return {}
    segment = parts[1] + '=' * (-len(parts[1]) % 4)
    try:
        raw = base64.urlsafe_b64decode(segment.encode('ascii'))
        data = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return {}
    return data if isinstance(data, dict) else {}


def token_expiry(token: object) -> int:
    """JWT 的 `exp`（epoch 秒）；没有 / 不是数字返回 0。"""
    value = jwt_claims(token).get('exp')
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


def realm_hint(token: object) -> str | None:
    """从 JWT 认区域：`source_id` 以 `agent` 开头视为国内短信号。

    认不出返回 None，由账号记录里的 `realm` 兜底——**不要**因为它为空就猜
    另一区，那会把国内 token 发到国际域，结果只有 401。
    """
    claims = jwt_claims(token)
    source = str(claims.get('source_id') or claims.get('sourceId') or '')
    if source.strip().lower().startswith('agent'):
        return 'cn'
    return None


# ── 错误分级 ────────────────────────────────────────────────
#: 账号会话失效（HTTP 401）：需要重新登录，冷却没有意义。
KIND_SESSION = 'session_dead'
#: 积分 / 单模型额度耗尽（HTTP 402 或业务码 810000）。
KIND_CREDIT = 'hard_credit'
#: 限流（HTTP 429）：短冷却后可以重试。
KIND_SOFT_RATE = 'soft_rate'
#: 上游 5xx / 链路抖动：可换号，但不冻号。
KIND_TRANSIENT = 'transient'
#: 客户端请求本身有问题（4xx）：原样回给调用方。
KIND_CLIENT = 'client'
#: 风控 / 内容拦截：不是协议错误，也不能靠换号绕过。
KIND_BLOCKED = 'blocked'


@dataclass(frozen=True)
class Failure:
    """一次上游失败的分类结果；适配器据此决定回什么状态码、冻不冻号。"""

    kind: str
    status: int
    code: str
    message: str
    retry_after: int | None = None
    model_scoped: bool = False
    disable_account: bool = False
    biz_code: str = ''


_CODE_KEYS = ('code', 'biz_code', 'bizCode', 'err_code', 'errCode', 'errcode')


def _numeric_code(value: object) -> str:
    """只接受纯数字业务码；OpenAI 风格的字符串 `code` 不当作业务码。"""
    if isinstance(value, bool) or value is None:
        return ''
    if isinstance(value, int):
        return str(value)
    text = str(value).strip()
    return text if text.isdigit() else ''


def biz_code_of(payload: object) -> str:
    """从响应体里取业务码，兼容 `code` 在顶层 / `data` / `error` 的写法。"""
    if not isinstance(payload, dict):
        return ''
    for key in _CODE_KEYS:
        code = _numeric_code(payload.get(key))
        if code:
            return code
    for container in ('data', 'error', 'result'):
        node = payload.get(container)
        if not isinstance(node, dict):
            continue
        for key in _CODE_KEYS:
            code = _numeric_code(node.get(key))
            if code:
                return code
    return ''


def message_of(payload: object, fallback: str = '') -> str:
    """从各种错误体里取一句可读文案。"""
    if isinstance(payload, dict):
        error = payload.get('error')
        if isinstance(error, dict):
            for key in ('message', 'msg', 'detail'):
                if error.get(key):
                    return str(error[key]).strip()
        if isinstance(error, str) and error.strip():
            return error.strip()
        for key in ('message', 'msg', 'detail', 'error_message'):
            if payload.get(key):
                return str(payload[key]).strip()
        data = payload.get('data')
        if isinstance(data, dict):
            for key in ('message', 'msg', 'detail'):
                if data.get(key):
                    return str(data[key]).strip()
    elif isinstance(payload, str) and payload.strip():
        return payload.strip()
    return fallback


def _retry_after(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0, int(float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def is_dangerous_activity(value: object) -> bool:
    """上游把风控拦截当正常正文 / 错误文案返回时的标记。"""
    if isinstance(value, dict):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value or '')
    return 'DANGEROUS_ACTIVITY_BLOCKED' in text.upper()


def classify_error(status: int, *, biz_code: object = None, message: object = '',
                   body: object = None,
                   retry_after: object = None) -> Failure | None:
    """把 HTTP 状态 + 业务码映射成 `Failure`；不是错误返回 None。

    与参考实现 `internal/protocol/errors.go` 的分级一致，但有两个仓库内的
    约定：

      · 5xx 统一报 **502**（与其它平台一致），不原样透传 500/503；
      · 403 视为风控 / 拒绝，**不**当会话失效——国内把 `X-Tm` 写成 `linux`
        也会 403，冻号会误伤一池健康账号。
    """
    code = _numeric_code(biz_code) or biz_code_of(body)
    text = str(message or '').strip() or message_of(body)
    if not text:
        text = f'上游 HTTP {status}' if status else '上游返回错误'

    if is_dangerous_activity(text) or is_dangerous_activity(body):
        return Failure(KIND_BLOCKED, 403, 'content_blocked', text, biz_code=code)
    if code in C.SESSION_BIZ_CODES:
        return Failure(KIND_SESSION, 401, 'login_expired', text,
                       disable_account=True, biz_code=code)
    if code == C.BIZ_MODEL_QUOTA:
        return Failure(KIND_CREDIT, 402, 'model_quota_exhausted', text,
                       model_scoped=True, biz_code=code)
    if status == 401:
        return Failure(KIND_SESSION, 401, 'login_expired', text,
                       disable_account=True, biz_code=code)
    if status == 402:
        return Failure(KIND_CREDIT, 402, 'insufficient_credits', text,
                       biz_code=code)
    if status == 403:
        return Failure(KIND_BLOCKED, 403, 'forbidden', text, biz_code=code)
    if status == 429:
        return Failure(KIND_SOFT_RATE, 429, 'rate_limited', text,
                       retry_after=_retry_after(retry_after), biz_code=code)
    if status >= 500:
        return Failure(KIND_TRANSIENT, 502, 'upstream_error', text,
                       biz_code=code)
    if status >= 400:
        return Failure(KIND_CLIENT, status, 'bad_request', text, biz_code=code)
    return None
