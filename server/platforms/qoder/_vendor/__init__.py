"""qoder2api-hub 的内置副本（vendor）。

上游：https://github.com/shuishuipingan/qoder2api-hub
版本：1.9.9（commit d0f4837c8f38c090f6d76baadc3da2c09f5c66c0）
许可：MIT，见同目录 LICENSE。

与上游的差异有两类：

1. **导入语句**（每次 vendor 都由 `tools/vendor_qoder.py` 自动改写）：上游按
   「脚本目录在 `sys.path` 上」写成 `import qoder_accounts` 这类绝对导入，这里
   改成包内相对导入（`from . import qoder_accounts`），使它可以作为
   `server.platforms.qoder._vendor` 子包加载，不需要污染 `sys.path`、也不会和
   同名的顶层模块串味。
2. **账号权重**（本地补丁，见 `qoder_accounts.py` 的 `normalize_weight` /
   `selection_weight` 与 `qoder_proxy.py` 的 `/accounts/weight` 路由）：与
   Trae / WorkBuddy 同一口径的按账号权重，只影响选号分摊。重新 vendor 后需要
   重新应用，`server/tests/test_qoder_vendor.py` 会守住这条补丁。

升级方式见 `README.md`。
"""

VERSION = '1.9.9'
UPSTREAM = 'https://github.com/shuishuipingan/qoder2api-hub'
UPSTREAM_COMMIT = 'd0f4837c8f38c090f6d76baadc3da2c09f5c66c0'

__all__ = ['VERSION', 'UPSTREAM', 'UPSTREAM_COMMIT']
