"""从本机 Trae 客户端导入登录态（并保持两边同步）。

痛点：Trae 没有「导出账号」入口，登录态只存在于 VS Code 系的
`User/globalStorage/storage.json` 里，值还是加密的（见 `icube.py`）。
让用户手工抄 token 既反人类也容易把凭据贴到聊天窗口里——所以做成
「一键导入本机账号」，凭据只从磁盘读到内存再落进 `data/platforms/...`。

**双向同步是必需的，不是锦上添花**：Trae 的 refreshToken 是轮换式的，
我们刷新一次就会作废客户端手里的那份，用户下次打开 IDE 会被登出；
反过来，用户先打开 IDE 刷新了，我们手里的旧 token 再刷就是 401。
所以：

  · 刷新成功后，把新 token 写回客户端的 `storage.json`（原子替换 + 备份）；
  · 刷新前先看一眼客户端里是不是已经有更新的 token，有就直接采用。

写回永远只碰 `iCubeAuthInfo://icube.cloudide` 这一个键，且先备份
`storage.json.bak-trae-gateway`；任何一步失败都只记日志，绝不让
「同步没成功」把刷新结果也一起丢掉。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

from . import icube

logger = logging.getLogger('workbuddy.platforms.trae.desktop')

AUTH_KEY = 'iCubeAuthInfo://icube.cloudide'
# 国际版刷新令牌要用的设备私钥（见 deviceproof.py）；与登录态分开存放，
# 同一个 storage.json 里可能有多个 holder（每个账号一把）。
DEVICE_KEY_PREFIX = 'iCubeAuthInfo://icube-dc'
BACKUP_SUFFIX = '.bak-trae-gateway'
_REALM_DIRS = (('Trae CN', 'cn'), ('Trae', 'intl'))


def _appdata_dirs() -> list[Path]:
    """各平台的配置根目录（Trae / Trae CN）。"""
    home = Path.home()
    if sys.platform == 'win32':
        base = Path(os.environ.get('APPDATA') or home / 'AppData' / 'Roaming')
        return [base]
    if sys.platform == 'darwin':
        return [home / 'Library' / 'Application Support']
    return [Path(os.environ.get('XDG_CONFIG_HOME') or home / '.config')]


def storage_paths() -> list[dict]:
    """返回 `[{path, realm}]`：本机存在的 storage.json。

    `TRAE_DESKTOP_DIRS` 可覆盖（测试与便携版安装用），写法同 PATH。
    """
    roots: list[tuple[Path, str]] = []
    extra = os.environ.get('TRAE_DESKTOP_DIRS') or ''
    for part in extra.split(os.pathsep):
        part = part.strip()
        if part:
            roots.append((Path(part), ''))
    if not roots:
        for base in _appdata_dirs():
            for name, realm in _REALM_DIRS:
                roots.append((base / name, realm))

    out: list[dict] = []
    for root, realm in roots:
        path = root / 'User' / 'globalStorage' / 'storage.json'
        if not path.is_file():
            continue
        if not realm:
            low = str(root).lower()
            realm = 'cn' if ('trae cn' in low or 'trae-cn' in low) else 'intl'
        out.append({'path': path, 'realm': realm})
    return out


def _read_storage(path: Path) -> dict:
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(data, dict):
        raise ValueError(f'{path} 不是对象')
    return data


def _account_from_body(body: dict, hint: str) -> dict:
    """把解密后的客户端结构映射成我们账号文件的结构（**不落 key material**）。"""
    inner = body.get('account') if isinstance(body.get('account'), dict) else {}
    uid = str(body.get('userId') or body.get('uid')
              or inner.get('userId') or inner.get('uid') or '').strip()
    region = body.get('userRegion') if isinstance(body.get('userRegion'), dict) else {}
    region_code = str(region.get('region') or region.get('_aiRegion') or '').upper()
    host = str(body.get('host') or '')
    if not region_code and 'coreva-' in host:
        region_code = 'US'
    elif not region_code and 'coresg-' in host:
        region_code = 'SG'
    if region_code == 'CN' or 'trae.com.cn' in host or 'mchost.guru' in host \
            or str(inner.get('storeCountryCode') or '').lower() == 'cn':
        realm = 'cn'
    elif region_code or host:
        realm = 'intl'
    else:
        realm = hint or 'cn'
    return {
        'uid': uid,
        'realm': realm,
        'region': region_code,
        'api': host,
        'token': str(body.get('token') or ''),
        'refreshToken': str(body.get('refreshToken') or ''),
        'expiredAt': str(body.get('expiredAt') or ''),
        'refreshExpiredAt': str(body.get('refreshExpiredAt') or ''),
        'nickname': str(inner.get('username') or body.get('nickname') or ''),
        'email': str(inner.get('email') or body.get('email') or ''),
        'source': 'desktop',
    }


def read_accounts() -> list[dict]:
    """读出本机所有已登录的 Trae 账号（含明文凭据，仅供导入流程使用）。"""
    accounts: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for entry in storage_paths():
        path: Path = entry['path']
        try:
            storage = _read_storage(path)
        except (OSError, ValueError) as exc:
            logger.warning('读取 Trae 客户端配置失败（%s）：%s', path, exc)
            continue
        for key, value in storage.items():
            if not isinstance(key, str) or not key.startswith('iCubeAuthInfo://'):
                continue
            if not key.endswith('icube.cloudide'):
                # usertag 等键不是登录态；硬解只会得到「格式错误」的噪音
                continue
            try:
                body, _ = icube.decode(value)
            except ValueError as exc:
                logger.warning('解密 Trae 登录态失败（%s）：%s', path, exc)
                continue
            account = _account_from_body(body, entry['realm'])
            if not account['uid'] or not account['token']:
                continue
            dedupe = (account['realm'], account['uid'])
            if dedupe in seen:
                continue
            seen.add(dedupe)
            account['desktopPath'] = str(path)
            accounts.append(account)
    return accounts


def read_devices() -> list[dict]:
    """读出本机客户端的**设备证明私钥**（国际版刷新用）。

    与 `read_accounts` 同源同封装：`iCubeAuthInfo://icube-dc:<holderId>` 解密后
    是 `{privateKeyPEM, publicKeyPEM}`。返回值只允许喂给 `deviceproof`，
    任何日志 / 接口响应都不得包含私钥（`public_account` 也不暴露该字段）。
    """
    devices: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for entry in storage_paths():
        path: Path = entry['path']
        try:
            storage = _read_storage(path)
        except (OSError, ValueError):
            continue
        for key, value in storage.items():
            if not isinstance(key, str) or not key.startswith(DEVICE_KEY_PREFIX):
                continue
            holder = key.split(':', 2)[-1].strip()
            try:
                body, _ = icube.decode(value)
            except ValueError:
                logger.warning('解密 Trae 设备私钥失败（%s）', path)
                continue
            pem = str((body or {}).get('privateKeyPEM') or '')
            if not pem:
                continue
            dedupe = (entry['realm'], holder)
            if dedupe in seen:
                continue
            seen.add(dedupe)
            devices.append({
                'realm': entry['realm'],
                'holderId': holder,
                # 客户端 DeviceInfo.MachineID 用的是 telemetry.machineId，
                # 不是按 uid 派生的；与私钥放在一起返回，调用方不用再读盘。
                'machineId': str(storage.get('telemetry.machineId') or ''),
                'privateKeyPEM': pem,
                'publicKeyPEM': str((body or {}).get('publicKeyPEM') or ''),
                'source': str(path),
            })
    return devices


# ── 客户端本地模型清单 ──────────────────────────────────────
# 客户端把账号能看到的模型按「场景分组」缓存在 state.vscdb：
#   <uid>_AI.agent.model.model_list_map = {chat_v3: [...], solo_agent: [...], ...}
# 这才是 Trae 界面里那一长串模型的来源。`model_list` 接口只回
# `llm_raw_chat` 认的老模型（实测只有 5 个），两者因此差得很远。
MODEL_KEY_SUFFIX = '_AI.agent.model.model_list_map'
MODEL_GROUP = 'chat_v3'


def _state_db(realm: str) -> Path | None:
    """客户端 `state.vscdb` 路径（不存在返回 None）。"""
    folder = 'Trae CN' if str(realm).lower() != 'intl' else 'Trae'
    roots: list[Path] = []
    extra = os.environ.get('TRAE_DESKTOP_DIRS') or ''
    for part in extra.split(os.pathsep):
        part = part.strip()
        if part:
            roots.append(Path(part))
    if not roots:
        roots = [base / folder for base in _appdata_dirs()]
    for root in roots:
        path = root / 'User' / 'globalStorage' / 'state.vscdb'
        if path.is_file():
            return path
    return None


def _consumption_rate(raw: dict) -> float | None:
    """客户端条目里的「积分消耗速度」（每模型倍率）。

    形状：`features.consumption_rate = {"enable": true, "data": {"rate": 0.06}}`，
    客户端 UI 就渲染成「积分消耗速度 0.06 ×」。这是**客户端侧**的权威来源，
    优先级高于随代码发布的内置默认值（见 `rates` 模块）。

    注意：服务端 `batch_get_detail_param` 也会返回每模型 rate，但那是「配置明细」
    接口，和客户端拉模型清单的通道不同源（详见 `rates` 模块），目录落后一代、
    同一模型的倍率也可能不同，所以不用它当主来源；`model_list` 则完全不带
    `features`。

    取不到返回 `None` 而不是 0：0 会被界面读成「这个模型不扣积分」，而真实
    情况是「不知道」。
    """
    features = raw.get('features')
    if not isinstance(features, dict):
        return None
    node = features.get('consumption_rate')
    if not isinstance(node, dict) or node.get('enable') is False:
        return None
    data = node.get('data')
    rate = data.get('rate') if isinstance(data, dict) else node.get('rate')
    if isinstance(rate, bool) or not isinstance(rate, (int, float)):
        return None
    return float(rate)


def _client_model(raw: dict, realm: str) -> dict | None:
    """客户端原始条目 → 我们目录的形状（只取渲染需要的字段）。"""
    if not isinstance(raw, dict):
        return None
    if raw.get('status') is False or raw.get('selectable') is False:
        return None
    model_id = str(raw.get('key') or raw.get('name') or '').strip()
    if not model_id:
        return None
    size = raw.get('context_window_size')
    size = size if isinstance(size, dict) else {}
    context = raw.get('context_window') or size.get('default')
    if not context:
        maximum = size.get('max')
        if isinstance(maximum, list) and maximum:
            context = maximum[0]
        elif isinstance(maximum, (int, float)):
            context = maximum
    return {
        'id': model_id,
        'name': str(raw.get('display_name') or model_id),
        'realm': realm,
        'context_window': int(context or 0),
        'max_tokens': int(raw.get('max_tokens') or 0),
        'multimodal': bool(raw.get('multimodal')),
        'thinking': bool(raw.get('thinking_enable')
                         or raw.get('model_type') == 'reasoning_model'),
        # 积分消耗倍率（同一 prompt 在这个模型上的扣费倍数）。
        # 只有客户端快照带这个字段，见 `_consumption_rate`。
        'credit_ratio': _consumption_rate(raw),
        # 客户端聊天模型：国内版走 Agent 通道（raw_chat 对它们一律 4023），
        # 国际版没有 Agent 通道，只能走 IDE 数据面。
        'channel': 'solo' if realm == 'cn' else 'chat',
    }


def read_models(realm: str = 'cn') -> list[dict]:
    """读客户端缓存的 `chat_v3` 模型清单（只读；任何失败都返回空）。

    为什么值得读本地库：官方 `model_list` 只回 raw_chat 能跑的老模型，
    而客户端界面用的是这份 `chat_v3` 快照。不读它，网关永远只能看到 5 个
    模型，而用户在自己的 Trae 里看到的是 18 个以上。
    """
    path = _state_db(realm)
    if path is None:
        return []
    try:
        uri = 'file:' + path.as_posix() + '?mode=ro'
        con = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            rows = con.execute(
                'select value from ItemTable where key like ?',
                ('%' + MODEL_KEY_SUFFIX,)).fetchall()
        finally:
            con.close()
    except (OSError, sqlite3.Error) as exc:
        logger.warning('读取客户端模型清单失败（%s）：%s', path, exc)
        return []

    groups: list[list] = []
    preferred: list | None = None
    for (value,) in rows:
        text = value.decode('utf-8', 'replace') if isinstance(value, bytes) else str(value)
        try:
            data = json.loads(text)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        for name, group in data.items():
            if not isinstance(group, list) or not group:
                continue
            groups.append(group)
            # 每个账号的 key 前缀不同（`<uid>_` 与 `<uid>:`），所以按组名挑
            # 「聊天」那一份，而不是按 key。
            if name == MODEL_GROUP:
                preferred = group
    if not groups:
        return []
    best = preferred or max(groups, key=len)

    # 倍率按模型 id 汇总**所有分组**：同一个模型在 `chat_v3` 与 `solo_agent`
    # 里的 rate 是一致的，但某些模型只出现在某个分组里（如 `solo_coder`），
    # 只看 chat_v3 会让它们缺倍率。
    rates: dict[str, float] = {}
    for group in groups:
        for item in group:
            if not isinstance(item, dict):
                continue
            key = str(item.get('key') or item.get('name') or '').strip().lower()
            rate = _consumption_rate(item)
            if key and rate is not None:
                rates.setdefault(key, rate)

    out = [_client_model(m, realm) for m in best]
    models = [m for m in out if m]
    for model in models:
        if model.get('credit_ratio') is None:
            model['credit_ratio'] = rates.get(model['id'].lower())
    return models


def find(account: dict) -> dict | None:
    """在当前客户端的 storage.json 里找回这个账号的解密结构。"""
    uid = str((account or {}).get('uid') or '').strip()
    if not uid:
        return None
    candidates: list[Path] = []
    stored = str((account or {}).get('desktopPath') or '').strip()
    if stored:
        candidates.append(Path(stored))
    candidates.extend(Path(e['path']) for e in storage_paths())
    for path in candidates:
        try:
            storage = _read_storage(path)
        except (OSError, ValueError):
            continue
        value = storage.get(AUTH_KEY)
        if not isinstance(value, str):
            continue
        try:
            body, key_material = icube.decode(value)
        except ValueError:
            continue
        if str(body.get('userId') or '').strip() != uid:
            continue
        return {'path': path, 'body': body, 'keyMaterial': key_material}
    return None


def fresher(account: dict) -> dict | None:
    """客户端里的 token 与账号文件不同 → 返回客户端那份（它才是最新的事实）。

    只在确实不同且未过期时返回：客户端可能是旧版残留，不能无条件覆盖。
    """
    found = find(account)
    if not found:
        return None
    current = str((account or {}).get('token') or '')
    body = found['body']
    token = str(body.get('token') or '')
    if not token or token == current:
        return None
    return {
        'token': token,
        'refreshToken': str(body.get('refreshToken') or
                            (account or {}).get('refreshToken') or ''),
        'expiredAt': str(body.get('expiredAt') or (account or {}).get('expiredAt') or ''),
        'refreshExpiredAt': str(body.get('refreshExpiredAt') or
                                (account or {}).get('refreshExpiredAt') or ''),
    }


def sync(account: dict) -> dict:
    """把刷新后的凭据写回客户端，保持 IDE 与网关都能继续刷新。

    返回结构化结果用于日志；**失败不抛**——调用方是刷新令牌的热路径，
    同步失败只意味着用户下次打开 IDE 需要重新登录，不应该让本次刷新失败。
    """
    uid = str((account or {}).get('uid') or '').strip()
    token = str((account or {}).get('token') or '')
    if not uid or not token:
        return {'ok': False, 'reason': 'no_token'}
    found = find(account)
    if not found:
        return {'ok': False, 'reason': 'no_desktop_storage'}
    path: Path = found['path']
    body = dict(found['body'])
    body['token'] = token
    if account.get('refreshToken'):
        body['refreshToken'] = str(account['refreshToken'])
    for key in ('expiredAt', 'refreshExpiredAt'):
        if account.get(key):
            body[key] = str(account[key])
    try:
        storage = _read_storage(path)
        storage[AUTH_KEY] = icube.encode(body, found['keyMaterial'])
        backup = path.with_name(path.name + BACKUP_SUFFIX)
        try:
            shutil.copy2(path, backup)
        except OSError:
            pass
        fd, tmp = tempfile.mkstemp(prefix='.trae-storage-', suffix='.json',
                                   dir=str(path.parent))
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                json.dump(storage, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as exc:  # noqa: BLE001 —— 同步失败不该影响刷新结果
        logger.warning('回写 Trae 客户端登录态失败（%s）：%s', path, exc)
        return {'ok': False, 'reason': 'write_failed', 'error': str(exc)}
    return {'ok': True, 'path': str(path)}
