"""仪表盘 / 统计 / 日志必须跟着「平台」下拉走（而不是只跟版本）。

## 为什么单独钉这一条

平台切换是后加的能力，而这三页原先全部按 `realm`（国内 / 国际）取数——`realm`
是 WorkBuddy 账号池的概念。切到 Trae 后页面照常渲染、数字照样「看起来正常」，
只是**全是 WorkBuddy 的**：用户唯一的线索是「我明明切了平台，数字没变」。
这类缺口不会被类型系统或 i18n 守卫发现，只能靠人点一遍（实际就是这么发现的）。

## 两条不变量

1. 三个页面调用统计接口时必须把 `platform` 传下去，否则后端按缺省口径返回
   **所有平台**的合计——那是「混着看」，比单看 WorkBuddy 更容易误判。
2. 版本（realm）只对**分版本的平台**传：WorkBuddy 是账号池划分；Trae 这类
   `supports_realm` 的平台由**实际选中的账号**决定，网关按它记账。其余平台
   （组合等）没有版本概念，带上用户上次选的「国际版」只会把行过滤空。

第 2 条用「`isWorkbuddy || platformInfo?.supports_realm` 才取 realm」来钉：
源码里必须出现这层归一化，而不是无条件传 `realm`，也不是早先那样只认
WorkBuddy——那会把 Trae 国际版的用量与国内版混在一起（实际就是这么发现的）。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_ROOT = Path(__file__).resolve().parents[2]
_WEB = _ROOT / 'web' / 'app' / '(main)'
_DASHBOARD = _WEB / 'dashboard' / 'page.tsx'
_STATS = _WEB / 'stats' / 'page.tsx'
_LOGS = _WEB / 'logs' / 'page.tsx'

# 统计接口的调用：抓到括号里那一串实参。允许**一层**内嵌括号
# （`Number(flowDays) || 7` 这种），否则参数会在第一个 `)` 处被截断。
_CALL = re.compile(
    r'statsApi\.(summary|daily|creditsFlow|byModel|byKey|hourly)'
    r'\(((?:[^()]|\([^()]*\))*)\)')
# 版本归一化：WorkBuddy 与「分版本的平台」才给 realm
_NORMALIZED = re.compile(
    r'const\s+(statsRealm|logRealm)\s*=\s*isWorkbuddy\s*\|\|\s*'
    r'platformInfo\?\.supports_realm\s*\?\s*realm\s*:\s*undefined\s*;')


def _strip_comments(src: str) -> str:
    src = re.sub(r'/\*.*?\*/', '', src, flags=re.S)
    return re.sub(r'//[^\n]*', '', src)


def _calls(src: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for name, args in _CALL.findall(_strip_comments(src)):
        out.setdefault(name, []).append(args.strip())
    return out


class DashboardPlatformScopeTest(unittest.TestCase):
    """首页：平台模式下换数据源，而不是继续报 WorkBuddy 的账号池。"""

    def setUp(self) -> None:
        self.src = _DASHBOARD.read_text(encoding='utf-8')
        self.calls = _calls(self.src)

    def test_scan_is_not_vacuous(self) -> None:
        self.assertIn('summary', self.calls, '没扫到 statsApi.summary 调用 —— 正则失效了')
        self.assertIn('usePlatform', self.src)

    def test_usage_queries_carry_platform(self) -> None:
        for name in ('summary', 'daily'):
            for args in self.calls[name]:
                self.assertTrue(
                    args.endswith('platform'),
                    f'statsApi.{name}({args}) 没把 platform 传下去：'
                    '后端会按缺省口径把所有平台合在一起统计')
        for args in self.calls.get('creditsFlow', []):
            self.assertTrue(args.endswith('platform'),
                            f'statsApi.creditsFlow({args}) 没把 platform 传下去')

    def test_realm_is_scoped_to_realm_aware_platforms(self) -> None:
        self.assertRegex(
            self.src, _NORMALIZED,
            '仪表盘必须把 realm 传给 WorkBuddy 与分版本的平台'
            '（否则 Trae 国际版会被统计成国内版）')
        for name in ('summary', 'daily'):
            for args in self.calls[name]:
                self.assertIn('statsRealm', args, f'statsApi.{name} 应传 statsRealm')

    def test_platform_mode_reads_platform_accounts(self) -> None:
        """平台模式下得真的去取平台账号 / 积分，而不是把 WorkBuddy 的池子留着。"""
        self.assertIn('platformApi.accounts(platform)', self.src)
        self.assertIn('platformApi.credits(platform)', self.src)
        self.assertIn('supports_credits', self.src)


class StatsAndLogsPlatformScopeTest(unittest.TestCase):
    """统计页与日志页同一口径（两处都曾被 `realm` 挡住平台数据）。"""

    def test_stats_page_carries_platform_and_scopes_realm(self) -> None:
        src = _STATS.read_text(encoding='utf-8')
        calls = _calls(src)
        self.assertIn('summary', calls, '没扫到统计页的 statsApi.summary')
        for args in calls['summary'] + calls['daily'] + calls['byModel'] + calls['byKey']:
            self.assertTrue(args.endswith('platform'), f'统计页没传 platform：{args}')
            self.assertIn('statsRealm', args, f'统计页应传 statsRealm：{args}')
        self.assertRegex(src, _NORMALIZED)

    def test_logs_page_carries_platform_and_scopes_realm(self) -> None:
        src = _LOGS.read_text(encoding='utf-8')
        self.assertIn('platform,', src)
        self.assertRegex(src, _NORMALIZED)
        self.assertIn('realm: logRealm', src)


class GuardIsNotVacuousTest(unittest.TestCase):
    """反向验证：漏传 platform、或把 realm 只限定给 WorkBuddy，守卫必须报错。"""

    def test_missing_platform_is_detected(self) -> None:
        broken = 'summary: () => statsApi.summary(realm),'
        args = _calls(broken)['summary'][0]
        self.assertFalse(args.endswith('platform'))

    def test_workbuddy_only_realm_is_detected(self) -> None:
        """只认 WorkBuddy 的旧写法会让 Trae 国际版与国内版混在一起。"""
        broken = ('const x = 1;\n'
                  'const statsRealm = isWorkbuddy ? realm : undefined;\n'
                  'summary: () => statsApi.summary(statsRealm, platform),')
        self.assertNotRegex(broken, _NORMALIZED)
        self.assertTrue(_calls(broken)['summary'][0].endswith('platform'))


if __name__ == '__main__':
    unittest.main()
