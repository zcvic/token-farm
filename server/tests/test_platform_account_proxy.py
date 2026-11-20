"""平台账号的出口线路绑定（账号级代理）。

## 钉住的问题

国际站账号（MiniMax `global`、Trae 国际版）常被上游要求走境外出口。适配器与
账号文件一直支持 `proxy` 字段（`config.resolve_account_proxy` 会读），但面板
既没有接口也没有入口：用户只能手改凭据 JSON，而报错信息又让人「绑定境外代理」。

这里保证：
  · 四个原生平台都能写 / 清这个字段，且清空是真的把键删掉（不是留空串）；
  · `/api/platforms` 如实声明能力，Qoder（网关选号）不显示点了必然 400 的入口；
  · 路由会先用 `config.account_proxy` 校验线路名，不会把永远解析不了的线路落盘。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config  # noqa: E402
from server.platforms.dumate import store as dumate_store  # noqa: E402
from server.platforms.loomy import store as loomy_store  # noqa: E402
from server.platforms.minimax import store as minimax_store  # noqa: E402
from server.platforms.trae import store as trae_store  # noqa: E402
from server.routers import platforms as platforms_router  # noqa: E402

_STORES = {
    'trae': trae_store,
    'loomy': loomy_store,
    'dumate': dumate_store,
    'minimax': minimax_store,
}


class _TempData(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(config, 'DATA_DIR', Path(self.tmp.name))
        patch.start()
        self.addCleanup(patch.stop)


class StoreSetProxyTest(_TempData):
    def test_each_native_store_writes_and_clears(self) -> None:
        for name, store in _STORES.items():
            with self.subTest(platform=name):
                store.save_account({'uid': 'u1', 'nickname': 'n'})
                saved = store.set_proxy('u1', '  hk-1  ')
                self.assertEqual(saved['proxy'], 'hk-1')
                self.assertEqual(store.read_account('u1')['proxy'], 'hk-1')
                self.assertEqual(store.public_account(
                    store.read_account('u1'))['proxy'], 'hk-1')
                store.set_proxy('u1', '')
                self.assertNotIn('proxy', store.read_account('u1'),
                                 '清空要删键，不能留一个空串')

    def test_direct_route_is_kept_verbatim(self) -> None:
        for name, store in _STORES.items():
            with self.subTest(platform=name):
                store.save_account({'uid': 'u2'})
                store.set_proxy('u2', '__direct__')
                self.assertEqual(store.read_account('u2')['proxy'], '__direct__')

    def test_missing_account_raises(self) -> None:
        for name, store in _STORES.items():
            with self.subTest(platform=name):
                with self.assertRaises(FileNotFoundError):
                    store.set_proxy('nope', 'hk-1')


class PlatformCapabilityTest(unittest.TestCase):
    def test_only_native_platforms_advertise_proxy(self) -> None:
        items = {item['name']: item
                 for item in platforms_router.list_platforms({})['platforms']}
        for name in _STORES:
            self.assertTrue(items[name].get('supports_proxy'), name)
        self.assertFalse(items['qoder'].get('supports_proxy'))
        self.assertFalse(items.get('combo', {}).get('supports_proxy', False))

    def test_base_rejects_proxy_for_qoder(self) -> None:
        from server.platforms.base import PlatformError
        from server.platforms.qoder.adapter import QoderAdapter

        adapter = QoderAdapter.__new__(QoderAdapter)
        with self.assertRaises(PlatformError) as ctx:
            asyncio.run(QoderAdapter.set_account_proxy(adapter, 'u1', 'hk-1'))
        self.assertEqual(ctx.exception.code, 'account_proxy_unsupported')


class UpdateAccountProxyRouteTest(_TempData):
    def test_route_persists_a_known_route(self) -> None:
        minimax_store.save_account({'uid': 'u1'})
        adapter = mock.Mock()
        adapter.set_account_proxy.return_value = {'uid': 'u1', 'proxy': 'hk-1'}
        with mock.patch.object(platforms_router, '_adapter', lambda name: adapter), \
             mock.patch.object(platforms_router.config, 'account_proxy',
                               lambda auth: 'socks5://127.0.0.1:7890'):
            out = asyncio.run(platforms_router.update_account_options(
                'minimax', 'u1', {'proxy': 'hk-1'}, user={'username': 'admin'}))
        self.assertEqual(out['account']['proxy'], 'hk-1')
        adapter.set_account_proxy.assert_called_once_with('u1', 'hk-1')

    def test_route_rejects_unknown_route_before_writing(self) -> None:
        from fastapi import HTTPException

        adapter = mock.Mock()
        with mock.patch.object(platforms_router, '_adapter', lambda name: adapter), \
             mock.patch.object(platforms_router.config, 'account_proxy',
                               side_effect=ValueError('线路不存在')):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(platforms_router.update_account_options(
                    'minimax', 'u1', {'proxy': 'gone'}, user={'username': 'admin'}))
        self.assertEqual(ctx.exception.status_code, 400)
        adapter.set_account_proxy.assert_not_called()

    def test_route_rejects_non_string(self) -> None:
        from fastapi import HTTPException

        adapter = mock.Mock()
        with mock.patch.object(platforms_router, '_adapter', lambda name: adapter):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(platforms_router.update_account_options(
                    'minimax', 'u1', {'proxy': 123}, user={'username': 'admin'}))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_empty_proxy_means_follow_pool(self) -> None:
        adapter = mock.Mock()
        adapter.set_account_proxy.return_value = {'uid': 'u1', 'proxy': ''}
        with mock.patch.object(platforms_router, '_adapter', lambda name: adapter), \
             mock.patch.object(platforms_router.config, 'account_proxy',
                               lambda auth: None):
            asyncio.run(platforms_router.update_account_options(
                'minimax', 'u1', {'proxy': ''}, user={'username': 'admin'}))
        adapter.set_account_proxy.assert_called_once_with('u1', '')


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
