# AutoClaw 接入（智谱 / Z.ai）

> **状态：已接入。** `server/platforms/autoclaw/` 已包含 `constants.py` /
> `protocol.py` / `catalog.py` / `store.py` / `client.py` / `login.py` /
> `adapter.py`，并已注册到平台列表（前缀 `autoclawcn/` / `autoclawcom/`，
> 旧式 `autoclaw/` 兼容）。用户向的完整说明见
> [`docs/multi-platform.md`](multi-platform.md) §18；本文保留接入前的协议事实、
> 分歧记录与「有意不做」。
>
> **本文中标注「待实测」的端点仍没有用本仓库的真实账号跑过**——实现按
> 「主地址 + 回退」与「首选路径 + 回退路径」写，联调后只需调整
> `constants.SITES` / `protocol.chat_paths()` 的顺序，不必改策略层。
> 协议事实与来源见 [`deploy/autoclaw-ref.txt`](../deploy/autoclaw-ref.txt)。

AutoClaw（中文名「澳龙」）是智谱推出的桌面端 AI Agent，基于 OpenClaw 封装，
积分制，模型为 GLM 系列 + DeepSeek 系列。官方站：国内
`https://autoclaw.zhipuai.cn`，国际 `https://autoclaw.z.ai`。

它与本仓库已有的 Trae / Qoder / MiniMax Code 属于同一类接入对象：
**国内版与国际版是两套账号体系，凭证不通用**。

---

## 1. 双区对照

| | 国内版 | 国际版 |
| --- | --- | --- |
| 登录 | +86 手机号 + 短信验证码 | Google / z.ai 网页 OAuth |
| 账号面（登录 / 刷新 / 签到 / 钱包 / 模型目录） | `https://autoglm-api.zhipuai.cn` | `https://autoglm-api.autoglm.ai` |
| 推理面 | `https://autoglm-acceleration-api.zhipuai.cn` | `https://autoglm-api.autoglm.ai` |
| 客户端版本（参考） | 2.1.0（2.x） | 1.18.x（1.x） |
| 模型目录（参考） | 4 个：`zai_auto` / `zai_auto-fast` / `zaicoding_glm-5.3` / `zai_glm-5.3-flash` | 另含 `zai_glm-5-turbo`、`tdpsk_deepseek-*` |
| 签到 | **待实测**（有实现说无，有实现说有） | 有 `daily_signin` |

国内版为什么要拆两个域名：账号 / 积分 / 任务仍在 `autoglm-api`，而模型推理
被迁到加速域 `autoglm-acceleration-api`。国内 token 打国际 chat 域会 401，
国际 token 打国内同理——适配器必须按 token 的 JWT `source_id`
（`agent` 开头 = 国内短信号）或账号记录的 realm 选域，不能按模型名猜。

---

## 2. 接入后的形态（拟）

```
client ──autoclawcn/<model>──┐
       ──autoclawcom/<model>─┤
                             ▼
                    token-farm 网关
                      │ 鉴权 / IP / 限流 / 记账（与其他平台共用）
                      └──> AutoClawAdapter
                             ├── 账号池（双区，data/platforms/autoclaw/accounts/）
                             ├── 国内：短信登录 + 账号面签名
                             ├── 国际：OAuth 回环 + 粘回调兜底
                             ├── 模型目录：实时 model-config
                             ├── 积分：wallets / ledgers_std
                             └── 对话：OpenAI 兼容端点（SSE 原样透传）
```

命名跟随仓库既有约定，而不是照抄参考项目的 `autoclawglobal`：

- 平台名：`autoclaw`，`label` 用「AutoClaw」；
- `realm_aware=True`，前缀 `autoclawcn/` 与 `autoclawcom/`；
- 旧式 `autoclaw/` 作为兼容前缀（按账号或目录推断区域）；
- 内部 realm 值用 `cn` / `global`（与 MiniMax Code 一致，也是 db 口径），
  界面显示 `cn` / `global`，只在 `web/lib/platform-realm.ts` 翻译一次。

---

## 3. 账号

拟落 `data/platforms/autoclaw/accounts/<uid>.json`，字段：

| 字段 | 说明 |
| --- | --- |
| `realm` | `cn` / `global`；决定基址、客户端版本与登录方式 |
| `accessToken` / `refreshToken` | refresh token 一次一换，刷新必须先落盘 |
| `expiresAt` | 优先取 JWT `exp`，其次 `expires_in`，都没有按 5 小时保守记（宁可早刷新） |
| `deviceId` | 登录时生成并贯穿刷新 / 积分 / 任务；JWT 里也有 `device_id` claim 可兜底 |
| `userId` / `phone` / `email` | 展示与去重用；不要用来选域 |
| `source` | `sms` / `oauth` / `manual` |

账号 id 拟由「区域 + 用户标识」派生（例如 `ac_<sha256 前缀>`），同一用户重登
覆盖，不新增一条。**第一版不做「从本机导入」**：2.x 桌面端凭证是 DPAPI + AES-GCM，
解密与设备绑定是另一条链路，且与桌面端共用 refresh token 会互踢。

---

## 4. 登录

### 国内：手机号 + 短信

两步，没有浏览器：

1. `POST {CN_ACCOUNT}/userapi/v1/agent-send-code`
   `{phone, source_id:"autoclaw", device_id}` → 记住 `device_id`；
2. `POST {CN_ACCOUNT}/userapi/v1/agent-login/`（末尾斜杠必须保留）
   `{phone, code:<数字>, platform:"web", source_id, device_id}` → tokens。

契约映射与 Loomy 的短信登录相同：`login_start` 返回 `pending`，
`login_send_code` / `login_submit_code` 完成，前端就地收手机号与验证码。
发码冷却、错误次数上限走同一套会话状态机。

### 国际：OAuth 回环

1. `POST {GLOBAL}/userapi/overseasv1/oauth-captcha-config` 拿阿里云滑块配置；
2. 本机起临时 HTTP 服务，内嵌 AliyunCaptcha 引导页完成滑块；
3. `POST .../{zai|google}-oauth-url` → `oauth_url`，打开授权页；
4. 回调 `http://localhost:<白名单端口>/auth/callback-<vendor>` 收 `code` / `state`；
5. `POST .../{zai|google}-oauth-login` 换 tokens。

端口必须是官方 OAuth client 注册过的白名单端口（18432 / 19654 / 19723 /
53699 等），随便挑端口会得到 `Redirect URI not registered for this client`。
远程部署时浏览器里的 `127.0.0.1` 指向用户自己的电脑，后端收不到回调——这时
退化为「打开授权页 + 粘回完整回调 URL」，与 Trae 国际版的兜底同形。

**滑块是这套接入最大的不确定性**：纯服务端做不了，必须在本机浏览器里完成。
第一版可以只做「粘回调」，把内嵌滑块留给后续。

---

## 5. 模型与对话

### 目录

`GET {host}/autoclaw-proxy/proxy/autoclaw-model-config`，每个区域用本区账号拉。
响应兼容顶层 `models` 与 `data.models`，字段：

```
id / name / tooltip / contextWindow / maxTokens / reasoning
input[]（含 image 才算多模态）/ creditConsumptionLevel
```

目录是实时事实源，不在代码里写死模型表；在线失败时可以退「上一份真实目录」，
连历史目录都没有就返回空表，**不内置离线快照**——写死一份名单会让客户端照着
已经下线的名字发请求。某区没有可用账号时，该区目录也应为空，**不要**把另一区
的目录借过来，那会让用户以为模型在本区可用。

### 对话

- 国际：`POST {GLOBAL}/autoclaw-proxy/proxy/autoclaw/v1/chat/completions`
- 国内：`POST {CN_INFERENCE}/autoclaw-proxy/proxy/autoclaw[/v1]/chat/completions`

两版都返回 OpenAI 兼容响应（SSE / 非流式），模型经 `X-Request-Model` 头指定，
部分版本也要求 `body.model` 保留带前缀的目录名。流式默认补
`stream_options.include_usage=true`。

**两代客户端的请求体约束不同**，这是接入时最容易被混用的地方：

| | 国际 1.x | 国内 2.x（参考） |
| --- | --- | --- |
| `max_tokens` 族 | 可用 | 禁发（406） |
| system | 常规 | 需带 2.x banner |
| `X-Harness-Type` | 账号面需要 | 对话禁发（406） |
| 额外头 | `X-Request-Model` | `X-Session-Id` / `X-Agent-Id` |
| 鉴权头 | `Authorization` / `X-Authorization` | 只认 `X-Authorization` |

第一版应固定一个版本矩阵（建议：国际 1.18.x、国内 2.1.0），不要把两个参考源的
常量混在一起。请求体归一放在「选号之前」，与用哪个账号无关。

和本仓库其他平台一样，适配器只做协议翻译：**不改写 system / messages**
去绕上游风控，也不做 `force-model` 静默改写。上游 406 或 WAF 拦截要原样
暴露成可解释的错误。

---

## 6. 积分、签到与任务

| 能力 | 端点 | 备注 |
| --- | --- | --- |
| 总积分 | `GET /agent-assetmgr/api/v2/wallets?biz_app_id=autoclaw` | `data.total_balance`，兼容 `wallets[]` |
| 分类余额 | `GET /agent-assetmgr/api/v1/wallet-instances?wallet_type=all&wallet_scope=all` | daily / reward / subscription … |
| 逐条账单 | `GET /agent-assetmgr/api/v1/ledgers_std` | 可按模型 + token + 时间窗回填单请求实扣 |
| 临期积分 | `GET /agent-assetmgr/api/v1/points/expiring` | 展示用 |
| 签到 | `POST /autoclaw-proxy/proxy/autoclaw-task-complete` `{"task_id":"daily_signin"}`（hubo 另带 `source_id`，是否必填待实测） | 幂等，`already_completed=true` |
| 任务列表 | `GET /autoclaw-proxy/proxy/autoclaw-task-list` | 判断今日是否已签 |
| 登录奖励 | `POST /userapi/v1/user-info` | 客户端启动那次登录会触发入账 |
| 新人 / 活动 | `/autoclaw-proxy/proxy/autoclaw-newbie-guide/token`、`/autoclaw-promotion-reward` | 只做明确可领的，不代领邀请类 |

**签到是否国内也有，目前没有定论**：三份实现说两区同路径同形状，两份实测说
国内没有签到 / 灵感中心 / 新人任务（404），积分靠活动发放。倾向「国际有、
国内没有」，但必须先用真实国内账号确认，再决定
`supports_checkin` 怎么按区域暴露。第一版可以按区域开关，国内默认关闭。

积分消耗以账单为准，不在请求结束时就估算；延迟归账的补齐逻辑可与 Loomy /
灵犀的 deferred credit 共用同一套后台探针。

---

## 7. 调度与错误

- 选号、权重、会话亲和、账号级代理：直接复用现有 `_NATIVE_POOL` 设置集。
- `410000` / `400000` → 401，账号标记失效 / 需重登。
- `402` → 长冷却；`810000` 是**单模型额度耗尽**，只冷却「账号 + 模型」，
  不冻整号。
- `429` → 短冷却，按 `Retry-After`。
- 5xx / 超时 / DNS / 代理断开 → `transient`，换下一个账号，**不冻号**。
- WAF 表现不统一：`{"message":"forbidden"}` 前缀、406、以及把
  `DANGEROUS_ACTIVITY_BLOCKED` 当正常正文返回（HTTP 200 / finish_reason=stop）。
  最后一种不是协议错误，也不能靠换号绕过；按上游内容判定处理，不自动重试。

---

## 8. 接入前必须确认的 5 件事

1. **国内 chat 路径带不带 `/v1`**，以及国内是否必须用 2.x 请求体。
2. **国内有没有 `daily_signin`**（决定 `supports_checkin`）。
3. **模型名放 header 还是 body**，两处都写会不会 400。
4. **鉴权头**：`Authorization` 与 `X-Authorization` 是否都需要。
5. **国际 OAuth 的端口池**与滑块引导页能否在服务器 + 远程浏览器场景下工作；
   如果不行，第一版只做「粘回调」。

这五件事没有实测结论之前，不建议直接写适配器主体。阶段 2 已经先把
`server/platforms/autoclaw/` 里的常量、签名、路径、目录归一与错误分级做成
不联网的纯函数（`constants.py` / `protocol.py` / `catalog.py`），并加了离线
单测；阶段 3 用真实账号逐条确认后，先把结论回写
[`deploy/autoclaw-ref.txt`](../deploy/autoclaw-ref.txt)，再改实现。

**接入完成后的现状**（第一版）：第 1 条按「首选路径 + 回退路径」处理
（`protocol.chat_paths()`），第 2 条按「先读任务列表、没有就如实返回没有」处理，
第 3 条模型名放 body（与参考实现一致，header 不重复写），第 4 条两种鉴权头都
支持，第 5 条只做「打开授权页 + 粘回调」。以上四条都不是赌一个答案，联调后只
需要收敛顺序，不需要改策略层。

---

## 9. 分阶段计划（拟）

| 阶段 | 状态 | 内容 | 验收 |
| --- | --- | --- | --- |
| 1 | 已完成 | `deploy/autoclaw-ref.txt` + 本文 | 事实有来源、分歧有标注 |
| 2 | 已完成 | 常量 / 签名 / 目录解析 / 错误分级（纯函数） | 离线单测通过 |
| 3 | 已完成 | 国内短信登录 + 账号存储 + 刷新 | 离线单测通过；真实国内号待联调 |
| 4 | 已完成 | 适配器注册、前缀路由、模型页、`/v1/models` | 网关三条入口都能走 `autoclawcn/` |
| 5 | 已完成（粘回调） | 国际 OAuth（先粘回调，后内嵌滑块） | 离线单测通过；真实国际号待联调 |
| 6 | 已完成 | 积分 / 签到 / 任务面板 | 钱包与签到幂等（离线单测） |

阶段 2~6 的代码：

| 文件 | 内容 |
| --- | --- |
| `server/platforms/autoclaw/constants.py` | 双区三套基址、版本矩阵、路径、业务码、超时 |
| `server/platforms/autoclaw/protocol.py` | 区域归一、URL、`X-Auth-Sign`、鉴权头、JWT、错误分级 |
| `server/platforms/autoclaw/catalog.py` | 模型目录归一（不内置离线快照） |
| `server/platforms/autoclaw/store.py` | 账号存储、uid 派生、设备号、公开视图 |
| `server/platforms/autoclaw/client.py` | 登录 / 刷新 / 目录 / 对话 / 积分 / 任务 / OAuth |
| `server/platforms/autoclaw/login.py` | 国内短信会话机 + 国际 OAuth 粘回调 |
| `server/platforms/autoclaw/adapter.py` | 双区选号、冷却分级、签到、积分、对话出口 |
| `server/tests/test_platform_autoclaw_adapter.py` | 协议 + 适配器单测（不联网） |

验证命令：

```
python -m unittest server.tests.test_platform_autoclaw_adapter -v
```

---

## 10. 边界

- 只管理**本人及亲属自愿授权**的账号；不做批量注册、账号交易、接码、
  盗号、绕过滑块或风控。
- 不引入参考仓的沙箱 relay / WebSocket 云设备唤醒：它要求先上线设备，
  与本仓库「服务器直连」的形态冲突。
- 参考源中**无 LICENSE** 的仓库只用于核对协议事实，不复制代码；
  AGPL 项目只做行为对照。
- 与智谱 / Z.ai / AutoClaw 无隶属关系。
