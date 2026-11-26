# Qoder 内置网关（vendor 自 qoder2api-hub）

这个目录是 [qoder2api-hub](https://github.com/shuishuipingan/qoder2api-hub)
的**原样副本**，用来在 token-farm 进程内跑 Qoder 国内版 / 国际版的协议层，
不再需要单独部署第二个项目。

| 项 | 值 |
| --- | --- |
| 上游版本 | `1.10.3` |
| 上游 commit | `36fa929592684c0676e3167b812a158edbf036f2` |
| 许可 | MIT（见 `LICENSE`） |
| 第三方依赖 | 无必装依赖（Pillow 可选：缺失时图片原样透传，不做压缩） |

## 收录的文件

| 文件 | 作用 |
| --- | --- |
| `qoder_proxy.py` | 网关主体：请求体构造、COSY 签名调用、SSE 管线、OpenAI / Anthropic 出口 |
| `qoder_accounts.py` | 账号池：凭据刷新、设备登录、PAT 导入、冷却与熔断、VPC 端点 |
| `qoder_sign.py` | COSY 签名与本地凭据解密 |
| `qoder_fingerprint.py` | 设备指纹派生 |
| `qoder_catalog.py` | 双区模型目录（配合两个 `qoder_catalog_*.json`） |
| `qoder_tasks.py` | 签到 / 保活 |
| `qoder_settings.py` | 账号目录下的 `settings.json`（护栏、面板会话、API Key） |
| `qoder_pricing.py`、`qoder_session_cache.py`、`qoder_activity.py` | 计费 / 会话缓存 / 活动日志 |
| `qoder_agents.py`、`qoder_anthropic.py`、`qoder_webtools.py` | Agent 配置、Anthropic 桥、联网工具（本地 / 官方 / 外部三种后端） |
| `qoder_anthropic_web.py` | Anthropic Messages 原生托管 web 工具（`web_search_20250305` / `web_fetch_20250910`）在 `/v1/messages` 上的服务端执行 |
| `qoder_identity_guard.py` | 出站身份句守卫：中和第三方 Agent 品牌身份句，规避上游 10605 硬拒绝（`QD_IDENTITY_GUARD`，默认关闭，跟随上游） |
| `qoder_web_response.py` | 联网工具的请求级证据与 Responses 事件回放（引用、工具调用） |
| `qoder_official_web.py` | 官方搜索 / 抓取后端：用当前账号自己的 CN / INTL 登录态签名调用 |
| `qoder_external_web.py` | 外部搜索后端：SearXNG / Tavily / Brave / Serper / 自定义，配置在 `accounts/settings.json` |
| `qoder_images.py` | 内联图片预处理（Pillow 可选；缺失时原样透传） |
| `qoder_cache.py` | 面板统计结果的有界缓存 |
| `qoder_routing.py` | 分层路由 / 账号作用域（配置在 `accounts/admin/routing.json`） |
| `qoder_admin.py` | 上游管理接口的分组 / 总览视图（内置网关的 `/settings/admin*` 用） |
| `qoder_keepalive.py` | 会话缓存保温（`QD_CACHE_KEEPALIVE`，默认关闭） |
| `qoder_logpaths.py` | 日志 / usage 路径解析与旧目录迁移 |
| `qoder_scheduler.py`、`qoder_updater.py` | 上游的定时调度与自更新（token-farm 不使用，仅为满足导入） |
| `baseprompt.json` | 官方请求体骨架 |
| `qoder_catalog_cn.json`、`qoder_catalog_intl.json` | 双区模型目录快照 |

## 与上游的差异

**导入语句**：上游把仓库目录当作脚本目录（在 `sys.path` 上），所以写成
`import qoder_accounts`；这里改成包内相对导入：

```python
import qoder_accounts          ->  from . import qoder_accounts
from qoder_sign import x, y    ->  from .qoder_sign import x, y
```

`server/platforms/qoder/tools/vendor_qoder.py` 负责这件事，可以重复执行。

**账号权重（本地补丁）**：上游没有账号权重概念，token-farm 需要与
Trae / WorkBuddy 同一口径的按账号权重（`0` = 未设置，等效 `1.0`）：

- `qoder_accounts.py`：`WEIGHT_MIN` / `WEIGHT_MAX`、`normalize_weight()`、
  `selection_weight()`、`Account.weight`（写进账号 JSON 与 `public()`），
  加权选号在 `_weighted_pick()` / `_rotate_pick()`，写入入口是
  `AccountPool.set_weight()`；没有账号设置权重时仍走原来的纯轮转。
- `qoder_proxy.py`：`POST /accounts/weight` 路由。

重新 vendor 会覆盖这两处，必须按上面的落点重新应用；
`server/tests/test_qoder_vendor.py` 用离线断言守住它。

**明文导出（本地补丁）**：上游 1.10.2 起 `/accounts/export?secrets=1` 一律
403，只允许命令行 `--backup-accounts` 落一份可恢复备份。token-farm 管理端的
「导出明文凭据」需要 HTTP 导出，所以这里收紧成：**只有同时持有网关 API Key
的调用方**（内置网关由本进程拉起，Key 每进程随机、只在内存里）才允许
`secrets=1`；只拿 `X-Panel-Token` 的浏览器面板仍然只能读到元数据。落点在
`qoder_proxy.py` 的 `/accounts/export` 分支（多一行 `import hmac`）。

除这三类差异外，业务逻辑、常量、错误文案与上游逐字节一致，方便对上游的
修复做 diff。

## 升级

```bash
python server/platforms/qoder/tools/vendor_qoder.py --source /path/to/qoder2api-hub
```

脚本会重新拷贝文件、改写导入、更新本文件里的版本与 commit，并提示需要一起
核对的调用点（`server/platforms/qoder/embedded.py` 里的初始化参数）。

## 不收录的文件

`dashboard.html`、`i18n/`、启动 / 停止脚本、`_test_*.py` 与诊断脚本都没有收录：
token-farm 有自己的管理端，内置网关只作为协议层使用（`/health`、`/v1/*`、
`/accounts*` 由 `embedded.py` 在回环地址上拉起）。
