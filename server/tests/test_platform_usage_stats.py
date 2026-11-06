"""多平台用量 / 积分统计与日志过滤。

背景：平台调用（`trae/...`）此前会和 WorkBuddy 的流量混在同一行累计——
`usage_daily` 没有平台列，`platform` 只体现在模型名前缀里。结果是：

  · 顶部切到 Trae，统计页仍是 WorkBuddy 的数字（实际是全混在一起）；
  · 「今日消耗」把平台的 credit 算进 WorkBuddy；
  · 日志页也没法只看某个平台。

修法：`request_logs.platform` + `usage_daily/usage_hourly` 的 `platform` 维度
（主键也含它），统计与日志接口接受 `platform` 参数（`workbuddy` = 空串）。

这里钉住三件事：

  1. 同名模型在两个平台下**分开累计**，不会合并；
  2. 统计接口按平台过滤，WorkBuddy 包含历史 NULL（日志）与空串（汇总）；
  3. 平台模式下积分收入取本端签到记录（`checkin_logs.credits`），并用
     `income_partial=True` 说明它只覆盖签到渠道。
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db  # noqa: E402
from server.routers import logs, stats  # noqa: E402

_USER = {'username': 'a', 'role': 'admin'}


class PlatformUsageStatsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'platform.db'
        db._conn = None
        db.connect()
        db.execute(
            "INSERT INTO api_keys(name,key_hash,prefix,enabled,created_at,quota,used_tokens) "
            "VALUES('k','h','wbk_test',1,?,0,0)",
            (int(time.time()),),
        )
        self.kid = db.query_one('SELECT id FROM api_keys LIMIT 1')['id']

        # WorkBuddy：同名模型 + 空平台
        db.bump_usage(self.kid, 'glm-5.3', 20, 10, credit=0.5, realm='cn')
        # 平台：同一模型名 + trae
        db.bump_usage(self.kid, 'glm-5.3', 100, 50, credit=2.5,
                      realm='cn', platform='trae')
        # 平台签到收入：本端签到路由写入的到账积分
        db.add_checkin_log('u-trae', 'Trae 号', 'platform-batch', True, 0,
                           '签到成功', platform='trae', realm='cn',
                           credits=100)

    def tearDown(self) -> None:
        try:
            if db._conn is not None:
                db._conn.close()
        except Exception:  # noqa: BLE001
            pass
        db._conn = None
        config.DB_PATH = self._orig_db
        self._tmp.cleanup()

    # ── 累计 ─────────────────────────────────────────────
    def test_same_model_accumulates_per_platform(self) -> None:
        rows = db.query(
            'SELECT platform, requests, prompt_tokens, completion_tokens, credit '
            'FROM usage_daily ORDER BY platform'
        )
        self.assertEqual(len(rows), 2, '同名模型跨平台被合并成一行了')
        by_platform = {r['platform']: r for r in rows}
        self.assertEqual(set(by_platform), {'', 'trae'})
        self.assertEqual(int(by_platform['']['requests']), 1)
        self.assertEqual(int(by_platform['trae']['requests']), 1)
        self.assertAlmostEqual(float(by_platform['trae']['credit']), 2.5)

    def test_hourly_also_splits_platforms(self) -> None:
        rows = db.query('SELECT platform, SUM(requests) AS n FROM usage_hourly GROUP BY platform')
        self.assertEqual({r['platform']: int(r['n']) for r in rows},
                         {'': 1, 'trae': 1})

    # ── 统计接口 ─────────────────────────────────────────
    def test_summary_filters_by_platform(self) -> None:
        wb = stats.summary(platform='workbuddy', user=_USER)
        trae = stats.summary(platform='trae', user=_USER)
        self.assertEqual(wb['today_requests'], 1)
        self.assertEqual(trae['today_requests'], 1)
        self.assertEqual(trae['today_tokens'], 150)
        self.assertAlmostEqual(trae['today_credit'], 2.5)
        # 不过滤 = 两个平台合计
        allp = stats.summary(platform=None, user=_USER)
        self.assertEqual(allp['today_requests'], 2)
        self.assertEqual(allp['today_tokens'], 180)

    def test_breakdowns_filter_by_platform(self) -> None:
        models = stats.by_model(days=1, platform='trae', user=_USER)
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0]['name'], 'glm-5.3')
        self.assertEqual(models[0]['prompt_tokens'], 100)

        keys = stats.by_key(days=1, platform='trae', user=_USER)
        self.assertEqual(len(keys), 1)
        self.assertEqual(keys[0]['requests'], 1)

        daily = stats.daily(days=1, platform='trae', user=_USER)
        self.assertEqual(sum(p['requests'] for p in daily), 1)
        hourly = stats.hourly(platform='trae', user=_USER)
        self.assertEqual(sum(p['requests'] for p in hourly), 1)

    def test_credits_income_comes_from_checkin_logs_on_platforms(self) -> None:
        flow = stats.credits_flow(days=1, platform='trae', user=_USER)
        self.assertTrue(flow['income_tracked'])
        self.assertTrue(flow['income_partial'], '平台收入只覆盖签到渠道')
        self.assertAlmostEqual(flow['total']['income'], 100)
        self.assertAlmostEqual(flow['total']['expense'], 2.5)
        # WorkBuddy 仍按原来的口径追踪完整收入
        wb = stats.credits_flow(days=1, platform='workbuddy', user=_USER)
        self.assertTrue(wb['income_tracked'])
        self.assertFalse(wb['income_partial'])

    # ── 日志接口 ─────────────────────────────────────────
    def _log(self, model: str, mapped: str, platform: str | None) -> None:
        db.add_request_log(
            ts=int(time.time()), key_id=self.kid, ip='1.1.1.1', model=model,
            mapped_model=mapped, status=200, prompt_tokens=1, completion_tokens=1,
            latency_ms=10, first_token_ms=None, ua='UA', error=None, stream=1,
            credit=0.1, realm='cn', platform=platform,
        )

    def test_logs_filter_by_platform(self) -> None:
        self._log('glm-5.3', 'glm-5.3', '')
        self._log('trae/glm-5.3', 'trae/glm-5.3', 'trae')

        trae = logs.list_logs(platform='trae', user=_USER)
        self.assertEqual(trae['total'], 1)
        self.assertEqual(trae['items'][0]['platform'], 'trae')

        # WorkBuddy 必须同时兼容「空串」与历史 NULL
        self._log('old', 'old', None)
        wb = logs.list_logs(platform='workbuddy', user=_USER)
        self.assertEqual(wb['total'], 2)
        self.assertEqual({i['platform'] for i in wb['items']}, {''})

        self.assertEqual(logs.list_logs(user=_USER)['total'], 3)

    def test_unknown_platform_has_no_rows(self) -> None:
        self.assertEqual(stats.summary(platform='nope', user=_USER)['today_requests'], 0)
        self.assertEqual(logs.list_logs(platform='nope', user=_USER)['total'], 0)


if __name__ == '__main__':
    unittest.main()
