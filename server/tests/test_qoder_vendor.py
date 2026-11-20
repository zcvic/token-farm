"""Qoder 内置网关（`_vendor/`）的两条本地约定。

`server/platforms/qoder/_vendor/` 是 qoder2api-hub 的源码副本，升级时整目录
覆盖（`tools/vendor_qoder.py`）。覆盖会冲掉唯一一处本地补丁 —— **账号权重**，
所以这里用离线断言把它钉住：

  · `Account` 读出 / 写回 `weight`，非法值一律归 0（= 未设置）；
  · `AccountPool.set_weight` 的 0 / 区间 / 非数值语义与 Trae 一致；
  · 没有账号设置权重时选号仍是纯轮转，设置后按权重分摊；
  · `qoder_proxy` 里保留 `POST /accounts/weight` 路由。

测试不联网、不启动回环网关：只用临时账号目录与纯函数。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import unittest

from server.platforms.qoder._vendor import qoder_accounts


def _write_account(directory: str, uid: str, **extra) -> None:
    data = {
        'uid': uid,
        'realm': 'cn',
        'accessToken': 'token-' + uid,
        'refreshToken': 'refresh-' + uid,
        'expiresAt': 4102444800,       # 2100-01-01，别让它进刷新窗口
    }
    data.update(extra)
    with open(os.path.join(directory, uid + '.json'), 'w',
              encoding='utf-8') as fh:
        json.dump(data, fh)


class AccountWeightFieldTest(unittest.TestCase):

    def test_missing_weight_is_zero(self) -> None:
        account = qoder_accounts.Account({'uid': 'u-1'})
        self.assertEqual(account.weight, 0.0)
        self.assertEqual(account.public()['weight'], 0.0)
        self.assertEqual(account.to_dict()['weight'], 0.0)

    def test_invalid_weights_normalize_to_zero(self) -> None:
        for raw in (None, '', 'abc', -1, 0, float('nan'), float('inf')):
            with self.subTest(raw=raw):
                account = qoder_accounts.Account({'uid': 'u-1', 'weight': raw})
                self.assertEqual(account.weight, 0.0)

    def test_valid_weight_survives_save_and_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_account(tmp, 'u-1', weight=2.5)
            pool = qoder_accounts.AccountPool(tmp)
            pool.load()
            account = pool.get('u-1')
            self.assertIsNotNone(account)
            self.assertEqual(account.weight, 2.5)
            account.save(tmp)
            with open(os.path.join(tmp, 'u-1.json'), encoding='utf-8') as fh:
                self.assertEqual(json.load(fh)['weight'], 2.5)


class SetWeightTest(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        _write_account(self.dir, 'u-1')
        self.pool = qoder_accounts.AccountPool(self.dir)
        self.pool.load()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_zero_restores_default(self) -> None:
        updated = self.pool.set_weight('u-1', 3)
        self.assertEqual(updated['weight'], 3)
        updated = self.pool.set_weight('u-1', 0)
        self.assertEqual(updated['weight'], 0)

    def test_unknown_uid_returns_none(self) -> None:
        self.assertIsNone(self.pool.set_weight('missing', 1))

    def test_out_of_range_and_non_numeric_are_rejected(self) -> None:
        for raw in ('abc', None, 0.05, 100.5, float('nan'), float('inf')):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    self.pool.set_weight('u-1', raw)

    def test_weight_does_not_touch_credentials(self) -> None:
        before = self.pool.get('u-1')
        self.pool.set_weight('u-1', 2)
        after = self.pool.get('u-1')
        self.assertEqual(after.access_token, before.access_token)
        self.assertEqual(after.refresh_token, before.refresh_token)
        self.assertEqual(after.expires_at, before.expires_at)


class WeightedSelectionTest(unittest.TestCase):

    def _pool(self, weights: dict[str, float]) -> qoder_accounts.AccountPool:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for uid, weight in weights.items():
            _write_account(tmp.name, uid, weight=weight)
        pool = qoder_accounts.AccountPool(tmp.name)
        pool.load()
        return pool

    def test_no_weight_keeps_plain_round_robin(self) -> None:
        pool = self._pool({'a': 0, 'b': 0})
        picks = [pool.pick(realm='cn').uid for _ in range(4)]
        self.assertEqual(picks, ['a', 'b', 'a', 'b'])

    def test_weight_scales_the_share(self) -> None:
        """权重 3:1 → 40 次选号应当是 30:10（平滑加权轮转，不是随机）。"""
        pool = self._pool({'a': 3, 'b': 1})
        counts = {'a': 0, 'b': 0}
        for _ in range(40):
            counts[pool.pick(realm='cn').uid] += 1
        self.assertEqual(counts, {'a': 30, 'b': 10})

    def test_expiry_weight_still_multiplies(self) -> None:
        """账号权重不能覆盖「临期优先」：只与临期权重相乘（补丁落点守卫）。"""
        pool = self._pool({'a': 2, 'b': 2})
        account = pool.get('a')
        self.assertEqual(pool._expiry_weight(account), 1)
        self.assertEqual(
            qoder_accounts.selection_weight(account), 2.0)
        path = os.path.join(os.path.dirname(qoder_accounts.__file__),
                            'qoder_accounts.py')
        with open(path, encoding='utf-8') as fh:
            source = fh.read()
        self.assertIn(
            'weights = [self._expiry_weight(a) * selection_weight(a)',
            source)


class WeightRouteTest(unittest.TestCase):

    def test_proxy_handler_keeps_the_weight_route(self) -> None:
        path = os.path.join(os.path.dirname(qoder_accounts.__file__),
                            'qoder_proxy.py')
        with open(path, encoding='utf-8') as fh:
            source = fh.read()
        self.assertIn('"/accounts/weight"', source)
        self.assertIn('POOL.set_weight(uid, payload.get("weight"))', source)


class VendorManifestTest(unittest.TestCase):
    """`tools/vendor_qoder.py` 的模块清单必须覆盖副本里的每一条包内导入。

    1.9.12 起 `qoder_proxy.py` 在**顶层**就 `from . import qoder_web_response`，
    清单漏一个文件不是「少个可选能力」，而是内置网关直接 ImportError、整个
    Qoder 平台起不来。所以这里正向校验：副本里出现的每个 `qoder_*` 模块都
    必须被清单收录、且文件真的在。
    """

    def _manifest(self) -> list[str]:
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(qoder_accounts.__file__))), 'tools', 'vendor_qoder.py')
        spec = importlib.util.spec_from_file_location('_vendor_qoder_manifest', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return list(module.MODULES)

    def test_every_vendored_import_is_listed(self) -> None:
        manifest = self._manifest()
        listed = {name[:-3] if name.endswith('.py') else name for name in manifest}
        vendor_dir = os.path.dirname(os.path.abspath(qoder_accounts.__file__))
        referenced: set[str] = set()
        pattern = re.compile(r'^\s*(?:from \. import (qoder_\w+)|from \.qoder_(\w+) import)',
                             re.M)
        for name in os.listdir(vendor_dir):
            if not name.endswith('.py'):
                continue
            with open(os.path.join(vendor_dir, name), encoding='utf-8') as fh:
                for match in pattern.finditer(fh.read()):
                    referenced.add(match.group(1) or ('qoder_' + match.group(2)))
        missing = sorted(referenced - listed)
        self.assertEqual(missing, [], 'vendor_qoder.py 的 MODULES 漏了：%s' % missing)
        for name in manifest:
            self.assertTrue(os.path.isfile(os.path.join(vendor_dir, name)),
                            '清单里的 %s 不在 _vendor/ 里' % name)

    def test_web_tool_modules_are_shipped(self) -> None:
        """1.9.12 的三种联网后端各自一个模块，缺一个就少一种后端。"""
        manifest = self._manifest()
        for name in ('qoder_webtools.py', 'qoder_web_response.py',
                     'qoder_official_web.py', 'qoder_external_web.py'):
            self.assertIn(name, manifest)


if __name__ == '__main__':
    unittest.main()
