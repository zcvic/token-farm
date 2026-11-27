"""CodeArts 上游协议层（登录 / 刷新 / 目录 / 对话 / 积分）。

边界与其它平台一致：本模块只做「协议翻译」，不含选号、冷却、会话粘性；
那些策略在 `adapter.py`。

关键事实（见 `deploy/codearts-ref.txt`）：

  · token 端点要求 `DPoP` 证明，且 refresh_token 与 **client_id + DPoP 公钥**
    三者绑定；refresh_token 一次性轮换；
  · 拿到的是 STS 临时凭证，所有业务请求还要再做 `SDK-HMAC-SHA256` 签名；
  · 对话走 `/api/v2/chat/completions`，SSE 里的 `text` 是**累计全文**
    （替换语义），不是增量；直接透传会把全文重复 N 遍；
  · 未知模型 / 未领福利的模型在上游是 `InferHub.002002009.404`，必须按账号
    目录判定是否带 `maas_type: benefit`；
  · HTTP 200 里也可能内嵌业务错误，必须在首帧之前识别出来。
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncIterator, Iterable

import httpx

from ... import config
from ..base import PlatformError
from . import constants as C
from . import dpop, signer, store

__all__ = [
    'Credential', 'ChatStream', 'StreamTranslator',
    'refresh', 'exchange_code', 'poll_ticket', 'normalize_token_response',
    'current_user', 'caller_identity', 'probe',
    'fetch_agent_models', 'fetch_builtin_models', 'fetch_benefit_models',
    'claim_benefit', 'fetch_catalog', 'benefit_balance',
    'build_chat_body', 'open_chat', 'iter_events', 'aggregate', 'checkin',
    'canonical_model', 'session_id_for', 'new_chat_id',
]

# ── 通用 ────────────────────────────────────────────────────
def _selected_proxy(proxy: str | None) -> str:
    if proxy is None:
        return config.effective_http_proxy()
    return str(proxy).strip()


def _json(response: httpx.Response) -> object:
    text = response.text
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _message_of(payload: object, fallback: str = '') -> str:
    if not isinstance(payload, dict):
        return fallback
    for key in ('error_msg', 'message', 'msg', 'error_description',
                'errorMessage'):
        value = payload.get(key)
        if value:
            return str(value)
    error = payload.get('error')
    if isinstance(error, dict):
        for key in ('message', 'msg', 'detail'):
            if error.get(key):
                return str(error[key])
    if isinstance(error, str) and error.strip():
        return error
    return fallback


def _code_of(payload: object) -> str:
    if not isinstance(payload, dict):
        return ''
    for key in ('error_code', 'code', 'errorCode'):
        value = payload.get(key)
        if value not in (None, ''):
            return str(value).strip()
    return ''


def _is_success_code(code: str) -> bool:
    return code in ('', '0', '0000')


def _status_for(text: str) -> int:
    """业务错误文本 → 该回给客户端的状态码。"""
    if C.is_rate_limited(text):
        return 429
    if C.is_model_error(text) or C.is_bad_request(text):
        return 400
    return 502


def classify(status: int, payload: object,
             fallback: str = 'CodeArts 上游返回错误') -> PlatformError | None:
    """上游失败 → 带 HTTP 语义的 `PlatformError`；成功返回 None。

    只有明确的鉴权失败才报 401（否则账号会被误标失效并从池里摘掉）；
    限流报 429；模型级 / 参数级错误报 400（是客户端问题，不是账号故障）。
    """
    if status == 401:
        return PlatformError(_message_of(payload, fallback), status=401,
                             code='auth_expired', payload=payload)
    if status == 403:
        return PlatformError(_message_of(payload, fallback), status=403,
                             code='forbidden', payload=payload)
    if status == 429:
        return PlatformError(_message_of(payload, fallback), status=429,
                             code='rate_limited', payload=payload)
    code = _code_of(payload)
    text = ' '.join(filter(None, (_message_of(payload), code)))
    if not _is_success_code(code):
        return PlatformError(text.strip() or fallback,
                             status=_status_for(text), code='upstream_error',
                             payload=payload)
    if status >= 500:
        return PlatformError(_message_of(payload, fallback), status=502,
                             code='upstream_error', payload=payload)
    if status >= 400:
        return PlatformError(_message_of(payload, fallback), status=400,
                             code='bad_request', payload=payload)
    return None


def _network_error(exc: Exception) -> PlatformError:
    return PlatformError(f'连接 CodeArts 失败：{exc}', status=502,
                         code='network_error')


async def _request(method: str, url: str, *, headers: dict[str, str] | None = None,
                   body: bytes | None = None, proxy: str | None = None,
                   timeout: float, stream: bool = False) -> httpx.Response:
    client_timeout = httpx.Timeout(timeout, connect=15)
    if stream:
        async with config.http_client(
                client_timeout, proxy=_selected_proxy(proxy),
                lane='codearts') as client:
            request = client.build_request(method, url, headers=headers,
                                           content=body)
            return await client.send(request, stream=True)
    async with config.http_client(
            client_timeout, proxy=_selected_proxy(proxy),
            lane='codearts') as client:
        return await client.request(method, url, headers=headers, content=body)


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


# ── 凭据 ────────────────────────────────────────────────────
class Credential:
    """一次请求要用的 STS 临时凭证（AK/SK + security_token）。"""

    __slots__ = ('access_key', 'secret_key', 'security_token')

    def __init__(self, access_key: str = '', secret_key: str = '',
                 security_token: str = '') -> None:
        self.access_key = str(access_key or '').strip()
        self.secret_key = str(secret_key or '').strip()
        self.security_token = str(security_token or '').strip()

    @classmethod
    def of(cls, account: dict) -> 'Credential':
        return cls(account.get('accessKeyId'), account.get('secretAccessKey'),
                   account.get('securityToken'))

    def __bool__(self) -> bool:
        return bool(self.access_key and self.secret_key)


def _signed(cred: Credential, method: str, url: str,
            headers: dict[str, str], body: bytes = b'') -> dict[str, str]:
    if not cred:
        raise PlatformError('CodeArts 账号缺少 AK/SK', status=401,
                            code='auth_expired')
    return signer.sign(method, url, headers, body,
                       access_key=cred.access_key,
                       secret_key=cred.secret_key,
                       security_token=cred.security_token)


# ── 登录 / 刷新 ─────────────────────────────────────────────
def normalize_token_response(payload: object) -> dict:
    """把 token 响应归一成账号字段。

    新版走 `credentials`（`access_key_id` / `secret_access_key` /
    `security_token` / `expiration`）；旧 ticket 通道给的是
    `credential:{access,secret,securitytoken,expires_at}`，字段名完全不同。
    """
    body = payload if isinstance(payload, dict) else {}
    creds = body.get('credentials') if isinstance(body.get('credentials'),
                                                  dict) else {}
    legacy = body.get('credential') if isinstance(body.get('credential'),
                                                  dict) else {}
    access_key = str(creds.get('access_key_id') or legacy.get('access') or '')
    secret = str(creds.get('secret_access_key') or legacy.get('secret') or '')
    token = str(creds.get('security_token')
                or legacy.get('securitytoken') or '')
    expiration = str(creds.get('expiration') or legacy.get('expires_at') or '')
    if not token:
        raise PlatformError(
            f'CodeArts 登录响应里没有 security_token：{_message_of(body, "")}',
            status=502, code='oauth_bad_response', payload=payload)
    return {
        'user_id': str(body.get('user_id') or '').strip(),
        'user_name': str(body.get('user_name') or '').strip(),
        'domain_id': str(body.get('domain_id') or '').strip(),
        'refresh_token': str(body.get('refresh_token') or '').strip(),
        'access_key_id': access_key.strip(),
        'secret_access_key': secret.strip(),
        'security_token': token.strip(),
        'expiration': expiration.strip(),
        'expires_at': store.epoch_from_iso(expiration),
    }


async def _token_request(form: dict[str, str], jwk: dict, *,
                         proxy: str | None,
                         timeout: float = C.TOKEN_TIMEOUT) -> dict:
    """`POST {STS}/v1/oauth2/tokens`：form-urlencoded + DPoP 头。"""
    payload = '&'.join(
        f'{_form_escape(key)}={_form_escape(value)}'
        for key, value in form.items())
    headers = {
        'Content-Type': 'application/x-www-form-urlencoded',
        'Accept': 'application/json',
        'DPoP': dpop.proof(jwk, C.TOKEN_URL),
    }
    try:
        response = await _request('POST', C.TOKEN_URL, headers=headers,
                                  body=payload.encode('utf-8'), proxy=proxy,
                                  timeout=timeout)
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc
    data = _json(response)
    if response.status_code >= 400:
        message = _message_of(data, '')
        code = _code_of(data)
        combined = ' '.join(filter(None, (message, code)))
        if form.get('grant_type') == 'refresh_token' \
                and C.is_refresh_expired(combined):
            raise PlatformError(
                f'CodeArts refresh_token 已失效，请重新登录：{combined}',
                status=401, code='login_expired', payload=data)
        error = classify(response.status_code, data, 'CodeArts 换取令牌失败')
        if error is not None:
            raise error
        raise PlatformError(f'CodeArts 换取令牌失败：{combined}'.strip(),
                            status=502, code='oauth_failed', payload=data)
    return normalize_token_response(data)


def _form_escape(value: object) -> str:
    import urllib.parse
    return urllib.parse.quote_plus(str(value or ''), safe='')


async def exchange_code(code: str, code_verifier: str, jwk: dict, port: int, *,
                        client_id: str = C.CLIENT_ID,
                        proxy: str | None = None,
                        timeout: float = C.TOKEN_TIMEOUT) -> dict:
    """用授权码换 STS 凭证（`grant_type=authorization_code`）。"""
    if not str(code or '').strip():
        raise PlatformError('缺少授权码', status=400, code='invalid_callback')
    return await _token_request({
        'client_id': client_id or C.CLIENT_ID,
        'code': str(code).strip(),
        'code_verifier': str(code_verifier or ''),
        'grant_type': 'authorization_code',
        'redirect_uri': f'http://127.0.0.1:{int(port)}{C.REDIRECT_PATH}',
    }, jwk, proxy=proxy, timeout=timeout)


async def refresh(refresh_token: str, code_verifier: str, jwk: dict, *,
                  client_id: str = C.CLIENT_ID, proxy: str | None = None,
                  timeout: float = C.TOKEN_TIMEOUT) -> dict:
    """`grant_type=refresh_token`。

    必须传签发该 refresh_token 时用的 DPoP 私钥与 client_id，否则 STS 会回
    `InvalidDPoPHeader` / `invalid client id`。成功后上游会**轮换**
    refresh_token（响应里的新值必须落盘）。
    """
    key = str(refresh_token or '').strip()
    if not key:
        raise PlatformError('缺少 refresh_token', status=401,
                            code='login_expired')
    return await _token_request({
        'client_id': client_id or C.CLIENT_ID,
        'code_verifier': str(code_verifier or ''),
        'grant_type': 'refresh_token',
        'refresh_token': key,
    }, jwk, proxy=proxy, timeout=timeout)


async def poll_ticket(ticket_id: str, secret: str, *, proxy: str | None = None,
                      timeout: float = C.TOKEN_TIMEOUT) -> dict:
    """ticket 通道（兜底）：`GET {snap-manager}/v1/login/ticket`。

    只用于「回环回调收不到」的场景；返回的可能是旧式 `credential` 字段。
    """
    url = (f'{C.SNAP_MANAGER}{C.EP_LOGIN_TICKET}'
           f'?ticket_id={_form_escape(ticket_id)}&secret={_form_escape(secret)}')
    headers = {'Accept': 'application/json',
               'plugin-name': C.PLUGIN_NAME,
               'plugin-version': C.PLUGIN_VERSION}
    try:
        response = await _request('GET', url, headers=headers, proxy=proxy,
                                  timeout=timeout)
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc
    data = _json(response)
    error = classify(response.status_code, data, 'CodeArts 轮询登录结果失败')
    if error is not None:
        raise error
    return normalize_token_response(data)


# ── 身份 ────────────────────────────────────────────────────
async def current_user(cred: Credential, *, proxy: str | None = None,
                       timeout: float = C.CATALOG_TIMEOUT) -> dict:
    """`GET {snap-manager}/v1/current/user`。

    注意必须带 `/snap-manager` 前缀，漏掉会回 `APIG.0101 The API does not
    exist`。
    """
    url = C.SNAP_MANAGER + C.EP_CURRENT_USER
    headers = _signed(cred, 'GET', url,
                      {'Content-Type': 'application/json',
                       'X-Language': C.LANGUAGE})
    try:
        response = await _request('GET', url, headers=headers, proxy=proxy,
                                  timeout=timeout)
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc
    data = _json(response)
    error = classify(response.status_code, data, '查询 CodeArts 账号信息失败')
    if error is not None:
        raise error
    body = data if isinstance(data, dict) else {}
    return {'user_id': str(body.get('user_id') or '').strip(),
            'user_name': str(body.get('user_name') or '').strip(),
            'domain_id': str(body.get('domain_id') or '').strip()}


async def caller_identity(cred: Credential, *, proxy: str | None = None,
                          timeout: float = C.CATALOG_TIMEOUT) -> dict:
    """`GET {sts}/v5/caller-identity`（只在 `sts.cn-north-4` 域可用）。"""
    url = C.STS_HOST + C.EP_CALLER_IDENTITY
    headers = _signed(cred, 'GET', url,
                      {'Content-Type': 'application/json',
                       'X-Language': C.LANGUAGE})
    try:
        response = await _request('GET', url, headers=headers, proxy=proxy,
                                  timeout=timeout)
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc
    data = _json(response)
    error = classify(response.status_code, data, '查询 CodeArts 身份失败')
    if error is not None:
        raise error
    body = data if isinstance(data, dict) else {}
    urn = str(body.get('principal_urn') or '')
    name = urn.split(':user:')[-1] if ':user:' in urn else ''
    return {'account_id': str(body.get('account_id') or '').strip(),
            'principal_id': str(body.get('principal_id') or '').strip(),
            'user_name': name.strip()}


async def probe(cred: Credential, *, proxy: str | None = None,
                timeout: float = C.CATALOG_TIMEOUT) -> dict:
    """用身份接口探活：返回 `{state, message, identity}`。"""
    try:
        identity = await current_user(cred, proxy=proxy, timeout=timeout)
    except PlatformError as exc:
        return {'state': 'invalid' if exc.status == 401 else 'unknown',
                'message': exc.message, 'identity': {}}
    return {'state': 'ok', 'message': '', 'identity': identity}


# ── 模型目录 ────────────────────────────────────────────────
def _signed_get(url: str, cred: Credential, headers: dict[str, str]) -> dict:
    return _signed(cred, 'GET', url, headers)


async def _get_json(url: str, cred: Credential, headers: dict[str, str], *,
                    proxy: str | None, timeout: float,
                    fallback: str) -> object:
    signed = _signed(cred, 'GET', url, headers)
    try:
        response = await _request('GET', url, headers=signed, proxy=proxy,
                                  timeout=timeout)
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc
    data = _json(response)
    error = classify(response.status_code, data, fallback)
    if error is not None:
        raise error
    return data


def _model_entry(model_id: str, *, name: str = '', desc: str = '',
                 context: object = 0, max_tokens: object = 0,
                 benefit: bool = False) -> dict:
    return {
        'id': str(model_id or '').strip(),
        'name': str(name or model_id or '').strip(),
        'description': str(desc or '').strip(),
        'context_length': int(_num(context) or 0),
        'max_tokens': int(_num(max_tokens) or 0),
        'benefit': bool(benefit),
    }


def _num(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _first(*values: object) -> str:
    for value in values:
        text = str(value or '').strip()
        if text:
            return text
    return ''


async def fetch_agent_models(cred: Credential, *, proxy: str | None = None,
                             timeout: float = C.CATALOG_TIMEOUT) -> list[dict]:
    """agent-center：`useragents` → `detail.gpts.models`。

    默认 agent 的挑选口径与官方客户端一致：`agent_name=="CodeAgent"` 且
    `alias.alias_zh_cn=="智能体"` 且 `show_in_ide`；退化 `is_primary_agent`，
    再退化首个。
    """
    list_url = (f'{C.SNAP_HOST}{C.EP_AGENT_LIST}'
                '?offset=0&limit=100&is_primary_agent=true')
    headers = {'Content-Type': 'application/json',
               'X-Language': C.LANGUAGE,
               'Agent-Type': C.AGENT_TYPE_AGENT_CENTER}
    data = await _get_json(list_url, cred, headers, proxy=proxy,
                           timeout=timeout, fallback='拉取 CodeArts agent 列表失败')
    agents = data.get('agents') if isinstance(data, dict) else None
    if not isinstance(agents, list):
        raise PlatformError('CodeArts agent 列表为空', status=502,
                            code='empty_catalog', payload=data)
    chosen = ''
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        alias = agent.get('alias') if isinstance(agent.get('alias'),
                                                 dict) else {}
        if str(agent.get('agent_name') or '') == 'CodeAgent' \
                and str(alias.get('alias_zh_cn') or '') == '智能体' \
                and bool(agent.get('show_in_ide')):
            chosen = str(agent.get('agent_id') or '')
            break
    if not chosen:
        for agent in agents:
            if isinstance(agent, dict) and agent.get('is_primary_agent'):
                chosen = str(agent.get('agent_id') or '')
                break
    if not chosen and agents and isinstance(agents[0], dict):
        chosen = str(agents[0].get('agent_id') or '')
    if not chosen:
        raise PlatformError('CodeArts 没有可用的默认 agent', status=502,
                            code='empty_catalog')

    detail_url = (f'{C.SNAP_HOST}{C.EP_AGENT_DETAIL}'
                  f'?agent_id={_form_escape(chosen)}')
    detail = await _get_json(detail_url, cred, headers, proxy=proxy,
                             timeout=timeout,
                             fallback='拉取 CodeArts agent 详情失败')
    gpts = detail.get('gpts') if isinstance(detail, dict) else None
    models = gpts.get('models') if isinstance(gpts, dict) else None
    if not isinstance(models, list):
        return []
    out: list[dict] = []
    for raw in models:
        if not isinstance(raw, dict):
            continue
        params = raw.get('model_parameters') \
            if isinstance(raw.get('model_parameters'), dict) else {}
        model_id = _first(raw.get('model_name'), raw.get('model_id'),
                          raw.get('model_alias'), params.get('model_id'))
        if not model_id:
            continue
        out.append(_model_entry(
            model_id,
            name=_first(raw.get('model_name'), model_id),
            desc=_first(params.get('model_desc')),
            context=params.get('context_window'),
            max_tokens=params.get('max_tokens')))
    return out


async def fetch_builtin_models(cred: Credential, *, proxy: str | None = None,
                               timeout: float = C.CATALOG_TIMEOUT) -> list[dict]:
    """`GET {snap}/v1/model/builtin`（`Agent-Type: PromptCenter`）。"""
    url = C.SNAP_HOST + C.EP_MODEL_BUILTIN
    headers = {'Content-Type': 'application/json',
               'X-Language': C.LANGUAGE,
               'Agent-Type': C.AGENT_TYPE_PROMPT_CENTER}
    data = await _get_json(url, cred, headers, proxy=proxy, timeout=timeout,
                           fallback='拉取 CodeArts 内置模型失败')
    models = data.get('builtinModels') if isinstance(data, dict) else None
    if not isinstance(models, list):
        return []
    out: list[dict] = []
    for raw in models:
        if not isinstance(raw, dict):
            continue
        model_id = _first(raw.get('model_id'), raw.get('model_name'))
        if not model_id:
            continue
        out.append(_model_entry(
            model_id,
            name=_first(raw.get('model_name'), model_id),
            desc=_first(raw.get('model_desc')),
            context=raw.get('context_window'),
            max_tokens=raw.get('max_tokens')))
    return out


async def fetch_benefit_models(cred: Credential, *, proxy: str | None = None,
                               timeout: float = C.CATALOG_TIMEOUT) -> list[dict]:
    """限时福利（免费套餐）网关：`GET /api/v1/gateway/config`。

    返回的模型全部标记 `benefit=True`：对话时必须带 `maas_type: benefit`，
    否则上游按未注册模型 404。
    """
    url = C.BENEFIT_HOST + C.EP_BENEFIT_CONFIG
    headers = {'Content-Type': 'application/json', 'X-Language': C.LANGUAGE}
    data = await _get_json(url, cred, headers, proxy=proxy, timeout=timeout,
                           fallback='拉取 CodeArts 限时福利模型失败')
    body = data if isinstance(data, dict) else {}
    if _code_of(body) not in ('', '0000', '0'):
        raise PlatformError(
            f'CodeArts 福利网关返回错误：{_message_of(body)}',
            status=502, code='upstream_error', payload=body)
    result = body.get('result') if isinstance(body.get('result'), dict) else {}
    models = result.get('models')
    if not isinstance(models, list):
        return []
    out: list[dict] = []
    for raw in models:
        if not isinstance(raw, dict):
            continue
        model_id = _first(raw.get('model_id'), raw.get('model_name'))
        if not model_id:
            continue
        out.append(_model_entry(
            model_id,
            name=_first(raw.get('model_name'), model_id),
            desc=_first(raw.get('model_desc')),
            context=raw.get('context_window'),
            max_tokens=raw.get('max_tokens'),
            benefit=True))
    return out


async def claim_benefit(cred: Credential, *, proxy: str | None = None,
                        timeout: float = C.CATALOG_TIMEOUT) -> bool:
    """领取限时福利（幂等：已领取返回成功）。

    这是对账号的**写操作**，只在显式任务里调用（见 `adapter.checkin`），
    不在模型目录刷新时自动执行。
    """
    url = C.BENEFIT_HOST + C.EP_BENEFIT_CLAIM
    body = b'{}'
    headers = _signed(cred, 'POST', url,
                      {'Content-Type': 'application/json',
                       'X-Language': C.LANGUAGE}, body)
    try:
        response = await _request('POST', url, headers=headers, body=body,
                                  proxy=proxy, timeout=timeout)
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc
    data = _json(response)
    error = classify(response.status_code, data, '领取 CodeArts 限时福利失败')
    if error is not None:
        raise error
    code = _code_of(data)
    if code not in ('', '0000', '0'):
        raise PlatformError(f'领取 CodeArts 限时福利失败：{_message_of(data)}',
                            status=502, code='benefit_claim_failed',
                            payload=data)
    return True


def merge_models(*sets: Iterable[dict]) -> list[dict]:
    """合并多路模型：按小写 ID 去重，福利标记取或，参数更全者优先。

    结果按小写 ID 排序，保证 `/v1/models` 输出稳定（刷新前后顺序一致）。
    """
    merged: dict[str, dict] = {}
    for group in sets:
        for raw in group or ():
            if not isinstance(raw, dict):
                continue
            model_id = str(raw.get('id') or '').strip()
            if not model_id:
                continue
            key = model_id.lower()
            current = merged.get(key)
            if current is None:
                merged[key] = dict(raw)
                continue
            merged[key] = _richer(current, raw)
    return sorted(merged.values(), key=lambda m: str(m.get('id') or '').lower())


def _richer(current: dict, other: dict) -> dict:
    """福利标记取或（漏标即漏头）；其余取更全的一条。"""
    benefit = bool(current.get('benefit')) or bool(other.get('benefit'))
    keep, drop = current, other
    if _better(other, current):
        keep, drop = other, current
    out = dict(keep)
    out['benefit'] = benefit
    for field in ('name', 'description'):
        if not out.get(field):
            out[field] = drop.get(field) or ''
    for field in ('context_length', 'max_tokens'):
        if not out.get(field):
            out[field] = int(drop.get(field) or 0)
    return out


def _better(left: dict, right: dict) -> bool:
    left_ctx = int(left.get('context_length') or 0) > 0
    right_ctx = int(right.get('context_length') or 0) > 0
    if left_ctx != right_ctx:
        return left_ctx
    left_id = str(left.get('id') or '')
    right_id = str(right.get('id') or '')
    return left_id != left_id.lower() and right_id == right_id.lower()


async def fetch_catalog(cred: Credential, *, proxy: str | None = None,
                        timeout: float = C.CATALOG_TIMEOUT) -> list[dict]:
    """三路合并拉一次目录；任一路失败只丢那一路。"""
    groups: list[list[dict]] = []
    for fetcher in (fetch_agent_models, fetch_builtin_models,
                    fetch_benefit_models):
        try:
            groups.append(await fetcher(cred, proxy=proxy, timeout=timeout))
        except PlatformError:
            continue
    return merge_models(*groups)


async def benefit_balance(cred: Credential, *, proxy: str | None = None,
                          timeout: float = C.CREDITS_TIMEOUT) -> dict:
    """福利网关的 token 余额（上游没在文档里给形状，这里按常见字段兜底）。

    拿不到就让调用方退回「签到领取量」——绝不编一个 0 出来。
    """
    url = f'{C.BENEFIT_HOST}/api/v1/user/tokens/balance'
    headers = {'Content-Type': 'application/json', 'X-Language': C.LANGUAGE}
    data = await _get_json(url, cred, headers, proxy=proxy, timeout=timeout,
                           fallback='查询 CodeArts 积分余额失败')
    body = data if isinstance(data, dict) else {}
    result = body.get('result') if isinstance(body.get('result'), dict) else {}
    pool = result or (body.get('data') if isinstance(body.get('data'), dict)
                      else body)
    for key in ('balance', 'total', 'available', 'tokens', 'remain',
                'remaining'):
        value = _num(pool.get(key))
        if value is not None:
            return {'credits': value, 'raw': body}
    raise PlatformError('CodeArts 余额响应里没有可识别的余额字段', status=502,
                        code='credits_bad_response', payload=body)


# ── 对话 ────────────────────────────────────────────────────
def canonical_model(model: str, known: dict[str, str] | None = None) -> str:
    """客户端写的模型名 → 上游注册名（大小写不敏感 + 静态别名）。

    `known` 是「小写 → 精确拼写」的索引（来自目录发现），优先于静态别名。
    """
    text = str(model or '').strip()
    if not text:
        return C.DEFAULT_MODEL
    if known:
        exact = known.get(text.lower())
        if exact:
            return exact
    alias = C.STATIC_ALIASES.get(text.lower())
    return alias or text


def new_chat_id() -> str:
    """`chat_id` 必须是 32 位十六进制（UUID 去连字符），否则上游报参数错误。"""
    return uuid.uuid4().hex


def session_id_for(chat_id: str) -> str:
    """由 chat_id 派生稳定的会话标识（prompt cache 亲和）。"""
    import hashlib
    return hashlib.sha256(
        f'codearts-session:{chat_id}'.encode('utf-8')).hexdigest()[:32]


def build_chat_body(messages: list[dict], model: str, chat_id: str,
                    options: dict | None = None) -> dict:
    """原生 OpenAI 请求体 + CodeArts 需要的亲和字段。"""
    opts = options if isinstance(options, dict) else {}
    body: dict[str, Any] = {
        'model': model,
        'stream': True,
        'messages': messages,
        'prompt_cache_key': chat_id,
        'tool_stream': True,
        'chat_id': chat_id,
    }
    for key in ('reasoning_effort', 'max_tokens', 'temperature', 'top_p',
                'tools', 'tool_choice'):
        if opts.get(key) not in (None, '', [], {}):
            body[key] = opts[key]
    if body.get('tools') and body.get('tool_choice') in (None, ''):
        body.pop('tool_choice', None)
    return body


def _chat_headers(cred: Credential, body: bytes, *, chat_id: str = '',
                  session_id: str = '', benefit: bool = False) -> dict[str, str]:
    """聊天请求头：`maas_type` 必须在签名**之前**加上（计入 SignedHeaders）。"""
    url = C.SNAP_HOST + C.EP_CHAT_V2
    headers = {
        'Content-Type': 'application/json',
        'Accept': 'text/event-stream',
        'x-auth-token': cred.security_token,
        'x-snap-traceid': uuid.uuid4().hex,
        'X-Language': C.LANGUAGE,
        'app-id': C.APP_ID,
        'is_confidential': 'false',
    }
    if benefit:
        headers[C.HEADER_MAAS_TYPE] = C.MAAS_BENEFIT
    signed = _signed(cred, 'POST', url, headers, body)
    # Chat-Id / Session-Id 在签名之后设置（官方客户端也是这个顺序）。
    if chat_id:
        signed['Chat-Id'] = chat_id
    if session_id:
        signed['Session-Id'] = session_id
    return signed


class ChatStream:
    """已通过首帧校验的上游 SSE 流。

    `prefetched` 是校验阶段已经读掉的原始行，`iter_events` 会先重放它们，
    再接着读 `response`。
    """

    def __init__(self, response: httpx.Response, lines: AsyncIterator[str],
                 prefetched: list[str]) -> None:
        self.response = response
        self._lines = lines
        self.prefetched = prefetched

    async def aclose(self) -> None:
        try:
            await self.response.aclose()
        except Exception:  # noqa: BLE001
            pass


def _role_only(payload: object) -> bool:
    """只有 `role` 的首帧：上游可能在它之后才吐业务错误。"""
    if not isinstance(payload, dict):
        return False
    choices = payload.get('choices')
    if not isinstance(choices, list) or len(choices) != 1:
        return False
    choice = choices[0] if isinstance(choices[0], dict) else {}
    delta = choice.get('delta')
    if not isinstance(delta, dict):
        return False
    if delta.get('role') != 'assistant' or choice.get('finish_reason'):
        return False
    return not any(key != 'role' and value not in (None, '')
                   for key, value in delta.items())


def embedded_error(payload: object) -> PlatformError | None:
    """识别 HTTP 200 里内嵌的业务错误。"""
    if not isinstance(payload, dict):
        return None
    code = _code_of(payload)
    if _is_success_code(code):
        return None
    text = ' '.join(filter(None, (_message_of(payload), code))).strip()
    return PlatformError(text or 'CodeArts 上游返回错误',
                         status=_status_for(text), code='upstream_error',
                         payload=payload)


async def open_chat(cred: Credential, payload: dict, *, chat_id: str = '',
                    session_id: str = '', benefit: bool = False,
                    proxy: str | None = None,
                    idle_timeout: float = C.CHAT_IDLE_TIMEOUT) -> ChatStream:
    """发起流式对话，并在返回前完成首帧业务错误校验。"""
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    headers = _chat_headers(cred, body, chat_id=chat_id,
                            session_id=session_id, benefit=benefit)
    url = C.SNAP_HOST + C.EP_CHAT_V2
    try:
        response = await _request('POST', url, headers=headers, body=body,
                                  proxy=proxy, timeout=idle_timeout,
                                  stream=True)
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc
    if response.status_code >= 400:
        _, data = await _read_error(response)
        error = classify(response.status_code, data, 'CodeArts 对话失败')
        if error is not None:
            raise error
        raise PlatformError('CodeArts 对话失败', status=502,
                            code='upstream_error', payload=data)

    lines = response.aiter_lines()
    prefetched: list[str] = []
    pending_event = ''
    saw_role = False
    try:
        while len(prefetched) < 4096:
            try:
                line = await lines.__anext__()
            except StopAsyncIteration:
                break
            except httpx.HTTPError as exc:
                raise _network_error(exc) from exc
            prefetched.append(line)
            event, data = _scan_line(line, pending_event)
            if event is not None:
                pending_event = event
                continue
            if data is None:
                continue
            pending_event = ''
            if data.strip() == '[DONE]':
                break
            try:
                obj = json.loads(data)
            except ValueError:
                continue
            error = embedded_error(obj)
            if error is not None and (not saw_role or C.is_model_error(
                    error.message + ' ' + _code_of(obj))):
                await _close_quietly(response)
                raise error
            if not saw_role and _role_only(obj):
                saw_role = True
                continue
            break
    except BaseException:
        await _close_quietly(response)
        raise
    return ChatStream(response, lines, prefetched)


async def _close_quietly(response: httpx.Response) -> None:
    try:
        await response.aclose()
    except Exception:  # noqa: BLE001
        pass


def _scan_line(line: str, pending_event: str) \
        -> tuple[str | None, str | None]:
    """解析一行 SSE。

    返回 `(event, None)` = 这一行是 `event:`；`(None, data)` = 数据行；
    `(None, None)` = 其他（注释 / 空行 / 保活）。
    """
    text = str(line or '').rstrip('\r')
    if text.startswith('event:'):
        return text[6:].strip(), None
    if text.startswith('data:'):
        data = text[5:].strip()
        if not data:
            return None, None
        return None, data
    if text.startswith(':') or not text.strip():
        return None, None
    # 少数版本直接吐裸 JSON 行（没有 data: 前缀）。
    stripped = text.strip()
    if stripped.startswith('{'):
        return None, stripped
    return None, None


async def iter_events(stream: ChatStream) -> AsyncIterator[tuple[str, object]]:
    """把 SSE 读成 `(event, payload)`；`[DONE]` 结束。"""
    pending_event = ''
    for line in stream.prefetched:
        event, data = _scan_line(line, pending_event)
        if event is not None:
            pending_event = event
            continue
        if data is None:
            continue
        if data.strip() == '[DONE]':
            return
        try:
            payload = json.loads(data)
        except ValueError:
            pending_event = ''
            continue
        yield pending_event.lower(), payload
        pending_event = ''
    async for line in stream._lines:
        event, data = _scan_line(line, pending_event)
        if event is not None:
            pending_event = event
            continue
        if data is None:
            continue
        if data.strip() == '[DONE]':
            return
        try:
            payload = json.loads(data)
        except ValueError:
            pending_event = ''
            continue
        yield pending_event.lower(), payload
        pending_event = ''


# ── SSE 语义 → OpenAI chunk ─────────────────────────────────
_QA_BLOCKERS = ('import ', 'def ', 'class ', 'func ', 'package ', 'module ',
                'struct ', 'interface ')


def is_structured_qa(payload: object) -> bool:
    """是否是「相关追问」式的结构化问答对象（不是正文）。"""
    if not isinstance(payload, dict):
        return False
    question, answer = payload.get('question'), payload.get('answer')
    if not isinstance(question, str) or not isinstance(answer, str):
        return False
    if not question or len(question) > 500:
        return False
    if not answer or len(answer) > 100:
        return False
    low = answer.lower()
    if any(marker in low for marker in _QA_BLOCKERS):
        return False
    options = payload.get('options')
    if options is not None:
        if not isinstance(options, list) or not 2 <= len(options) <= 10:
            return False
    return True


def unwrap_qa(text: str) -> str:
    """剥掉 ```markdown / ```json 包装；内容本身是 QA JSON 时取 `answer`。"""
    raw = str(text or '')
    trimmed = raw.strip()
    parsed = _parse_qa_json(trimmed)
    if parsed is not None:
        return str(parsed.get('answer') or '')
    if raw.startswith('```'):
        lines = trimmed.split('\n')
        if len(lines) >= 3 and lines[-1].strip() == '```':
            first = lines[0].strip().lower()
            if first.startswith('```'):
                content = '\n'.join(lines[1:-1])
                parsed = _parse_qa_json(content.strip())
                if parsed is not None:
                    return str(parsed.get('answer') or '')
                return content
    return raw


def _parse_qa_json(text: str) -> dict | None:
    if not text.startswith('{'):
        return None
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    return obj if is_structured_qa(obj) else None


class StreamTranslator:
    """把 CodeArts 的事件序列翻译成 OpenAI chunk。

    `text` 是**累计全文**，所以这里记「已发出去的字符数」，每次只发新增部分；
    增量事件（`delta.content`）则直接追加。两类事件混用也不会重复。
    """

    def __init__(self, model: str) -> None:
        self.model = model
        self.id = f'chatcmpl-{uuid.uuid4().hex[:24]}'
        self.created = int(time.time())
        self.content = ''
        self.reasoning = ''
        self.finish = ''
        self.usage: dict = {}
        self.tool_calls: dict[int, dict] = {}
        self._sent_content = 0
        self._sent_reasoning = 0
        self._role_sent = False

    # ── 对外 ──
    def feed(self, event: str, payload: object) -> list[dict]:
        """应用一个事件，返回本次新增的 chunk。"""
        if not isinstance(payload, dict):
            return []
        if event in ('reasoning', 'thinking', 'onreasoning', 'onthinking'):
            self._apply_reasoning(payload)
            return self._drain()
        if event in ('done', 'end', 'finish'):
            self._raise_if_error(payload)
            finish = _first(payload.get('finish_reason'), payload.get('finish'),
                            payload.get('reason'))
            self.finish = finish or 'stop'
            self._apply_usage(payload)
            return self._drain()
        if event not in ('', 'data', 'onanswer', 'answer', 'delta', 'message',
                         'content'):
            return []
        self._raise_if_error(payload)
        self._apply_usage(payload)
        delta = _delta_of(payload)
        if delta is not None:
            self._apply_delta(delta)
            return self._drain()
        text = _first(payload.get('text'), payload.get('content'))
        if isinstance(payload.get('delta'), str):
            text = str(payload['delta'])
        if text:
            if text in ('[DONE]', 'done') or text.strip() == '[DONE]':
                self.finish = self.finish or 'stop'
                return self._drain()
            # 累计全文：替换而不是追加。
            self.content = text
            return self._drain()
        if 'usage' in payload and not payload.get('choices'):
            # 纯 usage 帧：正文为空。**必须**放在 text 之后——上游常把 usage
            # 挂在最后一个带 text 的帧上，先判 usage 会把那段正文整段丢掉。
            self.finish = self.finish or 'stop'
            return self._drain()
        output = payload.get('output')
        if isinstance(output, list) and output:
            joined = ''.join(
                str(item.get('text') or '') for item in output
                if isinstance(item, dict) and item.get('type') == 'output_text')
            if joined:
                self.content = joined
                return self._drain()
        if not self.content and is_structured_qa(payload):
            self.content = str(payload.get('answer') or '')
            return self._drain()
        return []

    def finish_chunks(self) -> list[dict]:
        """收尾：补 finish_reason 与 usage。"""
        if not self._role_sent:
            self._role_sent = True
            head = [self._chunk({'role': 'assistant'}, None)]
        else:
            head = []
        tail = self._drain()
        return head + tail + [self._chunk({}, self.finish or 'stop')]

    # ── 内部 ──
    def _raise_if_error(self, payload: dict) -> None:
        error = embedded_error(payload)
        if error is not None:
            raise error

    def _apply_usage(self, payload: dict) -> None:
        usage = payload.get('usage')
        if isinstance(usage, dict):
            self.usage.update(usage)
        prompt = _num(payload.get('prompt_tokens'))
        completion = _num(payload.get('completion_tokens'))
        if prompt is not None or completion is not None:
            self.usage.setdefault('prompt_tokens', int(prompt or 0))
            self.usage.setdefault('completion_tokens', int(completion or 0))
        # 上游的 usage 常常只有 prompt / completion 两项；补一个 total 供网关
        # 记账，别让下游各自去猜（有的客户端只读 total_tokens）。
        if 'total_tokens' not in self.usage and (
                'prompt_tokens' in self.usage
                or 'completion_tokens' in self.usage):
            self.usage['total_tokens'] = int(
                _num(self.usage.get('prompt_tokens')) or 0) + int(
                _num(self.usage.get('completion_tokens')) or 0)

    def _apply_reasoning(self, payload: dict) -> None:
        text = _first(payload.get('text'), payload.get('reasoning'),
                      payload.get('content'), payload.get('thinking'))
        if text:
            self.reasoning += text

    def _apply_delta(self, delta: dict) -> None:
        content = delta.get('content')
        if isinstance(content, str) and content:
            self.content += content
        think = delta.get('reasoning_content') or delta.get('reasoning')
        if isinstance(think, str) and think:
            self.reasoning += think
        calls = delta.get('tool_calls')
        if isinstance(calls, list):
            self._accumulate_tool_calls(calls)
        finish = delta.get('finish_reason')
        if finish:
            self.finish = str(finish)

    def _accumulate_tool_calls(self, calls: list) -> None:
        for position, raw in enumerate(calls):
            if not isinstance(raw, dict):
                continue
            try:
                index = int(raw.get('index', position))
            except (TypeError, ValueError):
                index = position
            call = self.tool_calls.setdefault(
                index, {'index': index, 'id': '', 'type': 'function',
                        'function': {'name': '', 'arguments': ''}})
            if raw.get('id'):
                call['id'] = str(raw['id'])
            if raw.get('type'):
                call['type'] = str(raw['type'])
            function = raw.get('function')
            if isinstance(function, dict):
                if function.get('name'):
                    call['function']['name'] += str(function['name'])
                if function.get('arguments'):
                    call['function']['arguments'] += str(function['arguments'])

    def _drain(self) -> list[dict]:
        out: list[dict] = []
        if not self._role_sent:
            self._role_sent = True
            out.append(self._chunk({'role': 'assistant'}, None))
        if len(self.content) > self._sent_content:
            out.append(self._chunk(
                {'content': self.content[self._sent_content:]}, None))
            self._sent_content = len(self.content)
        if len(self.reasoning) > self._sent_reasoning:
            out.append(self._chunk(
                {'reasoning_content': self.reasoning[self._sent_reasoning:]},
                None))
            self._sent_reasoning = len(self.reasoning)
        return out

    def _chunk(self, delta: dict, finish: str | None) -> dict:
        chunk: dict[str, Any] = {
            'id': self.id,
            'object': 'chat.completion.chunk',
            'created': self.created,
            'model': self.model,
            'choices': [{'index': 0, 'delta': delta,
                         'finish_reason': finish}],
        }
        if finish is not None and self.usage:
            chunk['usage'] = self.usage
        return chunk


def _delta_of(payload: dict) -> dict | None:
    if isinstance(payload.get('delta'), dict):
        return payload['delta']
    choices = payload.get('choices')
    if not isinstance(choices, list) or not choices:
        return None
    choice = choices[0] if isinstance(choices[0], dict) else {}
    delta = choice.get('delta')
    return delta if isinstance(delta, dict) else None


def aggregate(events: list[tuple[str, object]], model: str) -> dict:
    """把一串事件聚合成 `chat.completion`（非流式请求用）。"""
    translator = StreamTranslator(model)
    for event, payload in events:
        translator.feed(event, payload)
    content = unwrap_qa(translator.content)
    message: dict[str, Any] = {'role': 'assistant', 'content': content}
    if translator.reasoning:
        message['reasoning_content'] = translator.reasoning
    if translator.tool_calls:
        message['tool_calls'] = [translator.tool_calls[index]
                                 for index in sorted(translator.tool_calls)]
        if not content:
            message['content'] = None
    usage = translator.usage or {'prompt_tokens': 0, 'completion_tokens': 0,
                                 'total_tokens': 0}
    return {
        'id': translator.id,
        'object': 'chat.completion',
        'created': translator.created,
        'model': model,
        'choices': [{'index': 0, 'message': message,
                     'finish_reason': translator.finish or 'stop'}],
        'usage': usage,
    }


# ── 签到 / 积分 ─────────────────────────────────────────────
async def _credit_request(method: str, path: str, payload: object,
                          cred: Credential, *, proxy: str | None,
                          timeout: float) -> object:
    """积分活动接口：snap 根路径（**不带** `/snap-manager`）+ PromptCenter。"""
    url = C.SNAP_HOST + path
    body = (json.dumps(payload, ensure_ascii=False).encode('utf-8')
            if payload is not None else b'')
    headers = _signed(cred, method, url,
                      {'Content-Type': 'application/json',
                       'X-Language': C.LANGUAGE,
                       'Agent-Type': C.AGENT_TYPE_PROMPT_CENTER}, body)
    try:
        response = await _request(method, url, headers=headers, body=body,
                                  proxy=proxy, timeout=timeout)
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc
    data = _json(response)
    error = classify(response.status_code, data, 'CodeArts 积分活动接口失败')
    if error is not None:
        raise error
    body_obj = data if isinstance(data, dict) else {}
    if not _is_success_code(_code_of(body_obj)):
        raise PlatformError(
            f'CodeArts 积分活动接口返回错误：{_message_of(body_obj)}',
            status=400, code='credit_failed', payload=body_obj)
    return body_obj.get('data')


async def fetch_campaigns(cred: Credential, *, proxy: str | None = None,
                          timeout: float = C.CREDITS_TIMEOUT) -> list[dict]:
    data = await _credit_request(
        'GET', f'{C.EP_CREDIT_DELIVERY}?channel={C.CREDIT_CHANNEL}', None,
        cred, proxy=proxy, timeout=timeout)
    items = data.get('items') if isinstance(data, dict) else None
    if items is None:
        raise PlatformError('CodeArts 积分活动响应缺少 data.items',
                            status=502, code='credits_bad_response',
                            payload=data)
    return [item for item in items if isinstance(item, dict)] \
        if isinstance(items, list) else []


async def _claim(cred: Credential, campaign_id: int, *, proxy: str | None,
                 timeout: float) -> None:
    await _credit_request('POST', C.EP_CREDIT_CLAIM, {
        'campaignId': campaign_id,
        'channel': C.CREDIT_CHANNEL,
        'idempotentKey': f'claim_{campaign_id}_{int(time.time() * 1000)}',
    }, cred, proxy=proxy, timeout=timeout)


async def _confirm(cred: Credential, campaign_id: int, *, proxy: str | None,
                   timeout: float) -> None:
    await _credit_request('POST', C.EP_CREDIT_CONFIRM,
                          {'campaignId': campaign_id}, cred, proxy=proxy,
                          timeout=timeout)


def _parse_time(value: object) -> float:
    text = str(value or '').strip()
    if not text:
        return 0.0
    return store.epoch_from_iso(text)


async def checkin(cred: Credential, *, proxy: str | None = None,
                  timeout: float = C.CREDITS_TIMEOUT) -> dict:
    """领取每日签到（1）与新用户（4）积分。

    上游决定资格与每日刷新：`ELIGIBLE` 且 `claimable` 才 claim，`CLAIMED`
    只补 confirm（不得重复领取）。同一账号的领取必须由调用方串行化。
    """
    campaigns = await fetch_campaigns(cred, proxy=proxy, timeout=timeout)
    now = time.time()
    results: list[dict] = []
    total = 0.0
    ok = True
    for campaign in campaigns:
        try:
            campaign_id = int(campaign.get('campaignId'))
        except (TypeError, ValueError):
            continue
        if campaign_id not in C.CREDIT_CAMPAIGNS:
            continue
        unit = str(campaign.get('benefitUnit') or '')
        if unit and unit != 'CREDIT':
            continue
        extra = campaign.get('extra') if isinstance(campaign.get('extra'),
                                                    dict) else {}
        title = str(campaign.get('title') or f'积分活动 {campaign_id}')
        status = str(campaign.get('status') or '').strip().upper()
        result = {'campaign_id': campaign_id, 'title': title, 'ok': True,
                  'status': status, 'credits': 0.0, 'message': ''}
        end = _parse_time(extra.get('endTime'))
        start = _parse_time(extra.get('startTime'))
        if end and now >= end:
            result['message'] = '活动已结束'
            results.append(result)
            continue
        if start and now < start:
            result['message'] = '活动尚未开始'
            results.append(result)
            continue
        if status in ('CONFIRMED', 'CONSUMED'):
            result['message'] = '已领取'
            results.append(result)
            continue
        if status == 'ELIGIBLE':
            if not campaign.get('claimable'):
                result['ok'] = False
                result['message'] = '暂不可领取，稍后重试'
                ok = False
                results.append(result)
                continue
            try:
                await _claim(cred, campaign_id, proxy=proxy, timeout=timeout)
            except PlatformError as exc:
                result['ok'] = False
                result['message'] = exc.message
                ok = False
                results.append(result)
                continue
            result['status'] = 'CLAIMED'
            amount = _num(campaign.get('pendingTotalAmount'))
            if not amount:
                amount = _num(campaign.get('benefitAmount'))
            result['credits'] = float(amount or 0)
        elif status != 'CLAIMED':
            result['message'] = ('未获得活动资格' if not status
                                 else f'未知活动状态 {status}')
            result['ok'] = False
            ok = False
            results.append(result)
            continue
        try:
            await _confirm(cred, campaign_id, proxy=proxy, timeout=timeout)
        except PlatformError as exc:
            result['ok'] = False
            result['message'] = f'已领取，确认失败：{exc.message}'
            ok = False
            results.append(result)
            continue
        result['status'] = 'CONFIRMED'
        if result['credits'] > 0:
            result['message'] = f'领取成功 +{result["credits"]:g} 积分'
        else:
            result['message'] = '已补确认'
        total += float(result['credits'] or 0)
        results.append(result)

    if not results:
        message = '无可领取的签到或新手活动'
    else:
        message = '; '.join(f'{item["title"]}: {item["message"]}'
                            for item in results)
    return {'ok': ok, 'credits': total, 'message': message,
            'campaigns': results}
