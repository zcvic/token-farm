"""WorkBuddy 反代的**传输层**：一份实现，两处复用。

## 为什么单独一层

WorkBuddy 的请求原本只能在网关里发（`_chat` 的直连分支）。现在它还要能作为
「模型组合」的成员被调用（组合适配器 → 这里 → 上游）。若两边各写一遍转发链，
需要同步的东西有一长串：请求头、超时、SSE 逐字节透传、`11102 → 404` 的改判、
429 的 `Retry-After`、首字延迟、usage 提取。任何一处漏同步都只会**在其中一条
路径**上表现出来，而且很难看出是「两套实现漂移」。

所以这一层只做「怎么把请求发出去、怎么把响应收回来」，**不碰记账、不碰鉴权**：

  · 记账由调用方做**一次**（网关 `_record` / 组合的 `_platform_chat`），不会重复；
  · 鉴权（key / ip / 版本 / 白名单 / 限流）仍由网关的 `_authorize` 负责，
    本层不重判——组合内部的成员调用不该把调用方的权限再走一遍。

## 返回形态

`ForwardOutcome` 与 FastAPI 无关（不返回 `Response`），这样组合适配器才能把它
翻译成 `PlatformChat`。流式时 `stream` 是**上游原始字节**：WorkBuddy 的转发是
逐字节透传（见 `test_gateway_passthrough`），解析成 dict 再拼回去会改变字节、
丢掉未知帧。
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import time
from dataclasses import dataclass, field
from typing import AsyncIterator
from urllib.parse import urlsplit

from .. import config, upstreamsvc
from . import proxyroutes

# ── 上游响应里那些「必须翻译」的细节 ──────────────────────
_RETRY_AT_RE = re.compile(
    r'将在\s*(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})'
    r'(?:\s*UTC\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?)?')


def retry_after_seconds(text: str | None) -> int | None:
    """把上游 429 文案里的「将在 YYYY-MM-DD HH:MM:SS [UTC±N] 重置」换成秒数。

    上游 6004 的正文自带重置墙钟，客户端却只能从中文里自己抠——退回给它们一个
    标准头，重试节奏由协议决定，不靠猜。**时区以文案里的 `UTC±N` 为准**（缺省
    才按面板本地时间），否则面板跑在 UTC 时算出的间隔会整体差 8 小时。
    """
    if not text:
        return None
    match = _RETRY_AT_RE.search(text)
    if not match:
        return None
    try:
        y, mo, d, h, mi, sec = (int(g) for g in match.groups()[:6])
        sign, hours, minutes = match.group(7), match.group(8), match.group(9)
        tz = None
        if sign:
            offset = _dt.timedelta(hours=int(hours), minutes=int(minutes or 0))
            tz = _dt.timezone(-offset if sign == '-' else offset)
        when = _dt.datetime(y, mo, d, h, mi, sec, tzinfo=tz)
        now = _dt.datetime.now(tz) if tz is not None else _dt.datetime.now()
    except (TypeError, ValueError):
        return None
    delta = (when - now).total_seconds()
    if delta <= 1:
        return None
    return int(min(delta, 86400))


def retry_after_header(seconds: int | None) -> dict[str, str] | None:
    if not seconds or seconds <= 0:
        return None
    return {'Retry-After': str(int(seconds))}


def model_missing_body(payload: object, text: str) -> dict | None:
    """上游把「该模型在所有账号上都不存在」（11102）包成 503 时改判 404。

    重试无意义（换多少账号都没有这个模型），而 5xx 会让客户端反复重试、也把
    排障方向带到「号池挂了」。只在文案确实来自 11102 时改判，其余原样透传。
    """
    if not text or ('11102' not in text and 'service info not found' not in text):
        return None
    err = payload.get('error') if isinstance(payload, dict) else None
    message = str(err.get('message') or '') if isinstance(err, dict) else ''
    body: dict = {'error': {'message': message or text[:500],
                            'type': 'invalid_request_error',
                            'code': 'model_not_found'}}
    if isinstance(err, dict) and err.get('gateway_hint'):
        body['error']['gateway_hint'] = err['gateway_hint']
    return body


def scan_sse(pending: str, usage: dict) -> tuple[str, bool]:
    """扫描 SSE 文本片段：提取 usage，并判断是否已出现首个正文。

    返回 (未处理完的残留缓冲, 本次是否见到正文 delta)。
    为什么要单独判断「正文」：OpenAI 流的第一块通常只有 role、content 为空，
    若用「收到首块」当首字，会把连接建立时间也算进去，数字偏小且失真。

    **只读**：本层不参与转发（字节原样透传），这里仅用于统计。
    """
    saw_content = False
    while '\n' in pending:
        line, pending = pending.split('\n', 1)
        line = line.strip()
        if not line.startswith('data:'):
            continue
        payload = line[5:].strip()
        if not payload or payload == '[DONE]':
            continue
        try:
            obj = json.loads(payload)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        if isinstance(obj.get('usage'), dict):
            usage.update(obj['usage'])
        choices = obj.get('choices')
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                delta = choice.get('delta') or choice.get('message') or {}
                if isinstance(delta, dict) and (
                    delta.get('content') or delta.get('reasoning_content')
                ):
                    saw_content = True
    return pending, saw_content


def upstream_headers(upstream: dict | None = None) -> dict:
    """转发时用的请求头；`upstream` 为空 = 默认上游。

    多上游（密钥绑定上游）之后，请求头**必须按密钥解析出来的上游来构造** ——
    拿错上游的 api_key 会打到别的账号池上，而这正是本功能要隔离的东西。
    默认参数只为兼容「没有密钥」的调用方（探活 / 管理端调试台）。
    """
    up = upstream if upstream is not None else upstreamsvc.default_upstream()
    headers = {'Content-Type': 'application/json'}
    # 分组把 api_key 留空、且地址与默认上游相同时沿用默认那把（见
    # upstreamsvc.forward_api_key）——「添加分组」只填名称就是这个形态，
    # 不沿用的话绑定这类分组的密钥一调用就吃上游 401。
    api_key = upstreamsvc.forward_api_key(up)
    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'
    return headers


def upstream_hint(upstream: dict | None) -> str:
    """502/503 时补一句「本次打的是哪个上游、这一跳会不会走出口代理」。

    为什么要有（issue #167：两位用户都报「直接 curl 上游是通的、经面板全 502」）：
    原来的报错只有异常文本，看不出面板往哪打的、中间有没有代理。而最常见的两种成因
    恰恰要靠这两条信息才能自查：

      · 上游地址 / 端口填错（面板打的是配置里那个地址，不是你以为的那个）；
      · **上游地址写成公网域名、同时又配了出口代理**：面板→上游这一跳会走代理，
        代理绕不回你自己那台机器，于是全 502 —— 而内网与本机地址是自动直连的。

    `_is_internal_host` 与转发用的判断是同一个函数，所以这里说的「会走代理」
    与真实行为一致。
    """
    up = upstream if upstream is not None else upstreamsvc.default_upstream()
    base = str(up.get('base_url') or '').strip()
    if not base:
        return ''
    try:
        host = urlsplit(base).hostname
    except ValueError:
        host = None
    proxy = config.effective_http_proxy()
    if proxy and host:
        if config._is_internal_host(host):       # noqa: SLF001 —— 与转发判断同一份实现
            return f'（本次请求的上游：{base}；该地址属内网/本机，这一跳直连）'
        masked = proxyroutes.mask_url(proxy)
        return (f'（本次请求的上游：{base}；这一跳会经出口代理 {masked} —— '
                f'代理到不了这个地址就会 502）')
    return f'（本次请求的上游：{base}）'


@dataclass
class ForwardOutcome:
    """一次反代的结果（与 Web 框架无关）。

    非流式：`payload` 是回给客户端的 JSON 体（含错误体）；`status` 是最终状态码
    （11102 已被改判成 404）。
    流式：`stream` 是**原始字节**，`usage` / `first_token_ms` / `error_text` 由
    该流在结束时填好，调用方在自己的 `finally` 里读。
    """

    status: int
    payload: dict | None = None
    stream: AsyncIterator[bytes] | None = None
    content_type: str = 'text/event-stream'
    headers: dict[str, str] = field(default_factory=dict)
    usage: dict = field(default_factory=dict)
    error_text: str | None = None
    first_token_ms: int | None = None

    @property
    def streaming(self) -> bool:
        return self.stream is not None


def _error_payload(text: str) -> dict:
    """上游非 JSON 错误体：包成 OpenAI 形状，保留可读原文。"""
    try:
        parsed = json.loads(text)
    except Exception:  # noqa: BLE001
        parsed = None
    if parsed is not None:
        return parsed if isinstance(parsed, dict) else {'error': {'message': text[:1000],
                                                                 'type': 'api_error'}}
    return {'error': {'message': text[:1000], 'type': 'api_error'}}


async def forward(*, upstream: dict, upstream_path: str, body: dict, stream: bool,
                  started: float | None = None) -> ForwardOutcome:
    """把请求发给 WorkBuddy 上游，返回原始结果。

    传输层失败（连不上 / 超时）**直接抛出**，由调用方决定文案与状态码——两边
    都要在报文里带上各自的作用域信息（网关补 `upstream_hint`，组合成员补
    组合名），在这里统一措辞反而两边都不合适。
    """
    url = f'{upstream["base_url"]}{upstream_path}'
    started = time.time() if started is None else started
    headers = upstream_headers(upstream)

    if not stream:
        async with config.http_client(config.UPSTREAM_TIMEOUT, connect=5) as client:
            resp = await client.post(url, json=body, headers=headers)
        usage: dict = {}
        try:
            data = resp.json()
            usage = (data.get('usage') or {}) if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001 —— 非 JSON 体（网关页 / 反代错误）
            data = None
        error = None if resp.status_code < 400 else (
            str(data)[:500] if data is not None else resp.text[:500])
        # 只在出错时读一次正文：成功路径不该为了「万一要改判」去碰 .text
        #（那会让只有 json() 的响应对象直接炸，也白读一遍大响应体）。
        text_repr = resp.text[:2000] if resp.status_code >= 400 else ''
        missing = model_missing_body(data, text_repr) if resp.status_code >= 500 else None
        # 记账用「实际回给客户端的」状态码：11102 改判成 404 之后，日志里再写
        # 503 会让人以为号池挂了（与响应体不一致）。
        status = 404 if missing is not None else resp.status_code
        retry_after = None
        if resp.status_code == 429 and data is not None:
            retry_after = retry_after_seconds(json.dumps(data, ensure_ascii=False))
        return ForwardOutcome(
            status=status,
            payload=missing if missing is not None else (
                data if data is not None else _error_payload(resp.text)),
            headers=retry_after_header(retry_after) or {},
            usage=usage if isinstance(usage, dict) else {},
            error_text=error,
        )

    client = config.http_client(config.UPSTREAM_TIMEOUT, connect=5)
    try:
        req = client.build_request('POST', url, json=body, headers=headers)
        resp = await client.send(req, stream=True)
    except Exception:
        await client.aclose()
        raise

    status_code = resp.status_code
    content_type = resp.headers.get('content-type', 'text/event-stream')

    # 上游的 4xx/5xx 不是 SSE（正文是一小段 JSON），原先按流原样透传：客户端
    # 拿不到 Retry-After，11102 也只能以 503 出现。这里读完整段错误体后按 JSON
    # 返回，状态码与文案保持上游原样（唯一的例外是 11102 → 404）。
    if status_code >= 400:
        raw = await resp.aread()
        await resp.aclose()
        await client.aclose()
        text = raw.decode('utf-8', errors='replace')
        missing = model_missing_body(_error_payload(text), text) if status_code >= 500 else None
        return ForwardOutcome(
            status=404 if missing is not None else status_code,
            payload=missing if missing is not None else _error_payload(text),
            headers=retry_after_header(
                retry_after_seconds(text) if status_code == 429 else None) or {},
            error_text=text[:500],
        )

    usage: dict = {}
    outcome = ForwardOutcome(status=status_code, content_type=content_type,
                             usage=usage)

    async def gen() -> AsyncIterator[bytes]:
        pending = ''
        # 首字延迟：只记一次，取「首个含正文的 delta」到达时刻。
        # 注意起点含建连 + 上游排队 + 模型开始思考，这正是「上游多久开始回话」。
        try:
            async for chunk in resp.aiter_bytes():
                pending += chunk.decode('utf-8', errors='ignore')
                pending, saw_content = scan_sse(pending, usage)
                if saw_content and outcome.first_token_ms is None:
                    outcome.first_token_ms = int((time.time() - started) * 1000)
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    outcome.stream = gen()
    return outcome
