"""平台切换（前端逻辑，用 Node 执行）。

Discovery / Kilo / Cline 已注册、能路由、能进组合，但**不该出现在右上角的
平台下拉里**。这层过滤写错不会报错：三个平台又出现在下拉里，或者一个正常
平台被误伤消失。所以用行为测试跑真实实现，而不是在 Python 里复制一份名单。

没有 Node（或版本太旧）时**跳过**而不是失败。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / 'web' / 'lib' / 'platform-switch.test.mjs'
_PURE = _ROOT / 'web' / 'lib' / 'platform-switch.ts'
_NODE = shutil.which('node')
_MIN_MAJOR = 22


def _node_major() -> int | None:
    if not _NODE:
        return None
    try:
        out = subprocess.run([_NODE, '--version'], capture_output=True,
                             text=True, encoding='utf-8', errors='replace',
                             timeout=15).stdout.strip()
        return int(out.lstrip('v').split('.')[0])
    except Exception:  # noqa: BLE001
        return None


_MAJOR = _node_major()


def _code(path: Path) -> str:
    src = path.read_text(encoding='utf-8')
    src = re.sub(r'/\*.*?\*/', '', src, flags=re.S)
    return re.sub(r'//[^\n]*', '', src)


class PlatformSwitchBehaviourTest(unittest.TestCase):
    @unittest.skipUnless(_NODE, '未安装 node，跳过前端逻辑测试')
    @unittest.skipUnless(_MAJOR is not None and _MAJOR >= _MIN_MAJOR,
                         f'需要 node ≥ {_MIN_MAJOR}（type stripping），当前 {_MAJOR}')
    def test_switch_hides_discovery_kilo_cline(self) -> None:
        self.assertTrue(_SCRIPT.is_file(), f'缺少测试脚本: {_SCRIPT}')

        def run_once() -> subprocess.CompletedProcess:
            return subprocess.run(
                [_NODE, '--experimental-strip-types', str(_SCRIPT)],
                capture_output=True, text=True, encoding='utf-8', errors='replace',
                timeout=120, cwd=str(_ROOT),
            )

        proc = run_once()
        if proc.returncode not in (0, 1):
            proc = run_once()
        out = (proc.stdout or '') + (proc.stderr or '')
        self.assertEqual(proc.returncode, 0, f'平台切换过滤不符合预期：\n{out}')
        self.assertIn('all passed', out, f'脚本没有跑到通过：\n{out}')


class PlatformSwitchInvariantTest(unittest.TestCase):
    def test_module_stays_runnable_without_node_modules(self) -> None:
        offenders = [
            line.strip() for line in _code(_PURE).splitlines()
            if line.strip().startswith('import ')
            and not line.strip().startswith('import type')
        ]
        self.assertEqual(
            offenders, [],
            'platform-switch.ts 出现了运行时 import：\n  '
            + '\n  '.join(offenders)
            + '\n本模块被 .test.mjs 直接 import，引入运行时 import 会让那条测试'
              '以「找不到模块」失败。',
        )


class PlatformSwitchWiringTest(unittest.TestCase):
    """前端接线：平台清单必须经过过滤再交给下拉。

    这条是「不报错的错」：不接线时过滤函数本身全绿，但下拉里照样能看到
    Discovery / Kilo / Cline。
    """

    def test_platform_context_filters_switch_options(self) -> None:
        path = _ROOT / 'web' / 'lib' / 'platform-context.tsx'
        self.assertTrue(path.is_file(), f'缺少文件: {path}')
        src = path.read_text(encoding='utf-8')
        self.assertIn('filterSwitchablePlatforms', src)
        self.assertIn('setPlatforms(filterSwitchablePlatforms(list ?? []))', src)


if __name__ == '__main__':
    unittest.main()