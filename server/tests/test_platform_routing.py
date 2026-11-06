"""多平台路由：模型前缀分流、密钥校验的版本旁路、`/v1/models` 合并。

三条约束分别对应三个容易回归的点：

  1. 只有**已注册**的平台名才算前缀 —— 否则 `owner/model` 这类写法会被误判，
     把 WorkBuddy 的存量模型打到不存在的适配器上；
  2. 密钥的**版本归属**（cn / global）描述的是腾讯账号池，平台模型不走那条路，
     但**模型白名单**仍然生效；
  3. 合并平台模型时单个平台失败不能让整份清单消失，也不能重复 id。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db, keysvc, platforms  # noqa: E402
from server.routers import gateway  # noqa: E402


class _DbCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'routing.db'
        db._conn = None
        db.connect()

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _row(self, **kw) -> dict:
        created = keysvc.create_key('t', **kw)
        return keysvc.resolve(created['key'])


class RouteModelTest(unittest.TestCase):
    def test_known_prefix_splits(self) -> None:
        self.assertEqual(platforms.route_model('trae/glm-5.2'),
                         ('trae', 'glm-5.2', None))
        # 大小写：平台名归一化，模型名**原样保留**（Trae 的 id 大小写敏感）
        self.assertEqual(platforms.route_model('Trae/DeepSeek-V4-Flash-Official'),
                         ('trae', 'DeepSeek-V4-Flash-Official', None))

    def test_realm_prefix_splits(self) -> None:
        """区域化前缀带出版本提示；旧式前缀不带（None）。"""
        self.assertEqual(platforms.route_model('traecn/glm-5.2'),
                         ('trae', 'glm-5.2', 'cn'))
        self.assertEqual(platforms.route_model('traecom/glm-5.2'),
                         ('trae', 'glm-5.2', 'global'))
        self.assertEqual(platforms.route_model('qodercn/Qwen3.8-Max'),
                         ('qoder', 'Qwen3.8-Max', 'cn'))
        self.assertEqual(platforms.route_model('QoderCOM/GLM-5'),
                         ('qoder', 'GLM-5', 'global'))
        # 区域后缀只对 realm_aware 平台生效：WorkBuddy 不是可路由平台。
        self.assertIsNone(platforms.route_model('workbuddycn/glm-5.2'))

    def test_bare_model_is_not_a_platform(self) -> None:
        self.assertIsNone(platforms.route_model('glm-5.2'))
        self.assertIsNone(platforms.route_model('cn:glm-5.2'))
        self.assertIsNone(platforms.route_model('global:gpt-5.6-sol'))

    def test_unknown_prefix_is_left_to_workbuddy(self) -> None:
        """`owner/model` 不是平台前缀：认不出就返回 None，交回原有链路。"""
        self.assertIsNone(platforms.route_model('openai/gpt-4'))
        self.assertIsNone(platforms.route_model('trae/'))
        self.assertIsNone(platforms.route_model('trae/   '))
        self.assertIsNone(platforms.route_model('traecn/'))
        self.assertIsNone(platforms.route_model(''))
        self.assertIsNone(platforms.route_model(None))

    def test_registered_names_are_stable(self) -> None:
        self.assertIn('trae', platforms.names())
        adapter = platforms.get('trae')
        self.assertIsNotNone(adapter)
        self.assertEqual(adapter.model_prefix, 'trae/')
        self.assertEqual(adapter.model_prefix_for('cn'), 'traecn/')
        self.assertEqual(adapter.model_prefix_for('global'), 'traecom/')
        self.assertEqual(adapter.model_prefix_for(None), 'trae/')


class PlatformKeyValidationTest(_DbCase):
    """版本归属对平台模型不适用，白名单仍然适用。"""

    def test_realm_scoped_key_is_not_blocked_for_platform_model(self) -> None:
        # 先验证「没有旁路时会被拦」：国际版密钥调平台模型被判成国内版模型。
        row = self._row(realm='global')
        reason = keysvc.validate(row, '1.2.3.4', 'trae/glm-5.2')
        self.assertIsNotNone(reason, '未标平台模型的请求应继续按版本归属判断')
        self.assertIsNone(keysvc.validate(row, '1.2.3.4', 'trae/glm-5.2',
                                          platform_model=True))

        row = self._row(realm='cn')
        self.assertIsNone(keysvc.validate(row, '1.2.3.4', 'trae/gpt-5.5',
                                          platform_model=True))

    def test_whitelist_still_applies_to_platform_models(self) -> None:
        row = self._row(realm='global', models=['trae/glm-5.2'])
        self.assertIsNone(keysvc.validate(row, '1.2.3.4', 'trae/glm-5.2',
                                          platform_model=True))
        reason = keysvc.validate(row, '1.2.3.4', 'trae/kimi-k3',
                                 platform_model=True)
        self.assertIsNotNone(reason, '白名单不该因为平台旁路而失效')
        self.assertIn('白名单', reason)

    def test_legacy_whitelist_allows_both_realm_prefixes(self) -> None:
        """旧式 `trae/foo` 不携带区域：两个区域的调用都放行。"""
        row = self._row(models=['trae/glm-5.2'])
        for name in ('trae/glm-5.2', 'traecn/glm-5.2', 'traecom/glm-5.2'):
            self.assertIsNone(keysvc.validate(row, '1.2.3.4', name,
                                              platform_model=True), name)

    def test_realm_whitelist_is_strict(self) -> None:
        """区域化白名单只放行同区域；旧式调用仍可作为兼容入口。"""
        row = self._row(models=['traecn/glm-5.2'])
        self.assertIsNone(keysvc.validate(row, '1.2.3.4', 'traecn/glm-5.2',
                                          platform_model=True))
        self.assertIsNotNone(keysvc.validate(row, '1.2.3.4', 'traecom/glm-5.2',
                                             platform_model=True))
        self.assertIsNone(keysvc.validate(row, '1.2.3.4', 'trae/glm-5.2',
                                          platform_model=True),
                          '旧式调用不带区域，兼容放行')

    def test_plain_models_keep_old_behaviour(self) -> None:
        """不传 platform_model 时逐字沿用原判定（WorkBuddy 存量行为不变）。"""
        row = self._row(realm='cn')
        self.assertIsNone(keysvc.validate(row, '1.2.3.4', 'glm-5.2'))
        self.assertIsNotNone(keysvc.validate(row, '1.2.3.4', 'global:gpt-5.6-sol'))


class MergeModelsTest(_DbCase):
    def setUp(self) -> None:
        super().setUp()
        # 目录缓存在 DB 里；测试固定走内置目录，避免受本机历史数据影响
        patch = mock.patch.object(db, 'get_setting', return_value=None)
        patch.start()
        self.addCleanup(patch.stop)
        # 也不要触发「首次隐式拉取线上目录」——那会真的发网络请求
        patch = mock.patch.object(platforms.get('trae'), '_implicit_live_allowed',
                                  return_value=False)
        patch.start()
        self.addCleanup(patch.stop)

    def _merge(self, payload, key=None):
        return asyncio.run(gateway._merge_platform_models(payload, key))

    def test_platform_models_are_merged_with_prefix(self) -> None:
        payload = {'object': 'list', 'data': [
            {'id': 'glm-5.2', 'object': 'model', 'owned_by': 'tencent'}]}
        merged = self._merge(payload)
        ids = [m['id'] for m in merged['data']]
        self.assertIn('glm-5.2', ids, 'WorkBuddy 的模型不能被顶掉')
        # 模型列表按区域给 ID；旧式 `trae/glm-5.2` 仍能调用但不是展示名。
        self.assertIn('traecn/glm-5.2', ids)
        trae = [m for m in merged['data'] if m['id'] == 'traecn/glm-5.2'][0]
        self.assertEqual(trae['owned_by'], 'trae')
        self.assertEqual(trae['platform'], 'trae')
        self.assertEqual(trae['model_name'], 'glm-5.2')
        self.assertEqual(trae['realm'], 'cn')

    def test_merge_is_idempotent(self) -> None:
        payload = {'object': 'list', 'data': []}
        once = self._merge(payload)
        twice = self._merge(once)
        ids = [m['id'] for m in twice['data']]
        self.assertEqual(len(ids), len(set(ids)))

    def test_whitelist_filters_platform_models(self) -> None:
        payload = {'object': 'list', 'data': [
            {'id': 'glm-5.2', 'object': 'model', 'owned_by': 'tencent'}]}
        key = {'realm': '', 'models': ['trae/kimi-k3']}
        merged = self._merge(payload, key)
        trae_ids = [m['id'] for m in merged['data']
                    if m.get('platform') == 'trae']
        self.assertEqual(trae_ids, ['traecn/kimi-k3'])
        # 白名单只约束「客户端能发哪些名字」，WorkBuddy 的清单不由它裁剪
        self.assertIn('glm-5.2', [m['id'] for m in merged['data']])

    def test_realm_whitelist_keeps_only_its_region(self) -> None:
        payload = {'object': 'list', 'data': []}
        key = {'realm': '', 'models': ['traecn/glm-5.2']}
        ids = [m['id'] for m in self._merge(payload, key)['data']]
        self.assertIn('traecn/glm-5.2', ids)
        self.assertNotIn('traecom/glm-5.2', ids)

    def test_broken_platform_does_not_break_listing(self) -> None:
        payload = {'object': 'list', 'data': [
            {'id': 'glm-5.2', 'object': 'model', 'owned_by': 'tencent'}]}
        trae = platforms.get('trae')
        with mock.patch.object(trae, 'list_models', side_effect=RuntimeError('boom')):
            merged = self._merge(payload)
        ids = [m['id'] for m in merged['data']]
        self.assertIn('glm-5.2', ids)
        # 坏掉的平台一条都不能混进来，其他平台照常
        self.assertFalse([m for m in merged['data']
                          if m.get('platform') == 'trae'])

    def test_non_listing_payload_is_returned_unchanged(self) -> None:
        payload = {'error': {'message': 'x'}}
        self.assertIs(self._merge(payload), payload)


if __name__ == '__main__':
    unittest.main()
