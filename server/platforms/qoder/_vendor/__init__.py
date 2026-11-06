"""qoder2api-hub 的内置副本（vendor）。

上游：https://github.com/shuishuipingan/qoder2api-hub
版本：1.7.12（commit a59f89e60a6bc34b92d1ef08467618e1b78f136d）
许可：MIT，见同目录 LICENSE。

与上游的**唯一**差异是导入语句：上游按「脚本目录在 `sys.path` 上」写成
`import qoder_accounts` 这类绝对导入，这里改成包内相对导入
（`from . import qoder_accounts`），使它可以作为 `server.platforms.qoder._vendor`
子包加载，不需要污染 `sys.path`、也不会和同名的顶层模块串味。

除此之外没有改任何逻辑。升级方式见 `README.md`。
"""

VERSION = '1.7.12'
UPSTREAM = 'https://github.com/shuishuipingan/qoder2api-hub'
UPSTREAM_COMMIT = 'a59f89e60a6bc34b92d1ef08467618e1b78f136d'

__all__ = ['VERSION', 'UPSTREAM', 'UPSTREAM_COMMIT']
