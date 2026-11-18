"""默认列表 miss 时回落 mp 口径、以及 claimonly 任务的链路（issue #243）。

背景：
  · 上游任务中心改版后 mp 口径不再是默认口径的超集。`Library_read` 这类常规任务
    只在 mp 列表下发，`process_account` 只查默认列表时会把存在的任务判成
    「该账号没有此任务」，一项都不做。
  · `wb_wechat_oa_subscribe_task`（关注公众号满 24h）的进度由服务端计时发放，
    客户端没有可上报的动作：只能 accept → 回读 → claim，不能编造上报事件。

这里直接加载真实的 `upstream/scripts/task_runner.py`，钉住三处语义：
  1. 默认列表未命中时按 mp 口径再查一次，命中后照常执行；
  2. 两边都没有才记 skip；
  3. claimonly 只做 accept/claim，绝不构造或上报事件；未达标记 pending。
"""
from __future__ import annotations

import collections
import importlib.util
import pathlib
import sys
import types
import unittest
from unittest import mock

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCRIPTS = _ROOT / 'upstream' / 'scripts'


def _load_task_runner():
    """加载 upstream/scripts/task_runner.py（模块名唯一，避免与其他测试串味）。"""
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        'task_runner_mp_fallback_probe', _SCRIPTS / 'task_runner.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _stats() -> collections.Counter:
    return collections.Counter()


class DefaultListFallbackTest(unittest.TestCase):
    """默认列表 miss → mp 口径回落。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tr = _load_task_runner()
        cls.auth = {'uid': 'u1', 'token': 'at', 'nick': 'n'}

    def _run_account(self, task_list, mp_row, *, only=None):
        """跑 process_account，返回 (process_task 的调用参数, stats)。"""
        opts = types.SimpleNamespace(yes=False, gap=0, only_claim=False,
                                     only_codes=only)
        stats = _stats()
        calls: list[tuple] = []
        with mock.patch.object(self.tr.tc, 'do_get', return_value=(200, {'code': 0})), \
                mock.patch.object(self.tr.tc, 'list_tasks', return_value=task_list), \
                mock.patch.object(self.tr, '_mp_task_status', return_value=mp_row), \
                mock.patch.object(self.tr, 'process_task',
                                  side_effect=lambda *a: calls.append(a)):
            self.tr.process_account(self.auth, opts, stats)
        return calls, stats

    def test_missing_from_default_list_falls_back_to_mp(self) -> None:
        """只存在于 mp 列表的任务要照常执行，而不是被记成「该账号没有此任务」。"""
        row = {'task_code': 'Library_read', 'accept_status': 'not_accepted'}
        calls, stats = self._run_account([], row)
        ran = [c[1] for c in calls]
        self.assertIn('Library_read', ran)
        # 执行时用的是 mp 口径查到的那一行（不重复查第二次列表）
        self.assertEqual([c[2] for c in calls if c[1] == 'Library_read'], [row])
        self.assertEqual(stats['skip'], 0, stats)

    def test_absent_in_both_lists_still_skips(self) -> None:
        calls, stats = self._run_account([], None)
        self.assertEqual(calls, [])
        self.assertGreater(stats['skip'], 0, stats)
        self.assertIn('total', stats)

    def test_mp_probe_failure_does_not_crash(self) -> None:
        """回落查询自己失败时也要按「任务不存在」处理，不能把整轮打断。"""
        opts = types.SimpleNamespace(yes=False, gap=0, only_claim=False, only_codes=None)
        stats = _stats()
        with mock.patch.object(self.tr.tc, 'do_get', return_value=(200, {'code': 0})), \
                mock.patch.object(self.tr.tc, 'list_tasks', return_value=[]), \
                mock.patch.object(self.tr, '_mp_task_status',
                                  side_effect=RuntimeError('boom')):
            self.tr.process_account(self.auth, opts, stats)
        self.assertGreater(stats['skip'], 0, stats)

    def test_default_list_hit_does_not_probe_mp(self) -> None:
        """默认列表命中时不该为它多打一次 mp 列表（mp 专段自己的查询不算）。"""
        row = {'task_code': 'skill_1', 'accept_status': 'not_accepted'}
        opts = types.SimpleNamespace(yes=False, gap=0, only_claim=False, only_codes=None)
        stats = _stats()
        probed: list[str] = []

        def _probe(_auth, code):
            probed.append(code)
            return None

        with mock.patch.object(self.tr.tc, 'do_get', return_value=(200, {'code': 0})), \
                mock.patch.object(self.tr.tc, 'list_tasks', return_value=[row]), \
                mock.patch.object(self.tr, '_mp_task_status', side_effect=_probe), \
                mock.patch.object(self.tr, 'process_minichat_task'), \
                mock.patch.object(self.tr, 'process_task'):
            self.tr.process_account(self.auth, opts, stats)
        self.assertNotIn('skill_1', probed, '默认列表命中却还去回落 mp 口径')


class ClaimOnlyTaskTest(unittest.TestCase):
    """claimonly：服务端计时任务只 accept/claim，不造事件。"""

    CODE = 'wb_wechat_oa_subscribe_task'

    @classmethod
    def setUpClass(cls) -> None:
        cls.tr = _load_task_runner()
        cls.auth = {'uid': 'u1', 'token': 'at'}

    def test_registered_as_claimonly(self) -> None:
        spec = self.tr.MAPPING[self.CODE]
        self.assertEqual(spec['kind'], 'claimonly')
        self.assertEqual(spec['target'], 1)

    def test_accepted_but_not_reached_is_pending_without_events(self) -> None:
        opts = types.SimpleNamespace(yes=True, gap=0)
        stats = _stats()
        with mock.patch.object(self.tr, '_mp_task_status',
                               return_value={'accept_status': 'accepted',
                                             'progress': {'current': 0, 'target': 1}}), \
                mock.patch.object(self.tr, 'ids_for') as ids, \
                mock.patch.object(self.tr, 'build_event') as build, \
                mock.patch.object(self.tr, 'report_mp_event') as report, \
                mock.patch.object(self.tr.tc, 'do_post') as post, \
                mock.patch.object(self.tr.time, 'sleep'):
            self.tr.process_minichat_task(self.auth, self.CODE, opts, stats, 'u1')
        self.assertEqual(stats['pending'], 1, stats)
        self.assertEqual(stats['fail'], 0, stats)
        ids.assert_not_called()
        build.assert_not_called()
        report.assert_not_called()
        post.assert_not_called()   # 已 accept，不再重复

    def test_accept_then_reached_claims_with_mp_header(self) -> None:
        opts = types.SimpleNamespace(yes=True, gap=0)
        stats = _stats()
        states = iter([
            {'accept_status': 'not_accepted', 'progress': {'current': 0, 'target': 1}},
            {'accept_status': 'completed', 'progress': {'current': 1, 'target': 1}},
        ])
        accept_resp = (200, {'code': 0, 'data': {'results': [{'status': 'accepted'}]}})
        with mock.patch.object(self.tr, '_mp_task_status',
                               side_effect=lambda *a: next(states)), \
                mock.patch.object(self.tr, 'claim_one') as claim, \
                mock.patch.object(self.tr.tc, 'do_post', return_value=accept_resp) as post, \
                mock.patch.object(self.tr.time, 'sleep'):
            self.tr.process_minichat_task(self.auth, self.CODE, opts, stats, 'u1')
        post.assert_called_once()
        self.assertEqual(post.call_args.kwargs.get('headers'), self.tr.MP_PLATFORM_HEADER)
        claim.assert_called_once()
        self.assertEqual(claim.call_args.kwargs.get('mp'), True)
        self.assertEqual(stats['fail'], 0, stats)

    def test_accept_failure_counts_fail(self) -> None:
        opts = types.SimpleNamespace(yes=True, gap=0)
        stats = _stats()
        with mock.patch.object(self.tr, '_mp_task_status',
                               return_value={'accept_status': 'not_accepted',
                                             'progress': {'current': 0, 'target': 1}}), \
                mock.patch.object(self.tr, 'claim_one') as claim, \
                mock.patch.object(self.tr.tc, 'do_post',
                                  return_value=(200, {'code': 0, 'data': {
                                      'results': [{'status': 'task not found'}]}})), \
                mock.patch.object(self.tr.time, 'sleep'):
            self.tr.process_minichat_task(self.auth, self.CODE, opts, stats, 'u1')
        self.assertEqual(stats['fail'], 1, stats)
        claim.assert_not_called()

    def test_already_claimed_is_idempotent(self) -> None:
        opts = types.SimpleNamespace(yes=True, gap=0)
        stats = _stats()
        with mock.patch.object(self.tr, '_mp_task_status',
                               return_value={'accept_status': 'claimed'}), \
                mock.patch.object(self.tr, 'claim_one') as claim:
            self.tr.process_minichat_task(self.auth, self.CODE, opts, stats, 'u1')
        self.assertEqual(stats['already'], 1, stats)
        claim.assert_not_called()

    def test_dry_run_does_not_write(self) -> None:
        """dry-run 下连 accept 都不该发（默认不传 --yes）。"""
        opts = types.SimpleNamespace(yes=False, gap=0)
        stats = _stats()
        with mock.patch.object(self.tr, '_mp_task_status',
                               return_value={'accept_status': 'not_accepted',
                                             'progress': {'current': 0, 'target': 1}}), \
                mock.patch.object(self.tr.tc, 'do_post') as post:
            self.tr.process_minichat_task(self.auth, self.CODE, opts, stats, 'u1')
        post.assert_not_called()
        self.assertEqual(stats['pending'], 1, stats)


class MappingWiringTest(unittest.TestCase):
    """claimonly 必须走 mp 专段，不能被当常规任务用默认列表/默认头跑。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tr = _load_task_runner()

    def test_claimonly_is_excluded_from_default_codes_and_kept_in_mp(self) -> None:
        auth = {'uid': 'u1', 'token': 'at', 'nick': 'n'}
        opts = types.SimpleNamespace(yes=False, gap=0, only_claim=False, only_codes=None)
        stats = _stats()
        seen: list[str] = []
        with mock.patch.object(self.tr.tc, 'do_get', return_value=(200, {'code': 0})), \
                mock.patch.object(self.tr.tc, 'list_tasks', return_value=[]), \
                mock.patch.object(self.tr, '_mp_task_status', return_value=None), \
                mock.patch.object(self.tr, '_mp_list_tasks', return_value=[]), \
                mock.patch.object(self.tr, 'process_task',
                                  side_effect=lambda a, c, t, o, s: seen.append(c)), \
                mock.patch.object(self.tr, 'process_school_tasks'), \
                mock.patch.object(self.tr, 'process_minichat_task',
                                  side_effect=lambda a, c, o, s, u: seen.append(c)):
            self.tr.process_account(auth, opts, stats)
        self.assertIn('wb_wechat_oa_subscribe_task', seen,
                      'claimonly 任务没进入 mp 专段')
        # 而且是经 mp 段进的，不是被当常规任务跑
        self.assertEqual(seen.count('wb_wechat_oa_subscribe_task'), 1, seen)


if __name__ == '__main__':
    unittest.main()
