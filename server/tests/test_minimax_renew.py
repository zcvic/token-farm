"""MiniMax Code 账号的 token 自动续期（`services/renew.refresh_minimax_all`）。

为什么单独一组：MiniMax 的判据与前两个平台都不同——它看的是 **refresh token**
的到期时间（`refreshExpiredAt`），不是 access token 的 `expiredAt`。

access token 只有约 1 小时，适配器每次使用前会自己换；拿它当判据会让每个账号
**每一轮巡检**都落在窗口里，等于每小时轮换一遍一次性 refresh token（上游风控 +
「换发后没落盘」的丢链窗口都会被放大）。所以这里要钉住：

  · 只看 refresh token 的寿命，access token 快过期不算理由；
  · 进 3 天窗口才刷、force 无条件刷；
  · 解不出 refresh 到期时间就跳过，不臆测；
  · 缺 refreshToken 记失败，停用号跳过；
  · 续期必须走适配器（一次性令牌的落盘顺序在适配器里）。
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
        self.listed = 0

    def list_accounts(self) -> list[dict]:
        self.listed += 1
        return [dict(a) for a in self.accounts]

    async def refresh_account(self, uid: str, *, force: bool = False) -> dict:
        self.refreshed.append((uid, force))
        return {'uid': uid}


class MiniMaxRenewTest(unittest.TestCase):

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
        with mock.patch.object(renew, '_minimax_adapter', lambda: adapter):
            stats = asyncio.run(renew.refresh_minimax_all(force=force))
        return adapter, stats

    def test_in_window_is_renewed(self) -> None:
        adapter, stats = self._run([
            {'uid': 'm1', 'has_refresh_token': True,
             'refreshExpiredAt': _iso(3600)},
        ])
        self.assertEqual(adapter.refreshed, [('m1', True)])
        self.assertEqual(stats, {'checked': 1, 'renewed': 1,
                                 'failed': 0, 'skipped': 0})
        self.assertTrue(self.records, '续期成功要写一条任务记录，用户才能在任务页看到')

    def test_fresh_refresh_token_is_skipped(self) -> None:
        """refresh token 还剩 30 天：一次都不该打上游。"""
        adapter, stats = self._run([
            {'uid': 'm1', 'has_refresh_token': True,
             'refreshExpiredAt': _iso(30 * 86400)},
        ])
        self.assertEqual(adapter.refreshed, [])
        self.assertEqual(stats['renewed'], 0)

    def test_short_lived_access_token_alone_is_not_a_reason(self) -> None:
        """access token 只剩 60 秒也不算：那是适配器每次使用前自己处理的事。

        拿它当判据等于每小时轮换一遍一次性 refresh token，见模块文档。
        """
        adapter, stats = self._run([
            {'uid': 'm1', 'has_refresh_token': True,
             'expiredAt': _iso(60),
             'refreshExpiredAt': _iso(30 * 86400)},
        ])
        self.assertEqual(adapter.refreshed, [])
        self.assertEqual(stats['checked'], 0)

    def test_window_is_day_scale(self) -> None:
        """判的是 refresh token 的寿命（天级），沿用 WorkBuddy 的 3 天窗口。"""
        self.assertEqual(renew._MINIMAX_RENEW_BEFORE_SECONDS, 3 * 86400)

    def test_force_ignores_remaining_lifetime(self) -> None:
        """「每 N 天全量刷一次」：剩 30 天也照刷。"""
        adapter, stats = self._run([
            {'uid': 'm1', 'has_refresh_token': True,
             'refreshExpiredAt': _iso(30 * 86400)},
        ], force=True)
        self.assertEqual(adapter.refreshed, [('m1', True)])
        self.assertEqual(stats['renewed'], 1)

    def test_force_still_requires_a_refresh_token(self) -> None:
        """全量刷也不能碰没有 refreshToken 的号：它只能重新登录。"""
        adapter, stats = self._run([
            {'uid': 'm1', 'has_refresh_token': False, 'refreshExpiredAt': ''},
        ], force=True)
        self.assertEqual(adapter.refreshed, [])
        self.assertEqual(stats['failed'], 1)
        self.assertTrue(self.notes)

    def test_disabled_account_is_skipped(self) -> None:
        adapter, stats = self._run([
            {'uid': 'm1', 'has_refresh_token': True, 'disabled': True,
             'refreshExpiredAt': _iso(60)},
        ])
        self.assertEqual(adapter.refreshed, [])
        self.assertEqual(stats['skipped'], 1)

    def test_checkin_only_account_is_still_renewed(self) -> None:
        """仅签到只摘对话流量，凭证必须保持活跃（与 WorkBuddy 同一口径）。"""
        adapter, stats = self._run([
            {'uid': 'm1', 'has_refresh_token': True, 'checkin_only': True,
             'refreshExpiredAt': _iso(60)},
        ])
        self.assertEqual(adapter.refreshed, [('m1', True)])
        self.assertEqual(stats['renewed'], 1)

    def test_missing_refresh_token_is_a_failure(self) -> None:
        adapter, stats = self._run([
            {'uid': 'm1', 'has_refresh_token': False,
             'refreshExpiredAt': _iso(60)},
        ])
        self.assertEqual(adapter.refreshed, [])
        self.assertEqual(stats['failed'], 1)
        self.assertEqual(stats['checked'], 0)
        self.assertTrue(self.notes, '失败要留痕，否则用户不知道号为什么掉线')

    def test_unparseable_expiry_is_skipped_not_guessed(self) -> None:
        """设备码登录拿不到 refresh 寿命：不臆测，交给刷新计划与「刷新」按钮。"""
        adapter, stats = self._run([
            {'uid': 'm1', 'has_refresh_token': True,
             'expiredAt': _iso(60), 'refreshExpiredAt': ''},
        ])
        self.assertEqual(adapter.refreshed, [])
        self.assertEqual(stats['skipped'], 1)
        self.assertEqual(stats['checked'], 0)

    def test_force_does_not_require_an_expiry(self) -> None:
        """刷新计划的意义是「不看剩多少天」：缺到期时间反而更需要它。"""
        adapter, stats = self._run([
            {'uid': 'm1', 'has_refresh_token': True, 'refreshExpiredAt': ''},
        ], force=True)
        self.assertEqual(adapter.refreshed, [('m1', True)])
        self.assertEqual(stats['renewed'], 1)

    def test_adapter_absent_returns_empty_stats(self) -> None:
        with mock.patch.object(renew, '_minimax_adapter', lambda: None):
            stats = asyncio.run(renew.refresh_minimax_all())
        self.assertEqual(stats, {'checked': 0, 'renewed': 0,
                                 'failed': 0, 'skipped': 0})

    def test_refresh_exception_counts_as_failure(self) -> None:
        class Boom(_FakeAdapter):
            async def refresh_account(self, uid: str, *, force: bool = False) -> dict:
                raise RuntimeError('上游 502')

        adapter = Boom([{'uid': 'm1', 'has_refresh_token': True,
                         'refreshExpiredAt': _iso(60)}])
        with mock.patch.object(renew, '_minimax_adapter', lambda: adapter):
            stats = asyncio.run(renew.refresh_minimax_all())
        self.assertEqual(stats['failed'], 1)
        self.assertEqual(stats['renewed'], 0)


if __name__ == '__main__':
    unittest.main()
