"""`/v1/responses` 回放补齐：Chat 形状字段、惰性项与上下文压缩。

来源：misakano7545/workbuddy2api-panel 的 0e41952f / 94a68792 / fdf05eae /
f6682159（Go 实现，本仓在 Python 适配器上按同口径重写）。判据都是「出站 chat
历史是否仍能通过上游的位置配对校验」：

  · Chat 形状的 `role=tool` 丢了 `tool_call_id` → 配对清理把调用与结果一起删；
  · Chat 形状的 assistant 丢了 `tool_calls` → 留下孤儿 tool 消息（11148）；
  · 托管工具回执落成 user 空消息 → 拆开同一批工具调用、把图片插进并行结果中间；
  · compaction 项是后续对话的交接摘要，解不出内容时不能把密文当摘要送上游。
"""
from __future__ import annotations

import base64
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import server.routers.responses as R  # noqa: E402


def _roles(out: dict) -> list[str]:
    return [m['role'] for m in out['messages']]


class ChatShapeReplayTest(unittest.TestCase):
    def test_chat_tool_message_keeps_tool_call_id_and_name(self) -> None:
        out = R.to_chat_request({'model': 'm', 'input': [
            {'type': 'function_call', 'call_id': 'c1', 'name': 'f', 'arguments': '{}'},
            {'role': 'tool', 'tool_call_id': 'c1', 'name': 'f', 'content': 'done'},
        ]})
        self.assertEqual(_roles(out), ['assistant', 'tool'])
        tool = out['messages'][1]
        self.assertEqual(tool['tool_call_id'], 'c1')
        self.assertEqual(tool['name'], 'f')
        self.assertEqual(tool['content'], 'done')

    def test_chat_tool_message_images_lifted_out(self) -> None:
        """Chat 形状的工具结果同样要拆图，不能把 base64 当文本塞进 tool content。"""
        out = R.to_chat_request({'model': 'm', 'input': [
            {'type': 'function_call', 'call_id': 'c1', 'name': 'read', 'arguments': '{}'},
            {'role': 'tool', 'tool_call_id': 'c1', 'content': [
                {'type': 'text', 'text': '截图如下'},
                {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,QUJD'}},
            ]},
        ]})
        self.assertEqual(_roles(out), ['assistant', 'tool', 'user'])
        self.assertEqual(out['messages'][1]['content'], '截图如下')
        self.assertEqual(out['messages'][2]['content'][0]['type'], 'image_url')

    def test_chat_assistant_keeps_tool_calls_and_inline_reasoning(self) -> None:
        out = R.to_chat_request({'model': 'm', 'input': [
            {'role': 'assistant', 'content': None, 'reasoning_content': 'think',
             'tool_calls': [{'id': 'c1', 'type': 'function',
                             'function': {'name': 'f', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'c1', 'content': 'ok'},
        ]})
        self.assertEqual(_roles(out), ['assistant', 'tool'])
        asst = out['messages'][0]
        self.assertEqual(asst['tool_calls'][0]['id'], 'c1')
        self.assertEqual(asst['reasoning_content'], 'think')
        # 本仓既有契约：两个字段名都写（见 attach_reasoning）。
        self.assertEqual(asst['reasoning'], 'think')
        self.assertEqual(out['messages'][1]['tool_call_id'], 'c1')

    def test_reasoning_item_and_inline_reasoning_are_merged(self) -> None:
        out = R.to_chat_request({'model': 'm', 'input': [
            {'type': 'reasoning',
             'summary': [{'type': 'summary_text', 'text': 'a'}]},
            {'role': 'assistant', 'content': 'hi', 'reasoning_content': 'b'},
        ]})
        self.assertEqual(out['messages'][0]['reasoning_content'], 'a\nb')

    def test_inline_reasoning_identical_segment_kept_once(self) -> None:
        out = R.to_chat_request({'model': 'm', 'input': [
            {'type': 'reasoning',
             'summary': [{'type': 'summary_text', 'text': 'same'}]},
            {'role': 'assistant', 'content': 'hi', 'reasoning_content': 'same'},
        ]})
        self.assertEqual(out['messages'][0]['reasoning_content'], 'same')


class InertItemReplayTest(unittest.TestCase):
    def test_hosted_tool_receipt_does_not_break_parallel_group(self) -> None:
        out = R.to_chat_request({'model': 'm', 'input': [
            {'type': 'function_call', 'call_id': 'c1', 'name': 'f', 'arguments': '{}'},
            {'type': 'function_call', 'call_id': 'c2', 'name': 'g', 'arguments': '{}'},
            {'type': 'function_call_output', 'call_id': 'c1', 'output': [
                {'type': 'input_text', 'text': 'one'},
                {'type': 'input_image', 'image_url': 'https://x/1.png'}]},
            {'type': 'web_search_call', 'id': 'ws1', 'status': 'completed'},
            {'type': 'function_call_output', 'call_id': 'c2', 'output': [
                {'type': 'input_text', 'text': 'two'},
                {'type': 'input_image', 'image_url': 'https://x/2.png'}]},
        ]})
        # 回执不产生消息，也不把第一条结果的图片提前落成 user 消息。
        self.assertEqual(_roles(out), ['assistant', 'tool', 'tool', 'user'])
        self.assertEqual([b['image_url']['url'] for b in out['messages'][3]['content']],
                         ['https://x/1.png', 'https://x/2.png'])

    def test_meta_types_produce_no_message(self) -> None:
        out = R.to_chat_request({'model': 'm', 'input': [
            {'type': 'local_shell_call', 'id': 'ls1', 'command': 'ls'},
            {'type': 'local_shell_call_output', 'id': 'ls2', 'output': 'x'},
            {'type': 'compaction_trigger'},
        ]})
        self.assertEqual(out['messages'], [])

    def test_unknown_type_with_content_still_delivered(self) -> None:
        """带内容的未知类型照旧按消息送达，不因惰性判据丢内容。"""
        out = R.to_chat_request({'model': 'm', 'input': [
            {'type': 'future_call', 'content': 'keep me'},
        ]})
        self.assertEqual(_roles(out), ['user'])
        self.assertEqual(out['messages'][0]['content'], 'keep me')


class CompactionReplayTest(unittest.TestCase):
    @staticmethod
    def _blob(obj: object) -> str:
        return base64.b64encode(json.dumps(obj).encode('utf-8')).decode('ascii')

    def test_base64_json_summary_becomes_system_message(self) -> None:
        out = R.to_chat_request({'model': 'm', 'input': [
            {'type': 'compaction', 'encrypted_content': self._blob(
                {'v': 1, 'summary': 'we refactored the parser'})},
        ]})
        self.assertEqual(_roles(out), ['system'])
        self.assertIn('we refactored the parser', out['messages'][0]['content'])
        self.assertIn('context checkpoint', out['messages'][0]['content'])

    def test_opaque_blob_dropped(self) -> None:
        """URL-safe base64 密文解不出摘要：当乱码丢弃，不送上游。"""
        out = R.to_chat_request({'model': 'm', 'input': [
            {'type': 'compaction', 'encrypted_content': 'A' * 64},
        ]})
        self.assertEqual(out['messages'], [])

    def test_plain_text_summary_kept(self) -> None:
        out = R.to_chat_request({'model': 'm', 'input': [
            {'type': 'context_compaction', 'encrypted_content': 'plain checkpoint summary'},
        ]})
        self.assertEqual(_roles(out), ['system'])
        self.assertIn('plain checkpoint summary', out['messages'][0]['content'])

    def test_empty_json_summary_produces_no_message(self) -> None:
        out = R.to_chat_request({'model': 'm', 'input': [
            {'type': 'compaction', 'encrypted_content': self._blob({'v': 1, 'summary': ''})},
        ]})
        self.assertEqual(out['messages'], [])

    def test_summary_capped_at_32k(self) -> None:
        """纯文本摘要（含空白，不像密文）超长时按 UTF-8 字节封顶 32 KiB。"""
        long = 'word ' * 10_000  # ≈50 KiB，不是 base64 形态
        out = R.to_chat_request({'model': 'm', 'input': [
            {'type': 'compaction_summary', 'encrypted_content': long},
        ]})
        content = out['messages'][0]['content']
        body = content.split('\n\n', 1)[1]
        self.assertLessEqual(len(body.encode('utf-8')), 32 << 10)
        self.assertGreater(len(body), 30_000)


class SameTurnReplayTest(unittest.TestCase):
    """同轮的助手文字与工具调用必须落在**同一条** assistant 消息上。

    Responses 把一轮拆成 reasoning / message / function_call 三个 item，Chat
    只有一条 assistant。回放成两条 assistant 时，Kimi 系会在进度行之后停住
    （模型看到「上一轮已经说完话」，把工具结果当成新一轮输入）。
    """

    def test_progress_text_merges_with_parallel_calls(self) -> None:
        out = R.to_chat_request({'model': 'm', 'input': [
            {'role': 'user', 'content': 'fix tests'},
            {'type': 'reasoning', 'summary': [{'type': 'summary_text', 'text': 'inspect'}]},
            {'type': 'message', 'role': 'assistant', 'phase': 'commentary',
             'content': [{'type': 'output_text', 'text': 'running tests'}]},
            {'type': 'reasoning', 'summary': [{'type': 'summary_text', 'text': 'run both checks'}]},
            {'type': 'function_call', 'call_id': 'c1', 'name': 'check', 'arguments': '{}'},
            {'type': 'function_call', 'call_id': 'c2', 'name': 'check', 'arguments': '{}'},
            {'type': 'function_call_output', 'call_id': 'c1', 'output': 'failed'},
            {'type': 'function_call_output', 'call_id': 'c2', 'output': 'ok'},
            {'type': 'message', 'role': 'assistant', 'content': 'done'},
        ]})
        msgs = out['messages']
        self.assertEqual(_roles(out), ['user', 'assistant', 'tool', 'tool', 'assistant'])
        merged = msgs[1]
        self.assertEqual(merged['content'], 'running tests')
        # 两条 reasoning item 按序拼接：进度前的「inspect」与调用前的「run both checks」
        # 都属于这一轮决策，不能只留最后一段。
        self.assertEqual(merged['reasoning_content'], 'inspect\nrun both checks')
        self.assertEqual([c['id'] for c in merged['tool_calls']], ['c1', 'c2'])
        self.assertEqual([m['tool_call_id'] for m in msgs[2:4]], ['c1', 'c2'])
        self.assertEqual(msgs[4]['content'], 'done')

    def test_final_answer_is_a_turn_boundary(self) -> None:
        """`phase=final_answer` 的正文是上一轮的收尾，不能被下一轮调用吞掉。

        吞掉的后果：用户看到的那句回答在回放历史里挂上了后续工具调用，
        下一轮模型以为「我还没说完」——表现是重复输出同一段结论。
        """
        out = R.to_chat_request({'model': 'm', 'input': [
            {'type': 'message', 'role': 'assistant', 'phase': 'final_answer',
             'content': 'done'},
            {'type': 'function_call', 'call_id': 'c1', 'name': 'check', 'arguments': '{}'},
        ]})
        msgs = out['messages']
        self.assertEqual(len(msgs), 2)
        self.assertNotIn('tool_calls', msgs[0])
        self.assertEqual(msgs[0]['content'], 'done')
        self.assertEqual(msgs[1]['tool_calls'][0]['id'], 'c1')

    def test_compaction_is_a_turn_boundary(self) -> None:
        """压缩项是上下文交接点，跨过它的文字与调用不属于同一轮。"""
        blob = base64.b64encode(json.dumps(
            {'v': 1, 'summary': 'earlier work'}).encode()).decode()
        out = R.to_chat_request({'model': 'm', 'input': [
            {'role': 'assistant', 'content': 'done'},
            {'type': 'compaction', 'encrypted_content': blob},
            {'type': 'function_call', 'call_id': 'c1', 'name': 'check', 'arguments': '{}'},
        ]})
        asst = [m for m in out['messages'] if m['role'] == 'assistant']
        self.assertEqual(len(asst), 2, '压缩点前后必须各自成一条 assistant')
        self.assertNotIn('tool_calls', asst[0])
        self.assertEqual(asst[1]['tool_calls'][0]['id'], 'c1')

    def test_user_turn_does_not_merge(self) -> None:
        """user 边界同样要截断：把上一轮的正文合并到下一轮调用上会串轮。"""
        out = R.to_chat_request({'model': 'm', 'input': [
            {'role': 'assistant', 'content': 'ok'},
            {'role': 'user', 'content': 'next'},
            {'type': 'function_call', 'call_id': 'c1', 'name': 'check', 'arguments': '{}'},
        ]})
        self.assertEqual(_roles(out),
                         ['assistant', 'user', 'assistant'])


if __name__ == '__main__':
    unittest.main()
