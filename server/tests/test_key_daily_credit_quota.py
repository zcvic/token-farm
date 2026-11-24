"""密钥每日积分限额：与积分总额度各自独立，按本地日期计，跨天自动恢复。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db, keysvc  # noqa: E402


class KeyDailyCreditQuotaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        d = Path(cls._tmp.name)
        cls._orig = (config.DB_PATH, config.USERS_FILE, config.STATIC_DIR)
        config.DB_PATH = d / 'k.db'
        config.USERS_FILE = d / 'users.json'
        config.STATIC_DIR = d / 'no-static'
        config.USERS_FILE.write_text(json.dumps({
            'secret': 'S' * 64,
            'users': [{'username': 'admin', 'role': 'admin', 'pwd_hash': 'x'}],
            'api_keys': [],
        }), encoding='utf-8')
        db._conn = None
        db.connect()

    @classmethod
    def tearDownClass(cls) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH, config.USERS_FILE, config.STATIC_DIR = cls._orig
        try:
            cls._tmp.cleanup()
        except PermissionError:
            pass

    def _fresh(self, token: str) -> dict:
        key = keysvc.resolve(token)
        self.assertIsNotNone(key)
        return key

    def test_default_is_unlimited(self) -> None:
        made = keysvc.create_key('daily-credit-default')
        self.assertEqual(made['daily_quota_credit'], 0)
        self.assertEqual(made['daily_used_credit'], 0)
        key = self._fresh(made['key'])
        keysvc.touch(key, '1.1.1.1', credit=999.0)
        self.assertIsNone(keysvc.validate(self._fresh(made['key']), '1.1.1.1', 'm'))

    def test_blocks_after_daily_limit_and_resets_next_day(self) -> None:
        made = keysvc.create_key('daily-credit', daily_quota_credit=0.5)
        token = made['key']
        with mock.patch.object(db, 'day_of', return_value='2026-11-13'):
            keysvc.touch(self._fresh(token), '1.1.1.1', credit=0.3)
            key = self._fresh(token)
            self.assertAlmostEqual(key['daily_used_credit'], 0.3, places=6)
            self.assertIsNone(keysvc.validate(key, '1.1.1.1', 'm'))
            keysvc.touch(key, '1.1.1.1', credit=0.25)
            key = self._fresh(token)
            self.assertAlmostEqual(key['daily_used_credit'], 0.55, places=6)
            rej = keysvc.validate(key, '1.1.1.1', 'm')
            self.assertIsNotNone(rej)
            self.assertEqual(rej.status, 429)
            self.assertEqual(rej.code, 'daily_credit_quota_exhausted')
        with mock.patch.object(db, 'day_of', return_value='2026-11-14'):
            key = self._fresh(token)
            self.assertEqual(key['daily_used_credit'], 0.0)
            self.assertIsNone(keysvc.validate(key, '1.1.1.1', 'm'))
            keysvc.touch(key, '1.1.1.1', credit=0.1)
            self.assertAlmostEqual(self._fresh(token)['daily_used_credit'], 0.1, places=6)
            # 总用量照常累计，不受每日归零影响
            self.assertAlmostEqual(self._fresh(token)['used_credit'], 0.65, places=6)

    def test_token_only_day_does_not_reset_credit_day(self) -> None:
        """只有 token、没有积分的调用不能把积分当日累计冲掉。

        两笔用量分别记账（daily_used_day / daily_credit_day）：若共用日期列，
        一次「只记 token」的调用会在换日时把积分累计错误地重置成 0。
        """
        made = keysvc.create_key('daily-credit-split', daily_quota_credit=1.0)
        token = made['key']
        with mock.patch.object(db, 'day_of', return_value='2026-11-13'):
            keysvc.touch(self._fresh(token), '1.1.1.1', tokens=10, credit=0.4)
            self.assertAlmostEqual(self._fresh(token)['daily_used_credit'], 0.4, places=6)
        with mock.patch.object(db, 'day_of', return_value='2026-11-14'):
            # 只有 token：积分那天没有真实扣费，保持 0
            keysvc.touch(self._fresh(token), '1.1.1.1', tokens=10)
            self.assertEqual(self._fresh(token)['daily_used_credit'], 0.0)
            keysvc.touch(self._fresh(token), '1.1.1.1', credit=0.2)
            self.assertAlmostEqual(self._fresh(token)['daily_used_credit'], 0.2, places=6)

    def test_update_and_reset(self) -> None:
        made = keysvc.create_key('daily-credit-upd')
        updated = keysvc.update_key(made['id'], {'daily_quota_credit': 0.5})
        self.assertAlmostEqual(updated['daily_quota_credit'], 0.5, places=6)
        self.assertEqual(
            keysvc.update_key(made['id'], {'daily_quota_credit': -3})['daily_quota_credit'], 0)
        keysvc.update_key(made['id'], {'daily_quota_credit': 0.5})
        keysvc.touch(self._fresh(made['key']), '1.1.1.1', credit=0.8)
        self.assertIsNotNone(keysvc.validate(self._fresh(made['key']), '1.1.1.1', 'm'))
        self.assertTrue(keysvc.reset_usage(made['id']))
        self.assertIsNone(keysvc.validate(self._fresh(made['key']), '1.1.1.1', 'm'))


if __name__ == '__main__':
    unittest.main()