"""Trae 适配器策略：模型目录缓存、刷新旁路、多账号选择。

协议层在 `test_platform_trae_client.py`，账号文件在 `test_platform_trae_store.py`；
这里只测「策略」——它决定了 `/v1/models` 会不会拖慢客户端、刷新时会不会
把客户端的登录态打掉。
"""
from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from server import config, db
from server.platforms.base import PlatformError
from server.platforms.trae import catalog, client, store
from server.platforms.trae.adapter import TraeAdapter


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


class RealmCapabilityTest(_Base):
    """前端靠这个标记决定是否显示版本切换、是否按版本过滤。"""

    def test_trae_is_realm_aware(self) -> None:
        self.assertTrue(self.adapter.realm_aware)

    def test_base_default_is_not_realm_aware(self) -> None:
        from server.platforms.base import PlatformAdapter

        self.assertFalse(PlatformAdapter.realm_aware)


class ImportAccountTest(_Base):
    """导入要保留设备身份：服务器上没有客户端时，只能靠账号里的这几项签名。"""

    def test_device_identity_survives_import(self) -> None:
        uid = '7703072224128320520'
        out = self.adapter.import_account({
            'uid': uid, 'token': 't', 'refreshToken': 'r', 'realm': 'intl',
            'deviceKeyPEM': 'PEM', 'deviceId': '7703084028531181063',
            'machineId': 'm' * 64, 'devicePublicKeyPEM': 'PUB',
        })
        # 接口不回传私钥 / 公钥明文，只标记「有」
        self.assertNotIn('deviceKeyPEM', out)
        self.assertTrue(out['has_device_key'])
        saved = store.read_account(uid)
        self.assertEqual(saved['realm'], 'intl')
        self.assertEqual(saved['deviceId'], '7703084028531181063')
        self.assertEqual(saved['machineId'], 'm' * 64)
        self.assertEqual(saved['devicePublicKeyPEM'], 'PUB')

    def test_reimport_without_device_fields_keeps_them(self) -> None:
        # 刷新 / 重新导出一份只有令牌的账号，不该把设备身份抹掉。
        uid = '7703072224128320521'
        self.adapter.import_account({
            'uid': uid, 'token': 't', 'refreshToken': 'r', 'realm': 'intl',
            'deviceKeyPEM': 'PEM', 'deviceId': 'holder', 'machineId': 'mid',
        })
        self.adapter.import_account({
            'uid': uid, 'token': 't2', 'refreshToken': 'r2', 'realm': 'intl'})
        saved = store.read_account(uid)
        self.assertEqual(saved['token'], 't2')
        self.assertEqual(saved['deviceKeyPEM'], 'PEM')
        self.assertEqual(saved['deviceId'], 'holder')
        self.assertEqual(saved['machineId'], 'mid')


class ListModelsTest(_Base):
    def test_cache_miss_after_failed_attempt_uses_catalog_without_network(self) -> None:
        # 先记一次尝试时间：10 分钟内的隐式拉取会被跳过
        self.adapter._note_implicit_attempt()
        with mock.patch.object(self.adapter, '_live_models',
                               side_effect=AssertionError('不该发网络请求')):
            models = asyncio.run(self.adapter.list_models())
        ids = {m['model_name'] for m in models}
        self.assertIn('glm-5.2', ids)
        # 对外展示的是区域化 ID（`traecn/` / `traecom/`）
        self.assertTrue(all(m['id'].startswith(('traecn/', 'traecom/'))
                            for m in models))

    def test_implicit_attempt_throttled_after_failure(self) -> None:
        self.assertTrue(self.adapter._implicit_live_allowed())
        with mock.patch.object(self.adapter, '_live_models',
                               new=mock.AsyncMock(return_value=[])):
            asyncio.run(self.adapter.list_models())
        self.assertFalse(self.adapter._implicit_live_allowed(),
                         '隐式拉取失败后应冷却，不能让每次 /v1/models 都等超时')

    def test_first_call_uses_live_models(self) -> None:
        live = [{'id': 'deepseek-V3', 'name': 'DeepSeek-V3', 'realm': 'cn',
                 'context_window': 0, 'max_tokens': 0,
                 'multimodal': False, 'thinking': False}]
        with mock.patch.object(self.adapter, '_live_models',
                               new=mock.AsyncMock(return_value=live)):
            models = asyncio.run(self.adapter.list_models())
        self.assertEqual([m['model_name'] for m in models], ['deepseek-V3'])

    def test_refresh_uses_live_and_caches(self) -> None:
        live = [{'id': 'deepseek-V3', 'name': 'DeepSeek-V3', 'realm': 'cn',
                 'context_window': 0, 'max_tokens': 0,
                 'multimodal': False, 'thinking': False}]
        with mock.patch.object(self.adapter, '_live_models',
                               new=mock.AsyncMock(return_value=live)):
            models = asyncio.run(self.adapter.list_models(refresh=True))
        self.assertEqual([m['model_name'] for m in models], ['deepseek-V3'])
        # 刷新结果进缓存：下一次不带 refresh 也拿得到，且不再发请求
        with mock.patch.object(self.adapter, '_live_models',
                               side_effect=AssertionError('不该发网络请求')):
            again = asyncio.run(self.adapter.list_models())
        self.assertEqual([m['model_name'] for m in again], ['deepseek-V3'])

    def test_refresh_falls_back_to_catalog_when_live_empty(self) -> None:
        with mock.patch.object(self.adapter, '_live_models',
                               new=mock.AsyncMock(return_value=[])):
            models = asyncio.run(self.adapter.list_models(refresh=True))
        self.assertIn('glm-5.2', {m['model_name'] for m in models})

    def test_from_live_enriches_metadata_from_catalog(self) -> None:
        raw = {'name': 'glm-5.2', 'display_name': 'GLM-5.2'}
        model = self.adapter._from_live(raw)
        known = catalog.find('glm-5.2')
        self.assertEqual(model['id'], 'glm-5.2')
        self.assertEqual(model['context_window'], known['context_window'])
        self.assertEqual(model['max_tokens'], known['max_tokens'])

    def test_from_live_skips_non_chat_models(self) -> None:
        self.assertIsNone(self.adapter._from_live({'name': 'x', 'chat': False}))
        self.assertIsNone(self.adapter._from_live({}))

    def test_public_model_id_uses_region_prefix(self) -> None:
        cn = self.adapter._public_model({'id': 'glm-5.2', 'realm': 'cn'})
        intl = self.adapter._public_model({'id': 'glm-5.2', 'realm': 'intl'})
        self.assertEqual(cn['id'], 'traecn/glm-5.2')
        self.assertEqual(intl['id'], 'traecom/glm-5.2')
        self.assertEqual(cn['model_name'], 'glm-5.2')
        self.assertEqual(cn['realm'], 'cn')


class CreditRatioTest(_Base):
    def test_public_model_exposes_ratio(self) -> None:
        model = self.adapter._public_model({'id': 'x', 'credit_ratio': 0.06})
        self.assertAlmostEqual(model['credit_ratio'], 0.06)
        # 未知就是 None，不能是 0（0 会被读成「免费」）
        self.assertIsNone(self.adapter._public_model({'id': 'y'})['credit_ratio'])

    def test_cached_models_get_ratio_from_client_snapshot(self) -> None:
        cached = [{'id': 'glm-5.2', 'name': 'GLM-5.2', 'realm': 'cn'}]
        with mock.patch.object(self.adapter, '_cached_models', return_value=cached), \
             mock.patch.object(self.adapter, '_local_ratios',
                               return_value={'glm-5.2': 0.5}):
            models = asyncio.run(self.adapter.list_models())
        self.assertAlmostEqual(models[0]['credit_ratio'], 0.5)

    def test_from_live_leaves_ratio_for_merge(self) -> None:
        self.assertIsNone(self.adapter._from_live({'name': 'seed_m8'})['credit_ratio'])


class CreditMeterTest(_Base):
    """增量模式：前后夹一次账户累计（`credits_amount`），并发时不归属。"""

    account = {'uid': 'u1', 'realm': 'cn'}

    def test_disabled_returns_none(self) -> None:
        with mock.patch.object(config, 'TRAE_CREDIT_METER', False):
            self.assertIsNone(asyncio.run(self.adapter._meter_start(self.account)))

    def test_concurrent_request_is_not_attributed(self) -> None:
        with mock.patch.object(config, 'TRAE_CREDIT_METER', True), \
             mock.patch.object(client, 'fetch_consumed',
                               new=mock.AsyncMock(return_value=1.0)):
            first = asyncio.run(self.adapter._meter_start(self.account))
            self.assertIsNotNone(first)
            self.assertIsNone(asyncio.run(self.adapter._meter_start(self.account)),
                              '同账号并发在途时分不清增量归属，应该跳过')
            usage: dict = {}
            asyncio.run(self.adapter._meter_finish(self.account, first, usage))
            self.assertNotIn('credit', usage)
            self.assertNotIn('u1', self.adapter._metering)

    def test_finish_writes_credit_delta(self) -> None:
        usage = {'prompt_tokens': 10}
        with mock.patch.object(config, 'TRAE_CREDIT_METER', True), \
             mock.patch.object(client, 'fetch_consumed',
                               new=mock.AsyncMock(side_effect=[1.0, 1.06])):
            meter = asyncio.run(self.adapter._meter_start(self.account))
            asyncio.run(self.adapter._meter_finish(self.account, meter, usage))
        self.assertAlmostEqual(usage['credit'], 0.06)
        self.assertNotIn('u1', self.adapter._metering)

    def test_fetch_failure_releases_lock(self) -> None:
        with mock.patch.object(config, 'TRAE_CREDIT_METER', True), \
             mock.patch.object(client, 'fetch_consumed',
                               new=mock.AsyncMock(side_effect=PlatformError('x'))):
            self.assertIsNone(asyncio.run(self.adapter._meter_start(self.account)))
        self.assertNotIn('u1', self.adapter._metering, '读不到基线也要放开锁')

    def test_stale_token_does_not_release_new_lock(self) -> None:
        with mock.patch.object(config, 'TRAE_CREDIT_METER', True), \
             mock.patch.object(client, 'fetch_consumed',
                               new=mock.AsyncMock(return_value=1.0)):
            old = asyncio.run(self.adapter._meter_start(self.account))
            asyncio.run(self.adapter._meter_finish(self.account, old, {}))
            new = asyncio.run(self.adapter._meter_start(self.account))
            self.adapter._meter_abort(self.account, old)  # 上一个请求迟到的收尾
            self.assertIs(self.adapter._metering.get('u1'), new[0])
            asyncio.run(self.adapter._meter_finish(self.account, new, {}))


class CreditDetailMeterTest(_Base):
    """明细模式：按 `session_id` 取这一条记录的实扣积分（并发安全、零基线开销）。"""

    account = {'uid': 'u2', 'realm': 'cn'}

    def test_detail_meter_skips_baseline_and_lock(self) -> None:
        """请求前不打上游、不占锁：明细模式的全部开销都在收尾那一次查询。"""
        baseline = mock.AsyncMock(return_value=1.0)
        with mock.patch.object(config, 'TRAE_CREDIT_METER', 'detail'), \
             mock.patch.object(client, 'fetch_consumed', new=baseline):
            meter = asyncio.run(self.adapter._meter_start(self.account))
        baseline.assert_not_awaited()
        self.assertIsNotNone(meter)
        self.assertEqual(len(meter.session_id or ''), 24)
        self.assertIsNone(meter.before)
        self.assertIsNone(meter.token)
        self.assertNotIn('u2', self.adapter._metering, '明细模式不需要账号级锁')

    def test_detail_finish_writes_row_amount(self) -> None:
        row = {'session_id': 'abc', 'amount_float': 0.028, 'credits_float': 0.028}
        usage: dict = {}
        with mock.patch.object(config, 'TRAE_CREDIT_METER', 'detail'), \
             mock.patch.object(client, 'fetch_request_credit',
                               new=mock.AsyncMock(return_value=row)) as credit:
            meter = asyncio.run(self.adapter._meter_start(self.account))
            asyncio.run(self.adapter._meter_finish(self.account, meter, usage))
        self.assertAlmostEqual(usage['credit'], 0.028)
        self.assertEqual(credit.await_count, 1, '收尾只查一次，重试在函数内部')
        self.assertNotIn('u2', self.adapter._metering)

    def test_detail_leaves_credit_empty_when_row_missing(self) -> None:
        """没有基线就没有兜底：查不到宁可不写，也不能写个错数。"""
        usage: dict = {}
        baseline = mock.AsyncMock(return_value=1.0)
        with mock.patch.object(config, 'TRAE_CREDIT_METER', 'detail'), \
             mock.patch.object(client, 'fetch_consumed', new=baseline), \
             mock.patch.object(client, 'fetch_request_credit',
                               new=mock.AsyncMock(return_value=None)):
            meter = asyncio.run(self.adapter._meter_start(self.account))
            asyncio.run(self.adapter._meter_finish(self.account, meter, usage))
        self.assertNotIn('credit', usage)
        baseline.assert_not_awaited()

    def test_detail_concurrent_requests_still_metered(self) -> None:
        """账户增量分不清归属，但会话明细分得清：并发下每条都要记上。"""
        amounts = [0.01, 0.02]

        async def fake_credit(account, session_id, started):
            return {'amount_float': amounts.pop(0)}

        with mock.patch.object(config, 'TRAE_CREDIT_METER', 'detail'), \
             mock.patch.object(client, 'fetch_request_credit', new=fake_credit):
            first = asyncio.run(self.adapter._meter_start(self.account))
            second = asyncio.run(self.adapter._meter_start(self.account))
            self.assertIsNone(second.token, '明细模式不用锁')
            self.assertNotEqual(first.session_id, second.session_id)
            usage_first: dict = {}
            usage_second: dict = {}
            asyncio.run(self.adapter._meter_finish(self.account, first, usage_first))
            asyncio.run(self.adapter._meter_finish(self.account, second, usage_second))
        self.assertAlmostEqual(usage_first['credit'], 0.01)
        self.assertAlmostEqual(usage_second['credit'], 0.02)


class CandidateTest(_Base):
    def test_candidates_prefer_matching_realm(self) -> None:
        store.save_account({'uid': 'cn-1', 'realm': 'cn', 'token': 't'})
        store.save_account({'uid': 'intl-1', 'realm': 'intl', 'token': 't'})
        cn_models = [a for a in self.adapter._candidates('glm-5.2')
                     if store.read_account(a['uid'])['realm'] == 'cn']
        self.assertTrue(cn_models, '国内版模型不该被派到国际版账号')

    def test_explicit_realm_hint_is_strict(self) -> None:
        store.save_account({'uid': 'cn-1', 'realm': 'cn', 'token': 't'})
        store.save_account({'uid': 'intl-1', 'realm': 'intl', 'token': 't'})
        self.assertEqual([a['uid'] for a in self.adapter._candidates(
            'glm-5.2', 'global')], ['intl-1'])
        self.assertEqual([a['uid'] for a in self.adapter._candidates(
            'glm-5.2', 'cn')], ['cn-1'])

    def test_explicit_realm_without_accounts_does_not_fall_back(self) -> None:
        store.save_account({'uid': 'cn-1', 'realm': 'cn', 'token': 't'})
        self.assertEqual(self.adapter._candidates('glm-5.2', 'global'), [])

    def test_candidates_skip_accounts_without_credentials(self) -> None:
        store.save_account({'uid': 'empty', 'realm': 'cn'})
        self.assertEqual(self.adapter._candidates('glm-5.2'), [])

    def test_candidates_skip_disabled_and_cooling_accounts(self) -> None:
        store.save_account({'uid': 'frozen', 'realm': 'cn', 'token': 't',
                            'disabled': True})
        store.save_account({'uid': 'cooling', 'realm': 'cn', 'token': 't',
                            'coolingUntil': time.time() + 600})
        store.save_account({'uid': 'usable', 'realm': 'cn', 'token': 't'})
        self.assertEqual([a['uid'] for a in self.adapter._candidates('glm-5.2')],
                         ['usable'])

    def test_candidates_include_account_whose_cooling_expired(self) -> None:
        store.save_account({'uid': 'warm', 'realm': 'cn', 'token': 't',
                            'coolingUntil': time.time() - 10})
        self.assertEqual([a['uid'] for a in self.adapter._candidates('glm-5.2')],
                         ['warm'])

    # ── 会话粘性（prompt cache 亲和）────────────────────────
    def _seed_pair(self) -> None:
        store.save_account({'uid': 'a', 'realm': 'cn', 'token': 't'})
        store.save_account({'uid': 'b', 'realm': 'cn', 'token': 't'})

    def test_session_affinity_keeps_same_account(self) -> None:
        self._seed_pair()
        first = self.adapter._candidates('glm-5.2', None, 'sess-1')[0]['uid']
        before = dict(self.adapter._swrr)
        for _ in range(3):
            self.assertEqual(
                self.adapter._candidates('glm-5.2', None, 'sess-1')[0]['uid'],
                first, '同一会话换账号会让上游 prompt cache 全部作废')
        self.assertEqual(self.adapter._swrr, before,
                         '粘性命中不该推进 SWRR 游标')
        second = self.adapter._candidates('glm-5.2', None, 'sess-2')[0]['uid']
        self.assertNotEqual(second, first, '新会话仍应按权重轮换')

    def test_disabled_bound_account_rebinds(self) -> None:
        self._seed_pair()
        first = self.adapter._candidates('glm-5.2', None, 'sess-1')[0]['uid']
        store.set_disabled(first, True, '测试')
        second = self.adapter._candidates('glm-5.2', None, 'sess-1')[0]['uid']
        self.assertNotEqual(second, first, '绑定账号被冻结后应重新选路')
        self.assertEqual(
            self.adapter._candidates('glm-5.2', None, 'sess-1')[0]['uid'],
            second, '重新选路后应改绑到新账号')


class ChatAffinityTest(_Base):
    """对话路径：会话键来自请求体，账号/上游错误要释放绑定。"""

    def test_upstream_error_releases_affinity(self) -> None:
        store.save_account({'uid': 'cn-1', 'realm': 'cn', 'token': 't'})
        body = {'prompt_cache_key': 'sess-429'}
        with mock.patch.object(
                self.adapter, '_ensure_token',
                mock.AsyncMock(side_effect=lambda account: account)), \
             mock.patch.object(
                self.adapter, '_chat_once',
                mock.AsyncMock(side_effect=PlatformError(
                    '上游失败', status=429, code='platform_error'))):
            with self.assertRaises(PlatformError):
                asyncio.run(self.adapter._chat_accounts('glm-5.2', body))
        self.assertIsNone(self.adapter._affinity.get('prompt_cache_key:sess-429'),
                          '限流后不解绑会让同一会话每轮都撞同一个号')


if __name__ == '__main__':
    unittest.main()
