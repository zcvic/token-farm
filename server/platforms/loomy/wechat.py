"""微信开放平台「网站应用」扫码登录（Loomy 唯一的网页授权链路）。

来源：客户端 `electron/xfyun/wechat-oauth.js` + `account-service.js`，以及
参考实现 `loomy2api/wechat.py`（MIT）。链路：

  1. 打开授权页（本模块 `build_auth_url`）::

       https://open.weixin.qq.com/connect/qrconnect
         ?appid=wx18d60be432287cf8
         &redirect_uri=<https://loomy.xunfei.cn/oauth/wechat/callback>
         &response_type=code&scope=snsapi_login&state=<32hex>#wechat_redirect

  2. 用户扫码确认后，微信 302 到 `redirect_uri?code=…&state=…`。
     **该路径线上是 404**：官方客户端把授权页塞进 Electron `BrowserWindow`，
     在 `will-redirect` 里截住跳转，`code` 从来不出进程。

  3. 用 `code` 换 session（账号服务）::

       POST /login/thirdAccount/bind/auth  {tcode:{code}, type:"wx"} → {bind, rcode}
         bind=1 → POST /login/thirdAccount/bind/skip   {rcode} → session
         bind=0 → bind/sendMsg + bind/checkCode（绑手机号，需要短信验证码）

## 为什么除了「粘链接」还要「服务端取码」

回调域被微信白名单锁死在 `loomy.xunfei.cn`（只校验域名，不校验路径；
`loomy.xunfei.cn@127.0.0.1:port` 这类 userinfo 绕过一律被拒），面板进程
拿不到浏览器地址栏。于是有两条互补的路：

  · `fetch_login_qr` 直接把授权页拉下来，从 HTML 里抠出 `uuid`（页面自己留了
    `fordevtool = "…l/qrconnect?uuid=…"`），把二维码图片交给前端；再用
    `poll_login` 长轮询拿 `wx_code` —— 用户只扫码，不用切窗口。
  · 服务端连不上微信时退化成「粘链接」：用户在浏览器里扫码，把 404 页地址栏
    里那条链接粘回面板，由 `parse_callback` 解出 `code`。

## 代理口径

微信在公网，走**全局代理**（`config.effective_http_proxy()`，没配置就直连），
与账号服务同一条出口；不读环境变量里的 `HTTP(S)_PROXY`（`trust_env=False`），
避免代理只配给上游时把微信也绕进去。
"""
from __future__ import annotations

import re
import time
import urllib.parse
from typing import Any

import httpx

from ... import config
from ..base import PlatformError
from . import constants as C

__all__ = [
    'build_auth_url', 'parse_callback', 'qr_image_url', 'fetch_login_qr',
    'poll_login', 'ERR_WAITING', 'ERR_SCANNED', 'ERR_CONFIRMED',
    'ERR_CANCELLED', 'ERR_EXPIRED', 'ERRCODE_TEXT',
]

# 长轮询状态码（授权页 JS 里的 switch 分支）
ERR_WAITING = 408        # 未扫码
ERR_SCANNED = 404        # 已扫码，等用户在手机上点确认
ERR_CONFIRMED = 405      # 已确认，响应里带 wx_code
ERR_CANCELLED = 403      # 用户在手机上取消
ERR_EXPIRED = 402        # 二维码过期

ERRCODE_TEXT = {
    ERR_WAITING: '等待扫码',
    ERR_SCANNED: '已扫码，请在手机上确认',
    ERR_CONFIRMED: '已确认',
    ERR_CANCELLED: '已在手机上取消',
    ERR_EXPIRED: '二维码已过期',
}

#: 微信 code 是字母数字与 `-_` 的一次性授权码（对齐客户端校验）。
_CODE_RE = re.compile(r'^[A-Za-z0-9_-]{1,512}$')
#: 我方 state 是 32 位 hex；微信原样回传。
_STATE_RE = re.compile(r'^[A-Za-z0-9_.:-]{1,256}$')
_MAX_CALLBACK_LEN = 16 * 1024

_HEADERS = {
    # 微信会对明显的脚本 UA 返回精简页，取 uuid 需要普通浏览器 UA。
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                   'AppleWebKit/537.36 (KHTML, like Gecko) '
                   'Chrome/126.0.0.0 Safari/537.36'),
    'Referer': 'https://open.weixin.qq.com/',
    'Accept-Language': 'zh-CN,zh;q=0.9',
}


# ── URL ─────────────────────────────────────────────────────
def build_auth_url(state: str) -> str:
    """拼装微信扫码授权页 URL（与客户端 `buildAuthUrl` 逐字段一致）。"""
    state = str(state or '').strip()
    if not state:
        raise PlatformError('缺少微信登录 state', status=400,
                            code='login_state_required')
    query = urllib.parse.urlencode({
        'appid': C.WECHAT_APP_ID,
        'redirect_uri': C.WECHAT_REDIRECT_URI,
        'response_type': 'code',
        'scope': C.WECHAT_SCOPE,
        'state': state,
    }, quote_via=urllib.parse.quote)
    return f'{C.WECHAT_AUTH_BASE}?{query}#wechat_redirect'


def qr_image_url(uuid: str) -> str:
    return C.WECHAT_QRCODE_BASE + str(uuid or '')


# ── HTTP ────────────────────────────────────────────────────
def _get(url: str, *, timeout: float) -> str:
    """GET 一个微信页面 / 接口，错误统一成 `PlatformError`。

    超时单独给一个 code（`wechat_timeout`）：长轮询到点没状态变化是**正常**
    情况，调用方要把它当成「继续等」而不是登录失败。
    """
    kwargs: dict[str, Any] = {
        'timeout': httpx.Timeout(timeout, connect=10),
        'trust_env': False,
        'follow_redirects': True,
    }
    proxy = config.effective_http_proxy()
    if proxy:
        kwargs['proxy'] = proxy
    try:
        with httpx.Client(**kwargs) as client:
            response = client.get(url, headers=_HEADERS)
    except httpx.TimeoutException as exc:
        raise PlatformError('微信服务响应超时', status=504,
                            code='wechat_timeout') from exc
    except Exception as exc:  # noqa: BLE001 —— 网络异常统一成可重试错误
        raise PlatformError(f'微信服务不可达：{exc}', status=502,
                            code='wechat_unreachable') from exc
    if response.status_code >= 400:
        raise PlatformError(f'微信服务返回 HTTP {response.status_code}',
                            status=502, code='wechat_upstream')
    return response.text


def fetch_login_qr(state: str, *, timeout: float = 15) -> dict:
    """拉一次授权页并解出 `uuid`（服务端取码 + 内嵌二维码都用它）。

    页面里官方自己留了线索::

        var fordevtool = "https://long.open.weixin.qq.com/connect/l/qrconnect?uuid=<uuid>"

    页面改版时退回「任意 `uuid=` 参数」的宽松匹配。
    """
    auth_url = build_auth_url(state)
    page = _get(auth_url, timeout=timeout)
    found = re.search(r'fordevtool\s*=\s*"([^"]*uuid=([A-Za-z0-9_\-]+))"', page)
    if found:
        uuid = found.group(2)
    else:
        loose = re.search(r'uuid=([A-Za-z0-9_\-]{10,})', page)
        if not loose:
            raise PlatformError('微信授权页里没有二维码标识（页面可能已改版）',
                                status=502, code='wechat_no_qr')
        uuid = loose.group(1)
    return {'uuid': uuid, 'qr_url': qr_image_url(uuid), 'auth_url': auth_url}


def poll_login(uuid: str, *, timeout: float = 25) -> dict:
    """长轮询一次扫码状态：`{errcode, code, text}`。

    `code` 只在 `errcode == 405` 时非空。超时按「未扫码」返回，网络异常照常
    抛 `PlatformError`（调用方决定是否重试）。
    """
    uuid = str(uuid or '').strip()
    if not uuid:
        raise PlatformError('缺少扫码 uuid', status=400, code='wechat_no_uuid')
    query = urllib.parse.urlencode({
        'uuid': uuid,
        '_': str(int(time.time() * 1000)),
    })
    try:
        body = _get(f'{C.WECHAT_LONGPOLL_URL}?{query}', timeout=timeout)
    except PlatformError as exc:
        if exc.code == 'wechat_timeout':
            return {'errcode': ERR_WAITING, 'code': '', 'text': ERRCODE_TEXT[ERR_WAITING]}
        raise
    err = re.search(r'wx_errcode\s*=\s*(\d+)', body)
    code = re.search(r"wx_code\s*=\s*'([^']*)'", body)
    errcode = int(err.group(1)) if err else 0
    value = (code.group(1) if code else '') or ''
    return {'errcode': errcode, 'code': value,
            'text': ERRCODE_TEXT.get(errcode, f'未知状态 {errcode}')}


# ── 回调链接 ────────────────────────────────────────────────
def parse_callback(text: str) -> dict:
    """从用户粘回来的内容里解出 `{code, state}`。

    接受三种形态（用户很难粘错）：

      · 整条回调 URL：`https://loomy.xunfei.cn/oauth/wechat/callback?code=…&state=…`
      · 只有 query：`code=…&state=…`
      · 只有 `code` 本身

    解析失败一律抛 `PlatformError`（400），不返回半截结果。
    """
    raw = str(text or '').strip().strip('"').strip("'")
    if not raw:
        raise PlatformError('请粘贴微信回调链接（或授权 code）', status=400,
                            code='callback_required')
    if len(raw) > _MAX_CALLBACK_LEN:
        raise PlatformError('回调地址过长', status=400, code='callback_too_long')

    looks_like_url = ('?' in raw) or ('=' in raw) \
        or raw.lower().startswith('http')
    if looks_like_url:
        query = raw.split('?', 1)[1] if '?' in raw else raw
        query = query.split('#', 1)[0]
        params = urllib.parse.parse_qs(query, keep_blank_values=False)
        code = (params.get('code') or [''])[0].strip()
        state = (params.get('state') or [''])[0].strip()
        if not code:
            # 用户点了「取消」时微信回的是 errmsg=access_denied，说清楚原因
            # 比报「code 格式不对」有用。
            reason = ((params.get('errmsg') or params.get('error') or [''])[0]
                      or '微信未返回 code')
            raise PlatformError(f'微信授权失败：{reason}', status=400,
                                code='login_denied')
    else:
        code = raw
        state = ''

    if not code:
        raise PlatformError('没解析出 code，请确认粘贴的是完整回调链接',
                            status=400, code='callback_invalid')
    if not looks_like_url and len(code) < 8:
        raise PlatformError(
            f'粘进来的只有 {len(code)} 个字符，不是微信回调链接：'
            '请在跳转后的页面按 Ctrl+L 再 Ctrl+C 复制地址栏整条链接',
            status=400, code='callback_invalid')
    if not _CODE_RE.match(code):
        raise PlatformError(
            '这条内容里的 code 不合法（含非法字符或过长），'
            '请确认复制的是地址栏里以 loomy.xunfei.cn/oauth/wechat/callback '
            '开头的链接', status=400, code='callback_invalid')
    if state and not _STATE_RE.match(state):
        raise PlatformError('回调里的 state 格式不对，请重新复制',
                            status=400, code='callback_invalid')
    return {'code': code, 'state': state}
