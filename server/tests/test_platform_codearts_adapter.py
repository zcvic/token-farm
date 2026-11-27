"""华为云 CodeArts 码道（codearts）：签名 / DPoP / 令牌轮换 / 目录 / 对话 / 登录。

全部离线。这里钉住几件最容易接错的协议事实：

  · AK/SK 签名把**请求里的全部头**都算进 SignedHeaders，`maas_type` 必须在
    签名前加、`Chat-Id` / `Session-Id` 必须在签名后加；
  · `refresh_token` 一次性轮换且与 client_id + DPoP 私钥绑定，刷新成功必须
    **先落盘再使用**；
  · SSE 里的 `text` 是**累计全文**（替换语义），不能当增量透传；
  · 模型目录按账号，福利模型必须带 `maas_type: benefit`，漏带就是 404；
  · 登录第二次回调**只带 code**，按 secret → ticket_id → 活跃会话倒序逐个试。
"""
from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from server import config, db
from server.platforms.base import PlatformError
from server.platforms.codearts import (
    catalog, client, constants as C, dpop, login, signer, store)
from server.platforms.codearts.adapter import CodeArtsAdapter
from server.platforms import settings as runtime_settings


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


def _sse(*lines: str) -> str:
    return '\n'.join(lines)


class _DbCase(unittest.TestCase):
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

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db


# ── 签名 ────────────────────────────────────────────────────
class SignerTest(unittest.TestCase):
    def test_signed_headers_include_every_header(self) -> None:
        headers = signer.sign(
            'POST', f'{C.SNAP_HOST}{C.EP_CHAT_V2}',
            {'Content-Type': 'application/json', 'maas_type': 'benefit'},
            b'{"model":"GLM-5.2"}',
            access_key='AK', secret_key='SK', security_token='TOK',
            now=1700000000)
        auth = headers['Authorization']
        self.assertTrue(auth.startswith('SDK-HMAC-SHA256 Access=AK, '))
        # 签名前加的头必须在 SignedHeaders 里：漏了上游按「未签名头」处理。
        self.assertIn('maas_type', auth)
        self.assertIn('content-type', auth)
        self.assertIn('host', auth)
        self.assertIn('x-sdk-content-sha256', auth)
        self.assertIn('x-sdk-date', auth)
        self.assertIn('x-security-token', auth)
        # SignedHeaders 必须按字母序，否则签名与上游重算的不一致。
        signed = auth.split('SignedHeaders=')[1].split(',')[0].split(';')
        self.assertEqual(signed, sorted(signed))
        self.assertEqual(headers['X-Sdk-Date'], '20231114T221320Z')
        self.assertEqual(headers['Host'],
                         'snap-access.cn-north-4.myhuaweicloud.com')

    def test_signature_is_stable_for_same_input(self) -> None:
        args = dict(access_key='AK', secret_key='SK', now=1700000000)
        first = signer.sign('GET', 'https://x.example/v1/a?b=2&a=1',
                            {'X-Language': 'zh-cn'}, b'', **args)
        second = signer.sign('GET', 'https://x.example/v1/a?b=2&a=1',
                             {'X-Language': 'zh-cn'}, b'', **args)
        self.assertEqual(first['Authorization'], second['Authorization'])

    def test_content_length_is_not_signed(self) -> None:
        args = dict(access_key='AK', secret_key='SK', now=1700000000)
        base = signer.sign('POST', 'https://x.example/v1/a', {'A': '1'},
                           b'x', **args)
        with_len = signer.sign('POST', 'https://x.example/v1/a',
                               {'A': '1', 'Content-Length': '1'}, b'x', **args)
        # 客户端发送时才补 Content-Length；把它算进去签名就对不上了。
        self.assertNotEqual(base['Authorization'], with_len['Authorization'])
        self.assertNotIn('content-length', base['Authorization'])

    def test_empty_body_hash_matches_sha256_of_empty(self) -> None:
        headers = signer.sign('GET', 'https://x.example/v1/a', {},
                              b'', access_key='AK', secret_key='SK',
                              now=1700000000)
        self.assertEqual(
            headers['X-Sdk-Content-Sha256'],
            'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855')

    def test_canonical_uri_appends_trailing_slash(self) -> None:
        self.assertEqual(signer.canonical_uri('/v1/ops/delivery'),
                         '/v1/ops/delivery/')
        self.assertEqual(signer.canonical_uri('/'), '/')

    def test_canonical_query_sorts_keys_and_values(self) -> None:
        self.assertEqual(signer.canonical_query('b=2&a=1&a=0'),
                         'a=0&a=1&b=2')


# ── DPoP ────────────────────────────────────────────────────
class DpopTest(unittest.TestCase):
    def test_proof_round_trips(self) -> None:
        jwk = dpop.generate_jwk()
        token = dpop.proof(jwk, C.TOKEN_URL, now=1700000000)
        out = dpop.verify_proof(token, expected_htu=C.TOKEN_URL)
        self.assertEqual(out['header']['alg'], 'ES256')
        self.assertEqual(out['header']['typ'], 'dpop+jwt')
        self.assertEqual(out['payload']['htm'], 'POST')
        self.assertEqual(out['payload']['iat'], 1700000000)
        # 公钥只带 x / y，私钥 d 绝不能出现在 JWT header 里。
        self.assertNotIn('d', out['header']['jwk'])

    def test_signature_is_raw_rs_not_der(self) -> None:
        token = dpop.proof(dpop.generate_jwk(), C.TOKEN_URL)
        raw = dpop.b64url_decode(token.split('.')[2])
        self.assertEqual(len(raw), 64)

    def test_normalize_rejects_mismatched_key(self) -> None:
        left = dpop.generate_jwk()
        right = dpop.generate_jwk()
        broken = {**left, 'x': right['x'], 'y': right['y']}
        with self.assertRaises(ValueError):
            dpop.normalize_jwk(broken)
        with self.assertRaises(ValueError):
            dpop.normalize_jwk({'kty': 'RSA'})

    def test_pkce_pair_shape(self) -> None:
        verifier, challenge = dpop.pkce_pair()
        self.assertEqual(len(verifier), 86)
        self.assertEqual(len(challenge), 43)
        import hashlib
        self.assertEqual(
            challenge,
            _b64(hashlib.sha256(verifier.encode('ascii')).digest()))


# ── 存储 ────────────────────────────────────────────────────
class StoreTest(_DbCase):
    def test_safe_id_rejects_traversal(self) -> None:
        for bad in ('../x', 'a/b', 'a\\b', '', 'x' * 200):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    store.safe_id(bad)

    def test_derive_uid_prefers_user_id(self) -> None:
        first = store.derive_uid('u-1', 'alice')
        second = store.derive_uid('u-1', 'alice-renamed')
        self.assertTrue(first.startswith('ca_'))
        self.assertEqual(first, second, 'uid 不该随用户名变化')

    def test_public_account_hides_credentials(self) -> None:
        saved = store.save_account({
            'uid': 'ca_1', 'accessKeyId': 'AKIDEXAMPLE',
            'secretAccessKey': 'SECRET-VALUE', 'securityToken': 'STS-TOKEN',
            'refreshToken': 'REFRESH-VALUE', 'dpopJwk': dpop.generate_jwk(),
            'expiredAt': store.iso_from_epoch(time.time() + 3600)})
        public = store.public_account(saved)
        dumped = json.dumps(public, ensure_ascii=False)
        for secret in ('AKIDEXAMPLE', 'SECRET-VALUE', 'STS-TOKEN',
                       'REFRESH-VALUE'):
            self.assertNotIn(secret, dumped)
        self.assertTrue(public['has_token'])
        self.assertTrue(public['has_refresh_token'])
        self.assertTrue(public['token_auto_refresh'])
        self.assertEqual(public['realm'], 'cn')

    def test_normalize_credentials_accepts_reference_shape(self) -> None:
        out = store.normalize_credentials({
            'account': {
                'user_id': 'u-9',
                'refresh_token': 'RT',
                'credentials': {'access_key_id': 'AK', 'secret_access_key': 'SK',
                                'security_token': 'ST',
                                'expiration': '2026-01-01T00:00:00Z'},
                'dpop_private_key': dpop.generate_jwk(),
            }})
        self.assertEqual(out['uid'], 'u-9')
        self.assertEqual(out['accessKeyId'], 'AK')
        self.assertEqual(out['securityToken'], 'ST')
        self.assertEqual(out['refreshToken'], 'RT')
        self.assertTrue(out['dpopJwk'])

    def test_normalize_credentials_accepts_legacy_ticket_shape(self) -> None:
        out = store.normalize_credentials({
            'cloud_dragon_token': 'ST', 'access_key_id': 'AK',
            'secret_access_key': 'SK'})
        self.assertEqual(out['securityToken'], 'ST')
        self.assertEqual(out['accessKeyId'], 'AK')


# ── 目录 ────────────────────────────────────────────────────
class CatalogTest(_DbCase):
    def setUp(self) -> None:
        super().setUp()
        catalog.reset_source_backoff()

    def test_merge_marks_benefit_and_keeps_richer_params(self) -> None:
        merged = client.merge_models(
            [{'id': 'GLM-5.2', 'context_length': 128000}],
            [{'id': 'glm-5.2', 'max_tokens': 4096, 'benefit': True}])
        self.assertEqual(len(merged), 1)
        entry = merged[0]
        self.assertTrue(entry['benefit'], '福利标记必须取或')
        self.assertEqual(entry['context_length'], 128000)
        self.assertEqual(entry['max_tokens'], 4096)

    def test_merge_is_sorted_by_lower_id(self) -> None:
        merged = client.merge_models(
            [{'id': 'qwen'}, {'id': 'GLM-5.2'}, {'id': 'deepseek'}])
        self.assertEqual([m['id'] for m in merged],
                         ['deepseek', 'GLM-5.2', 'qwen'])

    def test_canonical_model_normalizes_case_and_alias(self) -> None:
        known = {'glm-5.2': 'GLM-5.2', 'qwen3-vl-235b': 'Qwen3-VL-235B'}
        self.assertEqual(client.canonical_model('glm-5.2', known), 'GLM-5.2')
        self.assertEqual(client.canonical_model('GLM-5.2', known), 'GLM-5.2')
        self.assertEqual(client.canonical_model('snap-chat', {}), 'GLM-5.2')
        # 目录里没有、也不是别名：原样返回，交给上游判。
        self.assertEqual(client.canonical_model('X-Custom', {}), 'X-Custom')

    def test_benefit_falls_back_to_seed_when_catalog_missing(self) -> None:
        self.assertTrue(catalog.is_benefit('ca_1', 'deepseek-v4-flash-0731'))
        self.assertFalse(catalog.is_benefit('ca_1', 'GLM-5.2'))

    def test_benefit_prefers_account_catalog(self) -> None:
        catalog.cache_put('ca_1', [
            {'id': 'GLM-5.2', 'benefit': True},
            {'id': 'deepseek-v4-flash-0731', 'benefit': False}])
        self.assertTrue(catalog.is_benefit('ca_1', 'glm-5.2'))
        # 目录里明确标了「不是福利」：不能回退种子，否则会多带一次头。
        self.assertFalse(catalog.is_benefit('ca_1', 'deepseek-v4-flash-0731'))
        # 别的账号不受影响。
        self.assertTrue(catalog.is_benefit('ca_2', 'deepseek-v4-flash-0731'))

    def test_source_backoff_blocks_until_reset(self) -> None:
        self.assertFalse(catalog.src_blocked('ca_1', 'benefit'))
        catalog.note_src_fail('ca_1', 'benefit')
        self.assertTrue(catalog.src_blocked('ca_1', 'benefit'))
        self.assertFalse(catalog.src_blocked('ca_2', 'benefit'))
        catalog.clear_src_fail('ca_1', 'benefit')
        self.assertFalse(catalog.src_blocked('ca_1', 'benefit'))

    def test_discover_keeps_previous_benefit_when_benefit_source_fails(self) -> None:
        catalog.cache_put('ca_1', [
            {'id': 'glm-5.3-flash', 'benefit': True}])

        async def agent(cred, **kwargs):
            return [{'id': 'GLM-5.2', 'context_length': 128000}]

        async def builtin(cred, **kwargs):
            return [{'id': 'GLM-5.1'}]

        async def benefit(cred, **kwargs):
            raise PlatformError('福利网关挂了', status=502, code='upstream_error')

        with mock.patch.object(client, 'fetch_agent_models', new=agent), \
                mock.patch.object(client, 'fetch_builtin_models', new=builtin), \
                mock.patch.object(client, 'fetch_benefit_models', new=benefit):
            models = asyncio.run(catalog.discover('cred', 'ca_1'))
        by_id = {m['id'].lower(): m for m in models}
        self.assertIn('glm-5.2', by_id)
        # 福利来源本轮失败：上一轮标过福利的继续按福利路由（漏标 = 漏头 = 404）。
        self.assertTrue(by_id['glm-5.3-flash']['benefit'])

    def test_discover_raises_only_when_every_source_fails(self) -> None:
        async def fail(cred, **kwargs):
            raise PlatformError('挂了', status=502, code='upstream_error')

        with mock.patch.object(client, 'fetch_agent_models', new=fail), \
                mock.patch.object(client, 'fetch_builtin_models', new=fail), \
                mock.patch.object(client, 'fetch_benefit_models', new=fail):
            with self.assertRaises(PlatformError):
                asyncio.run(catalog.discover('cred', 'ca_1'))

    def test_seed_models_are_listed_before_first_discovery(self) -> None:
        models = catalog.seed_models()
        ids = {m['id'] for m in models}
        self.assertIn('GLM-5.2', ids)
        self.assertIn('deepseek-v4-flash-0731', ids)
        self.assertFalse(catalog.has_live_catalog())


# ── 对话协议 ────────────────────────────────────────────────
class ChatProtocolTest(unittest.TestCase):
    def test_build_chat_body_is_always_streaming(self) -> None:
        body = client.build_chat_body(
            [{'role': 'user', 'content': 'hi'}], 'GLM-5.2', 'a' * 32,
            options={'stream': False, 'max_tokens': 10})
        self.assertTrue(body['stream'])
        self.assertEqual(body['model'], 'GLM-5.2')
        self.assertEqual(body['chat_id'], 'a' * 32)
        self.assertEqual(body['prompt_cache_key'], 'a' * 32)
        self.assertTrue(body['tool_stream'])
        self.assertEqual(body['max_tokens'], 10)

    def test_chat_id_is_32_hex(self) -> None:
        chat_id = client.new_chat_id()
        self.assertEqual(len(chat_id), 32)
        int(chat_id, 16)

    def test_session_id_is_derived_from_chat_id(self) -> None:
        chat_id = 'b' * 32
        self.assertEqual(client.session_id_for(chat_id),
                         client.session_id_for(chat_id))
        self.assertEqual(len(client.session_id_for(chat_id)), 32)

    def test_chat_headers_put_benefit_before_signing(self) -> None:
        cred = client.Credential('AK', 'SK', 'TOK')
        headers = client._chat_headers(cred, b'{}', chat_id='c' * 32,
                                       session_id='d' * 32, benefit=True)
        auth = headers['Authorization']
        self.assertIn('maas_type', auth, 'maas_type 必须在签名前加上')
        # Chat-Id / Session-Id 在签名之后设置，不计入 SignedHeaders。
        self.assertNotIn('chat-id', auth.lower())
        self.assertNotIn('session-id', auth.lower())
        self.assertEqual(headers['Chat-Id'], 'c' * 32)
        self.assertEqual(headers['Session-Id'], 'd' * 32)

    def test_stream_translator_treats_text_as_cumulative(self) -> None:
        translator = client.StreamTranslator('codearts/GLM-5.2')
        out: list[dict] = []
        out += translator.feed('', {'text': '你'})
        out += translator.feed('', {'text': '你好'})
        out += translator.feed('', {'text': '你好，世界'})
        text = ''.join(chunk['choices'][0]['delta'].get('content') or ''
                       for chunk in out)
        # 累计全文是替换语义：拼回去必须正好一份，不能是「你你你好…」。
        self.assertEqual(text, '你好，世界')
        tail = translator.finish_chunks()
        self.assertEqual(tail[-1]['choices'][0]['finish_reason'], 'stop')

    def test_stream_translator_handles_delta_and_reasoning(self) -> None:
        translator = client.StreamTranslator('codearts/x')
        out: list[dict] = []
        out += translator.feed('reasoning', {'text': '想一下'})
        out += translator.feed('', {'delta': {'content': '答'}})
        out += translator.feed('', {'delta': {'content': '案'}})
        content = ''.join(c['choices'][0]['delta'].get('content') or ''
                          for c in out)
        reasoning = ''.join(c['choices'][0]['delta'].get('reasoning_content') or ''
                            for c in out)
        self.assertEqual(content, '答案')
        self.assertEqual(reasoning, '想一下')

    def test_done_marker_closes_stream(self) -> None:
        translator = client.StreamTranslator('codearts/x')
        translator.feed('', {'text': 'hi'})
        translator.feed('', {'text': '[DONE]'})
        self.assertEqual(translator.finish, 'stop')

    def test_embedded_error_raises(self) -> None:
        translator = client.StreamTranslator('codearts/x')
        with self.assertRaises(PlatformError):
            translator.feed('', {'error_code': 'tm.00001041',
                                 'error_msg': '并发会话超限'})

    def test_aggregate_returns_chat_completion(self) -> None:
        data = client.aggregate(
            [('', {'text': '你好'}),
             ('', {'text': '你好世界', 'usage': {'prompt_tokens': 3,
                                                'completion_tokens': 4}})],
            'codearts/GLM-5.2')
        self.assertEqual(data['object'], 'chat.completion')
        self.assertEqual(data['choices'][0]['message']['content'], '你好世界')
        self.assertEqual(data['usage']['total_tokens'], 7)

    def test_classify_maps_rate_limit_and_model_errors(self) -> None:
        rate = client.classify(200, {'error_code': 'tm.00001041',
                                     'error_msg': '并发会话超限'})
        self.assertIsNotNone(rate)
        self.assertEqual(rate.status, 429)
        model = client.classify(200, {'error_code': 'InferHub.002002009.404',
                                      'error_msg': 'model is not registered'})
        self.assertIsNotNone(model)
        self.assertEqual(model.status, 400)
        self.assertIsNone(client.classify(200, {'text': 'ok'}))

    def test_normalize_token_response_reads_credentials(self) -> None:
        out = client.normalize_token_response({
            'user_id': 'u-1', 'user_name': 'alice', 'domain_id': 'd-1',
            'refresh_token': 'RT',
            'credentials': {'access_key_id': 'AK', 'secret_access_key': 'SK',
                            'security_token': 'ST',
                            'expiration': '2030-01-01T00:00:00Z'}})
        self.assertEqual(out['security_token'], 'ST')
        self.assertEqual(out['refresh_token'], 'RT')
        self.assertGreater(out['expires_at'], 0)

    def test_normalize_token_response_reads_legacy_ticket(self) -> None:
        out = client.normalize_token_response({
            'credential': {'access': 'AK', 'secret': 'SK',
                           'securitytoken': 'ST',
                           'expires_at': '2030-01-01T00:00:00Z'}})
        self.assertEqual(out['access_key_id'], 'AK')
        self.assertEqual(out['security_token'], 'ST')

    def test_normalize_token_response_requires_security_token(self) -> None:
        with self.assertRaises(PlatformError):
            client.normalize_token_response({'user_id': 'u-1'})


# ── 适配器 ──────────────────────────────────────────────────
class CodeArtsAdapterTest(_DbCase):
    def setUp(self) -> None:
        super().setUp()
        catalog.reset_source_backoff()
        self.adapter = CodeArtsAdapter()

    def _seed(self, uid: str = 'ca_1', **overrides) -> dict:
        jwk = overrides.pop('dpopJwk', None) or dpop.generate_jwk()
        data = {
            'uid': uid,
            'userName': overrides.pop('userName', 'alice'),
            'accessKeyId': 'AK-' + uid,
            'secretAccessKey': 'SK-' + uid,
            'securityToken': 'ST-' + uid,
            'expiredAt': store.iso_from_epoch(time.time() + 3600),
            'refreshToken': 'RT-' + uid,
            'codeVerifier': 'verifier-' + uid,
            'clientId': C.CLIENT_ID,
            'dpopJwk': jwk,
            'loginState': '正常',
        }
        data.update(overrides)
        store.save_account(data)
        return data

    # ── 导入 / 导出 ───────────────────────────────────────
    def test_import_account_keeps_dpop_key_and_derives_uid(self) -> None:
        jwk = dpop.generate_jwk()
        public = self.adapter.import_account({
            'user_id': 'u-77', 'user_name': 'bob',
            'refresh_token': 'RT-77', 'code_verifier': 'v-77',
            'credentials': {'access_key_id': 'AK', 'secret_access_key': 'SK',
                            'security_token': 'ST',
                            'expiration': '2030-01-01T00:00:00Z'},
            'dpop_private_key': jwk})
        raw = store.read_account(public['uid'])
        self.assertEqual(raw['refreshToken'], 'RT-77')
        self.assertEqual(raw['codeVerifier'], 'v-77')
        self.assertEqual(raw['clientId'], C.CLIENT_ID)
        self.assertEqual(dpop.normalize_jwk(raw['dpopJwk']), dpop.normalize_jwk(jwk))
        self.assertEqual(raw['loginState'], '正常')

    def test_import_account_requires_some_credential(self) -> None:
        with self.assertRaises(PlatformError):
            self.adapter.import_account({'name': 'nothing'})

    def test_import_account_rejects_broken_dpop_key(self) -> None:
        with self.assertRaises(PlatformError) as ctx:
            self.adapter.import_account({
                'user_id': 'u-1', 'refresh_token': 'RT',
                'dpop_private_key': {'kty': 'EC', 'crv': 'P-256',
                                     'x': 'AA', 'y': 'AA', 'd': 'AA'}})
        self.assertEqual(ctx.exception.status, 400)

    def test_export_accounts_includes_secrets(self) -> None:
        self._seed()
        exported = self.adapter.export_accounts()
        self.assertEqual(exported[0]['secretAccessKey'], 'SK-ca_1')

    # ── 令牌轮换 ──────────────────────────────────────────
    def test_refresh_persists_new_tokens_before_use(self) -> None:
        self._seed(expiredAt=store.iso_from_epoch(time.time() - 10))
        seen: dict = {}

        async def fake_refresh(refresh_token, verifier, jwk, **kwargs):
            # 一次性 refresh token：请求发出前旧值必须还在文件里（还没轮换），
            # 但**返回值**必须已经落盘后才能被使用。
            seen['refresh'] = refresh_token
            seen['verifier'] = verifier
            return {'access_key_id': 'AK-NEW', 'secret_access_key': 'SK-NEW',
                    'security_token': 'ST-NEW',
                    'refresh_token': 'RT-NEW',
                    'expires_at': time.time() + 3600,
                    'expiration': store.iso_from_epoch(time.time() + 3600),
                    'user_id': 'u-1', 'user_name': 'alice', 'domain_id': 'd-1'}

        with mock.patch.object(client, 'refresh', new=fake_refresh):
            cred = asyncio.run(self.adapter._token_for(
                store.read_account('ca_1'), proxy=None))

        self.assertEqual(cred.security_token, 'ST-NEW')
        self.assertEqual(seen['refresh'], 'RT-ca_1')
        self.assertEqual(seen['verifier'], 'verifier-ca_1')
        raw = store.read_account('ca_1')
        # 刷新成功必须**先落盘再使用**：文件里已经是新的一组。
        self.assertEqual(raw['refreshToken'], 'RT-NEW')
        self.assertEqual(raw['securityToken'], 'ST-NEW')
        self.assertEqual(raw['accessKeyId'], 'AK-NEW')

    def test_refresh_failure_marks_login_expired(self) -> None:
        self._seed(expiredAt=store.iso_from_epoch(time.time() - 10))

        async def fake_refresh(*args, **kwargs):
            raise PlatformError('invalid_grant', status=401,
                                code='login_expired')

        with mock.patch.object(client, 'refresh', new=fake_refresh):
            with self.assertRaises(PlatformError):
                asyncio.run(self.adapter._token_for(
                    store.read_account('ca_1'), proxy=None))
        self.assertEqual(store.read_account('ca_1')['loginState'], '登录已失效')

    def test_missing_dpop_key_fails_without_spending_refresh_token(self) -> None:
        self._seed(expiredAt=store.iso_from_epoch(time.time() - 10),
                   dpopJwk={'kty': 'EC', 'crv': 'P-256', 'x': 'AA',
                            'y': 'AA', 'd': 'AA'})
        called = []

        async def fake_refresh(*args, **kwargs):
            called.append(1)
            return {}

        with mock.patch.object(client, 'refresh', new=fake_refresh):
            with self.assertRaises(PlatformError) as ctx:
                asyncio.run(self.adapter._token_for(
                    store.read_account('ca_1'), proxy=None))
        self.assertEqual(ctx.exception.status, 401)
        self.assertFalse(called, '私钥不可用时不能拿 refresh token 去试')

    def test_credential_without_refresh_token_requires_relogin(self) -> None:
        self._seed(refreshToken='', expiredAt=store.iso_from_epoch(
            time.time() - 10))
        with self.assertRaises(PlatformError) as ctx:
            asyncio.run(self.adapter._token_for(
                store.read_account('ca_1'), proxy=None))
        self.assertEqual(ctx.exception.status, 401)
        self.assertEqual(ctx.exception.code, 'login_expired')

    # ── 模型目录 ──────────────────────────────────────────
    def test_list_models_falls_back_to_seed(self) -> None:
        models = asyncio.run(self.adapter.list_models())
        ids = {m['id'] for m in models}
        self.assertIn('codearts/GLM-5.2', ids)
        self.assertTrue(all(m['realm'] == 'cn' for m in models))
        free = [m for m in models if m['id'] == 'codearts/glm-5.3-flash'][0]
        self.assertTrue(free['free'])
        self.assertEqual(free['badge'], '限免')

    def test_list_models_saves_per_account_catalog(self) -> None:
        self._seed()
        live = [{'id': 'GLM-5.2', 'name': 'GLM 5.2', 'context_length': 128000,
                 'max_tokens': 4096, 'benefit': False}]

        async def fake_discover(cred, uid, **kwargs):
            return live

        with mock.patch.object(catalog, 'discover', new=fake_discover), \
                mock.patch.object(self.adapter, '_token_for',
                                  new=mock.AsyncMock(
                                      return_value=client.Credential('AK', 'SK',
                                                                     'ST'))):
            models = asyncio.run(self.adapter.list_models(refresh=True))
        self.assertEqual(models[0]['id'], 'codearts/GLM-5.2')
        self.assertEqual(models[0]['context_window'], 128000)
        self.assertEqual(store.list_accounts()[0]['uid'], 'ca_1')
        self.assertTrue(catalog.cache_get('ca_1'))

    def test_unknown_model_is_rejected_only_with_live_catalog(self) -> None:
        self._seed()
        # 冷启动只有种子：这时不能拿种子去判「未知」，否则会误拒合法模型。
        self.adapter._assert_known_model('some-new-model')
        catalog.cache_put('ca_1', [{'id': 'GLM-5.2'}])
        with self.assertRaises(PlatformError) as ctx:
            self.adapter._assert_known_model('some-new-model')
        self.assertEqual(ctx.exception.code, 'model_not_found')
        self.adapter._assert_known_model('glm-5.2')

    def test_upstream_model_normalizes_case_and_alias(self) -> None:
        self.assertEqual(self.adapter.upstream_model('default'), C.DEFAULT_MODEL)
        self.assertEqual(self.adapter.upstream_model(''), C.DEFAULT_MODEL)
        self.assertEqual(self.adapter.upstream_model('glm-5.2'), 'GLM-5.2')
        self.assertEqual(self.adapter.upstream_model('snap-chat'), 'GLM-5.2')

    # ── 对话 ──────────────────────────────────────────────
    def _fake_stream(self, lines: list[str]):
        class FakeResponse:
            def __init__(self) -> None:
                self.closed = False

            async def aclose(self) -> None:
                self.closed = True

        response = FakeResponse()

        async def opener():
            async def gen():
                for line in lines:
                    yield line
            return client.ChatStream(response, gen(), [])

        return response, opener

    def test_stream_relay_rewrites_cumulative_text(self) -> None:
        self._seed()
        response, opener = self._fake_stream([
            'data: {"text": "你"}',
            'data: {"text": "你好"}',
            'data: {"text": "[DONE]"}',
        ])
        seen: dict = {}

        async def fake_open(cred, payload, **kwargs):
            seen['payload'] = payload
            seen['benefit'] = kwargs.get('benefit')
            return await opener()

        async def collect() -> list[dict]:
            with mock.patch.object(client, 'open_chat', new=fake_open), \
                    mock.patch.object(self.adapter, '_token_for',
                                      new=mock.AsyncMock(
                                          return_value=client.Credential(
                                              'AK', 'SK', 'ST'))):
                chat = await self.adapter.chat(
                    'glm-5.2', {'messages': [{'role': 'user', 'content': 'hi'}],
                                'stream': True})
                out = [chunk async for chunk in chat.stream]
                await chat.aclose()
                return out

        chunks = asyncio.run(collect())
        text = ''.join(c['choices'][0]['delta'].get('content') or ''
                       for c in chunks)
        self.assertEqual(text, '你好')
        self.assertEqual(chunks[-1]['choices'][0]['finish_reason'], 'stop')
        self.assertEqual(seen['payload']['model'], 'GLM-5.2')
        self.assertFalse(seen['benefit'])
        self.assertTrue(response.closed)

    def test_benefit_model_sends_maas_type(self) -> None:
        self._seed()
        _, opener = self._fake_stream(['data: {"text": "ok"}',
                                       'data: {"text": "[DONE]"}'])
        seen: dict = {}

        async def fake_open(cred, payload, **kwargs):
            seen['benefit'] = kwargs.get('benefit')
            return await opener()

        async def run() -> None:
            with mock.patch.object(client, 'open_chat', new=fake_open), \
                    mock.patch.object(self.adapter, '_token_for',
                                      new=mock.AsyncMock(
                                          return_value=client.Credential(
                                              'AK', 'SK', 'ST'))):
                chat = await self.adapter.chat(
                    'glm-5.3-flash',
                    {'messages': [{'role': 'user', 'content': 'hi'}],
                     'stream': True})
                [chunk async for chunk in chat.stream]
                await chat.aclose()

        asyncio.run(run())
        self.assertTrue(seen['benefit'])

    def test_non_stream_request_is_aggregated_locally(self) -> None:
        self._seed()
        _, opener = self._fake_stream([
            'data: {"text": "你"}',
            'data: {"text": "你好", "usage": {"prompt_tokens": 2,'
            ' "completion_tokens": 3}}',
            'data: {"text": "[DONE]"}',
        ])
        seen: dict = {}

        async def fake_open(cred, payload, **kwargs):
            seen['payload'] = payload
            return await opener()

        async def run():
            with mock.patch.object(client, 'open_chat', new=fake_open), \
                    mock.patch.object(self.adapter, '_token_for',
                                      new=mock.AsyncMock(
                                          return_value=client.Credential(
                                              'AK', 'SK', 'ST'))):
                return await self.adapter.chat(
                    'GLM-5.2', {'messages': [{'role': 'user', 'content': 'hi'}],
                                'stream': False})

        chat = asyncio.run(run())
        # 上游永远走 SSE；非流式在本地聚合，且不能把 stream=False 发给上游。
        self.assertTrue(seen['payload']['stream'])
        self.assertIsNotNone(chat.payload)
        self.assertEqual(chat.payload['choices'][0]['message']['content'],
                         '你好')
        self.assertEqual(chat.payload['usage']['total_tokens'], 5)

    def test_model_spelling_error_retries_once_and_is_remembered(self) -> None:
        self._seed()
        attempts: list[str] = []
        _, opener = self._fake_stream(['data: {"text": "ok"}',
                                       'data: {"text": "[DONE]"}'])

        async def fake_open(cred, payload, **kwargs):
            attempts.append(payload['model'])
            if len(attempts) == 1:
                raise PlatformError('tm.000010051 无权调用该模型', status=400,
                                    code='upstream_error')
            return await opener()

        async def run() -> None:
            with mock.patch.object(client, 'open_chat', new=fake_open), \
                    mock.patch.object(self.adapter, '_token_for',
                                      new=mock.AsyncMock(
                                          return_value=client.Credential(
                                              'AK', 'SK', 'ST'))):
                chat = await self.adapter.chat(
                    'glm-5.2',
                    {'messages': [{'role': 'user', 'content': 'hi'}],
                     'stream': True})
                [chunk async for chunk in chat.stream]
                await chat.aclose()

        asyncio.run(run())
        self.assertEqual(len(attempts), 2)
        self.assertNotEqual(attempts[0], attempts[1])
        # 成功的拼写被记住：下一次直接用它。
        self.assertEqual(
            self.adapter._model_ids[('glm-5.2', False)], attempts[1])

    def test_no_account_raises_no_account(self) -> None:
        with self.assertRaises(PlatformError) as ctx:
            asyncio.run(self.adapter.chat(
                'GLM-5.2', {'messages': [{'role': 'user', 'content': 'hi'}]}))
        self.assertEqual(ctx.exception.status, 503)
        self.assertEqual(ctx.exception.code, 'no_account')

    def test_expired_account_is_retried_once_with_fresh_token(self) -> None:
        self._seed()
        tokens: list[bool] = []
        _, opener = self._fake_stream(['data: {"text": "ok"}',
                                       'data: {"text": "[DONE]"}'])

        async def fake_token(account, *, proxy=None, force=False):
            tokens.append(force)
            if not force:
                raise PlatformError('ExpiredToken', status=401,
                                    code='auth_expired')
            return client.Credential('AK', 'SK', 'ST')

        async def fake_open(cred, payload, **kwargs):
            return await opener()

        async def run() -> None:
            with mock.patch.object(client, 'open_chat', new=fake_open), \
                    mock.patch.object(self.adapter, '_token_for',
                                      new=fake_token):
                chat = await self.adapter.chat(
                    'GLM-5.2',
                    {'messages': [{'role': 'user', 'content': 'hi'}],
                     'stream': True})
                [chunk async for chunk in chat.stream]
                await chat.aclose()

        asyncio.run(run())
        self.assertEqual(tokens, [False, True])

    def test_checkin_serializes_and_records_reward(self) -> None:
        self._seed()

        async def fake_checkin(cred, **kwargs):
            return {'ok': True, 'credits': 30.0, 'message': '领取成功 +30 积分',
                    'campaigns': [{'campaign_id': 1, 'ok': True,
                                   'credits': 30.0}]}

        with mock.patch.object(client, 'checkin', new=fake_checkin), \
                mock.patch.object(self.adapter, '_token_for',
                                  new=mock.AsyncMock(
                                      return_value=client.Credential(
                                          'AK', 'SK', 'ST'))):
            out = asyncio.run(self.adapter.checkin('ca_1'))
        self.assertTrue(out['ok'])
        self.assertEqual(out['earned_credit'], 30.0)
        self.assertEqual(store.read_account('ca_1')['creditsAwarded'], 30.0)

    def test_credits_reports_checkin_total_not_a_fake_balance(self) -> None:
        self._seed(creditsAwarded=120.0)
        out = asyncio.run(self.adapter.credits('ca_1'))
        self.assertEqual(out['credits'], 120.0)
        self.assertIsNone(out['remain'])
        self.assertEqual(out['source'], 'checkin')

    def test_credits_without_checkin_is_an_honest_error(self) -> None:
        self._seed()
        with self.assertRaises(PlatformError) as ctx:
            asyncio.run(self.adapter.credits('ca_1'))
        self.assertEqual(ctx.exception.code, 'credits_unsupported')

    def test_candidates_skip_disabled_checkin_only_and_cooling(self) -> None:
        self._seed('ca_1')
        self._seed('ca_2', disabled=True)
        self._seed('ca_3', checkinOnly=True)
        self._seed('ca_4', coolingUntil=time.time() + 600)
        uids = [a['uid'] for a in self.adapter._candidates()]
        self.assertEqual(uids, ['ca_1'])


# ── 登录 ────────────────────────────────────────────────────
class LoginTest(_DbCase):
    def tearDown(self) -> None:
        for login_id in list(login._sessions):
            login.cancel(login_id)
        super().tearDown()

    def test_authorize_url_uses_sha256_method_name(self) -> None:
        url = login.build_authorize_url('tid', 'chal', 12345)
        self.assertIn('code_challenge_method=SHA-256', url)
        self.assertIn('client_id=codearts-agent', url)
        self.assertIn('plugin-name=snap_AIIDE', url)
        self.assertIn('port=12345', url)
        self.assertIn('ticket_id=tid', url)

    def test_parse_callback_rejects_foreign_host(self) -> None:
        with self.assertRaises(PlatformError) as ctx:
            login.parse_callback_url('https://evil.example/oauth/callback?code=x')
        self.assertEqual(ctx.exception.code, 'callback_host_invalid')
        params = login.parse_callback_url(
            'http://127.0.0.1:5000/oauth/callback?code=abc&secret=s1')
        self.assertEqual(params['code'], 'abc')
        self.assertEqual(params['secret'], 's1')
        # 纯 query 串也认（用户可能只复制了 `?code=...`）。
        self.assertEqual(login.parse_callback_url('code=abc')['code'], 'abc')

    def test_parse_callback_requires_code_or_params(self) -> None:
        for bad in ('', 'https://127.0.0.1:1/oauth/callback'):
            with self.subTest(bad=bad):
                with self.assertRaises(PlatformError):
                    login.parse_callback_url(bad)

    def test_candidates_prefer_secret_then_ticket_then_newest(self) -> None:
        now = time.time()
        with login._lock:
            login._sessions.clear()
            login._sessions['old'] = {
                'id': 'old', 'ticketId': 't-old', 'secret': 's-old',
                'state': 'pending', 'created': now - 10, 'updated': now - 10}
            login._sessions['new'] = {
                'id': 'new', 'ticketId': 't-new', 'secret': 's-new',
                'state': 'pending', 'created': now, 'updated': now}

        self.assertEqual([s['id'] for s in login.candidates(secret='s-old')],
                         ['old'])
        self.assertEqual([s['id'] for s in login.candidates(ticket_id='t-old')],
                         ['old'])
        # 只带 code（实测第二次回调就是这样）：按创建时间倒序，新的先试。
        self.assertEqual([s['id'] for s in login.candidates()], ['new', 'old'])
        # 带了 secret 却不命中：会话已失效，**不再**往下猜。
        self.assertEqual(login.candidates(secret='nope'), [])

    def test_accept_code_tries_candidates_until_one_matches(self) -> None:
        now = time.time()
        with login._lock:
            login._sessions.clear()
            for name in ('a', 'b'):
                login._sessions[name] = {
                    'id': name, 'ticketId': 't-' + name,
                    'secret': 's-' + name, 'state': 'pending',
                    'created': now, 'updated': now, 'verifier': 'v-' + name,
                    'dpopJwk': dpop.generate_jwk(), 'port': 1,
                    'server': None}
        tried: list[str] = []

        def fake_exchange(session, code):
            tried.append(session['id'])
            if session['id'] == 'b':
                return {'uid': 'ca_b'}
            raise PlatformError('STS5.1805 verifier 不匹配', status=400,
                                code='bad_request')

        with mock.patch.object(login, '_exchange_code', new=fake_exchange):
            out = login._accept_code('the-code')
        self.assertEqual(out['status'], 'success')
        self.assertEqual(out['account']['uid'], 'ca_b')
        # 授权码与 verifier 一对一：用错只是不匹配，可以继续试下一个。
        self.assertEqual(tried, ['a', 'b'])

    def test_accept_code_without_session_raises(self) -> None:
        with login._lock:
            login._sessions.clear()
        with self.assertRaises(PlatformError) as ctx:
            login._accept_code('code', secret='gone')
        self.assertEqual(ctx.exception.code, 'login_not_found')

    def test_start_returns_loopback_url(self) -> None:
        out = login.start()
        self.addCleanup(login.cancel, out['login_id'])
        # 授权页本身在 codearts.huaweicloud.com；本机回环地址只出现在
        # `port` 参数与 `redirect_uri` 里（用户浏览器回调到它）。
        session = login._sessions[out['login_id']]
        self.assertGreater(int(session['port']), 0)
        self.assertIn(f"port={session['port']}", out['url'])
        self.assertEqual(out['expires_in'], login.LOGIN_TTL)
        self.assertIn('code_challenge_method=SHA-256', out['url'])
        status = login.poll(out['login_id'])
        self.assertEqual(status['status'], 'pending')
        self.assertEqual(status['realm'], 'cn')

    def test_submit_callback_without_code_explains_first_hop(self) -> None:
        out = login.start()
        self.addCleanup(login.cancel, out['login_id'])
        with self.assertRaises(PlatformError) as ctx:
            login.submit_callback(
                out['login_id'],
                'http://127.0.0.1:1/oauth/callback?secret=abc&redirect=x')
        self.assertEqual(ctx.exception.code, 'login_no_credential')


class CapabilityTest(unittest.TestCase):
    def test_runtime_settings_group(self) -> None:
        caps = runtime_settings.capabilities('codearts')
        self.assertTrue(caps['max_accounts_try'])
        self.assertTrue(caps['session_affinity'])
        self.assertTrue(caps['catalog_timeout'])
        self.assertTrue(caps['credits_ttl'])
        self.assertTrue(caps['cooldown_429'])
        self.assertFalse(caps['min_credits'])
        self.assertFalse(caps['credit_meter'])
        self.assertFalse(caps['desktop_sync'])
        self.assertFalse(caps['usage_ledger'])

    def test_adapter_declares_capabilities(self) -> None:
        adapter = CodeArtsAdapter()
        self.assertTrue(adapter.supports_checkin)
        self.assertTrue(adapter.supports_web_login)
        self.assertFalse(adapter.supports_cookie_import)
        self.assertFalse(adapter.realm_aware)
        self.assertEqual(adapter.model_prefix, 'codearts/')
        self.assertTrue(hasattr(adapter, 'login_submit_callback'))


if __name__ == '__main__':
    unittest.main()
