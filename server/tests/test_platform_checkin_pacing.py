"""平台批量签到的节奏：并发上限 + 账号间隔。

## 钉住的问题

Trae / Qoder 的「全部签到」曾经是 `asyncio.gather` + 一个硬编码信号量：账号
之间**零间隔**，多个账号在同一秒打到同一个站点（Qoder 的活动平台还会按
「人 / 设备」去重，后面的账号领不到）。这个文件确保「加了间隔」不只是写进
文档，而是真的作用在批量签到的执行路径上。

## 默认值

没有任何设置时：并发 2、相邻账号开始签到之间 2–5 秒随机间隔。间隔为 0 时
应退化为「只受并发上限约束」的旧行为。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db  # noqa: E402
from server.platforms import checkinpacing  # noqa: E402
from server.routers import platforms as platforms_router  # noqa: E402


class _DbCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'pace.db'
        db._conn = None
        db.connect()

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass


class SettingsTest(_DbCase):
    def test_defaults_are_conservative(self) -> None:
        pace = checkinpacing.load('trae')
        self.assertEqual(pace['checkin_concurrency'], 2)
        self.assertEqual(pace['checkin_delay_min'], 2)
        self.assertEqual(pace['checkin_delay_max'], 5)
        self.assertEqual(pace['checkin_limits']['concurrency_max'], 8)
        self.assertEqual(pace['checkin_limits']['delay_max'], 60)

    def test_partial_update_keeps_other_fields(self) -> None:
        checkinpacing.save('trae', {'checkin_concurrency': 4,
                                    'checkin_delay_min': 1,
                                    'checkin_delay_max': 3})
        checkinpacing.save('trae', {'checkin_concurrency': 6})
        pace = checkinpacing.load('trae')
        self.assertEqual(pace['checkin_concurrency'], 6)
        self.assertEqual((pace['checkin_delay_min'],
                          pace['checkin_delay_max']), (1, 3))
        # 按平台分开：改 Trae 不能影响 Qoder。
        self.assertEqual(checkinpacing.load('qoder')['checkin_concurrency'], 2)

    def test_rejects_invalid_values(self) -> None:
        bad_payloads = (
            {'checkin_concurrency': 0},
            {'checkin_concurrency': 9},
            {'checkin_concurrency': 'x'},
            {'checkin_concurrency': True},
            {'checkin_delay_min': -1},
            {'checkin_delay_max': 61},
            {'checkin_delay_min': 9, 'checkin_delay_max': 3},
        )
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    checkinpacing.save('trae', payload)

    def test_broken_stored_value_falls_back_to_default(self) -> None:
        db.set_setting('platforms.trae.checkin_delay_max', 999)
        self.assertEqual(checkinpacing.load('trae')['checkin_delay_max'], 5)
        db.set_setting('platforms.trae.checkin_concurrency', 'two')
        self.assertEqual(checkinpacing.load('trae')['checkin_concurrency'], 2)


class _FakeAdapter:
    """记录每个账号的开始时刻与同时运行数；签到本身不联网。"""

    name = 'fake'
    label = 'Fake'

    def __init__(self, count: int, duration: float = 0.0) -> None:
        self.accounts = [{'uid': f'u{i}', 'nickname': f'n{i}'}
                         for i in range(count)]
        self.duration = duration
        self.starts: list[float] = []
        self.running = 0
        self.peak = 0

    def list_accounts(self) -> list[dict]:
        return list(self.accounts)

    async def checkin(self, account_id: str) -> dict:
        self.starts.append(time.monotonic())
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            if self.duration:
                await asyncio.sleep(self.duration)
        finally:
            self.running -= 1
        return {'ok': True, 'message': 'ok'}


class CheckinAllPacingTest(_DbCase):
    def _run(self, adapter: _FakeAdapter) -> dict:
        with mock.patch.object(platforms_router, '_adapter', return_value=adapter), \
             mock.patch.object(platforms_router, '_log_platform_checkin'):
            return asyncio.run(platforms_router.checkin_all(
                adapter.name, user={'role': 'admin'}))

    def test_concurrency_cap_is_applied(self) -> None:
        checkinpacing.save('fake', {'checkin_concurrency': 2,
                                    'checkin_delay_min': 0,
                                    'checkin_delay_max': 0})
        adapter = _FakeAdapter(5, duration=0.15)
        out = self._run(adapter)

        self.assertEqual(out['total'], 5)
        self.assertEqual(out['ok'], 5)
        self.assertEqual(adapter.peak, 2, '签到并发没有被设置约束')

    def test_account_starts_are_spaced(self) -> None:
        checkinpacing.save('fake', {'checkin_concurrency': 3,
                                    'checkin_delay_min': 1,
                                    'checkin_delay_max': 1})
        adapter = _FakeAdapter(3)
        out = self._run(adapter)

        self.assertEqual(out['ok'], 3)
        starts = sorted(adapter.starts)
        self.assertEqual(len(starts), 3)
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        for gap in gaps:
            self.assertGreaterEqual(gap, 0.85,
                                    f'相邻账号开始间隔过小: {gap:.3f}s')

    def test_zero_delay_means_no_wait(self) -> None:
        """间隔填 0 = 只受并发上限约束（旧行为），不能仍在偷偷 sleep。"""
        checkinpacing.save('fake', {'checkin_concurrency': 4,
                                    'checkin_delay_min': 0,
                                    'checkin_delay_max': 0})
        adapter = _FakeAdapter(4, duration=0.1)
        out = self._run(adapter)

        self.assertEqual(out['ok'], 4)
        self.assertEqual(adapter.peak, 4)
        starts = sorted(adapter.starts)
        self.assertLess(starts[-1] - starts[0], 0.5,
                        '间隔为 0 时仍在等待')


class SettingsRouteTest(_DbCase):
    def test_checkin_fields_can_be_saved_without_max_in_flight(self) -> None:
        """界面单独改签到节奏时，不必连带提交池级并发（旧接口会因此 400）。"""
        class _Adapter:
            name = 'fake'
            label = 'Fake'

        fake_security = mock.Mock()
        with mock.patch.object(platforms_router, '_adapter',
                               return_value=_Adapter()), \
             mock.patch.object(platforms_router, 'security', fake_security):
            out = platforms_router.save_platform_settings(
                'fake', {'checkin_concurrency': 4,
                         'checkin_delay_min': 1,
                         'checkin_delay_max': 2},
                user={'role': 'admin'})

        self.assertEqual(out['checkin_concurrency'], 4)
        self.assertEqual(out['checkin_delay_min'], 1)
        self.assertEqual(out['checkin_delay_max'], 2)
        fake_security.audit.assert_called_once()


if __name__ == '__main__':
    unittest.main()
