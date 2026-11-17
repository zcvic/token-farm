"""平台推理并发闸门：设置口径是**单账号**，不是整池。

## 钉住的问题

`platforms.<name>.max_in_flight` 以前表示「该平台所有账号合计的在途上限」。
账号一多，这个数字要么把整池卡死（2 个号各 1 条都跑不满），要么得跟着账号
数一起改。现在它表示**每个账号**的上限：

  · `AccountLimiter`（Trae）：两个账号各自一把信号量，互不挤占；
  · `PoolLimiter`（Qoder）：内置网关自己选号，总闸门 = 单账号上限 × 可用
    账号数，账号数变化时按新值重建。

0 仍然表示「不限」——默认值不变，老部署升级后行为一致。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db  # noqa: E402
from server.platforms import limiter  # noqa: E402


class _DbCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'limiter.db'
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
    def test_default_is_unlimited(self) -> None:
        self.assertEqual(limiter.load_limit('trae'), 0)

    def test_save_and_read_back(self) -> None:
        self.assertEqual(limiter.save_limit('trae', 3), 3)
        self.assertEqual(limiter.load_limit('trae'), 3)
        # 按平台分开：改 Trae 不影响 Qoder。
        self.assertEqual(limiter.load_limit('qoder'), 0)

    def test_rejects_invalid_values(self) -> None:
        for bad in (-1, limiter.LIMIT_MAX + 1, True, 'x', None):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    limiter.save_limit('trae', bad)

    def test_broken_stored_value_falls_back_to_unlimited(self) -> None:
        db.set_setting('platforms.trae.max_in_flight', 'many')
        self.assertEqual(limiter.load_limit('trae'), 0)
        db.set_setting('platforms.trae.max_in_flight', 999)
        self.assertEqual(limiter.load_limit('trae'), limiter.LIMIT_MAX)


class AccountLimiterTest(_DbCase):
    def test_each_account_has_its_own_slot(self) -> None:
        limiter.save_limit('trae', 1)
        gate = limiter.AccountLimiter('trae')

        async def scenario() -> None:
            first = await gate.acquire('uid-a')
            self.assertIsNotNone(first)
            # 另一个账号不受影响：这是「单账号」而不是「整池」的核心断言。
            other = await gate.acquire('uid-b')
            self.assertIsNotNone(other)
            # 同一个账号的第二个请求要等名额。
            self.assertIs(await gate.acquire('uid-a', timeout=0.01), False)
            gate.release(first)
            again = await gate.acquire('uid-a', timeout=0.5)
            self.assertIsNotNone(again)
            gate.release(again)
            gate.release(other)

        asyncio.run(scenario())

    def test_zero_means_unlimited(self) -> None:
        gate = limiter.AccountLimiter('trae')

        async def scenario() -> None:
            for _ in range(5):
                self.assertIsNone(await gate.acquire('uid-a'))

        asyncio.run(scenario())

    def test_invalidate_applies_new_limit_immediately(self) -> None:
        gate = limiter.AccountLimiter('trae')
        self.assertEqual(gate.limit(), 0)
        limiter.save_limit('trae', 2)
        # 缓存未过期时仍是旧值；路由保存后会调 invalidate。
        self.assertEqual(gate.limit(), 0)
        gate.invalidate()
        self.assertEqual(gate.limit(), 2)

    def test_raising_limit_rebuilds_semaphore(self) -> None:
        limiter.save_limit('trae', 1)
        gate = limiter.AccountLimiter('trae')

        async def scenario() -> None:
            first = await gate.acquire('uid-a')
            self.assertIsNotNone(first)
            limiter.save_limit('trae', 2)
            gate.invalidate()
            # 新上限立刻生效：不用等旧名额释放。
            second = await gate.acquire('uid-a', timeout=0.5)
            self.assertIsNotNone(second)
            gate.release(first)
            gate.release(second)

        asyncio.run(scenario())


class PoolLimiterTest(_DbCase):
    def test_total_is_per_account_times_account_count(self) -> None:
        limiter.save_limit('qoder', 2)
        gate = limiter.PoolLimiter('qoder', scale=lambda: 3)
        self.assertEqual(gate.limit(), 6)

    def test_scale_failure_falls_back_to_one_account(self) -> None:
        limiter.save_limit('qoder', 2)

        def broken() -> int:
            raise RuntimeError('gateway down')

        gate = limiter.PoolLimiter('qoder', scale=broken)
        self.assertEqual(gate.limit(), 2)

    def test_scale_zero_is_treated_as_one_account(self) -> None:
        limiter.save_limit('qoder', 4)
        gate = limiter.PoolLimiter('qoder', scale=lambda: 0)
        self.assertEqual(gate.limit(), 4)

    def test_unlimited_ignores_scale(self) -> None:
        gate = limiter.PoolLimiter('qoder', scale=lambda: 10)
        self.assertEqual(gate.limit(), 0)

    def test_per_account_reports_setting_not_total(self) -> None:
        """调用方要靠它判断「要不要算账号数」：必须给单账号值，不能给总闸门。"""
        limiter.save_limit('qoder', 3)
        gate = limiter.PoolLimiter('qoder', scale=lambda: 5)
        self.assertEqual(gate.per_account(), 3)
        self.assertEqual(gate.limit(), 15)

    def test_acquire_blocks_after_total_slots(self) -> None:
        limiter.save_limit('qoder', 1)
        gate = limiter.PoolLimiter('qoder', scale=lambda: 2)

        async def scenario() -> None:
            first = await gate.acquire()
            second = await gate.acquire()
            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            self.assertIs(await gate.acquire(timeout=0.01), False)
            gate.release(first)
            third = await gate.acquire(timeout=0.5)
            self.assertIsNotNone(third)
            gate.release(second)
            gate.release(third)

        asyncio.run(scenario())


if __name__ == '__main__':
    unittest.main()
