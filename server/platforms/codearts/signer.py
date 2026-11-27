"""华为云 `SDK-HMAC-SHA256` 请求签名（AK/SK + 可选 STS security token）。

对齐官方 SDK 的 `AKSKSigner`，几个容易写错、且错了只会得到 `APIG.0301`
（而不是「签名不对」）的点：

  · **signed headers = 请求里的全部头**（小写、排序），不是挑几个；新增头
    （例如福利模型的 `maas_type`）必须在签名**之前**加上，否则它不在
    SignedHeaders 里，上游按未签名头处理；
  · `CanonicalURI` 每段单独 percent-encode，且**末尾补 `/`**；
  · `CanonicalQuery` 按 key 排序、同 key 的值也排序后 escape；
  · `stringToSign = "SDK-HMAC-SHA256\\n" + X-Sdk-Date + "\\n" + sha256hex(canonicalRequest)`；
  · payload hash 同时写进 `X-Sdk-Content-Sha256` 与 canonical request 的最后一行，
    GET 的空 body 取 `sha256(b"")`。

`Content-Length` / `Host` 的处理与官方 SDK 一致：`Host` 显式设置并计入签名，
`Content-Length` 由 HTTP 客户端在发送时补，**不**计入签名。
"""
from __future__ import annotations

import hashlib
import hmac
import time
import urllib.parse
from typing import Mapping

__all__ = ['sign', 'sdk_date', 'sha256_hex', 'canonical_uri', 'canonical_query']

ALGORITHM = 'SDK-HMAC-SHA256'

# Go 的 `url.PathEscape` 在路径段里保留这些子分隔符（`/` 会被转义）。
_PATH_SAFE = "$&+,;=:@-_.~"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sdk_date(now: float | None = None) -> str:
    """`X-Sdk-Date`：UTC 的 `%Y%m%dT%H%M%SZ`。"""
    stamp = time.gmtime(time.time() if now is None else now)
    return time.strftime('%Y%m%dT%H%M%SZ', stamp)


def _path_escape(segment: str) -> str:
    return urllib.parse.quote(segment, safe=_PATH_SAFE)


def canonical_uri(path: str) -> str:
    """每段 percent-encode 并补尾斜杠（与官方 SDK 的 CanonicalURI 一致）。"""
    raw = str(path or '/')
    out = '/'.join(_path_escape(part) for part in raw.split('/'))
    if not out.endswith('/'):
        out += '/'
    return out


def canonical_query(raw_query: str) -> str:
    """key 排序、同 key 的值也排序，再逐个 escape（空格 → `+`）。"""
    text = str(raw_query or '')
    if not text:
        return ''
    pairs = urllib.parse.parse_qsl(text, keep_blank_values=True)
    grouped: dict[str, list[str]] = {}
    for key, value in pairs:
        grouped.setdefault(key, []).append(value)
    parts: list[str] = []
    for key in sorted(grouped):
        for value in sorted(grouped[key]):
            parts.append(f'{urllib.parse.quote_plus(key, safe="")}='
                         f'{urllib.parse.quote_plus(value, safe="")}')
    return '&'.join(parts)


def _split_url(url: str) -> tuple[str, str, str]:
    """拆成 `(host[:port], path, query)`。"""
    parsed = urllib.parse.urlsplit(str(url or ''))
    host = parsed.netloc or ''
    path = parsed.path or '/'
    return host, path, parsed.query


def sign(method: str, url: str, headers: Mapping[str, str],
         body: bytes = b'', *, access_key: str, secret_key: str,
         security_token: str = '', now: float | None = None) -> dict[str, str]:
    """返回**完整的**请求头（含新增的签名头），调用方原样发出即可。

    传进来的 `headers` 会被复制：调用方可以继续复用它拼别的请求。
    """
    out = {str(key): str(value) for key, value in (headers or {}).items()}
    host, path, query = _split_url(url)
    out['X-Sdk-Date'] = sdk_date(now)
    out['Host'] = host
    if security_token:
        out['X-Security-Token'] = security_token
    payload_hash = sha256_hex(body or b'')
    out['X-Sdk-Content-Sha256'] = payload_hash

    keys = sorted(key.lower() for key in out)
    canonical_headers = ''.join(
        f'{key}:{str(_header_value(out, key)).strip()}\n' for key in keys)
    signed_headers = ';'.join(keys)

    canonical_request = '\n'.join((
        str(method or 'GET').upper(),
        canonical_uri(path),
        canonical_query(query),
        canonical_headers,
        signed_headers,
        payload_hash,
    ))
    string_to_sign = '\n'.join((
        ALGORITHM,
        out['X-Sdk-Date'],
        sha256_hex(canonical_request.encode('utf-8')),
    ))
    signature = hmac.new(str(secret_key or '').encode('utf-8'),
                         string_to_sign.encode('utf-8'),
                         hashlib.sha256).hexdigest()
    out['Authorization'] = (
        f'{ALGORITHM} Access={access_key}, '
        f'SignedHeaders={signed_headers}, Signature={signature}')
    return out


def _header_value(headers: Mapping[str, str], lower_key: str) -> str:
    """按小写 key 取原值（头名大小写在 HTTP 里不敏感）。"""
    for key, value in headers.items():
        if key.lower() == lower_key:
            return str(value)
    return ''
