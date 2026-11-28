# Accio 接入（阿里国际站 AI 电商工作台）

> **状态：已接入。** `server/platforms/accio/` 已包含 `constants.py` /
> `protocol.py` / `catalog.py` / `store.py` / `client.py` / `chat.py` /
> `login.py` / `adapter.py` / `__init__.py`，并已注册到平台列表（前缀
> `acciocn/` / `acciocom/`，旧式 `accio/` 兼容）。用户向的完整说明见
> [`docs/multi-platform.md`](multi-platform.md) §19；本文保留接入前的协议事实、
> 分歧记录与「有意不做」。
>
> 协议事实来自 2026-11 对网页端 / 桌面端请求的核对，参考源是 `dsh-providers-hub`
> （MIT，DSH 插件包，`0.2.1-rc.5`）。本仓库为**原生重写**，没有 vendor 目录、
> 不合并参考源。来源与「未实测」标注见
> [`deploy/accio-ref.txt`](../deploy/accio-ref.txt)。

Accio 是阿里国际站的 AI 电商工作台（不是通用编程助手），走 OAuth 2.0 + PKCE
登录，对话协议是 Gemini 风格信封。它与本仓库已有的 Trae / Qoder / MiniMax Code /
AutoClaw 属于同一类接入对象：**没有开放平台 API，协议层由面板原生实现**。

---

## 1. 双区对照

| | 国内版 `cn` | 国际版 `global` |
| --- | --- | --- |
| 模型前缀 | `acciocn/` | `acciocom/` |
| 登录页 | `https://www.accio-ai.com/login` | `https://zh.accio.com/login` |
| 网关 | `https://phoenix-gw.alibaba.com` | 同左 |
| `x-package-region` | `CN` | `GLOBAL` |
| token | 两区通用 | 同左 |

**两个区域只差登录页域名**，网关、推理面、token 都是同一套；区域由服务端按
账号下发（`userinfo.routeRegion`），所以**登录前无法自动判断区域**，必须让用户
先选。旧式 `accio/` 前缀继续可用，实际区域取自被选中账号记录的 `realm`，不按
token 猜。

---

## 2. 凭据

| 字段 | 说明 |
| --- | --- |
| `accessToken` | 上游不透明 32 位串（**不是 JWT**），过期时刻只能来自 `expiresAt` |
| `refreshToken` | 续期用；缺失时手工导入的 access token 仍可用一次，401 再换号 |
| `expiredAt` | 落盘为 ISO；上游 `expiresAt` 是**秒**，使用时归一成毫秒 |
| `realm` | `cn` / `global`，只决定登录页 |
| `utdid` | 设备指纹，首次生成后持久化，只在请求体 `properties.utdid` 被校验 |
| `sgcookie` | 兑换响应偶尔下发，缺了不影响请求 |
| `teamId` / `routeRegion` | 登录后 `userinfo` 回填，分别写进 `x-team-id` / `x-accio-route-region` |

- uid 由「区域 + 用户标识」派生（`ao_<sha256 前缀 24>`），同一账号重登覆盖同一
  条记录。**不能用 token 哈希**：refresh 每次刷新都会换新。
- 导入接受 `access_token` / `accessToken`、`Bearer ...`、裸 token，以及整段
  Cookie（自动取出 `ac_token`）。
- 明文只落盘：`data/platforms/accio/accounts/<uid>.json`，文件锁 + 临时文件 +
  `os.replace` 原子替换，权限 0600。接口只回摘要与 `has_token` /
  `has_refresh_token`。

### 2.1 `expiresAt` 量纲（最大的坑）

上游给的是**秒**（如 `1767225600`）。如果原样当毫秒用，`new Date(1767225600)`
是 **1970 年**，于是每个请求都判「已过期」→ 每次都触发续期 → 并发下反复消费
refresh → `invalid_grant` → 账号被标死。这与 CodeArts 的「续期单向门」事故同型，
且更隐蔽（错的是时间量纲）。

归一只做一次：`protocol.expires_at_ms()`，`> 1e12` 视为调用方已经给了毫秒，
否则 ×1000。落盘写 ISO，读取必须用 `store.epoch_from_iso()`。

---

## 3. 登录（OAuth 2.0 + PKCE）

1. 面板先让用户选国内版 / 国际版，再调 `login_start(realm)`；
2. 后端生成：
   - `verifier` = 48 随机字节的 base64url（无 padding）；
   - `challenge` = base64url(SHA-256(verifier))；
   - `state` = 32 字节 hex；
   - `ttid` = `ttid_` + 32 hex；
3. 在浏览器打开授权 URL（参数：`return_url` / `state` / `code_challenge` /
   `code_challenge_method=S256` / `client_id=accio-work` / `ttid` /
   `language=zh`）；
4. `return_url` 用本机回环 `http://127.0.0.1:<端口>/auth/callback`——上游**没有**
   `return_url` 白名单，所以只能用本地回环（只绑 127.0.0.1）。回调里的 `state`
   必须与发起时**逐字相同**；
5. 兑换 `POST /api/oauth/token`，body
   `{code, codeVerifier, clientId, redirectUri}`；
6. 续期 `POST /api/auth/refresh_token`，body `{accessToken, refreshToken}`；
7. 登录成功后补一次 `userinfo` 拿 `routeRegion` / `activeTeam.teamId` /
   `userName`，失败不影响登录。`activeTeam` 为 null 表示个人空间，`teamId` 非空
   才写入账号与请求头。

`client_id='accio-work'` 是**公开客户端**，没有 `client_secret`。授权 URL 缺
`code_challenge` 会命中上游的 legacy 门禁，所以 PKCE 参数一个都不能少。

响应信封统一是 `{success, code, message, data}`：`success !== true` 即失败；
`accessToken` / `refreshToken` / `expiresAt` 三者缺一即无效。兑换响应偶尔带
`sgcookie`，顺手保存。

面板与浏览器不在同一台机器时，把地址栏里的整条回调链接粘回面板即可；后端只接受
回环主机名，拒绝外部域名。

---

## 4. 请求头与请求体

### 4.1 请求头

固定发：

```
Content-Type: application/json
Accept: application/json（流式时 text/event-stream）
utdid: <账号持久化值>
version: 0.34.4
appKey: OneChat
x-package-region: CN | GLOBAL
```

有值才发（空串一律不发）：

```
sgcookie: ...
x-team-id: <activeTeam.teamId>
x-accio-route-region: <userinfo.routeRegion>
```

### 4.2 对话请求体

`POST /api/adk/llm/generateContent?sg_k=<md5(requestId)>`

```json
{
  "model": "<modelCode>",
  "token": "<accessToken>",
  "empid": "", "tenant": "", "iai_tag": "",
  "request_id": "...", "user_request_id": "...",
  "message_id": "<必填且不重复>",
  "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
  "system_instruction": "sys（字符串，不是对象）",
  "tools": [{"name": "f", "description": "d", "parameters_json": "{\"type\":\"object\"}"}],
  "temperature": 0.7,
  "max_output_tokens": 1024,
  "stop_sequences": [],
  "incremental": true,
  "properties": {
    "utdid": "...", "version": "0.34.4",
    "appKey": "OneChat", "normalized_response": "true"
  }
}
```

三处硬约束：

1. **`token` 必须在 body**。放 `Authorization` 头会恒回 HTTP 200 + 流内 402；
2. `system_instruction` 是**字符串**，空 system 时整个键都不出现；
3. `tools` 是**扁平**形态且 schema 字符串化（`parameters_json`）；没有工具时
   不发 `tools` 键。

`properties` 子树**不参与**通用 camel → snake 转换，保持
`{utdid, version, appKey, normalized_response}` 这种混合键形——这是实测成功形状。

### 4.3 消息序列化

| OpenAI | Accio |
| --- | --- |
| system / developer | 拼进 `system_instruction`（多段用空行连接） |
| user（文本） | `{role:'user', parts:[{text}]}` |
| assistant（文本 + `tool_calls`） | `{role:'model', parts:[{text},{function_call:{id,name,args_json}}]}` |
| tool | `{role:'user', parts:[{function_response:{id,name,response_json}}]}` |

历史里工具调用缺参数串时补 `{}`：上游靠它重建「调用过什么」。**图片本轮不实装**，
遇到图片块抛 `UnsupportedContent`，由调用方转成 400，不静默丢弃。

---

## 5. 模型目录

`POST /api/llm/config/v2`，body `{token}`，响应 `data[].modelList[]`，带
`x-team-id` / `x-accio-route-region` / `x-package-region`。

- `visible !== true` 剔除；
- 对外 id 用展示名归一后的 slug：**先剥第一个 `·` 之后的内容**（含裸「免费」
  后缀），再做 slug 化；上行用上游 `modelCode`；
- `effectiveMultiple` 拼进展示名 `名称 · x倍率`：0 与 1 也拼，非有限值不拼；
  已带 `· x1.5` 后缀的先剥再拼；
- 目录按倍率升序，无倍率最后；
- `reasoningEfforts` 原样保留远端顺序；`null` 与 `[]` 都表达为「无档位」，但
  `efforts` 为 null 而 `defaultReasoningEffort` 有值时声明为单档；
  `defaultEffort` 只有落在 `efforts` 集合内才声明；
- `multimodal: false` 的模型逐条声明 `modalities.input = ['text']`（不能省略，
  否则调用方会以为能传图）；
- 目录**按区域分别缓存**（`platforms.accio.models.<realm>`），没有离线快照：
  本区拉不到就是空表，不借另一区的名单；
- 未知模型只在**本区真有过目录**时才本地拒绝（`_assert_known_model`），冷启动
  不拿空表误拒。

---

## 6. 对话流

上游是 SSE 的 Gemini 风格帧，`content.parts` 在顶层（不是 `candidates[]` 里的
增量）。`chat.py` 把它翻译成 OpenAI chunk。

- 判成功不能只看 HTTP 200：必须有 `content.parts` 且带 `text`，且没有
  `error_code`；`error_code === '200'` 是成功哨兵，必须先于错误判据返回；
- 流里没有 `[DONE]`，终帧即最后一行；终帧可能是 `{content:{role:"model"}}`
  （有 content 无 parts），不能当空流报错；
- 帧按**逐行**解析 `data:`：上游会把多帧拼成连续的 `data:` 行，不用空行分隔；
  解析失败才累积到空行再试；
- 工具调用分片到达：首帧只有 `{id, name}`，后续帧重复完整的 `args_json`。合并
  在 `chat.StreamTranslator`：只缓冲工具 part、按 `id` 分组、flush 排在当前行
  之前、非 `data:` 行原样透传；匿名 id 用 `anon-<n>`；
- 参数三形态：`args_json` 非空串优先；空串回退同帧 `args` 对象；再容忍
  `argsJson`；参数为空时省略 `args_json` 键；
- 纯 usage / finish 帧不丢弃；同帧的 `turn_complete` / `usage_metadata` /
  `finish_reason` 跟着合并，非 usage 后者覆盖，usage 取 `total` 最大者；
- 输出：正文 `delta.content`、思考 `delta.reasoning_content`、工具
  `delta.tool_calls[{index,id,type:'function',function:{name,arguments}}]`；
  非流式 `object='chat.completion'`，有工具时 `finish_reason='tool_calls'`；
- usage 映射：`promptTokenCount` → `prompt_tokens`、`candidatesTokenCount` →
  `completion_tokens`、`totalTokenCount` → `total_tokens`。

---

## 7. 积分（没有签到）

**Accio 没有「每日签到」端点。** 每日额度由服务端在登录时按日发放（24 小时不
累积），新用户另有一次性额度，都是服务端行为，没有可点的按钮。因此：

- `supports_checkin = False`，账号页不显示签到按钮；
- `checkin()` 明确抛 `checkin_unsupported`，不伪造签到。

余额是两段只读查询：

| 端点 | 用途 |
| --- | --- |
| `GET /api/entitlement/currentSubscription?accessToken=` | `entitlement.daily` / `monthly` / `referral` 三桶，各取 `remaining` / `total` / `used` |
| `GET /api/entitlement/quota?accessToken=` | `usagePercent` / `refreshCountdownSeconds`，只展示余额与重置倒计时 |

- 三桶顺序固定：每日 → 每月 → 推荐，标签「每日积分」/「每月积分」/「推荐积分」；
- 总额优先 `data.remaining`，缺席才逐桶求和；桶有对象但 `remaining` 读不到时
  跳过该桶，**不编造 0**；
- 只有每日桶知道周期何时结束（quota 倒计时），其余桶不臆造到期时刻；
- 单位是 `credit`；
- `userinfo` 必须 **POST + body `{accessToken}`**（GET 会 405）；额度端点是
  **GET + query**（缺 token 回 400）；额度信封判据 `success !== false`；
- 认证失败判据是 HTTP 401/403 或 body `code` / `message` 为 `'401'` /
  `NOT_LOGIN`；
- 团队额度（`/gateway/subscription/current`）只认网页 Cookie，token 打过去 401，
  如实记录为不支持。

---

## 8. 失败分级与调度

| 失败 | 处理 |
| --- | --- |
| `402` / `QUOTA_EXCEEDED` | 换号 + 冷却至 **UTC+8 当日 24:00** |
| `429` | 换号 + 冷却 **1 小时**（`retry_after` 优先） |
| `401` / `403` | 只换号，**绝不**写冷却 |
| `403 + text/html` | `PERMISSION_DENIED`：不换号、不写冷却 |
| `400` / `5xx` / 风控页 | 不换号，原样回给调用方 |

- `httpErrorCode` 映射：`401/403` → `AUTH`、`402` → `QUOTA_EXCEEDED`、
  `429` → `RATE_LIMIT`、`400` → `INVALID_REQUEST`、`>=500` → `SERVER`；
- 风控页是 HTTP 200 + `text/html`（含 `window._config_={"action":"deny"}`），
  **必须校验 `content-type`**，否则会被当成功响应解析；
- 单次请求最多试 8 个账号（`MAX_ACCOUNTS_TRY`）；
- 选号：SWRR + LRU + 会话亲和 + 单账号并发闸门；按区域过滤；
- 续期巡检提前 2 小时刷新，只按 access 到期判断（refresh 没有单独寿命），
  刷新成功**先落盘再使用**；只有终态错误才把账号标成「登录已失效」。

---

## 9. 有意不做

- **图片上行**：参考实现有图片通路，但本轮未实测。宁可明确报不支持，也不把
  图片悄悄丢掉（`multimodal:false` 的模型也逐条声明 `text`）。
- **团队额度**：只认网页 Cookie，token 打过去 401；不伪造数字。
- **浏览器自动化 / TLS 指纹伪造 / 内嵌滑块**：项目红线。
- **写死离线模型快照**、**跨区借用目录**、**改写 system prompt 绕风控**、
  **force-model 静默改写**：都会让用户看到一个上游并不支持的模型 / 请求。
- **参考实现的 HTTP 服务 / 账号池 / 面板 / 轮询调度**：选号、冷却、亲和全部按
  token-farm 口径重写（参考源是 TypeScript 插件，不是 Go）。

---

## 10. 文件一览

| 文件 | 职责 |
| --- | --- |
| `constants.py` | 区域、网关、登录页、路径、客户端指纹、超时、积分桶 |
| `protocol.py` | PKCE、URL、时间归一、消息 / 工具序列化、错误分级（纯函数） |
| `catalog.py` | 目录归一：slug、倍率、档位、模态 |
| `store.py` | 账号文件、设备指纹、冷却、脱敏 |
| `client.py` | HTTP 协议层（令牌 / 目录 / 额度 / 对话 / `iter_frames`） |
| `chat.py` | Gemini 帧 → OpenAI chunk、工具跨帧合并、非流式聚合 |
| `login.py` | PKCE + 回环回调登录会话 |
| `adapter.py` | 选号 / 续期 / 冷却 / 目录缓存 / 对话出口 |
| `__init__.py` | 注册 |
