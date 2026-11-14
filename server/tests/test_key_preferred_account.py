"""密钥的「优先账号」：存储、校验、转发头三条链路各钉一遍。

功能语义（与上游实现对齐）：一把密钥可以指定「先花哪个账号的积分」，但它**只是
优先**——该账号冷却 / 被禁用 / 在途占满 / 模型被限流时，上游会静默回落到普通
选号。所以本文件既测「设了能带到上游」，也测「坏值不会把请求弄挂」。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi import Request  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from server import config, db, keysvc  # noqa: E402
from server.routers import gateway, keys  # noqa: E402
from server.services import requestctx, wbforward  # noqa: E402


class PreferredAccountStoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        d = Path(cls._tmp.name)
        cls._orig = (config.DB_PATH, config.USERS_FILE, config.STATIC_DIR)
        config.DB_PATH = d / 'k.db'
        config.USERS_FILE = d / 'users.json'
        config.STATIC_DIR = d / 'no-static'
        config.USERS_FILE.write_text(json.dumps({
            'secret': 'S' * 64,
            'users': [{'username': 'admin', 'role': 'admin', 'pwd_hash': 'x'}],
            'api_keys': [],
        }), encoding='utf-8')
        db._conn = None
        db.connect()

    @classmethod
    def tearDownClass(cls) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH, config.USERS_FILE, config.STATIC_DIR = cls._orig
        try:
            cls._tmp.cleanup()
        except PermissionError:
            pass

    def test_default_is_unset(self) -> None:
        made = keysvc.create_key('pref-default')
        self.assertEqual(made['preferred_account'], '')
        self.assertEqual(keysvc.resolve(made['key'])['preferred_account'], '')

    def test_create_and_update_then_clear(self) -> None:
        made = keysvc.create_key('pref', preferred_account='acct-1')
        self.assertEqual(made['preferred_account'], 'acct-1')
        # 列表与网关解析路径都要带上这一列（否则 UI 看不到、转发也拿不到）
        self.assertEqual(keysvc.resolve(made['key'])['preferred_account'], 'acct-1')
        listed = {k['id']: k for k in keysvc.list_keys()}
        self.assertEqual(listed[made['id']]['preferred_account'], 'acct-1')

        updated = keysvc.update_key(made['id'], {'preferred_account': 'acct-2'})
        self.assertEqual(updated['preferred_account'], 'acct-2')
        # 显式空串 = 清除（与「没提交该字段」必须区分开）
        cleared = keysvc.update_key(made['id'], {'preferred_account': ''})
        self.assertEqual(cleared['preferred_account'], '')
        # 没提交该字段时不能被顺手清空
        keysvc.update_key(made['id'], {'preferred_account': 'acct-3'})
        kept = keysvc.update_key(made['id'], {'name': 'renamed'})
        self.assertEqual(kept['preferred_account'], 'acct-3')

    def test_invalid_uid_rejected(self) -> None:
        for bad in ['has space', 'a\nb', '中文', 'x' * 129, ['x'], {'uid': 'x'}]:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    keysvc._norm_preferred_account(bad)
        for good in ['', None, '  acct-1  ', 'a.b_c-9:0']:
            with self.subTest(good=good):
                self.assertIn(keysvc._norm_preferred_account(good),
                              ('', 'acct-1', 'a.b_c-9:0'))

    def test_router_turns_valueerror_into_400(self) -> None:
        for bad in ['has space', 'x' * 129, '中文']:
            with self.subTest(bad=bad):
                with self.assertRaises(HTTPException) as ctx:
                    keys._check_preferred_account(bad)
                self.assertEqual(ctx.exception.status_code, 400)
        keys._check_preferred_account('')        # 清除是合法的
        keys._check_preferred_account(None)      # PATCH 不带该字段


class PreferredAccountHeaderTest(unittest.TestCase):
    def tearDown(self) -> None:
        requestctx.set_preferred_account('')     # contextvar 不能漏到别的用例

    def test_header_only_when_set(self) -> None:
        upstream = {'base_url': 'http://up.example', 'api_key': 'k'}
        self.assertNotIn(requestctx.PREFERRED_ACCOUNT_HEADER,
                         wbforward.upstream_headers(upstream))
        requestctx.set_preferred_account('acct-1')
        headers = wbforward.upstream_headers(upstream)
        self.assertEqual(headers[requestctx.PREFERRED_ACCOUNT_HEADER], 'acct-1')
        # 其他头不受影响（Content-Type / 鉴权仍按原逻辑）
        self.assertEqual(headers['Content-Type'], 'application/json')
        self.assertEqual(headers['Authorization'], 'Bearer k')

    def test_context_filters_header_unsafe_values(self) -> None:
        for bad in ['has space', 'a\nb', '中文', 'x' * 129]:
            with self.subTest(bad=bad):
                requestctx.set_preferred_account(bad)
                self.assertEqual(requestctx.preferred_account(), '')
        requestctx.set_preferred_account('  acct-1  ')
        self.assertEqual(requestctx.preferred_account(), 'acct-1')

    def test_authorize_publishes_key_preference(self) -> None:
        """鉴权成功后，密钥里的优先账号必须进入请求上下文（不需要调用方再传）。"""
        scope = {
            'type': 'http', 'method': 'POST', 'path': '/v1/chat/completions',
            'headers': [(b'authorization', b'Bearer test-token')],
            'client': ('1.2.3.4', 1234), 'server': ('testserver', 80),
            'scheme': 'http', 'query_string': b'',
        }
        key = {'id': 1, 'preferred_account': 'acct-1'}
        with mock.patch.object(gateway.iputil, 'client_ip', return_value='1.2.3.4'), \
                mock.patch.object(gateway.keysvc, 'resolve', return_value=key), \
                mock.patch.object(gateway, '_global_ip_blocked', return_value=False), \
                mock.patch.object(gateway.keysvc, 'validate', return_value=None), \
                mock.patch.object(gateway, '_rate_limited', return_value=(False, 0)), \
                mock.patch.object(gateway, '_log_ip'):
            got, _ip, err = gateway._authorize(Request(scope), 'glm-5.2')
        self.assertIsNone(err)
        self.assertIs(got, key)
        self.assertEqual(requestctx.preferred_account(), 'acct-1')


if __name__ == '__main__':
    unittest.main()
