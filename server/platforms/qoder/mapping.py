"""Qoder 网关账号 / 模型 → token-farm 对外形状的纯映射。

这里**只做字段翻译**，不发请求、不碰凭据：账号脱敏、realm 口径、模型前缀
都在这一层收敛，适配器只负责调度与错误翻译。

两个口径差异值得记住：

  · 网关的 `realm` 是 `cn` / `intl`；token-farm 的统计口径是 `cn` / `global`
    （`PlatformChat.realm` 与日志 `realm` 列都用后者）。
  · 网关的账号凭据**永远不出网关**：`public()` 只给 `hasRefreshToken` /
    `hasPAT` / `tokenFamily`，所以 `token_preview` 只能是一句占位说明。
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

PLATFORM = 'qoder'
LABEL = 'Qoder'
REALM_LABEL = {'cn': '国内版', 'intl': '国际版'}

# Qoder 国内版账号的「永不过期」用 253402214400（9999-12-31）占位。面板上
# 显示这种日期没有意义，按「无到期时间」处理；2100 年之后的值一律不算。
_MAX_EXPIRY = 4102444800

# 网关 `public()` 的 source → token-farm 的三类来源。
_SOURCE_MAP = {
    'oauth': 'web',
    'desktop-app': 'desktop',
}


def normalize_realm(value: Any) -> str:
    return 'intl' if str(value or '').strip().lower() in ('intl', 'global') else 'cn'


def db_realm(value: Any) -> str:
    """网关 realm → token-farm 统计口径（`cn` / `global`）。"""
    return 'global' if normalize_realm(value) == 'intl' else 'cn'


def iso_from_epoch(value: Any) -> str:
    try:
        epoch = float(value)
    except (TypeError, ValueError):
        return ''
    if epoch <= 0:
        return ''
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _token_family(raw: dict) -> str:
    return str(raw.get('tokenFamily') or 'unknown')


def public_account(raw: dict, *, now: float | None = None) -> dict:
    """网关账号对象 → `PlatformAccount`（无明文凭据）。"""
    raw = raw if isinstance(raw, dict) else {}
    uid = str(raw.get('uid') or '')
    realm = normalize_realm(raw.get('realm'))
    family = _token_family(raw)
    has_refresh = bool(raw.get('hasRefreshToken'))
    has_pat = bool(raw.get('hasPAT'))
    has_token = bool(has_refresh or has_pat or raw.get('expiresAt'))
    source = _SOURCE_MAP.get(str(raw.get('source') or '').strip().lower(), 'manual')

    stamp = float(now if now is not None else time.time())
    cool_for = raw.get('cooldownFor')
    cooling = bool(raw.get('inCooldown'))
    try:
        cool_seconds = int(cool_for or 0)
    except (TypeError, ValueError):
        cool_seconds = 0
    if cooling and cool_seconds <= 0:
        cool_seconds = 60
    cool_until = int(stamp + cool_seconds) if cooling and cool_seconds > 0 else 0

    if has_token:
        preview = f'托管于 Qoder 内置网关（{family}）' if family != 'unknown' \
            else '托管于 Qoder 内置网关'
    else:
        preview = ''

    added = raw.get('addedAt')
    try:
        created = int(float(added or 0))
    except (TypeError, ValueError):
        created = 0

    try:
        weight = float(raw.get('weight') or 0)
    except (TypeError, ValueError):
        weight = 0.0
    if weight != weight or weight in (float('inf'), float('-inf')) or weight <= 0:
        weight = 0.0

    return {
        'platform': PLATFORM,
        'uid': uid,
        'nickname': str(raw.get('nickname') or ''),
        'email': '',
        'realm': realm,
        'realm_label': REALM_LABEL[realm],
        'proxy': '',
        'weight': weight,
        'disabled': not bool(raw.get('enabled', True)),
        'disabled_reason': '',
        'disabled_at': 0,
        'cooling': cooling,
        'cooling_until': cool_until,
        'cool_remaining_sec': cool_seconds if cooling else 0,
        'cooling_reason': '',
        'expiredAt': iso_from_epoch(raw.get('expiresAt')),
        'refreshExpiredAt': '',
        'has_token': has_token,
        'has_refresh_token': has_refresh,
        'has_device_key': False,
        'token_preview': preview,
        'source': source,
        'createdAt': created,
        'updatedAt': created,
        'refreshedAt': '',
        'lastRefreshError': str(raw.get('lastError')
                               or raw.get('creditsRefreshError') or ''),
        'fingerprint': {
            'uid': uid,
            'platform': str(raw.get('platform') or ''),
            'userType': str(raw.get('userType') or ''),
            'tokenFamily': family,
        },
        # 网关面板里的附加信息（前端暂未使用，排障与后续展示用）。
        'plan': raw.get('plan'),
        'org_label': raw.get('orgLabel') or '',
        'checked_in_today': bool(raw.get('checkedInToday')),
        'can_checkin': raw.get('canCheckin'),
    }


def public_model(raw: dict, *, prefix: str = f'{PLATFORM}/') -> dict:
    """网关 `/v1/models` 条目 → 平台模型（`id` 带平台前缀）。"""
    raw = raw if isinstance(raw, dict) else {}
    model_id = str(raw.get('id') or '').strip()
    realm = normalize_realm(raw.get('realm'))
    exclusive = str(raw.get('realm_exclusive') or '').strip().lower()
    if exclusive in ('cn', 'intl'):
        realms = [exclusive]
    else:
        declared = raw.get('realms')
        realms = [normalize_realm(r) for r in declared] \
            if isinstance(declared, list) and declared else ['cn', 'intl']
    capabilities = raw.get('capabilities') if isinstance(
        raw.get('capabilities'), dict) else {}
    modalities = raw.get('modalities') if isinstance(
        raw.get('modalities'), dict) else None
    if modalities is None:
        inputs = raw.get('input_modalities')
        modalities = {'input': inputs if isinstance(inputs, list) else ['text']}
    windows = raw.get('context_windows') if isinstance(
        raw.get('context_windows'), list) else []
    context_window = raw.get('context_length') or raw.get('max_input_tokens')
    if not context_window and windows:
        try:
            context_window = max(int(w) for w in windows)
        except (TypeError, ValueError):
            context_window = 0
    max_tokens = raw.get('max_output_tokens') or raw.get('max_completion_tokens') or 0
    return {
        'id': f'{prefix}{model_id}',
        'object': 'model',
        'created': int(raw.get('created') or 0),
        'owned_by': PLATFORM,
        'name': str(raw.get('name') or model_id),
        'platform': PLATFORM,
        'realm': realm,
        'realms': realms,
        'context_window': int(context_window or 0),
        'max_tokens': int(max_tokens or 0),
        'supports_reasoning': bool(raw.get('supports_reasoning')
                                   or capabilities.get('reasoning')),
        'modalities': modalities,
        'model_name': model_id,
        'description': str(raw.get('description') or ''),
        'aliases': raw.get('aliases') if isinstance(raw.get('aliases'), list) else [],
        'enabled': raw.get('enabled') is not False,
        'upstream_key': str(raw.get('upstream_key') or ''),
        'name_local': str(raw.get('name_local') or ''),
    }


def credits_of(raw: dict) -> dict:
    """网关账号里的 `credits` 快照 → 面板积分结构。

    网关不会为读余额单独打上游：`credits` 是**已登记**的快照，刷新由它的
    面板或后台任务负责。拿不到快照时返回明确的 `error`，而不是 0。
    """
    raw = raw if isinstance(raw, dict) else {}
    uid = str(raw.get('uid') or '')
    realm = normalize_realm(raw.get('realm'))
    snapshot = raw.get('credits') if isinstance(raw.get('credits'), dict) else None
    if not snapshot:
        return {
            'uid': uid,
            'realm': realm,
            'credits': None,
            'error': str(raw.get('creditsRefreshError')
                         or '尚无积分快照，请在账号页点一次「刷新」查询余额'),
            'code': 'credits_missing',
        }

    def _num(value: Any) -> int | float | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

    remain = _num(snapshot.get('remain_effective'))
    if remain is None:
        remain = _num(snapshot.get('remain'))
    limit = _num(snapshot.get('size'))
    used = _num(snapshot.get('used'))

    # 到期只认**包级**字段。
    #
    # 上游 `/quota/usage` 的响应顶层有一个 `expiresAt`，但它的归属未经证实
    # （内置网关的 `soonest_expiring_days` 也刻意不映射它）：CN 号那里常是
    # 253402214400 这类「永不过期」占位，直接把「按月重置」的额度贴成到期日
    # 会让面板显示一个假的有效期。只有 packages[] 里各包自己的到期字段才可信，
    # 上游补上后这里自动生效。
    expires: list[dict] = []
    seen: set[int] = set()
    packages = snapshot.get('packages')
    if isinstance(packages, list):
        for package in packages:
            if not isinstance(package, dict) or package.get('no_expiry'):
                continue
            exp = _num(package.get('expires_at'))
            if not exp or exp <= 0 or exp > _MAX_EXPIRY or int(exp) in seen:
                continue
            seen.add(int(exp))
            expires.append({'label': str(package.get('name') or '额度包'),
                            'amount': _num(package.get('remain')) or 0,
                            'expire_at': int(exp)})
    expires.sort(key=lambda item: item['expire_at'])
    next_expire = expires[0]['expire_at'] if expires else None

    delta = _num(raw.get('lastCreditsDelta'))
    checked = bool(raw.get('checkedInToday'))
    checkin_credits = int(delta) if checked and delta and delta > 0 else None

    return {
        'uid': uid,
        'realm': realm,
        'credits': remain,
        'remain': remain,
        'limit': limit,
        'used': used,
        'checkin_credits': checkin_credits,
        'checked_in': checked,
        'expires': expires,
        'next_expire_at': next_expire,
        'source': 'qoder.quota',
        'unit': 'credits',
        'packages': packages if isinstance(packages, list) else [],
    }


def usage_of(raw: dict) -> dict:
    """用量弹窗：把网关的额度快照放进 `entitlement`，与 Trae 的展示位对齐。"""
    raw = raw if isinstance(raw, dict) else {}
    snapshot = credits_of(raw)
    out: dict[str, Any] = {
        'uid': str(raw.get('uid') or ''),
        'realm': normalize_realm(raw.get('realm')),
        'session_usage': None,
        'entitlement': None,
        'errors': [],
    }
    if snapshot.get('credits') is not None:
        out['entitlement'] = {
            'source': 'qoder.quota',
            'remain': snapshot.get('remain'),
            'limit': snapshot.get('limit'),
            'used': snapshot.get('used'),
            'expires_at': snapshot.get('next_expire_at'),
            'packages': snapshot.get('packages') or [],
        }
    else:
        out['errors'].append({
            'endpoint': 'credits',
            'message': str(snapshot.get('error') or '尚无积分快照'),
        })
    return out


def checkin_of(result: dict, *, uid: str, realm: str) -> dict:
    """网关签到结果 → 适配器返回结构。"""
    result = result if isinstance(result, dict) else {}
    logs = result.get('logs') if isinstance(result.get('logs'), list) else []
    message = str(result.get('message') or result.get('msg')
                  or (logs[-1] if logs else '') or '')
    out = {
        'uid': uid,
        'realm': realm,
        'ok': bool(result.get('ok')),
        'message': message,
        'earned_credit': int(result.get('earned_credit') or 0),
        'credits': result.get('credits'),
        'claimed': result.get('claimed') or [],
        'next_available_at': result.get('next_available_at'),
        'next_available_note': result.get('next_available_note'),
        'logs': logs,
    }
    if not result:
        out['skipped'] = True
        out['message'] = '网关未执行签到（今日已签到或该账号不支持签到）'
        out['ok'] = True
    return out
