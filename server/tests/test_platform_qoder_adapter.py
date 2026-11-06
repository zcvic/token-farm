"""Qoder 适配器与映射：脱敏、模型前缀 / 双区、登录状态、chat 透传。

协议层在 `_vendor/`（内置网关）；适配器测试全部用注入的假 client，测的是
「网关字段 → token-farm 形状」的翻译与调度策略。最后一组测试会真的拉起
内置网关（回环端口 + 临时数据目录），但不发任何上游请求。
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import config
from server.platforms.base import ChatContext, PlatformAdapter, PlatformError
from server.platforms.qoder import embedded, mapping
from server.platforms.qoder.adapter import QoderAdapter, _split_realm_hint
from server.platforms.qoder.client import QoderClient
from server.platforms.registry import get as get_platform


def _raw_account(**overrides) -> dict:
    raw = {
        'uid': 'u-1',
        'nickname': '测试账号',
        'realm': 'cn',
        'platform': 'CLI',
        'enabled': True,
        'source': 'oauth',
        'tokenFamily': 'fam-1',
        'expiresAt': 1900000000,
        'hasRefreshToken': True,
        'hasPAT': False,
        'inCooldown': False,
        'cooldownFor': None,
        'addedAt': 1700000000,
        'lastError': '',
    }
    raw.update(overrides)
    return raw


class _FakeResponse:
    """`open_chat()` 的替身：只实现适配器用到的那几个方法。"""

    def __init__(self, lines: list[str], realm: str = 'cn') -> None:
        self.headers = {'x-qoder-realm': realm}
        self._lines = list(lines)
        self.closed = False

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aclose(self) -> None:
        self.closed = True


class _FakeClient:
    """记录调用参数的网关客户端替身。"""

    def __init__(self, *, accounts=None, models=None) -> None:
        self.accounts = list(accounts or [])
        self.models = list(models or [])
        self.calls: list[tuple] = []
        self.models_error: Exception | None = None
        self.list_accounts_error: Exception | None = None
        self.credits_error: Exception | None = None
        self.import_desktop_error: Exception | None = None
        self.chat_response: dict = {}
        self.chat_realm = 'cn'
        self.stream_lines: list[str] = []
        self.stream_response: _FakeResponse | None = None
        self.login_poll_result: dict = {'status': 'pending'}

    # 账号
    def list_accounts(self, realm: str = 'all') -> list[dict]:
        self.calls.append(('list_accounts', realm))
        if self.list_accounts_error is not None:
            raise self.list_accounts_error
        return list(self.accounts)

    def import_account(self, payload: dict) -> dict:
        self.calls.append(('import_account', payload))
        return _raw_account(uid='u-new', source='manual')

    def import_desktop(self) -> dict:
        self.calls.append(('import_desktop',))
        if self.import_desktop_error is not None:
            raise self.import_desktop_error
        return {'imported': [_raw_account(uid='u-desk', source='desktop-app')]}

    def delete_account(self, uid: str) -> bool:
        self.calls.append(('delete_account', uid))
        return True

    def refresh_account(self, uid: str) -> dict:
        self.calls.append(('refresh_account', uid))
        return _raw_account(uid=uid)

    def refresh_credits(self, uid: str) -> dict:
        self.calls.append(('refresh_credits', uid))
        if self.credits_error is not None:
            raise self.credits_error
        return _raw_account(uid=uid, credits={'remain': 321, 'size': 500,
                                              'used': 179})

    def set_enabled(self, uid: str, enabled: bool) -> dict:
        self.calls.append(('set_enabled', uid, enabled))
        return _raw_account(uid=uid, enabled=enabled)

    def set_weight(self, uid: str, weight: float) -> dict:
        self.calls.append(('set_weight', uid, weight))
        return _raw_account(uid=uid, weight=weight)

    # 任务
    def checkin(self, uid: str) -> dict:
        self.calls.append(('checkin', uid))
        return {'uid': uid, 'ok': True, 'earned_credit': 100,
                'message': '签到成功', 'logs': ['ok']}

    # 登录
    def login_start(self, realm: str) -> dict:
        self.calls.append(('login_start', realm))
        return {'state': 'st-1', 'authUrl': 'https://qoder.com/device?x=1',
                'realm': realm, 'platform': 'CLI'}

    def login_poll(self, state: str) -> dict:
        self.calls.append(('login_poll', state))
        return self.login_poll_result

    def login_cancel(self, state: str) -> bool:
        self.calls.append(('login_cancel', state))
        return True

    # 模型
    def list_models(self) -> list[dict]:
        self.calls.append(('list_models',))
        if self.models_error is not None:
            raise self.models_error
        return list(self.models)

    # 推理
    async def post_chat(self, body: dict, realm=None):
        self.calls.append(('post_chat', dict(body), realm))
        return dict(self.chat_response), self.chat_realm

    async def open_chat(self, body: dict, realm=None) -> _FakeResponse:
        self.calls.append(('open_chat', dict(body), realm))
        response = self.stream_response or _FakeResponse(self.stream_lines,
                                                         self.chat_realm)
        self.stream_response = response
        return response


class MappingTests(unittest.TestCase):
    def test_public_account_hides_credentials(self) -> None:
        account = mapping.public_account(_raw_account())
        self.assertNotIn('token', account)
        self.assertNotIn('refreshToken', account)
        self.assertNotIn('accessToken', account)
        self.assertTrue(account['has_token'])
        self.assertTrue(account['has_refresh_token'])
        self.assertIn('托管于 Qoder 内置网关', account['token_preview'])
        self.assertIn('fam-1', account['token_preview'])
        self.assertEqual(account['source'], 'web')

    def test_public_account_realm_and_cooling(self) -> None:
        account = mapping.public_account(
            _raw_account(realm='intl', inCooldown=True, cooldownFor=120),
            now=1_000_000.0)
        self.assertEqual(account['realm'], 'intl')
        self.assertEqual(account['realm_label'], '国际版')
        self.assertEqual(mapping.db_realm(account['realm']), 'global')
        self.assertTrue(account['cooling'])
        self.assertEqual(account['cool_remaining_sec'], 120)
        self.assertEqual(account['cooling_until'], 1_000_120)

    def test_public_account_disabled(self) -> None:
        account = mapping.public_account(_raw_account(enabled=False))
        self.assertTrue(account['disabled'])
        self.assertEqual(account['weight'], 0)

    def test_normalize_and_db_realm(self) -> None:
        self.assertEqual(mapping.normalize_realm('INTL'), 'intl')
        self.assertEqual(mapping.normalize_realm('global'), 'intl')
        self.assertEqual(mapping.normalize_realm(None), 'cn')
        self.assertEqual(mapping.db_realm('intl'), 'global')
        self.assertEqual(mapping.db_realm('cn'), 'cn')

    def test_public_model_prefix_and_realms(self) -> None:
        model = mapping.public_model({
            'id': 'Qwen3.8-Max',
            'name': 'Qwen3.8-Max',
            'realm': 'intl',
            'realm_exclusive': 'intl',
            'context_length': 200000,
            'max_output_tokens': 8000,
            'supports_reasoning': True,
        })
        self.assertEqual(model['id'], 'qoder/Qwen3.8-Max')
        self.assertEqual(model['model_name'], 'Qwen3.8-Max')
        self.assertEqual(model['platform'], 'qoder')
        self.assertEqual(model['realms'], ['intl'])
        self.assertEqual(model['context_window'], 200000)
        self.assertEqual(model['max_tokens'], 8000)
        self.assertTrue(model['supports_reasoning'])

    def test_public_model_dual_realm_defaults(self) -> None:
        model = mapping.public_model({'id': 'GLM-5', 'realm': 'cn'})
        self.assertEqual(model['realms'], ['cn', 'intl'])
        self.assertEqual(model['realm'], 'cn')

    def test_credits_missing_snapshot_is_an_error(self) -> None:
        out = mapping.credits_of(_raw_account())
        self.assertIsNone(out['credits'])
        self.assertEqual(out['code'], 'credits_missing')
        self.assertIn('积分快照', out['error'])

    def test_credits_maps_snapshot(self) -> None:
        out = mapping.credits_of(_raw_account(
            credits={'remain_effective': 1234, 'size': 5000, 'used': 3766,
                     'expires_at': 1900000000,
                     'packages': [{'name': '赠送', 'remain': 200,
                                   'expires_at': 1800000000}]},
            checkedInToday=True, lastCreditsDelta=100))
        self.assertEqual(out['credits'], 1234)
        self.assertEqual(out['limit'], 5000)
        self.assertEqual(out['used'], 3766)
        self.assertEqual(out['checkin_credits'], 100)
        self.assertTrue(out['checked_in'])
        # 只认包级到期：顶层 expires_at（1900000000）归属未证实，不映射。
        self.assertEqual([e['expire_at'] for e in out['expires']],
                         [1800000000])
        self.assertEqual(out['next_expire_at'], 1800000000)

    def test_credits_ignores_toplevel_expiry(self) -> None:
        """顶层 expires_at 可能是「按月重置」或永不过期占位，不能当到期日。"""
        out = mapping.credits_of(_raw_account(credits={
            'remain_effective': 100, 'size': 200, 'used': 100,
            'expires_at': 1900000000,
        }))
        self.assertEqual(out['expires'], [])
        self.assertIsNone(out['next_expire_at'])

    def test_credits_ignores_never_expire_sentinel(self) -> None:
        """CN 账号用 253402214400 表示「永不过期」，不该显示成 9999 年。"""
        out = mapping.credits_of(_raw_account(credits={
            'remain_effective': 10, 'size': 20, 'used': 10,
            'expires_at': 253402214400,
            'packages': [
                {'name': '基础额度', 'remain': 10, 'expires_at': 253402214400},
                {'name': '赠送', 'remain': 5, 'expires_at': 0,
                 'no_expiry': True},
            ],
        }))
        self.assertEqual(out['expires'], [])
        self.assertIsNone(out['next_expire_at'])

    def test_usage_puts_snapshot_in_entitlement(self) -> None:
        usage = mapping.usage_of(_raw_account(
            credits={'remain_effective': 10, 'size': 20, 'used': 10}))
        self.assertEqual(usage['entitlement']['remain'], 10)
        self.assertEqual(usage['errors'], [])
        missing = mapping.usage_of(_raw_account())
        self.assertIsNone(missing['entitlement'])
        self.assertEqual(missing['errors'][0]['endpoint'], 'credits')

    def test_checkin_maps_result(self) -> None:
        out = mapping.checkin_of(
            {'ok': True, 'earned_credit': 100, 'message': '签到成功',
             'logs': ['ok']}, uid='u-1', realm='cn')
        self.assertTrue(out['ok'])
        self.assertEqual(out['earned_credit'], 100)
        self.assertEqual(out['message'], '签到成功')
        self.assertNotIn('skipped', out)

    def test_checkin_without_result_is_skipped(self) -> None:
        out = mapping.checkin_of({}, uid='u-1', realm='cn')
        self.assertTrue(out['ok'])
        self.assertTrue(out['skipped'])
        self.assertIn('今日已签到', out['message'])


class AdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = _FakeClient(accounts=[_raw_account()], models=[
            {'id': 'Qwen3.8-Max', 'realm': 'cn'},
            {'id': 'Qwen3.8-Max', 'realm': 'cn'},  # 网关理论上已去重，这里再兜一层
            {'id': 'Sonus', 'realm': 'intl', 'realm_exclusive': 'intl'},
        ])
        self.adapter = QoderAdapter(client=self.client)

    def test_registered_and_routable(self) -> None:
        registered = get_platform('qoder')
        self.assertIsNotNone(registered)
        self.assertTrue(registered.routable)
        self.assertTrue(registered.ui_visible)
        self.assertTrue(registered.realm_aware)

    def test_unsupported_capabilities_stay_explicit(self) -> None:
        """Qoder 支持权重，但没有强制冷却 API：后者必须明确拒绝。"""
        self.assertIsNot(type(self.adapter).set_account_options,
                         PlatformAdapter.set_account_options)
        self.assertIs(type(self.adapter).set_account_cooling,
                      PlatformAdapter.set_account_cooling)
        self.assertFalse(hasattr(self.adapter, 'login_submit_callback'))
        with self.assertRaises(PlatformError):
            self.adapter.set_account_cooling('u-1', until=1_900_000_000)

    def test_set_account_weight_goes_through_gateway(self) -> None:
        self.adapter._invalidate_accounts()
        out = self.adapter.set_account_options('u-1', weight=2.5)
        self.assertEqual(out['weight'], 2.5)
        self.assertIn(('set_weight', 'u-1', 2.5), self.client.calls)

    def test_account_list_failure_is_remembered_briefly(self) -> None:
        """网关起不来时，开页不能每次都被连接超时拖住。"""
        self.client.list_accounts_error = PlatformError('网关不可达', status=502)
        with self.assertRaises(PlatformError):
            self.adapter.list_accounts()
        calls = len(self.client.calls)
        self.client.list_accounts_error = None
        with self.assertRaises(PlatformError):
            self.adapter.list_accounts()
        self.assertEqual(len(self.client.calls), calls,
                         '10 秒内应复用上次失败，不再打网关')
        # 一次成功的写操作会清掉失败缓存（说明网关已经回来了）
        self.adapter._invalidate_accounts()
        self.assertEqual([a['uid'] for a in self.adapter.list_accounts()],
                         ['u-1'])

    def test_list_accounts_desensitizes(self) -> None:
        accounts = self.adapter.list_accounts()
        self.assertEqual([a['uid'] for a in accounts], ['u-1'])
        self.assertEqual(accounts[0]['platform'], 'qoder')
        self.assertNotIn('accessToken', accounts[0])
        self.assertEqual(self.client.calls, [('list_accounts', 'all')])

    def test_has_accounts_hint_reads_disk_without_booting_gateway(self) -> None:
        """定时任务靠它判断「值不值得拉起网关」：只数 `<uid>.json`，不启动。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(embedded, 'accounts_dir', lambda: root), \
                 mock.patch.object(embedded, 'runtime',
                                   side_effect=AssertionError('不该启动网关')):
                # 只有配置 / 状态文件不算账号
                (root / 'settings.json').write_text('{}', encoding='utf-8')
                (root / 'active_realm.json').write_text('{}', encoding='utf-8')
                (root / 'machine_identity.json').write_text('{}', encoding='utf-8')
                self.assertFalse(self.adapter.has_accounts_hint())
                (root / 'u-1.json').write_text('{}', encoding='utf-8')
                self.assertTrue(self.adapter.has_accounts_hint())

    def test_desktop_import_maps_credentials_failure_to_failed(self) -> None:
        """上游 400（某个客户端凭据读失败）应落到 failed，而不是整页报错。"""
        self.client.import_desktop_error = PlatformError(
            'Qoder 网关：import failed: bad auth.v1.dat', status=400,
            code='invalid_request_error')
        out = self.adapter.import_from_desktop()
        self.assertEqual(out['imported'], [])
        self.assertEqual(len(out['failed']), 1)
        self.assertIn('bad auth.v1.dat', out['failed'][0]['error'])

    def test_desktop_import_keeps_gateway_errors(self) -> None:
        self.client.import_desktop_error = PlatformError(
            'Qoder 网关不可达', status=502, code='qoder_unreachable')
        with self.assertRaises(PlatformError):
            self.adapter.import_from_desktop()

    def test_list_models_dedupes_and_caches(self) -> None:
        models = asyncio.run(self.adapter.list_models())
        # 双区同名模型按区域各列一条；单区模型只列它自己的区域。
        self.assertEqual([(m['model_name'], m['realm']) for m in models],
                         [('Qwen3.8-Max', 'cn'), ('Qwen3.8-Max', 'intl'),
                          ('Sonus', 'intl')])
        self.assertEqual([m['id'] for m in models],
                         ['qodercn/Qwen3.8-Max', 'qodercom/Qwen3.8-Max',
                          'qodercom/Sonus'])
        # 第二次走缓存，不再请求网关
        again = asyncio.run(self.adapter.list_models())
        self.assertEqual([(m['model_name'], m['realm']) for m in again],
                         [('Qwen3.8-Max', 'cn'), ('Qwen3.8-Max', 'intl'),
                          ('Sonus', 'intl')])
        self.assertEqual([c for c in self.client.calls if c[0] == 'list_models'],
                         [('list_models',)])

    def test_list_models_falls_back_to_cache_on_error(self) -> None:
        asyncio.run(self.adapter.list_models())
        self.client.models_error = PlatformError('网关挂了', status=502)
        models = asyncio.run(self.adapter.list_models(refresh=True))
        self.assertEqual([m['model_name'] for m in models],
                         ['Qwen3.8-Max', 'Qwen3.8-Max', 'Sonus'])

    def test_model_failure_is_remembered_briefly(self) -> None:
        """网关起不来时，`/v1/models` 不能每次都等连接超时。"""
        self.client.models_error = PlatformError('网关挂了', status=502)
        with self.assertRaises(PlatformError):
            asyncio.run(self.adapter.list_models())
        calls = len(self.client.calls)
        self.client.models_error = None
        with self.assertRaises(PlatformError):
            asyncio.run(self.adapter.list_models())
        self.assertEqual(len(self.client.calls), calls,
                         '30 秒内应复用上次失败，不再打网关')
        # 显式刷新放行，网关恢复后立即拿到目录
        models = asyncio.run(self.adapter.list_models(refresh=True))
        self.assertEqual([m['model_name'] for m in models],
                         ['Qwen3.8-Max', 'Qwen3.8-Max', 'Sonus'])

    def test_login_start_maps_state_and_realm(self) -> None:
        out = self.adapter.login_start(realm='global')
        self.assertEqual(out['login_id'], 'st-1')
        self.assertEqual(out['realm'], 'intl')
        self.assertEqual(out['url'], 'https://qoder.com/device?x=1')
        self.assertEqual(self.client.calls[-1], ('login_start', 'intl'))

    def test_login_poll_status_mapping(self) -> None:
        cases = {
            'ok': 'success',
            'pending': 'pending',
            'expired': 'expired',
            'unknown': 'error',
            'error': 'error',
        }
        for raw, want in cases.items():
            self.client.login_poll_result = {'status': raw, 'message': 'x'}
            out = self.adapter.login_poll('st-1')
            self.assertEqual(out['status'], want, raw)

    def test_login_poll_success_carries_account(self) -> None:
        self.client.login_poll_result = {
            'status': 'ok', 'account': _raw_account(uid='u-2', realm='intl')}
        out = self.adapter.login_poll('st-1')
        self.assertEqual(out['status'], 'success')
        self.assertEqual(out['account']['uid'], 'u-2')
        self.assertEqual(out['realm'], 'intl')
        self.assertNotIn('accessToken', out['account'])

    def test_checkin_uses_account_realm(self) -> None:
        self.client.accounts = [_raw_account(realm='intl')]
        out = asyncio.run(self.adapter.checkin('u-1'))
        self.assertTrue(out['ok'])
        self.assertEqual(out['realm'], 'intl')
        self.assertEqual(out['earned_credit'], 100)

    def test_refresh_account_also_refreshes_credits(self) -> None:
        """账号页「刷新」既要换 token，也要让用户拿到实时余额。"""
        out = asyncio.run(self.adapter.refresh_account('u-1'))
        self.assertEqual(out['uid'], 'u-1')
        self.assertIn(('refresh_account', 'u-1'), self.client.calls)
        self.assertIn(('refresh_credits', 'u-1'), self.client.calls)

    def test_credits_failure_does_not_fail_account_refresh(self) -> None:
        self.client.credits_error = PlatformError('积分接口超时', status=502)
        out = asyncio.run(self.adapter.refresh_account('u-1'))
        self.assertEqual(out['uid'], 'u-1')

    def test_split_realm_hint(self) -> None:
        self.assertEqual(_split_realm_hint('intl:Qwen3.8-Max'),
                         ('intl', 'Qwen3.8-Max'))
        self.assertEqual(_split_realm_hint('cn:Qwen3.8-Max'),
                         ('cn', 'Qwen3.8-Max'))
        self.assertEqual(_split_realm_hint('global:Qwen3.8-Max'),
                         ('intl', 'Qwen3.8-Max'))
        self.assertEqual(_split_realm_hint('Qwen3.8-Max'), (None, 'Qwen3.8-Max'))

    def test_realm_for_explicit_prefix(self) -> None:
        realm, bare = self.adapter._realm_for('intl:Qwen3.8-Max', None)
        self.assertEqual((realm, bare), ('intl', 'Qwen3.8-Max'))

    def test_realm_for_single_realm_model(self) -> None:
        asyncio.run(self.adapter.list_models())
        self.assertEqual(self.adapter._realm_for('Sonus', None), ('intl', 'Sonus'))
        self.assertEqual(self.adapter._realm_for('Qwen3.8-Max', None),
                         (None, 'Qwen3.8-Max'))

    def test_realm_for_intl_key_only(self) -> None:
        asyncio.run(self.adapter.list_models())
        intl_ctx = ChatContext(key={'realm': 'global'})
        self.assertEqual(self.adapter._realm_for('Qwen3.8-Max', intl_ctx),
                         ('intl', 'Qwen3.8-Max'))
        # 默认 cn 的密钥不强制区域：双区模型仍交给网关自动选
        cn_ctx = ChatContext(key={'realm': 'cn'})
        self.assertEqual(self.adapter._realm_for('Qwen3.8-Max', cn_ctx),
                         (None, 'Qwen3.8-Max'))

    def test_realm_for_router_hint_wins(self) -> None:
        """区域化前缀（`qodercom/`）解析出的提示优先于目录与密钥版本。"""
        asyncio.run(self.adapter.list_models())
        cn_ctx = ChatContext(key={'realm': 'cn'}, realm='global')
        self.assertEqual(self.adapter._realm_for('Qwen3.8-Max', cn_ctx),
                         ('intl', 'Qwen3.8-Max'))
        intl_ctx = ChatContext(key={'realm': 'global'}, realm='cn')
        self.assertEqual(self.adapter._realm_for('Qwen3.8-Max', intl_ctx),
                         ('cn', 'Qwen3.8-Max'))

    def test_realm_of_model(self) -> None:
        asyncio.run(self.adapter.list_models())
        self.assertEqual(self.adapter.realm_of_model('Sonus'), 'global')
        self.assertIsNone(self.adapter.realm_of_model('Qwen3.8-Max'))
        self.assertEqual(self.adapter.realm_of_model('cn:Sonus'), 'cn')

    def test_chat_non_stream_passthrough(self) -> None:
        self.client.chat_response = {
            'id': 'chatcmpl-1',
            'choices': [{'index': 0, 'message': {'role': 'assistant',
                                                 'content': 'hi'}}],
            # 上游给的是复数 credits —— 适配器要归一成单数 credit，
            # 否则网关的积分统计与密钥积分配额都拿不到值。
            'usage': {'prompt_tokens': 3, 'completion_tokens': 2,
                      'total_tokens': 5, 'credits': 0.25},
        }
        self.client.chat_realm = 'intl'
        outcome = asyncio.run(self.adapter.chat(
            'intl:Qwen3.8-Max', {'model': 'x', 'stream': False}))
        self.assertFalse(outcome.streaming)
        self.assertEqual(outcome.payload['model'], 'qoder/intl:Qwen3.8-Max')
        self.assertEqual(outcome.usage['total_tokens'], 5)
        self.assertEqual(outcome.usage['credit'], 0.25)
        self.assertEqual(outcome.realm, 'global')
        self.assertEqual(outcome.model, 'qoder/intl:Qwen3.8-Max')
        # 账号由内置网关选、适配器拿不到；这里必须是空串等回填，
        # 不能把区域标签（qoder:intl）伪装成账号。
        self.assertEqual(outcome.account_hint, '')
        method, body, realm = self.client.calls[-1]
        self.assertEqual(method, 'post_chat')
        self.assertEqual(body['model'], 'Qwen3.8-Max')
        self.assertEqual(realm, 'intl')

    def test_chat_stream_passthrough_and_usage(self) -> None:
        self.client.stream_lines = [
            ': keep-alive',
            'data: {"id":"c1","choices":[{"delta":{"content":"he"}}]}',
            'data: {"id":"c1","choices":[{"delta":{"content":"llo"}}],'
            '"usage":{"prompt_tokens":3,"completion_tokens":2,"credits":0.5}}',
            'data: [DONE]',
        ]
        self.client.chat_realm = 'cn'

        async def run():
            outcome = await self.adapter.chat(
                'Qwen3.8-Max', {'model': 'x', 'stream': True})
            # 流开始前记下 dict 的引用：网关也会这样提前持有它，适配器回填
            # usage 必须**就地更新**，换对象会让网关读到最初那个空 dict。
            before = outcome.usage
            chunks = [chunk async for chunk in outcome.stream]
            await outcome.aclose()
            return outcome, chunks, before

        outcome, chunks, before = asyncio.run(run())
        self.assertEqual(len(chunks), 2)
        self.assertTrue(all(c['model'] == 'qoder/Qwen3.8-Max' for c in chunks))
        self.assertIs(outcome.usage, before, 'usage 被重新绑定，网关会读到空 dict')
        self.assertEqual(outcome.usage['completion_tokens'], 2)
        self.assertEqual(outcome.usage['credit'], 0.5)
        self.assertEqual(outcome.realm, 'cn')
        self.assertEqual(outcome.account_hint, '')
        self.assertIsNotNone(outcome.first_token_ms, '流式首字延迟未采集')
        self.assertTrue(self.client.stream_response.closed)
        method, body, realm = self.client.calls[-1]
        self.assertEqual(method, 'open_chat')
        self.assertEqual(body['model'], 'Qwen3.8-Max')
        self.assertIsNone(realm)

    def test_chat_stream_error_frame_raises(self) -> None:
        self.client.stream_lines = [
            'data: {"error":{"message":"quota exceeded","code":"quota"}}']

        async def run():
            outcome = await self.adapter.chat(
                'Qwen3.8-Max', {'model': 'x', 'stream': True})
            with self.assertRaises(PlatformError):
                async for _ in outcome.stream:
                    pass
            await outcome.aclose()

        asyncio.run(run())


class EmbeddedGatewayTests(unittest.TestCase):
    """内置网关：不用部署第二个项目，也不往源码目录写状态。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(config, 'DATA_DIR', Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        embedded.shutdown()
        self.addCleanup(embedded.shutdown)

    def test_client_construction_is_lazy(self) -> None:
        """构造 client / 导入适配器都不该拉起网关（服务器启动不能被拖慢）。"""
        client = QoderClient()
        self.addCleanup(client.close)
        self.assertIsNone(embedded._RUNTIME)
        self.assertTrue(client.base.startswith('http://127.0.0.1:'))
        self.assertIsNotNone(embedded._RUNTIME)

    def test_boots_inside_data_dir(self) -> None:
        runtime = embedded.runtime()
        root = Path(self.tmp.name) / 'platforms' / 'qoder'
        self.assertEqual(Path(runtime.accounts_dir), root / 'accounts')
        self.assertEqual(Path(runtime.usage_dir), root / 'usage')
        settings = json.loads(
            (root / 'accounts' / 'settings.json').read_text(encoding='utf-8'))
        # 面板会话用持久化签名密钥，不落盘任何明文口令
        self.assertTrue(settings.get('panel_session_secret'))
        # 首次启动会把默认口令 admin 换掉（只存哈希），本地进程不能靠默认口令登录
        self.assertTrue(settings.get('panel_password_hash'))
        self.assertIs(settings.get('panel_password_default'), False)
        self.assertGreaterEqual(len(runtime.api_key), 16)

    def test_accounts_and_models_without_upstream(self) -> None:
        """空账号池 + 静态模型目录：不联网也能给出可用结果。"""
        client = QoderClient()
        self.addCleanup(client.close)
        self.assertEqual(client.list_accounts('all'), [])
        models = client.list_models()
        self.assertIn('Auto', {m['id'] for m in models})
        self.assertTrue(all(m.get('realm') in ('cn', 'intl') for m in models))

    def test_panel_token_is_cached(self) -> None:
        client = QoderClient()
        self.addCleanup(client.close)
        token = client.ensure_panel_token()
        self.assertTrue(token)
        self.assertEqual(client.ensure_panel_token(), token)

    def test_client_recovers_after_gateway_restart(self) -> None:
        client = QoderClient()
        self.addCleanup(client.close)
        self.assertEqual(client.list_accounts('all'), [])
        embedded.shutdown()
        # 端口会换，旧连接池必须被丢弃
        self.assertEqual(client.list_accounts('all'), [])
        self.assertTrue(client.base.startswith('http://127.0.0.1:'))

    def test_shutdown_stops_background_refreshers(self) -> None:
        """重启网关不能留下持有旧账号池的刷新线程。"""
        embedded.runtime()
        proxy = embedded._PROXY
        refresher = proxy.CREDITS_USAGE_REFRESHER
        self.assertIsNotNone(refresher)
        self.assertTrue(refresher.running())
        embedded.shutdown()
        self.assertFalse(refresher.running())

    # ── 会话亲和：键必须显式透传到内置网关 ───────────────────
    def _chat_headers(self, body: dict, realm: str | None = 'cn') -> dict:
        """跑一次 `open_chat()`，返回它实际发出的请求头（上游被打桩）。

        真实选号在网关进程里：token-farm 不给 `X-Session-Id` 的话，厂商只按
        自己的消息前缀做亲和，客户端给的 `prompt_cache_key` 会被丢掉。
        """
        client = QoderClient()
        self.addCleanup(client.close)
        seen: list[dict] = []

        class _Http:
            def build_request(self, method, url, *, json=None, headers=None):
                seen.append(dict(headers or {}))
                return object()

            async def send(self, request, *, stream=False):
                response = _FakeResponse([])
                response.status_code = 200
                return response

        async def run() -> None:
            with mock.patch.object(config, 'http_client',
                                   lambda *a, **k: _Http()):
                resp = await client.open_chat(body, realm)
                await resp.aclose()

        asyncio.run(run())
        self.assertTrue(seen, 'open_chat 必须真的发出请求')
        return seen[0]

    def test_chat_forwards_prompt_cache_key(self) -> None:
        headers = self._chat_headers({
            'prompt_cache_key': 'conv-1',
            'messages': [{'role': 'user', 'content': 'hi'}],
        })
        self.assertEqual(headers.get('X-Session-Id'),
                         'prompt_cache_key:conv-1')
        self.assertEqual(headers.get('X-Realm'), 'cn')

    def test_chat_forwards_conversation_id(self) -> None:
        headers = self._chat_headers({'conversation_id': 'c-1'}, 'intl')
        self.assertEqual(headers.get('X-Session-Id'), 'conversation_id:c-1')
        self.assertEqual(headers.get('X-Realm'), 'intl')

    def test_chat_without_session_dimension_sends_no_header(self) -> None:
        headers = self._chat_headers({
            'user': 'alice',
            'messages': [{'role': 'assistant', 'content': 'x'}],
        })
        self.assertNotIn('X-Session-Id', headers,
                         '没有会话维度时应留给厂商自己的前缀亲和')

    def test_chat_hashes_non_ascii_session_key(self) -> None:
        """HTTP 头只收 ASCII：中文键直接发会让请求在 httpx 层就失败。"""
        headers = self._chat_headers({'prompt_cache_key': '会话-1'})
        self.assertTrue(headers.get('X-Session-Id', '').startswith('sha256:'))

    def test_boot_failure_cleans_up_partial_state(self) -> None:
        """端口占用等启动失败不能留下刷新线程或半截全局状态。"""
        started: dict = {}

        def boom(proxy):
            started['refresher'] = proxy.CREDITS_USAGE_REFRESHER
            raise RuntimeError('boom')

        with mock.patch.object(embedded, '_serve', side_effect=boom):
            with self.assertRaises(RuntimeError):
                embedded.runtime()
        self.assertIsNone(embedded._RUNTIME)
        self.assertIsNone(embedded._PROXY)
        self.assertIsNone(embedded._SERVER)
        self.assertFalse(started['refresher'].running())

    def test_refresh_credits_reads_fresh_snapshot(self) -> None:
        """账号页「刷新」通过网关强刷余额，返回的是新快照而不是旧缓存。"""
        root = Path(self.tmp.name) / 'platforms' / 'qoder' / 'accounts'
        root.mkdir(parents=True)
        row = {
            'uid': 'u-cred',
            'nickname': 'Cred',
            'realm': 'cn',
            'platform': 'CLI',
            'accessToken': 'at-cred',
            'refreshToken': 'rt-cred',
            'expiresAt': 1900000000,
            'addedAt': 1700000000,
            'source': 'oauth',
            'enabled': True,
        }
        (root / 'u-cred.json').write_text(
            json.dumps(row, ensure_ascii=False), encoding='utf-8')

        client = QoderClient()
        self.addCleanup(client.close)
        embedded.runtime()
        account = embedded._PROXY.POOL.get('u-cred')
        self.assertIsNotNone(account)

        def fetch_credits():
            account.credits = {'remain': 222, 'size': 500, 'used': 278,
                               'updated_at': 1900000000}
            return {'ok': True}

        account.fetch_credits = fetch_credits
        account.fetch_plan = lambda: None
        fresh = client.refresh_credits('u-cred')
        self.assertEqual(fresh['uid'], 'u-cred')
        self.assertEqual(fresh['credits']['remain'], 222)


if __name__ == '__main__':
    unittest.main()
