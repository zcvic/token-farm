"""本机 Trae 客户端登录态的导入 / 回写（离线）。

加密实现是纯 Python 的，**必须**用官方测试向量钉住：算法写错时解出来的只是
乱码，而「乱码」在导入流程里很容易被当成「客户端没登录」而误判。
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import config
from server.platforms.trae import desktop, icube, store
from server.platforms.trae.adapter import adapter as trae_adapter


def _body(**kw) -> dict:
    body = {
        'userId': '1839902393428953',
        'token': 'access-token-1',
        'refreshToken': 'refresh-token-1',
        'expiredAt': '2030-01-01T00:00:00.000Z',
        'refreshExpiredAt': '2030-02-01T00:00:00.000Z',
        'userRegion': {'region': 'CN', '_aiRegion': 'CN'},
        'host': 'https://api.trae.cn',
        'account': {'username': '阿黎', 'email': 'a@example.com',
                    'storeCountryCode': 'cn'},
    }
    body.update(kw)
    return body


class IcubeCryptoTest(unittest.TestCase):
    def test_fips_197_aes128_vector(self) -> None:
        key = bytes(range(16))
        plain = bytes.fromhex('00112233445566778899aabbccddeeff')
        cipher = bytes.fromhex('69c4e0d86a7b0430d8cdb78070b4c55a')
        round_keys = icube._expand_key(key)
        self.assertEqual(icube.encrypt_block(plain, round_keys), cipher)
        self.assertEqual(icube.decrypt_block(cipher, round_keys), plain)

    def test_blob_round_trip(self) -> None:
        payload = _body()
        key_material = bytes(range(32))
        blob = icube.encode(payload, key_material)
        decoded, material = icube.decode(blob)
        self.assertEqual(decoded, payload)
        self.assertEqual(material, key_material)

    def test_blob_round_trip_with_unicode(self) -> None:
        payload = {'note': '中文' * 500}
        self.assertEqual(icube.decode(icube.encode(payload))[0], payload)

    def test_rejects_bad_envelope(self) -> None:
        with self.assertRaises(ValueError):
            icube.decode('')
        with self.assertRaises(ValueError):
            icube.decode('!!!not-base64!!!')
        with self.assertRaises(ValueError):
            icube.decode('dGM=')  # 只有 magic

    def test_rejects_tampered_ciphertext(self) -> None:
        blob = icube.encode(_body())
        raw = bytearray(__import__('base64').b64decode(blob))
        raw[-1] ^= 0xFF
        with self.assertRaises(ValueError):
            icube.decode(__import__('base64').b64encode(bytes(raw)).decode('ascii'))


class DesktopTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'Trae CN'
        self.storage = self.root / 'User' / 'globalStorage' / 'storage.json'
        self.storage.parent.mkdir(parents=True)
        self._write_storage({'iCubeAuthInfo://icube.cloudide': icube.encode(_body()),
                             'other.setting': 1})
        patch = mock.patch.dict(os.environ, {'TRAE_DESKTOP_DIRS': str(self.root)})
        patch.start()
        self.addCleanup(patch.stop)

    def _write_storage(self, data: dict) -> None:
        self.storage.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')

    def _storage(self) -> dict:
        return json.loads(self.storage.read_text(encoding='utf-8'))

    def test_storage_paths_respect_override(self) -> None:
        paths = desktop.storage_paths()
        self.assertEqual([p['path'] for p in paths], [self.storage])
        self.assertEqual(paths[0]['realm'], 'cn')

    def test_read_accounts(self) -> None:
        accounts = desktop.read_accounts()
        self.assertEqual(len(accounts), 1)
        account = accounts[0]
        self.assertEqual(account['uid'], '1839902393428953')
        self.assertEqual(account['realm'], 'cn')
        self.assertEqual(account['token'], 'access-token-1')
        self.assertEqual(account['refreshToken'], 'refresh-token-1')
        self.assertEqual(account['nickname'], '阿黎')
        self.assertEqual(account['email'], 'a@example.com')
        self.assertEqual(account['desktopPath'], str(self.storage))

    def test_read_accounts_skips_broken_blob(self) -> None:
        self._write_storage({'iCubeAuthInfo://icube.cloudide': 'broken',
                             'other.setting': 1})
        self.assertEqual(desktop.read_accounts(), [])

    def test_read_accounts_skips_non_auth_keys(self) -> None:
        self._write_storage({
            'iCubeAuthInfo://icube.cloudide': icube.encode(_body()),
            'iCubeAuthInfo://usertag': icube.encode({'tag': 'x'}),
        })
        self.assertEqual(len(desktop.read_accounts()), 1)

    def test_sync_writes_back_rotated_credentials(self) -> None:
        account = desktop.read_accounts()[0]
        result = desktop.sync({**account, 'token': 'access-token-2',
                               'refreshToken': 'refresh-token-2'})
        self.assertTrue(result['ok'], result)
        storage = self._storage()
        body, _ = icube.decode(storage['iCubeAuthInfo://icube.cloudide'])
        self.assertEqual(body['token'], 'access-token-2')
        self.assertEqual(body['refreshToken'], 'refresh-token-2')
        # 其他键与无关设置保持原样
        self.assertEqual(storage['other.setting'], 1)
        self.assertEqual(body['userId'], '1839902393428953')
        self.assertTrue(self.storage.with_name(self.storage.name
                                                + desktop.BACKUP_SUFFIX).is_file())
        # 内存里不该残留临时文件
        leftovers = [p.name for p in self.storage.parent.iterdir()
                     if p.name.startswith('.trae-storage-')]
        self.assertEqual(leftovers, [])

    def test_fresher_only_returns_changed_token(self) -> None:
        account = desktop.read_accounts()[0]
        self.assertIsNone(desktop.fresher(account), '内容一致时不该覆盖')
        self._write_storage({'iCubeAuthInfo://icube.cloudide':
                             icube.encode(_body(token='access-token-9'))})
        fresh = desktop.fresher(account)
        self.assertIsNotNone(fresh)
        self.assertEqual(fresh['token'], 'access-token-9')

    def test_sync_without_storage_is_reported_not_raised(self) -> None:
        result = desktop.sync({'uid': 'nobody', 'token': 't'})
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'no_desktop_storage')


class ClientModelTest(unittest.TestCase):
    """客户端模型条目 → 目录形状（含「积分消耗速度」）。"""

    def test_consumption_rate_variants(self) -> None:
        def rate(node):  # noqa: ANN001, ANN202
            return desktop._consumption_rate({'features': {'consumption_rate': node}})

        self.assertEqual(rate({'enable': True, 'data': {'rate': 0.06}}), 0.06)
        self.assertEqual(rate({'data': {'rate': 2}}), 2.0)
        # 明确关闭 / 缺字段 / 类型不对一律「未知」，不能返回 0（0 = 免费）
        self.assertIsNone(rate({'enable': False, 'data': {'rate': 0.06}}))
        self.assertIsNone(rate({'enable': True}))
        self.assertIsNone(rate({'enable': True, 'data': {'rate': 'x'}}))
        self.assertIsNone(rate({'enable': True, 'data': {'rate': True}}))
        self.assertIsNone(desktop._consumption_rate({}))
        self.assertIsNone(desktop._consumption_rate({'features': None}))

    def test_client_model_keeps_ratio_and_channel(self) -> None:
        model = desktop._client_model({
            'name': 'deepseek-v4.1-flash', 'display_name': 'DeepSeek V4.1 Flash',
            'context_window_size': {'default': 128000}, 'max_tokens': 8192,
            'model_type': 'reasoning_model',
            'features': {'consumption_rate': {'enable': True,
                                              'data': {'rate': 0.06}}},
        }, 'cn')
        self.assertEqual(model['id'], 'deepseek-v4.1-flash')
        self.assertEqual(model['credit_ratio'], 0.06)
        self.assertTrue(model['thinking'])
        self.assertEqual(model['channel'], 'solo')

    def test_read_models_merges_ratio_from_other_groups(self) -> None:
        """倍率要跨分组汇总：只出现在 `solo_coder` 里的模型也得带上。"""
        db = Path(self._tmp()) / 'state.vscdb'
        con = sqlite3.connect(db)
        con.execute('create table ItemTable(key text, value text)')
        con.execute('insert into ItemTable values (?, ?)', (
            'u1_AI.agent.model.model_list_map',
            json.dumps({
                'chat_v3': [
                    {'name': 'a', 'display_name': 'A', 'status': True},
                    {'name': 'b', 'display_name': 'B', 'status': True},
                ],
                'solo_coder': [
                    {'name': 'a', 'display_name': 'A', 'status': True,
                     'features': {'consumption_rate': {'enable': True,
                                                       'data': {'rate': 0.5}}}},
                    {'name': 'c', 'display_name': 'C', 'status': True,
                     'features': {'consumption_rate': {'enable': True,
                                                       'data': {'rate': 0.2}}}},
                ],
            })))
        con.commit()
        con.close()
        with mock.patch.object(desktop, '_state_db', return_value=db):
            models = desktop.read_models('cn')
        self.assertEqual([m['id'] for m in models], ['a', 'b'])
        ratios = {m['id']: m['credit_ratio'] for m in models}
        self.assertEqual(ratios['a'], 0.5)
        self.assertIsNone(ratios['b'])

    def _tmp(self) -> str:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return tmp.name


class AdapterDesktopImportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.root = root / 'Trae CN'
        storage = self.root / 'User' / 'globalStorage' / 'storage.json'
        storage.parent.mkdir(parents=True)
        storage.write_text(json.dumps({
            'iCubeAuthInfo://icube.cloudide': icube.encode(_body())}),
            encoding='utf-8')
        for patch in (mock.patch.dict(os.environ,
                                      {'TRAE_DESKTOP_DIRS': str(self.root)}),
                      mock.patch.object(config, 'DATA_DIR', root / 'data')):
            patch.start()
            self.addCleanup(patch.stop)

    def test_import_from_desktop_then_list(self) -> None:
        result = trae_adapter.import_from_desktop()
        self.assertEqual(len(result['imported']), 1)
        self.assertEqual(result['failed'], [])
        self.assertEqual(result['imported'][0]['source'], 'desktop')
        listed = trae_adapter.list_accounts()
        self.assertEqual([a['uid'] for a in listed], ['1839902393428953'])
        self.assertEqual(listed[0]['realm_label'], '国内版')

    def test_import_sets_desktop_path_for_sync(self) -> None:
        trae_adapter.import_from_desktop()
        saved = store.read_account('1839902393428953')
        self.assertTrue(saved['desktopPath'].endswith('storage.json'))
        self.assertIn('desktopPath', saved)

    def test_import_filters_by_realm(self) -> None:
        result = trae_adapter.import_from_desktop(realm='intl')
        self.assertEqual(result['imported'], [])


if __name__ == '__main__':
    unittest.main()
