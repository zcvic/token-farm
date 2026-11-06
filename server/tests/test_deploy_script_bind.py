"""部署脚本里的「容器 → 宿主上游」地址判定（issue #243 的部署侧）。

## 为什么单独守这一块

管理端**跑在容器里**，容器内的 `127.0.0.1` 是它自己。上游端口如果只绑宿主回环：

  · Docker Desktop（Windows / macOS）：`host.docker.internal` 会被转发到宿主回环，
    能用；
  · Linux：没有这个转发，容器连不上 —— 表现是「面板起来了，但一直显示上游不可用」，
    而且日志里没有任何线索（健康检查是面板自己发的，不是容器发的）。

同一个坑有两条入口，所以两个脚本都得处理：

  1. 把上游 compose 的端口行收敛时，目标地址不能写死 `127.0.0.1`；
  2. `.env` 里 `WB2API_BASE=http://127.0.0.1:7863`（`.env.example` 的老形态）
     会被 compose 原样传进容器，那里等于连自己。

这些都不是语法错误，跑起来也不会报错，所以只能按文本钉住。真正的行为验证见
提交说明里的沙箱实测（`docker` / `curl` / `uname` 全打桩，跑真实脚本）。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_ROOT = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding='utf-8-sig')


class ComposeDefaultTest(unittest.TestCase):
    def test_base_is_overridable_and_defaults_to_host_gateway(self) -> None:
        """compose 里的上游地址要能被 .env 覆盖，默认走宿主网关。

        写死 `http://127.0.0.1:7863` 的后果：容器连自己，且用户改 .env 也没用。
        """
        text = _read('docker-compose.yml')
        self.assertIn('WB2API_BASE: ${WB2API_BASE:-http://host.docker.internal:7863}', text)
        self.assertNotRegex(
            text, r'(?m)^\s*WB2API_BASE:\s*http://127\.0\.0\.1',
            'compose 又把上游地址写死成 127.0.0.1 了 —— 容器里那是它自己',
        )

    def test_extra_hosts_maps_host_gateway(self) -> None:
        """Linux 上 `host.docker.internal` 必须靠 extra_hosts 解析到宿主。"""
        text = _read('docker-compose.yml')
        self.assertIn('host.docker.internal:host-gateway', text)


class ShellDeployBindTest(unittest.TestCase):
    """`deploy/deploy.sh`：按平台选绑定地址 + 改写 .env 里的回环值。"""

    def setUp(self) -> None:
        self.src = _read('deploy/deploy.sh')

    def test_has_bind_override(self) -> None:
        self.assertIn('TF_UPSTREAM_BIND', self.src, '没有给用户留覆盖入口')

    def test_linux_uses_docker0_gateway(self) -> None:
        """Linux 分支必须探测 docker0 网关，并在失败时回退 172.17.0.1。"""
        self.assertIn("docker network inspect bridge", self.src)
        self.assertRegex(self.src, r'UPSTREAM_BIND="\$\{UPSTREAM_BIND:-172\.17\.0\.1\}"')
        self.assertIn('uname -s', self.src, '没有按平台分流')

    def test_port_convergence_uses_detected_bind(self) -> None:
        """端口收敛的目标地址必须是探测出来的那个，不是硬编码回环。"""
        self.assertIn('${UPSTREAM_BIND}:${UPSTREAM_PORT}:${UPSTREAM_PORT}', self.src)
        self.assertNotIn('"127.0.0.1:${UPSTREAM_PORT}:${UPSTREAM_PORT}"', self.src,
                         'deploy.sh 仍把上游端口写死成 127.0.0.1（Linux 上容器连不上）')

    def test_normalizes_loopback_wb2api_base(self) -> None:
        """`.env` 里的 127.0.0.1 / localhost 要被改写，服务名与自定义 IP 不能动。"""
        self.assertIn('normalize_wb2api_base', self.src)
        fn = self.src.split('normalize_wb2api_base()', 1)[1].split('\n}', 1)[0]
        for token in ('127.0.0.1', 'localhost', '[::1]'):
            self.assertIn(token, fn, f'没有识别 {token}')
        self.assertIn('WB2API_TARGET', fn.replace('${WB2API_TARGET}', 'WB2API_TARGET'))
        self.assertIn('return 0', fn, '非回环值必须原样保留')


class PowerShellDeployBindTest(unittest.TestCase):
    """`deploy/deploy.ps1`：同一件事的 Windows 版本（Docker Desktop 走 host.docker.internal）。"""

    def setUp(self) -> None:
        self.src = _read('deploy/deploy.ps1')

    def test_defaults_to_loopback_and_host_gateway_name(self) -> None:
        self.assertIn("'127.0.0.1'", self.src)
        self.assertIn('http://host.docker.internal:', self.src)

    def test_has_normalizer(self) -> None:
        self.assertIn('function Update-Wb2ApiBase', self.src)
        fn = self.src.split('function Update-Wb2ApiBase', 1)[1].split('\n}', 1)[0]
        self.assertIn('WB2API_BASE', fn)
        self.assertIn('127\\.0\\.0\\.1', fn, '没有识别回环地址')
        self.assertIn('host.docker.internal', self.src)
        # 调用点也要在，否则函数是死代码
        self.assertRegex(self.src, r'(?m)^Update-Wb2ApiBase -Path \$envPath')

    def test_port_pattern_accepts_existing_host_prefix(self) -> None:
        """正则要能匹配已经收敛过的 `127.0.0.1:7863:7863`，否则换不了地址。"""
        self.assertRegex(self.src, r'\(\?:\\\.\\d\{1,3\}\)\{3\}',
                         '端口行正则不认「已有主机前缀」的形态')


class EnvExampleTest(unittest.TestCase):
    """模板里保留 127.0.0.1（systemd 形态），但必须说清容器部署会被改写。"""

    def test_documents_container_rewrite(self) -> None:
        text = _read('.env.example')
        self.assertRegex(text, r'(?m)^WB2API_BASE=http://127\.0\.0\.1:7863$')
        head = text.split('WB2API_BASE=', 1)[0]
        self.assertIn('容器', head, '.env.example 没提醒容器部署会改写这一行')


if __name__ == '__main__':
    unittest.main()
