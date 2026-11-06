"""国际版 DeviceProof：P-256 自检、PEM 解析、签名接线。

全部离线：私钥用测试里合成的 PEM（与客户端 `icube-dc` 同结构），
不读本机 `storage.json`，也不打上游。
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server.platforms.trae import client, desktop, deviceproof
from server.platforms.trae import icube


def _run(awaitable):  # noqa: ANN001
    return asyncio.run(awaitable)


# ── 合成 PEM（PKCS#8 / SEC1）─────────────────────────────────
def _tlv(tag: int, body: bytes) -> bytes:
    length = len(body)
    if length < 0x80:
        header = bytes([tag, length])
    else:
        raw = length.to_bytes((length.bit_length() + 7) // 8, 'big')
        header = bytes([tag, 0x80 | len(raw)]) + raw
    return header + body


def _int(value: int) -> bytes:
    raw = value.to_bytes((value.bit_length() + 7) // 8 or 1, 'big')
    if raw[0] & 0x80:
        raw = b'\x00' + raw
    return _tlv(0x02, raw)


_OID_EC_PUBLIC = _tlv(0x06, bytes.fromhex('2A8648CE3D0201'))     # 1.2.840.10045.2.1
_OID_P256 = _tlv(0x06, bytes.fromhex('2A8648CE3D030107'))        # 1.2.840.10045.3.1.7


def _pem(label: str, der: bytes) -> str:
    body = base64.b64encode(der).decode('ascii')
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    return '\n'.join([f'-----BEGIN {label}-----', *lines,
                      f'-----END {label}-----'])


def pkcs8_pem(scalar: int) -> str:
    ec_private = _tlv(0x30, _int(1) + _tlv(0x04, scalar.to_bytes(32, 'big'))
                      + _tlv(0xA0, _OID_P256))
    algorithm = _tlv(0x30, _OID_EC_PUBLIC + _OID_P256)
    der = _tlv(0x30, _int(0) + algorithm + _tlv(0x04, ec_private))
    return _pem('PRIVATE KEY', der)


def sec1_pem(scalar: int) -> str:
    ec_private = _tlv(0x30, _int(1) + _tlv(0x04, scalar.to_bytes(32, 'big'))
                      + _tlv(0xA0, _OID_P256))
    return _pem('EC PRIVATE KEY', ec_private)


TEST_SCALAR = 0x519B423D715F8B581F4FA8EE59F4771A5B44C8130B4E3EACCA54A56DDA72B464


class PemTest(unittest.TestCase):
    def test_pkcs8(self) -> None:
        self.assertEqual(deviceproof.ec_private_key_from_pem(pkcs8_pem(TEST_SCALAR)),
                         TEST_SCALAR)

    def test_sec1(self) -> None:
        self.assertEqual(deviceproof.ec_private_key_from_pem(sec1_pem(TEST_SCALAR)),
                         TEST_SCALAR)

    def test_rejects_garbage(self) -> None:
        for bad in ('', 'not a pem', _pem('PRIVATE KEY', b'\x02\x01\x01')):
            with self.assertRaises(ValueError, msg=bad):
                deviceproof.ec_private_key_from_pem(bad)


class GenerateKeypairTest(unittest.TestCase):
    """面板自己生成的设备密钥：PEM 形状与签名能力都要能回到客户端同款。"""

    def test_pem_roundtrip_matches_public_key(self) -> None:
        keys = deviceproof.generate_keypair()
        self.assertTrue(keys['privateKeyPEM'].startswith(
            '-----BEGIN PRIVATE KEY-----'))
        self.assertTrue(keys['publicKeyPEM'].startswith(
            '-----BEGIN PUBLIC KEY-----'))
        scalar = deviceproof.ec_private_key_from_pem(keys['privateKeyPEM'])
        self.assertTrue(1 <= scalar < deviceproof.P256_N)
        self.assertEqual(
            deviceproof.ec_public_key_from_pem(keys['publicKeyPEM']),
            deviceproof.p256_public_point(scalar))

    def test_generated_key_signs_device_proof(self) -> None:
        keys = deviceproof.generate_keypair()
        scalar = deviceproof.ec_private_key_from_pem(keys['privateKeyPEM'])
        proof = deviceproof.build_device_proof(
            {'uid': '42'}, path=client.PATH_INTL_EXCHANGE_TOKEN,
            client_id=client.CLIENT_ID, refresh_token='r',
            private_key_pem=keys['privateKeyPEM'],
            public_key_pem=keys['publicKeyPEM'],
            device_id='7703084028531181063', machine_id='m' * 64,
            timestamp=1793500000, nonce='n')
        self.assertEqual(proof['DeviceInfo']['DevicePublicKey'],
                         keys['publicKeyPEM'])
        message = deviceproof.proof_message(
            'POST', client.PATH_INTL_EXCHANGE_TOKEN, client.CLIENT_ID,
            'r', 1793500000, 'n')
        der = base64.b64decode(proof['DeviceProof']['Signature'])
        _, body, _ = deviceproof._der_tlv(der, 0)
        r, s = (int.from_bytes(chunk, 'big')
                for _, chunk in deviceproof._der_children(body))
        self.assertTrue(deviceproof.p256_verify(
            deviceproof.p256_public_point(scalar), message, r, s))

    def test_each_call_generates_new_key(self) -> None:
        first = deviceproof.generate_keypair()
        second = deviceproof.generate_keypair()
        self.assertNotEqual(first['privateKeyPEM'], second['privateKeyPEM'])
        self.assertNotEqual(first['publicKeyPEM'], second['publicKeyPEM'])


class SelfCheckTest(unittest.TestCase):
    def test_rfc6979_vector(self) -> None:
        """RFC 6979 A.2.5：签名/验签/低 s 归一化三条同时成立。"""
        self.assertTrue(deviceproof.self_check())


class ProofTest(unittest.TestCase):
    def test_fields_and_signature(self) -> None:
        scalar = TEST_SCALAR
        proof = deviceproof.build_device_proof(
            {'uid': '1234567890123456789'}, path=client.PATH_INTL_EXCHANGE_TOKEN,
            client_id=client.CLIENT_ID, refresh_token='refresh-1',
            private_key_pem=pkcs8_pem(scalar),
            timestamp=1793500000, nonce='nonce-1')
        info = proof['DeviceInfo']
        self.assertEqual(info['DeviceID'], '1234567890123456789')
        self.assertEqual(len(info['MachineID']), 64)
        self.assertEqual(info['DeviceType'], 'PC')
        self.assertEqual(info['PlatformCode'], 'IDE_PC')
        dp = proof['DeviceProof']
        self.assertEqual(dp['Timestamp'], 1793500000)
        self.assertIsInstance(dp['Timestamp'], int, 'Timestamp 必须是 int 秒')
        self.assertEqual(dp['Nonce'], 'nonce-1')
        message = deviceproof.proof_message(
            'POST', client.PATH_INTL_EXCHANGE_TOKEN, client.CLIENT_ID,
            'refresh-1', 1793500000, 'nonce-1')
        self.assertEqual(message.count(b'\n'), 5)
        der = base64.b64decode(dp['Signature'])
        tag, body, _ = deviceproof._der_tlv(der, 0)
        self.assertEqual(tag, 0x30)
        r, s = (int.from_bytes(chunk, 'big')
                for _, chunk in deviceproof._der_children(body))
        self.assertTrue(deviceproof.p256_verify(
            deviceproof.p256_public_point(scalar), message, r, s))

    def test_device_identity_overrides(self) -> None:
        proof = deviceproof.build_device_proof(
            {'uid': '42'}, path=client.PATH_INTL_EXCHANGE_TOKEN,
            client_id=client.CLIENT_ID, refresh_token='r',
            private_key_pem=pkcs8_pem(TEST_SCALAR), timestamp=1793500000,
            nonce='n', device_id='7703084028531181063',
            machine_id='m' * 64, public_key_pem='PUB', ide_version='3.6.4')
        info = proof['DeviceInfo']
        self.assertEqual(info['DeviceID'], '7703084028531181063')
        self.assertEqual(info['MachineID'], 'm' * 64)
        self.assertEqual(info['DevicePublicKey'], 'PUB')
        self.assertEqual(info['ClientVersion'], '3.6.4')

    def test_missing_key_raises(self) -> None:
        with self.assertRaises(ValueError):
            deviceproof.build_device_proof(
                {'uid': 'u'}, path=client.PATH_INTL_EXCHANGE_TOKEN,
                client_id=client.CLIENT_ID, refresh_token='r')


class DesktopDeviceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        storage = self.root / 'User' / 'globalStorage' / 'storage.json'
        storage.parent.mkdir(parents=True)
        blob = icube.encode({'privateKeyPEM': pkcs8_pem(TEST_SCALAR),
                             'publicKeyPEM': ''}, os.urandom(32))
        storage.write_text(json.dumps({
            'iCubeAuthInfo://icube-dc:987654321': blob,
            'iCubeAuthInfo://usertag': 'not-a-device',
        }), encoding='utf-8')

    def test_read_devices(self) -> None:
        with mock.patch.dict(os.environ, {'TRAE_DESKTOP_DIRS': str(self.root)}):
            found = desktop.read_devices()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['realm'], 'intl')
        self.assertEqual(found[0]['holderId'], '987654321')
        self.assertIn('BEGIN PRIVATE KEY', found[0]['privateKeyPEM'])

    def test_read_devices_skips_broken_blob(self) -> None:
        storage = self.root / 'User' / 'globalStorage' / 'storage.json'
        data = json.loads(storage.read_text(encoding='utf-8'))
        data['iCubeAuthInfo://icube-dc:111'] = 'garbage'
        storage.write_text(json.dumps(data), encoding='utf-8')
        with mock.patch.dict(os.environ, {'TRAE_DESKTOP_DIRS': str(self.root)}):
            found = desktop.read_devices()
        self.assertEqual([d['holderId'] for d in found], ['987654321'])


class ExchangeTokenProofTest(unittest.TestCase):
    """刷新请求体的接线：国际版走新路径 + 设备证明，国内版保持旧形状。"""

    def _exchange(self, account: dict, devices: list[dict]) -> dict:
        seen: dict = {}

        async def fake(account_arg, realm, hosts, path, body, timeout, headers,
                       *, method='POST', auth=True):  # noqa: ANN001, ANN202
            seen['realm'] = realm
            seen['path'] = path
            seen['body'] = body
            seen['headers'] = headers
            return {'Result': {'Token': 'new-token', 'RefreshToken': 'new-refresh',
                               'TokenExpireAt': 1893456000000,
                               'RefreshExpireAt': 1893456000000}}

        with mock.patch.object(client, 'desktop') as fake_desktop:
            fake_desktop.read_devices.return_value = devices
            with mock.patch.object(client, '_request_hosts', new=fake):
                fresh = _run(client.exchange_token(account))
        seen['fresh'] = fresh
        return seen

    def test_intl_attaches_proof(self) -> None:
        seen = self._exchange(
            {'uid': 'u1', 'realm': 'intl', 'refreshToken': 'r1'},
            [{'realm': 'intl', 'holderId': '7703084028531181063',
              'machineId': 'm' * 64, 'publicKeyPEM': 'PUB',
              'privateKeyPEM': pkcs8_pem(TEST_SCALAR)}])
        body = seen['body']
        self.assertEqual(seen['path'], client.PATH_INTL_EXCHANGE_TOKEN)
        self.assertEqual(body['RefreshToken'], 'r1')
        self.assertEqual(body['ClientSecret'], '')
        self.assertEqual(body['IDEVersion'], client.INTL_IDE_VERSION)
        self.assertIn('DeviceInfo', body)
        self.assertIn('DeviceProof', body)
        self.assertEqual(body['DeviceInfo']['DeviceID'], '7703084028531181063')
        self.assertEqual(body['DeviceInfo']['MachineID'], 'm' * 64)
        self.assertEqual(body['DeviceInfo']['DevicePublicKey'], 'PUB')
        self.assertIsInstance(body['DeviceProof']['Timestamp'], int)
        self.assertTrue(seen['fresh']['deviceProofUsed'])

    def test_intl_without_key_sends_basics(self) -> None:
        seen = self._exchange(
            {'uid': 'u1', 'realm': 'intl', 'refreshToken': 'r1'}, [])
        self.assertEqual(seen['path'], client.PATH_INTL_EXCHANGE_TOKEN)
        self.assertNotIn('DeviceProof', seen['body'])
        self.assertFalse(seen['fresh']['deviceProofUsed'])

    def test_cn_keeps_legacy_shape(self) -> None:
        seen = self._exchange(
            {'uid': 'u1', 'realm': 'cn', 'refreshToken': 'r1'},
            [{'realm': 'intl', 'holderId': 'u1', 'machineId': 'm',
              'privateKeyPEM': pkcs8_pem(TEST_SCALAR)}])
        self.assertEqual(seen['path'], client.PATH_EXCHANGE_TOKEN)
        self.assertNotIn('DeviceProof', seen['body'])
        self.assertEqual(seen['body']['ClientSecret'], client.CLIENT_SECRET)
        self.assertFalse(seen['fresh']['deviceProofUsed'])

    def test_account_key_wins_over_desktop(self) -> None:
        seen = self._exchange(
            {'uid': 'u1', 'realm': 'intl', 'refreshToken': 'r1',
             'deviceKeyPEM': pkcs8_pem(TEST_SCALAR)}, [])
        self.assertIn('DeviceProof', seen['body'])

    def test_broken_key_does_not_block_refresh(self) -> None:
        seen = self._exchange(
            {'uid': 'u1', 'realm': 'intl', 'refreshToken': 'r1',
             'deviceKeyPEM': 'not-a-key'}, [])
        self.assertNotIn('DeviceProof', seen['body'])
        self.assertEqual(seen['fresh']['token'], 'new-token')


class AuthCodeExchangeTest(unittest.TestCase):
    """国际版授权码兑换：带 `AuthCode` + `CodeVerifier` + `DeviceInfo`。

    与真实客户端（`exchangeTokenByAuthCode`）对齐：不带 `DeviceProof`，
    但 `DeviceInfo` 必须带设备公钥 / 设备号 / 机器号。
    """

    def _exchange(self, account: dict, devices: list[dict], code: str = 'code-1',
                  verifier: str = 'verifier-1') -> dict:
        seen: dict = {}

        async def fake(account_arg, realm, hosts, path, body, timeout, headers,
                       *, method='POST', auth=True):  # noqa: ANN001, ANN202
            seen['realm'] = realm
            seen['path'] = path
            seen['body'] = body
            seen['headers'] = headers
            return {'Result': {'Token': 'new-token', 'RefreshToken': 'new-refresh',
                               'TokenExpireAt': 1893456000000,
                               'RefreshExpireAt': 1893456000000}}

        with mock.patch.object(client, 'desktop') as fake_desktop:
            fake_desktop.read_devices.return_value = devices
            with mock.patch.object(client, '_request_hosts', new=fake):
                fresh = _run(client.exchange_token_by_auth_code(
                    account, code, verifier))
        seen['fresh'] = fresh
        return seen

    def test_intl_exchanges_auth_code(self) -> None:
        seen = self._exchange(
            {'uid': 'u1', 'realm': 'intl'},
            [{'realm': 'intl', 'holderId': '7703084028531181063',
              'machineId': 'm' * 64, 'publicKeyPEM': 'PUB',
              'privateKeyPEM': pkcs8_pem(TEST_SCALAR)}])
        body = seen['body']
        self.assertEqual(seen['path'], client.PATH_INTL_EXCHANGE_TOKEN)
        self.assertEqual(body['ClientID'], client.CLIENT_ID)
        self.assertEqual(body['AuthCode'], 'code-1')
        self.assertEqual(body['CodeVerifier'], 'verifier-1')
        self.assertEqual(body['IDEVersion'], client.INTL_IDE_VERSION)
        self.assertIn('DeviceInfo', body)
        self.assertNotIn('DeviceProof', body)
        self.assertEqual(body['DeviceInfo']['DeviceID'], '7703084028531181063')
        self.assertEqual(body['DeviceInfo']['MachineID'], 'm' * 64)
        self.assertEqual(body['DeviceInfo']['DevicePublicKey'], 'PUB')
        self.assertEqual(seen['headers'], {'x-cloudide-token': ''})
        self.assertEqual(seen['fresh']['token'], 'new-token')
        self.assertEqual(seen['fresh']['refreshToken'], 'new-refresh')
        self.assertTrue(seen['fresh']['authCodeUsed'])
        self.assertFalse(seen['fresh']['deviceProofUsed'])

    def test_intl_without_device_key_raises(self) -> None:
        with mock.patch.object(client, 'desktop') as fake_desktop:
            fake_desktop.read_devices.return_value = []
            with self.assertRaises(Exception) as ctx:
                _run(client.exchange_token_by_auth_code(
                    {'uid': 'u1', 'realm': 'intl'}, 'code-1', 'verifier-1'))
        self.assertEqual(getattr(ctx.exception, 'code', ''),
                         'login_device_key_missing')

    def test_cn_is_rejected(self) -> None:
        with self.assertRaises(Exception) as ctx:
            _run(client.exchange_token_by_auth_code(
                {'uid': 'u1', 'realm': 'cn'}, 'code-1', 'verifier-1'))
        self.assertEqual(getattr(ctx.exception, 'code', ''),
                         'auth_code_unsupported')


if __name__ == '__main__':
    unittest.main()
