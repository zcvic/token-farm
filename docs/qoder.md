# Qoder 接入（内置网关）

token-farm 里的 **Qoder** 平台同时支持国内版（`qoder.com.cn`）与国际版
（`qoder.com`）。协议层来自
[`qoder2api-hub`](https://github.com/shuishuipingan/qoder2api-hub)（MIT），
以**源码副本**的形式内置在 `server/platforms/qoder/_vendor/`，由
`server/platforms/qoder/embedded.py` 在进程内拉起。

**不需要再部署第二个项目。** 安装 token-farm 就能用 Qoder：没有额外的容器、
端口或环境变量要配。

```
client ──qodercn|qodercom/<model>──> token-farm 网关
                              │ 鉴权 / 限流 / 记账（与其他平台共用）
                              └──> QoderAdapter
                                     └──> 内置网关（127.0.0.1:<随机端口>，仅本进程可达）
                                            ├── 国内版账号池（qoder.com.cn）
                                            ├── 国际版账号池（qoder.com）
                                            └── COSY 签名 / 设备授权 / 签到 / 模型目录
```

内置网关只在回环地址上监听一个**随机空闲端口**，不对局域网暴露。API Key 每个
进程随机生成，只存在内存里；面板口令首次启动随机生成、只落 PBKDF2 哈希（上游
默认的 `admin` 不可用）。两者都不下发浏览器。

---

## 1. 数据存放

| 路径 | 内容 |
| --- | --- |
| `<WB_DATA_DIR>/platforms/qoder/accounts/` | 账号凭据（每个账号一个 JSON）+ `settings.json` + 机器身份 |
| `<WB_DATA_DIR>/platforms/qoder/usage/` | 用量日志（`usage.jsonl`）与汇总 |

`WB_DATA_DIR` 默认是仓库下的 `data/`（容器里是 `/app/data`）。这个目录里是
**明文凭据**，备份与权限按其他平台账号同等对待。

Qoder 的账号凭据**不会**经过管理端接口下发：账号列表里的 `token_preview` 只是
一句「托管于 Qoder 内置网关（tokenFamily）」，没有任何明文。

### 从旧的独立部署迁移

如果以前把 `qoder2api-hub` 单独部署过，把它的 `accounts/` 目录整体复制过来即可
（账号 JSON、`settings.json`、`machine_identity.json` 都会被识别）：

```bash
# 旧的 qoder2api-hub 部署在别处的话
cp -a /path/to/qoder2api-hub/accounts/. /path/to/token-farm/data/platforms/qoder/accounts/
```

复制后重启 token-farm，账号页就能看到原来的账号。

---

## 2. 配置

只有三个可选环境变量，都有合理默认值：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `QODER_TIMEOUT` | `300` | 推理读超时（秒）。长回答 / 推理模型需要更久 |
| `QODER_MANAGE_TIMEOUT` | `30` | 账号管理接口超时（秒）。刷新凭据、设备登录轮询仍要打上游 |
| `QD_CN_VPC_ENDPOINT` | 空 | 国内版企业 VPC 专属域：填实例名（如 `acme`）或 `acme.vpc.qoder.com.cn` / `acme-openapi.vpc.qoder.com.cn` / `acme-gateway.vpc.qoder.com.cn`；留空走公网 `qoder.com.cn`。配置无效时内置网关启动即报错（不会带着错域名打上游） |

`docker-compose.yml` 已透传这三个变量；不写就用默认值。企业 VPC 的**按账号**
覆盖（同一网关混用多个实例与公网账号）在账号 JSON 的 `vpcEndpoint` 字段里，
token-farm 的账号页暂不提供编辑入口，需要时按上游文档改账号文件。

---

## 3. 账号管理

### 网页登录（推荐）

账号页 → 选择 Qoder → 「网页登录」，选择国内版或国际版：

1. 生成设备授权链接并在浏览器打开；
2. 轮询授权状态，完成后账号自动加入。

设备授权没有本机回环回调，所以 Qoder 的登录弹窗**不显示**「粘贴回调地址」
输入框，也不显示「面板与浏览器不同机」的警告。登录窗口约 10 分钟有效。

### 从本机导入

扫描 **token-farm 所在机器**上已登录的 Qoder 客户端（桌面 App + CLI），一次
导入双区全部有效凭据。客户端不在同一台机器时没有可导入的内容。

### 手工 / PAT 导入

支持两种形状：

```json
{"uid": "u-...", "token": "pt-...", "realm": "cn"}
```

```json
{"uid": "u-...", "accessToken": "...", "refreshToken": "...", "realm": "intl"}
```

`pt-` 开头的令牌走 PAT 导入；其余走账号文档导入。没写 `realm` 时归入国内版。

### 其他操作

| 操作 | 说明 |
| --- | --- |
| 刷新令牌 | 换 token 并顺带强刷一次余额快照；被停用 / 没有凭据的账号会被跳过，界面显示明确原因 |
| 启用 / 停用 | 停用只影响推理调度，不删除凭据 |
| 每日签到 | 今日已签 / 该账号不支持时返回「已跳过」，不算失败 |
| 积分 / 额度 | 平时读已登记的 `credits` 快照；点「刷新」时查一次实时余额 |

**能力差异**：Qoder 有账号权重（与 Trae / WorkBuddy 同一口径，`0` = 恢复默认
1.0，只影响选号分摊），但**没有强制冷却 API**：账号页会隐藏冷却按钮与「清除
冷却」。这不是静默失败——后端方法本身会返回明确的「不支持」。

### 自动签到与令牌续期

两条都**默认开启**，不用逐个去点：

| 动作 | 由谁执行 | 说明 |
| --- | --- | --- |
| 每日签到 | 账号页「计划任务 → 每日任务 → 平台签到」 | 默认 9 点，与 Trae 一起跑；已签的号返回「今日已签」，不算失败 |
| 令牌续期 | 进程内巡检（每小时）+「令牌刷新计划」 | 剩余寿命不足 6 小时才刷；「每 N 天全量刷一次」默认开启，不看剩余寿命 |

三点值得知道：

- **内置网关自带的调度器没有启动**。它也会签到 / 保活，和 token-farm 自己的
  定时任务同时跑会重复打上游；签到与续期统一走上面这两条。
- **续期阈值是 6 小时而不是 WorkBuddy 的 3 天**：Qoder 的 jobToken 只有约
  24 小时寿命，deviceToken 约 30 天；按天判会让刚签发的令牌立刻落进窗口，
  等于每小时换一遍 token。
- **没有 Qoder 账号时不会启动内置网关**：定时任务先看
  `data/platforms/qoder/accounts/` 里有没有账号文件，没有就跳过，不会为了
  一次巡检多跑一个回环端口与后台线程。

国区账号的 `9999-12-31`（永不过期）占位不会被当成「已过期」；「刷新」按钮
仍然是立即续期并强刷余额的入口。

---

## 4. 模型与双区

对外模型名**按区域**加前缀：

| 前缀 | 区域 |
| --- | --- |
| `qodercn/<官方模型名>` | 国内版 |
| `qodercom/<官方模型名>` | 国际版 |
| `qoder/<官方模型名>` | 旧式兼容写法，不带区域提示 |

网关转发时剥掉前缀，内置网关看到的是官方模型名。`/v1/models` **按区域各列一条**：

- 双区同名模型 → `qodercn/Qwen3.8-Max` 与 `qodercom/Qwen3.8-Max` 两条；
- 只在一个区提供的模型 → 只列它所属的那一条。

条目仍带 `realm` 与单元素 `realms` 数组，前端据此按当前界面版本过滤。旧式
`qoder/<模型>` 继续可调用，行为与改造前一致（由内置网关自动选区）。

需要显式指定区域时，两种写法等价：

| 写法 | 含义 |
| --- | --- |
| `qodercn/Qwen3.8-Max` | 强制国内版 |
| `qodercom/Qwen3.8-Max` | 强制国际版 |
| `qoder/cn:Qwen3.8-Max` | 同 `qodercn/`，旧写法 |
| `qoder/intl:Qwen3.8-Max` | 同 `qodercom/`，旧写法 |
| `qoder/Qwen3.8-Max` | 交给内置网关自动选区 |

密钥绑定的版本也作为提示，但**只对显式绑定国际版（`global`）的密钥生效**：
国际版密钥调用双区模型时走国际版；默认 `cn` 的密钥不强制区域，否则国际版视图里
的双区模型会被悄悄拉回国内版。区域前缀的优先级最高——客户端明确选了区，就不再
被密钥版本或目录推断改掉。

模型目录缓存 5 分钟；暂时取不到时沿用上一次成功的结果。从未成功取到过目录时，
失败结论保留 30 秒再重试（模型页的「刷新」会立即重试）。

---

## 5. 协议兼容开关

内置网关的对话管线里有一批「模型行为不合预期」时的兜底策略，上游面板能改，
此前 token-farm 没有入口。现在它们在「设置 → 上游配置」的**协议兼容**一栏里，
跟随顶部平台切换，只对 Qoder 生效：

| 开关 | 可选值 | 默认 | 作用 |
| --- | --- | --- | --- |
| 工具残片收尾 | 严格 / 宽松 | 网关默认 | 模型把工具调用写成正文残片时：严格=本轮明确报错，宽松=正常收尾 |
| Anthropic 思考块 | 关闭 / 空签名 | 网关默认 | Claude Code 等客户端要求 thinking block 时是否输出（`off` 不输出，`empty` 输出空块） |
| 结构化工具历史 | 自动 / 开启 / 关闭 | 网关默认 | 历史里的工具调用按原生结构回放；模型会复述历史调用时改回文本化 |
| 图片预处理 | 自动 / 关闭 | 网关默认 | 大图先压缩再上传（需要装 Pillow）；关闭=原样透传，用于排查图片问题 |
| 本地 Web 工具 | 开 / 关 | 关 | 由网关执行 `web_search` / `web_fetch`。**唯一由模型驱动出网的能力**，默认关闭 |

「网关默认」= 不设任何值，用网关自己的策略（与改动前完全一致）。

> **联网工具现在有三种后端**（1.9.12 起）：`local`（网关本机抓，默认）、`qoder`
> （用当前账号自己的 CN / INTL 登录态调官方搜索 / 抓取接口）、`external`
> （SearXNG / Tavily / Brave / Serper / 自定义搜索 API）。面板上的「本地 Web 工具」
> 开关只切**总开关**（`enabled`），不改后端；要换后端或填外部搜索的地址 / API Key，
> 直接改 `<WB_DATA_DIR>/platforms/qoder/accounts/settings.json`：
>
> ```json
> {
>   "web_tools": true,
>   "web_tools_backend": "external",
>   "web_tools_external_provider": "searxng",
>   "web_tools_external_profiles": {
>     "searxng": {"endpoint": "http://127.0.0.1:8888/search"}
>   }
> }
> ```
>
> 外部后端的配置写入会整包校验（协议 / 凭据 / 字段名），非法值直接拒绝而不是
> 静默落一半。官方后端只支持 CN / INTL 普通账号，VPC 账号与跨区借用凭据都会被
> 显式拒绝。

保存后**立即生效，不用重启内置网关**：这些值在每次请求时现读，面板保存会同步
刷新网关进程里的环境变量；网关启动时也会按库里的值灌一遍，所以重启后仍然是
面板上的设置。对应的环境变量（`QD_TOOL_ECHO_MODE` / `QD_ANTHROPIC_THINKING` /
`QD_STRUCTURED_TOOL_HISTORY` / `QD_IMAGE_TRANSCODE` / `QD_WEB_TOOLS`）是网关
自己的入口，面板只是替你写它们；改回「网关默认」会恢复成面板接管前的值，
不会残留上一次的覆盖。

想临时排查而不动面板时，也可以直接给 token-farm 进程设同样的环境变量：
面板里从没保存过的开关不会去动它，所以你设的值照常生效；面板里保存过的
字段以面板为准，清回「网关默认」时又回到你设的那个值。

---

## 6. 升级内置的协议层

`_vendor/` 里的文件与上游**只差导入语句**（绝对导入 → 包内相对导入），外加
一处本地补丁：**账号权重**（`qoder_accounts.py` 的 `normalize_weight` /
`selection_weight` / `Account.weight` / `AccountPool.set_weight` 与
`qoder_proxy.py` 的 `POST /accounts/weight`）。升级：

```bash
python server/platforms/qoder/tools/vendor_qoder.py --source /path/to/qoder2api-hub
python server/platforms/qoder/tools/vendor_qoder.py --check   # 无残留绝对导入、能编译
```

脚本会重新拷贝模块、改写导入、更新版本号与 commit，并校验没有残留的绝对导入。
**模块清单必须覆盖副本里的每一条包内导入**：1.9.12 起 `qoder_proxy.py` 顶层就
`from . import qoder_web_response`，清单漏一个文件不是「少个可选能力」，而是
内置网关直接 ImportError、整个 Qoder 平台起不来。`server/tests/test_qoder_vendor.py`
正向校验这一点，也校验三种联网后端各自的模块都在清单里。

**重新 vendor 会覆盖账号权重补丁**，落点见 `_vendor/README.md` 的「与上游的
差异」；同一份测试会直接守住这条补丁（离线断言，不联网）。升级后请一起核对
`server/platforms/qoder/embedded.py`：上游 `main()` 里如果新增了必要的初始化
步骤（例如 VPC 配置校验、usage 目录迁移、保温线程），这里也要补上（脚本会提醒）。

来源、版本与收录清单见 `server/platforms/qoder/_vendor/README.md`。

---

## 6. 排障

| 现象 | 排查 |
| --- | --- |
| 账号页显示「Qoder 内置网关加载失败」 | 数据目录是否可写（`<WB_DATA_DIR>/platforms/qoder`）；升级后是否漏了 `_vendor/` 里的文件 |
| 账号列表为空 | 账号池是空的；用网页登录 / 本机导入 / PAT 导入添加 |
| 网页登录一直 pending | 浏览器里是否完成了授权；超过 10 分钟需重新发起 |
| 积分显示「尚无积分快照」 | 还没查询过该账号余额；在账号页点一次「刷新」即可 |
| 签到返回「已跳过」 | 今日已签到，或该账号所在区不支持签到，不是错误 |
| 401 / 403 | 上游令牌失效；刷新一次账号，仍失败就重新登录 |
| 统计页 Qoder 用量全是 0，但日志里有记录 | 旧版本会把流式 usage 读成空 dict，导致 `usage_daily` 一行都不写。已修复；升级后**新请求**即正常，历史记录可用「设置 → 修复统计」按日志回填 |
| 日志「账号」列显示 `qoder:cn` / `qoder:intl` | 旧版本把区域标签当成了账号。已改为留空，由 `services/qoderaccountlog` 从 `data/platforms/qoder/usage/usage.jsonl` 回填真实 uid；刚打完的请求会晚几秒显示 |
| 积分一直是 0，密钥的积分配额不减 | 旧版本只认单数 `credit`，而上游给的是复数 `credits`。已修复；历史积分无法补（上游响应里已经没有了） |

内置网关的日志走 token-farm 的 logging（`qoder` logger），访问日志在 debug 级别。

---

## 7. 测试

适配器与映射的测试全部离线：大部分用注入的假网关客户端，最后一组会真的拉起
内置网关（回环端口 + 临时数据目录），但不发任何上游请求。

```bash
python -m unittest server.tests.test_platform_qoder_adapter
```

覆盖：账号脱敏、realm / 冷却映射、模型前缀与 `realms`、模型去重与缓存回退、
登录状态翻译、签到结果映射、刷新令牌时顺带查询余额、非流式 / 流式对话透传与
usage 提取、区域前缀解析、「不支持权重 / 冷却 / 回调」的显式拒绝，以及内置网关
的启动位置、懒加载、重启恢复、启动失败清理与后台刷新线程的停止。
