"""Cline 账号的 token 自动续期（`services/renew.refresh_cline_all`）。

为什么单独一组：Cline 与 MiniMax 都是「一次性 refresh token」，但判据不同 ——
上游**不给** refresh token 的寿命，`refreshExpiredAt` 基本永远是空的。所以：

  · `force=False` 时一律跳过（解不出到期时间就不臆测，与 MiniMax 同一条口径）；
  · 只有「令牌刷新计划」（默认每 5 天 `force=True`）才真正轮换；
  · 缺 refreshToken 记失败、停用号跳过、仅签到号照常续期；
  · 续期必须走适配器（账号级锁 + 先落盘再使用都在适配器里）。

若哪天有人为了「让巡检看起来在工作」把 `force=False` 改成无条件刷新，这里会
立刻变红：按小时轮换一次性 token 只会放大「上游已换发、本地还没落盘」的丢链
窗口，而且每个号每天白打 24 次上游。
"""
from __future__ import annotations

import asyncio
import time
import unittest
from unittest import mock

from server.services import renew


def _iso(offset_seconds: int) -> str:
    """当前时间 + offset 的 ISO8601（UTC，秒级）—— 与 store 的输出同形。"""
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


class ClineRenewTest(unittest.TestCase):
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

    def _run(self, accounts: list[dict], *, force: bool = False):
        adapter = _FakeAdapter(accounts)
        with mock.patch.object(renew, '_cline_adapter', lambda: adapter):
            stats = asyncio.run(renew.refresh_cline_all(force=force))
        return adapter, stats

    def test_missing_expiry_is_skipped_when_not_forced(self) -> None:
        """上游不给 refresh token 寿命：不臆测，交给刷新计划。"""
        adapter, stats = self._run([
            {'uid': 'c1', 'has_refresh_token': True, 'refreshExpiredAt': ''},
        ])
        self.assertEqual(adapter.refreshed, [])
        self.assertEqual(stats['skipped'], 1)
        self.assertEqual(stats['checked'], 0)

    def test_force_does_not_require_an_expiry(self) -> None:
        adapter, stats = self._run([
            {'uid': 'c1', 'has_refresh_token': True, 'refreshExpiredAt': ''},
        ], force=True)
        self.assertEqual(adapter.refreshed, [('c1', True)])
        self.assertEqual(stats['renewed'], 1)
        self.assertTrue(self.records, '续期成功要写任务记录，用户才能在任务页看到')

    def test_in_window_is_renewed(self) -> None:
        """万一上游开始回寿命：进 3 天窗口就该轮换。"""
        adapter, stats = self._run([
            {'uid': 'c1', 'has_refresh_token': True,
             'refreshExpiredAt': _iso(3600)},
        ])
        self.assertEqual(adapter.refreshed, [('c1', True)])
        self.assertEqual(stats, {'checked': 1, 'renewed': 1,
                                 'failed': 0, 'skipped': 0})

    def test_fresh_token_is_skipped(self) -> None:
        adapter, stats = self._run([
            {'uid': 'c1', 'has_refresh_token': True,
             'refreshExpiredAt': _iso(30 * 86400)},
        ])
        self.assertEqual(adapter.refreshed, [])
        self.assertEqual(stats['renewed'], 0)
        self.assertEqual(stats['skipped'], 0, '不在窗口里既不是跳过也不是失败')

    def test_short_lived_access_token_alone_is_not_a_reason(self) -> None:
        """access token 只剩 60 秒不算理由：适配器每次使用前自己换。"""
        adapter, stats = self._run([
            {'uid': 'c1', 'has_refresh_token': True,
             'expiredAt': _iso(60), 'refreshExpiredAt': _iso(30 * 86400)},
        ])
        self.assertEqual(adapter.refreshed, [])
        self.assertEqual(stats['checked'], 0)

    def test_disabled_account_is_skipped(self) -> None:
        adapter, stats = self._run([
            {'uid': 'c1', 'has_refresh_token': True, 'disabled': True,
             'refreshExpiredAt': _iso(60)},
        ])
        self.assertEqual(adapter.refreshed, [])
        self.assertEqual(stats['skipped'], 1)

    def test_checkin_only_account_is_still_renewed(self) -> None:
        adapter, stats = self._run([
            {'uid': 'c1', 'has_refresh_token': True, 'checkin_only': True,
             'refreshExpiredAt': _iso(60)},
        ])
        self.assertEqual(adapter.refreshed, [('c1', True)])
        self.assertEqual(stats['renewed'], 1)

    def test_missing_refresh_token_is_a_failure(self) -> None:
        adapter, stats = self._run([
            {'uid': 'c1', 'has_refresh_token': False, 'refreshExpiredAt': ''},
        ], force=True)
        self.assertEqual(adapter.refreshed, [])
        self.assertEqual(stats['failed'], 1)
        self.assertEqual(stats['checked'], 0)
        self.assertTrue(self.notes, '失败要留痕，否则用户不知道号为什么掉线')

    def test_adapter_absent_returns_empty_stats(self) -> None:
        with mock.patch.object(renew, '_cline_adapter', lambda: None):
            stats = asyncio.run(renew.refresh_cline_all())
        self.assertEqual(stats, {'checked': 0, 'renewed': 0,
                                 'failed': 0, 'skipped': 0})

    def test_refresh_exception_counts_as_failure(self) -> None:
        class Boom(_FakeAdapter):
            async def refresh_account(self, uid: str, *,
                                      force: bool = False) -> dict:
                raise RuntimeError('上游 502')

        adapter = Boom([{'uid': 'c1', 'has_refresh_token': True,
                         'refreshExpiredAt': _iso(60)}])
        with mock.patch.object(renew, '_cline_adapter', lambda: adapter):
            stats = asyncio.run(renew.refresh_cline_all())
        self.assertEqual(stats['failed'], 1)
        self.assertEqual(stats['renewed'], 0)

    def test_window_matches_the_shared_threshold(self) -> None:
        """沿用 WorkBuddy / MiniMax 的 3 天窗口，不另立一套数字。"""
        self.assertEqual(renew._MINIMAX_RENEW_BEFORE_SECONDS, 3 * 86400)


if __name__ == '__main__':
    unittest.main()
