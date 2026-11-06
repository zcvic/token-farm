"""Trae 平台适配器：把协议层与存储层组装成平台能力。

这里负责「策略」而非「协议」：

  · 选哪个账号（按模型所属区域 + 最近最少使用）；
  · 过期先刷新、刷新串行且失败退避；
  · 模型目录的缓存与兜底；
  · 流式 / 非流式两条出口的最后一步。

模型对外按区域加前缀：`traecn/<模型>`（国内版）/ `traecom/<模型>`（国际版）；
旧式 `trae/<模型>` 继续兼容，但只按目录 / 密钥推断区域。
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from typing import Any, NamedTuple

from ... import config, db
from ...services.session_affinity import SessionAffinity, affinity_key
from ..base import ChatContext, PlatformAdapter, PlatformChat, PlatformError
from ..limiter import PoolLimiter
from . import catalog, chat as chatmod, client, desktop, rates, store
from .constants import DEFAULT_REALM, REALMS, normalize_realm

logger = logging.getLogger(__name__)

SETTING_MODELS = 'platforms.trae.models'
SETTING_MODELS_ATTEMPT = 'platforms.trae.models_attempt'
# 历史用量版本归属的一次性回填标记（见 backfill_realm_stats）。
SETTING_REALM_BACKFILL = 'platforms.trae.realm_backfill_done'
_TTL = 6 * 3600              # 目录缓存 6 小时
_IMPLICIT_TTL = 600          # 隐式拉取（/v1/models）失败后的冷却，避免每次请求都等超时
_IMPLICIT_TIMEOUT = 10       # 隐式拉取的超时：客户端启动时等 45 秒是不行的
_REFRESH_BASE = 30           # 刷新失败退避起点（秒）
_REFRESH_MAX = 1800          # 退避封顶（秒）
_MAX_ACCOUNTS_TRY = 3        # 单次对话最多换几个账号（仅 401/403 才换）
_CREDITS_TTL = 60            # 积分缓存（秒）：列表一次拉全部账号，别每刷新一次就打一遍上游
_CREDITS_TIMEOUT = 20        # 积分接口的超时：它只是状态查询，不该拖住页面


class _CreditMeter(NamedTuple):
    """一次请求的计量凭据。

    `session_id` 非空表示走了「明细模式」：请求里带这个会话号，结束后按它去
    用量明细里取这一条的实扣积分（并发安全，请求前零开销）。
    `token` / `before` 只属于「增量模式」：账号级锁 + 请求前的累计基线。
    """

    token: object | None
    before: float | None
    session_id: str | None
    started: int



def _pick(raw: dict, *keys: str) -> str:
    for key in keys:
        value = raw.get(key)
        if value not in (None, ''):
            return str(value)
    return ''


def _num(value: object) -> float:
    """上游金额字段可能是字符串 / None / 浮点；非数字一律当 0。"""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _account_weight(account: dict) -> float:
    """选号用的权重：缺省 / 非法 / <=0 → 默认 1，合法值钳到 0.1–100。

    钳制放在选号时而不是读文件时：界面要回显文件里的真实值（与 WorkBuddy
    的 `_weight_of` 同一原则），而调度只认一个安全区间。
    """
    value = _num(account.get('weight'))
    if value <= 0:
        return 1.0
    return min(store.WEIGHT_MAX, max(store.WEIGHT_MIN, value))


def _is_model_unknown(exc: PlatformError) -> bool:
    """是不是「这条通道不认识这个模型」（`[4023] model is unknown`）。

    这个错误决定**换通道**而不是换账号：模型本身存在，只是走错了门
    （老模型在 IDE 数据面、新模型在 Agent 通道）。认错了会把一个本来能用的
    模型报成不可用，所以三个来源都看：payload 的 code、payload 的正文、
    以及异常消息本身。
    """
    payload = exc.payload
    if isinstance(payload, dict):
        code = payload.get('code')
        try:
            if int(code) == 4023:  # type: ignore[arg-type]
                return True
        except (TypeError, ValueError):
            pass
        text = ' '.join(str(payload.get(key) or '')
                        for key in ('message', 'Message', 'msg'))
        if '4023' in text or 'model is unknown' in text.lower():
            return True
    text = f'{exc.message}\n{exc.payload if isinstance(exc.payload, str) else ""}'
    return '4023' in text or 'model is unknown' in text.lower()


def _credit_expiries(ent_usage: object) -> list[dict]:
    """把权益包拆成 `[{amount, expire_at, label}]`，按到期时间升序。

    积分是**分批过期**的（实测：4000 的新用户包到 11-30，500 的每月登录包
    到下月初），`usage_summary` 只给合计。只显示一个总额会让用户以为全部
    积分同时作废，所以逐包给出金额与到期时刻。
    """
    if not isinstance(ent_usage, dict):
        return []
    packs = ent_usage.get('user_entitlement_pack_list')
    if not isinstance(packs, list):
        return []
    out: list[dict] = []
    for pack in packs:
        if not isinstance(pack, dict):
            continue
        base = pack.get('entitlement_base_info')
        base = base if isinstance(base, dict) else {}
        quota = pack.get('quota')
        if not isinstance(quota, dict):
            quota = base.get('quota') if isinstance(base.get('quota'), dict) else {}
        amount = _num(quota.get('credits_limit'))
        if amount <= 0:
            continue
        expire = (_num(pack.get('expire_time'))
                  or _num(base.get('end_time'))
                  or _num(pack.get('yearly_expire_time')))
        if expire <= 0:
            continue
        extra = base.get('package_extra')
        extra = extra if isinstance(extra, dict) else {}
        out.append({
            'amount': int(amount) if amount.is_integer() else round(amount, 2),
            'expire_at': int(expire),
            'label': str(pack.get('display_desc')
                         or extra.get('package_name') or ''),
        })
    out.sort(key=lambda item: item['expire_at'])
    return out


def _dollar_usage(payload: object) -> dict | None:
    """把国际版 `user_current_entitlement_list` 的权益包读成美元额度。

    实测（2026-11-01，Free plan）：
      · 额度在 `entitlement_base_info.quota.basic_usage_limit`（美元），
        `bonus_usage_limit` 另算；
      · 已用在 `usage.basic_usage_amount` / `bonus_usage_amount`（美元）；
      · 到期时间 `entitlement_base_info.end_time`（epoch 秒）。
    没有权益包时返回 None——调用方据此区分「取不到」和「额度为 0」。
    """
    packs = payload.get('user_entitlement_pack_list') \
        if isinstance(payload, dict) else None
    if not isinstance(packs, list):
        return None
    for pack in packs:
        if not isinstance(pack, dict):
            continue
        base = pack.get('entitlement_base_info')
        base = base if isinstance(base, dict) else {}
        quota = base.get('quota')
        if not isinstance(quota, dict):
            quota = pack.get('quota')
        if not isinstance(quota, dict):
            continue
        usage = pack.get('usage')
        usage = usage if isinstance(usage, dict) else {}
        limit = _num(quota.get('basic_usage_limit')) \
            + _num(quota.get('bonus_usage_limit'))
        used = _num(usage.get('basic_usage_amount')) \
            + _num(usage.get('bonus_usage_amount'))
        end = _num(base.get('end_time')) or _num(pack.get('expire_time'))
        return {
            'limit': round(limit, 4),
            'used': round(used, 4),
            'end_at': int(end) if end > 0 else None,
            'label': str(pack.get('display_desc')
                         or base.get('product_name') or ''),
        }
    return None


def _normalize_realm_value(value: object) -> str:
    text = str(value or '').strip().lower()
    if text in ('intl', 'international', 'global', 'us', '海外', '国际'):
        return 'intl'
    if text in ('cn', 'china', 'domestic', '国内'):
        return 'cn'
    return DEFAULT_REALM


def _db_realm(realm: object) -> str:
    """Trae 的版本口径（cn / intl）→ 统计口径（cn / global）。

    两个模块故意用了不同的词：Trae 协议里叫 `intl`，而库里（以及 WorkBuddy）
    一直叫 `global`。转换只放在这一处，避免各处各自拼字符串。兼容 `global`
    / `us` 等别名（账号文件是用户可编辑的）。
    """
    return 'global' if _normalize_realm_value(realm) == 'intl' else 'cn'



class TraeAdapter(PlatformAdapter):
    name = 'trae'
    label = 'Trae'
    supports_checkin = True
    supports_web_login = True
    # 账号按 realm（cn / intl）分池，前端据此显示版本切换并按版本过滤。
    realm_aware = True

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._failures: dict[str, tuple[int, float]] = {}
        self._last_used: dict[str, float] = {}
        # uid -> (过期时间, 积分结果)。签到 / 导入 / 删除会主动清掉对应项。
        self._credits: dict[str, tuple[float, dict]] = {}
        # 模型 -> 实际可用的通道（chat / solo）。4023 换通道成功后写入，
        # 后续同模型请求不再先撞一次失败。
        self._routes: dict[str, str] = {}
        # 池级并发闸门：一个平台上所有账号合计的在途上限，配置见 settings
        # `platforms.trae.max_in_flight`。流式请求会占到流结束才释放。
        self._limiter = PoolLimiter(self.name)
        # 账号权重的 SWRR 游标（uid -> current）。只在选号时原地推进被选中的
        # 那一个，重启后从头开始，不需要持久化。
        self._swrr: dict[str, float] = {}
        # 正在「前后夹一次权益接口」的账号（uid -> 1）。并发在途时增量无法归属，
        # 后来的请求直接跳过计量，见 _meter_start。
        self._metering: dict[str, int] = {}
        # 会话粘性：会话键 → 账号 uid。上游 prompt cache 按账号隔离，同一段对话
        # 在多账号间轮换会让前缀缓存全部作废（见 services/session_affinity）。
        self._affinity = SessionAffinity()

    # ── 内部工具 ──────────────────────────────────────────
    def _lock(self, account_id: str) -> asyncio.Lock:
        lock = self._locks.get(account_id)
        if lock is None:
            lock = self._locks[account_id] = asyncio.Lock()
        return lock

    def _require(self, account_id: object) -> dict:
        account = store.read_account(account_id)
        if not account:
            raise PlatformError('账号不存在', status=404, code='account_not_found')
        return account

    def _backoff_remaining(self, uid: str) -> float:
        entry = self._failures.get(uid)
        if not entry:
            return 0.0
        return max(0.0, entry[1] - time.time())

    def _note_failure(self, uid: str) -> None:
        count = (self._failures.get(uid) or (0, 0.0))[0] + 1
        delay = min(_REFRESH_MAX, _REFRESH_BASE * (2 ** min(count - 1, 6)))
        self._failures[uid] = (count, time.time() + delay)

    def _clear_failure(self, uid: str) -> None:
        self._failures.pop(uid, None)

    async def _ensure_token(self, account: dict, *, force: bool = False) -> dict:
        """确保 access token 可用；需要时刷新并落盘。

        两个与客户端共存的细节：

          · **刷新前**先看客户端是不是已经刷新过（`desktop.fresher`）。用户开着
            IDE 时，它会在我们之前轮换 refreshToken，我们手里的那份已经作废；
            直接拿去换只会拿到 401，还会顺手把账号打进退避。
          · **刷新后**把新凭据写回客户端（`desktop.sync`）。轮换是单向的：
            不写回，用户下次打开 IDE 就会被登出。
        """
        uid = str(account.get('uid') or '')
        if not force and not store.expired(account):
            return account
        if not force:
            remaining = self._backoff_remaining(uid)
            if remaining > 0:
                raise PlatformError(
                    f'令牌刷新失败冷却中（约 {int(remaining)} 秒后可重试）',
                    status=429, code='refresh_backoff', retry_after=int(remaining))
        async with self._lock(uid):
            # 等锁期间别的请求可能已经刷好了
            fresh_read = store.read_account(uid) or account
            if not force and not store.expired(fresh_read):
                return fresh_read

            adopted = desktop.fresher(fresh_read)
            if adopted:
                fresh_read = store.save_account({**fresh_read, **adopted})
                if not store.expired(fresh_read):
                    return fresh_read

            try:
                fresh = await client.exchange_token(fresh_read)
            except PlatformError as exc:
                # 客户端可能刚好在我们重试之前完成了轮换
                adopted = desktop.fresher(fresh_read)
                if adopted:
                    saved = store.save_account({**fresh_read, **adopted})
                    if not store.expired(saved):
                        self._clear_failure(uid)
                        return saved
                self._note_failure(uid)
                raise
            except Exception as exc:  # noqa: BLE001 —— 网络类异常也要退避
                self._note_failure(uid)
                raise PlatformError(f'刷新令牌失败：{exc}', status=502,
                                    code='refresh_failed') from exc
            self._clear_failure(uid)
            saved = store.save_account({**fresh_read, **fresh})
            if saved.get('desktopPath'):
                desktop.sync(saved)
            return saved

    # ── 账号 ──────────────────────────────────────────────
    def list_accounts(self) -> list[dict]:
        return [store.public_account(a) for a in store.list_accounts()]

    def import_account(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise PlatformError('账号必须是 JSON 对象', status=400, code='invalid_account')
        raw = payload.get('account') if isinstance(payload.get('account'), dict) else payload
        uid = _pick(raw, 'uid', 'user_id', 'userId')
        if not uid:
            raise PlatformError('缺少 uid', status=400, code='invalid_account')
        token = _pick(raw, 'token', 'accessToken', 'access_token')
        refresh = _pick(raw, 'refreshToken', 'refresh_token')
        if not token and not refresh:
            raise PlatformError('缺少 token 或 refreshToken', status=400,
                                code='invalid_account')

        old = store.read_account(uid) or {}
        expired_raw = raw.get('expiredAt', raw.get('expiresAt', raw.get('expired_at')))
        expired_at = client.iso_from_ms(expired_raw) if isinstance(expired_raw, (int, float)) \
            else (str(expired_raw) if expired_raw not in (None, '') else old.get('expiredAt', ''))

        account = {
            'uid': uid,
            'realm': _normalize_realm_value(raw.get('realm') or old.get('realm')),
            'token': token or old.get('token') or '',
            'refreshToken': refresh or old.get('refreshToken') or '',
            'expiredAt': expired_at or '',
            'refreshExpiredAt': str(
                client.iso_from_ms(raw.get('refreshExpiredAt'))
                if isinstance(raw.get('refreshExpiredAt'), (int, float))
                else (raw.get('refreshExpiredAt') or old.get('refreshExpiredAt') or '')),
            'nickname': _pick(raw, 'nickname', 'name', 'displayName') or old.get('nickname', ''),
            'email': _pick(raw, 'email') or old.get('email', ''),
            # 国际版的地域与主机提示：决定用量接口走 api-us-east 还是
            # api-sg-central（`region` 来自登录 / 客户端导入，可能为空）。
            'region': _pick(raw, 'region', 'aiRegion', 'AIRegion', 'Region')
            or old.get('region', ''),
            'api': _pick(raw, 'api', 'host') or old.get('api', ''),
            'proxy': _pick(raw, 'proxy', 'egress') or old.get('proxy', ''),
            # 来源由调用方显式标注（web 登录 / 本机导入 / 手工粘贴）；
            # 缺省时按 desktopPath 推断，兼容既有导入路径。
            'source': _pick(raw, 'source')
            or ('desktop' if _pick(raw, 'desktopPath') else 'manual'),
            # 来自本机客户端的账号才做「回写」；手工粘贴的 token 不该去改 IDE 的
            # storage.json（路径可能根本不属于这个账号）。
            'desktopPath': _pick(raw, 'desktopPath') or old.get('desktopPath', ''),
            # 国际版刷新令牌要用的设备私钥（`icube-dc`）。默认从本机客户端现读；
            # 服务器部署没有客户端时才需要随账号导入。与 refreshToken 同级敏感，
            # 管理端接口永不返回明文（`public_account` 只给 has_device_key）。
            'deviceKeyPEM': _pick(raw, 'deviceKeyPEM', 'devicePrivateKeyPEM',
                                  'privateKeyPEM') or old.get('deviceKeyPEM', ''),
            # 私钥必须与**同一台设备**的设备号配对：ExchangeToken 的 DeviceInfo
            # 里 DeviceID / MachineID / DevicePublicKey 对不上时上游回 20405。
            # 本机导入不用填（刷新时现读 storage.json）；服务器部署没有客户端，
            # 才需要随账号一起给这三项。
            'deviceId': _pick(raw, 'deviceId', 'device_id', 'holderId')
            or old.get('deviceId', ''),
            'machineId': _pick(raw, 'machineId') or old.get('machineId', ''),
            'devicePublicKeyPEM': _pick(raw, 'devicePublicKeyPEM', 'publicKeyPEM')
            or old.get('devicePublicKeyPEM', ''),
        }
        self._credits.pop(uid, None)
        return store.public_account(store.save_account(account))

    def import_from_desktop(self, *, realm: str | None = None) -> dict:
        """一键导入本机 Trae 客户端里已登录的账号。

        单个账号失败不影响其他账号：客户端可能同时装了国内版与国际版，其中
        一个的 blob 坏掉时，另一个仍然应该能导进来。
        """
        wanted = str(realm or '').strip().lower() or None
        imported: list[dict] = []
        failed: list[dict] = []
        for account in desktop.read_accounts():
            if wanted and account.get('realm') != wanted:
                continue
            account['source'] = 'desktop'
            try:
                imported.append(self.import_account(account))
            except (PlatformError, ValueError) as exc:
                failed.append({'uid': str(account.get('uid') or ''),
                               'error': getattr(exc, 'message', str(exc))})
        return {
            'platform': self.name,
            'sources': [str(e['path']) for e in desktop.storage_paths()],
            'imported': imported,
            'failed': failed,
        }

    def delete_account(self, account_id: str) -> bool:
        self._credits.pop(str(account_id or ''), None)
        return store.delete_account(account_id)

    # ── 网页登录 ──────────────────────────────────────────
    def login_start(self, *, realm: str | None = None) -> dict:
        """发起一次网页登录（本机回环回调），返回 `{login_id, url, expires_in}`。"""
        from . import login as loginmod  # noqa: PLC0415 —— login 反向依赖 client/store
        return loginmod.start(normalize_realm(realm))

    def login_poll(self, login_id: str) -> dict:
        from . import login as loginmod  # noqa: PLC0415
        return loginmod.poll(login_id)

    def login_cancel(self, login_id: str) -> bool:
        from . import login as loginmod  # noqa: PLC0415
        return loginmod.cancel(login_id)

    def login_submit_callback(self, login_id: str, callback: str) -> dict:
        """远程部署时提交浏览器地址栏里的回调 URL（授权页仍只认 127.0.0.1）。"""
        from . import login as loginmod  # noqa: PLC0415
        return loginmod.submit_callback(login_id, callback)

    async def refresh_account(self, account_id: str, *, force: bool = False) -> dict:
        account = self._require(account_id)
        if not force and not store.expired(account):
            return store.public_account(account)
        fresh = await self._ensure_token(account, force=True)
        return store.public_account(fresh)

    def fingerprint(self, account_id: str) -> dict:
        account = self._require(account_id)
        from . import fingerprint

        return fingerprint.summary(str(account.get('uid') or ''))

    # ── 任务 ──────────────────────────────────────────────
    async def checkin(self, account_id: str) -> dict:
        account = self._require(account_id)
        realm = normalize_realm(account.get('realm'))
        if not REALMS[realm]['supports_checkin']:
            raise PlatformError(f'{REALMS[realm]["label"]}不支持每日签到',
                                status=400, code='checkin_unsupported')
        account = await self._ensure_token(account)
        result = await client.claim_checkin(account, realm=realm)
        result.pop('raw', None)
        # 签到会改变积分：让下一次查询重新去上游取，而不是用缓存里的旧值。
        self._credits.pop(str(account.get('uid') or ''), None)
        return {**result, 'uid': str(account.get('uid') or ''), 'realm': realm}

    async def usage(self, account_id: str) -> dict:
        account = self._require(account_id)
        realm = normalize_realm(account.get('realm'))
        account = await self._ensure_token(account)
        out: dict[str, Any] = {
            'uid': str(account.get('uid') or ''),
            'realm': realm,
            'session_usage': None,
            'entitlement': None,
            'errors': [],
        }
        try:
            out['session_usage'] = await client.fetch_usage(account, realm=realm)
        except PlatformError as exc:
            out['errors'].append({'endpoint': 'session_usage', 'code': exc.code,
                                  'message': exc.message})
        try:
            out['entitlement'] = await client.fetch_entitlement(account, realm=realm)
        except PlatformError as exc:
            out['errors'].append({'endpoint': 'entitlement', 'code': exc.code,
                                  'message': exc.message})
        return out

    async def credits(self, account_id: str) -> dict:
        """积分余额：**套餐积分池**和签到奖池分开返回。

        **口径修正（重要）**：以前只读签到接口的 `credits`，而它只是「签到奖池」
        （实测 100），不是账户余额。Trae 客户端右上角显示的几千分来自 v2
        entitlement 的 `usage_summary`（`total_amount` - `consumed_amount`），
        于是出现了「客户端 4700、面板 100」。现在两个都取：

          · `credits` / `remain` / `limit` / `used` —— 套餐积分池，界面主数字；
          · `checkin_credits` / `checked_in` —— 今日签到奖池，单独标注。

        国际版没有签到接口，且额度口径是**美元**而非积分：走网页同款的
        `user_current_entitlement_list`（`basic/bonus_usage_limit` 与
        `basic/bonus_usage_amount`）。两边都取不到时才抛错——返回 0 会被
        读成「积分用完了」。

        结果缓存 60 秒：界面进一次页面就会问全部账号，不缓存等于每刷新一次
        就往上游打一轮请求。签到 / 导入 / 删除会主动让缓存失效。
        """
        account = self._require(account_id)
        uid = str(account.get('uid') or '')
        cached = self._credits.get(uid)
        if cached and cached[0] > time.time():
            return cached[1]

        realm = normalize_realm(account.get('realm'))
        ready = await self._ensure_token(account)

        limit = used = 0.0
        has_quota = False
        expires: list[dict] = []
        checkin_credits: int | None = None
        checked_in = False
        failures: list[PlatformError] = []

        # 「刷新积分」按钮会清缓存，这里只做普通的 60 秒复用
        if realm == 'intl':
            # 国际版是**美元额度**，与国内版积分池不是同一个接口/口径。
            try:
                data = await client.fetch_intl_entitlement(
                    ready, realm=realm, timeout=_CREDITS_TIMEOUT)
                usage = _dollar_usage(data)
                if usage is not None:
                    limit = usage['limit']
                    used = usage['used']
                    has_quota = True
                    if usage['end_at']:
                        expires = [{
                            'amount': round(max(0.0, limit - used), 2),
                            'expire_at': usage['end_at'],
                            'label': usage['label'],
                        }]
            except PlatformError as exc:
                failures.append(exc)
        else:
            try:
                ent = await client.fetch_entitlement(ready, realm=realm,
                                                     timeout=_CREDITS_TIMEOUT)
                ent_usage = ent.get('ent_usage') if isinstance(ent, dict) else None
                summary = ((ent_usage or {}).get('usage_summary')
                           if isinstance(ent_usage, dict) else None)
                if isinstance(summary, dict) and 'total_amount' in summary:
                    limit = _num(summary.get('total_amount'))
                    used = _num(summary.get('consumed_amount'))
                    has_quota = True
                expires = _credit_expiries(ent_usage)
            except PlatformError as exc:
                failures.append(exc)

        if REALMS[realm]['supports_checkin']:
            try:
                status = await client.checkin_status(ready, realm=realm,
                                                     timeout=_CREDITS_TIMEOUT)
                checkin_credits = int(status.get('credits') or 0)
                checked_in = bool(status.get('checked_in'))
            except PlatformError as exc:
                failures.append(exc)

        if not has_quota and checkin_credits is None:
            raise failures[0] if failures else PlatformError(
                '取不到积分余额', status=502, code='credits_unavailable')

        remain = round(max(0.0, limit - used), 2)
        # 国内版积分是整数；国际版是美元，保留小数（0.99 不能截成 0）。
        credits_value = (int(remain) if float(remain).is_integer() else remain) \
            if has_quota else int(checkin_credits or 0)
        out = {
            'uid': uid,
            'realm': realm,
            # 主数字 = 套餐积分池 / 国际版美元额度；没有 entitlement 时才退回签到奖池。
            'credits': credits_value,
            'remain': remain if has_quota else None,
            'limit': limit if has_quota else None,
            'used': used if has_quota else None,
            # 单位随版本变化：国内版是积分，国际版是美元。
            'unit': 'usd' if realm == 'intl' else 'credits',
            'checkin_credits': checkin_credits,
            'checked_in': checked_in,
            'expires': expires,
            'next_expire_at': expires[0]['expire_at'] if expires else None,
            'source': 'entitlement+checkin' if has_quota and checkin_credits is not None
                      else ('entitlement' if has_quota else 'checkin'),
        }
        if failures:
            out['errors'] = [{'code': e.code, 'message': e.message} for e in failures]
        self._credits[uid] = (time.time() + _CREDITS_TTL, out)
        return out

    # ── 模型目录 ──────────────────────────────────────────
    def _cached_models(self) -> list[dict] | None:
        try:
            cached = db.get_setting(SETTING_MODELS, None)
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(cached, dict):
            return None
        if float(cached.get('fetched_at') or 0) + _TTL < time.time():
            return None
        models = cached.get('models')
        if not isinstance(models, list) or not models:
            return None
        return [m for m in models if isinstance(m, dict) and m.get('id')]

    def _save_models(self, models: list[dict]) -> None:
        try:
            db.set_setting(SETTING_MODELS, {'fetched_at': int(time.time()),
                                            'models': models})
        except Exception:  # noqa: BLE001 —— 缓存写不进去不影响本次返回
            pass

    @staticmethod
    def _implicit_live_allowed() -> bool:
        """首次启动（还没有缓存）时允许拉一次线上目录。

        为什么要允许：本地目录里有相当一部分模型对当前账号是**调不通**的
        （上游回 `4023 model is unknown`），客户端照着列表选一个就失败，比
        「列表略少」糟糕得多。只在**完全没有缓存**时尝试，且失败后冷却
        10 分钟，避免每次 `/v1/models` 都等超时。
        """
        try:
            last = float(db.get_setting(SETTING_MODELS_ATTEMPT, 0) or 0)
        except Exception:  # noqa: BLE001
            return False
        return time.time() - last > _IMPLICIT_TTL

    @staticmethod
    def _note_implicit_attempt() -> None:
        try:
            db.set_setting(SETTING_MODELS_ATTEMPT, int(time.time()))
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _from_live(raw: dict) -> dict | None:
        if raw.get('chat') is False:
            return None
        model_id = raw.get('key') or raw.get('name') or raw.get('id')
        if not model_id:
            return None
        # 国内的 `model_list` 只回 `{name, display_name}`（没有上下文长度等），
        # 而本地目录里有这些元数据：按 id 补上，客户端才不会把模型当成 0 上下文。
        known = catalog.find(str(model_id)) or {}
        thinking = raw.get('thinking') if isinstance(raw.get('thinking'), dict) else {}
        return {
            'id': str(model_id),
            'name': str(raw.get('display_name') or raw.get('name')
                        or known.get('name') or model_id),
            'realm': 'cn',
            'context_window': int(raw.get('context_window')
                                  or known.get('context_window') or 0),
            'max_tokens': int(raw.get('max_tokens') or known.get('max_tokens') or 0),
            'multimodal': bool(raw.get('multimodal', known.get('multimodal'))),
            'thinking': bool(thinking.get('supported', known.get('thinking'))),
            # 倍率不在 model_list 里；先留空，合并时按 id 从倍率表回填
            # （见 _live_models / rates）。
            'credit_ratio': None,
            # `model_list` 回来的模型正是 `llm_raw_chat` 认的那批。
            'channel': 'chat',
        }

    @staticmethod
    def _local_ratios() -> dict[str, float]:
        """模型 id（小写）→ 积分消耗倍率。

        倍率有三个来源，优先级与合并规则见 `rates` 模块：面板导入 >
        本机客户端快照 > 内置默认值。读本地 sqlite 是毫秒级，所以每次目录
        请求都现取，用户不用先点「刷新模型」才能看到倍率。
        """
        try:
            return rates.lookup()
        except Exception as exc:  # noqa: BLE001 —— 取不到就当作没有倍率
            logger.warning('读取模型倍率失败：%s', exc)
            return {}

    # ── 倍率表（面板导入 / 导出）─────────────────────────────
    def model_rates(self) -> dict:
        """当前生效的倍率表与各来源条数（面板展示用）。"""
        return rates.summary()

    def import_model_rates(self, payload: object) -> dict:
        """导入一份倍率 JSON（形状见 `rates.normalize`）。"""
        return rates.save_imported(payload)

    def reset_model_rates(self) -> dict:
        """丢弃导入的倍率表，回到客户端快照 + 内置默认值。"""
        return rates.clear_imported()

    def export_model_rates(self) -> dict:
        """把当前合并结果导成可再次导入的 JSON。"""
        return rates.export_payload()

    # ── 模型目录表（面板导入 / 导出）─────────────────────────
    def model_catalog(self) -> dict:
        """当前生效的模型目录与各来源条数（面板展示用）。"""
        return catalog.summary()

    def import_model_catalog(self, payload: object) -> dict:
        """导入一份目录 JSON（形状见 `catalog.normalize`）。"""
        return catalog.save_imported(payload)

    def reset_model_catalog(self) -> dict:
        """丢弃导入的目录，回到客户端快照 + 内置默认值。"""
        return catalog.clear_imported()

    def export_model_catalog(self) -> dict:
        """把当前合并结果导成可再次导入的 JSON。"""
        return catalog.export_payload()

    async def _live_models(self, *, timeout: float = 45) -> list[dict]:
        """拼一次完整目录：导入 > 客户端 `chat_v3` > `model_list` > 内置兜底。

        四个来源各有缺口，缺一不可：

          · **面板导入** —— 用户显式覆盖（服务器部署补模型、改元数据）；
          · **客户端 `chat_v3`** —— Trae 界面里那一长串模型的真实来源。
            `model_list` 只回 5 个老模型，不读本地库就永远看不到新模型；
          · **`model_list`** —— 老模型（`seed_m8` 等）走 IDE 数据面，本地
            快照里没有，也不能漏；
          · **内置兜底** —— 服务器不在客户端那台机器上时，前两者都拿不到。

        同一模型只保留第一次出现的那条，`channel` 也跟着保留——这决定了它
        走 `llm_raw_chat` 还是 Agent 通道。

        **不刷新令牌**：目录是只读信息，不值得为它阻塞一次可能 30 秒的刷新，
        更不该因为「客户端问了一下模型列表」就轮换掉用户的 refreshToken。
        过期的账号直接跳过，交给管理端的「刷新」按钮处理。
        """
        live: list[dict] = []
        for account in store.list_accounts():
            if normalize_realm(account.get('realm')) != 'cn':
                continue
            if store.expired(account):
                continue
            try:
                raw = await client.fetch_model_list(account, realm='cn',
                                                    timeout=timeout)
            except PlatformError:
                continue
            live = [m for m in (self._from_live(x) for x in raw) if m]
            if live:
                break

        local = desktop.read_models('cn')
        # 目录三层（导入 > 客户端快照 > 内置默认值），线上 `model_list` 插在
        # 「快照与兜底之间」：它确实是可调用的真实模型，但信息比客户端少
        # （没有倍率、没有上下文长度）。同一 id 先出现者胜出。
        #
        # 倍率按 id 从合并表覆盖（面板导入 > 客户端快照 > 内置默认值，见
        # rates 模块）——否则用户导入的倍率压不过客户端快照里的旧值，而且
        # 服务器部署只有本地清单里有倍率。
        rate_table = self._local_ratios()
        merged: list[dict] = []
        seen: set[str] = set()
        for group in (catalog.imported(), local, live, catalog.bundled()):
            for model in group:
                model_id = str(model.get('id') or '').strip()
                key = model_id.lower()
                if not key or key in seen:
                    continue
                seen.add(key)
                rate = rate_table.get(key)
                if rate is None:
                    merged.append(model)
                else:
                    merged.append({**model, 'credit_ratio': rate})
        return merged

    @staticmethod
    def _prefer_imported(models: list[dict]) -> list[dict]:
        """把「之后导入的目录」叠在缓存结果之上。

        缓存记录的是上一次合并结果（TTL 6 小时）；用户刚导入一份 JSON 却要等
        「刷新模型」才看得到，会以为导入没生效。导入的条目按 id 覆盖缓存里的
        同 id 条目，缓存里多出来的条目保留——导入是逐条覆盖而不是整表替换，
        与倍率表的口径一致。
        """
        imported = catalog.imported()
        if not imported:
            return models
        by_id = {str(m.get('id') or '').lower(): m for m in models}
        out: list[dict] = []
        seen: set[str] = set()
        for model in imported:
            key = str(model.get('id') or '').lower()
            if not key or key in seen:
                continue
            seen.add(key)
            # 缓存里同 id 的条目带着 credit_ratio 等字段，先展开再被导入覆盖。
            out.append({**by_id.get(key, {}), **model})
        for model in models:
            key = str(model.get('id') or '').lower()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(model)
        return out

    async def list_models(self, *, refresh: bool = False) -> list[dict]:
        """模型目录。

        默认**只读缓存**（发请求会让客户端启动时等上游超时），但缓存为空时
        允许尝试一次线上目录——本地兜底目录里有一部分模型对这个账号根本调
        不通（`4023 model is unknown`），照着它选模型必然失败。
        """
        models = self._cached_models() if not refresh else None
        if models is None and (refresh or self._implicit_live_allowed()):
            live = await self._live_models(
                timeout=45 if refresh else _IMPLICIT_TIMEOUT)
            self._note_implicit_attempt()
            if live:
                self._save_models(live)
                models = live
        if models is None:
            models = catalog.default_models()
        else:
            # 缓存是「上一次合并的结果」，TTL 6 小时。用户刚导入一份目录却要等
            # 「刷新模型」才看得到，会以为导入没生效——把导入表叠上去。
            models = self._prefer_imported(models)
        # 缓存里可能还没有倍率（老版本存下的），也可能用户刚导入了一份新倍率：
        # 统一按当前合并表覆盖一次，不必等用户点「刷新模型」。
        rate_table = self._local_ratios()
        models = [{**m, 'credit_ratio': rate_table.get(
                       str(m.get('id') or '').lower(), m.get('credit_ratio'))}
                  for m in models]
        return [self._public_model(m) for m in models if m.get('id')]

    def _public_model(self, raw: dict) -> dict:
        model_id = str(raw.get('id') or '')
        realm = normalize_realm(raw.get('realm'))
        inputs = ['text']
        if raw.get('multimodal'):
            inputs.append('image')
        ratio = raw.get('credit_ratio')
        return {
            # 目录里每个模型只属于一个区域：直接给出该区域的 ID
            # （`traecn/...` / `traecom/...`）。旧式 `trae/...` 仍可调用。
            'id': self.public_model_id(model_id, _db_realm(realm)),
            'object': 'model',
            'created': 0,
            'owned_by': self.name,
            'name': str(raw.get('name') or model_id),
            'platform': self.name,
            'realm': realm,
            'context_window': int(raw.get('context_window') or 0),
            'max_tokens': int(raw.get('max_tokens') or 0),
            'supports_reasoning': bool(raw.get('thinking')),
            'modalities': {'input': inputs},
            'model_name': model_id,
            # 「积分消耗速度」：同一 prompt 在这个模型上的扣费倍数（客户端显示
            # 成 `{rate} ×`）。None = 未知——不要把 0 当成免费。
            'credit_ratio': float(ratio) if isinstance(ratio, (int, float)) else None,
        }

    @staticmethod
    def _model_realm(model: str) -> str | None:
        found = catalog.find(model)
        return normalize_realm(found['realm']) if found else None

    def realm_of_model(self, model: str) -> str | None:
        """按目录判模型属于哪个版本；目录没有这个模型时返回 None。

        成功调用不用它（`PlatformChat.realm` 才是实际账号的版本）；它只给
        **失败路径**用：调用还没选到账号就报错时，日志至少能按目录落对栏。
        """
        want = self._model_realm(model)
        return _db_realm(want) if want else None

    def _channel(self, model: str) -> str:
        """这个模型该走哪条通道：`chat`（IDE 数据面）还是 `solo`（Agent）。

        查询顺序是「最便宜且最准」在前：运行时探测结果 → 目录（面板导入 /
        内置默认值）→ 目录缓存 → 客户端快照。全都没有时才默认 `chat`
        （老模型的通路），因为猜错的代价只是一次 4023 + 自动换通道重试。
        """
        hit = self._routes.get(model)
        if hit:
            return hit
        found = catalog.find(model)
        if found and found.get('channel'):
            return str(found['channel'])
        target = model.strip().lower()
        for item in self._cached_models() or []:
            if str(item.get('id') or '').strip().lower() == target:
                channel = item.get('channel')
                if channel:
                    return str(channel)
        for item in desktop.read_models('cn'):
            if item['id'].strip().lower() == target:
                return 'solo'
        return 'chat'

    # ── 对话 ──────────────────────────────────────────────
    def _candidates(self, model: str, realm_hint: str | None = None,
                    affinity: str | None = None) -> list[dict]:
        """本次请求的账号顺序：**权重决定分摊，权重相同才轮到 LRU**。

        权重用平滑加权轮询（SWRR）：只推进**被选中**的那个游标，而不是先把
        一整条顺序都算出来——后者会让没被用到的账号游标也漂移，实际比例
        偏离配置值。其余账号按权重降序作为失败重试顺序，权重相同则最近最少
        使用优先（保留升级前的分摊行为）。

        `affinity` 是会话键：命中绑定时沿用上次的账号，**不推进 SWRR 游标**
        （长会话会把全局比例带偏）；绑定账号被冻结 / 冷却 / 删除后重新选路。
        新会话才参与加权，长期分摊仍由权重决定。

        `realm_hint` 来自区域化模型名前缀（`traecn/` / `traecom/`），是**严格**
        约束：指定了区域却一个对应账号都没有时返回空列表，不回退到其他区域——
        回退会让「我只想走国际版」静默变成国内版调用。
        """
        accounts = [a for a in store.list_accounts()
                    if str(a.get('uid') or '').strip()
                    and (a.get('token') or a.get('refreshToken'))
                    and not store.is_disabled(a)
                    and not store.is_cooling(a)]
        if not accounts:
            return []
        if realm_hint:
            want = _normalize_realm_value(realm_hint)
            accounts = [a for a in accounts
                        if normalize_realm(a.get('realm')) == want]
            if not accounts:
                return []
        else:
            want = self._model_realm(model)
            if want:
                matched = [a for a in accounts
                           if normalize_realm(a.get('realm')) == want]
                if matched:
                    accounts = matched

        def uid_of(account: dict) -> str:
            return str(account.get('uid') or '')

        accounts.sort(key=lambda a: self._last_used.get(uid_of(a), 0.0))

        if affinity:
            bound_uid = self._affinity.get(affinity)
            if bound_uid:
                bound = next((a for a in accounts if uid_of(a) == bound_uid), None)
                if bound is not None:
                    rest = [a for a in accounts if a is not bound]
                    rest.sort(key=lambda a: (-_account_weight(a),
                                             self._last_used.get(uid_of(a), 0.0)))
                    return [bound, *rest]
                # 账号被冻结 / 冷却 / 删除：绑定失效，重新按权重选。
                self._affinity.unbind(affinity)

        total = sum(_account_weight(a) for a in accounts)
        first = max(accounts, key=lambda a: (
            self._swrr.get(uid_of(a), 0.0) + _account_weight(a)))
        first_uid = uid_of(first)
        self._swrr[first_uid] = (self._swrr.get(first_uid, 0.0)
                                 + _account_weight(first) - total)
        rest = [a for a in accounts if a is not first]
        rest.sort(key=lambda a: (-_account_weight(a),
                                 self._last_used.get(uid_of(a), 0.0)))
        # 先绑定再调用：同一会话并发发出的两条请求也应落同一账号，等结果回来
        # 才绑定就晚了；失败换号成功时 `_chat_accounts` 会改绑。
        if affinity:
            self._affinity.bind(affinity, first_uid)
        return [first, *rest]

    async def chat(self, model: str, body: dict,
                   ctx: ChatContext | None = None) -> PlatformChat:
        """池级并发闸门 + 一次真实的对话调用。

        闸门**必须占到这个请求结束**：非流式在聚合完释放，流式在 `closer`
        里释放（客户端断开也会走到 `PlatformChat.aclose`）。在 `chat()`
        返回时就释放会让上限形同虚设——那时流才刚开始。

        `ctx` 是本签名的公共参数（见 `platforms/base.py`）。Trae 用不上它：
        选哪个账号由自己的池决定，与调用方是谁无关——收下并忽略即可。
        """
        lease = await self._limiter.acquire()
        if lease is False:
            raise PlatformError(
                'Trae 并发已满（平台池级上限），请稍后重试',
                status=429, code='pool_busy', retry_after=1)
        released = False

        def release() -> None:
            nonlocal released
            if released:
                return
            released = True
            self._limiter.release(lease)

        try:
            result = await self._chat_accounts(model, body,
                                               realm_hint=ctx.realm if ctx else None)
        except BaseException:
            release()
            raise
        if result.stream is None:
            release()
            return result

        inner = result.closer

        async def closer() -> None:
            try:
                if inner is not None:
                    await inner()
            finally:
                release()

        result.closer = closer
        return result

    async def _chat_accounts(self, model: str, body: dict,
                             realm_hint: str | None = None) -> PlatformChat:
        wrapper_model = f'{self.model_prefix}{model}'
        # 同一会话粘同一账号：上游 prompt cache 按账号隔离（见 session_affinity）。
        affinity = affinity_key(body)
        candidates = self._candidates(model, realm_hint, affinity)
        if not candidates:
            where = '（指定区域）' if realm_hint else ''
            raise PlatformError(
                f'没有可用的 Trae 账号{where}（请先导入并刷新令牌）',
                status=503, code='no_account')
        preferred = self._channel(model)
        last_error: PlatformError | None = None
        for account in candidates[:_MAX_ACCOUNTS_TRY]:
            uid = str(account.get('uid') or '')
            # Agent 通道只有国内版有；国际版账号只能走 IDE 数据面。
            order = [preferred]
            if normalize_realm(account.get('realm')) == 'cn':
                order.append('solo' if preferred == 'chat' else 'chat')
            for channel in order:
                try:
                    ready = await self._ensure_token(account)
                    result = await self._chat_once(ready, model, body,
                                                   wrapper_model, channel=channel)
                except PlatformError as exc:
                    last_error = exc
                    # 只有「凭据不可用」才换号；429/5xx 换号往往只是把限流扩散到更多账号
                    if exc.status in (401, 403):
                        break
                    # 通道不对（`[4023] model is unknown`）：换另一条通道再试一次，
                    # 并把结论记住，后续同模型请求不再白跑一次失败。
                    if _is_model_unknown(exc) and channel != order[-1]:
                        continue
                    # 账号 / 上游类错误：解绑，避免同一会话每轮都先撞同一个坏号。
                    # 请求本身的问题（400 等）保留绑定——账号没坏，下次还该走它。
                    if affinity and (exc.status in (401, 403, 429)
                                     or exc.status >= 500):
                        self._affinity.unbind(affinity)
                    raise
                if channel != preferred:
                    self._routes[model] = channel
                self._last_used[uid] = time.time()
                # 换号成功后改绑：否则这个会话每轮都会先撞一次坏号。
                if affinity:
                    self._affinity.bind(affinity, uid)
                return result
        # 试过的号都不可用：解绑，下一轮重新按权重选，不继续撞同一个坏号。
        if affinity:
            self._affinity.unbind(affinity)
        raise last_error or PlatformError('Trae 账号均不可用', status=503,
                                          code='no_account')

    # ── 账号调度参数（权重）──────────────────────────────
    def set_account_options(self, account_id: str, *, weight=None,
                            concurrency=None) -> dict:
        """只改调度参数，不动凭据。

        单账号并发上限**不支持**：并发是平台池级的（`platforms.trae.max_in_flight`）。
        给每个账号各设一个数字在账号很多时等于没有上限，而且与 WorkBuddy 的
        `pool.max_in_flight` 语义不一致。
        """
        if concurrency is not None:
            raise PlatformError(
                'Trae 只支持平台池级并发上限，不支持单账号并发',
                status=400, code='unsupported_option')
        if weight is None:
            raise PlatformError('没有要修改的项', status=400, code='nothing_to_update')
        try:
            return store.set_weight(account_id, weight)
        except FileNotFoundError as exc:
            raise PlatformError('账号不存在', status=404,
                                code='account_not_found') from exc
        except ValueError as exc:
            raise PlatformError(str(exc), status=400, code='invalid_weight') from exc

    def set_account_disabled(self, account_id: str, disabled: bool,
                             reason: str = '') -> dict:
        """临时冻结 / 恢复：只改账号文件里的调度位，不动凭据。"""
        try:
            return store.set_disabled(account_id, bool(disabled), reason)
        except FileNotFoundError as exc:
            raise PlatformError('账号不存在', status=404,
                                code='account_not_found') from exc
        except ValueError as exc:
            raise PlatformError(str(exc), status=400,
                                code='invalid_account_state') from exc

    def set_account_cooling(self, account_id: str, until: float,
                            reason: str = '') -> dict:
        try:
            return store.set_cooling(account_id, until, reason)
        except FileNotFoundError as exc:
            raise PlatformError('账号不存在', status=404,
                                code='account_not_found') from exc
        except ValueError as exc:
            raise PlatformError(str(exc), status=400,
                                code='invalid_cooling') from exc

    def clear_account_cooling(self, account_id: str) -> dict:
        try:
            return store.clear_cooling(account_id)
        except FileNotFoundError as exc:
            raise PlatformError('账号不存在', status=404,
                                code='account_not_found') from exc
        except ValueError as exc:
            raise PlatformError(str(exc), status=400,
                                code='invalid_cooling') from exc

    def invalidate_limit(self) -> None:
        """池级上限改了之后立刻生效，不用等 5 秒缓存。"""
        self._limiter.invalidate()

    # ── 每请求实扣积分（TF_TRAE_CREDIT_METER）────────────────
    async def _meter_start(self, account: dict) -> _CreditMeter | None:
        """请求前准备计量凭据；这次不做归属时返回 None。

        两种口径（`TF_TRAE_CREDIT_METER` 的取值决定）：

        · `delta`（`1/true/on`）：前后各查一次账户累计求增量。**要抢一把账号
          级锁**，并发在途时分不清归属，只能跳过后来的请求。
        · `detail`：给这次请求生成一个 `session_id`，结束后按它去「用量明细」
          （`query_user_usage_group_by_session`，`usage_type=7`）里取这一条的
          `amount_float`。**并发安全**，而且是上游逐条的扣费记录；实测落账是
          同步的（流结束后首查即中），所以这里**不查基线、不抢锁**，请求前
          零上游开销。
        """
        mode = config.TRAE_CREDIT_METER
        if not mode:
            return None
        if str(mode).strip().lower() == 'detail':
            # 明细模式：只要一个会话号，请求前不打任何上游接口。
            return _CreditMeter(None, None, secrets.token_hex(12),
                                int(time.time()) - 2)
        uid = str(account.get('uid') or '')
        token: object = object()
        if uid and self._metering.get(uid):
            return None
        if uid:
            self._metering[uid] = token
        try:
            before = await client.fetch_consumed(account)
        except PlatformError:
            before = None
        if before is None:
            self._release_meter(uid, token)
            return None
        # 往前留 2 秒：真发出去时上游记的是服务端收到的时刻。
        return _CreditMeter(token, before, None, int(time.time()) - 2)

    def _release_meter(self, uid: str, token: object | None) -> None:
        """只释放自己那把锁（并发下 A 的收尾不能删掉 B 的锁）。"""
        if uid and token is not None and self._metering.get(uid) is token:
            self._metering.pop(uid, None)

    async def _meter_finish(self, account: dict, meter: _CreditMeter | None,
                            usage: dict | None) -> None:
        """请求结束后把本次实付积分写进 `usage.credit`（网关据此记账）。

        `usage` 是 `StreamState.usage` 同一个 dict：非流式时它已经被塞进返回体，
        流式时网关在流结束后才读它——两种形态都能带上这个值。

        明细模式：**一次**查询（内部最多 3 次极短重试，合计 <1 秒），没有基线
        也就没有增量兜底——查不到就不写，宁可这一条空着也不写个错的。
        增量模式：查一次累计，与基线相减。
        """
        if meter is None:
            return
        uid = str(account.get('uid') or '')
        credit: float | None = None
        try:
            if meter.session_id:
                row = await client.fetch_request_credit(account, meter.session_id,
                                                        meter.started)
                if row:
                    value = row.get('amount_float')
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        credit = round(float(value), 6)
            elif meter.before is not None:
                after = await client.fetch_consumed(account)
                if after is not None and after >= meter.before:
                    delta = round(after - meter.before, 4)
                    if delta > 0:
                        credit = delta
            if credit is not None and credit >= 0 and usage is not None:
                usage['credit'] = credit
        except PlatformError:
            pass
        finally:
            self._release_meter(uid, meter.token)

    def _meter_abort(self, account: dict, meter: _CreditMeter | None) -> None:
        """请求在拿到流之前就失败时，把计量锁释放掉。"""
        if meter is not None:
            self._release_meter(str(account.get('uid') or ''), meter.token)

    async def _chat_once(self, account: dict, model: str, body: dict,
                         wrapper_model: str, *, channel: str) -> PlatformChat:
        # 这次实际用的是哪个版本的账号：统计/日志必须按它归档，而不是按模型名
        # ——`trae/<model>` 没有 `global:` 前缀，按名字只会永远落到国内版。
        realm = _db_realm(account.get('realm'))
        # 判断依据是**客户端**声明了什么：关掉工具透传（`TRAE_NATIVE_TOOLS=0`）
        # 之后 body 里已经没有 tools，但客户端这一轮仍然在有工具的前提下对话，
        # 正文里的调用照样该还原。
        declared_tools = bool(body.get('tools'))
        # 标签形态的还原需要「客户端到底声明了哪些工具」当白名单；要在
        # `TRAE_NATIVE_TOOLS=0` 剥掉 body 之前取。
        rescue_names = client.tool_names(body.get('tools')) if declared_tools else []
        if not config.TRAE_NATIVE_TOOLS:
            # 总开关：确认上游行为变化后可以整体关掉工具透传，不必回滚代码。
            # 历史里的工具痕迹仍会折成正文（那是模型能看懂的纯文本，不属于
            # 这条通道的「工具字段」）。
            body = {key: value for key, value in body.items()
                    if key not in ('tools', 'tool_choice')}
        meter = await self._meter_start(account)
        session_id = meter.session_id if meter is not None else None
        try:
            if channel == 'solo':
                payload = client.build_solo_body(model, body.get('messages'), body,
                                                 session_id=session_id)
                response = await client.open_solo_chat(account, payload)
                events = client.iter_solo_events(response)
            else:
                payload = client.build_chat_body(model, body.get('messages'), body,
                                                 session_id=session_id)
                response = await client.open_chat(account, payload)
                events = client.iter_events(response)
            stream = bool(body.get('stream'))

            # 先读一帧：上游的鉴权/风控错误通常就是第一帧。此时还没向客户端写
            # 任何字节，可以干净地改成 JSON 错误体，而不是发出半条流再断掉。
            prefetched: list[tuple[str, Any]] = []
            try:
                first = await events.__anext__()
            except StopAsyncIteration:
                first = None
            if first is not None:
                if first[0] == 'error':
                    await response.aclose()
                    raise chatmod.error_from_event(first[1])
                prefetched.append(first)
        except BaseException:
            self._meter_abort(account, meter)
            raise

        async def chained():
            for item in prefetched:
                yield item
            async for item in events:
                yield item

        state = chatmod.StreamState()
        # 只有客户端真的声明了 tools，正文里的调用才可能是「模型想调工具」；
        # 没声明工具的请求按纯文本处理，不做任何改写。
        rescue = config.TRAE_TEXT_TOOL_RESCUE and declared_tools
        rescue_tags = rescue_names if (rescue and config.TRAE_TEXT_TOOL_TAGS) else ()
        if not stream:
            try:
                result = await chatmod.aggregate(
                    chained(), model=wrapper_model, state=state,
                    reasoning_as_content=config.TRAE_REASONING_AS_CONTENT,
                    text_tool_rescue=rescue, tool_names=rescue_tags,
                    strict_tail=config.TRAE_RESCUE_TAIL_STRICT)
            except BaseException:
                # 非流式在聚合阶段报错（含退化工具调用）也要释放计量锁，
                # 否则 delta 口径下这个账号会一直显示「有请求在途」。
                self._meter_abort(account, meter)
                raise
            finally:
                await response.aclose()
            await self._meter_finish(account, meter, state.usage)
            return PlatformChat(payload=result, usage=state.usage,
                                model=wrapper_model,
                                account_hint=str(account.get('uid') or ''),
                                realm=realm, error=state.error)

        generator = chatmod.translate(chained(), model=wrapper_model, state=state,
                                      reasoning_as_content=config.TRAE_REASONING_AS_CONTENT,
                                      text_tool_rescue=rescue, tool_names=rescue_tags,
                                      strict_tail=config.TRAE_RESCUE_TAIL_STRICT)

        if meter is not None:
            inner = generator

            async def metered():  # noqa: ANN202 —— 只有一个用途，不值得抽公共件
                """在流真正结束（或客户端断开）后再补一次积分查询。

                `usage` 要在**收尾时**才写：网关是流结束后才 `_record`，所以
                不会漏；客户端收到的末帧里没有这个值（它本来也拿不到扣费）。
                """
                try:
                    async for chunk in inner:
                        yield chunk
                finally:
                    try:
                        await inner.aclose()  # type: ignore[attr-defined]
                    finally:
                        await self._meter_finish(account, meter, state.usage)

            generator = metered()

        async def closer() -> None:
            try:
                await generator.aclose()  # type: ignore[attr-defined]
            finally:
                await response.aclose()
                self._meter_abort(account, meter)

        return PlatformChat(stream=generator, usage=state.usage, model=wrapper_model,
                            closer=closer, account_hint=str(account.get('uid') or ''),
                            realm=realm)


def backfill_realm_stats() -> dict:
    """一次性纠正历史上「国际版调用被记成国内版」的用量归属。

    成因：网关记 `realm` 时只按模型名前缀判（`global:`），而 Trae 的模型名是
    `trae/<model>`，永远不带前缀——国际版账号的调用因此全落进了国内版。
    修复后的新调用会带上实际账号的版本（`PlatformChat.realm`），这里只管旧数据。

    只跑一次（`SETTING_REALM_BACKFILL`）：修完之后的调用已经是对的，重复扫
    整张日志表只是拖慢启动。失败也不影响启动——统计暂时不准，但服务照常可用。
    """
    try:
        if db.get_setting(SETTING_REALM_BACKFILL, False):
            return {}
        pairs = []
        for account in store.list_accounts():
            uid = str(account.get('uid') or '').strip()
            if uid:
                pairs.append(('trae', uid, _db_realm(account.get('realm'))))
        result = db.reassign_platform_realms(pairs) if pairs else {}
        db.set_setting(SETTING_REALM_BACKFILL, True)
        return result
    except Exception as exc:  # noqa: BLE001 —— 旁路迁移，失败不该挡住启动
        logger.warning('Trae 历史用量版本归属回填失败: %s', exc)
        return {}


adapter = TraeAdapter()
