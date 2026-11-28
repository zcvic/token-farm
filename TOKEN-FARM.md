# token-farm

**多平台 token 农场**：一套面板同时管理 **WorkBuddy（腾讯 CodeBuddy）**、
**Trae**、**Qoder**、**Loomy（讯飞星火）**、**DuMate（百度搭子）**、
**MiniMax Code**、**Discovery（Intern TokenPlan）**、**Kilo**、**Cline**、
**豆包（Doubao）**、**WPS 灵犀（Lingxi）**、**商汤小浣熊（Raccoon）**、
**华为云 CodeArts 码道（CodeArts Agent）**、**AutoClaw（智谱 / Z.ai）** 与
**Accio（阿里国际站 AI 电商工作台）** 十五类账号，统一对外提供 OpenAI 兼容接口。

本项目派生自 [`workbuddy-manager`](https://github.com/ithtelab/workbuddy-manager)
（MIT）。上游的全部能力都保留且行为不变 —— 账号池、扫码纳管、签到保活、密钥分发、
IP / 模型白名单、用量与日志。在此之上新增：

| 新增 | 位置 | 说明 |
|---|---|---|
| 平台适配层 | `server/platforms/` | 以 `Platform` 契约为边界，WorkBuddy 仍委托给 `workbuddy2api`，Trae / Loomy / DuMate / MiniMax Code / Discovery / Kilo / Cline / 豆包 / 灵犀 / 小浣熊 / CodeArts / AutoClaw / Accio 为原生实现，Qoder 用内置网关 |
| Trae 适配器 | `server/platforms/trae/` | 账号存储 / 令牌刷新 / 签到 / 额度 / 模型 / 对话（SSE → OpenAI chunk） |
| Qoder 适配器 | `server/platforms/qoder/` | 账号 / 签到 / 额度 / 模型 / 对话；协议层内置在 `_vendor/`，无需额外部署 |
| Loomy 适配器 | `server/platforms/loomy/` | 账号 / 微信扫码登录（含手机号补绑）/ 手机验证码登录 / 本机会话导入 / 签到 / 积分 / 模型 / 对话；账号服务与 OpenAI 兼容网关均为原生实现，无额外部署 |
| DuMate 适配器 | `server/platforms/dumate/` | 账号（网页 Cookie）/ 百度 Passport 扫码登录 / token 兑换与缓存 / 签到 / 积分 / 模型（桌面端 → 缓存 → 快照）/ 对话；网关 OpenAI 兼容，单区域前缀 `dumate/` |
| MiniMax Code 适配器 | `server/platforms/minimax/` | 账号（OAuth 设备码 + 一次性 refresh token）/ 双区（`minimaxcn/`、`minimaxcom/`）/ 签到 / 积分 / M Plan 窗口 / 模型 / 对话；Anthropic Messages 端点由适配器内部翻译成 OpenAI 形状 |
| Discovery 适配器 | `server/platforms/discovery/` | 账号（Intern TokenPlan API Key）/ `/v1/models` 额度探针（耗尽 → 402 冻 1 小时）/ 模型（在线目录 → 内置 8 条）/ 对话；单区域前缀 `discovery/`，无签到、无网页登录 |
| Kilo 适配器 | `server/platforms/kilo/` | 账号（官方设备授权码 → 一年期 token）/ 资料与余额（**美元**）/ 模型（免鉴权 OpenRouter schema → 自动路由兜底）/ 对话；单区域前缀 `kilo/`，无签到、无 refresh |
| Cline 适配器 | `server/platforms/cline/` | 账号（WorkOS 设备码 → 一次性 refresh token）/ token 轮换（先落盘再使用）/ 模型（免费档 + 全量合并）/ 对话；免费档强制流式并聚合，伪装限流判成 429；单区域前缀 `cline/` |
| 豆包适配器 | `server/platforms/doubao/` | 账号（网页 Cookie，抓包导入）/ 设备指纹 / 签到 / 额度（`window_limits` 汇总）/ 模型（内置快照）/ 对话（固定 SSE → OpenAI chunk）；上游无 usage，按字符 ÷1.7 估算；单区域前缀 `doubao/`，无扫码登录 |
| 灵犀适配器 | `server/platforms/lingxi/` | 账号（网页 Cookie `wps_sid`）/ 客户端设备指纹 / 三步握手（建会话 → 领 run → SSE 对话）/ run 缓存 1 小时 / 签到（每日签到任务）/ 余额（赠送批次与到期）/ 模型（`/sessions/plans` + 内置快照）/ 对话（`text` 字段 → OpenAI chunk）；单区域前缀 `lingxi/`，无扫码登录 |
| 小浣熊适配器 | `server/platforms/raccoon/` | 账号（JWT access + 一次性 refresh）/ token 轮换（先落盘再使用）/ 本机客户端 `auth.json` 导入 / 在线模型目录 / 签到（触发桌面端每日积分后核对账单）/ 积分余额与流水 / 对话（v2 流式，失败降级 v1 非流式合成 SSE）；单区域前缀 `raccoon/`，未知模型本地拒绝 |
| CodeArts 适配器 | `server/platforms/codearts/` | 账号（华为云 STS 临时 AK/SK + `security_token` + 一次性 refresh + DPoP 私钥）/ AK/SK 请求签名 / DPoP（ES256）/ 网页登录（OAuth2 PKCE 回环回调 + ticket 轮询 + 粘贴兜底）/ token 轮换（先落盘再使用）/ 按账号的三路模型目录（agent-center + 内置 + 限时福利）/ 签到（`/v1/ops` 三步）/ 对话（`text` 累计全文 → OpenAI chunk，非流式本地聚合）；单区域前缀 `codearts/` |
| AutoClaw 适配器 | `server/platforms/autoclaw/` | 账号（access JWT + 一次性 refresh + 持久化设备号）/ token 轮换（先落盘再使用）/ 双区（`autoclawcn/`、`autoclawcom/`）/ 国内短信验证码登录 + 国际 OAuth「打开授权页 + 粘回调」/ 按区域独立的在线模型目录 / 签到（任务列表判断，无任务不算失败）/ 积分钱包与流水 / 对话（OpenAI 兼容 SSE，空流报错）；`810000` 只冷却单模型 |
| Accio 适配器 | `server/platforms/accio/` | 账号（OAuth 2.0 + PKCE(S256) 回环登录，access + refresh + 秒级 `expiresAt` 归一化）/ 双区（`acciocn/`、`acciocom/`，仅登录页域名不同）/ 手工导入（含 `ac_token` Cookie 解包）/ 在线模型目录（`/api/llm/config/v2`，倍率拼进展示名）/ 积分三桶（每日 / 每月 / 推荐，`/api/entitlement/*`）/ 对话（Gemini 风格帧 → OpenAI chunk，工具参数跨帧合并）；**无签到端点**，402 冷至次日 24:00、429 冷 1 小时 |
| 网关分流 | `server/routers/gateway.py` | 模型名带 `traecn/` / `traecom/` / `qodercn/` / `qodercom/` / `loomy/` / `dumate/` / `minimaxcn/` / `minimaxcom/` / `discovery/` / `kilo/` / `cline/` / `doubao/` / `lingxi/` / `raccoon/` / `codearts/` / `autoclawcn/` / `autoclawcom/` / `acciocn/` / `acciocom/` 前缀走对应平台（旧式 `trae/` / `qoder/` / `minimax/` / `autoclaw/` / `accio/` 继续兼容），其余保持原样走 WorkBuddy |
| 多平台接口 | `server/routers/platforms.py` | `/api/platforms/*`：账号、导入、刷新、签到、额度、模型 |
| 多平台页面 | `web/app/(main)/accounts/page.tsx`、`models/page.tsx` | 顶部平台下拉切换；账号 / 模型页随平台更换数据源，分版本的平台（Trae / Qoder / MiniMax Code）再按国内版 / 国际版过滤，统计与日志按平台过滤 |
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
│  ├─ sync-upstream.sh  # 对照 Trae 协议参考源（Linux/macOS）
│  ├─ sync-upstream.ps1 # 对照 Trae 协议参考源（Windows）
│  ├─ trae-ref.txt      # Trae 参考源里已人工核对过的提交（--refs 的基线）
│  ├─ qoder-ref.txt     # Qoder 源码副本的基线与本地补丁
│  ├─ loomy-ref.txt     # Loomy 参考源的基线、取用范围与「有意不跟」
│  ├─ dumate-ref.txt    # DuMate 参考源的端点、形状与「有意不跟」
│  ├─ minimax-ref.txt   # MiniMax Code 参考源的 OAuth / 对话 / 签名事实
│  ├─ discovery-ref.txt # Discovery 参考源的 Key 池 / 额度探针 / 「有意不跟」
│  ├─ kilo-ref.txt      # Kilo 官方客户端的设备码 / 余额 / 目录事实
│  ├─ cline-ref.txt     # Cline 参考源的 WorkOS 设备码 / 强制流式 / 伪装限流
│  ├─ doubao-ref.txt    # 豆包参考源的 Cookie / SSE 事件 / 签到 / 额度事实
│  ├─ lingxi-ref.txt    # 灵犀参考源的三步握手 / SSE 翻译 / 目录 / 签到事实
│  ├─ raccoon-ref.txt   # 小浣熊参考源的 JWT 轮换 / 目录 / 对话 / 积分事实
│  ├─ codearts-ref.txt  # CodeArts 参考源的签名 / DPoP / 目录 / 对话 / 签到事实
│  ├─ autoclaw-ref.txt  # AutoClaw 参考源的双区域名 / 签名 / 目录 / 对话 / 签到事实
│  ├─ accio-ref.txt     # Accio 参考源的双区登录页 / OAuth PKCE / 目录 / 积分 / 对话事实
│  ├─ install.sh        # 上游原版：装依赖 + systemd（Linux 服务器）
│  └─ windows-native/   # 上游原版：原生 Windows（无需 Docker）
├─ docs/                # 用户文档与维护者手册
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
`TF_UPSTREAM_BIND`（上游端口绑哪个宿主地址）、
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
3. **Trae**：切到 Trae 后先用右上角的版本开关选「国内版 / 国际版」，再点
   「网页登录」（本机已装对应版本客户端时最省事）、「从本机导入」（管理端与
   IDE 在同一台机器时）或手工粘贴
   `{"uid": "...", "token": "...", "refreshToken": "..."}`；手工粘贴时没写
   `realm` 的账号会归入当前版本。国际版网页登录会优先复用本机 Trae
   国际版客户端的设备身份；没装客户端时面板会自动生成一套并随账号保存，
   不再要求先安装客户端；
4. 下游把 base_url 指到 `http://<服务器>:7864/v1`，模型名写 `traecn/<模型名>` 或
   `traecom/<模型名>` 就调 Trae（旧式 `trae/<模型名>` 仍兼容），不写平台前缀就调 WorkBuddy。

### 备份与恢复（手动）

**只有四样东西丢了就找不回来**，其余（镜像、代码、前端产物）都能重建：

| 路径 | 里面是什么 | 丢了会怎样 |
|---|---|---|
| `data/` | `manager.db`（密钥的**哈希**、配额 / 白名单 / IP 规则 / 用量日志 / 设置）、`users.json`（管理员账号 + 会话签名密钥）、`initial-admin-password.txt`（首启明文，第一次启动才写）、`platforms/trae/accounts/*.json`（Trae 令牌与 refreshToken）、`platforms/trae/device.json`（国际版设备私钥）、`upstream-scripts/` | 已发出去的密钥全部失效、管理员要重建、Trae 账号要逐个重新导入 |
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
docker compose stop token-farm

sudo tar czf ~/token-farm-backup/data-$TS.tar.gz     -C .             data
sudo tar czf ~/token-farm-backup/upstream-$TS.tar.gz -C ../workbuddy2api config.json auths
sudo cp .env ~/token-farm-backup/env-$TS
sudo chmod 600 ~/token-farm-backup/*-$TS*

docker compose start token-farm      # 备份完立刻起回来
```

`sudo` 是因为 `data/` 属主是容器 uid 10001（`chown -R 10001:10001 ./data` 那一步）。

#### Windows（Docker Desktop）

```powershell
$ts  = Get-Date -Format 'yyyyMMdd-HHmmss'
$dst = "$env:USERPROFILE\token-farm-backup"
New-Item -ItemType Directory -Force $dst | Out-Null

docker compose stop token-farm

Compress-Archive -Path .\data -DestinationPath "$dst\data-$ts.zip" -Force
Compress-Archive -Path ..\workbuddy2api\config.json, ..\workbuddy2api\auths `
                 -DestinationPath "$dst\upstream-$ts.zip" -Force
Copy-Item .env "$dst\env-$ts"

docker compose start token-farm
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

（没装 `sqlite3` CLI 就 `docker compose exec -T token-farm python -c
"import sqlite3;print(sqlite3.connect('/app/data/manager.db').execute('select count(*) from api_keys').fetchone())"`。）

`quick_check` 回 `ok`、`api_keys` 计数和你发出的密钥数量对得上，这份备份才是可用的。
恢复演练请找一台空机器做，别在唯一的部署上试。

#### `.env` 里的 `WB2API_BASE`

`WB2API_BASE`。如果写成 `http://127.0.0.1:7863`，那是「管理端跑在宿主上」的
形态；管理端在容器里时，`127.0.0.1` 是容器自己。`deploy.sh` 检测到这种回环值会
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
- 发布包产物名是 `token-farm-vX.Y.Z.tar.gz`，容器/镜像/compose 服务/systemd unit
  也统一叫 `token-farm`，安装目录是 `/opt/token-farm`。这套命名由
  `.github/workflows/release.yml`、`deploy/install.sh`、`deploy/update.py`、
  `deploy/token-farm.service`、`docker-compose.yml` 共同约定；改动时几处必须同步，
  否则「一键更新」会找不到包。
- 想彻底关掉更新：设 `WB_MANAGER_REPO=none/none`，按钮会一直报「找不到发布」。

`WB_SYNC_UPSTREAM=1`（发布包含上游源码变化时一并更新 `../workbuddy2api`）
对本仓库的 Trae 代码没有影响，可以保持默认。

---

## 三、参考源与能力同步

本仓库的第三方参考源**都只读、都不合并**：

| 参考源 | 指向 | 关系 | 怎么用 |
|---|---|---|---|
| `upstream` | [workbuddy-manager](https://github.com/ithtelab/workbuddy-manager) | 面板的派生来源，**无共享历史** | `git fetch` 后对照提交，人工挑需要的改动 |
| `trae-ref` | [trae2api-hub](https://github.com/shuishuipingan/trae2api-hub)（MIT） | Trae 协议事实来源，**无共享历史** | `deploy/sync-upstream.* --refs`：只对照，永不 merge |
| `qoder-ref` | [qoder2api-hub](https://github.com/shuishuipingan/qoder2api-hub)（MIT） | **源码副本**（`server/platforms/qoder/_vendor/`） | 整目录重新 vendor，见下 |
| `loomy-ref` | [loomy2api](https://github.com/xingluoyuankong/loomy2api)（MIT） | Loomy 协议事实来源，**原生重写**、无副本 | 对照提交，改 `server/platforms/loomy/`，永不 merge |
| `dumate-ref` | [dumate2api](https://github.com/wearetheone777/dumate2api)（MIT） | DuMate 协议事实来源，**原生重写**、无副本 | 对照端点与业务码，改 `server/platforms/dumate/`，永不 merge |
| `minimax-ref` | [opencode-minimax-auth](https://www.npmjs.com/package/@magpie-community/opencode-minimax-auth)（MIT） | MiniMax Code 协议事实来源，**原生重写**、无副本 | 对照 OAuth / 签名 / 对话形状，改 `server/platforms/minimax/` |
| `discovery-ref` | [discovery2api](https://github.com/ZFXing-lite/discovery2api)（MIT） | Discovery 协议事实来源，**原生重写**、无副本 | 对照 Key 池 / 额度探针 / 目录，改 `server/platforms/discovery/` |
| `kilo-ref` | [kilocode](https://github.com/Kilo-Org/kilocode)（Apache-2.0） | Kilo **官方客户端**的协议事实来源，**原生重写**、无副本 | 对照 `packages/kilo-gateway` 的设备码 / 余额 / 目录，改 `server/platforms/kilo/` |
| `cline-ref` | [cline2api](https://github.com/luawei1/cline2api)（MIT） | Cline 协议事实来源，**原生重写**、无副本 | 对照 `worker.js` 的设备码 / 伪装头 / 强制流式，改 `server/platforms/cline/` |
| `doubao-ref` | [doubao2api](https://github.com/yyydsz/doubao2api)（MIT） | 豆包协议事实来源，**原生重写**、无副本 | 对照 Cookie 鉴权 / SSE 事件 / 签到 / 额度，改 `server/platforms/doubao/` |
| `lingxi-ref` | [dsh-connect-lingxi](https://github.com/xinstart-1/dsh-connect-lingxi)（MIT）、[dsh-router-wpslingxi](https://github.com/xfyang728/dsh-router-wpslingxi)（MIT） | 灵犀桌面端私有协议事实来源，**原生重写**、无副本 | 对照三步握手 / 请求体约束 / SSE 翻译 / 目录 / 签到，改 `server/platforms/lingxi/` |
| `raccoon-ref` | [xiaohuanxiong2api](https://github.com/haoxinyu1/xiaohuanxiong2api)、[pi-provider-xhx](https://github.com/flying-dragon-ai/pi-provider-xhx)、[dsh-raccoon-connect](https://github.com/zhlcba/dsh-raccoon-connect)、[Coding2API](https://github.com/RobbsLuo/Coding2API) | 小浣熊网页 / 桌面端协议事实来源，**原生重写**、无副本 | 对照 JWT 一次性轮换 / 在线目录 / 对话降级 / 积分账单，改 `server/platforms/raccoon/` |
| `codearts-ref` | [codearts2api](https://github.com/HITZY2002/codearts2api)（MIT） | 华为云 CodeArts 码道协议事实来源，**原生重写**、无副本 | 对照 AK/SK 签名 / DPoP / OAuth2 PKCE / 三路目录 / SSE 重拼 / 签到三步，改 `server/platforms/codearts/` |
| `autoclaw-ref` | [AutoClawProxyAPI](https://github.com/VVVVVILK/AutoClawProxyAPI)（MIT）、[opencode-autoclaw-auth](https://github.com/hubo1989/opencode-autoclaw-auth)（MIT）、[AutoClaw2api](https://github.com/hj01857655/AutoClaw2api)、[autoclaw2api](https://github.com/ZFXing-lite/autoclaw2api)、[autoclaw-switch](https://github.com/apmengzi/autoclaw-switch)（MIT） | AutoClaw 双区协议事实来源，**原生重写**、无副本 | 对照双区基址 / `X-Auth-Sign` / 短信与 OAuth 登录 / 目录 / 对话 / 签到，改 `server/platforms/autoclaw/` |
| `accio-ref` | `dsh-providers-hub`（MIT，DSH 插件包） | Accio 双站协议事实来源，**原生重写**、无副本 | 对照双区登录页 / OAuth PKCE / `generateContent` 信封 / 目录倍率 / 积分三桶，改 `server/platforms/accio/` |

上游 Go 源码（workbuddy2api）已经并入本仓库的 `upstream/`，日常直接在这里改，
不再从外部仓库 fetch / merge。面板侧新能力的参考来源是
`misakano7545/workbuddy2api-panel`，同样只挑对本项目有益的提交，记录在
`deploy/workbuddy-panel-ref.txt`。

Qoder 的协议层也是第三方源码（`shuishuipingan/qoder2api-hub`，MIT），但它走的是
**源码副本**：协议层原样放在 `server/platforms/qoder/_vendor/`，由内置网关在
进程内拉起。它的升级不是 `git am` 补丁，而是整目录重新 vendor：

```bash
python server/platforms/qoder/tools/vendor_qoder.py --source /path/to/qoder2api-hub
python server/platforms/qoder/tools/vendor_qoder.py --check
```

已核对的上游基线与每批取舍记录在 `deploy/qoder-ref.txt`。唯一需要重新应用的
本地补丁是账号权重（落点见 `_vendor/README.md`），
`server/tests/test_qoder_vendor.py` 会守住它。

Loomy 的协议参考是 [`xingluoyuankong/loomy2api`](https://github.com/xingluoyuankong/loomy2api)
（MIT），但**不是源码副本**：登录签名 / RSA 加密 / 微信扫码 / 积分与签到解析
都按参考实现重写在 `server/platforms/loomy/`，没有引入它的 HTTP 服务与面板，
也不新增依赖。已核对范围是 `eed1efa`（2026-10-02）→ `cb27afd`（2026-11-14，
40 个提交）；取了什么、哪些有意不跟、实测到的上游形状都记在
`deploy/loomy-ref.txt`，用户向的说明见 [`docs/loomy.md`](docs/loomy.md)。

DuMate（百度搭子）的协议参考是 [`wearetheone777/dumate2api`](https://github.com/wearetheone777/dumate2api)，
同样是**原生重写**：网页接口换 gateway token、`dumate-svc.baidu.com` 的 OpenAI
兼容对话、签到与积分都在 `server/platforms/dumate/` 里实现，没有引入参考仓
的 HTTP 服务。端点、业务码与「有意不跟」（例如不引入参考仓的 playwright
浏览器登录器、不批量注册）记在 `deploy/dumate-ref.txt`。网页登录用纯 HTTP
的百度 Passport 扫码（服务端取码，手机确认），与手工粘贴 Cookie 并存。

MiniMax Code 的协议参考是
[`@magpie-community/opencode-minimax-auth`](https://www.npmjs.com/package/@magpie-community/opencode-minimax-auth)
（MIT）。本项目只取**协议事实**：OAuth 设备码 + PKCE、一次性 refresh token
轮换、带 `yy` / `x-signature` 的账号接口、Anthropic Messages 对话端点与
`minimax-cloud` 签到 / 积分。请求 / 响应 / SSE 的双向翻译在
`server/platforms/minimax/translate.py`，不引入参考包的 OpenCode 插件形态，
也不复制它的运行时。已核对的范围记在 `deploy/minimax-ref.txt`。

Discovery、Kilo、Cline 三个平台都是「OpenAI 兼容直连 + 账号池」，接入时没有
引入任何第三方运行时，只按协议事实原生重写：

- **Discovery**（[`ZFXing-lite/discovery2api`](https://github.com/ZFXing-lite/discovery2api)，
  MIT）：只取 Key 池的形状、`/v1/models` 额度探针与目录来源。参考仓的
  `force-model` 静默改写**不采纳**——把客户端要的模型换掉，报错完全指不回
  适配器。已核对范围见 `deploy/discovery-ref.txt`。
- **Kilo**：参考源是**官方客户端**
  [`Kilo-Org/kilocode`](https://github.com/Kilo-Org/kilocode) 的
  `packages/kilo-gateway`（Apache-2.0），不是第三方 2api 项目。设备码登录、
  资料、余额（美元）、OpenRouter schema 目录都按协议事实重写；
  `deanxv/kilo2api` 的 cookie 抓取方案因源码不公开**不作参考源**。已核对范围见
  `deploy/kilo-ref.txt`。
- **Cline**（[`luawei1/cline2api`](https://github.com/luawei1/cline2api)，MIT）：
  取 WorkOS 设备码 → `/auth/register` 换 refresh token 的两段式登录、伪装客户端
  头、免费档强制流式与「限流伪装成 200 正常帧」的识别。Cloudflare Worker 运行时
  与 KV 存储一律不引入。已核对范围见 `deploy/cline-ref.txt`。

三者都**没有**桌面端导入：不读也不回写本机客户端，`import_from_desktop` 不存在。

豆包（[`yyydsz/doubao2api`](https://github.com/yyydsz/doubao2api)，MIT）也是
**原生重写**：凭证是网页 Cookie（`sessionid` / `sid_tt` / `ttwid` 等），对话走
`POST www.doubao.com/chat/completion`（固定 SSE），签到与额度走两个 `alice/*`
网页接口。上游不校验 `a_bogus`，所以纯 HTTP 即可；扫码登录需要 Passport 三层
签名、参考实现自己也不稳，因此**只支持抓包导入 Cookie**。上游不返回 usage，
统计按「输出字符 ÷ 1.7」估算——这是估算值，不能拿去对账。已核对范围见
`deploy/doubao-ref.txt`。

WPS 灵犀（Lingxi）没有开放平台 API，协议事实来自其**桌面客户端**自己使用的
私有 app-server 接口，核对自
[`xinstart-1/dsh-connect-lingxi`](https://github.com/xinstart-1/dsh-connect-lingxi)
（含 `docs/PROTOCOL.md`）与
[`xfyang728/dsh-router-wpslingxi`](https://github.com/xfyang728/dsh-router-wpslingxi)
（均为 MIT），同样是**原生重写**：凭证是网页 Cookie（`wps_sid`），一次对话是
「建会话 → 领 run → SSE」三步握手，会话 / run 按账号 + 设备 + 模型缓存 1 小时，
签到与余额走网页任务 / 积分接口。已核对范围见 `deploy/lingxi-ref.txt`，
用户向的说明见 [`docs/multi-platform.md`](docs/multi-platform.md) §15。

商汤小浣熊（Raccoon）同样没有开放平台 API，协议事实来自其网页 / 桌面客户端
共用的 `xiaohuanxiong.com` 后端，核对自
[`haoxinyu1/xiaohuanxiong2api`](https://github.com/haoxinyu1/xiaohuanxiong2api)、
[`flying-dragon-ai/pi-provider-xhx`](https://github.com/flying-dragon-ai/pi-provider-xhx)
与 [`zhlcba/dsh-raccoon-connect`](https://github.com/zhlcba/dsh-raccoon-connect)
（另有 [`RobbsLuo/Coding2API`](https://github.com/RobbsLuo/Coding2API) 交叉核对
网页 JWT）。本仓库为**原生重写**：凭证是 access JWT + **一次性**
refresh JWT，对话是标准 OpenAI Chat Completions，目录在线拉取，签到靠读一次
桌面端 `setting_info` 触发每日积分后核对账单。已核对范围与「有意不跟」见
`deploy/raccoon-ref.txt`，用户向的说明见
[`docs/multi-platform.md`](docs/multi-platform.md) §16。

华为云 CodeArts 码道（CodeArts Agent）没有开放平台 API，协议事实来自官方
CodeArts Agent 插件的 `resources/app/extensions/vscode-codebot` 与真机抓包，
核对自 [`HITZY2002/codearts2api`](https://github.com/HITZY2002/codearts2api)
（MIT）。本仓库为**原生重写**：凭证是约 1 小时的华为云 STS 临时 AK/SK
（每个请求再做一次 `SDK-HMAC-SHA256` 签名），登录是 OAuth2 PKCE + DPoP，
`refresh_token` 与 DPoP 私钥绑定且一次性轮换；模型目录按账号分三路合并
（agent-center / 内置 / 限时福利），签到走 `/v1/ops` 三步。已核对范围与
「有意不跟」见 `deploy/codearts-ref.txt`，用户向的说明见
[`docs/multi-platform.md`](docs/multi-platform.md) §17。

AutoClaw（智谱 / Z.ai，中文名「澳龙」）同样没有开放平台 API，协议事实来自
社区实现与真机抓包，核对自
[`VVVVVILK/AutoClawProxyAPI`](https://github.com/VVVVVILK/AutoClawProxyAPI)、
[`hubo1989/opencode-autoclaw-auth`](https://github.com/hubo1989/opencode-autoclaw-auth)、
[`hj01857655/AutoClaw2api`](https://github.com/hj01857655/AutoClaw2api)、
[`ZFXing-lite/autoclaw2api`](https://github.com/ZFXing-lite/autoclaw2api) 与
[`apmengzi/autoclaw-switch`](https://github.com/apmengzi/autoclaw-switch)。本仓库为
**原生重写**：国内版与国际版是两套独立账号体系（域名、客户端版本、登录方式、
模型名单都不同，token 不通用），账号是 access JWT + **一次性** refresh JWT，
国内用手机号 + 短信验证码登录，国际用 OAuth「打开授权页 + 粘回调」，模型目录
按区域独立在线拉取，`810000` 只冷却「账号 + 模型」不冻整号。已核对范围与
「有意不跟」见 `deploy/autoclaw-ref.txt`，用户向的说明见
[`docs/multi-platform.md`](docs/multi-platform.md) §18。

Accio（阿里国际站 AI 电商工作台）没有开放平台 API，协议事实来自网页端与桌面端
的真实请求，核对自 `dsh-providers-hub`（MIT）。本仓库为**原生重写**：国内与国际
两站只有登录页域名不同（`www.accio-ai.com` / `zh.accio.com`），网关与推理面是同一
个（`phoenix-gw.alibaba.com`），区域由服务端按账号下发，登录前无法自动判断，所以
登录时必须先选区域；登录是 OAuth 2.0 + PKCE(S256) 回环回调，`client_id` 是公开
客户端、无 secret；对话是 Gemini 风格信封，`token` 必须放在**请求体**里（放
`Authorization` 头恒回 402），`system_instruction` 是字符串、工具 schema 字符串化，
工具参数分片到达需要跨帧合并。积分只有每日 / 每月 / 推荐三个只读桶，**没有签到
端点**，所以不提供签到按钮。已核对范围与「有意不跟」见 `deploy/accio-ref.txt`，
用户向的说明见 [`docs/multi-platform.md`](docs/multi-platform.md) §19。

所有参考源都没有共享提交历史，`git merge` / `git pull` 不成立 ——
只能挑单个提交，导出成补丁再应用。流程与注意事项见
[`docs/upstream-sync.md`](docs/upstream-sync.md)。

自己的提交推到 `origin`。`main` 是唯一长期分支，日常直接提交；发版靠打 tag
（推 tag 即触发 Release 构建与镜像发布）。

### 3.1 从参考仓挑能力（手工，不整体合并）

**看有什么新东西（只取提交，不动代码）**

```bash
git fetch upstream
git log --oneline --no-merges HEAD..upstream/main    # 参考仓的新提交
git diff --stat HEAD...upstream/main                 # 涉及哪些文件
```

**把需要的提交应用进来**

```bash
# 在参考仓的临时 clone 里，把目标提交导出成补丁
git format-patch -1 <commit> -o /tmp/sync

# 面板改动直接落仓库根目录
git am -3 /tmp/sync/0001-*.patch

# 上游 Go 源码改动：-p1 去掉 a/ b/ 前缀，--directory 落到 upstream/
git am -3 -p1 --directory=upstream /tmp/sync/0001-*.patch
```

- 一次只挑需要的提交，不整仓 merge —— 参考仓有自己的内嵌面板、配置结构与
  账号池语义，整仓合并会冲掉本项目的改动。
- 有冲突：按普通 git 冲突解决，`git add` 后 `git am --continue`；放弃用
  `git am --abort`。
- 多个提交按顺序 `git am`，顺序不能乱（后面的提交可能依赖前面的）。
- **PowerShell 不能把补丁直接 `|` 管道给 `git am`**：先把补丁落成文件再传路径。
- 同步完在提交信息里写清来源仓库与提交号，并把来源补进
  `deploy/workbuddy-panel-ref.txt`。

### 3.2 trae-ref（trae2api-hub）：只看，不合并

Trae 这一层不是 fork，而是照着参考实现的协议要点重写的原生实现（端点 / 请求头 /
SSE 事件 / 设备指纹 / 令牌交换）。所以对它**只能人工跟进协议变化**，把它的提交
合进来没有意义（会带进它整套看板与任务编排）。

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

### 手工应用补丁时冲突主要会出现在哪

本仓库相对参考仓改过的地方集中在少数几处，冲突时**保留本仓库语义**，再把参考仓
的改动并进去：

| 文件 | 本仓库的改动 |
|---|---|
| `server/{keysvc,main}.py`、`server/routers/gateway.py` | 注册平台路由、`trae/` 分流 |
| `web/lib/{api,types}.ts` | `platformApi` 与平台类型 |
| `web/lib/i18n/locales/*.json` | `nav.platforms` + `platforms.*`（五份必须键集一致） |
| `web/components/common/layout/ManagementBar.tsx`、`web/lib/command-palette.ts` | 底栏 / ⌘K 多平台入口 |
| `server/tests/test_*.py`、`web/lib/*.test.mjs` | 导航与计数守卫的期望值 |
| `README.md` / `README.en.md` | 顶部一行 token-farm 说明（两份必须同增同删） |

**已经统一**的命名：容器名 / 镜像名 / compose 服务名 / systemd unit / 发布资产名 /
一键更新链路里的包名 / 安装目录，全部叫 `token-farm`（安装目录是 `/opt/token-farm`）。
再改时注意 `.github/workflows/release.yml`、`deploy/install.sh`、`deploy/update.py`、
`deploy/token-farm.service`、`docker-compose.yml` 和 `windows/update.ps1` 必须一起改。

具体到「哪一层叫什么」：

| 位置 | 现在叫 |
|---|---|
| 仓库 / 文档 / 部署脚本 | `token-farm`（本文、`README.md` 顶部、`deploy/deploy.*`、`deploy/sync-upstream.*`） |
| 浏览器标题、登录页、底栏用户卡片、设置「关于」、API title | `token-farm` |
| `web/package.json` / `package-lock.json` / PWA 清单 | `token-farm-web` / `token-farm` |
| Windows 安装包 / 控制窗口 / 安装目录 / 开始菜单 / 卸载项 | `TokenFarmSetup.exe` / `TokenFarm.exe` / `%USERPROFILE%\token-farm` / `token-farm` |
| 容器、镜像、compose 服务、systemd unit、发布资产名、更新器里的包名 | `token-farm` |
| 安装目录 | `/opt/token-farm` |

仍与上游保持一致、**不跟着改**的：服务账号 `workbuddy`（uid 10001）、上游目录
`/opt/workbuddy2api`、环境变量前缀 `WB_*` —— 它们要和管理端共享上游的 `auths/`
与 `config.json` 写权限，改了会互相写不进去。

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

- **Trae 国际站（global realm）的令牌刷新**：已用真实 SG 账号跑通
  （2026-11-01）。`DeviceProof` 用 ECDSA-P256，私钥优先取本机
  `iCubeAuthInfo://icube-dc`；签名原文是秒级时间戳 + `\n` 连接，路径
  `/trae/api/v3/oauth/ExchangeToken`。面板与 IDE 同机时什么都不用填；服务器上
  没有客户端时，网页登录会自动生成并随账号保存 `deviceKeyPEM`、`deviceId`、
  `machineId` 和 `devicePublicKeyPEM`。手工导入的账号若要刷新，这四项仍需
  来自同一台设备，否则上游会以设备不匹配拒绝刷新。
- **Trae 国际版网页登录**已开放：授权页 `https://www.trae.ai/authorization`
  与国内版不同，走 PKCE，回调拿 `authCodeInfo` 后由本机 `code_verifier` 调
  `exchange_token_by_auth_code`（带 `DeviceInfo`，不带 `DeviceProof`）。
  没有本机客户端也能登录：面板会自己生成一套稳定的设备身份并保存在
  `data/platforms/trae/device.json`，刷新时用它签 `DeviceProof`。
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
