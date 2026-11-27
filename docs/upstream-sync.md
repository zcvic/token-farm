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

除 misakano 面板仓外，2026-11 还有多个参考源（manager 仓、Qoder、Loomy，以及
trae / discovery / dumate / cline / kilo 五个协议源）。

> **面板仓批（2026-11-24，`c4052548` → `d221c721fc`）**：8 个非 merge 提交里
> 取 1 个、5 个不取、1 个只取教训，另**补了一笔基线之前的漏取**。
>
> - `d221c721fc` 同域同名的优惠模型在列表里补标签（issue #286）：上游把限时免费
>   的 `hy4flash-free` 与付费的 `hy4flash` 的显示名都写成「Hy4 flash」，只认
>   `name` 的客户端里两行长一样，用户以为选了免费那条、实际发出的却是付费 id。
>   参考仓改在 Go 侧 `modelList()` 末尾；本仓的 `/v1/models` 是**面板侧**组装的，
>   所以重写为 `server/routers/gateway.py` 的 `_disambiguate_promo_names`。两处
>   有意偏离：① 域判据不能用 `cn:` 前缀 —— 本仓在 `_bare_cn_model_ids` 之后已把
>   `cn:` 剥掉，照抄会让功能静默失效，改为「`global:` = 国际版、其余裸名 =
>   国内版、`<平台>/` 单独分段」；② 必须放在 `modelpolicy.filter_payload`
>   **之后**，否则被隐藏的兄弟仍参与计数，唯一剩下的那条会白拿后缀。
>   `id` 一个字节不动。
> - `f0c427d2ce` 的**毫秒 `expiresAt` 归一**（漏取补课）：官方 CodeBuddy CLI /
>   桌面端的凭据把 `expiresAt` 写成毫秒（实测 `1795254516410`），本仓按秒读。
>   后果已用探针实测：到期时间落到公元 58800 年 → `NeedsRefresh` 恒 false →
>   token 永不刷新 → 到期后账号静默 401；Python 侧更早暴露（面板算出「剩余五万
>   多年」、续期永不触发）。Go 侧补 `Parse` 归一 + `expiresAtMillis` 标志 +
>   `SaveAtomic` 按来源单位写回；Python 侧在读（`wb2api`）、导入
>   （`routers/accounts`）、写回（`tencent.update_auth_tokens`）与整份重建
>   （`tencent.write_auth_file`，重登 / 覆盖导入）四处都归一或跟随原单位。
>   这个提交此前只在 baseline-gaps 的 P2-4 下被评估过（只取了登录链路那半）。
> - `c84b6f2be7` 只取**教训**：参考仓把该归一的测试夹具写成固定毫秒串，到期那天
>   CI 必然变红。本仓没有这个夹具、也没有这个故障，但新增用例一律用相对当前
>   时间构造。**同一类「用例依赖外部条件」的毛病本仓也有一处**，是全量回归时撞出
>   来的（不是从参考仓抄的）：Cline 的目录回退用例没堵住两个抓取函数，有网机器上
>   会真的拉线上目录，于是断言必然红。顺带发现 `list_models` 只判 `is None`，
>   两个接口都失败时内置免费档的兜底走不到 —— 已一并修掉并补用例钉住。
> - 不取三条视频提交（`c27d73114c` / `e2d0eb6a40` / `4221f2b202`）与一条 MCP
>   提交（`24e59f89e3`）：本仓没有 `/v1/videos/generations`、没有 MCP 工具面，
>   整条数据面不存在。两个 README 提交（加代理广告位）与本项目无关。

**`ithtelab/workbuddy-manager`**（Go 网关 + 面板一体，本文简称「上游 manager 仓」）
已核对的范围是 `80b4b21`（v2.11.10）→ `93395e16f1`（v2.11.23 之后的未发版段）。
下表是 `80b4b21` → `a83d93738b` 这一段的分批汇总，更晚的批次（第二十三批起）
以表后的 `>` 段逐批记录：

| 区间 | 取到的能力 |
|---|---|
| `80b4b21` → `8b5a4df4`（v2.11.14） | Go 侧补丁 0051–0056、面板侧余额 / 积分 / 账号 / 统计 / 调度等修复 |
| `8b5a4df4` → `48ba599b`（v2.11.15） | 静态资源 gzip、密钥每日 Token 限额、账号拖拽排序、单账号刷新积分 / 到期角标 / 趋势积分线、弹窗浮层滚轮、Docker data 卷命名卷开关、`/v1/tools/search` 透传 |
| `48ba599b` → `f32521fd`（v2.11.17） | `/v1/tools/search` 全参数入站校验；`/v1`、`/v2` 数据面 CORS 面板可配（含与 `WB_CORS_ORIGINS` 共存去重、发版前审计的 8 处修正） |
| `f32521fd` → `d780cab` | Responses 透传 `parallel_tool_calls`；统计页顶部实时并发 / RPM 指标条；账号保护预警移到「设置 → 上游」；`/v1/usage` 积分按小数返回；「限时免费」判定统一；自定义密钥形态（`WB_KEY_CUSTOM_ENABLED`） |
| `d780cab` → `2b6b386` | PG 备份按字节预算装批 + 失败退避 + keepalive；实时指标口径纠错（区间方向 / 桶内最大重叠 / 按 realm 取上限 / `/status` 短缓存）；客户端在途计数与 1 秒刷新；密钥 hash 唯一索引 + 并发 409；弹窗模型浮层碰撞边界；Docker 时区修正 |
| `2b6b386` → `7d5b163`（v2.11.20） | 模型中心剔除视频 / 音频生成模型（含 `-video` / `-tts` / `-audio` 后缀，多模态对话模型保留）；统计页范围选择器补「昨日」 |
| `7d5b163` → `eb76ab8b`（v2.11.21） | 字体改为随仓库分发（`next/font/local`，构建不再依赖 Google Fonts）；`.env.example` 数据卷默认改为命名卷，补首次部署防呆 |
| `eb76ab8b` → `caa82c22`（v2.11.23） | compose 透传 `.env` 的 `WB_ADMIN_PASSWORD`、镜像名可覆盖；容器日志降噪（启动失败只留根因与修法、逐请求 access log 默认关）；入口脚本按 PUID/PGID 对齐挂载目录属主，NAS 不必开 SSH；i18n 术语统一 |
| `caa82c22` → `a83d93738b` | 流式转发补客户端断连检测与「内容空闲」看门狗（`WB_STREAM_IDLE_TIMEOUT`，默认 120 秒 / 0 关闭）；设置页时长字段在配置缺该键时不再显示 undefined 并标红 |

这九批都**只取对本项目有益的部分**；Trace Tap（调用抓包）整段未取，
国际版 agenttool 搜索、上游自己的 CI / 发版提交同样未取。逐项清单与
「未取原因」记录在 `deploy/workbuddy-panel-ref.txt` 的第二批、第四批、第五批、
第七批、第十二批、第十四批、第十六批、第十九批、第二十二批段落里；Web 搜索的
协议与错误语义单独写在 [web-search-passthrough.md](web-search-passthrough.md)。
`a83d93738b` 之后的同步按**上游父链逐个提交**推进（第二十三批起，已扫到
`93395e16f1`），每批在 `deploy/workbuddy-panel-ref.txt` 里各占一个段落：取到东西的
批次以 40 位哈希行收尾，进度以**最后一个哈希行**为准；判定不取的批次只写提交号与
理由，不追加哈希（避免把没同步的提交记成已同步）。

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

> **容器部署批（2026-11-22，`eb76ab8b` → `caa82c22`，v2.11.23）**：11 个非 merge
> 提交里取 4 项、2 个发版提交不取，其余 5 项因架构不同不适用。逐条：
>
> - `e9790faf` compose 透传 `.env`：`WB_ADMIN_PASSWORD` 原来在
>   `docker-compose.yml` 里是**注释掉的固定值**，而 `.env.example` 写明「留空则
>   随机生成并打印到日志」—— 用户按模板填的密码根本不会进容器，只会静默走随机
>   密码，还以为是配置没生效。改为 `${WB_ADMIN_PASSWORD:-}`（空值仍走
>   `security.load_users` 的 `env_pwd or secrets.token_urlsafe(12)`，不会出现空
>   口令管理员）；镜像名改为 `${WB_MANAGER_IMAGE:-token-farm:latest}`，默认仍是
>   本地构建产物名，想用预构建镜像时设变量 + `--no-build`。
> - `58189e99` 容器日志降噪（issue #30 的现场）：`server/main.py` 的 lifespan
>   原来是一整段裸函数，任何一步启动失败都会被 Starlette 把**完整异常链**打到
>   stderr —— 实测一次数据库权限故障刷 350 行，其中 96 行是
>   `merged_lifespan` / `__aenter__` 之类的框架帧，带修法的
>   `RuntimeError: 无法打开数据库文件...` 被埋在最后。新增 `server/logsetup.py`：
>   lifespan 拆成 `_start_services()` / `_stop_services()`，启动失败走
>   `exit_on_startup_failure()`（异常链逐层「类型: 说明」+ 只留项目栈帧 +
>   **直接写 stderr 后 `os._exit(1)`**）。
>   上游第一版只 `raise SystemExit(1)`，实测仍被框架补 150 行栈，降噪只完成一半
>   —— 本地按修正后的形态落地。逐请求 access log 默认关闭
>   （`WB_ACCESS_LOG=0`）：容器日志会被 30s 一次的健康检查与前端轮询刷屏，而
>   真正要查的访问信息在 `request_logs` / `ip_access_logs` 两张表里。
>   `WB_STARTUP_FAILURE_STACK=0` 可连精简栈一起关掉。
> - `b7ce2197` + `a9063d79` 入口脚本按 PUID/PGID 对齐挂载目录属主（合并为一项）：
>   bind mount 的属主由宿主机决定（群晖 1026、普通 Linux 1000、root 建目录则
>   是 0），镜像里固定跑 10001 时启动第一步 `DATA_DIR.mkdir()` 就是
>   `PermissionError`，然后 `restart: unless-stopped` 无限重启 —— 而修法要求
>   用户在 NAS 上开 SSH。新增 `deploy/docker-entrypoint.sh`：以 root 起，
>   `/proc/mounts` 判断哪些目录**真被挂载**（不假定布局），按 PUID/PGID 或挂载
>   目录实际属主对齐 `app` 的 uid/gid，chown 后降权执行 CMD；探测到属主是 0 时
>   回落 10001，绝不以 root 跑业务进程；`WB_FIX_OWNERSHIP=0` **只跳过 chown**，
>   不做 early-exit（上游第一版写成 `exec "$@"` 把后续提示路径全跳过了）。
>   Dockerfile 删掉 `USER app`、改 `ENTRYPOINT`。
>   **本地偏离**：上游用 Alpine + `su-exec` + `shadow`，本地是 Debian-slim，降权
>   用 util-linux 的 `setpriv --reuid --regid --init-groups`，uid/gid 改名用
>   `passwd` 包的 `usermod`/`groupmod`（两者都显式写进运行阶段的 apt 列表）。
>   `.gitattributes` 补 `*.sh text eol=lf` —— CRLF 会让 shebang 变成
>   `#!/bin/sh\r` 而报 `: not found`。
>   `dev/check_dockerfile.py` 补三条结构断言（必须有 ENTRYPOINT、最终阶段不得写
>   `USER`、入口脚本用到的外部命令必须在最终阶段装好），并**实测能拦住**三种
>   真实故障（删 ENTRYPOINT / 加回 USER / 漏装 util-linux）。
>   最后一条来自 `9f040fe3` 的教训：上游把 `su-exec` 只装在中间阶段，跨阶段
>   `COPY` 只搬文件不带依赖，容器启动几毫秒就 exit 127，而当时那条断言在**整个
>   文件**里搜，中间阶段那行就满足了它 —— 等于从没检查过「工具是否真在最终镜像
>   里」。
> - `7b2f76c7` i18n 术语统一：同一中文「反代网关」在外文译文里不一致（en 一律
>   gateway，ja/ko 在标题里多写 reverse proxy，繁中 9 处写「反向代理」）。只改
>   ja/ko/zh-TW 的译文值，不动 key、不动中文源文、不动代码。本地 ja/ko 各 1 处、
>   zh-TW 9 处，另把本地独有的 `dashboard.platformScope` 与两条 `phrases` 一并
>   统一（上游没有这两个 key）。
>
> **不取**（架构不同，不是「没价值」）：
>
> - `faf69381` / `b5024a6f` / `3901a932` 镜像内置上游任务脚本 + 新增
>   `docker-compose.full.yml` 双服务方案：会在本仓 Dockerfile 里加
>   `FROM ${UPSTREAM_IMAGE} AS upstream` 与 `COPY --from=upstream`，让管理端镜像
>   **硬依赖外部 GHCR 镜像**才能构建（上游自己也承认「拉不到时构建会失败」），
>   破坏本仓「镜像不含上游服务、可离线构建」的取舍。`3901a932` 只是给这套接线
>   补版本钉住，同源不取。
> - `9f040fe3` Alpine 运行阶段补装 `su-exec`/`shadow`：本仓是 Debian-slim，没有
>   这两个包，也没有那个 bug。**只取其教训**（见上面 `dev/check_dockerfile.py`
>   的第 7 条）。
> - `caa82c22` / `efaef036` 发版提交（只改上游 `CHANGELOG` 与版本号），不取。
>
> 本批没有 Go `upstream/` 改动。**发布前需要真机验证**：本机 docker daemon 未
> 运行，`setpriv` 降权路径只在结构层面被测试锁住（语法、挂载点判断、PUID=0
> 回落、工具齐备），没有跑过真实容器。第一次发版前请在有 docker 的机器上
> `docker compose up -d --build` 起一次，确认容器内 `id` 是 PUID 而不是 0。

> **流式看门狗批（2026-11-26，`caa82c22` → `a83d93738b`）**：2 个提交全取。
>
> - `6c7648e4d3` 流式转发补客户端断连检测与「内容空闲」看门狗（issue #485）：
>   `UPSTREAM_TIMEOUT` 是 httpx 的逐字节 read timeout，保活帧会不断重置它，
>   于是「连接还活着、正文早就不产出」的流能挂到 999 秒。本仓的流式出口比
>   参考仓多（直连 + 平台 raw 透传 + 平台 `openai_sse` + anthropic / responses
>   两个转换层），所以做成「一处配置、五个出口」：`server/config.py` 新增
>   `STREAM_IDLE_TIMEOUT`（`WB_STREAM_IDLE_TIMEOUT`，默认 120 秒 / 0 关闭），
>   `gateway._sse_frame_has_content()` 按**单帧**判断 content /
>   reasoning_content / tool_calls（不能用累计的 `translator.saw_content`，
>   它在首个 token 后永远为真，看门狗会当场失效）。**有意偏离**：raw 透传
>   路径只断开上游并把原因写进 `request_logs.error`，不注入网关自造的 SSE 帧
>   （截断由客户端按「流未正常结束」处理）；转换层复用既有的 `error` /
>   `response.failed` 收尾。参考仓的 `docker-compose.full.yml` 本仓不存在，跳过。
>   测试：新增 `server/tests/test_stream_idle_watchdog.py`（假上游一帧正文后只
>   发保活，断言 0.55 秒内中断、上游连接关闭、日志含 idle、响应体无注入帧），
>   `test_platform_sse.py` 补 idle / disconnect 两项。
> - `a83d93738b` 设置页时长字段在配置缺该键时显示 undefined 并标红（issue #486）：
>   `pickValues` 先归一成同一个字符串再判合法，键不存在（`undefined` / `null`）
>   明确回退 `f.def`；`dev/verify_new_controls.mjs` 补回归断言。
>
> 本批没有 Go `upstream/` 改动。

> **流式看门狗修正（2026-11-27，`a83d93738b` → `423d697acb`）**：1 个提交，修上一批
> 自己引入的误杀。上一批按 `content` / `reasoning_content` 判活跃，而长时间工具
> 调用（生成一个大文件的参数）可能连续几分钟只有工具调用增量、没有正文——那条
> 流会被当成空闲并中断。改为「这一块里有 `data:` 行就算活跃」，只有纯保活注释
> （`: keepalive`）才计时。
>
> 本仓的判定下沉到传输层 `server/services/wbforward.py::has_data_frame()`（按**行首**
> `data:` 判，正文里的 `"data:"` 在 JSON 值内部不会误续命），因为 raw 透传与
> `_platform_chat` 两条路径都在那里取 SSE 文本；`gateway.py` 两个 generator、
> `anthropic._translate_stream`、`responses._stream_events` 都改成先判它再解析。
> `gateway._sse_frame_has_content()` 随删除（唯一用途就是续命）。首字延迟仍按
> 正文 delta 判——工具调用帧**不算**首字，否则「上游多久开始回话」会失真。
> `platforms/sse.py` 的 `openai_sse` 顺带把判据从「除 role 外还有任意键」收紧成
> 「除 role 外还有非空值」。raw 透传路径仍然只断开、不注入错误帧（本地偏离保留）。
> 测试：`test_stream_idle_watchdog.py` 加 `_ToolStream` 与
> `test_tool_call_only_stream_is_not_killed`，转换层补两条工具帧用例，
> `test_platform_sse.py` 补 `test_tool_call_only_stream_is_not_idle`。

> **续期失败日志 uid 批（2026-11-27，`423d697acb` → `aac5cff8d0`）**：1 个提交
> （issue #477）。旧版 `_note_failure` 只传 `{'uid': '', 'file': name}`，而 `_record`
> 取 `uid or file`，于是历史行的 uid 是账号**文件名**；任务记录页的版本筛选按 uid
> 查账号表，文件名永远查不到，那条失败记录在两个版本视图里都被整段过滤掉。
>
> 本仓的写入端早已修好（`renew.py` 的四个 WorkBuddy 失败分支都传真实账号 dict），
> 所以这次只补**读端**：`accounts._account_meta_resolvers` 增加 file → nickname
> 反向表、`_realm_uid_filter` 把文件名也映射到版本，两种形态（`x.json` 与去掉
> `.json` 的 `x`）都收、都用 `setdefault`，真实 uid 的映射与前缀匹配逻辑不动。
> 测试：`test_issue51_realm_summary_rows.py` 新增 `LegacyFileUidCompatTest` 两条。

> **首帧延迟批（2026-11-27，`aac5cff8d0` → `e846aa27b6`）**：1 个提交（issue #493，
> 接手 PR #495）。现有 `first_token_ms` 量的是「首个正文/推理 delta」，而 #493
> 现场上游 TTFB 1.48s、面板首字 125s —— 上游先发 role 帧，模型再思考，正文最后
> 才来。新增 `first_frame_ms` 记「首个**带负载的 `data:` 帧」，两个口径并列。
>
> 口径：`: keepalive` 注释与 `data: [DONE]` 都不算首帧（前者是排队心跳，算进来
> 会让首帧退化成建连延迟；后者不代表产出过内容）。判定落在传输层
> `wbforward.has_payload_frame()` / `first_frame_probe()`（跨块拼残片、未命中只留
> 最后一行），与看门狗的 `has_data_frame()` **分开**：看门狗要包含 `[DONE]` 与空
> payload，首帧不能算。`db.py` 加列 + 迁移 + INSERT 三处同步；`gateway._record`
> 加 `first_frame` 参数；raw 透传与 `_platform_chat` 在活跃度判定前打点；
> `platforms/sse.py` 的 `on_finish` 从 3 参扩到 4 参（那层拿到的是已解析 chunk，
> 保活帧早被丢掉，「第一个 chunk」就是首帧）；anthropic / responses 转换层同样
> 打点并放进 `state['first_frame']`。读端 `/api/logs` 加字段，日志页加一列一行，
> 五语言 i18n 补键，前端一律 `!= null` 判断（0 是合法值）。
> 测试：新增 `server/tests/test_first_frame.py`（列/迁移/写库回读、口径单测、
> role-only 先到 300ms 后正文、保活不算首帧、`[DONE]`-only 为 NULL、三条流式
> 协议端到端），`test_platform_sse.py` 补 3 条（role-only 定首帧、空流为 None、
> `on_finish` 四元组）。

> **登录态失效标记批（2026-11-27，`e846aa27b6` → `79d6977d42`）**：1 个提交
> （issue #490）。账号登录态被上游作废后 billing 返回 401，面板拿不到实时积分只能
> 回退展示 `/status` 旧快照 —— 那个数字会让用户以为账号还能用；而前端原先只在
> 「未加载」分支用 `invalid_reason`，池内账号显示的是「在线 + 积分空白」，tooltip
> 还说「上游不可达」，恰好把「该重登」说成「上游坏了」。
>
> 后端：`credits.py` 新增标记表（settings 的 `credits_session_dead`），判定克制是
> 重点 —— 只认**明确的 401**（`查询失败 code=401`，`4010` 不误命中；`查询异常` 与
> 带「代理/proxy」的文案不算）、连续两次才置位（纯 401 可能是网关抖动或 token 竞态）、
> 非 401 的临时失败**不改**标记（否则一次超时抹掉有效告警）、查询成功/重登成功/
> 删号都清、只在计数变化时写库、7 天 TTL、兼容旧的「uid → 时间戳」形态。
> `wb2api.merge_pool_status` 下发布尔 `needs_relogin` 并把 `credits` 置空（上游那份
> 也是旧快照），配 30 秒短缓存（列表页每个账号都要读它）。`accounts._save_and_finish`
> 落盘成功后清标记，用户重登完不必等下一次积分查询。
> 前端：`account-status.ts` 新增 `needsRelogin` 分档（归「需要注意」，优先于在线/
> 冷却判定），徽章与积分格都显示「需重登」、tooltip 给「重新扫码」；仪表盘分档计数
> 补新键；五语言 i18n 补 `badgeNeedsRelogin` / `badgeNeedsReloginTitle`。
> 测试：新增 `server/tests/test_session_dead.py` 8 项，`web/lib/account-status.test.mjs`
> 补两项。

> **保活闸门批（2026-11-27，`aac5cff8d0` → `9290eeac39`，补丁 0058）**：1 个提交
> （issue #489）。refresh 响应实测 `expiresIn=5184000` 秒（60 天），而旧行为每天把
> 全池刷一遍——每个令牌一生要打 60 倍于必要的刷新请求，30+ 个号挤在同一分钟，正是
> 上游判「过频」的诱因；429 还会被记成 `failed` 并推进 session_dead 计数，三轮就能
> 把健康号禁用。
>
> 本仓早就有 selective 窗口闸门（G-P1-15 / `764293b8` 的
> `Config.KeepaliveRefreshWindow` + `a.NeedsRefresh(window)`），所以只补增量：
> `auth.ExpiresAtValue()` 加锁访问器（锁外直读 `ExpiresAt` 是数据竞争，且必须能区分
> 「到期未知」与「已过期」——`NeedsRefresh` 对两者都为真）；
> `scheduler.keepaliveRefreshDue()` 三条判据（临期 / 到期未知且距上次刷新 ≥24h /
> 距上次刷新 ≥14 天保证 RT 轮换）；「上次刷新时刻」用 auth 文件 mtime 近似
> （`authLastWrite`，不新增会被任一条写路径静默丢掉的 `last_refresh` 字段）；
> 账号间基础间隔 0 → 2 秒；失败分支先判 `ErrSoftRate`，记 `skipped` 而不算失败、
> 不进 session_dead 计数。
>
> 两处有意偏离：手动「全部保活」仍是**全量**（`RunKeepaliveNow` 传 `selective=false`，
> 人点了按钮就是要立刻全刷，也是运维唯一明确的强制轮换手段）；闸门跳过的号记
> `not_due` 而不是 `skipped`，否则日志看不出是窗口在起作用还是池里没号。
> 上游把 56 行的 `keepalive_window_test.go` 整份换成 185 行并全改调 `RunKeepaliveNow`，
> 本仓保留原文件的两条 `NeedsRefresh` 锚点、新增 8 条闸门用例另放
> `keepalive_due_test.go`（统一走 `runKeepalive(ctx, true)`）。
> 逐条见 `deploy/workbuddy-panel-ref.txt` 第二十八批。

> **成本账本口径批（2026-11-27，`ad1d9b9ac5` → `2d35cb339c`，补丁 0059）**：1 个提交
> （issue #491）。选号的成本分层从「每千 token 单价」改成「每次请求平均扣费」：
> credit 主要是每次请求的固定底价，几乎不随 token 增长，旧的 per-1k 实际度量的是
> 「这个号平均接到多长的请求」，跨账号能差 1300 倍——长请求号被当成「便宜」，越选
> 越长，形成正反馈。
>
> 落点：`pool/modelCostEntry` 增加累计量 `TotalCredit` / `TotalRequests` /
> `TotalTokens`，`costPerReq()` 为选号权威口径、`per1k()` 降级为诊断值；
> `NoteModelCost` 去掉 EMA（alpha=0.3）改累计量相加，负 credit 按 0 防御；
> `costTier()` 的三层语义改为「0 = 累计两笔以上 0 / 1 = 无观测或单笔 0 未知 /
> 2 = 已累计收费」，单笔 credit=0 不再直接判免费（可能只是短请求的固定底价豁免）；
> 新增 `modelCostMinTokens = 2000`，低于该 token 数的请求**只扣余额、不记账**
> （不建条目、不刷 `LastSeen`），否则 `max_tokens=5` 的短探测会长期污染排序；
> `pick.go` 的 `weighted.cost1k` 改名 `costReq`，tier 2 内按 per-req 升序。
>
> 迁移语义是这批最容易踩的地方：老 `state.json` 只有 `cost_per_1k`
> （`TotalRequests == 0`），`costPerReq()` / `costTier()` 一律沿用旧的
> `CostPer1k` + `Samples` 结论，**绝不把 `CostPerReq` 置 0**——否则升级瞬间全池按
> 0 成本挤进 tier 0。恢复侧同时剔除负累计量，落盘侧写现算的 `per1k()`。
> 逐条见 `deploy/workbuddy-panel-ref.txt` 第二十九批。

> **尾段评估（2026-11-27，`2d35cb339c` → `93395e16f1`，8 个提交全部不取）**：
> 这一段已逐条判完，没有可取的项，因此 `deploy/workbuddy-panel-ref.txt` 的最后一个
> 哈希锚点仍停在 `2d35cb339c`。逐条原因：
>
> - **`16e16e6c4f` 慢号识别 + 自动降权（#496）**：新服务 814 行，属选号/风控面且要
>   灰度。本仓账号权重有本地约束（`set_weight` 只允许持有进程内网关 API Key 的调用
>   方），自动降权会绕过它；多分组同 realm 时上游自己也承认无法区分请求归属（409）。
> - **`407db001b1` 预警短 TTL 缓存 + 批量回填（#476）**：优化对象是上游 282 行的
>   `warnings()`（逐行 SELECT 回填 detail）。本仓同名函数约 40 行、单次 LIMIT 200、
>   没有那条 N+1 路径，收益接近于零，缓存反而给暂停/恢复状态加延迟。
> - **`b7aa9ad51f` 每日任务统一组件（#478）**：1154 行里 938 行是 Next.js 组件，
>   本仓面板不是同一套组件树；后端语义绑在那个组件的交互上，没有落点。
> - **`ad1d9b9ac5` / `d2117697cb` 数据库维护与手动压缩（#492 两步）**：本仓已有
>   `request_logs` 的滚动清理与侧表保留期/条数上限，缺的只是 `checkin_logs` /
>   `account_risk_events` 的保留期；但上游方案依赖它的 `tap` 模块与整套
>   `RETENTION_DAYS` 开关，还要改掉 `_prune_request_logs` 的签名与语义（返回值、
>   0 = 永久保留）——动的是**每笔请求都会走**的写入路径，该独立排期。第二步的
>   压缩面板依赖第一步，没有基座。
> - 剩下 `4385320f0c`（上游 deploy/README 变量表）、`c1853fe9be` / `93395e16f1`
>   （上游 CHANGELOG）都是上游文档，本仓结构与版本线不同。

**`shuishuipingan/qoder2api-hub`**（Qoder 协议层参考源）不是 fork、也没有共享
历史，接入方式是**整目录 vendor**，不按提交 cherry-pick：

```bash
git remote add qoder-ref https://github.com/shuishuipingan/qoder2api-hub.git
git fetch qoder-ref --no-tags
```

已核对到 `44b6f33`（v1.9.11）→ `25d0594`（v1.9.12）→ `285d98be`（v1.9.13）→
`88753423`（v1.9.14）→ `3cc8617e88`（v1.10.1）→ `36fa92959`（v1.10.3）。基线、
取用范围与两处本地补丁（账号权重、明文导出）记录在 `deploy/qoder-ref.txt`。
升级方式是重跑
`python server/platforms/qoder/tools/vendor_qoder.py --source <clone>`，
`--check` 校验一致性；重新 vendor 后必须重新应用那两处补丁
（`server/tests/test_qoder_vendor.py` 守着）。

> **Qoder 批（2026-11-27，`36fa929592` → `2b0981c991`，v1.10.4）**：1 个非 merge
> 提交，整目录重 vendor。三条线都只改「怎么说」不改协议：①**签到回执**（#174）
> `claim_campaign` 把金额单独归一（`benefit.amount` 优先、`amount` 兜底，
> malformed 记 0 而不是当成正收益），新增 `amount_reported` 区分「上游明确回报 0」
> 与「上游没回报金额」，`run_checkin` 只在 `only_daily` 且 `earned == 0` 时改写成
> 「上游确认处理，本次未新增积分」，并新增 `state`
> （`claimed` / `already_claimed` / `no_credit`）供上层区分。②**模型回显**（#175）
> 已知目录模型被上游回报通用标签 `auto` 时，Chat Completions 响应的 `model`
> 回显请求名（`_chat_display_model`，只在这一种组合下改写；上游明确回报的其他
> 模型名、未知 / BYOK、自动路由都不动，usage 的 `upstream_model_key` /
> `resolved_model` 仍独立记账）。③**思考意图审计**（#176）`requested_effort`
> 拆成 `reasoning_request` 返回 `(档位, intent)`，`intent` 区分 `absent` /
> `level` / `disabled` / `enabled_default` / `budget_unsupported` /
> `unrecognized`（显式关闭的四种写法都归 `disabled`），随 `session_trace` 落
> usage 行的 `reasoning_intent`；`chat done` 日志补 `reasoning_tokens`。
> 账号权重与明文导出补丁已按 `_vendor/README.md` 的落点重放；`--check` 与
> Qoder 四套件 100 项测试通过。逐条见 `deploy/qoder-ref.txt` 第二十四批。

> **Qoder 批（2026-11-26，`3cc8617e88` → `36fa92959`，v1.10.3）**：5 个非 merge
> 提交，整目录重 vendor。三条线：①**出站身份句守卫**（PR #168）：上游对第三方
> Agent 品牌身份句做大小写敏感的字面检测，命中返回 HTTP 200 + SSE 内嵌
> 403/10605 硬拒。新增 `qoder_identity_guard.py`，只在命中句子（逗号/句号前）
> 插一个空格，自家品牌 Qoder 豁免、无命中零改动；本仓**跟随上游默认（关闭）**，
> 需要时设 `QD_IDENTITY_GUARD=1` 开启。②**网关默认值加固**（v1.10.2）：
> 推理凭据不再接受 `?key=`；HTTP 导出默认只给元数据；面板口令至少 12 位且
> 会话绑定当前口令版本；settings 原子写、读失败不覆盖、写入补锁；`/realm` 与
> 设置类路由补面板鉴权；桌面凭据导入只认服务端扫描结果；新增三个仅终端可用
> 的离线入口。③版本发布。
> `vendor_qoder.py` 的 `MODULES` 补了 `qoder_identity_guard.py`；账号权重补丁
> 已在 `/accounts/set` 段后重新应用；**明文导出**补丁改为只对持有网关 API Key
> 的调用方放开 `secrets=1`（浏览器面板仍只能读元数据），本仓管理端的导出功能
> 因此不受 1.10.2 的 403 影响。`--check` 与 qoder 相关 98 项测试通过。

> **Qoder 批（2026-11-25，`88753423` → `3cc8617e88`，v1.10.1）**：8 个代码提交，
> 整目录重 vendor。四条线：①抓取（fetch）后端可与搜索后端分开配置
> （`web_fetch_backend` / `web_fetch_external`，默认 `inherit`；endpoint 变化清旧
> Key，环境变量覆盖时不写盘）；②Anthropic 入口按请求决定 thinking（客户端显式
> 要思考才发空签名 block，显式 off 仍优先；thinking 固定在 text 前；
> `stream_anthropic_events` 重写块生命周期与工具参数缓冲）；③usage 补
> `output_tokens_details.thinking_tokens`（兼容保留 `reasoning_tokens`）；
> ④上游 `provider_error` + `Cannot find model` 归请求级故障，不重试、不换号、
> 不冷却，SSE 内嵌错误同样识别。模型目录新增限时活动模型 HungerG
> （INTL、free，`CATALOG_SUPPLEMENTS` 到期自动隐藏）。`vendor_qoder.py` 清单不变，
> 账号权重补丁已重新应用（`/accounts/weight` 路由锚点跟随上游迁到 `/accounts/set`
> 段后），`--check` 与 qoder 相关 132 项测试通过；`embedded.py` 无需改动。
> 面板侧 web fetch 表单与 docs 更新不取（同上批：token-farm 无托管工具设置界面，
> 配置走环境变量或 `accounts/settings.json`）。

> **Qoder 批（2026-11-24，`285d98be` → `88753423`，v1.9.14）**：6 个非 merge 提交，
> 整目录重 vendor。这一批是 **Anthropic 托管网络工具**：新增
> `qoder_anthropic_web.py`（`web_search_20250305` / `web_fetch_20250910` 翻成
> Qoder 托管工具并按 Anthropic 事件序列回给客户端），`qoder_proxy.py` 加挂载点，
> `qoder_anthropic.py` / `qoder_web_response.py` / `qoder_webtools.py` 只做小改。
> `vendor_qoder.py` 的 `MODULES` 补了 `qoder_anthropic_web.py`；账号权重补丁已重新
> 应用，`--check` 通过。`88753423` 本身是版本号与发布说明，不取。

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

**`shuishuipingan/trae2api-hub`、`ZFXing-lite/discovery2api`、
`wearetheone777/dumate2api`、`luawei1/cline2api`、`Kilo-Org/kilocode`** 五个来源
同样**不 fork、不 vendor**（Qoder 是唯一整目录 vendor 的例外），协议事实按能力在
`server/platforms/{trae,discovery,dumate,cline,kilo}/` 重写。各自的锚点提交、
逐项取用 / 未取原因与实测记录在 `deploy/*-ref.txt`。

> **协议批（2026-11-24）**：五个来源各判一轮，共取 10 项（另取 1 项的一半）。
>
> - **trae**（`e7eef187` → `f7b75787`，5 个非 merge）：取 `355503e6` SOLO 内联图片
>   边界校验（单张 5 MiB / 单请求合计 10 MiB，decoded 字节，只对 user 角色、严格
>   base64、拒远程 URL，跨消息累计）。不取 `1773f8b0`（收尾帧已带 usage）、
>   `5190977f`（`normalize_content` 已原样透传非文本 part、`normalize_chat_images`
>   已归一 `input_image` / Anthropic `source`）、测试整理与发版。
> - **discovery**（`3e8463a6` → `35a8d552`）：取 `f06bb548` 请求方言归一（剥
>   `reasoning_effort` / `reasoning` / `verbosity` / `store` / `previous_response_id`
>   / `truncation` / `instructions` / `text`，`max_completion_tokens` 收敛成
>   `max_tokens`，`developer*` 角色改 `system`，标准体原样返回同一对象）、
>   `35a8d552` 慢速 400 跨键重试（耗时 ≥10s 且错误码为
>   `upstream_request_rejected` 才换键，预算独立于 `max_accounts_try`）、
>   `980bb415` 只取「陈旧目录兜底」（在线失败改用上一份真实目录，不再一过 TTL
>   就缩成内置 8 条）。池级熔断 / `HealthCheckAll` / `/v1/models` singleflight /
>   sample-keys 指标不取：本仓没有那套状态机，探针是逐账号 `_probe`。
> - **dumate**（`54f603d4` → `9f02228e`）：取 token 字段归一（云端网关只认
>   `max_tokens`，双发 400、只发 `max_completion_tokens` 被静默忽略，先收敛成
>   唯一 `max_tokens` 再做上限钳制）、思考档位（白名单五个后端认
>   `none/low/high/max`，`low` 归一 `high`，`none` 必须显式
>   `thinking:{type:'disabled'}` + `enable_thinking:false`，对外暴露
>   `['none','high','max']`）。桌面清单三级降级本仓已有、积分分池与实测倍率探针
>   不随包跑；面板 / 端口统一 / Docker 不取。
> - **cline**（`d5cb69ae` → `f459f003`）：取 token 自愈（`invalid_grant` 时走
>   WorkOS `authenticate` + `grant_type=refresh_token` 再换一次，只有它判 401，
>   网络错误不消耗一次性 refresh token；成功后重跑注册，失败退回 WorkOS access
>   并按 1 小时减 60 秒兜底）、工具名截短还原（>64 字符截成「前 55 + `_` +
>   sha256 前 8 位」，覆盖 tools / tool_calls / tool_choice，流式按行改写、
>   无映射原样返回）；另取 `68751e7ea` 的**被动一半**——真实请求的模型级成败
>   参与模型列表排序（可用 → 未知 → 失败，失败 30 分钟 / 成功 2 小时保鲜，
>   没有信号时顺序不变，只影响排序不影响可用性判定）。主动 15 分钟补测与
>   `probeTier` 排序体系不取：本仓有 402 冷却、无池级状态机，补测还会额外消耗
>   免费额度；反向故障转移模型归属修复不适用（本仓无 zen 池）。
> - **kilo**（`bfdec5496`，只看 `packages/kilo-gateway`）：取
>   `d7496501c6` 网关地址可覆盖（`KILO_AI_GATEWAY_URL` 优先，legacy
>   `KILO_API_URL` → `${api}/api/gateway/v1`，默认
>   `https://ai-gateway.kilo.ai/api/v1`）、`cf41438e82` 推理档位
>   （`opencode.reasoning_options` → `efforts`：`[]` → `high`，只有 toggle →
>   `none`/`high`，effort 档位去重后按上游 schema 白名单过滤，认不出的条目逐条
>   丢弃；档位经适配器与测试台下拉透出）。legacy `variants` 不取（语义是「变体名
>   → 参数覆盖」，不是档位集合）；auto-routing worker / 计费 / 遥测不取。
>
> **复核（2026-11-24，`git fetch --all --prune` 之后）**：其余七个来源
> （`upstream` / `trae-ref` / `qoder-ref` / `discovery-ref` / `cline-ref` /
> `kilo-ref` / `loomy-ref`）相对各自锚点**均无新提交**。dumate 有 14 个新提交
> （`9f02228e` → `db5c4348`），其中 5 个上一批已判过，其余为一份 WIP 快照
> （`878ea2c3`）、两个文档提交、四个 merge 与一个发版提交，**逐项核对后无新增
> 可取项**：413 排空方案已用三种客户端形态实测本仓不存在「RST 吞响应」问题；
> 指纹清洗归上游 `workbuddy2api` 负责（本仓只做组合转发）；其余是参考仓的池 /
> 面板 / 调度组件。详细理由与实测结论见 `deploy/dumate-ref.txt` 第三批。
> 核对过程中另外发现并修掉两个真实缺口（嵌套业务码解析、探测会话头族的等值
> 关系），见该文件同段。
>
> **discovery 增量（2026-11-25，`35a8d552` → `763ed2f510`，1 个提交）**：取三项
> 工程语义——①链路失败（DNS / 超时 / 代理断开）不归因到 Key：即使打开
> `cooldown_5xx` 也不冻号，`_chat_accounts` 换下一个账号继续（每个账号可走不同
> 代理），全部失败才抛出最后错误；②`Cluster TPM` 429 是账户级上限，标记为
> `cluster_rate_limited`，不冷却、不轮换，按 retry-after 等窗口（≤15s × 2 次）
> 后重试**同一账号**；③`model_disabled` + `suggested_model` 保留 404 与稳定
> code，并把替代模型写进错误文案。参考仓的 model 静默改写、池级
> `ReviveStaleLinkRests` / `ProbeRound` / 定时 sample、缓存落盘与 15 分钟刷新
> 不取（本仓已有 DB 目录缓存 + `catalog_ttl` + 陈旧兜底；探针是人工触发，
> 冷却清除有显式入口）。测试从 35 项扩到 42 项。

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

### 已评估：misakano `d221c721fc..b1233f09`

第二十批之后参考仓又推了 6 个非 merge 提交（2026-11-25），**取 1 项**：

- **取 `392f8ac7`**（issue #283）：模型目录降级 WARN 每种只报一次。global 域的企业
  端点永续 500、而 `/v3/config` 每次都接住时，每轮 warm model rates 都重打一行
  WARN，真故障会被淹掉。参考仓新增 `degradedWarnOnce(key, format, args...)`
  （进程内 map + mutex，同 key 只打一次），四条降级路径各给一个 key。本仓按结构重写：
  `upstream/internal/upstream/global_models.go` 放 `degradedWarnOnce` 与两个包级变量，
  两处等价落点各给两个 key——`probeGlobalModels`（`global-models/v3`、
  `global-models/enterprise`）与 `client.go` 的 `FetchModels`（`fetch-models/v3`、
  `fetch-models/enterprise`）。新增 `upstream/internal/upstream/degraded_warn_test.go`。
  反向代价与参考仓相同：恢复后再失败不会重新告警，得等重启。
- **不取 `6dc02b30` / `f60c68d5` / `2e2358ca`**（browser-complete 控制标记救援、
  续写提示中性化、合成续写不带网关身份）：落点集中在
  `internal/server/{browser_complete.go,continue.go,narration.go,toolonly.go}`。
  本仓 `upstream/internal/server/` 有 60 个 go 文件但**没有**这几个——推理续写状态机
  与 browser-complete 的独立控制标记在本仓整段不存在，照搬会凭空多出一套没人驱动的
  中间层。
- **不取 `53fbd2ec`**（面板「错误汇总可标已读」+ 账号池批量导出）：前者依赖参考仓
  内嵌 Go 面板的日志环聚合表（`internal/panel/errors.go` 的 `ErrStat` + `last_unix`），
  本仓日志页按请求行直出，没有「按账号 × 错误类型聚合 + 本机已读标记」这一层；
  后者的能力本仓已有——`GET /accounts/export`（明文凭据、批量、审计日志
  user/count/ip），导出格式与本仓导入格式的对齐已由 `test_account_upload.py` 覆盖，
  不需要再抄一份参考仓的 `{accounts:[...]}` 形状。
- **不取 `b1233f09`**（每日变化柱状图 `<title>` 挪进柱子自身）：参考仓是手写 SVG
  字符串，把 `<title>` 挂在 `<svg>` 根下导致所有柱子共用第一条提示；本仓仪表盘的
  柱子/折线是 React + recharts（`dashboard/page.tsx` 的 `<Bar>` + `<Tooltip>`），悬停
  提示由组件按每个数据点渲染，不存在「兄弟节点 `<title>` 被当成整图说明」这个形态。

逐条见 `deploy/workbuddy-panel-ref.txt` 第二十一批。

### 已评估：misakano `b1233f09..e39c2528ba`

第二十一批之后参考仓又推了 4 个非 merge 提交（2026-11-25），**取 2 项**：

- **取 `e39c2528ba`** TraeCode 自述句进脱敏表：上游按「interactive agent in
  TraeCode」这一连续短语判非官方渠道，命中即 400 `11-128`；单独 `TraeCode` /
  `in Trae IDE` / `in Cursor` 都 200。参考仓在 `sanitizeFeatures` 加
  `"You are an interactive agent in TraeCode"`、在 `sanitizeRewrites` 加
  → `"You are an interactive agent"`（只删这一段，句子其余部分与行为指令原样
  保留）。本仓 `sanitize.go` 结构相同，直接落位；逐字节快照
  `TestSanitizeLiteralByteExact` 的特征数 9→10、改写对数 7→8，并新增
  `TestTraeCodeIntroRewritten`（含「散词 `TraeCode` 不得误伤」）。
- **取 `e64f7d3162`** tool 配对吸收 hub #26 三修：①相邻两批
  `assistant.tool_calls`（中间夹正文）先合并（`mergeAdjacentToolCalls`）；
  ②repack 遇到孤儿 tool 结果不再 `break`，改成暂存后移；③cleanup 对同一
  `tool_call_id` 的重复结果只留第一条。本仓 repack 的变量名/边界写法不同
  （`notices` / `noticeBeforeLaterResult` vs 参考仓 `between` / `sawNonTool`），
  按本仓结构重写并保留既有边界语义：结果前的 user/assistant 是轮次边界，
  只有 system/developer 视为插入物；后续带 tool_calls 的 assistant 是新组头，
  必须交还外层。`payload.go` 管线改为 合并 → 重排早到结果 → repack → 清理。
  新增 `upstream/internal/upstream/tool_pairing_hub26_test.go`。
- **不取 `f4c6f89b19` / `787401df7d`**（embeddings 请求级错误码与 413 归类、
  面板 `cool_kind` 熔断）：本仓没有 embeddings 数据面（全仓搜 `/v1/embeddings`
  零命中），整条链路不存在，没有落点。

逐条见 `deploy/workbuddy-panel-ref.txt` 第二十三批。

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
