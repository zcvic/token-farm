"""Trae 国际版用量：美元额度解析、主机选择、适配器口径。

2026-11-01 用真实网页请求核对过：接口是
`POST https://ug-normal.trae.ai/trae/api/v1/pay/user_current_entitlement_list`，
请求体 `{"require_usage": true}`，额度字段在 `entitlement_base_info.quota`。
这里全部离线，响应样本按真实结构裁剪。
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import config, db
from server.platforms.base import PlatformError
from server.platforms.trae import client, store
from server.platforms.trae.adapter import TraeAdapter, _dollar_usage


SAMPLE = {
    'billing_version': 3,
    'is_dollar_usage_billing': True,
    'user_entitlement_pack_list': [{
        'display_desc': 'Free plan',
        'entitlement_base_info': {
            'end_time': 1796083199,
            'quota': {
                'basic_usage_limit': 10,
                'bonus_usage_limit': 5,
            },
        },
        'usage': {
            'basic_usage_amount': 2.5,
            'bonus_usage_amount': 1.25,
        },
    }],
}


def _run(awaitable):  # noqa: ANN001
    return asyncio.run(awaitable)


class DollarUsageTest(unittest.TestCase):
    def test_sums_basic_and_bonus(self) -> None:
        usage = _dollar_usage(SAMPLE)
        assert usage is not None
        self.assertEqual(usage['limit'], 15.0)
        self.assertEqual(usage['used'], 3.75)
        self.assertEqual(usage['end_at'], 1796083199)
        self.assertEqual(usage['label'], 'Free plan')

    def test_accepts_pack_level_quota(self) -> None:
        payload = {'user_entitlement_pack_list': [{
            'quota': {'basic_usage_limit': 2, 'bonus_usage_limit': 0},
            'usage': {'basic_usage_amount': 0.5},
        }]}
        usage = _dollar_usage(payload)
        assert usage is not None
        self.assertEqual(usage['limit'], 2.0)
        self.assertEqual(usage['used'], 0.5)
        self.assertIsNone(usage['end_at'])

    def test_none_without_pack(self) -> None:
        for payload in (None, {}, {'user_entitlement_pack_list': None},
                        {'user_entitlement_pack_list': []},
                        {'user_entitlement_pack_list': [{'usage': {}}]}):
            self.assertIsNone(_dollar_usage(payload), payload)


class HostSelectionTest(unittest.TestCase):
    def test_web_host_comes_first(self) -> None:
        hosts = client.intl_entitlement_hosts({'region': 'JP'})
        self.assertEqual(hosts[0], 'https://ug-normal.trae.ai')
        self.assertEqual(hosts[1], 'https://api-sg-central.trae.ai')

    def test_us_account_uses_us_host(self) -> None:
        hosts = client.intl_entitlement_hosts({'region': 'US-east'})
        self.assertEqual(hosts[1], 'https://api-us-east.trae.ai')
        self.assertEqual(hosts[2], 'https://api-sg-central.trae.ai')

    def test_api_host_hint(self) -> None:
        hosts = client.intl_entitlement_hosts({'api': 'https://api-us.x'})
        self.assertIn('https://api-us-east.trae.ai', hosts[:2])

    def test_region_of_reads_nested_user_info(self) -> None:
        payload = {'Result': {'Data': {'UserInfo': {'region': 'US'}}}}
        self.assertEqual(client.region_of(payload), 'US')
        self.assertEqual(client.region_of({'userRegion': {'region': 'sg'}}), 'sg')
        self.assertEqual(client.region_of({}), '')


class FetchIntlEntitlementTest(unittest.TestCase):
    def test_posts_web_body_and_falls_back(self) -> None:
        calls: list[dict] = []

        async def fake(account, realm, hosts, path, body, timeout, headers,
                       **kwargs):  # noqa: ANN001, ANN202
            calls.append({'hosts': hosts, 'path': path, 'body': body})
            if len(calls) == 1:
                raise PlatformError('Api Scope Forbidden', status=403,
                                    code='403')
            return SAMPLE

        with mock.patch.object(client, '_request_hosts', new=fake):
            got = _run(client.fetch_intl_entitlement(
                {'realm': 'intl', 'region': 'JP'}))
        self.assertEqual(got['billing_version'], 3)
        self.assertEqual(calls[0]['path'], client.PATH_INTL_ENTITLEMENT)
        self.assertEqual(calls[0]['body'], {'require_usage': True})
        self.assertEqual(calls[0]['hosts'], ('https://ug-normal.trae.ai',))
        self.assertEqual(calls[1]['hosts'], ('https://api-sg-central.trae.ai',))

    def test_all_hosts_failing_raises_first_error(self) -> None:
        errors = [PlatformError('first', status=403, code='403'),
                  PlatformError('second', status=502, code='upstream')]
        calls = 0

        async def counted(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            nonlocal calls
            calls += 1
            raise errors[0] if calls == 1 else errors[1]

        with mock.patch.object(client, '_request_hosts', new=counted):
            with self.assertRaises(PlatformError) as ctx:
                _run(client.fetch_intl_entitlement({'realm': 'intl'}))
        self.assertEqual(ctx.exception.message, 'first')
        self.assertEqual(calls, 3)


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._orig_db = config.DB_PATH
        config.DB_PATH = Path(self.tmp.name) / 'a.db'
        db._conn = None
        db.connect()
        patch = mock.patch.object(config, 'DATA_DIR', Path(self.tmp.name))
        patch.start()
        self.addCleanup(patch.stop)
        self.adapter = TraeAdapter()

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db

    def _account(self, **extra) -> dict:
        account = {'uid': '7703072224128320520', 'realm': 'intl',
                   'token': 't', 'refreshToken': 'r'}
        account.update(extra)
        store.save_account(account)
        return account

    def test_intl_credits_use_dollar_entitlement(self) -> None:
        account = self._account(region='JP')
        fetch = mock.AsyncMock(return_value=SAMPLE)
        legacy = mock.AsyncMock(side_effect=AssertionError('不该走国内版接口'))
        with mock.patch.object(client, 'fetch_intl_entitlement', fetch), \
                mock.patch.object(client, 'fetch_entitlement', legacy):
            got = _run(self.adapter.credits(account['uid']))
        self.assertEqual(got['unit'], 'usd')
        self.assertEqual(got['limit'], 15.0)
        self.assertEqual(got['used'], 3.75)
        self.assertEqual(got['remain'], 11.25)
        self.assertEqual(got['credits'], 11.25)
        self.assertEqual(got['source'], 'entitlement')
        self.assertEqual(got['expires'][0]['expire_at'], 1796083199)
        fetch.assert_awaited_once()

    def test_intl_credits_keep_two_decimals(self) -> None:
        account = self._account()
        sample = {
            'user_entitlement_pack_list': [{
                'entitlement_base_info': {'quota': {
                    'basic_usage_limit': 1, 'bonus_usage_limit': 0}},
                'usage': {'basic_usage_amount': 0.25},
            }],
        }
        with mock.patch.object(client, 'fetch_intl_entitlement',
                               mock.AsyncMock(return_value=sample)):
            got = _run(self.adapter.credits(account['uid']))
        self.assertEqual(got['remain'], 0.75)
        self.assertEqual(got['credits'], 0.75)

    def test_intl_without_pack_raises_instead_of_zero(self) -> None:
        account = self._account()
        with mock.patch.object(client, 'fetch_intl_entitlement',
                               mock.AsyncMock(return_value={})):
            with self.assertRaises(PlatformError):
                _run(self.adapter.credits(account['uid']))

    def test_import_keeps_region_hint(self) -> None:
        out = self.adapter.import_account({
            'uid': '7703072224128320521', 'token': 't', 'refreshToken': 'r',
            'realm': 'intl', 'region': 'US', 'api': 'https://api-us-east.trae.ai',
        })
        self.assertEqual(out['realm'], 'intl')
        saved = store.read_account('7703072224128320521')
        assert saved is not None
        self.assertEqual(saved['region'], 'US')
        self.assertEqual(saved['api'], 'https://api-us-east.trae.ai')


if __name__ == '__main__':
    unittest.main()
