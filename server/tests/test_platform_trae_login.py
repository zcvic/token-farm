"""Trae 网页登录：本机回调 → 兑换 → 落盘。

这里不碰真实网络：
  · `client.exchange_token` / `client.fetch_user_info` 被替换成 AsyncMock；
  · `registry.get('trae')` 换成一个只记录 `import_account` 入参的假适配器。

钉住的契约（都是线上踩过的坑）：
  1. 回调里的 `refreshToken` 兑换后**必须**用返回的新 refreshToken 落盘
     （轮换式凭据，只存回调值等于下次刷新就失效）；
  2. `GetUserInfo` 返回的是 `Result.UserID` / `ScreenName`（不是嵌套的 UserInfo）；
  3. `loginTraceID` 不匹配时拒绝，且会话保持 pending；
  4. 取消后会话消失，轮询 404；
  5. 远程部署粘贴回调 URL 与浏览器直连回调走同一套校验 / 兑换逻辑；
  6. 国际版走 PKCE：授权页回调给 `authCodeInfo`，兑换必须带会话里的
     `code_verifier`，且不能再走 refreshToken 那条路。
"""
from __future__ import annotations

import base64
import hashlib
import json
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config  # noqa: E402
from server.platforms import registry  # noqa: E402
from server.platforms.base import PlatformError  # noqa: E402
from server.platforms.trae import deviceproof, login, store  # noqa: E402


class FakeAdapter:
    name = 'trae'
    label = 'Trae'

    def __init__(self) -> None:
        self.imported: list[dict] = []

    def import_account(self, payload: dict) -> dict:
        self.imported.append(payload)
        return {'uid': payload['uid'], 'nickname': payload['nickname'],
                'source': payload['source']}


def _callback_port_and_trace(url: str) -> tuple[int, str]:
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    callback = query['auth_callback_url'][0]
    port = int(urllib.parse.urlsplit(callback).port or 0)
    return port, query['login_trace_id'][0]


def _trace_of(url: str) -> str:
    return _callback_port_and_trace(url)[1]


def _callback_url(trace: str, **extra: str) -> str:
    """模拟浏览器地址栏里的回调链接（端口是用户电脑上的，后端并不监听）。"""
    params = {'refreshToken': 'refresh-from-callback', 'loginTraceID': trace, **extra}
    return 'http://127.0.0.1:43210/authorize?' + urllib.parse.urlencode(params)


def _call(url: str, timeout: float = 5.0) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - 本机回环
            return resp.status, resp.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode('utf-8', 'replace')


def _wait_settled(login_id: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        out = login.poll(login_id)
        if out['status'] not in ('pending', 'exchanging'):
            return out
        time.sleep(0.05)
    raise AssertionError('登录会话没有在超时前结束')


class WebLoginTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._data_dir = mock.patch.object(config, 'DATA_DIR',
                                           Path(self.tmp.name))
        self._data_dir.start()
        self.addCleanup(self._data_dir.stop)
        self.adapter = FakeAdapter()
        self.exchange = mock.AsyncMock(return_value={
            'token': 'access-new', 'refreshToken': 'refresh-rotated',
            'expiredAt': '2026-12-01T00:00:00.000Z',
            'refreshExpiredAt': '2027-01-01T00:00:00.000Z',
        })
        self.exchange_auth_code = mock.AsyncMock(return_value={
            'token': 'access-intl', 'refreshToken': 'refresh-intl',
            'expiredAt': '2026-12-01T00:00:00.000Z',
            'refreshExpiredAt': '2027-01-01T00:00:00.000Z',
        })
        self.userinfo = mock.AsyncMock(return_value={'Result': {
            'UserID': '42', 'ScreenName': '阿柒', 'NonPlainTextEmail': '',
        }})
        self._patches = [
            mock.patch.object(login.client, 'exchange_token', self.exchange),
            mock.patch.object(login.client, 'exchange_token_by_auth_code',
                              self.exchange_auth_code),
            mock.patch.object(login.client, 'fetch_user_info', self.userinfo),
            mock.patch.object(registry, 'get', return_value=self.adapter),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def test_callback_exchanges_and_imports(self) -> None:
        start = login.start('cn')
        self.addCleanup(login.cancel, start['login_id'])
        port, trace = _callback_port_and_trace(start['url'])
        # 国内版仍走 refreshToken 分支，没有 PKCE。
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(start['url']).query)
        self.assertNotIn('code_challenge', query)

        url = (f'http://127.0.0.1:{port}/authorize?'
               + urllib.parse.urlencode({'refreshToken': 'refresh-from-callback',
                                         'loginTraceID': trace,
                                         'refreshExpireAt': '1800000000000'}))
        status, _ = _call(url)
        self.assertEqual(status, 200)
        out = _wait_settled(start['login_id'])
        self.assertEqual(out['status'], 'success', out)

        self.exchange.assert_awaited_once()
        account = self.exchange.await_args.args[0]
        self.assertEqual(account['refreshToken'], 'refresh-from-callback')
        self.userinfo.assert_awaited_once()

        self.assertEqual(len(self.adapter.imported), 1)
        payload = self.adapter.imported[0]
        self.assertEqual(payload['uid'], '42')
        self.assertEqual(payload['nickname'], '阿柒')
        self.assertEqual(payload['source'], 'web')
        # 轮换后的 refreshToken 才是落盘值，不能只存回调里的那个。
        self.assertEqual(payload['refreshToken'], 'refresh-rotated')
        self.assertEqual(out['account']['uid'], '42')

    def test_trace_mismatch_is_rejected_and_session_stays_pending(self) -> None:
        start = login.start('cn')
        self.addCleanup(login.cancel, start['login_id'])
        port, _ = _callback_port_and_trace(start['url'])
        status, _ = _call(f'http://127.0.0.1:{port}/authorize?'
                          'refreshToken=x&loginTraceID=not-the-trace')
        self.assertEqual(status, 400)
        self.assertEqual(login.poll(start['login_id'])['status'], 'pending')
        self.assertEqual(self.adapter.imported, [])

    def test_pasted_callback_url_completes_login(self) -> None:
        """远程部署：浏览器打不开 127.0.0.1，但粘贴地址栏链接仍能完成登录。"""
        start = login.start('cn')
        self.addCleanup(login.cancel, start['login_id'])
        url = _callback_url(_trace_of(start['url']))

        out = login.submit_callback(start['login_id'], url)
        self.assertIn(out['status'], ('exchanging', 'success'))

        settled = _wait_settled(start['login_id'])
        self.assertEqual(settled['status'], 'success', settled)
        self.exchange.assert_awaited_once()
        self.assertEqual(
            self.exchange.await_args.args[0]['refreshToken'],
            'refresh-from-callback')
        self.assertEqual(self.adapter.imported[0]['refreshToken'], 'refresh-rotated')

    def test_pasted_bare_query_is_accepted(self) -> None:
        """允许只粘贴 `refreshToken=…&loginTraceID=…`（有些浏览器会截掉前缀）。"""
        start = login.start('cn')
        self.addCleanup(login.cancel, start['login_id'])
        query = urllib.parse.urlencode({
            'refreshToken': 'refresh-from-query',
            'loginTraceID': _trace_of(start['url']),
        })
        out = login.submit_callback(start['login_id'], f'?{query}')
        self.assertIn(out['status'], ('exchanging', 'success'))
        self.assertEqual(_wait_settled(start['login_id'])['status'], 'success')

    def test_pasted_callback_without_refresh_token_is_rejected(self) -> None:
        start = login.start('cn')
        self.addCleanup(login.cancel, start['login_id'])
        url = ('http://127.0.0.1:43210/authorize?'
               + urllib.parse.urlencode({'loginTraceID': _trace_of(start['url'])}))
        with self.assertRaises(PlatformError) as ctx:
            login.submit_callback(start['login_id'], url)
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(ctx.exception.code, 'login_no_credential')
        self.assertEqual(login.poll(start['login_id'])['status'], 'error')
        self.exchange.assert_not_awaited()
        self.assertEqual(self.adapter.imported, [])

    def test_pasted_callback_trace_mismatch_keeps_session_pending(self) -> None:
        start = login.start('cn')
        self.addCleanup(login.cancel, start['login_id'])
        with self.assertRaises(PlatformError) as ctx:
            login.submit_callback(start['login_id'],
                                  _callback_url('not-the-trace'))
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(ctx.exception.code, 'login_trace_mismatch')
        self.assertEqual(login.poll(start['login_id'])['status'], 'pending')
        self.exchange.assert_not_awaited()

    def test_pasted_callback_unknown_session_is_404(self) -> None:
        with self.assertRaises(PlatformError) as ctx:
            login.submit_callback('missing', _callback_url('whatever'))
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(ctx.exception.code, 'login_not_found')

    def test_pasted_callback_after_success_is_idempotent(self) -> None:
        start = login.start('cn')
        self.addCleanup(login.cancel, start['login_id'])
        url = _callback_url(_trace_of(start['url']))
        login.submit_callback(start['login_id'], url)
        self.assertEqual(_wait_settled(start['login_id'])['status'], 'success')

        again = login.submit_callback(start['login_id'], url)
        self.assertEqual(again['status'], 'success')
        self.exchange.assert_awaited_once()

    def test_non_loopback_callback_host_is_rejected(self) -> None:
        with self.assertRaises(PlatformError) as ctx:
            login.parse_callback_url(
                'http://evil.example.com/authorize?refreshToken=x')
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(ctx.exception.code, 'callback_host_invalid')

    def test_cancel_removes_session(self) -> None:
        start = login.start('cn')
        self.assertTrue(login.cancel(start['login_id']))
        with self.assertRaises(PlatformError) as ctx:
            login.poll(start['login_id'])
        self.assertEqual(ctx.exception.status, 404)

    def test_intl_without_client_generates_stable_panel_identity(self) -> None:
        """没有本机客户端也能发起国际版登录：面板自生成并复用设备身份。"""
        with mock.patch.object(login.desktop, 'read_devices', return_value=[]):
            first = login.start('intl')
            self.addCleanup(login.cancel, first['login_id'])
            second = login.start('intl')
            self.addCleanup(login.cancel, second['login_id'])

        query_first = urllib.parse.parse_qs(urllib.parse.urlsplit(first['url']).query)
        query_second = urllib.parse.parse_qs(urllib.parse.urlsplit(second['url']).query)
        self.assertRegex(query_first['device_id'][0], r'^\d{19}$')
        self.assertRegex(query_first['x_machine_id'][0], r'^[0-9a-f]{64}$')
        # 第二次登录必须复用同一套身份，不能每次换设备。
        self.assertEqual(query_second['device_id'][0], query_first['device_id'][0])
        self.assertEqual(query_second['x_machine_id'][0],
                         query_first['x_machine_id'][0])

        saved = store.read_device_identity()
        self.assertEqual(saved['deviceId'], query_first['device_id'][0])
        self.assertEqual(saved['devicePublicKeyPEM'].splitlines()[0],
                         '-----BEGIN PUBLIC KEY-----')
        self.assertIn('BEGIN PRIVATE KEY', saved['deviceKeyPEM'])

    def test_intl_generated_identity_is_used_for_exchange(self) -> None:
        """自生成的设备身份要同时用于兑换和落盘，刷新才能自己签名。"""
        self.exchange_auth_code.return_value['realm'] = 'intl'
        with mock.patch.object(login.desktop, 'read_devices', return_value=[]):
            start = login.start('intl')
        self.addCleanup(login.cancel, start['login_id'])
        url = ('http://127.0.0.1:43210/authorize?'
               + urllib.parse.urlencode({
                   'authCodeInfo': json.dumps({'AuthCode': 'auth-code-1'}),
                   'loginTraceID': _trace_of(start['url']),
               }))
        login.submit_callback(start['login_id'], url)
        self.assertEqual(_wait_settled(start['login_id'])['status'], 'success')

        exchange_account = self.exchange_auth_code.await_args.args[0]
        saved = store.read_device_identity()
        self.assertEqual(exchange_account['deviceId'], saved['deviceId'])
        self.assertEqual(exchange_account['deviceKeyPEM'], saved['deviceKeyPEM'])
        payload = self.adapter.imported[0]
        self.assertEqual(payload['deviceId'], saved['deviceId'])
        self.assertEqual(payload['machineId'], saved['machineId'])
        self.assertEqual(payload['deviceKeyPEM'], saved['deviceKeyPEM'])
        self.assertEqual(payload['devicePublicKeyPEM'],
                         saved['devicePublicKeyPEM'])

    def test_intl_uses_trae_ai_and_client_identity(self) -> None:
        key = deviceproof.generate_keypair()
        with mock.patch.object(login.desktop, 'read_devices', return_value=[{
                'realm': 'intl', 'holderId': '7703084028531181063',
                'machineId': 'm' * 64, 'privateKeyPEM': key['privateKeyPEM'],
                'publicKeyPEM': key['publicKeyPEM']}]):
            start = login.start('intl')
        self.addCleanup(login.cancel, start['login_id'])
        parts = urllib.parse.urlsplit(start['url'])
        query = urllib.parse.parse_qs(parts.query)
        self.assertEqual(parts.netloc, 'www.trae.ai')
        self.assertEqual(query['device_id'][0], '7703084028531181063')
        self.assertEqual(query['x_machine_id'][0], 'm' * 64)
        self.assertEqual(query['x_app_version'][0], '3.6.4')
        self.assertEqual(query['plugin_version'][0], '2.3.93439')
        # 国际版客户端始终走 PKCE，回调给的是 AuthCode 而不是 refreshToken。
        self.assertEqual(query['code_challenge_method'][0], 'S256')
        challenge = query['code_challenge'][0]
        self.assertEqual(len(challenge), 43)
        verifier = login._sessions[start['login_id']]['codeVerifier']
        self.assertEqual(len(verifier), 64)
        self.assertEqual(
            base64.urlsafe_b64encode(
                hashlib.sha256(verifier.encode('ascii')).digest()
            ).decode('ascii').rstrip('='),
            challenge)

    def test_intl_callback_exchanges_with_auth_code(self) -> None:
        self.exchange_auth_code.return_value['realm'] = 'intl'
        key = deviceproof.generate_keypair()
        with mock.patch.object(login.desktop, 'read_devices', return_value=[{
                'realm': 'intl', 'holderId': '7703084028531181063',
                'machineId': 'm' * 64, 'privateKeyPEM': key['privateKeyPEM'],
                'publicKeyPEM': key['publicKeyPEM']}]):
            start = login.start('intl')
        self.addCleanup(login.cancel, start['login_id'])
        verifier = login._sessions[start['login_id']]['codeVerifier']
        auth_code_info = json.dumps({'AuthCode': 'auth-code-from-callback'})
        url = ('http://127.0.0.1:43210/authorize?'
               + urllib.parse.urlencode({
                   'authCodeInfo': auth_code_info,
                   'loginTraceID': _trace_of(start['url']),
               }))
        out = login.submit_callback(start['login_id'], url)
        self.assertIn(out['status'], ('exchanging', 'success'))
        settled = _wait_settled(start['login_id'])
        self.assertEqual(settled['status'], 'success', settled)

        # 走的是 AuthCode + 会话里的 PKCE verifier，不是 refreshToken 那条路。
        self.exchange.assert_not_awaited()
        self.exchange_auth_code.assert_awaited_once()
        args = self.exchange_auth_code.await_args.args
        self.assertEqual(args[0]['realm'], 'intl')
        self.assertEqual(args[0]['deviceId'], '7703084028531181063')
        self.assertEqual(args[0]['machineId'], 'm' * 64)
        self.assertEqual(args[0]['deviceKeyPEM'], key['privateKeyPEM'])
        self.assertEqual(args[1], 'auth-code-from-callback')
        self.assertEqual(args[2], verifier)
        self.assertEqual(self.userinfo.await_args.args[0]['realm'], 'intl')
        self.assertEqual(self.adapter.imported[0]['realm'], 'intl')
        self.assertEqual(self.adapter.imported[0]['refreshToken'], 'refresh-intl')
        # 设备私钥随账号落盘，后续刷新不依赖本机客户端。
        self.assertEqual(self.adapter.imported[0]['deviceId'],
                         '7703084028531181063')
        self.assertEqual(self.adapter.imported[0]['deviceKeyPEM'],
                         key['privateKeyPEM'])

    def test_intl_auth_code_info_is_percent_decoded(self) -> None:
        """粘贴的地址栏链接里 `authCodeInfo` 是 URL 编码过的 JSON。"""
        params = login.parse_callback_url(
            'http://127.0.0.1:43210/authorize?'
            + urllib.parse.urlencode({
                'authCodeInfo': json.dumps({'AuthCode': 'abc123'}),
                'loginTraceID': 't',
            }))
        self.assertEqual(json.loads(params['authCodeInfo'])['AuthCode'], 'abc123')


if __name__ == '__main__':
    unittest.main()
