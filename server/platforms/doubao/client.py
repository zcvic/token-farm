"""豆包上游协议层（网页 Cookie）。

边界与其它平台一致：本模块只做「协议翻译」，不含选号、冷却、会话粘性；
那些策略在 `adapter.py`。

三条链路共用同一个 base（`https://www.doubao.com`）：

  · `POST /chat/completion` —— 对话。请求体是豆包自己的「客户端消息 + option」
    结构（见 `chat.py`），响应**固定是 SSE**，没有 `stream` 开关；
  · `POST /alice/ug/campaign/landing/checkin` —— 每日签到；
  · `POST /alice/commerce/sale/subscription/quota/summary/` —— 额度汇总。

三者都要带同一组查询参数（`constants.QUERY`）与逐请求的设备参数
（`device_id` / `fp` / `tea_uuid` / `web_tab_id`）。缺了这些参数上游会按
「网页匿名会话」处理，表现为「请求成功但没有正文」。

代理口径与项目其余部分一致：`None` = 用全局代理，`''` = 强制直连
（`trust_env=False`，不读环境代理）。
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any, AsyncIterator

import httpx

from ... import config
from ..base import PlatformError
from . import constants as C
from . import store

__all__ = ['iter_events', 'open_chat', 'checkin', 'quota', 'probe',
           'parse_error', 'sse_event']

# 上游错误体里表示「登录态失效」的文案片段。豆包不返回稳定的错误码枚举，
# 只能按文案归类；认不出的一律当上游错误（不冻号、不换号）。
_LOGIN_HINTS = ('登录', '未登录', 'login', 'session', 'unauthorized', 'passport')


# ── 通用 ────────────────────────────────────────────────────
def _selected_proxy(proxy: str | None) -> str:
    if proxy is None:
        return config.effective_http_proxy()
    return str(proxy).strip()


def _query(account: dict) -> dict[str, str]:
    """基础参数 + 逐请求设备参数（与参考实现 `buildURL` 同口径）。"""
    device = store.device_of(account)
    query = dict(C.QUERY)
    query['device_id'] = device
    query['fp'] = 'verify_' + device
    query['tea_uuid'] = device
    web_id = str(account.get('webId') or '').strip()
    if web_id:
        query['web_id'] = web_id
    query['web_tab_id'] = str(uuid.uuid4())
    return query


def _chat_headers(cookie: str) -> dict[str, str]:
    return {
        'Content-Type': 'application/json',
        'Accept': '*/*',
        'agw-js-conv': 'str, str',
        'last-event-id': 'undefined',
        'x-flow-trace': '04-0000000000000000000000000000000-'
                        '0000000000000000-01',
        'User-Agent': C.UA,
        'Referer': C.BASE_URL + '/',
        'Cookie': cookie,
    }


def _web_headers(cookie: str) -> dict[str, str]:
    return {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'User-Agent': C.UA,
        'Referer': C.BASE_URL + '/',
        'Cookie': cookie,
    }


def _json(response: httpx.Response) -> object:
    text = response.text
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _error_text(payload: object, fallback: str) -> str:
    if isinstance(payload, dict):
        for key in ('message', 'msg', 'desc', 'error_msg'):
            value = payload.get(key)
            if value:
                return str(value)
        error = payload.get('error')
        if isinstance(error, dict):
            for key in ('message', 'msg', 'code'):
                if error.get(key):
                    return str(error[key])
        if isinstance(error, str) and error:
            return error
    return fallback


def parse_error(payload: object) -> str:
    """上游错误体里的稳定错误码（可能没有）。"""
    if not isinstance(payload, dict):
        return ''
    for key in ('code', 'error_code', 'status_code'):
        value = payload.get(key)
        if value not in (None, '', 0, '0'):
            return str(value)
    error = payload.get('error')
    if isinstance(error, dict):
        return str(error.get('code') or error.get('type') or '').strip()
    return ''


def _retry_after(response: httpx.Response) -> int | None:
    raw = response.headers.get('retry-after')
    if not raw:
        return None
    try:
        return max(0, int(float(raw)))
    except (TypeError, ValueError):
        return None


def _looks_like_login_error(message: str) -> bool:
    text = str(message or '').lower()
    return any(hint in text for hint in _LOGIN_HINTS)


def _response_error(response: httpx.Response, text: str,
                    payload: object = None) -> PlatformError:
    """把上游错误映射成带「该不该换号 / 冻多久」语义的状态码。

    401 = Cookie 失效（冻了也不会自己好，只换号）；
    429 = 限流；
    5xx = 上游故障；
    其余 4xx = 请求问题（不换号）。
    """
    message = _error_text(payload, text[:200] or f'上游 HTTP {response.status_code}')
    status = response.status_code
    if status in (401, 403) or (status < 500 and _looks_like_login_error(message)):
        return PlatformError(message, status=401, code='login_expired',
                             payload=payload)
    if status == 429:
        return PlatformError(message, status=429, code='rate_limited',
                             retry_after=_retry_after(response), payload=payload)
    if status >= 500:
        return PlatformError(message, status=502, code='upstream_error',
                             payload=payload)
    return PlatformError(message, status=status, code='bad_request',
                         payload=payload)


def _network_error(exc: Exception) -> PlatformError:
    return PlatformError(f'连接豆包失败：{exc}', status=502,
                         code='network_error')


# ── SSE ─────────────────────────────────────────────────────
def sse_event(lines: list[str]) -> tuple[str, str]:
    """一组 SSE 行 → `(事件名, data 文本)`。

    豆包把事件名放在 `event:` 行、JSON 放在 `data:` 行；`data:` 可能出现多行
    （按 SSE 规范用换行拼接）。注释帧（`:` 开头）忽略。
    """
    event = ''
    data: list[str] = []
    for line in lines:
        if line.startswith('event:'):
            event = line[6:].strip()
        elif line.startswith('data:'):
            data.append(line[5:].strip())
    return event, '\n'.join(data)


async def iter_events(response: httpx.Response) -> AsyncIterator[tuple[str, object]]:
    """逐事件解析豆包 SSE，产出 `(事件名, payload)`。

    `payload` 是解析后的 JSON 对象；空帧 / `[DONE]` / 坏 JSON 直接跳过——
    对话正文只认 `CHUNK_DELTA` / `STREAM_CHUNK`，跳过一帧坏数据比中断整条流好。
    读流过程中的网络错误统一翻成 `PlatformError`，与 `open_chat` 的口径一致。
    """
    try:
        async for item in _parse_events(response):
            yield item
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc


async def _parse_events(response: httpx.Response) -> AsyncIterator[tuple[str, object]]:
    buffer: list[str] = []
    async for line in response.aiter_lines():
        if line == '':
            if not buffer:
                continue
            event, text = sse_event(buffer)
            buffer = []
            if not text or text == '[DONE]':
                continue
            try:
                payload = json.loads(text)
            except ValueError:
                continue
            yield event, payload
            continue
        buffer.append(line)
    if buffer:
        event, text = sse_event(buffer)
        if text and text != '[DONE]':
            try:
                payload = json.loads(text)
            except ValueError:
                payload = None
            if payload is not None:
                yield event, payload


# ── 对话 ────────────────────────────────────────────────────
async def open_chat(account: dict, body: dict, *, proxy: str | None = None,
                    idle_timeout: float = C.CHAT_IDLE_TIMEOUT,
                    connect_timeout: float = 15) -> httpx.Response:
    """发起对话，返回**未读取**的上游响应（调用方负责关闭）。

    上游正常情况下回 `text/event-stream`。Cookie 失效 / 参数错误时可能回
    HTTP 200 + JSON（错误体）——照原样解析会把一段 JSON 当 SSE，所以非 SSE
    响应在这里一次性读完并按错误归类。
    """
    cookie = str(account.get('cookie') or '')
    if not cookie:
        raise PlatformError('豆包账号缺少 Cookie', status=401,
                            code='login_expired')
    try:
        async with config.http_client(
                httpx.Timeout(idle_timeout, connect=connect_timeout),
                proxy=_selected_proxy(proxy), lane='doubao') as client:
            request = client.build_request(
                'POST', C.BASE_URL + C.CHAT_PATH,
                params=_query(account),
                headers={**_chat_headers(cookie), 'Accept': 'text/event-stream'},
                content=json.dumps(body, ensure_ascii=False).encode('utf-8'))
            response = await client.send(request, stream=True)
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc

    if response.status_code >= 400:
        text = (await response.aread()).decode('utf-8', errors='replace')
        await response.aclose()
        payload: Any = None
        try:
            payload = json.loads(text) if text else None
        except ValueError:
            payload = None
        raise _response_error(response, text, payload)

    ctype = str(response.headers.get('content-type') or '').lower()
    if 'text/event-stream' not in ctype:
        text = (await response.aread()).decode('utf-8', errors='replace')
        await response.aclose()
        try:
            payload = json.loads(text) if text else None
        except ValueError:
            payload = None
        message = _error_text(payload, text[:200] or '对话接口未返回 SSE')
        status = 401 if _looks_like_login_error(message) else 502
        raise PlatformError(message, status=status,
                            code='login_expired' if status == 401
                            else 'bad_upstream', payload=payload)
    return response


# ── 签到 ────────────────────────────────────────────────────
_ALREADY_HINTS = ('已签到', 'already', 'idempotent')
_NO_CAMPAIGN_CODE = 710010702


async def checkin(account: dict, *, proxy: str | None = None,
                  timeout: float = C.CHECKIN_TIMEOUT) -> dict:
    """每日签到。返回 `{ok, already, code, message, reward, days}`。

    判定顺序与参考实现一致：幂等命中 → 已签到；`checked_in` → 成功；
    文案含「已签到」→ 已签到；`710010702` → 当前无活动；其余算失败。
    """
    cookie = str(account.get('cookie') or '')
    if not cookie:
        raise PlatformError('豆包账号缺少 Cookie', status=401,
                            code='login_expired')
    try:
        async with config.http_client(
                httpx.Timeout(timeout, connect=10),
                proxy=_selected_proxy(proxy), lane='doubao') as client:
            response = await client.post(
                C.BASE_URL + C.CHECKIN_PATH, params=_query(account),
                headers=_web_headers(cookie),
                content=json.dumps({'scene': C.CHECKIN_SCENE}).encode('utf-8'))
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc

    text = response.text
    payload = _json(response)
    if response.status_code >= 400:
        raise _response_error(response, text, payload)

    body = payload if isinstance(payload, dict) else {}
    data = body.get('data') if isinstance(body.get('data'), dict) else {}
    try:
        code = int(body.get('code') or 0)
    except (TypeError, ValueError):
        code = 0
    message = str(body.get('message') or body.get('msg') or '')
    already = bool(data.get('idempotent')) or any(
        hint in message.lower() for hint in _ALREADY_HINTS)
    checked = bool(data.get('checked_in')) or code == 0 and not already
    if code == _NO_CAMPAIGN_CODE:
        return {'ok': False, 'already': False, 'code': 'no_campaign',
                'message': message or '当前无可用签到活动',
                'reward': '', 'reward_amount': 0, 'days': None}
    if code != 0:
        return {'ok': False, 'already': False, 'code': code,
                'message': message or f'上游返回 code={code}',
                'reward': '', 'reward_amount': 0, 'days': None}
    if already:
        return {'ok': True, 'already': True, 'code': 0,
                'message': message or '今日已签到', 'reward': '',
                'reward_amount': 0,
                'days': _int_or_none(data.get('continuous_days'))}
    if not checked:
        return {'ok': False, 'already': False, 'code': 0,
                'message': message or '签到结果未知', 'reward': '',
                'reward_amount': 0, 'days': None}
    reward = _int_or_none(data.get('reward_amount')) or 0
    return {
        'ok': True, 'already': False, 'code': 0,
        'message': message or '签到成功',
        'reward': f'{reward} 积分' if reward else '',
        'reward_amount': reward,
        'days': _int_or_none(data.get('continuous_days')),
    }


# ── 额度 ────────────────────────────────────────────────────
_NUM_CLEAN = re.compile(r'[,\s¥$￥]')


def _num(value: object) -> float | None:
    """兼容 JSON 数字与带千分位 / 货币符号的字符串。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = _NUM_CLEAN.sub('', str(value))
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _int_or_none(value: object) -> int | None:
    number = _num(value)
    return int(number) if number is not None else None


def _collect_quota(node: object, out: dict) -> None:
    """递归找所有 `window_limits[]`，累加总额 / 已用（与参考实现同口径）。"""
    if isinstance(node, dict):
        limits = node.get('window_limits')
        if isinstance(limits, list):
            group_name = str(node.get('feature_group_name') or '')
            for item in limits:
                if not isinstance(item, dict):
                    continue
                total = _num(item.get('total_amount')) or 0.0
                used = _num(item.get('used_amount')) or 0.0
                if total <= 0 and used <= 0:
                    continue
                out['total'] += total
                out['used'] += used
                out['groups'].append({
                    'name': group_name,
                    'total': total,
                    'used': used,
                    'percent': _num(item.get('used_percent')) or 0.0,
                    'end_time': _int_or_none(item.get('end_time')) or 0,
                })
        for child in node.values():
            _collect_quota(child, out)
    elif isinstance(node, list):
        for child in node:
            _collect_quota(child, out)


async def quota(account: dict, *, proxy: str | None = None,
                timeout: float = C.QUOTA_TIMEOUT) -> dict:
    """额度汇总：返回 `{total, used, remaining, percent, groups}`。"""
    cookie = str(account.get('cookie') or '')
    if not cookie:
        raise PlatformError('豆包账号缺少 Cookie', status=401,
                            code='login_expired')
    try:
        async with config.http_client(
                httpx.Timeout(timeout, connect=10),
                proxy=_selected_proxy(proxy), lane='doubao') as client:
            response = await client.post(
                C.BASE_URL + C.QUOTA_PATH, params=_query(account),
                headers=_web_headers(cookie), content=b'{}')
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc

    text = response.text
    payload = _json(response)
    if response.status_code >= 400:
        raise _response_error(response, text, payload)
    body = payload if isinstance(payload, dict) else {}
    data = body.get('data') if isinstance(body.get('data'), dict) else body
    out: dict[str, Any] = {'total': 0.0, 'used': 0.0, 'groups': []}
    _collect_quota(data, out)
    total = round(out['total'], 4)
    used = round(out['used'], 4)
    remaining = max(0.0, total - used)
    return {
        'total': total,
        'used': used,
        'remaining': round(remaining, 4),
        'percent': round(used / total * 100, 1) if total > 0 else 0.0,
        'groups': out['groups'],
    }


async def probe(account: dict, *, proxy: str | None = None,
                timeout: float = C.QUOTA_TIMEOUT) -> dict:
    """Cookie 探活：返回 `{state, message}`，`state` ∈ `ok` / `invalid` / `unknown`。

    豆包没有「查我是不是登录了」的只读接口，这里借额度接口：HTTP 200 且不是
    登录类错误文案就算 `ok`。**不抛异常**——探活是「看一眼」，不该把账号页
    打挂；真正要报错的路径是 `open_chat`。
    """
    cookie = str(account.get('cookie') or '')
    if not cookie:
        return {'state': 'invalid', 'message': '缺少 Cookie'}
    try:
        async with config.http_client(
                httpx.Timeout(timeout, connect=10),
                proxy=_selected_proxy(proxy), lane='doubao') as client:
            response = await client.post(
                C.BASE_URL + C.QUOTA_PATH, params=_query(account),
                headers=_web_headers(cookie), content=b'{}')
    except httpx.HTTPError as exc:
        return {'state': 'unknown', 'message': f'连接失败：{exc}'}

    text = response.text
    payload = _json(response)
    if response.status_code in (401, 403):
        return {'state': 'invalid',
                'message': _error_text(payload, f'HTTP {response.status_code}')}
    if response.status_code >= 500:
        return {'state': 'unknown',
                'message': _error_text(payload, f'HTTP {response.status_code}')}
    message = _error_text(payload, '')
    if response.status_code < 400 and _looks_like_login_error(message):
        return {'state': 'invalid', 'message': message}
    if response.status_code >= 400:
        return {'state': 'unknown',
                'message': message or f'HTTP {response.status_code}'}
    return {'state': 'ok', 'message': ''}
