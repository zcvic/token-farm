"""平台运行参数：请求超时、换号次数与平台专属开关。

## 钉住的问题

这些值曾经散落在适配器常量里（`CHAT_READ_TIMEOUT` / `_MAX_ACCOUNTS_TRY` /
`AFFINITY_TTL`），改一次要重新发布。现在它们按平台存进 `settings` 表，界面上「上游配置」跟随顶部平台切换。本文件保证：

  · 默认值仍然是之前那套保守值；
  · 保存只影响提交的字段，改 Trae 不影响 Qoder；
  · 平台不支持的能力会明确拒绝，而不是静默丢弃让界面以为保存成功；
  · 适配器真的读到了这些值（不是只写进库、界面上好看）。
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db  # noqa: E402
from server.platforms import settings as runtime_settings  # noqa: E402
from server.platforms.base import PlatformChat, PlatformError  # noqa: E402
from server.platforms.qoder import embedded as qoder_embedded  # noqa: E402
from server.platforms.trae import chat as trae_chat  # noqa: E402
from server.platforms.trae import client as trae_client  # noqa: E402
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
        # DuMate 的对话消耗入账默认关闭（旧行为：只在弹窗展示），回补窗口 7 天。
        dumate = runtime_settings.load('dumate')
        self.assertFalse(dumate['usage_ledger'])
        self.assertEqual(dumate['usage_sync_days'], 7)
        self.assertEqual(dumate['runtime_limits']['usage_sync_days'],
                         {'min': 1, 'max': 90})

    def test_trae_cache_and_cooldown_defaults_match_the_old_constants(self) -> None:
        """缓存 TTL / 退避 / 积分查询的默认值就是原先 adapter.py 的常量。"""
        values = runtime_settings.load('trae')
        self.assertEqual(values['catalog_ttl'], 6 * 3600)
        self.assertEqual(values['catalog_retry_ttl'], 600)
        self.assertEqual(values['catalog_timeout'], 10)
        self.assertEqual(values['catalog_backoff_base'], 30)
        self.assertEqual(values['catalog_backoff_max'], 1800)
        self.assertEqual(values['credits_ttl'], 60)
        self.assertEqual(values['credits_timeout'], 20)
        # 0 = 旧行为：429/5xx 直接上抛，不冻结账号。
        self.assertEqual(values['cooldown_429'], 0)
        self.assertEqual(values['cooldown_5xx'], 0)

    def test_env_backed_defaults_follow_config(self) -> None:
        """未保存过时，面板回显的就是 TF_TRAE_* 的现值（不是抄死的常量）。"""
        with mock.patch.object(config, 'TRAE_SOLO_MAX_TOKENS', 123456), \
             mock.patch.object(config, 'TRAE_NATIVE_TOOLS', False), \
             mock.patch.object(config, 'TRAE_CREDIT_METER', 'detail'), \
             mock.patch.object(config, 'LINGXI_CREDIT_METER', 'delta'):
            values = runtime_settings.load('trae')
            self.assertEqual(values['solo_max_tokens'], 123456)
            self.assertFalse(values['native_tools'])
            self.assertEqual(values['credit_meter'], 'detail')
            # 同名开关在不同平台跟随各自的环境变量：灵犀是 TF_LINGXI_*。
            self.assertEqual(runtime_settings.credit_meter('lingxi'), 'delta')

    def test_capabilities_are_per_platform(self) -> None:
        trae = runtime_settings.capabilities('trae')
        qoder = runtime_settings.capabilities('qoder')
        loomy = runtime_settings.capabilities('loomy')
        self.assertTrue(trae['max_accounts_try'])
        self.assertTrue(trae['session_affinity'])
        self.assertFalse(qoder['max_accounts_try'])
        self.assertFalse(qoder['session_affinity'])
        self.assertTrue(qoder['request_timeout'])
        self.assertTrue(runtime_settings.capabilities('lingxi')['credit_meter'])
        # 保底积分只属于 Loomy：同构的 DuMate / MiniMax 没有实现选号时的阈值。
        self.assertTrue(loomy['min_credits'])
        self.assertFalse(runtime_settings.capabilities('dumate')['min_credits'])
        self.assertFalse(runtime_settings.capabilities('minimax')['min_credits'])
        self.assertFalse(trae['min_credits'])
        self.assertFalse(qoder['min_credits'])
        # DuMate 的模型目录读本机客户端快照，本进程不发目录请求：露出「目录
        # 拉取超时」只会是保存成功但不生效的假开关。MiniMax 是真在线拉取。
        self.assertFalse(runtime_settings.capabilities('dumate')['catalog_timeout'])
        self.assertTrue(runtime_settings.capabilities('minimax')['catalog_timeout'])
        # 对话消耗入账是 DuMate 专属：其他平台没有 `/points/records/usage` 口径。
        dumate = runtime_settings.capabilities('dumate')
        self.assertTrue(dumate['usage_ledger'])
        self.assertTrue(dumate['usage_sync_days'])
        for name in ('trae', 'qoder', 'loomy', 'minimax', 'discovery', 'kilo'):
            self.assertFalse(runtime_settings.capabilities(name)['usage_ledger'])
            self.assertFalse(runtime_settings.capabilities(name)['usage_sync_days'])
        # Discovery / Kilo / Cline 共用 `_NATIVE_POOL`：与 Loomy 同一组选号 /
        # 缓存 / 冷却，但都**没有**保底积分（余额口径不是积分），目录则都是
        # 真在线拉取（Discovery 的目录拉取同时是额度探针），所以留 timeout。
        for name in ('discovery', 'kilo', 'cline'):
            caps = runtime_settings.capabilities(name)
            self.assertTrue(caps['max_accounts_try'], name)
            self.assertTrue(caps['session_affinity'], name)
            self.assertTrue(caps['catalog_timeout'], name)
            self.assertTrue(caps['credits_ttl'], name)
            self.assertFalse(caps['min_credits'], name)
            self.assertFalse(caps['desktop_sync'], name)
        # 小浣熊与它们同组：原生选号 / 在线目录 / 积分余额 / 签到，一次性
        # refresh token 由适配器按需轮换。`credit_meter` 不适用（每请求扣 1 分，
        # 账单延迟归账），桌面端只读导入因此没有 `desktop_sync`。
        raccoon = runtime_settings.capabilities('raccoon')
        self.assertTrue(raccoon['max_accounts_try'])
        self.assertTrue(raccoon['session_affinity'])
        self.assertTrue(raccoon['catalog_timeout'])
        self.assertTrue(raccoon['credits_ttl'])
        self.assertFalse(raccoon['min_credits'])
        self.assertFalse(raccoon['credit_meter'])
        self.assertFalse(raccoon['desktop_sync'])
        # CodeArts 码道与小浣熊同组：原生选号 / 按账号的在线目录 / 签到。
        # 区别在「余额」——上游没有积分余额接口，`credits` 给的是累计签到
        # 领取量，因此同样没有 `min_credits`（不能按余额挑号）。
        codearts = runtime_settings.capabilities('codearts')
        self.assertTrue(codearts['max_accounts_try'])
        self.assertTrue(codearts['session_affinity'])
        self.assertTrue(codearts['catalog_timeout'])
        self.assertTrue(codearts['credits_ttl'])
        self.assertFalse(codearts['min_credits'])
        self.assertFalse(codearts['credit_meter'])
        self.assertFalse(codearts['desktop_sync'])
        self.assertFalse(codearts['usage_ledger'])
        # Qoder 的目录 / 冷却 / 换号都在内置网关里，适配器只做透传。
        self.assertFalse(qoder['catalog_ttl'])
        self.assertFalse(qoder['cooldown_429'])
        self.assertEqual(runtime_settings.load('loomy')['min_credits'], 5000)
        self.assertEqual(runtime_settings.load('loomy')['runtime_limits']['min_credits'],
                         {'min': 0, 'max': 10_000_000})
        # 协议兼容开关只属于 Qoder：Trae 没有对应的网关开关，不能显示成可改。
        self.assertTrue(qoder['tool_echo_mode'])
        self.assertTrue(qoder['anthropic_thinking'])
        self.assertTrue(qoder['structured_tool_history'])
        self.assertTrue(qoder['image_transcode'])
        self.assertTrue(qoder['web_tools'])
        self.assertFalse(trae['tool_echo_mode'])
        self.assertFalse(trae['web_tools'])

    def test_unknown_platform_gets_the_generic_two(self) -> None:
        caps = runtime_settings.capabilities('fake')
        self.assertTrue(caps['request_timeout'])
        self.assertTrue(caps['idle_timeout'])
        self.assertFalse(caps['max_accounts_try'])

    def test_combo_only_exposes_session_affinity(self) -> None:
        """组合是虚拟平台：没有自己的请求与账号，只应有粘性两个字段。

        组合页的「会话粘性」弹窗直接读写 `/api/platforms/combo/settings`；
        能力表多开或漏开一个字段，弹窗要么画不出来、要么保存时 400。
        """
        caps = runtime_settings.capabilities('combo')
        self.assertTrue(caps['session_affinity'])
        self.assertTrue(caps['session_affinity_ttl'])
        self.assertFalse(caps['request_timeout'])
        self.assertFalse(caps['max_accounts_try'])
        values = runtime_settings.load('combo')
        self.assertTrue(values['session_affinity'], '粘性默认开启')
        self.assertEqual(values['session_affinity_ttl'], 30 * 60)
        self.assertEqual(values['runtime_limits']['session_affinity_ttl'],
                         {'min': 60, 'max': 12 * 3600})


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

    def test_cache_cooldown_and_protocol_fields_round_trip(self) -> None:
        runtime_settings.save('trae', {
            'catalog_ttl': 900, 'catalog_retry_ttl': 120,
            'catalog_timeout': 5, 'catalog_backoff_base': 10,
            'catalog_backoff_max': 300, 'credits_ttl': 30,
            'credits_timeout': 8, 'cooldown_429': 120, 'cooldown_5xx': 30,
            'solo_max_tokens': 4096, 'credit_meter': 'detail',
            'native_tools': False, 'strict_stream_end': False,
        })
        values = runtime_settings.load('trae')
        for field, expected in (
            ('catalog_ttl', 900), ('catalog_retry_ttl', 120),
            ('catalog_timeout', 5), ('catalog_backoff_base', 10),
            ('catalog_backoff_max', 300), ('credits_ttl', 30),
            ('credits_timeout', 8), ('cooldown_429', 120),
            ('cooldown_5xx', 30), ('solo_max_tokens', 4096),
        ):
            self.assertEqual(values[field], expected, field)
        self.assertEqual(values['credit_meter'], 'detail')
        self.assertFalse(values['native_tools'])
        self.assertFalse(values['strict_stream_end'])
        self.assertEqual(runtime_settings.protocol_switches('trae')['native_tools'],
                         False)
        self.assertEqual(runtime_settings.credit_meter('trae'), 'detail')
        self.assertEqual(runtime_settings.cooldown_429('trae'), 120)

    def test_dumate_usage_ledger_round_trips(self) -> None:
        runtime_settings.save('dumate', {'usage_ledger': True,
                                         'usage_sync_days': 30})
        self.assertTrue(runtime_settings.usage_ledger('dumate'))
        self.assertEqual(runtime_settings.usage_sync_days('dumate'), 30)
        self.assertFalse(runtime_settings.usage_ledger('loomy'))
        for bad in ({'usage_sync_days': 0}, {'usage_sync_days': 91},
                    {'usage_ledger': 'yes'}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    runtime_settings.save('dumate', bad)

    def test_panel_override_wins_over_environment(self) -> None:
        """显式保存过的值优先；没保存的字段仍跟随 TF_TRAE_*。"""
        with mock.patch.object(config, 'TRAE_NATIVE_TOOLS', True), \
             mock.patch.object(config, 'TRAE_TOOL_PROTOCOL_HINT', False):
            runtime_settings.save('trae', {'native_tools': False})
            values = runtime_settings.load('trae')
            self.assertFalse(values['native_tools'])
            self.assertFalse(values['tool_protocol_hint'])  # 未被覆盖，跟随 config

    def test_new_fields_reject_out_of_range_values(self) -> None:
        bad_payloads = (
            {'catalog_ttl': 599},
            {'catalog_ttl': 86401},
            {'catalog_timeout': 2},
            {'credits_ttl': 14},
            {'cooldown_429': 601},
            {'solo_max_tokens': 1023},
            {'credit_meter': 'nope'},
            {'native_tools': 'yes'},
        )
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    runtime_settings.save('trae', payload)

    def test_qoder_rejects_trae_only_cache_fields(self) -> None:
        with self.assertRaises(ValueError):
            runtime_settings.save('qoder', {'catalog_ttl': 900})
        with self.assertRaises(ValueError):
            runtime_settings.save('qoder', {'native_tools': False})

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
                      'session_affinity', 'session_affinity_ttl', 'desktop_sync',
                      'catalog_ttl', 'credits_ttl', 'cooldown_429',
                      'solo_max_tokens', 'credit_meter', 'native_tools'):
            self.assertIn(field, out)
        self.assertIn('capabilities', out)
        self.assertIn('checkin_concurrency', out)
        self.assertEqual(out['enum_choices']['credit_meter'],
                         ['', 'delta', 'detail'])

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
                               mock.AsyncMock(return_value=PlatformChat(
                                   payload={'ok': True}))) as chat_once:
            result = asyncio.run(self.adapter._chat_accounts('m', body))
        return result, candidates, chat_once

    def test_runtime_values_reach_the_chat_call(self) -> None:
        runtime_settings.save('trae', {'request_timeout': 120,
                                       'idle_timeout': 30,
                                       'max_accounts_try': 1})
        result, _, chat_once = self._run_chat({'prompt_cache_key': 's1'})
        self.assertEqual(result.payload, {'ok': True})
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


class ProtocolSwitchTest(_DbCase):
    """Qoder 的协议兼容开关：默认跟随网关、取值受白名单约束、可清回默认。"""

    def test_defaults_follow_the_gateway(self) -> None:
        values = runtime_settings.load('qoder')
        for field in ('tool_echo_mode', 'anthropic_thinking',
                      'structured_tool_history', 'image_transcode'):
            self.assertEqual(values[field], '')
        self.assertFalse(values['web_tools'])
        self.assertEqual(values['enum_choices']['tool_echo_mode'],
                         ['', 'strict', 'lenient'])
        # 只下发本平台支持的枚举项：Trae 有「实扣积分模式」，但没有协议开关。
        self.assertNotIn('tool_echo_mode',
                         runtime_settings.load('trae')['enum_choices'])

    def test_enum_values_round_trip(self) -> None:
        runtime_settings.save('qoder', {
            'tool_echo_mode': 'lenient',
            'anthropic_thinking': 'empty',
            'structured_tool_history': 'off',
            'image_transcode': 'off',
            'web_tools': True,
        })
        values = runtime_settings.load('qoder')
        self.assertEqual(values['tool_echo_mode'], 'lenient')
        self.assertEqual(values['anthropic_thinking'], 'empty')
        self.assertEqual(values['structured_tool_history'], 'off')
        self.assertEqual(values['image_transcode'], 'off')
        self.assertTrue(values['web_tools'])

    def test_rejects_values_outside_the_choices(self) -> None:
        bad_payloads = (
            {'tool_echo_mode': 'loud'},
            {'tool_echo_mode': True},
            {'tool_echo_mode': 1},
            {'anthropic_thinking': 'on'},
            {'structured_tool_history': 'strict'},
            {'image_transcode': 'hard'},
            {'web_tools': 'yes'},
        )
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    runtime_settings.save('qoder', payload)

    def test_empty_value_clears_back_to_gateway_default(self) -> None:
        runtime_settings.save('qoder', {'tool_echo_mode': 'lenient'})
        runtime_settings.save('qoder', {'tool_echo_mode': ''})
        self.assertEqual(runtime_settings.load('qoder')['tool_echo_mode'], '')
        # 坏值（直接改库）也回落到默认，不让界面显示一个网关不认识的模式。
        db.set_setting('platforms.qoder.image_transcode', 'nope')
        self.assertEqual(runtime_settings.load('qoder')['image_transcode'], '')

    def test_trae_cannot_use_the_gateway_switches(self) -> None:
        for payload in ({'tool_echo_mode': 'strict'}, {'web_tools': True}):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    runtime_settings.save('trae', payload)


class QoderEnvTest(_DbCase):
    """保存的值要真的下发到网关读的环境变量（不是只写进库）。"""

    ENV = ('QD_TOOL_ECHO_MODE', 'QD_ANTHROPIC_THINKING',
           'QD_STRUCTURED_TOOL_HISTORY', 'QD_IMAGE_TRANSCODE', 'QD_WEB_TOOLS')

    def setUp(self) -> None:
        super().setUp()
        self._saved = {name: os.environ.get(name) for name in self.ENV}
        self._orig_env = dict(qoder_embedded._ENV_ORIGINAL)
        qoder_embedded._ENV_ORIGINAL.clear()
        for name in self.ENV:
            os.environ.pop(name, None)

    def tearDown(self) -> None:
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        qoder_embedded._ENV_ORIGINAL.clear()
        qoder_embedded._ENV_ORIGINAL.update(self._orig_env)
        super().tearDown()

    def test_settings_map_to_gateway_env(self) -> None:
        runtime_settings.save('qoder', {
            'tool_echo_mode': 'lenient',
            'anthropic_thinking': 'empty',
            'structured_tool_history': 'off',
            'image_transcode': 'off',
            'web_tools': True,
        })
        applied = qoder_embedded.apply_runtime_settings()
        self.assertEqual(os.environ['QD_TOOL_ECHO_MODE'], 'lenient')
        self.assertEqual(os.environ['QD_ANTHROPIC_THINKING'], 'empty')
        self.assertEqual(os.environ['QD_STRUCTURED_TOOL_HISTORY'], 'off')
        self.assertEqual(os.environ['QD_IMAGE_TRANSCODE'], 'off')
        self.assertEqual(os.environ['QD_WEB_TOOLS'], '1')
        self.assertEqual(applied['QD_TOOL_ECHO_MODE'], 'lenient')

    def test_saving_default_restores_the_previous_env(self) -> None:
        # 运维原来设了 lenient：面板保存 strict 后再改回「网关默认」，
        # 应该回到 lenient，而不是把运维的设置抹成「没设」。
        os.environ['QD_TOOL_ECHO_MODE'] = 'lenient'
        runtime_settings.save('qoder', {'tool_echo_mode': 'strict'})
        qoder_embedded.apply_runtime_settings()
        self.assertEqual(os.environ['QD_TOOL_ECHO_MODE'], 'strict')

        runtime_settings.save('qoder', {'tool_echo_mode': ''})
        qoder_embedded.apply_runtime_settings()
        self.assertEqual(os.environ['QD_TOOL_ECHO_MODE'], 'lenient')

    def test_untouched_switches_keep_the_operator_env(self) -> None:
        # 面板从没碰过 Web 工具：启动时不能把运维设的 QD_WEB_TOOLS=1 改成 0。
        os.environ['QD_WEB_TOOLS'] = '1'
        runtime_settings.save('qoder', {'request_timeout': 120})
        self.assertEqual(qoder_embedded.apply_runtime_settings(), {})
        self.assertEqual(os.environ['QD_WEB_TOOLS'], '1')

    def test_explicit_values_only_apply_saved_fields(self) -> None:
        runtime_settings.save('qoder', {'tool_echo_mode': 'lenient',
                                        'web_tools': True})
        applied = qoder_embedded.apply_runtime_settings({
            'tool_echo_mode': 'strict', 'web_tools': False,
            'anthropic_thinking': 'empty',
        })
        self.assertEqual(applied, {'QD_TOOL_ECHO_MODE': 'strict',
                                   'QD_WEB_TOOLS': '0'})
        self.assertNotIn('QD_ANTHROPIC_THINKING', os.environ)


class QoderSettingsRouteTest(_DbCase):
    def test_save_pushes_switches_to_the_gateway(self) -> None:
        seen: dict = {}

        class _Adapter:
            name = 'qoder'
            label = 'Qoder'

            def apply_runtime_settings(self, values=None) -> None:
                seen.update(values or {})

        fake_security = mock.Mock()
        with mock.patch.object(platforms_router, '_adapter',
                               return_value=_Adapter()), \
             mock.patch.object(platforms_router, 'security', fake_security):
            out = platforms_router.save_platform_settings(
                'qoder', {'tool_echo_mode': 'lenient', 'web_tools': True},
                user={'role': 'admin'})

        self.assertEqual(out['tool_echo_mode'], 'lenient')
        self.assertTrue(out['web_tools'])
        self.assertEqual(seen['tool_echo_mode'], 'lenient')
        self.assertTrue(seen['web_tools'])
        fake_security.audit.assert_called_once()


class TraeCooldownTest(_DbCase):
    """429/5xx 冷却：默认 0 = 旧行为；打开后冻结账号并换下一个。"""

    def setUp(self) -> None:
        super().setUp()
        self.adapter = TraeAdapter()
        self.accounts = [{'uid': 'u1', 'realm': 'cn'},
                         {'uid': 'u2', 'realm': 'cn'}]

    def _run(self, exc: Exception):
        chat_once = mock.AsyncMock(side_effect=[
            exc, PlatformChat(payload={'ok': True})])
        with mock.patch.object(self.adapter, '_candidates',
                               return_value=self.accounts), \
             mock.patch.object(self.adapter, '_ensure_token',
                               mock.AsyncMock(side_effect=lambda a: a)), \
             mock.patch.object(self.adapter, '_chat_once', chat_once), \
             mock.patch('server.platforms.trae.adapter.store.set_cooling') as cooling:
            try:
                return asyncio.run(self.adapter._chat_accounts('m', {})), cooling
            except PlatformError as err:
                return err, cooling

    def test_rate_limit_raises_by_default(self) -> None:
        err = PlatformError('limited', status=429, code='rate_limited')
        out, cooling = self._run(err)
        self.assertIsInstance(out, PlatformError)
        cooling.assert_not_called()

    def test_rate_limit_cools_down_and_tries_next_account(self) -> None:
        runtime_settings.save('trae', {'cooldown_429': 90})
        out, cooling = self._run(
            PlatformError('limited', status=429, code='rate_limited'))
        self.assertEqual(out.payload, {'ok': True})
        cooling.assert_called_once()
        uid, until, reason = cooling.call_args.args
        self.assertEqual(uid, 'u1')
        self.assertGreater(until, time.time())
        self.assertIn('429', reason)

    def test_5xx_uses_its_own_seconds(self) -> None:
        runtime_settings.save('trae', {'cooldown_5xx': 30})
        out, cooling = self._run(
            PlatformError('boom', status=503, code='upstream_error'))
        self.assertEqual(out.payload, {'ok': True})
        cooling.assert_called_once()
        self.assertAlmostEqual(cooling.call_args.args[1] - time.time(), 30,
                               delta=10)

    def test_model_unknown_does_not_cool_the_account(self) -> None:
        """4023 是「模型走错通道」，账号本身没问题，冻结它只会白少一个号。"""
        runtime_settings.save('trae', {'cooldown_5xx': 30, 'max_accounts_try': 1})
        err = PlatformError('[4023] model is unknown', status=502,
                            code='upstream_error', payload={'code': 4023})
        chat_once = mock.AsyncMock(side_effect=[err, err])
        with mock.patch.object(self.adapter, '_candidates',
                               return_value=self.accounts), \
             mock.patch.object(self.adapter, '_ensure_token',
                               mock.AsyncMock(side_effect=lambda a: a)), \
             mock.patch.object(self.adapter, '_chat_once', chat_once), \
             mock.patch('server.platforms.trae.adapter.store.set_cooling') as cooling:
            with self.assertRaises(PlatformError):
                asyncio.run(self.adapter._chat_accounts('m', {}))
        cooling.assert_not_called()

    def test_cool_down_write_failure_keeps_raising(self) -> None:
        runtime_settings.save('trae', {'cooldown_429': 90})
        chat_once = mock.AsyncMock(
            side_effect=[PlatformError('limited', status=429, code='rate_limited'),
                         'ok'])
        with mock.patch.object(self.adapter, '_candidates',
                               return_value=self.accounts), \
             mock.patch.object(self.adapter, '_ensure_token',
                               mock.AsyncMock(side_effect=lambda a: a)), \
             mock.patch.object(self.adapter, '_chat_once', chat_once), \
             mock.patch('server.platforms.trae.adapter.store.set_cooling',
                        side_effect=FileNotFoundError('gone')):
            with self.assertRaises(PlatformError):
                asyncio.run(self.adapter._chat_accounts('m', {}))


class TraeProtocolWiringTest(_DbCase):
    """面板上的协议开关要真的传到请求体构造函数与收尾翻译器。"""

    def setUp(self) -> None:
        super().setUp()
        self.adapter = TraeAdapter()

    @staticmethod
    def _events():
        async def gen():
            yield ('output', {'response': 'hi'})
            yield ('done', {'finish_reason': 'stop'})
        return gen()

    def _run_once(self, *, channel: str, body: dict):
        response = mock.Mock()
        response.aclose = mock.AsyncMock()
        opener = 'open_solo_chat' if channel == 'solo' else 'open_chat'
        iterer = 'iter_solo_events' if channel == 'solo' else 'iter_events'
        with mock.patch.object(trae_client, f'build_{channel}_body',
                               return_value={'built': True}) as build, \
             mock.patch.object(trae_client, opener,
                               mock.AsyncMock(return_value=response)), \
             mock.patch.object(trae_client, iterer,
                               lambda *a, **kw: self._events()):
            asyncio.run(self.adapter._chat_once(
                {'uid': 'u1', 'realm': 'cn'}, 'm', body, 'trae/m',
                channel=channel, request_timeout=30, idle_timeout=30))
        return build

    def test_native_tools_off_strips_tools_and_passes_hint_switch(self) -> None:
        runtime_settings.save('trae', {'native_tools': False,
                                       'tool_protocol_hint': False,
                                       'reasoning_effort': False})
        tools = [{'type': 'function', 'function': {'name': 'f'}}]
        build = self._run_once(channel='chat', body={
            'messages': [{'role': 'user', 'content': 'x'}],
            'stream': False, 'tools': tools})
        passed = build.call_args.args[2]
        self.assertNotIn('tools', passed)
        self.assertFalse(build.call_args.kwargs['protocol_hint'])
        self.assertFalse(build.call_args.kwargs['reasoning_effort'])

    def test_solo_budget_comes_from_the_panel(self) -> None:
        runtime_settings.save('trae', {'solo_max_tokens': 4096})
        build = self._run_once(channel='solo', body={
            'messages': [{'role': 'user', 'content': 'x'}], 'stream': False})
        self.assertEqual(build.call_args.kwargs['max_tokens'], 4096)

    def test_tail_switches_reach_translate(self) -> None:
        runtime_settings.save('trae', {'strict_stream_end': False,
                                       'empty_output_error': False,
                                       'degenerate_call_error': False,
                                       'reasoning_as_content': True,
                                       'rescue_tail_strict': False})
        body = {'messages': [{'role': 'user', 'content': 'x'}], 'stream': True}
        response = mock.Mock()
        response.aclose = mock.AsyncMock()
        with mock.patch.object(trae_client, 'build_chat_body',
                               return_value={'built': True}), \
             mock.patch.object(trae_client, 'open_chat',
                               mock.AsyncMock(return_value=response)), \
             mock.patch.object(trae_client, 'iter_events',
                               lambda *a, **kw: self._events()), \
             mock.patch.object(trae_chat, 'translate',
                               return_value=object()) as translate:
            asyncio.run(self.adapter._chat_once(
                {'uid': 'u1', 'realm': 'cn'}, 'm', body, 'trae/m',
                channel='chat', request_timeout=30, idle_timeout=30))
        kwargs = translate.call_args.kwargs
        self.assertFalse(kwargs['strict_end'])
        self.assertFalse(kwargs['empty_output_error'])
        self.assertFalse(kwargs['degenerate_error'])
        self.assertTrue(kwargs['reasoning_as_content'])
        self.assertFalse(kwargs['strict_tail'])


if __name__ == '__main__':
    unittest.main()
