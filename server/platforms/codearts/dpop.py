"""CodeArts 的 DPoP 证明（RFC 9449）：ES256 + P-256。

华为云 STS 的 token 端点要求 `DPoP` 头里带一个 JWT 证明，并且**把
refresh_token 绑定到首次换取时的公钥上**：

  · 换一个 DPoP 私钥去刷新 → `400 InvalidDPoPHeader`；
  · 换一个 client_id → `400 invalid client id`；
  · 用旧 refresh_token 再刷一次 → `the refresh token has been used`。

所以私钥必须与 refresh_token 一起持久化、长期复用（见 `store.py` 的
`dpopJwk` 字段）。

JWT 形状：

```
header  = {alg:"ES256", typ:"dpop+jwt", jwk:{kty:"EC", crv:"P-256", x, y}}
payload = {htm:"POST", htu:<token 端点完整 URL>, iat:<秒>, jti:<32 字节 hex>}
签名    = ES256 over b64url(header) + "." + b64url(payload)
```

签名值用 `r || s` 各 32 字节的**原始拼接**（JWS 规定），不是 DER；`s` 归一化为
low-S。P-256 运算复用 `trae/deviceproof.py`——那份实现已经用 RFC 6979 官方向量
自检过，再抄一份只会多一处会写错的地方。
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import time
from typing import Any

from ..trae.deviceproof import P256_N, P256_P, p256_public_point, p256_sign, p256_verify

__all__ = ['generate_jwk', 'normalize_jwk', 'proof', 'verify_proof',
           'jwk_public', 'b64url_decode']


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


def b64url_decode(text: object) -> bytes:
    raw = str(text or '').strip()
    raw += '=' * (-len(raw) % 4)
    return base64.urlsafe_b64decode(raw.encode('ascii'))


def _padded(value: int) -> bytes:
    return value.to_bytes(32, 'big')


def generate_jwk() -> dict[str, str]:
    """生成一把可持久化的 P-256 私钥（JWK，四个字段都是 base64url）。"""
    scalar = secrets.randbelow(P256_N - 1) + 1
    point = p256_public_point(scalar)
    return {
        'kty': 'EC',
        'crv': 'P-256',
        'x': _b64url(_padded(point[0])),
        'y': _b64url(_padded(point[1])),
        'd': _b64url(_padded(scalar)),
    }


def normalize_jwk(raw: object) -> dict[str, str]:
    """校验并归一化一份持久化的私钥 JWK。

    校验项与官方 SDK 一致：`kty` / `crv` 对得上、`x` / `y` / `d` 都是 32 字节、
    `d` 在 `[1, N)` 内、公钥点在曲线上、且 `d·G` 确实等于 `(x, y)`。缺任何一项
    都直接拒绝——用一把错的私钥去刷新，只会把 refresh_token 白白作废。
    """
    if not isinstance(raw, dict):
        raise ValueError('DPoP 私钥缺失')
    if str(raw.get('kty') or '') != 'EC' or str(raw.get('crv') or '') != 'P-256':
        raise ValueError('DPoP 私钥不是 P-256')

    def field(name: str) -> bytes:
        try:
            value = b64url_decode(raw.get(name))
        except (ValueError, TypeError) as exc:
            raise ValueError(f'DPoP 私钥 {name} 不是 base64url') from exc
        if len(value) != 32:
            raise ValueError(f'DPoP 私钥 {name} 不是 32 字节')
        return value

    x_raw, y_raw, d_raw = field('x'), field('y'), field('d')
    d = int.from_bytes(d_raw, 'big')
    x = int.from_bytes(x_raw, 'big')
    y = int.from_bytes(y_raw, 'big')
    if not 1 <= d < P256_N:
        raise ValueError('DPoP 私钥标量越界')
    if not (0 <= x < P256_P and 0 <= y < P256_P):
        raise ValueError('DPoP 公钥点越界')
    want = p256_public_point(d)
    if want != (x, y):
        raise ValueError('DPoP 公私钥不匹配')
    return {'kty': 'EC', 'crv': 'P-256',
            'x': _b64url(x_raw), 'y': _b64url(y_raw), 'd': _b64url(d_raw)}


def jwk_public(jwk: object) -> dict[str, str]:
    """只取公钥部分（写进 JWT header 的那三个字段）。"""
    item = normalize_jwk(jwk)
    return {'kty': 'EC', 'crv': 'P-256', 'x': item['x'], 'y': item['y']}


def proof(jwk: object, htu: str, *, htm: str = 'POST',
          now: float | None = None) -> str:
    """生成一个 DPoP 证明 JWT。`htu` 必须是 token 端点的**完整 URL**。"""
    key = normalize_jwk(jwk)
    header = {'alg': 'ES256', 'typ': 'dpop+jwt', 'jwk': jwk_public(key)}
    payload = {
        'htm': str(htm or 'POST').upper(),
        'htu': str(htu or ''),
        'iat': int(time.time() if now is None else now),
        'jti': secrets.token_hex(32),
    }
    head = _b64url(json.dumps(header, separators=(',', ':')).encode('utf-8'))
    body = _b64url(json.dumps(payload, separators=(',', ':')).encode('utf-8'))
    signing_input = f'{head}.{body}'
    r, s = p256_sign(int.from_bytes(b64url_decode(key['d']), 'big'),
                     signing_input.encode('ascii'))
    return f'{signing_input}.{_b64url(_padded(r) + _padded(s))}'


def verify_proof(token: str, *, expected_htu: str = '') -> dict[str, Any]:
    """验签 + 取 payload（仅测试与排障用，生产只签名）。"""
    parts = str(token or '').split('.')
    if len(parts) != 3:
        raise ValueError('DPoP 证明不是三段式 JWT')
    header = json.loads(b64url_decode(parts[0]))
    payload = json.loads(b64url_decode(parts[1]))
    jwk = header.get('jwk') or {}
    point = (int.from_bytes(b64url_decode(jwk.get('x')), 'big'),
             int.from_bytes(b64url_decode(jwk.get('y')), 'big'))
    signature = b64url_decode(parts[2])
    if len(signature) != 64:
        raise ValueError('DPoP 签名不是 r||s 各 32 字节')
    r = int.from_bytes(signature[:32], 'big')
    s = int.from_bytes(signature[32:], 'big')
    ok = p256_verify(point, f'{parts[0]}.{parts[1]}'.encode('ascii'), r, s)
    if not ok:
        raise ValueError('DPoP 验签失败')
    if expected_htu and payload.get('htu') != expected_htu:
        raise ValueError('DPoP htu 不匹配')
    if header.get('alg') != 'ES256' or header.get('typ') != 'dpop+jwt':
        raise ValueError('DPoP header 形状不对')
    return {'header': header, 'payload': payload}


def pkce_pair() -> tuple[str, str]:
    """PKCE：verifier 是 64 随机字节的 base64url，challenge 是它的 SHA-256。"""
    import hashlib

    verifier = _b64url(os.urandom(64))
    challenge = _b64url(hashlib.sha256(verifier.encode('ascii')).digest())
    return verifier, challenge
