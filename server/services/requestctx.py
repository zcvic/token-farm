"""请求级上下文：`X-Request-Id` 的生成、透传与读取。

## 为什么需要它

一次调用出问题时，用户能给的往往只有「刚才那次失败」和一张截图。没有请求 ID
时，排查要在网关日志、请求日志、上游账号日志之间靠**时间 + 模型名**去猜；
并发一高就基本对不上。有了 ID，用户把响应头里的值发过来，就能直接定位到
`request_logs` 的那一行。

## 为什么用纯 ASGI 中间件而不是 `@app.middleware('http')`

函数式中间件（`BaseHTTPMiddleware`）在 `call_next` 返回时就把上下文收掉了，
而流式响应的生成器是在**那之后**才逐帧执行的。网关记账写在流式生成器的
`finally` 里 —— 用函数式中间件会读到空的上下文，流式请求的 `request_id`
全部丢失。纯 ASGI 包装器在整个响应（含流式 body）发送完毕后才退出，因此
上下文覆盖完整生命周期，同步路由（线程池）也能通过 contextvar 副本读到。

## 安全

只接受**看起来像 ID** 的入站值（8-128 位 `[A-Za-z0-9._-]`）。把任意字符串
原样回显到响应头有两个风险：头注入（换行）与日志污染（超长 / 伪造格式）。
不合规时**静默重新生成**，不报错 —— 客户端自定义 ID 只是便利，不是契约。
"""
from __future__ import annotations

import re
import uuid
from contextvars import ContextVar

HEADER = 'X-Request-Id'

# 反向代理 / 客户端常带这些头；按优先级取第一个合法的。
_INBOUND_HEADERS = (
    'x-request-id',
    'x-correlation-id',
    'x-trace-id',
)

_VALID = re.compile(r'^[A-Za-z0-9._-]{8,128}$')

_current: ContextVar[str] = ContextVar('request_id', default='')


def new_id() -> str:
    """生成一个新的请求 ID。用随机串而不是自增/时间：跨进程也不会撞。"""
    return uuid.uuid4().hex


def sanitize(value: object) -> str:
    """入站 ID 合法则沿用，否则返回空串（由调用方决定是否重新生成）。"""
    text = str(value or '').strip()
    return text if _VALID.match(text) else ''


def sanitize_or_new(value: object) -> str:
    return sanitize(value) or new_id()


def current() -> str:
    """当前请求的 ID；不在请求上下文中（定时任务、脚本）时为空串。"""
    return _current.get()


class RequestIdMiddleware:
    """纯 ASGI 中间件：生成/透传 ID、写入响应头、供给 contextvar。"""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get('type') != 'http':
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode('latin-1').lower(): value.decode('latin-1')
            for key, value in (scope.get('headers') or [])
        }
        rid = ''
        for name in _INBOUND_HEADERS:
            rid = sanitize(headers.get(name))
            if rid:
                break
        rid = rid or new_id()

        # 让路由 / 依赖也能直接拿到（例如未来在审计里附带 ID），但记账不依赖它：
        # 记账统一走 contextvar，流式收尾时仍然有效。
        scope.setdefault('state', {})['request_id'] = rid
        token = _current.set(rid)

        async def send_with_header(message):
            if message.get('type') == 'http.response.start':
                raw = message.setdefault('headers', [])
                # 覆盖而不是追加：某些代理链路会带一个空的同名头，重复会让
                # 客户端 / 网关取到错的那个。
                raw[:] = [(k, v) for k, v in raw
                          if k.lower() != HEADER.lower().encode('latin-1')]
                raw.append((HEADER.encode('latin-1'), rid.encode('latin-1')))
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            _current.reset(token)
