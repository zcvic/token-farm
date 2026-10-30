"""平台适配器的公共契约。

这里只定义「一个平台要接入需要提供什么」，不包含任何具体协议的实现。
网关、管理端路由都只依赖本文件的类型，因此新增平台不必改动它们。

设计要点：

  · 网关只认 OpenAI 形状。平台协议的差异（例如 Trae 永远返回 SSE）必须
    在适配器内部消化，不能泄漏到路由层。
  · 账号凭据只在适配器内部流转；对外返回的账号结构必须已经去掉明文。
  · 失败一律抛 `PlatformError`，它带着「该回给客户端的状态码」，避免每个
    调用点各自猜。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable


class PlatformError(Exception):
    """平台侧的失败：带状态码与稳定错误码，路由层直接转成 OpenAI 错误体。

    `payload` 保留上游原始响应，仅用于日志与排障——**不要**原样回给客户端：
    里面可能带内部字段，也常常不是 OpenAI 形状。
    """

    def __init__(
        self,
        message: str,
        *,
        status: int = 502,
        code: str = 'platform_error',
        retry_after: int | None = None,
        payload: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = str(message)
        self.status = int(status)
        self.code = str(code)
        self.retry_after = retry_after
        self.payload = payload

    def as_openai(self) -> dict:
        """转成 OpenAI 错误体（供网关直接返回）。"""
        return {'error': {'message': self.message,
                          'type': 'api_error' if self.status >= 500 else 'invalid_request_error',
                          'code': self.code}}


@dataclass
class PlatformChat:
    """一次对话调用的结果。

    两种形态互斥：

      · `payload` 非空 → 非流式，路由直接 JSONResponse(payload)；
      · `stream` 非空  → 流式，路由把每个 dict 拼成一帧 SSE。

    `usage` 在流式场景下由适配器在结束时填充（同一个 dict 对象），网关在
    generator 收尾时读取它记账。`closer` 负责释放上游连接。
    """

    status: int = 200
    payload: dict | None = None
    stream: AsyncIterator[dict] | None = None
    usage: dict = field(default_factory=dict)
    closer: Callable[[], Awaitable[None]] | None = None
    model: str = ''
    error: str | None = None
    # 上游实际用的账号（仅用于日志展示，不对外暴露）
    account_hint: str = ''

    @property
    def streaming(self) -> bool:
        return self.stream is not None

    async def aclose(self) -> None:
        if self.closer is not None:
            try:
                await self.closer()
            except Exception:  # noqa: BLE001 —— 收尾失败不该再抛给客户端
                pass


class PlatformAdapter(ABC):
    """一个平台的全部能力。

    `name` 同时是网关的模型前缀（`trae/...`），必须全局唯一且只含
    `[a-z0-9_-]`：它会出现在模型名里，客户端会原样回传。
    """

    name: str = ''
    label: str = ''
    supports_checkin: bool = False

    @property
    def model_prefix(self) -> str:
        return f'{self.name}/'

    # ── 账号 ────────────────────────────────────────────────
    @abstractmethod
    def list_accounts(self) -> list[dict]:
        """账号列表；**不含** token / refreshToken 明文。"""

    @abstractmethod
    def import_account(self, payload: dict) -> dict:
        """导入或更新一个账号，返回脱敏后的账号。"""

    @abstractmethod
    def delete_account(self, account_id: str) -> bool:
        """删除账号；不存在返回 False。"""

    @abstractmethod
    async def refresh_account(self, account_id: str, *, force: bool = False) -> dict:
        """刷新令牌。轮换式平台必须在返回前把新令牌原子落盘。"""

    # ── 任务 ────────────────────────────────────────────────
    @abstractmethod
    async def checkin(self, account_id: str) -> dict:
        """执行该平台的每日任务（签到 / 保活）。"""

    @abstractmethod
    async def usage(self, account_id: str) -> dict:
        """额度 / 用量快照。"""

    # ── 网关 ────────────────────────────────────────────────
    @abstractmethod
    async def list_models(self, *, refresh: bool = False) -> list[dict]:
        """该平台可调模型，结构至少含 `id`。

        `id` **带**平台前缀（`trae/deepseek-V3`）：这份列表会原样并进
        `/v1/models`，而客户端拿到什么名字就会回传什么名字——前缀在这里
        加一次，网关才不用在合并时再补一遍、也不会出现两处前缀拼法不一致。
        裸模型名另由 `model_name` 字段给出，供界面展示与上游调用。
        """

    @abstractmethod
    async def chat(self, model: str, body: dict) -> PlatformChat:
        """执行一次对话。`model` 已经剥掉平台前缀。"""
