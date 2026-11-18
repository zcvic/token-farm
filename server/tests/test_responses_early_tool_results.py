"""早到工具结果重排（`/v1/responses` → chat 历史）。

Codex 经 cc-switch 类本地代理转发时，`function_call_output` 会排在
`function_call` 之前；`convertInputItems` 按原始顺序落消息，于是
`role=tool` 出现在声明它的 assistant 之前。上游按位置配对，判
`11148 tool_call_sequence_broken`，整条会话从此不可继续。

重排只调顺序、不改内容；归属判定按「已声明、未应答」计数，不能
「同 id 跟最后一次声明走」——网关在客户端没给 id 时按轮内下标补
`call_0` / `toolu_0`，同一会话每轮都可能有一个 `call_0`。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import server.routers.responses as R  # noqa: E402


def _roles(out: dict) -> list[str]:
    return [m['role'] for m in out['messages']]


class ReorderEarlyToolResultsTest(unittest.TestCase):
    def test_early_result_moves_after_its_call(self) -> None:
        out = R.to_chat_request({'model': 'm', 'input': [
            {'type': 'function_call_output', 'call_id': 'c1', 'output': 'early'},
            {'type': 'function_call', 'call_id': 'c1', 'name': 'f', 'arguments': '{}'},
        ]})
        self.assertEqual(_roles(out), ['assistant', 'tool'])
        self.assertEqual(out['messages'][1]['content'], 'early')
        self.assertEqual(out['messages'][1]['tool_call_id'], 'c1')

    def test_normal_order_unchanged(self) -> None:
        out = R.to_chat_request({'model': 'm', 'input': [
            {'type': 'function_call', 'call_id': 'c1', 'name': 'f', 'arguments': '{}'},
            {'type': 'function_call_output', 'call_id': 'c1', 'output': 'ok'},
        ]})
        self.assertEqual(_roles(out), ['assistant', 'tool'])

    def test_multiple_early_results_keep_relative_order(self) -> None:
        out = R.to_chat_request({'model': 'm', 'input': [
            {'type': 'function_call_output', 'call_id': 'c1', 'output': 'one'},
            {'type': 'function_call_output', 'call_id': 'c2', 'output': 'two'},
            {'type': 'function_call', 'call_id': 'c1', 'name': 'f', 'arguments': '{}'},
            {'type': 'function_call', 'call_id': 'c2', 'name': 'g', 'arguments': '{}'},
        ]})
        # 连续的 function_call 合并进同一条 assistant（既有契约），两条早到结果
        # 按原相对顺序补在它之后。
        self.assertEqual(_roles(out), ['assistant', 'tool', 'tool'])
        self.assertEqual(out['messages'][1]['content'], 'one')
        self.assertEqual(out['messages'][2]['content'], 'two')

    def test_early_result_follows_whole_tool_group(self) -> None:
        """早到结果补在声明之后**连续 tool 结果之后**，不能插进正常结果中间。"""
        out = R.to_chat_request({'model': 'm', 'input': [
            {'type': 'function_call_output', 'call_id': 'c2', 'output': 'early'},
            {'type': 'function_call', 'call_id': 'c1', 'name': 'f', 'arguments': '{}'},
            {'type': 'function_call_output', 'call_id': 'c1', 'output': 'normal'},
            {'type': 'function_call', 'call_id': 'c2', 'name': 'g', 'arguments': '{}'},
        ]})
        self.assertEqual(_roles(out), ['assistant', 'tool', 'assistant', 'tool'])
        self.assertEqual(out['messages'][1]['content'], 'normal')
        self.assertEqual(out['messages'][3]['content'], 'early')

    def test_id_reuse_across_turns_keeps_first_round_in_place(self) -> None:
        """跨轮复用 call_0：第一轮顺序正确的结果不能被搬到最后一轮声明之后。"""
        out = R.to_chat_request({'model': 'm', 'input': [
            {'type': 'function_call', 'call_id': 'call_0', 'name': 'f', 'arguments': '{}'},
            {'type': 'function_call_output', 'call_id': 'call_0', 'output': 'one'},
            {'type': 'message', 'role': 'user', 'content': 'next'},
            {'type': 'function_call_output', 'call_id': 'call_0', 'output': 'two'},
            {'type': 'function_call', 'call_id': 'call_0', 'name': 'f', 'arguments': '{}'},
        ]})
        self.assertEqual(_roles(out),
                         ['assistant', 'tool', 'user', 'assistant', 'tool'])
        self.assertEqual(out['messages'][1]['content'], 'one')
        self.assertEqual(out['messages'][4]['content'], 'two')

    def test_orphan_result_untouched(self) -> None:
        """前后都找不到声明者的孤儿结果保持原样（既有降级逻辑负责）。"""
        out = R.to_chat_request({'model': 'm', 'input': [
            {'type': 'function_call_output', 'call_id': 'ghost', 'output': 'r'},
            {'type': 'function_call', 'call_id': 'c1', 'name': 'f', 'arguments': '{}'},
            {'type': 'function_call_output', 'call_id': 'c1', 'output': 'ok'},
        ]})
        self.assertEqual(_roles(out), ['tool', 'assistant', 'tool'])
        self.assertEqual(out['messages'][0]['content'], 'r')


if __name__ == '__main__':
    unittest.main()
