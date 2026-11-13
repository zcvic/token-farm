"""Qoder 内置网关的 HTTP 客户端。

`embedded.py` 把 qoder2api-hub 挂在回环地址的随机端口上，这里按它的接口形状
调用（与上游独立部署时完全一致）：

  · OpenAI 兼容推理：`POST /v1/chat/completions`、`GET /v1/models`
  · 面板管理接口：`/accounts*`

这里做两件事：

  1. **管理面**用同步 `httpx.Client`（适配器的账号方法都是同步的）；
  2. **推理面**用 `config.http_client(..., proxy='')` 的异步客户端，网关在
     本机回环，不能被面板的出口代理接管。

凭据规则（与上游一致）：

  · 推理 / 模型目录用网关的 API Key（每个进程随机生成）；
  · 账号管理接口（`/accounts*`）属于**面板路由**，必须带 `X-Panel-Token`，
    与 API Key 无关。内置网关与适配器同进程，token 由 `embedded.panel_token()`
    直接签发，不需要密码登录。

面板 token 与内置网关的 Key 都只存在内存里，不落盘、不下发到浏览器。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from ... import config
from ...services.session_affinity import affinity_key, header_key
from ..base import PlatformError
from . import embedded

_CHAT_PATH = '/v1/chat/completions'
_MODELS_PATH = '/v1/models'


def _error_payload(resp: httpx.Response) -> tuple[str, str]:
    """从网关错误响应里取 (message, code)，拿不到时给一句兜底。"""
    text = ''
    try:
        text = resp.text
    except Exception:  # noqa: BLE001 —— 读取失败不影响主错误
        text = ''
    message = ''
    code = ''
    try:
        data = json.loads(text)
    except Exception:  # noqa: BLE001
        data = None
    if isinstance(data, dict):
        err = data.get('error')
        if isinstance(err, dict):
            message = str(err.get('message') or err.get('code') or '')
            # 网关的 `code` 是 HTTP 状态码（数字），`type` 才是错误类别；
            # 数字当 code 回给下游没有信息量，优先取 type。
            raw_code = str(err.get('code') or '')
            code = raw_code if raw_code and not raw_code.isdigit() \
                else str(err.get('type') or '')
        elif isinstance(err, str):
            message = err
        message = message or str(data.get('message') or data.get('msg') or '')
        code = code or str(data.get('code') or '')
    if not message:
        message = (text or resp.reason_phrase or '').strip()[:300]
    return message or f'HTTP {resp.status_code}', code


def _platform_error(resp: httpx.Response, *, prefix: str = 'Qoder 网关') -> PlatformError:
    message, code = _error_payload(resp)
    status = resp.status_code if 400 <= resp.status_code < 600 else 502
    # 5xx 统一成 502：这是「上游不可用」，不是 token-farm 自己 500。
    if status >= 500:
        status = 502
    retry_after = None
    try:
        raw = resp.headers.get('retry-after')
        if raw:
            retry_after = max(1, int(float(raw)))
    except Exception:  # noqa: BLE001
        retry_after = None
    return PlatformError(f'{prefix}：{message}', status=status,
                         code=code or 'qoder_upstream_error',
                         retry_after=retry_after)


class QoderClient:
    """内置网关的管理 API + OpenAI 推理 API 薄封装。"""

    def __init__(self, *, timeout: int | None = None,
                 manage_timeout: int | None = None) -> None:
        self.timeout = int(timeout if timeout is not None else config.QODER_TIMEOUT)
        self.manage_timeout = int(
            manage_timeout if manage_timeout is not None else config.QODER_MANAGE_TIMEOUT)
        self._http: httpx.Client | None = None
        self._http_base = ''

    # ── 内置网关 ────────────────────────────────────────────
    def _gateway(self) -> embedded.Runtime:
        """取（必要时启动）内置网关；参数只在内存里。

        每次调用都走 `embedded.runtime()`（它自己缓存结果），这样网关被
        重启（测试 / 进程回收）后能拿到新端口。
        """
        return embedded.runtime()

    @property
    def base(self) -> str:
        return self._gateway().base

    @property
    def api_key(self) -> str:
        return self._gateway().api_key

    # ── 底层 ────────────────────────────────────────────────
    def _client(self) -> httpx.Client:
        base = self.base
        if self._http is None or self._http_base != base:
            # 网关重启（测试或进程回收）会换端口：旧连接池必须换掉。
            self.close()
            # trust_env=False：网关在 127.0.0.1，绝不能被系统/环境代理接管。
            self._http = httpx.Client(
                base_url=base,
                timeout=httpx.Timeout(self.manage_timeout, connect=5),
                trust_env=False)
            self._http_base = base
        return self._http

    def close(self) -> None:
        if self._http is not None:
            try:
                self._http.close()
            finally:
                self._http = None
                self._http_base = ''

    def _headers(self, *, with_panel: bool = True) -> dict[str, str]:
        headers: dict[str, str] = {'Accept': 'application/json'}
        api_key = self.api_key
        if api_key:
            headers['Authorization'] = f'Bearer {api_key}'
        if with_panel:
            headers['X-Panel-Token'] = self.ensure_panel_token()
        return headers

    def ensure_panel_token(self) -> str:
        """管理接口需要的面板会话 token。

        内置网关与适配器在同一个进程：token 由 `PanelSessions.create()` 直接
        签发（签名密钥持久化在 `accounts/settings.json`），不需要知道面板口令。
        """
        return embedded.panel_token()

    def _request(self, method: str, path: str, *, json_body: Any = None,
                 params: dict | None = None) -> dict:
        headers = self._headers()
        try:
            resp = self._client().request(method, path, json=json_body,
                                          params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise PlatformError(
                f'连接 Qoder 网关失败（{self.base}）：{exc}', status=502,
                code='qoder_unreachable') from exc
        if resp.status_code >= 400:
            raise _platform_error(resp)
        if not resp.content:
            return {}
        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise PlatformError(f'Qoder 网关返回了非 JSON 响应：{exc}', status=502,
                                code='qoder_bad_response') from exc
        return data if isinstance(data, dict) else {'data': data}

    # ── 账号 ────────────────────────────────────────────────
    def list_accounts(self, realm: str = 'all') -> list[dict]:
        data = self._request('GET', '/accounts', params={'realm': realm})
        accounts = data.get('accounts')
        return [a for a in accounts if isinstance(a, dict)] \
            if isinstance(accounts, list) else []

    def import_account(self, payload: dict) -> dict:
        """导入一份账号文档，返回网关里的账号对象。

        支持三种入口：
          · `{pat: "pt-..."}` 或 `{token: "pt-..."}` → PAT 导入；
          · 普通账号对象（accessToken / refreshToken / auth ...）→ 文档导入；
          · `{account: {...}}` 包裹形态。
        """
        row = payload.get('account') if isinstance(payload.get('account'), dict) else payload
        if not isinstance(row, dict):
            raise PlatformError('账号必须是 JSON 对象', status=400,
                                code='invalid_account')
        realm = str(row.get('realm') or payload.get('realm') or 'cn').strip().lower()
        if realm not in ('cn', 'intl'):
            realm = 'cn'
        pat = str(row.get('pat') or '').strip()
        if not pat:
            candidate = str(row.get('token') or row.get('accessToken')
                            or row.get('access_token') or '').strip()
            if candidate.startswith('pt-'):
                pat = candidate
        if pat:
            data = self._request('POST', '/accounts/import/pat',
                                 json_body={'pat': pat, 'realm': realm})
            imported = data.get('imported') or []
            if not imported:
                raise PlatformError('PAT 导入没有返回账号', status=502,
                                    code='qoder_import_failed')
            return imported[0]

        body: dict[str, Any] = {'data': row, 'overwrite': True, 'realm': realm}
        data = self._request('POST', '/accounts/import', json_body=body)
        report = data.get('result') if isinstance(data.get('result'), dict) else {}
        uid = str(row.get('uid') or row.get('user_id') or row.get('userId') or '')
        if not uid:
            for key in ('added', 'updated'):
                values = report.get(key) or []
                if isinstance(values, list) and values:
                    uid = str(values[0] or '')
                    break
        accounts = [a for a in (data.get('accounts') or []) if isinstance(a, dict)]
        if uid:
            for account in accounts:
                if str(account.get('uid') or '') == uid:
                    return account
        # 导入被跳过（重复 / 格式不合法）时给出网关的原话。
        for bucket in ('invalid', 'skipped'):
            rows = report.get(bucket) or []
            if isinstance(rows, list) and rows:
                first = rows[0]
                reason = first.get('reason') if isinstance(first, dict) else str(first)
                raise PlatformError(f'Qoder 导入失败：{reason}', status=400,
                                    code='qoder_import_failed')
        raise PlatformError('Qoder 导入成功但网关没有返回该账号', status=502,
                            code='qoder_import_failed')

    def delete_account(self, uid: str) -> bool:
        data = self._request('POST', '/accounts/delete', json_body={'uid': uid})
        return bool(data.get('deleted'))

    def refresh_account(self, uid: str) -> dict:
        data = self._request('POST', '/accounts/refresh', json_body={'uid': uid})
        results = [r for r in (data.get('results') or []) if isinstance(r, dict)]
        hit = next((r for r in results if str(r.get('uid') or '') == uid), None)
        if hit is None:
            skipped = [r for r in (data.get('skipped') or []) if isinstance(r, dict)]
            reason = next((r.get('reason') for r in skipped
                           if str(r.get('uid') or '') == uid), '')
            if reason == 'disabled':
                raise PlatformError('账号已被停用，网关跳过刷新', status=400,
                                    code='qoder_refresh_skipped')
            if reason == 'no_credential':
                raise PlatformError('账号没有可刷新的凭据', status=400,
                                    code='qoder_refresh_skipped')
            raise PlatformError('Qoder 网关没有返回该账号的刷新结果', status=502,
                                code='qoder_refresh_failed')
        if not hit.get('ok'):
            raise PlatformError(f"刷新失败：{hit.get('error') or '未知原因'}",
                                status=502, code='qoder_refresh_failed')
        account = self._find_account(uid)
        if account is None:
            raise PlatformError('刷新成功但网关里找不到该账号', status=502,
                                code='qoder_refresh_failed')
        return account

    def refresh_credits(self, uid: str) -> dict:
        """强刷单个账号的积分快照（上游 `POST /accounts/credits/fetch`）。

        余额快照平时由上游的用量驱动刷新器维护；新导入的账号还没被使用过时
        没有快照，面板会显示「尚无积分快照」。账号页的「刷新」按钮走这里，
        让用户点一次就能拿到实时余额。
        """
        data = self._request('POST', '/accounts/credits/fetch',
                             json_body={'uid': uid})
        results = [r for r in (data.get('results') or [])
                   if isinstance(r, dict)]
        hit = next((r for r in results if str(r.get('uid') or '') == uid), None)
        if hit is not None and not hit.get('ok'):
            raise PlatformError(
                f"积分查询失败：{hit.get('error') or '未知原因'}",
                status=502, code='qoder_credits_failed')
        accounts = [a for a in (data.get('accounts') or [])
                    if isinstance(a, dict)]
        account = next((a for a in accounts
                        if str(a.get('uid') or '') == uid), None)
        if account is None:
            account = self._find_account(uid)
        if account is None:
            raise PlatformError('积分刷新成功但网关里找不到该账号', status=502,
                                code='qoder_credits_failed')
        return account

    def set_enabled(self, uid: str, enabled: bool) -> dict:
        data = self._request('POST', '/accounts/set',
                             json_body={'uid': uid, 'enabled': bool(enabled)})
        account = data.get('account')
        return account if isinstance(account, dict) else {}

    def set_weight(self, uid: str, weight: float) -> dict:
        """设置账号权重（0 = 恢复默认 1.0）；非法值由网关返回 400。"""
        data = self._request('POST', '/accounts/weight',
                             json_body={'uid': uid, 'weight': weight})
        account = data.get('account')
        return account if isinstance(account, dict) else {}

    def _find_account(self, uid: str) -> dict | None:
        for account in self.list_accounts('all'):
            if str(account.get('uid') or '') == uid:
                return account
        return None

    # ── 任务 ────────────────────────────────────────────────
    def checkin(self, uid: str) -> dict:
        data = self._request('POST', '/accounts/checkin', json_body={'uid': uid})
        results = [r for r in (data.get('results') or []) if isinstance(r, dict)]
        return next((r for r in results if str(r.get('uid') or '') == uid), {})

    # ── 登录 ────────────────────────────────────────────────
    def login_start(self, realm: str) -> dict:
        return self._request('POST', '/accounts/login/start',
                             json_body={'realm': realm, 'platform': 'CLI'})

    def login_poll(self, state: str) -> dict:
        return self._request('GET', '/accounts/login/poll',
                             params={'state': state})

    def login_cancel(self, state: str) -> bool:
        data = self._request('POST', '/accounts/login/cancel',
                             json_body={'state': state})
        return bool(data.get('cancelled'))

    def import_desktop(self) -> dict:
        """扫描本机 Qoder 客户端（App + CLI）并导入全部有效凭据。"""
        return self._request('POST', '/accounts/import/desktop',
                             json_body={'all': True})

    # ── 模型 ────────────────────────────────────────────────
    def list_models(self) -> list[dict]:
        data = self._request('GET', _MODELS_PATH,
                             params={'merge_realms': 1})
        models = data.get('data')
        return [m for m in models if isinstance(m, dict)] \
            if isinstance(models, list) else []

    # ── 推理 ────────────────────────────────────────────────
    def _inference_headers(self, realm: str | None,
                           session: str = '') -> dict[str, str]:
        headers = self._headers()
        headers['Content-Type'] = 'application/json'
        if realm in ('cn', 'intl'):
            headers['X-Realm'] = realm
        if session:
            # 内置网关按这个头做会话粘性（`extract_session_key` 认得
            # X-Session-Id）；不传就只能退回厂商自己的消息前缀亲和。
            headers['X-Session-Id'] = session
        return headers

    async def open_chat(self, body: dict, realm: str | None = None, *,
                        timeout: float | None = None) -> httpx.Response:
        """发起一次流式请求，返回**尚未读取**的响应对象。

        调用方负责 `aiter_lines()` / `aread()` 并最终 `aclose()`。这里不
        `async with`，因为响应体要在适配器的生成器里跨多个事件循环 tick 使用。

        `timeout` 是本次请求的读超时；None = 用客户端默认值（环境变量 / 平台
        设置没配时的兜底）。平台设置里可调，所以不能只在构造时读一次。
        """
        # 首次调用可能触发上游模块导入（~0.6s）：放到线程里，别让一次冷启动
        # 卡住整个网关的事件循环。
        await asyncio.to_thread(embedded.runtime)
        client = config.http_client(
            self.timeout if timeout is None else timeout, connect=10, proxy='')
        url = f'{self.base}{_CHAT_PATH}'
        # 真实选号在网关进程里，只把 body 透传过去的话 `prompt_cache_key`
        # 会被厂商丢掉（它只认自己的会话 id / 消息前缀）：显式转成
        # X-Session-Id，同一会话才会落到同一账号、命中前缀缓存。
        session = header_key(affinity_key(body))
        headers = self._inference_headers(realm, session)

        async def send() -> httpx.Response:
            request = client.build_request('POST', url, json=body, headers=headers)
            return await client.send(request, stream=True)

        try:
            resp = await send()
        except httpx.HTTPError as exc:
            raise PlatformError(
                f'连接 Qoder 网关失败（{self.base}）：{exc}', status=502,
                code='qoder_unreachable') from exc
        if resp.status_code >= 400:
            try:
                await resp.aread()
            finally:
                await resp.aclose()
            raise _platform_error(resp)
        return resp

    async def post_chat(self, body: dict, realm: str | None = None, *,
                        timeout: float | None = None) -> tuple[dict, str]:
        """非流式对话；返回 `(响应体, 网关实际使用的 realm)`。"""
        resp = await self.open_chat(body, realm, timeout=timeout)
        actual_realm = str(resp.headers.get('x-qoder-realm') or '')
        try:
            raw = await resp.aread()
        finally:
            await resp.aclose()
        try:
            data = json.loads(raw.decode('utf-8'))
        except Exception as exc:  # noqa: BLE001
            raise PlatformError(f'Qoder 返回了非 JSON 响应：{exc}', status=502,
                                code='qoder_bad_response') from exc
        if not isinstance(data, dict):
            raise PlatformError('Qoder 返回了非对象响应', status=502,
                                code='qoder_bad_response')
        if data.get('error'):
            message = data['error']
            if isinstance(message, dict):
                message = message.get('message') or message.get('code') or '未知错误'
            raise PlatformError(f'Qoder 上游错误：{message}', status=502,
                                code='qoder_upstream_error', payload=data)
        return data, actual_realm
