"""模型组合（combo）的后端回归测试。

锁定的性质：

  · 写路径**严格**（坏配置 400，不静默丢弃），读路径**宽容**（坏条目跳过，
    不让一份历史脏数据把网关打成 5xx）；
  · 选路是确定性的平滑加权轮询，长期比例符合配置的权重；
  · 失败切换只发生在「换一个成员可能成功」的错误上（凭据 / 限流 / 5xx），
    请求本身不合法时原样抛出；
  · 组合是虚拟平台：不出现在平台清单里，但模型出现在 `/v1/models`。
"""
from __future__ import annotations

import asyncio
import importlib
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import db  # noqa: E402
from server.platforms.base import PlatformChat, PlatformError  # noqa: E402
from server.platforms.combo import adapter as combo_adapter  # noqa: E402
from server.routers import platforms as platforms_router  # noqa: E402
from server.services import combo  # noqa: E402

# 包 __init__ 把 `adapter` 这个名字导出成了实例（同名遮蔽子模块），
# 打补丁要拿到**模块**本身，所以按完整路径取一次。
combo_module = importlib.import_module('server.platforms.combo.adapter')


def _member(platform: str, model: str, weight: float = 1.0,
            enabled: bool = True, realm: str | None = None) -> dict:
    member = {'platform': platform, 'model': model,
              'weight': weight, 'enabled': enabled}
    if realm is not None:
        member['realm'] = realm
    return member


def _combo(name: str = 'fast', members: list[dict] | None = None,
           enabled: bool = True) -> dict:
    return {'name': name, 'enabled': enabled,
            'members': members or [_member('p1', 'm1'), _member('p2', 'm2')]}


class ComboValidateTest(unittest.TestCase):
    def test_rejects_non_list(self) -> None:
        for value in ({}, None, 'fast', 1):
            with self.assertRaises(ValueError):
                combo.validate(value)

    def test_rejects_bad_name(self) -> None:
        for name in ('', 'Fast Combo', 'a/b', '-lead', 'x' * 33):
            with self.assertRaises(ValueError, msg=f'应拒绝名字 {name!r}'):
                combo.validate([{**_combo(), 'name': name}])

    def test_rejects_duplicate_names(self) -> None:
        with self.assertRaises(ValueError):
            combo.validate([_combo(), _combo()])

    def test_rejects_empty_members(self) -> None:
        with self.assertRaises(ValueError):
            combo.validate([{**_combo(), 'members': []}])

    def test_rejects_platform_with_slash(self) -> None:
        with self.assertRaises(ValueError):
            combo.validate([_combo(members=[_member('a/b', 'm')])])

    def test_rejects_duplicate_members(self) -> None:
        with self.assertRaises(ValueError):
            combo.validate([_combo(members=[_member('p', 'm'), _member('p', 'm')])])

    def test_member_realm_is_normalized(self) -> None:
        out = combo.validate([_combo(members=[
            _member('trae', 'x', realm='intl'),
            _member('minimax', 'x', realm='global'),
            _member('qoder', 'x', realm='cn'),
        ])])
        self.assertEqual([m['realm'] for m in out[0]['members']],
                         ['global', 'global', 'cn'])

    def test_same_model_different_realm_is_not_duplicate(self) -> None:
        """双区同名模型：成员必须能分别指向两版，且选路键不能相撞。"""
        out = combo.validate([_combo(members=[
            _member('minimax', 'x', realm='cn'),
            _member('minimax', 'x', realm='global'),
            _member('minimax', 'x'),
        ])])
        members = out[0]['members']
        self.assertEqual([m['realm'] for m in members], ['cn', 'global', ''])
        self.assertEqual(len({combo.member_key(m) for m in members}), 3)

    def test_rejects_invalid_member_realm(self) -> None:
        with self.assertRaises(ValueError):
            combo.validate([_combo(members=[_member('p', 'm', realm='nope')])])

    def test_load_tolerates_bad_member_realm(self) -> None:
        """读路径宽容：坏版本当作「未指定」，不让整份配置不可用。"""
        out = combo.load([_combo(members=[_member('p', 'm', realm='nope')])])
        self.assertEqual(out[0]['members'][0]['realm'], '')

    def test_rejects_all_members_disabled(self) -> None:
        with self.assertRaises(ValueError):
            combo.validate([_combo(members=[_member('p', 'm', enabled=False)])])

    def test_load_skips_bad_entries(self) -> None:
        raw = [
            _combo(),
            'not-a-dict',
            {**_combo('BAD NAME')},
            _combo(),                      # 重名：只保留第一条
            {**_combo('empty'), 'members': []},
        ]
        out = combo.load(raw)
        self.assertEqual([c['name'] for c in out], ['fast'])

    def test_weight_is_clamped_not_rejected(self) -> None:
        out = combo.validate([_combo(members=[_member('p', 'm', weight=0)])])
        self.assertEqual(out[0]['members'][0]['weight'], combo.DEFAULT_WEIGHT)
        out = combo.validate([_combo(members=[_member('p', 'm', weight=999)])])
        self.assertEqual(out[0]['members'][0]['weight'], combo.WEIGHT_MAX)


class ComboPickTest(unittest.TestCase):
    def test_weight_distribution(self) -> None:
        """3:1 跑 400 次，首选应稳定落在 300 附近（SWRR 不是随机）。"""
        members = [_member('p', 'a', 3), _member('p', 'b', 1)]
        currents: dict[str, float] = {}
        hits = 0
        for _ in range(400):
            best, _rest = combo.pick(members, currents)
            if best['model'] == 'a':
                hits += 1
        self.assertGreaterEqual(hits, 250)
        self.assertLessEqual(hits, 350)
        # 游标被原地更新：这是「跨请求平滑」的前提。
        self.assertIn('p/a', currents)
        self.assertIn('p/b', currents)

    def test_rest_sorted_by_weight(self) -> None:
        members = [_member('p', 'a', 1), _member('p', 'b', 5), _member('p', 'c', 3)]
        currents: dict[str, float] = {}
        best, rest = combo.pick(members, currents)
        self.assertEqual(best['model'], 'b', '首轮应选权重最高的成员')
        self.assertEqual([m['model'] for m in rest], ['c', 'a'])

    def test_empty_members(self) -> None:
        self.assertEqual(combo.pick([], {}), (None, []))


class _FakeAdapter:
    """按顺序返回预设结果（异常或 PlatformChat）的假成员平台。"""

    def __init__(self, *results: object) -> None:
        self.results = list(results)
        self.calls: list[str] = []
        # 组合必须把调用方的请求上下文原样透传（WorkBuddy 成员靠它拿 key/ip）。
        self.contexts: list[object] = []

    async def chat(self, model: str, body: dict, ctx: object | None = None) -> PlatformChat:
        self.calls.append(model)
        self.contexts.append(ctx)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class ComboChatTest(unittest.TestCase):
    def setUp(self) -> None:
        # 选路游标、粘性绑定与成员冷却都是进程内状态；不重置的话相邻用例会
        # 从上一次的位置继续轮询 / 直接命中上一条用例留下的绑定。
        combo_adapter._currents.clear()
        combo_adapter._affinity.clear()
        combo_adapter._cooldowns.clear()

    def _run(self, fake_map: dict[str, _FakeAdapter], *,
             body: dict | None = None,
             raw: list[dict] | None = None):
        combos = [_combo()] if raw is None else raw
        with mock.patch.object(db, 'get_setting',
                               lambda key, default=None: combos
                               if key == 'model_combos' else default):
            with mock.patch.object(combo_module, 'get_platform',
                                   fake_map.get):
                return asyncio.run(combo_adapter.chat('fast', body or {}))

    def _pair(self, count: int = 4) -> tuple[_FakeAdapter, _FakeAdapter]:
        return (_FakeAdapter(*[PlatformChat(payload={'ok': True})
                               for _ in range(count)]),
                _FakeAdapter(*[PlatformChat(payload={'ok': True})
                               for _ in range(count)]))

    def test_routes_to_selected_member_and_fills_model(self) -> None:
        p1 = _FakeAdapter(PlatformChat(payload={'ok': True}))
        p2 = _FakeAdapter(PlatformChat(payload={'ok': True}))
        outcome = self._run({'p1': p1, 'p2': p2})
        self.assertEqual(p1.calls, ['m1'])
        self.assertEqual(p2.calls, [])
        self.assertEqual(outcome.model, 'p1/m1', '组合必须把真实去向写进 outcome.model')

    def test_switches_on_429(self) -> None:
        p1 = _FakeAdapter(PlatformError('限流', status=429, code='rate_limited'))
        p2 = _FakeAdapter(PlatformChat(payload={'ok': True}))
        outcome = self._run({'p1': p1, 'p2': p2})
        self.assertEqual(p1.calls, ['m1'])
        self.assertEqual(p2.calls, ['m2'])
        self.assertEqual(outcome.model, 'p2/m2')

    def test_does_not_switch_on_400(self) -> None:
        exc = PlatformError('参数不对', status=400, code='invalid_request')
        p1 = _FakeAdapter(exc)
        p2 = _FakeAdapter(PlatformChat(payload={'ok': True}))
        with self.assertRaises(PlatformError) as ctx:
            self._run({'p1': p1, 'p2': p2})
        self.assertIs(ctx.exception, exc, '不可切换的错误必须原样抛出')
        self.assertEqual(p2.calls, [], '400 不该切到第二个成员')

    def test_all_failed(self) -> None:
        p1 = _FakeAdapter(PlatformError('上游挂了', status=503, code='platform_error'))
        p2 = _FakeAdapter(PlatformError('也挂了', status=502, code='platform_error'))
        with self.assertRaises(PlatformError) as ctx:
            self._run({'p1': p1, 'p2': p2})
        self.assertEqual(ctx.exception.status, 503)
        self.assertEqual(ctx.exception.code, 'combo_all_failed')
        self.assertIn('p1/m1', ctx.exception.message)
        self.assertIn('p2/m2', ctx.exception.message)

    def test_unknown_platform_is_skipped(self) -> None:
        p2 = _FakeAdapter(PlatformChat(payload={'ok': True}))
        outcome = self._run({'p2': p2})   # p1 没注册
        self.assertEqual(p2.calls, ['m2'])
        self.assertEqual(outcome.model, 'p2/m2')

    def _run_with_ctx(self, fake_map: dict[str, _FakeAdapter],
                      raw: list[dict], ctx: object):
        with mock.patch.object(db, 'get_setting',
                               lambda key, default=None: raw
                               if key == 'model_combos' else default):
            with mock.patch.object(combo_module, 'get_platform',
                                   fake_map.get):
                return asyncio.run(combo_adapter.chat('fast', {}, ctx))

    def test_member_realm_is_passed_as_ctx_realm(self) -> None:
        """成员指定版本时必须透传给分版本平台，而不是让它在账号池里自己猜。"""
        p1 = _FakeAdapter(PlatformChat(payload={'ok': True}))
        p1.realm_aware = True
        raw = [_combo(members=[_member('p1', 'm1', realm='global')])]
        ctx = combo_module.ChatContext(key={'id': 'k'}, ip='10.0.0.1')
        outcome = self._run_with_ctx({'p1': p1}, raw, ctx)
        self.assertEqual(outcome.model, 'p1/m1')
        self.assertEqual(p1.contexts[0].realm, 'global')
        self.assertIsNone(ctx.realm, '不能改动调用方传入的 ctx（frozen dataclass 也不允许）')

    def test_member_realm_is_ignored_for_non_realm_aware(self) -> None:
        """非分版本平台（WorkBuddy）的版本写在模型名里，不该被额外覆盖。"""
        p1 = _FakeAdapter(PlatformChat(payload={'ok': True}))
        raw = [_combo(members=[_member('p1', 'm1', realm='global')])]
        ctx = combo_module.ChatContext(key={'id': 'k'}, ip='10.0.0.1')
        self._run_with_ctx({'p1': p1}, raw, ctx)
        self.assertIs(p1.contexts[0], ctx)

    # ── 会话粘性（prompt cache 亲和）────────────────────────
    def test_same_session_sticks_to_one_member(self) -> None:
        p1, p2 = self._pair()
        body = {'prompt_cache_key': 'conv-1'}
        first = self._run({'p1': p1, 'p2': p2}, body=body)
        for _ in range(3):
            got = self._run({'p1': p1, 'p2': p2}, body=body)
            self.assertEqual(got.model, first.model,
                             '同一会话轮换成员会让上游 prompt cache 全部作废')

    def test_new_sessions_still_follow_weights(self) -> None:
        p1, p2 = self._pair()
        a = self._run({'p1': p1, 'p2': p2}, body={'prompt_cache_key': 'conv-a'})
        b = self._run({'p1': p1, 'p2': p2}, body={'prompt_cache_key': 'conv-b'})
        self.assertNotEqual(a.model, b.model,
                            '不同会话应继续按 SWRR 轮换，否则权重失去意义')

    def test_bound_request_does_not_advance_cursor(self) -> None:
        p1, p2 = self._pair()
        body = {'prompt_cache_key': 'conv-cursor'}
        self._run({'p1': p1, 'p2': p2}, body=body)
        before = dict(combo_adapter._currents['fast'])
        self._run({'p1': p1, 'p2': p2}, body=body)
        self.assertEqual(combo_adapter._currents['fast'], before,
                         '粘性命中不该推进 SWRR 游标（长会话会把比例带偏）')

    def _bound(self, key: str = 'conv-x') -> str | None:
        return combo_adapter._affinity.get(
            combo_adapter._binding('fast', f'prompt_cache_key:{key}'))

    def test_transient_failure_fails_over_without_rebinding(self) -> None:
        p1 = _FakeAdapter(PlatformChat(payload={'ok': True}),
                          PlatformError('限流', status=429, code='rate_limited'),
                          PlatformChat(payload={'ok': True}))
        p2 = _FakeAdapter(*[PlatformChat(payload={'ok': True}) for _ in range(3)])
        body = {'prompt_cache_key': 'conv-x'}
        self.assertEqual(self._run({'p1': p1, 'p2': p2}, body=body).model, 'p1/m1')
        second = self._run({'p1': p1, 'p2': p2}, body=body)
        self.assertEqual(second.model, 'p2/m2', '绑定成员冷却时先切到别的成员')
        self.assertEqual(p1.calls, ['m1', 'm1'], '第二次失败后不再重复探测')
        self.assertEqual(self._bound(), 'p1/m1',
                         '瞬时失败不能把会话永久搬到别的平台')
        # 冷却结束后回到原成员：这才是缓存能持续命中的关键。
        combo_adapter._cooldowns.clear()
        third = self._run({'p1': p1, 'p2': p2}, body=body)
        self.assertEqual(third.model, 'p1/m1')
        self.assertEqual(p1.calls, ['m1', 'm1', 'm1'])

    def test_permanent_failure_rebinds_session(self) -> None:
        p1 = _FakeAdapter(PlatformChat(payload={'ok': True}),
                          PlatformError('模型下线', status=404,
                                        code='model_not_found'))
        p2 = _FakeAdapter(*[PlatformChat(payload={'ok': True}) for _ in range(2)])
        body = {'prompt_cache_key': 'conv-x'}
        self.assertEqual(self._run({'p1': p1, 'p2': p2}, body=body).model, 'p1/m1')
        second = self._run({'p1': p1, 'p2': p2}, body=body)
        self.assertEqual(second.model, 'p2/m2')
        self.assertEqual(self._bound(), 'p2/m2',
                         '凭据 / 模型确定不可用时应改绑，避免每轮白试')

    def test_third_member_is_reached_when_first_two_fail(self) -> None:
        p1 = _FakeAdapter(PlatformError('挂了', status=503, code='platform_error'))
        p2 = _FakeAdapter(PlatformError('也挂了', status=502, code='platform_error'))
        p3 = _FakeAdapter(PlatformChat(payload={'ok': True}))
        raw = [_combo(members=[_member('p1', 'm1'), _member('p2', 'm2'),
                               _member('p3', 'm3')])]
        outcome = self._run({'p1': p1, 'p2': p2, 'p3': p3}, raw=raw)
        self.assertEqual(outcome.model, 'p3/m3',
                         '前两个都故障时，健康的第三个成员仍应被尝试')
        self.assertEqual(p3.calls, ['m3'])

    def test_cooldown_skips_failing_member_for_other_sessions(self) -> None:
        p1 = _FakeAdapter(PlatformError('挂了', status=503, code='platform_error'))
        p2 = _FakeAdapter(*[PlatformChat(payload={'ok': True}) for _ in range(3)])
        self._run({'p1': p1, 'p2': p2}, body={'prompt_cache_key': 'conv-a'})
        other = self._run({'p1': p1, 'p2': p2},
                          body={'prompt_cache_key': 'conv-b'})
        self.assertEqual(other.model, 'p2/m2')
        self.assertEqual(p1.calls, ['m1'], '冷却中的成员不参与新会话选路')

    def test_affinity_can_be_disabled(self) -> None:
        p1, p2 = self._pair()
        body = {'prompt_cache_key': 'conv-off'}
        with mock.patch.object(combo_module.runtime_settings,
                               'session_affinity', lambda name: False):
            first = self._run({'p1': p1, 'p2': p2}, body=body)
            second = self._run({'p1': p1, 'p2': p2}, body=body)
        self.assertNotEqual(first.model, second.model, '关掉粘性后按权重轮换')
        self.assertIsNone(self._bound('conv-off'))

    def test_rotating_prompt_cache_key_falls_back_to_content(self) -> None:
        p1, p2 = self._pair()
        messages = [{'role': 'user', 'content': '同一个会话的开头'}]
        first = self._run({'p1': p1, 'p2': p2},
                          body={'prompt_cache_key': 'k1',
                                'messages': messages})
        second = self._run({'p1': p1, 'p2': p2},
                           body={'prompt_cache_key': 'k2',
                                 'messages': messages})
        self.assertEqual(first.model, second.model,
                         '显式键每轮都变时，内容指纹应把会话留在同一成员')
        self.assertIsNone(
            combo_adapter._affinity.get(
                combo_adapter._binding('fast', 'prompt_cache_key:k2')),
            '兜底命中的新显式键不该写入绑定表（随机键会撑爆容量）')

    def test_disabled_bound_member_drops_affinity(self) -> None:
        p1, p2 = self._pair()
        body = {'prompt_cache_key': 'conv-y'}
        first = self._run({'p1': p1, 'p2': p2}, body=body)
        self.assertEqual(first.model, 'p1/m1')
        raw = [_combo(members=[_member('p1', 'm1', enabled=False),
                               _member('p2', 'm2')])]
        got = self._run({'p1': p1, 'p2': p2}, body=body, raw=raw)
        self.assertEqual(got.model, 'p2/m2',
                         '绑定成员被停用后应重新按剩余成员选路')

    def test_unknown_combo(self) -> None:
        with mock.patch.object(db, 'get_setting',
                               lambda key, default=None: [] if key == 'model_combos' else default):
            with self.assertRaises(PlatformError) as ctx:
                asyncio.run(combo_adapter.chat('nope', {}))
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(ctx.exception.code, 'combo_not_found')


class ComboModelsTest(unittest.TestCase):
    def test_list_models_hides_disabled(self) -> None:
        raw = [_combo(), _combo('off', enabled=False)]
        with mock.patch.object(db, 'get_setting', lambda key, default=None: raw):
            models = asyncio.run(combo_adapter.list_models())
        self.assertEqual([m['id'] for m in models], ['combo/fast'])
        self.assertEqual(models[0]['platform'], 'combo')
        self.assertEqual(models[0]['model_name'], 'fast')

    def test_ui_visible_filters_platform_list(self) -> None:
        """组合是虚拟平台，平台下拉里不该出现它（直接调路由函数，无需鉴权）。"""
        names = [p['name'] for p in platforms_router.list_platforms({})['platforms']]
        self.assertNotIn('combo', names)
        self.assertIn('trae', names)


class ComboCatalogTest(unittest.TestCase):
    """组合成员目录：双区同名模型必须两条都在，才能分别选版本。"""

    class _FakePlatform:
        name = 'trae'
        label = 'Trae'
        ui_visible = True
        combo_member = False
        realm_aware = True

        async def list_models(self) -> list[dict]:
            return [
                {'id': 'traecn/x', 'model_name': 'x', 'realm': 'cn'},
                {'id': 'traecom/x', 'model_name': 'x', 'realm': 'intl'},
                {'id': 'traecn/y', 'model_name': 'y', 'realm': 'cn'},
            ]

    def test_catalog_keeps_realm_variants(self) -> None:
        with mock.patch.object(platforms_router, 'all_adapters',
                               lambda: [self._FakePlatform()]):
            out = asyncio.run(platforms_router.model_catalog({}))
        entry = out['platforms'][0]
        self.assertTrue(entry['realm_aware'])
        self.assertEqual([(m['model_name'], m['realm']) for m in entry['models']],
                         [('x', 'cn'), ('x', 'intl'), ('y', 'cn')])

    def test_non_realm_aware_still_dedupes_by_bare_name(self) -> None:
        """非分版本平台保持旧行为：同名条目只列一次，避免下拉出现重复项。"""

        class Fake(self._FakePlatform):
            name = 'workbuddy'
            realm_aware = False

            async def list_models(self) -> list[dict]:
                return [
                    {'id': 'workbuddy/m', 'model_name': 'm', 'realm': 'cn'},
                    {'id': 'workbuddy/m2', 'model_name': 'm', 'realm': 'global'},
                ]

        with mock.patch.object(platforms_router, 'all_adapters',
                               lambda: [Fake()]):
            out = asyncio.run(platforms_router.model_catalog({}))
        self.assertEqual([m['model_name'] for m in out['platforms'][0]['models']],
                         ['m'])


if __name__ == '__main__':
    unittest.main()
