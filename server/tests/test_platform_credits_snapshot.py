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
                         {'uid': 'u1', 'credits': 4700, 'registered_at': at,
                          'expires': [], 'next_expire_at': None})
        entries = creditsvc.platform_snapshot_entries('trae')
        self.assertEqual(entries['platform'], 'trae')
        self.assertEqual(entries['snapshot_at'], at)
        self.assertEqual(entries['accounts'],
                         [{'uid': 'u1', 'credits': 4700, 'registered_at': at,
                           'expires': [], 'next_expire_at': None}])

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

    def test_usd_unit_survives_the_snapshot(self) -> None:
        """美元余额要连单位一起登记：回退旧值时只交回数字，界面会按积分画。"""
        creditsvc.record_platform_balances('kilo', {'u1': {
            'uid': 'u1', 'credits': 12.34, 'unit': 'usd'}})
        entry = creditsvc.platform_snapshot_entry('kilo', 'u1')
        self.assertEqual(entry['credits'], 12.34)
        self.assertEqual(entry['unit'], 'usd')
        rows = creditsvc.platform_snapshot_entries('kilo')['accounts']
        self.assertEqual(rows[0]['unit'], 'usd')

    def test_credit_unit_is_not_written(self) -> None:
        """积分平台不写 `unit`：快照是对账用的，多一个恒为 credits 的字段
        只会让既有条目与外部脚本无谓地对不上。"""
        creditsvc.record_platform_balance('trae', 'u1', 4700)
        self.assertNotIn('unit', creditsvc.platform_snapshot_entry('trae', 'u1'))
        self.assertNotIn('unit', creditsvc.platform_snapshot_entries('trae')['accounts'][0])

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
            {'uid': 'u1', 'credits': 1, 'registered_at': at,
             'expires': [], 'next_expire_at': None},
            {'uid': 'u2', 'credits': 2.5, 'registered_at': at,
             'expires': [], 'next_expire_at': None},
        ])
        self.assertIsNone(creditsvc.record_platform_balances('trae', {}))
        self.assertIsNone(creditsvc.record_platform_balances('', {'u1': 1}))

    def test_batch_records_expiry_details(self) -> None:
        """快照不只要余额：查询失败回退旧值时，有效期也要跟着回来。"""
        future = int(time.time()) + 7 * 86400
        later = future + 86400
        at = creditsvc.record_platform_balances('trae', {'u1': {
            'uid': 'u1',
            'credits': 4700,
            'expires': [
                {'amount': 500, 'expire_at': later, 'label': '每月登录'},
                {'amount': 4200, 'expire_at': future, 'label': '新用户包'},
                {'amount': 0, 'expire_at': later, 'label': '已用完'},
                {'amount': 100, 'expire_at': 0, 'label': '没有到期时刻'},
            ],
        }})
        entry = creditsvc.platform_snapshot_entry('trae', 'u1')
        self.assertEqual(entry['credits'], 4700)
        self.assertEqual(entry['registered_at'], at)
        self.assertEqual(entry['expires'], [
            {'amount': 4200, 'expire_at': future, 'label': '新用户包'},
            {'amount': 500, 'expire_at': later, 'label': '每月登录'},
        ])
        self.assertEqual(entry['next_expire_at'], future)

    def test_old_snapshot_without_expiry_still_reads(self) -> None:
        """升级前的条目只有 {'credits','at'}，不能因此读不出余额。"""
        snapshot = {'trae': {'u1': {'credits': 7, 'at': 1700000000}}}
        with mock.patch.object(creditsvc, '_load_platform_snapshot',
                               lambda: snapshot):
            self.assertEqual(creditsvc.platform_snapshot_entry('trae', 'u1'), {
                'uid': 'u1', 'credits': 7, 'registered_at': 1700000000,
                'expires': [], 'next_expire_at': None,
            })


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

    def test_failure_returns_last_expiry(self) -> None:
        """失败回退时有效期不能消失：旧余额与旧到期出自同一份快照。"""
        future = int(time.time()) + 3 * 86400
        creditsvc.record_platform_balances('trae', {'u1': {
            'uid': 'u1', 'credits': 300,
            'expires': [{'amount': 300, 'expire_at': future, 'label': '每月登录'}],
        }})
        adapter = _FakeAdapter({'u1': PlatformError('令牌过期', status=401)})
        with mock.patch.object(platforms_router, '_adapter', lambda name: adapter):
            out = self._run(platforms_router.list_credits, 'trae', user={})
        item = out['credits'][0]
        self.assertEqual(item['last_credits'], 300)
        self.assertEqual(item['last_expires'], [
            {'amount': 300, 'expire_at': future, 'label': '每月登录'},
        ])
        self.assertEqual(item['last_next_expire_at'], future)

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

    def test_snapshot_endpoint_treats_checkin_only_as_inactive(self) -> None:
        """仅签到账号仍可查余额，但默认不算「可调度」，与 disabled 同档。"""
        creditsvc.record_platform_balance('trae', 'a', 1)
        creditsvc.record_platform_balance('trae', 'b', 2)
        adapter = _FakeAdapter({}, accounts=[
            {'uid': 'a', 'disabled': False, 'checkin_only': False},
            {'uid': 'b', 'disabled': False, 'checkin_only': True},
        ])
        with mock.patch.object(platforms_router, '_adapter', lambda name: adapter):
            body = platforms_router.platform_credits_snapshot(
                'trae', include_inactive=False, user={})
            all_rows = platforms_router.platform_credits_snapshot(
                'trae', include_inactive=True, user={})
        self.assertEqual([r['uid'] for r in body['accounts']], ['a'])
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


class PlatformOverviewTest(_DbCase):
    """设置页顶部「概况」卡：本地读账号 + 快照，不发上游。"""

    def test_counts_and_snapshot_totals(self) -> None:
        future = int(time.time()) + 86400
        creditsvc.record_platform_balances('trae', {
            'a': {'credits': 100,
                  'expires': [{'amount': 100, 'expire_at': future, 'label': '包'}]},
            'b': {'credits': 50.5},
        })
        adapter = _FakeAdapter({}, accounts=[
            {'uid': 'a', 'disabled': False},
            {'uid': 'b', 'disabled': False, 'checkin_only': True},
            {'uid': 'c', 'disabled': True},
            {'uid': 'd', 'disabled': False, 'cooling': True},
        ])
        out = platforms_router._platform_overview('trae', adapter)
        self.assertEqual(out['accounts'], 4)
        self.assertEqual(out['active'], 1)
        self.assertEqual(out['checkin_only'], 1)
        self.assertEqual(out['disabled'], 1)
        self.assertEqual(out['cooling'], 1)
        self.assertEqual(out['credits'], 150.5)
        self.assertEqual(out['credits_known'], 2)
        self.assertEqual(out['next_expire_at'], future)
        self.assertGreater(out['snapshot_at'], 0)
        self.assertIsNone(out['error'])

    def test_account_store_outage_is_reported_not_raised(self) -> None:
        class _Boom(_FakeAdapter):
            def list_accounts(self) -> list[dict]:
                raise RuntimeError('网关没起来')

        out = platforms_router._platform_overview('qoder', _Boom({}))
        self.assertEqual(out['accounts'], 0)
        self.assertEqual(out['error'], '网关没起来')

    def test_usd_totals_keep_their_unit(self) -> None:
        """Kilo / Trae 国际版的余额是美元：合计不能标成积分，否则会被读成
        「几十积分」。整批都是美元才标 `usd`。"""
        creditsvc.record_platform_balances('kilo', {
            'a': {'credits': 1.25, 'unit': 'usd'},
            'b': {'credits': 0.5, 'unit': 'usd'},
        })
        out = platforms_router._platform_overview(
            'kilo', _FakeAdapter({}, accounts=[
                {'uid': 'a', 'disabled': False}, {'uid': 'b', 'disabled': False}]))
        self.assertEqual(out['credits'], 1.75)
        self.assertEqual(out['credits_unit'], 'usd')

    def test_mixed_units_fall_back_to_credits(self) -> None:
        """混合单位不能标成美元：那种合计本身没有意义，更不能冒充美元。"""
        creditsvc.record_platform_balances('kilo', {
            'a': {'credits': 1.25, 'unit': 'usd'},
            'b': {'credits': 300},
        })
        out = platforms_router._platform_overview(
            'kilo', _FakeAdapter({}, accounts=[
                {'uid': 'a', 'disabled': False}, {'uid': 'b', 'disabled': False}]))
        self.assertEqual(out['credits_unit'], 'credits')

    def test_credit_platforms_have_no_usd_unit(self) -> None:
        creditsvc.record_platform_balances('trae', {'a': {'credits': 100}})
        out = platforms_router._platform_overview(
            'trae', _FakeAdapter({}, accounts=[{'uid': 'a', 'disabled': False}]))
        self.assertEqual(out['credits_unit'], 'credits')
        self.assertEqual(out['credits'], 100)

    def test_no_snapshot_yields_null_credits(self) -> None:
        out = platforms_router._platform_overview('trae', _FakeAdapter({}, []))
        self.assertIsNone(out['credits'])
        self.assertEqual(out['credits_known'], 0)
        self.assertEqual(out['snapshot_at'], 0)
        self.assertIsNone(out['next_expire_at'])


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
