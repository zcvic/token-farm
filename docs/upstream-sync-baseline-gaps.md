# 参考仓「基线之前」未同步能力清单（workbuddy2api-panel）

> 维护者文档，不进发布包。同步原则与 `git am` 操作见
> [upstream-sync.md](upstream-sync.md)。
>
> 本文与 [upstream-sync-misakano.md](upstream-sync-misakano.md) **互补，不重复**：
> 那一份按「基线之后的新提交」组织，本文按「基线之前一直没移植的能力」组织。
> 两份清单里同一项只在一处展开，另一处只做索引。

这份清单回答另一个问题：在 `49fad282` 那次同步**之前**，参考仓已经实现、
本项目**从未移植**的能力还有哪些。

结论同样分 P0/P1/P2 三档，每项写清「能力 / 来源提交 / 本地现状 / 建议动作 /
成本与风险」。清单本身不产生代码改动；真正移植时一次同步对应一个提交，并在
`deploy/workbuddy-panel-ref.txt` 追加该提交号。

> **编号前缀**：本文的条目在正文里写成 `G-P0-1` 形式（G = gaps）；
> [upstream-sync-misakano.md](upstream-sync-misakano.md) 那份写成 `M-P0-1`
> 形式（M = misakano）。两份文档原本都用裸 `P0-1`，同名条目指的不是同一件事
> （例如两边的 `P2-8` 分别指「10105 会话自救」与「模型目录指纹监控」），
> 交叉引用时必须写明是哪一份。下文标题保留裸编号以便检索，**凡跨文档引用
> 一律带 `G-` / `M-` 前缀**。

## 同步状态（截至 2026-11 低风险批）

**第一批六项**（基线之前的能力缺口）已全部落地，代码与测试都在本仓，未提交。
状态列的「✅」表示能力已可用，后面的括号说明是否**部分**同步。

| 编号 | 能力 | 状态 | 来源提交 |
|---|---|---|---|
| G-P0-1 | 状态文件落盘掉电安全（`safefile`） | ✅ 已同步（auth 文件、`state.json` 两处接入） | `4356a346` |
| G-P0-2 | Go 侧入站请求体上限 + 慢上传可见性 | ✅ 已同步（只挂 chat 主链路） | `b494426a`、`c6f953a7` |
| G-P0-3 | 入站读取超时与优雅关停窗口可配 | ✅ 已同步（`0` 按注释语义实现，非参考仓实现） | `1e7a693b`、`16f83d75` |
| G-P0-4 | CST 日界统一（`internal/cst`） | ✅ 已同步（9 处日界/钟点全部收敛） | `025cc107` |
| G-P1-15 | `schedule.keepalive_refresh_window` | ✅ 已同步（只保留「校验拒绝 0」一种口径） | `764293b8` |
| G-P1-16 | `/v1/models` 的 hidden 过滤（Go 侧） | ✅ 已同步（列表 + 点名两道闸；配置由面板镜像写入） | `b7297e5c`、`5ce1a2f4` |

> 来源提交里 `b494426a`（2026-11-07）与 `c6f953a7`（2026-11-07）其实**晚于**
> `49fad282`（2026-11-06），属于「基线之后」；其余七个早于基线。两处都列在
> [upstream-sync-misakano.md](upstream-sync-misakano.md) 的提交索引里，并已标
> 「已同步一半」。本文收它们只是因为**能力本身**是基线之前就有的缺口——判定
> 依据是「本地缺不缺」，不是「提交早不早」。

落地时的三处偏离，都是有意的，不是遗漏：

1. **G-P0-3 的 `shutdown_timeout=0`**：按配置注释实现（`0` → `srv.Close()`
   立即退），不照抄参考仓「注释说 0 不等待、实现回落 5s」的矛盾；
2. **G-P1-15 的 `0` 语义**：只保留「配置校验拒绝 `<=0`」这一种口径，运行期
   不再把 `0` 当「不过滤」。要全量刷新请配一个很大的窗口；
3. **G-P1-16 的配置来源**：Go 不新建配置项体系，读 `config.json` 的
   `models.hidden`，由面板 `save()` 单向镜像写入（`server/services/modelpolicy.py`
   的 `mirror_to_upstream`），10 秒热加载。

### 2026-11 低风险批（基线之后，九项）

这一批来自 [upstream-sync-misakano.md](upstream-sync-misakano.md) 的「一页摘要」
前 9 项，全部**不改流量分布、不引入依赖、不动 transport**，因此可以一批做完
再统一回归。同样是未提交状态。

| 编号 | 能力 | 状态 | 来源提交 |
|---|---|---|---|
| M-P0-3 | 单个 `data:` 行多个 JSON 时拆分 | ✅ 已同步（流式 + 聚合两条路径；参考仓只改流式） | `afb5f224` |
| M-P2-14 | 同 uid 多文件按 mtime 最新胜出 | ✅ 已同步（Go 侧；Python `tencent.py` 未动） | `97d3f259` |
| M-P2-15 | `isFirstPage` 字符串改布尔 | ✅ 已同步（`task_runner.py` 两处） | `8818458b` |
| M-P1-11 | 上游断流补 `finish_reason=length` | ✅ 已同步（流式 + `Aggregate` 同口径） | `84a48d9e` |
| M-P1-6 | 请求级错误不再解绑会话粘性 | ✅ 已同步（渠道校验仍走 `fail()`） | `2b210e92` |
| M-P1-12 | 11134 供应商过载归上游容量信号 | ✅ 已同步（`ErrUpstreamQueue` 改名 `ErrUpstreamBusy`） | `48306862` |
| M-P1-8 | 出站补位空 text 段 + reasoning 镜像 | ✅ 已同步（11151 归请求级本地此前已有；镜像放在 sanitize 之后） | `f987bacc` |
| M-P1-14 | 清洗上游注入的身份指令 | ✅ 已同步（两条正则 + `ErrHardCredit` 文案去「等签到恢复」） | `8f9fa3c5` |
| M-P1-15 | `/v1/models` 透出 promo 三字段 | ✅ 已同步（`promo.go`；不移植 `PromoNote` / `ModelTier`） | `b64773e8` |
| M-P0-2 | 签到「请稍后重试」分钟级自动补签 | ✅ 已同步（`checkin_retry.go` + pool 签到记账；回执四态未做） | `1adfa520` |

> 这九项的能力描述、本地现状与实施细节**不在本文展开**，只在
> [upstream-sync-misakano.md](upstream-sync-misakano.md) 各条目末尾的
> 「落地状态（2026-11 低风险批）」段里。本文这张表只作索引，避免两处重复。
>
> 该批九项**全部落地**，未提交。

第二批（改流量分布、需灰度）与「明确不移植」的项见文末「建议实施顺序」。

**部署注意**（G-P1-16 的镜像写入）：写入发生在**面板容器**里，目标是上游仓库目录
下的 `config.json`。默认部署把整个上游仓库以读写方式挂进面板
（`docker-compose.yml` 的 `- ../workbuddy2api:/opt/workbuddy2api`），所以默认
配置下镜像能成功；`upstream/docker-compose.yml` 里那条
`./config.json:/app/config.json:ro` 是**Go 容器自己**的挂载，只读即可，不影响
写入。

会写不进去的两种情况：把面板的上游挂载改成只读，或上游由别人维护、文件属主
不允许面板写。此时日志只有一条 WARN，**面板内的隐藏行为照常生效**（列表过滤 +
点名拒绝都在 Python 侧），只有「直连 Go 端口」这条旁路看不到名单。修好权限后
重启面板即可——启动时 `lifespan` 会补写一次存量名单，是幂等的。

`upstream/config.example.json` 已补齐本批引入的三段配置
（`server.max_payload_bytes` / `read_timeout` / `shutdown_timeout`、
`models.hidden`、`schedule.keepalive_refresh_window`），并有一条测试
（`cmd/server/example_config_test.go`）保证示例文件始终能被 `Load` 读进来——
否则用户照抄示例却起不来，是最难发现的一类回归。

---

## 基线

| 项 | 值 |
|---|---|
| 本地 HEAD（本文分析基准） | `24d4014b`（v1.1.0） |
| 本地已核对到的参考提交 | `49fad282`（记录在 `deploy/workbuddy-panel-ref.txt`） |
| 参考仓 HEAD | `7a48d72e`（2026-11-13；上一版按 `29e4701f` / 2026-11-07） |
| 参考仓规模 | 447 个 `.go` / 290 个测试文件；去掉 `internal/panel/**` 与非测试后 140 个（**上一版基线时的计数**，本次未重数） |
| 本地 `upstream/` 规模 | 274 个 `.go` / 193 个测试文件；非测试 16,156 行 |

> 参考仓 HEAD 已从 `29e4701f` 推进到 `7a48d72e`，期间新增 76 个非 merge 提交。
> 那些提交属于「基线之后」，按两份文档的分工**只进
> [upstream-sync-misakano.md](upstream-sync-misakano.md)**，本文不重复展开；
> 本文只更新了规模数字，并把新增能力在 P2-9 的索引表里补行。
>
> 本文的 P0/P1/P2 条目与「同步状态」表**不受基线推进影响**——它们描述的是
> `49fad282` 之前就存在、本地一直没移植的缺口，判定依据是「本地缺不缺」。

> 上表是**分析时**的快照。第一批六项落地后（未提交）本地为 274 个 `.go` /
> 193 个测试文件、非测试 16,156 行——新增的两个包（`safefile`、`cst`）与十二个
> 测试文件都在这里（清单见文末「第一批」）。

参考仓最早提交是 2026-09-12（`fd64aedb`，内嵌面板首版），本项目与其**没有共同
历史**，只能逐项重写，不能 `git merge`。

## 先看结构差异

两边的包划分已经分叉，本文的「本地现状」全部以**包是否存在**为第一判据：

| | 本地 `upstream/internal/` | 参考仓 `internal/` |
|---|---|---|
| 共有 | `auth` `logfmt` `pool` `prompt` `redisstore` `scheduler` `server` `session` `upstream` | 同左 |
| 本地独有 | `config`（`Schedule` 段，`cmd/server` 与 `cmd/activity` 共用）、`pool/watch.go`、`pool/refresh_risk.go`、`upstream/reasoning_fallback.go`、`upstream/refresh_retry.go`、`upstream/enterprise_usage.go`、`upstream/routes_file.go`、`upstream/growth_*.go` | — |
| 参考仓独有 | — | `alert` `balance` `credithist` `httpauth` `livecfg` `loginsite` `media` `probe` `proxypool` `reqlog` `updater` `usage`（另有 `panel`，不在本文范围） |
| 第一批之后新增 | `safefile`、`cst`（按参考仓同名包重写，已从「参考仓独有」移出） | — |

命令层也不一样：本地有 `cmd/acct`、`cmd/activity`、`cmd/stats`、`cmd/credit`、
`cmd/login`、`cmd/signin`、`cmd/trial`；参考仓只有 `cmd/credit`、`cmd/login`、
`cmd/signin`、`cmd/trial`、`cmd/server`。参考仓没有独立的 `internal/config`：
`Schedule` 段直接写在 `cmd/server/config.go` 里，所以它没有「多命令共享同一份
排程定义」这个问题——本地恰恰相反，`internal/config` 就是为消除 `cmd/server` 与
`cmd/activity` 的默认值漂移才抽出来的（`schedule.go` 文件头有记录）。

文件级差距集中在两处：`server/` 本地 9 个非测试文件、参考仓 31 个；`upstream/`
本地 27 个、参考仓 63 个。`pool/` 数量接近但内容不同：本地有 `watch.go` /
`refresh_risk.go` / `weightedbind.go`，参考仓有 `plan.go` / `modelview.go` /
`toggle.go`。

工具链也要先知道：本地 `go.mod` 是 **Go 1.22.5**，只依赖 `go-redis`；参考仓是
**Go 1.26.10**，额外依赖 `utls` / `brotli` / `x/net` / `x/sys`。任何需要改
transport 的项（TLS 指纹、头顺序）都会连带把工具链抬到 1.26，这一条在评估
P2 时是硬约束。

## 对另一份清单「已覆盖」表的更正

`upstream-sync-misakano.md` 的「已覆盖，不必再同步」表只列了 11 行，其中至少 5 行
是**部分覆盖**或**口径不同**，容易让人以为不用再看。本文按实际情况分档：

| 另一份清单的写法 | 实际情况 | 本文处置 |
|---|---|---|
| 入站请求体上限（`WB_GATEWAY_MAX_BODY_MB`） | 只覆盖 Python 入口；Go `chatCompletions` 原本是 `io.ReadAll(r.Body)` 无上限 | **G-P0-2** 展开（**已同步**） |
| 等价模型降级（`server/services`） | 那是 `modelmap.py` 的**别名映射**（客户端名→真实名，含 `*` 兜底），不是「6004 时同账号改道等价模型」 | **G-P1-7** 展开 |
| Prometheus 指标（`server/routers/metrics.py`） | 只覆盖 Python；Go 上游自己没有 `/metrics` | **G-P1-2** 展开 |
| 11140 的识别（`IsUpstreamRestriction`） | 只识别，无连坐保护 | M-P0-1 已展开，本文不重复 |
| 全局隐藏模型（`modelpolicy.py`） | 原本只覆盖 Python；Go `/v1/models` 直连透传，既不过滤列表也不拦点名调用 | 另一份清单只标「已覆盖」未展开；本文 **G-P1-16** 展开（**已同步**） |
| 账号备注 / 积分倒计时 / 临时停用 | 面板能力，与 Go 池状态是两套 | 不算缺口 |

---

## P0：建议优先移植（数据完整性与安全）

### P0-1　状态文件落盘掉电安全（`safefile`）

**能力**

状态文件（`state.json`、每个 auth 文件）的写入从「tmp + rename」升级为
「tmp + `fsync` + rename」，并在替换前把上一份有效内容留成 `<path>.bak`；
读取时内容坏（不是合法 JSON）就回退 `.bak`，两边都坏则报错说清路径与坏法，
**绝不静默当成空**。

为什么 rename 不够：rename 只保证读者看到的是一个完整文件，不保证内容已经
落盘。掉电时可能留下长度正确、内容全 0 的文件。而「读成空」比「读失败」更危险
——下一次写入会把空状态变成定局。

**来源**

- `4356a346` `fix(persist): 状态文件写盘先 fsync，读到坏文件回退 .bak`
- 参考文件：`internal/safefile/safefile.go`（`Write` / `WriteKeep` / `Read` +
  `.bak` + 损坏拒绝）；调用方 `pool/persist.go`、`auth/auth.go`、
  `scheduler/runstamps.go`、`credithist`、`reqlog`

**本地现状**

- `upstream/internal/pool/persist.go:262` `saveLocked()`：同目录 tmp 后
  `os.Rename(tmp, p.stateFp)`（L280），**没有 `Sync()`、没有 `.bak`、没有损坏检测**；
- `upstream/internal/pool/persist.go:134` `load()`：读文件失败或
  `json.Unmarshal` 失败都**直接 `return`**（L136–142），状态被当成「什么都没有」。
  这才是真正的危险点——不是读不出来，而是读坏了当成空；
- `upstream/internal/auth/auth.go:354` `SaveAtomic()`：同样是 tmp + rename
  （L402），同样没有 fsync / 备份；`auth.LoadDir` 对读失败或 `Parse` 失败的行
  `continue`（静默跳过），单文件坏掉等于该账号凭空消失；
- `upstream/internal/pool/watch.go` 每 5 秒轮询 `auths/` 目录指纹（文件名 +
  mtime + size）。凭证被截断成空文件时指纹变化会触发 `reloadAuthDir` →
  `SyncToDir`，`byUID` 里没有对应 uid 的账号被 `delete` 并立刻 `saveLocked()`——
  空内容被固化成定局，`.bak` 是唯一的退路，而现在没有。

**建议动作**

1. 新增 `upstream/internal/safefile`，接口按参考仓收敛为
   `Write(path, b, perm)` / `WriteKeep(path, b, perm)` / `Read(path, valid)`；
2. `persist.go` 与 `auth.go` 的写路径改用 `WriteKeep`，读路径改用
   `Read(..., json.Valid)`；
3. `auth.LoadDir`（`auth.go:428`）用同一读法，读到 `.bak` 回退时打 WARN；
   本地**没有** `pool.LoadDir`——池的 `load()` 读的是单个 `state.json`；
4. 遵守三条硬规则：文件不存在 = 用户特意删的，不去 `.bak` 复活；内容坏但 `.bak`
   好则回退；两边都坏必须报错。

**成本与风险**

- 成本：低。新增一个 100 行左右的包，两处调用点替换；
- 风险：`fsync` 会让每次写盘多一次磁盘刷写。状态文件写入频率是「每次选号成功 /
  每次任务结束」，量不大；若要更保守，可只对 `state.json` 开 `WriteKeep`，
  auth 文件保持现状；
- 平台：`os.File.Sync()` 在 Windows 上走 `FlushFileBuffers`，语义可用，不需要
  额外的构建标签分支（本地 `persist_other.go` / `persist_unix.go` 只处理
  uid/gid 日志字段，与本次无关）。测试仍应在 Windows 上真跑一遍写-读-损坏回退
  三步，而不是只跑 Unix CI。

**落地情况**

新增 `upstream/internal/safefile`（`Bak` / `Write` / `WriteKeep` / `Read` +
`keep` / `replace` / `describe`）+ 7 项测试（含「坏内容不污染 `.bak`」「两边都坏
必须报错」「文件不存在不去 `.bak` 复活」）。接入点：

- `pool/persist.go`：`load()` 改 `safefile.Read`，读不出来打 `ERR`（不再静默当空）；
  `saveLocked()` 改 `WriteKeep`；
- `auth/auth.go`：`SaveAtomic()` 改 `WriteKeep`；`LoadDir()` 改 `safefile.Read`，
  读/解析失败打 WARN 而不是静默 `continue`。

与参考仓的差异：本地 `auth.NeedsRefresh` 对 `ExpiresAt <= 0` 返回 `true`（API-Key
陷阱），已在测试里显式钉住；参考仓对 API Key 恒 `false`。接 API Key 账号前必须
同步这条，否则每轮保活都会去 refresh 一个不能 refresh 的凭证。

---

### P0-2　Go 侧入站请求体上限 + 慢上传可见性

**能力**

`readBody` 最多读 `limit+1` 字节，多出的那 1 字节就是「超限」证据；超限时**先
排空有限字节再回 413**（直接 `return` 会让 `net/http` 关掉连接，客户端往往只看
到 connection reset，看不到 413）。读体耗时超过 3 秒打一行 WARN（方法、路径、
字节数、耗时、声明的 `Content-Length`），不记 body。

**来源**

- `b494426a` `feat(server): 吸收 hub 的两处加固——入站请求体上限、11140 风控连坐保护`
- `c6f953a7` 慢上传可见性
- 参考文件：`internal/server/bodylimit.go`；配置 `server.max_payload_bytes`
  （允许范围 8–512 MiB，默认 64 MiB）

**本地现状**（移植前）

- `upstream/internal/server/handler.go:600` 直接 `body, err := io.ReadAll(r.Body)`，
  注释明确写着「请求体无大小上限」；
- Python 入口有护栏：`server/routers/gateway.py:98` 的 `WB_GATEWAY_MAX_BODY_MB`
  默认 32 MB、流式读、超限 413；但这条只保护 Python 侧；
- Go 侧唯一的落盘是 `handler.go:609` 的 `WB2A_DUMP_REQ`：固定 4 MB 阈值、
  固定路径 `/app/data/last_request.json`，只落请求体、不落响应。

**建议动作**

1. 新增 `upstream/internal/server/bodylimit.go`：`readBody(w, r)` 返回
   `([]byte, *bodyReadError)`，`TooLarge` 由调用方按各自协议形状回 413；
2. 排空用 `http.NewResponseController(w).SetReadDeadline(now+2s)` +
   `io.CopyN(io.Discard, r.Body, limit)`——限时是为了不让慢客户端把连接和
   goroutine 拖住（参考实现就是这么做的，不是「再读 1 MiB」的硬编码）；
3. `chatCompletions` 的 `io.ReadAll` 换成 `h.readBody`；其余端点可暂不动；
4. 配置加 `server.max_payload_bytes`（`int64`，默认 64 MiB，允许 8–512 MiB，
   越界 fail fast 而不是静默钳），与 Python 的 32 MB 默认值不要强行统一——
   Go 侧是最后一道护栏，宜宽不宜窄；
5. 慢上传 WARN 只在超过 3 秒时打，阈值写死常量即可，不必做成配置；
6. 错误形状按协议分：OpenAI 用 `writeOpenAIError`（code `payload_too_large`），
   Anthropic 用 `writeAnthropicError`。本地没有 `/v1/messages`，所以先只做
   OpenAI 形状即可，但函数签名留 `anthropic bool` 便于以后接。

**成本与风险**

- 成本：低到中。上限逻辑简单，难的是「排空」的边界：必须限时，不能跟着客户端节奏读；
- 风险：上限不能太低。长会话多图 base64 实测在几 MB 量级，参考仓把默认值放在
  64 MiB 就是刻意远高于真实会话；若按 8 MB 卡，会误杀正常长对话；
- 作用域：只挂 chat 主链路。`/v1/models`、`/status` 的 body 都很小，不值得加。

**落地情况**

新增 `upstream/internal/server/bodylimit.go`（`defaultMaxPayloadBytes` /
`slowBodyWarn` / `bodyReadError` / `readBody` / `drainBody` / `writeBodyError` /
`maxPayload`）+ 7 项测试；`handler.go` 的 `chatCompletions` 已改用
`h.readBody`，`Config.MaxPayloadBytes` 由 `server.max_payload_bytes` 注入。

两处与参考实现的差异：

1. `writeBodyError` **只保留 OpenAI 形状**——本地没有 `/v1/messages`，参考实现里
   的 `anthropic bool` 分支在这里是死代码；
2. 慢上传 WARN 用 `time.Since(start)` 而不是参考实现的读取中间计时，语义一致
   （都在超过 3 秒时打一条），但不依赖 `io.Reader` 包装。

---

### P0-3　入站读取超时与优雅关停窗口可配

**能力**

- `server.read_timeout`：入站请求读 body 的总时长上限，默认 `300s`，`0` = 不限；
- `server.shutdown_timeout`：SIGTERM / 更新重启时等在途请求排干的窗口，默认
  `30s`；
- 关停时先落盘池状态，再 `srv.Shutdown`；超时不再静默。

> 参考仓自己的 `shutdown_timeout` 有一处不一致，移植时别照抄：配置注释写
> 「`0` = 不等待直接退」，但 `main.go` 的实现是
> `if shutdownTimeout <= 0 { shutdownTimeout = 5 * time.Second }`——显式写 `0`
> 反而拿到 5 秒。本地做的时候要么按注释实现（`0` 直接 `srv.Close()`），要么
> 把注释改成「`0` 回落 5s」，不要两处各说一套。

**来源**

- `1e7a693b` `feat(server): 优雅关停窗口可配且超时不再静默；启动失败 Fatal 前落盘池状态`
- `16f83d75` `fix(server): 入站读取上限可配 + expiring_soon 文档对齐实现`
- 参考文件：`cmd/server/config.go` 的 `server` 段、`cmd/server/main.go`

**本地现状**

- `upstream/cmd/server/main.go:274` 硬编码 `ReadTimeout: 60 * time.Second`；
- `upstream/cmd/server/main.go:289` 硬编码
  `context.WithTimeout(context.Background(), 5*time.Second)`；
- `upstream/cmd/server/config.go:41` 的 `Server` 段是**空结构体**
  `struct{} \`json:"server"\``，注释说明 `max_body_mb` 已移除。

两个后果都是线上可复现的：

1. 反代链上传大 body 超过 60 秒会被掐成 `400 read body: ... i/o timeout`，
   客户端看到的是「请求没到」而不是「上传太慢」；
2. 5 秒关停窗口必然掐断在途 SSE。长流式响应合法时长可达分钟级，容器每次重启
   都会留下「内容正常但连接被重置」的客户端报错。

**建议动作**

1. `Server` 段改成两个字符串时长键（沿用 `cooldown.soft_rate` 的 `"600s"` 解析
   风格），默认 `read_timeout="300s"`、`shutdown_timeout="30s"`；
2. `main.go` 用解析后的值构造 `http.Server` 与 shutdown ctx；`0` 表示
   `ReadTimeout: 0`（不限）/ 立即退出；
3. `p.Flush()` 已经存在于关停路径（`main.go:282`），不需要新增；要补的是
   **`srv.Shutdown` 的错误处理**——现在写成 `_ = srv.Shutdown(shutdownCtx)`
   （L291），超时被完全吞掉，日志里看不出「有请求没排干」；
4. 超时时打 WARN 并带上错误（`context.DeadlineExceeded` 即「窗口内没排干」）。

**成本与风险**

- 成本：低。两个配置键 + 两处常量替换；
- 风险：`0` 语义必须写进文档——读不限 = 慢连接可以长期占用一个 goroutine；
  关停 30 秒会让 `docker restart` 变慢，属预期，不要因为「重启变慢」又改回 5s；
- 与 G-P0-2 的关系：G-P0-2 防「一次读死内存」，G-P0-3 防「一次读死连接」，两者不重叠。

**落地情况**

`cmd/server/config.go` 的 `Server` 段从空结构体改成 `max_payload_bytes` /
`read_timeout` / `shutdown_timeout`，新增 `normalizeServer` / `parseServerDur` 与
`ReadTimeoutDur` / `ShutdownTimeoutDur`；`main.go` 用配置值构造 `http.Server`
与 shutdown ctx，超时打 WARN，`0` 走 `srv.Close()`。

按上文那条「别照抄」的提示，本地**按注释语义实现**：显式 `0` → `srv.Close()`
立即退，而不是回落 5 秒。`config.example.json` 已同步补上 `server` 段。

---

### P0-4　CST 日界统一（`internal/cst`）

**能力**

固定 UTC+8 的**单一定义**：`Loc` / `Now()` / `Day(t)`。上游的自然日（签到、
额度、每日活跃、夜猫窗口、积分包到期）全部按北京时间零点切日，与宿主时区无关。

为什么需要单独一个包：网关可能跑在 Docker / NAS / 海外 VPS 上，那些机器的本地
时区不是 UTC+8。判定跟着宿主时区走，「今天」的边界就整体漂 8 小时——UTC 主机上
配 `activity_hours=[10]` 实际是 CST 18:00 跑，硬冷却的「次日 04:00」落在 CST
12:00，签到早已结束。这些错都不会报错，只会**静默少拿积分**。

**来源**

- `025cc107` `fix(schedule): 日界与钟点一律按 CST，不再随宿主时区漂移`
- 参考文件：`internal/cst/cst.go`

**本地现状**（移植前）

`time.FixedZone` 散在四处，且 scheduler 的日界用宿主时区：

| 位置 | 定义 |
|---|---|
| `upstream/internal/scheduler/travel.go:54` | `var cstZone = time.FixedZone("CST", 8*60*60)` |
| `upstream/internal/server/degrade.go:47` | `cst := time.FixedZone("CST", 8*60*60)`（`nextMidnightCST` 函数内） |
| `upstream/internal/upstream/growth_bonus.go:107` | `var cstShanghai = time.FixedZone("CST", 8*60*60)` |
| `upstream/internal/upstream/client.go:304` | `var softRateResetLoc = time.FixedZone("UTC+8", 8*60*60)` |
| `upstream/internal/scheduler/scheduler.go:295` | `loc := now.Location()`（**宿主时区**，`dueSlots` 的日界） |
| `upstream/internal/scheduler/scheduler.go:137` | `time.Date(..., now.Location())`（`nextFire`） |
| `upstream/internal/scheduler/random_schedule.go:66` | `time.Date(..., now.Location())`（`nextScheduledFire` 的日起点） |
| `upstream/internal/scheduler/random_schedule.go:40` | `time.Date(..., day.Location())`（`scheduledSlot`，跟随传入 `day` 的时区） |
| `upstream/internal/pool/cooldown.go:346/348` | `time.Date(..., now.Location())`（`nextDay4AM`，**硬冷却到次日 04:00**） |

后五处是同一个问题：`now.Location()` / `day.Location()` 是宿主时区，不是 CST。
注意 `random_schedule.go` 里 **两处**，只改 `nextScheduledFire` 不够——
`scheduledSlot` 会跟着 `day` 的时区把小时数重新解释一遍。

`cooldown.go` 的 `nextDay4AM` 是初版清单**漏掉**的一处，危害比 scheduler 那几处
更直接：UTC 主机上算出的「次日 04:00」落在 CST 12:00，签到早已结束，硬冷却白等
半天，而且不报错。它和 `scheduler.go:295` 属于同一类，必须一起改。

另外两处 `FixedZone` 不是「日界」而是「重置时刻」语义（`client.go:304` 的
429/6004 解封时间、`growth_bonus.go:107` 的积分包到期），迁移时不要顺手改成
`cst.Day()`，它们要的是 `cst.Loc` 而不是日键。

**建议动作**

1. 新增 `upstream/internal/cst`：`Loc`、`Now()`、`Day(t)`，与参考仓同接口；
2. 上面九处全部改用它；
3. **保留**「面向操作者的统计口径用宿主本地时区」的边界：日志按天切文件、面板
   报表按小时/按天分桶不动，那看的是「我的昨天」，不是上游的自然日；
4. 补一个跨时区测试：把 `TZ` 设成 `UTC`，验证 `Day()` 与签到槽位仍落在 CST。

**成本与风险**

- 成本：低。纯替换 + 一个测试；
- 风险：**最容易做错的是把操作者统计口径也一起改掉**。逐处确认调用方语义，
  只改「上游自然日」相关的判定；
- 与 G-P0-1 的关系：`runstamps`（M-附录 A 的 M-A-15）一旦落地，日键必须用同一个
  `cst.Day`，否则重启补跑标记会和签到槽位错位。

**落地情况**：`upstream/internal/cst/cst.go`（`Loc` / `Now` / `Day` / `At` /
`NextAt`）+ 9 处迁移全部完成。新增 `scheduler/cst_day_test.go`、
`pool/cst_day_test.go`、`server/cst_day_test.go` 三组跨时区回归测试，把「同一
绝对时刻在任何宿主时区下结果一致」钉住。

> 2026-11-14 补记：`pool` / `scheduler` / `server` 里还有一批**老测试**用
> `time.Local` 构造时刻、再直接比 `.Hour()`。生产代码早已固定 CST，但这些断言
> 跟的是 runner 的本地时区——GitHub Actions 的 Ubuntu runner 是 UTC，于是
> 「次日 04:00」被读成 20 点，发布工作流的 `go test ./...` 直接失败。已统一改成
> 用 `cst.Loc` 构造、用 `.In(cst.Loc)` 取小时：期望值不再随跑测试的机器漂移，
> 本地（CST）与 CI（UTC）结果一致。回归护栏是上面那三组跨时区测试——它们比较
> **绝对时刻**，本来就不受宿主时区影响。

---

## P1：建议同步（收益明确、成本可控）

### P1-1　`GET /v1/capacity?model=`

**能力**：对外暴露「这个模型现在有几号可服务」，返回
`model_available_accounts` / `available_accounts` 两个数。供面板和脚本在发请求前
判断容量，不必靠 503 撞出来。

**来源**：`afdfe936` `feat: expose authenticated model capacity counts`；参考文件
`internal/server/handler.go` 的 `capacity`。

**本地现状**：无此路由。`handler.go:186–210` 注册的是 chat / models / status /
stats / balance-refresh / admin / healthz。`/status` 有全池
`total/healthy/cooling/disabled`（`CountsDetailed`），但没有模型维度。

**建议动作**：参考实现是 `pool.CapacityForModel(model, realm) (modelAvailable,
poolAvailable)`（`internal/pool/state.go:498`）一次给出两个数，路由层再做三步：
先 `resolveModel`，再套 `modelAlias`（别名解析），最后 hidden / allowed 命中
就把 `modelAvailable` 归零。本地 `pool` 没有 `CapacityForModel`，但有
`AvailableUIDsForModel`（`state.go:309`）与 `AvailableUIDsForModelRealm`
（`realm.go:29`），按 model 参数（可带 `cn:` / `global:` 前缀）过滤即可。

注意：hidden / allowed 归零这一层依赖 G-P1-16 与 M-P2-3——**G-P1-16 已落地**，
所以现在可以直接用 `modelHidden` 判；M-P2-3（`models.allowed`）仍未做。别名解析
同理，依赖 G-P1-13（仍未做，Python `modelmap.py` 已覆盖直连以外的路径）。

**成本与风险**：低。只读接口，不改选号。

---

### P1-2　Go 侧 `/metrics`（Prometheus）

**能力**：`GET /metrics` 输出 Prometheus 文本 0.0.4，零第三方依赖；请求计数来自
`chatStat.done()` → `recordChatMetric()` 的现成累加器（`logging.go:61` /
`metrics.go:63`），池健康来自 `Pool` 的 realm 计数；不输出 uid。

**来源**：`0662608b`；参考文件 `internal/server/metrics_prom.go`。

**本地现状**：

- Python 侧已有 `/api/metrics` 与 `/metrics`（`server/routers/metrics.py:47/52`），
  外部观测走它没问题；
- Go 上游自己没有 `/metrics`。一旦直连上游、或只跑 Go 容器，就没有任何指标；
- `upstream/internal/server/metrics.go` 已有按模型的内存聚合（`/v1/stats`
  的数据源，含 requests/success/failed/ttfb/tokens/cache/credit），转成
  Prometheus 文本是纯映射。

**建议动作**：在 `metrics.go` 上加一个渲染函数，注册 `GET /metrics`；池健康用
`CountsDetailed` + `ServableForRealm`。指标前缀要与 Python 侧区分（例如
`wb2api_upstream_*`），避免两边同时抓取时重名。

**成本与风险**：低。风险是认证方式：`/metrics` 通常给 Prometheus 裸抓，用
`withAuth` 会让抓取端必须带 key。建议单独加一个 `metrics.public` 开关（默认
关，绑回环时再开）。

---

### P1-3　「思考吃光预算」救援重发（starved）

**能力**：当上游把 reasoning 与正文算在**同一份** `max_tokens` 预算里、且思考
独吞整份额度时，响应会结束于 `finish_reason=length`、正文零字节、没有
`tool_calls`——客户端看到的就是「空回复」。救援动作是「**同预算、关思考**」重发
一次（`thinking:{type:disabled}`），不放大客户端要的额度。

**来源**：`b0320cf1`；参考文件 `internal/server/starved.go`。参考仓的真机实测：
`max_tokens=60/100/150/200/300` 全部正文 0 字节，400 开始时好时坏，
关思考后同预算正常。

**本地现状**：无。`handler.go` 对 `finish_reason=length` 只透传；Python
`server/routers/responses.py` 有 length 分支（把 status 置为 incomplete），但
不重发。

**建议动作**：

1. 触发条件四条同时成立：`finish_reason=length` + 正文零字节 + 无 `tool_calls`
   + 预算 `>= 16`（`minStarvedBudget`；低于 16 是 key 探活，按原样失败）。预算
   三个键都要读：`max_tokens` / `max_completion_tokens` / `max_output_tokens`，
   取客户端实际传的那个；
2. 只重发一次，预算不变，body 里把 `thinking` 改成 `{type: disabled}`，并
   **删掉 `reasoning_effort`**——参考仓明确不赌「disabled + effort」组合的语义
   （上游 `thinking.go` 的 disabled 分支本来就要求两者不共存）；
3. 流式要缓冲「直到能判定零产出」——即收到 `finish_reason` 才能判定，不能看
   首帧；
4. 打 WARN 记模型、预算、原始 finish_reason。

**成本与风险**：中。流式判定要缓冲到结束，等于这一轮多占一份内存；误判会多发
一次上游请求（只多一次，不会放大）。

---

### P1-4　`budget` 积分闸（当日上限 + 突发 + 低缓存率）

**能力**：三道闸，按 CST 自然日惰性归零，进程内不落盘：

1. `daily_credit_limit`：当日累计 `usage.credit` 达上限即拒；
2. `burst_credit_limit`：5 分钟窗口内累计 credit 达上限即拒（高倍率模型一次
   140k 上下文就能吃 20+ 积分，日上限止血太慢）；
3. `cache_low_rate`：连续 5 轮缓存命中率低于阈值即冷 60 秒。

只累加**观测到的** `usage.credit`，缺失不等于 0。

**来源**：`0662608b` + `58262f31`；参考文件 `internal/server/budget.go`；配置段
`budget`。

**本地现状**：Go 侧无。Python 侧有 per-key 的 `quota` / `quota_credit`
（`server/keysvc.py:620/624`，拒绝码 `insufficient_quota`），那是**按密钥**的
额度；没有「整个实例当日烧了多少 credit」这一层。

**建议动作**：先做 `daily_credit_limit`（默认 0 = 不限），再做 burst；cache 闸
最后做，它需要先有稳定的 cache 命中率口径。

拒绝形状参考仓与直觉不同，**不是** `insufficient_quota`：它回
`429` + `code=daily_budget_exceeded`，message 里带「用了多少 / 上限多少 /
CST 00:00 重置」；快速闸（burst / cache）另用 `credit_guard_tripped`，
message 是闸自带的可读拒因。**不写 `Retry-After`**——当日额度要等到零点，
给一个秒数反而误导。两个码分开是有意的：客户端看到 `insufficient_quota` 会
判定「不可重试」，而当日额度到点会恢复。

`admitBudget()` 要在 `chatCompletions` 入口和「Responses 托管出图先走对话」
入口**共用**，否则两处的拒绝形状会漂移。参考仓还留了一个
`budgetAdmittedKey` context 标记：同一请求里出图已经过闸，后面的对话环节不再
重判——代价是最多超支一次对话，收益是「图扣了钱却交不出去」不会发生。

实现细节按参考仓的 `dailyBudget` 收敛：日键用 `cst.Day(now)`（依赖 G-P0-4，
否则日界随宿主时区漂移）；`admit()` 是**严格小于才放行**（`>= limit` 即拒），
拒绝原因要区分 `daily` / `burst` / `cache` 三种并各自计数——面板排障时
「是哪个闸在拦」比「被拦了」有用得多。三道闸都要能热改（`0` = 关），
改配置不应重置已累计的窗口与连败计数。

**成本与风险**：中低。最大风险是误拒真实流量——三道闸默认全部关闭，只在明确
需要止血时打开；缓存率闸依赖上游 `usage` 的缓存字段，缺失时应跳过而不是当成 0%。

---

### P1-5　`pool.reserve_credits` / `reserve_credits_by_realm`

**能力**：账号余额低于阈值时**停止派发**（默认 10），免费模型豁免；支持按域
覆盖（CN / global 共用一个数值时只能取更严的那个）。与 `credit_floor` 的区别：
`credit_floor` 是「不接实测收费模型」，`reserve_credits` 是「什么请求都不接，
把余额留着」。

**来源**：`d1995558`、`7be4c3b9`（按域覆盖，issue #147）、`5ee3b8fe`。

**本地现状**：`upstream/internal/pool/pick.go:69` 只有 `creditFloor` 过滤；没有
`reserveCredits`，没有按域覆盖。配置 `pool` 段里搜 `reserve_credits` 为 0 命中。

**建议动作**：`pick` 的候选过滤加一层 `credits <= reserve` 剔除；用 `*int64`
区分「键缺席 = 默认 10」与「显式 0 = 关闭」；按域时 `reserve_credits_by_realm`
优先，未写的域回落全局值；tier 0（免费）模型不受限。

参考仓的判据只有一行，但三个前提都不能少：

```go
func (p *Pool) reserveBlocked(e *entry, modelFree bool) bool {
    if modelFree { return false }
    t := p.reserveThreshold(e.a.Realm())
    return t > 0 && e.creditsTotal > 0 && e.credits <= t
}
```

`e.creditsTotal > 0` 是「**余额已知**」的哨兵：从未查过余额的号（刚装机、
`credits == 0` 但 `creditsTotal == 0`）不受影响，否则全新部署会看起来整池不可用。
本地 `entry.go` 只有 `credits` / `creditsExpiring`，**没有 `creditsTotal`**——
移植时要么先加这个字段（它同时是面板「总额度」的展示口径），要么用一个显式的
`creditsKnown bool`，别直接用 `credits > 0` 代替：那会把「余额真的 0」和
「还没查过」混成一类。

**两处容易漏的点**：

1. **全冷却兜底路径也要判**（`5ee3b8fe` 就是补这个）。本地 `pick.go` 的兜底分支
   会把「健康候选全被拦净」时的号捞回来；如果兜底不看 reserve，刚停牌的号会被
   重新选中接付费模型，reserve 形同虚设。更糟的是 `ServableForRealm` 按 reserve
   判「不可服务」，于是出现 `healthz` 报不可用、真请求却还能选到号的口径裂缝；
2. **免费模型豁免**：参考仓用 `modelFreeOf`（上游目录倍率 == 0）判断；本地没有
   这个函数，最接近的是 `pick.go:110` 的 `costTier`（`modelCostOf` 的实测扣费
   分层，tier 0 = 已实测免费）。豁免口径要选一个写死：按「目录倍率」还是按
   「实测扣费」，两者在限免期会给出不同答案。

**成本与风险**：低。语义边界要写进文档，否则会和 `credit_floor` 混淆。

---

### P1-6　`pool.account_priority` / `account_share`

**能力**：

- `account_share`：uid → 归一化流量占比。配了占比的号**独占本层流量**，按 share
  抽签；不在占比表里的号不参与这一层。优先级高于 `prefer_expiring`——配了占比的
  号独占本层，到期优先只对没配占比的号生效。
- `account_priority`：uid → 优先级层。高优先层的号先用完，才轮到下一层。

**来源**：`90eb0479` `feat(pool): 账号优先级与账号占比（issue #62）`。

**本地现状**：`auth.Weight` + `pool/pick.go` 的 `weightOf` 只有三个因子：
credits 占比、快过期积分占比、闲置补偿；没有 uid 维度的优先级或占比。

**建议动作**：先做 share（`pickByShare`：权重 = share，**不走 top5 截断**，否则
比例会失真），再做 priority。两者都要在 `/status` 或面板可见，否则运维无法验证
「我配的 30% 真的是 30%」。

**顺序是硬要求**（参考仓踩过）：

1. `account_priority` 的 `prioritizeLocked(cands)` 放在**成本分层之前**——人的
   显式配置压过自动启发式；
2. `account_share` 的 `sharePoolLocked(candsAll)` 放在**top5 截断之后**，且必须用
   **截断前的 `candsAll`**：截断按权重取前 5，低占比号会永远进不了短名单；
3. 两者都在「快过期虚拟实例加权」之前——显式占比压过自动启发式；
4. 占比层内号全不可用（冷却 / 在途满 / 停牌已被逐出）时**自动让位**回既有加权
   路由：占比是分配偏好，不是排他锁，不能把请求饿死。

零配置时两个函数都原样返回候选，对现有选号零影响——这一条要写成测试。

**成本与风险**：中。会直接改变现有流量分布；配错一个号就会把流量全压到它身上。
建议加「配置校验：share 之和必须 > 0，非正值剔除」。

---

### P1-7　`pool.model_fallback`（6004 等价降级）

**能力**：只在 **6004**（模型级限流）时，同账号把请求改道到等价模型再试一次；
限流解除后自动回原模型，不写持久状态。语义是「同一底层能力、只是计价不同」，
便宜的那个被限流时改走稍贵的继续服务。

**来源**：`0eb9e37d` `feat(server): 模型降级路由 model_fallback——6004 限流时同账号改道等价模型`、
`9b9174b1`（支持带域前缀的键，issue #127）。

**本地现状**：**没有**。另一份清单把「等价模型降级」标为已覆盖，实际那是 Python
`server/services/modelmap.py` 的**别名映射**：`target_for()` 把客户端模型名换成
真实模型名（`FALLBACK_KEY = '*'` 做兜底），是「请求入口的改名」，不是「运行时
的降级」。Go `handler.go` 对 6004 只做模型级冷却（`CooldownSoftForModel`），
然后换号。

**建议动作**：参考仓有**两个**触发点，只做第一个会在最需要降级的场景失效：

1. **上游返回 6004 之后**：先按**原模型**罚账号/记冷却，再同账号改道等价模型重试。
   关键是把该账号从 `tried` 里移除——降级是「换模型」不是「换号」，同一账号仍应
   有机会；
2. **选不到号时（`PickExcludingForRealm` 返回 nil）**：此时全池该模型都在 6004
   冷却里，如果只在「收到 6004 之后」降级，这里会**先报 503、根本走不到降级**。
   所以 `acct == nil` 分支也要尝试切模型，然后 `continue` 重新进入选号。

实现上要维护**两个模型变量**，这是最容易做错的地方：

- `curModel`：本轮**实际使用**的模型（选号 / 出站 body 重写 / 成本记账 / 提示语）；
- `bareModel`：**冷却记账**口径，始终是原模型。

把冷却记到等价模型名下，会把本来好好的等价模型也冷却掉，降级就白做了。
另需一个 `fallbackTried` 集合防 `A→B→A` 循环（与按账号去重的 `tried` 正交）。
`fallbackTo()` 里的拒绝条件：未配置 / 已试过 / 自指 / 目标被 hidden / 目标不在
allowed 白名单——最后两条让降级不能成为绕过模型策略的后门。

只对 6004 生效（11102 换模型也救不了；11101 / 普通 429 / 5xx 是账号级或瞬时问题）。
键查找顺序是**先 `realm+":"+model` 再裸名**（参考仓 2026-10-25 实案：只查裸名会
让 `global:xxx` 这类前缀键整表失效）。

**成本与风险**：中。降级目标必须是同能力模型，否则是**静默换模型**，用户拿到
的答案质量会变；默认 `nil` = 不降级，对老配置零影响。

---

### P1-8　`pool.balance_window` 窗口用量调度

**能力**：记录每号在窗口内的 token 用量，按用量**反比降权**，把热点号打散到
闲置号上，避免一个号烧到 6004 而同期其他号闲置。属软降权：`credit_floor`、
`account_priority`、`account_share` 这些硬语义不受影响。

**来源**：`b2d130d8` `feat(pool): 窗口用量调度，按 token 用量反比降权打散热点号（issue #112）`。

**本地现状**：无。`weightOf` 的三个因子（`pick.go:312` 起的注释块）只有
credits 占比、`creditsExpiring` 占比、闲置时长，全部是**当前状态**，不看窗口内
已消耗的 token。

**建议动作**：加按号的窗口计数器（参考仓是**翻滚窗口**，从窗口内首次记账起算，
不是滑动窗口——跨边界的最后一段会短暂少算，省掉每秒桶的复杂度）；`weightOf`
乘一个 `1 / (1 + used / mean)` 因子，`mean` 取**同层平均用量**（`sum/n`），
而不是固定阈值：量纲自洽，换池规模或模型倍率都不用调参；全员零用量时除数恒
`1`，行为与旧版逐字节一致。

三个容易做错的地方：

1. 这个因子要套**两处**——top5 排序与实际抽签。只套排序的话，penalty 只影响
   谁进短名单，不影响抽签概率，热点号仍会被抽中；
2. **只降权不排除**：极端热点权重趋近 0 但永不为 0。需要硬隔离就用
   `account_priority` / `account_share`；
3. 配置 `pool.balance_window`（`"0"` = 关闭）关闭时**不记账**，不是记了再乘 1。

注意与 `expiring_soon` 的加权叠加顺序，否则两个因子会互相抵消。

**成本与风险**：中。需要新的计数器与过期清理；窗口长度配得太短会让权重抖动。

---

### P1-9　`/v1/models` 的 models.dev 形状 + listing 补全

**能力**：在 `/v1/models` 条目上**追加** models.dev / OpenRouter 词表的能力声明，
让只认这套词表的客户端（如 magpie）能识别「能不能看图」「能选哪几档思考」：

- `modalities`：`{"input": ["text"] + (["image"] if supports_images), "output": ["text"] 或 ["image"]}`；
- `limit`：`{"context": <context_length>, "output": <max_output_tokens>}`，只搬运不计算；
- `tool_call` / `reasoning` 布尔；
- 三条硬规则：纯追加（不覆盖原有字段）、同源派生（不重复算上下文/输出上限）、
  不编造（没有数据源的字段整体省略）。

**来源**：`7490cfbf` `feat(server): /v1/models 追加 models.dev 形状能力声明 (#119)`；
参考文件 `internal/server/modelsdev_shape.go`。

**本地现状**：

- Go：`handler.go:352` 的 `addListingCompat` 只补 `modalities.input`（且只在
  `supports_images == true` 时）与 `supported_reasoning_levels`；**没有**
  `modalities.output`、`limit` 对象、`tool_call`、`reasoning` 布尔；
- Python：`server/routers/gateway.py:966` 调 `_listing_compat_models`（定义在
  L1203），口径与 Go 的 `addListingCompat` **基本一致**——同样只有
  `modalities.input`（同样只在 `supports_images == true` 时补）、
  `supported_reasoning_levels`、`supports_reasoning`，同样没有 `output` /
  `limit` / `tool_call` / `reasoning`；
- 两边都缺 models.dev 独有键，**不是「谁比谁新」而是两边都没做**；
- 参考仓的实现还多两样本地完全没有的：`reasoning_options`（档位列表 +
  `{"type":"toggle"}` 的关闭开关，把 `off`/`none`/`disabled` 从档位里折出去，
  避免客户端把 "off" 当成一个档）与 `imageOut` 分支（出图模型
  `output=["image"]` 且整段省略 `limit`）。

**建议动作**：Go 侧新增 `applyModelsDevShape`，Python 侧同步补同一份键；两边都
只追加、不覆盖。`limit` 直接搬已有的 `context_length` / `max_output_tokens`，
出图模型 `output=["image"]` 且不写 `limit`。`reasoning_options` 只在有档位时才
写，且要把关闭态折成 `{"type":"toggle"}`，不要混进 `values`。

**成本与风险**：低。唯一要守住的是「纯追加」——任何覆盖都会破坏现有客户端。
参考仓的验收口径是「原有输出逐字节不变」，本地也应照此写测试。

> 与 **G-P1-17** 的关系容易混淆：「关闭思考」这一个语义在参考仓要写进**三套**词表，
> 三种写法——models.dev 的 `reasoning_options` 里是 `{"type":"toggle"}`；
> magpie 私有方言 `supported_reasoning_levels` 里是 `{"effort":"none"}`
> （`off` 不被它的档位排序识别）；既有字段 `reasoning_supported_efforts` 里是
> 字符串 `"off"`。三处都要写，不能只写一个，也不要把 `off` 混进 models.dev 的
> `values` 数组。

---

### P1-10　`POST /admin/tasks/{name}/run`

**能力**：在 `admin.enabled` 下暴露任务触发口：`checkin` / `activity` /
`keepalive` / `travel` / `growth`。参考仓是**异步**语义——立刻回 `202 Accepted`
`{"task": name, "status": "started"}`，任务在后台 goroutine 里跑，**不返回**
每号结果摘要（结果看日志或 `/status`）。未知任务回 404 并列出已知名字，
同名任务已在跑回 409 `busy`。

**来源**：`0662608b`；参考文件 `internal/server/admin_tasks.go`。

**本地现状**：只有 `POST /v1/balance/refresh`（`handler.go:193`，且仅在
`Scheduler != nil` 时注册）。签到 / 活跃 / 保活 / 旅行 / 成长没有 HTTP 触发口；
Python 面板靠 `server/routers/accounts.py:1705` 的 `POST /task-run` 走
`upstream/scripts/task_runner.py`。

**建议动作**：`admin.enabled` 下注册 `/admin/tasks/{name}/run`，name 走白名单
映射到已有的 `RunCheckinNow`（`scheduler.go:422`）/ `RunActivityNow`（L702）/
`RunKeepaliveNow`（L946）/ `RunTravelNow`（`travel.go:65`）。`growth` 目前没有
独立的 `RunGrowthNow`——它挂在 `runActivity` 里（`scheduler.go:750`），要么加一个
只跑 `claimGrowthRewards` 的入口，要么先不暴露 growth。

参考仓用**按任务名的互斥表**（`taskRunning[name]` + `sync.Mutex`）防重入，比复用
scheduler 的 `ErrBusy` 更细：`checkin` 在跑不妨碍同时手动触发 `keepalive`。若本地
坚持复用 `ErrBusy`，就只能全 scheduler 串行，面板体验会差一截。

**成本与风险**：低到中。要防「面板连点两次触发两轮全池请求」——按任务名加互斥，
或直接返回 409。

---

### P1-11　`pool` 模型锁视图 + 手动开关 + 套餐分类

**能力**（三件独立的小事，按此顺序做）：

1. `modelview`：模型 × 域的聚合行——模型名、realm、总数、可服务数、锁定数、
   状态（`locked` / `starved` / `partial`）、最早解锁时刻、全部解锁时刻、锁因。
   只读，面板「模型锁池」视图直接用。与参考仓的 `ModelBlocked`（单次请求失败时
   才回答「这个模型此刻是不是全池被挡」）分工：这一份是全清单版本，不必先失败
   一次才知道哪些模型被锁。本地对应的「单次失败」回答是 `handler.go:1187` 的
   `no_healthy_account`，只区分「没号」不区分「被模型冷却挡住」。
2. `toggle`：人工把某个 `(账号, 模型)` 组合**永久**关掉，直到手动解除。与自动
   机制（6004 限流 / 11102 无此模型）语义正交；**手动优先**——自动恢复不能打开
   手动关掉的组合。存储只存被关闭的组合（稀疏），不存全矩阵。
3. `plan`：从上游积分包名判定「免费版 / 体验版 / 活动号 / 标准版」，用关键词
   宽松匹配（上游随时改文案），匹配不上返回 `unknown`，不猜。

**来源**：`545f706b`（modelview）、`0eb9e37d`（toggle）、`cac595a9`（plan）。

**本地现状**：

- `pool` 没有 `modelview.go` / `toggle.go` / `plan.go`；
- `/status` 的账号行有 `RateLimitedModels`（`entry.go:79` 的 `RateLimitedModel` 切片，
  含 `Model` / `Until` / `ResetAt` / `Reason`），但**没有**按模型聚合的视图；
- 没有人工永久关闭组合的入口；`/admin/accounts/{uid}/models/{model}/available`
  （`admin.go`）只能**清除**已有冷却，不能主动关闭；
- Python 侧没有套餐分类（`server/services` 里搜不到 trial/standard 的包名判定）。

**建议动作**：modelview 先做（只读、零风险）；toggle 第二（要写进 `state.json`，
与 G-P0-1 一起做）；plan 最后（纯展示，收益最低）。

toggle 的存储形态值得先定下来：`stateAccount` 加 `model_off`
（`map[model]stateModelOff`，`{since, note}`，`omitempty`），**只存被关闭的组合**
——全矩阵是「账号数 × 模型数」，号一多就膨胀。`model` 存**裸名**，不存
`cn:` / `global:` 前缀：前缀是网关侧路由协议，同一账号的域固定，带前缀存会在
「模型改名 / 换域」时对不上，剥前缀由调用方负责。

面板侧入口在参考仓是**一个** POST（`internal/panel/panel.go:1394` 的
`modelToggle`），body `{model, off, note}`，`off` 为真即关、为假即开，返回
`{ok, uid, model, off, list}`。`model` 接受带前缀形式（`cn:gpt-4`），入库前统一
剥前缀。本项目的面板是 Python，所以 Go 侧只要提供 `/admin` 下对应写接口，
面板路由照既有 `/admin/*` 风格接即可。

**成本与风险**：中低。toggle 的「手动优先」规则必须写死，否则「我关了它，一次
自动恢复又开了」会让人无法信任这个开关。

---

### P1-12　6020 / 6021 渠道排队语义

**能力**：6020（`retry_after`）/ 6021（`queue.full`）是**渠道级容量**信号，不是
账号问题。正确处置是：不罚号、不轮转，按上游给的等待时间回 `503` 或 `429` +
`Retry-After`，透传原文。

**来源**：`b0797d74`（6021 归渠道级容量）、`f07eba54`（6020/6021 改 429 +
`Retry-After`）、`65627315`（6020 带 `retry_after` 时不再换号重打，issue #125）；
参考实现 `ErrUpstreamBusy`。

**本地现状**：**完全没有识别**。`upstream/internal/upstream/client.go` 的
`ErrKind` 里没有 `ErrUpstreamBusy`；全仓搜 `6020` / `6021` / `queue.full` 均为
0 命中。这两个码现在会落进 `ErrClient` 默认分支，执行 `NoteFailures(uid)`——
**把渠道排队当成账号连败**，方向正好相反：越排队越把健康号降权。

**建议动作**：加 `ErrUpstreamBusy`；`Classify` 里用**结构化的业务码判定**
（本地已有 `hasBusinessCode`，`client.go:397`：只认 `"code":` 键，避免
requestId 之类的字段恰好含 `6020` 时误伤），插在**两处之间**：`client.go:645`
的 429+14018 → `ErrHardCredit` 之后、`client.go:652` 的 `status == 429` 兜底
**之前**——6020/6021 常以 429 返回，先走 429 分支就会被归成 `ErrSoftRate`；
参考仓对这两个码是**任意状态码都认**（防上游换状态码）。

等待时间要从 **body** 拿，不是头：参考仓真机（2026-10-28）看到 429 + code 6020
时，`data` 里有 `queue_position` / `queue_size` / `retry_after` /
`estimated_wait`，而 `Retry-After` 头根本没有。所以要单独写一个
`parseQueueRetryAfter(body)` 只认 `data.retry_after`（整数秒，超上限按异常值
封顶），解析不到再回落本地兜底。`msg` 是 i18n 键（`queue.waiting.title`），
里面没有数字可解析。

`applyErrorPolicy` 里该分支**零动作**（不冷却、不熔断、不 `NoteError`）；主循环
据此 `break`，把上游原文与 `Retry-After` 透传给客户端（终态 429，或按参考仓
早期口径 503；选一个写进文档，不要两处不一致）。

**成本与风险**：中。改错会把「渠道排队」当「账号故障」，或者反过来把真正的
账号限流当成排队放过去。两个码的 body 形态要先抓包确认。

**落地状态（2026-11 低风险批）**：✅ **已同步**，与 M-P1-12（11134 供应商过载）
合并成同一项落地，能力描述见
[upstream-sync-misakano.md](upstream-sync-misakano.md) 的 M-P1-12「落地状态」段。
要点：枚举名用中性的 `ErrUpstreamBusy`（6020 排队 / 6021 队列满 / 11134 过载
三形态同族）；`Classify` 用 `hasBusinessCode` 只认结构化业务码，且插在
429+14018 之后、`status==429` 兜底之前；`parseQueueRetryAfter` 认
`data.retry_after`（6020）与 `retry_policy.retry_after`（11134），超 2h 封顶；
`applyErrorPolicy` 该分支零动作。与上文建议的差异只有一处——终态统一
`429 + Retry-After`（不取 503），因为 WorkBuddy CLI 对 503 会直接中断任务。
来源提交 `48306862`（6020/6021 的原始提交 `b0797d74`、`f07eba54`、`65627315`
的能力已包含在内）。

---

### P1-13　`models.aliases`（Go 侧别名映射）

**能力**：配置 `models.aliases`（`客户端名=真实名`），把客户端固定的模型名落到
真实模型；支持域感知与 `*` 兜底；别名不能绕过黑名单。

**来源**：`e4ef71a7` `feat(server): 模型映射 models.aliases，客户端固定模型名落到真实模型（issue #30）`。

**本地现状**：Python 有完整实现（`server/services/modelmap.py` 的
`target_for()` / `fallback()` / `FALLBACK_KEY = '*'`，`gateway.py:832/1051/1089`
调用）；Go `handler.go` **没有任何别名处理**。只要外部流量永远走 Python，这就
不是缺口；一旦直连 Go，别名会 404 或撞 11102。

**建议动作**：先确认部署形态。若确实需要，在 `resolve_model.go` 之后加一层
alias（域感知、`*` 兜底），并保证别名解析发生在 hidden 过滤**之前**。

**现状决定（2026-11）**：第一批**不做**。理由与 G-P1-16 不同——hidden 只藏不
改语义，风险低；别名会改写用户请求里的模型名，直连 Go 时一旦两边映射表漂移，
表现是「同一个名字在面板和直连端口打到不同模型」，比 404 更难查。若将来要做，
优先让面板把 `modelmap.py` 的映射镜像进 `config.json`（与 G-P1-16 同一套机制），
而不是在 Go 里再维护一份。

**成本与风险**：低到中。主要风险是与 Python 的映射表双份维护、口径漂移。

---

### P1-14　`upstream.dump_dir` + `dump_max_bytes`

**能力**：显式配置目录时，每个 chat 请求落一个自包含文件——出站请求体 + 上游
原始响应字节，不经任何改写。单文件封顶（默认 8 MiB，**请求片段 + 响应体合计**），
文件名 `20060102-150405.000-<realm>-<uid前8位>.log`（可排序、不落完整 UID）。
默认空 = 不落盘、零开销。

还有一个独立的上限容易被漏掉：**请求体片段只写前 256 KiB**
（`dumpRequestMax = 1 << 18`，可被 `DumpRequestMax` 覆盖）。原因是长会话会把
`tools` 排在几百条 `messages` 之后，不截断的话请求片段就能吃光整个 8 MiB 配额，
响应一个字都留不下。

**来源**：`2892e6a8`（`WB2A_DUMP_UPSTREAM`，issue #149）、`575611b5`
（`upstream.dump_max_bytes`，issue #44）；参考文件 `internal/upstream/dump.go`。

**本地现状**：只有 `handler.go:609` 的 `WB2A_DUMP_REQ`：

- 固定阈值 4 MB（`dumpReqMinBytes`，`handler.go:108`）；
- 固定路径 `/app/data/last_request.json`，Windows 与容器外都不可用；
- 只落**入站**请求体，不落上游响应，排障时看不到上游到底回了什么。

**建议动作**：加 `upstream.dump_dir`（空 = 关）+ `dump_max_bytes`（默认 8 MiB）；
在出站响应包装处落「请求体片段 + 原始 SSE 字节」；保留现有 `WB2A_DUMP_REQ` 作为
轻量模式（只落入站请求体）。落盘用 `O_EXCL`，同名不覆盖；`MkdirAll` 与
`OpenFile` 失败都只 WARN 后**原样透传**，不能让排障开关把正常请求打挂。

**成本与风险**：低。正文含用户提示词，默认必须关；落盘失败只 WARN，不影响请求。

---

### P1-15　`schedule.keepalive_refresh_window`

**能力**：周期保活只刷新「剩余有效期 <= 窗口」或已过期的凭证，默认窗口
`168h`（7 天）。面板手动「全部保活」始终全量，不受影响。

**来源**：`764293b8` `feat(scheduler): 按临期窗口刷新 token（移植上游 PR #244）`。

**本地现状**：`upstream/internal/scheduler/scheduler.go:948` 的 `runKeepalive`
对池内所有有 refreshToken 的号全量刷新，没有窗口判断。

**建议动作**：加 `schedule.keepalive_refresh_window`（字符串时长，空 = `168h`，
非法或 <=0 **fail fast**）；排程路径只刷临期，手动路径不判断。

实现成本比看起来更低：本地已有 `auth.NeedsRefresh(within)`（`auth.go:250`，
`ExpiresAt <= 0` 视为需要刷新），排程分支里加一句
`if !a.NeedsRefresh(window) { skipped++; continue }` 即可。

参考仓的形态值得照抄：`runKeepalive(ctx, spread, selective)` 多一个 `selective`
参数——排程路径传 `true`（按窗口过滤），`RunKeepaliveNow` 传 `false`（全量）。
不要用「配置值 + 一个全局 bool」表达这个差别，参数化最清楚。

`runKeepalive` 现有的 `checked/ok/failed/skipped` 计数正好能把「窗口外跳过」
和「无 refreshToken 跳过」分开——建议把窗口外单独计数，否则日志里看不出
「只刷了 2 个号」是窗口在起作用还是池里就没号。

另外参考仓的**热更新**路径把 `0` 当作「不过滤（等价全量刷新）」，而**配置校验**
要求必须 > 0。两处口径不一致是历史遗留，本地实现时二选一写清，别同时保留。

还有一个容易忽略的边角：静态 API Key 账号没有 `expiresAt`。参考仓的
`NeedsRefresh` 对 API Key **恒 false**（`apikey_test.go:99` 专门锁这条），否则
「无 expiry」会被当成「已过期」，每轮保活都去 refresh 一个不能 refresh 的凭证。
本地若将来接 API Key 账号，这条必须同步；现在 `auth.NeedsRefresh` 的
`ExpiresAt <= 0 → true` 就是这个陷阱。

**成本与风险**：低。窗口配得太大等于没做，太小会漏刷——默认 7 天是参考仓实测值。

**落地情况**

- `scheduler.Config.KeepaliveRefreshWindow`；`runKeepalive(ctx, selective bool)`，
  排程传 `true`、`RunKeepaliveNow` 传 `false`；
- 日志新增 `not_due` 计数（窗口外跳过与「没号可刷」分开看）；
- `internal/config/schedule.go` 加 `keepalive_refresh_window`（默认 `168h`，空回落，
  非法/`<=0` fail fast）；
- `cmd/server/config.go` 加 `KeepaliveRefreshWindowDur` 并在 `normalize()` 解析。

按上文那条「两处口径不一致是历史遗留」，本地**只保留「校验拒绝 0」一种口径**：
运行期不再把 `0` 当「不过滤」。要全量刷新请配一个大值（例如 `"876000h"`），
不要用 `0` 表意。

---

### P1-16　`GET /v1/models` 的 hidden 过滤（Go 侧）

**能力**：Go 侧 `/v1/models` 也要过滤全局隐藏模型（裸名 / `cn:` / `global:`），
与 Python 侧 `modelpolicy.py` 同一口径。参考仓的 hidden 是**两道闸**：列表过滤 +
**点名调用直接拒绝**（知道名字硬调也不放行），后者本地 Go 同样没有。

**来源**：`b7297e5c` `feat(models): add models.hidden blacklist (list filtering + chat gating)`；
`5ce1a2f4`（补全生效面）。白名单 `models.allowed` 是后来的 `bd6f6cbb`，与 hidden
取交集（M-P2-3）。

**本地现状**（移植前）：Python `gateway.py:973` 调 `modelpolicy.filter_payload(payload)`，
L1419 另有一处点名调用判 hidden；Go `handler.go` 与 `internal/server` 全部非测试
文件搜 `hidden` / `modelpolicy` **0 命中**（只有测试用例里作为字符串字面量出现），
既不过滤列表，也不拦点名调用。外部流量走 Python 时看不出问题；直连 Go 时隐藏
名单完全失效。

**建议动作**

> **前提更正**：初版这里写的是「若确认 Go 端口永不直接对外，本项可降为文档说明」。
> 这个前提**不成立**：`upstream/docker-compose.yml` 里是 `ports: - "7863:7863"`，
> Go 端口是**发布到宿主机**的。同一份错误前提还被用来降级 G-P0-2（body limit）
> 与 G-P1-13（aliases）的优先级——这三项都要按「已经直连」重新评估。

在 `handler.go` 做**两道闸**，不要只做列表过滤：

1. `models` 输出前过滤（按 `id` 判定，与 Python `filter_payload` 同口径）；
2. `chatCompletions` 在 `resolveModel` 之后、**进入选号轮转之前**判 hidden，
   命中就回 `404` + `code=model_not_found`。只藏列表等于没藏——客户端存下 id 后
   照样能调；返回 404 而不是 403，是为了不额外暴露「这是一条策略」。

配置来源用 `config.json` 的 `models.hidden`，由面板单向镜像写入（见下）。Go 侧
热加载，否则「面板上藏了、直连端口还看得见」会成为常态。

**成本与风险**：低到中。难点是配置来源：Go 读 `config.json`，Python 的 hidden
在数据库里，两边要有一个同步点。

**落地情况**

- `upstream/internal/server/modelhidden.go`：`splitRealm` / `hasRealmPrefix` /
  `modelHidden` / `filterHidden`，与 Python `modelpolicy._matches` 同口径
  （裸名藏两域、`cn:` / `global:` 只藏对应域、忽略大小写）+ 7 项测试；
- `handler.go`：`Config.HiddenModels func() []string`、`models` 过滤、
  `chatCompletions` 点名 404；
- `cmd/server/hidden_models.go`：`hiddenProvider`，10 秒 TTL 热加载。
  **读失败或解析失败时保留上一份好值**，绝不清空——清空等于把整份隐藏名单
  一次性放开；
- `cmd/server/config.go`：`Models.Hidden []string`；
- `server/services/modelpolicy.py`：`save()` 在写 DB 之后调
  `mirror_to_upstream()`，把名单镜像进 `config.json` 的 `models.hidden`
  （读改写、保留其他键、原子替换；写失败只 WARN，不回滚 DB）；
- `server/main.py`：启动时补写一次存量名单（幂等），让升级后立刻生效；
- 测试：Go 侧 7 项匹配/过滤 + 7 项热加载；Python 侧 `test_model_policy.py` 13 项
  （含「只动 `models.hidden`」「空名单删键」「只读挂载不致命」）。

---

### P1-17　`/v1/models` 的思考声明：`can_disable_thinking` + `off` 档位

**能力**：`/v1/models` 里三处声明要与网关**实际行为**一致：

1. `can_disable_thinking`：该模型是否允许关思考（`thinking:{type:"disabled"}` /
   `effort:"off"`）；
2. `reasoning_supported_efforts` 里要能选到 `off`——`off` 是**开关不是档位**，
   上游的 `supportedEfforts` 本来不含它，但网关接受它，客户端要看到它才会把它当
   合法选项；
3. `reasoning_default_effort` 要写**网关实际生效**的默认档，不是上游自己的默认档。

第 3 条只在网关有档位策略层（M-B-15）时才成立：策略开启时网关会把缺档补成
`thinking.default_effort`（默认 `max`），客户端若按上游的 `high` 预填，正好把网关
的补档抵消掉。策略关闭时保持上游原值（网关不再干预）。

有一个反直觉的收尾：网关默认档解析成 `off` 时，参考仓是**删掉**
`reasoning_default_effort` 而不是写 `"off"`——`off` 不是可发送的档位，写进默认档
会让客户端发出一个语义含糊的值；客户端不填档位时网关自己会关思考。

**来源**：`b3623b59` `feat(server): /v1/models 按网关策略声明思考档位`；参考文件
`internal/server/listing.go`（`declareReasoning`，调用点在参考仓 `handler.go:682` /
`:742`）。

**本地现状**：

- `can_disable_thinking` **完全没有**：`upstream.ModelInfo`（`client.go:1573`）没有
  这个字段，`dynModelEntry`（`client.go:1599`）也没解析，`/v1/models` 自然不输出
  （全仓搜 `can_disable_thinking` / `CanDisableThinking` 0 命中）；
- `off` **能被接受但从不声明**：`payload.go:139` 的 `effortRank` 里 `"off": 0`，
  `thinking.go:169` 的 `disabled` 分支也按「关思考且删 effort」处理——即网关行为上
  允许 off，但 `EffortListing`（`effort_catalog.go:111`）只搬远端/静态档位表，
  不会追加 `off`；
- `reasoning_default_effort` 有，但只是**透传上游值**（`handler.go:462` / `:506`）：
  `EffortListing` 只在「上游默认档恰好在本模型的档位表里」时原样返回，不做任何
  网关侧改写；
- Python 侧同样没有：`server/routers/gateway.py:1203` 的 `_listing_compat_models`
  只补 `modalities.input` / `supported_reasoning_levels` / `supports_reasoning`，
  搜 `can_disable_thinking` / `reasoning_options` 0 命中。

结果是「支持哪些档位」与「能不能关」两份声明互相矛盾：客户端看到 `off` 不在列表里，
就不会给出关闭选项，即使网关完全支持。

**一个会踩的顺序陷阱**：本地**两侧**都是**从 `reasoning_supported_efforts`
派生** `supported_reasoning_levels` 的——Go 是 `addListingCompat`
（`handler.go:352`，每项 `{"effort": e}`），Python 是 `_listing_compat_models`
（`gateway.py:1239`，同样的推导）。如果先把 `off` 追加进
`reasoning_supported_efforts` 再派生，就会产出 `{"effort":"off"}`——而 magpie
那条私有方言的档位排序只认 `none/minimal/.../max/ultra`，`off` 会被忽略，等于
「关闭选项没出现，还多了一条无效档位」。参考仓的做法是**不再从前者派生**：
`supported_reasoning_levels` 由「档位列表 + `disable` 标志」独立构造，`disable`
时插一条 `{"effort":"none"}`，与 `reasoning_supported_efforts` 的 `"off"` 各写
各的。本地移植时照这个拆开最省事，别复用现有的派生逻辑（两侧都要改）。

**建议动作**：

1. `ModelInfo` / `dynModelEntry` 补 `CanDisableThinking`（上游字段名待抓包确认），
   `applyModelInfoFields` 里为真才写出（沿用「零值省略、不编造」）；
2. 在 `EffortListing` **之后**加一层收尾（不要塞进 `EffortListing` 内部——它只负责
   「哪些档位可用」，不认识 `can_disable_thinking`）：为真且档位表里没有 `off` 时
   追加 `off`，去重要**大小写不敏感**（参考仓 `hasEffort` 用 `EqualFold`，与
   payload 侧归一化同口径）；档位表为空时整段不动，不编造。同时把
   `supported_reasoning_levels` 改成独立构造（见上面的顺序陷阱）；
3. 第 3 条**不要单独做**，等 M-B-15 的策略层落地后一起改；在此之前保持透传，避免
   引入「改写了一个还没生效的默认值」这种半成品；
4. 补测试：`off` 追加幂等、无档位表时不编造字段、策略关闭时 default 不变、
   默认档解析成 `off` 时该键被删除而不是写成 `"off"`、`supported_reasoning_levels`
   里出现的是 `none` 而不是 `off`。

**成本与风险**：低。纯追加，不改路由；唯一风险是 `can_disable_thinking` 的上游字段名
要确认——写错就永远是 false，功能静默失效。

---

## P2：可选 / 需要先评估

### P2-1　`admin.audit_enabled` + `/admin` 操作审计

**来源**：`0662608b`；参考文件 `internal/server/admin_audit.go`。

**本地现状**：Python 有 `audit_logs` 表与 `GET /audit-logs`
（`server/routers/settings.py:528`）；Go `/admin` 的
禁用 / 启用 / 复活 / 清冷却操作**没有**审计记录。

**建议**：若面板已经记录了所有管理操作，Go 侧可只记「谁在什么时候调了
`/admin/*`」一行 JSONL，默认关。

**成本/风险**：低。

### P2-2　`livecfg` 热加载

**来源**：`fd64aedb`；参考文件 `internal/livecfg/livecfg.go`。

**本地现状**：`server/services/reload.py` 明确写着「config.json 只读一次，改配置
必须重启容器」，实测 `docker restart` 到可用约 0.45s；Go 侧 `main.go:39/44`
只在启动时加载一次。

**建议**：收益是「改 schedule 不必重启」，成本是并发读写快照与一致性。当前重启
足够快，除非 schedule 调整非常频繁，否则不做。

**成本/风险**：中。

### P2-3　`httpauth`（常量时间 SHA-256 比较）

**来源**：`fd64aedb`；参考文件 `internal/httpauth/httpauth.go`。

**本地现状**：`handler.go:226` 已经用
`subtle.ConstantTimeCompare([]byte(provided), []byte(h.cfg.APIKey))`，比较本身是
常量时间的，只有「长度不同时提前返回」这一点会泄漏长度。

**建议**：**不建议移植**。长度泄漏的信息量极低，换来的是一层不必要的哈希。

**成本/风险**：低，但收益几乎为零。

### P2-4　登录链路版本对齐（`cmd/login`）

**来源**：`244d4e65`、`f0c427d2`、`4537f83a`、`96b816da`；参考文件
`internal/loginsite/loginsite.go`。

**本地现状**：`upstream/cmd/login/main.go:41`
`clientUA = "CLI/2.63.2 CodeBuddy/2.63.2"`，比业务链路的
`defaultClientVersion = "5.5.4"` / `defaultCliVersion = "2.137.1"`
（`headers.go:21`）还要旧。参考仓登录链路是 `CLI/2.176.0`，桌面端
`5.9.3`（CN）/ `5.9.4`（GL），内置 CLI `2.165.0`。

**建议**：业务链路的版本对齐 M-B-19 已列；本文只补一点——**登录链路是单独
一份常量**，改业务链路时容易漏掉它。版本串确实会进风控判定（参考仓记录：落后
两个小版本时登录页按旧客户端渲染并埋点）。

**成本/风险**：低，但要真实抓包复核，不要凭 npm dist-tag 猜。

### P2-5　`internal/probe`（测量工具）

**来源**：`probe169.go`、`probe_channel.go`，均为 `//go:build ignore`。

**本地现状**：无。

**建议**：**不移植**。它们是参考仓自己的抓包/测量工具，不参与运行时；如果要
复现某项结论，按需读一遍再本地写脚本即可。

**成本/风险**：无。

### P2-6　`internal/updater`（二进制自替换）

**来源**：`79897f3e`。

**本地现状**：Python `server/services/updater.py` + `deploy/update.py` 已覆盖
面板与上游的更新。

**建议**：**不移植**。两套更新器会互相踩。

**成本/风险**：无。

### P2-7　`internal/reqlog` / `usage` / `balance` / `credithist`（Go 侧观测）

**来源**：`5eafcda6`（reqlog）、`9cb846e2`（usage 聚合）、`e1735298`（balance
采样）、`e1552204`（credithist，积分历史）。

**本地现状**：

- Python 侧已经有完整的请求日志（`request_logs`）、小时/日聚合
  （`usage_hourly` / `usage_daily`，`server/routers/stats.py:353/426`）与余额
  变动流水（`server/services/credits.py:75` 的 `record_balance`，只记增量、
  只记增加）；
- Go 侧这四个包一个都没有。

**建议**：**不建议在 Go 侧全量重写**——双运行时重复记账，口径必然漂移。若确实
需要「Go 独立可观测」，只做最小 `reqlog`（JSONL、保留天数/上限、队列丢弃计数），
余额采样与用量聚合继续留在 Python。

**成本/风险**：中；收益低。

### P2-8　上游模型目录指纹监控（`WatchCatalog`）

**能力**：周期对「客户端实际看到的 `/v1/models`」取指纹（条目按 id 排序后
sha256，与顺序无关，任一字段变化即变），变了就经现有告警通道推送；未配通道时只写
日志。首轮只建基线不通知（否则每次重启都报一次「变化」）。配置
`watchdog.enabled` / `interval_seconds`，**默认关**。

为什么要它：上游改目录 / 改能力字段（换模型名、加档位表、改 `can_disable_thinking`、
下架某模型）时，客户端只会表现为「报错」或「选项没了」，看不出是上游改的。盯住
这份指纹，变化能第一时间落到告警，而不是等用户来报。

关键设计是**不新起抓取**：指纹取自网关自己维护、已被各类轮询刷新的那份
`modelList()`，零新增上游调用，而且盯的正是客户端真正拿到的东西（含 hidden /
allowed 过滤后的结果）。

**来源**：`49f9db49`（第 4 件事，移植 ithtelab/workbuddy-manager #295 的最小形态）；
参考文件 `internal/server/watchdog.go`（`WatchCatalog` / `catalogFingerprint` /
`catalogDiff`），调用点 `cmd/server/main.go:600`。

**本地现状**：

- Go 侧**没有**：全仓搜 `WatchCatalog` / `catalogFingerprint` 0 命中；
- Python `server/services/watchdog.py` 有完整的监控骨架——目标列表、HTML/JS
  归一化、sha256 指纹、快照落盘（`_save_snapshot` / `load_snapshot`）、变化判定
  （`_should_notify`）、Webhook / 邮件 / QQ 通道——但它的 `targets` 是**外部页面
  URL**，监控的是页面改版与补偿领取，**不监控模型目录**（搜 `/v1/models` 0 命中）。

**建议**：**不移植 Go 侧实现**，在 Python watchdog 里加一个「内置目标」类型即可——
直接读本机网关的 `/v1/models`（或复用它已有的快照表），走同一套指纹与告警通道。
这样告警只有一条链路、不需要第二份 webhook 配置，也不用把模型目录再抄一份到 Go。

实现时照抄参考仓的三条细节：排序后哈希（否则上游调换顺序就误报）、首轮只建基线、
变化消息只列前若干个新增/下线模型（`catalogDiff` 的做法，避免一条消息刷屏）。

**成本/风险**：低。默认关；唯一要小心的是别和现有页面改版告警互相刷屏——建议
共用「同一目标静默 N 分钟」的抑制逻辑。

---

### P2-9　其余已由另一份清单覆盖、本文只做索引的项

以下能力参考仓有、本地缺，但 `upstream-sync-misakano.md` 已经展开到「文件 + 行号
+ 建议动作」的粒度，本文不重复。表里「M-位置」一列全部是**那份文档的编号**，
带 `M-` 前缀以免与本文同名条目混淆（两边的 `P2-8` 分别指「10105 会话自救」与
「模型目录指纹监控」，是完全不同的两件事）：

| 能力 | M-位置 | 参考文件 |
|---|---|---|
| 11140 风控连坐保护 | M-P0-1 | `internal/server/restrict.go`、`internal/upstream/restrict.go` |
| 覆盖凭据前先验证 | M-P1-1 | `internal/panel/import.go` |
| 盲盒消费 | M-P1-2 / M-A-13 | `internal/upstream/travel.go`、`scheduler/travel.go` |
| 截断自动续写 | M-P1-3 | `internal/server/continue.go` |
| `/v2/chat/busy` | M-P1-4 / M-B-17 | `internal/server/busy.go`、`internal/upstream/modelbusy.go` |
| 媒体接口（图/视频/去水印） | M-P2-1 | `images.go`、`videos.go`、`watermark.go` |
| MCP / Embeddings / search / fetch | M-P2-2 | `mcp.go`、`embeddings.go`、`search.go`、`fetch.go` |
| `models.allowed` 白名单 | M-P2-3 | `internal/server/handler.go:625`（`allowedModel`） |
| 池健康告警 | M-P2-4 | `internal/alert/alert.go` |
| 出站 TLS 指纹 / 头顺序 | M-P2-7 / M-B-22 | `tlsfingerprint.go`、`headorder*.go`、`headcase.go` |
| 10105 活跃会话自救 / 网页活跃腿 | M-P2-8 | `internal/upstream/webagent.go`、`scheduler/webactivity.go` |
| DSML 工具调用还原 | M-B-1 | `internal/upstream/dsml.go` |
| 会话轮级 body 字段 | M-B-2 | `internal/upstream/conv_fields.go` |
| Kimi 工具 `$ref` 展开 | M-B-3 | `internal/upstream/kimi_tools.go` |
| usage 缓存别名归一 | M-B-4 | `internal/upstream/usage.go` |
| 401 三态求证 | M-B-5 / M-A-18 | `internal/upstream/verify.go`、`scheduler/balance_401.go` |
| global 余额 summary/paid/free | M-B-7 | `internal/upstream/billing_summary.go` |
| 签到活动状态 | M-A-7 / M-B-8 | `internal/upstream/checkin_activity.go` |
| global 账号签到 / 成长奖励解耦 | M-A-5 / M-A-6 | `scheduler/checkin_global.go`、`scheduler/growth.go` |
| 礼包状态查询 | M-A-1 / M-B-9 | `internal/upstream/benefit.go` |
| 国际版注册 / 地区 | M-B-10 | `internal/upstream/global_register.go` |
| 账号昵称同步 | M-B-11 | `internal/upstream/profile.go` |
| 桌面事件 / 外观 | M-B-12 | `internal/upstream/desktop.go` |
| 模型 picker 裁剪 | M-B-14 | `internal/upstream/clientpick.go` |
| 思考档位策略层 | M-B-15 | `internal/upstream/effort_policy.go`（`/v1/models` 侧的声明改写见本文 **G-P1-17**） |
| 分段耗时观测 | M-B-16 | `internal/upstream/timing.go` |
| 补签范围 / 兑换档位 / 抽奖次数 | M-A-2 / M-A-3 / M-A-4 | `scheduler/streak.go`、`streak_extras.go` |
| 成长任务队列 / 补领清扫 | M-A-9 / M-A-14 | `scheduler/growth.go`、`sweep.go` |
| 重启补跑标记 | M-A-15 | `scheduler/runstamps.go` |
| 账号级摊开 / 分钟级抖动 | M-A-16 / M-A-17 | `scheduler/spread.go`、`humanize.go` |
| 代理池 | M-B-24 | `internal/proxypool`、`upstream/proxy.go` |

### 基线推进（`7a48d72e`）新增的索引行

以下能力来自 `49fad282` 之后的 76 个非 merge 提交，**展开在 misakano 那份**，
本文只做索引，避免两处各写一份：

| 能力 | M-位置 | 参考文件 |
|---|---|---|
| 签到「请稍后重试」分钟级补签 | M-P0-2 | `scheduler/checkin_retry.go` |
| 单个 `data:` 行多 JSON 拆分 | M-P0-3 | `upstream/sse_frame_split.go` |
| `$wbEncrypted` auth 文件解密 / 回写 | M-P0-4 | `auth/wbencrypted.go` |
| 只发 `user_id` 也派生粘性键 | M-P1-5 | `server/handler.go`（`DeriveCacheKey`） |
| 请求级错误不解绑粘性 | M-P1-6 | `server/handler.go`（`failKeepBinding`） |
| 首帧前 SSE 注释帧 | M-P1-7 | `server/headkeepalive.go` |
| 空 text 段补位 / reasoning 镜像 | M-P1-8 | `upstream/empty_text_part.go`、`reasoning_mirror.go` |
| `<think:...>` 拆回 thinking 块 | M-P1-9 | `upstream/thinktag.go` |
| 输出预算下限 | M-P1-10 | `upstream/output_budget.go` |
| 断流补截断终态 | M-P1-11 | `upstream/sse.go` |
| 11134 供应商过载 | M-P1-12 | `upstream/client.go`、`server/handler.go` |
| 签到回执四态 | M-P1-13 | `upstream/checkin_receipt.go` |
| 清洗上游注入的身份指令 | M-P1-14 | `upstream/sanitize.go` |
| `/v1/models` 透出 promo 字段 | M-P1-15 | `server/handler.go` |
| 免费号事前避让 `exclusive` 模型 | M-P2-10 | `pool/pick.go`、`billing_summary.go` |
| 运营活动批量读进度 / 领奖 | M-P2-11 | `upstream/campaign.go` |
| 日志落盘 / 劣化告警 / 选号策略 | M-P2-12 | `panel/logstore.go`、`server/upstreamhealth.go`、`pool/strategy.go` |
| 目录热重载护栏 | M-P2-13 | `panel/reload.go`、`pool/pool.go` |
| 同 uid 多文件按 mtime 最新胜出 | M-P2-14 | `auth/auth.go` |
| `isFirstPage` 改布尔 | M-P2-15 | `upstream/desktop.go` |
| 资料接口空昵称判失败 | M-P2-16 | `upstream/profile.go` |
| 续写 / 救援链 | M-P2-17 | `server/continue.go`、`narration.go`、`toolonly.go` |
| `WB2API_PPROF` 独立 pprof | M-P2-18 | `cmd/server/main.go` |
| 代理出口归属（proxypool） | M-P2-19 | `internal/proxypool/*` |

> 这张表只解决「去哪找」。**不要**在本文再写一遍建议动作——两份文档同一项
> 只在一处展开，改的时候也只改一处，否则很快就对不上。

---

## 已核对：不是缺口

以下能力在分析时逐项核对过，本地**已经有**，不要因为「参考仓有同名文件」就重复
实现：

| 能力 | 本地落点 |
|---|---|
| 11133 请求级错误 fail-fast | `handler.go:1058` 就地判 `ErrClient` + 11133 后直返 400；`upstream/hint.go:125` 的 `isModelParamInvalid` 提供 hint。**没有**独立的 `ErrModelParamInvalid` kind——功能在，只是落在内联分支而不是枚举里 |
| WAF IP 级 fail-fast | `upstream/internal/server/wafip.go` |
| 大请求 sticky / rotate | `handler.go` 的 `largeUsed` + `pool.large_request_*` |
| Redis 镜像 | `upstream/internal/redisstore` |
| 会话粘性 | `session_sticky` 配置 + `session` 包 |
| 企业用量 | `upstream/internal/upstream/enterprise_usage.go` |
| `reasoning_as_content` 回退 | `upstream/internal/upstream/reasoning_fallback.go` |
| 截断检测 | `upstream/internal/upstream/truncation.go` |
| 工具配对修复 | `upstream/internal/upstream/tool_pairing.go` |
| 指纹脱敏 | `upstream/internal/upstream/sanitize.go` |
| prompt 三模式（passthrough/custom/append） | `upstream/internal/prompt` + `config.go` |
| gzip body | `upstream.gzip_body` |
| 刷新风险（refresh_risk） | `pool/refresh_risk.go` + `server/refresh_risk.go` |
| 6004 模型级冷却 | `pool.CooldownSoftForModel` + `modelCooldowns` |
| 思考档位列表（部分） | `upstream.EffortListing`（`effort_catalog.go:111`）已输出 `reasoning_supported_efforts` + `reasoning_default_effort`，远端优先、静态表兜底；**缺** `can_disable_thinking`、`off` 追加、默认档改写，见 **G-P1-17** |
| 账号代理路由 | `server/services/proxyroutes.py` + `upstream/internal/upstream/transport.go` |
| 成长 streak / redeem / lottery / 补签 / 热力图 | `upstream/internal/upstream/growth_*.go` + `scheduler.go:793`。**能力在**，但范围比参考仓窄（补签只补昨天、兑换只取一档、抽奖只抽一次、无热力图补签依据），逐项差异见 M-附录 A 的 A-1～A-4 |
| 多平台适配器（Trae / Qoder / combo） | `server/platforms/**` |
| Python 侧 listing compat（部分） | `server/routers/gateway.py` 的 `_listing_compat_models` 补 `modalities.input` / `supported_reasoning_levels` / `supports_reasoning`；models.dev 独有键两边都缺，见 **G-P1-9** |
| `cmd/stats` / `cmd/activity` / `cmd/acct` / `cmd/credit` | 本地独有，参考仓没有 |
| Python 侧小时/日用量与余额流水 | `server/routers/stats.py`、`server/services/credits.py` |

---

## 建议实施顺序

### 第一批（已落地，未提交）

六项，按依赖顺序做的，现在全部可用：

1. **G-P0-1 `safefile`** → **G-P0-2 body limit** → **G-P0-3 超时/关停** →
   **G-P0-4 CST**。`safefile` 必须排第一：后面的 `toggle`、`runstamps` 都要
   一个不会读成空的写盘底座；CST 排最后，因为它会碰 scheduler 的日界，放在
   其它改动之后好单独回归。
2. **G-P1-15 `keepalive_refresh_window`**、**G-P1-16 Go 侧 hidden 过滤**。
   这两项不改选号、不改流量分布，可以和 P0 混在一起发。

`go build ./...` / `go vet ./...` / `go test ./... -count=1` 全过。新增测试：

| 文件 | 覆盖 |
|---|---|
| `internal/safefile/safefile_test.go` | 7 项：fsync / `.bak` / 损坏回退 / 两边都坏 / 不存在不复活 |
| `internal/cst/cst_test.go` | 6 项：`Day` / `At` / `NextAt` 与宿主时区无关 |
| `internal/server/bodylimit_test.go` | 7 项：超限 413 / 排空 / 慢上传 WARN |
| `internal/server/modelhidden_test.go` | 7 项：裸名藏两域 / 前缀只藏一域 / 不误伤 |
| `cmd/server/hidden_models_test.go` | 7 项：热加载 / 读失败保留上一份好值 |
| `cmd/server/server_config_test.go` | 5 项：`server` 段解析与 `0` 语义 |
| `cmd/server/example_config_test.go` | 1 项：示例配置能被 `Load` |
| `internal/scheduler/keepalive_window_test.go` | 窗口过滤 + `NeedsRefresh` 的 API-Key 陷阱 |
| `internal/config/keepalive_window_test.go` | 空回落 / 非法与 `<=0` fail fast |
| `scheduler/cst_day_test.go`、`pool/cst_day_test.go`、`server/cst_day_test.go` | 三处日界的跨时区回归 |

Python 侧 `server/tests/test_model_policy.py` 13 项（镜像写入 9 项 + 匹配口径
4 项）。三处有意偏离参考仓的地方见文首「同步状态」表下方。

### 2026-11 低风险批（已落地，未提交）

九项，来自 [upstream-sync-misakano.md](upstream-sync-misakano.md) 的一页摘要
前 9 项（M-P0-3、M-P2-14、M-P2-15、M-P1-11、M-P1-6、M-P1-8、M-P1-14、M-P1-12、
M-P1-15、M-P0-2），明细见文首「同步状态」的对应小节，这里只记顺序：M-P0-3 →
M-P2-14 → M-P2-15 → M-P1-11 → M-P1-6 → M-P1-8 → M-P1-14 → M-P1-12（依赖 M-P1-6
的 `failKeepBinding`）→ M-P1-15 → M-P0-2。`go build` / `go vet` /
`go test ./... -count=1` 全过，九项全部落地。

### 第二批（改流量分布，需要灰度）

按「影响面从小到大」排，每做一项都要在 `/status` 或面板上能验证，**不要合并成
一个大提交**：

1. **G-P1-5 `reserve_credits`**：只改「选号时是否预留额度」，失败面最小，先做；
2. **G-P1-7 `model_fallback`**：6004 时同账号改道等价模型。依赖上一步的额度
   预留，否则降级后额度判定会错；
3. ~~**G-P1-12 6020 / 6021 渠道排队语义**~~：**已提前落地**（见 G-P1-12 的
   「落地状态」，与 M-P1-12 合并成一项，随 2026-11 低风险批完成，不需灰度）；
4. **G-P1-6 `account_priority` / `account_share`**：直接改权重分布，灰度时
   盯每号 QPS 与冷却次数；
5. **G-P1-8 `balance_window`**：窗口用量调度，最后做——它需要前四项的计数
   口径稳定，否则窗口里的「已用」没有意义。

### 第三批（观测 / 管理，按需）

`/metrics`（G-P1-2）→ `/admin/tasks/*`（G-P1-10）→ modelview / toggle / plan
（G-P1-11）→ `budget`（G-P1-4）。其中 `budget` 依赖 `/metrics` 或至少 `chatStat`
的 credit 累加，放最后以免「闸做了但看不见它在拦什么」。

`/v1/capacity`（G-P1-1）、models.dev shape（G-P1-9）、`can_disable_thinking` +
`off` 档位（G-P1-17）、`dump_dir`（G-P1-14）都是纯追加，随时可做，不占灰度窗口。
（Go 侧 `aliases` 见下「明确不做」。）

### 明确不做

| 项 | 理由 |
|---|---|
| G-P2-3 `httpauth` | 本地已是常量时间比较，收益近乎为零 |
| G-P2-5 `internal/probe` | 开发期测量工具，不进发布链路 |
| G-P2-6 `internal/updater` | 二进制自替换与容器部署冲突 |
| G-P2-7 Go 侧 `reqlog` / `usage` / `balance` / `credithist` | Python 侧已有等价观测，Go 再记一份是重复 |
| G-P2-2 `livecfg` | `docker restart` 到可用约 0.45s，不值得引入并发快照 |
| Go 侧 `/v1/responses`、`/v1/messages` | 外部协议入口在 Python 侧，Go 只做 chat 主链路 |
| 媒体接口、search / fetch / MCP | 同上，面板侧已有 |
| uTLS / TLS 指纹、头顺序 | 收益不明确，维护成本高 |
| 10105 网页活跃自救 | 已判定不做（见 M-P2-8） |
| G-P1-13 `models.aliases`（Go 侧） | Python `modelmap.py` 已覆盖非直连路径；只有确认「直连 Go 也要别名」时才做，且优先由面板把别名镜像进 `config.json`，与 G-P1-16 同一套机制 |
| G-P2-8 目录指纹监控 | **写进 Python watchdog**，不在 Go 里加 goroutine |
| Windows README 段落 | 与代码无关 |

### P2 剩余

`admin` 审计（G-P2-1）、登录链路版本（G-P2-4）按需，成本低但优先级低。

## 维护方式

1. 真正移植某一项后，在 `deploy/workbuddy-panel-ref.txt` 追加对应提交号，并在
   提交信息里写清「来源仓库 + 提交号 + 只取了哪部分」；
2. 如果只移植了一个提交里的一部分，在 ref 文件里注明「部分同步」，避免下次
   误以为整提交已合入；
3. 本文与 `upstream-sync-misakano.md` 是**两份互补的清单**：新提交带来的能力写进
   那一份，基线之前从未移植的能力写进这一份；同一项只在一处展开，另一处只加
   索引行；
4. **跨文档引用带前缀**：`G-` 指本文，`M-` 指那一份。两边都有 `P0-1`、`P2-8`
   这样的裸编号，不带前缀就分不清指的是哪一项；
5. 本清单是**分析结果**，不是承诺：优先级会随线上问题变化，实施前再核对参考仓
   当时的最新实现。
