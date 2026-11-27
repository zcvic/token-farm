"""网关流式转发的透传性回归测试（含 tool_calls 帧形态）。

背景：上游 2026-09-14 修了流式 `tool_calls` 的 name 语义（issue #82）——把
「逐帧回填 name」改成「每个 index 只发一次 name」（对齐 OpenAI 官方流）。
辅助函数 `backfillToolCallNames` 被重写为 `stripToolCallNames`。

对管理端的判断：**不需要适配**。我们的网关是 `yield chunk` 原样透传上游字节，
只在旁边用 `_scan_sse` 读一份做用量与首字统计（不改写、不回写、不缓冲），
且全仓库完全不碰 tool_calls。所以上游这个修复会通过转发路径自动生效。

本文件把这个「结论」变成断言：若将来有人在转发热路径里开始改写 SSE
（例如为了统一 tool_calls 形态），这些用例会失败，逼迫他先想清楚
「上游已经修过的语义，我们是否要再改一遍」——那正是重复且易错的。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.routers import gateway  # noqa: E402


def _data(obj) -> str:
    return 'data: ' + json.dumps(obj, ensure_ascii=False) + '\n'


# 上游新形态：首帧带 name，后续帧**没有 name 键**只有 arguments 片段
TOOL_FIRST = _data({'choices': [{'delta': {'tool_calls': [
    {'index': 0, 'id': 'call_1', 'type': 'function',
     'function': {'name': 'Bash', 'arguments': ''}}]}}]})
TOOL_ARG_FRAME = _data({'choices': [{'delta': {'tool_calls': [
    {'index': 0, 'function': {'arguments': '{"cmd"'}}]}}]})
TOOL_ARG_FRAME2 = _data({'choices': [{'delta': {'tool_calls': [
    {'index': 0, 'function': {'arguments': ': "ls"}'}}]}}]})
TOOL_PARALLEL = _data({'choices': [{'delta': {'tool_calls': [
    {'index': 1, 'id': 'call_2', 'type': 'function',
     'function': {'name': 'Read', 'arguments': ''}}]}}]})


class ScanSseToolCallsTest(unittest.TestCase):
    """`_scan_sse` 不得依赖 tool_calls 的形态 —— 它只做统计，不参与转发。"""

    def test_tool_frames_do_not_break_scan(self) -> None:
        pending = ''
        for frame in (TOOL_FIRST, TOOL_ARG_FRAME, TOOL_ARG_FRAME2, TOOL_PARALLEL):
            pending, _ = gateway._scan_sse(pending + frame, {})
        self.assertEqual(pending, '', '残留缓冲应为空（每帧都以换行结束）')

    def test_tool_frames_are_not_counted_as_content(self) -> None:
        """工具调用帧不含正文，不能当作「首字」——否则首字延迟会偏小。

        这是真实语义：模型先决定调用工具，正文要等工具结果回来之后才产生。
        把 tool_calls 当首字会让「上游多久开始回话」这个指标失真。
        """
        for frame in (TOOL_FIRST, TOOL_ARG_FRAME, TOOL_PARALLEL):
            _, saw = gateway._scan_sse(frame, {})
            self.assertFalse(saw, f'工具帧不应计为正文：{frame[:60]}')

    def test_tool_frame_with_empty_name_is_ignored(self) -> None:
        """上游后续帧若带空串 name（旧形态），也不能影响统计。"""
        frame = _data({'choices': [{'delta': {'tool_calls': [
            {'index': 0, 'function': {'name': '', 'arguments': 'x'}}]}}]})
        pending, saw = gateway._scan_sse(frame, {})
        self.assertEqual(pending, '')
        self.assertFalse(saw)

    def test_usage_extracted_from_tool_frames(self) -> None:
        """工具调用流的末帧同样要能取到 usage（否则用量统计漏记）。"""
        usage: dict = {}
        frame = _data({'choices': [], 'usage': {'prompt_tokens': 7, 'completion_tokens': 3}})
        _, _ = gateway._scan_sse(frame, usage)
        self.assertEqual(usage['prompt_tokens'], 7)
        self.assertEqual(usage['completion_tokens'], 3)

    def test_scan_does_not_mutate_payload(self) -> None:
        """扫描必须是只读的：它拿到的对象不能被改（改了会污染转发内容）。

        这里直接传 dict 进去，断言扫描后原对象不变。
        """
        obj = {'choices': [{'delta': {'tool_calls': [
            {'index': 0, 'function': {'name': 'Bash', 'arguments': 'x'}}]}}]}
        snapshot = json.dumps(obj, sort_keys=True)
        gateway._scan_sse(_data(obj), {})
        self.assertEqual(json.dumps(obj, sort_keys=True), snapshot,
                         '_scan_sse 修改了传入对象 —— 它必须是只读的')


class GatewayPassthroughTest(unittest.TestCase):
    """确认网关没有「改写 SSE」的代码路径 —— 这是上面结论的前提。"""

    def test_gateway_source_does_not_rewrite_sse(self) -> None:
        src = (Path(__file__).resolve().parents[2] / 'server' / 'routers' / 'gateway.py'
               ).read_text(encoding='utf-8')
        # 转发热路径只 yield 上游 chunk；不应出现「重新构造/替换帧」的迹象
        for bad in ('yield _data(', 'yield json.dumps(', 'yield _rewrap('):
            self.assertNotIn(bad, src, f'网关似乎在重写 SSE：{bad}')
        # 所有 yield 出来的都必须是上游原样的 chunk（不能有第二个变量名混进来）
        # 注意：这里按**出现次数**断言，只查包含太弱——文件里有两处 yield，
        # 改掉其中一处仍会通过（这个弱点是被反证试出来的）。
        yields = [ln.strip() for ln in src.splitlines() if ln.strip().startswith('yield ')]
        self.assertEqual(yields, ['yield chunk'] * len(yields),
                         f'转发热路径出现了非透传的 yield：{yields}')

    def test_no_tool_calls_handling_anywhere(self) -> None:
        """转发热路径不碰 tool_calls —— 上游的修复通过透传自动生效。

        若将来需要在此处加逻辑（例如为了兼容某个客户端），请先确认
        **上游是否已经修过同一个问题**：重复修一遍往往引入新的不一致。

        两个例外都是**协议转换层**（不是转发热路径）：
          · **Anthropic 兼容层**（`routers/anthropic.py`）：它**必须**处理
            tool_calls，因为那正是协议转换本身——OpenAI 把工具参数作为
            **分片字符串**下发（`{"loc` + `ation":…}`），而 Anthropic 的客户端要的是
            `input_json_delta` 事件、且参数以对象形式呈现。
          · **Responses 兼容层**（`routers/responses.py`）：同理——Chat
            Completions 的 `tool_calls[].index` 要重建成 Responses 的
            `function_call` 输出项与 `function_call_arguments.delta` 事件。
          · **多平台适配层**（`platforms/*/chat.py`）：Trae 的私有协议把工具调用
            放在自己的 SSE 事件里（`output.tool_calls`），它不是 OpenAI 形状，
            必须在这里翻译成 `delta.tool_calls` 才能让客户端用工具。这一层
            只在 `trae/` 前缀命中时生效，WorkBuddy 的转发热路径一行都没改。

        这与「替上游兜底」是两回事：它们不改动转发给上游的内容，只改变回给
        客户端的表达形式；「转发热路径必须原样透传」这条约束由上面几个用例
        独立守着（`gateway.py` 里不得出现非透传的 yield）。

        新增此类转换层时**显式**登记在此，而不是放宽匹配——豁免名单越长越该
        有人复核，静默放行会让这条约束慢慢失效。
        """
        # 例外：协议转换层（不是转发热路径）
        #   · services/tencent.py：账号探测（issue #270）不是转发路径 —— 它扫一遍
        #     SSE 判断「模型是否真的产出内容」，读 tool_calls 是为了识别「只有角色
        #     预告/心跳」的假成功，不改写、不转发给客户端。
        #   · platforms/trae/chat.py：Trae 私有协议 → OpenAI 的翻译层。上游把
        #     tool_calls 放在自己的 SSE 事件里，不翻译客户端就拿不到工具调用；
        #     与 Routers 的两个兼容层同性质，且只作用于 `trae/` 前缀的模型。
        #   · platforms/trae/client.py：上一条的**帧分类器**——Agent 通道的 SSE
        #     帧不带 event 名，只能按字段判断哪一帧是正文 / 用量 / 结束，因此
        #     必须认识 `tool_calls` 这个字段名。它同样只产事件、不改写内容，
        #     真正翻译成 OpenAI diff 的仍是 chat.py。
        #   · platforms/trae/text_tools.py：同一前缀下的**文本还原层**——模型把调用
        #     写进正文（`[tool_call] name(args)`，照着历史记录 / 协议提示念）时把它
        #     解析回结构化调用。只认已声明 tools 的请求，解析不出的原样透传。
        #   · platforms/minimax/translate.py：MiniMax Code 的上游是 Anthropic
        #     Messages 形状，`tool_use` 必须翻译成 OpenAI 的 `tool_calls`，
        #     流式 `input_json_delta` 也必须重建成 `delta.tool_calls`。只作用于
        #     `minimaxcn/` / `minimaxcom/` 前缀。
        #   · platforms/minimax/adapter.py：上一条的**流式收尾器**——它只读
        #     OpenAI chunk 里的 `delta.tool_calls` 判断「这一帧算不算首字」，
        #     不改变内容；真正的协议翻译仍在 translate.py。
        #   · platforms/cline/client.py：Cline 的免费档只认流式，客户端要非流式
        #     时适配器强制走 stream 再由这里聚合回 `chat.completion`。聚合必须
        #     把 `delta.tool_calls` 收进 `message.tool_calls`，否则「强制流式」
        #     这条兜底路径会静默丢工具调用。它只作用于 `cline/` 前缀。
        #   · platforms/lingxi/chat.py：灵犀把工具调用放在 `choices[].tool_calls`
        #     （与正文 `choices[].text` 同层，不是 OpenAI 的 `delta`），必须翻译
        #     成 `delta.tool_calls` 并按 `index` 聚合参数。只作用于 `lingxi/` 前缀。
        #   · platforms/lingxi/adapter.py：上一条的**收尾判定器**——非流式出口
        #     用 `StreamTranslator.has_tool_calls` 区分「只有工具调用、没有正文」
        #     与真正的空响应，不改写内容；翻译仍全在 chat.py。
        #   · platforms/raccoon/client.py：小浣熊要「强制流式 + 本地聚合」——
        #     客户端要非流式时聚合必须把 `delta.tool_calls` 收进
        #     `message.tool_calls`，否则工具调用会静默丢失。只作用于 `raccoon/`。
        #   · platforms/codearts/client.py：CodeArts 上游**永远**走流式，非流式
        #     由适配器本地聚合，同样必须把分片的 `delta.tool_calls` 按 `index`
        #     聚合成完整调用。只作用于 `codearts/`。
        allow = {'routers/anthropic.py', 'routers/responses.py', 'services/tencent.py',
                 'platforms/trae/chat.py', 'platforms/trae/client.py',
                 'platforms/trae/text_tools.py',
                 'platforms/minimax/translate.py', 'platforms/minimax/adapter.py',
                 'platforms/cline/client.py',
                 'platforms/lingxi/chat.py', 'platforms/lingxi/adapter.py',
                 'platforms/raccoon/client.py', 'platforms/codearts/client.py'}
        root = Path(__file__).resolve().parents[2] / 'server'
        # Qoder 的内置网关是**第三方源码副本**（vendor 自 qoder2api-hub），它的
        # `tool_calls` 处理属于 Qoder 私有协议 ↔ OpenAI 的翻译层，与 trae/chat.py
        # 同性质；它不在这条「本仓库转发热路径」约束的范围内。
        vendor = root / 'platforms' / 'qoder' / '_vendor'
        hits = []
        for f in root.rglob('*.py'):
            if 'tests' in str(f):
                continue
            if vendor in f.parents:
                continue
            rel = str(f.relative_to(root)).replace('\\', '/')
            if rel in allow:
                continue
            if 'tool_calls' in f.read_text(encoding='utf-8'):
                hits.append(str(f.relative_to(root)))
        self.assertEqual(hits, [], f'以下文件开始处理 tool_calls：{hits}')


if __name__ == '__main__':
    unittest.main()
