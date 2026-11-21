# 上游源码的布局与同步（维护者）

本项目是**单仓**：面板（Python / Next.js）与上游网关（Go，`workbuddy2api`）在同一个
git 仓库里，上游源码就是 `upstream/` 这个普通目录。

> 这篇文档是维护者文档，不进发布包。发版流程本身见
> [release-process.md](release-process.md)。

## 为什么放在同一个仓库

早先上游放在独立仓，用「快照资产 + 顺序补丁 + `deploy/upstream-pin.json`」对齐。
那种方式的代价：每次改上游都要重打资产、重传 Release、更新 pin 与 sha256；补丁顺序
切片，上下文一漂移就要人工重做（`patch does not apply`）；面板与上游的改动没法放在
一起看。

现在源码就在同一棵树里：改完直接提交，发布包用 `git archive HEAD:upstream` 导出，
不从任何外部地址下载源码。

## 布局

```text
upstream/
  cmd/ internal/ scripts/       Go 源码、测试与运行时脚本
  Dockerfile docker-compose.yml 上游自己的构建与容器定义
  LICENSE                       MIT，版权归原作者 —— 必须保留
  config.example.json           示例配置（真实 config.json 不入库）
  login.sh signin.sh …          Dockerfile 会打进镜像的入口脚本
  start/stop/status-*.cmd       Windows 原生启动脚本
  .gitignore                    运行时数据的兜底忽略
```

只保留构建、运行和测试需要的文件：上游自带的开发辅助（`dev.sh` 与
`scripts/test_*.{py,sh}`）已移除；账号运维与签到仍可用 `acct.sh` / `checkin.sh`，
面板里也有对应入口。

**永远不要提交**（根 `.gitignore` 与 `upstream/.gitignore` 都锚定了）：

| 路径 | 为什么 |
|---|---|
| `upstream/config.json` | 含 `api_key` |
| `upstream/auths/` | 真实 CodeBuddy 账号授权凭据 |
| `upstream/data/` | 上游运行时数据 |
| `upstream/backups/` | 备份含授权 |
| `upstream/.apikey`、`.server.pid`、`*.log` | 运行期生成的密钥 / 进程号 / 日志 |

`upstream/LICENSE` 与版权声明必须随包分发：再发布时不能删。

## `.upstream-id` 从哪来

发布包里的 `upstream/.upstream-id` 是**发布时派生**的，不在仓库里存值：

```text
.upstream-id = sha256(ASCII(git rev-parse HEAD:upstream))
```

也就是「上游目录树对象 id 的 sha256」，64 位小写 hex。CI（`.github/workflows/
release.yml`）与 `windows/launcher/build.ps1` 用同一算法、同一字节（末尾一个 LF）。

- 同一份内容永远得到同一个值，与构建时间、机器、平台无关；
- 面板用它判断「这次更新是否需要同步上游」，上游镜像把它打成
  `dev.workbuddy.upstream-id` label；
- 因为值来自**已提交**的树，`upstream/` 有未提交改动时不能打包
  （`build.ps1` 会直接拒绝；CI 只导出已提交内容）。

## 改上游代码

```bash
cd upstream
# 改源码 / 加测试
go build ./... && go vet ./... && go test ./... -count=1
cd ..
git add upstream
git commit -m "upstream: 说清这次改了什么"
```

面板侧需要配套改动时，放在同一个提交或紧邻的提交里 —— 两边的协议耦合点见
[release-process.md](release-process.md) 的「仍然要守的老规矩」。

## 从参考仓同步能力

参考来源是 `misakano7545/workbuddy2api-panel`。原则没变：
**只挑需要的提交，不整仓 merge**。它的内嵌面板、配置结构与账号池语义都和我们
不同，整仓合并会冲掉本项目的改动。

基线之后还有哪些能力值得同步、哪些不建议同步，见
[upstream-sync-misakano.md](upstream-sync-misakano.md)（按 P0/P1/P2 分级，
每项带来源提交与本地落点）。

基线**之前**一直没移植的能力单独放在
[upstream-sync-baseline-gaps.md](upstream-sync-baseline-gaps.md)。两份清单互补：
前者按「新提交」组织，后者按「从未移植的能力」组织，同一项只在一处展开。

> 两份清单都用裸编号（`P0-1`、`P2-8` …），但**同名条目不是同一件事**
> （例如两边的 `P2-8` 分别指「10105 会话自救」与「模型目录指纹监控」）。
> 跨文档引用一律带前缀：`M-P0-1` 指 misakano 那份，`G-P0-1` 指 baseline-gaps
> 那份。第一批六项的落地情况与三处有意偏离，见 baseline-gaps 开头的
> 「同步状态」表。

除 misakano 面板仓外，2026-11 还有三个参考源。

**`ithtelab/workbuddy-manager`**（Go 网关 + 面板一体，本文简称「上游 manager 仓」）
已核对的范围是 `80b4b21`（v2.11.10）→ `eb76ab8b`（v2.11.21）：

| 区间 | 取到的能力 |
|---|---|
| `80b4b21` → `8b5a4df4`（v2.11.14） | Go 侧补丁 0051–0056、面板侧余额 / 积分 / 账号 / 统计 / 调度等修复 |
| `8b5a4df4` → `48ba599b`（v2.11.15） | 静态资源 gzip、密钥每日 Token 限额、账号拖拽排序、单账号刷新积分 / 到期角标 / 趋势积分线、弹窗浮层滚轮、Docker data 卷命名卷开关、`/v1/tools/search` 透传 |
| `48ba599b` → `f32521fd`（v2.11.17） | `/v1/tools/search` 全参数入站校验；`/v1`、`/v2` 数据面 CORS 面板可配（含与 `WB_CORS_ORIGINS` 共存去重、发版前审计的 8 处修正） |
| `f32521fd` → `d780cab` | Responses 透传 `parallel_tool_calls`；统计页顶部实时并发 / RPM 指标条；账号保护预警移到「设置 → 上游」；`/v1/usage` 积分按小数返回；「限时免费」判定统一；自定义密钥形态（`WB_KEY_CUSTOM_ENABLED`） |
| `d780cab` → `2b6b386` | PG 备份按字节预算装批 + 失败退避 + keepalive；实时指标口径纠错（区间方向 / 桶内最大重叠 / 按 realm 取上限 / `/status` 短缓存）；客户端在途计数与 1 秒刷新；密钥 hash 唯一索引 + 并发 409；弹窗模型浮层碰撞边界；Docker 时区修正 |
| `2b6b386` → `7d5b163`（v2.11.20） | 模型中心剔除视频 / 音频生成模型（含 `-video` / `-tts` / `-audio` 后缀，多模态对话模型保留）；统计页范围选择器补「昨日」 |
| `7d5b163` → `eb76ab8b`（v2.11.21） | 字体改为随仓库分发（`next/font/local`，构建不再依赖 Google Fonts）；`.env.example` 数据卷默认改为命名卷，补首次部署防呆 |

这七批都**只取对本项目有益的部分**；Trace Tap（调用抓包）整段未取，
国际版 agenttool 搜索、上游自己的 CI / 发版提交同样未取。逐项清单与
「未取原因」记录在 `deploy/workbuddy-panel-ref.txt` 的第二批、第四批、第五批、
第七批、第十二批、第十四批、第十六批段落里；Web 搜索的协议与错误语义单独写在
[web-search-passthrough.md](web-search-passthrough.md)。

> **面板批（2026-11-19，`d780cab` → `2b6b386`）**：这一批是 panel / Web /
> Docker 改动，没有 Go `upstream/` 改动。落点与三处有意偏离：
>
> - **实时指标**按 `[ts - latency, ts)` 计并发（`ts` 是请求**结束**时刻）；
>   桶内取最大重叠而不是命中数；`capacity` 按 realm 分别乘
>   `max_in_flight` / `max_in_flight_global`；`cn` 的 0 表示「不限」，接口返回
>   `null`、界面显示「不限」。上游 `211f233` 在 `_per_account_limit` 里留了一段
>   永远执行不到的旧 `pick` 调用，**未取**。
> - **PG 备份额度**取「行数 + 4 MiB 字节」双上限与失败指数退避。`211f233` 曾用
>   `socket_timeout`，那是 psycopg2 的参数、psycopg3 会在建连前直接抛
>   `ProgrammingError`；本地改用 `tcp_user_timeout` + keepalive 三件套（并有静态守卫）。
> - **Docker** 只取时区修正，不跟进 Alpine 重写（`ad1dae5` + `d2737dd`）：
>   本地镜像是 Debian-slim 的自定义构建（docker CLI 静态包、compose 插件、前端
>   构建阶段），整体换基座属于另一件事。时区问题两边同源，已在本仓修掉：
>   安装 `tzdata`、`build.args.TZ` 与 `environment.TZ` 对齐。
>
> 上游未合并分支 `fix/account-backfill-false-alarm`（`c2cc131`）核对后**已覆盖**：
> 本地账号诊断早就是「按 pending 账号判断」，不依赖旧的 `filled` 标记，跳过。
> 开机自启触发点（`2959b08` 的 `MainForm.cs`）本地已用 `OnHandleCreated` 重写
> 实现，并比上游多一个「只排队一次」的保护，不重复移植。

> **末批（2026-11-20，`2b6b386` → `7d5b163`）**：4 个提交里取 3 个。
>
> - `a990935` + `f0ffbe1` 模型中心剔除视频 / 音频生成模型：本地 `_non_chat_model`
>   原来只认 `text-to-image`，补 tag 白名单、id 片段与后缀三组规则；能读视频 /
>   音频但产出文本的多模态对话模型（`*-vl` / `*understanding` / `*omni` 等）
>   优先放行。仍然只在**国内版**生效（调用方 `realm != GLOBAL`）。
> - `6285931` 只取「昨日」这一半：范围选择器新增 `yesterday`，后端按该日取数、
>   图上不标「当前小时」。**自定义同一天的小时图**本地此前已按 `d === 1` +
>   把 `customStart` 传给小时端点修好（判定在 `spanDays` / `chartEnd`），不需要
>   上游的 `hourlyTargetFor()` 重构，未取。
> - `7d5b163` 是发版提交（只改 `CHANGELOG.md` 与上游 `server/main.py` 版本号），
>   本地版本线不同，不取。
>
> 本批没有 Go `upstream/` 改动。

> **字体 / 部署批（2026-11-20，`7d5b163` → `eb76ab8b`）**：2 个提交里取 1 个。
>
> - `8e6f22f2` 把字体改为随仓库分发：`web/app/layout.tsx` 从 `next/font/google`
>   换成 `next/font/local`，新增 `web/app/fonts/Inter-variable.woff2` 与
>   `NotoSansSC-variable.woff2`（与 Google 版同源、同 latin 子集），外观不变，
>   `next build` 不再需要访问 `fonts.googleapis.com`。本地 CI / 国内 Docker
>   构建同样会踩这个坑，所以照取。
> - 同一提交的部署防呆：`.env.example` 的 `WB_DATA_VOLUME` 默认值从空改为
>   `token-farm-data`，并补上「已有 `./data` 数据时先备份」与取文件的命令。
>   `docker-compose.yml` 早已支持这个变量，本次只翻转模板默认值；老部署的
>   `.env` 不受影响，留空仍是原来的 bind mount。
> - `eb76ab8b` 是发版提交（只改上游 `CHANGELOG` 与版本号），不取。
>
> 本批没有 Go `upstream/` 改动。

**`shuishuipingan/qoder2api-hub`**（Qoder 协议层参考源）不是 fork、也没有共享
历史，接入方式是**整目录 vendor**，不按提交 cherry-pick：

```bash
git remote add qoder-ref https://github.com/shuishuipingan/qoder2api-hub.git
git fetch qoder-ref --no-tags
```

已核对到 `44b6f33`（v1.9.11）→ `25d0594`（v1.9.12）→ `285d98be`（v1.9.13）。基线、
取用范围与本地唯一补丁（账号权重）记录在 `deploy/qoder-ref.txt`。升级方式是重跑
`python server/platforms/qoder/tools/vendor_qoder.py --source <clone>`，
`--check` 校验一致性；重新 vendor 后必须重新应用那份补丁
（`server/tests/test_qoder_vendor.py` 守着）。

> **Qoder 批（2026-11-21，`25d0594` → `285d98be`，v1.9.13）**：4 个非 merge 提交，
> 整目录重 vendor，变动的协议文件是 `qoder_accounts.py` / `qoder_proxy.py` /
> `qoder_scheduler.py` / `qoder_settings.py` / `qoder_tasks.py`（无新增模块与数据
> 文件，`vendor_qoder.py` 的 `MODULES` / `DATA_FILES` 不变）。三项实质修复：
> 签到结果保留上游奖励拒绝语义（`failureCode` 优先于 `errorCode`，
> `SAME_PERSON_ALREADY_CLAIMED` 仍归 blocked，`success=false` 不再被当成领取成功；
> 兑换码缺货 / 任务未完成分别归 pending / locked，其余拒绝写进错误明细而不是静默
> 吞掉）；新增 `settings.omit_default_system_prompt`（默认关闭，只有显式开启才
> 清空 baseprompt 里的官方 system 提示词，`SYSTEM_PROMPT` 默认值随之从
> `"You are a helpful assistant."` 改成 `None`）；该设置的写盘与面板刷新做了
> 防覆盖保护（`settings.json` 无效时拒绝写入，保存路由校验布尔类型）。
> `embedded.py` 跟着把 `proxy.SYSTEM_PROMPT` 设成 `None`——与上游默认等价，
> 内置网关行为不变。账号权重补丁已重新应用。面板侧的「省略默认系统提示词」表单
> 仍不取：token-farm 的入口是平台设置，需要时改 `accounts/settings.json`
> （`docs/qoder.md` 已写明）。

> **Qoder 批（2026-11-20，`44b6f33` → `25d0594`，v1.9.12）**：7 个非 merge 提交，
> 整目录重 vendor。这一批是**联网工具链重写**：新增 `qoder_web_response.py`、
> `qoder_external_web.py`、`qoder_official_web.py`，`qoder_webtools.py` 从单一
> 本地引擎扩成「本地 / 官方 / 外部」三种后端，`/settings/web-tools` 从布尔开关
> 变成 `{enabled, backend, external}`；同时修了托管工具的几个真 bug（预算用尽
> 后最后一轮结果被丢、`function_call_arguments` 首个分片丢空白、
> `content_filter` 未归入 incomplete、上游内嵌 error 帧被当正文回放）。
> `qoder_accounts.http_json` 新增 `follow_redirects`（官方签名请求不跟重定向）。
> 账号权重补丁已重新应用；`vendor_qoder.py` 的 `MODULES` 补了三个新模块，
> 并新增「副本里每个 `qoder_*` 包内导入都必须在清单里」的守卫——1.9.12 起
> `qoder_proxy.py` 顶层就 import `qoder_web_response`，漏一个文件是 ImportError
> 而不是少个可选能力。面板侧的 `/settings/web-tools` 表单不取：token-farm 的入口
> 仍是平台设置里的布尔开关，只切 enabled；要用官方 / 外部后端时改
> `accounts/settings.json`（`docs/qoder.md` 已写明）。

> **Qoder 批（2026-11-13 → 2026-11-19，`d0f4837` → `44b6f33`）**：4 个非 merge
> 提交，只有 `qoder_proxy.py` 变动，按整目录重 vendor 吸收：持久排队诊断
> （`f0b41e4`）、面板关闭网关（`adeadcb` + `deb48f8`）、流失败收尾与实际模型
> 身份审计（`44b6f33`，新增 `QD_UNKNOWN_MODEL`，默认 `pass` 与旧行为一致）。
> 内置网关跑在 uvicorn 进程里，`embedded.py` 在导入上游前设
> `QD_SHUTDOWN_DISABLE=1`，`/admin/shutdown` 稳定 403，误触不会带走整个后端。
> 账号权重补丁已重新应用；上游 dashboard / i18n / 自更新 / Docker / CI 仍不取。

**`xingluoyuankong/loomy2api`**（Loomy 协议参考源）既不是 fork，也**不 vendor**：
登录签名、RSA 加密、微信扫码、积分 / 签到、模型别名的协议事实读自它，
但代码按 token-farm 的适配器契约在 `server/platforms/loomy/` 里重写，
参考仓的网关、账号池与面板都不进来。

```bash
git remote add loomy-ref https://github.com/xingluoyuankong/loomy2api.git
git fetch loomy-ref --no-tags
git log --oneline --no-merges cb27afd..loomy-ref/HEAD   # 对照用，不 merge
git diff --stat cb27afd...loomy-ref/HEAD                # 只看协议文件有没有动
```

已核对到 `cb27afd`（2026-11-14，范围 `eed1efa` → `cb27afd`，40 个提交）。
基线、逐项取用 / 未取原因与实测记录在 `deploy/loomy-ref.txt`；升级时改
`server/platforms/loomy/` 的对应文件，跑
`server/tests/test_platform_loomy_adapter.py`，再把锚点提交号写回该文件。

这些来源的仓库布局与运行时都不同，**不要互相 cherry-pick**，按能力在本项目
对应文件重写。

### 已评估：misakano `10d01b48..c4b53cd`

参考仓在 `10d01b48` 之后积累了 69 个提交（`10d01b48` → `c4b53cd`，2026-11-16
当前 HEAD），其中 54 个非 merge、15 个 merge。本轮已对着本地代码**逐项**判过一遍
（54 行逐项判定见
[upstream-sync-misakano.md](upstream-sync-misakano.md) 的附录 D.2），结论：

- **值得做 7 项**（附录 D.3）：签到回执误判 `03a50ce0`（Go + Python 两处）、
  补签去重与按号错开 `8a39572f`、`LoadDir` 只给胜出文件回填 realm `1414e9f7`、
  账号目录热重载不清空整池 `47cc7e22`、空 text 段逐字节补位 `a7983640`、
  身份指令改写收窄 `767056cc`、截断终态不盖掉 error `084176e4`；
- **要排期 3 项**（附录 D.4）：模型规则按号批量 deny/allow `8519ea26`（+`74456c87`）、
  默认 + mp 任务列表合并 `0a2a55fb`；
- **需确认 2 个主题**（附录 D.5）：积分语义「可用量 vs 冻结快照」`eca5f30b`
  （+`f5d08c04`）与本地「全池相加」口径互斥（仍未动）；指纹版本 `2fa6876e`
  已随 M-P2-7 按 realm 拆分落地（不再单独改数字）；
- 其余 7 项是尚未移植的 M-P* 项的后续修正（附录 D.6，实现那项时必须一并带上）、
  5 项本地已覆盖（D.7）、29 项不适用（D.8）。

**这批的「值得」7 项已于 2026-11-16 落地**：每项一个提交（`69406ee0`、`c4c44610`、
`1e2acfe2`、`f7bf2f9e`、`527f9910`、`d771990c`、`6031f1c8`），来源号与落点逐行记在
附录 D.3 的表里并追加进 `deploy/workbuddy-panel-ref.txt` 第六批。排期（D.4）与需确认
（D.5）两项仍未动，不要当成已同步。

### 已评估：misakano `c4b53cd..34120825`

扫描边界之后上游又推了 67 个提交（12 merge / 55 非 merge，到 2026-11-18 的
`34120825`），同样逐条判定，写进
[upstream-sync-misakano.md](upstream-sync-misakano.md) 的附录 E：

- **值得做 3 项**（9 个提交）：工具 schema 出站纠偏 + 11129/11152 登记
  （E-1）、早到工具结果重排（E-2）、`/v1/responses` 回放补齐（E-3）；
- 15 个随项（M-P2-7 的 5.11.3 `X-Client-*` 与 mp 撤头一组、测试探针、merge 面）、
  1 个需确认（本地 Python updater 自查 0 字节临时文件与失败重试）；
- 42 个不适用：内嵌 Go 面板的实时任务卡、换段、narration / toolonly / rescue、
  官方头对拍 CI。本地没有这四个模块，不按提交逐个移植。

这 3 项**已落地**（2026-11-18，按本仓结构重写，每项一个提交并登记来源）：

- E-1 → `90b93807`（`schema_normalize.go` + 11129/11152 分类 + hint/帧码）；
- E-2 → `34c0afac`（Go `reorderEarlyToolResults` + Python 同口径收尾重排）；
- E-3 → `c848ca17`（Chat 形状字段、惰性项、compaction 交接摘要）。

E.4 的 updater 自查也已完成：本地 `deploy/update.py` 已具备「网络失败/5xx 重试
一次 + 解压前核对大小与 sha256 + 状态/包内文件原子替换」，0 字节或半截下载不会
进入安装；没有 30 分钟退避，结论与理由记在附录 E.4。

随项里的 **M-P2-7 指纹组**也已在 2026-11-18 落地（一个新提交，按本仓结构重写）：
D-12 的版本三元组与按 realm 分档（CN 5.11.3 / CLI 2.176.0，GL 5.9.4 / 2.165.0，
含 Python 登录 `version` 与 `task_runner` / `school_open_day` 的 desktop 三元组）、
CN 专发的 `X-Client-*` 三头、小程序口径撤头、global UA 品牌段改 slug
`workbuddy-ai`。来源提交 `2fa6876e` / `a0b9f87e` / `96265d71` / `77ca3fe7` /
`b85d4f25` / `c9b059c4`，明细见附录 D-12 与 E.3；billing UA 改 `axios/1.16.1`
与头集 / 头顺序（B-20 剩余部分、B-21、B-22）仍按原判定留待独立批次。

### 已评估：misakano `34120825..d40d4b8`

扫描边界之后上游又推了 3 个非 merge 提交（2026-11-19），逐条判定后**只取 1 项**：

- **取 `d40d4b8`**（issue #276）：选不到号时把成因拆解写进 503 与 WARN。本地落点
  `upstream/internal/pool/nopick.go` + `internal/server/noselect.go`，`gateway_hint`
  末尾追加 `no_pick{...}`；本地没有 reserve_credits / 硬性 credit_floor，拆解里
  不设这两类，手动停用沿用 `manualDisabled` 语义。
- **不取 `85179d4`**：narration 救援开关 / 指令自报身份 / 今日救援计数——本地没有
  narration 模块（M-P1-3 + M-P2-17 排期项）。
- **不取 `1fafb14`**：`/v1/search` / `/v1/fetch` / `/v1/embeddings` 进请求台账——
  这三个入口在本地是 Python 面板侧，Go 侧没有对应路由。
- 两个未取提交都已在上面写明；本批没有其他增量。

### 已评估：misakano `d40d4b8..c4052548`

扫描边界之后参考仓又推了两批（2026-11-20）：

- **`d40d4b8..937fef2`（附录 F，7 个非 merge）**：取 2 项 + 1 条对照用例——
  `abed8395` + `937fef2` 把工具 schema 里的 JS `\u` 转义转成 RE2，`b35a8d03`
  的 11103 归请求级；不取语音端点 `923dbec2`、DeepSeek 别名 `676f9da9`、
  加号出口 `6ef24501` 与 README `b902132c`。逐条见
  [upstream-sync-misakano.md](upstream-sync-misakano.md) 附录 F；落地项已登记在
  `deploy/workbuddy-panel-ref.txt` 第十五批。
- **`937fef2..be99749d`（附录 G，7 个非 merge）**：**0 取**。全部是语音链路
  （issue #277）的修复、测试与探针，本地 Go 侧没有 `/v1/audio/*`、面板也没有
  语音入口，没有可接的落点；`be99749d` 的 crossacct 探针结论「上游前缀缓存按
  账号隔离」与本地会话粘性一致，仅作参考。逐条见附录 G，来源号同时记在
  `deploy/workbuddy-panel-ref.txt` 第十七批。
- **`be99749d..c4052548`（附录 H，5 个非 merge）**：取 2 项、3 项语音仍不取。
  `d629a8ca` 把 11102/14401「模型 id 上游不存在」从 (账号,模型) 负缓存里拆出来，
  归请求级 `ErrModelMissing`（不罚号、不轮转、末端 400 透传原文），11158
  「账号类型无权限」仍走负缓存 + 轮转；`9ad206bd` 修 `/v1/responses` 回放的跨轮
  拼接——同轮的助手正文与工具调用必须落在同一条 assistant 上，`final_answer` /
  user / compaction 都是回合边界。三条音频提交（`026f9c06` / `d5ce7de4` /
  `c4052548`）与附录 G 同源，仍等本地真的接语音端点。另有一处面板侧联动：
  `wb2api._clear_cooling_online` / `modelrecovery` 的「在线清除 / 自动复查」判据
  原来只认 `11102` 前缀，11158 独立前缀后会掉出范围，已收口成
  `is_model_block_reason()`（`('11102','11158')` 前缀匹配，不误伤 6004）；
  `wbforward.model_missing_body` 同时认 14401 的 `route config not found`
  （旧版本上游会把它包成 503，同样改判 404）。
  逐条见附录 H，已同步项登记在 `deploy/workbuddy-panel-ref.txt` 第十八批。

```bash
# 1) 在参考仓的临时 clone 里，把目标提交导出成补丁（一次一个提交）
git format-patch -1 <commit> -o /tmp/sync

# 2) 应用进本仓的 upstream/ 目录
#    -p1            去掉补丁里的 a/ b/ 前缀
#    --directory    把路径落到 upstream/ 下
#    -3             允许三方合并（本机有改动时不会硬失败）
git am -3 -p1 --directory=upstream /tmp/sync/0001-*.patch
```

- 有冲突：按普通 git 冲突解决，`git add` 后 `git am --continue`；放弃用
  `git am --abort`。
- 多个提交按顺序 `git am`，顺序不能乱（后面的提交可能依赖前面的）。
- **PowerShell 不能用 `|` 直接管道**给 `git am`：先把补丁落成文件再传路径。
- 同步完在提交信息里写清来源仓库与提交号，并把这一批的来源补进
  `deploy/workbuddy-panel-ref.txt`。
- 如果参考仓的改动依赖它自己的逆向协议或运行时（uTLS 代理、SSE 续写、媒体 /
  MCP / Embeddings 等），先判断与本项目是否兼容；不兼容就只移植其中协议无关的
  部分，不要为了「同步完整」把两套运行时耦在一起。

## 剩余待办总览（2026-11-18 整理）

基线：参考仓 `d40d4b8`（附录 A–E 已逐提交评完；`34120825..d40d4b8` 的 3 个
提交也已判定，取的 `d40d4b8` 见上一节）。这一节把两份清单里**还没做**的
项合到一张表，按「改动面 / 要不要灰度」分档；明细仍以
[upstream-sync-misakano.md](upstream-sync-misakano.md)（`M-` 项、附录 A–E）与
[upstream-sync-baseline-gaps.md](upstream-sync-baseline-gaps.md)（`G-` 项）的条目为准，
这里只做索引，不重复展开每项的判据。

### 一档：下一批可做（不改流量分布、不动 transport）

| 项 | 一句话 | 本地落点 | 来源 | 状态 |
|---|---|---|---|---|
| **M-P1-9** | 正文里内联的 `<think:…>` 拆回 thinking 块；只在 content **以** `<think:` 开头时启用 | Python `server/routers/anthropic.py` | `771c887f` | ✅ `c718da1b` |
| **M-P1-10** | 输出预算下限：显式 `max_tokens` 低于档位下限时抬到下限，再以目录 `maxOutputTokens` 封顶 | Go `internal/upstream/output_budget.go` + `payload.go` | `2b9b7736` | ✅ `b8431873` |
| **M-P1-13** | 签到回执四态 `checked` / `already` / `pending` / `failed`；`failed` 压过 `already`，积分未知不显示数字 | Go `CheckinReceipt` + `scheduler`；Python `checkin_all` / `dailyplan.py` | `8125831d` | ✅ `c0d616aa` |
| **B-2** | 会话轮级 body 字段 `conversationRequestId` / `startsNewUserRequest` / `agent`，只写当前轮 | Go `PrepareBody*` 之后（`conv_fields.go`） | `56057810` | ✅ `4b3f7bc0` |
| **B-4** | usage 缓存命中别名归一（五种写法取最大正值回写，保留已有嵌套形态） | Go `sse.go` / 非流式组装 | `782a6414` | ✅ `e612cd5b` |
| **B-16** | prep / do 分段耗时，并把「请求档位 / 实际生效档位」入日志（纯观测） | Go `internal/server/logging.go` | `75576b8b` | ✅ `3353cd8f`（档位字段仍待做，见下） |
| **B-11** | 昵称手动同步（`GET /console/account`，只取 nickname / uid，手机号不解析不落日志） | Python `services/tencent.py` + `profilesync.py` + `routers/accounts.py` | `04005ba2` | ✅ `8b1f7cc5` |
| **M-P2-13** | 热重载补两条护栏（本轮读失败只增不删；在途请求的账号不删） | Go `internal/auth` + `internal/pool` | `9e5c2cda` / `c54c5176` / `4f6adc0e` | ✅ `6b7be32b` |
| **D-9** | 默认任务列表 miss 时按 mp 口径再查一次；`wb_wechat_oa_subscribe_task` 做 claim-only | Python `upstream/scripts/task_runner.py` | `0a2a55fb` | ✅ `f55a8be8` |

> **一档第一批（2026-11-18，4 项已落地）**：M-P1-10 / M-P1-13 / B-4 / B-16，
> 每项一个提交，来源号追加进 `deploy/workbuddy-panel-ref.txt` 第十批。四项都
> 不改流量分布、不动 transport，回归面只有各自的单测。
>
> B-16 本次只落 **prep/do 分段耗时**；同一提交里的「请求档位 / 实际生效档位」
> 一列本地还没有对应的解析产物（`normalizeReasoningEffort` 只回写 body、不返回
> 降级结果，要加返回值或旁路），留到 B-15 档位策略层一起做，不在本批硬塞。

> **一档第二批（2026-11-18，剩余 5 项已落地）**：M-P1-9 / B-2 / B-11 / M-P2-13 / D-9，
> 来源号追加进 `deploy/workbuddy-panel-ref.txt` 第十一批。落点与本地取舍：
>
> - M-P1-9 在 **Python**（本地 `/v1/messages` 只有 Python 实现）：`_ThinkTagSplitter`
>   只在 content **以** `<think:` 开头时启用；未开 thinking 时这些块按 CoT 丢弃，
>   `_StreamTranslator` 与 non-stream 两条路径都接入，收尾前 flush 残留。
> - B-2 用 `injectConversationFields` 只给**最后一条 user 消息**写
>   `conversationRequestId` + `startsNewUserRequest` + `agent:"cli"`；客户端自带字段
>   不覆盖，历史轮不改写；注入点在 `InjectPromptCacheKey` 之后。
> - M-P2-13 只取护栏 1（本轮有解析失败 → 只增不删）与护栏 4（在途请求 uid 不删）。
>   护栏 2/3（删除确认计数、内容指纹）与 `reloadMu` 跨进程锁在本仓结构里没有落点
>   （面板是独立进程，拿不到 Go 侧的锁），不做。
> - D-9 的默认列表 miss 回落：命中后**仍用默认头**走 `process_task`（只有列表需要
>   mp 头）；`wb_wechat_oa_subscribe_task` 登记为 claim-only（accept → 回读 → claim，
>   不构造事件）。
> - B-11 只在**手动**「刷新积分」收尾触发（`WB_PROFILE_CONCURRENCY`，默认 3），
>   后台定时器不碰资料接口；只解析 nickname / uid，手机号不落变量、不写日志。

> B-3（Kimi `$ref` 展开）原列在这一档，E-1 落地时已随 `schema_normalize.go` 的顶层
> `$ref` 内联展开一起覆盖，从待办里移除，判定已回填到附录 B.1。

### 二档：需要灰度（改选号 / 粘性 / 风控）

| 项 | 一句话 | 灰度要点 |
|---|---|---|
| **M-P0-1** | 11140 命中即停手 + 同一 15 分钟窗口 ≥2 个不同号被限就全池暂停（默认 1800s，`0` 只留停手） | 阈值误判会误伤整池；必须带附录 D.6 的三条修正（`d3edea90` / `de24d38f` / `7dcae8ed`）一起落，否则退避只涨不清 |
| **M-P1-5** | 只发 `user_id` 的客户端按「system + 首条 user」内容哈希派生粘性键 | 存量绑定会全量失效一次，前缀缓存短期 miss 上升；建议先只对「既无 `conversation_id` 也无 `prompt_cache_key`」的请求生效，并掺入 `d700ec56` 的 caller id 盐 |
| **G-P1-5 → G-P1-7 → G-P1-6 → G-P1-8** | `reserve_credits` → `model_fallback`（6004 降级）→ `account_priority` / `account_share` → `balance_window` | 一步一发，每步都能在面板上验证；`model_fallback` 依赖额度预留，`balance_window` 最后做 |

### 三档：中风险（账号健康与协议层）

- **M-P1-7** 等上游首帧期间补 SSE 注释帧（`headkeepalive`）：注释帧与真实写共用
  一把锁，另加「响应头已提交」的四条 `writeOpenAIError*` 分支——写坏锁会产出交错帧，
  比干等更糟。
- **M-P0-4** `$wbEncrypted` auth 解密 + 回写保持加密（Go 与 Python 两侧，保留桌面端
  字段与 JSON 数字类型）。
- **M-P1-1** 导入覆盖前先验活：有界并发 + 总时限，轮换后的新凭据必须落盘。
- **B-5** 401 三态求证（`VerifyAlive` / `VerifyDead` / `VerifyInconclusive`），chat 401
  与余额探测共用一条 `VerifySessionByRefresh`。
- **B-7** global 余额三件套（summary / paid / free）：本地 global 余额可能恒 0；
  字段可能是字符串小数，解析要宽容，明细失败只降级到期批次。
- **M-P2-10** 免费号事前避让 `exclusive` 模型（与 D-8 同一层，选号门只改一遍）。
- **M-P2-12** 日志落盘 / 劣化告警 / 选号策略，三件分开做。
- **B-6** 活跃上报 body 形态（本地走 `billingBase` + 自造 ID，参考仓走 `chatBase` +
  官方 UUID）：先同号 A/B 各发一条，回读 streak 与任务进度再切。

### 四档：要排期（新功能 / 面板 UI / 用户可见口径）

- **D-8 模型规则 deny / allow**（`8519ea26` + `74456c87`，含 D-10）：命名规则挂多号、
  deny 优先、`model_not_allowed` 400；要动规则表、账号挂载、选号闸门与 Next.js 页面。
- **D-11 积分语义**（`eca5f30b` + `f5d08c04`）：`credits` 改「当前可用量」+ `credits_raw`
  冻结快照。会改变用户看到的总额，必须独立发版并带迁移说明。
- **M-P1-3 + M-P2-17 续写 / 救援链**：本地没有 continue / toolonly / narration 三个
  模块，成本最高，按是否真需要自动续写再定。
- **B-1 DSML 工具调用还原**；**B-21 官方黄金头集**（先建 chat / report / refresh /
  billing / v3 的分组回归夹具，再逐组对齐）；**B-14** picker 裁剪；**B-15** 思考档位
  策略层（含 G-P1-17 的 `can_disable_thinking` + `off` 档）；**B-17** `/v2/chat/busy`
  （本地池状态版，或只做面板观测，二选一）。
- 纯追加、随时可做：**G-P1-1** `/v1/capacity`、**G-P1-9** models.dev 形状、
  **G-P1-14** `dump_dir`、**G-P1-2** Go `/metrics`、**G-P1-10** `/admin/tasks/{name}/run`、
  **G-P1-11** 模型锁视图 + 套餐分类、**G-P1-4** `budget`、**G-P2-1** admin 审计、
  **G-P2-4** `cmd/login` 版本三件套。

### 已判定不做

| 项 | 理由 |
|---|---|
| B-22 头顺序 / 大小写 / TLS 指纹、uTLS | 没有封号对比数据，改造成本高；HTTP/2 头顺序还受库控制 |
| A-9、A-14（Go 内嵌持久作业 + 分钟级 sweep） | 本地是 Python + Next.js，另开一套运行时只为一个任务中心 |
| A-15～A-18（补跑标记 / 外观单点 / spread / humanize） | 只解决时间相关性，不是防封；参考仓也没给出对比数据 |
| M-P2-11 / M-P2-16 / M-P2-18 / M-P2-19 | 活动已结束 / 与 M-B-11 合并再说 / 无 profiling 需求 / 参考仓自己已 revert |
| G-P2-2 `livecfg`、G-P2-3 `httpauth`、G-P2-5 `probe`、G-P2-6 `updater`、G-P2-7 Go 观测 | 各自的收益在本地已被 `docker restart` 0.45s、现有常量时间比较、容器部署与 Python 观测覆盖 |
| G-P1-13 Go `models.aliases`、G-P2-8 目录指纹 | 前者 Python `modelmap.py` 已覆盖；后者写进 Python watchdog，不在 Go 加 goroutine |
| Go 侧 `/v1/responses`、`/v1/messages`、媒体、search / fetch / MCP、Embeddings | 外部协议入口在 Python 面板侧，Go 只做 chat 主链路 |
| 10105 网页活跃自救、G-M2-6 表格居中、Windows README | 已单独判定过，与目标无关或需整体视觉评估 |

## 三步发版（与上游相关的最小检查）

```bash
# 1) 上游已提交、工作区干净
git status --short upstream          # 必须无输出

# 2) 上游能构建、测试全绿（CI 也会再跑一遍）
(cd upstream && go build ./... && go vet ./... && go test ./... -count=1)

# 3) 版本号与 CHANGELOG 落地，然后打 tag（推 tag 即发布）
python -m unittest discover -s server/tests -t .
git tag vX.Y.Z && git push origin vX.Y.Z
```

发布包里的 `upstream/` 由 CI 用 `git archive HEAD:upstream` 导出，**不需要**再
手工打包或上传任何「上游源码资产」。完整步骤见
[release-process.md](release-process.md)。
