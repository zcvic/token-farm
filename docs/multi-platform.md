# 多平台接入（token farm）

本文说明把 `workbuddy-manager` 从「WorkBuddy 专用管理面板」扩展成
「多平台账号池 + 任务 + 反代网关」的架构、边界与落地路线。

第一个原生接入的平台是 **Trae**（国内版 / 国际版）。WorkBuddy 的既有链路
**完全不改**：仍然由随包的 `workbuddy2api`（Go）持有账号池，面板只做管理面与
前置反代。第三个平台是 **Qoder**（国内版 / 国际版），协议层以源码副本内置在
进程里（`platforms/qoder/_vendor/`），同样不需要额外部署。第四个平台是
**Loomy**（讯飞星火），原生实现、单区域（`loomy/` 前缀），账号服务与模型网关
都由进程内代码直接访问。随后又按同一契约原生实现了 **DuMate**（百度搭子，
单区域 `dumate/` 前缀，百度 Passport 扫码或网页 Cookie 凭证）与
**MiniMax Code**（双区域，`minimaxcn/` / `minimaxcom/` 前缀，OAuth 设备码 +
一次性 refresh token）。

最新一批是三个「OpenAI 兼容直连 + 账号池」平台，都不引入第三方运行时：

- **Discovery**（Intern TokenPlan，`discovery/` 前缀）：凭证是 API Key，没有
  refresh；额度只能靠 `GET /v1/models` 探针判断。
- **Kilo**（`kilo/` 前缀）：官方设备授权码登录拿一年期 token，对话走
  `ai-gateway.kilo.ai/api/v1`（OpenRouter schema）。
- **Cline**（`cline/` 前缀）：WorkOS 设备授权码换一次性 refresh token；免费档
  只认流式，适配器强制走流式再聚合。

三者都是单区域（`realm_aware=False`），共用一套「权重 SWRR + LRU + 会话亲和」
选号策略。

再往后是 **豆包（Doubao）**（`doubao/` 前缀，详见 §14）：凭证是网页 Cookie
（抓包导入，没有扫码登录），对话端点固定返回 SSE，模型表是内置快照而不是
在线目录，上游不返回 usage（按字符 ÷ 1.7 估算）。

**商汤小浣熊（Raccoon）**（`raccoon/` 前缀，详见 §16）：JWT access + 一次性
refresh，模型目录在线拉取，对话是标准 OpenAI Chat Completions；签到没有独立
领取接口，靠读一次桌面端 `setting_info` 触发后核对积分账单。

---

## 1. 目标与非目标

### 目标

1. 一个进程、一套密钥体系、一张请求日志表，同时服务多个上游平台。
2. 每个平台提供两类能力：
   - **任务**：登录态保活 / 每日签到 / 额度查询；
   - **反代**：把平台的私有协议翻译成 OpenAI 兼容 API。
3. 新平台接入只需要新增一个 `PlatformAdapter` 实现，不改网关主流程。
4. WorkBuddy 的既有行为、接口、日志口径保持逐字不变（存量部署升级后无感）。

### 非目标

- 不做跨平台账号迁移（WorkBuddy 的 `auths/*.json` 不搬到 Trae 目录）。
- 不做统一计费（各平台额度单位不同，日志里分平台记录即可）。
- 不接管 `workbuddy2api` 的账号池：它仍是 WorkBuddy 的唯一事实来源。

---

## 2. 现状与目标架构

### 现状（仅 WorkBuddy）

```
client ──/v1/chat/completions──> token-farm (FastAPI)
                                    │ 鉴权/IP/限流/日志
                                    └──> workbuddy2api (Go, 127.0.0.1:7863)
                                            └── 腾讯账号池
```

### 目标（多平台）

```
client ──/v1/chat/completions──> token-farm
                                    │ 鉴权/IP/限流/日志  （共用，唯一入口）
                                    ├── model 无平台前缀 ──> workbuddy2api（旧行为）
                                    ├── model = "traecn/xxx" / "traecom/xxx" ─> TraeAdapter
                                    │                           ├── 账号池（本地文件）
                                    │                           ├── token 轮换刷新
                                    │                           └── llm_raw_chat（SSE）
                                    ├── model = "qodercn/xxx" / "qodercom/xxx" ─> QoderAdapter
                                    │                                           └── 内置网关（回环随机端口）
                                    │                                                 ├── 双区账号池
                                    │                                                 └── COSY 签名 / 设备授权 / 签到
                                    ├── model = "loomy/xxx" ─> LoomyAdapter
                                    │                           ├── 账号池（本地文件）
                                    │                           ├── 手机号密码登录 / 会话重登
                                    │                           └── OpenAI 兼容网关（SSE 原样透传）
                                    ├── model = "dumate/xxx" ─> DuMateAdapter
                                    │                           ├── 账号池（网页 Cookie）
                                    │                           └── 百度搭子网关（OpenAI 兼容 SSE）
                                    ├── model = "minimaxcn/xxx" / "minimaxcom/xxx" ─> MinimaxAdapter
                                    │                                           ├── 双区账号池（OAuth refresh token）
                                    │                                           ├── Anthropic Messages 端点（内部翻译）
                                    │                                           └── 签到 / 积分 / M Plan 窗口
                                    ├── model = "discovery/xxx" ─> DiscoveryAdapter
                                    │                           ├── 账号池（API Key）
                                    │                           └── /v1/models 额度探针（429 quota_exceeded → 402）
                                    ├── model = "kilo/xxx" ─> KiloAdapter
                                    │                           ├── 账号池（设备码换来的 token）
                                    │                           └── ai-gateway.kilo.ai（OpenAI 兼容 SSE）
                                    ├── model = "cline/xxx" ─> ClineAdapter
                                    │                           ├── 账号池（一次性 refresh token）
                                    │                           └── api.cline.bot（免费档强制流式 + 聚合）
                                    └── model = "doubao/xxx" ─> DoubaoAdapter
                                                                ├── 账号池（网页 Cookie）
                                                                └── www.doubao.com（固定 SSE + 签到 / 额度）
```

平台路由**只由模型名决定**，不由密钥决定。原因：一把密钥可能既想调
WorkBuddy 又想调 Trae；按密钥绑平台会让用户为每个平台各建一把密钥，而额度、
IP 白名单、日志口径又得重复配置一遍。

---

## 3. 目录结构

```
server/platforms/
  __init__.py            # 对外只暴露 registry + openai_sse
  base.py                # PlatformAdapter 契约 / ChatContext / PlatformError / PlatformChat
  registry.py            # 注册表 + 模型前缀解析（跳过 routable=False）
  sse.py                 # OpenAI chunk → SSE 帧（含错误帧 / 收尾记账回调）
  workbuddy/
    adapter.py           # WorkBuddy 组合成员（ui_visible / routable 均为 False）
  combo/
    adapter.py           # 模型组合（虚拟平台，ui_visible=False）
  trae/
    __init__.py
    constants.py         # 双区主机、路径、App-Id、UA
    fingerprint.py       # 由 uid 稳定派生设备指纹
    catalog.py           # 模型目录三层合并（导入 > 客户端快照 > 内置）
    model_catalog.json   # 目录内置默认值（脚本可刷新）
    rates.py             # 积分倍率三层合并
    model_rates.json     # 倍率内置默认值（脚本可刷新）
    store.py             # 账号文件（原子写 + 文件锁）
    client.py            # 上游协议层（httpx 异步）
    chat.py              # SSE → OpenAI chunk / chat.completion
    icube.py             # 客户端凭据 blob 的 AES 编解码
    desktop.py           # 从本机客户端导入、并把新凭据回写客户端
    adapter.py           # 实现 PlatformAdapter：账号/签到/额度/模型/对话
  qoder/
    __init__.py
    adapter.py           # 实现 PlatformAdapter：账号/签到/额度/模型/对话
    client.py            # 内置网关的 HTTP 客户端（回环端口）
    mapping.py           # 网关字段 → 平台账号 / 模型 / 积分 / 签到
    embedded.py          # 在回环随机端口拉起内置网关（懒加载）
    _vendor/             # qoder2api-hub 的源码副本（相对导入，见其 README）
    tools/vendor_qoder.py # 重新 vendor / 校验导入改写
  loomy/
    __init__.py
    constants.py         # 网关 / 账号服务地址、签名 AK、模型别名与兜底目录
    crypto.py            # 登录用 RSA/PKCS#1 v1.5 加密（纯标准库）
    signer.py            # 账号服务 HMAC-SHA1 签名
    store.py             # 账号文件（原子写 + 文件锁 + 脱敏）
    client.py            # 上游协议层（登录 / 模型 / 积分 / 签到 / 对话）
    wechat.py            # 微信开放平台「网站应用」扫码（取码 / 长轮询 / 换 session）
    login.py             # 扫码登录会话状态机（服务端取码 + 粘回调兜底）
    desktop.py           # 从本机客户端读已登录会话（只读，不写回）
    adapter.py           # 实现 PlatformAdapter：账号/签到/积分/模型/对话
  dumate/
    __init__.py
    catalog.json         # 内置模型目录快照（在线 / 桌面端拿不到时兜底）
    constants.py         # 站点 / 网关 / 业务路径 / 输出上限
    store.py             # 账号文件（原子写 + 文件锁 + 脱敏）
    client.py            # 网页接口换 token / 对话 / 签到 / 积分
    login.py             # 网页登录：百度 Passport 扫码（取码 / 长轮询 / BDUSS 兑换）
    catalog.py           # 目录三层降级：桌面端配置 → 进程缓存 → 快照
    adapter.py           # 实现 PlatformAdapter：账号/签到/积分/模型/对话
  minimax/
    __init__.py
    constants.py         # 双区站点 / OAuth / 签名常量 / 模型快照与上限
    store.py             # 账号文件（原子写 + 文件锁 + 脱敏）
    catalog.py           # 在线模型配置解析（`providers[minimax]`）
    translate.py         # OpenAI Chat ↔ Anthropic Messages 请求 / 响应 / SSE
    client.py            # OAuth 设备码 / 签名账号接口 / 对话端点
    login.py             # 设备码登录后台线程（PKCE + 轮询）
    adapter.py           # 实现 PlatformAdapter：双区选号 / 轮换 / 签到 / 积分 / 对话
  discovery/
    __init__.py
    constants.py         # 网关地址、默认模型 `auto`、额度探针错误码
    store.py             # API Key 账号文件（原子写 + 文件锁 + 脱敏）
    catalog.py           # 在线目录解析 + 内置 8 条兜底
    client.py            # `/v1/models` 额度探针 / 对话（流式与非流式）
    adapter.py           # 实现 PlatformAdapter：选号 / 冷却 / 探活 / 对话
  kilo/
    __init__.py
    constants.py         # 账号侧 / 模型侧两套域名、伪装头、一年期 token
    store.py             # token 账号文件（原子写 + 文件锁 + 脱敏）
    catalog.py           # OpenRouter schema 目录归一 + 自动路由兜底
    client.py            # 设备码（同步 / 异步）/ 资料 / 余额（美元）/ 对话
    login.py             # 设备授权码登录后台线程（用 `code` 轮询）
    adapter.py           # 实现 PlatformAdapter：选号 / 余额 / 探活 / 对话
  cline/
    __init__.py
    constants.py         # 上游 / WorkOS 设备码、伪装头、强制流式前缀
    store.py             # refresh token 账号文件（原子写 + 文件锁 + 脱敏）
    catalog.py           # 免费档 + 全量目录合并（免费排前面）
    client.py            # WorkOS 设备码 / register / token 轮换 / SSE 聚合
    login.py             # WorkOS 设备码登录后台线程
    adapter.py           # 实现 PlatformAdapter：选号 / 轮换 / 强制流式 / 对话
  doubao/
    __init__.py
    constants.py         # 上游地址 / 查询参数 / 客户端版本 / 模型表与别名
    store.py             # Cookie 账号文件（原子写 + 文件锁 + 脱敏 + 设备指纹）
    chat.py              # 请求体构造 + SSE → OpenAI chunk / chat.completion
    catalog.py           # 内置目录归一（豆包无在线目录接口）
    client.py            # 对话 / 签到 / 额度（Cookie 鉴权 + 设备查询参数）
    adapter.py           # 实现 PlatformAdapter：选号 / 冷却 / 探活 / 签到 / 对话
  lingxi/
    __init__.py
    constants.py         # 双基址 / 客户端指纹 / 内置模型快照与别名
    store.py             # Cookie 账号文件（原子写 + 文件锁 + 脱敏 + 设备号）
    catalog.py           # `/sessions/plans` 解析 + 内置快照兜底
    chat.py              # 请求体归一 + SSE → OpenAI chunk / chat.completion
    client.py            # 三步握手 / run 缓存 / 余额 / 任务 / 探活
    adapter.py           # 实现 PlatformAdapter：选号 / 冷却 / 签到 / 余额 / 对话
  raccoon/
    __init__.py
    constants.py         # 站点 / 路径 / 请求头 / 令牌寿命 / 内置模型快照
    store.py             # JWT 账号文件（原子写 + 文件锁 + 脱敏 + 设备号）
    catalog.py           # 在线目录归一 + 内置快照兜底
    client.py            # 刷新 / 目录 / 对话 / 积分 / 账单 / 每日发放触发
    desktop.py           # 从本机 `.box-agent/config/auth.json` 读登录态（只读）
    adapter.py           # 实现 PlatformAdapter：选号 / 轮换 / 签到 / 积分 / 对话
  server/services/wbforward.py  # WorkBuddy 反代传输层（网关直连与组合成员共用）
```

新增平台的模板：

```python
# server/platforms/foo/adapter.py
class FooAdapter(PlatformAdapter):
    name = 'foo'
    label = 'Foo'
    model_prefix = 'foo/'
    supports_checkin = False
    ...
registry.register(FooAdapter())
```

---

## 4. 适配器契约

`server/platforms/base.py`：

| 成员 | 说明 |
| --- | --- |
| `name` | 路由前缀（`trae`），全局唯一 |
| `label` | 界面上显示的名字 |
| `model_prefix` | `f'{name}/'`，旧式兼容前缀 |
| `model_prefix_for(realm)` | 区域化前缀：国内版 `traecn/`、国际版 `traecom/`；`realm=None` 退回旧前缀 |
| `public_model_id(model, realm)` | 裸模型名 → 对外模型 ID（按区域加前缀） |
| `realm_of_prefix(head)` | `traecn` → `cn`、`traecom` → `global`（仅 `realm_aware` 平台） |
| `supports_checkin` | 该平台是否有「每日签到」 |
| `supports_web_login` | 能否在网页里直接发起登录（需要平台实现 `login_start` / `login_poll`） |
| `supports_tasks()` | **可选**：是否有平台任务面板（签到之外的邀请码 / 兑换码等，见 §4.1） |
| `list_accounts()` | 不含明文凭据的账号列表 |
| `export_accounts()` | **可选**：导出含明文凭据的账号行，只由管理员导出接口调用（默认不支持） |
| `import_account(payload)` | 导入 / 更新一个账号 |
| `delete_account(id)` | 删除 |
| `refresh_account(id, force)` | 刷新令牌（轮换式必须原子落盘） |
| `checkin(id)` | 执行签到，返回结构化结果 |
| `usage(id)` | 额度 / 用量快照 |
| `list_models()` | 该平台可调模型（`id` 为区域化前缀 + 裸名，`model_name` 为裸名） |
| `chat(model, body, ctx)` | 返回 `PlatformChat`；`ctx` 是请求上下文（`key` / `ip` / `ua` / `upstream_path` / `realm`），不需要的平台忽略 |
| `upstream_model(model)` | **可选**：成员名 → 上游线上名（Loomy 的别名映射、Qoder 剥 `global:` 前缀）。默认原样 |
| `chat_body(model, body)` | 复制请求体并写入线上名。**适配器不要自己写 `body['model']`**：漏改会让组合把 `combo/fast` 原样发给上游，而报错完全指不到这里 |
| `ui_visible` / `combo_member` / `routable` | 是否出现在平台下拉 / 组合成员候选 / `<平台>[cn|com]/<模型>` 前缀解析（见 §12.1） |
| `pool_weight` | 账号权重是否只在上游**池内**生效（WorkBuddy 为 `True`，界面标注「池级权重」） |
| `record_name(bare_model)` | 记账用的 `mapped_model`：默认 `<平台>/<模型>`；WorkBuddy 为裸名 |
| `import_from_desktop()` | **可选**：从本机客户端一键导入（没有该方法的平台，管理端会回 400） |
| `set_account_options(id, weight)` / `set_account_disabled` / `set_account_checkin_only` | **可选**：账号级调度参数（权重 / 停用 / 仅签到）。基类默认抛 400，`/api/platforms` 的 `supports_*` 据此决定是否露出入口 |
| `set_account_proxy(id, proxy)` | **可选**：账号级出口线路（空串 = 跟随号池，`__direct__` = 直连）。原生选号的平台都实现了；Qoder 的选号在内置网关里，不提供 |
| `set_account_cooling` / `clear_account_cooling` | **可选**：强制冷却（定时 / 永久）与解除 |
| `login_start/poll/cancel` | **可选**：网页登录（`login_start` / `login_poll` / `login_cancel` / `login_submit_callback`，见下节） |

`PlatformChat` 有三种形态：

- **非流式**：直接给 `payload`（已经是 OpenAI 形状的 `chat.completion`）；
- **流式（chunk）**：给 `stream`，异步产出 OpenAI `chat.completion.chunk` 字典，
  由 `platforms/sse.py` 拼成 SSE 字节流（网关不拼帧——`routers/gateway.py`
  里「只透传上游 chunk」的架构约束由 `test_gateway_passthrough` 逐行守着）；
- **流式（原始字节）**：给 `raw_stream`，已经是**拼好的 SSE 字节**，网关原样透传。
  WorkBuddy 用这一形态：解析成 dict 再拼回去会改变字节、丢掉未知帧。

这样协议差异（Trae 永远返回 SSE、WorkBuddy 返回标准 SSE）被关在适配器内，
网关只认 OpenAI 形状。

管理端端点：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/platforms` | 平台清单与能力（含账号数） |
| GET | `/api/platforms/{name}/accounts` | 脱敏账号列表 |
| GET | `/api/platforms/{name}/accounts/export` | 导出账号（**含明文凭据**；仅会话管理员，写审计） |
| POST | `/api/platforms/{name}/accounts/import` | 手工导入（单条或 `{accounts:[...]}`） |
| POST | `/api/platforms/{name}/accounts/import-desktop` | 从本机客户端导入 |
| POST | `/api/platforms/{name}/accounts/{id}/refresh` | 刷新令牌 |
| DELETE | `/api/platforms/{name}/accounts/{id}` | 删除 |
| GET | `/api/platforms/{name}/accounts/{id}/usage` | 额度快照 |
| POST | `/api/platforms/{name}/accounts/{id}/checkin` | 单个签到 |
| POST | `/api/platforms/{name}/checkin-all` | 批量签到（并发与账号间隔按平台设置，默认并发 2、间隔 2–5 秒；单条失败不中断；跳过已冻结账号） |
| GET | `/api/platforms/{name}/settings` | 平台设置：单账号在途上限（总吞吐按可用账号数折算）+ 批量签到节奏 + 请求超时 / 换号次数 / 平台专属开关（会话亲和、桌面端回写），以及设置页顶部的本地「平台概况」（账号池状态 + 已登记积分，不发上游请求） |
| PUT | `/api/platforms/{name}/settings` | 改上述设置（可只提交要改的字段；不支持的字段会 400） |
| PATCH | `/api/platforms/{name}/accounts/{id}` | 账号调度参数（`weight` / `disabled` / `checkin_only` / `proxy`） |
| POST | `/api/platforms/{name}/accounts/{id}/cooling` | 强制冷却（定时 / 永久） |
| DELETE | `/api/platforms/{name}/accounts/{id}/cooling` | 立即解除冷却 |
| POST | `/api/platforms/{name}/login/start` | 发起网页登录，返回授权 URL |
| POST | `/api/platforms/{name}/login/{login_id}/callback` | 提交浏览器回调 URL（远程部署兜底） |
| GET | `/api/platforms/{name}/login/poll?login_id=…` | 轮询登录状态 |
| DELETE | `/api/platforms/{name}/login/{login_id}` | 取消登录（释放本机回调端口） |
| GET | `/api/platforms/{name}/models` | 模型目录（`?refresh=true` 拉线上） |

权限分四档（与账号页同口径，不是一律「已登录」）：

- **读**（平台清单 / 账号列表 / 额度 / 模型目录）→ `current_user`，只读令牌也能看；
- **添加账号**（手工导入 / 一键导入 / 网页登录 / 登录轮询与取消）→
  `require_session_account_adder`：管理员或 `operator`（只读 + 添加账号）都能用，
  但 operator 只能**新增**（重复 uid 会被拒绝），仍拒绝 API Token；
- **操作**（刷新令牌 / 签到 / 批量签到）→ `require_admin`；
- **删除账号 / 导出账号** → `require_session_admin`，与 `/api/accounts/upload` 同级：
  把外部平台凭据写进池子、从池子里删掉、或打包带走这件事只对真人开放。
  导出会写一条 `export_platform_accounts` 审计。

这些写端点已登记进 `test_api_tokens` 的 `SESSION_ONLY` 清单；`realm` /
`force` 两个 query 参数已登记进 `test_request_binding` 的
`ALLOWED_QUERY_POSTS` —— 两处都是仓库既有的「权限 / 参数形态必须显式声明」
闸门，新增接口不登记就会红。

### 账号调度：权重、冷却与临时冻结

平台账号页与 WorkBuddy 账号页保持同一套用户语义，避免同一个词在两处含义不同：

- **权重**（`weight`，0.1–100，0 = 默认 1.0）：同一平台内多个账号之间的分摊比例。
  选号用平滑加权轮询，高权重不会把低权重账号饿死。存在平台账号文件里，不需要
  重启上游，下一次选号立即生效。
- **强制冷却**（`coolingUntil` + `coolingReason`）：只暂停**选号**，到期自动恢复，
  也可随时手动解除；令牌刷新、积分查询、批量签到照常可用。永久冷却的截止时刻是
  2099-12-31，与 WorkBuddy 的口径一致。
- **仅签到**（`checkinOnly`）：退出推理选号（对话不再消耗它的积分），但签到、
  批量签到与平台任务照常执行；随时可以关掉并恢复选号。它解决的是「这个号只用来
  攒签到 / 做任务」，而不是「先别用了」——所以不要用 `disabled` 代替它。
- **临时冻结**（`disabled` + `disabledReason`）：退出选号**与批量任务**（批量签到
  会跳过它），凭据仍保留，随时可以恢复。它解决的是「这个号先别用了」而不是
  「这个号坏了」——所以界面把它归进「已停用」，而不是「需处理」。它与「仅签到」
  互斥：设冻结会清掉 `checkinOnly`，设仅签到也会清掉冻结。
- **出口线路**（`proxy`）：账号级绑定，取值与 WorkBuddy 账号一致——空 = 跟随
  号池（按「设置 → 代理」的池策略分配），`__direct__` = 强制直连，其余为线路名
  （必须存在且启用，否则保存会被拒绝）。国际站账号（MiniMax 国际版、Trae 国际版）
  常需要境外出口；改完下一次上游请求即生效。Qoder 的选号在网关进程里，不提供这个
  入口。
- **会话粘性（提示词缓存亲和）**：上游 prompt cache 按账号隔离，同一段对话每轮
  换号会让前缀缓存作废。第一个请求按权重选出账号后，按会话键（`prompt_cache_key`
  → 会话 id → 首条 user 消息哈希，见 `services/session_affinity.py`）钉住
  默认 30 分钟；命中绑定**不推进** SWRR，新会话仍按权重分摊。账号被冻结 / 冷却或
  凭据失败换号成功时改绑。绑定是进程内状态（上限 4096，按到期时间淘汰）。
  开关与时长在「设置 → 上游配置」随平台切换后可见（Trae 专属）。
- **请求预算**：`request_timeout`（单次读超时）与 `idle_timeout`（流式响应多久
  没新数据就中止）同样按平台保存；Trae 另有 `max_accounts_try`（单次对话最多
  试几个账号）与 `desktop_sync`（刷新令牌后是否写回本机客户端）。这些值默认
  沿用适配器原来的常量，保存后下一次请求立即生效，不需要重启。
- **缓存与冷却**（Trae）：模型目录缓存 `catalog_ttl`（默认 6 小时）、目录失败
  冷却 `catalog_retry_ttl`（默认 10 分钟）、隐式拉取超时 `catalog_timeout`
  （默认 10 秒）、刷新退避的起点 / 封顶 `catalog_backoff_base` /
  `catalog_backoff_max`（默认 30 秒 / 30 分钟）、积分缓存 `credits_ttl`
  （默认 60 秒）与积分查询超时 `credits_timeout`（默认 20 秒）。
  `cooldown_429` / `cooldown_5xx` 是 429 / 5xx 时冻结账号的秒数，**默认 0**：
  维持改动前的「直接上抛、不冻结也不换号」，只有大于 0 才启用强制冷却；
  `4023 model is unknown` 是模型走错通道，永不冻结账号。
- **输出与计量**（Trae）：`solo_max_tokens` 是 SOLO 通道客户端没传
  `max_tokens` 时的默认预算（思考与正文共享）；`credit_meter` 选实扣积分口径
  （不计量 / 累计差值 / 会话明细）。这两项的默认值仍实时跟随
  `TF_TRAE_SOLO_MAX_TOKENS` 与 `TF_TRAE_CREDIT_METER`——**环境变量照常生效**，
  只有面板显式保存过才以库里的值为准。灵犀也有同名的 `credit_meter`，但默认
  跟随 `TF_LINGXI_CREDIT_METER`，语义与「打开即真实扣费」见 §15.6。
- **Trae 协议开关**：原先只能用 `TF_TRAE_*` 环境变量改的十个兼容开关（原生
  工具透传、正文 / 标签调用还原、工具协议提示、退化调用判错、尾部规则、思考
  强度透传、思考内容兜底、空产出判错、严格结束判据）现在也在同一张表单里，
  同样「未保存跟随环境变量、保存后以面板为准」，改完立即生效。
- **协议兼容开关**（Qoder）：内置网关的工具残片收尾 / Anthropic 思考块 /
  结构化工具历史 / 图片预处理 / 本地 Web 工具在同一张表单的「协议兼容」里，
  默认「网关默认」= 与改动前完全一致。网关每次请求现读，保存即生效，不用重启
  内置网关；本地 Web 工具是唯一由模型驱动出网的能力，默认关闭。这个开关只切
  **总开关**——联网工具还支持「官方搜索接口」与「外部搜索 API」两种后端，切换
  后端要改 `accounts/settings.json`（见 [`docs/qoder.md`](qoder.md) 的
  「协议兼容开关」）。
- **Loomy 的设置集**：与 Trae 同构的选号 / 缓存 / 冷却参数（`max_accounts_try`、
  `min_credits`、`session_affinity`、`catalog_*`、`credits_*`、`cooldown_*`）。
  其中 `min_credits`（保底积分，默认 5000）只属于 Loomy：余额低于阈值的账号只
  作最后候选。它支持「从本机导入」，但客户端文件是**只读**的（没有会话回写），
  也没有协议兼容开关，因此不含 `desktop_sync` / `solo_max_tokens` /
  `credit_meter`。详见 [`docs/loomy.md`](loomy.md)。
- **DuMate / MiniMax Code 的设置集**：直接复用 Loomy 那一组选号 / 缓存 / 冷却
  参数（见 `server/platforms/settings.py` 的 `_DUMATE`、`_MINIMAX`）。两边都支持
  网页登录：MiniMax Code 走 OAuth 设备码，DuMate 走百度 Passport 扫码（见下节）。
  DuMate 的凭证仍是网页 Cookie，手工导入后**不联网**校验，首次刷新或对话时才
  向上游换 token。
- **Discovery / Kilo / Cline 的设置集**：同一组选号 / 缓存 / 冷却参数，集中在
  `server/platforms/settings.py` 的 `_NATIVE_POOL`（= `_LOOMY` 去掉 `min_credits`）。
  三者都没有 `min_credits`（余额口径不是积分）、没有 `desktop_sync`（不读本机
  客户端）、没有协议兼容开关。Kilo 与 Cline 保留 `catalog_timeout`（在线目录），
  Discovery 也一样——它的目录拉取同时是额度探针，超时同样要能中断。

三种状态都只写平台自己的账号文件（`data/platforms/<平台>/accounts/*.json`），
不会改动 WorkBuddy 的 `auths/`，也不会删除凭据。

### 网页登录（Trae）

「多平台 → Trae → 网页登录」会在本机开一个临时回调服务，用户在弹出的授权页
完成登录后，后端用回调里的凭据兑换令牌、拉一次用户资料并落盘，整个过程不需要
手工粘贴凭据。国内版与国际版的授权回调**形状不同**，后端按区分别处理。

链路（逆向自 Trae CN 3.4.11 / Trae 国际版 3.6.4）：

1. `login_start` 生成 `machine_id` / `device_id`、`login_trace_id`，起
   `http.server` 监听 `127.0.0.1:0`，返回授权 URL。国内版用机器维度指纹；
   国际版优先复用本机客户端身份（`telemetry.machineId` +
   `iCubeAuthInfo://icube-dc:<holderId>`），没有客户端时由面板自己生成一套
   稳定的设备身份（P-256 密钥对 + 19 位设备号 + 64 位机器号）并保存在
   `data/platforms/trae/device.json`，登录与后续刷新都用它；
2. 授权页完成后回调 `http://127.0.0.1:<port>/authorize`：
   - 国内版带 `refreshToken` / `loginTraceID` / `userRegion` / `refreshExpireAt`；
   - 国际版带 `authCodeInfo`（URL 编码的 JSON，内含一次性 `AuthCode`）与
     `loginTraceID`；
3. 后端校验 `loginTraceID`，按区兑换：
   - 国内版：`ExchangeToken`（轮换 refreshToken）；
   - 国际版：用会话内存里的 PKCE `code_verifier` 调
     `exchange_token_by_auth_code`（请求体 `AuthCode` + `CodeVerifier` +
     `DeviceInfo` + `IDEVersion`，**不带** `DeviceProof`）；
   然后统一 `GetUserInfo`（`x-cloudide-token`）→ `adapter.import_account` 落盘；
4. 前端每 2 秒轮询 `/login/poll`，`success` 后刷新账号列表。

**PKCE 按区分叉**：国际版客户端**始终**带 PKCE（`code_challenge_method=S256`），
网页登录照做；`verifier` 是 48 字节随机数的 base64url，只存在于内存会话里。
国内版仍不带 PKCE，授权页直接回 `refreshToken`，复用已有的刷新链路。早先
「国际版也可以主动省略 PKCE」的结论已作废——省略后走的是 refreshToken 分支，
兑换形状与真实客户端不一致。

**登录设备贯穿到数据面**：登录时上报并随账号落盘的 `deviceId` / `machineId`，
之后所有 IDE 数据面请求的 `X-Device-Id` / `X-Machine-Id` 都继续用这两个值，
而不是按 uid 重新派生——浏览器令牌绑定的是授权时那台设备，换成另一套设备画像
会被上游按异常处理。只有没有登录设备记录的账号（手工粘贴 / 旧版导入）才回退到
按 uid 派生。

**本机回调监听器**：只绑定 `127.0.0.1`、只处理 `GET /authorize`，并校验
`Host` / `Origin` 必须来自同一回环端口（挡 DNS rebinding 与跨站请求）；回包带
`Cache-Control: no-store` 与 `Referrer-Policy: no-referrer`，也不把查询串写进
访问日志。同时最多 16 个挂起会话，超过后新请求直接拒绝，避免反复点「开始授权」
把本机端口耗尽。

**同机约束与远程兜底**：授权页强校验 `auth_callback_url` 必须是
`http://127.0.0.1:<port>/authorize`，所以后端只能在本机回环地址上等回调。
面板与浏览器同机时链路是全自动的；远程部署时用户浏览器里的 `127.0.0.1`
指向用户自己的电脑，后端收不到回调——此时让用户把授权页失败后地址栏里
的**完整链接**粘贴到登录弹窗（`POST .../login/{login_id}/callback`）。
后端按同一套规则校验 `loginTraceID`、解析 `refreshToken` 或 `authCodeInfo`
并完成兑换，不需要域名、IP 白名单或 SSH 隧道。

**国际版已开放（2026-11-01 实测）**：

- 授权页：`https://www.trae.ai/authorization`，`client_id` 仍为
  `ono9krqynydwx5`，请求带上真实客户端的 `x_app_version=3.6.4` /
  `plugin_version=2.3.93439`，并带 `code_challenge` / `S256`；
- 授权码兑换：`POST https://grow-normal.trae.ai/trae/api/v3/oauth/ExchangeToken`，
  请求体 `ClientID` / `AuthCode` / `CodeVerifier` / `DeviceInfo` / `IDEVersion`，
  请求头 `x-cloudide-token: ""`。`DeviceInfo` 要带设备公钥、`DeviceID`
  （私钥 holderId）与 `MachineID`（`telemetry.machineId`）；
- 令牌刷新：同一路径，`ClientSecret=""`，带 `DeviceInfo` + `DeviceProof` +
  `IDEVersion`；签名原文是**秒级**时间戳、六段用 `\n` 连接（见 §11）；
- 用户资料：`GetUserInfo` 必须走 `grow-normal.trae.ai`；同账号在
  `grow-normal.traeapi.us` 会回 `20315 User Not Found`；
- 设备身份：本机有 Trae 国际版客户端时优先复用它；没有时面板自动生成
  一套稳定身份（不再要求先安装客户端），并把私钥 / 公钥 / 设备号 / 机器号
  随账号落盘，后续刷新由面板自己签 `DeviceProof`。私钥只存在 `data/` 与
  账号文件里，不进日志、不回传前端。

### 网页登录（Loomy，微信扫码 / 手机验证码）

Loomy 的网页授权主链路是微信开放平台「网站应用」扫码
（`open.weixin.qq.com/connect/qrconnect`）；另有短信验证码登录（客户端内部
接口，不跳外部页面）。回调域被微信白名单锁死在 `loomy.xunfei.cn`，**不能**
改成本机回环，所以这里扩展了契约：

- `login_start` 接受 `mode`（`qr` / `sms`）并额外返回 `qr`（二维码图片地址，
  可空）。前端看到 `qr` 就在弹窗里内嵌二维码，**不**自动打开浏览器；
- 取码是异步的：`login_start` 立刻返回 `pending` + `message="正在获取二维码…"`，
  后台线程拉授权页、从 HTML 里抠 `uuid`，之后 `login_poll` 带出 `qr`；
- 后台线程继续长轮询 `l/qrconnect`，`wx_errcode=405` 时拿 `wx_code` →
  `bind/auth` → `bind/skip` 换 session → `adapter.import_account` 落盘
  （`source=web`，没有密码）；
- `bind=0`（微信未绑手机号）不算失败：`login_poll` 返回 `status=needs_bind`，
  前端就地收手机号，`login_send_code` / `login_submit_code` 走
  `bind/sendMsg` + `bind/checkCode` 完成绑定 + 登录（`rcode` 只留在服务端）；
- `mode=sms` 时不能扫码：`login_send_code` 走 `/login/phone/sendMsgCode`、
  `login_submit_code` 走 `/login/phone/checkCode`；同一会话发码冷却 60 秒、
  最多 5 次，连错 5 次作废；
- 服务端连不上微信时退化为「打开授权页 + 粘回调」：
  `login_submit_callback` 解析 `https://loomy.xunfei.cn/oauth/wechat/callback?code=…`，
  并用 `state == login_id` 校验是不是本次会话的链接；
- 登录会话只在内存里：二维码 4 分钟、验证码阶段 15 分钟有效；`login_id`
  直接复用微信的 `state`。

### 网页登录（MiniMax Code，OAuth 设备码）

MiniMax Code 用标准 OAuth 设备码 + PKCE，没有二维码，也没有回调地址：

- `login_start(realm)` 先同步向 `account.minimax.cn` / `account.minimax.io`
  申请设备码，**拿到授权页地址再返回**，前端可以立刻 `window.open(url)`；
- 后台线程按 `interval` 轮询 `/oauth2/token`
  （`grant_type=urn:ietf:params:oauth:grant-type:device_code`），
  `authorization_pending` 继续等、`slow_down` 拉长间隔、`denied` / `expired`
  结束会话；
- 成功后用 access token 读一次账号信息，设备码登录结果按 `realUserID` 派生
  `uid` 落盘（同一用户重新登录是覆盖，不会多出一条账号）；
- refresh token 是一次性的：`adapter` 每次刷新都先落盘新的 refresh token，
  再拿去发请求；旧 token 再用会被上游判成 `invalid_grant`。

### 网页登录（DuMate，百度 Passport 扫码）

DuMate 的网页登录本质就是百度账号登录：官方登录页
（`login.bce.baidu.com`）走 Passport 的二维码协议。这里不引入 playwright，
也不要求面板和浏览器同机，服务端自己完成「取码 → 长轮询 → 兑换 → 落域」：

- `login_start` 立刻返回 `pending`，后台线程去
  `passport.baidu.com/v2/api/getqrcode` 取 `sign` + `imgurl`，再把图片取回来
  **内联成 data URL** 交给 `login_poll`：面板是静态导出且 CSP 只允许
  `self` / `data:` / `blob:` 的图片，直接给 passport 外链会被浏览器拦掉；
  面板内嵌显示（`url` 为空，不自动开浏览器）；
- `channel/unicast` 是 30 秒长轮询：`errno=1` 表示还没扫码；`errno=0` 且
  `channel_v.status=0` 时 `channel_v.v` 是一次性 bduss，`status=1` 是
  「已扫码待确认」，继续等；
- 拿到一次性 bduss 后调 `v3/login/main/qrbdusslogin` 换正式 `BDUSS` /
  `STOKEN`。响应是单/双引号混用的 JSON（也可能是 JSONP 或只靠
  `Set-Cookie`），实现里逐字段抠，不整体 `json.loads`；
- 带着 BDUSS 访问 `login.bce.baidu.com` → `/postlogin/passport/wap`，再落到
  `console.bce.baidu.com` 与 `dumate.baidu.com/app`，让服务端种下
  `bce-long-term-sessionid`。**这一步不能省**：`/api/dumate/*` 依赖 console
  会话，只有 Passport 的 BDUSS 会一直回 `10001`；
- 最后用组装好的整段 Cookie 调一次 `/api/dumate/app/token/get` 探活：
  401/403 判登录失败，错误信息会提示改用「粘贴 Cookie」；网络 5xx 只把账号
  标成「待验证」，凭证照常落盘；
- 登录会话只在内存里，二维码 5 分钟有效；成功后 `login_id` 对应的会话保留
  一段时间供前端轮询收尾。手工粘贴 Cookie 的导入路径不变，扫码不可用时仍可
  使用：账号页的「手工导入」既接受 `{"cookie": "..."}`，也接受直接粘贴整行
  `BDUSS=...; bce-long-term-sessionid=...`（前端会替你包成 JSON）；空状态里
  同时给出「网页登录」和「手工导入」两个入口，不再只提示从本机导入。

### 网页登录（Kilo，官方设备授权码）

Kilo 的登录与官方 CLI 同一套协议，没有二维码，也没有回调地址：

- `login_start()` 先**同步**向 `api.kilo.ai/api/device-auth/codes` 申请设备码，
  拿到 `verificationUrl` 再返回，前端可以立刻打开浏览器；`login_start` **不声明
  `mode` 参数**（那是 Loomy 独有的），路由按签名判断后不会传。
- 后台线程按 `interval` 轮询 `GET /api/device-auth/codes/{code}`：202 等待、
  403 拒绝、410 过期、200 通过并带 `token`。⚠️ 轮询用的是响应里的 **`code`**，
  不是 `device_code`——实测拿 `device_code` 会直接 410。
- 设备码（`user_code`）放在 `message` 文案里，前端目前没有单独渲染它的字段，
  所以文案必须自带代码，否则用户不知道要在授权页确认什么。
- 拿到 token 后读一次 `/api/profile` 补邮箱 / 组织，落盘时按一年期推算
  `expiredAt`（上游不回到期时间），界面因此有「何时该重登」的依据。

### 网页登录（Cline，WorkOS 设备授权码）

Cline 的登录是两段：先 WorkOS 授权，再用 WorkOS token 换 Cline 凭证。

- `POST api.workos.com/user_management/authorize/device`（`client_id` 是公开值）
  拿 `device_code` / `user_code` / `verification_uri_complete`；
- 后台线程 `POST .../authenticate`（`grant_type=…:device_code`）：
  `authorization_pending` 继续等、`slow_down` 拉长间隔、`access_denied` /
  `expired_token` 结束会话；
- 通过后调 `POST api.cline.bot/api/v1/auth/register`
  `{accessToken, refreshToken}` 换 Cline 的 **refresh token**——后续所有对话都
  靠它，WorkOS token **不进账号文件**；
- 换来的 refresh token 一次性轮换：适配器每次刷新都先落盘新的，再拿去发请求。

三个新平台都不实现 `login_submit_callback` / `login_submit_code`，所以前端不会
显示「粘回调 / 粘验证码」的输入框，也不会提示「必须与浏览器同机」——这两项能力
是按 `hasattr(adapter, ...)` 推导的，不是按平台名硬编码。

### 前端：国内版 / 国际版切换（Trae）

Trae 的账号按 `realm`（`cn` / `intl`）分成两池，前端沿用 WorkBuddy 已有的版本开关：

- `/api/platforms` 的每一项带 `supports_realm`（Trae 为 `true`）。只有它或
  WorkBuddy 才显示右上角的版本切换；其它平台不会出现一个不起作用的开关。
- 界面上的版本名是 `cn` / `global`（与 WorkBuddy 一致），平台账号与模型里存的是
  `cn` / `intl`。两者只在 `web/lib/platform-realm.ts` 翻译一次，禁止在组件里
  各自判断。
- 账号页、模型页与仪表盘的平台账号快照按当前版本过滤，计数 / 分页 / 统计卡都
  跟着当前版本走；请求日志与用量统计仍按平台整体统计（原因见 §5）。
- **版本选择按平台分别保存**：WorkBuddy 沿用 `wb.realm`，其它平台用
  `wb.realm.<平台名>`。把 WorkBuddy 切到国际版不会把 Trae 也带进国际版，反之
  亦然；切换平台时读的是该平台自己的历史选择。
- **只属于 WorkBuddy 的页面固定读 WorkBuddy 的版本**：密钥的版本限制、签到 /
  任务记录、扫码添加账号都不跟随顶部当前平台。否则把 Trae 切到国际版后新建
  密钥会默认变成「仅全球版」，任务页也会误显示「国际版没有任务体系」。
- 「从本机导入」把当前版本传给后端，只导入该版本客户端目录里的账号；手工导入会
  给没写 `realm` 的条目补上当前版本，避免在国际版视图里粘贴的账号落进国内版池。

### 4.1 平台任务（可选能力）

「任务」指该平台客户端里除对话之外的成长 / 福利动作——Loomy 是签到、绑定
邀请码、兑换兑换码。与 `checkin` 的分工：`checkin` 是每日固定动作，调度器会
自己跑；任务由用户**显式**触发，且需要按账号返回状态。

| 成员 | 说明 |
| --- | --- |
| `supports_tasks()` | 返回 `True` 时 `/api/platforms` 带 `supports_tasks`，账号页出现「任务」分段 |
| `task_status(id)` | **只读**：激活情况 + 最近一次同步的缓存。不得在这里替用户签到 |
| `run_task(id, action, **params)` | 执行 `checkin` / `bind_invite` / `redeem` 等动作 |

默认实现全部回 400（`tasks_unsupported`）。路由（`server/routers/platforms.py`）：

- `GET /api/platforms/{name}/accounts/{uid}/tasks` —— `current_user` 可读；
- `POST /api/platforms/{name}/accounts/{uid}/tasks/{action}` —— `require_admin`。

任务里的 `checkin` 与账号页的 `/checkin` 共用同一把「平台 + 账号」锁，避免
手动签到与批量签到同时命中同一个号、把当天的奖励记两遍。写操作一律审计
（`run_platform_task`）。

#### DuMate 的任务体系

DuMate（百度搭子）复用同一组契约，但动作集合不同：任务来自「成长计划」，
另有抽奖与「邀请有礼」。`task_status` 只读地并发拉四路（任务 / 抽奖 /
邀请码 / 邀请统计），单路失败按路回落上次缓存，错误放 `errors`：

| 动作 | 上游 | 说明 |
| --- | --- | --- |
| `checkin` | `login_bonus_info` + `claim_login_bonus` | 与账号页签到同一实现 |
| `complete_task` | `POST /activity/growth-plan/task/complete` | `task_id` 为空时只跑可自动任务（`QUERY_INPUT` / `USE_SKILL`） |
| `draw` | `POST /activity/growth-plan/draw` | 每次由后端生成新 UUID 幂等键；UI 禁连点 |
| `claim_prize` | `POST /activity/growth-plan/prize/claim` | 只给实物奖；积分 / 会员奖自动到账 |
| `bind_invite` | `POST /invitation_code/redeem` | 填写别人的邀请码，一次性；不能填自己的 |

`QUERY_INPUT` 会先用该账号发一条匹配 `query` 的消息（`model-text`，
`max_tokens=64`）再上报完成。`completed_count >= repeat_count` 即已完成；
上游业务码 `410121`（次数已发放）按已完成处理，不算失败。抽奖响应里的奖品
字段在**顶层**（`prize_name` / `prize_type`），不是嵌套的 `prize`。

任务写操作按账号串行，但用的是独立的 `_task_locks`，不能复用 token 锁：
完成任务途中 `_token_for` 会拿 token 锁，同一把锁不可重入会死锁。

DuMate 的每日签到奖励由服务端 00:00 自动发放，手动签到时 `hasIssued` 往往已是
`true`、余额差为 0。适配器因此从 `quota_overview.packages` 里认当天那笔
`source='login_bonus'` 的发放额作为 `earned_credit`，用 `checkin.auto_grant_at`
做每日幂等，保证「签到收支」只记一次。对话消耗走
`/points/records/usage`（按会话记账），在账号用量弹窗里展示今日 / 近 7 天消耗；
它不写 `usage_daily.credit`（上游对话 usage 只有 token，没有积分字段）。

前端按 `platform === 'dumate'` 渲染成长任务 / 抽奖 / 邀请三个分段；Loomy 的
「激活状态 / 兑换码」分段不出现。任务面板字段见 `web/lib/types.ts`
的 `PlatformTaskItem` / `PlatformDrawStatus` / `PlatformInviteStatus`。

#### 灵犀的任务体系

灵犀复用同一组契约（`supports_tasks` / `task_status` / `run_task`）：

| 动作 | 上游 | 说明 |
| --- | --- | --- |
| `checkin` | `GET /tasks` + `POST /tasks/daily_check_in/claim` | 与账号页签到同一实现 |
| `complete_task` | `POST /tasks/{task_key}/claim` | 只代领 `claimable`；`incomplete` 提示去客户端；每日签到才带 `?date=` |
| `bind_invite` | `POST /referral/bind` | body `{referral_code, source:"manual"}`，6 位字母数字，一次性 |
| `redeem` | `POST /tasks/gift_card/redeem` | 任务中心的「兑换权益」，与邀请码是两套东西 |

`task_status` 并发拉任务中心 + 邀请活动 / 我的邀请码 / 邀请进度四路。
邀请接口必须带 `X-Open-Position: invite_rewards`，响应是
`{v7code, result, data}` 信封，由 `client.referral_data` 拆包；单路失败放
`errors`，其余分段照常显示。任务状态词是 `incomplete` / `claimable` /
`claimed`，签到日历同时接受 `day`（桌面端实际形状）与 `date`。绑定失败
（`already_bound` / `invalid_code` / `self_invite` / `not_new_user` /
`activity_ended` 等）映射成 4xx，不按 502。前端复用 DuMate 的
「签到 / 任务 / 邀请」分段，但没有抽奖，另加兑换码与连签天数。

---

## 5. 路由与模型命名

- `traecn/<model>` / `traecom/<model>` → Trae（国内版 / 国际版）。前缀在转发前
  剥掉，上游只看到裸模型名。
- `trae/<model>` 是旧式兼容前缀：不带区域提示，选号行为与改造前一致。
- `qodercn/<model>` / `qodercom/<model>`、`loomy/<model>` 分别走 Qoder 与
  Loomy；前缀同样在转发前剥掉。
- `dumate/<model>` 走 DuMate（百度搭子）；它是单区域平台，账号与模型统一
  `realm='cn'`。
- `minimaxcn/<model>` / `minimaxcom/<model>` 走 MiniMax Code（国内站 /
  国际站）；旧式 `minimax/<model>` 不带区域提示，按目录 / 账号推断。
- `discovery/<model>` 走 Discovery（Intern TokenPlan，单区域，账号是 API Key）。
- `kilo/<model>` 走 Kilo（单区域，模型名本身含 `kilo-auto/…` 这类斜杠，前缀
  仍然只剥一层：`kilo/kilo-auto/free` → 上游 `kilo-auto/free`）。
- `cline/<model>` 走 Cline（单区域，模型名含 `cline-free/…`、`openai/…` 这类
  厂商前缀，同样只剥一层）。
- `doubao/<model>` 走豆包（单区域，Cookie 账号池）。目录里是友好的短名
  （`deepseek-v4-flash` / `kimi-k3` / `doubao-2.1-pro` …），转发前翻成上游的
  `model_item_key`；未收录的名字原样透传，方便上游上架新模型时先试。
- `lingxi/<model>` 走 WPS 灵犀（单区域，Cookie 账号池）。目录里的 id 就是上游
  `model_id`（`deepseek-v4.1-flash` / `GLM-5.3` / `kimi-k3` / `plan_pro` …），
  只剥 `wps-lingxi/` / `lingxi/` 前缀；`default` 落到默认模型，未收录的名字
  原样透传。
- `raccoon/<model>` 走商汤小浣熊（单区域，JWT 账号池）。目录里的 id 就是上游
  `name`（`sn-glm-5-3` / `sn-kimi-k3` / `sn-deepseek-v4-1-flash` …）；`default`
  落到内置默认模型，未收录的名字在本地就拒绝（上游会对未知模型静默回落默认
  模型并返回 200）。
- 其他模型名 → 原样走 WorkBuddy（与今天完全一致）。
- `/v1/models` 会把 Trae 的模型**按区域**并入列表（`traecn/` / `traecom/`），
  `owned_by` 标成 `trae`、`realm` 标出所属区域，客户端据此区分来源。

**为什么必须带前缀**：Trae 的模型 id 大小写混排（`DeepSeek-V4-Flash-Official`），
且与腾讯的模型名可能重名。前缀是唯一稳定的路由信号，不依赖探测或猜测。

区域化前缀同时也是**选号约束**：`traecom/<model>` 只会在国际版账号里选，一个
国际版账号都没有时直接报错，不回退到国内版——否则「我只想走国际版」会静默变成
国内版调用。旧式 `trae/<model>` 仍按目录 / 密钥推断区域。

前缀只加在**模型名**上，不影响密钥白名单语义：白名单里写 `traecn/glm-5.2`
即可精确限制到国内版；写旧式 `trae/glm-5.2` 则视为跨区域通配（两个区域都放行），
保证存量密钥继续可用。

### 协议覆盖（chat / responses / messages）

平台与组合路由在**三条对话入口上同一口径**，都由模型名决定，不由路径决定：

| 入口 | 平台 / 组合 | 说明 |
|---|---|---|
| `POST /v1/chat/completions`（与既有 `/v2` 兼容路径） | ✅ | 网关 `_platform_chat` |
| `POST /v1/responses`（与 `/responses`） | ✅ | 本层翻译成 OpenAI Chat 后交给适配器 |
| `POST /v1/messages` | ✅ | 同上；Anthropic 协议翻译不变 |

实现要点：

- 鉴权时按 `platforms.route_model(mapped or model)` 判定 `platform_model=True`，
  与 chat 路径同一判据——平台模型不属于腾讯账号池的任何版本，否则限定版本的
  密钥会被 `combo/fast` 误拒。`/v1/messages/count_tokens` 也要带这个标志：
  客户端发消息前会先数 token，用的还是同一个模型名。
- 适配器返回的已经是 OpenAI 形状，翻译层**复用**既有实现（`to_responses_object`
  / `to_anthropic_response` / 各自的 `_StreamTranslator`）。流式有三种来源形态
  （JSON、dict chunk、原始 SSE 字节），由 `services/platformcall.py` 归一化成
  字节流后走**同一份**解析循环，避免「直连好了、组合还坏着」。
- 记账只在协议层做一次，`mapped_model` 用实际去向（组合回填成员真名），
  日志的 `platform` / `realm` 两列与 chat 路径一致。
- 错误按各自协议的形状返回：Responses 用 OpenAI 错误体，Anthropic 用
  `{"type":"error", ...}`；上游在开流前报错时保留真实状态码，不以 200 开流。

### 模型列表的来源与缓存

国内版的 `model_list` 会随账号权益变化，而且**比客户端目录小很多**：实测一个
新账号只回 7 个模型，客户端的 `model_list_map` 里则有一长串；反过来，`model_list`
里的老模型（`seed_m8` 等）客户端目录里又没有。两边的缺口正好互补，所以一次
刷新按下面的顺序合并（同一 id 大小写不敏感、先出现者胜出，见 `catalog.py`）：

1. 面板导入的目录（显式覆盖）
2. 本机客户端快照的 `chat_v3`
3. 线上 `model_list`
4. 仓库内置的 `model_catalog.json`

刷新结果进缓存（6 小时），之后以缓存为准：

- 管理端「刷新模型」→ 重算一次上面的合并并写缓存；
- `/v1/models` 默认只读缓存，**不发网络请求**（客户端启动时不能被上游超时拖住）；
- 只有「完全没有缓存」时才隐式尝试一次（10 秒超时，失败后冷却 10 分钟），
  这样首次使用不会把一堆调不通的模型推给客户端；
- 缓存与失败冷却都存在 `settings` 表里，重启不丢。

服务器部署没有客户端快照时，第 1 层（面板导入）和第 4 层（内置默认值）仍然
保证目录不空——这正是「与倍率同构」的意思，细节见第 7 节。

#### Discovery / Kilo / Cline 的目录

这三个平台的目录都是**在线拉取**（没有桌面端快照那一层），降级链只有两级：

| 平台 | 在线来源 | 离线兜底 |
| --- | --- | --- |
| Discovery | `GET /v1/models`（要任意一把可用 Key） | 内置 8 条（含 `auto`） |
| Kilo | `GET ai-gateway.kilo.ai/api/v1/models`（免鉴权） | `kilo-auto/free` / `balanced` / `efficient` |
| Cline | `/models` + `/ai/cline/recommended-models` 的 `free` 分组 | 4 条 `cline-free/*` |

三者的缓存 / 失败冷却口径与 Trae 一致（`catalog_ttl` / `catalog_retry_ttl` /
`catalog_timeout` 都在平台设置里）。差别只在兜底内容：**兜底只放免费档与自动
路由**——付费模型的可用性取决于账号余额，放进兜底等于把一堆点了必然失败的
模型推给用户。

Cline 的合并规则是「免费档优先」：`recommended` 的 `free` 分组是官方标记的
权威来源，`/models` 补其余条目，同 id 以免费档为准，合并后免费排前面（账号
没余额时用户最该先看到能用的那几条）。Kilo 的目录是 OpenRouter schema，
`pricing` 是按 token 的字符串价，只用来判断「是不是免费」，不做金额换算。
Discovery 的目录是 OpenAI schema，`/v1/models` 同时承担**额度探针**的角色
（见 §6.3），所以拉目录本身就要用一把真 Key。

#### 豆包的目录

豆包**没有**公开的模型列表接口：可调用模型由客户端版本与账号权益决定，名字是
内部的 `model_item_key`（`person-deepseek-v4-1-flash-261017` 这类）。所以目录
只能是**内置快照**（`server/platforms/doubao/constants.py`），没有「在线拉取 →
缓存 → 降级」三级，平台设置里也刻意**不给** `catalog_ttl` / `catalog_retry_ttl`
/ `catalog_timeout`——露出来就是「保存成功但不生效」的假开关。

内置 8 条：`auto`、`doubao-2.1-lite` / `pro`、`deepseek-v4-flash` / `pro`、
`kimi-k3`、`kimi-k3.1-pro`、`kimi-k3.1-pro-fast`。上游实测报错的两个 GLM 模型
不列进目录；需要直连时仍可显式发 `doubao/glm-5.3`，`upstream_model()` 对未收录
的名字原样透传（与 Discovery 的 `force-model` 相反：**不改写**客户端要的模型，
否则报错指不回适配器）。

#### 灵犀的目录

灵犀**有**在线目录：`GET /api/aioffice/v1/sessions/plans` 返回 `data.plans[]`
（Pro / Max 模式）与 `data.models[]`（具体模型），顺序必须保持「先模式、后模型」。
它没有桌面端快照那一层，降级链是「在线目录 → 内置快照」两级，缓存 / 失败冷却
口径与 Trae 一致（`catalog_ttl` / `catalog_retry_ttl` / `catalog_timeout`）。

字段归一见 `server/platforms/lingxi/catalog.py`：`reasoning_efforts` 存在才算
思考模型（`[]` = 确实没有，不猜），`max_new_tokens` 是输出上限，倍率的
`~` / `～` / `〜` 统一成 ASCII，徽标字段叫 `tag`，介绍字段叫 `intro`（`desc`
常为空）。`default` 映射到 `deepseek-v4.1-flash`，并剥掉 `wps-lingxi/` /
`lingxi/` 前缀。

内置快照是 13 条真实响应（2026-11-10），包含两种模式和 11 个模型；上游新上架
的模型只要在目录里出现就会被带出来，未收录的名字也可以显式发
`lingxi/<model>`，`upstream_model()` 原样透传。

**推理档位（`efforts`）**：平台模型条目可以带 `efforts`（如
`['low','high','max']`），测试台的模型下拉据此显示档位选择器；空数组 = 不显示。
目前三个平台提供：

- **Kilo**：上游 `opencode.reasoning_options` 归一而来。`[]`（推理常开）→ 单档
  `high`；只有 `toggle` → `none` / `high`；有 `effort` 档位 → 去重后按上游
  schema 白名单（`none/minimal/low/medium/high/xhigh/max`）过滤，带 `toggle`
  且没有 `none` 时补一档。认不出的条目（如 `budget_tokens`）逐条丢弃，不让整份
  清单失效。legacy `variants` 不当档位用——它是「变体名 → 参数覆盖」。
- **DuMate**：只对同一后端的五个白名单模型（`deepseek-v41-flash` /
  `model-text` / `dm-auto-model` 的 `text.L0`、`mm.L0`、`mm.L2`）暴露
  `['none','high','max']`。`low` 在百度渠道与 `high` 同强度，内部归一到 `high`，
  对外不再单列；`none` 会显式下发 `thinking:{type:'disabled'}` +
  `enable_thinking:false`（只删 `reasoning_effort` 不够，上游仍会按默认档思考）。
- **灵犀**：目录里声明了 `reasoning_efforts` 的模型原样透传（当前实测均为
  `['low','high','max']`）；没有声明的模型是空数组，界面不显示档位选择器。

**Kilo 网关地址可覆盖**：默认 `https://ai-gateway.kilo.ai/api/v1`。设
`KILO_AI_GATEWAY_URL` 可指向自建 / 代理网关（末尾斜杠自动剥掉）；只配了 legacy
的 `KILO_API_URL` 时走 `${KILO_API_URL}/api/gateway/v1`（该 API 会改写到 AI 网关
的 `/api/v1`）。两者都不设才是官方地址，已有部署不受影响。

**Cline 的模型排序**：`list_models()` 会把真实请求观测到的模型级成败参与排序
（可用 → 未知 → 失败，稳定排序）。失败信号保鲜 30 分钟、成功 2 小时，只影响
排序不影响可用性判定，纯内存不落盘。只记模型级失败（404/429/5xx）；401/402/403
是账号或余额问题，记进来会把整池拖下水。上游把限流伪装成 200 正常帧的情况没有
状态码可看，走「直接记一次失败」的入口。参考仓的主动 15 分钟补测不取：本仓有
402 冷却、无池级 `probeTier` 排序体系，定时补测还会额外消耗免费额度。

### 平台归属与用量统计

平台归属落库，不靠前端按模型名前缀猜：

| 表 | 列 | 口径 |
|---|---|---|
| `request_logs` | `platform` | `''` / NULL = WorkBuddy；`trae` = 平台调用 |
| `usage_daily` | `platform`（在主键里） | 同上，同名模型跨平台分行累计 |
| `usage_hourly` | `platform`（在主键里） | 同上，供「今日」趋势图 |

- 网关在 `_record` 里用 `platforms.route_model(mapped or model)` 判定归属，
  与分流用的是同一个函数；平台调用的 `account` 来自适配器的 `account_hint`。
- 统计 / 日志接口接受 `platform` 参数：缺省 / `all` 不过滤；`workbuddy` 表示
  `platform` 为 NULL 或空串；其他值按平台名精确匹配。
- 升级时 `_backfill_platform_from_model` 按 `trae/` 前缀回填一次历史日志与
  汇总，幂等。
- 平台模式下积分**收入**只覆盖签到：由本端路由写入 `checkin_logs.credits`（带
  `platform` / `realm`），`/api/stats/credits` 同时返回 `income_partial=true`——
  平台没有 WorkBuddy 那样的活跃 / 旅行流水。**支出**（`usage.credit`）与 token
  用量照常按平台统计。
- 每次平台余额查询成功都会登记一份快照（存在 `credits_snapshot_platforms`，
  与 WorkBuddy 的 `credits_snapshot` 分开），余额之外**同时记下到期明细**
  （`expires` / `next_expire_at`）；可用 `GET /api/platforms/{name}/credits-snapshot`
  只读对账。查询失败时账号列表会带回 `last_credits` / `last_registered_at`，以及
  同一份快照里的 `last_expires` / `last_next_expire_at`，界面以 `~` 标成上次登记值，
  有效期照常显示，而不是把旧数字伪装成实时余额或让到期时间整块消失。
  到期明细的契约固定为 `{'amount', 'expire_at', 'label'}`：`amount` 是该包
  **当前剩余**（不是包上限），`expire_at` 是 **epoch 秒**。上游给毫秒的平台
  （MiniMax / DuMate）在适配器里折回秒，字段名也在适配器里归一——这两处不一致
  不会报错，只会让界面的有效期整列静默变空，新增平台时注意对齐 `trae`。
- 快照里**只在余额确实是美元时**才写 `unit: 'usd'`（Kilo、Trae 国际版），积分
  平台不写这一项——老条目与外部对账脚本读出来的形状与改造前一致。查询失败
  回退旧值时路由会把该字段带回 `unit`，界面才不会把 `$12.34` 画成「12 积分」。
  设置页「已登记余额」的合计另给 `credits_unit`：整批都是美元才标 `usd`，
  混合单位退回 `credits`（那种合计本身没有意义，更不能冒充美元）。
- **版本的统计口径按平台分两类**。WorkBuddy 的版本是账号池划分，看客户端传的
  `global:` 前缀；Trae 这类分版本的平台，模型名是 `trae/<model>`、本身不含
  版本，版本由**实际选中的账号**决定。适配器在 `PlatformChat.realm` 里回报
  账号版本（`intl` → `global`），网关记账时优先用它，失败路径才退回按模型名
  判——否则国际版的调用会全部记成国内版。
- 仪表盘 / 统计 / 日志在 `isWorkbuddy || platformInfo.supports_realm` 时才传
  `realm`；没有版本概念的平台（组合等）不传，否则用户上次选的「国际版」会把
  该平台的行全部过滤掉，表现为「切到 Trae 后图表全空」——比看到错误的数字更
  难查。账号与模型列表是另一回事：它们直接读本端账号/目录里的 `realm`，所以
  会按版本过滤（见 §4「前端」）。
- 升级到 1.0.8 时，启动会按 `request_logs.account` 与 Trae 账号的版本回填一次
  历史日志，并以它为准重建 `trae` 的用量汇总（只影响 Trae，其他平台原样保留）。
- 仪表盘同样按平台切换：顶部五张卡片、账号快照与右侧面板在平台模式下换成该
  平台自己的账号 / 积分 / 令牌状态；「反代上游」与账号池健康度是 WorkBuddy
  独有的能力，平台模式下换成一句说明 + 该平台的账号签发状态，而不是留一个
  空面板让人以为上游挂了。

---

## 6. 账号存储

```
data/platforms/trae/accounts/<uid>.json
```

与 WorkBuddy 的 `auths/` 严格分开：两边文件格式、刷新语义、失效判断都不同，
混放会让「账号页显示一个、上游池里是另一个」这类不一致重新出现。

每个平台一个目录，文件名是 `<uid>.json`，uid 由凭据派生（同一份凭据重复导入
会覆盖同一条账号）：

| 平台 | 目录 | uid 前缀 | 凭据字段 |
| --- | --- | --- | --- |
| Trae | `platforms/trae/` | 上游 uid | `token` / `refreshToken` |
| Loomy | `platforms/loomy/` | `s_` | `session` |
| DuMate | `platforms/dumate/` | `dm_` | `cookie`（BDUSS 为核心） |
| MiniMax Code | `platforms/minimax/` | `mm_` | `token` / `refreshToken` |
| Discovery | `platforms/discovery/` | `dc_` | `apiKey` |
| Kilo | `platforms/kilo/` | `kl_` | `token`（一年期，无 refresh） |
| Cline | `platforms/cline/` | `cl_` | `refreshToken`（+ 缓存的 `token`） |
| 豆包 | `platforms/doubao/` | `db_` | `cookie`（`sessionid` 为稳定标识） |
| 灵犀 | `platforms/lingxi/` | `lx_` | `cookie`（`wps_sid`） |
| 商汤小浣熊 | `platforms/raccoon/` | `rc_` | `token` / `refreshToken`（两个 JWT） |

三个新平台（Discovery / Kilo / Cline）与豆包都**没有** `desktopPath`：它们不读也
不回写本机客户端，`import_from_desktop` 不存在，管理端的「从本机导入」入口不会
出现。

单账号文件字段：

```json
{
  "uid": "1234567890123456789",
  "nickname": "",
  "email": "",
  "realm": "cn",
  "token": "<access token>",
  "refreshToken": "<rotating refresh token>",
  "expiredAt": "2026-10-30T15:04:05.123Z",
  "refreshExpiredAt": "2026-12-01T00:00:00.000Z",
  "proxy": "",
  "createdAt": 0,
  "updatedAt": 0
}
```

### 轮换安全（最容易出事的一处）

Trae 的 `ExchangeToken` 每刷新一次就作废旧的 `refreshToken`。因此：

1. 刷新结果必须**先落盘再使用**；落盘用临时文件 + `os.replace` 原子替换。
2. 同一账号的刷新必须**串行**（进程内 `asyncio.Lock`），否则并发请求会拿同一个
   旧 refreshToken 去换两次，第二次必然失败并被上游判定为异常。
3. 刷新失败后指数退避，避免「1 秒一次 401」把账号打黑。

多进程部署时进程内锁不够，此时需要外部互斥（本轮不做，先按单 worker 运行；
`uvicorn` 默认就是单 worker）。

### 6.1 本机客户端一键导入与**双向同步**

Trae 没有导出账号的入口，登录态只存在 `%APPDATA%\Trae CN\User\globalStorage\
storage.json` 的 `iCubeAuthInfo://icube.cloudide` 里，值是加密的
（见 `icube.py`：`tc` 头 + AES-128-CBC，密钥由 blob 自带的 key material 派生）。
`POST /api/platforms/trae/accounts/import-desktop` 直接读它，凭据只在内存里过一遍
就落到 `data/platforms/trae/accounts/`，接口只返回脱敏结构。

路径按平台取（Windows `%APPDATA%`、macOS `~/Library/Application Support`、
Linux `$XDG_CONFIG_HOME`）；`TRAE_DESKTOP_DIRS` 可指定自定义安装目录，
`TRAE_USER_HOME` 可覆盖下面临时 token 所在的用户主目录。

桌面客户端没登录、但 IDE / CLI 留了 7 天临时 token 时，还会兜底读
`~/.trae-cn/trae-jwt-token`（国际版 `~/.trae/trae-jwt-token`）。这种 token 只有
access token、不能刷新，也没有 userId，导入时按 token 摘要生成稳定的合成 uid；
同一区域已有 `storage.json` 登录态时不使用它——用不能刷新的一次性令牌顶替
可刷新账号只会让账号更快失效。

**同步是双向的**，否则会把用户从 IDE 里踢出去：

| 方向 | 时机 | 为什么 |
| --- | --- | --- |
| 客户端 → 网关 | 刷新前先读客户端 | 用户开着 IDE 时它可能已经轮换过，我们手里的 refreshToken 已被作废 |
| 网关 → 客户端 | 刷新成功后回写 | 轮换是单向的：不写回，用户下次打开 IDE 就登出 |

回写只改 `iCubeAuthInfo://icube.cloudide` 这一个键，先备份
`storage.json.bak-trae-gateway`，用临时文件 + `os.replace` 原子替换。回写失败
只记日志，绝不让「同步失败」连累刷新结果。手工粘贴 token 的账号（没有
`desktopPath`）不会去碰客户端的文件。

### 6.2 令牌自动续期（哪些平台、什么阈值）

进程内巡检（`server/services/renew.py`，每小时一跳）对平台账号做两件事：按剩余
寿命续期，以及「每 N 天全量刷一次」的计划任务。两份排程共用同一张清单
`_PLATFORM_RENEWERS`，新增平台只加一行，巡检循环 / 计划任务 / 「立即执行一次」
三处自动覆盖。

| 平台 | 判据 | 阈值 | 为什么不更短 / 更长 |
| --- | --- | --- | --- |
| Trae | accessToken `expiredAt` | 3 天 | 令牌寿命约 60 天，3 天足够覆盖闲置号 |
| Qoder | `expiredAt`（网关 `expiresAt`） | 6 小时 | jobToken 约 24 小时，按天判会让刚签发的令牌立刻进窗口 |
| MiniMax Code | **refreshToken** `refreshExpiredAt` | 3 天 | access token 只有约 1 小时，由适配器按需刷新；拿它判会每小时轮换一遍一次性 refresh token |
| Cline | **refreshToken** `refreshExpiredAt` | 3 天 | 与 MiniMax 同理：access token 约 10 分钟且由 `_token_for` 按需轮换；refresh token 一次性，按 access 判会反复轮换 |
| 商汤小浣熊 | **refreshToken** `refreshExpiredAt` | 3 天 | access token 约 3 小时且由 `_token_for` 按需轮换；refresh token 一次性、约 30 天，按 access 判会每小时轮换一遍 |

三条共同口径：

- **续期一律走适配器的 `refresh_account`**。轮换式令牌（Trae / MiniMax / Cline）
  必须先原子落盘再使用，Qoder 的 token 落盘与 COSY 会话重建都在内置网关里；绕开
  适配器会丢令牌或把用户登出。
- **解不出到期时间就不刷**，交给「刷新」按钮与计划任务，不按小时去猜。
- **停用的号跳过**；「仅签到」的号照常续期（它只是不参与对话，凭证要活着）。

以下五个平台**有意不在巡检里**：

- **Loomy**：没有 refresh token，只能密码重登。按小时对一批账号重复登录既没有
  意义，也容易触发风控；闲置号在下次请求 401 时自动重登即可（见
  [`loomy.md`](loomy.md)）。
- **DuMate**：凭证是长期 Cookie，换 token 只是探活；账号文件里也没有 token
  到期时间可判，强行定时探活只会多打上游。失效时由 401 触发重新导入。
- **Kilo**：token 是一年期、**没有 refresh token**，续不了；过期只能重新登录。
  按小时去读资料 / 余额既没有意义，也只是多打上游。账号页的「过期时间」直接
  显示 token 的 `expiredAt`（按一年期推算），到期前重新登录即可。
- **Discovery**：凭证是 API Key（约 6 个月），没有刷新语义。巡检里的「续期」
  只能是探活，而探活就是一次 `/v1/models`——它同时还是**额度探针**，按小时
  对整池 Key 打一轮会让已经耗尽的账号反复被计入限流。失效与耗尽都由对话的
  401 / 402 触发，不需要定时任务。
- **豆包**：凭证是网页 Cookie，没有 refresh token，也拿不到可靠的过期时间
  （失效只能从上游 401 得知）。巡检只能探活，而探活要打额度接口；失效同样由
  401 触发重新抓取 Cookie，不需要定时任务。

「令牌刷新计划」默认开启（每 5 天凌晨 3 点全量刷一遍），对 MiniMax 与 Cline
尤其重要：设备码登录拿不到 refresh token 的寿命，`refreshExpiredAt` 为空的账号
只能靠这条排程定期轮换。

MiniMax 的 refresh token 是**一次性**的：换发后旧的立即作废，所以轮换成功后
旧记录里那个到期时间也一并失效，适配器会把它清空（上游回 `refresh_expires_in`
时按它记新的）。不清空的话，巡检每小时都会把「旧的到期时间快到了」当成理由再
轮换一轮，白打上游。

账号页对 MiniMax 的「过期时间」一列显示的是 **refresh token** 的到期时间
（`token_auto_refresh` 标记告诉前端换字段）：它的 access token 只有约 1 小时，
按它判「已过期」会把闲置一小时的正常账号整片标红。首页的平台卡片与「令牌最早
过期」排序用同一份判定。

Cline 是同一个问题、同一个做法：access token 约 10 分钟且由适配器按需轮换，
`public_account()` 也把 `token_auto_refresh` 置为 `True`。区别在于 Cline 的
`refreshExpiredAt` 通常是空的（上游不告诉 refresh token 的寿命），所以这条排程
对它是**唯一**的定期轮换入口——`refresh_cline_all(force=False)` 在
`refreshExpiredAt` 缺失时一律跳过，只有「令牌刷新计划」以 `force=True` 调用时才
真正换发。这与 MiniMax 的「解不出到期时间就不刷」是同一条口径的两种落法：
一个是「按 refresh 寿命判」，一个是「没有寿命可判就只靠计划任务」。

### 6.3 三个新平台的能力边界：签到 / 余额 / 续期

| 平台 | 签到 | 余额接口 | 续期 | 说明 |
| --- | --- | --- | --- | --- |
| Discovery | 无 | 无 | 无 | 额度只能用 `/v1/models` 探针：200 有额度、429 `quota_exceeded` 耗尽 |
| Kilo | 无 | 有（**美元**） | 无 | token 一年期、不可刷新；余额走 `/api/profile/balance` |
| Cline | 无 | 无 | 有（refresh token） | 余额不足由 402 `insufficient_credits` 表达 |
| 豆包 | 有 | 有（额度汇总） | 无 | Cookie 无 refresh；额度来自 `window_limits` 汇总，401 换号、429/5xx 按设置冷却 |
| 灵犀 | 有（每日签到） | 有（智点 + 赠送批次） | 无 | Cookie 无 refresh；余额来自 `credits/balance`，15 天内到期批次单列；401 换号、429/5xx 按设置冷却 |
| 商汤小浣熊 | 有（触发每日积分） | 有（积分 + 明细） | 有（一次性 refresh） | 余额来自 `points/v1/balance`；签到靠桌面端 `setting_info` 触发后核对账单；401 换号、200108 按 25 分钟冷却 |

Discovery 的「额度」在界面上是**三态**，不是数字：`ok` / `rate_limited` /
`quota_exhausted`。把 `429 quota_exceeded` 当成普通限流（几十秒冷却）会让已经
耗尽的账号被反复重打，所以适配器把它映射成 **402** 并冻 1 小时；`rate_limit_exceeded`
仍按 429 + `retry-after` 处理。这条区分是整个 Discovery 适配里最容易被「顺手
简化」掉的地方。

Cline 的免费档有两处硬约束，都写在 `client.py` 里：`deepseek/` / `cline-free/` /
`cline-pass/` 前缀**只认流式**（非流式会被回 500 `empty response content`），
请求体也**不能带 `max_tokens`**。适配器因此对这几类前缀强制走流式、再聚合成
`chat.completion`；上游把限流伪装成 200 正常帧时（`Daily free limit` /
`INFERENCE_CAP_ERROR` / 只有 reasoning 没有正文），按 429 换号而不是把空回复
交给用户。

余额的**单位**会一路带到界面：`credits()` 给出的 `unit` 字段（`usd` / `credits`）
也随快照一起存下来，所以查询失败回退「上次登记」时不会把 `$12.34` 画成
「12 积分」。账号页的列头、首页的平台卡片与设置页的「已登记余额」合计都按同一
份单位走；同一平台的账号单位一致，取到一个 `usd` 就整列按美元，混合单位退回
积分口径（那种合计本身没有意义，更不能冒充美元）。

---

## 7. Trae 协议要点（实现依据）

来源：[`shuishuipingan/trae2api-hub`](https://github.com/shuishuipingan/trae2api-hub)（MIT）
的 `trae_client.py`，本仓库只借鉴协议事实，不复制其看板/任务编排代码。

两边**没有共享提交历史**（这里是按协议重写的原生实现，不是 fork），所以不合并：
它以只读远端 `trae-ref` 的形式接进来，用 `bash deploy/sync-upstream.sh --refs`
对照协议事实文件有没有变，人工决定是否跟进。

每次同步到哪个提交、同步了什么、哪些有意不跟，记录在 `deploy/trae-ref.txt`。

企业版 / SaaS 租户走独立协议，个人版的 `ExchangeToken` / 设备证明刷新不了它们。
`server/platforms/trae/enterprise.py` 在导入、网页登录收尾、刷新响应落盘与本机
`storage.json` 扫描四个入口，按显式租户元数据（`realm=enterprise|saas`、
`scope` / `loginScope=saas`、非空 `saasBootConfig` 或 `TenantInfoBase`）拒绝，
不根据裸 token 猜、也不扫描用户正文（来源 `c724a65`，v1.6.1）。

| 用途 | 国内版 | 国际版 |
| --- | --- | --- |
| 核心数据面 | `https://trae-api-cn.mchost.guru` | `https://core-normal.traeapi.us` |
| 账号 / 刷新 | `https://api.trae.com.cn` | `https://grow-normal.trae.ai` |
| 用户资料 | `https://api.trae.cn` | `https://grow-normal.trae.ai` |
| 付费 / 权益 | `https://api.trae.com.cn` | `https://grow-normal.traeapi.us` |
| 用量（国际版） | 无（积分走付费接口） | `https://ug-normal.trae.ai`（US 备选 `api-us-east`，其余 `api-sg-central`） |
| 签到 | `https://api.trae.cn` | 无（404） |
| UA | `Trae CN/3.4.3` | `Trae/3.6.2` |

- 对话：`POST /api/ide/v1/llm_raw_chat`，**服务端始终 SSE**。
- 鉴权：`x-ide-token` + `Authorization: Cloud-IDE-JWT <token>`（国内版）；
  国际版对话改用 `Authorization: Bearer <token>` 并额外带客户端版本头。
- 刷新：国内版 `POST /cloudide/api/v3/trae/oauth/ExchangeToken`；国际版
  `POST /trae/api/v3/oauth/ExchangeToken`（带 `DeviceInfo` / `DeviceProof`）。
- 签到：`/trae/api/v2/ug/checkin_credits/{status,claim}`，国内版真实可用。
- 国际版用量：`POST /trae/api/v1/pay/user_current_entitlement_list`，请求体
  `{"require_usage": true}`；读 `entitlement_base_info.quota` 的
  `basic/bonus_usage_limit`（美元额度）与 `usage` 的
  `basic/bonus_usage_amount`（已用）。官网「Usage」页就是这一口径，
  与国内版积分池不是同一个接口。
- 免费额度用尽：上游回业务码 `4008`（`quota exceeded`），网关改判成
  `429 Too Many Requests` + `Retry-After: 60`，错误码 `quota_exceeded`。
  它**不是**风控：不冷却账号、不换号；4015 / 4019 才是风控（403）。
  风控命中后只冷却命中的那个账号并**终止本次请求**，不再轮换整池——风控是设备 /
  出口维度，换号只会拿到同一个答复，还会把每个号逐个推进 1800 秒冷却
  （来源 `683a3385`）。
- 模型通道：`llm_utils_chat` 的 `solo_work_remote` 与 `solo_agent_remote` 两组
  模型集合不通用，`Doubao-Seed-Code` / `qwen3.8-flash` 只在 agent 组。网关按模型
  选 `function`（`server/platforms/trae/catalog.py` 的 `solo_function_for`），
  选错时上游回的是 `4001 the param is invalid` 而不是「模型不存在」
  （来源 `32961736` / `93d24a14`）。
- 模型列表：`POST /api/ide/v1/model_list`（仅国内版；国际版 403，用本地目录）。

### 流结束判据（不要静默截断）

流式回复的收尾只认两种信号：`event: done`（或裸 `data: [DONE]`）。两者都没
收到就说明连接被掐断（代理重置 / 上游提前关闭），**不能**再按 `finish_reason:
stop` 下发——客户端会把它当成一条完整回复，用户看到的就是「回复到一半没了」。

- 流式：补一帧 `{"error": {...}}`，`finish_reason` 用 `length`（断流）或
  `error`（上游明确报错）；
- 非流式：保留已经拿到的正文，`finish_reason` 标成 `length` / `error`；
- 空闲超时：连续 60 秒没有任何新行就按上游卡死处理，补 `idle_timeout` 错误帧。
  以前只有 httpx 的 300 秒读超时，客户端会空等到挂死。

开关 `TF_TRAE_STRICT_STREAM_END=0` 可退回旧行为（用于确认某个通道本来就不发
结束帧的场合）。

### 工具调用（Agent 通道，实测 2026-11-01）

`llm_utils_chat` 收的是**客户端私有形状**，和 OpenAI 有两处硬差异，发错时上游
直接 400（`cannot unmarshal object into Go struct field ... of type string`）：

| 位置 | 上游要的 | 我们发出的 |
| --- | --- | --- |
| `tools[].function.parameters` | JSON **字符串** | `{"type":"object",...}` 序列化后的文本 |
| `tool_choice` | **字符串** | `auto` / `none` / `required` / 函数名 |

回到客户端的形状是 `choices[].delta.tool_calls` 的 OpenAI 形状，由 `chat.py`
把上游的 `function_call` + 按 `index` 分片的 `arguments` 重新聚合。上游即使
带工具调用也报 `finish_reason: "stop"`，适配器改写成 `tool_calls`。

**历史里的工具痕迹不能结构化传**：上游只认 `role` + `content` parts，`assistant.
tool_calls` 与 `role: "tool"` 会被静默丢掉。于是折成正文的 `[历史调用] …` /
`[历史结果] …`，模型仍能读懂上下文。前缀刻意不用 `[tool_call]`——那是正文
还原器认的调用语法，会让模型复述历史时被当成新请求执行。整体开关：
`TF_TRAE_NATIVE_TOOLS=0`。

折叠有个副作用：上下文里这类文本多了，flash 档模型会照着**文本格式**念出下一次
调用（`output.response` 里出现 `tool_call edit({...})`），客户端只收到一条普通回复
就停住。`chat.py` 因此带一层还原（`text_tools.py`），把这种正文里的调用解析回
`delta.tool_calls` / `message.tool_calls`；只对声明了 tools 的请求生效，参数不是
合法 JSON 就原样透传。

除了 `tool_call` 家族，还原器也认 Trae 原生文本标记（参考实现叫
`ToolMarkerStream`）：

```
<|FunctionCallBegin|>[{"name":"bash","parameters":{"command":"ls"}}]<|FunctionCallEnd|>
```

数组里可以有多条调用；流式下标记被帧切开会先扣住，收尾仍不完整则按退化调用
报错，不会把协议标记漏成正文。这里只做**防御性还原**，协议提示里不教模型写
这个格式——原生 tools 通道正常时教它只会诱导模型退化。

### 采样参数与思考强度

- `llm_raw_chat` 的采样参数（`temperature` / `top_p` / `max_tokens` / `stop` /
  penalties）必须放在 `parameters` 对象里；平铺在请求体顶层**上游不读**。
- `reasoning_effort` / `reasoning.effort` / `thinking.effort|level` /
  `thinking: false` 会归一成 Trae 的 `reasoning_effort_level`：
  `minimal|low|light → light`、`medium|high → high`、
  `xhigh|max|extra_high → extra_high`、关闭类取值 → `none`。
  raw chat 放在 `parameters` 下，solo 通道是平铺字段。开关
  `TF_TRAE_REASONING_EFFORT=0` 可整体关掉。
- solo 通道客户端不传 `max_tokens` 时默认发
  `TF_TRAE_SOLO_MAX_TOKENS`（默认 `1000000`）。思考 token 与正文共享预算，
  空着走默认值容易出现「思考完就没额度」。

### 积分消耗倍率（「积分消耗速度」）

Trae 客户端在选择模型时显示的 `{rate} ×` 来自模型条目的
`features.consumption_rate = {enable, data:{rate}}`。`model_list` **不带**这个字段，
所以按优先级取三层来源（合并规则在 `server/platforms/trae/rates.py`）：

| 优先级 | 来源 | 说明 |
| --- | --- | --- |
| 1 | 面板导入的 JSON | `<DATA_DIR>/platforms/trae/model_rates.json`，显式覆盖 |
| 2 | 本机客户端快照 | `state.vscdb` 的 `<uid>_AI.agent.model.model_list_map` |
| 3 | 仓库内置默认值 | `server/platforms/trae/model_rates.json`，随代码发布 |

- 服务器部署没有客户端快照，靠第 3 层也能显示倍率，不必与客户端同机；
- 同一模型在不同分组（`chat_v3` / `solo_agent` / `solo_coder`）里的 rate 一致，
  读取时**跨分组汇总**，避免只出现在某个分组里的模型缺倍率；
- 取不到 → `credit_ratio: null`（界面显示 `—`），**不写 0**（0 会被读成「免费」）；
- 对外字段是 `credit_ratio`（float），出现在
  `/api/platforms/{name}/models` 与 `/api/platforms/model-catalog` 的模型条目里。

**为什么不用服务端接口**：`POST /api/ide/v1/batch_get_detail_param`
（`{"functions": ["chat_v3"], "mode_type": 1}`）确实会回每个模型的
`display_contact_config.consumption_rate.data.rate`，但它和客户端拉清单的通道
**不是同一条**：客户端走内部 RPC `model/model_list_by_function`
（`{functions: [...], force_refresh}`，由本地 `ai_agent` 服务处理，结果落
`model_list_map`），条目里带 `features` / `icon` / `fee_model_level`；
`batch_get_detail_param` 回的是 `function_configs[].config_info_list[]`
「配置明细」，字段是 `config_name` / `display_config` 那一套。

两边的目录因此在时间上错开，实测 2026-11-01：服务端只有 `glm-5.2` /
`kimi-k2-0905`，没有客户端已经上架的 `glm-5.3` / `kimi-k3` / `qwen3.8`；连
同一模型的倍率都不同（`DeepSeek-V4-Pro-Official` 服务端 0.72、客户端 0.36）。
换参数也救不回来：`mode_type` 0-3、`access_type` 0-2、各种 `agent_type`、以及
16 个 `functions`（`chat_v3` / `solo_agent` / `builder_v3` / `solo_work_lite` …）
返回的都是同一份旧目录。云端 `get_model_list` 系列（`/api/ide/v1|v2`、
`/api/agent/v3` × core / api.trae.com.cn / api.trae.cn）实测全部 404，所以
服务端拿不到新模型的倍率是确定事实，不是配置问题。

**维护内置默认值**（在装了客户端的机器上跑）：

```powershell
python dev/update_trae_rates.py            # 检测本机 Trae，有变化才写回 JSON
python dev/update_trae_rates.py --check    # 只检查：有变化时退出码 1（CI 可用）
python dev/update_trae_rates.py --dry-run  # 打印差异，不落盘
python dev/update_trae_rates.py --prune    # 连客户端已下架的模型一起删
```

脚本先探测安装目录与 `state.vscdb`，没装 Trae 时退出码 2；读到快照后与目标 JSON
逐项比较（模型 id 大小写不敏感），只有新增 / 变更（或 `--prune` 下的删除）才写回。

**面板入口**：模型页的「导入倍率 / 导出倍率 / 重置」三个按钮，只在
`supports_model_rates` 的平台显示（Trae 有）。导入走
`POST /api/platforms/{name}/model-rates/import`（`require_admin`），接受三种形状：

- `{"glm-5.3": 0.78, ...}`
- `{"rates": {...}}`（本项目导出的形状）
- `[{"id": "glm-5.3", "credit_ratio": 0.78}, ...]`

解析不到任何条目时返回 400 且**不清空**已有配置（多半是选错了文件）。
`DELETE /api/platforms/{name}/model-rates/import` 丢掉导入表，回到下面两层。

### 模型目录（有哪些模型 / 上下文多长 / 走哪条通道）

目录与倍率同构，也是三层来源（合并规则在 `server/platforms/trae/catalog.py`）：

| 优先级 | 来源 | 说明 |
| --- | --- | --- |
| 1 | 面板导入的 JSON | `<DATA_DIR>/platforms/trae/model_catalog.json`，显式覆盖 |
| 2 | 本机客户端快照 | 同一个 `model_list_map`，取 `chat_v3` 分组 |
| 3 | 仓库内置默认值 | `server/platforms/trae/model_catalog.json`，随代码发布 |

- 运行时缓存（`platforms.trae.models`，TTL 6 小时）记录上次合并结果；读取时
  导入的条目会再叠一次，所以导入新目录**立刻生效**，不必等刷新或缓存过期；
- 同一 id 大小写不敏感、先出现者胜出，`channel` 跟随保留（决定
  `llm_raw_chat` 还是 `llm_utils_chat`）；
- 线上 `model_list` 只回 5 个老模型，插在「客户端快照」与「内置默认值」之间；
- 服务器部署没有客户端时，靠第 3 层也能列出完整目录，不必与客户端同机。

**维护内置默认值**（在装了客户端的机器上跑，与倍率脚本同构）：

```powershell
python dev/update_trae_catalog.py            # 检测本机 Trae，有变化才写回 JSON
python dev/update_trae_catalog.py --check    # 只检查：有变化时退出码 1（CI 可用）
python dev/update_trae_catalog.py --dry-run  # 打印差异，不落盘
python dev/update_trae_catalog.py --prune    # 连客户端 chat_v3 里已消失的模型一起删
```

脚本逐字段比较 `name` / `realm` / `context_window` / `max_tokens` / `multimodal` /
`thinking` / `channel`，只有新增或变更才写回；没装 Trae 时退出码 2。
`--prune` 只删「本次读取的那个区」的条目——跑 `--realm cn` 不会误删国际版。

**面板入口**：「导入目录 / 导出目录 / 重置」，只在 `supports_model_catalog` 的
平台显示。导入走 `POST /api/platforms/{name}/model-catalog/import`
（`require_admin`），接受：

- `{"models": [...]}`（本项目导出的形状）
- 裸数组 `[...]`
- `{"glm-5.3": {"name": ..., "context_window": ...}}`（id → 字段）
- 参考项目 `trae2api-hub` 的 `trae_catalog_*.json`（`upstream_key` /
  `display_name` / `context_window_size` / `thinking.supported` / `chat`）

解析不到任何条目时返回 400 且**不清空**已有配置。
`DELETE /api/platforms/{name}/model-catalog/import` 丢掉导入表，回到下面两层。

### 每请求实扣积分（可选）

Trae 的**流里没有**「本次扣费」：末帧 `token_usage` 只有 token 数，
`prompt_completion_id` 恒为 0。但上游另有一个「用量明细」接口，
就是这个数在客户端 UI 里的来源：

```
POST https://api.trae.cn/trae/api/v1/pay/query_user_usage_group_by_session
{start_time, end_time, page_size, page_num, usage_type: [7], Request}
```

`usage_type` 必须带 **7**（积分计费）。只传 1..6（autocomplete / advanced /
fast / slow / dollar）时，v2 计费账号恒返回 `total=0`——看起来像「没有明细」，
其实只差这一个枚举值。返回按 `session_id` 分组，每条给
`amount_float`/`credits_float`（积分）、`cost_money_float`（美元）与四段 token。

明细**按请求里的 `session_id` 归属**：请求体不带会话号时上游照样扣费
（账户累计会涨），但不会生成明细行。所以 `TF_TRAE_CREDIT_METER` 有三档：

- 关（**默认**）：不记，热路径零额外开销。
- `1`（delta）：请求前后各查一次账户累计求增量。便宜，但**并发在途时不归属**
  （分不清是谁的），后到的请求干脆不记。
- `detail`：给请求生成 `session_id`，流结束后按它去明细里取这条的
  `amount_float`。**精确到 4 位小数且并发安全**；请求前不打上游、不抢锁，
  收尾只查一次（实测落账与请求同步：流结束后首查即中，0.24–0.48 秒基本是
  一次网络往返），重试 0/0.25/0.5 秒兜底。

`amount_float` 与账户 `credits_amount` 的增量完全相等，也确实就是客户端
「用量明细」里显示的那个数（UI 只保留两位小数，0.416 显示成 0.42）。
三档都可以用，但默认关闭：打开 `detail` 会给每个请求多加一次上游请求和
约 0.3–0.5 秒尾延迟。

倍率与实扣是两个口径——倍率只是「相对贵不贵」，不同模型的每 token 基准价并不
相同（实测 deepseek-v4.1-flash 与 glm-5.3-flash 同样 token 下相差数倍），
所以**不能**用 `tokens × rate` 反推积分。

### 设备指纹

Trae 客户端长期复用同一组设备头，跨账号绝不共享。这里全部由 uid 单向派生：

```
machineId = sha256("trae-machine:" + uid)
deviceId  = uid（纯数字时）否则 19 位派生数字
brand/os  = 本机真实值，取不到用固定兜底
```

同一个 uid 永远得到同一台「虚拟物理机」，改了会让账号的历史设备画像断裂。

---

## 8. SSE 翻译

Trae 事件 → OpenAI chunk：

| Trae `event` | `data` 字段 | 映射 |
| --- | --- | --- |
| `output` | `response` | `delta.content` |
| `output` | `reasoning_content` | `delta.reasoning_content` |
| `output` | `tool_calls` | `delta.tool_calls` |
| `metadata` | — | 记下 model / session，不发给客户端 |
| `token_usage` | — | 转成 `usage`，末帧带 `include_usage` |
| `done` | `finish_reason` | `finish_reason`，随后 `[DONE]` |
| `error` | `code/message` | 已经产出正文时**不能**改状态码，追加一个错误块并结束 |

非流式请求：把整条 SSE 聚合后拼一个 `chat.completion`，`usage` 原样带上。

**只有思考、没有正文**在正常回合里不会发生：实测 `deepseek-v4.1-flash` /
`glm-5.3-flash` 的文本回合都会在 `reasoning_content` 之后继续下发 `response`。
唯一合法的「无正文」是**工具调用轮**——此时正文为空、`tool_calls` 非空，OpenAI
客户端据此执行工具，属于正常语义。因此适配器不干预这类轮次。

异常的空产出不再伪装成 `stop`：默认按错误收尾（流式补 `error` 帧、非流式
502），`code` 为 `reasoning_only`（只有思考）或 `empty_response`（什么都没有），
让 Agent 客户端知道该重试而不是静默停住。开关：
`TF_TRAE_EMPTY_OUTPUT_ERROR=0` 退回旧行为。

`TF_TRAE_REASONING_AS_CONTENT` 默认**关闭**：打开后，整轮既没有正文也没有工具
调用时，先把思考原文补一帧 `delta.content`，于是不会触发上面的空产出错误。
上游明确报错时不补，避免把错误伪装成回答。

---

## 9. 安全边界

1. **平台账号凭据只在本地落盘**，管理端接口永远不返回 `token` / `refreshToken`
   原文，只返回掩码与过期时间。
2. 账号目录的文件名必须过白名单校验（纯字母数字下划线连字符），拒绝 `..`、
   路径分隔符与 NUL。
3. 网关侧所有既有防线（密钥校验、IP 规则、限流、请求体上限、日志清洗）对
   Trae 请求同样生效——分流发生在鉴权之后。
4. 平台调用复用 `config.http_client`，因此沿用「内网直连 / 外网走设置代理」的
   规则，不会把凭据发给错误的出口。

---

## 10. 测试策略

- `server/tests/test_platform_trae_store.py`：账号文件的原子写、路径校验、
  凭据掩码、轮换落盘。
- `server/tests/test_platform_trae_client.py`：请求头、SSE 解析、事件→chunk 翻译、
  usage 的**对象身份**（换对象会让网关记账丢 token）。
- `server/tests/test_platform_trae_adapter.py`：目录缓存策略、隐式拉取冷却、
  多账号选择。
- `server/tests/test_platform_trae_desktop.py`：blob 编解码（含 FIPS-197 官方向量）、
  导入（含 CLI 临时 token 兜底）、回写、备份。
- `server/tests/test_platform_loomy_adapter.py`：Loomy 的签名 / RSA 原语、账号文件、
  模型别名与输出上限、`/no_think` 翻译、本机会话导入、微信扫码登录状态机。
- `server/tests/test_platform_dumate_adapter.py`：DuMate 的 Cookie 归一与脱敏、
  目录降级、选号、token 缓存与 401 重试、`max_tokens` 钳制、SSE 扫描、签到与积分、
  成长任务（字段归一 / 自动完成 / 410121 / 一键完成的跳过口径）、抽奖（请求体
  双幂等键、每次新 UUID、空池）、邀请码（拒绝自己的码 / 大小写只 strip）、
  任务缓存的按路回落，以及百度扫码登录的事件 / 兑换解析与会话状态机。
- `server/tests/test_platform_minimax_adapter.py`：MiniMax Code 的账号脱敏、目录解析、
  双区选号、refresh token 轮换落盘（含换发后清掉旧 refresh 到期时间）、401 重试、
  签到 / 积分、OpenAI ↔ Anthropic 请求 / 响应 / 流式翻译。
- `server/tests/test_minimax_renew.py`：MiniMax 的续期判据（看 refresh token 的
  寿命而不是 1 小时的 access token）、force 全刷、缺 refreshToken 记失败、
  仅签到号照常续期、解不出到期时间不臆测。
- `server/tests/test_cline_renew.py`：Cline 的续期判据（上游不给 refresh 寿命，
  `force=False` 一律跳过）、force 全刷、缺 refreshToken 记失败、仅签到号照常
  续期、没进窗口的号不计「跳过」、适配器缺失时返回空统计。
- `server/tests/test_platform_discovery_adapter.py`：Discovery 的 Key 脱敏与
  `dc_` uid 派生、目录归一与 8 条兜底、`quota_exceeded` → 402 冻 1 小时 /
  `rate_limit_exceeded` → 429 的区分、空模型名补 `auto`、`max_tokens` 原样透传、
  流式透传与 `usage` / 首字统计、`credits` / `checkin` 不支持。
- `server/tests/test_platform_kilo_adapter.py`：Kilo 的 token 脱敏与 `kl_` uid
  派生、资料两种形状、余额坏值返回 None、OpenRouter schema 目录（`isFree` /
  `pricing=0` / `supported_parameters`）、选号跳过 disabled / checkinOnly /
  冷却、`stream_options` 保留而 `store` / `service_tier` 被剥、美元额度与 60 秒
  缓存、空模型名兜底 `kilo-auto/free`。
- `server/tests/test_platform_cline_adapter.py`：Cline 的 refresh token 脱敏与
  `cl_` uid 派生、免费档合并顺序、`needs_force_stream` / `limit_signal`、
  「先落盘再使用」的 token 轮换、401 后同账号换 token 重试一次、SSE 聚合
  （reasoning 兜底 / `tool_calls` / 伪装限流抛 429）、非流式免费档被强制走流式
  且 `max_tokens` 被剥、空模型名兜底。
- `server/tests/test_platform_credits_snapshot.py`：平台余额快照的登记与回退，
  含美元单位（`unit='usd'`）随快照存取、积分平台不写该字段、设置页合计在整批
  美元时标 `usd` 而混合单位退回 `credits`。
- `server/tests/test_platform_credits.py`：前端余额展示口径（借 Node 跑
  `web/lib/platform-credits.ts`），含美元 `$` 格式化与「没有 unit 按积分」的兜底。
- `server/tests/test_platform_sse.py`：SSE 拼帧、错误帧、断开时不补错误帧。
- `server/tests/test_platform_realm.py`：前端版本映射（`global` ↔ `intl`）、
  版本开关的显示条件、账号 / 模型过滤、导入时补 `realm`。
- `server/tests/test_platform_routing.py`：`trae/` / `traecn/` / `traecom/` 前缀分流、
  非前缀模型不变、密钥版本旁路 + 白名单（含区域别名兼容）、`/v1/models` 合并。
- `server/tests/test_combo.py`：组合的写路径严格 / 读路径宽容、平滑加权选路、
  失败切换（含 402 额度耗尽与「模型不存在」这类 4xx 错误码）、全部失败时的状态码
  （429 / 402 / 404 与仍回 503 的凭据失效）、选路指标、配置快照在保存与整库
  替换后立即失效（读库失败时退回上一份）。
- `server/tests/test_protocol_platform_routing.py`：`/v1/responses` 与
  `/v1/messages` 的平台 / 组合路由（非流式、dict chunk 流、原始 SSE 字节流、
  错误体、版本旁路、`count_tokens`）。

测试一律离线（打桩 httpx / 用固定 SSE 文本 / 用自造 blob），不依赖真实账号。

---

## 11. 已知限制

1. **国际版刷新**已用真实 SG 账号跑通（2026-11-01）。
   `deviceproof.py` 的 ECDSA-P256 签名私钥优先取本机 `storage.json` 的
   `iCubeAuthInfo://icube-dc:<holder>`；没有客户端时用面板自生成的
   `data/platforms/trae/device.json`。签名原文是**秒级**时间戳 + `\n` 连接，
   路径 `/trae/api/v3/oauth/ExchangeToken`；`DeviceInfo.DeviceID` 用 holderId、
   `MachineID` 用绑定时上报的机器号。
   服务器部署没有客户端时，网页登录会自动生成并随账号保存
   `deviceKeyPEM` + `deviceId` + `machineId` + `devicePublicKeyPEM`；手工导入
   的账号若要刷新，也需要四项齐全且来自同一套设备（缺一项或对不上上游会以
   设备不匹配 20405 拒绝）。没有私钥时不带该字段，上游报错会如实回给界面，
   不会假装成功。
2. 平台调用在 `request_logs.realm` 里仍记成 `cn`（`realm` 语义是腾讯账号池）；
   平台归属另存 `request_logs.platform`（WorkBuddy 为空串），统计与日志都按它
   过滤。`usage_daily` / `usage_hourly` 的主键也含 `platform`，同名模型不会跨
   平台合并累计。
3. 刷新互斥是进程内锁：`uvicorn --workers N > 1` 时仍可能并发刷新同一账号，
   需要外部锁（Redis / 文件锁）才能多进程部署。

## 12. 模型组合（combo）

**目标**：让同一个下游模型名由多个平台成员分摊请求，从而让积分从多个平台扣。
单个请求仍然只落在一个成员上——上游的一次对话没法拆成两半计费。

- **虚拟平台**：组合注册成 `name='combo'` 的适配器，复用 `route_model` /
  `_platform_chat` / `/v1/models` 合并 / 密钥的 `platform_model` 旁路，网关不需要
  为它加分支。它没有账号，所以 `ui_visible=False`，不出现在平台下拉与账号页。
- **命名**：对外 `combo/<name>`；成员是 `platform/model`，**不要求同名**——不同
  平台对同一个模型叫法不同，要求同名会把最常见的情况挡在门外。
- **版本**：分版本平台（Trae / MiniMax / Qoder）的同一个裸模型可能在国内外两版
  都存在，只靠裸名无法区分。成员因此可以带一个可选的 `realm`（统计口径
  `cn` / `global`，也接受 Trae / Qoder 的 `intl` 与 `com`）：调用成员时透传成
  `ctx.realm`，与客户端直接发 `traecn/x` / `traecom/x` 是同一个提示。留空 =
  不指定，沿用平台原来的自动判断（Trae 按目录、MiniMax 按账号池、Qoder 按
  前缀/目录/密钥）。成员键包含版本（`trae/cn/x` 与 `trae/global/x` 不同），
  因此两版同名模型可以同时挂进一个组合，也可以各给不同权重。组合编辑页在
  目录出现多个版本时显示「版本」下拉；非分版本平台（WorkBuddy）不接受
  `realm`，它的版本写在模型名前缀里。
- **权重**：0.1–100（与账号权重同一口径），相对比例而不是优先级。选路用平滑加权
  轮询（SWRR）：只推进**被选中**成员的游标，否则没被用到的成员游标也会漂移，
  实际比例会偏离配置值。
- **失败切换**：一次请求最多尝试 3 个成员（含首选）。切换的条件是「换一个成员
  可能成功」：401/402/403/404/408/409/425/429 与 ≥500，以及虽然常是 4xx、但
  换成员有意义的错误码（`unsupported_model` / `model_not_found` / `quota_exceeded`
  / `login_expired` / `no_account` 等）。请求本身的问题（`invalid_request` /
  上下文超长 / 不支持的参数以及其他 4xx）原样抛出——换个平台也一样会失败，
  只会把真实原因换成笼统的「全部失败」。流式一旦产出首帧就不再切换。失败过的
  成员进入短冷却（15 秒起、按连续失败次数指数退避、封顶 10 分钟；429 优先采用
  上游的 `retry_after`；402 额度耗尽从 120 秒起，短退避只会白撞），冷却期内不再
  参与选路。**全部成员都失败时按失败原因决定状态码**：全 429 → 429
  `combo_rate_limited`（带 `Retry-After`），全 402 → 402 `combo_quota_exhausted`，
  全 404 / 全是「模型不认识」（含 Loomy / DuMate 的 400 `unsupported_model`）→
  404 `combo_model_not_found`（配置问题，重试不会好），其余才是 503
  `combo_all_failed`。一律回 503 会让客户端把「额度用完了」当成「网关抽风」而
  立即重试。**全 401/403 仍然回 503**：那是成员侧的账号坏了，不是调用方的密钥
  无效，对客户端说 403 会让人去改一个没写错的 token。`error.type` 与状态码
  同口径：402 与「额度用尽」类 code 给 `insufficient_quota`，聚合出的 429 给
  `rate_limit_error`——只看状态码的话 402 会落到 `invalid_request_error`，客户端
  会去改一个没写错的请求体。
- **会话粘性**：上游提示词缓存按账号/平台隔离，同一段对话换成员等于缓存全部
  作废；纯 SWRR 在等权两成员上几乎每轮换一次。所以第一个请求按权重选出成员后，
  按会话键钉住 30 分钟（与 Trae 账号粘性共用 `services/session_affinity.py`）：
  键的优先级是 `prompt_cache_key` → 会话 id
  （`conversation_id` / `conversation` / `metadata.*`）→ 首条 user 消息的哈希
  （**不用 `user`**：它通常是终端用户维度，恒定值会让权重分配失效）。显式键
  未命中时还会用「instructions / system + 首条 user」的内容指纹兜底——有些
  客户端每轮都换 `prompt_cache_key`，只按显式键会让同一段对话退化成纯轮询。
  内容兜底命中时不会写入新的显式键，避免随机键把绑定表撑爆。命中绑定
  **不推进** SWRR 游标（长会话会把全局比例带偏），只有新会话
  参与加权，长期比例仍符合权重。
  **绑定成员只是暂时不可用（429 / 5xx）时，本次请求切到其它成员但保留绑定**，
  冷却结束后自动回到原成员；只有 401/403/404（凭据或模型确定不可用）或连续
  失败 3 次才改绑。否则一次限流就会把整段对话永久搬到另一个平台，缓存反复
  作废、上下文被重复读取。粘性可在组合页右上角的「会话粘性」弹窗里关闭或
  调整时长（对应平台设置 `platforms.combo.session_affinity` /
  `session_affinity_ttl`，1–720 分钟）。绑定是进程内状态（上限 4096，按到期
  时间淘汰），重启后各会话重新选一次。
- **记账**：`_platform_chat` 用 `outcome.model`（组合适配器回填成真实成员，
  如 `trae/deepseek-V3`）作为 `mapped_model`，日志的 `platform` 列因此反映**实际
  去向**，而不是每次都记在虚拟平台名下。没有平台前缀的成员（WorkBuddy）记裸名，
  `platform` 列为空串、`realm` 由 `global:` 前缀判定，与直连一致。
- **请求日志**：组合卡片上的「请求日志」走 `GET /api/logs/combo/<name>`，按
  `request_logs.model = 'combo/<name>'` **等值**筛选（日志页的模型筛是 LIKE，
  会把 `combo/fast` 与 `combo/fast2` 混在一起）。返回最近 N 条明细 +
  整段时间范围的汇总（请求数、成功率、成功请求平均耗时、token、积分）。
  汇总不受 `limit` 影响；平均耗时只统计成功请求，避免 429 / 5xx 的毫秒级
  失败把均值拉低。明细里的「实际成员」取 `mapped_model`，版本取该行的 `realm`。
  「导出 CSV」走 `GET /api/logs/combo/<name>/export`：同一时间范围、同样的
  等值筛选，但**不受弹窗 100 条限制**；列里带首字延迟、生成耗时与输出速率
  （与统计页同一公式 `latency − first_token`），便于按成员 / 时间透视比较快慢。
  行数上限为 20000，超出时只给最近的一段，并用 `X-Export-Truncated` /
  `X-Export-Rows` / `X-Export-Total` 响应头告知，前端据此提示缩小时间范围。
  CSV 带 BOM（Excel 中文不乱码），以 `= + - @` 开头的文本单元格会加前导
  `'`，避免上游返回的错误文案在表格里被当成公式执行。
- **配置存储**：`settings.model_combos`，已加入 PG 同步白名单（`pgsync`），否则
  多实例部署时组合配置会丢。
- **读路径缓存**：组合配置在**每个组合请求**上都要读一次，所以网关走
  `combo.snapshot()` 的进程内快照，不再每请求 `get_setting` + 归一化。失效挂在
  `db` 的写入通知上（`on_setting_change`）：面板保存（`set_setting`）与 PG 恢复
  （`replace_database` 整库替换）都会立即生效。只挂保存路由的话，从 PG 恢复后
  网关会继续用旧组合——那是最难查的一类状态。读库失败时退回上一份快照，不把
  正在跑的请求变成 404。
- **下发模型名只有一个出口**：成员名 → 线上名的翻译（Loomy 别名、Qoder 剥
  `global:` 前缀）收敛到适配器的 `upstream_model()`，`chat_body()` 是**唯一**
  写 `body['model']` 的地方。各适配器自己写 `{**body, 'model': ...}` 时，新平台
  漏改就会把 `combo/fast` 原样发给上游，而上游报错完全指不到这里。组合这一层
  还会在调用成员之前先用 `chat_body()` 把入口名换成成员名，并且**每个成员各拿
  一份副本**——`body` 在多个成员之间复用，原地改写会让切成员后的请求带着上一个
  成员的模型名。
- **选路可观测性**：每次「入口名 × 成员 × 成败」都打一个
  `tokenfarm_combo_route_total{combo,member,result}`（见 `docs/metrics.md`），
  回答的是「这个成员是不是被反复选中后一直失败」。日志侧用一行 debug 说清
  **这次为什么落到这里**：`reason=pinned`（命中会话粘性）/ `pinned-cooling`
  （粘性成员在冷却，临时借道）/ `weighted`（新会话按权重），同时列出当前冷却中
  的成员。排障时最常见的两个问题——「流量为什么全在同一个成员」与「某个成员
  为什么一直没被选中」——分别靠 `reason` 和 `cooling` 列表回答。成员失败本身
  记 warning（带 status / code / 连续次数 / 冷却秒数），因为它是「上游坏了」而
  不是「请求有问题」；成员平台未注册同样记 warning，否则表现只是「这个成员从来
  没被用过」。

### 12.1 WorkBuddy 作为成员（v2）

WorkBuddy 在网关里的形态与平台适配器不同：它没有自己的账户页，走的是
`platform_route` 为空时的**直接反代**（分组解析 → 请求头 → JSON / SSE 转发），
账号选择、冷却、粘性会话、账号权重都在 `workbuddy2api` 进程里。因此它被接成
一个**伪适配器**（`platforms/workbuddy/`），并且**只能**作为组合成员：

- `ui_visible=False`：不进平台下拉与账号页（账号仍由上游池管理）；
- `combo_member=True`：出现在组合编辑页的成员候选里；
- `routable=False`：`workbuddy/<模型>` **不参与前缀解析**。它只给组合内部调用，
  若让前缀生效，就等于凭空多出一个与直连语义重复、且绕开请求上下文的入口。

三条实现要点：

1. **请求上下文透传**。`PlatformAdapter.chat(model, body, ctx)` 的 `ctx` 是
   `ChatContext(key / ip / ua / upstream_path)`，由网关的 `_authorize` 之后构造。
   WorkBuddy 成员用 `upstreamsvc.resolve_for_key(ctx.key)` 解析上游——绑定分组优先，
   **不回落默认上游**（否则密钥的分组隔离静默失效）；`upstream_path` 让成员沿用
   客户端打的那条入站路径（`/v1/chat/completions` 或 `/v2/chat/completions`，
   两条都是既有路由），被组合包一层不换协议版本。`ip` / `ua`
   留给需要按调用方区分的适配器；WorkBuddy 反代时上游看到的是面板自己的出口 IP
   （与直连路径一致，不伪造 `X-Forwarded-For`）。
2. **传输层只有一份**。`services/wbforward.py` 提供 `forward()` 与相关助手
   （请求头、超时、SSE 逐字节透传、`11102` / `14401` → 404 改判、429 `Retry-After`、
   usage 提取、首字延迟）。网关直连与组合成员共用它——分头写必然漂移，而漂移
   只会表现在其中一条路径上。
3. **记账只做一次**。鉴权（密钥 / IP / 版本 / 白名单 / 限流）在 `_authorize` 做，
   记账在 `_platform_chat` 做；成员适配器**不记账、不鉴权**。否则一次请求会出现
   两条日志、配额窗口被双倍消耗。

另外两处细节：

- **版本校验补丁**。组合入口名是 `combo/<name>`，不属于任何版本，`_authorize` 传
  `platform_model=True` 跳过了版本检查。成员却可能指向 `global:xxx`。所以组合
  适配器在**调用成员之前**对非 routable 成员补一次
  `keysvc.realm_allows(key, member_model, member=True)`：限定国内版的密钥拿到
  国际版成员时直接 400 `realm_mismatch`（拒绝发生在出网之前，不白花一次上游请求）。
- **成员模型名按版本限定**。模型目录里国际版的 id 是裸名，成员清单会补回
  `global:` 前缀。否则上游会把国际版模型当国内版去查，报 11102「模型不存在」——
  而不是报「版本不对」，排查时几乎看不出来。
- **权重是池级的**。成员权重只决定「组合内选到它的概率」；WorkBuddy 的账号权重
  仍由上游按 `auth.weight` 调度。两者不是一个口径，编辑页对该平台标注「池级权重」。

保存期校验：`services/combo.py:validate()` 只校验形状，路由层
（`routers/settings.py:save_combos`）再要求成员平台**存在且可用**
（`ui_visible or combo_member`）。未知平台、以及嵌套组合（虚拟平台）都会在保存
时被 400 拒绝，而不是等到调用时才表现为「全部失败」。

---

## 13. Qoder（内置网关）

Qoder 的协议包含 COSY 签名、设备指纹、OAuth 设备授权、模型目录与签到风控，
[`qoder2api-hub`](https://github.com/shuishuipingan/qoder2api-hub)（MIT）已经
把这些实现成一个完整的网关。token-farm 把它的协议层**以源码副本内置**在
`server/platforms/qoder/_vendor/`，由 `embedded.py` 在回环地址的随机空闲端口
拉起——不需要单独部署第二个项目，也没有额外的容器 / 端口 / 密钥要配。

部署与使用见 [`docs/qoder.md`](qoder.md)。架构上有四点值得记住：

1. **复用而不是复刻**。对话管线包含 SSE 心跳、工具调用泄漏回收、队列等待、
   账号熔断与换号、usage 记账；抄一遍必然与上游漂移。内置网关只在本进程内
   可达，适配器仍按 HTTP 形状调用它（`/v1/*`、`/accounts*`），行为与独立部署
   时逐字节一致。
2. **凭据不出进程**。账号池在内置网关里，`public()` 不返回明文令牌，所以管理端
   接口天然只拿到脱敏字段；内置网关的 API Key 每个进程随机生成、只在内存里，
   面板口令首次启动随机生成、只落哈希（默认 `admin` 不可用）。
3. **双区口径翻译**。网关用 `cn` / `intl`，token-farm 统计用 `cn` / `global`；
   `mapping.db_realm()` 是唯一的翻译点。模型列表把上游条目**按区域各列一条**
   （`qodercn/` / `qodercom/`），双区同名模型因此出现两条。
4. **能力开关**。Qoder 有账号权重（与 Trae / WorkBuddy 同一口径，只影响选号
   分摊），但没有强制冷却 API，也没有本机回环回调，所以
   `supports_cooling` / `supports_login_callback` 为 false，前端据此隐藏按钮，
   而不是让用户点下去才收到 400。**出口线路同理**：Qoder 的选号与出网都在
   内置网关进程里，账号级 `proxy` 没有落点，`supports_proxy` 也为 false；
   要给 Qoder 换出口，改网关自身的代理配置，而不是账号行上的线路。
5. **会话粘性由网关执行，键由适配器传**。真实选号在网关进程里，厂商自带的
   亲和有两种：消息前缀（`messages[:2]` 的哈希）与会话 id。前者在客户端每轮
   重排 system 消息时会漂移，且厂商**不认识** `prompt_cache_key`。所以
   `QoderClient.open_chat()` 用 `services/session_affinity.affinity_key()` 取键
   （`prompt_cache_key` → 会话 id → 首条 user 消息哈希），非空时随请求发
   `X-Session-Id`（厂商 `extract_session_key` 识别该头）；没有会话维度时不发，
   仍由厂商的前缀亲和兜底。绑定的 TTL / 容量与换号失效规则都在网关的
   `SessionAffinity` 里。

模型名对外是 `qodercn/<官方模型名>`（国内版）与 `qodercom/<官方模型名>`
（国际版）；旧式 `qoder/<官方模型名>` 继续兼容，`qoder/cn:` / `qoder/intl:`
仍可显式指定区域。未指定区域时交给内置网关按账号与模型自动选区。

---

## 14. 豆包（Doubao）

原生实现，包在 `server/platforms/doubao/`，不引入第三方运行时，也没有浏览器 /
签名依赖。协议事实来自 [`yyydsz/doubao2api`](https://github.com/yyydsz/doubao2api)
（MIT），核对范围见 `deploy/doubao-ref.txt`。

### 14.1 凭据与导入

凭证是**网页 Cookie**：`sessionid` / `sid_tt` / `session` / `ttwid` /
`s_v_web_id` / `odin_tt` / `passport_csrf_token` 至少有一个（导入时校验，避免
等第一次对话才 401）。没有 OAuth、没有 refresh token，Cookie 失效只能重新登录
网页版再抓一次。

- uid 由 `sessionid` 派生（缺失时退到 `sid_tt`，再退到整串 Cookie 的哈希），
  前缀 `db_`；同一份登录态重复导入覆盖同一条账号。
- 面板的「手工导入」支持直接粘贴整段 Cookie（后端认 `{cookie: ...}`），也能粘
  JSON。**没有**扫码登录：Passport 三层签名在参考实现里自己也不稳，提供一个
  必然失败的入口只会误导用户。
- Cookie 明文只落盘（`data/platforms/doubao/accounts/<uid>.json`，文件锁 + 原子
  替换 + 0600），接口只回摘要（有哪些字段、`sessionid` 尾部 6 位、长度）。

可选的设备字段随账号落盘：`deviceId` / `webId` / `localAppId`（默认 `582478`）。
缺 `deviceId` 时由 Cookie 哈希派生稳定值（`store.device_of`）——上游用
`device_id` / `fp=verify_<dev>` / `tea_uuid` 做风控，同一个账号每次随机换设备
指纹等于自己制造风险特征。

### 14.2 对话协议

`POST https://www.doubao.com/chat/completion`，查询参数是 `constants.QUERY`
（`aid=582478`、`pc_version=2.32.6`、`runtime_version=3.42.0`、`language=zh` 等）
加上逐请求的 `device_id` / `fp` / `tea_uuid` / `web_tab_id`；缺这些参数上游会按
匿名网页会话处理，表现为「200 但没有正文」。

请求体是豆包自己的「客户端消息 + option」结构，三条硬约束：

1. `messages[]` 只有一条 `block_type=10000` 的文本块；历史由
   `chat.flatten_messages` 压成「系统 / 用户 / 助手」的一段文本，最后一条 user
   放在末尾（豆包没有 OpenAI 那种多轮数组语义）。
2. `option.model_config.model_item_key`、`option.conversation_init_ext.model_item_key`、
   `option.aggregate_params.model_item_key` **三处必须一致**；只改一处，上游会
   按默认模型回答且不报错。
3. 工作模式（`agent_mode=1` / `conversation_mode=2` / `mode_id="3"`）要带
   `general_task_param` 与 `ext`，headless 场景必须 `enable_sandbox=false`，
   否则上游要求本地沙箱。

响应**固定是 SSE**（没有 `stream` 开关），翻译在 `chat.StreamTranslator`：

| 上游事件 | 处理 |
| --- | --- |
| `CHUNK_DELTA.text` | 正文增量（首选） |
| `STREAM_CHUNK` + `block_type=10000` | `text_block.text` 快照；用公共前缀算增量，`CHUNK_DELTA` 出现后不再使用 |
| `STREAM_CHUNK` + `block_type=10040` | `thinking_block.streaming_title`（或 `finish_title`）→ `reasoning_content` |
| `SSE_REPLY_END` | `end_type` 结束；**没有正文时**才用 `msg_finish_attr.brief` 兜底 |
| `SSE_ACK` | 只解析不保存（本轮不做上游会话复用） |

流式出口是 `PlatformChat.stream`（OpenAI chunk dict），由 `platforms/sse.py` 拼帧；
非流式则把整条 SSE 读完聚合成 `chat.completion`。上游错误帧（`error` /
`SSE_ERROR`）翻成 `StreamError`，收尾后继续抛出，让 `openai_sse` 补一帧错误并让
网关记 502——只设 `chat.error` 不抛，客户端会看到一条「正常结束的空流」。

### 14.3 usage 是估算值

豆包 SSE 里没有 token 计数。`chat.StreamTranslator` 按「字符 ÷ 1.7」估算
（`constants.CHARS_PER_TOKEN`），`usage.prompt_tokens` 用请求体里最终发给上游的
提示文本长度、`completion_tokens` 用已产出正文字符数。这是**估算**，不是上游
账单；统计页的数字只用于趋势与配额参考，不能拿去对账。

### 14.4 签到与额度

- 签到：`POST /alice/ug/campaign/landing/checkin`，body
  `{"scene": "pc_incentive_task_mvp_v3"}`。判定顺序：`code=710010702` → 无活动；
  `data.idempotent` 或文案含「已签到 / already」→ 已签到；`code=0` →
  `data.checked_in` 成功；其余算失败。上游按自然日幂等，重复调用不会多发。
  `earned_credit` 用 `reward_amount`（幂等命中时为 0），同时把
  `continuous_days` / `reward` 写入账号快照。
- 额度：`POST /alice/commerce/sale/subscription/quota/summary/`，body `{}`。
  递归找所有 `window_limits[]` 累加 `total_amount` / `used_amount`，
  `total_amount` 可能是带千分位 / `¥` / `$` 的字符串，统一清洗；每个窗口的
  `end_time` 作为到期明细。`credits()` 返回剩余额度（`unit='credits'`），并写
  `quota` 快照供账号页展示。
- 探活：豆包没有「查我是不是登录了」的只读接口，`client.probe` 借额度接口，
  200 且不是登录类文案 → `ok`。只在「刷新」按钮 / `refresh_account` 里调用，
  **不**进巡检（见 §6.2）。

### 14.5 账号调度

与 Discovery / Kilo / Cline 同一套 `_NATIVE_POOL`：权重 SWRR + LRU、会话亲和
（`session_affinity`）、单账号并发闸门、`max_accounts_try` 换号、`cooldown_429` /
`cooldown_5xx` 冷却。差别：

- **401 只换号、不冷却**：Cookie 失效不会自己好，冻一段时间没有意义。
- **429 默认冷却 60 秒**（`cooldown_429=0` 时也按 60 秒处理，与 Discovery 一致），
  额度耗尽的文案走 429，冻 1 小时由 `_cool_down` 的 402 分支承担（当前
  `client` 把明确的额度错误映射成 402 时才会触发）。
- 「仅签到」与「停用」互斥：开「仅签到」会清掉停用位，停用会清掉「仅签到」。

### 14.6 有意不跟

- **上游会话复用**：参考实现会保存 `SSE_ACK` 的 `conversation_id`，下一轮带回去。
  这里每轮都重发「压平后的历史 + 最后一条 user」：复用上游会话会让客户端重发
  的全文与上游已有历史叠在一起（上下文翻倍），而且上游会话被外部删除 / 过期时
  会静默失败，排查成本很高。
- **扫码登录 / 批量注册**：见 §14.1。
- **附件 / 图片**：本轮只做文本；模型条目 `attachment=False`，界面据此隐藏上传
  入口。
- **GLM 模型进目录**：上游实测报错，不列；需要时显式发 `doubao/glm-5.3`，
  `upstream_model()` 原样透传。

---

## 15. WPS 灵犀（Lingxi）

原生实现，包在 `server/platforms/lingxi/`，不引入第三方运行时。灵犀没有开放
平台 API，协议事实来自其桌面客户端自己使用的私有 **app-server** 接口，核对
自两份社区实现（均 MIT）：`xinstart-1/dsh-connect-lingxi`（含
`docs/PROTOCOL.md` 与逐字段抓包）与 `xfyang728/dsh-router-wpslingxi`。

### 15.1 凭据与导入

凭证是**网页 Cookie**，登录态字段是 `wps_sid`（导入时校验，缺了直接 400）。
没有 OAuth、没有 refresh token；Cookie 失效只能重新登录网页版再抓一次，所以
`refresh_account` 做的是「拿 Cookie 探一次活」，不是续期。

- uid 由 `wps_sid` 派生，前缀 `lx_`；同一份登录态重复导入覆盖同一条账号。
- 面板「手工导入」支持整段 Cookie、`Cookie:` 前缀、以及只粘 `wps_sid` 的值
  （后端正回键名）。没有扫码登录。
- Cookie 明文只落盘（`data/platforms/lingxi/accounts/<uid>.json`，文件锁 + 原子
  替换 + 0600），接口只回摘要与尾部 6 位。
- 请求头里的客户端指纹三件套 `x-kso-app-name` / `x-kso-app-version` /
  `x-kso-device-id` 由适配器补齐；设备号默认由 Cookie 稳定派生
  （`constants.device_id_for`），同一账号不会每次换一台设备。

### 15.2 对话协议：三步握手

一次对话是三次 HTTP 请求，全部带同一份 Cookie：

1. `POST /api/aioffice/v1/cowork/sessions` 建会话，取 `data.session_id`；
2. `POST /api/public/v1/credits/runs` 领 run，取 `data.run_id` / `group_id`；
3. `POST /api/public/v1/agent-runtime/chat/completions`（OpenAI 形状请求体）。

部分账号的会话只在 `lingxi.kdocs.cn` 有效，建会话 / 领 run 按「主站 → 登录域」
两个基址都试；**401 立即上抛**，不换基址也不重试。

**没开实扣积分时**（默认）会话 + run 按「账号 + 设备 + 模型」缓存 1 小时
（`constants.RUN_TTL_SECONDS`），与参考实现一致；run 失效（404 且文案含 run /
会话 / 过期 / not found / 不存在）时丢掉缓存重建一次再试。开了实扣积分后 run
必须每轮新建（见 §15.6）。对话头另带 `X-Client-Type` / `X-Cc-Session-ID` /
`X-Cc-Run-ID` / `X-Request-Id`，Cookie 有 `copilot_server_branch` 时再带
`X-Cc-Region`。

请求体本身是 OpenAI 形状，但实测有两条硬约束（`chat.build_body`）：

- `content` **只能是字符串**：分段数组（`[{type:"text",text}]`）任何角色都
  400 `InvalidArgument`，逐条展平；`developer` 角色换成 `system`。
- `tool_choice` **只能是字符串**：对象形式（指定具体函数）同样 400，降级成
  `required` / `auto`；`tools[].function.strict` 原样保留。

另外 `max_tokens` 要写进 `base_llm_arguments.max_tokens`，`reasoning_effort`
同时写顶层与 `llm_arguments.reasoning_effort`（只写一处上游行为不稳定）。

### 15.3 SSE 翻译

上游与 OpenAI 有几处本质差异（`chat.StreamTranslator`）：

| 上游 | 处理 |
| --- | --- |
| `choices[].text` | 正文增量 → `delta.content` |
| `choices[].reasoning_content` | 同层思考内容 → `delta.reasoning_content` |
| `choices[].tool_calls` | 同层工具调用；按 `index` 聚合 `function.arguments` |
| `finish_reason` | 唯一的结束标记；**没有** `[DONE]`，收尾帧由 `finish()` 统一发 |
| `usage.total_tokens` | 上游给的是**上下文大小**，必须 `prompt + completion` 重算 |

流结束前没见到 `finish_reason` → `truncated_stream`（502）；一条正文都没有 →
非流式 `empty_response`（502），流式补一段「(空响应)」再正常收尾。

`code !== "Success"` 与审核拦截都是**流内错误**：翻成 `StreamError` 后继续抛，
让 `openai_sse` 补错误帧并记 502。审核判定只看 `code` / `message` / `msg` /
`errmsg` / `error_msg` / `error` / `detail` / `reason` 等诊断字段（以及顶层的
纯字符串 `data`），**不看模型正文**——正文里出现 `sensitive` 这种普通英文词
曾被误判成审核拦截。

### 15.4 模型目录

在线目录是 `GET /api/aioffice/v1/sessions/plans`，`plans[]`（Pro / Max 模式）
在前、`models[]` 在后，顺序必须保持；有 `reasoning_efforts` 才算思考模型，
`max_new_tokens` 是输出上限，倍率里的全角 / 日文波浪线统一成 ASCII，徽标字段
叫 `tag`，介绍字段叫 `intro`（`desc` 常为空）。`default` 映射到
`deepseek-v4.1-flash`，并剥掉 `wps-lingxi/` / `lingxi/` 前缀。

读不到目录（未登录 / 接口失败）时回退 `constants.DEFAULT_MODELS`（逐字段抄自
一次真实响应，2026-11-10）。目录缓存与刷退避走 `catalog_ttl` /
`catalog_retry_ttl` / `catalog_timeout`。

### 15.5 签到与余额

- 签到 = 任务中心的 `daily_check_in`：先 `GET /api/public/v1/tasks` 找任务与
  可用日期（优先北京时间今天，否则第一个 `available`），再
  `POST /api/public/v1/tasks/daily_check_in/claim[?date=YYYY-MM-DD]`。
  `result == "ok" || "Success"` 或缺失按成功；「已领取 / 已签到」按 `already`
  处理而不是失败（否则用户会以为签到坏了）。
- 余额 = `GET /api/public/v1/credits/balance`：优先 `data.total_balance`，
  否则 `data.balance`；赠送批次取 `data.bonus_credits` 或
  `data.bonus_summary.items`，无时区的时间串按北京时间（`+08:00`）解析；
  15 天内到期的批次汇总成 `expiring`，`credits()` 一并给出到期明细。
- 探活 = `GET /api/public/v1/account/profile`：200 且 `result` 不是
  `UserUnauthorized` → `ok`，顺带取昵称。
- 任务面板（§4.1）：`GET /api/public/v1/tasks` 只读；`claimable` 由
  `POST /tasks/{key}/claim` 代领。邀请接口四个：`referral/activity` /
  `referral/code` / `referral/summary` / `referral/bind`，都要
  `X-Open-Position: invite_rewards`，返回 `{v7code, result, data}` 信封；
  `bind` body `{referral_code, source:"manual"}`。兑换权益走
  `POST /tasks/gift_card/redeem`（body `{code}`，`data.granted` 判成功）。
  签到日历实际下发 `{day, status}`，`parse_tasks` 同时保留 `day` 与 `date`。

### 15.6 每请求实扣积分（`credit_meter`）

设置页「实扣积分模式」与 Trae 同形（`''` / `delta` / `detail`），默认值跟随
`TF_LINGXI_CREDIT_METER`。**灵犀与 Trae 有一个关键差别**：上游只在网关上报了
run 生命周期（`running → completed`）后才结算，不上报的 run 永远不扣费。所以
对灵犀来说，这个开关同时决定「是否计费」：

| 取值 | 生命周期 | 实际扣费 | 取回本次金额的方式 |
| --- | --- | --- | --- |
| `''`（默认） | 不上报 | **不扣费**（旧行为） | 不取，日志 `credit` 为 NULL |
| `detail` | 每轮新会话 + 新 run，上报 `running` / `completed` | **扣** | 按 `session_id` 查上游流水 |
| `delta` | 同上，但会话复用 | **扣** | 账户累计消耗前后相减 |

> 打开任一非空档位 = 与桌面客户端一样真实消耗智点。不想扣费就保持 `''`。

实现要点（`adapter._meter_*`、`client.report_status`、`settle.py`）：

- 发对话前 `PUT /api/public/v1/sessions/{session_id}/execution_status`，body
  `{group_id, status:"running", run_token, lease_seconds:90}`；成功收尾再报
  `completed`（`terminal_reason: completed`），失败收尾报 `failed`。`completed`
  是上游结算的**唯一触发点**，缺了就不扣费。
- `completed` 的上报**扛得住取消**：客户端断开会让请求作用域被取消，但这时
  上报会改用独立任务补发一次——否则这次对话白跑（上游不扣费，本端也查不到
  流水）。这与参考实现「必须用独立 context」的口径一致。
- 灵犀的账是**延迟归账**：对话结束后立刻查余额 / 流水通常是 0，真实记录要过
  几分钟才出现。所以收尾**不等待**（否则 SSE 会挂几分钟），而是注册一个后台
  探针（20s / 40s / 60s / 120s / 180s / 300s，共约 12 分钟）：
  - `detail`：`GET /api/public/v1/credits/transaction/expense?session_id=…`，
    会话是本次请求专用的，负数 `amount` 即这次实付；并发安全。
  - `delta`：先记账户累计消耗基线，结算后再查一次求增量。并发时新请求会让
    旧窗口主动放弃（宁少勿重）；**因为延迟归账，这一档多数拿不到值**，只作
    兼容保留，优先用 `detail`。
- 查到后一次性补齐三处口径：`request_logs.credit`（日志页）、`usage_daily` /
  `usage_hourly`（统计页）、`api_keys.used_credit`（密钥积分额度）。补记按**请求
  发生那天**回填统计行（跨午夜的请求不会算到新的一天）；密钥的**每日**额度
  只在请求发生在今天时才动 —— 跨午夜的补记只进总额，否则会把今天的额度改
  写成昨天、等于凭空清零。窗口内始终查不到就保持 NULL（界面显示「—」），
  **绝不写估算值**。
- 未结算的探针只在内存里：进程重启会丢掉它们，那些请求的 `credit` 继续为空。
  丢的只是展示值，上游自己的账不受影响。

### 15.7 账号调度

与 DuMate / Discovery 同一套 `_NATIVE_POOL`：权重 SWRR + LRU、会话亲和
（`session_affinity`）、单账号并发闸门、`max_accounts_try` 换号、
`cooldown_429` / `cooldown_5xx` 冷却。**401 只换号、不冷却**；`min_credits`
不适用于灵犀（没有保底积分选号实现），设置页也不会露出该字段。

### 15.8 有意不跟

- **群聊任务**：`try_group_chat` 需要 WebSocket（`completions/ws`）+ 建群 /
  解散，本轮只做普通对话与每日签到。
- **一次性任务的自动完成**：专家模式 / 客户端任务需要用户在客户端产生行为，
  本端只展示并代领已经 `claimable` 的奖励，不代做。
- **图片输入**：`content` 只接受字符串，分段里的图片会被丢弃（日志记数量），
  模型条目仍如实标 `attachment` 供界面展示。
- **扫码登录**：见 §15.1。

---

## 16. 商汤小浣熊（Raccoon）

原生实现，包在 `server/platforms/raccoon/`，不引入第三方运行时，也没有 vendor
任何参考仓源码。小浣熊（`xiaohuanxiong.com`，商汤「办公小浣熊」）没有开放平台
API，协议事实来自其网页 / 桌面客户端共用的私有后端，核对自四份社区实现：
`haoxinyu1/xiaohuanxiong2api`（协议主体，Go）、
`flying-dragon-ai/pi-provider-xhx`（v1 / v2 差异、OAuth、思考档位）、
`zhlcba/dsh-raccoon-connect`（实测头、目录、对话、刷新、识图、风控）与
`RobbsLuo/Coding2API`（网页 JWT 交叉核对）。取用范围与「有意不跟」见
`deploy/raccoon-ref.txt`。

### 16.1 凭据与导入

凭证是**两个 JWT**：access token（约 3 小时）与 refresh token（约 30 天）。
refresh token 是**一次性轮换**的——上游换发后旧的立即作废，所以刷新成功必须
**先原子落盘再使用**（§16.2）。

- uid 派生优先读 JWT 里的 `user_id` / `userId` / `uid` / `userID` / `sub`，
  前缀 `rc_`；拿不到才退回凭证哈希。**不能用 refresh token 哈希**：它每次
  轮换都会变，会把同一个账号拆成多条。
- 面板「手工导入」接受裸 JWT、`Bearer` 前缀、整段 Cookie（里面的 `xhx_token`
  / `access_token` 会被抽出来），也支持 `{account: {...}}` 与扁平对象两种形状。
  导入阶段**不联网**，首次刷新 / 探活发生在 `refresh_account` 或第一次对话时。
- **本机客户端一键导入**：只读 `%USERPROFILE%\.box-agent\config\auth.json`
  （`BOX_AGENT_CONFIG_DIR` 可覆盖），兼容嵌套 `auth` / `tokens` 与 `token` /
  `auth_token` 作为 access 别名；不回写客户端。`import_from_desktop` 因此
  存在，「从本机导入」入口会出现。
- 凭据明文只落盘（`data/platforms/raccoon/accounts/<uid>.json`，文件锁 +
  原子替换 + 0600），接口只回摘要与 `has_token` / `has_refresh_token`。
- 请求头默认只带 `Content-Type` / `Accept` / `User-Agent`（Chrome 131
  Windows）/ `X-Org-Code`（个人版空）/ `X-Raccoon-Language: zh`，少带指纹；
  桌面端专用接口（每日积分触发）另补 `X-Client-Platform` /
  `X-Client-Version` / `X-Client-Device-ID`。

### 16.2 令牌轮换

`_token_for` 是唯一的取 token 入口：

- 内存缓存 access token 过期时刻（`exp`），按 5 分钟余量提前刷新；refresh
  token 永远以磁盘为准，因为它一次性、轮换后内存副本立刻过期。
- 刷新由**账号级锁**串行，避免并发请求拿同一个旧 refresh 去换两次。
- `POST /api/web/auth/v1/refresh`，body 只有 `{"refresh_token":"<jwt>"}`，
  不带 `Authorization`。响应 `{code:0,data:{access_token,refresh_token}}`；
  上游可能不回 `refresh_token`，此时沿用旧值。
- **先落盘再使用**：把新 access / refresh 与到期时间写回账号文件成功后才更新
  内存并返回。进程崩在中间时，至少新的 refresh 已在文件里，不会把账号永久
  踢下线。
- 刷新失败（非 401）先冷却一段时间（`REFRESH_FAIL_COOLDOWN`），避免上游抖动
  时每个请求都去轮换一次性 token、放大丢链窗口；401 则标记登录失效。

巡检（§6.2）看的是 **refresh token 的到期时间**，不是 access：access 只有约
3 小时且由 `_token_for` 按需换，拿它当判据会每小时轮换一遍一次性 refresh。

### 16.3 对话协议与端点降级

对话是标准 OpenAI Chat Completions 形状，路径 `POST /api/web/llm/v2/chat/completions`
（v1 同路径在 `/llm/v1`）。流式是真增量 SSE，末尾 `data: [DONE]`，思考在
`choices[].delta.reasoning_content`；非流式在 `choices[0].message`。

参考实现与真机抓包共同确认的三条事实决定了降级策略：

| 端点 | 流式 | 非流式 |
| --- | --- | --- |
| v2 | 官方客户端路径，最完整；非官网客户端偶被风控 `429 code 200108` | 可用 |
| v1 | 实测报 `invalid upstream stream response` | 可用 |

- 客户端要流式：先走 v2 流式；非鉴权类失败（401/402/403 除外）退回 v1
  **非流式**，再由 `synthesize_stream` 合成 OpenAI chunk——客户端仍然拿到 SSE，
  只是正文一次性到达。
- 非流式请求：先 v2、失败再 v1，不合成。
- **未知模型**：上游会**静默回落默认模型并返回 200**。所以本地先按目录校验
  （`_assert_known_model`），并在首帧比对上游回显的 `model`：不一致判
  `model_fallback`（502），绝不把「默认模型的回答」当成用户请求的模型。
- 请求体归一（`chat_body`）：`reasoning_effort` 只做半套校验（垃圾值也可能
  200），默认剥离；控制思考用 `raccoon_thinking`；`verbose: true` 是官方
  客户端会带的字段；`max_tokens` 缺省补 16384（太小会得到「只有思考没有
  正文」），客户端只发 OpenAI 新字段 `max_completion_tokens` 时沿用它的预算。

### 16.4 模型目录

在线目录是 `GET /api/web/llm/v2/model_catalog`：

- 根上有 `data` 就用 `data`；**平铺所有 `categories[].models[]`**，不按
  `category.type == "chat"` 过滤（参考实现口径）；同名模型首次出现者胜；
  `raccoon-work` 是应用内建 agent 路由位，跳过。
- 模型 id 在 `name`（也兼容 `model_name`）；展示名优先 `display_description`
  （截 64）> `description` > `name`。
- `context_window` / `max_tokens` 先看顶层再看 `params`；倍率字段
  `billing_multiplier` / `billing_effective_multiplier` / `billing_status`，
  `billing_effective_multiplier=0` 为限免。
- 思考：`params.raccoon_thinking`（`supported` / `can_disable` /
  `display_levels[].value` / `default_level`），上游思考字段同名。
- **识图标签不可靠**：`sn-glm-5-3` 标 vision 实际丢图；`sn-glm-5-3-flash` /
  `sn-deepseek-v4-1-flash` 无标签却能识图。已实测模型以
  `constants.MEASURED_VISION` 为准，未测过才回退标签；只作展示参考，不用它
  决定是否接受图片。

读不到目录时回退 `constants.DEFAULT_MODELS`（逐字段抄自一次真实响应）。
目录缓存与刷退避走 `catalog_ttl` / `catalog_retry_ttl` / `catalog_timeout`。

### 16.5 签到与积分

积分口径：每笔请求扣 1 积分（与 token 无关），每日 00:00 发 300 分（7 天
有效）。

- 余额 = `GET /api/web/points/v1/balance` →
  `data.{available_points,daily_points,monthly_points,reward_points,topup_points}`
  （数字或字符串，统一转数值）。`credits()` 返回 `credits` / `remain` /
  `daily_points` / `monthly_points` 等，默认 60 秒缓存。
- 明细 = `GET /api/web/points/v1/bills?paging.limit=&paging.offset=0`，列表在
  `data.items[]`，字段 `bill_id/client_type/event_name/biz_type/points/created_at`。
- **签到没有独立领取接口**：桌面客户端打开时读一次
  `GET /api/web/office/v3/setting_info`，服务端就会把当天积分发下来；响应
  `data.point_grant_popups` / `data.point_grant_toast` 是领取提示。所以
  `checkin` 先触发发放、再查 `bills`，只汇总 `points > 0` 且 `created_at`
  属于**北京时间当天**（01:30 前按前一天宽限）的条目。触发了提示才算「本次
  发放」，积分早已在账上时按 `already` 处理，而不是失败。

### 16.6 账号调度

与 DuMate / Discovery / 灵犀同一套 `_NATIVE_POOL`：权重 SWRR + LRU、会话
亲和（`session_affinity`）、单账号并发闸门、`max_accounts_try` 换号、
`cooldown_429` / `cooldown_5xx` 冷却。**401 只换号、不冷却**；账号并发默认 2
（上游约 5 并发，第 6 个回 `429 user_model_rate_limited`）。风控 `200108` 按
约 25 分钟冷却；积分耗尽按 402 冻 10 分钟。`min_credits` / `credit_meter`
不适用：上游每请求扣固定 1 分，账单延迟归账，无法按本次对话精确计量。

### 16.7 有意不跟

- **网页 OAuth 扫码登录**：回环回调需要常驻回调端口，面板已有「手工导入 +
  本机客户端导入」两条路径。授权码换 token
  （`POST /auth/v1/login_with_authorization_code`）已记录协议，但本轮不接。
- **设备绑定自愈**：`POST /auth/v1/devices_current_bind` 在已绑会话上重复
  bind 会永久锁死，风险高于收益；需要时由用户在网页重新登录。
- **参考仓的 Go 运行时 / 账号池 / 面板**：选号 / 冷却 / 亲和全部按 token-farm
  口径。
- **按目录 vision 标签决定接受图片**：标签实测两个方向都错过。
