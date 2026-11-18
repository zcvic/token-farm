"""平台（Trae / Qoder）积分快照。

背景：WorkBuddy 的余额一直有落库快照（`credits_snapshot`，供看板 / 对账与
「积分收入」比对），Trae / Qoder 此前没有——Trae 只有 60 秒内存缓存，Qoder 的
快照在内置网关的账号文件里，面板侧读不到，重启或查询失败后只能显示 `—`。

这里钉住四件事：
  1. 平台快照与 WorkBuddy 快照**分开存**：uid 命名空间不同，混在一起会让
     WorkBuddy 的余额合计与收入比对把平台余额也算进去；
  2. 每个平台之间也互不覆盖（同名 uid 在两个平台各记各的）；
  3. 实时查询失败时能读回上次登记值，并带登记时刻（界面据此标成旧值）；
  4. 删号后不再下发该账号的登记值。
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
from server.platforms.base import PlatformError  # noqa: E402
from server.routers import platforms as platforms_router  # noqa: E402
from server.services import credits as creditsvc  # noqa: E402


class _DbCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        config.DB_PATH = Path(self._tmp.name) / 'm.db'
        db._conn = None
        db.connect()
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        self._tmp.cleanup()


class RecordPlatformBalanceTest(_DbCase):
    def test_stores_value_and_time(self) -> None:
        before = int(time.time())
        at = creditsvc.record_platform_balance('trae', 'u1', 4700)
        self.assertIsNotNone(at)
        self.assertGreaterEqual(at, before)
        self.assertEqual(creditsvc.platform_snapshot_entry('trae', 'u1'),
                         {'uid': 'u1', 'credits': 4700, 'registered_at': at})
        entries = creditsvc.platform_snapshot_entries('trae')
        self.assertEqual(entries['platform'], 'trae')
        self.assertEqual(entries['snapshot_at'], at)
        self.assertEqual(entries['accounts'],
                         [{'uid': 'u1', 'credits': 4700, 'registered_at': at}])

    def test_does_not_leak_into_workbuddy_snapshot(self) -> None:
        creditsvc.record_platform_balance('qoder', 'same-uid', 200)
        self.assertEqual(creditsvc.snapshot_entries()['accounts'], [],
                         '平台余额不能进 WorkBuddy 的余额合计')
        creditsvc.record_balance('same-uid', '同名', 900)
        self.assertEqual(creditsvc.snapshot_entries()['accounts'][0]['credits'], 900)
        self.assertEqual(creditsvc.platform_snapshot_entry('qoder', 'same-uid')['credits'],
                         200, '两边各记各的，互相不覆盖')

    def test_platforms_are_isolated(self) -> None:
        creditsvc.record_platform_balance('trae', 'u1', 10)
        creditsvc.record_platform_balance('qoder', 'u1', 20)
        self.assertEqual(
            creditsvc.platform_snapshot_entries('trae')['accounts'][0]['credits'], 10)
        self.assertEqual(
            creditsvc.platform_snapshot_entries('qoder')['accounts'][0]['credits'], 20)

    def test_decimal_kept_and_invalid_ignored(self) -> None:
        """国际版额度是美元（0.75 不能截成 0），脏值不登记。"""
        creditsvc.record_platform_balance('trae', 'intl', 0.75)
        self.assertEqual(creditsvc.platform_snapshot_entry('trae', 'intl')['credits'], 0.75)
        for value in (None, 'nope', float('nan')):
            self.assertIsNone(creditsvc.record_platform_balance('trae', 'bad', value))
        self.assertIsNone(creditsvc.platform_snapshot_entry('trae', 'bad'))
        self.assertIsNone(creditsvc.record_platform_balance('', 'u1', 1))
        self.assertIsNone(creditsvc.record_platform_balance('trae', '', 1))

    def test_forget_only_clears_that_platform(self) -> None:
        creditsvc.record_platform_balance('trae', 'u1', 10)
        creditsvc.record_platform_balance('qoder', 'u1', 20)
        creditsvc.forget_platform('trae', 'u1')
        self.assertIsNone(creditsvc.platform_snapshot_entry('trae', 'u1'))
        self.assertIsNotNone(creditsvc.platform_snapshot_entry('qoder', 'u1'))

    def test_batch_write_keeps_every_account(self) -> None:
        """一批账号一次读改写：不能出现后写的把先写的覆盖掉。"""
        at = creditsvc.record_platform_balances(
            'trae', {'u1': 1, 'u2': 2.5, 'bad': None, '': 9})
        self.assertIsNotNone(at)
        self.assertEqual(creditsvc.platform_snapshot_entries('trae')['accounts'], [
            {'uid': 'u1', 'credits': 1, 'registered_at': at},
            {'uid': 'u2', 'credits': 2.5, 'registered_at': at},
        ])
        self.assertIsNone(creditsvc.record_platform_balances('trae', {}))
        self.assertIsNone(creditsvc.record_platform_balances('', {'u1': 1}))


class _FakeAdapter:
    def __init__(self, items: dict, accounts: list[dict] | None = None) -> None:
        self._items = items
        self.accounts = accounts if accounts is not None else [
            {'uid': uid, 'nickname': uid, 'disabled': False} for uid in items]
        self.deleted: list[str] = []

    def list_accounts(self) -> list[dict]:
        return self.accounts

    async def credits(self, uid: str) -> dict:
        value = self._items[uid]
        if isinstance(value, Exception):
            raise value
        return value

    def delete_account(self, uid: str) -> bool:
        self.deleted.append(uid)
        return True


class PlatformCreditsRouteTest(_DbCase):
    def _run(self, fn, *args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    def test_success_registers_snapshot(self) -> None:
        adapter = _FakeAdapter({'u1': {'uid': 'u1', 'credits': 321}})
        with mock.patch.object(platforms_router, '_adapter', lambda name: adapter):
            out = self._run(platforms_router.list_credits, 'trae', user={})
        item = out['credits'][0]
        self.assertEqual(item['credits'], 321)
        self.assertIsInstance(item['registered_at'], int)
        self.assertEqual(creditsvc.platform_snapshot_entry('trae', 'u1')['credits'], 321)

    def test_batch_registers_every_account(self) -> None:
        """一次请求里的 N 个账号都要登记，失败的那条只带旧值、不写快照。"""
        adapter = _FakeAdapter({
            'u1': {'uid': 'u1', 'credits': 1},
            'u2': {'uid': 'u2', 'credits': 2},
            'u3': PlatformError('超时', status=504),
        })
        with mock.patch.object(platforms_router, '_adapter', lambda name: adapter):
            out = self._run(platforms_router.list_credits, 'trae', user={})
        by_uid = {item['uid']: item for item in out['credits']}
        self.assertEqual((by_uid['u1']['credits'], by_uid['u2']['credits']), (1, 2))
        self.assertEqual(by_uid['u1']['registered_at'], by_uid['u2']['registered_at'],
                         '同一批查询共用同一个登记时刻')
        self.assertNotIn('registered_at', by_uid['u3'])
        rows = {r['uid']: r['credits']
                for r in creditsvc.platform_snapshot_entries('trae')['accounts']}
        self.assertEqual(rows, {'u1': 1, 'u2': 2})

    def test_success_does_not_mutate_adapter_cache(self) -> None:
        """Trae 的 `credits()` 返回缓存里的同一个 dict，加字段前必须复制。"""
        cached = {'uid': 'u1', 'credits': 5}
        adapter = _FakeAdapter({'u1': cached})
        with mock.patch.object(platforms_router, '_adapter', lambda name: adapter):
            self._run(platforms_router.list_credits, 'trae', user={})
        self.assertNotIn('registered_at', cached)

    def test_failure_returns_last_recorded(self) -> None:
        creditsvc.record_platform_balance('trae', 'u1', 300)
        adapter = _FakeAdapter({'u1': PlatformError('令牌过期', status=401)})
        with mock.patch.object(platforms_router, '_adapter', lambda name: adapter):
            out = self._run(platforms_router.list_credits, 'trae', user={})
        item = out['credits'][0]
        self.assertNotIn('credits', item)
        self.assertEqual(item['error'], '令牌过期')
        self.assertEqual(item['last_credits'], 300)
        self.assertIsInstance(item['last_registered_at'], int)

    def test_adapter_error_body_returns_last_recorded(self) -> None:
        """Qoder 没有网关侧快照时是「正常返回 + error」，同样要退到面板登记值。"""
        creditsvc.record_platform_balance('qoder', 'u1', 88)
        adapter = _FakeAdapter({'u1': {'uid': 'u1', 'credits': None,
                                       'error': '尚无积分快照',
                                       'code': 'credits_missing'}})
        with mock.patch.object(platforms_router, '_adapter', lambda name: adapter):
            out = self._run(platforms_router.list_credits, 'qoder', user={})
        item = out['credits'][0]
        self.assertIsNone(item['credits'])
        self.assertEqual(item['last_credits'], 88)

    def test_snapshot_endpoint_filters_inactive(self) -> None:
        creditsvc.record_platform_balance('trae', 'a', 1)
        creditsvc.record_platform_balance('trae', 'b', 2)
        creditsvc.record_platform_balance('trae', 'gone', 3)
        adapter = _FakeAdapter({}, accounts=[
            {'uid': 'a', 'disabled': False},
            {'uid': 'b', 'disabled': True},
        ])
        with mock.patch.object(platforms_router, '_adapter', lambda name: adapter):
            body = platforms_router.platform_credits_snapshot(
                'trae', include_inactive=False, user={})
            all_rows = platforms_router.platform_credits_snapshot(
                'trae', include_inactive=True, user={})
        self.assertEqual([r['uid'] for r in body['accounts']], ['a'])
        self.assertTrue(body['accounts'][0]['active'])
        self.assertEqual(body['total_registered'], 3, '库里登记总数应如实给出')
        self.assertEqual([r['uid'] for r in all_rows['accounts']], ['a', 'b'])
        self.assertFalse(all_rows['accounts'][1]['active'])

    def test_snapshot_endpoint_survives_account_store_outage(self) -> None:
        """账号列表读不到时保留旧值（对账脚本正在这时候需要它），不报 500。"""
        creditsvc.record_platform_balance('qoder', 'a', 66)

        class _Boom(_FakeAdapter):
            def list_accounts(self) -> list[dict]:
                raise RuntimeError('网关没起来')

        with mock.patch.object(platforms_router, '_adapter', lambda name: _Boom({})):
            body = platforms_router.platform_credits_snapshot(
                'qoder', include_inactive=False, user={})
        self.assertEqual([r['uid'] for r in body['accounts']], ['a'])
        self.assertEqual(body['accounts_unavailable'], '网关没起来')

    def test_delete_account_forgets_snapshot(self) -> None:
        creditsvc.record_platform_balance('qoder', 'u1', 50)
        adapter = _FakeAdapter({})
        with mock.patch.object(platforms_router, '_adapter', lambda name: adapter):
            out = self._run(platforms_router.delete_account, 'qoder', 'u1', user={})
        self.assertEqual(out['deleted'], 'u1')
        self.assertIsNone(creditsvc.platform_snapshot_entry('qoder', 'u1'))


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
