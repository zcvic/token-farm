"""模型目录：三种来源的解析、优先级与导入落盘。

目录和倍率不同——它决定「客户端能不能看到这个模型」。写错方向的后果比倍率
更直接：用户导入的目录被客户端快照盖掉，或者服务器部署只剩内置默认值，
都会让面板和 `/v1/models` 缺模型。这里的用例守住两条：优先级固定为
导入 > 客户端快照 > 内置默认值；解析失败时不清空已有配置。

2026-11-01 之前的实现把目录写死在 `catalog._DEFAULT_MODELS` 里，运行时只能
被缓存覆盖。现在改成与倍率同构的三层来源，`_DEFAULT_MODELS` 搬到了
`model_catalog.json`。
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import config
from server.platforms.trae import catalog
from server.platforms.trae.adapter import TraeAdapter


def _model(model_id: str, **extra) -> dict:
    base = {'id': model_id, 'name': model_id, 'realm': 'cn',
            'context_window': 1000, 'max_tokens': 100,
            'multimodal': False, 'thinking': False}
    base.update(extra)
    return base


class NormalizeTest(unittest.TestCase):
    def test_export_shape(self) -> None:
        payload = {'version': 1, 'models': [_model('glm-5.3')]}
        self.assertEqual([m['id'] for m in catalog.normalize(payload)], ['glm-5.3'])

    def test_bare_list(self) -> None:
        self.assertEqual(
            [m['id'] for m in catalog.normalize([_model('kimi-k3')])],
            ['kimi-k3'])

    def test_reference_project_shape(self) -> None:
        """参考项目 `trae_catalog_*.json` 的条目要能直接导入。"""
        payload = {'models': [{
            'upstream_key': 'glm-5.3',
            'display_name': 'GLM-5.3',
            'chat': True,
            'multimodal': False,
            'context_window': 116000,
            'context_window_max': 1000000,
            'max_tokens': 64000,
            'thinking': {'supported': True},
            'consumption_rate': 0.78,
        }]}
        item = catalog.normalize(payload)[0]
        self.assertEqual(item['id'], 'glm-5.3')
        self.assertEqual(item['name'], 'GLM-5.3')
        self.assertEqual(item['context_window'], 116000)
        self.assertEqual(item['max_tokens'], 64000)
        self.assertTrue(item['thinking'])
        self.assertEqual(item['channel'], 'solo')

    def test_mapping_shape(self) -> None:
        payload = {'glm-5.3': {'name': 'GLM-5.3', 'context_window': 116000}}
        item = catalog.normalize(payload)[0]
        self.assertEqual(item['id'], 'glm-5.3')
        self.assertEqual(item['context_window'], 116000)

    def test_drops_non_chat_and_disabled(self) -> None:
        payload = [
            {'id': 'internal', 'chat': False},
            {'id': 'off', 'status': False},
            {'id': 'hidden', 'selectable': False},
            {'id': 'ok'},
        ]
        self.assertEqual([m['id'] for m in catalog.normalize(payload)], ['ok'])

    def test_dedupes_case_insensitively_first_wins(self) -> None:
        payload = [_model('GLM-5.3', max_tokens=1), _model('glm-5.3', max_tokens=2)]
        models = catalog.normalize(payload)
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0]['max_tokens'], 1)

    def test_channel_follows_realm_but_explicit_wins(self) -> None:
        payload = [
            _model('a', realm='cn'),
            _model('b', realm='intl'),
            _model('c', realm='cn', channel='chat'),
        ]
        out = {m['id']: m['channel'] for m in catalog.normalize(payload)}
        self.assertEqual(out, {'a': 'solo', 'b': 'chat', 'c': 'chat'})

    def test_client_entry_shape_is_accepted(self) -> None:
        """`desktop.read_models` 的输出（含 context_window/thinking/channel）也要能回灌。"""
        payload = [_model('glm-5.3', channel='solo', thinking=True)]
        item = catalog.normalize(payload)[0]
        self.assertTrue(item['thinking'])
        self.assertEqual(item['channel'], 'solo')

    def test_garbage_returns_empty(self) -> None:
        for payload in (None, 42, 'text', {}, [], {'models': [1, 'x', None]},
                        {'glm-5.3': 0.78}):
            self.assertEqual(catalog.normalize(payload), [])


class SourcesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(config, 'DATA_DIR', Path(self.tmp.name))
        patch.start()
        self.addCleanup(patch.stop)
        local = mock.patch.object(catalog, 'local', return_value=[])
        local.start()
        self.addCleanup(local.stop)

    def test_bundled_catalog_is_loadable(self) -> None:
        models = catalog.bundled()
        # 随代码发布的默认目录要真的能解析，否则服务器部署永远看不到模型
        self.assertGreaterEqual(len(models), 20)
        ids = {m['id'] for m in models}
        for model_id in ('glm-5.3', 'kimi-k3', 'qwen3.8-max',
                         'deepseek-v4.1-flash', 'gpt-6-astra'):
            self.assertIn(model_id, ids)
        self.assertTrue(all(m['channel'] in ('solo', 'chat') for m in models))

    def test_priority_imported_over_local_over_bundled(self) -> None:
        local = [_model('glm-5.3', name='本地 GLM', context_window=1)]
        catalog.save_imported([_model('glm-5.3', name='导入 GLM',
                                      context_window=2)])
        with mock.patch.object(catalog, 'local', return_value=local):
            merged = {m['id'].lower(): m for m in catalog.merged()}
        self.assertEqual(merged['glm-5.3']['name'], '导入 GLM')
        self.assertEqual(merged['glm-5.3']['context_window'], 2)
        # 没被导入/本地覆盖的条目仍来自内置默认值
        self.assertIn('kimi-k3', merged)

    def test_local_overrides_bundled(self) -> None:
        local = [_model('kimi-k3', name='客户端 Kimi')]
        with mock.patch.object(catalog, 'local', return_value=local):
            merged = {m['id'].lower(): m for m in catalog.merged()}
        self.assertEqual(merged['kimi-k3']['name'], '客户端 Kimi')

    def test_find_is_case_insensitive_and_prefers_imported(self) -> None:
        catalog.save_imported([_model('GLM-5.3', context_window=42)])
        found = catalog.find('glm-5.3')
        self.assertIsNotNone(found)
        self.assertEqual(found['context_window'], 42)
        self.assertIsNone(catalog.find(''))

    def test_save_then_clear_round_trip(self) -> None:
        result = catalog.save_imported([_model('glm-5.3')])
        self.assertEqual(result['sources']['imported'], 1)
        self.assertTrue(catalog.store_path().is_file())
        body = json.loads(catalog.store_path().read_text(encoding='utf-8'))
        self.assertEqual(body['models'][0]['id'], 'glm-5.3')

        cleared = catalog.clear_imported()
        self.assertEqual(cleared['sources']['imported'], 0)
        self.assertFalse(catalog.store_path().exists())

    def test_empty_import_does_not_wipe_existing(self) -> None:
        catalog.save_imported([_model('glm-5.3')])
        with self.assertRaises(ValueError):
            catalog.save_imported([])
        self.assertEqual([m['id'] for m in catalog.imported()], ['glm-5.3'])

    def test_export_can_be_reimported(self) -> None:
        catalog.save_imported([_model('glm-5.3')])
        exported = catalog.export_payload()
        catalog.clear_imported()
        catalog.save_imported(exported)
        # 导出的是一份「当前生效的完整目录」，再导入即把整份钉住
        ids = {m['id'] for m in catalog.imported()}
        self.assertIn('glm-5.3', ids)
        self.assertIn('kimi-k3', ids)
        self.assertEqual([m['id'] for m in catalog.merged()],
                         [m['id'] for m in catalog.imported()])

    def test_summary_counts_sources(self) -> None:
        catalog.save_imported([_model('glm-5.3')])
        summary = catalog.summary()
        self.assertEqual(summary['sources']['imported'], 1)
        self.assertGreater(summary['sources']['bundled'], 0)
        self.assertGreaterEqual(summary['total'], summary['sources']['bundled'])
        self.assertTrue(summary['imported_at'])


class AdapterCatalogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(config, 'DATA_DIR', Path(self.tmp.name))
        patch.start()
        self.addCleanup(patch.stop)
        local = mock.patch.object(catalog, 'local', return_value=[])
        local.start()
        self.addCleanup(local.stop)
        from server.platforms.trae import rates
        client_rates = mock.patch.object(rates, 'local', return_value={})
        client_rates.start()
        self.addCleanup(client_rates.stop)
        self.adapter = TraeAdapter()

    def test_server_deployment_without_desktop_lists_bundled(self) -> None:
        """没有客户端、没有缓存：内置目录要兜住，客户端才不会看到空列表。"""
        with mock.patch.object(self.adapter, '_cached_models', return_value=None), \
             mock.patch.object(self.adapter, '_implicit_live_allowed',
                               return_value=False):
            models = asyncio.run(self.adapter.list_models())
        ids = {m['model_name'] for m in models}
        self.assertIn('glm-5.3', ids)
        self.assertIn('gpt-6-astra', ids)
        self.assertTrue(all(m['id'].startswith(('traecn/', 'traecom/'))
                            for m in models))

    def test_imported_model_appears_on_refresh(self) -> None:
        """刷新时线上目录为空 → 落到合并目录，导入的模型要能被列出来。"""
        catalog.save_imported([_model('new-model-x')])
        with mock.patch.object(self.adapter, '_live_models',
                               new=mock.AsyncMock(return_value=[])):
            models = asyncio.run(self.adapter.list_models(refresh=True))
        self.assertIn('new-model-x', {m['model_name'] for m in models})

    def test_imported_catalog_applies_on_cache_hit(self) -> None:
        """缓存命中也立刻生效：导入后不必等「刷新模型」或 6 小时过期。"""
        catalog.save_imported([_model('new-model-x'),
                               _model('glm-5.3', name='导入的 GLM')])
        cached = [_model('glm-5.3', name='缓存的 GLM'), _model('kimi-k3')]
        with mock.patch.object(self.adapter, '_cached_models',
                               return_value=cached):
            models = asyncio.run(self.adapter.list_models())
        by_name = {m['model_name']: m for m in models}
        self.assertIn('new-model-x', by_name)
        # 同 id 以导入的为准，缓存里其它条目保留
        self.assertEqual(by_name['glm-5.3']['name'], '导入的 GLM')
        self.assertIn('kimi-k3', by_name)
        summary = self.adapter.model_catalog()
        self.assertEqual(summary['sources']['imported'], 2)

    def test_imported_rates_override_cached_ratio(self) -> None:
        """导入的倍率要压过缓存/客户端旧值（优先级与 rates 模块一致）。"""
        from server.platforms.trae import rates
        cached = [{'id': 'glm-5.3', 'name': 'GLM-5.3', 'realm': 'cn',
                   'credit_ratio': 0.1}]
        rates.save_imported({'glm-5.3': 0.9})
        with mock.patch.object(self.adapter, '_cached_models',
                               return_value=cached):
            models = asyncio.run(self.adapter.list_models())
        self.assertAlmostEqual(models[0]['credit_ratio'], 0.9)

    def test_channel_uses_imported_catalog(self) -> None:
        catalog.save_imported([_model('custom-model', realm='cn',
                                      channel='chat')])
        self.assertEqual(self.adapter._channel('custom-model'), 'chat')

    def test_export_includes_bundled_catalog(self) -> None:
        exported = self.adapter.export_model_catalog()
        self.assertGreaterEqual(len(exported['models']), 20)
        self.assertEqual(exported['source'], 'trae-token-farm')


if __name__ == '__main__':
    unittest.main()
