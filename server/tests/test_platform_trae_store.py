"""Trae 账号存储：路径安全、原子落盘、合并更新与凭据脱敏。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import config
from server.platforms.base import PlatformError
from server.platforms.trae.adapter import adapter as trae_adapter
from server.platforms.trae import store


class TraeStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(config, 'DATA_DIR', Path(self.tmp.name))
        patch.start()
        self.addCleanup(patch.stop)

    def test_account_path_rejects_traversal(self) -> None:
        for bad in ('../etc/passwd', 'a/b', 'a\\b', '', '   ', 'x' * 200,
                    'uid\x00', '..', '.'):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    store.account_path(bad)

    def test_save_merges_old_fields(self) -> None:
        store.save_account({'uid': '1234567890123456789', 'token': 'old-token',
                            'refreshToken': 'refresh-1', 'nickname': '昵称',
                            'realm': 'cn', 'proxy': '__direct__'})
        # 刷新只带回 token：昵称 / 代理必须保留
        saved = store.save_account({'uid': '1234567890123456789',
                                    'token': 'new-token',
                                    'refreshToken': 'refresh-2'})
        self.assertEqual(saved['nickname'], '昵称')
        self.assertEqual(saved['proxy'], '__direct__')
        self.assertEqual(saved['refreshToken'], 'refresh-2')
        self.assertEqual(saved['token'], 'new-token')
        self.assertGreater(saved['createdAt'], 0)
        self.assertGreaterEqual(saved['updatedAt'], saved['createdAt'])

    def test_atomic_write_leaves_valid_json(self) -> None:
        store.save_account({'uid': '42', 'token': 't', 'nickname': 'n'})
        path = store.account_path('42')
        # 目录里不应残留临时文件
        leftovers = [p.name for p in path.parent.iterdir() if p.name.endswith('.tmp')]
        self.assertEqual(leftovers, [])
        raw = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(raw['uid'], '42')

    def test_list_and_delete(self) -> None:
        store.save_account({'uid': '2', 'token': 't2'})
        store.save_account({'uid': '1', 'token': 't1'})
        ids = [a['uid'] for a in store.list_accounts()]
        self.assertEqual(ids, ['1', '2'])
        self.assertTrue(store.delete_account('1'))
        self.assertFalse(store.delete_account('1'))
        self.assertEqual([a['uid'] for a in store.list_accounts()], ['2'])

    def test_public_account_hides_secrets(self) -> None:
        saved = store.save_account({
            'uid': '999', 'token': 'secret-access-token-value',
            'refreshToken': 'secret-refresh-token-value', 'nickname': 'n',
        })
        public = store.public_account(saved)
        dumped = json.dumps(public, ensure_ascii=False)
        self.assertNotIn('secret-access-token-value', dumped)
        self.assertNotIn('secret-refresh-token-value', dumped)
        self.assertNotIn('token', public)
        self.assertNotIn('refreshToken', public)
        self.assertTrue(public['has_token'])
        self.assertTrue(public['has_refresh_token'])

    def test_import_normalizes_and_requires_credentials(self) -> None:
        adapter = trae_adapter
        imported = adapter.import_account({
            'uid': 123, 'accessToken': 'tok', 'refreshToken': 'ref',
            'realm': 'global', 'name': 'Alice', 'expiresAt': 1893456000000,
        })
        self.assertEqual(imported['uid'], '123')
        self.assertEqual(imported['realm'], 'intl')
        self.assertEqual(imported['nickname'], 'Alice')
        self.assertTrue(imported['expiredAt'].startswith('2030-01-01'))
        with self.assertRaises(PlatformError):
            adapter.import_account({'uid': 'x'})


if __name__ == '__main__':
    unittest.main()
