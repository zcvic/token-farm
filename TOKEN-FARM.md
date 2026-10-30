# token-farm

**多平台 token 农场**：一套面板同时管理 **WorkBuddy（腾讯 CodeBuddy）** 与
**Trae** 两类账号，统一对外提供 OpenAI 兼容接口。

本项目派生自 [`workbuddy-manager`](https://github.com/ithtelab/workbuddy-manager)
（MIT）。上游的全部能力都保留且行为不变 —— 账号池、扫码纳管、签到保活、密钥分发、
IP / 模型白名单、用量与日志。在此之上新增：

| 新增 | 位置 | 说明 |
|---|---|---|
| 平台适配层 | `server/platforms/` | 以 `Platform` 契约为边界，WorkBuddy 仍委托给 `workbuddy2api`，Trae 为原生实现 |
| Trae 适配器 | `server/platforms/trae/` | 账号存储 / 令牌刷新 / 签到 / 额度 / 模型 / 对话（SSE → OpenAI chunk） |
| 网关分流 | `server/routers/gateway.py` | 模型名带 `trae/` 前缀走 Trae，其余保持原样走 WorkBuddy |
| 多平台接口 | `server/routers/platforms.py` | `/api/platforms/*`：账号、导入、刷新、签到、额度、模型 |
| 多平台页面 | `web/app/(main)/platforms/page.tsx` | 底栏「运营」组 + ⌘K，五语种 |
| 设计文档 | [`docs/multi-platform.md`](docs/multi-platform.md) | 契约、路由、凭据安全、已知限制 |

> **凭据只在本机**：平台账号落在 `data/platforms/<平台>/accounts/`，接口只返回
> 脱敏预览，明文令牌永不下发到浏览器。

---

## 一、项目结构

仓库根就是可部署单元，没有嵌套目录：

```
token-farm/
├─ server/              # FastAPI 管理端 + 网关（Python）
│  ├─ platforms/        # 多平台适配层（base 契约 / trae 实现 / sse 拼帧）
│  ├─ routers/          # /api/* 与 /v1/* 路由
│  ├─ services/         # 上游交互、腾讯协议客户端、统计、更新器……
│  └─ tests/            # 全部单测（unittest，禁止联网）
├─ web/                 # Next.js 15 前端（构建后产物在 web/out）
├─ deploy/              # 部署脚本
│  ├─ deploy.sh         # 一键部署（Linux/macOS，Docker Compose）
│  ├─ deploy.ps1        # 一键部署（Windows，Docker Compose）
│  ├─ sync-upstream.sh  # 同步参考项目（Linux/macOS）
│  ├─ sync-upstream.ps1 # 同步参考项目（Windows）
│  ├─ trae-ref.txt      # Trae 参考源里已人工核对过的提交（--refs 的基线）
│  ├─ install.sh        # 上游原版：装依赖 + systemd（Linux 服务器）
│  └─ windows-native/   # 上游原版：原生 Windows（无需 Docker）
├─ docs/                # 用户文档与设计文档
├─ windows/             # 原生 Windows 启动 / 安装器工具链
├─ data/                # 运行时数据（**不入库**）：db、账号、平台凭据
├─ docker-compose.yml   # 管理端容器（上游 workbuddy2api 作为兄弟目录挂进来）
└─ .env.example         # 环境变量模板
```

`data/` 与 `.env` 都在 `.gitignore` 里。**任何情况下都不要提交它们**。

---

## 二、部署

三种方式按场景选：

| 场景 | 命令 |
|---|---|
| Linux / macOS 服务器，一条命令起全套 | `bash deploy/deploy.sh` |
| Windows（Docker Desktop） | `powershell -ExecutionPolicy Bypass -File deploy\deploy.ps1` |
| Linux 服务器装成 systemd 服务（上游原版路径） | `sudo bash deploy/install.sh` |

`deploy.sh` / `deploy.ps1` 做这几件事，重复执行是安全的（幂等）：

1. 预检 Docker 与 Compose，缺了就直说怎么装；
2. `.env` 不存在就从 `.env.example` 生成（管理员密码留空 = 首启随机生成并打印到日志）；
3. 准备兄弟目录 `../workbuddy2api`（已有就复用；没有就 `git clone` 一份）；
4. 建好 `data/`，Linux 下把属主改成容器内的 `10001`（bind mount 的经典坑）；
5. `docker compose build` + `up -d`，轮询 `/api/healthz` 至就绪；
6. 打印访问地址与初始密码，以及下一步该干什么。

常用子命令：

```bash
bash deploy/deploy.sh up        # 默认：构建并启动
bash deploy/deploy.sh build     # 只构建
bash deploy/deploy.sh restart   # 重启（不改代码）
bash deploy/deploy.sh ps        # 运行状态
bash deploy/deploy.sh logs      # 跟日志
bash deploy/deploy.sh down      # 停止（保留 data/）
```

可覆盖的变量：`TF_UPSTREAM_DIR`（上游目录）、`TF_UPSTREAM_SRC`（上游源码包/目录）、
`TF_UPSTREAM_REPO`（上游 git 地址）、`TF_UPSTREAM_BIND`（上游端口绑哪个宿主地址）、
`TF_SKIP_UPSTREAM=1`（只起管理端）、`TF_NO_BUILD=1`（用已有镜像）、
`TF_HEALTH_TIMEOUT`、`TF_PYTHON`（生成 api_key 用）。

关于 `TF_UPSTREAM_BIND`：管理端**跑在容器里**，容器内的 `127.0.0.1` 是它自己。
所以脚本会按平台决定上游端口绑哪：

| 平台 | 上游绑定 | `WB2API_BASE` |
|---|---|---|
| Linux | docker0 网关（通常 `172.17.0.1`，`docker network inspect bridge` 取） | `http://172.17.0.1:7863` |
| Windows / macOS（Docker Desktop） | `127.0.0.1` | `http://host.docker.internal:7863` |

两处都会自动写进配置：上游 compose 的端口行，以及 `.env` 里那个
`WB2API_BASE=http://127.0.0.1:7863`（`.env.example` 的老形态，在容器里等于连自己）。
你自己填的服务名（`http://workbuddy2api:7863`）、`host.docker.internal` 或某个局域网
IP 都**不会被改**——那是明确的意图。

细节（反向代理 / HTTPS、端口收敛、子路径部署、容器内操作 docker、
原生 Windows、一键更新的信任链）见 [`deploy/README.md`](deploy/README.md)。

### 部署后最短路径

1. 浏览器打开 `http://<服务器>:7864`，用脚本打印的初始密码登录；
2. **WorkBuddy**：账号页 → 添加账号 → 扫码；随后在密钥页发一把密钥；
3. **Trae**：多平台页 → 「从本机导入」（管理端与 IDE 在同一台机器时）或手工粘贴
   `{"uid": "...", "token": "...", "refreshToken": "..."}`；
4. 下游把 base_url 指到 `http://<服务器>:7864/v1`，模型名写 `trae/<模型名>`
   就调 Trae，不写前缀就调 WorkBuddy。

### 备份与恢复（手动）

**只有四样东西丢了就找不回来**，其余（镜像、代码、前端产物）都能重建：

| 路径 | 里面是什么 | 丢了会怎样 |
|---|---|---|
| `data/` | `manager.db`（密钥的**哈希**、配额 / 白名单 / IP 规则 / 用量日志 / 设置）、`users.json`（管理员账号 + 会话签名密钥）、`initial-admin-password.txt`（首启明文，第一次启动才写）、`platforms/trae/accounts/*.json`（Trae 令牌与 refreshToken）、`upstream-scripts/` | 已发出去的密钥全部失效、管理员要重建、Trae 账号要逐个重新导入 |
| `../workbuddy2api/config.json` | 上游 `api_key`、线路（`proxies`）、调度（`schedule`）、`global` 段 | 管理端连不上上游，线路与排程全要重配 |
| `../workbuddy2api/auths/` | 每个 WorkBuddy 账号的 `workbuddy-<uid>.json`（accessToken） | 每个号都得重新扫码 |
| `.env` | 端口、`WB_ADMIN_PASSWORD`、出口代理、镜像加速参数 | 得一格格重填 |

> **密钥只看一次**：`api_keys` 表里存的是哈希，明文只在创建时显示。所以「备份了
> 数据库」= 下游手上的 key 继续能用；**没备份就得重新发一把并通知所有下游换 key**。
>
> 这些文件全是**明文凭据**（Trae refreshToken、WorkBuddy accessToken、等于管理员
> 会话的 `users.json.secret`）。备份包按密码对待：`chmod 600`、别进 git（`data/`
> 已被忽略）、别丢公有云未加密桶里；长期存放建议 `gpg -c` 或 7z 带密码。

#### Linux / macOS（Docker）

```bash
cd /opt/token-farm                      # docker-compose.yml 所在目录
TS=$(date +%Y%m%d-%H%M%S)
mkdir -p ~/token-farm-backup && chmod 700 ~/token-farm-backup

# 先停管理端：SQLite 是 WAL 模式，进程停稳再整目录拷才一定一致
docker compose stop workbuddy-manager

sudo tar czf ~/token-farm-backup/data-$TS.tar.gz     -C .             data
sudo tar czf ~/token-farm-backup/upstream-$TS.tar.gz -C ../workbuddy2api config.json auths
sudo cp .env ~/token-farm-backup/env-$TS
sudo chmod 600 ~/token-farm-backup/*-$TS*

docker compose start workbuddy-manager      # 备份完立刻起回来
```

`sudo` 是因为 `data/` 属主是容器 uid 10001（`chown -R 10001:10001 ./data` 那一步）。

#### Windows（Docker Desktop）

```powershell
$ts  = Get-Date -Format 'yyyyMMdd-HHmmss'
$dst = "$env:USERPROFILE\token-farm-backup"
New-Item -ItemType Directory -Force $dst | Out-Null

docker compose stop workbuddy-manager

Compress-Archive -Path .\data -DestinationPath "$dst\data-$ts.zip" -Force
Compress-Archive -Path ..\workbuddy2api\config.json, ..\workbuddy2api\auths `
                 -DestinationPath "$dst\upstream-$ts.zip" -Force
Copy-Item .env "$dst\env-$ts"

docker compose start workbuddy-manager
```

**原生 Windows**（`windows\start.ps1`，无 Docker）：上游在仓库的 `upstream\`，
数据在 `data\` —— 先 `windows\stop.ps1`，再复制 `data\`、`upstream\config.json`、
`upstream\auths\`、`.env` 这四样即可。

#### 不想停机

- 只备数据库、要求一致：`sqlite3 data/manager.db ".backup /tmp/manager.db"`
  （WAL 下 `.backup` 是安全的在线快照），再把 `users.json`、`platforms/`、
  `initial-admin-password.txt` 单独拷。**不要**在运行中直接 `cp manager.db`
  —— 可能带上半写状态或漏掉 `-wal` 里未合并的事务。
- 设置 →「数据备份」（PostgreSQL 异地备份）：定时把库推到另一台 PG，适合
  灾难恢复的「在线热备」。注意它**只备库里的业务数据**，`.env` 与 `users.json`
  不在其中，替代不了上面这套打包。

#### 恢复

```bash
cd /opt/token-farm
docker compose down

sudo tar xzf ~/token-farm-backup/data-$TS.tar.gz     -C .
sudo tar xzf ~/token-farm-backup/upstream-$TS.tar.gz -C ../workbuddy2api
sudo cp ~/token-farm-backup/env-$TS .env

# bind mount 的属主由宿主机决定，恢复后必须重设，否则 sqlite 打不开、账号读不到
sudo chown -R 10001:10001 data ../workbuddy2api/auths

docker compose up -d
```

Windows 对应 `docker compose down` → `Expand-Archive -Force` 覆盖 → `up -d`；
原生 Windows 用 `windows\stop.ps1` / `windows\start.ps1`，不需要 chown。

#### 重新跑过部署脚本之后，恢复旧数据

`deploy.sh` 是幂等的，**不会**清数据：它只在 `.env` 缺失时生成、在
`config.json` 缺失时新建、在 `auths/` 缺失时建空目录。所以同一台机器上重跑脚本
基本不需要恢复。真正要「替换」的是**换了机器 / 重装系统 / 把旧环境的包搬过来**：

```bash
cd /opt/token-farm
docker compose down                       # 必须是 down：停稳后再动 data/

# 旧的不要直接删，改名留底（出事能倒回去）
TS=$(date +%Y%m%d-%H%M%S)
mv data data.old-$TS
mv ../workbuddy2api/auths ../workbuddy2api/auths.old-$TS
mv ../workbuddy2api/config.json ../workbuddy2api/config.json.old-$TS
cp .env .env.old-$TS

# 还原
tar xzf ~/token-farm-backup/data-$TS.tar.gz     -C .
tar xzf ~/token-farm-backup/upstream-$TS.tar.gz -C ../workbuddy2api
cp ~/token-farm-backup/env-$TS .env

# 备份若来自运行中的机器，可能带未合并的 WAL：别让它混进新库
rm -f data/manager.db-wal data/manager.db-shm

chown -R 10001:10001 data ../workbuddy2api/auths
docker compose up -d
```

**只换一部分会怎样**（这是最容易踩的）：

| 只恢复 | 结果 |
|---|---|
| 只 `data/` | 密钥、管理员、设置、Trae 账号都在；**WorkBuddy 账号池是空的**（`auths/` 没回来） |
| 只 `auths/` | 账号能读，但密钥 / 备注 / 分组 / 额度 / 日志全没了（都在 `manager.db`）；且新 `config.json` 的 `api_key` 与旧 `.env` 的 `WB2API_KEY` 不一致会 401 |
| `data/` + `auths/`（最常见的组合） | **能跑**，但要满足下面三条 |

`data/` + `auths/` 之外还有三条硬要求：

1. **`.env` 里的 `WB2API_KEY` 优先于上游 `config.json` 的 `api_key`**（`config.upstream_api_key()`）。
   两者必须一致 —— 要么把旧 `config.json` 一起带过来，要么让 `.env` 的
   `WB2API_KEY` 留空（那就按 `config.json` 走）。不一致的典型症状是「面板能登，
   但账号页/统计全红：上游返回 401」。
2. **目录属主**：新部署自动建的 `data/` 归 root（或你自己的 uid），容器以
   `10001` 跑。不 `chown` 的症状是 `sqlite3.OperationalError: unable to open
   database file`，或者「auths 里明明有文件、面板显示 0 个账号」。
3. **只能往前兼容**：新代码启动时会把旧库迁移到当前 schema（只前进）。把新版本
   的库塞回**旧代码**会缺列，起不来。

恢复完先看三处对齐：`/api/healthz` 正常 → 账号数等于备份前的数量 → 密钥页能看到
原来那些 key 的前缀。三处都对，下游不用改 base_url / key 就能继续用。

#### 备份包里应看到什么（校验一下再收起来）

```bash
tar tzf ~/token-farm-backup/data-$TS.tar.gz | head     # 第一行应是 data/manager.db
tar tzf ~/token-farm-backup/upstream-$TS.tar.gz | head # config.json + auths/workbuddy-*.json
sqlite3 /tmp/manager.db "PRAGMA quick_check; select count(*) from api_keys;"
```

（没装 `sqlite3` CLI 就 `docker compose exec -T workbuddy-manager python -c
"import sqlite3;print(sqlite3.connect('/app/data/manager.db').execute('select count(*) from api_keys').fetchone())"`。）

`quick_check` 回 `ok`、`api_keys` 计数和你发出的密钥数量对得上，这份备份才是可用的。
恢复演练请找一台空机器做，别在唯一的部署上试。

### 从 workbuddy-manager 迁移过来（升级到 token-farm）

原来用 [`ithtelab/workbuddy-manager`](https://github.com/ithtelab/workbuddy-manager)
的 `deploy.sh` 部署的，升级到本仓库**不用重装、不用重新扫码、下游不用换 key**：
本仓库是在上游基础上加了平台层，数据格式、容器名、端口、挂载路径全部兼容。

关键是认清「什么是代码、什么是状态」：

| | 位置 | 迁移时 |
|---|---|---|
| 代码 | 仓库目录（`server/`、`web/out/`、`deploy/`…） | 换成新仓库的 |
| 状态 | `data/`、`../workbuddy2api/`、`.env` | **原样搬过去**，一个字节都别改 |

#### 步骤（Docker Compose，最常见）

```bash
# 0) 先按上一节备份四样东西（data/ + 上游 config.json / auths + .env）。别跳。

# 1) 停掉旧面板（容器名相同，不停会冲突）。data/ 不会被删
cd /opt/workbuddy-manager
docker compose down

# 2) 在**同一个父目录**下放新仓库：这样 ../workbuddy2api 天然对得上
cd /opt
git clone <你的 token-farm 仓库地址> token-farm
cd token-farm

# 3) 搬状态
cp -a /opt/workbuddy-manager/data ./data
cp -a /opt/workbuddy-manager/.env  ./.env       # 没有就跳过，脚本会从模板生成

# 4) 属主（bind mount 由宿主机决定，与镜像内的 chown 无关）
chown -R 10001:10001 data ../workbuddy2api/auths

# 5) 起新面板：上游目录已存在，脚本会复用，不会重新 clone / 覆盖 config.json
bash deploy/deploy.sh
```

第 2 步的位置很重要：compose 挂的是 `../workbuddy2api`，上游目录**已经在**
`/opt/workbuddy2api`，所以新仓库必须也放在 `/opt` 下。旧目录可以先改名留着
（`mv workbuddy-manager workbuddy-manager.old`），确认新面板正常后再删。

新仓库**不要**复用旧的 `docker-compose.yml`（那份没有 `host.docker.internal` 的
`extra_hosts`、没有可选的上游地址变量）。只搬 `data/`、`.env`，compose 用新的。

#### 旧 `.env` 里唯一会被改的一行

`WB2API_BASE`。老模板写的是 `http://127.0.0.1:7863`，那是「管理端跑在宿主上」的
形态；现在管理端在容器里，`127.0.0.1` 是容器自己。`deploy.sh` 检测到这种回环值会
自动改写成该平台的正确地址（Linux → docker0 网关，Docker Desktop →
`host.docker.internal`），并把上游端口绑到同一地址。日志里会有一行：

```
[✓] WB2API_BASE 由 http://127.0.0.1:7863 改为 http://172.17.0.1:7863（容器里的回环地址连不到上游）
[✓] 上游端口已收敛为 172.17.0.1:7863（不对公网暴露）
```

#### 一键更新：默认已经指向本仓库

`server/services/updater.py` 与 `deploy/update.py` 的默认值都是

```python
MANAGER_REPO = os.environ.get('WB_MANAGER_REPO') or 'zcvic/token-farm'
```

所以面板「设置 → 系统 → 一键更新」拉的是**本仓库**的发布包（含 `server/`、`web/out/`、
`deploy/`），Trae 平台层不会被覆盖回去；Windows 的 `windows/update.ps1` 与启动器里的
「检查更新」也查本仓库的 Release。

- 换源（自建镜像、另一个 fork）：设 `WB_MANAGER_REPO=用户名/仓库名` 即可覆盖上面两处默认值。
- 发布包产物名仍然是 `workbuddy-manager-vX.Y.Z.tar.gz`：那是更新器的锚点（拆包、校验、
  回滚都按这个名字），跟品牌名无关，**不要改**。
- 想彻底关掉更新：设 `WB_MANAGER_REPO=none/none`，按钮会一直报「找不到发布」。

如果你的机器上还跑着**上游**的旧面板（`MANAGER_REPO` 还是上游），那它的一次「一键
更新」仍会把 Trae 层换回上游代码——先按上一节换成本仓库，再点更新。数据不受影响
（`data/platforms/trae/` 不随发布包走）。

#### 版本号重置为 1.0.0 后，第一次要手动装

本项目从上游的 `2.11.6` 起把版本号重置为 `1.0.0`，而更新器只会提示**比当前版本高**
的发布——所以已经在跑 `2.11.x` 的机器**看不到** `v1.0.0` 的更新提示。这一次请手动升级：

- Docker / `install.sh`：`git pull && bash deploy/deploy.sh`（数据不动）。
- Windows 安装版：下载新的 `TokenFarmSetup.exe` 覆盖安装（见
  [`windows/README.md`](windows/README.md#从-workbuddy-manager-升级)）。

升到 1.0.0 之后版本号就连续了，后续版本照常走一键更新。

同理，`WB_SYNC_UPSTREAM=1`（发布包含上游源码变化时一并更新 `../workbuddy2api`）
对本仓库的 Trae 代码没有影响，可以保持默认。

#### 如果是 `install.sh`（systemd）装的

```bash
sudo systemctl disable --now workbuddy-web     # 旧的 systemd 服务
sudo mv /opt/workbuddy-manager /opt/workbuddy-manager.old
sudo git clone <你的 token-farm 仓库地址> /opt/token-farm
cd /opt/token-farm
sudo cp -a /opt/workbuddy-manager.old/data ./data
sudo cp -a /opt/workbuddy-manager.old/.env ./.env
sudo chown -R 10001:10001 data /opt/workbuddy2api/auths
bash deploy/deploy.sh                          # 之后不再用 systemd，统一反代到 127.0.0.1:7864
```

原来的 nginx / Caddy 配置指向 `127.0.0.1:7864` 的可以不动（端口没变）。

#### 迁完核对四处

1. `bash deploy/deploy.sh ps`——两个容器都在跑；
2. `/api/healthz` 正常，仪表盘不报「上游不可用」；
3. 账号页数量 = 迁移前，密钥页能看到原来的前缀（不用重发）；
4. 多平台页能看到 Trae 区块，`/v1/models` 里出现带 `trae/` 前缀的模型。

---

## 三、同步参考项目

本仓库有两个参考源，**性质完全不同**，别混：

| 远端 | 指向 | 关系 | 怎么用 |
|---|---|---|---|
| `upstream` | [workbuddy-manager](https://github.com/ithtelab/workbuddy-manager) | 派生来源，有共享提交历史 | `sync-upstream` 默认动作：fetch + merge |
| `trae-ref` | [trae2api-hub](https://github.com/shuishuipingan/trae2api-hub)（MIT） | 协议事实来源，**无共享历史** | `--refs`：只对照，永不 merge |

Trae 这一层不是 fork，而是照着参考实现的协议要点重写的原生实现（端点 / 请求头 /
SSE 事件 / 设备指纹 / 令牌交换）。所以对它**只能人工跟进协议变化**，把它的提交
合进来没有意义（会带进它整套看板与任务编排）。

自己的提交推到 `origin`：

```bash
git remote -v                 # upstream / trae-ref = 参考源；origin = 你自己的仓库
git remote add origin <你的仓库地址>   # 只做一次
git push -u origin dev        # 开发分支叫 dev
```

本地分支约定：`dev` 是开发分支（多平台扩展都在这条线上），`main` 只用来跟
`upstream/main` 对齐（`sync-upstream` 合并时以它对照）。

### 3.1 upstream（workbuddy-manager）：合并

**看有什么新东西（不动代码）**

```bash
bash deploy/sync-upstream.sh --check      # Windows: deploy\sync-upstream.ps1 -Check
```

输出「本地没有的上游提交」清单与文件差异概况。

**拉进来并验证**

```bash
bash deploy/sync-upstream.sh              # fetch + merge + 跑测试
bash deploy/sync-upstream.sh --no-test    # 只合并
```

脚本会：要求工作区干净（否则先 commit / stash）→ `git fetch upstream` →
快进或三方合并 `upstream/main` → 冲突时列出文件并给出解法 →
跑 `python -m unittest discover -s server/tests -t .`（`TF_PYTHON` 可指定解释器）。

**手动等价操作**

```bash
git fetch upstream
git log --oneline --no-merges HEAD..upstream/main    # 上游新提交
git merge upstream/main
```

### 3.2 trae-ref（trae2api-hub）：只看，不合并

```bash
bash deploy/sync-upstream.sh --refs        # Windows: deploy\sync-upstream.ps1 -Refs
```

它做三件事：

1. `git fetch trae-ref`（远端不存在会自动按地址补上）；
2. 拿 `deploy/trae-ref.txt` 里记录的**已核对基线**和远端当前 HEAD 比；
3. 只列出**协议事实文件**的改动：`trae_client.py`、`trae_fingerprint.py`、
   `trae_accounts.py`、`trae_sign.py`、两个 `trae_catalog_*.json`、
   `pricing/trae_pricing.json`、`CHANGELOG.md`。参考仓库里其余部分是它自己的
   看板 / 调度，改多少都与你无关，不会打扰你。

看过 diff 后，两种结局：

```bash
# ① 有值得跟进的改动 → 改本仓库 server/platforms/trae/ 下的对应实现
# ② 确认与本仓库无关   → 直接把基线推到最新
bash deploy/sync-upstream.sh --refs --mark    # Windows: -Refs -Mark
```

`--mark` 只改 `deploy/trae-ref.txt`（一行 SHA + 注释），不碰分支、不产生合并。

`trae_sign.py` 是国际站 `DeviceProof` 那套签名的参考实现，第五节里的已知限制就卡在
这里；参考源一旦补全这块，`--refs` 会直接点出来。

### 冲突主要会出现在哪

改过的上游文件集中在少数几处，冲突时**保留本仓库语义**再做合并：

| 文件 | 本仓库的改动 |
|---|---|
| `server/{keysvc,main}.py`、`server/routers/gateway.py` | 注册平台路由、`trae/` 分流 |
| `web/lib/{api,types}.ts` | `platformApi` 与平台类型 |
| `web/lib/i18n/locales/*.json` | `nav.platforms` + `platforms.*`（五份必须键集一致） |
| `web/components/common/layout/ManagementBar.tsx`、`web/lib/command-palette.ts` | 底栏 / ⌘K 多平台入口 |
| `server/tests/test_*.py`、`web/lib/*.test.mjs` | 导航与计数守卫的期望值 |
| `README.md` / `README.en.md` | 顶部一行 token-farm 说明（两份必须同增同删） |

**刻意没改**的：容器名 / 镜像名 / compose 服务名 / 发布资产名 / 一键更新链路里的
包名（全部仍叫 `workbuddy-manager`）。这些是自更新的锚点，改名会让「一键更新」
和发布校验链失效，收益却只有好看。项目身份放在仓库层与用户可见层。

具体到「哪一层叫什么」：

| 位置 | 现在叫 |
|---|---|
| 仓库 / 文档 / 部署脚本 | `token-farm`（本文、`README.md` 顶部、`deploy/deploy.*`、`deploy/sync-upstream.*`） |
| 浏览器标题、登录页、底栏用户卡片、设置「关于」、API title | `token-farm` |
| `web/package.json` / `package-lock.json` / PWA 清单 | `token-farm-web` / `token-farm` |
| Windows 安装包 / 控制窗口 / 安装目录 / 开始菜单 / 卸载项 | `TokenFarmSetup.exe` / `TokenFarm.exe` / `%USERPROFILE%\token-farm` / `token-farm` |
| 容器、镜像、compose 服务、systemd unit、发布资产名、更新器里的包名 | `workbuddy-manager`（保留，见上） |

`docker compose` 的项目名取自目录（现在就是 `token-farm`），而容器名与发布资产名
仍与上游一致，这样一键更新、签名校验、`install.sh` 的 `/opt/workbuddy-manager`
约定都不用动。

Windows 那份从 `workbuddy-manager` 时代升上来的老装机：安装向导会读旧卸载项/旧默认
目录，默认**原地覆盖安装**（`.env`、账号、`data/` 都留着）；旧快捷方式、旧卸载项
和旧 `WorkBuddyManager.exe` 会在安装/卸载时一并清掉。细节见
[`windows/README.md`](windows/README.md#从-workbuddy-manager-升级)。

修改上游代码前先想一句：**这条改动能不能只放在新增文件里？** 能就别动上游文件。

---

## 四、本地开发

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r server/requirements.txt
python -m uvicorn server.main:app --reload --port 7864   # 后端

cd web && npm install && npm run dev              # 前端（默认 3000，代理到 7864）
```

测试只跑单测，不联网：

```bash
python -m unittest discover -s server/tests -t . -q
cd web && node --experimental-strip-types lib/section-nav.test.mjs
```

`web/` 里的 `*.test.mjs` 需要 `node` ≥ 22（`--experimental-strip-types`）。
没有 `web/node_modules` 时，`test_*` 里那批「跑 node 脚本」的用例会失败 ——
这是环境缺依赖，不是代码问题；`npm install` 之后即恢复正常。

架构与目录约定的更多说明见 [`docs/development.md`](docs/development.md)。

---

## 五、已知限制

- **Trae 国际站（global realm）的令牌刷新**：疑似需要 `DeviceProof`（用 iCube
  私钥做 ECDSA-P256 签名），目前只用 CN 账号验证过；国际站账号可能刷不动，
  界面会如实显示错误，不会假装成功。
- **平台调用的日志 `realm` 字段**固定写 `cn`（历史原因，纯展示字段）；平台身份
  以模型前缀 `trae/` 为准。
- 平台模型以**实时 `model_list` 为准**：本地目录只是兜底，真实账号可能只返回
  少数几个模型；不存在的模型上游会回 `4023 model is unknown`。
- 本仓库的中文文档为主（与上游一致：`CHANGELOG.md` 也只有中文）；`README.md` /
  `README.en.md` 的双语结构守卫仍然生效。

---

## 六、许可与边界

派生自 `workbuddy-manager`（MIT），保留原 [`LICENSE`](LICENSE) 与版权声明。
使用边界同上游：只管理**本人及亲属自愿授权**的账号，禁止批量注册、账号交易、
接码、盗号、绕过风控与任何商业转售。本项目与腾讯、Trae / 字节跳动无隶属关系。
