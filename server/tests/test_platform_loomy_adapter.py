"""Loomy 平台：协议原语、账号文件、适配器策略。

全部离线：登录 / 对话 / 签到 / 积分都替换成假客户端，测的是
「上游字段 → token-farm 形状」的翻译与选号 / 续期策略。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import tempfile
import threading
import time
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest import mock

import httpx

from server import config, db
from server.platforms.base import PlatformError
from server.platforms.loomy import (client, constants as C, crypto, desktop, login,
                                    signer, store, wechat)
from server.platforms.loomy.adapter import (LoomyAdapter, _multiplier_of,
                                            _no_think_messages, _points_of,
                                            _scan_sse)


# ── 测试用 DER 构造 ────────────────────────────────────────
def _der_len(length: int) -> bytes:
    if length < 0x80:
        return bytes([length])
    raw = length.to_bytes((length.bit_length() + 7) // 8, 'big')
    return bytes([0x80 | len(raw)]) + raw


def _der_int(value: int) -> bytes:
    raw = value.to_bytes(max(1, (value.bit_length() + 7) // 8), 'big')
    if raw[0] & 0x80:
        raw = b'\x00' + raw
    return b'\x02' + _der_len(len(raw)) + raw


def _spki_b64(modulus: int, exponent: int) -> str:
    rsa_public = _der_int(modulus) + _der_int(exponent)
    rsa_public = b'\x30' + _der_len(len(rsa_public)) + rsa_public
    algorithm = bytes.fromhex('300d06092a864886f70d0101010500')
    bit_string = b'\x03' + _der_len(len(rsa_public) + 1) + b'\x00' + rsa_public
    body = algorithm + bit_string
    der = b'\x30' + _der_len(len(body)) + body
    return base64.b64encode(der).decode()


class LoomyCryptoTest(unittest.TestCase):
    def test_der_numbers_roundtrip(self) -> None:
        modulus = int.from_bytes(b'\x7f' + b'\x11' * 63, 'big')
        payload = _spki_b64(modulus, 65537)
        der = base64.b64decode(payload)
        n, e = crypto.der_to_numbers(der)
        self.assertEqual(n, modulus)
        self.assertEqual(e, 65537)

    def test_rsa_encrypt_matches_manual_pkcs1(self) -> None:
        modulus = int.from_bytes(b'\x00\xff' + b'\x22' * 62, 'big')
        size = (modulus.bit_length() + 7) // 8
        message = b'secret-password'
        padding = b'\x01' * (size - len(message) - 3)
        block = b'\x00\x02' + padding + b'\x00' + message
        expected = pow(int.from_bytes(block, 'big'), 65537, modulus)
        out = crypto.rsa_encrypt(_spki_b64(modulus, 65537),
                                 message.decode(), rng=padding)
        self.assertEqual(base64.b64decode(out),
                         expected.to_bytes(size, 'big'))

    def test_rsa_rejects_overlong_message(self) -> None:
        modulus = int.from_bytes(b'\xff' * 16, 'big')  # 128-bit → 16 bytes
        with self.assertRaises(ValueError):
            crypto.rsa_encrypt(_spki_b64(modulus, 65537), 'x' * 20)


class LoomySignerTest(unittest.TestCase):
    def test_string_to_sign_has_nine_lines(self) -> None:
        text = signer.string_to_sign(
            method='post', path='/login/account/getPuKey', body='',
            date='Tue, 18 Nov 2026 00:00:00 GMT', nonce='n-1')
        lines = text.split('\n')
        self.assertEqual(len(lines), 9)
        self.assertEqual(lines[0], 'POST')
        self.assertEqual(lines[1], '/login/account/getPuKey')
        self.assertEqual(lines[2], '')
        self.assertEqual(lines[3], '')          # 空 body → 无 Content-MD5
        self.assertEqual(lines[4], 'application/json')
        self.assertEqual(lines[7], '')
        self.assertEqual(lines[8], '')

    def test_sign_is_hmac_sha1_of_string_to_sign(self) -> None:
        kwargs = dict(method='POST', path='/login/account/byPwd',
                      body='{"a":1}', date='d', nonce='n')
        expected = base64.b64encode(hmac.new(
            b'secret', signer.string_to_sign(**kwargs).encode(), hashlib.sha1
        ).digest()).decode()
        self.assertEqual(signer.sign(secret='secret', **kwargs), expected)

    def test_build_headers_shape(self) -> None:
        headers = signer.build_headers(
            'ak-id', 'ak-secret', method='POST', path='/x', body='{}',
            clock=1700000000)
        self.assertTrue(headers['Authorization'].startswith('account ak-id:'))
        self.assertEqual(headers['Content-Type'], 'application/json')
        self.assertEqual(headers['Content-MD5'],
                         base64.b64encode(hashlib.md5(b'{}').digest()).decode())
        self.assertTrue(headers['Date'].endswith('GMT'))
        self.assertTrue(headers['Nonce'])


class LoomyStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(config, 'DATA_DIR', Path(self.tmp.name))
        patch.start()
        self.addCleanup(patch.stop)

    def test_derive_uid_prefers_userid_then_phone(self) -> None:
        self.assertEqual(store.derive_uid('123', '13800000000', 's'), '123')
        self.assertEqual(store.derive_uid('', '13800000000', 's'), '13800000000')
        hashed = store.derive_uid('', '', 'session-value')
        self.assertTrue(hashed.startswith('s_'))
        self.assertEqual(hashed, store.derive_uid('', '', 'session-value'))
        with self.assertRaises(ValueError):
            store.derive_uid('', '', '')

    def test_public_account_hides_session_and_password(self) -> None:
        saved = store.save_account({
            'uid': 'u-1', 'session': 'very-secret-session-token',
            'password': 'very-secret-password', 'phone': '13800000000',
            'nickname': '小明',
        })
        public = store.public_account(saved)
        dumped = json.dumps(public, ensure_ascii=False)
        self.assertNotIn('very-secret-session-token', dumped)
        self.assertNotIn('very-secret-password', dumped)
        self.assertTrue(public['has_token'])
        self.assertTrue(public['has_password'])
        self.assertFalse(public['has_refresh_token'])
        self.assertEqual(public['realm'], 'cn')
        self.assertEqual(public['phone'], '13800000000')

    def test_safe_id_rejects_traversal(self) -> None:
        for bad in ('../x', 'a/b', 'a\\b', '', 'x' * 200):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    store.safe_id(bad)


class LoomyDesktopTest(unittest.TestCase):
    """本机客户端登录态的读取（只读、按 `<用户目录>/userData` 扫）。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'Loomy'
        patch = mock.patch.dict(os.environ,
                                {'LOOMY_CLIENT_ROOT': str(self.root)})
        patch.start()
        self.addCleanup(patch.stop)

    def _write(self, user: str, payload: dict) -> Path:
        path = self.root / user / 'userData' / desktop.SESSION_FILE
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(payload), encoding='utf-8')
        return path

    def test_reads_every_client_install(self) -> None:
        first = self._write('aaaa', {'session': 's1', 'userid': '11',
                                     'phone': '13800000000',
                                     'updatedAt': 1763000000000})
        self._write('bbbb', {'session': 's2', 'userid': '22'})
        paths = [item['path'] for item in desktop.storage_paths()]
        self.assertEqual(paths, [first, self.root / 'bbbb' / 'userData'
                                 / desktop.SESSION_FILE])
        accounts = desktop.read_accounts()
        self.assertEqual([a['session'] for a in accounts], ['s1', 's2'])
        self.assertEqual(accounts[0]['phone'], '13800000000')
        self.assertEqual(accounts[0]['userid'], '11')
        self.assertEqual(accounts[0]['source'], 'desktop')
        # 客户端文件没有昵称：用手机尾号占位，导入时不再打 baseInfo
        self.assertEqual(accounts[0]['nickname'], 'Loomy 0000')
        # 毫秒 → 秒，并按客户端声称的 14 天算到期
        self.assertEqual(accounts[0]['obtainedAt'], 1763000000)
        self.assertEqual(accounts[0]['expireAt'],
                         1763000000 + C.SESSION_EXPIRE_SECONDS)
        # 没有时间戳的条目由适配器按导入时刻补
        self.assertEqual(accounts[1]['obtainedAt'], 0)

    def test_skips_empty_and_duplicate_sessions(self) -> None:
        self._write('aaaa', {'session': 'same'})
        self._write('bbbb', {'session': 'same'})
        self._write('cccc', {'session': '   '})
        self.assertEqual(len(desktop.read_accounts()), 1)

    def test_ignores_files_outside_userdata(self) -> None:
        stray = self.root / 'aaaa' / desktop.SESSION_FILE
        stray.parent.mkdir(parents=True)
        stray.write_text('{"session": "nope"}', encoding='utf-8')
        self.assertEqual(desktop.read_accounts(), [])

    def test_missing_root_is_not_an_error(self) -> None:
        self.assertEqual(desktop.storage_paths(), [])
        self.assertEqual(desktop.read_accounts(), [])

    def test_broken_json_is_skipped(self) -> None:
        path = self.root / 'aaaa' / 'userData' / desktop.SESSION_FILE
        path.parent.mkdir(parents=True)
        path.write_text('{oops', encoding='utf-8')
        self.assertEqual(desktop.read_accounts(), [])


class _FakeStreamResponse:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.closed = False
        self.headers = {'content-type': 'text/event-stream'}

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class LoomyAdapterTest(unittest.TestCase):
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
        self.adapter = LoomyAdapter()

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db

    def _seed(self, **overrides) -> dict:
        data = {
            'uid': 'u-1', 'session': 'session-1', 'password': 'pw',
            'phone': '13800000000', 'nickname': 'n', 'obtainedAt': int(time.time()),
        }
        data.update(overrides)
        store.save_account(data)
        return data

    # ── 解析 ──────────────────────────────────────────────
    def test_points_of_ignores_signin_current_balance(self) -> None:
        self.assertEqual(_points_of({'availableBalance': 5}), 5)
        self.assertEqual(_points_of({'totalBalance': 7}), 7)
        # 签到响应的 currentBalance 与 points/records 不是同一口径
        self.assertIsNone(_points_of({'currentBalance': 99}))

    def test_multiplier_last_x_wins(self) -> None:
        self.assertEqual(_multiplier_of({'multiplier': 2.0}), 2.0)
        self.assertEqual(_multiplier_of(
            {'name': 'Spark-X2.6 正式版(限时免费)(x0.0)', 'multiplier': 0}),
            0.0)
        self.assertIsNone(_multiplier_of({'multiplier': None, 'name': 'auto'}))

    def test_scan_sse_extracts_usage_and_first_content(self) -> None:
        usage: dict = {}
        pending, saw, error = _scan_sse(
            b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n'
            b'data: {"choices":[{"delta":{"content":"hi"}}]}\n',
            usage)
        self.assertTrue(saw)
        self.assertEqual(error, '')
        self.assertEqual(pending, b'')
        pending, saw, error = _scan_sse(
            b'data: {"choices":[{"delta":{"content":"!"}}],"usage":'
            b'{"prompt_tokens":3,"completion_tokens":4,"points_consumed":2}}\n',
            usage)
        self.assertTrue(saw)
        self.assertEqual(usage['prompt_tokens'], 3)
        self.assertEqual(usage['credit'], 2.0)

    def test_scan_sse_keeps_partial_line(self) -> None:
        usage: dict = {}
        pending, saw, _ = _scan_sse(b'data: {"choices":[{"delta":{"con', usage)
        self.assertEqual(pending, b'data: {"choices":[{"delta":{"con')
        self.assertFalse(saw)

    # ── 关闭思考（/no_think）──────────────────────────────
    def test_no_think_only_when_client_asks(self) -> None:
        body = {'messages': [{'role': 'user', 'content': 'hi'}]}
        self.assertIsNone(_no_think_messages(body))
        self.assertEqual(body['messages'][0]['content'], 'hi')
        self.assertIsNone(_no_think_messages(
            {'messages': [{'role': 'user', 'content': 'hi'}],
             'reasoning_effort': 'high'}))

    def test_no_think_prefixes_first_user_message(self) -> None:
        body = {'reasoning_effort': 'none',
                'messages': [{'role': 'system', 'content': 's'},
                             {'role': 'user', 'content': 'hi'},
                             {'role': 'user', 'content': 'again'}]}
        out = _no_think_messages(body)
        self.assertEqual([m['content'] for m in out], ['s', '/no_think hi', 'again'])
        # 不改调用方传进来的请求体
        self.assertEqual(body['messages'][1]['content'], 'hi')

    def test_no_think_accepts_thinking_flags(self) -> None:
        for extra in ({'enable_thinking': False},
                      {'chat_template_kwargs': {'enable_thinking': False}}):
            with self.subTest(extra=extra):
                out = _no_think_messages(
                    {'messages': [{'role': 'user', 'content': 'hi'}], **extra})
                self.assertEqual(out[0]['content'], '/no_think hi')

    def test_no_think_does_not_double_prefix(self) -> None:
        out = _no_think_messages({'reasoning_effort': 'none',
                                  'messages': [{'role': 'user',
                                                'content': '/no_think hi'}]})
        self.assertIsNone(out)

    def test_no_think_handles_multimodal_content(self) -> None:
        out = _no_think_messages({'reasoning_effort': 'none', 'messages': [{
            'role': 'user',
            'content': [{'type': 'text', 'text': 'look'},
                        {'type': 'image_url', 'image_url': {'url': 'x'}}]}]})
        self.assertEqual(out[0]['content'][0]['text'], '/no_think look')
        self.assertEqual(out[0]['content'][1]['type'], 'image_url')

    # ── 导入 / 续期 ───────────────────────────────────────
    def test_import_session_only_derives_uid(self) -> None:
        with mock.patch.object(client, 'query_base_info_sync',
                               return_value={'userid': 'u-9',
                                             'nickname': '昵称'}) as info:
            public = self.adapter.import_account({'session': 'abc123'})
        info.assert_called_once()
        self.assertEqual(public['uid'], 'u-9')
        self.assertEqual(public['nickname'], '昵称')
        self.assertTrue(public['has_token'])
        self.assertFalse(public['has_password'])

    def test_import_with_password_logs_in(self) -> None:
        fake = {
            'session': 'new-session', 'userid': 'u-7', 'phone': '13900000000',
            'nickname': '新号', 'expire_at': int(time.time()) + 3600,
        }
        with mock.patch.object(client, 'login_by_password_sync',
                               return_value=fake) as login:
            public = self.adapter.import_account({
                'phone': '13900000000', 'password': 'pw-1',
            })
        login.assert_called_once()
        self.assertEqual(public['uid'], 'u-7')
        self.assertTrue(public['has_password'])
        raw = store.read_account('u-7')
        self.assertEqual(raw['session'], 'new-session')
        self.assertEqual(raw['password'], 'pw-1')

    def test_import_requires_credentials(self) -> None:
        with self.assertRaises(PlatformError):
            self.adapter.import_account({'nickname': 'x'})

    def test_import_from_desktop_skips_bad_entries(self) -> None:
        sessions = [
            {'phone': '13800000000', 'userid': 'u-1', 'session': 's1',
             'source': 'desktop'},
            {'source': 'desktop'},          # 既没 session 也没密码
        ]
        info = mock.patch.object(
            client, 'query_base_info_sync',
            side_effect=PlatformError('登录已失效', status=401,
                                      code='login_expired'))
        with mock.patch.object(desktop, 'read_accounts',
                               return_value=sessions), \
                mock.patch.object(desktop, 'storage_paths', return_value=[
                    {'path': 'C:/Users/Public/Loomy/aaaa/userData/auth-session.json',
                     'user': 'aaaa'}]), info:
            out = self.adapter.import_from_desktop()
        self.assertEqual(out['platform'], 'loomy')
        self.assertEqual(len(out['imported']), 1)
        self.assertEqual(out['imported'][0]['uid'], 'u-1')
        self.assertEqual(len(out['failed']), 1)
        self.assertIn('auth-session.json', out['sources'][0])

    def test_import_from_desktop_writes_session(self) -> None:
        with mock.patch.object(desktop, 'read_accounts', return_value=[
                {'phone': '13800000000', 'userid': 'u-5', 'session': 'from-client',
                 'nickname': '', 'source': 'desktop'}]), \
                mock.patch.object(desktop, 'storage_paths', return_value=[]), \
                mock.patch.object(client, 'query_base_info_sync',
                                  return_value={'nickname': '本机号'}):
            self.adapter.import_from_desktop()
        raw = store.read_account('u-5')
        self.assertEqual(raw['session'], 'from-client')
        self.assertEqual(raw['nickname'], '本机号')
        # 只有会话没有密码：能用到失效为止，不能续期
        self.assertEqual(raw['password'], '')
        self.assertEqual(raw['source'], 'desktop')

    def test_refresh_requires_password(self) -> None:
        self._seed(password='')
        with self.assertRaises(PlatformError) as ctx:
            asyncio.run(self.adapter.refresh_account('u-1'))
        self.assertEqual(ctx.exception.code, 'no_password')

    def test_refresh_relogins_and_saves(self) -> None:
        self._seed(session='old')
        with mock.patch.object(client, 'login_by_password',
                               new=mock.AsyncMock(return_value={
                                   'session': 'fresh', 'userid': 'u-1',
                                   'phone': '13800000000',
                                   'expire_at': int(time.time()) + 60})) as login:
            asyncio.run(self.adapter.refresh_account('u-1'))
        login.assert_awaited_once()
        self.assertEqual(store.read_account('u-1')['session'], 'fresh')

    # ── 选号 ──────────────────────────────────────────────
    def test_candidates_skip_disabled_and_cooling(self) -> None:
        self._seed(uid='u-1')
        self._seed(uid='u-2', disabled=True)
        self._seed(uid='u-3', coolingUntil=time.time() + 600)
        ids = [a['uid'] for a in self.adapter._candidates()]
        self.assertEqual(ids, ['u-1'])

    def test_weights_rotate_first_choice(self) -> None:
        self._seed(uid='u-1', weight=1)
        self._seed(uid='u-2', weight=1)
        firsts = [self.adapter._candidates()[0]['uid'] for _ in range(4)]
        self.assertEqual(sorted(firsts), ['u-1', 'u-1', 'u-2', 'u-2'])

    def test_affinity_pins_account(self) -> None:
        self._seed(uid='u-1', weight=1)
        self._seed(uid='u-2', weight=1)
        self.adapter._affinity.bind('k', 'u-2')
        self.assertEqual(self.adapter._candidates('k')[0]['uid'], 'u-2')

    # ── 对话 ──────────────────────────────────────────────
    def test_chat_nonstream_translates_model_and_credit(self) -> None:
        self._seed()
        captured: dict = {}

        async def fake_chat(session, body, **kwargs):
            captured.update(body)
            return {'choices': [{'message': {'content': 'ok'}}],
                    'usage': {'prompt_tokens': 1, 'completion_tokens': 2,
                              'points_consumed': 3}}

        with mock.patch.object(client, 'chat', new=fake_chat):
            chat = asyncio.run(self.adapter.chat(
                'deepseek-v4.1-flash-1017',
                {'messages': [{'role': 'user', 'content': 'hi'}]}))
        self.assertEqual(captured['model'], 'deepseek-v4-flash-1017')
        self.assertEqual(chat.payload['choices'][0]['message']['content'], 'ok')
        self.assertEqual(chat.usage['credit'], 3.0)
        self.assertEqual(chat.model, 'loomy/deepseek-v4.1-flash-1017')

    def test_chat_sends_no_think_prefix(self) -> None:
        self._seed()
        captured: dict = {}

        async def fake_chat(session, body, **kwargs):
            captured.update(body)
            return {'choices': [], 'usage': {}}

        body = {'reasoning_effort': 'none',
                'messages': [{'role': 'user', 'content': 'hi'}]}
        with mock.patch.object(client, 'chat', new=fake_chat):
            asyncio.run(self.adapter.chat('m', body))
        self.assertEqual(captured['messages'][0]['content'], '/no_think hi')
        # 调用方（网关日志 / 重试）拿到的还是原始请求体
        self.assertEqual(body['messages'][0]['content'], 'hi')

    def test_chat_retries_after_session_invalid(self) -> None:
        self._seed(session='dead')
        calls = {'n': 0}

        async def fake_chat(session, body, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                raise PlatformError('401', status=401, code='session_invalid')
            return {'choices': [], 'usage': {}}

        relogin = mock.AsyncMock(return_value={
            'session': 'alive', 'userid': 'u-1', 'phone': '13800000000'})
        with mock.patch.object(client, 'chat', new=fake_chat), \
                mock.patch.object(client, 'login_by_password', new=relogin):
            asyncio.run(self.adapter.chat('m', {'messages': []}))
        relogin.assert_awaited_once()
        self.assertEqual(calls['n'], 2)
        self.assertEqual(store.read_account('u-1')['session'], 'alive')

    def test_chat_stream_relays_bytes_and_fills_usage(self) -> None:
        self._seed()
        chunks = [
            b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n',
            b'data: {"choices":[{"delta":{"content":"hi"}}]}\n',
            b'data: {"usage":{"prompt_tokens":5,"completion_tokens":6,',
            b'"points_consumed":4}}\n',
            b'data: [DONE]\n',
        ]
        response = _FakeStreamResponse(chunks)

        async def fake_open(*args, **kwargs):
            return response

        with mock.patch.object(client, 'open_chat', new=fake_open):
            chat = asyncio.run(self.adapter.chat(
                'm', {'stream': True, 'messages': []}))

            async def collect():
                out = b''
                async for chunk in chat.raw_stream:
                    out += chunk
                return out

            out = asyncio.run(collect())
        self.assertEqual(out, b''.join(chunks))
        self.assertEqual(chat.usage['prompt_tokens'], 5)
        self.assertEqual(chat.usage['credit'], 4.0)
        self.assertIsNotNone(chat.first_token_ms)
        self.assertTrue(response.closed)

    # ── 任务 ──────────────────────────────────────────────
    def test_credits_parses_available_balance(self) -> None:
        self._seed()
        payload = {'data': {'availableBalance': 321, 'totalBalance': 400,
                            'dailyQuota': 1000}}
        with mock.patch.object(client, 'fetch_points',
                               new=mock.AsyncMock(return_value=payload)):
            out = asyncio.run(self.adapter.credits('u-1'))
        self.assertEqual(out['credits'], 321)
        self.assertEqual(out['remain'], 321)
        self.assertEqual(out['unit'], 'credits')
        self.assertEqual(out['daily_quota'], 1000)

    def test_credits_reports_expired_session(self) -> None:
        self._seed()
        payload = {'code': '100002', 'desc': '登录已失效', 'data': {}}
        with mock.patch.object(client, 'fetch_points',
                               new=mock.AsyncMock(return_value=payload)):
            with self.assertRaises(PlatformError) as ctx:
                asyncio.run(self.adapter.credits('u-1'))
        self.assertEqual(ctx.exception.status, 401)
        self.assertEqual(ctx.exception.code, 'login_expired')

    def test_checkin_computes_earned_delta(self) -> None:
        self._seed()
        balances = iter([100, 150])
        payload = {'code': '000000', 'data': {
            'alreadyProcessed': False, 'currentBalance': 150,
            'permanentBalance': 150, 'dailyBalance': 0, 'dailyQuota': 1000}}

        async def fake_fetch(*args, **kwargs):
            return {'data': {'availableBalance': next(balances)}}

        with mock.patch.object(client, 'fetch_points', new=fake_fetch), \
                mock.patch.object(client, 'first_login',
                                  new=mock.AsyncMock(return_value=payload)):
            result = asyncio.run(self.adapter.checkin('u-1'))
        self.assertTrue(result['ok'])
        self.assertFalse(result['already'])
        self.assertEqual(result['earned_credit'], 50)
        self.assertEqual(result['credits'], 150)

    def test_checkin_already_processed_earns_zero(self) -> None:
        self._seed()
        payload = {'code': '000000', 'data': {'alreadyProcessed': True,
                                              'currentBalance': 150}}
        with mock.patch.object(client, 'fetch_points',
                               new=mock.AsyncMock(return_value={
                                   'data': {'availableBalance': 150}})), \
                mock.patch.object(client, 'first_login',
                                  new=mock.AsyncMock(return_value=payload)):
            result = asyncio.run(self.adapter.checkin('u-1'))
        self.assertTrue(result['already'])
        self.assertEqual(result['earned_credit'], 0)

    # ── 模型 ──────────────────────────────────────────────
    def test_list_models_exposes_prefix_and_filters_images(self) -> None:
        models = asyncio.run(self.adapter.list_models())
        ids = {m['id'] for m in models}
        self.assertIn('loomy/deepseek-v4.1-flash-1017', ids)
        self.assertNotIn('loomy/Hy-Image-3.5-preview', ids)
        for item in models:
            self.assertEqual(item['realm'], 'cn')
            self.assertEqual(item['platform'], 'loomy')

    def test_cached_model_ids_include_fallback(self) -> None:
        ids = self.adapter.cached_model_ids()
        self.assertIn('loomy/spark-x2.6', ids)


class LoomyConstantsTest(unittest.TestCase):
    def test_wire_and_outward_aliases(self) -> None:
        self.assertEqual(C.wire_model('deepseek-v4.1-flash'),
                         'DeepSeek-V4.1-Flash')
        self.assertEqual(C.wire_model('deepseek-v4.1-flash-1017'),
                         'deepseek-v4-flash-1017')
        self.assertEqual(C.outward_model('DeepSeek-V4.1-Flash'),
                         'deepseek-v4.1-flash')
        self.assertEqual(C.outward_model('deepseek-v4-flash-1017'),
                         'deepseek-v4.1-flash-1017')
        self.assertEqual(C.wire_model('GLM-5.3-Flash'), 'GLM-5.3-Flash')


class LoomyClientTest(unittest.TestCase):
    """上游「HTTP 200 + code=100002」的失效形态必须归成 401。"""

    @staticmethod
    def _mock_http(handler):
        @asynccontextmanager
        async def factory(*args, **kwargs):
            async with httpx.AsyncClient(
                    transport=httpx.MockTransport(handler)) as http:
                yield http
        return factory

    @staticmethod
    def _expired(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={'content-type': 'application/json'},
            json={'code': '100002', 'desc': '登录已失效，请重新登录',
                  'data': {}})

    def test_chat_nonstream_business_expiry_is_401(self) -> None:
        with mock.patch.object(config, 'http_client',
                               self._mock_http(self._expired)):
            with self.assertRaises(PlatformError) as ctx:
                asyncio.run(client.chat('s', {'messages': []}))
        self.assertEqual(ctx.exception.status, 401)
        self.assertEqual(ctx.exception.code, 'login_expired')

    def test_open_chat_business_expiry_is_401(self) -> None:
        with mock.patch.object(config, 'http_client',
                               self._mock_http(self._expired)):
            with self.assertRaises(PlatformError) as ctx:
                asyncio.run(client.open_chat('s', {'stream': True}))
        self.assertEqual(ctx.exception.status, 401)
        # 关键：不是把 JSON 当 SSE 透传，也不是 502。
        self.assertEqual(ctx.exception.code, 'login_expired')

    def test_open_chat_passes_sse_through(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, headers={'content-type': 'text/event-stream'},
                content=b'data: {"choices":[]}\n\ndata: [DONE]\n\n')

        with mock.patch.object(config, 'http_client',
                               self._mock_http(handler)):
            response = asyncio.run(client.open_chat('s', {'stream': True}))

            async def read() -> bytes:
                try:
                    return await response.aread()
                finally:
                    await response.aclose()

            body = asyncio.run(read())
        self.assertIn(b'[DONE]', body)
        self.assertTrue(response.is_closed)

    def test_points_breakdown_reports_expired_session(self) -> None:
        payload = {'code': '100002', 'desc': '登录已失效', 'data': {}}
        with mock.patch.object(client, 'fetch_points',
                               new=mock.AsyncMock(return_value=payload)):
            with self.assertRaises(PlatformError) as ctx:
                asyncio.run(client.fetch_points_breakdown('s'))
        self.assertEqual(ctx.exception.code, 'login_expired')


class LoomyWechatTest(unittest.TestCase):
    """微信授权页 / 回调解析（全部离线，只测形状与错误分类）。"""

    def test_build_auth_url(self) -> None:
        state = 'a' * 32
        url = wechat.build_auth_url(state)
        self.assertTrue(url.startswith(
            'https://open.weixin.qq.com/connect/qrconnect?'))
        self.assertIn('appid=wx18d60be432287cf8', url)
        self.assertIn(
            'redirect_uri=https%3A%2F%2Floomy.xunfei.cn%2Foauth%2Fwechat%2Fcallback',
            url)
        self.assertIn('response_type=code', url)
        self.assertIn('scope=snsapi_login', url)
        self.assertIn(f'state={state}', url)
        self.assertTrue(url.endswith('#wechat_redirect'))

    def test_build_auth_url_requires_state(self) -> None:
        with self.assertRaises(PlatformError) as ctx:
            wechat.build_auth_url('')
        self.assertEqual(ctx.exception.code, 'login_state_required')

    def test_parse_callback_full_url(self) -> None:
        parsed = wechat.parse_callback(
            'https://loomy.xunfei.cn/oauth/wechat/callback'
            '?code=061abcDEF_-&state=deadbeef#wechat_redirect')
        self.assertEqual(parsed, {'code': '061abcDEF_-', 'state': 'deadbeef'})

    def test_parse_callback_query_and_bare_code(self) -> None:
        self.assertEqual(wechat.parse_callback('code=abcdefgh12&state=x'),
                         {'code': 'abcdefgh12', 'state': 'x'})
        self.assertEqual(wechat.parse_callback('abcdefgh12'),
                         {'code': 'abcdefgh12', 'state': ''})

    def test_parse_callback_denied(self) -> None:
        with self.assertRaises(PlatformError) as ctx:
            wechat.parse_callback(
                'https://loomy.xunfei.cn/oauth/wechat/callback'
                '?errmsg=access_denied&state=x')
        self.assertEqual(ctx.exception.code, 'login_denied')
        self.assertIn('access_denied', ctx.exception.message)

    def test_parse_callback_short_code_rejected(self) -> None:
        with self.assertRaises(PlatformError) as ctx:
            wechat.parse_callback('abcd')
        self.assertEqual(ctx.exception.code, 'callback_invalid')

    def test_fetch_login_qr_uses_fordevtool_uuid(self) -> None:
        html = ('<script>var fordevtool = "https://long.open.weixin.qq.com'
                '/connect/l/qrconnect?uuid=0712abcdEFG-x_y";</script>')
        with mock.patch.object(wechat, '_get', return_value=html):
            out = wechat.fetch_login_qr('s' * 32)
        self.assertEqual(out['uuid'], '0712abcdEFG-x_y')
        self.assertEqual(out['qr_url'],
                         'https://open.weixin.qq.com/connect/qrcode/0712abcdEFG-x_y')
        self.assertIn('qrconnect', out['auth_url'])

    def test_fetch_login_qr_loose_match(self) -> None:
        with mock.patch.object(wechat, '_get',
                               return_value='<img src="x?uuid=0123456789abcdef">'):
            out = wechat.fetch_login_qr('s' * 32)
        self.assertEqual(out['uuid'], '0123456789abcdef')

    def test_fetch_login_qr_without_uuid(self) -> None:
        with mock.patch.object(wechat, '_get', return_value='<html>403</html>'):
            with self.assertRaises(PlatformError) as ctx:
                wechat.fetch_login_qr('s' * 32)
        self.assertEqual(ctx.exception.code, 'wechat_no_qr')

    def test_poll_login_confirmed(self) -> None:
        body = "window.wx_errcode=405;window.wx_code='CODE-1';"
        with mock.patch.object(wechat, '_get', return_value=body) as get:
            out = wechat.poll_login('uuid-1', timeout=5)
        self.assertEqual(out['errcode'], wechat.ERR_CONFIRMED)
        self.assertEqual(out['code'], 'CODE-1')
        self.assertIn('uuid=uuid-1', get.call_args[0][0])

    def test_poll_login_timeout_is_waiting(self) -> None:
        timeout = PlatformError('微信服务响应超时', status=504,
                                code='wechat_timeout')
        with mock.patch.object(wechat, '_get', side_effect=timeout):
            out = wechat.poll_login('uuid-1', timeout=1)
        self.assertEqual(out['errcode'], wechat.ERR_WAITING)
        self.assertEqual(out['code'], '')


class LoomyWechatClientTest(unittest.TestCase):
    """`bind/auth` → `bind/skip` 的字段与错误分类。"""

    def _calls(self, auth_data: dict, skip_data: dict | None = None,
               extra: list | None = None):
        calls: list = []

        def fake(path, body, **kwargs):
            calls.append((path, body))
            if path.endswith('/bind/auth'):
                if extra is not None:
                    extra.append(path)
                return {'code': '000000', 'data': auth_data}
            return {'code': '000000', 'data': skip_data or {}}

        return calls, mock.patch.object(client, '_account_call_sync',
                                        side_effect=fake)

    def test_bind_one_exchanges_session(self) -> None:
        calls, patch = self._calls(
            {'bind': 1, 'rcode': 'rc-1'},
            {'session': 'wx-session', 'userid': 'u-3', 'phone': '13800000001'})
        with patch:
            out = client.login_by_wechat_sync('wx-code')
        self.assertEqual([c[0] for c in calls],
                         ['/login/thirdAccount/bind/auth',
                          '/login/thirdAccount/bind/skip'])
        self.assertEqual(calls[0][1]['param'],
                         {'tcode': {'code': 'wx-code'}, 'type': 'wx'})
        self.assertEqual(calls[1][1]['param']['rcode'], 'rc-1')
        self.assertEqual(out['session'], 'wx-session')
        self.assertEqual(out['userid'], 'u-3')

    def test_bind_zero_requires_phone_binding(self) -> None:
        calls, patch = self._calls({'bind': 0, 'rcode': 'rc-1'})
        with patch:
            with self.assertRaises(client.AccountError) as ctx:
                client.login_by_wechat_sync('wx-code')
        self.assertEqual(ctx.exception.code, 'wechat_unbound')
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(len(calls), 1)          # 不能再去 bind/skip

    def test_missing_rcode_is_upstream_error(self) -> None:
        _calls, patch = self._calls({'bind': 1})
        with patch:
            with self.assertRaises(client.AccountError) as ctx:
                client.login_by_wechat_sync('wx-code')
        self.assertEqual(ctx.exception.code, 'wechat_no_rcode')
        self.assertEqual(ctx.exception.status, 502)

    def test_missing_code_never_hits_upstream(self) -> None:
        calls, patch = self._calls({'bind': 1})
        with patch:
            with self.assertRaises(client.AccountError) as ctx:
                client.login_by_wechat_sync('  ')
        self.assertEqual(ctx.exception.code, 'missing_code')
        self.assertEqual(calls, [])


class LoomyWechatLoginTest(unittest.TestCase):
    """登录会话状态机：扫码完成、粘回调、取消、过期与退化路径。"""

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
        with login._lock:
            login._sessions.clear()
        self.addCleanup(self._clear_sessions)

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db

    @staticmethod
    def _clear_sessions() -> None:
        with login._lock:
            for session in login._sessions.values():
                stop = session.get('stop')
                if stop is not None:
                    stop.set()
            login._sessions.clear()

    def _session(self, login_id: str = 'a' * 32, qr_uuid: str = '') -> str:
        now = time.time()
        with login._lock:
            login._sessions[login_id] = {
                'id': login_id, 'status': 'pending', 'message': '',
                'uuid': qr_uuid, 'qr': '', 'url': wechat.build_auth_url(login_id),
                'account': None, 'created': now, 'updated': now,
                'stop': threading.Event(),
            }
        return login_id

    def test_start_returns_pending_flow(self) -> None:
        class _NoThread:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def start(self) -> None:
                pass

        with mock.patch.object(login.threading, 'Thread', _NoThread):
            out = login.start()
        self.assertEqual(out['status'], 'pending')
        self.assertEqual(len(out['login_id']), 32)
        self.assertIn('qrconnect', out['url'])
        self.assertIn(out['login_id'], out['url'])
        self.assertEqual(out['expires_in'], login.LOGIN_TTL)
        self.assertEqual(login.poll(out['login_id'])['login_id'], out['login_id'])

    def test_start_rejects_too_many_pending(self) -> None:
        for index in range(login._MAX_PENDING):
            self._session(login_id=f'{index:032x}')
        with self.assertRaises(PlatformError) as ctx:
            login.start()
        self.assertEqual(ctx.exception.status, 429)

    def test_run_falls_back_to_paste_when_qr_fails(self) -> None:
        sid = self._session()
        failure = PlatformError('微信服务不可达', status=502,
                                code='wechat_unreachable')
        with mock.patch.object(wechat, 'fetch_login_qr', side_effect=failure):
            login._run(sid)
        view = login.poll(sid)
        self.assertEqual(view['status'], 'pending')
        self.assertIn('二维码获取失败', view['message'])
        self.assertEqual(view['qr'], '')

    def test_wait_confirmed_logs_in_and_saves(self) -> None:
        sid = self._session(qr_uuid='uuid-1')
        polls = [
            {'errcode': wechat.ERR_WAITING, 'code': '', 'text': ''},
            {'errcode': wechat.ERR_CONFIRMED, 'code': 'CODE-1', 'text': ''},
        ]
        account = {'session': 'sess-wx', 'userid': 'u-9',
                   'phone': '13800000000', 'nickname': '微信用户'}
        with mock.patch.object(wechat, 'poll_login', side_effect=polls), \
                mock.patch.object(client, 'login_by_wechat_sync',
                                  return_value=account) as exchange:
            login._wait(sid)
        exchange.assert_called_once_with('CODE-1')
        view = login.poll(sid)
        self.assertEqual(view['status'], 'success')
        self.assertEqual(view['account']['uid'], 'u-9')
        raw = store.read_account('u-9')
        self.assertEqual(raw['session'], 'sess-wx')
        self.assertEqual(raw['source'], 'web')
        self.assertFalse(raw['password'])

    def test_wait_marks_scanned(self) -> None:
        sid = self._session(qr_uuid='uuid-1')

        def fake_poll(uuid: str, *, timeout: float = 0) -> dict:
            with login._lock:
                login._sessions[sid]['stop'].set()
            return {'errcode': wechat.ERR_SCANNED, 'code': '', 'text': ''}

        with mock.patch.object(wechat, 'poll_login', side_effect=fake_poll):
            login._wait(sid)
        self.assertEqual(login.poll(sid)['message'], '已扫码，请在手机上确认')

    def test_wait_handles_user_cancel(self) -> None:
        sid = self._session(qr_uuid='uuid-1')
        with mock.patch.object(wechat, 'poll_login',
                               return_value={'errcode': wechat.ERR_CANCELLED,
                                             'code': '', 'text': ''}):
            login._wait(sid)
        view = login.poll(sid)
        self.assertEqual(view['status'], 'expired')
        self.assertIn('取消', view['message'])

    def test_submit_callback_checks_state(self) -> None:
        sid = self._session()
        with self.assertRaises(PlatformError) as ctx:
            login.submit_callback(
                sid, 'https://loomy.xunfei.cn/oauth/wechat/callback'
                     '?code=abcdefgh12&state=' + 'b' * 32)
        self.assertEqual(ctx.exception.code, 'login_state_mismatch')

    def test_submit_callback_success(self) -> None:
        sid = self._session()
        account = {'session': 'sess-wx', 'userid': 'u-2', 'phone': '',
                   'nickname': '微信用户'}
        url = ('https://loomy.xunfei.cn/oauth/wechat/callback'
               f'?code=abcdefgh12&state={sid}')
        with mock.patch.object(client, 'login_by_wechat_sync',
                               return_value=account) as exchange:
            out = login.submit_callback(sid, url)
        exchange.assert_called_once_with('abcdefgh12')
        self.assertEqual(out['status'], 'success')
        self.assertEqual(store.read_account('u-2')['source'], 'web')
        # 成功后再提交同一链接：直接返回成功，不重复打上游。
        with mock.patch.object(client, 'login_by_wechat_sync') as again:
            self.assertEqual(login.submit_callback(sid, url)['status'], 'success')
        again.assert_not_called()

    def test_submit_callback_marks_error_on_unbound_wechat(self) -> None:
        sid = self._session()
        denied = PlatformError('该微信还没有绑定讯飞账号', status=400,
                               code='wechat_unbound')
        with mock.patch.object(client, 'login_by_wechat_sync', side_effect=denied):
            with self.assertRaises(PlatformError):
                login.submit_callback(sid, 'code=abcdefgh12')
        self.assertEqual(login.poll(sid)['status'], 'error')

    def test_cancel_removes_session(self) -> None:
        sid = self._session()
        self.assertTrue(login.cancel(sid))
        self.assertFalse(login.cancel(sid))
        with self.assertRaises(PlatformError) as ctx:
            login.poll(sid)
        self.assertEqual(ctx.exception.status, 404)

    def test_adapter_exposes_web_login(self) -> None:
        self.assertTrue(LoomyAdapter.supports_web_login)
        for name in ('login_start', 'login_poll', 'login_cancel',
                     'login_submit_callback'):
            self.assertTrue(callable(getattr(LoomyAdapter, name, None)), name)


class LoomyRoutingTest(unittest.TestCase):
    def test_registry_routes_loomy_prefix(self) -> None:
        from server.platforms import registry

        self.assertEqual(registry.route_model('loomy/deepseek-v4.1-flash'),
                         ('loomy', 'deepseek-v4.1-flash', None))
        # 没有区域后缀这一说：`loomycn/...` 不是本平台的别名。
        self.assertIsNone(registry.route_model('loomycn/deepseek-v4.1-flash'))

    def test_settings_capabilities_exclude_trae_only_fields(self) -> None:
        from server.platforms import settings as runtime_settings

        caps = runtime_settings.capabilities('loomy')
        self.assertTrue(caps['session_affinity'])
        self.assertTrue(caps['catalog_ttl'])
        self.assertTrue(caps['credits_ttl'])
        self.assertFalse(caps['desktop_sync'])
        self.assertFalse(caps['solo_max_tokens'])


if __name__ == '__main__':
    unittest.main()
