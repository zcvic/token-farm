"""Cline 模型可用性信号：排序策略与保鲜期。

只收真实流量的被动观测（上游 `cline2api` 的主动 15 分钟补测**不取**：本仓
没有平台级定时探针的宿主，额外打请求也会消耗免费额度）。
"""
from __future__ import annotations

import unittest

from server.platforms.cline.availability import (FAIL_TTL, OK_TTL, ModelHealth)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class ModelHealthTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = _Clock()
        self.health = ModelHealth(clock=self.clock)

    def test_unknown_models_keep_original_order(self) -> None:
        models = [{'id': 'a'}, {'id': 'b'}, {'id': 'c'}]
        self.assertEqual([m['id'] for m in self.health.sort(list(models))],
                         ['a', 'b', 'c'], '没有信号时顺序必须与改造前一致')
        self.assertEqual(self.health.rank('a'), 1)

    def test_ok_floats_to_top_and_fail_sinks(self) -> None:
        self.health.record_fail('b', 500, 'empty response content')
        self.health.record_ok('c')
        models = [{'id': 'a'}, {'id': 'b'}, {'id': 'c'}]
        self.assertEqual([m['id'] for m in self.health.sort(models)],
                         ['c', 'a', 'b'], '可用 → 未知 → 失败')

    def test_account_level_errors_do_not_count(self) -> None:
        for status in (401, 402, 403):
            self.health.record_fail('m', status, 'auth')
        self.assertEqual(self.health.state('m'), (None, ''),
                         '账号 / 余额错误与模型无关，不能把模型判死')

    def test_fail_signal_expires_after_ttl(self) -> None:
        self.health.record_fail('m', 500)
        self.assertEqual(self.health.state('m')[0], False)
        self.clock.now += FAIL_TTL + 1
        self.assertEqual(self.health.state('m'), (None, ''),
                         '过期后回到未知，不能永久沉底')

    def test_ok_signal_expires_later_than_fail(self) -> None:
        self.health.record_ok('m')
        self.clock.now += FAIL_TTL + 1
        self.assertEqual(self.health.state('m')[0], True,
                         '成功信号保鲜期更长')
        self.clock.now += OK_TTL
        self.assertEqual(self.health.state('m')[0], None)

    def test_record_unavailable_bypasses_status_filter(self) -> None:
        """流内伪装成正常帧的限流没有状态码，但模型确实没产出。"""
        self.health.record_unavailable('m', 'Daily free limit')
        self.assertEqual(self.health.state('m'), (False, 'Daily free limit'))

    def test_model_ids_are_case_insensitive(self) -> None:
        self.health.record_ok('Cline-Free/Model')
        self.assertEqual(self.health.rank('cline-free/model'), 0)


if __name__ == '__main__':
    unittest.main()
