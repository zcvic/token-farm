"""Discovery 上游协议层（API Key + Bearer）。

边界与其它平台一致：本模块只做「协议翻译」，不含选号、冷却、会话粘性；
那些策略在 `adapter.py`。

两条链路共用同一个 base（`discovery-api.intern-ai.org.cn`）：

  · `GET /v1/models` —— 模型目录，同时是**额度探针**（200 = 还有额度，
    429 `quota_exceeded` = 耗尽，429 `rate_limit_exceeded` = 仅限流）；
  · `POST /v1/chat/completions` —— OpenAI 兼容对话，流式原样透传。

代理口径与项目其余部分一致：`None` = 用全局代理，`''` = 强制直连
（`trust_env=False`，不读环境代理）。
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from ... import config
from ..base import PlatformError
from . import constants as C

__all__ = ['probe', 'fetch_models', 'chat', 'open_chat', 'parse_error',
           'normalize_body', 'is_transient_rejection']


# ── 请求方言归一 ────────────────────────────────────────────
# 上游只认「较早的 OpenAI 兼容面」：o-series / Responses API 那一套字段它不
# 认识，收到就整条 400（参考实现的生产事故：同一请求形状 30 次成功里夹着
# 16 次 400）。这里只改写**上游明确会拒**的部分，其余字段一字不动；标准体
# 进去出来完全不变，重复调用也不会有第二次改动（幂等）。
_ROLE_MAP = {'developer': 'system', 'developer_v2': 'system'}
_STRIP_FIELDS = ('reasoning_effort', 'reasoning', 'verbosity', 'store',
                 'previous_response_id', 'truncation', 'instructions', 'text')


def normalize_body(body: dict) -> tuple[dict, list[str]]:
    """把请求体改写成上游认识的方言，返回 `(新体, 改动清单)`。

    改动清单只用于日志：它是排障时「为什么这次请求和客户端发的不一样」的
    唯一线索，不给客户端看。没有可改的地方时返回**原对象**，调用方据此
    判断要不要替换。
    """
    if not isinstance(body, dict):
        return body, []
    notes: list[str] = []
    out = dict(body)
    for field in _STRIP_FIELDS:
        if field in out:
            out.pop(field, None)
            notes.append(f'stripped:{field}')
    if 'max_completion_tokens' in out:
        value = out.pop('max_completion_tokens')
        if 'max_tokens' in out:
            notes.append('dropped:max_completion_tokens(max_tokens already set)')
        else:
            out['max_tokens'] = value
            notes.append('moved:max_completion_tokens->max_tokens')
    messages = out.get('messages')
    if isinstance(messages, list):
        rewritten: list = []
        changed = False
        for item in messages:
            if not isinstance(item, dict):
                rewritten.append(item)
                continue
            role = item.get('role')
            mapped = _ROLE_MAP.get(role.strip().lower()) \
                if isinstance(role, str) else None
            if mapped and role != mapped:
                # 复制一份再改：`body` 由网关持有，组合会把同一份交给多个成员。
                item = {**item, 'role': mapped}
                notes.append(f'role:{role}->{mapped}')
                changed = True
            rewritten.append(item)
        if changed:
            out['messages'] = rewritten
    if not notes:
        return body, []
    return out, notes


# 慢速 400 的判据（参考实现 35a8d552）：上游把请求收下、处理了十几秒才回
# 「upstream_request_rejected」，那是上游后端抖动，不是客户端写错。这类
# 400 换一把 Key 重试一次往往就过了；schema 级的 400 都在 1 秒内回来，
# 所以「耗时」就是最干净的判别信号。
TRANSIENT_400_CODE = 'upstream_request_rejected'
TRANSIENT_400_MIN_ELAPSED = 10.0


def is_transient_rejection(exc: PlatformError, elapsed: float) -> bool:
    """这次 400 是不是「上游后端拒绝」，值得换 Key 再试一次。"""
    if getattr(exc, 'status', None) != 400:
        return False
    if elapsed < TRANSIENT_400_MIN_ELAPSED:
        return False
    code = str(getattr(exc, 'code', '') or '')
    if code == TRANSIENT_400_CODE:
        return True
    payload = getattr(exc, 'payload', None)
    return parse_error(payload) == TRANSIENT_400_CODE


# ── 通用 ────────────────────────────────────────────────────
def _selected_proxy(proxy: str | None) -> str:
    if proxy is None:
        return config.effective_http_proxy()
    return str(proxy).strip()


def _headers(api_key: str) -> dict[str, str]:
    return {
        'Authorization': f'Bearer {api_key}',
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'User-Agent': C.UA,
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
        error = payload.get('error')
        if isinstance(error, dict):
            for key in ('message', 'msg', 'detail'):
                if error.get(key):
                    return str(error[key])
        for key in ('message', 'msg', 'detail'):
            if payload.get(key):
                return str(payload[key])
    return fallback


def parse_error(payload: object) -> str:
    """上游错误体里的稳定错误码（`quota_exceeded` / `rate_limit_exceeded`）。"""
    if not isinstance(payload, dict):
        return ''
    error = payload.get('error')
    if isinstance(error, dict):
        return str(error.get('code') or error.get('type') or '').strip()
    return str(payload.get('code') or '').strip()


def _retry_after(response: httpx.Response) -> int | None:
    raw = response.headers.get('retry-after')
    if not raw:
        return None
    try:
        return max(0, int(float(raw)))
    except (TypeError, ValueError):
        return None


def _response_error(response: httpx.Response, text: str,
                    payload: object = None) -> PlatformError:
    """把上游错误映射成带「该不该换号 / 冻多久」语义的状态码。

    401 = Key 失效（凭证问题，冻了也不会自己好）；
    429 `quota_exceeded` = **额度耗尽**，按 402 上报（适配器据此冻 1 小时，
    而不是按限流的几十秒——额度不会在几十秒内回来）；
    429 其它 = 限流；
    5xx = 上游故障。
    """
    message = _error_text(payload, text[:200] or f'上游 HTTP {response.status_code}')
    status = response.status_code
    code = parse_error(payload)
    if status in (401, 403) or code == C.CODE_INVALID_KEY:
        return PlatformError(message, status=401, code='login_expired',
                             payload=payload)
    if code == 'model_disabled':
        # 上游下线模型时返回 404 + error.suggested_model。原样透传 404，但把
        # 替代模型写进文案：客户端至少知道该改什么，网关不静默改写请求模型。
        suggested = ''
        if isinstance(payload, dict) and isinstance(payload.get('error'), dict):
            suggested = str(payload['error'].get('suggested_model') or '').strip()
        if suggested:
            message = f'{message}（上游建议改用 {suggested}；不会自动改写请求）'
        return PlatformError(message, status=404, code='model_disabled',
                             payload=payload)
    if code == C.CODE_QUOTA:
        return PlatformError(message, status=402, code='quota_exceeded',
                             payload=payload)
    if status == 429 or code == C.CODE_RATE:
        lowered = message.casefold()
        if status == 429 and ('cluster tpm' in lowered
                              or 'cluster rate limit' in lowered):
            # 账户级（Cluster TPM）限流：所有 Key 共享同一个上限，换号逃不掉，
            # 冷却单个账号只会把整池轮着冻一遍。标记出来让适配器等窗口。
            return PlatformError(message, status=429, code='cluster_rate_limited',
                                 retry_after=_retry_after(response), payload=payload)
        return PlatformError(message, status=429, code='rate_limited',
                             retry_after=_retry_after(response), payload=payload)
    if status >= 500:
        return PlatformError(message, status=502, code='upstream_error',
                             payload=payload)
    return PlatformError(message, status=status, code='bad_request',
                         payload=payload)


def _network_error(exc: Exception) -> PlatformError:
    return PlatformError(f'连接 Discovery 失败：{exc}', status=502,
                         code='network_error')


# ── 模型目录 / 额度探针 ─────────────────────────────────────
def parse_models(payload: object) -> list[dict]:
    """解析 `/v1/models`；形状不对返回空表（调用方降级到内置目录）。"""
    if not isinstance(payload, dict):
        return []
    data = payload.get('data')
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for raw in data:
        if not isinstance(raw, dict):
            continue
        model_id = str(raw.get('id') or '').strip()
        if not model_id or model_id.lower() in seen:
            continue
        seen.add(model_id.lower())
        context = raw.get('context_length') or raw.get('context_window') or 0
        try:
            context = int(context or 0)
        except (TypeError, ValueError):
            context = 0
        out.append({
            'id': model_id,
            'name': str(raw.get('name') or model_id),
            'context_length': context,
            'max_tokens': 0,
            'modalities': ['text'],
            'reasoning': bool(raw.get('reasoning')
                              or raw.get('supports_reasoning')),
            'type': 'chat',
        })
    return out


async def probe(api_key: str, *, proxy: str | None = None,
                timeout: float = C.MODELS_TIMEOUT) -> dict:
    """额度探针 + 模型目录（一次请求）。

    返回 `{state, message, models}`，`state` ∈
    `ok` / `quota_exhausted` / `rate_limited` / `invalid` / `unknown`。

    **不抛异常**：探针是「看一眼还有没有额度」，调用方（账号页 / 批量探活）
    要的是状态，而不是让一条上游抖动把整页打挂。真正要报错的路径是 `chat`。
    """
    key = str(api_key or '').strip()
    if not key:
        return {'state': 'invalid', 'message': '缺少 API Key', 'models': []}
    try:
        async with config.http_client(
                httpx.Timeout(timeout, connect=10),
                proxy=_selected_proxy(proxy), lane='discovery') as client:
            response = await client.get(C.BASE_URL + C.MODELS_PATH,
                                        headers=_headers(key))
    except httpx.HTTPError as exc:
        return {'state': 'unknown', 'message': f'连接失败：{exc}', 'models': []}

    payload = _json(response)
    if response.status_code == 200:
        return {'state': 'ok', 'message': '',
                'models': parse_models(payload)}
    code = parse_error(payload)
    message = _error_text(payload, f'HTTP {response.status_code}')
    if response.status_code in (401, 403) or code == C.CODE_INVALID_KEY:
        return {'state': 'invalid', 'message': message, 'models': []}
    if code == C.CODE_QUOTA:
        return {'state': 'quota_exhausted', 'message': message, 'models': []}
    if response.status_code == 429:
        return {'state': 'rate_limited', 'message': message, 'models': []}
    return {'state': 'unknown', 'message': message, 'models': []}


async def fetch_models(api_key: str, *, proxy: str | None = None,
                       timeout: float = C.MODELS_TIMEOUT) -> list[dict]:
    """只取模型目录；拿不到时抛 `PlatformError`（`list_models` 需要知道失败）。"""
    key = str(api_key or '').strip()
    if not key:
        raise PlatformError('缺少 API Key', status=401, code='login_expired')
    try:
        async with config.http_client(
                httpx.Timeout(timeout, connect=10),
                proxy=_selected_proxy(proxy), lane='discovery') as client:
            response = await client.get(C.BASE_URL + C.MODELS_PATH,
                                        headers=_headers(key))
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc
    payload = _json(response)
    if response.status_code >= 400:
        raise _response_error(response, response.text, payload)
    return parse_models(payload)


# ── 对话 ────────────────────────────────────────────────────
async def chat(api_key: str, body: dict, *, proxy: str | None = None,
               timeout: float = C.CHAT_TIMEOUT) -> dict:
    """非流式对话，返回上游 JSON。"""
    async with config.http_client(
            httpx.Timeout(timeout, connect=10),
            proxy=_selected_proxy(proxy), lane='discovery') as client:
        response = await client.post(
            C.BASE_URL + C.CHAT_PATH, headers=_headers(api_key),
            content=json.dumps(body, ensure_ascii=False).encode('utf-8'))
    text = response.text
    if response.status_code >= 400:
        raise _response_error(response, text, _json(response))
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise PlatformError('Discovery 对话响应不是 JSON', status=502,
                            code='bad_upstream', payload=text[:300]) from exc
    if not isinstance(payload, dict):
        raise PlatformError('Discovery 对话响应形状异常', status=502,
                            code='bad_upstream', payload=payload)
    return payload


async def open_chat(api_key: str, body: dict, *, proxy: str | None = None,
                    idle_timeout: float = C.CHAT_IDLE_TIMEOUT,
                    connect_timeout: float = 10) -> httpx.Response:
    """流式对话：返回**未读取**的上游响应（调用方负责关闭）。

    上游正常情况下回 `text/event-stream`；凭证 / 额度出问题时可能回 HTTP 200
    + JSON。照原样透传会把一段 JSON 当成 SSE 发给客户端，所以非 SSE 的响应
    在这里一次性读完并按错误归类。
    """
    async with config.http_client(
            httpx.Timeout(idle_timeout, connect=connect_timeout),
            proxy=_selected_proxy(proxy), lane='discovery') as client:
        request = client.build_request(
            'POST', C.BASE_URL + C.CHAT_PATH,
            headers={**_headers(api_key), 'Accept': 'text/event-stream'},
            content=json.dumps(body, ensure_ascii=False).encode('utf-8'))
        response = await client.send(request, stream=True)
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
            payload = None
            try:
                payload = json.loads(text) if text else None
            except ValueError:
                payload = None
            raise PlatformError(
                _error_text(payload, text[:200] or '对话接口未返回 SSE'),
                status=502, code='bad_upstream', payload=payload)
        return response
