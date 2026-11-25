"""DuMate 对话扣费账本：落库、去重、按日 / 按小时聚合。

这里跑的是**真实 SQLite**（不是 mock SQL）：统计接口的聚合 SQL 与写入时的
指纹去重都在这张表上，只有真库才能钉住「重复同步不会重复计账」与「本地日
/ 小时分桶与 usage_daily 同口径」。
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db  # noqa: E402


class DumateUsageLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'dumate-ledger.db'
        db._conn = None
        db.connect()
        self.today = db.day_of(int(time.time()))
        self.today_ts = db.day_start_ts(self.today)
        self.yesterday = db.day_of(self.today_ts - 86400)

    def tearDown(self) -> None:
        try:
            if db._conn is not None:
                db._conn.close()
        except Exception:  # noqa: BLE001
            pass
        db._conn = None
        config.DB_PATH = self._orig_db
        self._tmp.cleanup()

    def _rows(self) -> list[dict]:
        return [dict(r) for r in db.query(
            'SELECT * FROM dumate_usage_records ORDER BY ts, id')]

    def test_records_land_with_local_day_and_positive_points(self) -> None:
        stored = db.add_dumate_usage_records('dm_1', [
            # 上游 pointsChange 是负数；账本存正数消耗。
            {'at': self.today_ts + 10 * 3600 + 30 * 60, 'points': -3.5,
             'conversation': 'c1', 'package_id': 'p1'},
            {'at': self.today_ts - 3600, 'points': -1.25,
             'conversation': 'c0', 'package_id': 'p1'},
        ])
        self.assertEqual(stored, 2)
        rows = self._rows()
        self.assertEqual([r['day'] for r in rows],
                         [self.yesterday, self.today])
        self.assertEqual([r['points'] for r in rows], [1.25, 3.5])
        self.assertTrue(all(r['fingerprint'] for r in rows))

    def test_repeat_sync_is_idempotent(self) -> None:
        rows = [{'at': self.today_ts + 60, 'points': -2,
                 'conversation': 'c1', 'package_id': 'p1'}]
        self.assertEqual(db.add_dumate_usage_records('dm_1', rows), 1)
        # 同一窗口再同步一次（返回值是**新插入**条数，不含重复）。
        self.assertEqual(db.add_dumate_usage_records('dm_1', rows), 0)
        self.assertEqual(len(self._rows()), 1)

    def test_same_amount_same_second_stays_distinct_with_record_id(self) -> None:
        ts = self.today_ts + 120
        rows = [
            {'at': ts, 'points': -1, 'conversation': 'c1',
             'package_id': 'p1', 'record_id': 'r1'},
            {'at': ts, 'points': -1, 'conversation': 'c1',
             'package_id': 'p1', 'record_id': 'r2'},
        ]
        self.assertEqual(db.add_dumate_usage_records('dm_1', rows), 2)
        self.assertEqual(len(self._rows()), 2)

    def test_invalid_rows_are_skipped(self) -> None:
        stored = db.add_dumate_usage_records('dm_1', [
            {'points': -3},                        # 没有时刻 → 无法归日
            {'at': self.today_ts, 'points': 0},    # 0 消耗
            {'at': 'not-a-time', 'points': -2},    # 时间认不出
            {'at': self.today_ts, 'points': -9},   # 正常
        ])
        self.assertEqual(stored, 1)
        self.assertEqual(len(self._rows()), 1)

    def test_empty_uid_writes_nothing(self) -> None:
        self.assertEqual(db.add_dumate_usage_records(
            '', [{'at': self.today_ts, 'points': -1}]), 0)

    def test_expense_aggregates_by_day_and_hour(self) -> None:
        db.add_dumate_usage_records('dm_1', [
            {'at': self.today_ts + 9 * 3600, 'points': -2},
            {'at': self.today_ts + 9 * 3600 + 5, 'points': -3},
            {'at': self.today_ts + 18 * 3600, 'points': -4},
        ])
        db.add_dumate_usage_records('dm_2', [
            {'at': 'bad-time', 'points': -1},  # 认不出的时刻
            {'at': self.today_ts - 3600, 'points': -5},
        ])
        self.assertEqual(db.dumate_expense_by_day(self.today), {self.today: 9.0})
        self.assertEqual(
            db.dumate_expense_by_day(self.yesterday, self.today),
            {self.yesterday: 5.0, self.today: 9.0})
        self.assertEqual(db.dumate_expense_by_hour(self.today), {9: 5.0, 18: 4.0})
        # 昨天那条在本地 23:00（today 的 0 点前 1 小时），按小时口径归昨天。
        self.assertEqual(db.dumate_expense_by_hour(self.yesterday), {23: 5.0})


if __name__ == '__main__':
    unittest.main()