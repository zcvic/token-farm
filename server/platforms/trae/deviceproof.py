"""国际版 ExchangeToken 的设备证明（DeviceProof）。

官方客户端刷新**国际版** access token 时，会在请求体里额外带 `DeviceInfo` +
`DeviceProof`：

```
message   = "POST\n/trae/api/v3/oauth/ExchangeToken\n<ClientID>\n<RefreshToken>\n
             <秒级时间戳>\n<nonce>"                 # 换行连接
Signature = base64( DER(ECDSA-SHA256(message)) )
```

签名私钥优先取客户端 `storage.json` 的
`iCubeAuthInfo://icube-dc:<holderId>` 条目（与 `icube.cloudide` 同一套
tc/AES-128-CBC 封装，用 `icube.decode` 就能读到 `privateKeyPEM`）。没有
客户端时，面板用 `generate_keypair()` 自己生成一套并长期复用——客户端本来
也是启动时自生成的，并不要求先登录。

**实测（2026-11-01，SG 账号）**：时间戳是**秒**、六段用 `\n` 连接、路径是
`/trae/api/v3/oauth/ExchangeToken`；用毫秒 / 空格会得到 20405。`DeviceInfo`
里的 `DeviceID` 必须是私钥 holderId，`MachineID` 是客户端
`telemetry.machineId`，不能按 uid 派生。

为什么自己实现 P-256：这一步只在**刷新国际版令牌**时跑，且部署环境（容器 /
精简系统）不保证装了 `cryptography`。与 `icube.py` 的 AES 同理——为一个可选
路径引入二进制依赖不划算。实现覆盖签名所需的 P-256 运算，并用 RFC 6979
A.2.5 官方向量自检（写错了会立刻炸，而不是安静地签出一串废签名）。
"""
from __future__ import annotations

import base64
import hashlib
import os
import platform
import secrets
import socket
import time
import uuid
from typing import Any

from . import fingerprint
from .constants import INTL_IDE_VERSION

# ── P-256 域参数 ─────────────────────────────────────────────
P256_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
P256_A = P256_P - 3
P256_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
P256_GX = 0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296
P256_GY = 0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5
P256_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
P256_G = (P256_GX, P256_GY)


def _ec_add(point: tuple[int, int] | None,
            other: tuple[int, int] | None) -> tuple[int, int] | None:
    """仿射点加；None 表示无穷远点。"""
    if point is None:
        return other
    if other is None:
        return point
    x1, y1 = point
    x2, y2 = other
    if x1 == x2:
        if (y1 + y2) % P256_P == 0:
            return None
        slope = (3 * x1 * x1 + P256_A) * pow(2 * y1, -1, P256_P) % P256_P
    else:
        slope = (y2 - y1) * pow(x2 - x1, -1, P256_P) % P256_P
    x3 = (slope * slope - x1 - x2) % P256_P
    y3 = (slope * (x1 - x3) - y1) % P256_P
    return (x3, y3)


def _ec_mul(scalar: int, point: tuple[int, int] = P256_G) \
        -> tuple[int, int] | None:
    """标量乘（double-and-add）；标量为 0 返回 None。"""
    scalar %= P256_N
    result: tuple[int, int] | None = None
    addend: tuple[int, int] | None = point
    while scalar:
        if scalar & 1:
            result = _ec_add(result, addend)
        addend = _ec_add(addend, addend)
        scalar >>= 1
    return result


def p256_public_point(private_key: int) -> tuple[int, int]:
    """由私钥标量导出公钥点。"""
    point = _ec_mul(private_key)
    if point is None:
        raise ValueError('私钥导出公钥失败')
    return point


def p256_sign(private_key: int, message: bytes, k: int | None = None) \
        -> tuple[int, int]:
    """ECDSA-SHA256 签名，返回 (r, s)；s 归一化为 low-S（客户端常见做法）。"""
    digest = hashlib.sha256(message).digest()
    z = int.from_bytes(digest, 'big')
    while True:
        if k is None:
            nonce = int.from_bytes(os.urandom(32), 'big') % P256_N
        else:
            nonce = k % P256_N
        point = _ec_mul(nonce)
        if point is None:
            if k is not None:
                raise ValueError('k 无效')
            continue
        r = point[0] % P256_N
        if r == 0:
            if k is not None:
                raise ValueError('r=0')
            continue
        s = (pow(nonce, -1, P256_N) * (z + r * private_key)) % P256_N
        if s == 0:
            if k is not None:
                raise ValueError('s=0')
            continue
        if s > P256_N // 2:
            s = P256_N - s
        return r, s


def p256_verify(public_point: tuple[int, int], message: bytes,
                r: int, s: int) -> bool:
    """ECDSA 验签（自检 / 测试夹具用，生产只签名）。"""
    if not (1 <= r < P256_N and 1 <= s < P256_N):
        return False
    z = int.from_bytes(hashlib.sha256(message).digest(), 'big')
    w = pow(s, -1, P256_N)
    point = _ec_add(_ec_mul(z * w % P256_N),
                    _ec_mul(r * w % P256_N, public_point))
    if point is None:
        return False
    return point[0] % P256_N == r


# ── DER / PEM ───────────────────────────────────────────────
def _der_tlv(data: bytes, offset: int) -> tuple[int, bytes, int]:
    """读一个 DER TLV，返回 (tag, value, next_offset)。"""
    tag = data[offset]
    length = data[offset + 1]
    offset += 2
    if length & 0x80:
        count = length & 0x7F
        length = int.from_bytes(data[offset:offset + count], 'big')
        offset += count
    return tag, data[offset:offset + length], offset + length


def _der_children(value: bytes) -> list[tuple[int, bytes]]:
    items: list[tuple[int, bytes]] = []
    offset = 0
    while offset < len(value):
        tag, chunk, offset = _der_tlv(value, offset)
        items.append((tag, chunk))
    return items


def _pem_to_der(pem: str) -> bytes:
    lines = [line.strip() for line in str(pem).strip().splitlines()]
    body = ''.join(line for line in lines if line and not line.startswith('-----'))
    if not body:
        raise ValueError('PEM 没有正文')
    return base64.b64decode(body)


def ec_private_key_from_pem(pem: str) -> int:
    """PKCS#8 或 SEC1 PEM → 私钥标量 d（prime256v1）。

    客户端 `icube-dc` 里给的是 `-----BEGIN PRIVATE KEY-----`（PKCS#8），
    这里同时接受 `BEGIN EC PRIVATE KEY`（SEC1），避免不同版本客户端差异。
    """
    der = _pem_to_der(pem)
    tag, body, _ = _der_tlv(der, 0)
    if tag != 0x30:
        raise ValueError('私钥 PEM 不是 DER SEQUENCE')
    items = _der_children(body)
    if not items:
        raise ValueError('私钥 PEM 为空')
    # PKCS#8: INTEGER 0, SEQUENCE(alg), OCTET STRING(ECPrivateKey)
    if items[0][0] == 0x02 and len(items) >= 3 and items[2][0] == 0x04:
        tag2, body2, _ = _der_tlv(items[2][1], 0)
        if tag2 != 0x30:
            raise ValueError('PKCS#8 内层不是 ECPrivateKey')
        inner = _der_children(body2)
        if len(inner) < 2 or inner[1][0] != 0x04:
            raise ValueError('ECPrivateKey 缺少私钥字段')
        scalar = int.from_bytes(inner[1][1], 'big')
    # SEC1: INTEGER 1, OCTET STRING(d)
    elif items[0][0] == 0x02 and len(items) >= 2 and items[1][0] == 0x04:
        scalar = int.from_bytes(items[1][1], 'big')
    else:
        raise ValueError('无法识别的 EC 私钥 PEM 结构')
    if not 1 <= scalar < P256_N:
        raise ValueError('EC 私钥标量超出 P-256 范围')
    return scalar


def ec_public_key_from_pem(pem: str) -> tuple[int, int]:
    """SPKI PEM → (x, y)；仅用于自检。"""
    der = _pem_to_der(pem)
    tag, body, _ = _der_tlv(der, 0)
    if tag != 0x30:
        raise ValueError('公钥 PEM 不是 DER SEQUENCE')
    bit = None
    for item_tag, chunk in _der_children(body):
        if item_tag == 0x03:
            bit = chunk
    if bit is None:
        raise ValueError('SPKI 缺少 BIT STRING')
    point = bit[1:]  # 去掉 unused-bits 字节
    if point[:1] != b'\x04' or len(point) != 65:
        raise ValueError('公钥不是未压缩 P-256 点')
    return (int.from_bytes(point[1:33], 'big'),
            int.from_bytes(point[33:], 'big'))


def _der_encode_integer(value: int) -> bytes:
    raw = value.to_bytes((value.bit_length() + 7) // 8 or 1, 'big')
    if raw[0] & 0x80:
        raw = b'\x00' + raw
    return b'\x02' + bytes([len(raw)]) + raw


def der_encode_signature(r: int, s: int) -> bytes:
    """ECDSA-Sig-Value ::= SEQUENCE { r INTEGER, s INTEGER }"""
    body = _der_encode_integer(r) + _der_encode_integer(s)
    return b'\x30' + bytes([len(body)]) + body


def _der_encode_length(length: int) -> bytes:
    if length < 0x80:
        return bytes([length])
    raw = length.to_bytes((length.bit_length() + 7) // 8, 'big')
    return bytes([0x80 | len(raw)]) + raw


def _der_encode_tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + _der_encode_length(len(value)) + value


def _pem_encode(label: str, der: bytes) -> str:
    body = base64.b64encode(der).decode('ascii')
    lines = [body[index:index + 64] for index in range(0, len(body), 64)]
    return '-----BEGIN %s-----\n%s\n-----END %s-----' % (
        label, '\n'.join(lines), label)


# OID 常量直接写成 DER 编码（1.2.840.10045.2.1 / 1.2.840.10045.3.1.7）。
_OID_EC_PUBLIC_KEY = bytes.fromhex('06072A8648CE3D0201')
_OID_PRIME256V1 = bytes.fromhex('06082A8648CE3D030107')


def _public_point_bytes(point: tuple[int, int]) -> bytes:
    return (b'\x04' + point[0].to_bytes(32, 'big')
            + point[1].to_bytes(32, 'big'))


def generate_keypair() -> dict[str, str]:
    """生成一把随机 P-256 设备密钥，返回 PKCS#8 / SPKI 两份 PEM。

    为什么需要它：客户端启动时自己生成密钥对（`Oxe()`），并没有要求先登录。
    面板在没装客户端的机器上也要能完成国际版网页登录与后续刷新，所以这里
    按同一形状生成一套**面板级**设备身份，登录时上报公钥、之后用私钥签
    `DeviceProof`。私钥只会被调用方写进 `data/`（gitignore）与账号文件，
    不会出现在任何接口响应或日志里。
    """
    scalar = secrets.randbelow(P256_N - 1) + 1
    point = p256_public_point(scalar)
    algorithm = _der_encode_tlv(0x30, _OID_EC_PUBLIC_KEY + _OID_PRIME256V1)
    # SEC1 ECPrivateKey：version / privateKey / [0] parameters / [1] publicKey
    sec1 = _der_encode_tlv(0x30, b''.join((
        b'\x02\x01\x01',
        _der_encode_tlv(0x04, scalar.to_bytes(32, 'big')),
        _der_encode_tlv(0xA0, _OID_PRIME256V1),
        _der_encode_tlv(0xA1, _der_encode_tlv(0x03,
                                              b'\x00' + _public_point_bytes(point))),
    )))
    # PKCS#8 PrivateKeyInfo：version / AlgorithmIdentifier / OCTET STRING
    private_der = _der_encode_tlv(0x30, b''.join((
        b'\x02\x01\x00',
        algorithm,
        _der_encode_tlv(0x04, sec1),
    )))
    # SubjectPublicKeyInfo：AlgorithmIdentifier / BIT STRING
    public_der = _der_encode_tlv(0x30, algorithm + _der_encode_tlv(
        0x03, b'\x00' + _public_point_bytes(point)))
    return {
        'privateKeyPEM': _pem_encode('PRIVATE KEY', private_der),
        'publicKeyPEM': _pem_encode('PUBLIC KEY', public_der),
    }


# ── DeviceInfo / DeviceProof ────────────────────────────────
def _os_name() -> str:
    if os.name == 'nt':
        return 'Windows'
    return platform.system() or 'Linux'


def device_info(uid: str, *, device_id: str | None = None,
                machine_id: str | None = None, public_key_pem: str | None = None,
                ide_version: str | None = None) -> dict[str, str]:
    """与客户端同形的设备信息。

    设备号优先取客户端 storage.json：
      · DeviceID = `icube-dc:<holderId>` 里的 holder；
      · MachineID = `telemetry.machineId`。
    没有客户端时用面板自己生成的稳定设备身份（见 `generate_keypair` 与
    `store.read_device_identity`）；按 uid 派生只是最后的兜底（服务端会回 20405）。
    """
    uid = str(uid or '')
    return {
        'DeviceID': str(device_id or fingerprint.device_id(uid)),
        'MachineID': str(machine_id or fingerprint.machine_id(uid)),
        'PlatformCode': 'IDE_PC',
        'DeviceType': 'PC',
        'DeviceName': socket.gethostname() or 'Windows',
        'DeviceModel': fingerprint._hardware_brand(),
        'ClientVersion': str(ide_version or INTL_IDE_VERSION),
        'DevicePublicKey': str(public_key_pem or ''),
        'DeviceBrand': fingerprint._hardware_brand(),
        'DeviceCPU': 'Intel',
        'OSInfo': _os_name(),
        'OSVersion': fingerprint._os_version(),
    }


def proof_message(method: str, path: str, client_id: str, refresh_token: str,
                  timestamp: int, nonce: str) -> bytes:
    """签名原文：换行连接的六元组（顺序与客户端一致，不要"整理"）。"""
    return '\n'.join([str(method), str(path), str(client_id),
                      str(refresh_token), str(timestamp), str(nonce)]).encode('utf-8')


def build_device_proof(account: dict[str, Any], *, method: str = 'POST',
                       path: str, client_id: str, refresh_token: str,
                       private_key_pem: str | None = None,
                       timestamp: int | None = None,
                       nonce: str | None = None,
                       device_id: str | None = None,
                       machine_id: str | None = None,
                       public_key_pem: str | None = None,
                       ide_version: str | None = None) -> dict[str, Any]:
    """构造 `{DeviceInfo, DeviceProof}`，直接 update 进 ExchangeToken 请求体。

    `Timestamp` 是 **秒级整数**（客户端 `Math.floor(Date.now()/1e3)`）；
    传毫秒会被上游 20405 拒绝。
    """
    pem = private_key_pem or account.get('deviceKeyPEM') \
        or account.get('privateKeyPEM') or ''
    if not pem:
        raise ValueError('缺少 icube-dc 设备私钥，无法签名 DeviceProof')
    scalar = ec_private_key_from_pem(pem)
    stamp = int(timestamp) if timestamp is not None else int(time.time())
    random_part = str(nonce if nonce is not None else uuid.uuid4().hex)
    message = proof_message(method, path, client_id, refresh_token, stamp,
                            random_part)
    r, s = p256_sign(scalar, message)
    signature = base64.b64encode(der_encode_signature(r, s)).decode('ascii')
    return {
        'DeviceInfo': device_info(
            str(account.get('uid') or ''), device_id=device_id,
            machine_id=machine_id, public_key_pem=public_key_pem,
            ide_version=ide_version),
        'DeviceProof': {'Signature': signature, 'Timestamp': stamp,
                        'Nonce': random_part},
    }


def self_check() -> bool:
    """RFC 6979 A.2.5（P-256/SHA-256, message='sample'）向量自检。"""
    d = 0xC9AFA9D845BA75166B5C215767B1D6934E50C3DB36E89B127B8A622B120F6721
    k = 0xA6E3C57DD01ABE90086538398355DD4C3B17AA873382B0F24D6129493D8AAD60
    r, s = p256_sign(d, b'sample', k=k)
    expect_r = 0xEFD48B2AACB6A8FD1140DD9CD45E81D69D2C877B56AAF991C34D0EA84EAF3716
    expect_s = 0xF7CB1C942D657C41D436C7A1B6E29F65F3E900DBB9AFF4064DC4AB2F843ACDA8
    return bool(r == expect_r
                and min(s, P256_N - s) == min(expect_s, P256_N - expect_s)
                and p256_verify(p256_public_point(d), b'sample', r, s))
