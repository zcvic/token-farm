"""平台账号导出：能力声明、明文凭据与格式。

## 钉住的问题

平台账号列表刻意脱敏（只给 `token_preview`），但迁移到另一台面板需要完整
凭据。导出接口必须：
  · 只对会话管理员开放（权限在路由签名上，权限本身由 security 测试覆盖）；
  · 返回与导入接口同构的文档，`accounts` 直接回灌即可；
  · 虚拟平台（组合 / WorkBuddy 成员）没有自己的账号文件，不能假装支持；
  · 写审计日志——导出等于把这些账号的钥匙交出去。
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
from server.platforms import PlatformAdapter  # noqa: E402
from server.platforms.combo.adapter import ComboAdapter  # noqa: E402
from server.platforms.trae import store as trae_store  # noqa: E402
from server.routers import platforms as platforms_router  # noqa: E402


class PlatformExportCapabilityTest(unittest.TestCase):
    def test_native_platforms_advertise_export(self) -> None:
        items = {item['name']: item
                 for item in platforms_router.list_platforms({})['platforms']}
        for name in ('trae', 'qoder', 'loomy', 'dumate', 'minimax',
                     'discovery', 'kilo', 'cline'):
            with self.subTest(platform=name):
                self.assertTrue(items[name].get('supports_export'), name)

    def test_virtual_platforms_do_not_advertise_export(self) -> None:
        items = {item['name']: item
                 for item in platforms_router.list_platforms({})['platforms']}
        for name in ('combo', 'workbuddy'):
            if name in items:
                self.assertFalse(items[name].get('supports_export'), name)


class PlatformExportRouteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(config, 'DATA_DIR', Path(self.tmp.name))
        patch.start()
        self.addCleanup(patch.stop)
        self.audit = mock.patch.object(platforms_router.security, 'audit').start()
        self.addCleanup(mock.patch.stopall)

    def test_export_contains_raw_credentials(self) -> None:
        trae_store.save_account({
            'uid': 'u1',
            'nickname': 'n',
            'token': 'access-secret',
            'refreshToken': 'refresh-secret',
        })
        adapter = platforms_router._adapter('trae')
        out = asyncio.run(platforms_router.export_accounts(
            'trae', user={'username': 'admin'}))
        self.assertEqual(out['format'], 'trae-accounts')
        self.assertEqual(out['version'], 1)
        self.assertEqual(out['count'], 1)
        row = out['accounts'][0]
        self.assertEqual(row['token'], 'access-secret')
        self.assertEqual(row['refreshToken'], 'refresh-secret')
        # 列表接口仍然脱敏，导出不能反过来污染它。
        public = adapter.list_accounts()[0]
        self.assertNotIn('token', public)
        self.audit.assert_called_once()
        self.assertEqual(self.audit.call_args.args[1], 'export_platform_accounts')

    def test_export_is_reimportable_shape(self) -> None:
        trae_store.save_account({'uid': 'u1', 'token': 't', 'refreshToken': 'r'})
        out = asyncio.run(platforms_router.export_accounts(
            'trae', user={'username': 'admin'}))
        # 导入接口接受 {accounts: [...]}；导出的账号行原样就是它的入参。
        self.assertIsInstance(out['accounts'], list)
        self.assertTrue(out['accounts'])
        self.assertEqual(out['accounts'][0]['uid'], 'u1')

    def test_unsupported_platform_returns_400(self) -> None:
        from fastapi import HTTPException

        with mock.patch.object(platforms_router, '_adapter',
                               lambda name: ComboAdapter()):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(platforms_router.export_accounts(
                    'combo', user={'username': 'admin'}))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIs(ComboAdapter.export_accounts, PlatformAdapter.export_accounts)


if __name__ == '__main__':  # pragma: no cover
    unittest.main()