"""多平台管理接口：账号导入 / 刷新 / 签到 / 额度 / 模型。

平台名从路径里取，只接受 `server.platforms.registry` 里已注册的名字——
**不做模糊匹配**，避免把请求导到一个不存在（或拼错）的平台上。

权限口径与账号页一致，而不是一律 `current_user`：这里的写接口动的是
**凭据**（导入 / 删除账号）和**上游行为**（刷新令牌、签到），只读令牌不该
能调。分三档：

  · 读（列表 / 额度 / 模型目录）→ `current_user`，只读令牌也看得到；
  · 操作（刷新令牌 / 签到）      → `require_admin`，与管理令牌同级；
  · 改凭据（导入 / 删除账号）    → `require_session_admin`，与
    `/api/accounts/upload` 同级：写外部凭据进号池这件事只对真人开放。
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from .. import db, security
from ..platforms import PlatformAdapter, PlatformError, all_adapters, get as get_platform
from ..platforms import checkinpacing, limiter, settings as runtime_settings

logger = logging.getLogger('workbuddy.platforms')

router = APIRouter(prefix='/api/platforms', tags=['platforms'])

# 积分查询并发：只读状态接口，比签到宽松一档；账号很多时也不至于把站点打满。
_CREDITS_CONCURRENCY = 4
# 「永久冷却」的截止时刻（2099-12-31 23:59:59 UTC）。与 Trae 存储层里的
# 常量同值；路由层不 import 平台内部模块，否则每接一个平台都要改这里。
_PERMANENT_UNTIL = 4102444799


def _adapter(name: str):
    adapter = get_platform(name)
    if adapter is None:
        raise HTTPException(status_code=404, detail=f'未知平台 {name!r}')
    return adapter


def _fail(exc: PlatformError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.message)


@router.get('')
def list_platforms(user: dict = Depends(security.current_user)) -> dict:
    """平台能力清单（前端据此决定显示哪些按钮）。

    `ui_visible=False` 的适配器（组合这类虚拟平台）不出现在这里：它们没有
    账号页，露出来只会让人以为「这里要导入账号」。
    """
    out = []
    for adapter in all_adapters():
        if not getattr(adapter, 'ui_visible', True):
            continue
        item: dict[str, Any] = {
            'name': adapter.name,
            'label': adapter.label,
            'model_prefix': adapter.model_prefix,
            'supports_checkin': adapter.supports_checkin,
            # 能否查积分（Trae 读签到状态里的 credits；Qoder 读余额快照；
            # 基类默认实现是「不支持」）
            'supports_credits': type(adapter).credits is not PlatformAdapter.credits,
            # 能否从本机客户端一键导入（Trae 读 %APPDATA%\Trae CN 的登录态；
            # Qoder 读官方 App / CLI 的登录态）
            'supports_desktop_import': hasattr(adapter, 'import_from_desktop'),
            # 能否在网页里直接登录（Trae 本机回环回调 + refreshToken 兑换；
            # Qoder 设备授权轮询）
            'supports_web_login': bool(getattr(adapter, 'supports_web_login', False)),
            # 账号是否分国内版 / 国际版（Trae / Qoder）：前端据此显示版本切换并按版本过滤
            'supports_realm': bool(getattr(adapter, 'realm_aware', False)),
            # 能否维护「积分消耗倍率」表（Trae：面板导入 / 导出 JSON）
            'supports_model_rates': hasattr(adapter, 'model_rates'),
            # 能否维护「模型目录」表（Trae：面板导入 / 导出 JSON）
            'supports_model_catalog': hasattr(adapter, 'model_catalog'),
            # 能否修改账号权重 / 设置强制冷却 / 提交网页登录回调。
            # 非池化平台（Qoder）没有这些能力：前端据此隐藏按钮，而不是让
            # 用户点下去才收到 400。
            'supports_weight': type(adapter).set_account_options
            is not PlatformAdapter.set_account_options,
            'supports_cooling': type(adapter).set_account_cooling
            is not PlatformAdapter.set_account_cooling,
            'supports_login_callback': hasattr(adapter, 'login_submit_callback'),
            'account_count': 0,
        }
        try:
            item['account_count'] = len(adapter.list_accounts())
        except Exception as exc:  # noqa: BLE001 —— 单个平台坏掉不该拖垮整个列表
            item['error'] = str(exc)
        out.append(item)
    return {'platforms': out}


# ── 模型目录（跨平台一次取回）─────────────────────────────
@router.get('/model-catalog')
async def model_catalog(user: dict = Depends(security.current_user)) -> dict:
    """所有可见平台的模型清单，一次请求取回。

    为什么合并成一个接口：模型映射/组合的编辑界面要同时列出多个平台的模型，
    逐个平台发请求会让「打开页面」变成 N 次往返，而且任何一个平台慢都会让
    整页干等。这里**单个平台失败只记在该条的 `error` 里**——一个平台的令牌
    过期不该让整份清单变成空白。

    收录范围是 `ui_visible or combo_member`：组合的编辑页要能选 WorkBuddy
    （它没有平台下拉/账号页，只作为成员存在），其余页面拿到的仍是可见平台。
    """
    out = []
    for adapter in all_adapters():
        if not (getattr(adapter, 'ui_visible', True)
                or getattr(adapter, 'combo_member', False)):
            continue
        item: dict[str, Any] = {'name': adapter.name,
                                'label': adapter.label, 'models': [],
                                # 池级权重：WorkBuddy 的账号权重在上游池里整池
                                # 生效，成员权重与它不是一个口径，前端据此提示。
                                'pool_weight': bool(getattr(adapter, 'pool_weight', False))}
        try:
            models = await adapter.list_models()
            # 组合成员按 `平台 + 裸模型名` 保存：双区同名模型（Qoder）在目录里
            # 是两条区域化 ID，但组合只能选到裸名，重复列出只会让下拉出现
            # 两个一模一样的选项。保留第一条即可。
            seen: set[str] = set()
            unique: list[dict] = []
            for m in models:
                bare = str(m.get('model_name') or m.get('id') or '').strip()
                if not bare or bare in seen:
                    continue
                seen.add(bare)
                unique.append(m)
            item['models'] = unique
        except PlatformError as exc:
            item['error'] = exc.message
        except Exception as exc:  # noqa: BLE001 —— 单个平台异常不拖垮整份清单
            logger.warning('平台模型目录异常（%s）: %s', adapter.name, exc)
            item['error'] = str(exc)
        out.append(item)
    return {'platforms': out}


# ── 平台设置（池级并发上限 / 签到节奏）────────────────────
def _settings_payload(name: str) -> dict:
    return {
        'platform': name,
        'max_in_flight': limiter.load_limit(name),
        'limits': {'min': limiter.LIMIT_MIN, 'max': limiter.LIMIT_MAX},
        # 批量签到的并发与账号间隔（与上面的推理并发是两个口径）。
        **checkinpacing.load(name),
        # 单次请求的时间预算与平台专属开关（超时 / 换号 / 会话亲和 / 回写）。
        **runtime_settings.load(name),
    }


@router.get('/{name}/settings')
def platform_settings(name: str,
                      user: dict = Depends(security.current_user)) -> dict:
    """平台级设置：在途请求上限 + 批量签到节奏 + 单次请求预算 / 平台开关。"""
    _adapter(name)
    return _settings_payload(name)


@router.put('/{name}/settings')
def save_platform_settings(name: str, body: dict = Body(...),
                           user: dict = Depends(security.require_admin)) -> dict:
    """改平台设置（池级并发 / 签到节奏 / 超时与换号 / 平台专属开关）。

    各部分可以分开提交：界面改签到节奏时不必把 `max_in_flight` 一起带上。
    保存后立刻让适配器丢掉缓存的信号量（`invalidate_limit`）：否则新上限要等
    适配器自己的缓存过期才生效，界面会显示「已保存但没变化」。

    `runtime_settings.save` 只认本平台支持的字段：例如 Qoder 没有换号 /
    会话亲和开关，提交了会明确 400，而不是静默忽略让界面以为保存成功。
    """
    adapter = _adapter(name)
    payload = body if isinstance(body, dict) else {}
    changes: list[str] = []
    try:
        if 'max_in_flight' in payload:
            value = limiter.save_limit(name, payload.get('max_in_flight'))
            changes.append(f'max_in_flight={value}')
            # 并非所有适配器都用到并发闸门（基类没有这个方法），取不到就跳过。
            getattr(adapter, 'invalidate_limit', lambda: None)()
        pace = checkinpacing.save(name, payload)
        runtime = runtime_settings.save(name, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    changes.extend(f'{field}={pace[field]}' for field in (
        'checkin_concurrency', 'checkin_delay_min', 'checkin_delay_max')
        if payload.get(field) is not None)
    changes.extend(f'{field}={runtime[field]}' for field in (
        'request_timeout', 'idle_timeout', 'max_accounts_try',
        'session_affinity', 'session_affinity_ttl', 'desktop_sync')
        if payload.get(field) is not None)
    security.audit(user, 'update_platform_settings', name, ' '.join(changes))
    return _settings_payload(name)


# ── 账号 ────────────────────────────────────────────────
@router.get('/{name}/accounts')
async def list_accounts(name: str,
                        user: dict = Depends(security.current_user)) -> dict:
    adapter = _adapter(name)
    return {'platform': name, 'accounts': adapter.list_accounts()}


@router.post('/{name}/accounts/import')
async def import_accounts(
    name: str,
    payload: dict = Body(...),
    user: dict = Depends(security.require_session_admin),
) -> dict:
    """导入账号。接受单个对象，或 `{accounts: [...]}` 批量。

    单个失败不中断整批：返回 `imported` 与 `failed` 两段，界面上能逐条看到
    哪一个是格式问题——这比整批 400 更有用（批量导入时用户往往一次贴几十个）。
    """
    adapter = _adapter(name)
    if isinstance(payload.get('accounts'), list):
        entries = payload['accounts']
    else:
        entries = [payload]
    imported: list[dict] = []
    failed: list[dict] = []
    for entry in entries:
        try:
            imported.append(adapter.import_account(entry))
        except PlatformError as exc:
            failed.append({'error': exc.message, 'code': exc.code})
        except ValueError as exc:
            failed.append({'error': str(exc), 'code': 'invalid_account'})
    return {'platform': name, 'imported': imported, 'failed': failed}


@router.post('/{name}/accounts/import-desktop')
async def import_from_desktop(name: str, realm: str | None = Query(None),
                              user: dict = Depends(security.require_session_admin)) -> dict:
    """从本机客户端一键导入（Trae：读 `%APPDATA%\\Trae CN` 里已登录的账号；
    Qoder：读官方桌面 App 与 CLI 的登录态，双区一起导入）。

    凭据只从磁盘进内存、再落到本平台的账号目录；接口返回的是**脱敏后**的
    账号结构，和手工导入完全一致。
    """
    adapter = _adapter(name)
    importer = getattr(adapter, 'import_from_desktop', None)
    if importer is None:
        raise HTTPException(status_code=400,
                            detail=f'{adapter.label} 不支持从本机客户端导入')
    return importer(realm=realm)


@router.post('/{name}/login/start')
async def login_start(name: str, body: dict | None = Body(None),
                      user: dict = Depends(security.require_session_admin)) -> dict:
    """发起「网页登录」：返回授权 URL，前端打开浏览器后轮询结果。

    Trae 走本机回环回调：回调服务由**后端**在 `127.0.0.1` 随机端口上临时起
    一个（授权页要求 `auth_callback_url` 严格是
    `http://127.0.0.1:<port>/authorize`），所以面板后端必须和浏览器在同一台
    机器上。Qoder 走设备授权轮询，只有授权 URL，不需要回调。
    """
    adapter = _adapter(name)
    starter = getattr(adapter, 'login_start', None)
    if starter is None:
        raise HTTPException(status_code=400,
                            detail=f'{adapter.label} 不支持网页登录')
    realm = str((body or {}).get('realm') or 'cn').strip().lower()
    try:
        out = starter(realm=realm)
    except PlatformError as exc:
        raise _fail(exc) from exc
    security.audit(user, 'platform_login_start', name, realm)
    return {'platform': name, **out}


@router.get('/{name}/login/poll')
def login_poll(name: str, login_id: str = Query(...),
               user: dict = Depends(security.require_session_admin)) -> dict:
    """查询网页登录状态：`pending` / `exchanging` / `success` / `error` / `expired`。"""
    adapter = _adapter(name)
    poller = getattr(adapter, 'login_poll', None)
    if poller is None:
        raise HTTPException(status_code=400,
                            detail=f'{adapter.label} 不支持网页登录')
    try:
        return {'platform': name, **poller(login_id)}
    except PlatformError as exc:
        raise _fail(exc) from exc


@router.post('/{name}/login/{login_id}/callback')
async def login_callback(name: str, login_id: str,
                         body: dict | None = Body(None),
                         user: dict = Depends(security.require_session_admin)) -> dict:
    """提交回调地址，完成一次网页登录。

    授权页只允许回调 `http://127.0.0.1:<port>/authorize`，远程部署时浏览器
    访问不到服务器上的回环地址。用户把地址栏里的完整链接粘贴回来，后端按
    同一套规则校验 `loginTraceID` 并兑换 `refreshToken`。
    """
    adapter = _adapter(name)
    submitter = getattr(adapter, 'login_submit_callback', None)
    if submitter is None:
        raise HTTPException(status_code=400,
                            detail=f'{adapter.label} 不支持网页登录')
    callback = str((body or {}).get('url') or '').strip()
    try:
        out = submitter(login_id, callback)
    except PlatformError as exc:
        raise _fail(exc) from exc
    security.audit(user, 'platform_login_callback', name, login_id)
    return {'platform': name, **out}


@router.delete('/{name}/login/{login_id}')
async def login_cancel(name: str, login_id: str,
                       user: dict = Depends(security.require_session_admin)) -> dict:
    adapter = _adapter(name)
    canceller = getattr(adapter, 'login_cancel', None)
    if canceller is None:
        raise HTTPException(status_code=400,
                            detail=f'{adapter.label} 不支持网页登录')
    return {'platform': name, 'cancelled': bool(canceller(login_id))}


@router.patch('/{name}/accounts/{account_id}')
async def update_account_options(name: str, account_id: str, body: dict = Body(...),
                                 user: dict = Depends(security.require_admin)) -> dict:
    """修改账号级调度参数（权重 / 临时冻结）。

    适配器负责校验与落库；未覆盖对应能力的平台会由基类抛明确的「不支持」
    （400），交给 `_fail` 原样回给界面 —— 这里不判断「平台是否支持」，
    否则同一份能力判断要写两遍。
    """
    adapter = _adapter(name)
    payload = body if isinstance(body, dict) else {}
    weight = payload.get('weight')
    has_disabled = 'disabled' in payload
    disabled = payload.get('disabled')
    reason = str(payload.get('reason') or '')
    if weight is None and not has_disabled:
        raise HTTPException(status_code=400, detail='没有要修改的项')
    if has_disabled and not isinstance(disabled, bool):
        raise HTTPException(status_code=400, detail='disabled 必须是布尔值')
    try:
        account: dict | None = None
        # 先冻结再改权重：两个都传时最终返回的账号仍带着冻结状态。
        if has_disabled:
            account = adapter.set_account_disabled(account_id, disabled, reason)
        if weight is not None:
            account = adapter.set_account_options(account_id, weight=weight)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PlatformError as exc:
        raise _fail(exc) from exc
    security.audit(
        user,
        'update_account_disabled' if has_disabled and weight is None
        else 'update_account_options',
        f'{name}/{account_id}',
        f'weight={weight} disabled={disabled}' if has_disabled else f'weight={weight}',
    )
    return {'platform': name, 'account': account}


def _cooldown_until(payload: dict) -> float:
    """把界面传来的三种写法归一成截止时刻（epoch 秒）。"""
    if payload.get('permanent') is True:
        return float(_PERMANENT_UNTIL)
    duration = payload.get('duration_seconds')
    if duration not in (None, ''):
        try:
            seconds = float(duration)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail='冷却时长必须是数字') from exc
        if not (seconds > 0) or seconds > _PERMANENT_UNTIL - time.time():
            raise HTTPException(status_code=400, detail='冷却时长不合法')
        return time.time() + seconds
    until = payload.get('until')
    if isinstance(until, (int, float)) and not isinstance(until, bool):
        return float(until)
    text = str(until or '').strip()
    if text:
        try:
            parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail='冷却截止时间格式不正确') from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    raise HTTPException(status_code=400, detail='缺少冷却时长或截止时间')


@router.post('/{name}/accounts/{account_id}/cooling')
async def set_account_cooling(name: str, account_id: str,
                              body: dict = Body(...),
                              user: dict = Depends(security.require_admin)) -> dict:
    """给账号设置强制冷却（定时 / 永久），期间不会被调度。"""
    adapter = _adapter(name)
    payload = body if isinstance(body, dict) else {}
    until = _cooldown_until(payload)
    if until <= time.time():
        raise HTTPException(status_code=400, detail='冷却截止时间必须在未来')
    reason = str(payload.get('reason') or '')
    try:
        account = adapter.set_account_cooling(account_id, until, reason)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PlatformError as exc:
        raise _fail(exc) from exc
    security.audit(user, 'set_account_cooling', f'{name}/{account_id}',
                   f'until={int(until)}')
    return {'platform': name, 'account': account}


@router.delete('/{name}/accounts/{account_id}/cooling')
async def clear_account_cooling(name: str, account_id: str,
                                user: dict = Depends(security.require_admin)) -> dict:
    """立即解除强制冷却。"""
    adapter = _adapter(name)
    try:
        account = adapter.clear_account_cooling(account_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PlatformError as exc:
        raise _fail(exc) from exc
    security.audit(user, 'clear_account_cooling', f'{name}/{account_id}')
    return {'platform': name, 'account': account}


@router.delete('/{name}/accounts/{account_id}')
async def delete_account(name: str, account_id: str,
                         user: dict = Depends(security.require_session_admin)) -> dict:
    adapter = _adapter(name)
    try:
        removed = adapter.delete_account(account_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not removed:
        raise HTTPException(status_code=404, detail='账号不存在')
    return {'platform': name, 'deleted': account_id}


@router.post('/{name}/accounts/{account_id}/refresh')
async def refresh_account(name: str, account_id: str,
                          force: bool = Query(False),
                          user: dict = Depends(security.require_admin)) -> dict:
    adapter = _adapter(name)
    try:
        return {'platform': name,
                'account': await adapter.refresh_account(account_id, force=force)}
    except PlatformError as exc:
        raise _fail(exc) from exc


@router.get('/{name}/accounts/{account_id}/usage')
async def account_usage(name: str, account_id: str,
                        user: dict = Depends(security.current_user)) -> dict:
    adapter = _adapter(name)
    try:
        return {'platform': name, 'usage': await adapter.usage(account_id)}
    except PlatformError as exc:
        raise _fail(exc) from exc


@router.get('/{name}/credits')
async def list_credits(name: str,
                       user: dict = Depends(security.current_user)) -> dict:
    """全部账号的积分余额（一次请求）。

    为什么要批量：余额是列表里的一列，逐个账号请求会让「打开页面」变成 N 次
    往返。单个账号失败只记进该条，不影响其他账号——某个号的令牌过期了，
    不该让整张表都显示「—」。

    只读能力，`current_user` 即可（与账号列表、用量弹窗同档）。
    """
    adapter = _adapter(name)
    accounts = adapter.list_accounts()
    semaphore = asyncio.Semaphore(_CREDITS_CONCURRENCY)

    async def run(uid: str) -> dict:
        async with semaphore:
            try:
                return await adapter.credits(uid)
            except PlatformError as exc:
                return {'uid': uid, 'error': exc.message, 'code': exc.code}
            except Exception as exc:  # noqa: BLE001 —— 单个账号异常不该拖垮整张表
                logger.warning('平台积分查询异常（%s/%s）: %s', name, uid, exc)
                return {'uid': uid, 'error': str(exc), 'code': 'exception'}

    items = await asyncio.gather(*(run(str(a.get('uid') or '')) for a in accounts))
    return {'platform': name, 'credits': list(items)}


# ── 任务 ────────────────────────────────────────────────
def _db_realm(account: dict) -> str:
    """平台账号的 realm → 签到记录 / 统计口径（`cn` / `global`）。

    平台内部用 `cn` / `intl` 或 `cn` / `global` 两套词，签到记录统一落
    `cn` / `global`，与 `usage_daily`、`request_logs` 的 realm 列同口径。
    """
    realm = str((account or {}).get('realm') or '').strip().lower()
    return 'global' if realm in ('intl', 'global') else 'cn'


def _log_platform_checkin(name: str, account: dict, result: dict,
                          source: str) -> None:
    """把平台签到结果写进统一的签到记录，供平台页与积分收支使用。

    平台签到由本端路由触发，上游不会替我们落日志——不写这一条，平台页的
    「签到记录」与看板的「积分收入」就都拿不到数据。`earned_credit` 是平台
    适配器算出的**实际到账差额**（Trae / Qoder 都给了），不是签到奖池的
    理论值，所以可以直接作为收入记账。
    """
    account = account if isinstance(account, dict) else {}
    result = result if isinstance(result, dict) else {}
    uid = str(account.get('uid') or '')
    if not uid:
        return
    db.add_checkin_log(
        uid,
        str(account.get('nickname') or ''),
        source,
        bool(result.get('ok')),
        # 平台结果没有统一的数字错误码；字符串码留给 message，不硬塞进 code
        result.get('code') if isinstance(result.get('code'), int) else None,
        str(result.get('message') or ''),
        kind='checkin',
        platform=name,
        realm=_db_realm(account),
        credits=int(result.get('earned_credit') or 0),
    )


@router.post('/{name}/accounts/{account_id}/checkin')
async def checkin(name: str, account_id: str,
                  user: dict = Depends(security.require_admin)) -> dict:
    adapter = _adapter(name)
    try:
        # 只为拿昵称 / 版本写签到记录；列表失败不应挡在签到前面（签到本身
        # 会给出真正的错误），记录里缺这些字段也能落库。
        account = next((a for a in adapter.list_accounts()
                        if str(a.get('uid') or '') == str(account_id or '')), {})
    except Exception:  # noqa: BLE001
        account = {}
    try:
        result = await adapter.checkin(account_id)
    except PlatformError as exc:
        _log_platform_checkin(name, account, {'ok': False, 'message': exc.message},
                              'platform-manual')
        raise _fail(exc) from exc
    _log_platform_checkin(name, account, result, 'platform-manual')
    return {'platform': name, 'result': result}


@router.post('/{name}/checkin-all')
async def checkin_all(name: str,
                      user: dict = Depends(security.require_admin)) -> dict:
    """对全部账号执行签到。

    单个账号失败只记进结果，不影响其他账号——批量任务里一个坏号让整批回滚
    是反直觉的，而且用户没法知道到底哪个号有问题。

    节奏按平台设置（`checkinpacing.py`）：并发上限默认 2，相邻两个账号的
    **开始时刻**默认间隔 2–5 秒。先占并发名额再等间隔，避免间隔结束后排队
    账号同时开跑；间隔为 0 时退化为「只受并发上限约束」的旧行为。
    """
    adapter = _adapter(name)
    pace = checkinpacing.load(name)
    # 临时冻结的账号不参与批量任务：这就是「冻结」与「只冷却调度」的区别，
    # 界面文案也按这个口径写给用户。
    accounts = [a for a in adapter.list_accounts() if not a.get('disabled')]
    semaphore = asyncio.Semaphore(pace['checkin_concurrency'])
    pacer = checkinpacing.StartPacer(pace['checkin_delay_min'],
                                     pace['checkin_delay_max'])

    async def run(account: dict) -> dict:
        account_id = str(account.get('uid') or '')
        async with semaphore:
            await pacer.wait()
            try:
                result = await adapter.checkin(account_id)
                _log_platform_checkin(name, account, result, 'platform-batch')
                return {'uid': account_id, 'ok': bool(result.get('ok')),
                        'message': result.get('message') or '', 'result': result}
            except PlatformError as exc:
                _log_platform_checkin(
                    name, account,
                    {'ok': False, 'message': exc.message}, 'platform-batch')
                return {'uid': account_id, 'ok': False, 'message': exc.message,
                        'code': exc.code}
            except Exception as exc:  # noqa: BLE001
                logger.warning('平台签到异常（%s/%s）: %s', name, account_id, exc)
                _log_platform_checkin(
                    name, account,
                    {'ok': False, 'message': str(exc)}, 'platform-batch')
                return {'uid': account_id, 'ok': False, 'message': str(exc),
                        'code': 'exception'}

    results = await asyncio.gather(*(run(a) for a in accounts))
    return {'platform': name, 'total': len(results),
            'ok': sum(1 for r in results if r.get('ok')), 'results': results}


# ── 模型 ────────────────────────────────────────────────
@router.get('/{name}/models')
async def list_models(name: str, refresh: bool = Query(False),
                      user: dict = Depends(security.current_user)) -> dict:
    adapter = _adapter(name)
    try:
        models = await adapter.list_models(refresh=refresh)
    except PlatformError as exc:
        raise _fail(exc) from exc
    return {'platform': name, 'models': models}


# ── 积分消耗倍率（Trae）────────────────────────────────────
def _rates_adapter(name: str):
    adapter = _adapter(name)
    if not hasattr(adapter, 'model_rates'):
        raise HTTPException(status_code=400,
                            detail=f'{adapter.label} 不支持维护积分倍率')
    return adapter


@router.get('/{name}/model-rates')
def get_model_rates(name: str,
                    user: dict = Depends(security.current_user)) -> dict:
    """当前生效的倍率表（导入 > 客户端快照 > 内置默认值）。

    只读信息，`current_user` 即可——它和模型目录是同一档。
    """
    adapter = _rates_adapter(name)
    return {'platform': name, **adapter.model_rates()}


@router.get('/{name}/model-rates/export')
def export_model_rates(name: str,
                       user: dict = Depends(security.current_user)) -> dict:
    """导出成可直接再导入的 JSON（面板「导出」按钮直接下载）。"""
    adapter = _rates_adapter(name)
    return {'platform': name, **adapter.export_model_rates()}


@router.post('/{name}/model-rates/import')
def import_model_rates(name: str, payload: dict = Body(...),
                       user: dict = Depends(security.require_admin)) -> dict:
    """导入倍率 JSON。接受 `{模型: 倍率}`、`{rates: {...}}` 或数组三种形状。

    写的是平台自己的配置文件（不是凭据），所以与「改平台设置」同档：
    `require_admin`。解析不到任何条目时 400，**不清空**已有配置——
    空表通常意味着用户选错了文件。
    """
    adapter = _rates_adapter(name)
    try:
        result = adapter.import_model_rates(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    security.audit(user, 'import_platform_model_rates', name,
                   f"imported={result.get('sources', {}).get('imported')}")
    return {'platform': name, **result}


@router.delete('/{name}/model-rates/import')
def reset_model_rates(name: str,
                      user: dict = Depends(security.require_admin)) -> dict:
    """丢弃导入的倍率表，回到底层来源（客户端快照 + 内置默认值）。"""
    adapter = _rates_adapter(name)
    try:
        result = adapter.reset_model_rates()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    security.audit(user, 'reset_platform_model_rates', name)
    return {'platform': name, **result}


# ── 模型目录（Trae）────────────────────────────────────────
# 与倍率同样三层来源，但维护的是「有哪些模型 / 上下文多长 / 走哪条通道」。
# 注意路径不要和上面的静态 `/model-catalog`（跨平台聚合）混淆：那条只有两段，
# 这里都是 `/{name}/model-catalog...` 三段。
def _catalog_adapter(name: str):
    adapter = _adapter(name)
    if not hasattr(adapter, 'model_catalog'):
        raise HTTPException(status_code=400,
                            detail=f'{adapter.label} 不支持维护模型目录')
    return adapter


@router.get('/{name}/model-catalog')
def get_model_catalog(name: str,
                      user: dict = Depends(security.current_user)) -> dict:
    """当前生效的目录来源统计（导入 > 客户端快照 > 内置默认值）。"""
    adapter = _catalog_adapter(name)
    return {'platform': name, **adapter.model_catalog()}


@router.get('/{name}/model-catalog/export')
def export_model_catalog(name: str,
                         user: dict = Depends(security.current_user)) -> dict:
    """导出成可直接再导入的 JSON（面板「导出目录」按钮直接下载）。"""
    adapter = _catalog_adapter(name)
    return {'platform': name, **adapter.export_model_catalog()}


@router.post('/{name}/model-catalog/import')
def import_model_catalog(name: str, payload: dict = Body(...),
                         user: dict = Depends(security.require_admin)) -> dict:
    """导入目录 JSON。接受 `{models: [...]}`、裸数组、id→字段 映射等形状。

    写的是平台自己的配置文件（不是凭据），所以与「改平台设置」同档：
    `require_admin`。解析不到任何条目时 400，**不清空**已有配置——
    空表通常意味着用户选错了文件。
    """
    adapter = _catalog_adapter(name)
    try:
        result = adapter.import_model_catalog(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    security.audit(user, 'import_platform_model_catalog', name,
                   f"imported={result.get('sources', {}).get('imported')}")
    return {'platform': name, **result}


@router.delete('/{name}/model-catalog/import')
def reset_model_catalog(name: str,
                        user: dict = Depends(security.require_admin)) -> dict:
    """丢弃导入的目录，回到底层来源（客户端快照 + 内置默认值）。"""
    adapter = _catalog_adapter(name)
    try:
        result = adapter.reset_model_catalog()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    security.audit(user, 'reset_platform_model_catalog', name)
    return {'platform': name, **result}
