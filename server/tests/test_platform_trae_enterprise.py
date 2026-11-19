"""Trae 企业版 / SaaS 账号护栏（来源 trae2api-hub c724a65）。

企业租户与个人版使用不同协议：把企业凭据塞进个人版刷新链只会得到
401/20405 一类含混错误。护栏只在**看到明确租户元数据**时拒绝，
不根据裸 token 猜，也不递归扫描用户正文。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import config, db
from server.platforms.base import PlatformError
from server.platforms.trae import client, desktop, enterprise, store
from server.platforms.trae.adapter import TraeAdapter


class DetectionTest(unittest.TestCase):

    def test_explicit_enterprise_metadata(self) -> None:
        cases = [
            {'realm': 'enterprise'},
            {'realm': 'SaaS'},
            {'account': {'scope': 'saas'}},
            {'Result': {'Data': {'TenantInfoBase': {'tenantId': 't-1'}}}},
            {'Data': {'UserInfo': {'loginScope': 'saas'}}},
            {'extra': {'saasBootConfig': {'tenant': 'acme'}}},
        ]
        for value in cases:
            with self.subTest(value=value):
                self.assertTrue(enterprise.is_enterprise_account(value))

    def test_personal_shapes_are_not_enterprise(self) -> None:
        cases = [
            {},
            {'realm': 'cn'},
            {'realm': 'intl'},
            {'scope': 'user'},
            {'Data': {'TenantInfoBase': {}}},
            {'Result': {'UserInfo': {'uid': 'u-1', 'scope': 'personal'}}},
            # 正文里的同名字段不算租户证据：只沿具名节点看。
            {'messages': [{'realm': 'enterprise'}]},
        ]
        for value in cases:
            with self.subTest(value=value):
                self.assertFalse(enterprise.is_enterprise_account(value))


class _TempData(unittest.TestCase):
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


class ImportGuardTest(_TempData):

    def test_import_rejects_enterprise_payload_without_writing(self) -> None:
        for payload in (
            {'uid': 'e-1', 'token': 't', 'realm': 'enterprise'},
            {'account': {'uid': 'e-2', 'token': 't'},
             'Data': {'TenantInfoBase': {'tenantId': 't-1'}}},
            {'uid': 'e-3', 'token': 't', 'scope': 'saas'},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(PlatformError) as caught:
                    self.adapter.import_account(payload)
                self.assertEqual(caught.exception.status, 400)
                self.assertEqual(caught.exception.code,
                                 'unsupported_enterprise_account')
        self.assertEqual(store.list_accounts(), [])

    def test_personal_account_still_imports(self) -> None:
        saved = self.adapter.import_account(
            {'uid': 'p-1', 'token': 't', 'realm': 'cn'})
        self.assertEqual(saved['uid'], 'p-1')
        self.assertEqual(store.read_account('p-1')['token'], 't')


class ResponseGuardTest(unittest.TestCase):

    def test_token_result_rejects_enterprise_response(self) -> None:
        parsed = {'Result': {'Token': 'token-1'},
                  'Data': {'TenantInfoBase': {'tenantId': 't-1'}}}
        with self.assertRaises(PlatformError) as caught:
            client._token_result(parsed, 'cn')
        self.assertEqual(caught.exception.code,
                         'unsupported_enterprise_account')

    def test_desktop_body_rejects_enterprise(self) -> None:
        with self.assertRaises(PlatformError):
            desktop._account_from_body(
                {'userId': 'e-1', 'token': 't', 'scope': 'saas'}, 'cn')


if __name__ == '__main__':
    unittest.main()