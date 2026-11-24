"""DuMate（百度搭子）平台：账号存储、模型目录、适配器策略。

全部离线：token / 对话 / 签到 / 积分 / 成长任务 / 抽奖 / 邀请码都替换成假客户端，
测的是「上游字段 → token-farm 形状」的翻译、选号 / 续期策略与任务编排。
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

import httpx

from server import config, db
from server.platforms.base import PlatformError
from server.platforms.dumate import catalog, client, constants as C, login, store
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

    def test_import_keeps_login_state_from_web_login(self) -> None:
        """扫码登录在探活失败时会带「待验证 + 原因」导入，不能被清掉。"""
        public = self.adapter.import_account({
            'cookie': 'BDUSS=abc123efg', 'source': 'web',
            'loginState': '待验证', 'lastRefreshError': '上游 502',
        })
        raw = store.read_account(public['uid'])
        self.assertEqual(raw['source'], 'web')
        self.assertEqual(raw['loginState'], '待验证')
        self.assertEqual(raw['lastRefreshError'], '上游 502')
        self.assertEqual(public['login_state'], '待验证')

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

    def test_list_models_exposes_efforts_for_known_backend(self) -> None:
        models = {m['model_name']: m for m in asyncio.run(self.adapter.list_models())}
        self.assertEqual(models['deepseek-v41-flash']['efforts'],
                         ['none', 'high', 'max'],
                         '`low` 归一成 `high`，对外不再单列')
        self.assertEqual(models['glm-53']['efforts'], [],
                         '非白名单后端不显示档位选择器')

    # ── 选号 ──────────────────────────────────────────────
    def test_candidates_skip_disabled_and_cooling(self) -> None:
        self._seed(uid='dm_1')
        self._seed(uid='dm_2', disabled=True)
        self._seed(uid='dm_3', coolingUntil=time.time() + 600)
        ids = [a['uid'] for a in self.adapter._candidates()]
        self.assertEqual(ids, ['dm_1'])

    def test_candidates_skip_checkin_only(self) -> None:
        self._seed(uid='dm_1')
        self._seed(uid='dm_2', checkinOnly=True)
        ids = [a['uid'] for a in self.adapter._candidates()]
        self.assertEqual(ids, ['dm_1'])

    def test_checkin_only_state_roundtrip(self) -> None:
        """仅签到与完全停用互斥：开一个必须清掉另一个。"""
        self._seed(uid='dm_1')
        only = store.set_checkin_only('dm_1', True)
        self.assertTrue(only['checkin_only'])
        self.assertFalse(only['disabled'])

        frozen = store.set_disabled('dm_1', True, '完全停用')
        self.assertTrue(frozen['disabled'])
        self.assertFalse(frozen['checkin_only'])

        only_again = store.set_checkin_only('dm_1', True)
        self.assertTrue(only_again['checkin_only'])
        self.assertFalse(only_again['disabled'])

        off = store.set_checkin_only('dm_1', False)
        self.assertFalse(off['checkin_only'])

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

    def test_chat_collapses_token_fields_to_single_max_tokens(self) -> None:
        """两个字段同发会被上游 400；只发 mct 会被静默忽略。"""
        self._seed()
        captured: dict = {}

        async def fake_token(cookie, **kwargs):
            return {'token': 't', 'expired_at': '2099-01-01T00:00:00Z'}

        async def fake_chat(token, body, **kwargs):
            captured.update(body)
            return {'choices': []}

        with mock.patch.object(client, 'fetch_token', new=fake_token), \
                mock.patch.object(client, 'chat', new=fake_chat):
            asyncio.run(self.adapter.chat('glm-53', {
                'messages': [{'role': 'user', 'content': 'hi'}],
                'max_tokens': 1000, 'max_completion_tokens': 4096,
            }))
        self.assertNotIn('max_completion_tokens', captured)
        self.assertEqual(captured['max_tokens'], 4096, '两值不等取较大者')

    def test_chat_maps_max_completion_tokens_only(self) -> None:
        self._seed()
        captured: dict = {}

        async def fake_token(cookie, **kwargs):
            return {'token': 't', 'expired_at': '2099-01-01T00:00:00Z'}

        async def fake_chat(token, body, **kwargs):
            captured.update(body)
            return {'choices': []}

        with mock.patch.object(client, 'fetch_token', new=fake_token), \
                mock.patch.object(client, 'chat', new=fake_chat):
            asyncio.run(self.adapter.chat('glm-53', {
                'messages': [{'role': 'user', 'content': 'hi'}],
                'max_completion_tokens': 2048,
            }))
        self.assertEqual(captured['max_tokens'], 2048)
        self.assertNotIn('max_completion_tokens', captured)

    def test_chat_normalizes_reasoning_effort_on_known_backend(self) -> None:
        self._seed()
        captured: dict = {}

        async def fake_token(cookie, **kwargs):
            return {'token': 't', 'expired_at': '2099-01-01T00:00:00Z'}

        async def fake_chat(token, body, **kwargs):
            captured.update(body)
            return {'choices': []}

        with mock.patch.object(client, 'fetch_token', new=fake_token), \
                mock.patch.object(client, 'chat', new=fake_chat):
            asyncio.run(self.adapter.chat('deepseek-v41-flash', {
                'messages': [{'role': 'user', 'content': 'hi'}],
                'reasoning_effort': 'none',
            }))
        self.assertNotIn('reasoning_effort', captured)
        self.assertEqual(captured['thinking'], {'type': 'disabled'})
        self.assertIs(captured['enable_thinking'], False)

    def test_chat_leaves_effort_alone_on_other_backends(self) -> None:
        self._seed()
        captured: dict = {}

        async def fake_token(cookie, **kwargs):
            return {'token': 't', 'expired_at': '2099-01-01T00:00:00Z'}

        async def fake_chat(token, body, **kwargs):
            captured.update(body)
            return {'choices': []}

        with mock.patch.object(client, 'fetch_token', new=fake_token), \
                mock.patch.object(client, 'chat', new=fake_chat):
            asyncio.run(self.adapter.chat('glm-53', {
                'messages': [{'role': 'user', 'content': 'hi'}],
                'reasoning_effort': 'none',
            }))
        self.assertEqual(captured['reasoning_effort'], 'none',
                         '非白名单模型的档位原样透传，不注入 thinking')
        self.assertNotIn('thinking', captured)
        self.assertNotIn('enable_thinking', captured)

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
        # 上游给毫秒，对外契约是 epoch 秒；字段名是 amount（不是 credits）。
        self.assertEqual(out['expires'], [
            {'amount': 70, 'expire_at': 1765000000, 'label': 'monthly'}])
        self.assertEqual(out['next_expire_at'], 1765000000)
        self.assertTrue(out['subscribed'])

    # ── 平台任务（成长任务 / 抽奖 / 邀请码）────────────────
    def test_supports_tasks(self) -> None:
        self.assertTrue(self.adapter.supports_tasks())

    def test_task_status_merges_sources_and_caches(self) -> None:
        self._seed()
        created_at = int(time.time()) - 86400

        async def fake_tasks(cookie, **kwargs):
            return {'groups': [{'group_code': 'g1', 'group_name': '每日',
                                'tasks': [{'task_id': 't1', 'task_type': 'QUERY_INPUT',
                                           'title': '问一句', 'done': False,
                                           'auto': True, 'repeat_count': 1,
                                           'completed_count': 0}]}],
                    'tasks': [{'task_id': 't1', 'task_type': 'QUERY_INPUT',
                               'title': '问一句', 'done': False, 'auto': True,
                               'repeat_count': 1, 'completed_count': 0,
                               'raw': {'task_id': 't1', 'secret': 'x'}}]}

        async def fake_draw(cookie, **kwargs):
            return {'remaining_draws': 2, 'prizes': [{'prize_name': '积分'}],
                    'my_prizes': [], 'winning_records': []}

        async def fake_invite(cookie, **kwargs):
            return {'invitation_code': 'ABC123', 'type': 'normal',
                    'created_at': created_at, 'invite_message': '来玩'}

        async def fake_count(cookie, **kwargs):
            return {'invited_count': 3, 'total_points': 6000}

        with mock.patch.object(client, 'tasks', new=fake_tasks), \
                mock.patch.object(client, 'draw_status', new=fake_draw), \
                mock.patch.object(client, 'invitation_code', new=fake_invite), \
                mock.patch.object(client, 'invited_count', new=fake_count):
            out = asyncio.run(self.adapter.task_status('dm_1'))

        self.assertEqual(out['tasks'][0]['task_id'], 't1')
        self.assertNotIn('raw', out['tasks'][0], '协议层的 raw 不往接口吐')
        self.assertEqual(out['task_groups'][0]['group_name'], '每日')
        self.assertEqual(out['draw']['remaining_draws'], 2)
        self.assertEqual(out['invite']['invitation_code'], 'ABC123')
        self.assertEqual(out['invite']['invited_count'], 3)
        self.assertTrue(out['invite']['within_7_days'])
        self.assertEqual(out['invite_codes'][0]['inviteCode'], 'ABC123')
        self.assertEqual(out['errors'], [])
        cached = store.read_account('dm_1')['taskCache']
        self.assertEqual(cached['tasks'][0]['task_id'], 't1')
        self.assertEqual(cached['draw']['remaining_draws'], 2)
        self.assertEqual(cached['invite']['invitation_code'], 'ABC123')

    def test_task_status_surfaces_checkin_from_account_page(self) -> None:
        """签到发生在账号页（批量签到）时，任务面板也要显示上次结果。"""
        self._seed(checkin={'last_at': 1700000000,
                            'last_result': '签到成功 +5'},
                   points={'left': 120, 'at': 1700000000})

        async def empty_tasks(cookie, **kwargs):
            return {'tasks': [], 'groups': []}

        async def empty_draw(cookie, **kwargs):
            return {'remaining_draws': 0, 'prizes': [], 'my_prizes': [],
                    'winning_records': []}

        async def empty_invite(cookie, **kwargs):
            return {'invitation_code': '', 'type': '', 'created_at': 0,
                    'invite_message': ''}

        async def empty_count(cookie, **kwargs):
            return {'invited_count': 0, 'total_points': 0}

        with mock.patch.object(client, 'tasks', new=empty_tasks), \
                mock.patch.object(client, 'draw_status', new=empty_draw), \
                mock.patch.object(client, 'invitation_code', new=empty_invite), \
                mock.patch.object(client, 'invited_count', new=empty_count):
            out = asyncio.run(self.adapter.task_status('dm_1'))

        self.assertTrue(out['checkin']['ok'])
        self.assertEqual(out['checkin']['balance'], 120)
        self.assertIn('签到成功', out['checkin']['message'])

    def test_task_status_falls_back_to_cache_when_all_reads_fail(self) -> None:
        cache = {
            'tasks': [{'task_id': 'old', 'title': '缓存任务'}],
            'taskGroups': [], 'draw': {'remaining_draws': 1},
            'invite': {'invitation_code': 'OLD'}, 'syncedAt': 1700000000,
        }
        self._seed(taskCache=cache)

        async def boom(*args, **kwargs):
            raise PlatformError('登录态已失效', status=401, code='login_expired')

        with mock.patch.object(client, 'tasks', new=boom), \
                mock.patch.object(client, 'draw_status', new=boom), \
                mock.patch.object(client, 'invitation_code', new=boom), \
                mock.patch.object(client, 'invited_count', new=boom):
            out = asyncio.run(self.adapter.task_status('dm_1'))

        self.assertEqual(out['tasks'][0]['task_id'], 'old')
        self.assertEqual(out['draw']['remaining_draws'], 1)
        self.assertEqual(out['synced_at'], 1700000000)
        self.assertEqual(len(out['errors']), 4)

    def test_complete_tasks_auto_runs_and_treats_410121_as_done(self) -> None:
        self._seed()
        tasks_data = {
            'tasks': [
                {'task_id': 't1', 'task_type': 'QUERY_INPUT', 'title': '提问',
                 'auto': True, 'done': False, 'repeat_count': 1,
                 'completed_count': 0},
                {'task_id': 't2', 'task_type': 'QUERY_INPUT', 'title': '再问',
                 'auto': True, 'done': False, 'repeat_count': 1,
                 'completed_count': 0},
                {'task_id': 't3', 'task_type': 'PC_PUSH', 'title': '推送到桌面',
                 'auto': False, 'done': False, 'repeat_count': 1,
                 'completed_count': 0},
            ],
            'groups': [],
        }
        sent: list[str] = []

        async def fake_tasks(cookie, **kwargs):
            return tasks_data

        async def fake_complete(cookie, task_id, **kwargs):
            if task_id == 't2':
                raise PlatformError('该任务次数已发放', status=502,
                                    code='web_biz_failed',
                                    payload={'code': C.TASK_ALREADY_GRANTED_CODE})
            return {'result': {'reward_count': 1, 'reward_points': 100}}

        async def fake_query(account, task, **kwargs):
            sent.append(task['task_id'])

        with mock.patch.object(client, 'tasks', new=fake_tasks), \
                mock.patch.object(client, 'complete_task', new=fake_complete), \
                mock.patch.object(self.adapter, '_send_task_query', new=fake_query):
            out = asyncio.run(self.adapter.run_task('dm_1', 'complete_task'))

        self.assertEqual(sent, ['t1', 't2'], '两条自动任务各先发一条消息')
        self.assertEqual(out['done_count'], 2)
        self.assertEqual(out['already_count'], 1)
        self.assertEqual(out['fail_count'], 0, '一键完成只跑可自动任务，跳过的不算失败')
        by_id = {item['task_id']: item for item in out['results']}
        self.assertTrue(by_id['t1']['ok'])
        self.assertTrue(by_id['t2']['already'])
        self.assertNotIn('t3', by_id, '需要客户端的任务不发起完成请求')

    def test_complete_tasks_rejects_unknown_task_id(self) -> None:
        self._seed()

        async def fake_tasks(cookie, **kwargs):
            return {'tasks': [], 'groups': []}

        with mock.patch.object(client, 'tasks', new=fake_tasks):
            with self.assertRaises(PlatformError) as ctx:
                asyncio.run(self.adapter.run_task(
                    'dm_1', 'complete_task', task_id='missing'))
        self.assertEqual(ctx.exception.code, 'task_not_found')

    def test_draw_uses_fresh_request_id_and_returns_prize(self) -> None:
        self._seed()
        keys: list[str] = []

        async def fake_status(cookie, **kwargs):
            return {'remaining_draws': 2, 'prizes': [], 'my_prizes': [],
                    'winning_records': []}

        async def fake_draw(cookie, request_id, **kwargs):
            keys.append(request_id)
            return {'draw_record_id': f'dr-{len(keys)}', 'remaining_draws': 1,
                    'prize_name': '会员 3 天', 'prize_type': 'VIRTUAL',
                    'prize': {'prize_name': '会员 3 天', 'prize_type': 'VIRTUAL'},
                    'raw': {}}

        with mock.patch.object(client, 'draw_status', new=fake_status), \
                mock.patch.object(client, 'draw', new=fake_draw):
            first = asyncio.run(self.adapter.run_task('dm_1', 'draw'))
            second = asyncio.run(self.adapter.run_task('dm_1', 'draw'))

        self.assertEqual(len(keys), 2)
        self.assertNotEqual(keys[0], keys[1], '幂等键必须每次新生成')
        self.assertEqual(first['prize_name'], '会员 3 天')
        self.assertFalse(first['needs_claim'])
        self.assertEqual(second['draw_record_id'], 'dr-2')

    def test_draw_rejects_empty_pool(self) -> None:
        self._seed()

        async def fake_status(cookie, **kwargs):
            return {'remaining_draws': 0, 'prizes': [], 'my_prizes': [],
                    'winning_records': []}

        async def fail_draw(*args, **kwargs):
            raise AssertionError('没有次数时不该请求上游')

        with mock.patch.object(client, 'draw_status', new=fake_status), \
                mock.patch.object(client, 'draw', new=fail_draw):
            with self.assertRaises(PlatformError) as ctx:
                asyncio.run(self.adapter.run_task('dm_1', 'draw'))
        self.assertEqual(ctx.exception.code, 'no_draws')

    def test_draw_body_carries_uuid_in_both_keys(self) -> None:
        """网页 bundle 用 `x-bce-request-id`，参考实现用 `request_id`；两个都带。"""
        seen: dict = {}

        async def fake_web(method, path, cookie, *, proxy=None, timeout=20,
                           body=None):
            seen.update({'method': method, 'path': path, 'body': body})
            return {'code': 0, 'result': {
                'draw_record_id': 'dr-1', 'remaining_draws': 2,
                'prize_name': '会员 3 天', 'prize_type': 'VIRTUAL'}}

        with mock.patch.object(client, '_web_json', new=fake_web):
            out = asyncio.run(client.draw('cookie', 'uuid-1'))

        self.assertEqual(seen['method'], 'POST')
        self.assertEqual(seen['path'], C.DRAW_PATH)
        self.assertEqual(seen['body']['x-bce-request-id'], 'uuid-1')
        self.assertEqual(seen['body']['request_id'], 'uuid-1')
        self.assertEqual(seen['body']['source'], 'web')
        self.assertEqual(out['prize_name'], '会员 3 天')
        self.assertEqual(out['prize_type'], 'VIRTUAL')

    def test_normalize_task_uses_count_ratio_and_auto_types(self) -> None:
        done = client._normalize_task(
            {'task_id': 't1', 'task_type': 'QUERY_INPUT', 'title': '提问',
             'repeat_count': '2', 'completed_count': 2, 'reward_points': '100'},
            {'group_code': 'g1', 'group_name': '每日'})
        self.assertTrue(done['done'])
        self.assertTrue(done['auto'])
        self.assertEqual(done['repeat_count'], 2)
        self.assertEqual(done['reward_points'], 100)
        self.assertEqual(done['group_name'], '每日')

        pending = client._normalize_task(
            {'task_id': 't2', 'task_type': 'PC_PUSH', 'repeat_count': 0},
            {})
        self.assertEqual(pending['repeat_count'], 1, 'repeat_count 缺省按 1')
        self.assertFalse(pending['done'])
        self.assertFalse(pending['auto'], '需要客户端的任务不代跑')

    def test_draw_allows_when_remaining_is_unknown(self) -> None:
        """上游没回次数不等于 0 次：不能因此把抽奖按钮锁死。"""
        self._seed()

        async def fake_status(cookie, **kwargs):
            return {'remaining_draws': None, 'prizes': [], 'my_prizes': [],
                    'winning_records': []}

        async def fake_draw(cookie, request_id, **kwargs):
            return {'draw_record_id': 'dr-1', 'remaining_draws': 1,
                    'prize_name': '积分', 'prize_type': 'POINTS'}

        with mock.patch.object(client, 'draw_status', new=fake_status), \
                mock.patch.object(client, 'draw', new=fake_draw):
            out = asyncio.run(self.adapter.run_task('dm_1', 'draw'))
        self.assertEqual(out['draw_record_id'], 'dr-1')
        self.assertEqual(out['prize_name'], '积分')

    def test_draw_status_missing_count_stays_none(self) -> None:
        async def fake_web(method, path, cookie, *, proxy=None, timeout=20,
                           body=None):
            return {'code': 0, 'result': {'prizes': [], 'my_prizes': [],
                                          'winning_records': []}}

        with mock.patch.object(client, '_web_json', new=fake_web):
            out = asyncio.run(client.draw_status('cookie'))
        self.assertIsNone(out['remaining_draws'], '缺字段不能被读成 0 次')

    def test_bind_invite_rejects_own_code(self) -> None:
        self._seed()

        async def fake_invite(cookie, **kwargs):
            return {'invitation_code': 'MINE', 'type': '', 'created_at': 0,
                    'invite_message': ''}

        async def fail_redeem(*args, **kwargs):
            raise AssertionError('自己的邀请码不该发到上游')

        with mock.patch.object(client, 'invitation_code', new=fake_invite), \
                mock.patch.object(client, 'redeem_invitation_code',
                                  new=fail_redeem):
            with self.assertRaises(PlatformError) as ctx:
                asyncio.run(self.adapter.run_task(
                    'dm_1', 'bind_invite', invite_code='MINE'))
        self.assertEqual(ctx.exception.code, 'self_invite_code')

    def test_bind_invite_redeems_and_clears_credits(self) -> None:
        self._seed()
        self.adapter._credits['dm_1'] = (time.time() + 60, {'credits': 1})
        seen: dict = {}

        async def fake_invite(cookie, **kwargs):
            return {'invitation_code': 'MINE', 'type': '', 'created_at': 0,
                    'invite_message': ''}

        async def fake_redeem(cookie, code, **kwargs):
            seen['code'] = code
            return {'message': '领取成功，双方各得 2000 积分',
                    'notice': {'content': '领取成功'}, 'raw': {}}

        with mock.patch.object(client, 'invitation_code', new=fake_invite), \
                mock.patch.object(client, 'redeem_invitation_code',
                                  new=fake_redeem):
            out = asyncio.run(self.adapter.run_task(
                'dm_1', 'bind_invite', invite_code=' FRIEND '))

        self.assertEqual(seen['code'], 'FRIEND', '只 strip，不改大小写')
        self.assertEqual(out['invite_code'], 'FRIEND')
        self.assertIn('2000', out['desc'])
        self.assertNotIn('dm_1', self.adapter._credits)

    def test_claim_prize_requires_draw_record_id(self) -> None:
        self._seed()
        with self.assertRaises(PlatformError) as ctx:
            asyncio.run(self.adapter.run_task('dm_1', 'claim_prize'))
        self.assertEqual(ctx.exception.code, 'missing_draw_record_id')

    def test_claim_prize_passes_contact_and_clears_credits(self) -> None:
        self._seed()
        self.adapter._credits['dm_1'] = (time.time() + 60, {'credits': 1})
        seen: dict = {}

        async def fake_claim(cookie, record, *, contact='', **kwargs):
            seen.update({'record': record, 'contact': contact})
            return {'result': {}, 'raw': {'code': 0}}

        with mock.patch.object(client, 'claim_prize', new=fake_claim):
            out = asyncio.run(self.adapter.run_task(
                'dm_1', 'claim_prize', draw_record_id='dr-9',
                contact=' 13800000000 '))

        self.assertEqual(seen, {'record': 'dr-9', 'contact': '13800000000'})
        self.assertTrue(out['ok'])
        self.assertEqual(out['action'], 'claim_prize')
        self.assertNotIn('dm_1', self.adapter._credits)

    def test_usage_collects_errors_without_raising(self) -> None:
        self._seed()

        async def fake_quota(cookie, **kwargs):
            raise PlatformError('登录态已失效', status=401,
                                code='login_expired')

        with mock.patch.object(client, 'quota_overview', new=fake_quota):
            out = asyncio.run(self.adapter.usage('dm_1'))
        self.assertIsNone(out['session_usage'])
        self.assertEqual(out['errors'][0]['code'], 'login_expired')

    # ── 网页登录 ──────────────────────────────────────────
    def test_web_login_is_enabled_and_wired(self) -> None:
        self.assertTrue(self.adapter.supports_web_login)
        with mock.patch.object(login, 'start',
                               return_value={'login_id': 'x',
                                             'status': 'pending'}) as starter:
            self.assertEqual(self.adapter.login_start()['login_id'], 'x')
        starter.assert_called_once_with()


class DumateLoginTest(unittest.TestCase):
    """扫码登录的纯离线部分：事件解析、兑换解析、会话状态机。"""

    def setUp(self) -> None:
        self._sessions = dict(login._sessions)
        login._sessions.clear()
        self.addCleanup(login._sessions.update, self._sessions)
        self.addCleanup(login._sessions.clear)

    def test_parse_exchange_reads_mixed_quote_body(self) -> None:
        body = ("{\"errInfo\":{\"no\":\"0\",\"msg\":\"\"},"
                "\"data\":{\"session\":{'bduss':'BDUSS-1','stoken':'ST-1',"
                "'ptoken':'PT-1'}}}")
        out = login.parse_exchange(body)
        self.assertEqual(out['bduss'], 'BDUSS-1')
        self.assertEqual(out['stoken'], 'ST-1')
        self.assertEqual(out['ptoken'], 'PT-1')

    def test_parse_exchange_reports_upstream_error(self) -> None:
        with self.assertRaises(PlatformError) as ctx:
            login.parse_exchange('{"errInfo":{"no":"12","msg":"验证信息已过期"}}')
        self.assertEqual(ctx.exception.code, 'qr_login_failed')
        self.assertIn('验证信息已过期', ctx.exception.message)

    def test_fetch_qrcode_inlines_png_for_csp(self) -> None:
        """二维码必须内联：面板的 CSP 只允许 self/data/blob 的图片。"""
        http = _QrHttp(json.dumps({'sign': 's1',
                                   'imgurl': '//passport.baidu.com/x.png'}),
                       b'\x89PNG\r\n\x1a\n')
        out = login.fetch_qrcode(http)
        self.assertEqual(out['sign'], 's1')
        self.assertTrue(out['qr'].startswith('data:image/png;base64,'))

    def test_fetch_qrcode_falls_back_to_url(self) -> None:
        http = _QrHttp(json.dumps({'sign': 's2',
                                   'imgurl': 'passport.baidu.com/x.png'}),
                       b'', fail=True)
        out = login.fetch_qrcode(http)
        self.assertEqual(out['qr'], 'https://passport.baidu.com/x.png')

    def test_exchange_falls_back_to_set_cookie(self) -> None:
        """兑换响应只下发 Set-Cookie（没有 BDUSS 字段）时也要接住。"""
        cookies = httpx.Cookies()
        cookies.set('BDUSS', 'jar-bduss', domain='.baidu.com', path='/')
        http = mock.Mock()
        http.get.return_value = _FakeResponse('{"errInfo":{"no":"12"}}')
        http.cookies = cookies
        out = login.exchange(http, 'temp')
        self.assertEqual(out['bduss'], 'jar-bduss')

    def test_poll_qrcode_accepts_object_channel(self) -> None:
        out = login.poll_qrcode(
            _FakeHttp('{"errno":0,"channel_v":{"status":0,"v":"t"}}'), 'sign')
        self.assertEqual(out, {'status': 'confirmed', 'token': 't'})

    def test_poll_qrcode_states(self) -> None:
        cases = [
            ('{"errno":1}', 'waiting', ''),
            ('{"errno":0,"channel_v":"{\\"status\\":1,\\"v\\":\\"t\\"}"}',
             'waiting', ''),
            ('{"errno":0,"channel_v":"{\\"status\\":0,\\"v\\":\\"temp-bduss\\"}"}',
             'confirmed', 'temp-bduss'),
            ('{"errno":0,"channel_v":"{\\"status\\":2}"}', 'expired', ''),
            ('this_is_callback({"errno":1})', 'waiting', ''),
        ]
        for text, status, token in cases:
            with self.subTest(text=text):
                out = login.poll_qrcode(_FakeHttp(text), 'sign')
                self.assertEqual(out['status'], status)
                self.assertEqual(out['token'], token)

    def test_cookie_header_prefers_more_specific_domain(self) -> None:
        cookies = httpx.Cookies()
        cookies.set('BDUSS', 'broad', domain='.baidu.com', path='/')
        cookies.set('BDUSS', 'specific', domain='.console.bce.baidu.com', path='/')
        cookies.set('bce-long-term-sessionid', 'session',
                    domain='.bce.baidu.com', path='/')
        header = login.cookie_header(cookies)
        self.assertIn('bce-long-term-sessionid=session', header)
        self.assertIn('BDUSS=specific', header)
        self.assertNotIn('BDUSS=broad', header)

    def test_session_lifecycle_without_network(self) -> None:
        with mock.patch.object(login, '_run'):
            view = login.start()
            login_id = view['login_id']
            self.assertEqual(view['status'], 'pending')
            self.assertEqual(view['realm'], 'cn')
            self.assertEqual(login.poll(login_id)['status'], 'pending')
            self.assertTrue(login.cancel(login_id))
        with self.assertRaises(PlatformError):
            login.poll(login_id)


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text
        self.status_code = 200

    def json(self):
        return json.loads(self.text)


class _FakeHttp:
    def __init__(self, text: str) -> None:
        self._text = text

    def get(self, *args, **kwargs) -> _FakeResponse:
        return _FakeResponse(self._text)


class _QrImageResponse:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.status_code = 200
        self.headers = {'content-type': 'image/png'}


class _QrHttp:
    """第一次 GET 回二维码 JSON，第二次 GET 回图片（或抛错）。"""

    def __init__(self, payload: str, image: bytes, *, fail: bool = False) -> None:
        self._payload = payload
        self._image = image
        self._fail = fail

    def get(self, url, *args, **kwargs):
        if 'getqrcode' in str(url):
            return _FakeResponse(self._payload)
        if self._fail:
            raise httpx.ConnectError('no route')
        return _QrImageResponse(self._image)


if __name__ == '__main__':
    unittest.main()
