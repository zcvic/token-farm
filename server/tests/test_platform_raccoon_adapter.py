"""商汤小浣熊（raccoon）：账号存储、目录、token 轮换、签到与对话策略。

全部离线：刷新 / 对话 / 积分接口都替换成假客户端。这里钉住几件最容易接错的
协议事实：

  · refresh token 是**一次性**的：刷新结果必须**先落盘再使用**；
  · 未知模型上游会静默回落默认模型并 200，必须本地按目录拒绝；
  · v2 流式失败要退回 v1 非流式，再由 `synthesize_stream` 合成 SSE；
  · 签到没有独立领取接口，靠桌面端 `setting_info` 触发后核对账单；
  · 积分只按「北京时间当天、points > 0」的条目汇总（01:30 前按前一天宽限）。
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from server import config, db
from server.platforms.base import PlatformError
from server.platforms.raccoon import catalog, client, constants as C, store
from server.platforms.raccoon.adapter import (
    CREDITS_COOLDOWN_SECONDS, RaccoonAdapter)
from server.platforms import settings as runtime_settings

_BEIJING = timezone(timedelta(hours=8))


def _jwt(**payload) -> str:
    header = {'alg': 'none', 'typ': 'JWT'}
    body = {'exp': int(time.time()) + 3600, 'user_id': 'user-1'}
    body.update(payload)

    def encode(value: dict) -> str:
        raw = json.dumps(value, separators=(',', ':')).encode('utf-8')
        return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')

    return f'{encode(header)}.{encode(body)}.signature'


def _bill_time(day) -> str:
    return f'{day} 09:00:00'


class RaccoonStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(config, 'DATA_DIR', Path(self.tmp.name))
        patch.start()
        self.addCleanup(patch.stop)

    def test_normalize_token_handles_bearer_and_cookie(self) -> None:
        self.assertEqual(store.normalize_token('Bearer abc.def'), 'abc.def')
        self.assertEqual(
            store.normalize_token('foo=1; xhx_token=xyz.123; bar=2'), 'xyz.123')
        self.assertEqual(
            store.normalize_token('authorization: Bearer "tok"'), 'tok')

    def test_derive_uid_is_stable_across_refresh_rotation(self) -> None:
        access = _jwt(user_id='u-42')
        first = store.derive_uid(access, _jwt(user_id='u-42', jti='a'))
        second = store.derive_uid(access, _jwt(user_id='u-42', jti='b'))
        self.assertTrue(first.startswith('rc_'))
        self.assertEqual(first, second, 'refresh 轮换不该变成两条账号')

    def test_public_account_hides_tokens(self) -> None:
        access = _jwt()
        refresh = _jwt(user_id='u-42')
        saved = store.save_account({
            'uid': 'rc_1', 'token': access, 'refreshToken': refresh,
            'email': 'a@b.c'})
        public = store.public_account(saved)
        dumped = json.dumps(public, ensure_ascii=False)
        self.assertNotIn(access, dumped)
        self.assertNotIn(refresh, dumped)
        self.assertTrue(public['has_token'])
        self.assertTrue(public['has_refresh_token'])
        self.assertTrue(public['token_auto_refresh'])
        self.assertEqual(public['realm'], 'cn')

    def test_safe_id_rejects_traversal(self) -> None:
        for bad in ('../x', 'a/b', 'a\\b', '', 'x' * 200):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    store.safe_id(bad)


class RaccoonCatalogTest(unittest.TestCase):
    def test_parse_catalog_flattens_all_categories(self) -> None:
        payload = {'code': 0, 'data': {'categories': [
            {'type': 'agent', 'models': [
                {'name': 'sn-glm-5-3', 'display_description': 'GLM 5.3',
                 'description': 'a much longer description',
                 'context_window': '200000', 'max_tokens': '8000',
                 'tags': ['reasoning'], 'billing_effective_multiplier': 0,
                 'params': {'raccoon_thinking': {
                     'supported': True, 'can_disable': False,
                     'display_levels': [{'value': 'level_1'}],
                     'default_level': 'level_1'}}},
                {'name': 'raccoon-work'},
                {'name': 'sn-glm-5-3', 'description': 'duplicate'},
            ]},
            {'type': 'not-chat', 'models': [
                {'name': 'sn-kimi-k3', 'params': {'context_window': 1000}},
            ]},
        ]}}
        models = client.parse_catalog(payload)
        self.assertEqual([m['id'] for m in models],
                         ['sn-glm-5-3', 'sn-kimi-k3'])
        first = models[0]
        self.assertEqual(first['name'], 'GLM 5.3')
        self.assertEqual(first['context_length'], 200000)
        self.assertEqual(first['max_tokens'], 8000)
        self.assertTrue(first['reasoning'])
        self.assertTrue(first['free'], 'effective multiplier 0 = 限免')
        self.assertEqual(first['thinking']['levels'], ['level_1'])
        self.assertEqual(models[1]['context_length'], 1000)

    def test_default_models_are_usable(self) -> None:
        models = catalog.default_models()
        self.assertTrue(models)
        self.assertTrue(all(m['id'] for m in models))
        self.assertTrue(any(m['free'] for m in models))

    def test_measured_vision_overrides_unreliable_tag(self) -> None:
        models = client.parse_catalog({'categories': [{'models': [
            {'name': 'sn-glm-5-3', 'tags': ['vision']},
            {'name': 'sn-glm-5-3-flash'},
        ]}]})
        by_id = {m['id']: m for m in models}
        self.assertNotIn('image', by_id['sn-glm-5-3']['modalities'])
        self.assertIn('image', by_id['sn-glm-5-3-flash']['modalities'])


class RaccoonProtocolTest(unittest.TestCase):
    def test_chat_body_strips_reasoning_effort_and_fills_defaults(self) -> None:
        body = client.chat_body({'model': 'sn-glm-5-3',
                                 'reasoning_effort': 'high',
                                 'messages': []})
        self.assertNotIn('reasoning_effort', body)
        self.assertEqual(body['max_tokens'], C.DEFAULT_MAX_TOKENS)
        self.assertTrue(body['verbose'])
        self.assertFalse(body['stream'])
        # OpenAI 新字段名：客户端只发 max_completion_tokens 时不能把预算丢掉。
        newer = client.chat_body({'model': 'sn-glm-5-3',
                                  'max_completion_tokens': 1024,
                                  'messages': []})
        self.assertEqual(newer['max_tokens'], 1024)

    def test_classify_risk_control_is_429_with_long_cooldown(self) -> None:
        error = client.classify(200, {'code': 200108,
                                      'message': 'abnormal traffic'})
        self.assertIsNotNone(error)
        assert error is not None
        self.assertEqual(error.status, 429)
        self.assertEqual(error.retry_after, C.RISK_CONTROL_COOLDOWN)
        self.assertTrue(client.is_risk_control(error))

    def test_classify_200_error_envelope_is_upstream_error(self) -> None:
        """HTTP 200 也可能是错误信封（没有顶层 code，只有 error）。"""
        error = client.classify(200, {'error': {'message': 'content blocked'}})
        self.assertIsNotNone(error)
        assert error is not None
        self.assertEqual(error.status, 502)
        self.assertEqual(error.code, 'upstream_error')
        self.assertIn('content blocked', error.message)
        # 正常响应里的 error:null 不能被误判
        self.assertIsNone(client.classify(200, {'error': None, 'choices': [{}]}))

    def test_completion_of_unwraps_data_envelope(self) -> None:
        inner = {'choices': [{'index': 0, 'finish_reason': 'stop',
                              'message': {'role': 'assistant',
                                          'content': 'ok'}}],
                 'usage': {'prompt_tokens': 1, 'completion_tokens': 1}}
        root = client.completion_of({'code': 0, 'model': 'sn-glm-5-3',
                                     'data': inner})
        self.assertIsNotNone(root)
        assert root is not None
        self.assertEqual(root['choices'][0]['message']['content'], 'ok')
        self.assertEqual(root['model'], 'sn-glm-5-3', '外层 model 应补回')
        # data 被序列化成字符串也认
        wrapped = client.completion_of({'data': json.dumps(inner)})
        self.assertIsNotNone(wrapped)
        assert wrapped is not None
        self.assertEqual(wrapped['choices'][0]['message']['content'], 'ok')
        # 业务信封、空 choices 都不能当成完成响应
        self.assertIsNone(client.completion_of({'code': 0, 'data': {'foo': 1}}))
        self.assertIsNone(client.completion_of({'choices': []}))
        self.assertIsNone(client.completion_of('not json'))

    def test_chat_missing_choices_includes_upstream_detail(self) -> None:
        """找不到 choices 时错误里要带上游摘要，而不是一句无从查起的话。"""
        class FakeResponse:
            status_code = 200
            text = '{"code":0,"message":"model unavailable","data":null}'

        async def fake_request(*args, **kwargs):
            return FakeResponse()

        with mock.patch.object(client, '_request', new=fake_request):
            with self.assertRaises(PlatformError) as ctx:
                asyncio.run(client.chat({}, 'token',
                                        {'model': 'x', 'messages': []}))
        message = ctx.exception.message
        self.assertIn('缺少 choices', message)
        self.assertIn('model unavailable', message)

    def test_daily_reward_only_counts_positive_today(self) -> None:
        now = datetime(2026, 9, 1, 12, 0, tzinfo=_BEIJING).timestamp()
        items = [
            {'points': 300, 'created_at': _bill_time('2026-09-01')},
            {'points': 50, 'created_at': '2026-09-01T08:00:00+08:00'},
            {'points': -1, 'created_at': _bill_time('2026-09-01')},
            {'points': 300, 'created_at': _bill_time('2026-08-31')},
            {'points': 0, 'created_at': _bill_time('2026-09-01')},
        ]
        self.assertEqual(client.daily_reward(items, now), 350)

    def test_bill_grace_before_0130_uses_previous_day(self) -> None:
        now = datetime(2026, 9, 2, 1, 0, tzinfo=_BEIJING).timestamp()
        bill = {'points': 300, 'created_at': _bill_time('2026-09-01')}
        self.assertTrue(client.bill_is_today(bill, now))
        yes = {'points': 300, 'created_at': _bill_time('2026-09-02')}
        self.assertFalse(client.bill_is_today(yes, now))


class RaccoonAdapterTest(unittest.TestCase):
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
        self.adapter = RaccoonAdapter()

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db

    def _seed(self, **overrides) -> dict:
        uid = str(overrides.pop('uid', 'rc_1'))
        access = overrides.pop('token', _jwt(user_id='u-1'))
        data = {'uid': uid, 'token': access,
                'refreshToken': overrides.pop('refreshToken', _jwt(user_id='u-1')),
                'expiredAt': store.iso_from_epoch(time.time() + 3600)}
        data.update(overrides)
        store.save_account(data)
        return data

    @staticmethod
    async def _fake_token(_account, **_kwargs) -> str:
        return 'access-token'

    # ── 导入 ──────────────────────────────────────────────
    def test_import_account_derives_uid_from_jwt(self) -> None:
        access = _jwt(user_id='u-77')
        public = self.adapter.import_account({
            'access_token': access,
            'refresh_token': _jwt(user_id='u-77', jti='a')})
        raw = store.read_account(public['uid'])
        self.assertEqual(raw['token'], access)
        self.assertTrue(raw['refreshToken'])
        self.assertTrue(raw['expiredAt'])
        self.assertEqual(raw['loginState'], '正常')
        # 同一用户换一对 token 重新导入，仍是同一条账号。
        again = self.adapter.import_account({
            'access_token': access,
            'refresh_token': _jwt(user_id='u-77', jti='b')})
        self.assertEqual(public['uid'], again['uid'])

    def test_import_accepts_cookie_fragment(self) -> None:
        public = self.adapter.import_account({
            'cookie': f'foo=1; xhx_token={_jwt(user_id="u-9")}; bar=2',
            'refresh_token': _jwt(user_id='u-9')})
        raw = store.read_account(public['uid'])
        self.assertTrue(raw['token'].count('.') == 2)

    def test_import_requires_credentials(self) -> None:
        with self.assertRaises(PlatformError):
            self.adapter.import_account({'name': 'nothing'})

    def test_import_from_desktop_reads_auth_json(self) -> None:
        config_dir = Path(self.tmp.name) / 'box-agent' / 'config'
        config_dir.mkdir(parents=True)
        access = _jwt(user_id='u-desk')
        (config_dir / 'auth.json').write_text(json.dumps({
            # 部分版本把凭据包在 `auth` 里，真实文件也是这个形状。
            'auth': {'access_token': access,
                     'refresh_token': _jwt(user_id='u-desk', jti='r'),
                     'office_org_name': '测试组织'}}), encoding='utf-8')
        with mock.patch.dict(os.environ,
                             {'BOX_AGENT_CONFIG_DIR': str(config_dir)}):
            out = self.adapter.import_from_desktop()
        self.assertEqual(out['platform'], 'raccoon')
        self.assertEqual(len(out['imported']), 1)
        self.assertTrue(out['sources'][0].endswith('auth.json'))
        uid = out['imported'][0]['uid']
        raw = store.read_account(uid)
        self.assertEqual(raw['token'], access)
        self.assertEqual(raw['source'], 'desktop')
        self.assertEqual(raw['office_org_name'], '测试组织')
        # 只读客户端，不回写它的文件。
        self.assertTrue((config_dir / 'auth.json').is_file())

    def test_import_from_desktop_skips_bad_entries(self) -> None:
        from server.platforms.raccoon import desktop  # noqa: PLC0415

        entries = [
            {'access_token': _jwt(user_id='u-ok'),
             'refresh_token': _jwt(user_id='u-ok', jti='r')},
            {'nickname': '没有凭据'},
        ]
        with mock.patch.object(desktop, 'read_accounts',
                               return_value=entries), \
                mock.patch.object(desktop, 'storage_paths', return_value=[
                    {'path': Path('C:/Users/Public/.box-agent/config/auth.json'),
                     'user': 'x'}]):
            out = self.adapter.import_from_desktop()
        self.assertEqual(len(out['imported']), 1)
        self.assertEqual(len(out['failed']), 1)
        self.assertIn('auth.json', out['sources'][0])

    # ── token 轮换 ────────────────────────────────────────
    def test_refresh_rotates_and_persists_both_tokens(self) -> None:
        self._seed(token='', expiredAt='',
                   refreshToken=_jwt(user_id='u-1', jti='old'))
        new_access = _jwt(user_id='u-1', jti='new-access')

        async def fake_refresh(refresh, **kwargs):
            self.assertIn('old', store.jwt_payload(refresh).get('jti', ''))
            return {'access_token': new_access,
                    'refresh_token': _jwt(user_id='u-1', jti='new-refresh'),
                    'expires_at': time.time() + 3600,
                    'refresh_expires_at': time.time() + 86400}

        with mock.patch.object(client, 'refresh', new=fake_refresh):
            token = asyncio.run(self.adapter._token_for(
                store.read_account('rc_1'), proxy=None))
        self.assertEqual(token, new_access)
        raw = store.read_account('rc_1')
        self.assertEqual(raw['token'], new_access)
        self.assertIn('new-refresh', store.jwt_payload(
            raw['refreshToken']).get('jti', ''))
        self.assertTrue(raw['refreshExpiredAt'])

    def test_refresh_conflict_marks_login_expired(self) -> None:
        self._seed(token='', expiredAt='')

        async def fake_refresh(refresh, **kwargs):
            raise PlatformError('refresh_conflict', status=401,
                                code='login_expired')

        with mock.patch.object(client, 'refresh', new=fake_refresh):
            with self.assertRaises(PlatformError):
                asyncio.run(self.adapter._token_for(
                    store.read_account('rc_1'), proxy=None))
        self.assertEqual(store.read_account('rc_1')['loginState'], '登录已失效')

    def test_access_only_account_is_used_then_expires(self) -> None:
        self._seed(refreshToken='')
        token = asyncio.run(self.adapter._token_for(
            store.read_account('rc_1'), proxy=None))
        self.assertTrue(token)
        self.assertEqual(self.adapter._tokens['rc_1'][1], token)

    # ── 模型目录 ──────────────────────────────────────────
    def test_default_alias_maps_to_builtin_default_model(self) -> None:
        self.assertEqual(self.adapter.upstream_model('default'), C.DEFAULT_MODEL)
        self.assertEqual(self.adapter.upstream_model(''), C.DEFAULT_MODEL)
        self.assertEqual(self.adapter.upstream_model('sn-glm-5-3'), 'sn-glm-5-3')
        # `default` 是别名，不该被本地未知模型校验拦下。
        self.adapter._assert_known_model('default')

    def test_unknown_model_is_rejected_locally(self) -> None:
        self._seed()
        db.set_setting('platforms.raccoon.models', {
            'fetched_at': int(time.time()),
            'models': [{'id': 'sn-glm-5-3', 'name': 'GLM'}]})
        with self.assertRaises(PlatformError) as ctx:
            asyncio.run(self.adapter.chat(
                'not-a-model', {'messages': [{'role': 'user', 'content': 'hi'}]}))
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(ctx.exception.code, 'model_not_found')

    def test_list_models_falls_back_to_default_snapshot(self) -> None:
        async def offline(**kwargs):
            return []

        with mock.patch.object(self.adapter, '_live_models', new=offline):
            models = asyncio.run(self.adapter.list_models(refresh=True))
        ids = {m['id'] for m in models}
        self.assertIn('raccoon/sn-glm-5-3', ids)
        self.assertTrue(all(m['realm'] == 'cn' for m in models))
        self.assertIn('raccoon/sn-glm-5-3', self.adapter.cached_model_ids())

    def test_list_models_saves_live_catalog(self) -> None:
        async def live(**kwargs):
            return [{'id': 'sn-x', 'name': 'X', 'context_length': 1000,
                     'max_tokens': 100, 'tags': ['vision'],
                     'modalities': ['text', 'image'], 'reasoning': True}]

        with mock.patch.object(self.adapter, '_live_models', new=live):
            models = asyncio.run(self.adapter.list_models(refresh=True))
        self.assertEqual(models[0]['id'], 'raccoon/sn-x')
        self.assertTrue(models[0]['attachment'])
        self.assertTrue(self.adapter._cached_models())

    # ── 对话 ──────────────────────────────────────────────
    def test_stream_relay_forwards_chunks_and_usage(self) -> None:
        self._seed()

        class FakeResponse:
            def __init__(self) -> None:
                self.closed = False

            async def aclose(self) -> None:
                self.closed = True

        response = FakeResponse()

        async def fake_open(*args, **kwargs):
            return response

        async def fake_chunks(_response):
            yield {'model': 'sn-glm-5-3',
                   'choices': [{'delta': {'role': 'assistant'}}]}
            yield {'model': 'sn-glm-5-3',
                   'choices': [{'delta': {'content': 'he'}}]}
            yield {'model': 'sn-glm-5-3',
                   'choices': [{'delta': {'content': 'llo'},
                                'finish_reason': 'stop'}],
                   'usage': {'prompt_tokens': 3, 'completion_tokens': 2}}

        async def collect() -> list[dict]:
            with mock.patch.object(self.adapter, '_token_for',
                                   new=self._fake_token), \
                    mock.patch.object(client, 'open_chat', new=fake_open), \
                    mock.patch.object(client, 'iter_chunks', new=fake_chunks):
                chat = await self.adapter._chat_once(
                    store.read_account('rc_1'), 'sn-glm-5-3',
                    {'messages': [{'role': 'user', 'content': 'hi'}],
                     'stream': True},
                    'raccoon/sn-glm-5-3', proxy=None,
                    runtime=runtime_settings.load('raccoon'))
                out = [chunk async for chunk in chat.stream]
                return out, chat

        chunks, chat = asyncio.run(collect())
        text = ''.join((chunk['choices'][0]['delta'].get('content') or '')
                       for chunk in chunks)
        self.assertEqual(text, 'hello')
        self.assertEqual(chat.usage['completion_tokens'], 2)
        self.assertTrue(response.closed)

    def test_stream_rejects_silent_model_fallback(self) -> None:
        self._seed()

        class FakeResponse:
            async def aclose(self) -> None:
                return None

        async def fake_open(*args, **kwargs):
            return FakeResponse()

        async def fake_chunks(_response):
            yield {'model': 'sn-default',
                   'choices': [{'delta': {'content': 'wrong'}}]}

        async def run() -> None:
            with mock.patch.object(self.adapter, '_token_for',
                                   new=self._fake_token), \
                    mock.patch.object(client, 'open_chat', new=fake_open), \
                    mock.patch.object(client, 'iter_chunks', new=fake_chunks):
                chat = await self.adapter._chat_once(
                    store.read_account('rc_1'), 'sn-glm-5-3',
                    {'messages': [], 'stream': True},
                    'raccoon/sn-glm-5-3', proxy=None,
                    runtime=runtime_settings.load('raccoon'))
                _ = [chunk async for chunk in chat.stream]

        with self.assertRaises(PlatformError) as ctx:
            asyncio.run(run())
        self.assertEqual(ctx.exception.code, 'model_fallback')

    def test_stream_falls_back_to_v1_non_stream(self) -> None:
        self._seed()
        calls: list[str] = []

        async def fake_open(*args, **kwargs):
            raise PlatformError('invalid upstream stream response', status=502,
                                code='not_sse')

        async def fake_chat(account, access, body, *, base, **kwargs):
            calls.append(base)
            return {'id': 'chatcmpl-1', 'created': 1,
                    'model': 'sn-glm-5-3',
                    'choices': [{'index': 0, 'finish_reason': 'stop',
                                 'message': {'role': 'assistant',
                                             'content': 'fallback'}}],
                    'usage': {'prompt_tokens': 1, 'completion_tokens': 2}}

        async def run() -> list[dict]:
            with mock.patch.object(self.adapter, '_token_for',
                                   new=self._fake_token), \
                    mock.patch.object(client, 'open_chat', new=fake_open), \
                    mock.patch.object(client, 'chat', new=fake_chat):
                chat = await self.adapter._chat_once(
                    store.read_account('rc_1'), 'sn-glm-5-3',
                    {'messages': [], 'stream': True},
                    'raccoon/sn-glm-5-3', proxy=None,
                    runtime=runtime_settings.load('raccoon'))
                return [chunk async for chunk in chat.stream]

        chunks = asyncio.run(run())
        self.assertEqual(calls, [C.LLM_V1])
        text = ''.join((chunk['choices'][0]['delta'].get('content') or '')
                       for chunk in chunks)
        self.assertEqual(text, 'fallback')

    def test_non_stream_falls_back_to_v1(self) -> None:
        self._seed()
        calls: list[str] = []

        async def fake_chat(account, access, body, *, base, **kwargs):
            calls.append(base)
            if base == C.LLM_V2:
                raise PlatformError('risk', status=429, code='rate_limited',
                                    payload={'code': 200108})
            return {'id': 'chatcmpl-1', 'created': 1,
                    'model': 'sn-glm-5-3',
                    'choices': [{'index': 0, 'finish_reason': 'stop',
                                 'message': {'role': 'assistant',
                                             'content': 'ok'}}],
                    'usage': {'prompt_tokens': 1, 'completion_tokens': 1}}

        async def run():
            with mock.patch.object(self.adapter, '_token_for',
                                   new=self._fake_token), \
                    mock.patch.object(client, 'chat', new=fake_chat):
                return await self.adapter._chat_once(
                    store.read_account('rc_1'), 'sn-glm-5-3',
                    {'messages': []}, 'raccoon/sn-glm-5-3', proxy=None,
                    runtime=runtime_settings.load('raccoon'))

        chat = asyncio.run(run())
        self.assertEqual(calls, [C.LLM_V2, C.LLM_V1])
        self.assertEqual(chat.payload['choices'][0]['message']['content'], 'ok')

    # ── 签到 / 积分 ───────────────────────────────────────
    @staticmethod
    def _today_or_grace_day() -> str:
        now = client.beijing_now()
        day = now.date()
        if now.hour * 60 + now.minute < 90:
            day = day - timedelta(days=1)
        return day.isoformat()

    def test_checkin_counts_today_positive_bills(self) -> None:
        self._seed()
        day = self._today_or_grace_day()

        async def fake_grant(account, access, **kwargs):
            return {'ok': True, 'popups': [{'points': 300}], 'toast': '到账 300'}

        async def fake_bills(access, **kwargs):
            return [
                {'points': 300, 'created_at': f'{day} 00:05:00'},
                {'points': -1, 'created_at': f'{day} 09:00:00'},
                {'points': 30, 'created_at': '2000-01-01 09:00:00'},
            ]

        with mock.patch.object(self.adapter, '_token_for',
                               new=self._fake_token), \
                mock.patch.object(client, 'claim_daily_grant',
                                  new=fake_grant), \
                mock.patch.object(client, 'bills', new=fake_bills):
            out = asyncio.run(self.adapter.checkin('rc_1'))
        self.assertTrue(out['ok'])
        self.assertFalse(out['already'])
        self.assertEqual(out['reward'], 300)
        self.assertIn('300', out['message'])
        self.assertEqual(store.read_account('rc_1')['checkin']['reward'], 300)

    def test_checkin_without_grant_popup_is_already(self) -> None:
        self._seed()
        day = self._today_or_grace_day()

        async def fake_grant(account, access, **kwargs):
            return {'ok': True, 'popups': [], 'toast': None}

        async def fake_bills(access, **kwargs):
            return [{'points': 300, 'created_at': f'{day} 00:05:00'}]

        with mock.patch.object(self.adapter, '_token_for',
                               new=self._fake_token), \
                mock.patch.object(client, 'claim_daily_grant',
                                  new=fake_grant), \
                mock.patch.object(client, 'bills', new=fake_bills):
            out = asyncio.run(self.adapter.checkin('rc_1'))
        self.assertTrue(out['ok'])
        self.assertTrue(out['already'])
        self.assertEqual(store.read_account('rc_1')['checkin']['last_result'],
                         'already')

    def test_credits_reads_balance_and_caches(self) -> None:
        self._seed()
        calls: list[str] = []

        async def fake_balance(access, **kwargs):
            calls.append(access)
            return {'available_points': 1200, 'daily_points': 300,
                    'monthly_points': 900, 'reward_points': 0,
                    'topup_points': 0, 'raw': {}}

        with mock.patch.object(self.adapter, '_token_for',
                               new=self._fake_token), \
                mock.patch.object(client, 'balance', new=fake_balance):
            first = asyncio.run(self.adapter.credits('rc_1'))
            second = asyncio.run(self.adapter.credits('rc_1'))
        self.assertEqual(first['credits'], 1200)
        self.assertEqual(first['unit'], 'points')
        self.assertEqual(second['credits'], 1200)
        self.assertEqual(len(calls), 1, '60 秒缓存内不该重复打上游')
        self.assertEqual(store.read_account('rc_1')['balance']['remaining'],
                         1200)

    # ── 冷却 ──────────────────────────────────────────────
    def test_risk_control_cools_for_25_minutes_and_drops_from_selection(
            self) -> None:
        self._seed()
        exc = client.classify(429, {'code': 200108, 'message': 'abnormal'})
        self.assertEqual(exc.status, 429)
        self.assertTrue(self.adapter._cool_down('rc_1', exc))
        remaining = store.read_account('rc_1')['coolingUntil'] - time.time()
        self.assertGreater(remaining, C.RISK_CONTROL_COOLDOWN - 10)
        # 风控号立刻从选号池里消失，不会下一个请求又撞上去。
        self.assertEqual(self.adapter._candidates(), [])

    def test_credits_exhausted_cools_for_ten_minutes(self) -> None:
        self._seed()
        exc = PlatformError('积分不足', status=402, code='insufficient_credits')
        self.assertTrue(self.adapter._cool_down('rc_1', exc))
        remaining = store.read_account('rc_1')['coolingUntil'] - time.time()
        self.assertGreater(remaining, CREDITS_COOLDOWN_SECONDS - 10)

    def test_auth_failure_does_not_cool_the_account(self) -> None:
        self._seed()
        exc = PlatformError('expired', status=401, code='auth_expired')
        self.assertFalse(self.adapter._cool_down('rc_1', exc))
        self.assertFalse(store.read_account('rc_1').get('coolingUntil'))


if __name__ == '__main__':
    unittest.main()
