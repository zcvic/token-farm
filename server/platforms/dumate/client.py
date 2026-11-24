"""DuMate 上游协议层（网页 cookie + Bearer token）。

边界与其它平台一致：本模块只做「协议翻译」，不含选号、冷却、会话粘性；
那些策略在 `adapter.py`。

两条链路：

  · **网页接口**（`www.dumate.cn` / `console.bce.baidu.com`）—— cookie 鉴权，
    负责换 token、用户信息、签到、积分；
  · **模型网关**（`dumate-svc.baidu.com/gateway/apis/v1`）—— Bearer token 鉴权，
    标准 OpenAI Chat Completions，流式响应原样透传。

代理口径与项目其余部分一致：`adapter` 先用 `config.resolve_account_proxy`
把账号线路解析成 URL，本模块只接受「已解析」的地址：`None` = 用全局代理，
`''` = 强制直连（`trust_env=False`，不读环境代理）。
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from ... import config
from ..base import PlatformError
from . import constants as C

__all__ = [
    'fetch_token', 'fetch_user_info', 'login_bonus_info', 'claim_login_bonus',
    'quota_overview', 'usage_records', 'tasks', 'complete_task',
    'draw_status', 'draw', 'claim_prize', 'invitation_code', 'invited_count',
    'redeem_invitation_code', 'chat', 'open_chat',
]


# ── 通用 ────────────────────────────────────────────────────
def _selected_proxy(proxy: str | None) -> str:
    if proxy is None:
        return config.effective_http_proxy()
    return str(proxy).strip()


def _web_headers(cookie: str) -> dict[str, str]:
    return {
        'Cookie': cookie,
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'zh-CN,zh;q=0.9',
        'X-Dumate-Client-Type': C.CLIENT_TYPE,
        'Referer': C.WEB_BASE + '/app',
        'Origin': C.WEB_BASE,
        'User-Agent': C.UA,
    }


def _gateway_headers(token: str) -> dict[str, str]:
    return {
        'Authorization': f'Bearer {token}',
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'X-Dumate-Client-Type': C.CLIENT_TYPE,
        'X-Dumate-Account-Id': C.ACCOUNT_ID,
        'User-Agent': C.UA,
    }


def pick_result(payload: object) -> object:
    """上游把结果包在 `result` / `data` / 顶层三种位置，按顺序取。"""
    if not isinstance(payload, dict):
        return payload
    for key in ('result', 'data'):
        if key in payload:
            return payload[key]
    return payload


def _expired(payload: object, status: int) -> bool:
    if status == 401:
        return True
    if not isinstance(payload, dict):
        return False
    try:
        code = int(payload.get('code'))
    except (TypeError, ValueError):
        code = 0
    return code in C.EXPIRED_CODES


def _error_text(payload: object, fallback: str) -> str:
    if isinstance(payload, dict):
        for key in ('message', 'msg', 'error_message', 'desc'):
            value = payload.get(key)
            if value:
                return str(value)
        error = payload.get('error')
        if isinstance(error, dict) and error.get('message'):
            return str(error['message'])
        if isinstance(error, str):
            return error
    return fallback


def _biz_failed(payload: object) -> bool:
    """HTTP 200 不代表成功：网页接口用 `code != 0` / `success === false`。

    与参考实现同口径：显式 `success:true` 时即便 `code` 不是 0 也算成功——
    百度不同网关的 code 约定不完全一致，不能把「非 0」一律当失败。
    """
    if not isinstance(payload, dict):
        return False
    if payload.get('success') is False:
        return True
    raw = payload.get('code')
    if raw is None or raw == '':
        return False
    try:
        code = int(raw)
    except (TypeError, ValueError):
        return False
    return code != 0 and payload.get('success') is not True


def _raise_web(payload: object, status: int, what: str) -> None:
    if _expired(payload, status):
        raise PlatformError(
            _error_text(payload, 'DuMate 登录态已失效，请重新登录（扫码或粘贴 Cookie）'),
            status=401, code='login_expired', payload=payload)
    if status >= 500:
        raise PlatformError(
            f'{what} 上游 HTTP {status}', status=502, code='upstream_error',
            payload=payload)
    if status >= 400:
        raise PlatformError(
            _error_text(payload, f'{what} 上游 HTTP {status}'), status=status,
            code='web_request_failed', payload=payload)
    if _biz_failed(payload):
        raise PlatformError(
            _error_text(payload, f'{what} 业务失败'), status=502,
            code='web_biz_failed', payload=payload)


async def _web_json(method: str, path: str, cookie: str, *,
                    proxy: str | None = None,
                    timeout: float = C.API_TIMEOUT,
                    body: dict | None = None) -> dict:
    """网页接口的一次 JSON 调用（固定主域）。"""
    async with config.http_client(
            httpx.Timeout(timeout, connect=10),
            proxy=_selected_proxy(proxy), lane='dumate') as client:
        response = await client.request(
            method, C.WEB_BASE + path, headers=_web_headers(cookie),
            json=body if body is not None else None)
    text = response.text
    try:
        payload = json.loads(text) if text else {}
    except ValueError as exc:
        raise PlatformError(
            f'{path} 返回非 JSON（HTTP {response.status_code}）',
            status=502, code='bad_upstream',
            payload=text[:300]) from exc
    _raise_web(payload, response.status_code, path)
    return payload if isinstance(payload, dict) else {}


# ── token ───────────────────────────────────────────────────
async def fetch_token(cookie: str, *, proxy: str | None = None,
                      timeout: float = C.TOKEN_TIMEOUT) -> dict:
    """用网页 cookie 换模型网关 token。

    候选域只在「这一域本身不通」时切换：网络错误 / 5xx / 2xx 非 JSON。
    401/403 是凭证问题，换域不会变好，还会把同一份 cookie 多报给一个域。
    """
    cookie = str(cookie or '').strip()
    if not cookie:
        raise PlatformError('DuMate Cookie 为空', status=401,
                            code='login_expired')

    last: tuple[int, object, str] | None = None
    for host in C.WEB_HOSTS:
        try:
            async with config.http_client(
                    httpx.Timeout(timeout, connect=8),
                    proxy=_selected_proxy(proxy), lane='dumate') as client:
                response = await client.get(
                    host + C.TOKEN_PATH, headers=_web_headers(cookie))
        except httpx.HTTPError as exc:
            last = (0, None, str(exc))
            continue

        text = response.text
        payload: object
        try:
            payload = json.loads(text) if text else {}
        except ValueError:
            payload = None

        if response.status_code == 200 and isinstance(payload, dict):
            result = pick_result(payload)
            token = str((result or {}).get('token') or '') \
                if isinstance(result, dict) else ''
            if token:
                return {
                    'token': token,
                    'expired_at': str((result or {}).get('expired_at')
                                      or (result or {}).get('expiredAt') or ''),
                }
            # 200 但不是预期形状：登录态失效 / 被跳登录页。
            if _expired(payload, response.status_code):
                raise PlatformError(
                    _error_text(payload, 'DuMate 登录态已失效，请重新登录（扫码或粘贴 Cookie）'),
                    status=401, code='login_expired', payload=payload)
            last = (response.status_code, payload, 'token 字段缺失')
            continue

        last = (response.status_code, payload, text[:200])
        # 凭证 / 请求问题：不换域。
        if response.status_code not in (0, 500, 502, 503, 504):
            break

    status, payload, detail = last or (0, None, '')
    if _expired(payload, status):
        raise PlatformError(
            _error_text(payload, 'DuMate 登录态已失效，请重新登录（扫码或粘贴 Cookie）'),
            status=401, code='login_expired', payload=payload)
    raise PlatformError(
        f'换取 DuMate token 失败：{_error_text(payload, detail or "网络错误")}',
        status=502, code='token_failed', payload=payload)


# ── 账号信息 / 签到 / 积分 ──────────────────────────────────
async def fetch_user_info(cookie: str, *, proxy: str | None = None,
                          timeout: float = C.API_TIMEOUT) -> dict:
    payload = await _web_json('GET', C.USER_INFO_PATH, cookie,
                              proxy=proxy, timeout=timeout)
    result = pick_result(payload)
    data = result if isinstance(result, dict) else {}
    return {
        'uid': str(data.get('uid') or data.get('userId') or ''),
        'nickname': str(data.get('nickname') or data.get('displayName')
                        or data.get('name') or ''),
        'raw': data,
    }


async def login_bonus_info(cookie: str, *, proxy: str | None = None,
                           timeout: float = C.API_TIMEOUT) -> dict:
    payload = await _web_json('GET', C.LOGIN_BONUS_INFO_PATH, cookie,
                              proxy=proxy, timeout=timeout)
    result = pick_result(payload)
    data = result if isinstance(result, dict) else {}
    return {
        'has_issued': bool(data.get('hasIssued', data.get('has_issued'))),
        'total_times': data.get('totalTimes', data.get('total_times')),
        'total_points': data.get('totalPoints', data.get('total_points')),
        'sign_in_days': data.get('signInDays', data.get('sign_in_days')) or [],
        'month_points': data.get('monthPoints', data.get('month_points')),
        'raw': data,
    }


async def claim_login_bonus(cookie: str, *, proxy: str | None = None,
                            timeout: float = C.API_TIMEOUT) -> dict:
    payload = await _web_json('POST', C.LOGIN_BONUS_PATH, cookie,
                              proxy=proxy, timeout=timeout)
    return {'result': pick_result(payload), 'raw': payload}


async def quota_overview(cookie: str, *, proxy: str | None = None,
                         timeout: float = C.API_TIMEOUT) -> dict:
    payload = await _web_json('GET', C.QUOTA_OVERVIEW_PATH + C.QUOTA_QUERY,
                              cookie, proxy=proxy, timeout=timeout)
    result = pick_result(payload)
    data = result if isinstance(result, dict) else {}

    def num(value: object) -> float | None:
        if value in (None, ''):
            return None
        try:
            return float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    total = num(data.get('totalPoints'))
    used = num(data.get('usedPoints'))
    left = max(0.0, total - used) if total is not None and used is not None else None
    packages = []
    for kind, key in (('subscription', 'subscription'),
                      ('incremental', 'incremental')):
        entries = data.get(key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            package_total = num(entry.get('totalPoints'))
            package_used = num(entry.get('usedPoints'))
            packages.append({
                'kind': kind,
                'package_type': str(entry.get('packageType') or ''),
                'source': str(entry.get('source') or ''),
                'total': package_total,
                'used': package_used,
                'left': (max(0.0, package_total - package_used)
                         if package_total is not None
                         and package_used is not None else None),
                'expire_at': (int(entry.get('expireDate') or 0) * 1000
                              or None),
                'points_expire_at': (int(entry.get('pointsExpireDate') or 0)
                                     * 1000 or None),
                # 包的**发放时刻**（上游 startDate 是秒，本仓统一毫秒）。登录奖励
                # 每天 00:00 自动发放，签到动作只是「确认」；要认出今天那笔包的
                # source='login_bonus' 并用 granted_at 判重，靠的就是这个字段。
                'granted_at': (int(entry.get('startDate') or 0) * 1000
                               or None),
                'product': str(entry.get('product') or ''),
                'status': str(entry.get('status') or ''),
            })

    throttle = data.get('modelThrottleInfo')
    return {
        'total': total,
        'used': used,
        'left': left,
        'balance_known': left is not None,
        'subscribed': bool(data.get('isSubscribed')),
        'throttled': bool(isinstance(throttle, dict)
                          and throttle.get('throttled')),
        'packages': packages,
        'raw': data,
    }


async def usage_records(cookie: str, *, start_at: int | None = None,
                        end_at: int | None = None, page: int | None = None,
                        limit: int | None = None, proxy: str | None = None,
                        timeout: float = C.API_TIMEOUT) -> dict:
    """积分消耗记录（`/points/records/usage`，对话扣费明细）。

    上游**必须**带 `startAt` / `endAt`，缺一个就报「参数错误:StartAt」。返回的
    `consumedPoints` 是窗口内总扣费（字符串），`list` 是逐条记录：
    `createdAt`（秒）、`pointsChange`（带负号的字符串）、`conversationName`。
    计费规则在上游，本地无法从 token 数推算，所以只如实转述。
    """
    query: list[str] = []
    if start_at:
        query.append(f'startAt={int(start_at)}')
    if end_at:
        query.append(f'endAt={int(end_at)}')
    if page:
        query.append(f'page={int(page)}')
    if limit:
        query.append(f'limit={int(limit)}')
    suffix = ('?' + '&'.join(query)) if query else ''
    payload = await _web_json('GET', C.USAGE_RECORDS_PATH + suffix, cookie,
                              proxy=proxy, timeout=timeout)
    data = unwrap_result(payload)
    rows: list[dict] = []
    for item in (data.get('list') or []):
        if not isinstance(item, dict):
            continue
        rows.append({
            'at': _optional_int(item.get('createdAt')
                                if item.get('createdAt') is not None
                                else item.get('created_at')),
            # pointsChange 是负值（扣费），保留符号：调用方按正负着色。
            'points': _optional_float(item.get('pointsChange')
                                      if item.get('pointsChange') is not None
                                      else item.get('points_change')),
            'conversation': str(item.get('conversationName')
                                or item.get('conversation_name') or ''),
            'package_id': str(item.get('packageId')
                              or item.get('package_id') or ''),
        })
    return {
        'consumed_points': _optional_float(
            data.get('consumedPoints')
            if data.get('consumedPoints') is not None
            else data.get('consumed_points')) or 0.0,
        'total_count': _int(data.get('totalCount')
                            if data.get('totalCount') is not None
                            else data.get('total_count')),
        'list': rows,
    }


# ── 成长任务 / 抽奖 / 邀请码 ────────────────────────────────
def unwrap_result(payload: object) -> dict:
    """网页接口的结果可能再套一层 `result`（bundle 的 unwrap 同口径）。"""
    data: object = pick_result(payload)
    for _ in range(3):
        if isinstance(data, dict) and 'code' in data and 'result' in data:
            data = data['result']
            continue
        break
    return data if isinstance(data, dict) else {}


def _int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _optional_int(value: object) -> int | None:
    if value is None or value == '':
        return None
    return _int(value)


def _optional_float(value: object) -> float | None:
    """数字字段可能是带符号的字符串（`pointsChange: "-5.58"`）；认不出返回 None。"""
    if isinstance(value, bool) or value is None or value == '':
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _normalize_task(task: dict, group: dict) -> dict:
    """上游任务字段 → 面板统一形状；完成口径是 completed_count >= repeat_count。"""
    repeat = max(1, _int(task.get('repeat_count'), 1))
    completed = max(0, _int(task.get('completed_count')))
    task_type = str(task.get('task_type') or '')
    return {
        'task_id': str(task.get('task_id') or ''),
        'task_type': task_type,
        'title': str(task.get('title') or ''),
        'sub_title': str(task.get('sub_title') or ''),
        'query': str(task.get('query') or ''),
        'suite_name': str(task.get('suite_name') or ''),
        'jump_link': str(task.get('jump_link') or ''),
        'reward_count': _int(task.get('reward_count')),
        'reward_points': _int(task.get('reward_points')),
        'repeat_count': repeat,
        'completed_count': completed,
        'done': completed >= repeat,
        'auto': task_type in C.AUTO_TASK_TYPES,
        'display_terminal': (task.get('display_terminal')
                             if isinstance(task.get('display_terminal'), list)
                             else []),
        'group_code': str(group.get('group_code') or ''),
        'group_name': str(group.get('group_name') or ''),
        'raw': task,
    }


async def tasks(cookie: str, *, proxy: str | None = None,
                timeout: float = C.API_TIMEOUT) -> dict:
    """成长计划任务列表：`GET /activity/growth-plan/tasks?device=web`。"""
    payload = await _web_json('GET', C.TASKS_PATH + C.TASKS_QUERY, cookie,
                              proxy=proxy, timeout=timeout)
    data = unwrap_result(payload)
    groups: list[dict] = []
    flat: list[dict] = []
    raw_groups = data.get('groups')
    if isinstance(raw_groups, list):
        for group in raw_groups:
            if not isinstance(group, dict):
                continue
            items = group.get('tasks')
            normalized = [
                _normalize_task(item, group)
                for item in (items if isinstance(items, list) else [])
                if isinstance(item, dict) and str(item.get('task_id') or '')
            ]
            groups.append({
                'group_code': str(group.get('group_code') or ''),
                'group_name': str(group.get('group_name') or ''),
                'tasks': normalized,
                'raw': group,
            })
            flat.extend(normalized)
    return {'groups': groups, 'tasks': flat, 'raw': data}


async def complete_task(cookie: str, task_id: str, *, proxy: str | None = None,
                        timeout: float = C.API_TIMEOUT) -> dict:
    """上报任务完成：`POST /activity/growth-plan/task/complete` body `{task_id}`。

    业务失败（例如 410121「该任务次数已发放」）由 `_web_json` 抛
    `PlatformError`，调用方从 `exc.payload` 里取业务码决定是否按「已完成」处理。
    网页客户端还会带 `source: web`，上游据此记活动来源。
    """
    payload = await _web_json(
        'POST', C.TASK_COMPLETE_PATH, cookie, proxy=proxy, timeout=timeout,
        body={'task_id': str(task_id or ''), 'source': C.ACTIVITY_SOURCE})
    return {'result': unwrap_result(payload), 'raw': payload}


async def draw_status(cookie: str, *, proxy: str | None = None,
                      timeout: float = C.API_TIMEOUT) -> dict:
    """抽奖状态：剩余次数 / 奖品池 / 我的奖品 / 中奖记录。"""
    payload = await _web_json(
        'GET', f'{C.DRAW_STATUS_PATH}?source={C.ACTIVITY_SOURCE}', cookie,
        proxy=proxy, timeout=timeout)
    data = unwrap_result(payload)
    return {
        # 缺字段 / null 与「确实是 0 次」要分开：前者不该把抽奖按钮锁死。
        'remaining_draws': _optional_int(
            data.get('remaining_draws', data.get('remainingDraws'))),
        'prizes': data.get('prizes') if isinstance(data.get('prizes'), list)
        else [],
        'my_prizes': (data.get('my_prizes')
                      if isinstance(data.get('my_prizes'), list) else []),
        'winning_records': (data.get('winning_records')
                            if isinstance(data.get('winning_records'), list)
                            else []),
        'raw': data,
    }


async def draw(cookie: str, request_id: str, *, proxy: str | None = None,
               timeout: float = C.API_TIMEOUT) -> dict:
    """抽一次奖：`POST /activity/growth-plan/draw`。

    幂等键（客户端用 `crypto.randomUUID()` 生成），必须每次新生成；复用同一个
    值会让第二次被上游当成重复请求。网页 bundle 把键放在 body 的
    `x-bce-request-id`（并带 `source`）；参考实现实测 `request_id` 也认，这里
    两个都带上，兼容两套网关。
    """
    key = str(request_id or '')
    payload = await _web_json(
        'POST', C.DRAW_PATH, cookie, proxy=proxy, timeout=timeout,
        body={'x-bce-request-id': key, 'request_id': key,
              'source': C.ACTIVITY_SOURCE})
    data = unwrap_result(payload)
    # 网页端把奖品字段直接放在抽奖结果顶层（`prize_name` / `prize_type` /
    # `prize_value`），不是嵌套的 `prize` 对象。
    prize: dict[str, Any] = {}
    for key_name in ('prize_name', 'prize_type', 'prize_value'):
        if data.get(key_name) not in (None, ''):
            prize[key_name] = data[key_name]
    nested = data.get('prize')
    if not prize and isinstance(nested, dict):
        prize = dict(nested)
    return {
        'draw_record_id': str(data.get('draw_record_id')
                              or data.get('drawRecordId') or ''),
        'remaining_draws': _optional_int(
            data.get('remaining_draws', data.get('remainingDraws'))),
        'prize': prize or None,
        'prize_name': str(prize.get('prize_name') or prize.get('name') or ''),
        'prize_type': str(prize.get('prize_type') or ''),
        'draw_time': str(data.get('draw_time') or ''),
        'raw': data,
        'payload': payload,
    }


async def claim_prize(cookie: str, draw_record_id: str, *,
                      contact: str = '', proxy: str | None = None,
                      timeout: float = C.API_TIMEOUT) -> dict:
    """领取实物奖品：`POST /activity/growth-plan/prize/claim`。

    积分 / 会员类奖品抽到即自动到账，**不要**再调这个接口；对已 SUCCESS 的
    记录调用会报「参数错误:DrawRecordID」。只有实物奖（`prize_type=PHYSICAL`）
    需要收货信息。
    """
    body: dict[str, Any] = {'draw_record_id': str(draw_record_id or '')}
    if str(contact or '').strip():
        body['contact'] = str(contact).strip()
    payload = await _web_json(
        'POST', C.PRIZE_CLAIM_PATH, cookie, proxy=proxy, timeout=timeout,
        body=body)
    return {'result': unwrap_result(payload), 'raw': payload}


async def invitation_code(cookie: str, *, proxy: str | None = None,
                          timeout: float = C.API_TIMEOUT,
                          activity_id: str = '') -> dict:
    """自己名下的邀请码：`GET /invitation_code/get?activityId=`。"""
    path = C.INVITATION_CODE_GET_PATH
    if activity_id:
        path += '?activityId=' + activity_id
    payload = await _web_json('GET', path, cookie, proxy=proxy, timeout=timeout)
    data = unwrap_result(payload)
    return {
        'invitation_code': str(data.get('invitationCode') or ''),
        'type': str(data.get('type') or ''),
        'created_at': _int(data.get('createdAt')),
        'invite_message': str(data.get('inviteMessage') or ''),
        'raw': data,
    }


async def invited_count(cookie: str, *, proxy: str | None = None,
                        timeout: float = C.API_TIMEOUT,
                        activity_id: str = '') -> dict:
    """邀请统计：`GET /invitation_code/invited/count?activityId=`。"""
    path = C.INVITATION_CODE_INVITED_COUNT_PATH
    if activity_id:
        path += '?activityId=' + activity_id
    payload = await _web_json('GET', path, cookie, proxy=proxy, timeout=timeout)
    data = unwrap_result(payload)
    return {
        'invited_count': _int(data.get('invitedCount')),
        'total_points': _int(data.get('totalPoints')),
        'raw': data,
    }


async def redeem_invitation_code(cookie: str, code: str, *,
                                 proxy: str | None = None,
                                 timeout: float = C.API_TIMEOUT,
                                 activity_id: str = '') -> dict:
    """填写别人的邀请码：`POST /invitation_code/redeem`。

    body 与网页 bundle 一致：`{invitationCode, timezone, source, activityId?}`。
    邀请码区分大小写，调用方只做 `strip()`，不改大小写。
    """
    body: dict[str, Any] = {
        'invitationCode': str(code or '').strip(),
        'timezone': C.INVITATION_TIMEZONE,
        'source': C.ACTIVITY_SOURCE,
    }
    if activity_id:
        body['activityId'] = activity_id
    payload = await _web_json(
        'POST', C.INVITATION_CODE_REDEEM_PATH, cookie, proxy=proxy,
        timeout=timeout, body=body)
    data = unwrap_result(payload)
    return {
        'message': str(payload.get('message') or '')
        if isinstance(payload, dict) else '',
        'notice': data.get('notice') if isinstance(data.get('notice'), dict)
        else data,
        'raw': data,
        'payload': payload,
    }


# ── 对话 ────────────────────────────────────────────────────
def _chat_error(response: httpx.Response, text: str) -> PlatformError:
    """把网关错误映射成带「该不该换号」语义的状态码。

    401 = token 失效（adapter 强刷一次）；402 = 积分耗尽；429 = 限流；
    5xx = 上游故障；其它 4xx 是请求本身的问题，换号也不会成功。
    """
    payload: object = None
    try:
        payload = json.loads(text) if text else None
    except ValueError:
        payload = None
    message = _error_text(payload, text[:200] or f'上游 HTTP {response.status_code}')
    status = response.status_code
    if status == 401:
        return PlatformError(message, status=401, code='token_expired',
                             payload=payload)
    if status == 402:
        return PlatformError(message, status=402, code='quota_exceeded',
                             payload=payload)
    if status == 429:
        retry = response.headers.get('retry-after')
        try:
            retry_after = int(float(retry)) if retry else None
        except (TypeError, ValueError):
            retry_after = None
        return PlatformError(message, status=429, code='rate_limited',
                             retry_after=retry_after, payload=payload)
    if status >= 500:
        return PlatformError(message, status=502, code='upstream_error',
                             payload=payload)
    return PlatformError(message, status=status, code='bad_request',
                         payload=payload)


async def chat(token: str, body: dict, *, proxy: str | None = None,
               timeout: float = C.CHAT_TIMEOUT) -> dict:
    """非流式对话，返回上游 JSON。"""
    async with config.http_client(
            httpx.Timeout(timeout, connect=10),
            proxy=_selected_proxy(proxy), lane='dumate') as client:
        response = await client.post(
            C.GATEWAY_BASE + C.CHAT_PATH, headers=_gateway_headers(token),
            content=json.dumps(body, ensure_ascii=False).encode('utf-8'))
    text = response.text
    if response.status_code >= 400:
        raise _chat_error(response, text)
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise PlatformError('DuMate 对话响应不是 JSON', status=502,
                            code='bad_upstream', payload=text[:300]) from exc
    if not isinstance(payload, dict):
        raise PlatformError('DuMate 对话响应形状异常', status=502,
                            code='bad_upstream', payload=payload)
    return payload


async def open_chat(token: str, body: dict, *, proxy: str | None = None,
                    idle_timeout: float = C.CHAT_IDLE_TIMEOUT,
                    connect_timeout: float = 10) -> httpx.Response:
    """流式对话：返回**未读取**的上游响应（调用方负责关闭）。

    上游正常情况下回 `text/event-stream`；登录态失效时可能回 HTTP 200 +
    JSON（不是 SSE）。照原样透传会把一段 JSON 当成 SSE 发给客户端，所以
    非 SSE 的响应在这里一次性读完并按错误归类。
    """
    async with config.http_client(
            httpx.Timeout(idle_timeout, connect=connect_timeout),
            proxy=_selected_proxy(proxy), lane='dumate') as client:
        request = client.build_request(
            'POST', C.GATEWAY_BASE + C.CHAT_PATH,
            headers={**_gateway_headers(token), 'Accept': 'text/event-stream'},
            content=json.dumps(body, ensure_ascii=False).encode('utf-8'))
        response = await client.send(request, stream=True)
        if response.status_code >= 400:
            text = (await response.aread()).decode('utf-8', errors='replace')
            await response.aclose()
            raise _chat_error(response, text)
        ctype = str(response.headers.get('content-type') or '').lower()
        if 'text/event-stream' not in ctype:
            text = (await response.aread()).decode('utf-8', errors='replace')
            await response.aclose()
            payload: object = None
            try:
                payload = json.loads(text) if text else None
            except ValueError:
                payload = None
            if _expired(payload, response.status_code):
                raise PlatformError(
                    _error_text(payload, 'DuMate 登录态已失效'),
                    status=401, code='token_expired', payload=payload)
            raise PlatformError(
                _error_text(payload, text[:200] or '对话接口未返回 SSE'),
                status=502, code='bad_upstream', payload=payload)
        return response
