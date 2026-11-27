"""AutoClaw 协议层：区域、地址、签名、目录归一、错误分级与 JWT。

全部离线。当前只钉 `docs/autoclaw.md` 阶段 2 的纯函数；账号存储 / 登录 /
对话的适配器测试等阶段 3+ 落地后追加到本文件。

重点不是「抄参考实现的常量」，而是钉住三条容易回归的边界：

  · 国内账号面与推理面是两个域名，且两边都保留回退——不能按「一个区域
    只有一个域名」写死；
  · 国内 2.x 的 chat 路径与 `/v1` 的关系尚未实测，`chat_paths()` 必须同时
    保留两种写法；
  · `810000` 是**单模型额度耗尽**，只能冷却「账号 + 模型」，不能冻整号；
    `403` 是风控 / 指纹错误，也不能当会话失效。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from server import config, db
from server.platforms.base import PlatformError
from server.platforms.autoclaw import (
    catalog, client, constants as C, protocol as P, store)
from server.platforms.autoclaw.adapter import AutoclawAdapter
from server.platforms import settings as runtime_settings


def _jwt(claims: dict) -> str:
    header = base64.urlsafe_b64encode(
        json.dumps({'alg': 'none'}).encode()).rstrip(b'=').decode()
    body = base64.urlsafe_b64encode(
        json.dumps(claims).encode()).rstrip(b'=').decode()
    return f'{header}.{body}.sig'


def _exp(seconds: int = 3600) -> int:
    return int(time.time()) + seconds


class AutoclawRealmTest(unittest.TestCase):
    def test_normalize_realm_aliases(self) -> None:
        for value in ('cn', 'CN', 'china', 'domestic', '国内'):
            self.assertEqual(P.normalize_realm(value), 'cn', value)
        for value in ('global', 'intl', 'international', 'com', '海外', '国际'):
            self.assertEqual(P.normalize_realm(value), 'global', value)
        self.assertEqual(P.normalize_realm(''), C.DEFAULT_REALM)
        self.assertEqual(P.normalize_realm('nope'), C.DEFAULT_REALM)

    def test_cn_has_two_domains_with_fallbacks(self) -> None:
        self.assertEqual(P.account_hosts('cn')[0],
                         'https://autoglm-api.zhipuai.cn')
        self.assertEqual(P.inference_hosts('cn')[0],
                         'https://autoglm-acceleration-api.zhipuai.cn')
        self.assertIn('https://autoglm-acceleration-api.zhipuai.cn',
                      P.account_hosts('cn'))
        self.assertIn('https://autoglm-api.zhipuai.cn',
                      P.inference_hosts('cn'))

    def test_global_uses_one_domain(self) -> None:
        self.assertEqual(P.account_hosts('global'),
                         ('https://autoglm-api.autoglm.ai',))
        self.assertEqual(P.inference_hosts('global'),
                         ('https://autoglm-api.autoglm.ai',))
        self.assertEqual(P.catalog_hosts('global'),
                         ('https://autoglm-api.autoglm.ai',))

    def test_catalog_hosts_tries_inference_before_account(self) -> None:
        hosts = P.catalog_hosts('cn')
        self.assertEqual(hosts[0], 'https://autoglm-acceleration-api.zhipuai.cn')
        self.assertEqual(len(hosts), 2)

    def test_site_has_version_matrix(self) -> None:
        self.assertEqual(C.SITES['cn']['contract'], 'v2')
        self.assertEqual(C.SITES['global']['contract'], 'v1')
        self.assertEqual(C.SITES['cn']['channel'], 'AutoClaw26')
        self.assertEqual(C.SITES['global']['channel'], 'AutoClaw4')


class AutoclawUrlTest(unittest.TestCase):
    def test_join_normalizes_slashes(self) -> None:
        self.assertEqual(P.join('https://a/', '/x'), 'https://a/x')
        self.assertEqual(P.join('https://a', 'x'), 'https://a/x')
        self.assertEqual(P.join('https://a///', '///x'), 'https://a/x')
        self.assertEqual(P.join('', '/x'), '')
        self.assertEqual(P.join('https://a', ''), 'https://a')

    def test_chat_paths_keep_both_cn_forms(self) -> None:
        cn = P.chat_paths('cn')
        self.assertEqual(cn[0], C.CHAT_PATH_NO_V1)
        self.assertIn(C.CHAT_PATH, cn)
        self.assertEqual(P.chat_paths('global'), (C.CHAT_PATH,))

    def test_chat_url_uses_region_preference(self) -> None:
        self.assertTrue(P.chat_url('https://cn.example', 'cn')
                        .endswith(C.CHAT_PATH_NO_V1))
        self.assertTrue(P.chat_url('https://global.example', 'global')
                        .endswith(C.CHAT_PATH))

    def test_model_config_url(self) -> None:
        self.assertEqual(
            P.model_config_url('https://a/'),
            'https://a/autoclaw-proxy/proxy/autoclaw-model-config')

    def test_oauth_callback_rejects_unknown_vendor(self) -> None:
        self.assertEqual(
            P.oauth_callback_url(18432, 'zai'),
            'http://localhost:18432/auth/callback-zai')
        with self.assertRaises(ValueError):
            P.oauth_callback_url(18432, 'github')


class AutoclawSignatureTest(unittest.TestCase):
    TS = 1700000000

    def test_sign_is_md5_of_appid_ts_secret(self) -> None:
        expected = hashlib.md5(
            f'{C.APP_ID}&{self.TS}&{C.APP_SECRET}'.encode()).hexdigest()
        self.assertEqual(P.sign(self.TS), expected)

    def test_headers_are_region_aware(self) -> None:
        headers = P.signed_headers(realm='cn', timestamp=self.TS,
                                   trace_id='0123456789abcdef')
        self.assertEqual(headers['X-Auth-Appid'], C.APP_ID)
        self.assertEqual(headers['X-Auth-TimeStamp'], str(self.TS))
        self.assertEqual(headers['X-Auth-Sign'], P.sign(self.TS))
        self.assertEqual(headers['X-Version'], '2.1.0')
        self.assertEqual(headers['X-Channel'], 'AutoClaw26')
        self.assertEqual(headers['X-Product'], 'autoclaw')
        self.assertEqual(headers['X-Client-Type'], 'pc')
        self.assertEqual(headers['X-Tm'], 'win')
        self.assertEqual(headers['X-Trace-Id'], '0123456789abcdef')
        self.assertEqual(headers['User-Agent'], 'AutoClaw/2.1.0')

        global_headers = P.signed_headers(
            realm='global', timestamp=self.TS, trace_id='0123456789abcdef')
        self.assertEqual(global_headers['X-Version'], '1.18.1')
        self.assertEqual(global_headers['X-Channel'], 'AutoClaw4')

    def test_headers_allow_overrides(self) -> None:
        headers = P.signed_headers(
            realm='cn', timestamp=self.TS, version='1.18.5',
            channel='official', platform='linux-x64', lang='en-US',
            trace_id='abc')
        self.assertEqual(headers['X-Version'], '1.18.5')
        self.assertEqual(headers['X-Channel'], 'official')
        self.assertEqual(headers['X-Tm'], 'linux')
        self.assertEqual(headers['X-Lang'], 'en-US')
        self.assertEqual(headers['User-Agent'], 'AutoClaw/1.18.5')

    def test_trace_id_defaults_to_16_hex(self) -> None:
        trace = P.signed_headers(timestamp=self.TS)['X-Trace-Id']
        self.assertEqual(len(trace), 16)
        int(trace, 16)

    def test_client_tm_aliases(self) -> None:
        self.assertEqual(P.client_tm('darwin'), 'mac')
        self.assertEqual(P.client_tm('macOS'), 'mac')
        self.assertEqual(P.client_tm('win32'), 'win')
        self.assertEqual(P.client_tm('linux-x64'), 'linux')
        self.assertEqual(P.client_tm('freebsd'), C.DEFAULT_TM)

    def test_bearer_headers_can_send_one_or_both(self) -> None:
        both = P.bearer_headers(' Bearer abc ')
        self.assertEqual(both['Authorization'], 'Bearer abc')
        self.assertEqual(both['X-Authorization'], 'Bearer abc')
        only_x = P.bearer_headers('abc', standard=False)
        self.assertNotIn('Authorization', only_x)
        self.assertEqual(only_x['X-Authorization'], 'Bearer abc')
        self.assertEqual(P.bearer_headers(''), {})


class AutoclawJwtTest(unittest.TestCase):
    def test_claims_expiry_and_realm_hint(self) -> None:
        token = _jwt({'exp': 1700003600, 'source_id': 'agent-cn-1',
                      'device_id': 'dev-1'})
        self.assertEqual(P.jwt_claims(token)['device_id'], 'dev-1')
        self.assertEqual(P.token_expiry(token), 1700003600)
        self.assertEqual(P.realm_hint(token), 'cn')

    def test_bearer_prefix_is_accepted(self) -> None:
        token = _jwt({'exp': 1, 'source_id': 'agent'})
        self.assertEqual(P.token_expiry(f'Bearer {token}'), 1)

    def test_unknown_realm_is_none(self) -> None:
        self.assertIsNone(P.realm_hint(_jwt({'source_id': 'web'})))
        self.assertIsNone(P.realm_hint('not-a-jwt'))

    def test_malformed_tokens_return_empty(self) -> None:
        for token in ('', 'a.b.c', 'a.!!!.c', 'a.e30.c', 'only-one-part'):
            self.assertEqual(P.jwt_claims(token), {}, token)
            self.assertEqual(P.token_expiry(token), 0, token)
            self.assertIsNone(P.realm_hint(token), token)

    def test_non_numeric_exp_is_zero(self) -> None:
        self.assertEqual(P.token_expiry(_jwt({'exp': 'later'})), 0)
        self.assertEqual(P.token_expiry(_jwt({'exp': True})), 0)


class AutoclawCatalogTest(unittest.TestCase):
    def test_parse_top_level_models(self) -> None:
        payload = {'models': [{
            'id': 'zai_auto',
            'name': 'Auto',
            'tooltip': '自动选择',
            'contextWindow': 200000,
            'maxTokens': 32000,
            'reasoning': True,
            'input': ['text', 'image'],
            'creditConsumptionLevel': 2,
        }]}
        models = catalog.parse(payload, realm='cn')
        self.assertEqual(len(models), 1)
        item = models[0]
        self.assertEqual(item['id'], 'zai_auto')
        self.assertEqual(item['name'], 'Auto')
        self.assertEqual(item['tooltip'], '自动选择')
        self.assertEqual(item['context_length'], 200000)
        self.assertEqual(item['max_tokens'], 32000)
        self.assertEqual(item['modalities'], ['text', 'image'])
        self.assertTrue(item['reasoning'])
        self.assertTrue(item['attachment'])
        self.assertEqual(item['credit_level'], 2)
        self.assertEqual(item['realm'], 'cn')

    def test_parse_nested_data_models(self) -> None:
        payload = {'data': {'models': [{'id': 'glm-5.3'}]}}
        self.assertEqual([m['id'] for m in catalog.parse(payload)],
                         ['glm-5.3'])

    def test_parse_data_as_list(self) -> None:
        payload = {'data': [{'id': 'a'}, {'id': 'b'}]}
        self.assertEqual([m['id'] for m in catalog.parse(payload)],
                         ['a', 'b'])

    def test_parse_wrong_shape_returns_empty(self) -> None:
        for payload in ({}, {'data': 'x'}, {'models': 'x'}, None, 1):
            self.assertEqual(catalog.parse(payload), [], payload)

    def test_bad_entries_and_duplicates_are_dropped(self) -> None:
        models = catalog.normalize([
            {'id': 'a'},
            {'id': ''},
            'not-a-dict',
            {'id': 'A'},
            {'id': 'b'},
        ])
        self.assertEqual([m['id'] for m in models], ['a', 'b'])

    def test_snake_case_and_alternate_fields(self) -> None:
        models = catalog.normalize([{
            'id': 'x',
            'context_window': '128000',
            'max_output_tokens': 8192,
            'thinking': {'supported': True},
            'modalities': {'input': ['text', 'vision']},
            'tool_call': False,
            'attachment': False,
            'reasoning_efforts': ['low', 'high'],
        }])
        item = models[0]
        self.assertEqual(item['context_length'], 128000)
        self.assertEqual(item['max_tokens'], 8192)
        self.assertTrue(item['reasoning'])
        self.assertEqual(item['modalities'], ['text', 'image'])
        self.assertFalse(item['tool_call'])
        self.assertTrue(item['attachment'])
        self.assertEqual(item['efforts'], ['low', 'high'])

    def test_reasoning_defaults_to_false(self) -> None:
        self.assertFalse(catalog.normalize([{'id': 'x'}])[0]['reasoning'])

    def test_no_offline_snapshot(self) -> None:
        self.assertEqual(catalog.default_models(), [])


class AutoclawErrorTest(unittest.TestCase):
    def test_business_session_codes_disable_account(self) -> None:
        for code in (C.BIZ_NOT_LOGGED_IN, C.BIZ_AUTH_EXPIRED):
            failure = P.classify_error(200, body={'code': int(code)})
            self.assertIsNotNone(failure)
            self.assertEqual(failure.kind, P.KIND_SESSION)
            self.assertEqual(failure.status, 401)
            self.assertEqual(failure.code, 'login_expired')
            self.assertTrue(failure.disable_account)
            self.assertEqual(failure.biz_code, code)

    def test_model_quota_is_model_scoped_not_account_wide(self) -> None:
        failure = P.classify_error(403, biz_code=C.BIZ_MODEL_QUOTA)
        self.assertEqual(failure.kind, P.KIND_CREDIT)
        self.assertEqual(failure.status, 402)
        self.assertEqual(failure.code, 'model_quota_exhausted')
        self.assertTrue(failure.model_scoped)
        self.assertFalse(failure.disable_account)

    def test_http_401_is_session_dead(self) -> None:
        failure = P.classify_error(401, message='expired')
        self.assertEqual(failure.kind, P.KIND_SESSION)
        self.assertTrue(failure.disable_account)

    def test_http_402_is_credits(self) -> None:
        failure = P.classify_error(402, message='no money')
        self.assertEqual(failure.kind, P.KIND_CREDIT)
        self.assertEqual(failure.code, 'insufficient_credits')

    def test_429_carries_retry_after(self) -> None:
        failure = P.classify_error(429, message='slow down',
                                   retry_after='7.9')
        self.assertEqual(failure.kind, P.KIND_SOFT_RATE)
        self.assertEqual(failure.retry_after, 7)
        self.assertIsNone(P.classify_error(429).retry_after)

    def test_5xx_is_transient_and_mapped_to_502(self) -> None:
        failure = P.classify_error(503, message='upstream down')
        self.assertEqual(failure.kind, P.KIND_TRANSIENT)
        self.assertEqual(failure.status, 502)
        self.assertFalse(failure.disable_account)

    def test_403_is_blocked_not_session_dead(self) -> None:
        failure = P.classify_error(403, message='forbidden')
        self.assertEqual(failure.kind, P.KIND_BLOCKED)
        self.assertEqual(failure.code, 'forbidden')
        self.assertFalse(failure.disable_account)

    def test_other_4xx_is_client_error(self) -> None:
        failure = P.classify_error(
            422, body={'error': {'message': 'bad schema'}})
        self.assertEqual(failure.kind, P.KIND_CLIENT)
        self.assertEqual(failure.status, 422)
        self.assertEqual(failure.code, 'bad_request')
        self.assertEqual(failure.message, 'bad schema')

    def test_dangerous_activity_is_blocked_even_on_http_200(self) -> None:
        failure = P.classify_error(
            200, body={'choices': [{'message': {
                'content': 'DANGEROUS_ACTIVITY_BLOCKED'}}]})
        self.assertEqual(failure.kind, P.KIND_BLOCKED)
        self.assertEqual(failure.status, 403)
        self.assertEqual(failure.code, 'content_blocked')

    def test_clean_2xx_is_not_an_error(self) -> None:
        self.assertIsNone(P.classify_error(200, body={'choices': []}))
        self.assertIsNone(P.classify_error(201))

    def test_biz_code_lookup_is_tolerant(self) -> None:
        self.assertEqual(P.biz_code_of({'data': {'code': '810000'}}), '810000')
        self.assertEqual(P.biz_code_of({'error': {'bizCode': 400000}}), '400000')
        self.assertEqual(P.biz_code_of({'result': {'code': 410000}}), '410000')
        # OpenAI 风格的字符串 code 不是业务码。
        self.assertEqual(P.biz_code_of({'error': {'code': 'invalid_key'}}), '')
        self.assertEqual(P.biz_code_of('not-a-dict'), '')

    def test_message_extraction(self) -> None:
        self.assertEqual(
            P.message_of({'error': {'message': 'boom'}}), 'boom')
        self.assertEqual(P.message_of({'message': 'boom'}), 'boom')
        self.assertEqual(P.message_of({'data': {'msg': 'boom'}}), 'boom')
        self.assertEqual(P.message_of('boom'), 'boom')
        self.assertEqual(P.message_of({}, fallback='fallback'), 'fallback')

    def test_dangerous_activity_marker_is_case_insensitive(self) -> None:
        self.assertTrue(P.is_dangerous_activity(
            {'message': 'dangerous_activity_blocked'}))
        self.assertFalse(P.is_dangerous_activity({'message': 'ok'}))


class AutoclawAdapterTest(unittest.TestCase):
    """适配器层：导入 / token 轮换 / 双区选号 / 冷却 / 签到 / 目录。

    全部离线：上游 HTTP 一律替换成假实现。钉住四件事：

      · refresh token 一次一换，刷新结果必须**先落盘再使用**；
      · 区域来自账号记录，`autoclawcn/` 只挑国内号、`autoclawcom/` 只挑国际号；
      · `810000` 只冷却「账号 + 模型」，同一账号的其它模型仍可选；
      · 国内站没有 `daily_signin` 时如实返回「没有可签到的任务」，不算失败。
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._orig_db = config.DB_PATH
        config.DB_PATH = Path(self.tmp.name) / 'autoclaw.db'
        db._conn = None
        db.connect()
        for target, value in (('DATA_DIR', Path(self.tmp.name)),):
            patch = mock.patch.object(config, target, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.adapter = AutoclawAdapter()

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db

    def _seed(self, **overrides) -> dict:
        realm = str(overrides.pop('realm', 'cn'))
        uid = str(overrides.pop('uid', f'ac_{realm}_1'))
        access = overrides.pop(
            'accessToken', _jwt({'user_id': uid, 'exp': _exp()}))
        data = {
            'uid': uid,
            'realm': realm,
            'accessToken': access,
            'refreshToken': overrides.pop(
                'refreshToken', _jwt({'user_id': uid, 'exp': _exp(86400)})),
            'expiredAt': store.iso_from_epoch(time.time() + 3600),
        }
        data.update(overrides)
        store.save_account(data)
        return data

    # ── 导入 ──────────────────────────────────────────────
    def test_import_derives_stable_uid_from_identity(self) -> None:
        public = self.adapter.import_account({
            'realm': 'cn',
            'phone': '13800000000',
            'access_token': _jwt({'user_id': 'u-1', 'exp': _exp()}),
            'refresh_token': _jwt({'user_id': 'u-1', 'exp': _exp(86400)}),
        })
        self.assertTrue(public['uid'].startswith('ac_'))
        self.assertEqual(public['realm'], 'cn')
        # 换一枚 refresh 重新导入，仍是同一条账号（uid 不绑 refresh 哈希）。
        again = self.adapter.import_account({
            'realm': 'cn',
            'phone': '13800000000',
            'access_token': _jwt({'user_id': 'u-1', 'exp': _exp()}),
            'refresh_token': _jwt({'user_id': 'u-1', 'exp': _exp(86400),
                                   'jti': 'rotated'}),
        })
        self.assertEqual(public['uid'], again['uid'])

    def test_import_same_user_in_two_realms_is_two_accounts(self) -> None:
        cn = self.adapter.import_account({
            'realm': 'cn', 'phone': '13800000000',
            'access_token': _jwt({'user_id': 'u-1', 'exp': _exp()}),
        })
        gl = self.adapter.import_account({
            'realm': 'global', 'phone': '13800000000',
            'access_token': _jwt({'user_id': 'u-1', 'exp': _exp()}),
        })
        self.assertNotEqual(cn['uid'], gl['uid'])
        self.assertEqual(gl['realm'], 'global')

    def test_import_defaults_to_cn_and_rejects_empty(self) -> None:
        public = self.adapter.import_account({
            'access_token': _jwt({'user_id': 'u-2', 'exp': _exp()})})
        self.assertEqual(public['realm'], 'cn')
        with self.assertRaises(PlatformError):
            self.adapter.import_account({'name': 'nothing'})

    def test_import_keeps_device_id(self) -> None:
        public = self.adapter.import_account({
            'realm': 'cn', 'device_id': 'dev-keep-me',
            'access_token': _jwt({'user_id': 'u-3', 'exp': _exp()})})
        raw = store.read_account(public['uid'])
        self.assertEqual(raw['deviceId'], 'dev-keep-me')
        self.assertEqual(public['fingerprint']['device_id'], 'dev-keep-me')

    def test_device_id_falls_back_to_derived_value(self) -> None:
        public = self.adapter.import_account({
            'realm': 'cn',
            'access_token': _jwt({'user_id': 'u-4', 'exp': _exp()})})
        raw = store.read_account(public['uid'])
        self.assertTrue(str(raw['deviceId']).startswith('ac-'))
        # 派生值是稳定的：重复读取不会每次换一个设备号。
        self.assertEqual(store.device_id_for(raw), raw['deviceId'])

    # ── token 轮换 ────────────────────────────────────────
    def test_refresh_rotates_and_persists_before_use(self) -> None:
        self._seed(accessToken='', expiredAt='',
                   refreshToken=_jwt({'user_id': 'u-1', 'exp': _exp(86400),
                                      'jti': 'old'}))
        new_access = _jwt({'user_id': 'u-1', 'exp': _exp()})

        async def fake_refresh(realm, refresh, **kwargs):
            self.assertEqual(realm, 'cn')
            self.assertEqual(kwargs.get('device_id'),
                             store.device_id_for(store.read_account('ac_cn_1')))
            return {'access_token': new_access,
                    'refresh_token': _jwt({'user_id': 'u-1',
                                           'exp': _exp(86400),
                                           'jti': 'new'}),
                    'expires_at': time.time() + 3600,
                    'refresh_expires_at': time.time() + 86400}

        with mock.patch.object(client, 'refresh', new=fake_refresh):
            token = asyncio.run(self.adapter._token_for(
                store.read_account('ac_cn_1'), proxy=None))
        self.assertEqual(token, new_access)
        raw = store.read_account('ac_cn_1')
        self.assertEqual(raw['accessToken'], new_access)
        # 新的 refresh 也落盘了（一次性 token：不落盘就等于丢链）。
        self.assertNotEqual(raw['refreshToken'], '')
        self.assertTrue(store.jwt_exp(raw['refreshToken']) > time.time())

    def test_refresh_without_refresh_lifetime_clears_stale_value(self) -> None:
        self._seed(accessToken='', expiredAt='',
                   refreshExpiredAt=store.iso_from_epoch(time.time() + 86400))

        async def fake_refresh(*args, **kwargs):
            return {'access_token': _jwt({'user_id': 'u-1', 'exp': _exp()}),
                    'refresh_token': _jwt({'user_id': 'u-1',
                                           'exp': _exp(86400)}),
                    'expires_at': time.time() + 3600,
                    'refresh_expires_at': 0}

        with mock.patch.object(client, 'refresh', new=fake_refresh):
            asyncio.run(self.adapter._token_for(
                store.read_account('ac_cn_1'), proxy=None))
        # 旧的 refreshExpiredAt 描述的是已经作废的那一枚，必须清掉。
        self.assertEqual(store.read_account('ac_cn_1')['refreshExpiredAt'], '')

    def test_refresh_401_marks_login_expired(self) -> None:
        self._seed(accessToken='', expiredAt='',
                   refreshToken=_jwt({'user_id': 'u-1', 'exp': _exp(86400)}))

        async def fake_refresh(*args, **kwargs):
            raise PlatformError('登录已失效', status=401, code='login_expired')

        with mock.patch.object(client, 'refresh', new=fake_refresh):
            with self.assertRaises(PlatformError):
                asyncio.run(self.adapter._token_for(
                    store.read_account('ac_cn_1'), proxy=None))
        self.assertEqual(store.read_account('ac_cn_1')['loginState'], '登录已失效')

    def test_missing_refresh_token_reports_relogin(self) -> None:
        self._seed(accessToken='', refreshToken='', expiredAt='')
        with self.assertRaises(PlatformError) as ctx:
            asyncio.run(self.adapter._token_for(
                store.read_account('ac_cn_1'), proxy=None))
        self.assertEqual(ctx.exception.status, 401)

    def test_valid_access_token_is_reused_without_refresh(self) -> None:
        self._seed()

        async def boom(*args, **kwargs):
            raise AssertionError('不该刷新还有效的 access token')

        with mock.patch.object(client, 'refresh', new=boom):
            token = asyncio.run(self.adapter._token_for(
                store.read_account('ac_cn_1'), proxy=None))
        self.assertEqual(token, store.read_account('ac_cn_1')['accessToken'])

    # ── 选号 ──────────────────────────────────────────────
    def test_candidates_are_partitioned_by_realm(self) -> None:
        self._seed(uid='ac_cn_1', realm='cn')
        self._seed(uid='ac_gl_1', realm='global')
        cn = self.adapter._candidates('cn')
        gl = self.adapter._candidates('global')
        self.assertEqual([a['uid'] for a in cn], ['ac_cn_1'])
        self.assertEqual([a['uid'] for a in gl], ['ac_gl_1'])
        # 不带区域时两区都在池里。
        both = {a['uid'] for a in self.adapter._candidates(None)}
        self.assertEqual(both, {'ac_cn_1', 'ac_gl_1'})

    def test_disabled_and_checkin_only_accounts_are_skipped(self) -> None:
        self._seed(uid='ac_cn_1', realm='cn')
        self._seed(uid='ac_cn_2', realm='cn')
        self._seed(uid='ac_cn_3', realm='cn')
        store.set_disabled('ac_cn_2', True, '手动停用')
        store.set_checkin_only('ac_cn_3', True)
        names = [a['uid'] for a in self.adapter._candidates('cn')]
        self.assertEqual(names, ['ac_cn_1'])

    def test_no_account_error_names_the_realm(self) -> None:
        self._seed(uid='ac_cn_1', realm='cn')

        async def run():
            return await self.adapter.chat('glm-4.6', {
                'messages': [{'role': 'user', 'content': 'hi'}]},
                type('Ctx', (), {'realm': 'global'})())

        with self.assertRaises(PlatformError) as ctx:
            asyncio.run(run())
        self.assertEqual(ctx.exception.code, 'no_account')
        self.assertIn('国际版', ctx.exception.message)

    # ── 冷却 ──────────────────────────────────────────────
    def test_model_quota_only_cools_one_model(self) -> None:
        self._seed(uid='ac_cn_1', realm='cn')
        exc = PlatformError('额度耗尽', status=402, code='model_quota_exhausted')
        failure = P.classify_error(200, biz_code=C.BIZ_MODEL_QUOTA)
        exc.failure = failure  # type: ignore[attr-defined]
        self.assertTrue(self.adapter._cool_down('ac_cn_1', exc, 'glm-4.6'))
        # 账号本身没被冻：其它模型仍然可选。
        self.assertEqual(
            [a['uid'] for a in self.adapter._candidates('cn', model='glm-4.6')],
            [])
        self.assertEqual(
            [a['uid'] for a in self.adapter._candidates('cn',
                                                        model='glm-4.5')],
            ['ac_cn_1'])
        self.assertFalse(store.is_cooling(store.read_account('ac_cn_1')))

    def test_403_switches_account_without_freezing(self) -> None:
        self._seed(uid='ac_cn_1', realm='cn')
        exc = PlatformError('风控', status=403, code='forbidden')
        self.assertTrue(self.adapter._cool_down('ac_cn_1', exc, 'glm-4.6'))
        self.assertFalse(store.is_cooling(store.read_account('ac_cn_1')))

    def test_402_cools_account(self) -> None:
        self._seed(uid='ac_cn_1', realm='cn')
        exc = PlatformError('余额不足', status=402, code='insufficient_credits')
        self.assertTrue(self.adapter._cool_down('ac_cn_1', exc, 'glm-4.6'))
        self.assertTrue(store.is_cooling(store.read_account('ac_cn_1')))

    def test_client_error_does_not_switch_account(self) -> None:
        self._seed(uid='ac_cn_1', realm='cn')
        exc = PlatformError('参数错误', status=400, code='bad_request')
        self.assertFalse(self.adapter._cool_down('ac_cn_1', exc, 'glm-4.6'))

    # ── 签到 ──────────────────────────────────────────────
    def test_checkin_without_daily_task_is_not_a_failure(self) -> None:
        self._seed(uid='ac_cn_1', realm='cn')

        async def fake_tasks(*args, **kwargs):
            return [{'task_id': 'daily_inspiration_center', 'completed': False}]

        with mock.patch.object(client, 'task_list', new=fake_tasks):
            out = asyncio.run(self.adapter.checkin('ac_cn_1'))
        self.assertTrue(out['ok'])
        self.assertFalse(out['checked_in'])
        self.assertIn('没有每日签到', out['message'])

    def test_checkin_completes_daily_task(self) -> None:
        self._seed(uid='ac_gl_1', realm='global')

        async def fake_tasks(*args, **kwargs):
            return [{'task_id': 'daily_signin', 'completed': False}]

        async def fake_complete(*args, **kwargs):
            return {'ok': True, 'already': False, 'reward': 5.0,
                    'message': '签到成功'}

        async def fake_wallets(*args, **kwargs):
            return {'total': 120.0}

        with mock.patch.object(client, 'task_list', new=fake_tasks), \
                mock.patch.object(client, 'checkin_task', new=fake_complete), \
                mock.patch.object(client, 'wallets', new=fake_wallets):
            out = asyncio.run(self.adapter.checkin('ac_gl_1'))
        self.assertTrue(out['checked_in'])
        self.assertEqual(out['earned_credit'], 5.0)
        self.assertEqual(out['credits'], 120.0)
        raw = store.read_account('ac_gl_1')
        self.assertEqual(raw['checkin']['last_result'], 'ok')

    def test_checkin_twice_is_idempotent(self) -> None:
        self._seed(uid='ac_gl_1', realm='global')

        async def fake_tasks(*args, **kwargs):
            return [{'task_id': 'daily_signin', 'completed': True}]

        async def boom(*args, **kwargs):
            raise AssertionError('已完成时不该再打领取接口')

        async def fake_wallets(*args, **kwargs):
            return {'total': 100.0}

        with mock.patch.object(client, 'task_list', new=fake_tasks), \
                mock.patch.object(client, 'checkin_task', new=boom), \
                mock.patch.object(client, 'wallets', new=fake_wallets):
            out = asyncio.run(self.adapter.checkin('ac_gl_1'))
        self.assertTrue(out['already'])
        self.assertEqual(out['earned_credit'], 0)

    # ── 目录 ──────────────────────────────────────────────
    def test_models_are_cached_per_realm(self) -> None:
        self._seed(uid='ac_cn_1', realm='cn')
        self._seed(uid='ac_gl_1', realm='global')

        async def fake_catalog(realm, access, **kwargs):
            return [{'id': f'{realm}-model', 'name': realm}]

        with mock.patch.object(client, 'fetch_catalog', new=fake_catalog):
            models = asyncio.run(self.adapter.list_models(refresh=True))
        ids = {m['id'] for m in models}
        self.assertIn('autoclawcn/cn-model', ids)
        self.assertIn('autoclawcom/global-model', ids)
        # 缓存按区域分开存：国内号拉不到时不会借国际的名单。
        self.assertIsNotNone(self.adapter._stored_models('cn'))
        self.assertIsNotNone(self.adapter._stored_models('global'))

    def test_missing_realm_catalog_stays_empty(self) -> None:
        self._seed(uid='ac_cn_1', realm='cn')

        async def fake_catalog(realm, access, **kwargs):
            if realm == 'global':
                raise PlatformError('无账号', status=401, code='login_expired')
            return [{'id': 'glm-4.6'}]

        with mock.patch.object(client, 'fetch_catalog', new=fake_catalog):
            models = asyncio.run(self.adapter.list_models(refresh=True))
        ids = {m['id'] for m in models}
        self.assertIn('autoclawcn/glm-4.6', ids)
        self.assertFalse(any(i.startswith('autoclawcom/') for i in ids))

    def test_unknown_model_is_rejected_when_catalog_cached(self) -> None:
        self._seed(uid='ac_cn_1', realm='cn')
        self.adapter._save_models('cn', catalog.normalize(
            [{'id': 'glm-4.6'}], realm='cn'))
        with self.assertRaises(PlatformError) as ctx:
            self.adapter._assert_known_model('glm-9.9', 'cn')
        self.assertEqual(ctx.exception.code, 'model_not_found')
        # 没缓存目录时不做本地判定（不能把好名字判错）。
        self.adapter._assert_known_model('glm-9.9', 'global')

    def test_upstream_model_has_no_alias(self) -> None:
        self.assertEqual(self.adapter.upstream_model(' glm-4.6 '), 'glm-4.6')
        self.assertEqual(self.adapter.upstream_model('default'), '')
        self.assertEqual(self.adapter.upstream_model(''), '')

    # ── 对话 ──────────────────────────────────────────────
    def test_non_stream_chat_returns_payload(self) -> None:
        self._seed(uid='ac_cn_1', realm='cn')

        async def fake_chat(realm, access, body, **kwargs):
            self.assertEqual(realm, 'cn')
            self.assertEqual(body['model'], 'glm-4.6')
            return {'id': 'x', 'choices': [{'message': {
                'role': 'assistant', 'content': 'hi'}}],
                'usage': {'prompt_tokens': 1, 'completion_tokens': 1}}

        with mock.patch.object(client, 'chat', new=fake_chat):
            out = asyncio.run(self.adapter.chat('glm-4.6', {
                'messages': [{'role': 'user', 'content': 'hi'}]}))
        self.assertEqual(out.payload['choices'][0]['message']['content'], 'hi')
        self.assertEqual(out.realm, 'cn')
        self.assertEqual(out.model, 'autoclawcn/glm-4.6')

    def test_stream_relay_collects_usage(self) -> None:
        self._seed(uid='ac_cn_1', realm='cn')

        class FakeResponse:
            closed = False

            async def aclose(self) -> None:
                FakeResponse.closed = True

        async def fake_open(*args, **kwargs):
            return FakeResponse()

        async def fake_chunks(_response):
            yield {'choices': [{'delta': {'role': 'assistant'}}]}
            yield {'choices': [{'delta': {'content': 'he'}}]}
            yield {'choices': [{'delta': {'content': 'llo'}}],
                   'usage': {'prompt_tokens': 3, 'completion_tokens': 2}}

        async def run():
            with mock.patch.object(client, 'open_chat', new=fake_open), \
                    mock.patch.object(client, 'iter_chunks', new=fake_chunks):
                chat = await self.adapter.chat('glm-4.6', {
                    'messages': [{'role': 'user', 'content': 'hi'}],
                    'stream': True})
                chunks = [c async for c in chat.stream]
                await chat.aclose()
                return chunks, chat

        chunks, chat = asyncio.run(run())
        text = ''.join((c['choices'][0]['delta'].get('content') or '')
                       for c in chunks)
        self.assertEqual(text, 'hello')
        self.assertEqual(chat.usage['completion_tokens'], 2)
        self.assertTrue(FakeResponse.closed)

    def test_empty_stream_is_reported_as_error(self) -> None:
        self._seed(uid='ac_cn_1', realm='cn')

        class FakeResponse:
            async def aclose(self) -> None:
                return None

        async def fake_open(*args, **kwargs):
            return FakeResponse()

        async def fake_chunks(_response):
            yield {'choices': [{'delta': {'role': 'assistant'}}]}

        async def run():
            with mock.patch.object(client, 'open_chat', new=fake_open), \
                    mock.patch.object(client, 'iter_chunks', new=fake_chunks):
                chat = await self.adapter.chat('glm-4.6', {
                    'messages': [{'role': 'user', 'content': 'hi'}],
                    'stream': True})
                return [c async for c in chat.stream]

        with self.assertRaises(PlatformError) as ctx:
            asyncio.run(run())
        self.assertEqual(ctx.exception.code, 'empty_response')

    # ── 积分 ──────────────────────────────────────────────
    def test_credits_reads_wallet_total_and_caches(self) -> None:
        self._seed(uid='ac_cn_1', realm='cn')
        calls = {'n': 0}

        async def fake_wallets(*args, **kwargs):
            calls['n'] += 1
            return {'total': 88.0}

        with mock.patch.object(client, 'wallets', new=fake_wallets):
            first = asyncio.run(self.adapter.credits('ac_cn_1'))
            second = asyncio.run(self.adapter.credits('ac_cn_1'))
        self.assertEqual(first['credits'], 88.0)
        self.assertEqual(second['credits'], 88.0)
        self.assertEqual(calls['n'], 1)

    def test_capabilities_are_declared(self) -> None:
        adapter = self.adapter
        self.assertTrue(adapter.realm_aware)
        self.assertTrue(adapter.supports_web_login)
        self.assertTrue(adapter.supports_checkin)
        self.assertFalse(adapter.supports_cookie_import)
        self.assertFalse(hasattr(adapter, 'import_from_desktop'))
        # 路由按 `hasattr` 判断两条登录路径：短信 + 粘回调都必须有。
        for name in ('login_start', 'login_poll', 'login_cancel',
                     'login_send_code', 'login_submit_code',
                     'login_submit_callback'):
            self.assertTrue(callable(getattr(adapter, name, None)), name)
        caps = runtime_settings.capabilities('autoclaw')
        self.assertTrue(caps['max_accounts_try'])
        self.assertFalse(caps['min_credits'])


if __name__ == '__main__':
    unittest.main()
