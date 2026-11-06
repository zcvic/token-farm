"""平台账号列表的检索 / 筛选 / 排序（前端逻辑，借 Node 执行）。

与 `test_account_list.py` 同一套路：这批改的是**操作手感**，错了不会报错，
只是列表看起来不对（把冻结显示成过期、把未知积分当成 0 分、排序改动入参）。
行为测试跑的是真实实现，不是复制一份逻辑。

没有 Node（或版本太旧）时**跳过**而不是失败。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / 'web' / 'lib' / 'platform-account-list.test.mjs'
_PURE = _ROOT / 'web' / 'lib' / 'platform-account-list.ts'
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


class PlatformAccountListBehaviourTest(unittest.TestCase):
    @unittest.skipUnless(_NODE, '未安装 node，跳过前端逻辑测试')
    @unittest.skipUnless(_MAJOR is not None and _MAJOR >= _MIN_MAJOR,
                         f'需要 node ≥ {_MIN_MAJOR}（type stripping），当前 {_MAJOR}')
    def test_filter_sort(self) -> None:
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
        self.assertEqual(proc.returncode, 0, f'平台账号列表逻辑不符合预期：\n{out}')
        self.assertIn('all passed', out, f'脚本没有跑到通过：\n{out}')


class PlatformAccountListInvariantTest(unittest.TestCase):
    def test_module_stays_runnable_without_node_modules(self) -> None:
        offenders = [
            line.strip() for line in _code(_PURE).splitlines()
            if line.strip().startswith('import ')
            and not line.strip().startswith('import type')
        ]
        self.assertEqual(
            offenders, [],
            'platform-account-list.ts 出现了运行时 import：\n  '
            + '\n  '.join(offenders)
            + '\n本模块被 .test.mjs 直接 import，引入运行时 import 会让那条测试'
              '以「找不到模块」失败。',
        )


if __name__ == '__main__':
    unittest.main()
