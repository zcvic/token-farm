"""正文工具调用还原：属性测试（等价类 + 分片无关 + 流式/非流式同口径）。

这里不测「某个具体样本要不要还原」，而是测**不变量**：

1. `Sniffer` 的输出只取决于缓冲区内容（`_buf` / `_fence` / `_line_start`），
   没有隐藏的分片状态，所以「逐字符喂」与「任意切分喂」必须完全一样。
   逐字符等价于穷举所有切分，比逐个样例试几种切法强得多。
2. 同一份输入，`translate()`（流式）与 `aggregate()`（非流式）给出的
   正文、调用、`finish_reason`、错误码必须一致——2026-11-04 的线上问题
   正是同一段文本在两种传输方式下结论不同。
3. 一旦还原出调用，正文里就不能再残留调用脚手架。
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import unittest
from typing import Any, AsyncIterator

from server.platforms.base import PlatformError
from server.platforms.trae import chat as chatmod
from server.platforms.trae import client
from server.platforms.trae import text_tools


class _FakeResponse:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def aiter_lines(self) -> AsyncIterator[str]:
        for line in self._lines:
            yield line


def _events(lines: list[str]) -> AsyncIterator[tuple[str, Any]]:
    async def walk() -> AsyncIterator[tuple[str, Any]]:
        async for event, data in client.iter_events(_FakeResponse(lines)):
            yield event, data
    return walk()


def _sse(event: str, payload: object) -> list[str]:
    return [f'event: {event}',
            'data: ' + json.dumps(payload, ensure_ascii=False), '']


def _lines(frames: list[str]) -> list[str]:
    lines: list[str] = []
    for frame in frames:
        lines += _sse('output', {'response': frame})
    lines += _sse('done', {'finish_reason': 'stop'})
    return lines


def _run(coro):  # noqa: ANN001
    return asyncio.run(coro)


def _stream(frames: list[str], tool_names: list[str]) -> dict:
    lines = _lines(frames)
    state = chatmod.StreamState()

    async def collect() -> list[dict]:
        return [chunk async for chunk in chatmod.translate(
            _events(lines), model='trae/x', state=state,
            tool_names=tool_names)]

    chunks = _run(collect())
    content: list[str] = []
    calls: list[tuple[str, str]] = []
    errors: list[str] = []
    finish = 'stop'
    for chunk in chunks:
        if 'error' in chunk:
            errors.append(str(chunk['error'].get('code')))
            finish = 'error'
            continue
        if 'choices' not in chunk:
            continue
        choice = chunk['choices'][0]
        delta = choice.get('delta') or {}
        if delta.get('content'):
            content.append(delta['content'])
        for call in delta.get('tool_calls') or []:
            fn = call.get('function') or {}
            calls.append((str(fn.get('name')), str(fn.get('arguments'))))
        if choice.get('finish_reason') in ('tool_calls', 'error'):
            finish = choice['finish_reason']
    return {'content': ''.join(content), 'calls': calls,
            'finish': finish, 'errors': errors}


def _aggregate(frames: list[str], tool_names: list[str]) -> dict:
    lines = _lines(frames)
    state = chatmod.StreamState()

    async def collect() -> dict:
        return await chatmod.aggregate(
            _events(lines), model='trae/x', state=state,
            tool_names=tool_names)

    try:
        payload = _run(collect())
    except PlatformError as exc:
        return {'content': '', 'calls': [], 'finish': 'error',
                'errors': [str(exc.code)]}
    choice = payload['choices'][0]
    message = choice['message']
    calls = [(str((call.get('function') or {}).get('name')),
              str((call.get('function') or {}).get('arguments')))
             for call in message.get('tool_calls') or []]
    errors = [str(item.get('code')) for item in payload.get('error') or []]
    return {'content': message.get('content') or '', 'calls': calls,
            'finish': choice.get('finish_reason') or 'stop',
            'errors': errors}


def _split(text: str, sizes: list[int]) -> list[str]:
    frames: list[str] = []
    position = 0
    for size in sizes:
        if position >= len(text):
            break
        frames.append(text[position:position + size])
        position += size
    if position < len(text):
        frames.append(text[position:])
    return frames or ['']


def _chunkings(text: str, seed: int) -> list[list[str]]:
    """一组有代表性的切分：整段、逐字符、随机 1–7 字符。"""
    out = [[text], list(text)]
    rng = random.Random(seed)
    for _ in range(8):
        sizes = [rng.randint(1, 7) for _ in range(len(text) + 1)]
        out.append(_split(text, sizes))
    return out


def _block(name: str, params: dict[str, str]) -> str:
    lines = ['<call>']
    for key, value in params.items():
        lines.append(f'<parameter name="{key}">{value}</parameter>')
    lines.append('</call>')
    return '\n'.join(lines)


def _dsml(name: str, params: dict[str, str]) -> str:
    sep = '\uff5c\uff5c'
    token = lambda kind: f'<{sep}DSML{sep}{kind}>'          # noqa: E731
    close = lambda kind: f'</{sep}DSML{sep}{kind}>'         # noqa: E731
    lines = [token('tool_calls'), token(f'invoke name="{name}"')]
    for key, value in params.items():
        lines.append(token(f'parameter name="{key}"') + value
                     + close('parameter'))
    lines.append(close('invoke'))
    lines.append(close('tool_calls'))
    return '\n'.join(lines)


CALL = 'tool_call write({"path": "a.py", "content": "print(1)"})'


CORPUS: list[tuple[str, str, list[str]]] = [
    ('plain', '先看看目录，然后再说。', ['bash']),
    ('own-line-call', '先看目录：\n' + CALL + '\n看完了。', ['write']),
    ('call-at-eof', '先看目录：\n' + CALL, ['write']),
    ('inline-example', '我建议写成 ' + CALL + ' 这样，不过仅供参考。',
     ['write']),
    ('fenced-example', '格式是这样：\n```\n' + CALL + '\n```\n',
     ['write']),
    ('inline-code', '写成 `' + CALL + '` 这样是不行的。', ['write']),
    ('stacked-markers', '<tool_call>\n<tool_call>\n' + CALL, ['write']),
    ('marker-wrapped',
     '<tool_call>read({"path": "a"})</tool_call>\n[call] done', ['read']),
    ('json-payload',
     '[tool_call] {"name": "read", "arguments": {"path": "a"}}', ['read']),
    ('container', '先读一下：\n' + _block('read', {'path': 'a'}) + '\n继续。',
     ['read']),
    ('dsml', '先看目录：\n' + _dsml('bash', {'command': 'ls -la'}), ['bash']),
    ('placeholder', '[tool_call] bash({"command": "..."})', ['bash']),
    ('history-fold',
     '[历史调用] write({"path": "a.py"})\n← Edit success\n继续。', ['write']),
    ('two-calls', CALL + '\ntool_call read({"path": "b"})', ['write', 'read']),
    ('truncated', '先看目录。\n[tool_call] bash({"command": "ls"', ['bash']),
    ('nameless-container', '<call>\n<parameter name="path">a</parameter>\n'
     '</call>', ['read']),
    ('unclosed-tag', '<execute code="ls -la">', ['bash']),
    ('blank-lines', '第一段。\n\n' + CALL + '\n\n第二段。', ['write']),
    ('crlf', '第一段。\r\n' + CALL + '\r\n第二段。', ['write']),
]


SCAFFOLD = re.compile(
    r'[\[<]\s*tool_?call\b|<call\b|</call>|<invoke\b|<parameter\b|DSML',
    re.I)


def _same(result_a: dict, result_b: dict) -> bool:
    """比较两次流式结果；正文只允许**尾部空白**不同。

    逐字符喂时，收尾的纯空白会先被扣在 `Sniffer._pending` 里、流结束时丢掉；
    整段一次喂则留在 `flush()` 的剩余正文里。这只影响正文末尾的空白，不影响
    正文内容、调用、收尾方式或错误码。
    """
    return (result_a['content'].rstrip() == result_b['content'].rstrip()
            and result_a['calls'] == result_b['calls']
            and result_a['finish'] == result_b['finish']
            and result_a['errors'] == result_b['errors'])


class ChunkingInvariantTest(unittest.TestCase):
    """逐字符喂 = 任意切分喂。"""

    def test_every_chunking_matches_char_by_char(self) -> None:
        for index, (name, text, tools) in enumerate(CORPUS):
            baseline = _stream(list(text), tools)
            with self.subTest(sample=name):
                for frames in _chunkings(text, seed=index):
                    self.assertTrue(
                        _same(_stream(frames, tools), baseline),
                        _stream(frames, tools))

    def test_every_chunking_matches_whole_text(self) -> None:
        for name, text, tools in CORPUS:
            with self.subTest(sample=name):
                self.assertTrue(
                    _same(_stream([text], tools), _stream(list(text), tools)),
                    _stream([text], tools))


class StreamAggregateParityTest(unittest.TestCase):
    """流式与非流式必须给出同一个结论。"""

    def test_parity(self) -> None:
        for name, text, tools in CORPUS:
            with self.subTest(sample=name):
                stream = _stream([text], tools)
                aggregate = _aggregate([text], tools)
                self.assertEqual(aggregate['calls'], stream['calls'])
                self.assertEqual(aggregate['finish'], stream['finish'])
                self.assertEqual(aggregate['errors'], stream['errors'])
                if aggregate['errors']:
                    # 退化/截断收尾时非流式整轮作废（502），而流式已经把前面的
                    # 正文发出去了，收不回来。错误码与收尾方式必须一致。
                    continue
                # 非流式会把整段剩余正文的尾部空白吃掉，正文按 strip 后比。
                self.assertEqual(aggregate['content'].strip(),
                                 stream['content'].strip())

    def test_parity_under_arbitrary_chunking(self) -> None:
        for index, (name, text, tools) in enumerate(CORPUS):
            aggregate = _aggregate([text], tools)
            frames = _split(text, [2] * len(text))
            with self.subTest(sample=name):
                stream = _stream(frames, tools)
                self.assertEqual(aggregate['calls'], stream['calls'])
                self.assertEqual(aggregate['finish'], stream['finish'])
                self.assertEqual(aggregate['errors'], stream['errors'])


class NoScaffoldLeakTest(unittest.TestCase):
    """还原出调用后，正文里不能再出现调用脚手架。"""

    def test_rescued_turns_do_not_leak_scaffolding(self) -> None:
        for name, text, tools in CORPUS:
            result = _stream(list(text), tools)
            if not result['calls']:
                continue
            with self.subTest(sample=name):
                self.assertIsNone(SCAFFOLD.search(result['content']),
                                  result['content'])

    def test_history_label_is_never_parsed(self) -> None:
        folded = '[历史调用] write({"path": "a.py", "content": "x"})\n' \
                 '[历史结果] ok'
        calls, rest = text_tools.parse_calls(folded, ['write'])
        self.assertEqual(calls, [])
        self.assertEqual(rest, folded)
        self.assertFalse(text_tools.is_degenerate_call(folded, ['write']))


class OwnLineRuleTest(unittest.TestCase):
    """判据本身：块的两侧同一行只能有空白。"""

    def _state(self, text: str, *, start: int | None = None,
               end: int | None = None, pending: bool = False,
               line_start: bool | None = None) -> str:
        if start is None:
            start = text.find('write(')
            if start < 0:
                start = 0
        if end is None:
            found = text.find('})', start)
            end = found + 2 if found >= 0 else len(text)
        return text_tools._own_line(
            text, start, end, pending=pending, line_start=line_start)

    def test_standalone_call_is_yes(self) -> None:
        self.assertEqual(self._state('write({"path": "a"})'), 'yes')
        self.assertEqual(self._state('   write({"path": "a"})   '), 'yes')

    def test_multiline_call_is_yes(self) -> None:
        text = 'write({\n  "path": "a",\n  "content": "x"\n})'
        self.assertEqual(self._state(text), 'yes')

    def test_prose_before_on_same_line_is_no(self) -> None:
        self.assertEqual(self._state('先写成 write({"path": "a"})'), 'no')

    def test_prose_after_on_same_line_is_no(self) -> None:
        self.assertEqual(self._state('write({"path": "a"}) 这样'), 'no')

    def test_prose_after_next_line_is_still_yes(self) -> None:
        self.assertEqual(self._state('write({"path": "a"})\n后面再说'), 'yes')

    def test_pending_without_newline_needs_more(self) -> None:
        self.assertEqual(
            self._state('write({"path": "a"})', pending=True), 'need')
        self.assertEqual(
            self._state('write({"path": "a"})\n', pending=True), 'yes')
        self.assertEqual(
            self._state('write({"path": "a"}) 这样', pending=True), 'no')

    def test_trailing_whitespace_after_block_is_ignored(self) -> None:
        self.assertEqual(self._state('write({"path": "a"})   \n'), 'yes')

    def test_buffer_start_uses_reported_line_start(self) -> None:
        self.assertEqual(
            self._state('write({"path": "a"})', line_start=True), 'yes')
        self.assertEqual(
            self._state('write({"path": "a"})', line_start=False), 'no')


if __name__ == '__main__':
    unittest.main()
