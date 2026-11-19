# Loomy 接入（讯飞星火）

token-farm 的 **Loomy** 平台对接讯飞星火 Loomy 桌面客户端的两套上游：

| 用途 | 地址 |
| --- | --- |
| 账号服务（登录 / 用户信息） | `https://account.xfinfr.com` |
| 模型网关（OpenAI 兼容） | `https://loomyad.xunfei.cn/api/v1` |

协议照 [`loomy2api`](https://github.com/xingluoyuankong/loomy2api)（MIT）重写为
**原生实现**，代码在 `server/platforms/loomy/`。不需要额外部署进程、容器或端口；
登录用的 RSA 加密与账号服务签名都是纯标准库实现，没有新增 Python 依赖。

```
client ──loomy/<model>──> token-farm 网关
                             │ 鉴权 / 限流 / 记账（与其他平台共用）
                             └──> LoomyAdapter
                                    ├── 账号池（data/platforms/loomy/accounts/）
                                    ├── 手机号密码登录 / 微信扫码 / 401 自动重登
                                    └── 模型网关（SSE 原样透传）
```

Loomy 是**单区域**平台（讯飞星火国内站），没有国内版 / 国际版之分：账号与模型
一律记 `realm='cn'`，也不需要在界面上切换版本。

---

## 1. 数据存放

| 路径 | 内容 |
| --- | --- |
| `<WB_DATA_DIR>/platforms/loomy/accounts/<uid>.json` | 每个账号一个 JSON（**含明文 session 与密码**） |

账号 id 依次取：显式 `uid` → 上游 `userid` → 手机号 → 会话摘要
（`s_<sha256 前缀>`，只粘 session 时用它去重）。

账号文件里含明文密码，因为 **Loomy 没有 refresh token**：会话失效后唯一的续期
方式就是拿手机号 + 密码重新登录。管理端接口一律走 `public_account` 脱敏，只返回
`has_token` / `has_password` / `token_preview`（`abc123…7890`），不返回 session
或密码明文（手机号用于账号识别，仍会展示）。

> `expiredAt` 对外**固定为空**：登录响应里没有可靠的到期时间，本地按 14 天估算
> 一次只会造成假警报。凭证是否还有效，唯一权威是上游返回 401 / 403。

---

## 2. 账号管理

四条录入路径：**微信扫码**（网页登录）、**手机号 + 密码**（导入）、**只粘
session**（导入）、**从本机客户端一键导入**。平台下拉 → Loomy 后，「网页登录」
在账号列表上方，「导入」接受两种形状（单个对象或 `{"accounts": [...]}` 批量，
字段冗余写法也认）。

### 微信扫码（网页登录，推荐）

点「网页登录」→ 弹窗内嵌微信二维码 → 手机微信扫码并确认，账号自动加入。

这是 Loomy 客户端**唯一**的「跳转授权」链路（微信开放平台「网站应用」扫码）；
密码登录与短信登录都是客户端内部 API，不跳任何外部页面，也就没有别的网页
登录方式。链路：

1. 服务端拉一次授权页，从 HTML 里取 `uuid`；二维码图片是
   `https://open.weixin.qq.com/connect/qrcode/<uuid>`，直接内嵌；
2. 后台长轮询 `https://long.open.weixin.qq.com/connect/l/qrconnect?uuid=…`，
   `wx_errcode=405` 的响应里带 `wx_code`；404 表示已扫码待确认；
3. `code` → `POST /login/thirdAccount/bind/auth` → `bind=1` 时
   `POST /login/thirdAccount/bind/skip` 换 session → 落盘（`source=web`）。

几个要留意的点：

- **`bind=0`（该微信没绑手机号）会明确报错**：上游要求再走
  `bind/sendMsg` + `bind/checkCode` 绑一次手机号（本质是注册），面板的登录
  弹窗只有一个输入框，放不下这段二次验证。请改用「手机号 + 密码」导入，
  或先在官方客户端用微信登录完成绑定，再回来扫码。
- **二维码取不到时自动退化**：服务端连不上 `open.weixin.qq.com`（海外 / 容器
  网络）时，弹窗会提示「二维码获取失败」，此时点「打开授权页」用浏览器扫码，
  再把回调链接粘回来即可。扫码后浏览器会停在
  `https://loomy.xunfei.cn/oauth/wechat/callback?code=…` 的 **404 页面**——
  这是正常现象（官方客户端把授权页塞进 Electron 窗口、在 `will-redirect`
  里截住跳转），按 `Ctrl+L` 再 `Ctrl+C` 复制地址栏整条链接即可。
- **不能做本机回环回调**：微信只认登记过的域名 `loomy.xunfei.cn`，所以没法
  像 Trae 那样在 `127.0.0.1:<端口>` 起临时服务自动收回调，这也是主路径要用
  「服务端自己的二维码会话」的原因。
- **扫码登录的账号没有密码**：`has_password=false`，会话失效后「刷新」会提示
  只能重新扫码（与只粘 session 的账号同一处境）。
- 登录会话只存内存、4 分钟有效；进程重启后要重新扫码。

### 手机号 + 密码（推荐用于需要自动续期）

```json
{"phone": "13800000000", "password": "…"}
```

服务端直接完成登录：`getPuKey` 取 RSA 公钥 → 加密密码 → `byPwd` 换 session →
落盘。以后会话失效时，对话路径会用保存的密码自动重登一次；积分 / 签到
不自动重登（避免后台巡检式重复登录），点账号上的「刷新」即可换新会话。

### 只粘 session

```json
{"session": "<Bearer token>", "userid": "1234567890", "nickname": "小明"}
```

`userid` / `nickname` 缺省时会尝试用 `/userinfo/query/baseInfo` 补全，补不到也能导入。
这种账号**不能刷新**——没有密码就无从重新登录，只能重新导入。

### 从本机客户端一键导入

token-farm 与 Loomy 客户端装在**同一台机器**上时，账号页的「从本机导入」会
直接读取客户端已登录的会话：

```
C:\Users\Public\Loomy\<hash>\userData\auth-session.json      （Windows）
/Users/Shared/Loomy/<hash>/userData/auth-session.json        （macOS）
```

`<hash>` 是客户端按用户生成的目录名；装了多份 / 多个用户的会全部扫出来，逐个
导入，单个文件坏掉只跳过它。`LOOMY_CLIENT_ROOT` 环境变量可覆盖根目录（便携版
安装与测试用）。

这份文件里只有 `session` / `userid` / `phone` / `updatedAt`，**没有密码**，所以
导入后的账号和「只粘 session」同一处境：能用到会话失效为止。昵称先显示成
`Loomy <手机尾号>`（客户端文件里没有昵称，导入时也不额外请求上游，避免账号服务
慢时「一键导入」长时间转圈）。与 Trae 的桌面导入不同，这里**不写回**客户端文件
——Loomy 没有 refresh token，我们用密码重登拿到的是另一个会话，写回去只会把
客户端正在用的那份顶掉。

### 「刷新」按钮

Loomy 没有 refresh token，所以「刷新」的语义是**用保存的手机号 / 密码重新登录
换一个新会话**：

- 有密码：直接重登，成功即写回；
- 只有 session（含微信扫码进来的账号）：返回明确的「只能重新导入 / 重新扫码」，
  而不是假装成功。

后台令牌巡检（`services/renew.py`）**不包含 Loomy**：它只能靠密码重登，
按小时对一批账号重复登录既没有意义，也容易触发风控。闲置账号的会话在下次
对话请求遇到 401 时自动续期即可。

---

## 3. 模型与网关前缀

- 对外模型 id 一律带 `loomy/` 前缀，例如 `loomy/deepseek-v4.1-flash-1017`；
- 不写平台前缀仍走 WorkBuddy（与 Trae / Qoder 同一规则）；
- 目录来自上游 `/models`，失败时退回内置兜底（`constants.FALLBACK_MODELS`）；
- 非 `chat` 模型（文生图等）不出现在模型清单里——本仓库只有
  `/v1/chat/completions` 一条上游通路。

上游有两处模型名与官方 id 对不上，请求时必须翻回上游名，否则上游会**静默
回落到默认模型**（实测是 `spark-x2.6`）：

| 对外 id | 上游线 id |
| --- | --- |
| `deepseek-v4.1-flash` | `DeepSeek-V4.1-Flash` |
| `deepseek-v4.1-flash-1017` | `deepseek-v4-flash-1017` |

翻名表在 `constants.MODEL_ALIASES`。

### 输出上限

思考类模型在小 `max_tokens` 下会把预算全花在 reasoning 上、正文为空。官方客户端
因此**始终**按模型最大值下发；适配器沿用同一口径：目录里有该模型时，把
`max_tokens`（以及客户端显式传的 `max_completion_tokens`）抬到模型最大值。
目录里查不到的模型不动客户端给的值。

### 关闭思考

上游没有 `reasoning_effort` / `enable_thinking` 这类开关字段（传过去会被静默
忽略），只认正文里的 `/no_think` 前缀。所以当客户端**显式**要求不思考时：

- `reasoning_effort: "none"`，或
- `enable_thinking: false`，或
- `chat_template_kwargs.enable_thinking: false`

适配器会在首条 user 消息的正文前加 `/no_think `（多模态消息加在第一个文本
片段上），已经带前缀的不重复加。没传这些字段时请求原样透传，思考类模型的默认
行为不受影响。

### 流式

上游本身就是 OpenAI 兼容 SSE，适配器**逐字节透传**（`PlatformChat.raw_stream`），
同时在本地扫描帧里的 `usage`（`prompt_tokens` / `completion_tokens` /
`points_consumed`）与首个正文 delta，用来填日志里的 token、实扣积分与首字延迟。
`points_consumed` 会归一成网关认的 `usage.credit`。

---

## 4. 积分 / 签到 / 用量

- **积分**（账号页那一列）：`GET /points/records` 的 `availableBalance`，默认
  60 秒缓存；查不到就是错误，不会显示成 0。
- **签到**：`POST /points/first-login`，每天首次登录发奖，重复调用幂等返回
  `alreadyProcessed`。`earned_credit` 用签到前后的 `availableBalance` 差额计算，
  与「签到记录 / 积分收入」共用同一口径。
- **用量弹窗**：余额 + 最近流水（`points/records`，最多 3 页 × 100 条）。

注意 `points/records` 只认 **camelCase** 的 `pageNo` / `pageSize`；写成 snake_case
会被静默忽略（永远只回 20 条）。

---

## 5. 平台设置

`/api/platforms/loomy/settings`（界面：账号页 →「平台设置」）：

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `max_in_flight` | `0` | 单账号在途请求上限（0 = 不限） |
| `request_timeout` | `300` | 非流式请求读超时（秒） |
| `idle_timeout` | `60` | 流式连续无数据多久中止（秒） |
| `max_accounts_try` | `3` | 一次对话最多尝试几个账号 |
| `session_affinity` | 开 | 同一会话固定到同一账号（上游缓存按账号隔离） |
| `session_affinity_ttl` | `1800` | 会话亲和的滑动 TTL（秒） |
| `catalog_ttl` | `21600` | 模型目录缓存（秒） |
| `catalog_retry_ttl` | `600` | 目录拉取失败后多久不再尝试（秒） |
| `catalog_timeout` | `10` | 隐式拉取目录的超时（秒） |
| `credits_ttl` | `60` | 积分缓存（秒） |
| `credits_timeout` | `20` | 积分 / 签到查询超时（秒） |
| `cooldown_429` | `0` | 429 时冻结账号的秒数（0 = 不冻结，直接上抛；>0 冻结并换下一个账号） |
| `cooldown_5xx` | `0` | 5xx 时冻结账号的秒数（0 = 不冻结，直接上抛；>0 冻结并换号） |

账号级可改「权重」（SWRR 选号）与「停用 / 冷却」，与 Trae 同一套界面。

---

## 6. 出口代理

与其它平台共用账号出口规则（见 [`account-proxy-routes.md`](account-proxy-routes.md)）：

1. 账号绑定的线路（`proxy` 字段）；
2. 代理池；
3. 全局上游代理（`WB_HTTP_PROXY`）；
4. 直连。

导入时如果账号已经绑定线路，登录请求也从同一条出口走；代理池线路里的
`{account}` 会话名会随账号一起保存（`proxy_session`），改昵称不会换出口 IP。

微信扫码登录的**取码请求**发生在账号存在之前，只能走「全局上游代理
（`WB_HTTP_PROXY`）→ 直连」，与账号线路无关。

---

## 7. 已知限制与排障

- **没有 refresh token**：只有 session 的账号不能续期。想要自动续期，请用
  「手机号 + 密码」导入；微信扫码得到的账号同样没有密码，只能重新扫码。
- **业务码级登录失效**：上游可能 HTTP 200 但返回 `code=100002` 或 desc 含
  「登录已失效」（实测 `/models`、`/points/records`、`/chat/completions`
  都是这个形状，连流式请求也回 JSON 而不是 SSE）。适配器把它统一归成
  401 `login_expired`，**但不清空本地会话**：凭据是否还有效由下一次真实请求
  说话。对话路径会在 401 / 403 时用保存的密码自动重登一次；积分 / 签到
  不自动重登，只报「登录已失效」，点「刷新」即可。
- **对话 401 / 403**：自动用保存的密码换一次新会话并重试；没有密码则换下一个
  账号，全部失败后返回明确错误。
- **上游要求 `traceparent` 头**：缺失时上游会挂起不回包。适配器已按客户端格式
  生成（`00-<32hex>-<16hex>-01`），不需要手工配置。
- **模型被静默换掉**：如果客户端一直拿到 `spark-x2.6` 的回答，通常是模型名没
  经过别名翻译——请确认请求用的是 `loomy/<对外 id>`。
- **扫码登录只存在单进程内存**：`uvicorn --workers N > 1` 时，`login/start`
  落在哪个 worker、后续轮询打到哪个 worker 是不确定的，可能返回「登录会话
  不存在」；二维码会话与 Trae 的回调服务一样要求单进程部署。
- **签到收入为 0**：当天重复签到是幂等的（`already=true`），不是失败。

参考实现：[`xingluoyuankong/loomy2api`](https://github.com/xingluoyuankong/loomy2api)
（MIT）。本仓库只重写了与 token-farm 契约相关的部分，未引入其 HTTP 服务、面板
与账号池实现。已核对的参考提交（`eed1efa` → `cb27afd`）与取了 / 不取的能力
记在维护者文档 `deploy/loomy-ref.txt` 里（不在本发布包内）。
