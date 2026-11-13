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
| 本次分析的参考 HEAD | `7a48d72e`（2026-11-13） |
| 基线之后的非 merge 提交 | 76 个（2026-11-07 → 2026-11-13） |
| 其中应跳过 | 面板 / testdata / 依赖 / chore / proxypool 类，逐条见文末「提交索引」 |

`49fad282` 那次同步的成果是 1.1.0 的四项能力：`X-Request-Id`、Prometheus
`/api/metrics`、隐藏模型、通用 Webhook。基线之后的新提交，以及基线之前一直
没移植的较大能力，都列在下面。

> **基线已推进**：上一版按 `29e4701f`（2026-11-07）梳理，只有 10 个非 merge 提交；
> 本次按 `7a48d72e`（2026-11-13）重扫，非 merge 提交 76 个。新增条目集中在
> P0-2～P0-5、P1-5～P1-10、P2-10～P2-18，上一版的 P0-1 / P1-1～P1-4 / P2-1～P2-9
> 编号与内容保持不变，便于和旧记录对照。`29e4701f` 那批的三个 AutoBot chore
> （`0afd8ae4`、`e466d543`、`29e4701f`）仍在提交索引里，处置不变。

> **2026-11 第一批已落地**（六项，见
> [upstream-sync-baseline-gaps.md](upstream-sync-baseline-gaps.md) 的「同步状态」表）：
> `safefile`、Go 侧入站体上限、入站读取超时 / 关停窗口、CST 日界、
> `keepalive_refresh_window`、Go 侧 hidden 过滤。本文受影响的只有两处——
> P0-1 里的「入站请求体上限」那一半已完成（连坐保护仍未做），M-P2-6 的慢上传
> 可见性已完成（在途名额释放回归仍未做）。

> **2026-11 低风险批**（本文「一页摘要」前 9 项，逐项见各条目末尾的「落地状态」）：
> **九项全部落地**——M-P0-3、M-P2-14（Go 侧）、M-P2-15、M-P1-11、M-P1-6、
> M-P1-8、M-P1-14、M-P1-12、M-P1-15、M-P0-2。这批不改流量分布，与下一批
> （`reserve_credits` → `model_fallback` → 6020/6021 → `account_priority` /
> `account_share` → `balance_window`，需灰度）不是同一批。

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

### P0-2　签到「请稍后重试」的分钟级自动补签

**能力**

上游对签到偶发返回 `http 500 {"code":10001,"msg":"签到失败，请稍后重试"}`。
参考仓实测：同一批 25 个号里 2 个这样失败，20 分钟后手动重签立刻成功、各 +100
积分——是上游瞬时状态，不是账号问题。

本地对这类瞬时失败只有**秒级**原地重试，窗口一过就等 12 小时后的下一个签到时点，
当天积分直接丢掉；漏一天就是断连登，而面板只显示一行「签到失败」，看不出还会再试。

**来源**

- `1adfa520` `fix(scheduler): 签到遇到「请稍后重试」不再直接放弃，10/40 分钟后自动补签`
- 参考文件：`internal/scheduler/checkin_retry.go`、`scheduler.go`、
  `internal/pool/cooldown.go`、`internal/upstream/report.go`

**本地现状**

- `upstream/internal/scheduler/scheduler.go:460` 的 `checkinRetryDelays` 是
  `5s / 15s / 60s` 三次原地重试，`dailyCheckinWithRetry` 跑完就返回；
- `checkinRetryable`（同文件 `:465`）只认 HTTP 5xx 与 `code 10001`，判据本身
  与参考仓一致，缺的是「主批结束后再排一轮」的机制；
- 没有 `checkin_retry.go`，也没有后台补签队列。

**建议动作（只改 Go 上游）**

1. 新增 `internal/scheduler/checkin_retry.go`：主批结束后把「瞬时失败」的账号
   收进内存队列，在第 10、40 分钟各补签一次（两个延迟做成包级变量，测试可改小）；
2. 只排 `checkinRetryable` 为真的错误。业务拒绝（企业号「不支持该操作」、参数错）
   重试必然同样失败，排进去只会每天多打几轮无效请求；
3. 补签前先查本地 `checkin_done` 记账：用户可能已在手机上签过，上一轮补签也可能
   已经成功；
4. 后台 goroutine + ctx 取消即退。**不能阻塞 `runCheckin` 返回**——摊开启用时要
   等它返回才清状态，阻塞会让下一个时点整批被跳过；
5. 把单账号签到抽成 `checkinOne`，让主批与补签共用同一份判据（活动状态查询、
   已签记账、幂等拒绝三处语义原样搬过去）。

**成本与风险**

- 成本：低。纯调度层，不碰协议、不加依赖；
- 风险：补签是写操作，必须受「每账号每日最多补两次」约束；进程重启会丢队列，
  属于可接受的降级（次日排程仍在）；
- 与 P1-12（签到回执四态）有交集：补签成功/失败也要写进同一份回执，否则面板
  仍看不出「补签已重试过」。两项建议一起做。

**落地状态（2026-11 低风险批）**：✅ **已同步**。新增
`upstream/internal/scheduler/checkin_retry.go`：

- `checkinLateRetryDelays = []time.Duration{10m, 40m}`（包级变量，测试可改小）；
  与本地已有的 `checkinRetryDelays`（5s/15s/60s **原地**退避）语义不同，故另起名
  `checkinLateRetry*`——同名会让两套节奏混在一个变量里；
- 抽出 `checkinOne(a, uid) (CheckinStatus, error)`：成功 → `CheckinOK`；上游
  幂等拒绝 → `CheckinAlready` 且 err 为 nil；真失败 → `CheckinFail` + err。两种
  「今天确实已签」都落 `NoteCheckinDone`，主批与补签共用同一份判据；
  `dailyCheckinWithRetry` 改名 `checkinWithRetry` 并改为调它，语义不变；
- `checkinAll` 收集 `lateRetry`（只收 `checkinRetryable` 为真的错误），末尾
  `startCheckinLateRetry(ctx, lateRetry)` 起后台 goroutine，**不阻塞返回**；
- 每轮补签前先 `Pool.CheckinDoneToday(uid)` 跳过已签账号；再按主批同一条跳过
  规则排除 `Disabled` / `RiskPaused` / `global` / 无凭证；
- `lateRetryWait(round, prev)` 单独成函数：延迟是**从本批结束起算的绝对偏移**，
  第二轮只再等 40m−10m=30m。内联 `[round]-prev` 的算术差异藏在分钟级时间里，
  功能测试看不出来（漏掉 `-prev` 会让第二次补签被推到 50 分钟后）。

`pool` 侧补签到记账（参考仓已有、本地原先完全没有）：新增
`internal/pool/checkin.go` 的 `NoteCheckinDone` / `CheckinDoneToday`，日键走
`checkinDayKey` → `cst.Day`；`entry.lastCheckinDay` +
`stateAccount.LastCheckinDay`（`last_checkin_day`，空串省略）+ `persist.go`
落盘/恢复两处映射。**没有**照抄参考仓把记账塞进 `cooldown.go`——签到与冷却域
正交，混在冷却文件里会让人误以为改冷却会影响签到。

**有意不移植**（本地没有对应能力，照抄会引入空壳）：`GetCheckinActivityStatus`
（上游活动状态查询，本地无此接口，签到自身的幂等错误标记仍是兜底判据）、
`SupportsPersonalEndpoints`（本地用 `RefreshTokenValue() != ""` + `IsGlobal()`）、
`includeDisabledInTasks`（本地无此开关，直接按 `Disabled` 跳过）、
`upstream.IsTransientBilling`（本地等价物就是 `checkinRetryable`，判据一致）。

测试：`checkin_retry_test.go` 7 条（瞬时失败后补签成功 / 两轮后成功 /
两轮后放弃 / 业务拒绝不排 / 已签跳过 / 禁用与 global 跳过 / ctx 取消即退）+
`TestLateRetryWaitIsAbsoluteOffset` + `TestCheckinOneAlreadyIsIdempotent`；
`pool/checkin_test.go` 5 条（记账 / CST 日键 / 跨重启保留 / 空值不落盘 /
`checkinDayKey` 与宿主时区无关）。14 条变异全 RED。来源提交 `1adfa520`
（部分同步）。

**仍未做**：P1-12（签到回执四态）没做，所以补签成功/失败**不写进**
`CheckinOutcome` 回执——面板在补签那一轮看不到结果，只能从日志看。这是本项的
已知缺口，两项一起做才闭环。

### P0-3　单个 `data:` 行里塞了多个 JSON 时拆分

**能力**

上游偶发把两个 chunk 塞进同一个 `data:` 行：`data: {"id":"..."} {"id":"..."}`。
按 SSE 规范，一个事件的 data 值就是整行文本，客户端只能把它当一份 JSON 解析，
必然失败在「第二个对象」上（`Unexpected non-whitespace character after JSON`），
**整轮对话作废**，而报错在读侧，看不出是上游粘帧。

参考仓在 `writeFrame` 解析失败时先尝试切分：按字节扫描、跟踪字符串状态（含 `\`
转义）与 `{}`、`[]` 嵌套深度，深度从 0 回到 0 且下一段首尾相接就切一刀；每段
再单独 `json.Valid` 校验，**宁可少切也不产出半截对象**。切不动就保持原样透传。

**来源**

- `afb5f224` `fix(upstream): 一个 data 行里塞了多个 JSON 时拆分，不再让客户端解析失败`
- 参考文件：`internal/upstream/sse_frame_split.go`、`sse.go`

**本地现状**

- `upstream/internal/upstream/sse.go` 三处（`:167` 聚合、`:570` 流式 `writeFrame`、
  `:800` 另一条流式路径）都是直接 `json.Unmarshal` 整行，失败就原样透传；
- 没有 `sse_frame_split.go`，也没有「看起来该是 JSON 却解析失败」的 WARN——
  线上出现粘帧时完全查不到。

**建议动作（只改 Go 上游）**

1. 新增 `internal/upstream/sse_frame_split.go`：`splitConcatenatedJSON` +
   `looksLikeJSON`，单帧最多拆 64 份（防止几百 KB 的坏帧被切成上千次写）；
2. `writeFrame` 解析失败时先试拆分，切得动就逐个按常规路径递归写出（每段仍是
   独立事件、独立 `\n\n`），切不动保持原样透传；
3. 拆分命中打一条 WARN（含份数与首段截断预览）；以 `{` / `[` 开头却解析失败的帧
   也补一条 WARN——纯文本心跳不刷屏；
4. 聚合路径（`:167`）同样接入，否则非流式请求仍会整轮失败；
5. 测试：字符串内花括号、半截对象、括号不配平、纯文本反例，以及端到端
   「客户端每个事件恰好一个 data 行、每帧单个合法 JSON、`[DONE]` 恰好一个」。

**成本与风险**

- 成本：低。一个纯函数 + 三处接线，无新依赖；
- 风险：切分器必须严格——切错会把一个合法帧劈成两个半截，比不切更糟。所以每段
  都要单独 `json.Valid`，任何一段不合法就整体放弃拆分。

**落地状态（2026-11 低风险批）**：✅ **已同步**。新增
`upstream/internal/upstream/sse_frame_split.go`（`splitConcatenatedJSON` /
`looksLikeJSON` / `trimJSONValue` / `maxSplitParts=64`），`sse.go` 的流式
`writeFrame` 与聚合路径 `Aggregate` 两处接入（参考仓只改了流式一处，本地聚合路径
解析失败是静默 `continue`、内容整段丢失，所以一并接）。测试
`sse_frame_split_test.go` 7 组用例；三条变异（流式去拆分 / 聚合去拆分 / 切分器
去 `json.Valid`）全部 RED。来源提交 `afb5f224`。

### P0-4　新版桌面端 `$wbEncrypted` auth 文件（解密 + 回写保持加密）

**能力**

WorkBuddy 桌面端新版本把 auth 文件里的 `accessToken` / `refreshToken` /
`nickname` 等字段换成了**字段级信封**：

```json
{"$wbEncrypted":1,"envelope":"<base64(JSON{suite,keyId,nonce,authTag,ciphertext})>"}
```

参考仓补了两件事：

1. **读**：`key = sha256(utf8(atRestSecretKey))`，AES-256-GCM 解密；AAD 是按固定
   口径拼接的（`WB-AAD\0` + 版本/套件/`keyId` 的长度前缀 + framing），错一个字节
   就解不开；明文可能是纯文本 / JSON / Buffer 包装的 JSON，个别字段还双重 JSON；
   `keyId` 对不上直接报 `at_rest_key_mismatch`，**不猜、不拿乱码当 token 用**；
2. **写**：来源是加密文件的，`SaveAtomic` 必须**按读入时记录的字段清单**重新封成
   信封，否则刷新后的 token 会以明文覆盖掉桌面端的加密文件（桌面端读不了，加密
   也白做）。凭据两个字段漏封则拒绝落盘，不静默降级成明文。

第二条有个容易做错的细节：客户端自己只封 `accessToken` / `refreshToken` /
`nickname` / `phoneNumber`，`expiresAt` 保持明文。按固定清单封会把明文字段加密，
桌面端读不回来——所以必须按「解密时哪些字段是信封」逐个复原。

**来源**

- `c7cc2167` `feat(auth): 支持新版桌面端的 $wbEncrypted auth 文件（自动解密 + 回写保持加密）`
- `97d3f259` `fix(auth): 回写只封「读入时确实加密」的字段；同一 uid 多文件时最新凭证胜出`
- 参考文件：`internal/auth/wbencrypted.go`、`auth.go`

**本地现状**

- `upstream/internal/auth/auth.go` 的 `Parse` 按 `string` / `int64` 解字段，遇到
  信封直接 `storage_parse_error`；`SaveAtomic`（`:356`）只会写明文；
- 没有 `wbencrypted.go`，全仓搜 `wbEncrypted` 0 命中；
- Python 侧 `server/services/tencent.py:479` / `:533` 也是 `json.loads` 后直接读
  `account.uid`，信封形态同样读不出 uid——所以**面板导入也会失败**，不只是 Go。

**建议动作（Go 与 Python 都要改）**

1. Go：新增 `internal/auth/wbencrypted.go`，`Parse` 在字段解包失败时尝试解密；
   记录「哪些字段是信封」到 `Auth` 的私有字段，`SaveAtomic` 按这份清单复原；
2. Go：`keyId` 不匹配、AAD 校验失败、密文不完整一律返回明确错误码，不要回退成
   「当普通字符串用」；
3. Go：无 `$wbEncrypted` 字段的文件原样处理，不做二次序列化（零回归）；
4. Python：`find_auth_file_for_uid` / `write_auth_file` 也要认信封，否则面板导入
   仍然读不到 uid。**Python 侧只需要读 uid**，解密可以只做这一层，或直接复用
   Go 的解密结果；
5. 测试向量必须是**合成**的（同一套密钥 + AAD 口径加密，不是真凭据），并有一条
   对着参考实现逐字节校验 AAD、一条 `keyId` 不匹配必须报错、一条回写后文件里
   不得出现明文 token 且能再解析回来。

**成本与风险**

- 成本：中。密码学口径必须逐字节对齐，AAD 拼错就全解不开；
- 风险：**回写方向比读方向更危险**——一旦用固定清单封字段，会把桌面端要读的
  明文字段加密，用户升级客户端后账号就废了。所以「按读入清单复原」是硬要求，
  不是优化；
- 附带一条独立修复：同一 uid 出现在多个文件时（个人版与企业版 uid 相同、
  客户端还会写 `<name>.<ts>.<pid>.<uuid>.info` 轮转备份），参考仓改为**按 mtime
  升序载入**，最新凭证最后进池。本地 `LoadAuthFiles` 是 `sort.Strings(files)`
  （字典序），陈旧备份可能排在后面胜出。这一条可以单独先做，成本极低。

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

### P1-5　只发 `user_id` 的客户端也按内容派生键粘账号

**能力**

上游前缀缓存按账号隔离（`prompt_cache_key` 掺 uid），换号就是全量 miss。只发
`metadata.user_id` 的客户端被 `ExtractKey` 的 anti-monopoly 闸判成「无会话键」，
`StickyFallbackKey` 又会因为「带 user_id」主动返回空串，于是每轮都回落加权轮换。

参考仓实测（同一份 3.6k body 连发两次）：

| 客户端带什么 | 是否同号 | 第二轮缓存命中 |
|---|---|---|
| `conversation_id` | 是 | 3456/3634（95.1%） |
| 只有 `user_id` | **否**（两个不同号） | 0/3634 |
| 什么都不带 | 是 | 3456/3634（95.1%） |

改法：绑定键改成「`sessKey`，为空时用 `DeriveCacheKey`」——后者是
**system + 首条 user 的内容哈希**，天然是对话级而非用户级：同一用户的并行对话
首条 user 不同则键不同，照旧分散，anti-monopoly 想避免的「一个 user 全钉同一号」
不会发生。

**来源**

- `d165e6e1` `fix(session): 只发 user_id 的客户端也按内容派生键粘账号，前缀缓存不再全量 miss`
- 参考文件：`internal/server/handler.go`、`internal/server/sticky_derived_bind_test.go`

**本地现状**

- `upstream/internal/session/session.go:433` 的 `StickyFallbackKey` 在 body 带
  `user_id` 时**恒返回空串**（有意为之，注释在 `:425-429`）；
- 没有 `DeriveCacheKey`，全仓 0 命中；
- `upstream/internal/server/handler.go:683-685` 用的正是 `StickyFallbackKey`，
  所以「只发 user_id」的客户端确实每轮换号。

**建议动作（只改 Go 上游）**

1. 新增 `session.DeriveCacheKey(body)`：`system` 文本 + 首条 `user` 文本的
   sha256 前 16 字节，与 `StickyFallbackKey` 共用 `firstUserText` / 内容签名；
2. `handler.go` 的 `stickyKey` 取值链改为 `sessKey` → `DeriveCacheKey`，
   **去掉 `StickyFallbackKey` 的 `hasUserID` 抑制**（抑制的初衷是防 user 维度
   过粗，而内容哈希是对话级，不触发该问题）；
3. `StickyFallbackKey` 本身保留：它与 `DeriveCacheKey` 的差别只在是否看 `system`，
   两者可合并成一个函数 + 参数，但合并会改既有键的前缀，**存量绑定会全量失效
   一次**——宁可先并存；
4. 测试：`user_id`-only 请求两轮同号、并行对话不同号、无 `messages` 不伪造键。

**成本与风险**

- 成本：低。一个哈希函数 + 一行取值链；
- 风险：**这是一次流量分布变更**。改完第一批请求会重新洗一遍绑定，前缀缓存
  短期 miss 上升，之后才降。参考仓实测的代价是「每条 8k 前缀从 0.02 credit
  变 0.34」（约 17×）——那是**未命中时**的单价，命中后回到 0.02。要灰度的话
  先只对「body 里既无 conversation_id 也无 prompt_cache_key」的请求生效。

### P1-6　请求级错误不再解绑会话粘性

**能力**

`fail()` 把「释放租约」和「解绑会话」绑在一起，而请求级错误（内容拦截 11151 /
11-128、prompt 超长 11115、图片无效、工具与结果不匹配）**与账号无关**：同一个
body 换任何号都会撞。

解绑之后客户端重试会被分配到另一个号，而缓存键是 `hash(uid+会话)`、上游前缀
缓存按账号隔离，等于十万到几十万 token 的前缀从头重算。上游 `content_blocked`
成片出现时更会连环换号，每一跳都是全量 miss——观感就是「一旦报错，缓存就永远
热不起来」，同时首字延迟从 3~4 秒涨到 10~20 秒。

改法：新增 `failKeepBinding()`，**只释放租约、不解绑**，用在四个请求级分支。
账号级故障（401/403/429 冷却、熔断、连败）仍走 `fail()` 解绑——那些确实该换号。

**来源**

- `2b210e92` `fix(server): 请求级错误不再解绑会话粘性（报错一次缓存就再也不生效）`
- 参考文件：`internal/server/handler.go`、`internal/server/sticky_request_error_test.go`

**本地现状**

- `handler.go` 的 `fail` 定义在 `:747` 附近，语义是
  `releaseHeld()` + `if stickyUID != "" && uid == stickyUID { unbindSticky() }`；
- `fail(acct.UID)` 共 10 处调用（`:931`、`:969`、`:1000`、`:1026`、`:1046`、
  `:1056`、`:1072`、`:1085`、`:1099`、`:1120`），其中请求级分支与账号级分支
  **混在同一套收尾**里，没有区分；
- `ErrUpstreamBusy`（6020/6021）早就是这个语义，但请求级那几类没有跟上。

**建议动作（只改 Go 上游）**

1. 加 `failKeepBinding(uid)`：只 `releaseHeld()`，不动 `Session.Unbind`；
2. 把下列分支从 `fail()` 换成 `failKeepBinding()`：`ErrContentBlocked`、
   `ErrPromptTooLong`、`ErrImageInvalid`、`ErrToolCallMismatch`、
   `ErrEmptyMessage`、`ErrBadParams`（请求体本身畸形的那些）；
3. 账号级分支保持 `fail()`：`ErrSessionDead`、`ErrAuthRejected`、`ErrHardCredit`、
   `ErrSoftRate`、`ErrServer`、`ErrWafBlock`、`ErrAccountFault`；
4. 测试：用假 Store 直接断言绑定表**没被清空**。只看内存 `Resolve` 会因账号
   仍被租用而测不出差异——参考仓第一版就这么漏掉了变异。

**成本与风险**

- 成本：低。一个函数 + 若干调用点替换；
- 风险：请求级错误后不解绑，意味着客户端重试**会回到同一个号**。如果那个号
   恰好也在冷却，就会撞一次冷却再换——多一次往返，但换来的前缀缓存命中远大于
  这一跳成本。真正的账号级故障不受影响（仍解绑）。

**落地状态（2026-11 低风险批）**：✅ **已同步**。新增 `failKeepBinding()`（只
`releaseHeld()`），6 个请求级分支改用它：`ErrContentBlocked`、`ErrPromptTooLong`、
`ErrImageInvalid`、`ErrEmptyMessage`、`ErrToolCallMismatch`、`ErrClient`+11133。
**渠道校验分支（`IsChannelCheck`）仍走 `fail()` 解绑**——它按账号灰度启用、分支
直接返回不轮转，解绑才能换到未灰度号（与参考仓一致，参考仓也只改了 4 处）。新增
`sticky_request_error_test.go`（6 个子用例 + 账号级 429 反例 + 渠道校验反例）；
六处改回 `fail()` 全 RED。来源提交 `2b210e92`。

### P1-7　等上游首帧期间补 SSE 注释帧

**能力**

`StreamHint` 是「首帧否决」设计：上游第一个有效帧到达前一个字节都不写，好处是
首帧就是错误时可以静默换号，代价是客户端从建连起**零字节干等**，等满整个 TTFB。

大上下文把这个空窗拉到秒级：参考仓线上 160–320k 档 TTFB 中位 2.6s / P90 4.8s，
320k+ 档 P90 6.1s；而「零内容 + 客户端断开」的中断全部落在 3.46~4.09s 这个带上。

修法：在 chat 流式路径的轮转循环之前包一层 `ResponseWriter`，等首帧期间按
`server.head_keepalive`（缺省 1500ms）写 SSE **注释帧**（`": "` 开头的 no-op 行）。
首帧一到心跳立刻停手，正常流一条注释都不多写。

三条不变量（都是踩过的坑）：

1. 注释帧与真实写**共用一把锁**，绝不插进写了一半的事件帧中间；
2. 注释帧会提交响应头，所以 4 个 `writeOpenAIError*` 要增加「已提交」分支——
   全号失败时降级成 `200` + error 帧 + `[DONE]`，不再往已提交的 SSE 流里塞 JSON；
3. 注释帧自己补齐 `Content-Type` / `Cache-Control` / `X-Accel-Buffering`，
   否则内容类型为空、反代会缓冲。

**来源**

- `8df99ac3` `fix(server): 等上游首帧期间补 SSE 注释帧，客户端不再干等 3~6 秒`
- 参考文件：`internal/server/headkeepalive.go`、`handler.go`、`cmd/server/config.go`

**本地现状**

- 本地 `handler.go` 的流式路径**没有**任何「已提交响应头」的判定
  （全仓 `WriteHeader` 只在 `writeJSON` 与 `sse.go:498` 出现），也没有注释帧；
- `upstream/internal/upstream/sse.go:498` 只在 `StreamHint` 内部设头，说明响应头
  是等首帧才开始写的——与本项要改的位置一致；
- 没有 `headkeepalive.go`，也没有 `server.head_keepalive` 配置。

**建议动作（只改 Go 上游）**

1. 新增 `internal/server/headkeepalive.go`：包装 `ResponseWriter`，按间隔写
   `": \n\n"`，`WriteHeader` / 首次真实 `Write` 后关闭；
2. 锁要与 `StreamHint` 的写路径共用（`sse.go` 现在没有锁，需要一并加，
   否则注释帧可能插进帧中间）；
3. 配置 `server.head_keepalive`，缺省 1500ms，`0` 关闭；
4. 补「响应头已提交」分支：错误响应在头已发出时降级成 SSE error 帧 + `[DONE]`；
5. 测试：快首帧零注释、慢首帧按间隔出注释、注释不插进帧中间、头已提交后错误
   走 SSE 分支。

**成本与风险**

- 成本：中低。主要是并发正确性，参考仓实测热路径开销可忽略（14k 帧/请求约
  多 0.4ms）；
- 风险：写坏锁会产出交错帧，比干等更糟。**必须**有「注释不插进半帧」的测试；
- 与本地的 4 个 `writeOpenAIError*` 有耦合：不做第 4 步就会在头已提交后写出
  非法 JSON 混流。

### P1-8　出站补位空 text 段 + 11151 归请求级 + reasoning 镜像

**能力**

三件都是「同 body 换任何账号都必然失败」的形态，按同一批实测搬进来：

1. **空 text 段（上游 400 / code 11151）**：上游对每个 text 段判长度，空串 / null /
   缺失三种直接被拒，`"   "` 放行；未识别 type 被忽略，非数组对象不嗅探。更麻烦的
   是「字符串 content 只要去空白后以 `[` 开头，上游就当 parts 数组解析」——工具
   结果里抓到的表单/按钮数组正是这个形状，于是数据被嗅探成一个空文本段。
   修法**只补值不删段**（填一个空格）：删段会改段落数量与下标，而该段本来没有
   内容可给模型看。补位放在 repack / cleanup 之后，因为那两步会把字符串 content
   挪到别的消息上；
2. **11151 归请求级**：补位漏网时按请求级错误 fail-fast 400 透传原文，不轮转、
   不罚号。若退回 `ErrClient`，客户端拿到的是 `503 no_healthy_account`——把必然
   失败的请求报成「账号不可用，稍后重试」；
3. **`reasoning_content` → `reasoning` 镜像**：Kimi / GLM / MiniMax / MiMo / Step /
   Space-Bunny 按 `reasoning` 读历史，客户端只发 `reasoning_content` 时上游看到的
   是「assistant 没思考」。只镜像**已有的非空值**，绝不新建字段或填占位——
   与 `thinking.go` 里 DeepSeek 那份刻意分开（那份是补齐，连空历史都补占位，
   因为上游校验 `len>0`）。

**来源**

- `f987bacc` `fix(upstream): 出站补位空 text 段 + 11151 归请求级；Kimi/GLM 补 reasoning 镜像`
- 参考文件：`internal/upstream/empty_text_part.go`、`reasoning_mirror.go`、
  `payload.go`、`client.go`、`handler.go`

**本地现状**

- `payload.go:260` 有 `msg["content"].([]any)` 的遍历，但**没有**空 text 段补位，
  全仓搜「空 text」0 命中；
- 11151 已经有 `ErrEmptyMessage` 与 fail-fast 分支（`handler.go:1070` 一带），
  这一半**已完成**，不需要重做；
- `reasoning_content` 的镜像：`sse.go:243` 与 `thinking.go` 有 DeepSeek 方向的
  处理，但没有「Kimi/GLM 家族读 `reasoning`」这条镜像。

**建议动作（只改 Go 上游）**

1. 新增 `internal/upstream/empty_text_part.go`：`fillEmptyTextParts(body)`，
   在 repack / cleanup **之后**调用；只处理 `content` 是数组的 assistant /
   user 消息，`type` 为 `text` 且 `text` 为空串 / null / 缺失时填 `" "`；
2. 顺带处理「字符串 content 以 `[` 开头被上游嗅探成数组」：这类字符串不要动
   （它是工具结果原文），只保证嗅探出来的空段被补位；
3. 新增 `internal/upstream/reasoning_mirror.go`：对 Kimi / GLM / MiniMax / MiMo /
   Step / Space-Bunny 家族的 assistant 历史消息，`reasoning` 缺失或为空且
   `reasoning_content` 非空时镜像过去；**不新建字段**；
4. 测试：形态矩阵（空串 / null / 缺失 / 纯空白 / 未识别 type / 非数组）、
   镜像六家族命中 + `gpt` / `qwen` 反例 + 已非空不覆盖 + 空值不造字段。

**成本与风险**

- 成本：低。两个纯函数 + 两处接线；
- 风险：补位值是 `" "` 而不是 `""`——写错成空串等于没修。镜像方向也要小心：
  只在 `reasoning` **缺失或为空**时写，已非空绝不覆盖（客户端可能有意带空）。

**落地状态（2026-11 低风险批）**：✅ **已同步**（部分；11151 归请求级本地此前
已有 `ErrEmptyMessage` + fail-fast 分支，没重做）。新增
`internal/upstream/empty_text_part.go`（`guardEmptyTextParts` / `sniffParts` /
`fillEmptyTextParts` / `partType` / `isEmptyText`）与 `reasoning_mirror.go`
（`readsUpstreamReasoning` / `mirrorUpstreamReasoning` /
`upstreamReasoningModelPrefixes`），都在 `payload.go` 的
`PrepareBodyOptWithEffortsAndDefault` 里接线，位置在 repack / cleanup 之后。

与参考仓的一处**有意偏离**：镜像放在 `sanitize` **之后**（参考仓在之前）。
sanitize 只净化 `reasoning_content`、不看 `reasoning`，先镜像会把未净化的原文
抄进 `reasoning`，等于绕过脱敏；后置则抄的是已净化值。

补位只补值不删段（`" "`），嗅探只在「去前导空白后以 `[` 开头 + 能解析成非空
对象数组」时生效。测试 `empty_text_part_test.go`：形态矩阵 7 组 + 嗅探 1 组 +
不嗅探 5 组 + 零改动 1 组 + 六家族命中 + realm 前缀 + 5 条反例 + 管线级 2 条；
10 条变异里 9 条 RED（「嗅探去掉首字符判据」是等价变异——`json.Unmarshal` 本身
就容忍前导空白，已在注释里写明，不算漏测）。来源提交 `f987bacc`。

### P1-9　上游漏进正文的 `<think:...>` 拆回 thinking 块

**能力**

上游的 vLLM reasoning parser 偶发把整段 CoT 当 `content` 下发：`usage` 里
`reasoning_tokens=0`，正文首尾带模型自己的控制 token `<think:6124c78e>…</think:6124c78e>`
（实测 31 条 assistant 消息里 4 条）。Anthropic 客户端只认
`content_block.type=thinking`，内联标签一律当正文显示——上游 parser 修不了，
网关不该把控制 token 当正文发出去。

参考仓新增 `upstream.ThinkTag`：**只在 content 以 `<think:` 开头时启用**
（正文中间提到这几个字不重写），流式跨片维护半个标签，正文 / 思考按原顺序
各归各类。

**来源**

- `771c887f` `fix(messages): 上游漏进正文的 <think:SUF> 拆回 thinking 块（issue #194）`
- 参考文件：`internal/upstream/thinktag.go`、`internal/server/messages.go`

**本地现状**

- 全仓搜 `think:` 只有 `TrimSuffix` 的无关命中，**没有** ThinkTag 拆分；
- 本地 Go 侧**没有** `/v1/messages`（Anthropic 兼容端点由 Python 侧
  `server/platforms/anthropic.py` 承担）；
- Python 侧 `anthropic.py:390-431` 已能处理结构化 `thinking` / `redacted_thinking`
  块，但不处理「正文里内联的 `<think:...>` 标签」。

**建议动作**

1. 判断落点：本项的触发场景是 **Anthropic 客户端**，而 Go 侧没有 messages 端点，
   所以真正该改的是 **Python `server/platforms/anthropic.py`**——在把
   `content` 转成 `content_block` 时，若文本以 `<think:` 开头则拆成
   `thinking` 块 + 正文块；
2. 若将来 Go 侧要接 `/v1/messages`，再把 `thinktag.go` 搬过去（流式跨片状态机
   是 Go 侧才需要的部分）；
3. 测试：完整标签、跨片半个标签、正文中间提到 `<think:` 不重写、多段拼接。

**成本与风险**

- 成本：低（Python 侧只需在现有块转换处加一层）；
- 风险：判定必须严格到「content **以** `<think:` 开头」。放宽成「包含」会把
  正常讨论 thinking 的正文切开。

### P1-10　输出预算下限（修「高档位思考吃光自己额度」）

**能力**

客户端指定高档位思考、又把 `max_tokens` 给得比这档思考还小时，上游把额度先花在
思考上，正文只剩一半就断在句子中间——用户看到的就是「用着用着中断」。

参考仓实测（`cn:deepseek-v4.1-flash`，同一「写 3000+ 字文档」提示词）：

| 请求 | total | think | 正文 | finish |
|---|---|---|---|---|
| `effort=high` + `max_tokens=2048` | 4096 | 2048 | 2048 | `length` |
| `effort=max` + `max_tokens=2048` | 4096 | 2048 | 2048 | `length` |
| `effort=high` + 不给额度 | 16720 | 2998 | 13722 | `stop` |
| `effort=max` + 不给额度 | 48816 | 37110 | 11706 | `stop` |

客户端不可能知道模型的思考要花多少，它只能拍一个数；这个组合错误只能由网关修。

改法：`max_tokens` 低于「该档思考起码要花的量」时抬到下限（low/minimal 4096、
medium 6144、high 8192、xhigh 12288、max 16384）。**抬的是上限不是目标长度**——
模型写完自然停，不会因为上限变大而多写一个字，也不额外计费。

不动的四种情形：没给显式额度（目录已补 131072）、探活级额度（<16）、关闭思考
（`thinking.type=disabled`）、档位未知。目录 `maxOutputTokens` 更小时以目录封顶。

**来源**

- `2b9b7736` `feat(upstream): 输出预算下限，修掉「高档位思考把自己那份额度吃光」的截断`
- 相关：`34a3ae67`（抬过额度的请求在 `length` 处自动续接）、`e54c6e07`（续写段
  继承输出预算下限）、`29db8786`（续写闸门从「最多 3 段」改成「累计输出上限」）、
  `6e7fe628`（客户端额度 ≥ 32k 时按长文续接）
- 参考文件：`internal/upstream/output_budget.go`、`payload.go`

**本地现状**

- `payload.go` 对 `max_tokens` / `max_completion_tokens` 的处理是「显式给了就优先、
  只删不补」（`:46-123`），**没有下限**；
- 全仓搜 `budget_floor` / `outputFloor` 0 命中；
- 续写（M-P1-3）本地也还没有，所以「续写段继承下限」那部分可以等续写做完再补。

**建议动作（只改 Go 上游）**

1. 新增 `internal/upstream/output_budget.go`：`ensureMinOutputBudget(body, catalogMax)`，
   在 `PrepareBody*` 之后调用；
2. 档位映射表与 `effort_catalog.go` 共用，不要另写一份；
3. 目录 `maxOutputTokens` 更小时以目录封顶（否则会把上限抬到超过模型能力）；
4. 日志记一行「抬额」便于归因，但不要每请求都 WARN（改成 DEBUG 级或采样）；
5. 测试：抬额、四种不猜情形、目录封顶、端到端「抬额度但不改档位」、
   「客户端给了大额度时不动」。

**成本与风险**

- 成本：低。纯请求体重写，无新依赖；
- 风险：抬上限会让**最坏情况**的账单变大（模型真的写满）。参考仓实测
  `effort=max` 不给额度时正文 11706、think 37110，说明模型不会写满上限；
  但仍建议只在「客户端显式给了额度且额度 < 下限」时抬，不要给所有请求抬。

### P1-11　上游断流补截断终态

**能力**

上游既没给 `finish_reason`、也没发 `data: [DONE]` 就 EOF（连接说到一半断了）时，
`StreamHint` 只补一个 `[DONE]` 就当成功结束：chat 客户端看到「内容 + 正常结束」
但 `finish_reason` 为空，`/v1/responses` 适配器更直接回 `response.completed`。
Agent 侧的表现就是「说到一半停了、也不报错」，只能人工接着催。

改法：`StreamHint` 记录 `upstreamDone`，流结束时若
`!upstreamDone && !finishSeen && !toolFinishSent`（且写过有效帧）就补一帧
`finish_reason=length` 并落 WARN。chat 侧看到截断，responses 侧由既有映射转成
`incomplete` + `max_output_tokens`，messages 侧转 `stop_reason=max_tokens`——
三边共用这一处接缝。

**来源**

- `84a48d9e` `fix(upstream): 上游断流补截断终态，不再伪装成正常收尾`
- 参考文件：`internal/upstream/sse.go`

**本地现状**

- `sse.go:611` 已有截断判定
  `truncated := validFrames > 0 && !sawDone && !sawFinish`；
- 但**只**在 `[DONE]` 之前插了一帧 error
  （`code: "upstream_truncated"`），**没有补 `finish_reason=length`**——
  客户端如果只读 `finish_reason`（很多 SDK 都是）仍会当成正常结束；
- `Aggregate`（`:257`）在 `finishReason == "length" || !sawDone` 时会把
  `finish_reason` 改成 `length`，所以**非流式已经是正确语义，流式不是**。

**建议动作（只改 Go 上游）**

1. 在截断分支里，除了现有 error 帧，再补一帧带 `finish_reason=length` 的
   `choices[0]`（`delta` 为空）；
2. **只在一个终态都没发过时补**——补两帧会让后一个 wash 掉前一个；
3. 保留现有 error 帧（客户端优先识别 error 时仍能判失败），两者语义不冲突：
   error 帧给「这是故障」，`finish_reason` 帧给「内容被截断了」；
4. 与 `Aggregate` 的口径对齐，抽一个共用的 `truncationFinishReason` 常量；
5. 测试：断流必补且恰好一帧、正常收尾不得多补、变异把取值从 `length` 改成
   `stop` 时用例必须变红。

**成本与风险**

- 成本：低。流式收尾处加一帧；
- 风险：**多补一帧会污染正常流**。判据必须严格（`!sawDone && !sawFinish`），
  且要在 `flushTail()` 之后、`[DONE]` 之前。

**落地状态（2026-11 低风险批）**：✅ **已同步**。新增导出常量
`TruncationFinishReason = "length"`；`StreamHint` 的截断分支补
`finish_reason=length` 帧（在本地 error 帧之前、`[DONE]` 之前）；`Aggregate`
补 `sawFinish` 并在 `!sawDone && !sawFinish` 时改写 `finishReason`（本地此前
非流式已在 `finishReason == "length" || !sawDone` 时丢掉残缺 tool_calls，但
`finish_reason` 仍下发默认 `stop`）。新增 `sse_truncation_test.go` 6 条用例；
四条变异（流式不补帧 / 取值改 stop / 聚合去掉改写 / 判据放宽）全 RED。
来源提交 `84a48d9e`。

### P1-12　11134 供应商过载不罚号 / 不换号并保留会话粘性

**能力**

`11134`（`the model provider is temporarily unavailable, please retry later or
switch to another model`）是**模型供应商侧过载**，与账号健康无关（参考仓实测：
15 分钟内跨 6 个不同账号同时返回）。它落 `status>=500` → `ErrServer` 会走两条
错误的路：

- `applyErrorPolicy` 的 `ErrServer` 分支 `NoteError` → 熔断（threshold 3 →
  30m 冷却），高峰期把整片健康号打进冷却；
- 请求内换号重试撞的是同一个 provider，白耗在途名额；而换号会打断会话粘性，
  上游 prompt cache 按账号隔离，同号 97–99% vs 换号 0–12%。

三处改动：`Classify` 认结构化 `code:11134` → `ErrUpstreamBusy`（与 6020/6021
同族）；`parseQueueRetryAfter` 增加 `retry_policy.retry_after`；`ErrUpstreamBusy`
分支的 `fail(uid)` 改成 `releaseHeld()`（只释放在途租约，不解绑粘性）。末端仍是
`429` + `Retry-After`，上限 600s。

**来源**

- `48306862` `fix(upstream): 11134 供应商过载不再罚号/换号，并保留会话粘性（issue #188）`
- 参考文件：`internal/upstream/client.go`、`internal/server/handler.go`

**本地现状**

- 全仓搜 `11134` **0 命中**，`ErrUpstreamBusy` 也不存在；
- 已有 `ErrUpstreamQueue`（6020/6021 排队）与 `RetryAfter` 解析
  （`client.go:161`、`:454-460`），语义可复用；
- 11134 目前会落 `ErrServer`，即**确实在罚号**。

**建议动作（只改 Go 上游）**

1. `client.go` 的 `ErrKind` 增加 `ErrUpstreamBusy`（或直接复用 `ErrUpstreamQueue`
   并把名字改中性——本地已经有 `ErrUpstreamQueue`，加新枚举会让 `String()` 与
   面板文案都多一份）；
2. `Classify` 只认**结构化 `code:11134`**，不认「temporarily unavailable」文案——
   没有业务码的 5xx 仍是 `ErrServer`；
3. `parseQueueRetryAfter` 增加 `retry_policy.retry_after`（与 6020/6021 的
   `data.retry_after` 并列），头侧 `Retry-After` 仍优先；
4. 该分支的收尾改成只释放租约、不解绑（与 M-P1-6 同一套 `failKeepBinding`）；
5. 测试：端到端「只打粘性号一次、`429` + `Retry-After:8`、上游原文透传、
   绑定保留、账号零惩罚」。

**成本与风险**

- 成本：低。分类规则 + 一个分支；
- 风险：`retry_after` 的单位未能确认（参考仓按秒解释，日志 body 截断在 200 字符）。
  实现时按值域判断（>1000 视为毫秒）更稳；
- 与 M-P1-6 有依赖：`failKeepBinding` 先落地，这项才能直接复用。

**落地状态（2026-11 低风险批）**：✅ **已同步**（有意偏离见下）。不新增枚举，
把本地 `ErrUpstreamQueue` **全局改名 `ErrUpstreamBusy`**（`String()` 也从
`upstream_queue` 改为 `upstream_busy`），语义扩成「上游容量信号」三形态：6020
排队 / 6021 队列满 / 11134 供应商过载。`Classify` 只认结构化
`hasBusinessCode(body,"11134")`（无业务码的 5xx 仍是 `ErrServer`）；新增
`parseQueueRetryAfter`（认 `data.retry_after` 与 `retry_policy.retry_after`，超
`retryAfterSanity`=2h 封顶）；`IsUpstreamQueue` 补 6021（`queue.full` +
`hasBusinessCode("6021")`）；handler 新增 `ErrUpstreamBusy` 专用分支用
`failKeepBinding()`，且 `RetryAfter>0` 时 `break` 不轮转（无口径时保留轮转，撞
间歇空窗）；末端映射改 `429` + code `upstream_busy` + `Retry-After`（向上取整、
封顶 `maxRetryAfterSec`=600、无口径兜底 5s）。测试：
`queue_retry_after_test.go`（9 子用例 + 数字字段不误伤）、`upstream_busy_test.go`
（4 条端到端）；7 条变异全 RED。

有意偏离参考仓：`doJSON`（refresh/report/travel 共用）**不改** 429 口径
（`refreshRetryAfter` 有 300s 下限），只在 chat 流路径接 `parseQueueRetryAfter`。
`retry_after` 单位按秒解释（与参考仓一致），未加毫秒猜测。来源提交 `48306862`。

### P1-13　签到回执四态（`checked` / `already` / `pending` / `failed`）

**能力**

重复点签到，面板每次都弹「签到成功 (+100 积分)」，上游其实一个积分都没给。
参考仓在生产 1288 条签到记录里发现 250 条是幂等拒绝被当成新签到——根因是只按
code 分支判定、只看 `errors`，前端再拿一个布尔拼死字符串。

参考仓的本地实测（不是推断）：

```
首次  HTTP 200 {"credit":100,"streak_days":44,"is_streak_day":false}
重复  HTTP 400 {"code":10001,"msg":"今天已签到，请明天再来","requestId":"…"}
```

判定层本来就是对的（`IsAlreadyCheckin` 按文案匹配，不卡 code），病在**回执被丢掉**：
`DailyCheckin` 只有 `error`、没有回执，于是 handler 只能把 `already` 和 `checked`
归成同一个 `checkin_done=true`，前端又只认这个布尔——两种情况都显示绿色
「签到已完成」，上游那句「今天已签到」被吞掉。

参考仓新增 `CheckinReceipt` 四态，判定顺序 `pending → failed → already → checked`。
「失败」是硬词，必须压过 `already`；积分只取回执里真实带回的 `credit`，没带回时
`CreditKnown=false`，调用方不得显示数字（既不显示 0，也不硬编码 100）。

**来源**

- `8125831d` `fix(panel): 重复签到不再报「签到已完成」，按回执分四态出文案`
- 参考文件：`internal/upstream/checkin_receipt.go`、`client.go`、`report.go`

**本地现状**

- `upstream/internal/scheduler/scheduler.go:126-129` 已有 `CheckinStatus` 四态
  （`ok` / `already` / `fail` / `skipped`），但**没有「上游回执」这一层**：
  `DailyCheckin`（`client.go:2170`）只返回 `error`；
- 所以 `checkinAll` 只能靠 `IsAlreadyCheckin(err)` 二分（`:517`），
  `oc.Detail` 直接塞 `err.Error()`——面板看到的是
  `upstream client (http 400): {"code":10001,…}` 整段原文；
- Python 侧 `server/services/dailyplan.py:266-278` 已经有 `ok_n / already / failed`
  三计数，但同样拿不到「上游回执里的真实积分」；
- 有一个真错判：`签到失败，已签到天数不足 7 天` 同时含「已签到」，旧逻辑会把它
  当成幂等成功置 `checkin_done`，面板显示「今日已签」，其实是失败。

**建议动作**

1. Go：`DailyCheckin` 改为返回 `(receipt, error)`，或新增 `DailyCheckinReceipt`；
   四态判定放进 `upstream` 包，顺序 `pending → failed → already → checked`；
2. Go：`scheduler.CheckinStatus` 与上游四态**不要混用**——调度状态是「这一轮
   要不要再试」，回执是「上游到底给了什么」。两者可以映射，但不能合并；
3. Go：`oc.Credit` 只在 `CreditKnown=true` 时填，面板不得在未知时显示数字；
4. Python：`dailyplan.py` 的「成功 N 个」计数改成按回执四态分别计数——现在
   「HTTP 成功」也会把 `already` 算进成功（签到端点对失败也回 200）；
5. 测试：四态各一条 + 「失败文案含已签到」必须判 failed + `code=0` 但文案是
   「今天已签到」判 already。

**成本与风险**

- 成本：中低。要改 `DailyCheckin` 的签名，调用点集中在 scheduler；
- 风险：判定顺序不能错——`failed` 必须先于 `already`，否则「签到失败，已签到
  天数不足 7 天」会被误判成功。这是本项的核心，不是细节；
- 与 M-P0-2（分钟级补签）有交集：补签结果也要写进同一份回执。

### P1-14　清洗上游注入的身份指令

**能力**

上游服务端会往模型可见上下文里拼一段 `model_identity` 注入块（不出站、不落盘，
所以 dump 里看不到），模型在 `reasoning_content` 里复述原文，客户端把这段思维链
带回历史，下一轮就原样进出站——「声明 → 进历史 → 再看到 → 再声明」，越滚越频繁。

网关侧能做且唯一有效的是**掐断回流**。参考仓补了两条规则：

1. 收紧到整句的
   `When asked about your <…> identity, identify yourself as <名字>.` → 中性表述。
   单独匹配 `identify yourself as` 会误伤合法指令（`Do not identify yourself as
   an AI.` 是正常要求），所以要求「identity, identify yourself as」连写才命中；
2. 去掉 `<model_identity>` 标签本身、保留内部文本——标签留着会让模型继续把它当
   「有个身份块」每轮重提。

两条规则**挂在通用门控**上，而不是 identity 门控：泄漏出现在 assistant 消息里，
而 identity 门控只对 `system` / `developer` 生效，挂错门控等于规则永远不执行。

**来源**

- `8f9fa3c5` `fix(upstream): 清洗上游注入的身份指令 + 去掉全球号「等签到恢复」的错误承诺`
- 参考文件：`internal/upstream/sanitize.go`、`hint.go`

**本地现状**

- `upstream/internal/upstream/sanitize.go` 有 `sanitizeText`（`:167`）与
  `hasFingerprint`（`:213`），清洗覆盖 `content` / `reasoning_content` / `reasoning`；
- 但**没有** `sanitizeInjectedIdentityRe` / `sanitizeModelIdentityTagRe`，
  全仓搜 `identify yourself` 与 `model_identity` 0 命中；
- 顺带一条：`ErrHardCredit` 的 `gateway_hint` 文案写「等签到恢复」，对国际版账号
  是永远不会兑现的承诺（国际版没有签到活动），应改 realm 中立表述。

**建议动作（只改 Go 上游）**

1. 在 `sanitize.go` 加两条规则，挂在 `sanitizeText` 的通用路径上（不是
   identity 门控）；
2. 清洗顺序不用改：`payload.go` 里 `mirrorUpstreamReasoning` 在 `sanitizeMessages`
   之前，而清洗覆盖三个字段，所以镜像出来的那份也一起被洗掉；
3. `hint.go` 里 `ErrHardCredit` 的文案改成 realm 中立（例如「今日额度已用尽，
   等待上游恢复」），不要承诺签到；
4. 测试：整句命中、夹在推理文本里的复述形态（含标签剥离、无关内容不动）、
   误伤防护（`Do not identify yourself as an AI.` 必须不动）。

**成本与风险**

- 成本：低。两条正则 + 一处文案；
- 风险：正则写宽会误伤正常 system prompt。参考仓的「连写」约束是关键，
  不要简化成单个短语。

**落地状态（2026-11 低风险批）**：✅ **已同步**。`sanitize.go` 加
`sanitizeInjectedIdentityRe`（`when asked about your … identity, identify yourself
as …` 整句，`{0,60}` 两段宽松上限 + `[^.\n]` 防跨句）+ `sanitizeModelIdentityTagRe`
（`</?model_identity\s*/?>`，含自闭合变体）；两条规则挂在 `sanitizeText` 的通用
路径上，并把 `identify yourself as` / `model_identity` 补进 `sanitizeFeatures`
预检（漏了预检等于规则永远不执行）。`hint.go` 的 `ErrHardCredit` 改成 realm 中立：
CN 号走次日签到、global 号需充值，不再承诺「等签到恢复」。

与上文建议的第 2 条**有意偏离**：`mirrorUpstreamReasoning` 现在排在
`sanitizeMessages` **之后**（见 M-P1-8 的落地状态），所以镜像出来的是**已净化**
值，不是原文。原方案「镜像在前、靠 sanitize 一并洗掉」依赖 sanitize 也清洗
`reasoning` 字段，而本地 `sanitizeMessages` 只洗 `content` / `reasoning_content` /
`tool_calls`——照原顺序会把未净化原文抄进 `reasoning`，等于绕过脱敏。

`TestSanitizeLiteralByteExact` 快照同步更新（features 9→11 项、正则 3→5 个）。
新增 `sanitize_identity_test.go` 6 条用例（整句命中 / 大写变体 / 4 条误伤反例 /
标签剥离 / 标签变体 / assistant reasoning 载体）；8 条变异全 RED。来源提交
`8f9fa3c5`。

### P1-15　`/v1/models` 透出 `promo_factor` / `promo_credits` / `promo_label`

**能力**

`credits` 是上游**牌价**倍率，`promo_*` 才是当前生效折扣（WorkBuddy 客户端显示的
就是生效价）。面板的 `/panel/api/models` 一直有这三个字段，但 OpenAI 兼容端点
没有——第三方客户端 / 启动器只读 `/v1/models`，于是限时免费的模型显示成收费，
或者显示旧牌价。

口径：`PromoFactor` 非 nil（上游给了可算的折扣系数）→ 写 `promo_factor` +
`promo_credits`；`PromoLabel` 非空 → 写 `promo_label`（「错峰使用」这类只有标签
没有系数的也透出）；都没有 → 一个都不写，不编造「原价」。

`promo_factor` 用**非 nil 判断**而不是 `!= 0`：`0` 恰恰是「限时免费」这个最该被
看到的取值，用零值判断会把它省略掉。

**来源**

- `b64773e8` `feat(server): /v1/models 透出 promo_factor / promo_credits / promo_label (#203)`
- 参考文件：`internal/server/handler.go`、`models_promo_test.go`、`billing_summary.go`

**本地现状**

- `handler.go` 的 `applyModelInfoFields`（`:399`）写 `credits` / `tags` /
  `vendor` / `is_default` 等字段，**没有** `promo_*`；
- `upstream.ModelInfo`（`client.go` 的 `type ModelInfo`）没有 `PromoFactor` /
  `PromoCredits` / `PromoLabel` 三个字段——解析层就没读；
- Python 侧 `/panel/api/models` 有这三个字段，但 Go 侧 `/v1/models` 没有。

**建议动作（只改 Go 上游）**

1. `ModelInfo` 增加三个字段，在 `FetchModels` / 动态目录解析处填充
   （`promoFactor` 用 `*float64`，才能区分「没有」与「0」）；
2. `applyModelInfoFields` 按上面的口径写三个键；
3. 测试：限时免费（`factor=0`）必须能被读出来、只有标签的优惠照常透出、
   无优惠时三个字段都不出现。

**成本与风险**

- 成本：低。解析 + 输出各一处；
- 风险：`promo_factor` 用零值判断会把「免费」吞掉——这是本项唯一容易做错的地方。

**落地状态（2026-11 低风险批）**：✅ **已同步**。新增
`upstream/internal/upstream/promo.go`（参考仓把 promo 塞在 `client.go`，本地拆成
独立文件）：

- `v3ModelPromotion` 用具名子结构 `v3PromoBadge` / `v3PromoDiscount` /
  `v3PromoWindow` / `v3PromoSchedule`，不照抄匿名嵌套——匿名结构体在测试里没法
  复用（构造一条带窗口的优惠要重复写三层类型字面量）；
- `promoActive` 的时段判定用本地 `internal/cst` 的 `cst.Loc`，不新建
  `promoZone`（`time.FixedZone`）。`cst` 已经承载「上游恒 UTC+8」这一事实，
  再开一个同义常量就是双表示；`TestPromoActiveCSTNotHostZone` 用 UTC 入参锁定
  「结论只取决于 CST，不取决于宿主时区」；
- 挂载点选在 `parseGlobalModelNames` 末尾（`applyPromotionsFromRaw`），而不是
  `fetchV3Models`。CN `FetchModels` 与 global 探测共用这条解析、响应同构，两处
  各写一遍必然漂移；
- `applyModelInfoFields` 按「`PromoFactor` 非 nil → 写 `promo_factor` +
  `promo_credits`；`PromoLabel` 非空 → 写 `promo_label`」输出，牌价 `credits`
  原样保留（启动器要拿它划掉显示）。

**有意不移植**：参考仓的 `PromoNote`（hover 文案，只有面板悬浮提示用得上）与
`applyModelTiers` / `ModelTier`（本地没有「官方标识文案」这个能力，属于
M-P2-10 的另一条线）。

测试：`promo_test.go` 8 条（时段矩阵 13 子用例、CST 非宿主时区、priority 取高、
`factor=0` 非 nil、badge-only、目录外不挂、坏 JSON/窄表不拖垮解析、目录解析挂载）
+ `models_promo_test.go` 4 条（三字段透出 / 只有标签 / 无优惠全省略 / 端到端）。
11 条变异全 RED。来源提交 `b64773e8`（部分同步）。

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

### P2-10　免费号事前不派「免费调不通」的模型（`modelTiers accessMode`）

**来源**：`7a48d72e`；参考文件 `internal/pool/pick.go`、`pool.go`、
`upstream/billing_summary.go`、`client.go`。

**能力**：此前只在撞上 11158 之后做确定性负缓存，是事后补救；目录里的
`modelTiers` 本来就说明了哪些模型免费号根本调不通。判据只认
`accessMode=exclusive`——CN 那条「订阅优先」同样带 badge 文案但没有这个字段，
只是排队靠后，混成同一判据会误拒能用的模型。付费状态取自 billing summary 的
`IsPaidUser`（该字段一直在解析却被丢掉，现在顺手记在 `upstream`，零额外请求）。

三条安全默认：没观测过付费状态 → 放行；池里只剩已知免费的号 → 放行（宁可撞一次
11158，也不能说无号可用）；粘性绑定不拦，靠 11158 负缓存换号。

**本地现状**：全仓搜 `accessMode` / `modelTiers` / `11158` **0 命中**；
`ModelInfo` 不解析 `modelTiers`；billing summary 也没有 `IsPaidUser`
（本地只有 `ResourceSummary`，见 G-P1 的 global 余额一项）。

**建议**：**依赖两件事先做**——`ModelInfo` 解析 `modelTiers.accessMode`，
以及 billing summary 解析 `IsPaidUser`（后者与 M-B-7 的 global 余额三件套
同源）。两者都有之后，选号只需加一条 `exclusive` 过滤。没有付费状态时一律放行，
不要用「没数据 → 当作免费」的反向默认。

**成本/风险**：中。依赖两个解析层改动；判据写宽（把「订阅优先」也算进去）会
误拒能用模型，这是参考仓专门记录过的教训。

### P2-11　运营活动批量读进度 + 一键领奖（`campaign`）

**来源**：`0fadf989`、`1c007bb0`；参考文件 `internal/upstream/campaign.go`、
`internal/panel/campaign.go`、`docs/campaign-probe.md`。

**能力**：WorkBuddy × 星巴克活动（2026-11-12 ~ 11-16）每个号能拿 300 积分 +
1 张券，但要在小程序里逐号点三件事。参考仓先花了一整轮做**证伪**：六条路径
（mp 埋点、真实 chat、web agent 会话、小程序口径会话、origin 变体、真实
conversationId 埋点）都在服务端造出了「看起来像真对话」的证据，但
`ai_chat.progress` 全部保持 0——**任务判据依赖小程序客户端侧的埋点/设备指纹，
服务端无法伪造**。

所以只做了两件能做的：`GET /panel/api/campaign` 逐号读进度（3 并发），
`POST /panel/api/campaign/claim` 对 `ready` 的任务先 `viewed` 再 `claim`，
`dry_run` 缺省 `true`。

**本地现状**：Python 侧没有 campaign 模块（搜 `campaign` 只有无关的
「活动」字样的日志文案）；Go 侧也没有。

**建议**：**先判断活动是否还值得做**（2026-11-16 结束）。如果要做，只需要
「读进度 + 领奖」这两步，**不要**尝试自动完成任务——参考仓已经用六条证伪证明
服务端做不到。`activity_code` 要参数化（这期是 `campaign_starbucks_2026`，
模块名是 `campaign`，不是 `coffee_festival_`）。

**成本/风险**：中。接口本身不复杂，但活动有期限，做完可能已经结束；
`claim` 是写操作，`dry_run` 默认值必须保留。

### P2-12　运行日志落盘 + 导出、上游劣化滑窗告警、选号策略可配

**来源**：`18a67761`；参考文件 `internal/panel/logstore.go`、`logexport.go`、
`internal/server/upstreamhealth.go`、`internal/pool/strategy.go`。

**能力**（三件独立的事）：

1. **运行日志按天落盘**：三频道（chat/task/sys）统一按 CST 自然日写
   `panel-YYYY-MM-DD.jsonl`，单文件超上限轮转 `.2/.3`（不覆盖）；启动时从最近
   文件回填「最近 N 条」；保留 7 天 / 上限 50 MiB 可配；`GET /panel/api/logs/export`
   出 ZIP（先落临时文件再回传——zip 目录在尾部，边写边发会给出「看起来完整」的
   坏包；区间无数据回 404 而不是空 zip）；
2. **上游劣化滑窗告警**：网关只在请求失败时说话，上游慢到不可用时请求仍回 200、
   流水每行 success。参考仓实测 10 分钟 TTFB 中位 17.8s、最大 39.2s，6 条请求
   全部 success、零 WARN，客户端在自己 60s 超时里连撞后停摆。改法是在
   `chatStat.done()` 采样，5 分钟滑窗：TTFB ≥8s 占比 ≥30% 或 5xx ≥10% 且 ≥3 条时
   聚合打一条 WARN，10 分钟内不重复；`499` / `interrupted` 一律丢弃（那是调用方
   走了，混进来用户点个停止就能触发告警）；
3. **选号策略可配**：`weighted`（缺省）/ `round_robin` / `sequential`，
   `pool.selection_strategy`，热生效。边界：策略只在**已过全部可用性判定**的
   候选里决定「选哪个」——健康、冷却、在途上限、积分保底、成本分层、账号优先级
   全部先于它生效；切到 `sequential` 也烧不穿 `credit_floor`。游标按
   `(realm, 模型)` 分桶并带策略标签，在 `candsAll` 上选（不受 top5 截断）。

**本地现状**：

- 日志：Go 侧 `internal/server` 有 `chatStat` 但**没有按天落盘**；Python 侧有
  `server/services/tasklog.py` / `accountlog.py` 各自落盘，**没有统一的三频道
  JSONL + ZIP 导出**；
- 劣化告警：`server/services/watchdog.py` 只监控页面改版与补偿领取，
  **没有 TTFB 滑窗**；Go 侧也没有；
- 选号：`pool/pick.go` 只有加权随机，**没有策略配置**。

**建议**：

- 第 1 项**写进 Python 侧**（日志本来就归面板管），Go 不需要第二套 logstore；
  导出必须「先落盘再回传」；
- 第 2 项也**写进 Python watchdog**（与 G-P2-8 的模型目录指纹监控共用同一套
  告警抑制），不要在 Go 里加 goroutine；
- 第 3 项是**唯一该进 Go 的**：`pool/pick.go` 加策略枚举 + 游标，缺省 `weighted`
  保持现状。这一项会改流量分布，属于第二批（需灰度）的同类。

**成本/风险**：第 1、2 项低；第 3 项中低但要灰度。`sequential` 的语义容易
误解——它不绕过任何可用性判定，只是「在可用候选里按顺序选」。

### P2-13　账号目录热重载的护栏（本地已有轮询，缺四条保护）

**来源**：`9e5c2cda`、`c54c5176`、`4f6adc0e`、`a439202a`；
参考文件 `internal/panel/reload.go`、`internal/pool/pool.go`、`internal/auth/auth.go`。

**能力**：参考仓给「auths/ 增删改不必重启」补了四条防「踢号」护栏：

1. 本轮**任何文件解析失败 → 只增不删**。半写文件（`SaveAtomic` 瞬间）会被当成
   「文件已删」，而剔除结果会持久化，下次启动也不复活；
2. 删除要连续 N 次扫描确认（默认 2）；
3. 扫描前后各取一次目录指纹，变动则重扫一次再判；
4. 剔除前再 `stat` 一次该账号的文件（文件在 = 不是删除），且**在途请求未结束
   不删**。

轮询判据用**内容指纹**（`uid|expiresAt|realm` 的有序哈希）而不是 mtime：
`LoadDir` 的 realm 回填会自己改 mtime，按 mtime 判断会自激。

`c54c5176` 补的是串行化：`accountRemove` / `addAPIKey` / `loginPoll` 与重载
共用一个 `reloadMu`，锁序 `proxyAccountMu → reloadMu → pool.mu`。

**本地现状**：

- `upstream/internal/pool/watch.go:39` 已有 `StartAuthDirWatch`，并在
  `cmd/server/main.go:81` 装配——**热重载本身已覆盖**；
- 但**没有**上面四条护栏：`reloadAuthDir`（`:86`）直接 `SyncToDir`，
  `SyncToDir` 会删掉「这一轮没看到的 uid」；`dirFingerprint`（`:111`）用的是
  **文件名 + mtime + size**，不是内容指纹；
- 没有 `reloadMu`，面板写入与重载扫描没有串行化（Python 面板是另一个进程，
  这条在本地本来也不成立——面板写入是跨进程的）。

**建议**：**只补护栏 1 和 4**，这两条对应真实的「半写文件被当成删除」风险：

1. `reloadAuthDir` 里，若 `auth.LoadDir` 返回的解析失败数 > 0，本轮**只增不删**
   （`LoadDir` 目前只打 WARN 不回报失败数，需要加一个 `LoadDirReport` 或让
   `SyncToDir` 接受「本轮禁止删除」标志）；
2. 剔除前检查该 uid 是否还有在途请求（`pool.inFlight`），有则跳过本轮删除；
3. 护栏 2 / 3 与内容指纹可以不做——本地轮询间隔 10s、目录只有几十个文件，
   重复扫描的成本与收益不匹配；而且面板写入是跨进程，`reloadMu` 在 Go 侧
   拿不到面板的锁，做了也是假串行化。

**成本/风险**：低。改动集中在 `pool/watch.go` 与 `auth.LoadDir` 的返回值。
风险是「只增不删」会让坏文件永久滞留——需要一条手动「强制重扫」入口。

### P2-14　同一 uid 多文件时按 mtime 最新胜出

**来源**：`97d3f259`（与 M-P0-4 同批）；参考文件 `internal/auth/auth.go`。

**能力**：同一 uid 出现在多个文件时（个人版与企业版 uid 相同、客户端还会写
`<name>.<ts>.<pid>.<uuid>.info` 轮转备份），池里只有一个槽位、后载入者胜出。
参考仓原来「后载入」= glob 字典序，陈旧备份可能排在后面胜出，表现为那个账号
token 失效。改为**按 mtime 升序载入**，最新凭证最后进池。

**本地现状**：`internal/auth/auth.go` 的 `LoadAuthFiles` 是 `sort.Strings(files)`
（字典序）；`LoadDir` 有重复 UID 的 WARN（`:454`），但胜出规则仍是字典序。
Python 侧 `server/services/tencent.py:475` 是 `sorted(base.glob(...))`，同样字典序。

**建议**：Go 侧改成按 `os.Stat().ModTime()` 升序，字典序作为 tie-break；
Python 侧同样处理。**这是本清单里成本最低的一项**，可以单独先做。

**成本/风险**：极低。一个排序比较函数。

**落地状态（2026-11 低风险批）**：✅ **已同步**（Go 侧）。`LoadAuthFiles` 改为
`sortAuthFilesByModTime`（mtime 升序，mtime 相同或 `stat` 失败退回字典序
tie-break）；`LoadDir` 本身不去重，去重在 `pool.SyncToDir`（后载入者覆盖），
测试断言点是「最后一个是最新」。新增 `TestLoadAuthFilesSortsByModTime`、
`TestLoadDirNewestCredentialWins`，原 `TestAuthFileGlobUnification` 的排序断言
改为集合断言。变异回 `sort.Strings` 两条用例全 RED。Python 侧
`server/services/tencent.py` 的同类排序**未动**（部分同步）。来源提交 `97d3f259`。

### P2-15　`buddyapp_enter_click` 的 `isFirstPage` 改布尔

**来源**：`8818458b`、`cf16478d`（merge PR #206）；参考文件 `internal/upstream/desktop.go`。

**能力**：任务 `Buddy_App` 一直显示 0/1 完成不了，根因是 `isFirstPage` 发的是
字符串 `"1"` / `"0"`，上游按布尔解析。参考仓改成布尔后任务能正常完成。

**本地现状**：`upstream/scripts/task_runner.py:718` 发 `"isFirstPage": "1"`，
`:1563` 发 `"isFirstPage": "0"`——**同样的 bug 在本地存在**。

**建议**：改两处为 `True` / `False`。这是**一行修复**，收益是 `Buddy_App` 任务
能点亮。

**成本/风险**：极低。唯一风险是上游若对某些号仍接受字符串——布尔是更符合
命名的形态，且参考仓已实测有效。

**落地状态（2026-11 低风险批）**：✅ **已同步**。`task_runner.py` 两处改
`True` / `False`，并把降级单发事件抽成 `desktop_buddy_enter_single()`（便于测试
锁类型）。新增 `server/tests/test_task_runner_buddyapp_is_first_page.py`（3 条
用例，直接 `importlib` 加载真实脚本，不复制事件构造）；两处改回字符串全 RED。
来源提交 `8818458b`。

### P2-16　个人资料接口空昵称不再静默成功

**来源**：`d78131ba`；参考文件 `internal/upstream/profile.go`。

**能力**：资料接口返回空昵称时，参考仓不再当成成功（否则面板永远显示空昵称、
用户以为同步成功），并兼容「顶层平铺响应」（不带 `data` 信封）。

**本地现状**：Go 侧**没有** `/console/account` 调用（与 M-B-11 同一个缺口，
昵称只来自导入文件）；Python 侧也没有。

**建议**：与 M-B-11（账号昵称同步）合并做，不要单独做。做的时候带上这两条：
空昵称判失败、兼容顶层平铺。

**成本/风险**：低。但依赖 M-B-11 先落地。

### P2-17　思考完直接收尾时救援一次 / 说一句就停的救援链

**来源**：`07f8ae03`、`c2003af1`、`1037397c`、`2e30d7d3`、`910d8517`、
`baf9155d`、`79ba3d62`、`92dadae3`、`4bdbd607`、`478d2aa8`；
参考文件 `internal/server/continue.go`、`narration.go`、`toolonly.go`。

**能力**：这一串提交都在修同一个体验问题——模型「说一句就停」或「思考完直接
收尾」，客户端看到空回复或半截回复。参考仓逐步补了：

- 思考吃光预算时自动续写一次（`562d0d88`）；
- 正文停在预告句上就收尾时续写一次（`c2003af1`），正文上限从 240B 放宽到 2000B
  （`910d8517`）；
- 工具轮里「说一句就停」时试续一次，只采纳工具调用（`1037397c`）；
- 两道闸门：正文 ≥120B 且以句末标点收尾 → 视为最终答复不救；续写段的工具调用
  与「请求体里最后一个带 `tool_calls` 的 assistant 消息」同名同参 → 判为重发，
  整段丢弃（`baf9155d`）；
- DSML 标记按完整性分流：完整块不续写、截断块必须续写补完（`92dadae3`）；
- 等用户拍板的收尾不再救援、narration 加长度闸门（`79ba3d62`）；
- 续写闸门从「最多 3 段」改成「累计输出上限」（`29db8786`）。

**本地现状**：本地 Go 侧**没有** `continue.go` / `narration.go` / `toolonly.go`
（全仓搜 `rescue` / `narration` / `toolonly` 0 命中）；只有
`reasoning_fallback.go` 的「整轮无正文时把思考当正文」兜底。

**建议**：**这一整串与 M-P1-3（截断自动续写）是同一套机制**，不要分开做。
按参考仓的演进顺序实施反而更稳：

1. 先做续写状态机（M-P1-3）；
2. 再加「说一句就停」的救援触发（`1037397c`）与两道闸门（`baf9155d`）——
   **闸门必须与触发同时上线**，参考仓实测「29 次试续、29 次采纳、0 次回滚」
   说明没有闸门的救援事实上在给每个 agent 回合追加一次工具调用；
3. 最后才做 narration 长度闸门与 DSML 完整性分流。

**成本/风险**：高。这是本清单里最复杂的一块，涉及帧级拼接、工具调用去重、
输出预算继承。没有真实环境验证不要上线——参考仓的每一条闸门都是从线上误救
样本反推出来的。

### P2-18　`WB2API_PPROF` 独立 pprof 监听

**来源**：`b42712c5`；参考文件 `cmd/server/main.go`。

**能力**：通过环境变量打开一个**独立**的 pprof 监听（不是挂在主 mux 上），
默认关闭。

**本地现状**：全仓搜 `pprof` **0 命中**；主 mux 也没有 `/debug/pprof`。

**建议**：如果需要线上 profiling，按参考仓做法加**独立监听**（`127.0.0.1:6060`
之类的单独端口），不要挂到主 mux——挂上去等于给所有能访问网关的人一个
CPU/内存 profile 接口。默认关闭，用环境变量打开。

**成本/风险**：极低。但要记住「独立端口 + 默认关闭」这两条，缺一条就是信息泄露。

### P2-19　代理出口归属记录（`proxypool`）

**来源**：`3d0f293e`、`73667400`、`bfdba519`、`592fb01a`、`65e644eb`；
参考文件 `internal/proxypool/*`。

**能力**：记下每个出口的归属（国家 / 住宅或机房 / ISP），供选号时按出口质量
排序；以及订阅里 `proxies` 的流式数组解析、直连传输层失败记到出口而不是按轮转
逐号记连败、代理订阅导入。`65e644eb` 是一次 revert（撤销号池分区），说明这套
设计本身还在调整。

**本地现状**：本地**没有** `proxypool` 包；只有 `Auth.ProxyName` 与按账号
transport（`upstream/transport.go`），Python 侧有
`server/services/proxyroutes.py`。

**建议**：**暂不移植**。参考仓自己刚 revert 了「号池分区」，说明出口归属与选号
的耦合方式还没稳定；本地按账号代理已够用。等参考仓稳定下来再评估。

**成本/风险**：高（要新建一整套出口池 + 订阅解析 + 归属探测）。

---

## 本版（`7a48d72e`）新增条目的一页摘要

按「本地缺不缺 + 收益 / 成本」排序，建议的实施顺序是：

| 顺序 | 条目 | 一句话 | 成本 | 状态 |
|---|---|---|---|---|
| 1 | **M-P0-3** | 一个 `data:` 行多个 JSON 会整轮失败，纯函数修复 | 低 | ✅ 已同步 |
| 2 | **M-P2-14** | 同 uid 多文件按 mtime 最新胜出，一个排序比较函数 | 极低 | ✅ 已同步（Go 侧；Python 侧未动） |
| 3 | **M-P2-15** | `isFirstPage` 字符串改布尔，一行修复 | 极低 | ✅ 已同步 |
| 4 | **M-P0-2** | 签到分钟级补签，纯调度层 | 低 | ✅ 已同步 |
| 5 | **M-P1-11** | 断流补 `finish_reason=length`，流式收尾加一帧 | 低 | ✅ 已同步 |
| 6 | **M-P1-6** | 请求级错误不解绑粘性，一个函数 + 调用点替换 | 低 | ✅ 已同步 |
| 7 | **M-P1-8** | 空 text 段补位 + reasoning 镜像，两个纯函数 | 低 | ✅ 已同步 |
| 8 | **M-P1-14** | 清洗注入的身份指令，两条正则 | 低 | ✅ 已同步 |
| 9 | **M-P1-15** | `/v1/models` 透出 promo 三字段 | 低 | ✅ 已同步 |
| 10 | **M-P1-13** | 签到回执四态（要改 `DailyCheckin` 签名） | 中低 | 待做 |
| 11 | **M-P1-12** | 11134 归供应商过载，依赖 `failKeepBinding` | 低 | ✅ 已同步 |
| 12 | **M-P1-10** | 输出预算下限 | 低 | 待做 |
| 13 | **M-P1-9** | `<think:...>` 拆回 thinking（落点在 Python） | 低 | 待做 |
| 14 | **M-P1-7** | 首帧前 SSE 注释帧（并发正确性） | 中低 | 待做 |
| 15 | **M-P0-4** | `$wbEncrypted` 解密 + 回写保持加密（Go + Python） | 中 | 待做 |
| 16 | **M-P1-5** | 只发 `user_id` 也派生粘性键（改流量分布，需灰度） | 低 | 待做（单独灰度） |
| 17 | **M-P2-13** | 目录热重载补两条护栏 | 低 | 待做 |
| 18 | **M-P2-12** | 日志落盘 / 劣化告警 / 选号策略（三件分开做） | 中 | 待做 |
| 19 | **M-P2-10** | 免费号事前避让 `exclusive` 模型（依赖两处解析） | 中 | 待做 |
| 20 | **M-P2-17** | 续写 / 救援链（与 M-P1-3 同一套机制） | 高 | 待做 |

> **进度（2026-11 低风险批）**：前 9 项**全部落地**（M-P0-3、M-P2-14、
> M-P2-15、M-P1-11、M-P1-6、M-P1-8、M-P1-14、M-P1-12、M-P1-15、M-P0-2，
> 各项末尾都有「落地状态」段）。M-P1-12 原本排在第 11 位，
> 因为依赖的 `failKeepBinding` 已在第 6 项落地，提前并入本批一起做。

明确**暂不移植**：M-P2-11（活动 2026-11-16 结束）、M-P2-18（无 profiling 需求
时不必开）、M-P2-19（参考仓自己刚 revert 了号池分区）、M-P2-16（与 M-B-11 合并
再做）。

> 前 9 项的共同点：都不改流量分布、不引入依赖、不动 transport，可以一批做完
> 再一起回归。第 16 项（M-P1-5）是唯一会立刻改变账号分配的一项，必须单独灰度。

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

> 上面 A-1～A-18 是**按旧基线（`29e4701f`）**整理的成长任务缺口。基线推进到
> `7a48d72e` 之后又筛出 3 项直接相关的（另 1 项是活动任务，不是成长中心），
> 展开在主体清单里，索引见 **A.4**。

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

#### A.3.1 取舍：成长任务里哪些真值得做

判断口径只有一条——**这一项能不能换成积分**。成长任务里绝大多数动作是「该拿没拿到」，
不做就是每天确定性地少拿，收益随天数线性累积；纯调度健壮性（A-15～A-18）只防重复
写和降低时间相关性，不直接产生积分，所以排在后面。

| 结论 | 条目 | 理由 |
|---|---|---|
| **值得，先做** | A-5 | 现在 activity 一关或上报不满 5 条，连登兑换/抽奖**整条跳过**。这是最大的一处漏，成本却只有「换个调用点」 |
| **值得，先做** | A-3、A-4 | 只领一档 / 只抽一次，等于每天主动放弃 N-1 档和 N-1 次。改动小、只对 `available` 写 |
| **值得，先做** | A-1、A-8 | global 号**完全不领礼包**；CN 盲领把「已领过」打成假成功行。每天一次只读换确定性 |
| **值得** | A-2 → M-P0-2 | 补签范围 + 失败重排。**顺序不能反**：先扩范围再排队，否则队列补的还是「只有昨日」 |
| **值得** | M-P1-13 | 回执四态。本身不产积分，但它是 M-P0-2 和 A-2 的观测前提——不修就看不见补签到底成没成 |
| **值得** | A-7、A-6 | 只读状态查询，成本极低；A-6 的 global 分支建议只展示状态、不发写请求 |
| **值得（一行）** | M-P2-15 | `isFirstPage` 字符串改布尔，`Buddy_App` 一直 0/1 的根因。改动两行，风险几乎为零 |
| **缓** | A-15 | 重启补跑标记只防重复写。**但如果 A-9 不做**，本地本来就没有分钟级任务，重复写风险很小 |
| **缓** | A-12 | 只点亮 `Hp_Appearance` 一个任务点，且要先确认该任务在当前账号上还挂得出来 |
| **缓** | A-16、A-17 | `spread` / `humanize` 只解决时间相关性，**不是防封**。参考仓自己也没给出封号对比数据 |
| **缓** | A-18 | 与 B-5 是同一件事，合并到协议层一起做，不要在成长任务里单独开一份 |
| **不做** | A-9、A-14 | Go 内嵌持久作业 + 分钟级 sweep，本地是 Python + Next.js，搬过来多一套运行时。只有在「确实要跑全自动任务中心」时才值得 |
| **不做** | A-11 全量对齐 | 桌面事件链/指纹按任务完成率再定，不要为了「同步完整」对齐 |
| **不做** | M-P2-11 | 活动 2026-11-16 就结束了，做完可能已经过期；且参考仓已用六条证伪证明「自动完成任务」服务端做不到 |
| **不做** | B-6 切换 | 必须先同号 A/B 实测。本地注释说 billingBase 返 200，但 200 ≠ 计分 |

### A.4 基线推进后新增的成长任务项（只做索引）

下表来自 `49fad282` 之后的提交，**展开在主体清单**，这里只回答「成长任务还差什么」
时能查到。判定与建议动作以主体条目为准：

| 能力 | M-位置 | 一句话 |
|---|---|---|
| 签到「请稍后重试」分钟级补签 | **M-P0-2** | 主批失败后第 10、40 分钟各补一次；只排 `checkinRetryable` 为真的错误 |
| 签到回执四态 | **M-P1-13** | 上游回执分 `checked`/`already`/`pending`/`failed`，面板不再把幂等拒绝显示成「+100」 |
| `isFirstPage` 改布尔 | **M-P2-15** | `Buddy_App` 一直 0/1 的根因，`task_runner.py:718`/`:1563` 两处 |
| 运营活动批量读进度 + 领奖 | **M-P2-11** | **不属于成长中心**（星巴克那期），但同属「任务面」；只做读进度 + 领奖，不要试图自动完成 |

> 与 A-1～A-18 的关系：A-1/A-2/A-3/A-4 是「闭环本身缺」，
> M-P0-2/M-P1-13 是「闭环有了但**失败路径**没兜住」——补签没排队、回执被丢掉。
> 两项都建议**先做 A-2 的补签范围**再做 M-P0-2，否则补签队列补的仍是「只有昨日」
> 那一个日期。M-P2-15 与 A-11/A-12 同属桌面任务链，但它是独立的一行修复，
> 不必等 A-12 的外观 apply。
>
> 另外两项**看着像成长任务、其实不是**，别在附录 A 找：
> M-P2-10（免费号避让 `exclusive` 模型）是模型目录问题，索引在 **B.7**；
> M-P2-12 第 1/2 项（日志落盘、劣化告警）是面板运维，主体清单已注明只取第 3 项进 Go。

### A.5 成长任务里明确不搬的

- `scheduler/growth.go` 的 Go 内嵌版持久作业：本地是 Python + Next.js，
  搬过来会多一套运行时（同 B.6 第一条）。
- `sweep.go` / `tasksweep.go`：只取「周期任务 + per-account 锁」的思路，
  实现写在 Python 侧（A-14）。
- `runstamps.go` 的「按天落小文件」：本地方案可以更简单——Python 侧已有
  `tasklog.py` 落盘，复用即可，不必新开一个 stamp 文件格式。

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

#### B.5.1 取舍：协议层里哪些真值得做

判断口径比成长任务多一条——除了「有没有收益」，还要看**改错了会不会影响全部请求**。
协议层的改动分三类：纯函数（最安全）、错误分类与状态（影响单请求）、
出站头与 transport（影响所有请求）。第三类收益是「更像官方客户端」，但没有封号
对比数据，所以一律排到最后。

| 结论 | 条目 | 理由 |
|---|---|---|
| **值得，最优先** | **M-P0-3** | 一个 `data:` 行塞两个 JSON 会让**整轮对话作废**，且报错在读侧、完全查不到。纯函数修复，切不动就原样透传，没有下行风险 |
| **值得，最优先** | **M-P1-6** | 请求级错误解绑粘性 = 「报错一次，前缀缓存就永远热不起来」。一个函数 + 调用点替换，收益是十万到几十万 token 的前缀不用重算 |
| **值得，最优先** | **M-P1-11** | 断流伪装成正常收尾，agent 侧只能人工催。流式收尾加一帧，与 `Aggregate` 对齐即可 |
| **值得** | **M-P1-14** | 身份指令回流会「越滚越频繁」，两条正则就能掐断。风险只在正则写宽 |
| **值得** | **M-P1-8** | 空 text 段是同 body 必失败，补位是纯函数。11151 那半已完成，只需补补位与镜像 |
| **值得** | **M-P1-12** | 11134 高峰期会把整片健康号打进冷却，这是**真实罚号**。依赖 M-P1-6，一起做 |
| **值得** | **M-P1-15** | promo 三字段不做，第三方客户端把限时免费显示成收费。解析 + 输出各一处 |
| **值得** | **M-P2-14** | 一个排序比较函数，修「陈旧备份胜出」导致的 token 失效 |
| **值得** | **M-P1-10** | 高档位思考吃光自己额度，用户看到的是「用着用着中断」。注意只抬上限、不抬目标长度 |
| **值得（Python）** | **M-P1-9** | 落点在 Python 的 anthropic 适配层，Go 侧无 `/v1/messages`。改动小但只影响 Anthropic 客户端 |
| **值得但需灰度** | **M-P1-5** | 唯一会**立刻改变账号分配**的一项。修好了前缀缓存命中，改错会短期 miss 上升。必须单独灰度 |
| **值得但需灰度** | **M-P2-12** 第 3 项 | 选号策略同理，缺省保持 `weighted`，只是把选择权交给配置 |
| **中，按需** | **M-P1-7** | 首帧注释帧解决「干等 3~6 秒」，但**并发正确性**要求高——写坏锁会产出交错帧，比干等更糟 |
| **中，按需** | **M-P0-4** | `$wbEncrypted` 是「新版客户端文件读不了」，不修就导不进来。但密码学口径要逐字节对齐，且**回写方向比读方向危险** |
| **中，按需** | **M-P2-13** | 只取护栏 1、4（防半写文件被当成删除）。本地轮询已能用，不做也不会立刻出问题 |
| **中，按需** | **M-P2-10** | 要等两处解析层先做（`modelTiers` + `IsPaidUser`）。收益是少撞 11158 |
| **缓** | **M-P2-16** | 与 B-11 合并做，单独做没有意义 |
| **缓** | **M-P2-17** | 最复杂的一块（帧级拼接 + 工具调用去重 + 预算继承），且与 M-P1-3 是同一套机制。没有真实环境验证不要上线 |
| **不做** | B-1 DSML | 收益明确但误判正文会破坏正常输出；严格判定优先，且必须有开关 |
| **不做** | B-21、B-22 | 出站头集合 / 头顺序 / TLS 指纹：改错影响**全部**出站请求，没有封号证据前不动 |
| **不做** | **M-P2-18** | pprof 不是协议能力，无 profiling 需求不必开 |
| **不做** | **M-P2-19** | 参考仓自己刚 revert 了号池分区，设计还没稳定 |
| **不做** | B-6 切换 | 200 ≠ 计分，必须先同号 A/B 实测 |

> 一句话总结：**协议层值得做的是「纯函数 + 错误分类」那一半**（M-P0-3、M-P1-6、
> M-P1-11、M-P1-8、M-P1-14、M-P1-12、M-P1-15），它们不改 transport、不改流量
> 分布、改错也只影响单个请求。**出站头 / TLS / 续写救援链**这三类一律往后放。
>
> 成长任务值得做的是「确定性少拿积分」那一半（A-5、A-3、A-4、A-1/A-8、
> A-2→M-P0-2），而 A-9/A-14 的 Go 版持久作业、A-16/A-17 的抖动摊开
> 都属于「不直接产生积分」，排最后。

### B.6 不要为了「同步完整」而搬的东西

- `internal/panel/*` 的内嵌面板、`taskjobs.go`、`tasksweep.go` 的 Go 实现：本地是 Next.js + Python，搬过来只会多一套运行时。
- `tlsfingerprint.go`、`headorder*`：收益是「更像官方客户端」，但改错会影响全部出站请求；没有封号证据前不做。
- `tasks.go`、`mp_events.go`：本地 Python 脚本已经覆盖，重写没有收益。
- `webactivity.go` / 10105 自救：本地没有网页活跃腿，单独搬清理逻辑没有触发场景（见主清单 M-P2-8）。

### B.7 基线推进后新增的协议层项（只做索引）

B-1～B-24 同样是按旧基线整理的。`7a48d72e` 之后又筛出 18 项协议层能力，
展开在主体清单里，按「改什么」分组索引如下：

**请求 / 响应体**

| 能力 | M-位置 | 改哪一侧 |
|---|---|---|
| 单个 `data:` 行多 JSON 拆分 | **M-P0-3** | Go `sse.go`（三处解析点）+ 新纯函数 |
| 空 text 段补位 + reasoning 镜像 | **M-P1-8** | Go `payload.go`（11151 那半已完成） |
| `<think:...>` 拆回 thinking 块 | **M-P1-9** | **Python** `platforms/anthropic.py`（Go 无 `/v1/messages`） |
| 输出预算下限 | **M-P1-10** | Go `payload.go` 之后 |
| 上游断流补截断终态 | **M-P1-11** | Go `sse.go` 收尾处（与 `Aggregate` 对齐） |
| 上游注入的身份指令清洗 | **M-P1-14** | Go `sanitize.go` 通用门控 |
| 续写 / 救援链 | **M-P2-17** | Go `server/` 新增 `continue.go` 一类（与 M-P1-3 同一套机制，**最复杂**） |
| 资料接口空昵称判失败 | **M-P2-16** | Go `upstream/profile.go`（与 B-11 合并做） |

**错误分类与会话粘性**

| 能力 | M-位置 | 改哪一侧 |
|---|---|---|
| 请求级错误不解绑粘性 | **M-P1-6** | Go `handler.go`（`failKeepBinding`） |
| 11134 供应商过载不罚号 | **M-P1-12** | Go `client.go` 的 `ErrKind` + `handler.go`（依赖 M-P1-6） |
| 只发 `user_id` 也派生粘性键 | **M-P1-5** | Go `session.go` + `handler.go`（**改流量分布，需灰度**） |

**流式体验**

| 能力 | M-位置 | 改哪一侧 |
|---|---|---|
| 首帧前 SSE 注释帧 | **M-P1-7** | Go `server/` 新增包装 + `sse.go` 加锁 |

**模型目录**

| 能力 | M-位置 | 改哪一侧 |
|---|---|---|
| `/v1/models` 透出 promo 三字段 | **M-P1-15** | Go `ModelInfo` 解析 + `applyModelInfoFields` |
| 免费号事前避让 `exclusive` 模型 | **M-P2-10** | Go `pool/pick.go`（依赖 `modelTiers` + `IsPaidUser` 两处解析） |
| 选号策略可配（`weighted`/`round_robin`/`sequential`） | **M-P2-12** 第 3 项 | Go `pool/pick.go`（**改流量分布，需灰度**） |

**账号文件与目录**

| 能力 | M-位置 | 改哪一侧 |
|---|---|---|
| `$wbEncrypted` 解密 + 回写保持加密 | **M-P0-4** | Go `auth/` **与** Python `tencent.py`（面板导入也读不出 uid） |
| 同 uid 多文件按 mtime 最新胜出 | **M-P2-14** | Go `auth.LoadAuthFiles` + Python `tencent.py` |
| 目录热重载护栏（只取 2 条） | **M-P2-13** | Go `pool/watch.go` |

> 与 B-1～B-24 的关系：B-1（DSML）、B-21/B-22（头集 / 头顺序 / TLS）仍是**未动的
> 高风险项**，本轮新增的 14 项都不碰 transport 与出站头，所以两批可以独立推进——
> 先做 B.7 这批，B-21/B-22 仍按原结论等封号证据。
>
> B-19（版本号偏旧）与 B-20（UA 品牌段）没有新增条目覆盖，仍是独立的一步：
> 改常量、按 realm 分档，风险低，可以和 B.7 一起做。

### B.8 协议层里明确不搬的（本轮新增的补充）

- `internal/proxypool/*`（M-P2-19）：参考仓自己刚 revert 了「号池分区」，
  说明出口归属与选号的耦合方式还没稳定；本地按账号代理已够用。
- `WB2API_PPROF`（M-P2-18）：不是协议能力，且本地没有 profiling 需求；
  真要加必须「独立端口 + 默认关闭」，不要挂主 mux。
- 参考仓 `cache_audit.py`（`24d0b61b`）、`reqlog` / `usage` / `balance` 面板侧
  记录层：本地 Python 面板已有自己的记录链路，不重复建。

---

## 提交索引

> 下表只列**基线之后**的提交（`49fad282` → `7a48d72e`）。附录 A/B 里引用的多数
> 提交早于 `49fad282`，属于「基线之前一直没移植」的能力，未在本表重复；真正移植
> 时同样要在 `deploy/workbuddy-panel-ref.txt` 追加提交号并注明「部分同步」。
>
> 表中「处置」列的 `M-` 编号指向本文正文条目。跳过项集中列在表末，原因写在同一行。

| 提交 | 标题 | 处置 |
|---|---|---|
| `7a48d72e` | 免费号事前不派「免费调不通」的模型（`modelTiers accessMode`） | **M-P2-10**，依赖两处解析层 |
| `3d0f293e` | 记下每个出口的归属（国家 / 住宅或机房 / ISP） | **M-P2-19**，暂不移植 |
| `1c007bb0` | 活动路径不带模块段（星巴克这期是 `/portal/activity/tasks`） | **M-P2-11** 的一部分 |
| `0fadf989` | 运营活动批量读进度 + 一键领奖 | **M-P2-11** |
| `8125831d` | 重复签到按回执分四态出文案 | **M-P1-13** |
| `8f9fa3c5` | 清洗上游注入的身份指令 + 去掉全球号「等签到恢复」的错误承诺 | **M-P1-14** |
| `79ba3d62` | 等用户拍板的收尾不再救援；narration 加长度闸门 | **M-P2-17** |
| `92dadae3` | 工具调用标记按完整性分流——截断的补完、完整的别续写 | **M-P2-17** |
| `2b210e92` | 请求级错误不再解绑会话粘性 | **M-P1-6** |
| `8818458b` | `buddyapp_enter_click` 的 `isFirstPage` 改布尔 | **M-P2-15**（一行修复） |
| `baf9155d` | 收尾误救两道闸门 | **M-P2-17** |
| `1adfa520` | 签到「请稍后重试」10/40 分钟后自动补签 | **M-P0-2** |
| `73667400` | 订阅里 `proxies` 的流式数组不再被静默丢掉 | **M-P2-19**，暂不移植 |
| `d011fc84` | `dayAt` 用当日 00:00 采样（修测试漂移） | 跳过：参考仓自己的测试修复 |
| `8df99ac3` | 等上游首帧期间补 SSE 注释帧 | **M-P1-7** |
| `cf2b80f9` | 到期窗口锚到日界（修测试漂移） | 跳过：参考仓自己的测试修复 |
| `4f6adc0e` | 轮询热重载在 `reload_delete_confirm>1` 时永不删号 | **M-P2-13**（护栏 2 的 bugfix） |
| `b64773e8` | `/v1/models` 透出 `promo_factor` / `promo_credits` / `promo_label` | **M-P1-15** |
| `849d3381` | `klauspost/compress` 升级（GO-2026-6865） | 跳过：本地无该依赖 |
| `cfac3703` | 面板单选框被全局 100% 宽撑开 | 跳过：面板 UI |
| `e8df35b6` | 正文里写成文本的工具调用（DSML）不再触发续写 | **M-P2-17**（闸门之一） |
| `cfef70da` | 国际版到期批次按 `(PackageCode, 计量周期)` 聚合 | 见 G-P1 的 global 余额三件套（M-B-7） |
| `2e30d7d3` | 扩大「说一句就停」的救援覆盖 | **M-P2-17** |
| `c54c5176` | 账号增删/登录落盘与目录热重载真正串行化 | **M-P2-13**（本地跨进程，只取护栏 1/4） |
| `9e5c2cda` | 账号目录热重载 + 更新代理/镜像/失败归因 | **M-P2-13**；更新器部分跳过（见 G-P2-6） |
| `afb5f224` | 一个 `data` 行里塞了多个 JSON 时拆分 | **M-P0-3** |
| `910d8517` | 安全续写的正文上限 240B → 2000B | **M-P2-17** |
| `d7ffe424` | 「缺函数名」告警不再把已发出的工具调用算进去 | 跳过：依赖参考仓的 `/v1/responses` 适配层 |
| `65e644eb` | 撤销号池分区 | 跳过：revert，参考仓自己在调整 |
| `592fb01a` | 额度窗口探针、号池分区、代理订阅导入 | **M-P2-19**，暂不移植 |
| `bfdba519` | 直连传输层失败记到出口，不再按轮转逐号记连败 | **M-P2-19**，暂不移植 |
| `18a67761` | 运行日志落盘+导出、上游劣化滑窗告警、选号策略可配 | **M-P2-12** |
| `4bdbd607` | narration/toolonly 记录尾部片段与续写结果 | **M-P2-17**（观测部分） |
| `1037397c` | 工具轮里「说一句就停」时试续一次 | **M-P2-17** |
| `44bc0595` | 工具调用缺函数名被丢弃时留 WARN | 跳过：同 `d7ffe424` |
| `c5ce29b1` | 账号导入支持多文件，选中先预览 | 跳过：面板 UI |
| `d78131ba` | 资料接口空昵称不再静默成功，并兼容顶层平铺响应 | **M-P2-16**（与 M-B-11 合并做） |
| `c2003af1` | 正文停在预告句上就收尾时续写一次 | **M-P2-17** |
| `24d0b61b` | `cache_audit.py` —— 按输入档位审计前缀缓存 | 跳过：参考仓的运维脚本 |
| `07f8ae03` | 思考完直接收尾时也救援一次 | **M-P2-17** |
| `f987bacc` | 出站补位空 text 段 + 11151 归请求级；Kimi/GLM 补 reasoning 镜像 | **M-P1-8**（11151 那半本地已完成） |
| `cd884503` | 导入时信封的值是对象时别把信封名当 uid | 跳过：面板导入细节，随 M-P0-4 一起看 |
| `585ae7f2` | 导入兼容 cockpit 详情导出的嵌套/驼峰/字典形态 | 跳过：面板 UI |
| `d165e6e1` | 只发 `user_id` 的客户端也按内容派生键粘账号 | **M-P1-5** |
| `478d2aa8` | 抓「声明了工具却只说一句就收尾」的真实样本 | 跳过：诊断开关，参考仓专用 |
| `7028676c` | 构建产物不进仓库 | 跳过：仓库卫生 |
| `877324fe` | `thinking.default_effort` 缺省值统一到 `DefaultGatewayEffort` | **M-P2-17** 的前置（默认档统一） |
| `6e7fe628` | 客户端额度 ≥ 32k 时按长文续接 | **M-P2-17** |
| `771c887f` | 上游漏进正文的 `<think:SUF>` 拆回 thinking 块 | **M-P1-9** |
| `89c9b7d9` | 流式透传只在缓冲读空时 flush | 跳过：性能优化，本地 flush 语义不同（逐帧 flush 是有意的） |
| `4b0a3b46` | 请求体只解析一次（零分配扫描） | 跳过：性能优化，收益在参考仓的体量下才明显 |
| `fe51fd66` | 请求体预处理的指纹预检去掉 RE2 大小写折叠 | 跳过：同 `4b0a3b46` |
| `b42712c5` | `WB2API_PPROF` 打开独立 pprof 监听 | **M-P2-18** |
| `29db8786` | 续写闸门从「最多 3 段」改成「累计输出上限」 | **M-P2-17** |
| `34a3ae67` | 抬过额度的请求在 `length` 处自动续接 | **M-P1-10** / **M-P2-17** |
| `e54c6e07` | 续写/救援段继承输出预算下限 | **M-P2-17** |
| `2b9b7736` | 输出预算下限 | **M-P1-10** |
| `12d9ea6b` | AutoBot chore：参考仓库清单加回一个仓库 | 跳过 |
| `1df3b836` | 示例配置的 `default_effort` 断言跟随 `high` | **M-P2-17** 的前置 |
| `ffca1daf` | 默认档位改用 `high`、只有思考没正文改为 `failed` | **M-P2-17** 的前置；默认档本地已是 `high`（`thinking.go:32`） |
| `562d0d88` | 思考吃光预算时也自动续写 | **M-P2-17** |
| `84a48d9e` | 上游断流补截断终态 | **M-P1-11** |
| `72978243` | 上游错误 WARN 的 body 截断 200 → 512 | 低优先：本地 `client.go` 的 `truncate(raw, 200)` 有 3 处，改大即可 |
| `b674f71a` | 模型可用性按 `model_eff` 的裸名形态命中 | 跳过：面板 + reqlog，本地无该层 |
| `efe64fd2` | `model_eff` 按线协议原值记录 | 跳过：同 `b674f71a` |
| `1b3361a5` | 清除筛选没清掉记住的状态偏好 | 跳过：面板 UI |
| `69248a63` | 配置页两处错位 | 跳过：面板 UI |
| `4f37ffab` | 统一按钮与下拉的尺寸、模型表内距 | 跳过：面板 UI |
| `48306862` | 11134 供应商过载不再罚号/换号，并保留会话粘性 | **M-P1-12** |
| `6671b8d7` | 请求记录全链路区分降级前后模型 | 跳过：面板 + reqlog |
| `97d3f259` | 回写只封「读入时确实加密」的字段；同一 uid 多文件时最新凭证胜出 | **M-P0-4** + **M-P2-14** |
| `c7cc2167` | 支持新版桌面端的 `$wbEncrypted` auth 文件 | **M-P0-4** |
| `e4e6f5ce` | 兜底 system 只写行为约定，不再声明身份 | 跳过：本地 `prompt` 包已有自己的口径，与 `8f9fa3c5` 不同源 |
| `35228b36` | 响应头禁搜索引擎收录 | 跳过：面板 UI |
| `2b7ac6de` | 官方凭据夹具里的真机 uin 一并脱敏 | 跳过：testdata 卫生 |
| `e5bf2c56` | 脱敏真机凭证与第三方路径 | 跳过：testdata 卫生 |
| `29d98794` | `/v1/search` 卡片改写为等价检索链接 | **P2-2**，等有 search 端点再说 |
| `f6335ce8` | 余额趋势时间筛选器与刷新按钮补事件绑定 | **P2-5**，面板需自研 |
| `df988aae` | README 补 Windows 常驻运行章节 | **P2-9**，按需并入 watchdog 文档 |
| `b494426a` | 入站请求体上限 + 11140 风控连坐保护 | **已同步一半**：上限见 G-P0-2 ✅；连坐保护仍待做（M-P0-1） |
| `c6f953a7` | 慢上传可见性 + 在途名额释放回归 | **已同步一半**：慢上传 WARN 随 G-P0-2 ✅；名额释放回归仍待做（M-P2-6） |
| `e36ad073` | 覆盖已有凭据前先求证新凭据还活着 | **P1-1** |
| `4fb7cb42` | 盲盒消费 + 10105 活跃会话自救 | **P1-2**（盲盒）；10105 见 P2-8 |
| `0afd8ae4` | AutoBot chore | 跳过 |
| `e466d543` | AutoBot chore | 跳过 |
| `29e4701f` | AutoBot chore | 跳过 |

> **合并提交**（`c33799da`、`cf16478d`、`a439202a`、`cffb0389`、`5702ab65`、
> `1aa917d4`、`865f7779`、`d580c95b`、`ee5ae705`）不单列：其内容已由对应的
> 非 merge 提交覆盖。

> 这张表只覆盖基线之后的 76 个非 merge 提交。第一批实际动到的另外 7 个提交
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
5. **2026-11 低风险批**：本文「一页摘要」里前 9 项已按本地风格**全部**逐项重写
   落地（M-P1-12 也提前并入，依赖的 `failKeepBinding` 已就绪）。这批都不改流量
   分布、不引入依赖、不动 transport，`deploy/workbuddy-panel-ref.txt` 里单独成段。
   下一批（`reserve_credits` → `model_fallback` → 6020/6021 →
   `account_priority`/`account_share` → `balance_window`）会改流量分布，必须灰度，
   与这批不是同一批。
