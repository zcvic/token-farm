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
        """界面单独改签到节奏时，不必连带提交单账号并发（旧接口会因此 400）。"""
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


class ManualCheckinLogTest(_DbCase):
    """手动单账号签到：账号列表读不到时也必须把这条签到记下来。

    `_log_platform_checkin` 见 uid 为空会直接 return —— 签到真的成功了，但
    积分收支里没有这笔收入，用户只会看到「签到了却没加分」。
    """

    class _Adapter:
        name = 'fake'
        label = 'Fake'

        def __init__(self, accounts=None, broken: bool = False) -> None:
            self._accounts = accounts or []
            self._broken = broken

        def list_accounts(self) -> list[dict]:
            if self._broken:
                raise RuntimeError('账号目录读不到')
            return list(self._accounts)

        async def checkin(self, account_id: str) -> dict:
            return {'uid': account_id, 'ok': True, 'earned_credit': 200,
                    'credits': 200, 'message': '签到成功'}

    def _run(self, adapter) -> dict:  # noqa: ANN001
        with mock.patch.object(platforms_router, '_adapter',
                               return_value=adapter):
            return asyncio.run(platforms_router.checkin(
                adapter.name, 'u1', user={'role': 'admin'}))

    def _rows(self) -> list[dict]:
        return db.query('SELECT * FROM checkin_logs WHERE platform = ?',
                        ('fake',))

    def test_list_accounts_failure_keeps_the_record(self) -> None:
        self._run(self._Adapter(broken=True))
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['uid'], 'u1')
        self.assertEqual(rows[0]['credits'], 200)
        self.assertEqual(rows[0]['source'], 'platform-manual')

    def test_account_missing_from_list_keeps_the_record(self) -> None:
        self._run(self._Adapter(accounts=[{'uid': 'other'}]))
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['uid'], 'u1')

    def test_found_account_keeps_nickname(self) -> None:
        self._run(self._Adapter(accounts=[{'uid': 'u1', 'nickname': '小号'}]))
        rows = self._rows()
        self.assertEqual(rows[0]['nickname'], '小号')
        self.assertEqual(rows[0]['credits'], 200)


class AccountCheckinLockTest(_DbCase):
    """同一个账号的签到必须串行：手动与定时同时进来时只能有一次带收入。

    竞态：两路都先读到「未签到」，再各自 claim。上游按自然日幂等、不会多发
    积分，但两路都会看到「已签到」，于是各自写一条带收入的记录，同一天的
    奖励在积分收支里翻倍。锁按「平台 + 账号」，不同账号之间不互相阻塞。
    """

    class _Adapter:
        name = 'race'
        label = 'Race'

        def __init__(self, uids: tuple[str, ...] = ('u1',)) -> None:
            self.uids = list(uids)
            self.signed: set[str] = set()
            self.claims: list[str] = []
            self.active = 0
            self.max_active = 0

        def list_accounts(self) -> list[dict]:
            return [{'uid': uid, 'nickname': uid} for uid in self.uids]

        async def checkin(self, account_id: str) -> dict:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                # 让出控制权：没有账号锁时，第二路会在这里之前看到同一个
                # 「未签到」，两边都往 claims 里记一次。
                await asyncio.sleep(0.01)
                if account_id in self.signed:
                    return {'ok': True, 'already': True, 'earned_credit': 0,
                            'credits': 100, 'message': '今日已签到'}
                self.signed.add(account_id)
                self.claims.append(account_id)
                return {'ok': True, 'already': False, 'earned_credit': 100,
                        'credits': 100, 'message': '签到成功'}
            finally:
                self.active -= 1

    def tearDown(self) -> None:
        for key in [k for k in platforms_router._CHECKIN_LOCKS if k[0] == 'race']:
            platforms_router._CHECKIN_LOCKS.pop(key, None)
        super().tearDown()

    @staticmethod
    async def _checkin_all(uids: tuple[str, ...]) -> list[dict]:
        return list(await asyncio.gather(*(
            platforms_router.checkin('race', uid, user={'role': 'admin'})
            for uid in uids)))

    def _run(self, adapter) -> list[dict]:  # noqa: ANN001
        with mock.patch.object(platforms_router, '_adapter',
                               return_value=adapter):
            return asyncio.run(self._checkin_all(tuple(adapter.uids)))

    def _income_rows(self) -> list[dict]:
        return db.query('SELECT * FROM checkin_logs WHERE platform = ? '
                        'AND credits > 0', ('race',))

    def test_same_account_is_serialized_and_earns_once(self) -> None:
        adapter = self._Adapter()
        self._run(adapter)
        self.assertEqual(adapter.max_active, 1, '同一个账号被并发处理了')
        self.assertEqual(adapter.claims, ['u1'], '上游被 claim 了不止一次')
        rows = self._income_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['credits'], 100)

    def test_other_accounts_are_not_blocked(self) -> None:
        adapter = self._Adapter(uids=('u1', 'u2'))
        self._run(adapter)
        self.assertEqual(adapter.max_active, 2, '不同账号被串行化了')
        self.assertEqual(sorted(adapter.claims), ['u1', 'u2'])
        self.assertEqual(len(self._income_rows()), 2)


if __name__ == '__main__':
    unittest.main()
