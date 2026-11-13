"""会话亲和（prompt cache 亲和）：把同一段对话钉到同一个上游目标。

## 为什么需要

上游的 prompt cache 按**账号 / 平台**隔离：同一段对话换一个上游目标，前缀缓存
就全部作废。三处选路都有这个问题：

  · 组合（`combo/<name>`）按权重在多个成员平台间轮换，等权两成员时几乎每轮
    换一次；
  · Trae 在同一平台的多个账号间轮换，多账号时同样会打散缓存；
  · Qoder 的真实选号在内置网关里，客户端只给 `prompt_cache_key` 时厂商认不出
    （它只认自家的会话 id / 消息前缀），需要把键显式透传过去。

所以「选一次、后面复用」：第一个请求按原策略（SWRR）选出目标，用会话键把绑定
记下来，后续同一会话直接复用，不再推进轮询游标（否则一个长会话会把全局比例
带偏）。新会话仍然参与加权选路，长期分摊比例不受影响。

## 会话键从哪来

优先级：`prompt_cache_key` → body 里的会话 id → `metadata` 里的会话 id →
首条 user 消息的哈希。**不用 `user`**：它通常是终端用户而不是会话维度，单用户
部署里恒定，拿它当键会让所有会话永久粘在第一个目标上。

键长度受客户端控制，所以超长键会转成定长哈希再入表；绑定表本身有 TTL 与容量
上限，过期 / 超限按最早到期淘汰。
"""
from __future__ import annotations

import hashlib
import threading
import time

# 30 分钟没有新请求的会话视为结束，下一次重新按权重选。
AFFINITY_TTL = 30 * 60
# 高基数会话的容量上限，防止进程内存被撑大。
AFFINITY_MAX = 2048
# 超长会话键改成定长哈希（仍然稳定），短键原样保留便于排查。
AFFINITY_KEY_MAX = 96


def _key_text(value: object) -> str:
    """把会话标识归一成非空、**定长上限**的字符串；认不出的类型当作没给。"""
    if isinstance(value, str):
        text = value.strip()
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        text = str(value)
    else:
        return ''
    if len(text) > AFFINITY_KEY_MAX:
        digest = hashlib.sha256(text.encode('utf-8', 'ignore')).hexdigest()
        return f'sha256:{digest[:32]}'
    return text


def _content_text(content: object) -> str:
    """从消息 content 里取纯文本（兼容字符串与 part 数组两种写法）。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ''
    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict):
            text = part.get('text')
            if isinstance(text, str):
                parts.append(text)
    return '\n'.join(parts)


def affinity_key(body: object) -> str:
    """从请求体里取一个**跨请求稳定**的会话标识。

    返回空串表示这条请求没有可用的会话维度，调用方按普通选路处理。
    """
    if not isinstance(body, dict):
        return ''

    for name in ('prompt_cache_key', 'conversation_id', 'conversationId'):
        key = _key_text(body.get(name))
        if key:
            return f'{name}:{key}'

    conversation = body.get('conversation')
    if isinstance(conversation, dict):
        key = _key_text(conversation.get('id'))
    else:
        key = _key_text(conversation)
    if key:
        return f'conversation:{key}'

    metadata = body.get('metadata')
    if isinstance(metadata, dict):
        for name in ('conversation_id', 'session_id', 'thread_id'):
            key = _key_text(metadata.get(name))
            if key:
                return f'metadata.{name}:{key}'

    messages = body.get('messages')
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict) or message.get('role') != 'user':
                continue
            text = _content_text(message.get('content')).strip()
            if text:
                digest = hashlib.sha256(text.encode('utf-8', 'ignore')).hexdigest()
                return f'messages:{digest[:32]}'
    return ''


def header_key(value: str) -> str:
    """把会话键转成能安全放进 HTTP 头（ASCII）的值。

    `affinity_key()` 的短键原样保留，因此可能是中文或带控制字符（客户端直接
    给了这样的 `prompt_cache_key`）。HTTP 头只接受 ASCII 可见字符，这类键改成
    定长哈希——稳定性不变，只是不再可读。空键返回空串。
    """
    if not value:
        return ''
    try:
        value.encode('ascii')
    except UnicodeEncodeError:
        safe = False
    else:
        safe = all(0x20 <= ord(ch) < 0x7f for ch in value)
    if safe:
        return value
    digest = hashlib.sha256(value.encode('utf-8', 'ignore')).hexdigest()
    return f'sha256:{digest[:32]}'


class SessionAffinity:
    """「会话键 → 目标」的绑定表，带滑动 TTL 与容量上限。

    目标是什么由调用方决定（组合成员键 / Trae 账号 uid）。`get()` 会顺带把
    活跃会话的到期时间往后推；目标被停用或失败切换成功时调用方 `bind()` 改绑，
    目标彻底不可用时 `unbind()`。

    加锁是为了兼容「同一进程里多个事件循环 / 线程」的部署形态：表操作只有
    dict 读写，锁的代价可以忽略。
    """

    def __init__(self, ttl: float = AFFINITY_TTL,
                 max_entries: int = AFFINITY_MAX) -> None:
        self.ttl = ttl
        self.max_entries = max_entries
        self._bindings: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def set_ttl(self, ttl: float) -> None:
        """改绑定的滑动 TTL（秒）。平台设置里可调，下一次请求就生效。

        只影响**之后**的续期 / 新绑定：已存在的绑定在下一次 `get()` 时按新
        TTL 续期，不会把内存里那条记录的绝对到期时间改掉——那需要遍历整张表，
        而「最多晚一个 TTL 才收紧」在实际使用中无害。
        """
        with self._lock:
            self.ttl = max(0.0, float(ttl))

    def get(self, key: str) -> str | None:
        """取绑定的目标；过期返回 None，命中则续期（滑动窗口）。"""
        if not key:
            return None
        now = time.monotonic()
        with self._lock:
            item = self._bindings.get(key)
            if item is None:
                return None
            target, expires = item
            if expires <= now:
                self._bindings.pop(key, None)
                return None
            self._bindings[key] = (target, now + self.ttl)
            return target

    def bind(self, key: str, target: str) -> None:
        if not key or not target:
            return
        now = time.monotonic()
        with self._lock:
            self._bindings[key] = (target, now + self.ttl)
            if len(self._bindings) > self.max_entries:
                self._prune(now)

    def unbind(self, key: str) -> None:
        if not key:
            return
        with self._lock:
            self._bindings.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._bindings.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._bindings)

    def _prune(self, now: float) -> None:
        """超上限时先清过期、再按最早到期淘汰（调用方必须已持锁）。"""
        for key in [k for k, (_, expires) in self._bindings.items()
                    if expires <= now]:
            self._bindings.pop(key, None)
        overflow = len(self._bindings) - self.max_entries
        if overflow <= 0:
            return
        for key, _ in sorted(self._bindings.items(),
                             key=lambda item: item[1][1])[:overflow]:
            self._bindings.pop(key, None)
