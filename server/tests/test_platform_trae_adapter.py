"""Trae 适配器策略：模型目录缓存、刷新旁路、多账号选择。

协议层在 `test_platform_trae_client.py`，账号文件在 `test_platform_trae_store.py`；
这里只测「策略」——它决定了 `/v1/models` 会不会拖慢客户端、刷新时会不会
把客户端的登录态打掉。
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import config, db
from server.platforms.trae import catalog, store
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


class ListModelsTest(_Base):
    def test_cache_miss_after_failed_attempt_uses_catalog_without_network(self) -> None:
        # 先记一次尝试时间：10 分钟内的隐式拉取会被跳过
        self.adapter._note_implicit_attempt()
        with mock.patch.object(self.adapter, '_live_models',
                               side_effect=AssertionError('不该发网络请求')):
            models = asyncio.run(self.adapter.list_models())
        ids = {m['model_name'] for m in models}
        self.assertIn('glm-5.2', ids)
        self.assertTrue(all(m['id'].startswith('trae/') for m in models))

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


class CandidateTest(_Base):
    def test_candidates_prefer_matching_realm(self) -> None:
        store.save_account({'uid': 'cn-1', 'realm': 'cn', 'token': 't'})
        store.save_account({'uid': 'intl-1', 'realm': 'intl', 'token': 't'})
        cn_models = [a for a in self.adapter._candidates('glm-5.2')
                     if store.read_account(a['uid'])['realm'] == 'cn']
        self.assertTrue(cn_models, '国内版模型不该被派到国际版账号')

    def test_candidates_skip_accounts_without_credentials(self) -> None:
        store.save_account({'uid': 'empty', 'realm': 'cn'})
        self.assertEqual(self.adapter._candidates('glm-5.2'), [])


if __name__ == '__main__':
    unittest.main()
