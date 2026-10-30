# 多平台接入（token farm）

本文说明把 `workbuddy-manager` 从「WorkBuddy 专用管理面板」扩展成
「多平台账号池 + 任务 + 反代网关」的架构、边界与落地路线。

第一个原生接入的平台是 **Trae**（国内版 / 国际版）。WorkBuddy 的既有链路
**完全不改**：仍然由随包的 `workbuddy2api`（Go）持有账号池，面板只做管理面与
前置反代。

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
client ──/v1/chat/completions──> workbuddy-manager (FastAPI)
                                    │ 鉴权/IP/限流/日志
                                    └──> workbuddy2api (Go, 127.0.0.1:7863)
                                            └── 腾讯账号池
```

### 目标（多平台）

```
client ──/v1/chat/completions──> workbuddy-manager
                                    │ 鉴权/IP/限流/日志  （共用，唯一入口）
                                    ├── model 无平台前缀 ──> workbuddy2api（旧行为）
                                    └── model = "trae/xxx" ─> TraeAdapter
                                                                ├── 账号池（本地文件）
                                                                ├── token 轮换刷新
                                                                └── llm_raw_chat（SSE）
```

平台路由**只由模型名决定**，不由密钥决定。原因：一把密钥可能既想调
WorkBuddy 又想调 Trae；按密钥绑平台会让用户为每个平台各建一把密钥，而额度、
IP 白名单、日志口径又得重复配置一遍。

---

## 3. 目录结构

```
server/platforms/
  __init__.py            # 对外只暴露 registry + openai_sse
  base.py                # PlatformAdapter 契约 / PlatformError / PlatformChat
  registry.py            # 注册表 + 模型前缀解析
  sse.py                 # OpenAI chunk → SSE 帧（含错误帧 / 收尾记账回调）
  trae/
    __init__.py
    constants.py         # 双区主机、路径、App-Id、UA
    fingerprint.py       # 由 uid 稳定派生设备指纹
    catalog.py           # 模型目录兜底（chat=true 的条目）
    store.py             # 账号文件（原子写 + 文件锁）
    client.py            # 上游协议层（httpx 异步）
    chat.py              # SSE → OpenAI chunk / chat.completion
    icube.py             # 客户端凭据 blob 的 AES 编解码
    desktop.py           # 从本机客户端导入、并把新凭据回写客户端
    adapter.py           # 实现 PlatformAdapter：账号/签到/额度/模型/对话
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
| `model_prefix` | `f'{name}/'`，网关据此分流 |
| `supports_checkin` | 该平台是否有「每日签到」 |
| `list_accounts()` | 不含明文凭据的账号列表 |
| `import_account(payload)` | 导入 / 更新一个账号 |
| `delete_account(id)` | 删除 |
| `refresh_account(id, force)` | 刷新令牌（轮换式必须原子落盘） |
| `checkin(id)` | 执行签到，返回结构化结果 |
| `usage(id)` | 额度 / 用量快照 |
| `list_models()` | 该平台可调模型（`id` 带前缀，`model_name` 为裸名） |
| `chat(model, body)` | 返回 `PlatformChat` |
| `import_from_desktop()` | **可选**：从本机客户端一键导入（没有该方法的平台，管理端会回 400） |

`PlatformChat` 有两种形态：

- **非流式**：直接给 `payload`（已经是 OpenAI 形状的 `chat.completion`）；
- **流式**：给 `stream`，异步产出 OpenAI `chat.completion.chunk` 字典，
  由 `platforms/sse.py` 拼成 SSE 字节流（网关不拼帧——`routers/gateway.py`
  里「只透传上游 chunk」的架构约束由 `test_gateway_passthrough` 逐行守着）。

这样协议差异（Trae 永远返回 SSE、WorkBuddy 返回标准 SSE）被关在适配器内，
网关只认 OpenAI 形状。

管理端端点（都需要管理员会话）：

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
| POST | `/api/platforms/{name}/checkin-all` | 批量签到（并发 3，单条失败不中断） |
| GET | `/api/platforms/{name}/models` | 模型目录（`?refresh=true` 拉线上） |

权限分三档（与账号页同口径，不是一律「已登录」）：

- **读**（平台清单 / 账号列表 / 额度 / 模型目录）→ `current_user`，只读令牌也能看；
- **操作**（刷新令牌 / 签到 / 批量签到）→ `require_admin`；
- **改凭据**（手工导入 / 一键导入 / 删除账号）→ `require_session_admin`，
  与 `/api/accounts/upload` 同级：把外部平台凭据写进池子这件事只对真人开放。

这三个写端点已登记进 `test_api_tokens` 的 `SESSION_ONLY` 清单；`realm` /
`force` 两个 query 参数已登记进 `test_request_binding` 的
`ALLOWED_QUERY_POSTS` —— 两处都是仓库既有的「权限 / 参数形态必须显式声明」
闸门，新增接口不登记就会红。

---

## 5. 路由与模型命名

- `trae/<model>` → Trae。前缀在转发前剥掉，上游只看到裸模型名。
- 其他模型名 → 原样走 WorkBuddy（与今天完全一致）。
- `/v1/models` 会把 Trae 的模型以 `trae/` 前缀并入列表，`owned_by` 标成
  `trae`，客户端据此区分来源。

**为什么必须带前缀**：Trae 的模型 id 大小写混排（`DeepSeek-V4-Flash-Official`），
且与腾讯的模型名可能重名。前缀是唯一稳定的路由信号，不依赖探测或猜测。

前缀只加在**模型名**上，不影响密钥白名单语义：白名单里写
`trae/glm-5.2` 即可精确限制到该平台。

### 模型列表以**上游目录**为准

国内版的 `model_list` 会随账号权益变化，而且**比本地目录小很多**：实测一个
新账号只回 7 个模型，本地目录里那 30 个中相当一部分调用时直接回
`4023 model is unknown`。所以：

- 管理端「刷新模型」→ 拉上游目录并缓存（6 小时），之后以它为准；
- `/v1/models` 默认只读缓存，**不发网络请求**（客户端启动时不能被上游超时拖住）；
- 只有「完全没有缓存」时才隐式尝试一次（10 秒超时，失败后冷却 10 分钟），
  这样首次使用不会把一堆调不通的模型推给客户端；
- 缓存与失败冷却都存在 `settings` 表里，重启不丢。

本地目录 `catalog.py` 只作为**兜底**（国际版 `model_list` 是 403，或还没有
任何可用账号时）。

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

| 用途 | 国内版 | 国际版 |
| --- | --- | --- |
| 核心数据面 | `https://trae-api-cn.mchost.guru` | `https://core-normal.traeapi.us` |
| 账号 / 刷新 | `https://api.trae.com.cn` | `https://grow-normal.trae.ai` |
| 付费 / 权益 | `https://api.trae.com.cn` | `https://grow-normal.traeapi.us` |
| 签到 | `https://api.trae.cn` | 无（404） |
| UA | `Trae CN/3.4.3` | `Trae/3.6.2` |

- 对话：`POST /api/ide/v1/llm_raw_chat`，**服务端始终 SSE**。
- 鉴权：`x-ide-token` + `Authorization: Cloud-IDE-JWT <token>`（国内版）；
  国际版对话改用 `Authorization: Bearer <token>` 并额外带客户端版本头。
- 刷新：`POST /cloudide/api/v3/trae/oauth/ExchangeToken`。
- 签到：`/trae/api/v2/ug/checkin_credits/{status,claim}`，国内版真实可用。
- 模型列表：`POST /api/ide/v1/model_list`（仅国内版；国际版 403，用本地目录）。

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
  导入、回写、备份。
- `server/tests/test_platform_sse.py`：SSE 拼帧、错误帧、断开时不补错误帧。
- `server/tests/test_platform_routing.py`：`trae/` 前缀分流、非前缀模型不变、
  密钥版本旁路 + 白名单、`/v1/models` 合并。

测试一律离线（打桩 httpx / 用固定 SSE 文本 / 用自造 blob），不依赖真实账号。

---

## 11. 已知限制

1. **国际版刷新**可能需要 `DeviceProof`（ECDSA-P256 签名，私钥来自客户端的
   `iCubeAuthInfo://icube-dc`）。本机客户端只登录了国内版，未实测；解出私钥
   并在刷新时签名是可选的下一步。
2. 平台调用在 `request_logs.realm` 里被记成 `cn`（`realm` 语义是腾讯账号池），
   平台归属看 `model` 前缀即可；如需独立维度，应给日志表加 `platform` 列。
3. 刷新互斥是进程内锁：`uvicorn --workers N > 1` 时仍可能并发刷新同一账号，
   需要外部锁（Redis / 文件锁）才能多进程部署。
