"""小浣熊上游协议层（JWT 刷新 / 目录 / 对话 / 积分）。

边界与其它平台一致：本模块只做「协议翻译」，不含选号、冷却、会话粘性；
那些策略在 `adapter.py`。

关键事实（见 `deploy/raccoon-ref.txt`）：

  · refresh token **单次使用**：刷新会同时换发 access 与 refresh，调用方必须
    先落盘再使用；
  · 未知模型上游会**静默回落默认模型**并返回 200，因此模型校验必须本地做；
  · 对话是标准 OpenAI Chat Completions：流式真增量 SSE、末尾 `data: [DONE]`，
    思考在 `choices[].delta.reasoning_content`；
  · `reasoning_effort` 只做半套校验（垃圾值也可能 200），默认不往下传；
  · 积分是「每请求扣 1 分」而不是按 token 计费。
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

import httpx

from ... import config
from ..base import PlatformError
from . import constants as C

__all__ = [
    'refresh', 'probe', 'parse_catalog', 'fetch_catalog',
    'chat_body', 'chat', 'open_chat', 'iter_chunks', 'synthesize_stream',
    'sse_to_completion', 'balance', 'entitlement', 'bills',
    'claim_daily_grant', 'daily_reward', 'bill_is_today', 'beijing_date',
    'is_risk_control', 'is_stream_unavailable',
]

_BEIJING = timezone(timedelta(hours=8))


# ── 通用 ────────────────────────────────────────────────────
def _selected_proxy(proxy: str | None) -> str:
    if proxy is None:
        return config.effective_http_proxy()
    return str(proxy).strip()


def _headers(account: dict, token: str, *, desktop: bool = False,
             accept: str = 'application/json') -> dict[str, str]:
    """官方网页 / 桌面客户端共用的一套头。

    只补必要的几项：`Authorization`、浏览器 UA、`X-Org-Code`（个人版空串）与
    `X-Raccoon-Language`。桌面端接口再补 `X-Client-*`，普通对话不带——
    多带客户端指纹只会增加风控面。
    """
    out = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
        'Accept': accept,
        'User-Agent': C.USER_AGENT,
        'X-Org-Code': str(account.get('orgCode') or account.get('org_code') or ''),
        'X-Raccoon-Language': C.RACCOON_LANGUAGE,
    }
    if desktop:
        from . import store  # noqa: PLC0415 —— 只读设备号派生
        out.update({
            'X-Client-Platform': C.CLIENT_PLATFORM,
            'X-Client-Version': C.CLIENT_VERSION,
            'X-Client-Device-ID': store.device_id_for(account),
        })
    return out


def _json(response: httpx.Response) -> object:
    text = response.text
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _code_of(payload: object) -> int | None:
    if not isinstance(payload, dict):
        return None
    raw = payload.get('code')
    if raw in (None, ''):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _message_of(payload: object) -> str:
    if not isinstance(payload, dict):
        return ''
    error = payload.get('error')
    if isinstance(error, dict):
        for key in ('message', 'msg', 'detail'):
            if error.get(key):
                return str(error[key])
    if isinstance(error, str) and error.strip():
        return error
    for key in ('message', 'msg', 'details', 'detail'):
        if payload.get(key):
            return str(payload[key])
    return ''


def _retry_after(response: httpx.Response) -> int | None:
    raw = response.headers.get('retry-after')
    if not raw:
        return None
    try:
        return max(0, int(float(raw)))
    except (TypeError, ValueError):
        return None


def classify(status: int, payload: object,
             fallback: str = '小浣熊上游返回错误') -> PlatformError | None:
    """上游失败 → 带 HTTP 语义的 `PlatformError`；成功返回 None。

    只有明确的鉴权失败才报 401（否则账号会被误标失效并从池里摘掉）；
    风控 200108 映射成 429 并给足冷却；其余 4xx 按参数错误。
    """
    code = _code_of(payload)
    message = _message_of(payload) or fallback
    if code in C.AUTH_CODES or status == 401:
        return PlatformError(message, status=401, code='auth_expired',
                             payload=payload)
    if code in C.LOGIN_EXPIRED_CODES:
        return PlatformError(message, status=401, code='login_expired',
                             payload=payload)
    if code in C.RATE_LIMIT_CODES:
        return PlatformError(message, status=429, code='rate_limited',
                             retry_after=C.RISK_CONTROL_COOLDOWN,
                             payload=payload)
    if status == 429:
        return PlatformError(message, status=429, code='rate_limited',
                             payload=payload)
    if status == 402:
        return PlatformError(message, status=402, code='insufficient_credits',
                             payload=payload)
    if status == 403:
        return PlatformError(message, status=403, code='forbidden',
                             payload=payload)
    if status >= 500:
        return PlatformError(message, status=502, code='upstream_error',
                             payload=payload)
    if status >= 400 or code not in (None, 0):
        return PlatformError(message, status=400, code='bad_request',
                             payload=payload)
    return None


def _network_error(exc: Exception) -> PlatformError:
    return PlatformError(f'连接小浣熊失败：{exc}', status=502,
                         code='network_error')


async def _request(method: str, url: str, *, token: str = '',
                   body: object = None, headers: dict[str, str] | None = None,
                   proxy: str | None = None, timeout: float,
                   stream: bool = False) -> httpx.Response:
    if stream:
        async with config.http_client(
                httpx.Timeout(timeout, connect=10),
                proxy=_selected_proxy(proxy), lane='raccoon') as client:
            request = client.build_request(method, url, headers=headers,
                                           content=(json.dumps(
                                               body, ensure_ascii=False
                                           ).encode('utf-8') if body is not None
                                               else None))
            return await client.send(request, stream=True)
    async with config.http_client(
            httpx.Timeout(timeout, connect=10),
            proxy=_selected_proxy(proxy), lane='raccoon') as client:
        return await client.request(method, url, headers=headers,
                                    content=(json.dumps(
                                        body, ensure_ascii=False
                                    ).encode('utf-8') if body is not None
                                        else None))


async def _read_error(response: httpx.Response) -> tuple[str, object]:
    try:
        text = (await response.aread()).decode('utf-8', errors='replace')
    finally:
        try:
            await response.aclose()
        except Exception:  # noqa: BLE001
            pass
    payload: object = None
    if text:
        try:
            payload = json.loads(text)
        except ValueError:
            payload = None
    return text, payload


# ── 鉴权 ────────────────────────────────────────────────────
async def refresh(refresh_token: str, *, proxy: str | None = None,
                  timeout: float = C.REFRESH_TIMEOUT) -> dict:
    """`POST /api/web/auth/v1/refresh` → 一对新 token。

    请求体只有 `{"refresh_token": ...}`（参数名写错会回 100002），不带
    `Authorization`。成功时上游可能不返回新的 refresh（此时沿用旧值）。
    """
    key = str(refresh_token or '').strip()
    if not key:
        raise PlatformError('缺少 refresh token', status=401,
                            code='login_expired')
    try:
        response = await _request(
            'POST', C.AUTH_BASE + C.REFRESH_PATH,
            body={'refresh_token': key},
            headers={'Content-Type': 'application/json',
                     'Accept': 'application/json',
                     'User-Agent': C.USER_AGENT},
            proxy=proxy, timeout=timeout)
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc
    payload = _json(response)
    error = classify(response.status_code, payload, '刷新小浣熊 token 失败')
    if error is not None:
        raise error
    body = payload if isinstance(payload, dict) else {}
    data = body.get('data') if isinstance(body.get('data'), dict) else body
    access = str(data.get('access_token') or data.get('accessToken') or '').strip()
    new_refresh = str(data.get('refresh_token') or data.get('refreshToken')
                      or '').strip()
    if not access:
        raise PlatformError('小浣熊刷新响应里没有 access_token', status=502,
                            code='oauth_bad_response', payload=payload)
    from . import store  # noqa: PLC0415 —— 复用 JWT 解析
    access_exp = store.jwt_exp(access) or (time.time() + C.ACCESS_TTL_SECONDS)
    refresh_exp = (store.jwt_exp(new_refresh) if new_refresh else 0) \
        or (time.time() + C.REFRESH_TTL_SECONDS)
    return {
        'access_token': access,
        'refresh_token': new_refresh,
        'expires_at': access_exp,
        'refresh_expires_at': refresh_exp,
    }


async def probe(token: str, *, proxy: str | None = None,
                timeout: float = C.CATALOG_TIMEOUT) -> dict:
    """用目录接口探活：返回 `{state, models, message}`。"""
    try:
        models = await fetch_catalog(token, proxy=proxy, timeout=timeout)
    except PlatformError as exc:
        return {'state': 'invalid' if exc.status == 401 else 'unknown',
                'models': 0, 'message': exc.message}
    return {'state': 'ok', 'models': len(models), 'message': ''}


# ── 模型目录 ────────────────────────────────────────────────
def _num(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _int(value: object) -> int:
    number = _num(value)
    return int(number) if number is not None else 0


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit]


def _tags_of(raw: dict) -> list[str]:
    tags = raw.get('tags')
    if not isinstance(tags, list):
        return []
    return [str(tag).strip() for tag in tags if str(tag or '').strip()]


def _thinking_of(raw: dict) -> dict:
    params = raw.get('params') if isinstance(raw.get('params'), dict) else {}
    config_value = params.get('raccoon_thinking') \
        if isinstance(params.get('raccoon_thinking'), dict) else \
        raw.get('raccoon_thinking')
    if not isinstance(config_value, dict):
        return {}
    levels = config_value.get('display_levels')
    values: list[str] = []
    if isinstance(levels, list):
        for item in levels:
            if isinstance(item, dict) and item.get('value'):
                values.append(str(item['value']))
            elif isinstance(item, str) and item:
                values.append(item)
    return {
        'supported': bool(config_value.get('supported')),
        'can_disable': bool(config_value.get('can_disable')),
        'levels': values,
        'default_level': str(config_value.get('default_level') or ''),
    }


def _modalities_of(model_id: str, tags: list[str]) -> list[str]:
    """文本之外是否是图片输入。

    目录的 `vision` 标签**不可靠**（两个方向都实测错过）；已实测的模型以
    `MEASURED_VISION` 为准，未测过的才回退标签。这里只影响展示与上传入口，
    不影响对话（图片会被上游原样接收）。
    """
    lowered = {tag.lower() for tag in tags}
    if model_id in C.MEASURED_VISION:
        vision = C.MEASURED_VISION[model_id]
    else:
        vision = bool(lowered & {'vision', 'image', 'image-understanding'})
    return ['text', 'image'] if vision else ['text']


def parse_catalog(payload: object) -> list[dict]:
    """把上游目录归一成内部模型列表（坏条目丢弃）。

    解析口径（与参考实现一致）：根上有 `data` 就用 `data`，否则把根当 data；
    **平铺所有 `categories[].models[]`**，不按 `category.type` 过滤；同名模型
    首次出现者胜；空 name 丢弃；`context_window` / `max_tokens` 先看顶层再
    看 `params`；`visible` 缺省 true。
    """
    if not isinstance(payload, dict):
        return []
    root: object = payload
    if isinstance(payload.get('data'), dict):
        root = payload['data']
    if not isinstance(root, dict):
        return []
    categories = root.get('categories')
    if not isinstance(categories, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for category in categories:
        if not isinstance(category, dict):
            continue
        models = category.get('models')
        if not isinstance(models, list):
            continue
        for raw in models:
            if not isinstance(raw, dict):
                continue
            model_id = str(raw.get('model_name') or raw.get('name')
                           or raw.get('id') or '').strip()
            if not model_id or model_id in seen:
                continue
            # `raccoon-work` 是应用内建 agent 的路由位，不是可调用模型。
            if model_id == 'raccoon-work':
                continue
            seen.add(model_id)
            params = raw.get('params') if isinstance(raw.get('params'), dict) else {}
            description = str(raw.get('description') or '').strip()
            display_description = str(raw.get('display_description') or '').strip()
            display = _truncate(display_description or description or model_id, 64)
            context = raw.get('context_window')
            if context in (None, '', 0):
                context = params.get('context_window')
            max_tokens = raw.get('max_tokens')
            if max_tokens in (None, '', 0):
                max_tokens = params.get('max_tokens')
            tags = _tags_of(raw)
            lowered = {tag.lower() for tag in tags}
            effective = _num(raw.get('billing_effective_multiplier'))
            billing = _num(raw.get('billing_multiplier'))
            visible = raw.get('visible')
            thinking = _thinking_of(raw)
            out.append({
                'id': model_id,
                'name': display,
                'description': description,
                'context_length': _int(context),
                'max_tokens': _int(max_tokens),
                'tags': tags,
                'visible': True if visible is None else bool(visible),
                'reasoning': ('reasoning' in lowered) or bool(thinking.get('supported')),
                'modalities': _modalities_of(model_id, tags),
                'tool_call': True,
                'billing_multiplier': billing,
                'billing_effective_multiplier': effective,
                'billing_status': str(raw.get('billing_status') or ''),
                'free': (effective == 0) if effective is not None
                else bool(raw.get('free')),
                'thinking': thinking,
            })
    return out


async def fetch_catalog(token: str, *, proxy: str | None = None,
                        timeout: float = C.CATALOG_TIMEOUT) -> list[dict]:
    try:
        response = await _request(
            'GET', C.LLM_V2 + C.CATALOG_PATH,
            token=token, headers=_headers({}, token),
            proxy=proxy, timeout=timeout)
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc
    payload = _json(response)
    error = classify(response.status_code, payload, '拉取小浣熊模型目录失败')
    if error is not None:
        raise error
    models = parse_catalog(payload)
    if not models:
        raise PlatformError('小浣熊返回的模型目录为空', status=502,
                            code='empty_catalog', payload=payload)
    return models


# ── 对话 ────────────────────────────────────────────────────
def chat_body(body: dict) -> dict:
    """整理发往上游的请求体。

    · 剥掉 `reasoning_effort`：上游只做半套校验，垃圾值也可能 200，透传没有
      可预期语义；要控制思考请用 `raccoon_thinking`；
    · `max_tokens` 缺省时补 16k：留太小会得到「只有思考没有正文」；
      客户端只发 OpenAI 新字段 `max_completion_tokens` 时沿用它的预算；
    · `verbose: true` 是官方客户端会带的字段，上游用它回 usage。
    """
    payload = dict(body or {})
    payload.pop('reasoning_effort', None)
    if not str(payload.get('model') or '').strip():
        payload['model'] = C.DEFAULT_MODEL
    if not payload.get('max_tokens'):
        payload['max_tokens'] = payload.get('max_completion_tokens') \
            or C.DEFAULT_MAX_TOKENS
    payload.setdefault('verbose', True)
    payload.setdefault('stream', False)
    return payload


def _chat_headers(account: dict, token: str, stream: bool) -> dict[str, str]:
    return _headers(account, token,
                    accept='text/event-stream' if stream else 'application/json')


async def chat(account: dict, token: str, body: dict, *, base: str = C.LLM_V2,
               proxy: str | None = None,
               timeout: float = C.CHAT_TIMEOUT) -> dict:
    """非流式对话，返回上游 JSON。"""
    try:
        response = await _request(
            'POST', base + C.CHAT_PATH, token=token, body=body,
            headers=_chat_headers(account, token, False),
            proxy=proxy, timeout=timeout)
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc
    text = response.text
    payload: object = None
    if text:
        try:
            payload = json.loads(text)
        except ValueError:
            payload = None
    error = classify(response.status_code, payload, '小浣熊对话失败')
    if error is not None:
        raise error
    if not isinstance(payload, dict):
        raise PlatformError('小浣熊对话响应不是 JSON 对象', status=502,
                            code='bad_upstream', payload=text[:300])
    choices = payload.get('choices')
    if not isinstance(choices, list) or not choices:
        raise PlatformError('小浣熊对话响应缺少 choices', status=502,
                            code='bad_upstream', payload=payload)
    return payload


async def open_chat(account: dict, token: str, body: dict,
                    *, base: str = C.LLM_V2, proxy: str | None = None,
                    idle_timeout: float = C.CHAT_IDLE_TIMEOUT,
                    connect_timeout: float = 10) -> httpx.Response:
    """流式对话：返回**未读取**的上游响应（调用方负责关闭）。

    正常情况下上游回 `text/event-stream`。如果回的是 JSON（有些错误 /
    风控伪装成 200），在这里一次性读完并归类，绝不把 JSON 当 SSE 发出去。
    """
    try:
        response = await _request(
            'POST', base + C.CHAT_PATH, token=token, body=body,
            headers=_chat_headers(account, token, True),
            proxy=proxy, timeout=idle_timeout, stream=True)
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc
    if response.status_code >= 400:
        _, payload = await _read_error(response)
        error = classify(response.status_code, payload, '小浣熊对话失败')
        if error is not None:
            raise error
        raise PlatformError('小浣熊对话失败', status=502,
                            code='upstream_error', payload=payload)
    ctype = str(response.headers.get('content-type') or '').lower()
    if 'text/event-stream' not in ctype:
        text, payload = await _read_error(response)
        error = classify(200, payload, '小浣熊对话响应异常')
        if error is not None:
            raise error
        if isinstance(payload, dict) and isinstance(payload.get('choices'), list):
            # 上游忽略了 stream（或中途降级）：交给适配器聚合成非流式响应。
            raise PlatformError('小浣熊忽略 stream，返回了完整响应', status=502,
                                code='not_sse', payload=payload)
        raise PlatformError(text[:200] or '小浣熊对话接口未返回 SSE',
                            status=502, code='bad_upstream', payload=payload)
    return response


async def iter_chunks(response: httpx.Response) -> AsyncIterator[dict]:
    """把上游 SSE 读成 OpenAI chunk dict；跳过保活 / `[DONE]`。"""
    buffer: list[str] = []
    async for line in response.aiter_lines():
        if line == '':
            if not buffer:
                continue
            text = '\n'.join(buffer).strip()
            buffer = []
            if not text or text == '[DONE]':
                continue
            try:
                obj = json.loads(text)
            except ValueError:
                continue
            if isinstance(obj, dict):
                yield obj
            continue
        if line.startswith('data:'):
            buffer.append(line[5:].strip())
        elif line.startswith(':') or line.startswith('event:') \
                or line.startswith('id:') or line.startswith('retry:'):
            continue
        elif line.strip():
            buffer.append(line.strip())
    if buffer:
        text = '\n'.join(buffer).strip()
        if text and text != '[DONE]':
            try:
                obj = json.loads(text)
            except ValueError:
                obj = None
            if isinstance(obj, dict):
                yield obj


def _stream_text(delta: dict) -> bool:
    return bool(delta.get('content') or delta.get('reasoning_content')
                or delta.get('reasoning') or delta.get('tool_calls'))


async def synthesize_stream(payload: dict, model: str) -> AsyncIterator[dict]:
    """把一份完整 `chat.completion` 拆成 OpenAI chunk 流。

    用于 v2 流式不可用、v1 非流式兜底的场景：客户端仍然拿到 SSE，只是正文
    一次性到达，而不是逐 token。
    """
    choice = (payload.get('choices') or [{}])[0]
    if not isinstance(choice, dict):
        choice = {}
    message = choice.get('message') if isinstance(choice.get('message'), dict) else {}
    ident = str(payload.get('id') or f'chatcmpl-{uuid.uuid4().hex[:24]}')
    created = int(payload.get('created') or time.time())

    def chunk(delta: dict, finish: str | None = None,
              usage: dict | None = None) -> dict:
        out: dict[str, Any] = {
            'id': ident,
            'object': 'chat.completion.chunk',
            'created': created,
            'model': model,
            'choices': [{'index': 0, 'delta': delta,
                         'finish_reason': finish}],
        }
        if usage is not None:
            out['usage'] = usage
        return out

    yield chunk({'role': 'assistant'})
    reasoning = message.get('reasoning_content') or message.get('reasoning')
    if isinstance(reasoning, str) and reasoning:
        yield chunk({'reasoning_content': reasoning})
    content = message.get('content')
    if isinstance(content, str) and content:
        yield chunk({'content': content})
    calls = message.get('tool_calls')
    if isinstance(calls, list) and calls:
        yield chunk({'tool_calls': calls})
    usage = payload.get('usage') if isinstance(payload.get('usage'), dict) else None
    yield chunk({}, str(choice.get('finish_reason') or 'stop'), usage)


def sse_to_completion(chunks: list[object], model: str) -> dict:
    """把一串 SSE 事件聚合成 `chat.completion`（测试 / 非流式兜底）。"""
    content: list[str] = []
    reasoning: list[str] = []
    tool_calls: list[dict] = []
    finish_reason = ''
    usage: dict = {}
    ident = ''
    for raw in chunks:
        if not isinstance(raw, dict):
            continue
        if raw.get('id'):
            ident = str(raw['id'])
        if isinstance(raw.get('usage'), dict):
            usage.update(raw['usage'])
        for choice in raw.get('choices') or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get('delta')
            if not isinstance(delta, dict):
                delta = choice.get('message') if isinstance(
                    choice.get('message'), dict) else {}
            text = delta.get('content')
            if isinstance(text, str):
                content.append(text)
            think = delta.get('reasoning_content') or delta.get('reasoning')
            if isinstance(think, str):
                reasoning.append(think)
            for call in delta.get('tool_calls') or []:
                if isinstance(call, dict):
                    tool_calls.append(call)
            if choice.get('finish_reason'):
                finish_reason = str(choice['finish_reason'])
    body = ''.join(content)
    think = ''.join(reasoning)
    message: dict[str, Any] = {'role': 'assistant', 'content': body}
    if think and not body:
        message['content'] = think
        message['reasoning'] = think
    elif think:
        message['reasoning'] = think
    if tool_calls:
        message['tool_calls'] = tool_calls
    return {
        'id': ident or f'chatcmpl-{uuid.uuid4().hex[:24]}',
        'object': 'chat.completion',
        'created': int(time.time()),
        'model': model or C.DEFAULT_MODEL,
        'choices': [{'index': 0, 'message': message,
                     'finish_reason': finish_reason or 'stop'}],
        'usage': usage or {'prompt_tokens': 0, 'completion_tokens': 0,
                           'total_tokens': 0},
    }


def is_risk_control(exc: PlatformError) -> bool:
    """是否是账号级风控（200108 / 被标记异常流量）。"""
    if exc.status != 429:
        return False
    payload = exc.payload
    if isinstance(payload, dict) and _code_of(payload) in C.RATE_LIMIT_CODES:
        return True
    text = str(exc.message or '')
    return '200108' in text or 'abnormal' in text.lower()


def is_stream_unavailable(exc: PlatformError) -> bool:
    """流式端点不可用（能否降级到 v1 非流式由适配器决定）。"""
    text = str(exc.message or '').lower()
    return exc.code in ('not_sse', 'bad_upstream') \
        or 'invalid upstream stream response' in text


# ── 积分 ────────────────────────────────────────────────────
def _point(value: object) -> int:
    number = _num(value)
    return int(number) if number is not None else 0


async def balance(token: str, *, proxy: str | None = None,
                  timeout: float = C.BALANCE_TIMEOUT) -> dict:
    try:
        response = await _request(
            'GET', C.POINTS_BASE + C.BALANCE_PATH, token=token,
            headers=_headers({}, token), proxy=proxy, timeout=timeout)
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc
    payload = _json(response)
    error = classify(response.status_code, payload, '查询小浣熊积分失败')
    if error is not None:
        raise error
    body = payload if isinstance(payload, dict) else {}
    data = body.get('data') if isinstance(body.get('data'), dict) else body
    available = data.get('available_points')
    if available in (None, ''):
        available = data.get('total')
    if available in (None, ''):
        available = data.get('points')
    return {
        'available_points': _point(available),
        'daily_points': _point(data.get('daily_points')),
        'monthly_points': _point(data.get('monthly_points')),
        'reward_points': _point(data.get('reward_points')),
        'topup_points': _point(data.get('topup_points')),
        'raw': data,
    }


async def entitlement(token: str, *, proxy: str | None = None,
                      timeout: float = C.BALANCE_TIMEOUT) -> object:
    try:
        response = await _request(
            'GET', C.AUTH_BASE + C.ENTITLEMENT_PATH, token=token,
            headers=_headers({}, token), proxy=proxy, timeout=timeout)
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc
    payload = _json(response)
    error = classify(response.status_code, payload, '查询小浣熊订阅失败')
    if error is not None:
        raise error
    if isinstance(payload, dict):
        return payload.get('data')
    return None


async def claim_daily_grant(account: dict, token: str, *,
                            proxy: str | None = None,
                            timeout: float = C.BALANCE_TIMEOUT) -> dict:
    """读一次桌面端 `setting_info`，触发每日积分发放。

    上游没有独立的「领取」接口：桌面客户端打开时读这个接口，服务端就会把
    当天的积分发下来。返回 `popups` / `toast` 原样，供界面提示。
    """
    try:
        response = await _request(
            'GET', C.OFFICE_BASE + C.SETTING_INFO_PATH, token=token,
            headers=_headers(account, token, desktop=True),
            proxy=proxy, timeout=timeout)
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc
    payload = _json(response)
    error = classify(response.status_code, payload, '触发小浣熊每日积分失败')
    if error is not None:
        raise error
    body = payload if isinstance(payload, dict) else {}
    data = body.get('data') if isinstance(body.get('data'), dict) else {}
    return {
        'ok': True,
        'popups': data.get('point_grant_popups') or [],
        'toast': data.get('point_grant_toast'),
    }


async def bills(token: str, *, limit: int = 20, proxy: str | None = None,
                timeout: float = C.BILLS_TIMEOUT) -> list[dict]:
    """积分账单列表（`data.items[]`）。"""
    limit = max(1, min(100, int(limit or 20)))
    url = (f'{C.POINTS_BASE}{C.BILLS_PATH}'
           f'?paging.limit={limit}&paging.offset=0')
    try:
        response = await _request(
            'GET', url, token=token, headers=_headers({}, token),
            proxy=proxy, timeout=timeout)
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc
    payload = _json(response)
    error = classify(response.status_code, payload, '查询小浣熊积分账单失败')
    if error is not None:
        raise error
    body = payload if isinstance(payload, dict) else {}
    data = body.get('data') if isinstance(body.get('data'), dict) else {}
    items = data.get('items')
    if not isinstance(items, list):
        items = body.get('items')
    return [item for item in items if isinstance(item, dict)] \
        if isinstance(items, list) else []


# ── 时间 / 签到判定 ─────────────────────────────────────────
def beijing_now(now: float | None = None) -> datetime:
    return datetime.fromtimestamp(time.time() if now is None else now, tz=_BEIJING)


def beijing_date(now: float | None = None) -> str:
    return beijing_now(now).strftime('%Y-%m-%d')


def parse_bill_time(value: object) -> float:
    """账单 `created_at` → epoch 秒。兼容 ISO / 空格分隔 / epoch。"""
    text = str(value or '').strip()
    if not text:
        return 0.0
    try:
        number = float(text)
    except ValueError:
        number = 0.0
    if number > 0:
        return number / 1000.0 if number > 1e11 else number
    candidate = text.replace('Z', '+00:00')
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S'):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_BEIJING)
    return parsed.timestamp()


def bill_is_today(bill: dict, now: float | None = None,
                  grace_minutes: int = 90) -> bool:
    """账单是否属于「今天」（北京时间，凌晨按前一天宽限）。

    积分发放发生在 00:00 后不久；如果用户在 01:30 前签到，账单可能还挂在
    前一天，按北京时间当天硬判会把刚发的积分漏掉。
    """
    stamp = parse_bill_time(bill.get('created_at') or bill.get('createdAt'))
    if stamp <= 0:
        return False
    current = beijing_now(now)
    day = current.date()
    if (current.hour * 60 + current.minute) < grace_minutes:
        day = (current - timedelta(days=1)).date()
    return datetime.fromtimestamp(stamp, tz=_BEIJING).date() == day


def daily_reward(items: list[dict], now: float | None = None) -> int:
    """当天正向积分之和（只算 `points > 0` 的条目）。"""
    return sum(_point(item.get('points')) for item in items
               if bill_is_today(item, now) and _point(item.get('points')) > 0)
