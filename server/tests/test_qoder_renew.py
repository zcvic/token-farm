"""Qoder 账号的 token 自动续期（`services/renew.refresh_qoder_all`）。

为什么单独一组：Qoder 的账号在 `data/platforms/qoder/accounts/`，由内置网关
持有；续期必须走适配器（网关负责 token 落盘与 COSY 会话失效重建）。这条路径
与 Trae 相似但判据不同——**阈值是 6 小时不是 3 天**，因为它的 jobToken 寿命
只有约 24 小时，按天判会让每个刚签发的令牌立刻落进窗口。

这里用假适配器钉住判据：进窗口才刷、force 无条件刷、缺 refreshToken 记失败、
停用号跳过、解不出到期时间不臆测，以及「没有账号时不去拉起内置网关」。
"""
from __future__ import annotations

import asyncio
import time
import unittest
from unittest import mock

from server.services import renew


def _iso(offset_seconds: int) -> str:
    """当前时间 + offset 的 ISO8601（UTC，秒级）—— 与 mapping 的输出同形。"""
    return time.strftime('%Y-%m-%dT%H:%M:%SZ',
                         time.gmtime(time.time() + offset_seconds))


class _FakeAdapter:
    def __init__(self, accounts: list[dict], *, has_accounts: bool = True) -> None:
        self.accounts = accounts
        self.has_accounts = has_accounts
        self.refreshed: list[tuple[str, bool]] = []
        self.listed = 0

    def has_accounts_hint(self) -> bool:
        return self.has_accounts

    def list_accounts(self) -> list[dict]:
        self.listed += 1
        return [dict(a) for a in self.accounts]

    async def refresh_account(self, uid: str, *, force: bool = False) -> dict:
        self.refreshed.append((uid, force))
        return {'uid': uid}


class QoderRenewTest(unittest.TestCase):

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

    def _run(self, accounts: list[dict], *, force: bool = False,
             has_accounts: bool = True) -> tuple[_FakeAdapter, dict]:
        adapter = _FakeAdapter(accounts, has_accounts=has_accounts)
        with mock.patch.object(renew, '_qoder_adapter', lambda: adapter):
            stats = asyncio.run(renew.refresh_qoder_all(force=force))
        return adapter, stats

    def test_in_window_is_renewed(self) -> None:
        adapter, stats = self._run([
            {'uid': 'q1', 'has_refresh_token': True, 'expiredAt': _iso(3600)},
        ])
        self.assertEqual(adapter.refreshed, [('q1', True)])
        self.assertEqual(stats, {'checked': 1, 'renewed': 1,
                                 'failed': 0, 'skipped': 0})
        self.assertTrue(self.records, '续期成功要写一条任务记录，用户才能在任务页看到')

    def test_fresh_token_is_skipped(self) -> None:
        """还剩 20 小时：不在 6 小时窗口内，一次都不该打上游。"""
        adapter, stats = self._run([
            {'uid': 'q1', 'has_refresh_token': True,
             'expiredAt': _iso(20 * 3600)},
        ])
        self.assertEqual(adapter.refreshed, [])
        self.assertEqual(stats['renewed'], 0)

    def test_window_is_hours_scale(self) -> None:
        """阈值必须是小时量级：Qoder 的 jobToken 只有约 24 小时寿命。"""
        self.assertLess(renew._QODER_RENEW_BEFORE_SECONDS, 24 * 3600)
        self.assertGreaterEqual(renew._QODER_RENEW_BEFORE_SECONDS, 3600)

    def test_force_ignores_remaining_lifetime(self) -> None:
        """「每 N 天全量刷一次」：剩 20 小时也照刷。"""
        adapter, stats = self._run([
            {'uid': 'q1', 'has_refresh_token': True,
             'expiredAt': _iso(20 * 3600)},
        ], force=True)
        self.assertEqual(adapter.refreshed, [('q1', True)])
        self.assertEqual(stats['renewed'], 1)

    def test_never_expires_placeholder_is_skipped(self) -> None:
        """国内版用 9999-12-31 占位表示永不过期，不该被当成「已过期」。"""
        adapter, stats = self._run([
            {'uid': 'q1', 'has_refresh_token': True,
             'expiredAt': '9999-12-31T00:00:00+00:00'},
        ])
        self.assertEqual(adapter.refreshed, [])
        self.assertEqual(stats['checked'], 0)

    def test_disabled_account_is_skipped(self) -> None:
        adapter, stats = self._run([
            {'uid': 'q1', 'has_refresh_token': True, 'disabled': True,
             'expiredAt': _iso(60)},
        ])
        self.assertEqual(adapter.refreshed, [])
        self.assertEqual(stats['skipped'], 1)

    def test_missing_refresh_token_is_a_failure(self) -> None:
        adapter, stats = self._run([
            {'uid': 'q1', 'has_refresh_token': False, 'expiredAt': _iso(60)},
        ])
        self.assertEqual(adapter.refreshed, [])
        self.assertEqual(stats['failed'], 1)
        self.assertEqual(stats['checked'], 0)
        self.assertTrue(self.notes, '失败要留痕，否则用户不知道号为什么掉线')

    def test_unparseable_expiry_is_skipped_not_guessed(self) -> None:
        adapter, stats = self._run([
            {'uid': 'q1', 'has_refresh_token': True, 'expiredAt': ''},
        ])
        self.assertEqual(adapter.refreshed, [])
        self.assertEqual(stats['skipped'], 1)
        self.assertEqual(stats['checked'], 0)

    def test_no_local_accounts_does_not_touch_the_adapter(self) -> None:
        """没账号时连 `list_accounts()` 都不该调——那会把内置网关拉起来。"""
        adapter, stats = self._run([], has_accounts=False)
        self.assertEqual(adapter.listed, 0)
        self.assertEqual(stats, {'checked': 0, 'renewed': 0,
                                 'failed': 0, 'skipped': 0})

    def test_adapter_absent_returns_empty_stats(self) -> None:
        with mock.patch.object(renew, '_qoder_adapter', lambda: None):
            stats = asyncio.run(renew.refresh_qoder_all())
        self.assertEqual(stats, {'checked': 0, 'renewed': 0,
                                 'failed': 0, 'skipped': 0})

    def test_refresh_exception_counts_as_failure(self) -> None:
        class Boom(_FakeAdapter):
            async def refresh_account(self, uid: str, *, force: bool = False) -> dict:
                raise RuntimeError('网关 502')

        adapter = Boom([{'uid': 'q1', 'has_refresh_token': True,
                         'expiredAt': _iso(60)}])
        with mock.patch.object(renew, '_qoder_adapter', lambda: adapter):
            stats = asyncio.run(renew.refresh_qoder_all())
        self.assertEqual(stats['failed'], 1)
        self.assertEqual(stats['renewed'], 0)


class PlatformRenewersTest(unittest.TestCase):
    def test_qoder_is_in_the_shared_roster(self) -> None:
        names = [name for name, _label, _fn in renew._PLATFORM_RENEWERS]
        self.assertEqual(names, ['trae', 'qoder'],
                         '巡检循环 / 计划任务 / 立即执行都从这里取平台清单')


if __name__ == '__main__':
    unittest.main()
