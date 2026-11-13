"""平台运行参数：请求超时、换号次数与平台专属开关。

## 钉住的问题

这些值曾经散落在适配器常量里（`CHAT_READ_TIMEOUT` / `_MAX_ACCOUNTS_TRY` /
`AFFINITY_TTL`），改一次要重新发布。现在它们按平台存进 `settings` 表，界面上
「上游配置」跟随顶部平台切换。本文件保证：

  · 默认值仍然是之前那套保守值；
  · 保存只影响提交的字段，改 Trae 不影响 Qoder；
  · 平台不支持的能力会明确拒绝，而不是静默丢弃让界面以为保存成功；
  · 适配器真的读到了这些值（不是只写进库、界面上好看）。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db  # noqa: E402
from server.platforms import settings as runtime_settings  # noqa: E402
from server.platforms.trae.adapter import TraeAdapter  # noqa: E402
from server.routers import platforms as platforms_router  # noqa: E402


class _DbCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'runtime.db'
        db._conn = None
        db.connect()

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass


class DefaultsTest(_DbCase):
    def test_defaults_match_the_old_constants(self) -> None:
        values = runtime_settings.load('trae')
        self.assertEqual(values['request_timeout'], 300)
        self.assertEqual(values['idle_timeout'], 60)
        self.assertEqual(values['max_accounts_try'], 3)
        self.assertTrue(values['session_affinity'])
        self.assertEqual(values['session_affinity_ttl'], 30 * 60)
        self.assertTrue(values['desktop_sync'])
        self.assertEqual(values['runtime_limits']['request_timeout'],
                         {'min': 10, 'max': 900})

    def test_capabilities_are_per_platform(self) -> None:
        trae = runtime_settings.capabilities('trae')
        qoder = runtime_settings.capabilities('qoder')
        self.assertTrue(trae['max_accounts_try'])
        self.assertTrue(trae['session_affinity'])
        self.assertFalse(qoder['max_accounts_try'])
        self.assertFalse(qoder['session_affinity'])
        self.assertTrue(qoder['request_timeout'])

    def test_unknown_platform_gets_the_generic_two(self) -> None:
        caps = runtime_settings.capabilities('fake')
        self.assertTrue(caps['request_timeout'])
        self.assertTrue(caps['idle_timeout'])
        self.assertFalse(caps['max_accounts_try'])


class SaveTest(_DbCase):
    def test_partial_update_keeps_other_fields(self) -> None:
        runtime_settings.save('trae', {'request_timeout': 120,
                                       'idle_timeout': 30,
                                       'max_accounts_try': 5,
                                       'session_affinity': False})
        runtime_settings.save('trae', {'request_timeout': 60})
        values = runtime_settings.load('trae')
        self.assertEqual(values['request_timeout'], 60)
        self.assertEqual(values['idle_timeout'], 30)
        self.assertEqual(values['max_accounts_try'], 5)
        self.assertFalse(values['session_affinity'])

    def test_platforms_are_isolated(self) -> None:
        runtime_settings.save('trae', {'request_timeout': 42})
        self.assertEqual(runtime_settings.load('qoder')['request_timeout'], 300)
        self.assertEqual(runtime_settings.load('trae')['request_timeout'], 42)

    def test_rejects_invalid_values(self) -> None:
        bad_payloads = (
            {'request_timeout': 9},
            {'request_timeout': 901},
            {'request_timeout': 'x'},
            {'request_timeout': True},
            {'idle_timeout': 9},
            {'max_accounts_try': 0},
            {'max_accounts_try': 11},
            {'session_affinity': 'yes'},
            {'session_affinity_ttl': 59},
        )
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    runtime_settings.save('trae', payload)

    def test_unsupported_field_is_rejected_not_dropped(self) -> None:
        """Qoder 没有换号 / 亲和开关：提交了要 400，不能「保存成功但没变化」。"""
        with self.assertRaises(ValueError):
            runtime_settings.save('qoder', {'max_accounts_try': 4})
        with self.assertRaises(ValueError):
            runtime_settings.save('qoder', {'session_affinity': False})

    def test_broken_stored_value_falls_back_to_default(self) -> None:
        db.set_setting('platforms.trae.request_timeout', 9999)
        self.assertEqual(runtime_settings.load('trae')['request_timeout'], 300)
        db.set_setting('platforms.trae.session_affinity', 'no')
        self.assertTrue(runtime_settings.load('trae')['session_affinity'])

    def test_configured_tells_unset_from_default(self) -> None:
        self.assertFalse(runtime_settings.configured('qoder', 'request_timeout'))
        runtime_settings.save('qoder', {'request_timeout': 120})
        self.assertTrue(runtime_settings.configured('qoder', 'request_timeout'))


class RouteTest(_DbCase):
    def test_payload_includes_runtime_fields(self) -> None:
        class _Adapter:
            name = 'trae'
            label = 'Trae'

        with mock.patch.object(platforms_router, '_adapter',
                               return_value=_Adapter()):
            out = platforms_router.platform_settings('trae', user={'role': 'admin'})

        for field in ('request_timeout', 'idle_timeout', 'max_accounts_try',
                      'session_affinity', 'session_affinity_ttl', 'desktop_sync'):
            self.assertIn(field, out)
        self.assertIn('capabilities', out)
        self.assertIn('checkin_concurrency', out)

    def test_runtime_fields_save_without_max_in_flight(self) -> None:
        class _Adapter:
            name = 'trae'
            label = 'Trae'

        fake_security = mock.Mock()
        with mock.patch.object(platforms_router, '_adapter',
                               return_value=_Adapter()), \
             mock.patch.object(platforms_router, 'security', fake_security):
            out = platforms_router.save_platform_settings(
                'trae', {'request_timeout': 90, 'max_accounts_try': 2},
                user={'role': 'admin'})

        self.assertEqual(out['request_timeout'], 90)
        self.assertEqual(out['max_accounts_try'], 2)
        fake_security.audit.assert_called_once()


class TraeWiringTest(_DbCase):
    def setUp(self) -> None:
        super().setUp()
        self.adapter = TraeAdapter()

    def _run_chat(self, body: dict):
        account = {'uid': 'u1', 'realm': 'cn'}
        with mock.patch.object(self.adapter, '_candidates',
                               return_value=[account]) as candidates, \
             mock.patch.object(self.adapter, '_ensure_token',
                               mock.AsyncMock(side_effect=lambda a: a)), \
             mock.patch.object(self.adapter, '_chat_once',
                               mock.AsyncMock(return_value='ok')) as chat_once:
            result = asyncio.run(self.adapter._chat_accounts('m', body))
        return result, candidates, chat_once

    def test_runtime_values_reach_the_chat_call(self) -> None:
        runtime_settings.save('trae', {'request_timeout': 120,
                                       'idle_timeout': 30,
                                       'max_accounts_try': 1})
        result, _, chat_once = self._run_chat({'prompt_cache_key': 's1'})
        self.assertEqual(result, 'ok')
        kwargs = chat_once.call_args.kwargs
        self.assertEqual(kwargs['request_timeout'], 120)
        self.assertEqual(kwargs['idle_timeout'], 30)

    def test_session_affinity_can_be_turned_off(self) -> None:
        runtime_settings.save('trae', {'session_affinity': False})
        _, candidates, _ = self._run_chat({'prompt_cache_key': 's1'})
        # 关掉后不该把会话键传给选路：每个请求都按权重重新轮换。
        self.assertEqual(candidates.call_args.args[2], '')

    def test_session_affinity_is_on_by_default(self) -> None:
        _, candidates, _ = self._run_chat({'prompt_cache_key': 's1'})
        self.assertEqual(candidates.call_args.args[2], 'prompt_cache_key:s1')

    def test_account_try_limit_is_applied(self) -> None:
        runtime_settings.save('trae', {'max_accounts_try': 1})
        accounts = [{'uid': f'u{i}', 'realm': 'cn'} for i in range(3)]
        chat_once = mock.AsyncMock(side_effect=RuntimeError('fail'))
        with mock.patch.object(self.adapter, '_candidates',
                               return_value=accounts), \
             mock.patch.object(self.adapter, '_ensure_token',
                               mock.AsyncMock(side_effect=lambda a: a)), \
             mock.patch.object(self.adapter, '_chat_once', chat_once):
            with self.assertRaises(RuntimeError):
                asyncio.run(self.adapter._chat_accounts('m', {}))
        # 只尝试了 1 个账号：不会把所有候选都打一遍。
        self.assertEqual(chat_once.call_count, 1)


if __name__ == '__main__':
    unittest.main()
