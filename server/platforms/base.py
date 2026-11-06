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


#: 区域化模型前缀的后缀（db 口径）：国内版 → `cn`，国际版 → `com`。
#:
#: 国际版用 `com` 而不是 `intl` / `global`：`trae.com` / `qoder.com` 是国际站
#: 域名，客户端一眼能分辨；也避免和第二套 `intl:` / `global:` 路由前缀混在一起。
REALM_PREFIX_SUFFIX = {'cn': 'cn', 'global': 'com'}

_REALM_ALIASES = {
    'cn': 'cn', 'china': 'cn', 'domestic': 'cn', '国内': 'cn',
    'global': 'global', 'intl': 'global', 'international': 'global',
    'us': 'global', '海外': 'global', '国际': 'global',
}


def normalize_db_realm(value: object) -> str | None:
    """任意版本写法 → 统计口径（`cn` / `global`）；认不出返回 None。

    与各适配器内部的协议口径（Trae / Qoder 用 `cn` / `intl`）解耦：跨层传递
    的版本提示统一走这里，适配器自己再翻译成协议值。
    """
    return _REALM_ALIASES.get(str(value or '').strip().lower())


def realm_prefix_suffix(realm: object) -> str | None:
    """版本 → 区域化前缀后缀；认不出返回 None（调用方退回旧前缀）。"""
    normalized = normalize_db_realm(realm)
    return REALM_PREFIX_SUFFIX.get(normalized) if normalized else None



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


@dataclass(frozen=True)
class ChatContext:
    """一次调用的**请求上下文**，供需要「按密钥 / 来源」出网的适配器使用。

    绝大多数平台用不上它：Trae 选了账号之后自己出网，与调用方是谁无关。
    但 WorkBuddy 作为组合成员时，请求仍然要按**这把密钥**绑定的分组去反代——
    那条信息只在网关层才有（`_authorize` 解析出来的 key），适配器自己拿不到。
    所以由网关显式传下来，而不是让适配器去猜默认上游（那会破坏密钥的分组隔离）。

    `ip` / `ua` 留给「需要按调用方区分」的适配器（粘性、审计）。WorkBuddy
    目前用不到：它反代时上游看到的是**面板自己的出口 IP**，不是终端用户的
    （原直连路径也是如此，不额外伪造 `X-Forwarded-For`）。

    `upstream_path` 保留客户端打的那条入站路径（`/v1/chat/completions` 或
    `/v2/chat/completions`，两条都是既有路由），与直连 WorkBuddy 时一致：
    成员不该因为被组合包了一层就换协议版本。
    """

    key: dict | None = None
    ip: str = ''
    ua: str | None = None
    upstream_path: str = '/v1/chat/completions'
    # 路由从**区域化模型名前缀**（`traecn/...` / `traecom/...`）解析出的版本
    # 提示，db 口径（`cn` / `global`）。None = 调用方没指定，适配器照旧按目录 /
    # 密钥 / 默认区域选号。
    realm: str | None = None


@dataclass
class PlatformChat:
    """一次对话调用的结果。

    三种形态互斥：

      · `payload` 非空     → 非流式，路由直接 JSONResponse(payload)；
      · `stream` 非空      → 流式，**OpenAI chunk**，路由把每个 dict 拼成一帧 SSE；
      · `raw_stream` 非空  → 流式，**已经拼好的字节**，路由原样透传。

    为什么会有 `raw_stream`：WorkBuddy 的反代把上游 SSE **逐字节透传**（见
    `test_gateway_passthrough`），解析成 dict 再拼回去会改变字节、丢掉未知帧，
    属于协议失真。把它做成独立形态，而不是「让适配器自己拼帧」，网关才知道
    该走哪条输出路径。

    `usage` 在流式场景下由适配器在结束时填充（同一个 dict 对象），网关在
    generator 收尾时读取它记账。`closer` 负责释放上游连接。
    """

    status: int = 200
    payload: dict | None = None
    stream: AsyncIterator[dict] | None = None
    raw_stream: AsyncIterator[bytes] | None = None
    content_type: str = 'text/event-stream'
    usage: dict = field(default_factory=dict)
    closer: Callable[[], Awaitable[None]] | None = None
    model: str = ''
    error: str | None = None
    # 上游实际用的账号（仅用于日志展示，不对外暴露）
    account_hint: str = ''
    # 本次调用实际走向的版本（db 口径：`cn` / `global`）。
    #
    # 为什么不能只靠模型名：WorkBuddy 的版本写在模型名前缀里（`global:`），
    # 但 Trae 的模型名是 `trae/<model>`，同一条目在两个版本里都可能存在，
    # 版本由**选中的账号**决定。为 None 时网关回退到 `db.realm_of_model`，
    # 保持 WorkBuddy / 组合成员原有的判定口径。
    realm: str | None = None
    # 首字延迟（毫秒）：原始字节流由适配器自己扫描，网关读这里记账
    first_token_ms: int | None = None

    @property
    def streaming(self) -> bool:
        return self.stream is not None or self.raw_stream is not None

    async def aclose(self) -> None:
        if self.closer is not None:
            try:
                await self.closer()
            except Exception:  # noqa: BLE001 —— 收尾失败不该再抛给客户端
                pass
        # 原始字节流没有经过 `openai_sse`，取消 / 提前退出时得在这里收尾，
        # 否则上游连接要等到 GC 才释放（大响应会一直占着池子）。
        raw = self.raw_stream
        closer = getattr(raw, 'aclose', None)
        if closer is not None:
            try:
                await closer()
            except Exception:  # noqa: BLE001
                pass


class PlatformAdapter(ABC):
    """一个平台的全部能力。

    `name` 同时是网关的模型前缀（`trae/...`），必须全局唯一且只含
    `[a-z0-9_-]`：它会出现在模型名里，客户端会原样回传。
    """

    name: str = ''
    label: str = ''
    supports_checkin: bool = False
    # 能否在网页里直接发起登录（需要平台提供 login_start / login_poll）。
    supports_web_login: bool = False
    # 账号是否分「国内版 / 国际版」（Trae）。为 True 时前端会露出右上角的版本
    # 切换，并按账号的 realm 过滤列表、把当前版本传给导入 / 登录接口。
    realm_aware: bool = False
    # 是否出现在平台下拉 / 账号页里。组合是**虚拟平台**：它只借用网关的
    # 路由前缀（`combo/...`），没有自己的账号与凭据，露在界面上只会让人
    # 以为「这里要导入账号」。
    ui_visible: bool = True
    # 能否作为「模型组合」的成员出现在组合编辑页。WorkBuddy 的适配器就是
    # 只为这件事存在的：它不出现在平台下拉（没有自己的账号页），但组合要能把
    # 请求分一份给整池。
    combo_member: bool = False
    # 是否参与网关的**模型名前缀解析**（`<name>/<model>`）。WorkBuddy 的适配器
    # 设成 False：它只给组合用。若可被前缀命中，客户端就能直接用
    # `workbuddy/glm-5.2` 调用，而这条路径绕过了「这次该走哪个分组」的
    # 直连逻辑，等于多开一个语义重复的入口。
    routable: bool = True
    # 账号权重是否只在上游**池内**生效（WorkBuddy：整池由 workbuddy2api 调度）。
    # 组合成员的 `weight` 是「选中这个成员的概率」，与池内账号权重不是一个口径。
    # 仅用于界面提示，不影响路由。
    pool_weight: bool = False

    def has_accounts_hint(self) -> bool:
        """本地是否**看起来**有账号（不联网、不启动子服务的廉价判断）。

        默认 True：多数平台的账号就在本进程的目录 / 内存里，读一下很便宜。
        内置网关型平台（Qoder）要覆盖它——`list_accounts()` 会顺手把网关进程
        内的服务拉起来，而「没有任何 Qoder 账号」的部署不该因为一条每日签到或
        令牌巡检就多跑一个回环端口与后台线程。
        """
        return True

    @property
    def model_prefix(self) -> str:
        return f'{self.name}/'

    def model_prefix_for(self, realm: object = None) -> str:
        """该版本对外的模型 ID 前缀（`traecn/` / `traecom/`）。

        `realm=None` 或非分版本平台退回旧的 `<name>/`：存量客户端、白名单与
        组合成员写的都是这个名字，必须继续有效。区域化前缀只是**新增别名**，
        不是替换——旧名字仍然能解析（见 `registry.route_model`）。
        """
        suffix = realm_prefix_suffix(realm) if self.realm_aware else None
        return f'{self.name}{suffix}/' if suffix else self.model_prefix

    def public_model_id(self, bare_model: str, realm: object = None) -> str:
        """裸模型名 → 对外模型 ID（按区域加前缀）。"""
        return f'{self.model_prefix_for(realm)}{bare_model}'

    def realm_of_prefix(self, head: object) -> str | None:
        """平台名前缀 → 版本（db 口径）：`traecn` → `cn`，`traecom` → `global`。

        只对 `realm_aware` 平台生效；返回 None 表示「这不是我的区域前缀」。
        """
        if not self.realm_aware:
            return None
        text = str(head or '').strip().lower()
        for suffix, realm in (('cn', 'cn'), ('com', 'global')):
            if text == f'{self.name}{suffix}':
                return realm
        return None

    def record_name(self, bare_model: str) -> str:
        """记账时该写进 `mapped_model` 的名字。

        默认是 `<平台>/<模型>`——它决定日志的 `platform` 列（见网关 `_record`）。
        WorkBuddy 是例外：它在上游侧的模型名就是裸名（`glm-5.2` / `global:x`），
        写成 `workbuddy/glm-5.2` 会让统计把它算到另一个平台名下。
        """
        return f'{self.model_prefix}{bare_model}'

    def realm_of_model(self, model: str) -> str | None:
        """模型名能确定的版本（db 口径 `cn` / `global`）。

        只给**失败路径**兜底用：成功调用以 `PlatformChat.realm` 为准（那才是
        实际用了哪个账号）。返回 None 表示「名字里看不出」，网关会退回
        `db.realm_of_model` 的旧口径。默认实现就是 None —— WorkBuddy 的版本
        写在模型名前缀里，由网关统一判即可。
        """
        return None

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

    async def credits(self, account_id: str) -> dict:
        """积分余额快照（**可选能力**）。

        没有积分概念的平台沿用这个默认实现：调用方拿到的是明确的
        「不支持」，而不是一个看起来像真的 0 —— 0 会被读成「积分用完了」。
        返回结构由平台自己定，但至少要有 `uid` 与 `credits`。
        """
        raise PlatformError(f'{self.label} 不支持查询积分', status=400,
                            code='credits_unsupported')

    def set_account_options(self, account_id: str, *, weight=None) -> dict:
        """修改账号级调度参数（**可选能力**）。

        目前只有「权重」一项：它决定池内选号时这个号被选中的相对比例。
        没有调度参数概念的平台沿用默认实现，返回明确的「不支持」—— 静默
        成功会让界面显示一个其实没有落库的值。
        """
        raise PlatformError(f'{self.label} 不支持修改账号调度参数', status=400,
                            code='account_options_unsupported')

    def set_account_disabled(self, account_id: str, disabled: bool,
                             reason: str = '') -> dict:
        """临时冻结 / 恢复账号（**可选能力**）。

        冻结只影响调度与批量任务，不删除凭据——界面上的「停用」走的正是这条。
        不支持该概念的平台沿用默认实现（明确的 400），而不是静默成功。
        """
        raise PlatformError(f'{self.label} 不支持停用账号', status=400,
                            code='account_disable_unsupported')

    def set_account_cooling(self, account_id: str, until: float,
                            reason: str = '') -> dict:
        """强制冷却到某个时刻（epoch 秒，**可选能力**）。"""
        raise PlatformError(f'{self.label} 不支持设置冷却', status=400,
                            code='account_cooling_unsupported')

    def clear_account_cooling(self, account_id: str) -> dict:
        """解除冷却（**可选能力**）。"""
        raise PlatformError(f'{self.label} 不支持设置冷却', status=400,
                            code='account_cooling_unsupported')

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
    async def chat(self, model: str, body: dict,
                   ctx: ChatContext | None = None) -> PlatformChat:
        """执行一次对话。`model` 已经剥掉平台前缀。

        `ctx` 是请求上下文（调用方是谁、来源 IP、原始上游路径）。只有需要
        「按密钥 / 来源出网」的平台会用到它（WorkBuddy 成员）；其余平台忽略即可。
        缺省 None 表示「没有上下文」——那时按平台自己的默认策略处理。
        """
