"""RSA/PKCS#1 v1.5 加密（纯标准库）。

讯飞账号服务登录时先 `getPuKey` 拿一把 1024-bit DER 公钥，密码要用它加密
后提交。用 `pow()` 做模幂即可，不必引入 `cryptography` —— 这把钥匙很小，
一次 modexp 是毫秒级。

参考 `loomy2api/crypto.py`（MIT）。
"""
from __future__ import annotations

import base64
import os
from typing import Optional, Tuple

__all__ = ['rsa_encrypt', 'der_to_numbers']


def _read_der(buf: bytes, pos: int) -> Tuple[int, int, int]:
    """极简 DER 读取 → ``(tag, content_start, content_len)``。"""
    tag = buf[pos]
    pos += 1
    if pos >= len(buf):
        raise ValueError('truncated DER')
    length = buf[pos]
    pos += 1
    if length & 0x80:
        count = length & 0x7F
        if count == 0 or count > 4:
            raise ValueError('unsupported DER length')
        length = int.from_bytes(buf[pos:pos + count], 'big')
        pos += count
    return tag, pos, length


def der_to_numbers(der: bytes) -> Tuple[int, int]:
    """从 DER SubjectPublicKeyInfo 取出 ``(modulus, exponent)``。

    布局：``SEQUENCE{ SEQUENCE{ OID, NULL }, BIT STRING{
    SEQUENCE{ INTEGER n, INTEGER e } } }``。
    """
    tag, pos, _ = _read_der(der, 0)          # outer SEQUENCE
    if tag != 0x30:
        raise ValueError('not a DER SEQUENCE')

    tag, pos, alg_len = _read_der(der, pos)  # AlgorithmIdentifier SEQUENCE
    if tag != 0x30:
        raise ValueError('missing AlgorithmIdentifier')
    pos += alg_len                           # skip OID + NULL params

    tag, body, _length = _read_der(der, pos)  # BIT STRING
    if tag != 0x03:
        raise ValueError('missing BIT STRING')
    if der[body] != 0x00:
        raise ValueError('unexpected unused bits in BIT STRING')
    pos = body + 1

    tag, pos, _ = _read_der(der, pos)        # inner RSAPublicKey SEQUENCE
    if tag != 0x30:
        raise ValueError('missing RSAPublicKey')

    values = []
    for _ in range(2):                       # INTEGER n, INTEGER e
        tag, body, length = _read_der(der, pos)
        if tag != 0x02:
            raise ValueError('expected INTEGER')
        values.append(int.from_bytes(der[body:body + length], 'big'))
        pos = body + length
    return values[0], values[1]


def rsa_encrypt(pubkey_der_b64: str,
                plaintext: str,
                rng: Optional[bytes] = None) -> str:
    """RSA/PKCS#1 v1.5 加密 ``plaintext`` → base64 密文。

    ``rng`` 只给测试注入确定性填充用。
    """
    der = base64.b64decode(pubkey_der_b64 + '=' * (-len(pubkey_der_b64) % 4))
    modulus, exponent = der_to_numbers(der)
    size = (modulus.bit_length() + 7) // 8
    message = plaintext.encode('utf-8')
    if len(message) > size - 11:
        raise ValueError(f'message too long for RSA-{size * 8}')

    ps_len = size - len(message) - 3
    while True:
        padding = rng if rng is not None else os.urandom(ps_len)
        if len(padding) != ps_len:
            raise ValueError('rng produced wrong length')
        if 0 not in padding:                 # PKCS#1 要求填充非零
            break
    block = b'\x00\x02' + padding + b'\x00' + message
    cipher = pow(int.from_bytes(block, 'big'), exponent, modulus)
    return base64.b64encode(cipher.to_bytes(size, 'big')).decode()
