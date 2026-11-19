"""讯飞账号服务请求签名（HMAC-SHA1）。

与客户端 `electron/xfyun/sign.js` 逐行对齐：

    stringToSign = "\\n".join([
        METHOD, ESCAPED_PATH, ESCAPED_QUERY, Content-MD5,
        Content-Type, Date, Nonce, SignedHeaders, CanonicalizedHeaders,
    ])
    signature = base64(hmac_sha1(accessKeySecret, stringToSign))
    Authorization = "account <accessKeyId>:<signature>"

最后两段在没有自定义 `x-*` 头时是空串，但**必须存在**：字符串是 9 行，
不是 7 行。少一行上游就判签名错误。

参考 `loomy2api/signer.py`（MIT）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
import uuid
from typing import Dict, Mapping, Optional
from urllib.parse import quote

__all__ = ['build_headers', 'sign', 'string_to_sign']

#: `urllib.parse.quote` 保留但服务端会转义的 RFC 3986 sub-delims。
_EXTRA_ESCAPES = {'!': '%21', "'": '%27', '(': '%28', ')': '%29', '*': '%2A'}


def _escape(value: str) -> str:
    out = quote(str(value), safe='-_.~')
    for raw, encoded in _EXTRA_ESCAPES.items():
        out = out.replace(raw, encoded)
    return out


def content_md5(body: str) -> str:
    """base64(md5(body))；空 body 返回空串。"""
    if not body:
        return ''
    return base64.b64encode(hashlib.md5(body.encode('utf-8')).digest()).decode()


def _escaped_path(path: str) -> str:
    value = path if path.startswith('/') else '/' + path
    if len(value) > 1 and value.endswith('/'):
        value = value[:-1]
    return '/'.join(_escape(seg) if seg else '' for seg in value.split('/'))


def _escaped_query(params: Mapping[str, object]) -> str:
    if not params:
        return ''
    return '&'.join(
        f'{_escape(key)}={_escape("" if value is None else value)}'
        for key, value in params.items())


def _canonicalized(headers: Mapping[str, object], prefix: str = 'x-'):
    picked = {key.lower(): str(value).strip() for key, value in headers.items()
              if key.lower().startswith(prefix)}
    if not picked:
        return '', ''
    keys = sorted(picked)
    canonical = ''.join(f'{key}:{picked[key]}\n' for key in keys)
    return ';'.join(keys), canonical


def string_to_sign(*, method: str, path: str, query: Optional[Mapping] = None,
                   body: str = '', content_type: str = 'application/json',
                   date: str, nonce: str,
                   custom_headers: Optional[Mapping] = None) -> str:
    signed_headers, canonical = _canonicalized(custom_headers or {})
    return '\n'.join([
        method.upper(),
        _escaped_path(path),
        _escaped_query(query or {}),
        content_md5(body),
        content_type,
        date,
        nonce,
        signed_headers,
        canonical.rstrip('\n'),
    ])


def sign(*, secret: str, **kwargs) -> str:
    digest = hmac.new(secret.encode('utf-8'),
                      string_to_sign(**kwargs).encode('utf-8'),
                      hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def build_headers(access_key_id: str, access_key_secret: str, *,
                  method: str, path: str, body: str = '',
                  query: Optional[Mapping] = None,
                  content_type: str = 'application/json',
                  custom_headers: Optional[Mapping] = None,
                  clock=None) -> Dict[str, str]:
    """一次账号服务请求的完整请求头。"""
    date = time.strftime('%a, %d %b %Y %H:%M:%S GMT',
                         time.gmtime(None if clock is None else clock))
    nonce = str(uuid.uuid4())
    signature = sign(
        secret=access_key_secret, method=method, path=path, query=query,
        body=body, content_type=content_type, date=date, nonce=nonce,
        custom_headers=custom_headers,
    )
    headers = {
        'Authorization': f'account {access_key_id}:{signature}',
        'Date': date,
        'Nonce': nonce,
        'Content-Type': content_type,
    }
    md5 = content_md5(body)
    if md5:
        headers['Content-MD5'] = md5
    for key, value in (custom_headers or {}).items():
        headers[key] = str(value)
    return headers
