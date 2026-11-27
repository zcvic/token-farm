"""WPS 灵犀上游协议层（网页 Cookie）。

边界与其它平台一致：本模块只做「协议翻译」，不含选号、冷却、会话粘性；
那些策略在 `adapter.py`，请求体构造与 SSE 翻译在 `chat.py`。

一次对话是**三步握手**（见 `docs/PROTOCOL.md` §3）：

  1. `POST /api/aioffice/v1/cowork/sessions`   建会话 → `data.session_id`
  2. `POST /api/public/v1/credits/runs`        领 run   → `data.run_id` / `group_id`
  3. `POST /api/public/v1/agent-runtime/chat/completions`  SSE 对话

三步都要带同一份 Cookie：用户 Cookie + 客户端指纹三件套
（`x-kso-app-name` / `x-kso-app-version` / `x-kso-device-id`）。对话请求再加
`X-Client-Type` / `X-Cc-Session-ID` / `X-Cc-Run-ID`。

灵犀有两个基址（`lingxi.wps.cn` 与登录域 `lingxi.kdocs.cn`）：部分账号的会话
只在一侧有效，所有只读接口都按「先主站、再登录域」试。

错误口径（`PROTOCOL.md` §5）：**只有明确的鉴权失败才报 401**，其余 4xx 归 404，
5xx 原样，兜底 502；审核拦截是 400 `audit_blocked`，且只匹配诊断字段，不匹配
模型正文（正文里出现 `sensitive` 这种普通英文词很常见）。
"""
from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator
from urllib.parse import quote

import httpx

from ... import config
from ..base import PlatformError
from . import constants as C
from . import store

__all__ = [
    'probe', 'list_plans', 'balance', 'account_consumed', 'tasks', 'claim',
    'referral_activity', 'referral_code', 'referral_summary',
    'bind_referral', 'referral_data', 'redeem_gift_card',
    'redeem_gift_outcome',
    'open_chat', 'iter_frames', 'parse_balance', 'parse_expenses',
    'session_expenses', 'report_status', 'parse_tasks', 'claim_outcome',
    'classify', 'is_audit', 'beijing_date', 'normalize_user_cookie',
    'forget_run',
]

# ── 错误语义 ────────────────────────────────────────────────
# 审核拦截的标记。**只匹配诊断字段**（见 `_audit_text`），绝不匹配正文：
# 一次普通的 reasoning 里写了 "for the sensitive lines" 曾被误判成审核拦截。
_AUDIT_MARKERS = (
    'auditfailed', 'audit_failed', 'audit failed',
    '内容被审核', '内容审核', 'sensitive',
)
_AUDIT_FIELDS = (
    'code', 'message', 'msg', 'errmsg', 'error_msg', 'err_msg',
    'errorMessage', 'error', 'detail', 'reason',
)
_AUTH_MARKERS = re.compile(r'unauthor|登录|凭据|credential|cookie|sid|expired',
                           re.I)


def _text_of(value: object, limit: int = 400) -> str:
    """任意 JSON → 可诊断的一小段文本。"""
    if isinstance(value, str):
        return value[:limit]
    try:
        text = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return ''
    return text[:limit]


def _audit_text(value: object) -> str:
    """只取诊断字段，供审核判定使用；模型正文不参与。"""
    if isinstance(value, str):
        return value[:400]
    if not isinstance(value, dict):
        return ''
    parts: list[str] = []
    for key in _AUDIT_FIELDS:
        field = value.get(key)
        if field in (None, ''):
            continue
        if isinstance(field, dict):
            for inner_key in _AUDIT_FIELDS:
                inner = field.get(inner_key)
                if inner in (None, '') or isinstance(inner, (dict, list)):
                    continue
                parts.append(str(inner))
            continue
        if isinstance(field, (dict, list)):
            continue
        parts.append(str(field))
    # `data` 可能是本次补全本身，只在它是纯字符串时才看。
    if isinstance(value.get('data'), str) and value.get('data'):
        parts.append(str(value['data']))
    return ' | '.join(parts)[:400]


def is_audit(value: object) -> bool:
    text = _audit_text(value).lower()
    return bool(text) and any(marker in text for marker in _AUDIT_MARKERS)


def classify(status: int, body: object, fallback: str) -> PlatformError:
    """上游失败 → 带 HTTP 语义的 `PlatformError`。

    关键规则：**只有明确的鉴权失败才报 401**，否则会把账号误标成「未登录」
    并从号池里摘掉；其余 4xx 一律 404（模型或参数不对），5xx 原样，兜底 502。
    """
    detail = _text_of(body)
    message = fallback if not detail else f'{fallback}：{detail}'
    if is_audit(body):
        return PlatformError(message, status=400, code='audit_blocked',
                             payload=body)
    if status in (401, 403):
        if not detail or _AUTH_MARKERS.search(detail):
            return PlatformError(message, status=401, code='auth_expired',
                                 payload=body)
        return PlatformError(message, status=404, code='model_not_found',
                             payload=body)
    if status in (400, 404, 422):
        return PlatformError(message, status=404, code='bad_request',
                             payload=body)
    if status >= 500:
        return PlatformError(message, status=status, code='upstream_error',
                             payload=body)
    return PlatformError(message, status=502, code='upstream_error',
                         payload=body)


def _network_error(exc: Exception) -> PlatformError:
    return PlatformError(f'连接 WPS 灵犀失败：{exc}', status=502,
                         code='network_error')


# ── Cookie / 请求头 ─────────────────────────────────────────
def normalize_user_cookie(raw: object) -> str:
    """兼容面板里的几种粘贴：`Cookie:` 前缀、裸 `wps_sid`、整段 Cookie。"""
    return store.normalize_cookie(raw)


def _cookie_value(user_cookie: str, device_id: str) -> str:
    """用户 Cookie + 客户端指纹三件套（`PROTOCOL.md` §1）。"""
    own = str(user_cookie or '').strip().rstrip(';')
    kso = '; '.join((
        f'x-kso-app-name={C.KSO_APP_NAME}',
        f'x-kso-app-version={C.KSO_APP_VERSION}',
        f'x-kso-device-id={device_id}',
    ))
    return f'{own}; {kso}' if own else kso


def _basic_headers(account: dict, base: str) -> dict[str, str]:
    cookie = _cookie_value(str(account.get('cookie') or ''),
                           store.device_of(account))
    return {
        'Cookie': cookie,
        'User-Agent': C.USER_AGENT,
        'Referer': f'{base}/',
        'Accept': 'application/json',
    }


def _agent_headers(account: dict, base: str, session_id: str,
                   run_id: str) -> dict[str, str]:
    headers = {
        **_basic_headers(account, base),
        'Content-Type': 'application/json',
        'X-Client-Type': C.CLIENT_TYPE,
        'X-Client-Lang': C.CLIENT_LANG,
        'X-Cc-Agent-Type': C.CLIENT_TYPE,
        'X-Request-Id': str(uuid.uuid4()),
        'X-Cc-Session-ID': session_id,
        'X-Cc-Run-ID': run_id,
    }
    region = store.cookie_region(str(account.get('cookie') or ''))
    if region:
        headers['X-Cc-Region'] = region
    return headers


def _bases(account: dict) -> list[str]:
    """先试账号上次成功的基址，再试另一个；两个都失败才报错。"""
    cached = str(account.get('base') or '').strip()
    order = [C.BASE_URL, C.KDOCS_BASE]
    if cached in order:
        return [cached, *[b for b in order if b != cached]]
    return order


def _selected_proxy(proxy: str | None) -> str:
    if proxy is None:
        return config.effective_http_proxy()
    return str(proxy).strip()


def _cookie_of(account: dict) -> str:
    return str(account.get('cookie') or '').strip()


def _content_text(content: object) -> str:
    """OpenAI content → 文本（兼容字符串与分段数组）。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ''
    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict) and isinstance(part.get('text'), str):
            parts.append(part['text'])
    return '\n'.join(p for p in parts if p)


def _last_user_text(messages: object) -> str:
    items = messages if isinstance(messages, list) else []
    for message in reversed(items):
        if not isinstance(message, dict):
            continue
        if str(message.get('role') or '').strip().lower() != 'user':
            continue
        text = _content_text(message.get('content')).strip()
        if text:
            return text[:4000]
    return ''


# ── 通用 JSON 请求 ──────────────────────────────────────────
def _parse_json(text: str) -> object:
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


async def _json_request(account: dict, method: str, path: str, *,
                        body: object = None, proxy: str | None,
                        timeout: float,
                        headers: dict[str, str] | None = None) -> object:
    cookie = _cookie_of(account)
    if not cookie:
        raise PlatformError('WPS 灵犀账号缺少 Cookie', status=401,
                            code='auth_expired')
    url = path
    try:
        async with config.http_client(
                httpx.Timeout(timeout, connect=10),
                proxy=_selected_proxy(proxy), lane='lingxi') as client:
            request = client.build_request(
                method, url,
                headers={**_basic_headers(account, path.split('/api/')[0]),
                         **(headers or {})},
                content=None if body is None
                else json.dumps(body, ensure_ascii=False).encode('utf-8'))
            response = await client.send(request)
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc
    text = response.text
    payload = _parse_json(text)
    if response.status_code >= 400:
        raise classify(response.status_code, payload,
                       f'灵犀请求失败（HTTP {response.status_code}）')
    return payload


async def _request_on_bases(account: dict, method: str, path: str, *,
                           body: object = None, proxy: str | None,
                           timeout: float,
                           headers: dict[str, str] | None = None
                           ) -> tuple[object, str]:
    """按基址顺序请求；401 立即抛出，其余错误换下一个基址再试。"""
    last: PlatformError | None = None
    for base in _bases(account):
        try:
            payload = await _json_request(
                account, method, f'{base}{path}', body=body, proxy=proxy,
                timeout=timeout, headers=headers)
            return payload, base
        except PlatformError as exc:
            last = exc
            if exc.status == 401:
                raise
    raise last or PlatformError('无法连接 WPS 灵犀', status=502,
                                code='network_error')


# ── 探活 / 目录 / 余额 / 任务 ───────────────────────────────
async def probe(account: dict, *, proxy: str | None = None,
                timeout: float = C.WEB_TIMEOUT) -> dict:
    """Cookie 探活：`GET /account/profile` 200 且不是 `UserUnauthorized`。

    返回 `{state, message, name, base}`，`state` ∈ `ok` / `invalid` / `unknown`。
    **不抛异常**：探活是「看一眼」，不该把账号页打挂。
    """
    if not _cookie_of(account):
        return {'state': 'invalid', 'message': '缺少 Cookie', 'name': '',
                'base': ''}
    last_message = ''
    for base in _bases(account):
        try:
            payload = await _json_request(
                account, 'GET', f'{base}{C.PROFILE_PATH}', proxy=proxy,
                timeout=timeout)
        except PlatformError as exc:
            if exc.status == 401:
                return {'state': 'invalid', 'message': exc.message, 'name': '',
                        'base': base}
            last_message = exc.message
            continue
        if isinstance(payload, dict) and payload.get('result') == 'UserUnauthorized':
            return {'state': 'invalid', 'message': 'Cookie 无效或已过期',
                    'name': '', 'base': base}
        data = payload.get('data') if isinstance(payload, dict) else None
        name = ''
        if isinstance(data, dict):
            for key in ('name', 'nickname', 'user_name', 'userName'):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    name = value.strip()
                    break
        return {'state': 'ok', 'message': '', 'name': name, 'base': base}
    return {'state': 'unknown', 'message': last_message or '无法连接灵犀',
            'name': '', 'base': ''}


async def list_plans(account: dict, *, proxy: str | None = None,
                     timeout: float = C.WEB_TIMEOUT) -> list[dict]:
    """在线模型目录；失败返回空列表（调用方回退内置快照）。"""
    from . import catalog  # noqa: PLC0415 —— catalog 反向依赖 constants 即可
    if not _cookie_of(account):
        return []
    for base in _bases(account):
        try:
            payload = await _json_request(
                account, 'GET', f'{base}{C.PLANS_PATH}', proxy=proxy,
                timeout=timeout)
        except PlatformError:
            continue
        models = catalog.parse_plans(payload)
        if models:
            return models
    return []


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float('inf'), float('-inf')) or number < 0:
        return None
    return number


def _signed(value: object) -> float | None:
    """带符号的有限数（流水金额用；`_finite` 会把消耗的负数当成坏值丢掉）。"""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float('inf'), float('-inf')):
        return None
    return number


def _parse_upstream_time(value: object) -> int | None:
    """上游时间串 → epoch 毫秒。

    `"YYYY-MM-DD HH:mm:ss"` 是**北京时间且不带时区**；直接用本机时区会算错，
    所以显式补 `+08:00`。ISO 带时区的原样解析。
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if re.match(r'^\d{4}-\d{2}-\d{2}T.*(?:Z|[+-]\d{2}:\d{2})$', text):
        try:
            return int(datetime.fromisoformat(text.replace('Z', '+00:00'))
                       .timestamp() * 1000)
        except ValueError:
            return None
    date_only = re.match(r'^(\d{4})-(\d{2})-(\d{2})$', text)
    if date_only:
        text = text + ' 00:00:00'
    with_time = re.match(r'^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}(?::\d{2})?)', text)
    if not with_time:
        return None
    time_part = with_time.group(2)
    if len(time_part) == 5:
        time_part += ':00'
    try:
        parsed = datetime.fromisoformat(
            f'{with_time.group(1)}T{time_part}+08:00')
    except ValueError:
        return None
    return int(parsed.timestamp() * 1000)


def parse_balance(payload: object, now: float | None = None) -> dict | None:
    """`/credits/balance` → 统一余额结构；认不出来返回 None（永不抛）。"""
    if not isinstance(payload, dict):
        return None
    data = payload.get('data')
    if not isinstance(data, dict):
        return None
    total = _finite(data.get('total_balance'))
    if total is None:
        total = _finite(data.get('balance'))
    if total is None:
        return None

    summary = data.get('bonus_summary')
    summary = summary if isinstance(summary, dict) else {}
    total_value = _finite(summary.get('total_value')) or 0.0
    consumed = _finite(summary.get('total_consumed'))
    raw_batches = data.get('bonus_credits')
    if not isinstance(raw_batches, list):
        raw_batches = summary.get('items')
    if not isinstance(raw_batches, list):
        raw_batches = []

    batches: list[dict] = []
    for raw in raw_batches:
        if not isinstance(raw, dict):
            continue
        balance = _finite(raw.get('balance'))
        if balance is None:
            continue
        value = _finite(raw.get('value'))
        value = balance if value is None else value
        used = _finite(raw.get('consumed'))
        used = max(0.0, value - balance) if used is None else used
        expire_time = raw.get('expire_time')
        expire_time = expire_time.strip() if isinstance(expire_time, str) else ''
        batch = {
            'balance': balance,
            'value': value,
            'consumed': used,
            'expire_time': expire_time,
        }
        expire_at = _parse_upstream_time(expire_time)
        if expire_at is not None:
            batch['expire_at'] = expire_at
        batches.append(batch)

    now_s = time.time() if now is None else float(now)
    now_ms = int(now_s * 1000)
    amount = 0.0
    earliest = ''
    earliest_at = None
    for batch in batches:
        at = batch.get('expire_at')
        if at is None or at <= now_ms:
            continue
        if at - now_ms > 15 * 24 * 3600 * 1000:
            continue
        amount += float(batch['balance'])
        if earliest_at is None or at < earliest_at:
            earliest_at = at
            earliest = str(batch.get('expire_time') or '')

    out: dict[str, Any] = {
        'total': total,
        'total_value': total_value,
        'batches': batches,
        'updated_at': now_s,
    }
    if consumed is not None:
        out['consumed'] = consumed
    if amount > 0 and earliest_at is not None:
        out['expiring'] = {'amount': amount, 'earliest': earliest,
                           'earliest_at': earliest_at // 1000}
    return out


async def balance(account: dict, *, proxy: str | None = None,
                  timeout: float = C.WEB_TIMEOUT) -> dict:
    """智点余额（含赠送批次与 15 天内的到期额度）。登录失效抛 401。"""
    payload, _ = await _request_on_bases(
        account, 'GET', C.BALANCE_PATH, proxy=proxy, timeout=timeout)
    summary = parse_balance(payload)
    if summary is None:
        raise PlatformError('取不到灵犀余额', status=502,
                            code='credits_unavailable', payload=payload)
    return summary


async def account_consumed(account: dict, *, proxy: str | None = None,
                           timeout: float = C.WEB_TIMEOUT) -> float | None:
    """账户累计消耗（各批次 `consumed` 之和；读不到返回 None）。

    `delta` 口径的基线：只累加「已消耗」，新的发放 / 签到不会让读数跳变。
    """
    payload, _ = await _request_on_bases(
        account, 'GET', C.BALANCE_PATH, proxy=proxy, timeout=timeout)
    summary = parse_balance(payload)
    if summary is None:
        return None
    used = [float(batch['consumed']) for batch in summary.get('batches') or []
            if isinstance(batch.get('consumed'), (int, float))
            and not isinstance(batch.get('consumed'), bool)]
    if used:
        return sum(used)
    value = summary.get('consumed')
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def parse_expenses(payload: object) -> list[dict]:
    """`/credits/transaction/expense` → 归一流水；认不出返回空数组（永不抛）。"""
    if not isinstance(payload, dict):
        return []
    data = payload.get('data')
    if not isinstance(data, dict):
        return []
    raw_rows = data.get('list')
    if not isinstance(raw_rows, list):
        return []
    out: list[dict] = []
    for raw in raw_rows:
        if not isinstance(raw, dict):
            continue
        amount = _signed(raw.get('amount'))
        if amount is None:
            continue
        # 上游的获得类 `type` 是 `topup`（不是 income），消耗类才是 expense；
        # 统一按**金额符号**归一，调用方不必记这套约定。
        kind = 'expense' if amount < 0 else 'income'
        out.append({
            'id': str(raw.get('id') or ''),
            'type': kind,
            'amount': amount,
            'group_id': str(raw.get('group_id') or ''),
            'session_id': str(raw.get('session_id') or ''),
            'title': str(raw.get('task_name') or raw.get('remark') or ''),
            'source': str(raw.get('source') or ''),
            'time': str(raw.get('time') or raw.get('create_time') or ''),
        })
    return out


async def session_expenses(account: dict, session_id: str, *,
                           proxy: str | None = None,
                           timeout: float = C.WEB_TIMEOUT) -> list[dict]:
    """某个会话的消耗明细（`detail` 口径）。失败返回空数组，不打扰主链路。"""
    sid = str(session_id or '').strip()
    if not sid:
        return []
    query = f'session_id={quote(sid, safe="")}&cursor=0&limit=50'
    try:
        payload, _ = await _request_on_bases(
            account, 'GET', f'{C.EXPENSE_PATH}?{query}', proxy=proxy,
            timeout=timeout)
    except PlatformError:
        return []
    return parse_expenses(payload)


async def report_status(account: dict, run: dict, status: str, *,
                        reason: str = '', proxy: str | None = None,
                        timeout: float = 8) -> bool:
    """上报 run 生命周期。`running → completed` 是上游结算 / 扣费的唯一触发点。

    失败返回 False（调用方只记账，不该因为上报失败把一次正常对话判错）。
    """
    sid = str(run.get('session_id') or '').strip()
    gid = str(run.get('group_id') or run.get('run_id') or '').strip()
    if not sid or not gid:
        return False
    body: dict[str, Any] = {
        'group_id': gid,
        'status': status,
        'run_token': gid,
    }
    if status == 'running':
        body['lease_seconds'] = 90
    elif reason:
        body['terminal_reason'] = reason
    path = C.EXECUTION_STATUS_PATH.format(session_id=quote(sid, safe=''))
    try:
        await _request_on_bases(account, 'PUT', path, body=body, proxy=proxy,
                                timeout=timeout)
    except PlatformError:
        return False
    return True


_TASK_TITLES = {
    'daily_check_in': '每日签到',
    'try_expert_mode': '体验灵犀专家模式',
    'try_group_chat': '体验群聊功能',
    'use_lingxi_client': '使用灵犀客户端',
}


def _checkin_days(raw: object) -> list[dict]:
    """签到日历：上游两种形状都认。

    · 桌面客户端实际下发 `{day: 1..7, status: available|claimed}`，并用
      `extra.today_day` 标今天；
    · 早期抓包见过 `{date: 'YYYY-MM-DD', status}`。
    两种字段都保留，签到日期的选择逻辑可以继续按 `date` 读。
    """
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        date = item.get('date')
        date = date.strip() if isinstance(date, str) else ''
        day = _finite(item.get('day'))
        if not date and day is None:
            continue
        status = item.get('status')
        out.append({'date': date,
                    'day': int(day) if day is not None else None,
                    'status': status.strip() if isinstance(status, str) else ''})
    return out


def parse_tasks(payload: object) -> list[dict]:
    """`/tasks` → 扁平任务列表；形状认不出来返回空数组（永不抛）。"""
    if not isinstance(payload, dict):
        return []
    data = payload.get('data')
    if not isinstance(data, dict):
        return []
    raw_tasks = data.get('tasks')
    if not isinstance(raw_tasks, list):
        return []
    out: list[dict] = []
    for raw in raw_tasks:
        if not isinstance(raw, dict):
            continue
        key = raw.get('task_key')
        key = key.strip() if isinstance(key, str) else ''
        if not key:
            continue
        extra = raw.get('extra') if isinstance(raw.get('extra'), dict) else {}
        streak = _finite(extra.get('consecutive_days'))
        today_day = _finite(extra.get('today_day'))
        title = ''
        for field in ('title', 'name'):
            value = raw.get(field)
            if isinstance(value, str) and value.strip():
                title = value.strip()
                break
        description = ''
        for field in ('description', 'desc', 'sub_title'):
            value = raw.get(field)
            if isinstance(value, str) and value.strip():
                description = value.strip()
                break
        action = raw.get('action')
        action = action.strip() if isinstance(action, str) else ''
        out.append({
            'key': key,
            'type': str(raw.get('type') or 'one_time').strip(),
            'title': title or _TASK_TITLES.get(key, key),
            'description': description,
            'status': str(raw.get('status') or '').strip(),
            'reward': _finite(raw.get('reward_amount')) or 0.0,
            'action': action,
            'streak': int(streak) if streak else None,
            'today_day': int(today_day) if today_day is not None else None,
            'today_reward': _finite(extra.get('today_reward')),
            'days': _checkin_days(extra.get('check_in_days')),
        })
    return out


async def tasks(account: dict, *, proxy: str | None = None,
                timeout: float = C.WEB_TIMEOUT) -> list[dict]:
    """任务中心列表；登录失效抛 401。"""
    payload, _ = await _request_on_bases(
        account, 'GET', C.TASKS_PATH, proxy=proxy, timeout=timeout)
    return parse_tasks(payload)


def claim_outcome(payload: object, fallback_reward: float = 0.0) -> dict:
    """领取响应 → `{ok, reward, already, message}`。

    上游成功时 `result == "ok"`；失败时 `result` 是别的值、`hint` 是给用户的话。
    **`result` 缺失按成功**（HTTP 200 已是成功信号）。「已领过」不是错误，
    报成失败会让用户以为签到坏了。
    """
    if not isinstance(payload, dict):
        return {'ok': True, 'reward': fallback_reward, 'already': False,
                'message': ''}
    result = payload.get('result')
    hint = payload.get('hint')
    hint = hint.strip() if isinstance(hint, str) else ''
    if isinstance(result, str) and result not in ('', 'ok', 'Success'):
        already = bool(re.search(r'已(?:经)?(?:领取|签到)|already', hint, re.I)
                       or re.search(r'done|claimed', result, re.I))
        return {
            'ok': already,
            'reward': 0.0,
            'already': already,
            'message': hint or (f'领取失败：{result}' if not already else ''),
        }
    data = payload.get('data') if isinstance(payload.get('data'), dict) else {}
    extra = data.get('extra') if isinstance(data.get('extra'), dict) else {}
    reward = _finite(extra.get('today_reward'))
    if reward is None:
        reward = _finite(data.get('reward_amount'))
    if reward is None:
        reward = _finite(payload.get('reward_amount'))
    if reward is None:
        reward = fallback_reward
    return {'ok': True, 'reward': reward, 'already': False, 'message': ''}


async def claim(account: dict, key: str, date: str = '', *,
                proxy: str | None = None, timeout: float = C.WEB_TIMEOUT) -> dict:
    """领取任务。**不重试、不换基址**：领取带幂等标记。"""
    if not str(key or '').strip():
        raise PlatformError('缺少 task_key', status=400, code='bad_request')
    path = f'{C.TASKS_PATH}/{key.strip()}/claim'
    if date:
        path += f'?date={date}'
    last: PlatformError | None = None
    for base in _bases(account):
        try:
            payload = await _json_request(
                account, 'POST', f'{base}{path}', body={}, proxy=proxy,
                timeout=timeout)
        except PlatformError as exc:
            last = exc
            if exc.status == 401:
                raise
            continue
        return claim_outcome(payload)
    raise last or PlatformError('领取任务失败', status=502,
                                code='upstream_error')


# ── 邀请有礼 / 兑换码 ────────────────────────────────────────
def referral_data(payload: object) -> dict:
    """公开活动接口的 `{v7code, result, data}` 信封 → `data`。

    活动页的请求层对 `protocol: public-api` 会做 `v7code === 0 &&
    result === 'ok'` 校验后返回 `data`；这里在代理 / 网关改写后也要能读，
    所以「是信封就拆、不是就原样用」。
    """
    if not isinstance(payload, dict):
        return {}
    if 'v7code' in payload or 'result' in payload:
        data = payload.get('data')
        return data if isinstance(data, dict) else {}
    return payload


def _referral_message(payload: object, data: dict) -> str:
    for field in ('message', 'msg', 'hint'):
        value = data.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if isinstance(payload, dict):
        for field in ('msg', 'message', 'hint'):
            value = payload.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ''


async def referral_activity(account: dict, *, proxy: str | None = None,
                            timeout: float = C.WEB_TIMEOUT) -> dict:
    """邀请活动规则：`campaign_status` / 奖励额度 / 邀请上限。"""
    payload, _ = await _request_on_bases(
        account, 'GET', C.REFERRAL_ACTIVITY_PATH, proxy=proxy,
        timeout=timeout, headers=C.REFERRAL_HEADERS)
    return referral_data(payload)


async def referral_code(account: dict, *, proxy: str | None = None,
                        timeout: float = C.WEB_TIMEOUT) -> dict:
    """我的邀请码：`{referral_code, campaign_id, ...}`。"""
    payload, _ = await _request_on_bases(
        account, 'GET', C.REFERRAL_CODE_PATH, proxy=proxy,
        timeout=timeout, headers=C.REFERRAL_HEADERS)
    return referral_data(payload)


async def referral_summary(account: dict, *, proxy: str | None = None,
                           timeout: float = C.WEB_TIMEOUT) -> dict:
    """我的邀请进度 / 兑换状态：`redeemed` / `success_count` / `total_reward`。"""
    payload, _ = await _request_on_bases(
        account, 'GET', C.REFERRAL_SUMMARY_PATH, proxy=proxy,
        timeout=timeout, headers=C.REFERRAL_HEADERS)
    return referral_data(payload)


async def bind_referral(account: dict, code: str, *,
                        proxy: str | None = None,
                        timeout: float = C.WEB_TIMEOUT) -> dict:
    """绑定邀请码（邀请人侧的口径：`referral_code` + `source=manual`）。

    返回 `{bound, message/reason, invitee_reward, reward_status,
    reward_expire_at, ...}`；业务失败通常仍是 HTTP 200，由 `bound` 判定。
    """
    referral = str(code or '').strip()
    if not referral:
        raise PlatformError('请填写邀请码', status=400,
                            code='missing_invite_code')
    payload, _ = await _request_on_bases(
        account, 'POST', C.REFERRAL_BIND_PATH,
        body={'referral_code': referral, 'source': 'manual'},
        proxy=proxy, timeout=timeout, headers=C.REFERRAL_HEADERS)
    data = referral_data(payload)
    if data.get('bound') is None and isinstance(payload, dict):
        # 信封里 `result != 'ok'` 时没有 data，但 `msg` 仍然可读。
        return {'bound': False, **data,
                'message': _referral_message(payload, data)}
    return data


def redeem_gift_outcome(payload: object) -> dict:
    """兑换码响应 → `{ok, reward, message, data}`。

    客户端 UI 的判定是 `result === 'ok' && data.granted`；`granted` 缺失
    （旧版本 / 网关裁剪）时按成功，避免把已到账的兑换报成失败。
    """
    if not isinstance(payload, dict):
        return {'ok': True, 'reward': 0.0, 'message': '', 'data': {}}
    data = payload.get('data') if isinstance(payload.get('data'), dict) else {}
    result = payload.get('result')
    ok = result in (None, '', 'ok', 'Success')
    granted = data.get('granted')
    return {
        'ok': ok and granted is not False,
        'reward': _finite(data.get('face_value')) or 0.0,
        'message': _referral_message(payload, data),
        'data': data,
    }


async def redeem_gift_card(account: dict, code: str, *,
                           proxy: str | None = None,
                           timeout: float = C.WEB_TIMEOUT) -> dict:
    """任务中心「兑换权益」：`POST /tasks/gift_card/redeem` body `{code}`。"""
    value = str(code or '').strip()
    if not value:
        raise PlatformError('请填写兑换码', status=400,
                            code='missing_redeem_code')
    payload, _ = await _request_on_bases(
        account, 'POST', C.GIFT_REDEEM_PATH, body={'code': value},
        proxy=proxy, timeout=timeout)
    return redeem_gift_outcome(payload)


def beijing_date(now: float | None = None) -> str:
    """按北京时间取 `YYYY-MM-DD`（签到是上游按北京时间判定的）。"""
    stamp = time.time() if now is None else float(now)
    shifted = datetime.fromtimestamp(stamp, tz=timezone.utc) \
        + timedelta(hours=8)
    return shifted.strftime('%Y-%m-%d')


# ── 三步握手 ────────────────────────────────────────────────
async def _create_session(account: dict, base: str, *, proxy: str | None,
                          timeout: float) -> str:
    payload = await _json_request(
        account, 'POST', f'{base}{C.SESSIONS_PATH}',
        body={
            'session_type': 'cowork_client',
            'agent_version': 2,
            'env': {
                'device_id': store.device_of(account),
                'device_name': store.device_name_of(account),
                'work_space_dir': C.WORK_SPACE_DIR,
            },
        },
        proxy=proxy, timeout=timeout)
    data = payload.get('data') if isinstance(payload, dict) else None
    session_id = data.get('session_id') if isinstance(data, dict) else None
    if not isinstance(session_id, str) or not session_id.strip():
        raise PlatformError('灵犀未返回 session_id', status=502,
                            code='bad_upstream', payload=payload)
    return session_id.strip()


async def _create_run(account: dict, base: str, session_id: str, model: str,
                      title: str, *, proxy: str | None,
                      timeout: float) -> dict:
    """领一个 run；返回 `{run_id, group_id}`。"""
    payload = await _json_request(
        account, 'POST', f'{base}{C.RUNS_PATH}',
        body={
            'client_request_id': str(int(time.time() * 1000)),
            'session_id': session_id,
            'model_provider': 'lingxi',
            'model_id': model,
            'agent_input': {
                'text': title,
                'files': [],
                'file_ids': [],
                'expect_model': model,
                'refs': [],
                'permission_mode': 'default',
                'current_mode': 'agent',
                'browser_plugin_enabled': False,
            },
        },
        proxy=proxy, timeout=timeout)
    data = payload.get('data') if isinstance(payload, dict) else None
    run_id = data.get('run_id') if isinstance(data, dict) else None
    if not isinstance(run_id, str) or not run_id.strip():
        raise PlatformError('灵犀未返回 run_id', status=502,
                            code='bad_upstream', payload=payload)
    group_id = data.get('group_id') if isinstance(data, dict) else None
    return {
        'run_id': run_id.strip(),
        'group_id': group_id.strip() if isinstance(group_id, str) else '',
    }


# run 缓存（不计量模式）：同一账号 + 设备 + 模型在 TTL 内复用同一次「会话 + run」。
# 每轮都重建会话会让上游不断新开 agent 会话（旧会话留在服务端），也丢掉
# prompt cache 亲和；参考实现同样缓存 1 小时（`RUN_TTL_MS`）。
_runs: dict[str, dict] = {}
# 会话缓存（计量模式）：`delta` 复用会话、但仍每轮新建 run（run 才是计费单元）；
# `detail` 每轮新建会话，保证 `transaction/expense` 只返回本次请求的流水。
_sessions: dict[str, tuple[float, str]] = {}

# run 失效的文案特征（只在 404 时参与判定）：重建一次再试。
_STALE_RUN = re.compile(r'run|会话|session|过期|expired|not\s*found|不存在',
                        re.I)


def _run_key(account: dict, model: str) -> str:
    return '|'.join((str(account.get('uid') or ''),
                     store.device_of(account), str(model or '')))


def forget_run(account: dict, model: str | None = None) -> None:
    """丢掉某个账号（某个模型）的 run / 会话缓存；导入新 Cookie / 换设备时用。"""
    if model is not None:
        key = _run_key(account, C.upstream_model_id(model))
        _runs.pop(key, None)
        _sessions.pop(key, None)
        return
    prefix = f'{str(account.get("uid") or "")}|'
    for table in (_runs, _sessions):
        for key in [k for k in table if k.startswith(prefix)]:
            table.pop(key, None)


def _cached_session(account: dict, model: str) -> str:
    """TTL 内的会话号（先看旧 run 缓存，再看计量模式的会话缓存）。"""
    key = _run_key(account, model)
    hit = _runs.get(key)
    if isinstance(hit, dict) and time.time() - float(hit.get('at') or 0) \
            < C.RUN_TTL_SECONDS:
        return str(hit.get('session_id') or '')
    cached = _sessions.get(key)
    if cached and time.time() - cached[0] < C.RUN_TTL_SECONDS:
        return cached[1]
    return ''


async def _ensure_run(account: dict, model: str, title: str, *,
                      proxy: str | None, timeout: float) -> dict:
    """取缓存的 run；过期 / 没有就按两个基址新建一次。"""
    key = _run_key(account, model)
    hit = _runs.get(key)
    if isinstance(hit, dict) and time.time() - float(hit.get('at') or 0) \
            < C.RUN_TTL_SECONDS:
        return hit

    last: PlatformError | None = None
    for base in _bases(account):
        try:
            session_id = await _create_session(
                account, base, proxy=proxy, timeout=timeout)
            created = await _create_run(
                account, base, session_id, model, title,
                proxy=proxy, timeout=timeout)
        except PlatformError as exc:
            last = exc
            if exc.status == 401:
                raise
            continue
        run = {
            'session_id': session_id,
            'run_id': created['run_id'],
            'group_id': created['group_id'],
            'base': base,
            'at': time.time(),
        }
        _runs[key] = run
        return run
    raise last or PlatformError('无法建立灵犀会话', status=502,
                                code='upstream_error')


async def _meter_run(account: dict, model: str, title: str, *,
                     proxy: str | None, timeout: float,
                     reuse_session: bool) -> dict:
    """计量模式：每轮新建 run（计费单元），会话按口径决定是否复用。

    `reuse_session=True`（delta）复用 TTL 内的会话，只新建 run；
    `False`（detail）连会话一起新建，`session_id` 即这次请求的归属凭据。
    """
    key = _run_key(account, model)
    session_id = _cached_session(account, model) if reuse_session else ''
    last: PlatformError | None = None
    for base in _bases(account):
        try:
            if not session_id:
                session_id = await _create_session(
                    account, base, proxy=proxy, timeout=timeout)
            created = await _create_run(
                account, base, session_id, model, title,
                proxy=proxy, timeout=timeout)
        except PlatformError as exc:
            last = exc
            session_id = ''  # 换基址后不能拿上一个基址创建的会话
            if exc.status == 401:
                raise
            continue
        run = {
            'session_id': session_id,
            'run_id': created['run_id'],
            'group_id': created['group_id'],
            'base': base,
            'at': time.time(),
        }
        _sessions[key] = (run['at'], session_id)
        return run
    raise last or PlatformError('无法建立灵犀会话', status=502,
                                code='upstream_error')


async def _send_chat(account: dict, run: dict, body: dict, *,
                     proxy: str | None, idle_timeout: float,
                     connect_timeout: float) -> httpx.Response:
    """用给定的 run 发起对话请求，返回**未读取**的上游响应。"""
    base = str(run.get('base') or C.BASE_URL)
    try:
        async with config.http_client(
                httpx.Timeout(idle_timeout, connect=connect_timeout),
                proxy=_selected_proxy(proxy), lane='lingxi') as client:
            request = client.build_request(
                'POST', f'{base}{C.CHAT_PATH}',
                headers=_agent_headers(
                    account, base, str(run.get('session_id') or ''),
                    str(run.get('run_id') or '')),
                content=json.dumps(body, ensure_ascii=False).encode('utf-8'))
            response = await client.send(request, stream=True)
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc

    if response.status_code >= 400:
        text = (await response.aread()).decode('utf-8', errors='replace')
        await response.aclose()
        raise classify(response.status_code, _parse_json(text),
                       f'灵犀对话失败（HTTP {response.status_code}）')

    ctype = str(response.headers.get('content-type') or '').lower()
    if 'text/event-stream' not in ctype:
        text = (await response.aread()).decode('utf-8', errors='replace')
        await response.aclose()
        payload = _parse_json(text)
        raise classify(502, payload, '灵犀对话接口未返回 SSE')
    return response


async def open_chat(account: dict, body: dict, model: str, *,
                    proxy: str | None = None,
                    idle_timeout: float = C.CHAT_IDLE_TIMEOUT,
                    connect_timeout: float = 15.0,
                    meter: dict | None = None) -> httpx.Response:
    """三步握手后发起对话，返回**未读取**的上游响应（调用方负责关闭）。

    正常情况下上游回 `text/event-stream`；Cookie 失效 / 参数错误时可能回
    HTTP 200 + JSON（错误体）。非 SSE 响应在这里一次性读完并按错误归类，
    避免把一段 JSON 当 SSE 解析。

    `meter` 非空时进入「每请求计费」：每轮新建 run（`detail` 还会新建会话），
    并在发对话前上报 `running`；会话 / run 标识回填进 `meter`，供收尾查流水。
    `meter` 为 None 时维持旧行为（run 缓存 1 小时、不上报生命周期、不扣费）。

    run 失效（404 且文案指向会话 / run 过期）时**丢缓存重试一次**：这是
    参考实现的行为，否则一次服务端回收会话就得等 TTL 到期才能再对话。
    """
    if not _cookie_of(account):
        raise PlatformError('WPS 灵犀账号缺少 Cookie', status=401,
                            code='auth_expired')
    model = C.upstream_model_id(model)
    title = _last_user_text(body.get('messages'))

    last: PlatformError | None = None
    for attempt in range(2):
        if meter is None:
            run = await _ensure_run(account, model, title, proxy=proxy,
                                    timeout=C.WEB_TIMEOUT)
        else:
            run = await _meter_run(
                account, model, title, proxy=proxy, timeout=C.WEB_TIMEOUT,
                reuse_session=str(meter.get('mode') or '') == 'delta')
            meter.update({key: run[key] for key in
                          ('session_id', 'run_id', 'group_id', 'base')})
            # running 必须在对话前上报：completed 是结算触发点，缺了 running
            # 这条 run 在上游眼里不成立。
            await report_status(account, run, 'running', proxy=proxy)
        try:
            return await _send_chat(
                account, run, body, proxy=proxy,
                idle_timeout=idle_timeout, connect_timeout=connect_timeout)
        except PlatformError as exc:
            last = exc
            if meter is not None:
                # 这一轮没跑起来也要收尾，否则 run 会一直挂在进行中。
                await report_status(account, run, 'completed', reason='failed',
                                    proxy=proxy)
            stale = exc.status == 404 and bool(_STALE_RUN.search(exc.message))
            if attempt == 0 and stale:
                forget_run(account, model)
                continue
            raise
    raise last or PlatformError('灵犀对话失败', status=502,
                                code='upstream_error')


async def iter_frames(response: httpx.Response) -> AsyncIterator[dict]:
    """逐帧解析灵犀 SSE。

    上游正文在 `choices[].text`、没有 `[DONE]`、靠 `finish_reason` 收尾；
    这里只负责把每个 `data:` 行解析成 dict，业务判断交给 `chat.py`。
    坏 JSON 跳过（跳过一帧比中断整条流好），网络错误统一翻成 `PlatformError`。
    """
    try:
        async for line in response.aiter_lines():
            text = line.strip()
            if not text or text.startswith(':') or text.startswith('event:'):
                continue
            if text.startswith('data:'):
                text = text[5:].strip()
            if not text or text == '[DONE]':
                continue
            try:
                frame = json.loads(text)
            except ValueError:
                continue
            if isinstance(frame, dict):
                yield frame
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc