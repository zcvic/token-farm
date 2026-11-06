# Qoder 内置网关（vendor 自 qoder2api-hub）

这个目录是 [qoder2api-hub](https://github.com/shuishuipingan/qoder2api-hub)
的**原样副本**，用来在 token-farm 进程内跑 Qoder 国内版 / 国际版的协议层，
不再需要单独部署第二个项目。

| 项 | 值 |
| --- | --- |
| 上游版本 | `1.7.12` |
| 上游 commit | `a59f89e60a6bc34b92d1ef08467618e1b78f136d` |
| 许可 | MIT（见 `LICENSE`） |
| 第三方依赖 | 无（纯标准库） |

## 收录的文件

| 文件 | 作用 |
| --- | --- |
| `qoder_proxy.py` | 网关主体：请求体构造、COSY 签名调用、SSE 管线、OpenAI / Anthropic 出口 |
| `qoder_accounts.py` | 账号池：凭据刷新、设备登录、PAT 导入、冷却与熔断 |
| `qoder_sign.py` | COSY 签名与本地凭据解密 |
| `qoder_fingerprint.py` | 设备指纹派生 |
| `qoder_catalog.py` | 双区模型目录（配合两个 `qoder_catalog_*.json`） |
| `qoder_tasks.py` | 签到 / 保活 |
| `qoder_settings.py` | 账号目录下的 `settings.json`（护栏、面板会话、API Key） |
| `qoder_pricing.py`、`qoder_session_cache.py`、`qoder_activity.py` | 计费 / 会话缓存 / 活动日志 |
| `qoder_agents.py`、`qoder_anthropic.py`、`qoder_webtools.py` | Agent 配置、Anthropic 桥、联网工具 |
| `qoder_scheduler.py`、`qoder_updater.py` | 上游的定时调度与自更新（token-farm 不使用，仅为满足导入） |
| `baseprompt.json` | 官方请求体骨架 |
| `qoder_catalog_cn.json`、`qoder_catalog_intl.json` | 双区模型目录快照 |

## 与上游的差异

**只有导入语句**。上游把仓库目录当作脚本目录（在 `sys.path` 上），所以写成
`import qoder_accounts`；这里改成包内相对导入：

```python
import qoder_accounts          ->  from . import qoder_accounts
from qoder_sign import x, y    ->  from .qoder_sign import x, y
```

`server/platforms/qoder/tools/vendor_qoder.py` 负责这件事，可以重复执行。
业务逻辑、常量、错误文案与上游逐字节一致，方便对上游的修复做 diff。

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
