# 参考仓待同步清单（workbuddy2api-panel）

> 维护者文档，不进发布包。同步原则与 `git am` 操作见
> [upstream-sync.md](upstream-sync.md)。

这份清单回答一个问题：参考仓
[`misakano7545/workbuddy2api-panel`](https://github.com/misakano7545/workbuddy2api-panel)
里还有哪些**对本项目有价值、但尚未同步**的能力。

结论按优先级分三档，每项都写清「能力 / 来源 / 本地现状 / 建议动作 / 成本与风险」。
清单本身不产生代码改动；真正移植时，一次同步对应一个提交，并在
`deploy/workbuddy-panel-ref.txt` 追加该提交号。

> **编号前缀**：本文的条目在正文里写成 `M-P0-1` 形式（M = misakano）；
> [upstream-sync-baseline-gaps.md](upstream-sync-baseline-gaps.md) 那份写成
> `G-P0-1` 形式（G = gaps）。两份文档原本都用裸 `P0-1`，同名条目指的不是同一件
> 事（例如两边的 `P2-8` 分别指「10105 会话自救」与「模型目录指纹监控」），
> 交叉引用时必须写明是哪一份。下文标题保留裸编号以便检索，**凡跨文档引用
> 一律带 `G-` / `M-` 前缀**。

成长任务与协议层的逐项对照，另外放在文末的**附录 A**、**附录 B**；主体清单只保留
优先级 P0/P1/P2，不重复展开附录里的行级细节。

## 基线

| 项 | 值 |
|---|---|
| 本地已核对到的参考提交 | `49fad282`（记录在 `deploy/workbuddy-panel-ref.txt`） |
| 本次分析的参考 HEAD | `29e4701f`（2026-11-07） |
| 基线之后的非 merge 提交 | 10 个 |
| 其中应跳过 | 3 个 AutoBot chore：`0afd8ae4`、`e466d543`、`29e4701f` |

`49fad282` 那次同步的成果是 1.1.0 的四项能力：`X-Request-Id`、Prometheus
`/api/metrics`、隐藏模型、通用 Webhook。基线之后的新提交，以及基线之前一直
没移植的较大能力，都列在下面。

> **2026-11 第一批已落地**（六项，见
> [upstream-sync-baseline-gaps.md](upstream-sync-baseline-gaps.md) 的「同步状态」表）：
> `safefile`、Go 侧入站体上限、入站读取超时 / 关停窗口、CST 日界、
> `keepalive_refresh_window`、Go 侧 hidden 过滤。本文受影响的只有两处——
> P0-1 里的「入站请求体上限」那一半已完成（连坐保护仍未做），M-P2-6 的慢上传
> 可见性已完成（在途名额释放回归仍未做）。

## 先看两套运行时的差异

本项目是 **Python 面板 + Go 上游网关** 双运行时；参考仓是**单二进制 Go 程序，
面板内嵌在其中**。因此同一项能力在两边可能落在完全不同的位置：

- 本地面板侧：`server/routers/*.py`、`server/services/*.py`、`web/app/**`；
- 本地网关侧：`upstream/internal/**`、`upstream/cmd/**`；
- 两边同名文件 110 个，但实现已经分叉（参考仓 489 个文件，本地 `upstream/`
  274 个），**补丁不能盲目整段 `git am`**。移植时按能力在本地对应文件重写，
  保留本项目已有的注释、错误语义与测试风格。

`upstream-sync.md` 的原则在这里同样适用：只取协议无关、与本地运行时兼容的部分；
依赖参考仓特有运行时（内嵌面板、uTLS、SSE 续写状态机、媒体管线）的改动，
先评估再决定是否重写，不要为了「同步完整」把两套运行时耦在一起。

## 已覆盖，不必再同步

| 能力 | 本地落点 |
|---|---|
| 每次请求的可追踪 ID（`X-Request-Id`） | `server/services/requestctx.py` + 日志页 |
| Prometheus 指标 `/api/metrics`、`/metrics` | `server/routers/metrics.py`、`server/services/metrics.py` |
| 全局隐藏模型（裸名 / `cn:` / `global:`） | 面板 `server/services/modelpolicy.py`；**Go 侧已同步**（G-P1-16，含列表过滤与点名 404） |
| 通用 Webhook（HMAC 签名、多通道互不影响） | `server/services/watchdog.py` |
| 入站请求体上限 | Python 入口 `WB_GATEWAY_MAX_BODY_MB`（默认 32 MB，流式读、超限 413）；**Go 侧已同步**（G-P0-2，`server.max_payload_bytes` 默认 64 MiB） |
| 签名请求数 / Token / 积分 / 首字延迟等请求日志 | `server/routers/logs.py`、`web/app/(main)/logs` |
| 账号备注、积分倒计时、临时停用、分组 | 面板账号页与 `server/routers/accounts.py` |
| PostgreSQL 备份、一键更新 | 面板设置页与 `server/services/pgsync.py`、`autoupdate.py` |
| 11140 的识别（中文限制文案） | `upstream/internal/upstream/client.go` 的 `IsUpstreamRestriction` |
| 11102 模型误标复查 / 6004 恢复 | `server/services/modelrecovery.py` |
| 等价模型降级 | `server/services` 与各协议路由既有分支 |

> **两处已补齐**：请求体上限与全局隐藏模型这两项，原先只有 Python 侧，
> Go 上游的 `chatCompletions` 是 `io.ReadAll(r.Body)` 无上限、`/v1/models`
> 直连透传不过滤。2026-11 的第一批同步已按 G-P0-2 / G-P1-16 补上 Go 侧，
> 两处都**不再是部分覆盖**。
>
> 另一处需要更正的前提：初版这里写过「只要上游端口不直接暴露公网，现状可接受」。
> 这个前提**不成立**——`upstream/docker-compose.yml` 里是 `ports: - "7863:7863"`，
> Go 端口是**发布到宿主机**的。判断任何「只影响直连上游」的项，都按「已经直连」
> 评估，不要再用这条假设降级优先级。

> 这张表只覆盖「**Python 面板侧已有**」的能力，**不代表 Go 上游侧也已有**。
> 上表里原本至少有 5 行属于部分覆盖或口径不同（请求体上限、等价模型降级、
> Prometheus 指标、隐藏模型、11140 识别），其中请求体上限与隐藏模型已在第一批
> 同步里补齐 Go 侧（G-P0-2、G-P1-16），**剩下三项仍未覆盖**。另有若干基线之前
> 就缺的能力根本没进这张表。逐项更正与完整缺口见
> [upstream-sync-baseline-gaps.md](upstream-sync-baseline-gaps.md) 的
> 「对另一份清单「已覆盖」表的更正」一节；那份文档按「基线之前从未移植的能力」
> 组织，与本文互补。

---

## P0：建议优先移植

### P0-1　11140 风控连坐保护（命中即停手 + 全池暂停）

**能力**

上游把「账号存在异常行为，已被限制使用」（403 / code 11140）判在**设备与出口**
上，不是判在单个账号上。当前本地网关命中 11140 后只 `Pool.Disable` 这一个号，
然后 `fail()` + `rotateBackoff()` 继续换下一个号处理**同一个请求**；一个请求
最多轮转 `MaxRotate`（默认 3）个号，反复请求就能把整池号依次送进风控。

参考仓补了两道闸：

1. **命中即停手**：11140 不再换号，直接把拒绝返回客户端；
2. **全池暂停**：同一 15 分钟窗口内有 ≥2 个**不同账号**被限制，判定为连坐扫荡，
   暂停派发 `restriction_hold_seconds`（默认 1800s）；期间请求快速失败
   `503` + `Retry-After`，不再拿账号去试。

**来源**

- `b494426a` `feat(server): 吸收 hub 的两处加固——入站请求体上限、11140 风控连坐保护`
- 参考文件：`internal/server/restrict.go`、`internal/upstream/restrict.go`、
  `internal/server/handler.go`、`internal/upstream/client.go`、
  `cmd/server/config.go`（`upstream.restriction_hold_seconds`）

**本地现状**

- 已有识别：`upstream/internal/upstream/client.go` 的 `IsUpstreamRestriction`
  与 `upstream/internal/server/restriction_cooldown_test.go`；
- `handler.go:1392` 命中后 `Pool.Disable(uid, …)` 再 `return` 出该分支，但外层
  轮转循环继续（`handler.go:1072` 之后仍走 `rotateBackoff`）；
- 没有 `restrict.go`、没有全池暂停、没有 `account_restricted_hold` 错误码。

> 同一个提交 `b494426a` 里的**另一半**（入站请求体上限）已在第一批同步中完成
> （G-P0-2）。本项是剩下的一半，仍需独立做——两者没有代码依赖，只是同源提交。

**建议动作（只改 Go 上游）**

1. 新增 `upstream/internal/server/restrict.go`：`restrictionGate` 状态机，
   `note(uid, hold)` / `remaining()` / `reset()`，窗口 15 分钟、阈值 2 个不同账号；
2. `applyErrorPolicy` 在 `IsUpstreamRestriction` 分支返回「停止轮转」信号，
   主循环据此 `break`，并把客户端响应固定为风控语义；
3. 请求入口（chat 主链路，其它端点不必挂）先查 `remaining()`，命中则回
   `503`、`Retry-After: <秒>`、错误码 `account_restricted_hold`；
4. 配置加 `upstream.restriction_hold_seconds`，默认 1800；`0` 表示只保留
   「命中即停手」，关闭全池暂停；
5. 把 `restriction_test.go` / `restrict_test.go` 的用例按本地风格重写。

**成本与风险**

- 成本：中低。状态在进程内、重启清零，无需 DB；
- 风险：阈值与窗口不能太激进。单号偶发 11140 仍应归账号自己的禁用，只有
  「窗口内多个不同账号」才暂停全池，否则一次误判会误伤整池；
- 作用域只挂 chat：11140 只从 chat 返回，其他端点流量与风险都小得多。

---

## P1：建议同步，收益明确、成本可控

### P1-1　覆盖已有凭据前先验证新凭据「还活着」

**能力**

导入账号时，如果池里已有同 uid 的账号，参考仓在 `SaveAtomic` 之前先用
`VerifySessionByRefresh` 做三态判定：

- `alive`（refresh 成功）→ 允许覆盖，落盘的是刚轮换出来的新 refresh token；
- `dead`（上游明确拒绝）→ 保留原凭据，拒绝覆盖；
- `inconclusive`（网络 5xx / 429 / 没判词）→ 同样保留原凭据。

起因是把一份旧导出重新导入时，文件里的 refresh token 早已被轮换；直接覆盖会把
本来能用的账号变成要重新登录，而 `.bak` 不会自动回退。

**来源**

- `e36ad073` `fix(panel): 覆盖已有凭据前先求证新凭据还活着`
- 参考文件：`internal/panel/import.go`、`internal/panel/import_verify_test.go`

**本地现状**

- 导入入口在 `server/routers/accounts.py`（`allow_overwrite=overwrite`，
  约 195–240 行），直接调 `tencent.write_auth_file(...)`；
- 覆盖路径没有「先验证新凭据」这一步；
- 已有可复用的 `tencent.refresh_token(auth)`（`server/services/tencent.py:535`），
  能返回新的 access / refresh / 过期时间，也可作为存活判定。

**建议动作（Python 面板侧）**

1. 在 `write_auth_file` 之前判断：目标 uid 是否已存在且本次是覆盖；
2. 是则对**待写入的新凭据**调用一次 `refresh_token`：
   - 成功：把返回的新 token 合并进待写入数据，再落盘；
   - 明确失败（12153 / 401 / 上游拒绝）：标记 rejected，保留原文件；
   - 网络错误 / 5xx / 429：保守保留原文件，并在返回结果里说明「未判定」；
3. 新账号（池里没有同 uid）不走额外请求，避免导入大批新号时放大上游调用；
4. 补单测：三态各一条，覆盖「覆盖成功」「明确失效拒绝」「无法判定保留」。

**成本与风险**

- 成本：低。复用现有 refresh 函数，改动集中在一个导入分支；
- 风险：refresh 会轮换 refresh token，必须保证「验证成功后写入的是新值」，
  否则会把账号的 RT 弄成已作废的旧值；
- 不要在导入大量新号时逐条验证（只对覆盖路径验证）。

### P1-2　盲盒消费（`buddy/quota` + `buddy/open`）

**能力**

任务奖励会发「能量」，但此前只解析、只展示，一个都花不掉。参考仓补了：

- `POST /activity/growth/buddy/quota` 现查可开次数；
- `POST /activity/growth/buddy/open` 连续开盲盒，单轮封顶 5 个；
- 挂在「猫猫旅行」巡检同一条腿上，**只有拥有猫的账号**才调用；
- 配额每次现查，避免「今天发的能量还没到账就判能量不足」的时序坑；
- 与派出 / 领奖互不影响。

**来源**

- `4fb7cb42` `feat(scheduler): 补上盲盒消费与 10105 活跃会话自救`
- 参考文件：`internal/upstream/travel.go`、`internal/scheduler/travel.go`

**本地现状**

- `upstream/internal/upstream/travel.go` 只有 status / depart / claim / info /
  first / agreement，没有 `buddy/quota`、`buddy/open`；
- `upstream/internal/scheduler/travel.go` 没有盲盒消费；
- Python 的 `taskrun.py` 只调用 `scripts/task_runner.py`，该脚本也没有盲盒。

**建议动作（Go 上游侧）**

1. 在 `internal/upstream/travel.go` 增加 `BuddyQuota`、`BuddyOpen` 两个客户端方法
   （沿用现有 `chatBase` 与请求头口径）；
2. 在 `internal/scheduler/travel.go` 的旅行巡检里，对「有猫」的账号先查配额，
   再按 `affordable` 连续开，单轮上限 5；
3. 失败只记任务日志、不影响派出 / 领奖；每轮配额现查；
4. 补测试：无可开次数、单次成功、达到上限停止、quota 接口失败不阻断旅行。

**成本与风险**

- 成本：低。接口与现有 travel 同域，复用鉴权与请求封装；
- 风险：盲盒接口属于成长域写操作，频率必须低（跟随旅行巡检即可），
  不要做成独立高频任务。

### P1-3　截断自动续写（长输出场景）

**能力**

上游对单次响应有硬上限（例如 `deepseek-v4.1-flash` 约 32000 输出 token，
请求显式要 50000 也会被夹回）。Codex 这类客户端不带输出限额，写大文件 / 长分析
时会在 32000 处被截断，然后客户端重连、几乎从头生成。

参考仓在帧级把截断点接上：某段以 `finish_reason=length` 结束、且本段是纯文本 /
推理输出、客户端未显式带输出限额时，用同一账号同一模型补发
「已输出内容 + 续写指令」的请求，把后续帧接进同一条 SSE；终端帧吞掉、usage
跨段合并、客户端只看到一次连续输出。每请求最多 3 段。工具参数被截断不续。

**来源**

- `internal/server/continue.go`（基线之前就已有，尚未移植）
- 相关修复：`fa5d6148`、`f0ca4d3d`

**本地现状**

- Python `server/routers/responses.py` 对 `finish_reason=length` 只做提示 /
  不执行工具，没有自动续写；
- Go 上游 chat 路径也没有续写状态机；
- 本地已有 SSE 解析、usage 合并、工具配对等基础设施可复用。

**建议动作**

- 先评估目标客户端：如果主要用户是 Codex / 长文写作，收益明显；如果只是
  普通对话，优先级可降；
- 实现放在 Go 上游 chat 流式路径（与参考仓一致），不要在 Python 网关重复做，
  避免 usage 与终止帧在两处各算一次；
- 只续纯文本 / 推理段；工具参数截断只记日志；
- 必须有 `maxContinueSegments` 封顶与客户端显式 `max_tokens` 时尊重不续。

**成本与风险**

- 成本：中高。要处理帧级拼接、续写段静默保活、usage 合并、断连；
- 风险：续写会放大单请求的输出与积分消耗，必须封顶；续写措辞与上游行为
  需要真实环境验证，不能只靠单测。

### P1-4　`/v2/chat/busy` 模型忙闲预检

**能力**

官方客户端每次对话前会调 `POST /v2/chat/busy`，body 形如
`{"models":["deepseek-v4.1-flash", ...]}`，上游回每个模型的
`busy_level: idle | busy | unknown`，客户端据此给模型选择器打「繁忙」标记。

参考仓**本地按池状态算**，不打上游：模型在本域所有可用账号上都处于模型级冷却
→ `busy`；还有账号能服务 → `idle`；模型不在服务目录 / 请求为空 → `unknown`。
这样国区 / 国际版行为一致，也不额外消耗额度。

**来源**

- `internal/server/busy.go`（基线之前）
- 路由：`POST /v2/chat/busy`

**本地现状**

- 本地对外路由只有 `/v2/chat/completions`，没有 `/v2/chat/busy`；
- 本地 Go 有**模型级冷却台账**（`pool.CooldownSoftForModel` 写的
  `modelCooldowns`），但**没有**参考仓那种「按池状态算 busy」的聚合判定
  （`ModelBlocked` / `ModelBlockedForRealm`，`internal/pool/cooldown.go:456`）——
  要新写一层，不是直接调现成函数。

**建议动作（Go 上游侧）**

1. 在 `upstream/internal/server` 增加 `chatBusy` handler，复用本地模型冷却判定；
2. 注册 `POST /v2/chat/busy`（鉴权口径与 chat 一致）；
3. 未知模型回 `unknown`，不要回 `idle`；
4. 补测试：idle / busy / unknown 三态与空请求。

**成本与风险**

- 成本：低；
- 风险：几乎没有网络副作用；主要是 keep 住与官方一致的响应形状
  （`code/msg/data/requestId`）。如果本项目不打算兼容官方客户端，可降到 P2。

---

## P2：可选 / 需要先评估

### P2-1　媒体接口：生图 / 改图 / 视频 / 去水印

**来源**：`internal/server/images.go`、`videos.go`、`watermark.go`、
`media_fetch.go`；相关提交 `6df19e55`、`81b806c9`、`dab1b08c`、`49fad282`。

**能力**：`/v1/images/generations`、`/v1/images/edits`、`/v1/videos` 系列、
`/v1/images/strip-watermark`，以及 Responses 里托管的 `image_generation` 工具。

**本地现状**：Python 网关对未知 `/v1` 路径返回 JSON 404，不会透传；本地没有
媒体接口，也没有托管 `image_generation`。

**建议**：先确认上游是否真的支持媒体产物协议、积分闸与产物直链回读。参考仓的
实现依赖它自己的媒体管线与产物域校验，不能整段搬。若要做，优先只做
「`/v1/images/generations` + 积分闸 + 产物直链回读」，视频与去水印后置。

**成本/风险**：高。涉及 multipart、异步任务台账、产物域白名单、积分闸、
重启收养、直链回读，任何一处不严都会变成越权或重复计费。

### P2-2　MCP、Embeddings、`/v1/search`、`/v1/fetch`

**来源**：`internal/server/mcp.go`、`embeddings.go`、`search.go`、`fetch.go`；
`29d98794`（`/v1/search` 把上游卡片改写为等价检索链接，仅 DSH）。

**本地现状**：全部没有；未知 `/v1` 路径 404。

**建议**：

- Embeddings：如果上游没有对应能力，单独实现意义不大；
- `/v1/search` / `/v1/fetch`：属联网检索工具，依赖上游检索运行时；若将来新增
  search 端点，再移植 `29d98794` 的「卡片改写为等价检索链接」；
- MCP：参考仓是网关侧 MCP server，价值取决于是否要把工具统一暴露给下游，
  与当前「OpenAI 兼容反代」定位不同，暂不移植。

**成本/风险**：高，且与本地协议边界不一致。除非明确要做「一站式工具网关」，
否则不建议为同步而同步。

### P2-3　全局模型白名单 `models.allowed`

**来源**：`internal/server/allowed_models_test.go` 等（基线之前）。参考仓的白名单
由 `bd6f6cbb` 引入，与 hidden 取交集；`5ce1a2f4` 只是补全了 hidden 的生效面
（见 G-P1-16）。

**本地现状**：只有**全局隐藏名单**（`server/services/modelpolicy.py`，Go 侧已同步
见 G-P1-16）与**每把密钥的模型白名单**（`server/routers/keys.py` 的 `models`），
没有全局 allowed 白名单。

**建议**：先判断需求。隐藏名单表达「藏掉这一个」，全局白名单表达「只允许这些」；
两者混用会让排查变复杂。如果确有「整个实例只开放几个模型」的需求，再加一个
独立的全局 allowed，不要和 per-key 白名单耦合。Go 侧若要补，可复用 G-P1-16 的
`modelhidden.go` 匹配逻辑（同一套 realm 前缀规则），配置仍走 `config.json` +
面板镜像，不要再开第二套配置来源。

**成本/风险**：低到中；主要是语义与优先级要写清（allowed 与 hidden、
per-key models 三者如何叠加）。

### P2-4　池健康告警

**来源**：参考仓面板的池状态 / 告警能力（基线之前）。

**本地现状**：`server/services/watchdog.py` 主要监控页面改版与补偿领取，
并通过 Webhook / 邮件 / QQ 发告警；**没有把「健康账号数跌破阈值 / 全池不可服务」
作为独立告警项**。

**建议**：在现有 watchdog 里加一条池健康检查（例如 `healthy == 0` 或低于阈值
持续 N 分钟），复用现成 Webhook 通道。成本低，但要注意别和页面改版告警互相刷屏。

> 同一处还要加「上游模型目录指纹监控」（参考仓 `WatchCatalog`），见
> `upstream-sync-baseline-gaps.md` 的 **G-P2-8**；两者都建议共用同一套告警抑制
> 逻辑，且都**写进 Python watchdog**，不在 Go 里加 goroutine。

**成本/风险**：低。

### P2-5　余额趋势、错误汇总、模型锁池视图

**来源**：参考仓内嵌面板（`internal/panel/*`，如 `f6335ce8` 余额趋势筛选器）。

**本地现状**：

- 数据侧已有基础：`server/services/credits.py` 会在每次真实查询后记录余额增加
  流水（`record_balance`），`tasklog.py` 也有变动流水；
- Web 侧没有余额趋势图、错误汇总页、模型锁池 / 实时繁忙视图。

**建议**：这些是**产品功能**，不是可直接移植的上游补丁。参考仓的面板是内嵌
Go/JS，本项目是 Next.js，必须自研。若要做，先做「余额趋势（按账号 / 时间筛选）」
和「错误汇总（按模型 / 错误码 / 时间段）」，两者数据都已具备。

**成本/风险**：中；主要是前端与聚合查询，不涉及协议。

### P2-6　慢上传可见性 + 在途名额释放回归

**来源**：`c6f953a7`；参考文件 `internal/server/bodylimit.go` 与
`slotlease_test.go`。

**能力**：读请求体超过 3s 打一条 WARN（方法、路径、字节数、耗时、声明的
Content-Length），不记 body；以及一组回归测试，钉住成功 / 各类错误路径下
在途名额都会释放。

**本地现状**：

- **慢上传可见性已随 G-P0-2 同步**：`upstream/internal/server/bodylimit.go` 的
  `readBody` 在读体耗时超过 3 秒时打 WARN（方法、路径、字节数、耗时、声明的
  `Content-Length`），不记 body；
- Python 网关有在途 limiter（`server/platforms/limiter.py`），读体慢上传仍没有
  专门日志——若线上出现「请求没到 / 上传慢」难排查，可在 Python 侧补一条同口径
  的 WARN；
- Go 上游的在途名额由 pool 管理，释放路径已有 defer + 显式 fail 两处兜底。

**建议**：Go 侧那一半**已完成**（G-P0-2）。剩下的只有两件可选的收尾：
Python 侧慢读日志（按需），以及在途名额释放的回归测试（当前结构下泄漏风险
较低，可只补测试，不必搬实现）。

**成本/风险**：低。

### P2-7　出站 TLS 指纹与请求头顺序对齐

**来源**：参考仓 `internal/upstream/tlsfingerprint.go`、`headorder.go`、
`headorder_conn.go`、`headcase.go` 等（基线之前）。

**本地现状**：

- Python 管理端直连腾讯的请求头已与上游参照实现逐条对齐
  （`server/tests/test_outbound_alignment.py`）；
- Go 上游没有 TLS 指纹 / 头顺序控制。

**建议**：高成本、高风险。uTLS 指纹与连接级头顺序需要改 transport /
连接劫持，收益是「更像官方客户端」、降低风控概率，但一旦实现错误会直接
影响所有出站请求。建议只在确有封号证据、且前面几项都已做完时再评估。

**成本/风险**：高。

### P2-8　10105 活跃会话自救（依赖网页活跃腿）

**来源**：`4fb7cb42`；参考文件 `internal/upstream/webagent.go`、
`internal/scheduler/webactivity.go`。

**能力**：上游单账号同时活跃的网页会话上限 10 条（超了 429 / code 10105）。
进程被 kill 时可能留下未收尾的 `working` 会话，攒满后网页活跃腿永久静默失败。
参考仓撞 10105 时先列会话、停掉最旧的活跃会话、保留 8 条，再重试一次；
清理按 `expert-<slug>` 前缀**按账号**分组，避免跨任务累计永远清不掉。

**本地现状**：本地**没有网页活跃腿**（`web_activity` / `console/as` 在面板与
上游调度里都不存在）。因此 10105 自救目前没有触发场景。

**建议**：先决定是否要引入网页活跃任务。若引入，自救逻辑必须同步做；
若不引入，本项仅记录，不移植。

**成本/风险**：引入网页活跃本身是中高成本（ACP / 沙箱 / SSE / 会话收尾），
只移植 10105 清理没有意义。

### P2-9　Windows 常驻运行文档

**来源**：`df988aae`（README 增加开机自启 + 崩溃重启）。

**本地现状**：本项目已有 `docs/watchdog.md` 与 Windows 启动脚本，
覆盖范围不同。

**建议**：不直接搬 README 段落；如确有缺口，把「计划任务 / NSSM」的做法
补进 `docs/watchdog.md`，保持本项目文档结构。

---

## 附录 A：成长任务逐项对照

这一节只回答「成长中心 / 任务中心里还有哪些没同步」。判断口径：

- **已覆盖**：本地已有等价能力，只是文件/运行时不同；
- **弱化**：本地有入口，但范围、频率或闭环比参考仓窄；
- **缺失**：本地完全没有对应能力；
- **分叉**：两边都有实现，但通道、指纹或语义已经不同，不能简单判缺失。

### A.0 结论摘要

优先做三件事，收益最大、风险最低：

1. **礼包改为「先查状态再领」，并把 global 从 early return 里拆出来**（A-1）。
   现状是 CN 盲领、global 完全不领；参考仓已经证明盲领会把「已领过」打成假的
   `+0c` 成功行。
2. **成长奖励从活跃上报链解耦**（A-5）。现在 `claimGrowthRewards` 只在
   `runActivity` 里调用，activity 一关或上报不满 5 条，连登兑换/抽奖当天就整条跳过。
3. **补签从「只补昨日」扩到「本月漏签，由近及远」**（A-2），并把兑换/抽奖
   从「每日一档/一次」改成「全部可领档/全部次数」（A-3、A-4）。

活跃上报通道（B-6）不要先动：本地注释称 global 的 billingBase 也返回 200，
但参考仓的真实抓包认为通道、UA、body 指纹都错了，且明确记录过「200 静默丢弃」。
这一项必须先做同号 A/B 实测，再决定是否切换。

### A.1 核心闭环

| 项 | 本地现状 | 参考实现 | 判定 | 建议动作 | 成本/风险 |
|---|---|---|---|---|---|
| A-1 新手礼包 / 补偿领取 | `scheduler.go:866` `claimGrowthBonus` 直接 `ClaimGift` + `ClaimCompensation`，业务错误静默；`claimGrowthRewards` 在 `scheduler.go:802` 对 global 提前 `return`，global 连礼包都不领 | `scheduler/benefit.go:23` `claimGiftQuiet`：`GetGiftStatus` → `Pending()` → `ClaimGift`；`gift\|uid\|endTime` guard 防同期重复；`scheduler/streak.go:28-32` 对 global 单独 `claimGiftQuiet`（礼包是 billing 域，国际号实测 200） | **缺失**（含 global 漏领） | 补 `upstream.GetGiftStatus`（`POST /billing/meter/check-gift-claimed`，**不带 `/v2`**）；scheduler 改为先查后领；把 global 的礼包领取移到 early return 之前。补偿活动已于 2026-10 结束，本地保留手动兜底即可，不必恢复状态查询 | 低；每号每天多一次只读请求。企业/禁用号查询失败应静默，其余 WARN |
| A-2 补签范围 | `scheduler.go:881` `makeupYesterday` 只判昨日；`growth_bonus.go:99` `UseMakeupCard`；补签成功重读 streak 挑档 | `scheduler/streak.go:103` `makeupMissed` + `streak_extras.go:36` `HeatmapBrokenDates`：取本月所有 `score==0`，从最近一天往回补，受 `schedule.makeup_cards_per_round` 上限 | **弱化** | 加 `HeatmapBrokenDates`；按日期倒序补；遇到上游拒绝即停（注册前日期同样是 0 分，无法与漏签区分）；加每轮上限配置 | 低到中；补签是改历史的写操作，必须封顶、必须可停 |
| A-3 连登兑换档位 | `scheduler.go:848` `growthEligibleTier` 从高到低只挑**一个**已达标未领档，每日一轮 | `scheduler/streak.go:40` 遍历 `full.RedemptionStatus.Tiers`，跳过 `locked`/`claimed`，其余**全部** redeem | **弱化** | 改为遍历所有 `available` 档；保留每日 `rewardClaimedToday` 闸；日志逐档记录 credit/energy/cards/chances | 低；只对 available 写，避免重复 redeem |
| A-4 抽奖次数 | `scheduler.go:907` `claimGrowthLottery` 只 `GrowthLotteryDraw` **一次** | `scheduler/streak.go:70` 按当前 `chances` 循环抽完 | **弱化** | 循环抽到 chances 用尽，失败即停；每次新 `client_token`；加单轮上限保护（例如 50） | 低；无次数/活动关闭按正常态静默 |
| A-5 成长奖励触发点 | `claimGrowthRewards` 只在 `runActivity`（`scheduler.go:750`）里调用；activity 关闭、上报失败或不足 `ActivityReportCount` 条时完全不领 | `RunStreakBonusNow` 在签到排程末尾调用（`scheduler/streak.go:17`），与 activity 解耦；面板也可手动触发 | **弱化 / 结构差异** | 在 `runCheckinNow` 每号签到+余额后调用 `claimGrowthRewards`，或做独立 `RunGrowthNow`；activity 只负责点亮与自检。保留每日闸防双触发 | 低；签到与 activity 可能同日各触发一次，有 `rewardClaimedToday` 兜底 |
| A-6 global 签到 | `checkinAll` 在 `scheduler.go:577` 直接 `CheckinSkipped, "global"`；Python `realm.supports_checkin()` 也返回 false | `scheduler/checkin_global.go`：先 `GetCheckinActivityStatus`，查询失败**直接返回**（不猜），只有 `active && !today_checked_in` 才 claim；国际活动当前常态关闭 | **弱化（可安全补）** | 加只读状态查询；global 走 fail-closed，CN 走 fail-open；面板文案同步。若不想发写请求，也可以只展示状态 | 低；国际活动可能 500/404，必须静默 |
| A-7 签到活动状态 | 无 `checkin-activity-status` 调用；Go 直接 `DailyCheckin`，靠 10001 判「今天已签」 | `upstream/checkin_activity.go`：`POST /billing/meter/checkin-activity-status`（CN `/v2`，global 无前缀优先、404 回落），返回 `active/today_checked_in/streak_days/end_time` | **缺失** | 补只读客户端；CN 失败不阻断签到，global 失败不写；面板显示活动是否有效、今天是否已签、连登天数、截止日 | 低；只读，无副作用 |
| A-8 国际版礼包 | 同 A-1，global 被 early return 挡住 | `scheduler/benefit.go` + `scheduler/streak.go` 的 global 分支 | **缺失** | 与 A-1 一起做；礼包按 realm 走 billing base，不要用 CN 固定 base | 低 |

### A.2 任务执行与调度

| 项 | 本地现状 | 参考实现 | 判定 | 建议动作 | 成本/风险 |
|---|---|---|---|---|---|
| A-9 成长任务中心自动化 | Python `server/services/dailyplan.py` + `taskrun.py` + `upstream/scripts/task_runner.py` 有任务列表/接受/领奖/小程序/桌面事件；`full` 默认关闭；状态在进程内，无持久作业、无断点恢复 | `scheduler/growth.go` + `panel/taskjobs.go`：每日排程启动**持久化后台作业**，per-account 锁，调度器不等待完整扫描；`sweep.go` 分钟级补领清扫；`runstamps.go` 重启不重打 | **架构差异 + 部分弱化** | 不搬 Go 内嵌面板。在 Python 侧补「定时 growth 队列 + per-account 锁 + 状态持久化 + claim sweep」；把 `full` 拆成 scan/accept/action/claim 四段，允许只跑安全段 | 中；重复动作与写风控是主要风险 |
| A-10 任务列表/接受/领奖 API | `task_runner.py` 已覆盖 `/activity/growth/tasks`、`/tasks/accept`、`/tasks/{code}/claim`、web fallback、小程序任务 | `upstream/tasks.go`、`upstream/mp_events.go` | **已覆盖（架构不同）** | 不搬 Go 文件；只补 Python 侧调度、锁与持久化 | 低 |
| A-11 桌面任务事件链 | `task_runner.py` 有 `RichMeow_Chat` 等桌面事件组，也发 `appearance_skin_apply` | `upstream/desktop.go` `desktopReport` + 桌面指纹；`SetAppearanceTheme` 走独立 API | **部分覆盖** | 先补 A-12 的真实主题应用；事件链/指纹按任务完成率再决定是否全量对齐 | 低到中 |
| A-12 外观任务（Hp_Appearance） | `task_runner.py:889` 只发 `appearance_skin_apply` 事件，没有 `POST /v2/user-asset/appearance/set` | `desktop.go` `SetAppearanceTheme`：先真正应用主题（`kind:"theme"` + `resource_key`），再发事件；参考仓注释明确这是「独有 API」 | **弱化（可能点不亮）** | 在 Python skin 任务里补真实 apply 调用，成功后再发事件；apply 失败降级为仅事件并记日志 | 低 |
| A-13 盲盒消费 | 本地无 `buddy/quota` / `buddy/open` | 参考仓 M-P1-2 已列：`internal/upstream/travel.go` + `scheduler/travel.go` | **缺失** | 见主清单 P1-2，不重复展开 | 低 |
| A-14 补领清扫 | 无分钟级 sweep；Python `dailyplan` 的 `claim` 是小时级/手动 | `scheduler/sweep.go` + `panel/tasksweep.go`：只 accept 待接受、只 claim 已达标未领 | **缺失** | 在 Python 侧加独立周期任务；只做只读列表 + 安全写（accept/claim），不跑行为链 | 中；必须与手动任务共用 per-account 锁 |
| A-15 重启补跑标记 | 无；进程重启后 `missedToday` 会把当天时点全判为「已过、未跑」，可能重复打签到/旅行/活跃 | `scheduler/runstamps.go`：按天落一个小文件，重启不重打 | **缺失** | 优先补；只记日期不记时刻，旧日期写过即丢 | 低 |
| A-16 账号级摊开 | `random_schedule.go` 只给账号间 gap 加 jitter；同一任务仍是顺序/并发集中打 | `scheduler/spread.go`：按 `hash(task\|uid\|day)` 在窗口内摊开，冲突时推开 | **弱化** | 补账号级偏移；偏移当天确定性，重启不重排 | 低到中；只解决时间相关性，不是防封 |
| A-17 分钟级人味化抖动 | 无 | `scheduler/humanize.go`：余额刷新/补领清扫间隔 ±frac 抖动，进程内种子稳定 | **缺失** | 视是否启用分钟级任务再补；先做 `runstamps` + `spread` | 低 |
| A-18 余额 401 求证 | 余额刷新失败只记日志；没有把 401 转成 session dead 的求证链 | `scheduler/balance_401.go`：401 先 refresh 求证，`alive` 不罚、`dead` 计 session dead、`inconclusive` 不罚 | **缺失** | 见 B-5；与 401 三态一起做 | 中；refresh 会轮换 token，必须落盘新值 |

### A.3 建议实施顺序

1. **第一批（只读 + 低风险）**：A-1 礼包状态查询、A-6/A-7 签到活动状态、A-2 的 `HeatmapBrokenDates` 只读部分。
2. **第二批（闭环）**：A-5 成长奖励解耦、A-3 全部档兑换、A-4 抽完次数、A-2 补签范围、A-1 global 礼包。
3. **第三批（任务系统）**：A-9 的 Python 侧持久作业与锁、A-12 外观 API、A-14 补领清扫、A-15～A-18 调度健壮性。
4. **最后再评估**：B-6 活跃上报通道切换。没有 A/B 实测数据不要动。

---

## 附录 B：协议层逐项对照

这一节只回答「请求/响应协议上还有哪些没同步」。按影响面分四组：

- B.1 请求体与响应体；
- B.2 端点与账号资料；
- B.3 模型目录、档位与观测；
- B.4 出站头与指纹。

### B.0 结论摘要

**低风险、建议先做**：

- B-2 会话轮级 body 字段（`conversationRequestId` / `startsNewUserRequest` / `agent`）；
- B-3 Kimi 工具 `$ref` 展开；
- B-4 usage 缓存命中别名归一；
- B-11 账号昵称手动同步（只读、隐私边界清楚）；
- B-16 prep/do 分段耗时（纯观测）。

**收益明确、成本中等**：

- B-5 401 三态求证；
- B-7 global 余额 summary/paid/free；
- B-14 模型 picker 裁剪；
- B-15 思考档位策略层；
- B-17 `/v2/chat/busy`（本地池状态版或上游观测版二选一）。

**高收益但高风险，必须分组验证**：

- B-1 DSML 工具调用还原；
- B-21 出站头/UA 黄金头集；
- B-22 头顺序、大小写、TLS 指纹。

### B.1 请求体与响应体

| 项 | 本地现状 | 参考实现 | 判定 | 建议动作 | 成本/风险 |
|---|---|---|---|---|---|
| B-1 DSML 工具调用还原 | 无。`tool_pairing.go` 只修已结构化的 `tool_calls` / `tool` 消息；`sanitize.go` 只剥指纹 | `upstream/dsml.go`（`e0ee6b9a`）：识别正文里的全角竖线 DSML 标记，流式与非流式都还原成标准 `tool_calls`；严格判定（工具名在名单）与弱判定（名单为空但块完整闭合、工具名形态合法）两级；本回合已有结构化 `tool_calls` 时不覆盖 | **缺失** | 在 `sse.go` 与非流式响应组装处加一层 `repairDSML`；只处理完整闭合块，块内不夹带正文；工具参数被截断不修；必须有开关与日志 | 中高；误判正文会破坏正常输出，严格判定优先 |
| B-2 会话轮级 body 字段 | 只在头里写 `X-Conversation-ID` / `X-Conversation-Request-ID`；消息体没有 `conversationRequestId`、`startsNewUserRequest`、`agent` | `upstream/conv_fields.go`（`56057810`，issue #51）：只给当前轮最后一个 user 消息写 `conversationRequestId` + `startsNewUserRequest`，并补 `agent:"cli"`；历史消息不伪造 | **缺失** | 在 `PrepareBody*` 之后注入；客户端已带则不覆盖；只写当前轮，不补历史 | 低 |
| B-3 Kimi 工具 `$ref` 展开 | `payload.go:437` 只做 `normalizeToolEnumTypes`（补 enum 的 type），不展开 `$ref` / `$defs` | `upstream/kimi_tools.go`（`854e74cd`）：仅 CN 且模型名 `kimi*` 时，把 tools schema 里的 `$ref` 内联展开；不裁剪任何工具；解析失败原样返回 | **缺失** | 在 enum 归一之后加 `expandToolRefs`；限制递归深度/环；只对 CN Kimi；失败保留原字节 | 低 |
| B-4 usage 缓存命中别名 | `sse.go:287` 只补 `total_tokens`，不统一缓存命中字段 | `upstream/usage.go`（`782a6414`）：取 `prompt_tokens_details.cached_tokens` / `prompt_cache_hit_tokens` / `cache_read_input_tokens` / `cached_tokens` / `input_tokens_details.cached_tokens` 中的最大正值，统一写回别名；流式与非流式都调用 | **缺失** | 加 `normalizeUsageCacheAliases`，在响应离开网关前调用；只在下游已有 `input_tokens_details` 时保留该嵌套形态 | 低 |
| B-5 401 三态求证 | `handler.go:884` refresh 失败且 `ErrSessionDead` 才 `NoteSessionDead`；其余 401 没有统一求证；余额刷新失败只记日志（`scheduler.go:587`） | `upstream/verify.go`（`21a6e456`、`f6bd0539`）+ `scheduler/balance_401.go`：`VerifyAlive` / `VerifyDead` / `VerifyInconclusive`，refresh 做权威求证；alive 不罚、dead 计 session dead、inconclusive 不罚 | **部分覆盖** | 把 401 求证收敛到一个 `VerifySessionByRefresh`；chat 401 与余额探测共用；refresh 成功必须保存轮换后的新 token；inconclusive 不写池状态 | 中；轮换 token 必须落盘，否则会把号弄坏 |
| B-6 活跃上报的 body 形态 | `report.go:93` 发 `billingBase`，事件只含 20 多个字段；scheduler 用 `wb2api-<ms>` / `wb2api-<ms>-rN` 作 conversation/request ID，每号发 `ActivityReportCount`（默认 5）条 | `report.go:252` `ReportChatActivityModel` 发 `chatBase`，用 `session.NewConversationID()` 官方 UUID，body 带设备/包指纹；每号每天 1 条；企业号（`SupportsPersonalGrowth()==false`）直接跳过 | **分叉** | 先做同号 A/B：两通道各发 1 条，回读 streak 与任务进度；确认后再切。切换时一并改 UUID、UA、头和指纹；企业号加跳过；条数是否降为 1 也按实测决定 | 中到高；本地注释称 billingBase 200，但 200 ≠ 计分 |

### B.2 端点与账号资料

| 项 | 本地现状 | 参考实现 | 判定 | 建议动作 | 成本/风险 |
|---|---|---|---|---|---|
| B-7 global 余额 summary / paid / free | Python `tencent.fetch_credits:796` 始终走 CN 的 `get-user-resource` + CN body；Go `client.go:2040` 对 global 也用同一 body 与解析；没有 summary/paid/free | `upstream/billing_summary.go`（`bde6da11`）：`get-user-resource-summary` + `get-user-resource-paid-packages` + `get-user-resource-free-packages`；`flexFloat` 兼容字符串小数；按 `PackageCode` 关联；明细失败时只降级到期批次，不丢总额 | **缺失**（global 余额可能恒 0） | 先改 Python 面板展示，再改 Go 池权重。global 才走三件套，CN 保持现状；summary 给权威 remain/total，paid/free 只补到期；字段可能是字符串，必须宽容解析 | 中；解析兼容与降级策略是关键 |
| B-8 签到活动状态 | 无 | `upstream/checkin_activity.go`（`17d12a5f`）：`POST /billing/meter/checkin-activity-status`；CN `/v2`，global 无前缀优先、404 回落；只读 | **缺失** | 见 A-6/A-7；CN 查询失败 fail-open，global fail-closed | 低 |
| B-9 礼包状态 | 无 | `upstream/benefit.go`（`5367e78d`、`16f39d26`）：`POST /billing/meter/check-gift-claimed`（**不带 `/v2`**）；`Pending() = active && !claimed && credit_num>0` | **缺失** | 见 A-1；不要复用 CN `billingMeterPaths`（它只给 `/v2`） | 低 |
| B-10 国际版注册 / 地区 / trial | Python `tencent.py:1545` `registration_status`、`:1578` `submit_region`、`:1664` `claim_trial`，并已调 `get-country-code` | `upstream/global_register.go`：注册状态 + `get-user-area-info` 当前地区 + `console/login/account` 提交地区 + `ide/trial` 领 trial；响应可能是双层 JSON 字符串 | **基本覆盖（Python）** | 不搬 Go；如需面板回显「当前地区」，补 `get-user-area-info` 只读调用；地区选择仍由用户决定 | 低；隐私/地区合规 |
| B-11 账号昵称同步 | 无 `/console/account` 调用；昵称来自导入文件 | `upstream/profile.go`（`04005ba2`，issue #94）：`GET {webBase}/console/account`，Bearer + `x-client-platform: web`；只解析 `nickname` 与 `uid`，手机号等字段不解析、不落日志、不透传；仅手动刷新触发 | **缺失（低优先）** | 只做手动刷新；只取 nickname/uid；后台余额刷新不调用 | 低；隐私边界必须写死 |
| B-12 桌面端 / 外观 | Python 有桌面事件链与 skin 事件，但没有真实 `appearance/set`；Go 无 | `upstream/desktop.go`（`5677928d`、`ee1c80d8`）：`desktopReport` + `SetAppearanceTheme`（`POST /v2/user-asset/appearance/set`，`kind:"theme"` + `resource_key`） | **部分覆盖 / 分叉** | 先补 A-12 的真实 apply；事件链与指纹按任务完成率再决定是否全量对齐 | 低到中 |
| B-13 小程序埋点 / 任务 | Python `school_open_day_2026.py` + `task_runner.py` 已覆盖小程序 `X-Client-Platform: miniprogram`、`chat_request_send` + `activityId`、小程序任务 | `upstream/mp_events.go`、`upstream/tasks.go` | **已覆盖（架构不同）** | 不搬；只补 Python 调度与锁 | 低 |

### B.3 模型目录、档位与观测

| 项 | 本地现状 | 参考实现 | 判定 | 建议动作 | 成本/风险 |
|---|---|---|---|---|---|
| B-14 模型 picker 裁剪 | `global_models.go` 已有 v3-config-merge；`client.go:1787` 已按 `agents[cli].models` 过滤企业端点；但没有 `ModelRateLimitCap.lines[].paidId` 合并、没有速度预设/auto 过滤，也没有多 UA 探测 | `upstream/clientpick.go`（`c5ac20fc`）：以默认 agent 的 `models` 为菜单源；`paidId` 并到 `freeId` 行不单列；`fast-model` / `balanced-model` / `deep-model` 预设保留；`auto` / `auto-chat` 不画成菜单行；没有 agents 时不裁剪 | **部分覆盖** | 在 `/v1/models` 输出前套 picker；只在 agents 存在时裁剪；不臆造目录里没有的 id；多 UA 探测（`v3ProbeUA` / `v3ProbeUALite`）只在目录明显不全时再加 | 低到中；列表变短需在文档里解释 |
| B-15 思考档位策略 | `effort_catalog.go` 有静态兜底表；`payload.go` 会把不支持的 effort 改到最近档；`thinking.go` 负责 DeepSeek 思维注入；没有网关策略层 | `upstream/effort_policy.go`（`1792ef1f`、`24b8f398`）：`Enabled` / `ClientPriority` / `OverrideScope=level\|all`；`default_effort=max` 取模型最高支持档；面板可配置 | **部分覆盖（缺策略层）** | 加 config 与 `SetEffortPolicy`；复用现有 catalog；默认关闭，保持现状；补客户端优先/网关优先/level/all 的测试矩阵。策略层落地时一并做 `/v1/models` 的声明改写（`can_disable_thinking` + `off` 档位 + 改写默认档），见 `upstream-sync-baseline-gaps.md` 的 **G-P1-17** | 中；会改写用户意图，默认必须保守 |
| B-16 分段耗时观测 | `server/logging.go` 只有 TTFB（首 data 帧），把选号、准备、建连、上游首字节混在一起 | `upstream/timing.go`（`75576b8b`）：`Prep`（本地准备）/ `Do`（上游往返）分段；记录客户端请求档位与实际生效档位；纯观测，不参与判定 | **缺失（观测）** | 在上游调用处埋点，日志增加 prep/do/requested_effort/effort；不影响路由与重试 | 低 |
| B-17 模型忙闲观测 / `/v2/chat/busy` | 无上游客户端；本地对外也没有 `/v2/chat/busy` 路由（主清单 M-P1-4 建议本地按池状态算） | `upstream/modelbusy.go`（`2737aed3`）：`POST /v2/chat/busy`，单次最多 50 个模型，`busy_level=idle\|normal\|busy\|congested\|unavailable\|unknown`；unknown 不当不可用；不参与选号 | **缺失（观测/兼容）** | 二选一：做本地池状态版 `/v2/chat/busy`（M-P1-4），或只做面板观测。不要拿它当选号依据 | 低到中 |
| B-18 模型目录 UA 探测 | 本地 v3 探测用默认 UA | `headers.go` `v3ProbeUA` / `v3ProbeUALite` / `rpcUAFor` / `uaBrandFor`：按路径分 UA 族，v3 用 IDE UA 拿更全目录 | **弱化** | 只有在确认目录缺模型时再加；不要把所有路径的 UA 混成一个 | 低到中 |

### B.4 出站头与指纹

| 项 | 本地现状 | 参考实现 | 判定 | 建议动作 | 成本/风险 |
|---|---|---|---|---|---|
| B-19 客户端版本 / CLI 版本 | `headers.go:21` 默认 `5.5.4`，CLI `2.137.1`，不分 realm | `headers.go:33-38`：CN `5.9.3`、global `5.9.4`、CLI `2.165.0` | **分叉（版本明显偏旧）** | 先只改版本常量并按 realm 分档；这是低风险、收益明确的一步 | 低；版本段与真实分发包一致即可 |
| B-20 UA 品牌段 | `defaultWorkBuddyUAFor` 对 global 用 `WorkBuddy AI`（带空格）；billing UA 用 `WorkBuddy/<ver>` | `defaultWorkBuddyUAFor` 首段恒 `WorkBuddy`，第二段 global 用 slug `workbuddy-ai`；billing UA 用 `axios/1.16.1`；另有 `rpcUAFor` / `v3ProbeUA` 分族 | **分叉** | 按路径分组对齐：chat 用 `WorkBuddy/<ver> <brand>/<ver> CLI/<ver>`，global 品牌段改 `workbuddy-ai`；billing 改 `axios/1.16.1`；不要全局一个 UA 打天下 | 中；品牌段错曾触发 11140，需真实抓包复核 |
| B-21 出站头集合 | 本地有 Origin/Referer、Accept-Language、X-Requested-With、X-CodeBuddy-Request、X-Machine-ID、X-Session-ID 全路径发送；缺 `b3`/`baggage`/`traceparent`/`X-Agent-Intent`/`X-Agent-Runtime`/`X-Agent-Type`/`X-Id-Source`/`X-Userinfo`/`X-Private-Data`/`X-Stainless-*`/`Accept-Encoding`/`Connection`/`acp-connection-id` | `headers.go`（`8f7366f2`、`d9b92b2d`、`bde6da11`、`ee1c80d8`、`8ecf115a`、`847deb10`、`5677928d`）：RPC 链不发 Origin/Referer/Accept-Language/XRW；`X-CodeBuddy-Request` 只 chat 带；`X-Machine-ID`/`X-Session-ID` 从头发移除、设备标识改放 report body；`Accept-Encoding` 四段声明并自行解压；身份头按账号形态条件发送；chat 补 b3/baggage/traceparent 与 SDK 的 `X-Stainless-*` | **分叉 / 缺口（高风险高收益）** | 不要零散改。先建「官方黄金头集」回归（按 chat/report/refresh/billing/v3 分组），再逐组对齐；先改版本/UA，再改增删头，最后动头顺序/TLS。每一步都用真实抓包验证 | 中到高；头改错会直接影响所有出站请求 |
| B-22 头顺序 / 大小写 / TLS 指纹 | 无 | `headcase.go`、`headorder.go`、`headorder_conn.go`、`tlsfingerprint.go` | **缺失（M-P2-7）** | 先不做；除非有封号证据。HTTP/2 头顺序受库控制，改造成本高 | 高 |
| B-23 sanitize / thinking / tool_pairing 的双向分叉 | 本地有额外特征（`workspace base`、`Tokens prefixed with @` 等）、Kimi enum 补 type、tool result repack / orphan cleanup | 参考仓有 `DeepSeek Harness` 身份特征、`sanitize_brand_scope`、reasoning backfill、DSML、`$ref` 等 | **双向分叉，不是单向缺失** | 逐函数 diff，保留本地已有特征，只补参考新增；不要整文件覆盖。优先补 `sanitize_brand_scope` 与 reasoning backfill（若本地没有） | 中；整段覆盖会丢本地特征 |
| B-24 代理池 / 按账号路由 | 本地已有 `Auth.ProxyName` 与按账号 transport；没有池级代理轮换 | `proxy.go` + `56bef12d` 的代理池与按账号路由 | **部分覆盖（非协议）** | 多出口是刚需时再评估池级轮换；否则不做 | 中 |

### B.5 建议实施顺序

1. **零风险观测/修正**：B-2 body 字段、B-3 `$ref`、B-4 usage 别名、B-16 分段耗时、B-11 昵称手动同步。
2. **账号健康**：B-5 401 三态、B-7 global 余额、B-8 签到状态、B-9 礼包状态。
3. **兼容与体验**：B-14 picker、B-15 effort policy、B-17 busy。
4. **高风险协议层**：B-1 DSML、B-21 头/UA 黄金头集、B-22 头顺序/TLS。
5. **已覆盖，不要重复做**：B-10 国际版注册（Python 已有）、B-12 桌面事件链（Python 已有主体，只缺 apply）、B-13 小程序任务（Python 已有）。

### B.6 不要为了「同步完整」而搬的东西

- `internal/panel/*` 的内嵌面板、`taskjobs.go`、`tasksweep.go` 的 Go 实现：本地是 Next.js + Python，搬过来只会多一套运行时。
- `tlsfingerprint.go`、`headorder*`：收益是「更像官方客户端」，但改错会影响全部出站请求；没有封号证据前不做。
- `tasks.go`、`mp_events.go`：本地 Python 脚本已经覆盖，重写没有收益。
- `webactivity.go` / 10105 自救：本地没有网页活跃腿，单独搬清理逻辑没有触发场景（见主清单 M-P2-8）。

---

## 提交索引

> 下表只列**基线之后**的提交。附录 A/B 里引用的多数提交早于 `49fad282`，
> 属于「基线之前一直没移植」的能力，未在本表重复；真正移植时同样要在
> `deploy/workbuddy-panel-ref.txt` 追加提交号并注明「部分同步」。

| 提交 | 标题 | 处置 |
|---|---|---|
| `29d98794` | `/v1/search` 卡片改写为等价检索链接（仅 DSH） | P2-2，等有 search 端点再说 |
| `f6335ce8` | 余额趋势时间筛选器与刷新按钮补事件绑定 | P2-5，面板需自研 |
| `df988aae` | README 补 Windows 常驻运行章节 | P2-9，按需并入 watchdog 文档 |
| `b494426a` | 入站请求体上限 + 11140 风控连坐保护 | **已同步一半**：上限见 G-P0-2 ✅；连坐保护仍待做（M-P0-1） |
| `c6f953a7` | 慢上传可见性 + 在途名额释放回归 | **已同步一半**：慢上传 WARN 随 G-P0-2 ✅；名额释放回归仍待做（M-P2-6） |
| `e36ad073` | 覆盖已有凭据前先求证新凭据还活着 | **P1-1** |
| `4fb7cb42` | 盲盒消费 + 10105 活跃会话自救 | **P1-2**（盲盒）；10105 见 P2-8 |
| `0afd8ae4` | AutoBot chore | 跳过 |
| `e466d543` | AutoBot chore | 跳过 |
| `29e4701f` | AutoBot chore | 跳过 |

> 这张表只覆盖基线之后的 10 个提交。第一批实际动到的另外 7 个提交
> （`4356a346`、`1e7a693b`、`16f83d75`、`025cc107`、`764293b8`、`b7297e5c`、
> `5ce1a2f4`）都**早于** `49fad282`，属于「基线之前」的缺口，已在
> `deploy/workbuddy-panel-ref.txt` 与
> [upstream-sync-baseline-gaps.md](upstream-sync-baseline-gaps.md) 的同步状态表里
> 记录。反过来说，上表里 `b494426a` / `c6f953a7` 虽然在这里，能力却归
> baseline-gaps 的 G-P0-2——判定依据是「本地缺不缺」，不是「提交早不早」。

## 维护方式

1. 真正移植某一项后，在 `deploy/workbuddy-panel-ref.txt` 追加对应提交号，
   并在提交信息里写清「来源仓库 + 提交号 + 只取了哪部分」；
2. 如果只移植了一个提交里的一部分（例如 `b494426a` 只取连坐保护），
   在 ref 文件里注明「部分同步」，避免下次误以为整提交已合入；
3. 本清单是**分析结果**，不是承诺：优先级会随线上问题变化，实施前再核对
   参考仓当时的最新实现。
4. **本文只收「基线之后」的新提交**。基线之前一直没移植的能力（`safefile`、
   Go 侧 body limit / 超时、CST 日界、`reserve_credits`、`account_share`、
   `model_fallback`、6020/6021、`/v1/capacity`、Go `/metrics`、`budget`、
   modelview/toggle/plan 等）见
   [upstream-sync-baseline-gaps.md](upstream-sync-baseline-gaps.md)，
   两份清单同一项只在一处展开。其中前四项与 `keepalive_refresh_window`、
   Go 侧 hidden 过滤**已于 2026-11 第一批落地**，状态见那份文档的「同步状态」表。
