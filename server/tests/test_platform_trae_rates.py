"""积分消耗倍率表：三种来源的解析、优先级与导入落盘。

倍率是「显示用」的辅助信息，错一位不会让请求失败，但**写错方向**（把未知
当成 0、或让旧的内置值盖掉用户导入的值）会让界面直接说谎。这里的用例就是
守住这两条：未知保持 `None`，优先级固定为 导入 > 客户端快照 > 内置默认值。
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import config
from server.platforms.trae import rates
from server.platforms.trae.adapter import TraeAdapter


class NormalizeTest(unittest.TestCase):
    def test_flat_mapping(self) -> None:
        self.assertEqual(rates.normalize({'glm-5.3': 0.78}), {'glm-5.3': 0.78})

    def test_rates_wrapper(self) -> None:
        payload = {'version': 1, 'rates': {'kimi-k3': 1.83}}
        self.assertEqual(rates.normalize(payload), {'kimi-k3': 1.83})

    def test_list_shape(self) -> None:
        payload = [
            {'id': 'glm-5.3', 'credit_ratio': 0.78},
            {'model_name': 'kimi-k3', 'rate': 1.83},
            {'name': 'mimo-v2.6-flash', 'credit_ratio': 0.06},
        ]
        self.assertEqual(rates.normalize(payload),
                         {'glm-5.3': 0.78, 'kimi-k3': 1.83,
                          'mimo-v2.6-flash': 0.06})

    def test_rejects_non_numeric_and_negative(self) -> None:
        payload = {'a': '0.1', 'b': True, 'c': -1, 'd': None, 'e': 0}
        self.assertEqual(rates.normalize(payload), {'e': 0.0})

    def test_drops_blank_model_names(self) -> None:
        self.assertEqual(rates.normalize({'  ': 0.1, 'x': 0.2}), {'x': 0.2})

    def test_garbage_returns_empty(self) -> None:
        for payload in (None, 42, 'text', {}, []):
            self.assertEqual(rates.normalize(payload), {})


class SourcesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(config, 'DATA_DIR', Path(self.tmp.name))
        patch.start()
        self.addCleanup(patch.stop)

    def test_bundled_defaults_are_loadable(self) -> None:
        table = rates.bundled()
        # 随代码发布的默认表要真的能解析，否则服务器部署永远显示 —
        self.assertGreaterEqual(len(table), 10)
        self.assertAlmostEqual(table['glm-5.3'], 0.78)
        self.assertAlmostEqual(table['kimi-k3'], 1.83)

    def test_priority_imported_over_local_over_bundled(self) -> None:
        local = {'glm-5.3': 0.5, 'kimi-k3': 1.5}
        rates.save_imported({'glm-5.3': 0.9})
        with mock.patch.object(rates, 'local', return_value=local):
            merged = rates.merged()
        self.assertAlmostEqual(merged['glm-5.3'], 0.9, msg='导入应覆盖快照与内置值')
        self.assertAlmostEqual(merged['kimi-k3'], 1.5, msg='快照覆盖内置值')
        self.assertAlmostEqual(merged['deepseek-v4.1-flash'], 0.06,
                               msg='缺少前两层时用内置默认值')

    def test_lookup_is_case_insensitive(self) -> None:
        rates.save_imported({'DeepSeek-V4-Pro-Official': 0.36})
        with mock.patch.object(rates, 'local', return_value={}):
            self.assertAlmostEqual(
                rates.lookup()['deepseek-v4-pro-official'], 0.36)

    def test_save_then_clear_round_trip(self) -> None:
        result = rates.save_imported({'glm-5.3': 0.78})
        self.assertEqual(result['sources']['imported'], 1)
        self.assertTrue(rates.store_path().is_file())
        body = json.loads(rates.store_path().read_text(encoding='utf-8'))
        self.assertEqual(body['rates'], {'glm-5.3': 0.78})

        cleared = rates.clear_imported()
        self.assertEqual(cleared['sources']['imported'], 0)
        self.assertFalse(rates.store_path().exists())

    def test_empty_import_does_not_wipe_existing(self) -> None:
        rates.save_imported({'glm-5.3': 0.78})
        with self.assertRaises(ValueError):
            rates.save_imported({})
        self.assertEqual(rates.imported(), {'glm-5.3': 0.78})

    def test_export_can_be_reimported(self) -> None:
        rates.save_imported({'glm-5.3': 0.78})
        exported = rates.export_payload()
        rates.clear_imported()
        with mock.patch.object(rates, 'local', return_value={}):
            rates.save_imported(exported)
            self.assertAlmostEqual(rates.imported()['glm-5.3'], 0.78)


class AdapterRatesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(config, 'DATA_DIR', Path(self.tmp.name))
        patch.start()
        self.addCleanup(patch.stop)
        self.adapter = TraeAdapter()

    def test_local_ratios_uses_merged_table_as_lowercase_keys(self) -> None:
        rates.save_imported({'GLM-5.3': 0.78})
        with mock.patch.object(rates, 'local', return_value={}):
            table = self.adapter._local_ratios()
        self.assertAlmostEqual(table['glm-5.3'], 0.78)

    def test_cached_model_without_rate_gets_bundled_default(self) -> None:
        cached = [{'id': 'glm-5.3', 'name': 'GLM-5.3', 'realm': 'cn'}]
        with mock.patch.object(self.adapter, '_cached_models',
                               return_value=cached), \
             mock.patch.object(rates, 'local', return_value={}):
            models = asyncio.run(self.adapter.list_models())
        self.assertAlmostEqual(models[0]['credit_ratio'], 0.78)

    def test_server_deployment_without_desktop_still_has_rates(self) -> None:
        """没有客户端快照、没有导入——内置默认值要兜住。"""
        with mock.patch.object(rates, 'local', return_value={}):
            summary = self.adapter.model_rates()
        self.assertEqual(summary['sources']['desktop'], 0)
        self.assertGreater(summary['sources']['bundled'], 0)
        self.assertAlmostEqual(summary['rates']['qwen3.8-max'], 1.5)


if __name__ == '__main__':
    unittest.main()
