"""DuMate（百度搭子）平台：账号存储、模型目录、适配器策略。

全部离线：token / 对话 / 签到 / 积分都替换成假客户端，测的是
「上游字段 → token-farm 形状」的翻译与选号 / 续期策略。
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from server import config, db
from server.platforms.base import PlatformError
from server.platforms.dumate import catalog, client, constants as C, store
from server.platforms.dumate.adapter import DumateAdapter, _scan_sse

_COOKIE = 'BDUSS=abc123efg; BAIDUID=deadbeef; STOKEN=xyz'


class DumateStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(config, 'DATA_DIR', Path(self.tmp.name))
        patch.start()
        self.addCleanup(patch.stop)

    def test_derive_uid_is_stable_and_requires_bduss(self) -> None:
        first = store.derive_uid(_COOKIE)
        self.assertTrue(first.startswith('dm_'))
        self.assertEqual(first, store.derive_uid(_COOKIE))
        self.assertNotEqual(first, store.derive_uid('BDUSS=other; BAIDUID=x'))
        with self.assertRaises(ValueError):
            store.derive_uid('BAIDUID=x')

    def test_normalize_cookie_strips_prefix_and_newlines(self) -> None:
        raw = 'Cookie: BDUSS=a;\r\n BAIDUID=b\n;; STOKEN=c;'
        self.assertEqual(store.normalize_cookie(raw), 'BDUSS=a; BAIDUID=b; STOKEN=c')

    def test_public_account_hides_cookie(self) -> None:
        saved = store.save_account({
            'uid': 'dm_1', 'cookie': _COOKIE, 'nickname': '小明',
        })
        public = store.public_account(saved)
        dumped = json.dumps(public, ensure_ascii=False)
        self.assertNotIn('abc123efg', dumped)
        self.assertNotIn('deadbeef', dumped)
        self.assertTrue(public['has_token'])
        self.assertFalse(public['has_refresh_token'])
        self.assertEqual(public['cookie_summary']['has_bduss'], True)
        self.assertEqual(public['realm'], 'cn')

    def test_safe_id_rejects_traversal(self) -> None:
        for bad in ('../x', 'a/b', 'a\\b', '', 'x' * 200):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    store.safe_id(bad)

    def test_weight_and_cooling_mutations(self) -> None:
        store.save_account({'uid': 'dm_1', 'cookie': _COOKIE})
        store.set_weight('dm_1', 2.5)
        self.assertEqual(store.read_account('dm_1')['weight'], 2.5)
        store.set_cooling('dm_1', time.time() + 60, 'test')
        self.assertTrue(store.is_cooling(store.read_account('dm_1')))
        store.clear_cooling('dm_1')
        self.assertFalse(store.is_cooling(store.read_account('dm_1')))


class DumateCatalogTest(unittest.TestCase):
    def test_import_models_filters_and_keeps_metadata(self) -> None:
        config_json = {'provider': {'QianfanPersonalQuota': {'models': {
            'model-text': {'id': 'model-text',
                           'limit': {'context': 1000000, 'output': 384000},
                           'tool_call': True,
                           'modalities': {'input': ['text', 'image']}},
            'glm-5-old': {'id': 'model-text',
                          'limit': {'context': 1, 'output': 1}},
            'dm-auto-model/text.L0': {'id': 'dm-auto-model/text.L0',
                                      'limit': {'input': 1048576, 'output': 393216}},
            'some-embedding': {'id': 'some-embedding',
                               'limit': {'context': 10, 'output': 10}},
            'model-ocr': {'id': 'model-ocr',
                          'limit': {'context': 10, 'output': 10}},
            'no-limit': {'id': 'no-limit'},
        }}}}
        models = catalog.import_models(config_json)
        ids = [m['id'] for m in models]
        # 别名指向真实条目 → 丢掉；embedding / model-ocr / 缺上限 → 丢掉
        self.assertEqual(ids, ['model-text', 'dm-auto-model/text.L0'])
        first = models[0]
        self.assertEqual(first['context_length'], 1000000)
        self.assertEqual(first['max_tokens'], 384000)
        self.assertTrue(first['attachment'])
        self.assertEqual(models[1]['context_length'], 1048576)

    def test_import_models_rejects_wrong_shape(self) -> None:
        self.assertEqual(catalog.import_models({}), [])
        self.assertEqual(catalog.import_models({'provider': {}}), [])
        self.assertEqual(catalog.read_desktop_models(), [])

    def test_default_catalog_has_model_text(self) -> None:
        models = catalog.default_models()
        ids = {m['id'] for m in models}
        self.assertIn('model-text', ids)
        self.assertIn('glm-53', ids)
        self.assertTrue(all(m['type'] == 'chat' for m in models))

    def test_source_path_uses_env_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'opencode.json'
            path.write_text(json.dumps({'provider': {
                'QianfanPersonalQuota': {'models': {
                    'model-text': {'id': 'model-text',
                                   'limit': {'context': 10, 'output': 10}}}}}}),
                encoding='utf-8')
            with mock.patch.dict(os.environ,
                                 {'DUMATE_OPENCODE_CONFIG': str(path)}):
                self.assertEqual(catalog.source_path(), path)
                self.assertEqual(len(catalog.read_desktop_models()), 1)


class DumateAdapterTest(unittest.TestCase):
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
        self.adapter = DumateAdapter()

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db

    def _seed(self, **overrides) -> dict:
        uid = str(overrides.pop('uid', 'dm_1'))
        data = {'uid': uid, 'cookie': _COOKIE, 'nickname': 'n'}
        data.update(overrides)
        store.save_account(data)
        return data

    # ── 导入 ──────────────────────────────────────────────
    def test_import_account_derives_uid_and_hides_cookie(self) -> None:
        public = self.adapter.import_account({'cookie': _COOKIE})
        self.assertTrue(public['uid'].startswith('dm_'))
        self.assertTrue(public['has_token'])
        raw = store.read_account(public['uid'])
        self.assertEqual(raw['cookie'], _COOKIE)
        self.assertEqual(public['cookie_summary']['has_bduss'], True)

    def test_import_account_accepts_nested_and_cookie_header(self) -> None:
        public = self.adapter.import_account(
            {'account': {'cookie': 'Cookie: BDUSS=abc123efg; BAIDUID=x'}})
        self.assertTrue(public['has_token'])
        self.assertEqual(store.read_account(public['uid'])['cookie'],
                         'BDUSS=abc123efg; BAIDUID=x')

    def test_import_requires_cookie(self) -> None:
        with self.assertRaises(PlatformError):
            self.adapter.import_account({'nickname': 'x'})

    # ── 模型目录 ──────────────────────────────────────────
    def test_list_models_adds_platform_prefix(self) -> None:
        models = asyncio.run(self.adapter.list_models())
        ids = {m['id'] for m in models}
        self.assertIn('dumate/model-text', ids)
        self.assertIn('dumate/glm-53', ids)
        self.assertTrue(all(m['realm'] == 'cn' for m in models))
        self.assertIn('dumate/model-text', self.adapter.cached_model_ids())

    def test_list_models_prefers_desktop_catalog(self) -> None:
        live = [{'id': 'only-desktop', 'name': 'only', 'context_length': 10,
                 'max_tokens': 10, 'modalities': ['text']}]
        with mock.patch.object(catalog, 'read_desktop_models', return_value=live):
            models = asyncio.run(self.adapter.list_models(refresh=True))
        self.assertEqual([m['model_name'] for m in models], ['only-desktop'])

    # ── 选号 ──────────────────────────────────────────────
    def test_candidates_skip_disabled_and_cooling(self) -> None:
        self._seed(uid='dm_1')
        self._seed(uid='dm_2', disabled=True)
        self._seed(uid='dm_3', coolingUntil=time.time() + 600)
        ids = [a['uid'] for a in self.adapter._candidates()]
        self.assertEqual(ids, ['dm_1'])

    def test_weights_rotate_first_choice(self) -> None:
        self._seed(uid='dm_1', weight=1)
        self._seed(uid='dm_2', weight=1)
        firsts = [self.adapter._candidates()[0]['uid'] for _ in range(4)]
        self.assertEqual(sorted(firsts), ['dm_1', 'dm_1', 'dm_2', 'dm_2'])

    # ── token / 对话 ──────────────────────────────────────
    def test_token_is_cached_until_skew(self) -> None:
        self._seed()
        calls: list[str] = []

        async def fake_token(cookie, **kwargs):
            calls.append(cookie)
            return {'token': 't1', 'expired_at': '2099-01-01T00:00:00Z'}

        async def fake_chat(token, body, **kwargs):
            return {'choices': [{'message': {'content': 'ok'}}],
                    'usage': {'prompt_tokens': 1, 'completion_tokens': 2}}

        with mock.patch.object(client, 'fetch_token', new=fake_token), \
                mock.patch.object(client, 'chat', new=fake_chat):
            asyncio.run(self.adapter.chat(
                'model-text', {'messages': [{'role': 'user', 'content': 'hi'}]}))
            asyncio.run(self.adapter.chat(
                'model-text', {'messages': [{'role': 'user', 'content': 'hi'}]}))
        self.assertEqual(len(calls), 1)

    def test_chat_retries_once_after_401(self) -> None:
        self._seed()
        tokens = iter(['t-old', 't-new'])
        seen: list[str] = []

        async def fake_token(cookie, **kwargs):
            return {'token': next(tokens), 'expired_at': '2099-01-01T00:00:00Z'}

        async def fake_chat(token, body, **kwargs):
            seen.append(token)
            if token == 't-old':
                raise PlatformError('token 失效', status=401, code='token_expired')
            return {'choices': [{'message': {'content': 'ok'}}],
                    'usage': {'prompt_tokens': 1, 'completion_tokens': 2}}

        with mock.patch.object(client, 'fetch_token', new=fake_token), \
                mock.patch.object(client, 'chat', new=fake_chat):
            result = asyncio.run(self.adapter.chat(
                'model-text', {'messages': [{'role': 'user', 'content': 'hi'}]}))
        self.assertEqual(seen, ['t-old', 't-new'])
        self.assertEqual(result.payload['choices'][0]['message']['content'], 'ok')

    def test_chat_clamps_max_tokens_to_real_cap(self) -> None:
        self._seed()
        captured: dict = {}

        async def fake_token(cookie, **kwargs):
            return {'token': 't', 'expired_at': '2099-01-01T00:00:00Z'}

        async def fake_chat(token, body, **kwargs):
            captured.update(body)
            return {'choices': []}

        with mock.patch.object(client, 'fetch_token', new=fake_token), \
                mock.patch.object(client, 'chat', new=fake_chat):
            asyncio.run(self.adapter.chat('model-text', {
                'messages': [{'role': 'user', 'content': 'hi'}],
                'max_tokens': 1_000_000,
            }))
        self.assertEqual(captured['max_tokens'], C.OUTPUT_CAPS['model-text'])

    def test_scan_sse_extracts_usage_and_content(self) -> None:
        usage: dict = {}
        pending, saw, error = _scan_sse(
            b'data: {"choices":[{"delta":{"content":"hi"}}]}\n'
            b'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":4}}\n',
            usage)
        self.assertTrue(saw)
        self.assertEqual(error, '')
        self.assertEqual(pending, b'')
        self.assertEqual(usage['prompt_tokens'], 3)

    def test_scan_sse_keeps_partial_line(self) -> None:
        usage: dict = {}
        pending, saw, _ = _scan_sse(b'data: {"choices":[{"delta":{"con', usage)
        self.assertEqual(pending, b'data: {"choices":[{"delta":{"con')
        self.assertFalse(saw)

    # ── 签到 / 积分 ───────────────────────────────────────
    def test_checkin_skips_claim_when_already_issued(self) -> None:
        self._seed()

        async def fake_quota(cookie, **kwargs):
            return {'left': 120, 'total': 200, 'used': 80}

        async def fake_info(cookie, **kwargs):
            return {'has_issued': True, 'total_times': 5,
                    'sign_in_days': [1, 2], 'month_points': 20}

        async def fail_claim(cookie, **kwargs):
            raise AssertionError('already issued should not claim')

        with mock.patch.object(client, 'quota_overview', new=fake_quota), \
                mock.patch.object(client, 'login_bonus_info', new=fake_info), \
                mock.patch.object(client, 'claim_login_bonus', new=fail_claim):
            result = asyncio.run(self.adapter.checkin('dm_1'))
        self.assertTrue(result['ok'])
        self.assertTrue(result['already'])
        self.assertEqual(result['credits'], 120)
        self.assertEqual(result['total_times'], 5)
        self.assertEqual(store.read_account('dm_1')['checkin']['total_times'], 5)

    def test_credits_maps_quota_overview(self) -> None:
        self._seed()

        async def fake_quota(cookie, **kwargs):
            return {'total': 100, 'used': 30, 'left': 70, 'subscribed': True,
                    'throttled': False,
                    'packages': [{'kind': 'subscription', 'left': 70,
                                  'expire_at': 1765000000000,
                                  'points_expire_at': 1765000000000,
                                  'source': 'monthly'}]}

        with mock.patch.object(client, 'quota_overview', new=fake_quota):
            out = asyncio.run(self.adapter.credits('dm_1'))
        self.assertEqual(out['credits'], 70)
        self.assertEqual(out['limit'], 100)
        self.assertEqual(out['used'], 30)
        self.assertEqual(out['unit'], 'points')
        self.assertEqual(out['next_expire_at'], 1765000000000)
        self.assertTrue(out['subscribed'])

    def test_usage_collects_errors_without_raising(self) -> None:
        self._seed()

        async def fake_quota(cookie, **kwargs):
            raise PlatformError('登录态已失效', status=401,
                                code='login_expired')

        with mock.patch.object(client, 'quota_overview', new=fake_quota):
            out = asyncio.run(self.adapter.usage('dm_1'))
        self.assertIsNone(out['session_usage'])
        self.assertEqual(out['errors'][0]['code'], 'login_expired')


if __name__ == '__main__':
    unittest.main()
