"""Trae 账号的 token 自动续期（`services/renew.refresh_trae_all`）。

为什么单独一组：Trae 的续期路径与 WorkBuddy **完全不是一套**——账号在
`data/platforms/trae/accounts/*.json`，续期必须走适配器（串行锁 + 原子落盘 +
回写 IDE），不能像 WorkBuddy 那样直接调 `tencent.refresh_token`。这条路径此前
只有实现没有测试，改坏的表现很隐蔽：账号在到期前几小时静默掉线，而巡检日志
里什么都看不出来。

这里用假适配器锁住**判据**：进窗口才刷、force 无条件刷、缺 refreshToken 记失败、
解不出到期时间不臆测，以及「立即执行一次」确实把平台段并进来。
"""
from __future__ import annotations

import asyncio
import time
import unittest
from unittest import mock

from server.services import renew


def _iso(offset_seconds: int) -> str:
    """当前时间 + offset 的 ISO8601（UTC，秒级）。"""
    return time.strftime('%Y-%m-%dT%H:%M:%SZ',
                         time.gmtime(time.time() + offset_seconds))


class _FakeAdapter:
    def __init__(self, accounts: list[dict]) -> None:
        self.accounts = accounts
        self.refreshed: list[tuple[str, bool]] = []

    def list_accounts(self) -> list[dict]:
        return [dict(a) for a in self.accounts]

    async def refresh_account(self, uid: str, *, force: bool = False) -> dict:
        self.refreshed.append((uid, force))
        return {'uid': uid}


class TraeRenewTest(unittest.TestCase):

    def setUp(self) -> None:
        self.notes: list[tuple[str, str]] = []
        self.records: list[tuple] = []
        self._patches = [
            mock.patch.object(renew, '_note_failure',
                              lambda name, msg: self.notes.append((name, msg))),
            mock.patch.object(renew, '_record',
                              lambda *args, **kwargs: self.records.append(args)),
        ]
        for patch in self._patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in self._patches:
            patch.stop()

    def _run(self, accounts: list[dict], *, force: bool = False) -> tuple[_FakeAdapter, dict]:
        adapter = _FakeAdapter(accounts)
        with mock.patch.object(renew, '_trae_adapter', lambda: adapter):
            stats = asyncio.run(renew.refresh_trae_all(force=force))
        return adapter, stats

    def test_in_window_is_renewed(self) -> None:
        adapter, stats = self._run([
            {'uid': 'a', 'has_refresh_token': True, 'expiredAt': _iso(3600)},
        ])
        self.assertEqual(adapter.refreshed, [('a', True)])
        self.assertEqual(stats, {'checked': 1, 'renewed': 1,
                                 'failed': 0, 'skipped': 0})
        self.assertTrue(self.records, '续期成功要写一条任务记录，用户才能在任务页看到')

    def test_fresh_account_is_skipped(self) -> None:
        """还剩 30 天：不在 3 天窗口内，一次都不该打上游。"""
        adapter, stats = self._run([
            {'uid': 'a', 'has_refresh_token': True, 'expiredAt': _iso(30 * 86400)},
        ])
        self.assertEqual(adapter.refreshed, [])
        self.assertEqual(stats['renewed'], 0)

    def test_force_ignores_remaining_lifetime(self) -> None:
        """「每 N 天全量刷一次」：剩 30 天也照刷。"""
        adapter, stats = self._run([
            {'uid': 'a', 'has_refresh_token': True, 'expiredAt': _iso(30 * 86400)},
        ], force=True)
        self.assertEqual(adapter.refreshed, [('a', True)])
        self.assertEqual(stats['renewed'], 1)

    def test_missing_refresh_token_is_a_failure(self) -> None:
        adapter, stats = self._run([
            {'uid': 'a', 'has_refresh_token': False, 'expiredAt': _iso(60)},
        ])
        self.assertEqual(adapter.refreshed, [])
        self.assertEqual(stats['failed'], 1)
        self.assertEqual(stats['checked'], 0)
        self.assertTrue(self.notes, '失败要留痕，否则用户不知道号为什么掉线')

    def test_unparseable_expiry_is_skipped_not_guessed(self) -> None:
        """解不得到期时间时不去轮换：避免每小时对一批「不知道何时过期」的号白跑。"""
        adapter, stats = self._run([
            {'uid': 'a', 'has_refresh_token': True, 'expiredAt': ''},
        ])
        self.assertEqual(adapter.refreshed, [])
        self.assertEqual(stats['skipped'], 1)
        self.assertEqual(stats['checked'], 0)

    def test_adapter_absent_returns_empty_stats(self) -> None:
        with mock.patch.object(renew, '_trae_adapter', lambda: None):
            stats = asyncio.run(renew.refresh_trae_all())
        self.assertEqual(stats, {'checked': 0, 'renewed': 0,
                                 'failed': 0, 'skipped': 0})

    def test_refresh_exception_counts_as_failure(self) -> None:
        class Boom(_FakeAdapter):
            async def refresh_account(self, uid: str, *, force: bool = False) -> dict:
                raise RuntimeError('上游 500')

        adapter = Boom([{'uid': 'a', 'has_refresh_token': True,
                         'expiredAt': _iso(60)}])
        with mock.patch.object(renew, '_trae_adapter', lambda: adapter):
            stats = asyncio.run(renew.refresh_trae_all())
        self.assertEqual(stats['failed'], 1)
        self.assertEqual(stats['renewed'], 0)


class RefreshAllPlatformsTest(unittest.TestCase):
    """「立即执行一次」必须同时刷 WorkBuddy 与平台账号。"""

    def test_merges_workbuddy_and_trae(self) -> None:
        adapter = _FakeAdapter([{'uid': 't1', 'has_refresh_token': True,
                                 'expiredAt': _iso(60)}])

        async def fake_main(force: bool = False, now=None) -> dict:
            return {'checked': 2, 'renewed': 2, 'failed': 0, 'skipped': 0}

        with mock.patch.object(renew, 'refresh_all', fake_main), \
             mock.patch.object(renew, '_trae_adapter', lambda: adapter), \
             mock.patch.object(renew, '_qoder_adapter', lambda: None), \
             mock.patch.object(renew, '_note_failure', lambda *a, **k: None), \
             mock.patch.object(renew, '_record', lambda *a, **k: None):
            stats = asyncio.run(renew.refresh_all_platforms(force=True))

        self.assertEqual(stats['checked'], 3)
        self.assertEqual(stats['renewed'], 3)
        self.assertEqual(stats['platforms']['trae']['renewed'], 1)
        # 没有 Qoder 账号 / 适配器时平台段照样出现在明细里（数字为 0），
        # 界面才能说明「这一轮把所有平台都看过了」。
        self.assertEqual(stats['platforms']['qoder']['renewed'], 0)
        self.assertEqual(stats['platforms']['qoder']['checked'], 0)

    def test_trae_failure_does_not_swallow_workbuddy_result(self) -> None:
        async def fake_main(force: bool = False, now=None) -> dict:
            return {'checked': 2, 'renewed': 2, 'failed': 0, 'skipped': 0}

        def boom() -> None:
            raise RuntimeError('平台层炸了')

        with mock.patch.object(renew, 'refresh_all', fake_main), \
             mock.patch.object(renew, '_trae_adapter', boom), \
             mock.patch.object(renew, '_qoder_adapter', lambda: None):
            stats = asyncio.run(renew.refresh_all_platforms(force=True))

        self.assertEqual(stats['renewed'], 2, '平台段异常不能吞掉 WorkBuddy 的结果')
        self.assertEqual(stats['platforms']['trae']['renewed'], 0)


if __name__ == '__main__':
    unittest.main()
