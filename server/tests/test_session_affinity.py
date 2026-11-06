"""会话亲和（prompt cache 亲和）的通用实现。

组合与 Trae 账号选路都依赖这里：键的优先级、超长键的定长化、绑定表的
TTL / 容量上限，任何一处漂移都会让「同一段对话换上游目标」重新出现。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.services import session_affinity as sa  # noqa: E402


class AffinityKeyTest(unittest.TestCase):
    """会话键：越显式的来源越优先，什么都没有时用首条 user 消息兜底。"""

    def test_prompt_cache_key_wins(self) -> None:
        body = {'prompt_cache_key': 'k', 'conversation_id': 'c', 'user': 'u'}
        self.assertEqual(sa.affinity_key(body), 'prompt_cache_key:k')

    def test_conversation_id_variants(self) -> None:
        self.assertEqual(sa.affinity_key({'conversation_id': 'c1'}),
                         'conversation_id:c1')
        self.assertEqual(sa.affinity_key({'conversationId': 'c2'}),
                         'conversationId:c2')
        self.assertEqual(sa.affinity_key({'conversation': {'id': 'c3'}}),
                         'conversation:c3')
        self.assertEqual(sa.affinity_key({'conversation': 'c4'}),
                         'conversation:c4')

    def test_metadata_session_id(self) -> None:
        self.assertEqual(
            sa.affinity_key({'metadata': {'session_id': 's1', 'user_id': 'u'}}),
            'metadata.session_id:s1')

    def test_message_hash_fallback(self) -> None:
        body = {'messages': [{'role': 'system', 'content': 'sys'},
                             {'role': 'user', 'content': 'hello'}]}
        first = sa.affinity_key(body)
        self.assertTrue(first.startswith('messages:'))
        self.assertEqual(first, sa.affinity_key(body))
        self.assertNotEqual(first, sa.affinity_key(
            {'messages': [{'role': 'user', 'content': 'hello2'}]}))
        # OpenAI part 数组写法也应认出同一段文本
        parts = {'messages': [{'role': 'user', 'content': [
            {'type': 'text', 'text': 'hello'}]}]}
        self.assertEqual(sa.affinity_key(parts), first)

    def test_no_session_dimension(self) -> None:
        for body in (None, {}, {'user': 'alice'}, {'messages': []},
                     {'messages': [{'role': 'assistant', 'content': 'x'}]}):
            self.assertEqual(sa.affinity_key(body), '',
                             f'{body!r} 不该被当成会话键')

    def test_long_key_is_bounded(self) -> None:
        """会话键长度不受客户端控制：超长键要哈希，否则绑定表能被撑爆。"""
        huge = 'x' * 100_000
        first = sa.affinity_key({'prompt_cache_key': huge})
        self.assertIn('sha256:', first)
        self.assertLess(len(first), 128)
        self.assertEqual(first, sa.affinity_key({'prompt_cache_key': huge}))

    def test_header_key_keeps_ascii(self) -> None:
        self.assertEqual(sa.header_key('prompt_cache_key:sess-1'),
                         'prompt_cache_key:sess-1')
        self.assertEqual(sa.header_key(''), '')

    def test_header_key_hashes_non_ascii_and_control(self) -> None:
        """HTTP 头只收 ASCII：中文键 / 换行键要转定长哈希，否则请求发不出去。"""
        for raw in ('会话-1', 'sess\ninject: x', 'sess\x00'):
            value = sa.header_key(raw)
            self.assertIn('sha256:', value, raw)
            self.assertLessEqual(len(value), 40)
            self.assertEqual(value, sa.header_key(raw), '同一键必须稳定')
        self.assertNotEqual(sa.header_key('会话-1'), sa.header_key('会话-2'))


class SessionAffinityTest(unittest.TestCase):
    def test_bind_and_get(self) -> None:
        table = sa.SessionAffinity()
        self.assertIsNone(table.get('s1'))
        table.bind('s1', 'uid-a')
        self.assertEqual(table.get('s1'), 'uid-a')

    def test_empty_key_or_target_is_ignored(self) -> None:
        table = sa.SessionAffinity()
        table.bind('', 'uid-a')
        table.bind('s1', '')
        self.assertEqual(len(table), 0)

    def test_expired_binding_is_ignored(self) -> None:
        table = sa.SessionAffinity(ttl=-1)   # 绑定即过期
        table.bind('s1', 'uid-a')
        self.assertIsNone(table.get('s1'))
        self.assertEqual(len(table), 0, '过期项应在读取时清掉')

    def test_unbind_and_clear(self) -> None:
        table = sa.SessionAffinity()
        table.bind('s1', 'uid-a')
        table.bind('s2', 'uid-b')
        table.unbind('s1')
        self.assertIsNone(table.get('s1'))
        self.assertEqual(table.get('s2'), 'uid-b')
        table.clear()
        self.assertEqual(len(table), 0)

    def test_capacity_evicts_oldest(self) -> None:
        table = sa.SessionAffinity(max_entries=3)
        for index in range(5):
            table.bind(f's{index}', f'uid-{index}')
        self.assertEqual(len(table), 3)
        self.assertIsNone(table.get('s0'))
        self.assertIsNone(table.get('s1'))
        self.assertEqual(table.get('s2'), 'uid-2')
        self.assertEqual(table.get('s4'), 'uid-4')


if __name__ == '__main__':
    unittest.main()
