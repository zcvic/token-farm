"""领奖回读与 accept 幂等态（`already_accepted`）的回归用例。

背景：
  · 上游 claim 存在 `200 + code=0` 但实际没有入账的形态，只看信封会记假账；
    L0NE-6/WorkBuddy-Daily 的 `_claim_readback` 用「回读任务行」补这层证据。
  · accept 的响应在「此前已接过」时返回 `already_accepted`，旧判定只认
    `accepted`，会把幂等成功当失败重试（白白多打一轮 accept）。

这里直接加载真实的 `upstream/scripts/task_runner.py`，钉住两处语义。
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
        'task_runner_claim_readback_probe', _SCRIPTS / 'task_runner.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _stats() -> collections.Counter:
    return collections.Counter()


class ClaimReceiptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tr = _load_task_runner()
        cls.auth = {'uid': 'u1', 'token': 'at'}

    def _parse(self, data, *, mp=False, stats=None):
        stats = stats if stats is not None else _stats()
        with mock.patch.object(self.tr.time, 'sleep'):
            rc = self.tr._claim_parse(
                self.auth, 'Task_1', 'u1', stats, 0, 200,
                {'code': 0, 'data': data}, mp=mp)
        return rc, stats

    def test_reward_in_receipt_counts_without_readback(self) -> None:
        """回执自带实际奖励 → 直接记入账，不必多打一次 list_tasks。"""
        with mock.patch.object(self.tr.tc, 'task_status') as readback:
            rc, stats = self._parse({'credit': 100, 'energy': 5})
        self.assertEqual(rc, 1)
        self.assertEqual(stats['ok'], 1)
        self.assertEqual(stats['credit'], 100)
        self.assertEqual(stats['energy'], 5)
        readback.assert_not_called()

    def test_already_claimed_is_idempotent_success(self) -> None:
        rc, stats = self._parse({'already_claimed': True, 'credit': 0, 'energy': 0})
        self.assertEqual(rc, 2)
        self.assertEqual(stats['already'], 1)
        self.assertEqual(stats['ok'], 0)

    def test_empty_receipt_readback_claimed_counts(self) -> None:
        """回执没有奖励字段，但回读任务已是 claimed → 记成功（金额未知不加）。"""
        with mock.patch.object(self.tr.tc, 'task_status',
                               return_value={'accept_status': 'claimed'}) as readback:
            rc, stats = self._parse({})
        self.assertEqual(rc, 1)
        self.assertEqual(stats['ok'], 1)
        self.assertEqual(stats['credit'], 0)
        readback.assert_called_once()

    def test_readback_none_does_not_count_credit(self) -> None:
        """回读读不到 claimed → 不记新入账（上游可能只是静默丢弃）。"""
        with mock.patch.object(self.tr.tc, 'task_status',
                               return_value={'accept_status': 'completed'}):
            rc, stats = self._parse({})
        self.assertEqual(rc, 0)
        self.assertEqual(stats['ok'], 0)
        self.assertEqual(stats['fail'], 1)
        self.assertEqual(stats['credit'], 0)

    def test_readback_claimed_true(self) -> None:
        with mock.patch.object(self.tr.tc, 'task_status',
                               return_value={'accept_status': 'completed', 'claimed': True}):
            rc, stats = self._parse({})
        self.assertEqual(rc, 1)
        self.assertEqual(stats['ok'], 1)

    def test_readback_actual_reward_fields(self) -> None:
        with mock.patch.object(self.tr.tc, 'task_status',
                               return_value={'accept_status': 'completed', 'actual_credit': 100}):
            rc, stats = self._parse({})
        self.assertEqual(rc, 1)
        self.assertEqual(stats['ok'], 1)

    def test_readback_task_config_reward_is_not_evidence(self) -> None:
        """任务行里的 reward_credit/reward_energy 是配置值，不是到账凭据。"""
        with mock.patch.object(self.tr.tc, 'task_status',
                               return_value={'accept_status': 'completed',
                                             'reward_credit': 100, 'reward_energy': 5}):
            rc, stats = self._parse({})
        self.assertEqual(rc, 0)
        self.assertEqual(stats['fail'], 1)

    def test_readback_error_is_not_success(self) -> None:
        with mock.patch.object(self.tr.tc, 'task_status',
                               side_effect=RuntimeError('list_tasks http=500')):
            rc, stats = self._parse({})
        self.assertEqual(rc, 0)
        self.assertEqual(stats['fail'], 1)
        self.assertEqual(stats['credit'], 0)

    def test_mp_readback_uses_mp_endpoint(self) -> None:
        with mock.patch.object(self.tr.tc, 'task_status') as cn, \
                mock.patch.object(self.tr, '_mp_task_status',
                                  return_value={'claimed': True}) as mp:
            rc, stats = self._parse({}, mp=True)
        self.assertEqual(rc, 1)
        self.assertEqual(stats['ok'], 1)
        mp.assert_called_once()
        cn.assert_not_called()

    def test_http_failure_is_failure(self) -> None:
        stats = _stats()
        with mock.patch.object(self.tr.time, 'sleep'):
            rc = self.tr._claim_parse(
                self.auth, 'Task_1', 'u1', stats, 0, 500, {'code': 500, 'msg': 'boom'})
        self.assertEqual(rc, 0)
        self.assertEqual(stats['fail'], 1)


class AcceptIdempotentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tr = _load_task_runner()
        cls.auth = {'uid': 'u1', 'token': 'at'}

    def test_already_accepted_is_success_without_retry(self) -> None:
        """响应 already_accepted + 回读 accepted → 成功，不再打第二次 accept。"""
        resp = (200, {'code': 0, 'data': {'results': [{'status': 'already_accepted'}]}})
        with mock.patch.object(self.tr.tc, 'accept_tasks', return_value=resp) as accept, \
                mock.patch.object(self.tr.tc, 'task_status',
                                  return_value={'accept_status': 'accepted'}), \
                mock.patch.object(self.tr.time, 'sleep'):
            ok = self.tr._accept_with_verify(self.auth, 'Task_1', 'u1', 0)
        self.assertTrue(ok)
        accept.assert_called_once()

    def test_readback_not_accepted_retries_once(self) -> None:
        """响应说 accepted 但回读未落账 → 再试一次；第二次回读 completed 即成功。"""
        resp = (200, {'code': 0, 'data': {'results': [{'status': 'accepted'}]}})
        states = iter([
            {'accept_status': 'not_accepted'},
            {'accept_status': 'completed'},
        ])
        with mock.patch.object(self.tr.tc, 'accept_tasks', return_value=resp) as accept, \
                mock.patch.object(self.tr.tc, 'task_status', side_effect=lambda *a: next(states)), \
                mock.patch.object(self.tr.time, 'sleep'):
            ok = self.tr._accept_with_verify(self.auth, 'Task_1', 'u1', 0)
        self.assertTrue(ok)
        self.assertEqual(accept.call_count, 2)

    def test_readback_not_accepted_twice_fails(self) -> None:
        resp = (200, {'code': 0, 'data': {'results': [{'status': 'accepted'}]}})
        with mock.patch.object(self.tr.tc, 'accept_tasks', return_value=resp) as accept, \
                mock.patch.object(self.tr.tc, 'task_status',
                                  return_value={'accept_status': 'not_accepted'}), \
                mock.patch.object(self.tr.time, 'sleep'):
            ok = self.tr._accept_with_verify(self.auth, 'Task_1', 'u1', 0)
        self.assertFalse(ok)
        self.assertEqual(accept.call_count, 2)

    def test_mp_accept_already_accepted_is_not_failure(self) -> None:
        """小程序 accept 分支也认 already_accepted，不再计入 fail。"""
        opts = types.SimpleNamespace(yes=True, gap=0)
        stats = _stats()
        states = iter([
            {'accept_status': 'not_accepted', 'progress': {'current': 0, 'target': 1}},
            {'accept_status': 'accepted', 'progress': {'current': 0, 'target': 1}},
            {'accept_status': 'claimed'},
        ])
        accept_resp = (200, {'code': 0, 'data': {'results': [{'status': 'already_accepted'}]}})
        with mock.patch.object(self.tr, '_mp_task_status',
                               side_effect=lambda *a: next(states)), \
                mock.patch.object(self.tr, 'ids_for', return_value=[('obj', {})]), \
                mock.patch.object(self.tr, 'build_event', return_value={'eventCode': 'x'}), \
                mock.patch.object(self.tr, 'report_mp_event', return_value=(200, 0)), \
                mock.patch.object(self.tr.tc, 'do_post', return_value=accept_resp) as post, \
                mock.patch.object(self.tr.time, 'sleep'):
            self.tr.process_minichat_task(self.auth, 'Sequential_Tasks_1', opts, stats, 'u1')
        self.assertEqual(stats['fail'], 0, stats)
        self.assertEqual(stats['already'], 1, stats)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs.get('headers'), self.tr.MP_PLATFORM_HEADER)


if __name__ == '__main__':
    unittest.main()
