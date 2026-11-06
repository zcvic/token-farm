"""平台 / 组合模型在 **Chat Completions 以外的协议层**的共用调用层。

`/v1/chat/completions` 的平台分支由 `gateway._platform_chat` 处理：它把
`PlatformChat` 直接包成 OpenAI 响应。Responses 与 Anthropic 两条协议不返回
OpenAI 形状，而是各自把 OpenAI 结果**翻译**成自己的协议——但它们需要的东西
完全一样：

  · 调一次适配器（`PlatformAdapter.chat`），拿到 `PlatformChat`；
  · 流式时把「dict chunk 流」或「原始 SSE 字节流」统一成**字节流**，
    交给既有的逐行解析循环（两套协议各自已经有一份，不能各写一套解析）；
  · 记账名用实际去向（组合会指向真实成员），错误信息统一成人可读的一句话。

所以这里只做「调用 + 归一化」，不碰协议翻译、不碰鉴权、不碰记账。

## 为什么把 dict chunk 也编码成字节

`/v1/chat/completions` 的热路径要求逐字节透传上游 chunk（`test_gateway_passthrough`），
但 Responses / Anthropic 层本来就要解析每一帧才能翻译，不存在「原样透传」。
统一成字节流后，两条协议的下游循环**一行都不用改**：它们本来就只认
`data: {...}\n\n`。dict chunk 用 `platforms.sse.frame()` 编码，原始字节流
（WorkBuddy 反代）原样返回。
"""
from __future__ import annotations

from typing import Any, AsyncIterator

from .. import platforms
from ..platforms import sse
from ..platforms.base import PlatformAdapter, PlatformChat


def _payload_error(payload: Any) -> str | None:
    """从上游错误体里取一句人可读的原因（拿不到返回 None）。"""
    if isinstance(payload, dict):
        err = payload.get('error')
        if isinstance(err, dict):
            msg = err.get('message')
            if isinstance(msg, str) and msg.strip():
                return msg[:500]
        elif isinstance(err, str) and err.strip():
            return err[:500]
        msg = payload.get('message')
        if isinstance(msg, str) and msg.strip():
            return msg[:500]
        if payload:
            return str(payload)[:300]
    elif payload is not None:
        return str(payload)[:300]
    return None


async def _frames(chat: PlatformChat) -> AsyncIterator[bytes]:
    """OpenAI chunk 流 → SSE 字节流；收尾时释放适配器资源。"""
    try:
        async for chunk in chat.stream or ():
            yield sse.frame(chunk)
        yield sse.DONE
    finally:
        # 客户端提前断开时不会走到上面的正常收尾，这里兜一次（aclose 幂等）。
        await chat.aclose()


class PlatformCall:
    """一次平台调用的归一化结果，供各协议层读取。

    只读 `PlatformChat` 的实时值（而不是构造时快照）：流式的 `usage` /
    `first_token_ms` / `error` 由适配器在**流结束时**回填，协议层要到收尾记账
    时才读得到。
    """

    def __init__(self, chat: PlatformChat, adapter: PlatformAdapter,
                 bare_model: str, stream: AsyncIterator[bytes] | None) -> None:
        self._chat = chat
        self._adapter = adapter
        self._bare_model = bare_model
        self.stream = stream

    @property
    def streaming(self) -> bool:
        return self.stream is not None

    @property
    def status(self) -> int:
        return self._chat.status

    @property
    def payload(self) -> dict | None:
        return self._chat.payload

    @property
    def model(self) -> str:
        """实际去向（记账用）：组合会返回真实成员名，而不是 `combo/xxx`。"""
        return str(self._chat.model or self._adapter.record_name(self._bare_model))

    @property
    def account(self) -> str:
        return str(self._chat.account_hint or '')

    @property
    def realm(self) -> str | None:
        """实际账号的版本（cn / global）；适配器不区分版本时为 None。"""
        return self._chat.realm

    @property
    def first_token_ms(self) -> int | None:
        return self._chat.first_token_ms

    @property
    def usage(self) -> dict:
        usage = self._chat.usage
        if isinstance(usage, dict) and usage:
            return usage
        payload = self._chat.payload
        if isinstance(payload, dict) and isinstance(payload.get('usage'), dict):
            return payload['usage']
        return {}

    @property
    def error(self) -> str | None:
        """错误文本（成功时为 None）：优先从错误体里取可读原因。

        上游错误体通常带 `error.message`；适配器的 `chat.error` 有时是整段原文
        （WorkBuddy 的非流式失败就是这么填的），直接回给客户端会把一句 JSON
        塞进协议错误字段。所以先摘 message，摘不到才退回原文。
        """
        payload = self._chat.payload
        readable = _payload_error(payload) if self._chat.status >= 400 else None
        if readable:
            return readable
        if self._chat.error:
            return str(self._chat.error)[:500]
        if self._chat.status >= 400:
            return f'上游返回状态 {self._chat.status}'
        return None

    async def aclose(self) -> None:
        await self._chat.aclose()


async def call(*, adapter: PlatformAdapter, model: str, body: dict,
               ctx: platforms.ChatContext) -> PlatformCall:
    """调一次平台/组合，并归一化成「JSON 结果」或「OpenAI SSE 字节流」。

    `PlatformError` 原样抛出：各协议层要用自己的错误体（Responses 的
    `{"error": ...}` 与 Anthropic 的 `{"type":"error", ...}` 不同）回给客户端。
    """
    chat = await adapter.chat(model, body, ctx)
    stream: AsyncIterator[bytes] | None = None
    if chat.raw_stream is not None:
        # WorkBuddy 反代：上游字节逐字节透传，不重新编码。
        stream = chat.raw_stream
    elif chat.stream is not None:
        stream = _frames(chat)
    return PlatformCall(chat, adapter, model, stream)
