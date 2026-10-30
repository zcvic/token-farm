"""Trae 设备指纹：全部由 uid 单向派生。

为什么不用随机值：官方客户端在同一个账号上长期复用同一组设备头，跨账号绝不
共享。随机设备号会让账号的历史设备画像每次调用都断裂，是最容易被风控标记的
特征之一。这里所有派生都是纯函数——同一 uid 恒等，不同 uid 互异。

参考实现：`trae2api-hub/trae_fingerprint.py`（MIT）。这里只保留数据面真正
需要的 5 个字段（X-Device-*），不包含其看板的虚拟机检测逻辑。
"""
from __future__ import annotations

import functools
import hashlib
import os
import urllib.parse
import uuid


def machine_id(uid: str) -> str:
    """64 位十六进制的 machineId（与官方客户端同形）。"""
    seed = 'trae-machine:%s' % (str(uid or '').strip() or 'anonymous')
    return hashlib.sha256(seed.encode('utf-8')).hexdigest()


def device_id(uid: str) -> str:
    """数值型 X-Device-Id。

    官方客户端直接复用账号 userId（19 位数字串）。导入的第三方账号 uid 未必
    是纯数字，此时从 sha256 稳定取 19 位数字，**不要**退化成随机设备号。
    """
    raw = str(uid or '').strip()
    if raw.isdigit():
        return raw
    digest = int(hashlib.sha256(('trae-device:%s' % (raw or 'anonymous'))
                               .encode('utf-8')).hexdigest()[:16], 16)
    return str(10 ** 18 + digest % (9 * 10 ** 18))


@functools.lru_cache(maxsize=1)
def _hardware_brand() -> str:
    """Windows 厂商 + 机型（官方样例 'TX Gaming FX608JMR_FX608JMR'）。"""
    if os.name != 'nt':
        return 'Default string'
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r'HARDWARE\DESCRIPTION\System\BIOS') as key:
            maker = str(winreg.QueryValueEx(key, 'SystemManufacturer')[0] or '').strip()
            model = str(winreg.QueryValueEx(key, 'SystemProductName')[0] or '').strip()
        return ' '.join(x for x in (maker, model) if x).strip() or 'Default string'
    except Exception:  # noqa: BLE001 —— 读不到注册表只是少了点"像真机"的信息
        return 'Default string'


@functools.lru_cache(maxsize=1)
def _os_version() -> str:
    """Windows 营销名 + 版本（官方样例 'Windows 11 Pro'）。"""
    if os.name != 'nt':
        return 'Linux'
    product, edition = 'Windows', ''
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r'SOFTWARE\Microsoft\Windows NT\CurrentVersion') as key:
            product = str(winreg.QueryValueEx(key, 'ProductName')[0] or 'Windows').strip()
            try:
                edition = str(winreg.QueryValueEx(key, 'EditionID')[0] or '').strip()
            except OSError:
                edition = ''
            try:
                build = int(str(winreg.QueryValueEx(key, 'CurrentBuild')[0]))
            except (OSError, ValueError):
                build = 0
        # Windows 11 的 ProductName 至今仍写着 "Windows 10"（微软的历史包袱），
        # build ≥ 22000 才是 11。不修正的话设备画像会与真实系统对不上。
        if build >= 22000 and product.startswith('Windows 10'):
            product = product.replace('Windows 10', 'Windows 11', 1)
        if edition and edition.lower() not in product.lower():
            product = f'{product} {edition}'
    except Exception:  # noqa: BLE001
        pass
    return product or 'Windows'


def device_headers(uid: str) -> dict[str, str]:
    """IDE 数据面的设备头（品牌与系统名按官方形态 URL 编码）。"""
    return {
        'X-Device-Cpu': 'Intel',
        'X-Device-Id': device_id(uid),
        'X-Machine-Id': machine_id(uid),
        'X-Device-Brand': urllib.parse.quote(_hardware_brand(), safe=''),
        'X-Device-Type': 'windows',
        'X-Os-Version': urllib.parse.quote(_os_version(), safe=''),
    }


def request_id() -> str:
    """X-Request-Id：每次请求唯一，与账号设备指纹解耦。"""
    return str(uuid.uuid4())


def summary(uid: str) -> dict:
    """诊断用摘要（不含 token）。"""
    return {
        'userId': str(uid or ''),
        'machineId': machine_id(uid),
        'deviceId': device_id(uid),
        'deviceBrand': _hardware_brand(),
        'osVersion': _os_version(),
        'deviceType': 'windows',
        'cpu': 'Intel',
    }
