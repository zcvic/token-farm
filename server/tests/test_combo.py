"""模型组合（combo）的后端回归测试。

锁定的性质：

  · 写路径**严格**（坏配置 400，不静默丢弃），读路径**宽容**（坏条目跳过，
    不让一份历史脏数据把网关打成 5xx）；
  · 选路是确定性的平滑加权轮询，长期比例符合配置的权重；
  · 失败切换只发生在「换一个成员可能成功」的错误上（凭据 / 限流 / 额度耗尽 /
    5xx），请求本身不合法时原样抛出；全部失败时按失败原因给状态码（429 / 402 /
    404 / 503），而不是一律 503；
  · 每次选路结果（入口名 × 成员 × 成败）都会进指标，用来回答「这个成员是不是
    被反复选中后一直失败」；
  · 组合是虚拟平台：不出现在平台清单里，但模型出现在 `/v1/models`。
"""
from __future__ import annotations

import asyncio
import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db  # noqa: E402
from server.platforms.base import PlatformChat, PlatformError  # noqa: E402
from server.platforms.combo import adapter as combo_adapter  # noqa: E402
from server.routers import platforms as platforms_router  # noqa: E402
from server.services import combo, metrics  # noqa: E402

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
        # 成员实际收到的请求体：组合要在这一层就把 `model` 换成成员名。
        self.bodies: list[dict] = []

    async def chat(self, model: str, body: dict, ctx: object | None = None) -> PlatformChat:
        self.calls.append(model)
        self.contexts.append(ctx)
        self.bodies.append(dict(body or {}))
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
        # 配置快照同理：这些用例用 `mock.patch.object(db, 'get_setting')` 伪造
        # 配置，不打掉缓存就会读到上一条用例留下的那份。
        combo.invalidate()

    def _run(self, fake_map: dict[str, _FakeAdapter], *,
             body: dict | None = None,
             raw: list[dict] | None = None):
        combos = [_combo()] if raw is None else raw
        combo.invalidate()
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

    def test_switches_on_quota_exhausted(self) -> None:
        """成员积分用完是组合最典型的分摊场景：必须换一个成员，而不是整组失败。"""
        p1 = _FakeAdapter(PlatformError('额度用完', status=402,
                                        code='quota_exhausted'))
        p2 = _FakeAdapter(PlatformChat(payload={'ok': True}))
        outcome = self._run({'p1': p1, 'p2': p2})
        self.assertEqual(p2.calls, ['m2'])
        self.assertEqual(outcome.model, 'p2/m2')

    def test_switches_on_model_not_found_code(self) -> None:
        """平台不认识这个模型 id（各平台叫法不同）时，换成员是有意义的。

        它通常是 400，只看状态码会被当成「请求写错了」而原样抛出，于是整个
        组合在某个成员目录变化后一直 400。
        """
        p1 = _FakeAdapter(PlatformError('模型不存在', status=400,
                                        code='unsupported_model'))
        p2 = _FakeAdapter(PlatformChat(payload={'ok': True}))
        outcome = self._run({'p1': p1, 'p2': p2})
        self.assertEqual(p2.calls, ['m2'])
        self.assertEqual(outcome.model, 'p2/m2')

    def test_all_quota_exhausted_returns_402(self) -> None:
        """全都没额度：回 402 而不是 503，客户端才不会当成「稍后重试就好」。"""
        p1 = _FakeAdapter(PlatformError('没额度', status=402, code='quota_exhausted'))
        p2 = _FakeAdapter(PlatformError('也没额度', status=402, code='quota_exhausted'))
        with self.assertRaises(PlatformError) as ctx:
            self._run({'p1': p1, 'p2': p2})
        self.assertEqual(ctx.exception.status, 402)
        self.assertEqual(ctx.exception.code, 'combo_quota_exhausted')
        self.assertIsInstance(ctx.exception.retry_after, int)
        self.assertGreater(ctx.exception.retry_after, 0)

    def test_all_rate_limited_returns_429_with_retry_after(self) -> None:
        """全被限流：回 429 并带等待时间，否则客户端会立刻重试把成员再撞一遍。"""
        p1 = _FakeAdapter(PlatformError('限流', status=429, code='rate_limited'))
        p2 = _FakeAdapter(PlatformError('也限流', status=429, code='rate_limited'))
        with self.assertRaises(PlatformError) as ctx:
            self._run({'p1': p1, 'p2': p2})
        self.assertEqual(ctx.exception.status, 429)
        self.assertEqual(ctx.exception.code, 'combo_rate_limited')
        self.assertGreater(ctx.exception.retry_after, 0)

    def test_all_model_not_found_returns_404(self) -> None:
        """所有成员都不认识这个模型是**配置问题**：回 404，别让人一直重试。"""
        p1 = _FakeAdapter(PlatformError('模型不存在', status=404,
                                        code='model_not_found'))
        p2 = _FakeAdapter(PlatformError('也不认识', status=404,
                                        code='model_not_found'))
        with self.assertRaises(PlatformError) as ctx:
            self._run({'p1': p1, 'p2': p2})
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(ctx.exception.code, 'combo_model_not_found')

    def test_all_unsupported_model_returns_404_not_503(self) -> None:
        """Loomy / DuMate 报的是 400 `unsupported_model`：同样是配置问题。

        只看状态码会把这种「所有成员都不认这个模型」聚合成 503，客户端以为
        等一会儿就好，于是一直重试一个永远不会成功的请求。
        """
        p1 = _FakeAdapter(PlatformError('不支持该模型', status=400,
                                        code='unsupported_model'))
        p2 = _FakeAdapter(PlatformError('也不支持', status=400,
                                        code='unsupported_model'))
        with self.assertRaises(PlatformError) as ctx:
            self._run({'p1': p1, 'p2': p2})
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(ctx.exception.code, 'combo_model_not_found')

    def test_all_unauthorized_stays_503(self) -> None:
        """成员凭据全失效仍是 503：对客户端说 403 会让人去改没写错的密钥。"""
        p1 = _FakeAdapter(PlatformError('登录失效', status=401, code='login_expired'))
        p2 = _FakeAdapter(PlatformError('登录失效', status=403, code='login_expired'))
        with self.assertRaises(PlatformError) as ctx:
            self._run({'p1': p1, 'p2': p2})
        self.assertEqual(ctx.exception.status, 503)
        self.assertEqual(ctx.exception.code, 'combo_all_failed')

    def test_route_metrics_count_switch(self) -> None:
        """一次「首选失败 → 换成员成功」要在指标里留下 failed + ok 两条。"""
        metrics.reset()
        try:
            p1 = _FakeAdapter(PlatformError('挂了', status=503, code='platform_error'))
            p2 = _FakeAdapter(PlatformChat(payload={'ok': True}))
            self._run({'p1': p1, 'p2': p2})
            out = metrics.render()
        finally:
            metrics.reset()
        # 标签用的是网关的入口名（`combo/<name>`），与日志的 model 列一致。
        self.assertIn('tokenfarm_combo_route_total{combo="combo/fast",member="p1/m1",'
                      'result="failed"} 1', out)
        self.assertIn('tokenfarm_combo_route_total{combo="combo/fast",member="p2/m2",'
                      'result="ok"} 1', out)

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
        combo.invalidate()
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
        combo.invalidate()
        with mock.patch.object(db, 'get_setting',
                               lambda key, default=None: [] if key == 'model_combos' else default):
            with self.assertRaises(PlatformError) as ctx:
                asyncio.run(combo_adapter.chat('nope', {}))
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(ctx.exception.code, 'combo_not_found')

    # ── 模型 id 下发 ────────────────────────────────────────
    def test_member_body_carries_member_model_not_combo_name(self) -> None:
        """组合入口名绝不能漏给上游：成员收到的 `model` 必须是成员名。

        这是「只在组合路径上出现」的那类故障——直连时 body 里本来就是真名，
        所以任何直连测试都发现不了。锁在组合这一层，新增成员平台不必再各自
        记得改写。
        """
        p1 = _FakeAdapter(PlatformChat(payload={'ok': True}))
        p2 = _FakeAdapter(PlatformChat(payload={'ok': True}))
        self._run({'p1': p1, 'p2': p2},
                  body={'model': 'combo/fast', 'messages': [{'role': 'user'}]})
        got = p1.bodies + p2.bodies
        self.assertEqual(len(got), 1, '只调用被选中的那一个成员')
        self.assertEqual(got[0]['model'], 'm1')
        self.assertEqual(got[0]['messages'], [{'role': 'user'}], '其余字段原样透传')

    def test_member_body_is_a_copy(self) -> None:
        """每个成员各拿一份副本：切成员不能把上一个成员的模型名带过去。"""
        p1 = _FakeAdapter(PlatformError('限流', status=429, code='rate_limited'))
        p2 = _FakeAdapter(PlatformChat(payload={'ok': True}))
        original = {'model': 'combo/fast'}
        self._run({'p1': p1, 'p2': p2}, body=original)
        self.assertEqual(p1.bodies[0]['model'], 'm1')
        self.assertEqual(p2.bodies[0]['model'], 'm2')
        self.assertEqual(original['model'], 'combo/fast', '调用方的 body 不能被改写')


class ComboSnapshotDbTest(unittest.TestCase):
    """配置快照的失效时机：保存后必须**立即**生效，不能等 TTL。

    以前网关每个组合请求都 `db.get_setting` + `load()`。加了进程内缓存之后，
    「保存了但请求还用旧配置」就成了唯一的新风险——尤其是 PG 恢复走整库替换、
    根本不经过保存路由。这里钉住两条写路径都会让缓存失效。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'combo-snap.db'
        db._conn = None
        db.connect()
        combo.invalidate()

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig
        combo.invalidate()
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    @staticmethod
    def _raw(name: str) -> list[dict]:
        return [{'name': name, 'enabled': True,
                 'members': [{'platform': 'p1', 'model': 'm1',
                              'weight': 1, 'enabled': True}]}]

    def test_set_setting_invalidates_immediately(self) -> None:
        db.set_setting('model_combos', self._raw('one'))
        self.assertEqual(combo.cached_names(), ['one'])
        before = combo.generation()
        db.set_setting('model_combos', self._raw('two'))
        self.assertGreater(combo.generation(), before, '写入必须触发失效')
        self.assertEqual(combo.cached_names(), ['two'],
                         '保存后下一次读就该是新配置，不能等 TTL')

    def test_other_settings_do_not_invalidate(self) -> None:
        """无关设置变了不该白清组合缓存（每请求读库的开销就回来了）。"""
        db.set_setting('model_combos', self._raw('one'))
        combo.cached_names()
        before = combo.generation()
        db.set_setting('something_else', 1)
        self.assertEqual(combo.generation(), before)

    def test_replace_database_invalidates_everything(self) -> None:
        """PG 恢复走整库替换，不经过 `set_setting`：必须靠存储层通知清缓存。"""
        db.set_setting('model_combos', self._raw('one'))
        combo.cached_names()
        before = combo.generation()
        db._notify_setting_change(None)
        self.assertGreater(combo.generation(), before)
        self.assertEqual(combo.cached_names(), ['one'],
                         '清缓存只是重新读库，配置本身还在')

    def test_read_failure_falls_back_to_last_snapshot(self) -> None:
        """读库失败时退回上一份快照：一份读不到的配置不该让组合突然全部 404。"""
        db.set_setting('model_combos', self._raw('one'))
        self.assertEqual(combo.cached_names(), ['one'])
        combo.invalidate()
        with mock.patch.object(db, 'get_setting',
                               side_effect=RuntimeError('db down')):
            self.assertEqual(combo.cached_names(), ['one'])


class ComboModelsTest(unittest.TestCase):
    def test_list_models_hides_disabled(self) -> None:
        raw = [_combo(), _combo('off', enabled=False)]
        combo.invalidate()
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


class ComboCacheInvalidationTest(unittest.TestCase):
    """配置快照的缓存与失效（不碰真实库，用补丁模拟读写）。

    锁定的性质：

      · 连续读只读一次库（否则加缓存就没有意义）；
      · 保存后**立即**看到新值，不必等 TTL；
      · 整库替换（PG 恢复）同样立即失效；
      · 读库失败时退回上一份快照，而不是让网关 500 或突然「没有组合」。
    """

    def setUp(self) -> None:
        combo.invalidate()
        self.calls = 0

    def tearDown(self) -> None:
        combo.invalidate()

    def _patch(self, value: object):
        def fake(key: str, default=None):
            self.calls += 1
            return value if key == 'model_combos' else default

        return mock.patch.object(db, 'get_setting', fake)

    def test_repeated_reads_hit_the_cache(self) -> None:
        raw = [_combo()]
        with self._patch(raw):
            for _ in range(5):
                self.assertEqual([c['name'] for c in combo.snapshot()], ['fast'])
        self.assertEqual(self.calls, 1, '同一份配置只该读一次库')

    def test_saving_a_setting_invalidates(self) -> None:
        """真实写路径：`set_setting` 之后下一次读必须看到新值。

        这里只验证**通知已挂在写路径上**（打补丁替换落库本身），不去动真实库：
        `test_combo.py` 的其余用例都不依赖数据库。
        """
        with self._patch([_combo()]):
            combo.snapshot()
            before = combo.generation()
            with mock.patch.object(db, 'execute'):
                db.set_setting('model_combos', [])
            self.assertEqual(combo.generation(), before + 1)

    def test_unrelated_setting_keeps_the_cache(self) -> None:
        """别的设置写入不该把组合缓存打掉（否则每次改配置都白读一遍库）。"""
        with self._patch([_combo()]):
            combo.snapshot()
            before = combo.generation()
            with mock.patch.object(db, 'execute'):
                db.set_setting('gateway_cors_origins', ['https://example.com'])
            self.assertEqual(combo.generation(), before)
            combo.snapshot()
        self.assertEqual(self.calls, 1)

    def test_database_replacement_invalidates_everything(self) -> None:
        """PG 恢复走整库替换，不经过 `set_setting`，也必须清缓存。"""
        with self._patch([_combo()]):
            combo.snapshot()
            before = combo.generation()
            db._notify_setting_change(None)
            self.assertEqual(combo.generation(), before + 1)

    def test_read_failure_falls_back_to_previous_snapshot(self) -> None:
        with self._patch([_combo()]):
            combo.snapshot()

        def boom(key: str, default=None):
            raise RuntimeError('库坏了')

        with mock.patch.object(combo, 'CACHE_TTL', -1), \
                mock.patch.object(db, 'get_setting', boom):
            self.assertEqual([c['name'] for c in combo.snapshot()], ['fast'],
                             '读库失败时用上一份，不能突然「没有组合」')

    def test_failed_read_without_snapshot_is_empty(self) -> None:
        """连「上一份」都没有时（进程刚起就读库失败），只能给空列表。

        `invalidate()` 会**保留**旧值当兜底，所以这里要手动把模块状态清成
        真正的「从未读过」——否则上一条用例留下的快照会被当成兜底返回。
        """
        with combo._cache_lock:
            combo._cache = None

        def boom(key: str, default=None):
            raise RuntimeError('库坏了')

        with mock.patch.object(db, 'get_setting', boom):
            self.assertEqual(combo.snapshot(), [])

    def test_stale_read_does_not_overwrite_newer_config(self) -> None:
        """读库期间配置被改：那份旧值不能再写进缓存。

        没有这道代数检查时，一次早于保存的读会把旧配置存回去，直到 TTL 过期，
        表现为「保存了但请求还用旧值」。
        """
        combo.invalidate()
        state = {'value': [_combo()]}

        def slow_read(key: str, default=None):
            # 模拟「读到了旧值 → 还没写缓存，保存发生了」。
            value = state['value']
            state['value'] = [_combo('other')]
            db._notify_setting_change('model_combos')
            return value

        with mock.patch.object(db, 'get_setting', slow_read):
            combo.snapshot()
        with self._patch([_combo('other')]):
            self.assertEqual([c['name'] for c in combo.snapshot()], ['other'],
                             '旧值不能覆盖保存后的新配置')


class ComboErrorTypeTest(unittest.TestCase):
    """聚合出的状态码要落到**正确的** `error.type`，而不只是状态码对。

    `_platform_err_type` 只看状态码时，402 会落到 `invalid_request_error`——
    客户端据此会去改请求体，而真正该做的是等额度恢复。这条只在组合新增 402 /
    429 聚合之后才会触发，所以钉在组合用例里。
    """

    def test_quota_and_rate_limit_are_not_invalid_request(self) -> None:
        from server.routers import gateway

        self.assertEqual(gateway._platform_err_type(402, 'combo_quota_exhausted'),
                         'insufficient_quota')
        self.assertEqual(gateway._platform_err_type(402, 'quota_exceeded'),
                         'insufficient_quota')
        self.assertEqual(gateway._platform_err_type(429, 'combo_rate_limited'),
                         'rate_limit_error')
        # 其余 4xx 仍是「请求写错了」，不要被上面的规则扩大化。
        self.assertEqual(gateway._platform_err_type(400, 'invalid_request'),
                         'invalid_request_error')
        self.assertEqual(gateway._platform_err_type(404, 'combo_model_not_found'),
                         'invalid_request_error')
        self.assertEqual(gateway._platform_err_type(503, 'combo_all_failed'),
                         'api_error')


class ComboCachedLookupTest(unittest.TestCase):
    """`cached()` / `cached_names()` 与 `find()` / `names()` 口径一致。"""

    def setUp(self) -> None:
        combo.invalidate()

    def tearDown(self) -> None:
        combo.invalidate()

    def test_lookup_matches_pure_functions(self) -> None:
        raw = [_combo(), _combo('off', enabled=False),
               _combo('empty', members=[_member('p1', 'm1', enabled=False)])]
        with mock.patch.object(db, 'get_setting',
                               lambda key, default=None: raw
                               if key == 'model_combos' else default):
            self.assertEqual(combo.cached('FAST')['name'], 'fast',
                             '名字大小写不敏感，与保存时的归一化一致')
            self.assertIsNone(combo.cached('off'), '停用的组合不可用')
            self.assertIsNone(combo.cached(''), '空名字不该命中')
            self.assertEqual(combo.cached_names(), ['fast'],
                             '没有启用成员（或停用）的组合不进模型清单')
            self.assertEqual(combo.cached_names(), combo.names(raw))
            self.assertEqual(combo.cached('fast'), combo.find(raw, 'fast'))


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
