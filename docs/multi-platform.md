# 多平台接入（token farm）

本文说明把 `workbuddy-manager` 从「WorkBuddy 专用管理面板」扩展成
「多平台账号池 + 任务 + 反代网关」的架构、边界与落地路线。

第一个原生接入的平台是 **Trae**（国内版 / 国际版）。WorkBuddy 的既有链路
**完全不改**：仍然由随包的 `workbuddy2api`（Go）持有账号池，面板只做管理面与
前置反代。第三个平台是 **Qoder**（国内版 / 国际版），协议层以源码副本内置在
进程里（`platforms/qoder/_vendor/`），同样不需要额外部署。第四个平台是
**Loomy**（讯飞星火），原生实现、单区域（`loomy/` 前缀），账号服务与模型网关
都由进程内代码直接访问。随后又按同一契约原生实现了 **DuMate**（百度搭子，
单区域 `dumate/` 前缀，网页 Cookie 凭证）与 **MiniMax Code**（双区域，
`minimaxcn/` / `minimaxcom/` 前缀，OAuth 设备码 + 一次性 refresh token）。

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
                                    └── model = "minimaxcn/xxx" / "minimaxcom/xxx" ─> MinimaxAdapter
                                                                ├── 双区账号池（OAuth refresh token）
                                                                ├── Anthropic Messages 端点（内部翻译）
                                                                └── 签到 / 积分 / M Plan 窗口
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
| `list_accounts()` | 不含明文凭据的账号列表 |
| `import_account(payload)` | 导入 / 更新一个账号 |
| `delete_account(id)` | 删除 |
| `refresh_account(id, force)` | 刷新令牌（轮换式必须原子落盘） |
| `checkin(id)` | 执行签到，返回结构化结果 |
| `usage(id)` | 额度 / 用量快照 |
| `list_models()` | 该平台可调模型（`id` 为区域化前缀 + 裸名，`model_name` 为裸名） |
| `chat(model, body, ctx)` | 返回 `PlatformChat`；`ctx` 是请求上下文（`key` / `ip` / `ua` / `upstream_path` / `realm`），不需要的平台忽略 |
| `ui_visible` / `combo_member` / `routable` | 是否出现在平台下拉 / 组合成员候选 / `<平台>[cn|com]/<模型>` 前缀解析（见 §12.1） |
| `pool_weight` | 账号权重是否只在上游**池内**生效（WorkBuddy 为 `True`，界面标注「池级权重」） |
| `record_name(bare_model)` | 记账用的 `mapped_model`：默认 `<平台>/<模型>`；WorkBuddy 为裸名 |
| `import_from_desktop()` | **可选**：从本机客户端一键导入（没有该方法的平台，管理端会回 400） |
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
| POST | `/api/platforms/{name}/accounts/import` | 手工导入（单条或 `{accounts:[...]}`） |
| POST | `/api/platforms/{name}/accounts/import-desktop` | 从本机客户端导入 |
| POST | `/api/platforms/{name}/accounts/{id}/refresh` | 刷新令牌 |
| DELETE | `/api/platforms/{name}/accounts/{id}` | 删除 |
| GET | `/api/platforms/{name}/accounts/{id}/usage` | 额度快照 |
| POST | `/api/platforms/{name}/accounts/{id}/checkin` | 单个签到 |
| POST | `/api/platforms/{name}/checkin-all` | 批量签到（并发与账号间隔按平台设置，默认并发 2、间隔 2–5 秒；单条失败不中断；跳过已冻结账号） |
| GET | `/api/platforms/{name}/settings` | 平台设置：单账号在途上限（总吞吐按可用账号数折算）+ 批量签到节奏 + 请求超时 / 换号次数 / 平台专属开关（会话亲和、桌面端回写） |
| PUT | `/api/platforms/{name}/settings` | 改上述设置（可只提交要改的字段；不支持的字段会 400） |
| PATCH | `/api/platforms/{name}/accounts/{id}` | 账号调度参数（`weight` / `disabled`） |
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
- **删除账号** → `require_session_admin`，与 `/api/accounts/upload` 同级：
  把外部平台凭据写进池子或从池子里删掉这件事只对真人开放。

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
- **临时冻结**（`disabled` + `disabledReason`）：退出选号**与批量任务**（批量签到
  会跳过它），凭据仍保留，随时可以恢复。它解决的是「这个号先别用了」而不是
  「这个号坏了」——所以界面把它归进「已停用」，而不是「需处理」。
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
  只有面板显式保存过才以库里的值为准。
- **Trae 协议开关**：原先只能用 `TF_TRAE_*` 环境变量改的十个兼容开关（原生
  工具透传、正文 / 标签调用还原、工具协议提示、退化调用判错、尾部规则、思考
  强度透传、思考内容兜底、空产出判错、严格结束判据）现在也在同一张表单里，
  同样「未保存跟随环境变量、保存后以面板为准」，改完立即生效。
- **协议兼容开关**（Qoder）：内置网关的工具残片收尾 / Anthropic 思考块 /
  结构化工具历史 / 图片预处理 / 本地 Web 工具在同一张表单的「协议兼容」里，
  默认「网关默认」= 与改动前完全一致。网关每次请求现读，保存即生效，不用重启
  内置网关；本地 Web 工具是唯一由模型驱动出网的能力，默认关闭。详见
  [`docs/qoder.md`](qoder.md) 的「协议兼容开关」。
- **Loomy 的设置集**：与 Trae 同构的选号 / 缓存 / 冷却参数（`max_accounts_try`、
  `session_affinity`、`catalog_*`、`credits_*`、`cooldown_*`）。它支持「从本机
  导入」，但客户端文件是**只读**的（没有会话回写），也没有协议兼容开关，因此
  不含 `desktop_sync` / `solo_max_tokens` / `credit_meter`。
  详见 [`docs/loomy.md`](loomy.md)。
- **DuMate / MiniMax Code 的设置集**：直接复用 Loomy 那一组选号 / 缓存 / 冷却
  参数（见 `server/platforms/settings.py` 的 `_DUMATE`、`_MINIMAX`）。MiniMax
  Code 额外支持网页设备码登录；DuMate 的凭证是网页 Cookie，导入后**不联网**
  校验，首次刷新或对话时才向上游换 token。

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
  与 WorkBuddy 的 `credits_snapshot` 分开），可用
  `GET /api/platforms/{name}/credits-snapshot` 只读对账。查询失败时账号列表会
  带回 `last_credits` / `last_registered_at`，界面以 `~` 标成上次登记值，而不是
  把旧数字伪装成实时余额。
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
  目录降级、选号、token 缓存与 401 重试、`max_tokens` 钳制、SSE 扫描、签到与积分。
- `server/tests/test_platform_minimax_adapter.py`：MiniMax Code 的账号脱敏、目录解析、
  双区选号、refresh token 轮换落盘、401 重试、签到 / 积分、OpenAI ↔ Anthropic
  请求 / 响应 / 流式翻译。
- `server/tests/test_platform_sse.py`：SSE 拼帧、错误帧、断开时不补错误帧。
- `server/tests/test_platform_realm.py`：前端版本映射（`global` ↔ `intl`）、
  版本开关的显示条件、账号 / 模型过滤、导入时补 `realm`。
- `server/tests/test_platform_routing.py`：`trae/` / `traecn/` / `traecom/` 前缀分流、
  非前缀模型不变、密钥版本旁路 + 白名单（含区域别名兼容）、`/v1/models` 合并。
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
- **权重**：0.1–100（与账号权重同一口径），相对比例而不是优先级。选路用平滑加权
  轮询（SWRR）：只推进**被选中**成员的游标，否则没被用到的成员游标也会漂移，
  实际比例会偏离配置值。
- **失败切换**：一次请求最多尝试 3 个成员（含首选）。只有
  401/403/404/408/409/425/429 与 ≥500 才切换；请求本身的问题
  （`invalid_request` / 上下文超长 / 不支持的参数以及其他 4xx）原样抛出——
  换个平台也一样会失败，只会把真实原因换成笼统的「全部失败」。流式一旦产出
  首帧就不再切换。失败过的成员进入短冷却（15 秒起、按连续失败次数指数退避、
  封顶 10 分钟；429 优先采用上游的 `retry_after`），冷却期内不再参与选路。
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
- **配置存储**：`settings.model_combos`，已加入 PG 同步白名单（`pgsync`），否则
  多实例部署时组合配置会丢。

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
   （请求头、超时、SSE 逐字节透传、`11102 → 404` 改判、429 `Retry-After`、
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
   而不是让用户点下去才收到 400。
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
