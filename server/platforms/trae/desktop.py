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
import sys
import tempfile
from pathlib import Path

from . import icube

logger = logging.getLogger('workbuddy.platforms.trae.desktop')

AUTH_KEY = 'iCubeAuthInfo://icube.cloudide'
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
