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
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from .. import security
from ..platforms import PlatformError, all_adapters, get as get_platform

logger = logging.getLogger('workbuddy.platforms')

router = APIRouter(prefix='/api/platforms', tags=['platforms'])

# 批量签到并发上限：与 WorkBuddy 的签到同口径，保守值避免触发风控。
_CHECKIN_CONCURRENCY = 3


def _adapter(name: str):
    adapter = get_platform(name)
    if adapter is None:
        raise HTTPException(status_code=404, detail=f'未知平台 {name!r}')
    return adapter


def _fail(exc: PlatformError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.message)


@router.get('')
def list_platforms(user: dict = Depends(security.current_user)) -> dict:
    """平台能力清单（前端据此决定显示哪些按钮）。"""
    out = []
    for adapter in all_adapters():
        item: dict[str, Any] = {
            'name': adapter.name,
            'label': adapter.label,
            'model_prefix': adapter.model_prefix,
            'supports_checkin': adapter.supports_checkin,
            # 能否从本机客户端一键导入（Trae：读 %APPDATA%\Trae CN 的登录态）
            'supports_desktop_import': hasattr(adapter, 'import_from_desktop'),
            'account_count': 0,
        }
        try:
            item['account_count'] = len(adapter.list_accounts())
        except Exception as exc:  # noqa: BLE001 —— 单个平台坏掉不该拖垮整个列表
            item['error'] = str(exc)
        out.append(item)
    return {'platforms': out}


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
    """从本机客户端一键导入（Trae：读 `%APPDATA%\\Trae CN` 里已登录的账号）。

    凭据只从磁盘进内存、再落到本平台的账号目录；接口返回的是**脱敏后**的
    账号结构，和手工导入完全一致。
    """
    adapter = _adapter(name)
    importer = getattr(adapter, 'import_from_desktop', None)
    if importer is None:
        raise HTTPException(status_code=400,
                            detail=f'{adapter.label} 不支持从本机客户端导入')
    return importer(realm=realm)


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


# ── 任务 ────────────────────────────────────────────────
@router.post('/{name}/accounts/{account_id}/checkin')
async def checkin(name: str, account_id: str,
                  user: dict = Depends(security.require_admin)) -> dict:
    adapter = _adapter(name)
    try:
        return {'platform': name, 'result': await adapter.checkin(account_id)}
    except PlatformError as exc:
        raise _fail(exc) from exc


@router.post('/{name}/checkin-all')
async def checkin_all(name: str,
                      user: dict = Depends(security.require_admin)) -> dict:
    """对全部账号执行签到。

    单个账号失败只记进结果，不影响其他账号——批量任务里一个坏号让整批回滚
    是反直觉的，而且用户没法知道到底哪个号有问题。
    """
    adapter = _adapter(name)
    accounts = adapter.list_accounts()
    semaphore = asyncio.Semaphore(_CHECKIN_CONCURRENCY)

    async def run(account: dict) -> dict:
        account_id = str(account.get('uid') or '')
        async with semaphore:
            try:
                result = await adapter.checkin(account_id)
                return {'uid': account_id, 'ok': bool(result.get('ok')),
                        'message': result.get('message') or '', 'result': result}
            except PlatformError as exc:
                return {'uid': account_id, 'ok': False, 'message': exc.message,
                        'code': exc.code}
            except Exception as exc:  # noqa: BLE001
                logger.warning('平台签到异常（%s/%s）: %s', name, account_id, exc)
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
