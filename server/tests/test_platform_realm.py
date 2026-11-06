"""平台版本映射（前端逻辑，用 Node 执行）。

界面用 `global` / `cn`，平台适配器用 `intl` / `cn`。这层映射写错**不会报错**
——切换国际版后列表只是空的，或者手工导入的账号被悄悄塞进国内版账号池。
所以用行为测试跑真实实现，而不是在 Python 里复制一份映射。

没有 Node（或版本太旧）时**跳过**而不是失败。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / 'web' / 'lib' / 'platform-realm.test.mjs'
_PURE = _ROOT / 'web' / 'lib' / 'platform-realm.ts'
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


class PlatformRealmBehaviourTest(unittest.TestCase):
    @unittest.skipUnless(_NODE, '未安装 node，跳过前端逻辑测试')
    @unittest.skipUnless(_MAJOR is not None and _MAJOR >= _MIN_MAJOR,
                         f'需要 node ≥ {_MIN_MAJOR}（type stripping），当前 {_MAJOR}')
    def test_realm_mapping(self) -> None:
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
        self.assertEqual(proc.returncode, 0, f'平台版本映射不符合预期：\n{out}')
        self.assertIn('all passed', out, f'脚本没有跑到通过：\n{out}')


class PlatformRealmInvariantTest(unittest.TestCase):
    def test_module_stays_runnable_without_node_modules(self) -> None:
        offenders = [
            line.strip() for line in _code(_PURE).splitlines()
            if line.strip().startswith('import ')
            and not line.strip().startswith('import type')
        ]
        self.assertEqual(
            offenders, [],
            'platform-realm.ts 出现了运行时 import：\n  '
            + '\n  '.join(offenders)
            + '\n本模块被 .test.mjs 直接 import，引入运行时 import 会让那条测试'
              '以「找不到模块」失败。',
        )


class PlatformRealmWiringTest(unittest.TestCase):
    """前端接线：开关要出现、列表要过滤、导入要带版本。

    这三条都是「不报错的错」：开关不出现 = 功能像没做；列表不过滤 = 两个
    版本的账号混在一起；导入不带版本 = 国际版账号被悄悄写进国内版池。
    单元测试覆盖不到 JSX，只能钉住接线点。
    """

    def _source(self, *parts: str) -> str:
        path = _ROOT.joinpath('web', *parts)
        self.assertTrue(path.is_file(), f'缺少文件: {path}')
        return path.read_text(encoding='utf-8')

    def test_toggle_is_shown_for_realm_aware_platforms(self) -> None:
        src = self._source('components', 'common', 'layout', 'RealmToggle.tsx')
        self.assertIn('platformInfo?.supports_realm', src)
        self.assertIn('isWorkbuddy', src)

    def test_accounts_panel_filters_and_forwards_realm(self) -> None:
        src = self._source('components', 'common', 'platforms',
                           'PlatformAccountsPanel.tsx')
        self.assertIn('filterByPlatformRealm', src)
        self.assertIn('withRealmDefault', src)
        self.assertIn('importDesktop(', src)
        # 不能再写死国内版：网页登录必须跟随当前版本（国际版由后端拒绝）
        self.assertNotIn("loginStart(platform, 'cn')", src)
        self.assertIn('realmAware ? realmScope', src)

    def test_models_panel_filters_by_realm(self) -> None:
        src = self._source('components', 'common', 'platforms',
                           'PlatformModelsPanel.tsx')
        self.assertIn('filterByPlatformRealm', src)
        self.assertIn('listed', src)

    def test_realm_choice_is_scoped_per_platform(self) -> None:
        """WorkBuddy 的国际版不能把 Trae 也带成国际版。"""
        src = self._source('lib', 'realm-context.tsx')
        self.assertIn('PLATFORM_STORAGE_PREFIX', src)
        self.assertIn('usePlatform', src)
        self.assertIn('realms[platform]', src)


if __name__ == '__main__':
    unittest.main()
