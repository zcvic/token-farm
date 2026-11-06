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
