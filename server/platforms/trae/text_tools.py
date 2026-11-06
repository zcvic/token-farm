"""把正文里的「工具调用」文本还原成结构化调用。

Trae 通道只认 `role` + `content`，历史里的工具痕迹只能折成正文（见
`client._fold_tool_history`，格式是 `[历史调用] name(args)` / `[历史结果] …`）。
上下文里这类文本多了，弱模型（flash 档尤其明显）会照着文本格式「念」出下一次
调用，而不是走 tools 通道——客户端收到的是一条普通回复加 `finish_reason:
"stop"`，Agent 循环当场停住，工具一个都没执行（2026-11-01 实测：
deepseek-v4.1-flash 的 `edit`）。

历史折叠**刻意不使用**下面这些调用语法：`[历史调用]` 前缀不会被解析，模型复述
历史时不会把上一轮的记录当成新请求执行（2026-11-04 线上样本）。

这个模块认得出四种调用写法：

    [tool_call] edit({"path": "...", "oldString": "...", "newString": "..."})
    <tool_call>edit({...})</tool_call>
    tool_call edit({...})                       ← 省略方括号
    [tool_call] {"name": "edit", "arguments": {...}}   ← JSON 载荷

以及**标签形态**（模型自造语法的常见退路）：

    <execute code="...">...</execute>
    <shell><command>ls -la</command></shell>
    <bash command="ls" />

以及 **DSML 形态**（DeepSeek 系模型的原生协议）与它的「扁平」变体：

    <｜｜DSML｜｜invoke name="bash">
    <｜｜DSML｜｜parameter name="command">ls -la</｜｜DSML｜｜parameter>
    <tool_call><function=read><parameter=path>a.py</parameter>

标签形态只在**标签名能对上客户端声明的工具**时才还原：先按同名匹配，再按
`_ALIAS_GROUPS` 里的同义组匹配——**只有该组恰好命中一个已声明工具时**才映射。
一个都对不上就原样透传，绝不猜。

判据始终是「宁可漏，不可错」：参数必须是合法 JSON 对象，括号/标签没闭合一律
按正文放行。被误判的代价是执行一条模型没要求的调用。

唯一的例外是**没有工具名的调用块**（`invoke` 缺 `name`、`<tool_call>` 后面直接
跟 `<parameter>`）：它无法执行，但也**绝不能当正文下发**——整段丢弃，整轮只剩
它时按退化报错（2026-11-03 线上样本：整串协议标签出现在回复里）。

反过来还有一条**退化判定**：如果整轮只剩下空标签（`<tool_call>\n</call>`）、
孤立闭合标签，或一条参数被截断/不是合法 JSON 的调用，那么这一轮既没有可执行
的调用、也没有可读正文。调用方据此报错（而不是下发空正文 + `stop`），否则
客户端的 Agent 循环会静默停住。
"""
from __future__ import annotations

import json
import re
from typing import Any

# 开头一律按**宽容正则**匹配：模型经常把两种写法混起来（线上样本
# `<tool_call] read({...})`、`[tool_call> ...`），固定字符串会把 `<` / `]`
# 漏进正文，整条调用既不执行也不报错。方括号与尖括号的四种组合都认。
_OPEN = re.compile(r'[\[<]\s*tool_?call\s*[\]>]', re.I)
# 裸 `tool_call` 仍然认（模型省略方括号时的常见写法），但必须紧跟「名字(」或「{」，
# 否则正文里提到 `tool_call` 这个词也会被当成调用。
_BARE = re.compile(r'tool_call(?=\s*(?:[A-Za-z_][A-Za-z0-9_.\-]*\s*\(|\{))', re.I)
# 尾部滞留用：裸标记可能已经完整到达，但「名字(」还没到（帧恰好切在
# `tool_call` / `tool_call ` / `tool_call write`）。这时 `_BARE` 的前瞻不成立，
# `_next_start` 找不到标记，旧逻辑就把它当正文放出去了——后面的参数再也接不回来。
# 这类尾巴要扣住，等下一帧出现 `(` 再决定。结尾的空白也要算进去：帧很容易切在
# `tool_call `（标记 + 下一个 token 的前导空格）上，漏掉空白就等于没扣
# （2026-11-04 实测：切在这个位置时整条调用漏成正文，非流式却正常）。
_BARE_TAIL = re.compile(
    r'tool_?call(?:\s*[A-Za-z_][A-Za-z0-9_.\-]*)?\s*$', re.I)
# 尾部滞留用：完整开头的所有前缀都要能扣住（跨帧切开），闭合标签同理。
_MARKERS = ('[tool_call]', '<tool_call>', '[toolcall]', '<toolcall>',
            '</tool_call>', '</toolcall>', '</call>', 'tool_call')

_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_.\-]*')
# 闭合标签的残缺写法：`</tool_call>` / `</toolcall>` / `</call>`。最后一种是
# 弱模型自造（线上样本 `<tool_call>\n</call>`）：认不出来就会把整段标签当正文
# 下发，客户端拿到空回复 + `stop`，Agent 循环静默停住。
_CLOSE = re.compile(r'\s*</\s*(?:tool_?call|call)\s*[\]>]', re.I)
_CLOSE_TAGS = ('</tool_call>', '</toolcall>', '</call>')
# 没被还原成调用的协议噪音：空对 `<tool_call></tool_call>` 与孤立闭合标签。
# 只删标签本身，标签里的正文保留——`<tool_call>` 后面跟着内容却没解析成调用时
# 仍按「宁可漏，不可错」原样透传。
_EMPTY_PAIR = re.compile(
    r'[\[<]\s*tool_?call\s*[\]>]\s*</\s*(?:tool_?call|call)\s*[\]>]', re.I)
_ORPHAN_CLOSE = re.compile(r'</\s*(?:tool_?call|call)\s*[\]>]', re.I)
# 「整轮只有调用脚手架」判定用：**只认 tool_call 家族**。
#
# 上一版写成 `</\s*[A-Za-z_]*…`，把任意 HTML 闭合标签（`</div>`、`</p>`）也算成
# 脚手架——模型正常回答一段 HTML 时会被判成「退化调用」并补一帧错误（2026-11-02
# 实测：只输出 `</div>` 得到 `content: "</div>"` + `degenerate_tool_call`）。这里
# 收窄到 `tool_call` / `toolcall` / `call` 三种名字，允许缺收尾括号；被截断成
# `</ca` 的情况由 `_partial_close` 单独处理。
_NOISE = re.compile(
    r'[\[<]\s*tool_?call\s*[\]>]?'
    r'|</\s*(?:tool_?call|call)\s*[\]>]?', re.I)

# ── DeepSeek DSML 工具调用 ───────────────────────────────────────
#
# 上游（DeepSeek 系模型）会把工具调用漏成 DSML 协议令牌：
#
#   <｜｜DSML｜｜tool_calls>
#   <｜｜DSML｜｜invoke name="bash">
#   <｜｜DSML｜｜parameter name="command">ls -la</｜｜DSML｜｜parameter>
#   </｜｜DSML｜｜invoke>
#   </｜｜DSML｜｜tool_calls>
#
# 竖线可能是全角 `｜`、半角 `|`、单根或双根；包裹用的 `calls` 有时整个没有。
# 2026-11-02 实测：Trae 侧完全不认这些令牌——既不会还原成 `tool_calls`，也不会
# 被判成退化，原样透传给客户端；「整轮只有 DSML」于是退化成空正文 + `stop`，
# 正是本轮要修的原始症状。Qoder 侧早就有 `parse_dsml_tool_calls()`，这里补上。
#
# 令牌中间的分隔符最多 4 个字符，`DSML` 两侧都允许；`kind` 用 `\b` 收尾，
# 因为 `invoke`/`parameter` 后面通常还跟着属性。
_DSML_TAG = re.compile(
    r'<(?P<close>/)?[^<>\n]{0,4}DSML[^<>\n]{0,4}'
    r'(?P<kind>tool_?calls?|calls|invoke|parameter)\b', re.I)
# 只要**开**令牌：闭合令牌留给 `_strip_tags` 清理，不参与「调用开头」的判定。
_DSML_OPEN = re.compile(
    r'<(?![/])[^<>\n]{0,4}DSML[^<>\n]{0,4}'
    r'(?P<kind>tool_?calls?|calls|invoke|parameter)\b', re.I)
# 属性值三种写法都认：`name="grep"` / `name='grep'` / `name=grep`。
# 上一版只认带引号的，模型写无引号时 `_DSML_INVOKE_OPEN` 匹配不上，整块
# 被当成「无名 invoke」漏进正文（2026-11-03 线上复现）。
_DSML_ATTR = (r'(?P<q>["\']?)'
              r'(?P<{name}>[^"\'\s>]+)'
              r'(?P=q)')
_DSML_INVOKE_OPEN = re.compile(
    r'<[^<>\n]{0,4}DSML[^<>\n]{0,4}invoke\s+name\s*=\s*'
    + _DSML_ATTR.format(name='name') + r'[^>]*>',
    re.I)
_DSML_INVOKE_CLOSE = re.compile(
    r'</[^<>\n]{0,4}DSML[^<>\n]{0,4}invoke\s*>', re.I)
_DSML_CALLS_CLOSE = re.compile(
    r'</[^<>\n]{0,4}DSML[^<>\n]{0,4}(?:tool_?calls?|calls)\s*>', re.I)
_DSML_PARAM = re.compile(
    r'<[^<>\n]{0,4}DSML[^<>\n]{0,4}parameter\s+name\s*=\s*'
    + _DSML_ATTR.format(name='key') + r'[^>]*>'
    r'(?P<value>.*?)'
    r'</[^<>\n]{0,4}DSML[^<>\n]{0,4}parameter\s*>', re.S | re.I)
_DSML_PARAM_OPEN = re.compile(
    r'<[^<>\n]{0,4}DSML[^<>\n]{0,4}parameter\b', re.I)
_DSML_CLOSE_TAG = re.compile(
    r'</[^<>\n]{0,4}DSML[^<>\n]{0,4}'
    r'(?:tool_?calls?|calls|invoke|parameter)\s*>', re.I)
# 孤立的 DSML **开**令牌同样不能当正文下发：解析失败时 `_strip_tags` 以前只删
# 闭合标签，开标签就原样漏了出去（线上样本里那一串 `<｜｜DSML｜｜tool_calls>`）。
_DSML_OPEN_TAG = re.compile(
    r'<(?![/])[^<>\n]{0,4}DSML[^<>\n]{0,4}'
    r'(?:tool_?calls?|calls|invoke|parameter)\b[^>]*>', re.I)
# 流式：末尾是不是 DSML 令牌的前半截（`<｜`、`<｜｜DSM`、`</｜`…）。
# 这种前缀不可能出现在正常正文里，扣住等下一帧最划算。
_DSML_TAIL_START = re.compile(r'</?[\uFF5C|]')

# ── 扁平参数标签（`<function=read>` + `<parameter name="…">`）────────
#
# 2026-11-03 线上样本：模型把 DSML 的 invoke 头丢掉，只剩
#
#   <tool_call>
#   <parameter name="path">C:\…\build_doc.py</parameter>
#   <parameter name="offset">138</parameter>
#   <parameter name="limit">25</parameter>
#   </tool_call>
#
# 或者把函数名写成独立令牌：
#
#   <tool_call>
#   <function=read>
#   <parameter=path>a.py</parameter>
#
# 前者没有工具名、无法执行：整块丢弃（`drop`），否则标签原样漏进回复；
# 后者能还原成真调用，照常下发。
_PARAM_OPEN = re.compile(
    r'<\s*parameter\s*(?:=\s*|name\s*=\s*)'
    r'(?P<q>["\']?)(?P<key>[^"\'\s>]+)(?P=q)[^>]*>', re.I)
_PARAM_PAIR = re.compile(
    r'<\s*parameter\s*(?:=\s*|name\s*=\s*)'
    r'(?P<q>["\']?)(?P<key>[^"\'\s>]+)(?P=q)[^>]*>'
    r'(?P<value>.*?)'
    r'</\s*parameter\s*>', re.S | re.I)
_FUNCTION_TOKEN = re.compile(
    r'<\s*function\s*(?:=\s*|name\s*=\s*)'
    r'(?P<q>["\']?)(?P<name>[^"\'\s>]+)(?P=q)\s*/?>', re.I)
# 帧切在 `<par` / `<fun` 时先扣住，等名字收全再判断（见 `_flat_prefix`）。
# 流式下「整块扣到收尾」的判定：`<tool_call>`（可带一个裸名字）后面紧跟
# 参数/函数标签。这类块不能按散文标签 2 KB 早退，否则长参数照样漏出去。
_FLAT_HEAD = re.compile(
    r'[\[<]\s*tool_?call\s*[\]>]\s*'
    r'(?:[A-Za-z_][A-Za-z0-9_.\-]*\s*)?'
    r'<\s*(?:parameter|function)\b', re.I)

# ── 无名调用容器（`<call>` / `<tool_calls>` / `<invoke>`）──────────
#
# 2026-11-03 线上样本（deepseek-v4.1-flash）：模型把工具名整个丢掉，只留一个
# 容器外壳和参数标签：
#
#   <call>
#   <parameter name="path">…</parameter>
#   <parameter name="oldString">…</parameter>
#   </call>
#
# `<tool_call>` 早就有扁平形态的解析（`_scan_flat`），但 `<call>` / `<calls>` /
# `<tool_calls>` / `<invoke>` 不在那套正则里：`_next_start` 找不到开头，整块
# 原样漏进回复；`_strip_tags` 又只删 `</call>`，于是线上看到的是「开标签在、
# 闭合标签没了」的残块。这里把这一族容器接到同一条解析线上：
#
# - 容器里有 `<parameter>` 子标签才算调用块（自由正文仍按正文处理）；
# - 工具名可以来自 `name="…"` 属性（`<invoke name="bash">`）、`<function=…>`
#   令牌，或裸名字（`<call>read<parameter …>`）；
# - 没有名字 → `drop`（无法执行，也绝不当正文下发），整轮只剩它时报退化。
_CONTAINER_NAMES = ('tool_calls', 'toolcalls', 'calls', 'call',
                    'invoke', 'function_call', 'function_calls')
# `tool_call` / `toolcall` 故意排除：它们由 `_OPEN` / `_scan_flat` 处理，
# 还多认方括号与混写括号的形态。
_CONTAINER = re.compile(
    r'<\s*(?P<tag>tool_?calls|calls?|invoke|function_calls?)\b'
    r'(?P<attrs>[^<>]*?)>', re.I)
_CONTAINER_OPEN = re.compile(
    r'<\s*(?:tool_?calls|calls?|invoke|function_calls?)\b[^<>]*>', re.I)
_CONTAINER_ANY_CLOSE = re.compile(
    r'</\s*(?:tool_?calls|calls?|invoke|function_calls?)\s*>', re.I)


def _container_prefix(head: str) -> bool:
    """`head` 是否可能是容器标签的前半截（开或闭，流式跨帧用）。

    `</inv` / `</calls` 这类闭合前缀同样要扣住：它是上一个被还原调用的收尾，
    漏成正文就会在回复末尾多出一截 `</invoke>`。
    """
    low = (head or '').lstrip().lower()
    if not low.startswith('<'):
        return False
    body = low[1:].lstrip()
    if body.startswith('/'):
        body = body[1:].lstrip()
    return any(name.startswith(body) or body.startswith(name)
               for name in _CONTAINER_NAMES)


# 参数值是这些字面量时，这条「调用」几乎一定是**示例/占位符**，不是真要执行。
# 线上样本：模型复述协议提示时照抄了 `{"command": "..."}`，上一版把它当真调用
# 下发，客户端执行一条 `...` 命令（2026-11-02 实测）。
_PLACEHOLDERS = frozenset({
    '...', '…', '....', '. . .', '<...>', '<…>', '{{...}}',
    'your_command', 'your-command', 'your command', '你的命令',
})


def _strip_tags(text: str) -> str:
    """删掉空对、孤立闭合标签与孤立的 DSML 开令牌，其余正文一字不动。"""
    if not text:
        return text
    text = _EMPTY_PAIR.sub('', text)
    text = _ORPHAN_CLOSE.sub('', text)
    text = _DSML_CLOSE_TAG.sub('', text)
    text = _DSML_OPEN_TAG.sub('', text)
    # 容器族的孤立闭合标签（`</calls>` / `</invoke>` / `</tool_calls>`）：内层
    # `invoke` 被还原成调用后，外层包裹的收尾就剩在这里，必须一并清掉。
    return _CONTAINER_ANY_CLOSE.sub('', text)


# ── 围栏（代码块 / 行内反引号）─────────────────────────────────
#
# 2026-11-02 实测：模型在正文里演示 ```` ```[tool_call] bash({...})``` ````，
# 上一版把代码块里的那行还原成**真调用**并执行了，正文只剩一对空围栏。
# 围栏内的内容一律不扫——那是在讲解格式，不是在请求执行。
#
# 行内反引号只屏蔽「内容里提到 tool_call」的那些：shell 参数里常有
# `` `date` ``（`{"command": "echo \`date\`"}`），无条件屏蔽会把合法 JSON
# 打成非法，反而丢掉真调用。
_FENCE = '```'
_INLINE_TICK = re.compile(r'`([^`\n]*)`')


def _mask_spans(text: str, inside: bool = False) -> tuple[str, bool]:
    """把围栏里的字符换成占位符，返回 (掩码文本, 收尾是否仍在围栏内)。

    下标**逐字符保持不变**，所以调用方可以继续拿原文取参数、取正文，只用掩码
    文本定位 marker —— 被屏蔽的区间里永远找不到 marker。
    """
    if not text:
        return text, inside
    chars = list(text)

    def blank(start: int, end: int) -> None:
        for index in range(max(0, start), min(end, len(chars))):
            if chars[index] != '\n':
                chars[index] = '\x00'

    position = 0
    if inside:
        closing = text.find(_FENCE)
        if closing < 0:
            blank(0, len(text))
            return ''.join(chars), True
        blank(0, closing + len(_FENCE))
        position = closing + len(_FENCE)
    while True:
        opening = text.find(_FENCE, position)
        if opening < 0:
            break
        closing = text.find(_FENCE, opening + len(_FENCE))
        if closing < 0:
            blank(opening, len(text))
            return ''.join(chars), True
        blank(opening, closing + len(_FENCE))
        position = closing + len(_FENCE)
    masked = ''.join(chars)
    for match in _INLINE_TICK.finditer(masked):
        if 'tool_call' in match.group(1).lower():
            blank(match.start(), match.end())
    return ''.join(chars), False


# 判据：调用块是否**独占一行**。只看块本身所在的两侧，不看到结尾。
#
# 旧判据（“调用之后到结尾只允许空白”）必须知道整段文本在哪里结束，流式在调用
# 刚写完时根本不知道后面还有没有正文，于是同一段文本在流式/非流式、不同分片下
# 会得出不同结论（2026-11-04 实测：模型复述 Agent 记录时，流式把调用当正文放走、
# 非流式却把它执行了）。现在只问两个局部问题：
#
#   · 块之前同一行有没有别的内容？（有 → 夹在句子里的举例）
#   · 块之后同一行有没有别的内容？（有 → 调用后面还接着说明）
#
# 两侧都没有，才当成真调用。判定在块闭合后最多再等一个换行，所以流式可以精确
# 复现非流式的结论；“后面隔一行再解释”不再影响判定。
def _own_line(text: str, start: int, end: int, *, pending: bool,
              line_start: bool | None = None) -> str:
    """调用块是否独占一行：`'yes'` / `'no'` / `'need'`（行还没结束）。

    `end` 可以是调用语法之后再带一截空白的位置（扁平调用/容器会把收尾空白
    一起吃掉），这里先退回到真正的语法结尾再判右侧。

    `pending` 为真表示文本还会增长（流式）：块后还没出现换行时返回 `'need'`，
    由调用方扣住等下一帧。`line_start` 用于流式下 `start == 0` 的情形——块在
    缓冲区开头时，要由调用方告知上一段已放出的文本是否停在行首。
    """
    while end > start and text[end - 1] in ' \t\r\n':
        end -= 1
    if start > 0:
        newline = text.rfind('\n', 0, start)
        lead = text[newline + 1:start]
    elif line_start is False:
        return 'no'
    else:
        lead = ''
    if lead.strip():
        return 'no'
    newline = text.find('\n', end)
    tail = text[end:] if newline < 0 else text[end:newline]
    tail = tail.strip()
    if pending and tail and _partial_close(tail):
        # 调用自己的闭合标签被帧切开（`</`、`</tool_ca`）：还可能是它的收尾，
        # 不能现在判成「同一行后面还有正文」，否则整条调用会漏成正文。
        return 'need'
    if tail:
        return 'no'
    if newline < 0 and pending:
        return 'need'
    return 'yes'


# 标签形态最多攒这么多字符。超了就认定它是散文里的「未闭合标签」而不是调用：
# 否则模型只要在正文里写一句「用 <read> 这种标签表示读取文件」，后面整段流都会被
# 扣在缓冲区里（实测：`Sniffer.feed` 一直返回空，直到流结束才一次性吐出）。
_TAG_HOLD_LIMIT = 2 * 1024


def _placeholder_args(arguments: str) -> bool:
    """参数里是否有明显的占位符字面量。

    只认「整个值就是省略号 / `你的命令`」这种极端形态：`{"path": ".../a.ts"}`
    这类部分占位仍按真调用走（模型可能真写了相对路径）。判据刻意保守。
    """
    try:
        value = json.loads(arguments or '{}')
    except ValueError:
        return False
    if not isinstance(value, dict):
        return False
    for item in value.values():
        if isinstance(item, str) and item.strip().lower() in _PLACEHOLDERS:
            return True
    return False


_TAG_NAME = re.compile(r'<([A-Za-z_][A-Za-z0-9_.\-]*)')
_TAG_ATTR = re.compile(
    r'([A-Za-z_][A-Za-z0-9_.\-]*)\s*=\s*'
    r'("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|[^\s"\'>]+)')
_NESTED_TAG = re.compile(
    r'<([A-Za-z_][A-Za-z0-9_.\-]*)>([\s\S]*)</\1>', re.IGNORECASE)

# 单次调用最多攒这么多字符；超了就当正文放行，防止把整段输出扣在缓冲区里。
_LIMIT = 64 * 1024

# 标签形态的同义组。映射规则刻意保守：**该组在客户端声明的工具里恰好命中
# 一个**才启用，命中 0 个或 2 个以上都不猜（例如 opencode 同时声明了 `glob`
# 和 `list`，那 `<find>` 就不映射）。
_ALIAS_GROUPS = (
    ('bash', ('bash', 'shell', 'sh', 'terminal', 'exec', 'execute', 'run',
              'cmd', 'command', 'powershell')),
    ('edit', ('edit', 'str_replace', 'strreplace', 'apply_patch', 'patch', 'replace')),
    ('read', ('read', 'cat', 'view', 'open', 'read_file', 'readfile')),
    ('write', ('write', 'create', 'write_file', 'writefile', 'save')),
    ('grep', ('grep', 'rg', 'ripgrep', 'search')),
    ('glob', ('glob', 'find', 'ls', 'list')),
)

# 这些标签的正文/属性天然是「一段代码」，落到 `code` 参数上；其余按
# `_ALIAS_GROUPS` 的组决定（shell 组 → `command`），再兜底到 `input`。
_CODE_TAGS = ('execute', 'code', 'eval', 'python', 'javascript', 'js', 'node')


class _Resolver:
    """把标签名解析成「客户端真的声明过」的工具名。

    `tool_names` 为空时整个标签形态关闭（退化成原来的三种文本形态）。
    """

    def __init__(self, tool_names: object = ()) -> None:
        self.exact: dict[str, str] = {}
        for raw in tool_names or ():
            name = str(raw or '').strip()
            if name:
                self.exact.setdefault(name.lower(), name)
        self.alias: dict[str, str] = {}
        self.groups: dict[str, str] = {}
        if self.exact:
            for group, members in _ALIAS_GROUPS:
                hits = [self.exact[m] for m in members if m in self.exact]
                if len(hits) != 1:
                    continue
                for member in members:
                    self.groups.setdefault(member, group)
                for member in members:
                    self.alias.setdefault(member, hits[0])

    def __bool__(self) -> bool:
        return bool(self.exact)

    def resolve(self, tag: str) -> str:
        low = str(tag or '').strip().lower()
        if not low:
            return ''
        return self.exact.get(low) or self.alias.get(low, '')

    def primary_arg(self, tag: str, name: str) -> str:
        """标签正文落到哪个参数名上。

        同名直配时优先按标签本身判断（`<execute>` 就是 `code`）；通过同义组
        映射换了工具时优先看目标工具（`<execute>` 落到 `bash` 要用 `command`）。
        """
        low = str(tag or '').strip().lower()
        group = self.groups.get(str(name or '').strip().lower(), '')
        if low in self.exact:
            if low in _CODE_TAGS:
                return 'code'
            return 'command' if group == 'bash' else 'input'
        if group == 'bash':
            return 'command'
        if low in _CODE_TAGS:
            return 'code'
        return 'input'

    def resolve_marker(self, name: str) -> str:
        """正文形态（`[tool_call] name(...)`）的工具名映射。

        上一版只给**标签形态**做白名单，`[tool_call] shell(...)` 会被原样下发
        一个客户端根本没声明的工具（2026-11-02 实测）。这里复用同一张表：
        同名直配原样，同义组唯一命中就映射，都对不上仍原样下发（客户端报
        unknown tool 后 Agent 循环还能继续），只是不算「还原成功」。

        `tool_names` 为空时不做任何映射（保持旧行为）。
        """
        low = str(name or '').strip().lower()
        if not low:
            return name
        return self.exact.get(low) or self.alias.get(low) or name

    def normalize_args(self, tag: str, name: str,
                       args: dict[str, Any]) -> dict[str, Any]:
        """别名映射换了工具时，把参数名一并改到目标工具认的键上。

        典型样本：opencode 只声明了 `bash`，模型写 `<execute code="ls">`。
        调用落到 `bash` 之后参数必须叫 `command`，否则工具一执行就报缺参。
        只做这一组已知的改名，其余原样保留——不认识的参数交给工具自己报错。
        """
        low = str(tag or '').strip().lower()
        group = self.groups.get(str(name or '').strip().lower(), '')
        if group != 'bash':
            return args
        if low in self.exact and low in _CODE_TAGS:
            return args          # 同名直配的 execute 类工具，code 就是它要的
        if 'command' not in args:
            for key in ('code', 'cmd', 'script', 'input', 'shell'):
                if key in args:
                    args['command'] = args.pop(key)
                    break
        return args


def _marker_at(text: str, index: int) -> str:
    for pattern in (_OPEN, _BARE):
        match = pattern.match(text, index)
        if match:
            return match.group(0)
    return ''


def _next_marker(text: str, start: int) -> int:
    found = -1
    for pattern in (_OPEN, _BARE):
        match = pattern.search(text, start)
        if match and (found < 0 or match.start() < found):
            found = match.start()
    return found


def _marker_tail(text: str, after: int) -> str:
    """开 marker 之后紧跟的是什么：`'marker'` / `'prefix'` / `'text'` / `'empty'`。

    2026-11-04 线上样本：模型把同一条调用的开头连写了两遍——

        <tool_call>
        <tool_call>
        tool_call write({"path": "…", "content": "…"})

    参数其实能解析（调用也执行了），但多出来的两个开头被当正文保留，客户端看到
    的回复就是一上来两行 `<tool_call>`。重复的开头是纯脚手架，必须丢掉；但正文
    里提到 `<tool_call>` 这个词（后面跟着句子）不能丢——所以只有「跳空白后紧跟
    另一条 marker / marker 的半截」才算。
    """
    probe = after
    while probe < len(text) and text[probe] in ' \t\r\n':
        probe += 1
    if probe >= len(text):
        return 'empty'
    if _marker_at(text, probe):
        return 'marker'
    tail = text[probe:].lower()
    if '>' not in tail and any(m.startswith(tail) for m in _MARKERS):
        return 'prefix'
    # 裸标记还没打出「名字(」（`tool_call w` / `tool_call `）：下一帧就可能
    # 变成调用，同样是重复开头的一部分。
    if _BARE_TAIL.match(text, probe):
        return 'prefix'
    return 'text'


def _next_tag(text: str, start: int, resolver: _Resolver | None) -> int:
    """下一个「可能是标签调用」的 `<`；普通正文里的 `<` 不算。"""
    if not resolver:
        return -1
    position = start
    while position < len(text):
        index = text.find('<', position)
        if index < 0:
            return -1
        if _marker_at(text, index):
            return index
        kind, _name, _arguments, _end = _scan_tag(text, index, resolver)
        if kind != 'no':
            return index
        position = index + 1
    return -1


def _next_container(text: str, start: int, *, final: bool = True) -> int:
    """下一个**带参数**的容器开标签（`<call>` / `<tool_calls>` / `<invoke>`）。

    不带 `<parameter>` 子标签的容器（`<calls>` 只是包裹、`<call>` 是正文里
    随手写的标签）不算调用开头，仍按正文处理。流式（`final=False`）时
    「还可能有参数」的容器算开头，交给 `Sniffer` 扣住。
    """
    position = start
    while True:
        match = _CONTAINER.search(text, position)
        if not match:
            return -1
        kind, _name, _arguments, _end = _scan_container(text, match.start(),
                                                        final=final)
        if kind != 'no':
            return match.start()
        position = match.end()


def _next_start(text: str, start: int, resolver: _Resolver | None, *,
                final: bool = True) -> int:
    found = _next_marker(text, start)
    dsml = _next_dsml(text, start)
    if dsml >= 0 and (found < 0 or dsml < found):
        found = dsml
    container = _next_container(text, start, final=final)
    if container >= 0 and (found < 0 or container < found):
        found = container
    tag = _next_tag(text, start, resolver)
    if tag >= 0 and (found < 0 or tag < found):
        return tag
    return found


def _tail_prefix(text: str) -> int:
    """正文末尾可能有几个字符是 marker 的开头（跨帧被切开），要留住。"""
    for marker in _MARKERS:
        for size in range(min(len(marker) - 1, len(text)), 0, -1):
            if text.endswith(marker[:size]):
                return size
    return 0


def _tail_hold(text: str, resolver: _Resolver | None) -> int:
    """正文末尾要扣住的长度：既可能是半个 marker，也可能是半个标签。

    普通文本里的 `<` 也最多扣到下一帧/收尾——代价是一次延迟，换来的是
    「标签被帧切开时不至于先把半截当正文发出去」。
    """
    hold = _tail_prefix(text)
    # 裸标记已经完整、但「名字(」还没到：扣住整段尾巴等下一帧。
    bare = _BARE_TAIL.search(text)
    if bare:
        hold = max(hold, len(text) - bare.start())
    index = text.rfind('<')
    if index < 0:
        return hold
    tail = text[index:]
    # DSML 令牌被帧切开（`<｜`、`<｜｜DSM`、`</｜`…）：这种前缀不可能出现在
    # 正常正文里，必须先扣住。它与 resolver 无关（DSML 不依赖已声明的工具名）。
    if _DSML_TAIL_START.match(tail) and '>' not in tail:
        return max(hold, len(tail))
    # 容器开标签被帧切开（`<cal`、`<tool_call`、`</inv`…）：同样与 resolver
    # 无关。不扣住就会先把半截标签当正文发出去，后面的参数再也接不回来。
    if '>' not in tail and _container_prefix(tail):
        return max(hold, len(tail))
    if not resolver:
        return hold
    if '>' in tail:
        return hold
    if tail == '<':
        return max(hold, 1)
    if _TAG_NAME.fullmatch(tail):
        return max(hold, len(tail))
    return hold


def _partial_close(text: str) -> bool:
    """`text` 是否是某个闭合标签的前半截（被帧切开）。

    完整闭合标签由调用方的 `_CLOSE.match` 消费；这里只扣住 `</ca` 这种
    还差几个字符的情况，避免先把开标签当正文放出去。
    """
    head = text.lstrip()
    if not head:
        return True
    return any(marker.startswith(head) and len(head) < len(marker)
               for marker in _CLOSE_TAGS)


def _balanced(text: str, start: int, open_char: str = '(',
              close_char: str = ')') -> int | None:
    """从 `start`（指向 `open_char`）扫到配对的 `close_char`，返回其后的下标。

    只认双引号字符串（JSON 语法），这样正文里的撇号不会影响括号计数。
    """
    depth = 0
    quoted = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def _from_json_call(value: object) -> tuple[str, str] | None:
    """把一坨 JSON 认成 `(name, arguments)`；认不出返回 None。"""
    if not isinstance(value, dict):
        return None
    inner = value.get('function')
    if isinstance(inner, dict):
        value = inner
    name = value.get('name') or value.get('tool') or value.get('tool_name')
    if not isinstance(name, str) or not name.strip():
        return None
    arguments = value.get('arguments')
    if arguments is None:
        arguments = value.get('parameters')
    if arguments is None:
        arguments = value.get('input')
    if arguments is None:
        arguments = {}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return None
    if not isinstance(arguments, dict):
        return None
    try:
        text = json.dumps(arguments, ensure_ascii=False)
    except (TypeError, ValueError):
        return None
    return name.strip(), text


def _scan_json(text: str, start: int) -> tuple[str, str, str, int]:
    end = _balanced(text, start, '{', '}')
    if end is None:
        return ('need', '', '', 0)
    try:
        value = json.loads(text[start:end])
    except ValueError:
        return ('no', '', '', 0)
    call = _from_json_call(value)
    if call is None:
        return ('no', '', '', 0)
    return ('ok', call[0], call[1], end)


def _flat_prefix(tail: str) -> bool:
    """`tail` 是不是一个还没写完的 `<parameter` / `<function` 标签。

    两种半截都要认：名字还没收全（`<par`），以及名字收了但属性/值还没到
    （`<parameter name="path"`）——后者不收住就会在流式下把标签漏出去。
    """
    low = (tail or '').lstrip().lower()
    if not low.startswith('<'):
        return False
    body = low[1:].lstrip()
    for tag in ('parameter', 'function'):
        if tag.startswith(body):
            return True
        if body.startswith(tag) and '>' not in body:
            return True
    return False


def _scan_flat(text: str, start: int, resolver: _Resolver | None,
               *, final: bool) -> tuple[str, str, str, int] | None:
    """`marker` 后面紧跟参数标签的「扁平调用」。

    两种写法：`<tool_call>` + `<parameter name="…">值</parameter>`（没有工具
    名，无法执行），以及 `<tool_call>` + `<function=read>` + `<parameter=path>`
    （有工具名，可以还原）。

    - 有名字 → `('ok', name, arguments, end)`；
    - 没有名字 → `('drop', '', '', end)`，整块从正文里删掉，**绝不原样透传**
      ——2026-11-03 线上样本就是整段 `<tool_call>` + `<parameter …>` 漏进回复；
    - 流式（`final=False`）一律 `('need', …)`：参数随时可能被帧切开，扣到收尾
      再定性，代价只是一次延迟；
    - 返回 None 表示「不是这种形态」，交回 `_scan` 的原有规则。
    """
    index = start
    name = ''
    token = _FUNCTION_TOKEN.match(text, index)
    if token:
        name = token.group('name')
        index = token.end()
    else:
        match = _NAME.match(text, index)
        if match:
            probe = match.end()
            while probe < len(text) and text[probe] in ' \t\r\n':
                probe += 1
            if _PARAM_OPEN.match(text, probe) or _flat_prefix(text[probe:]):
                name = match.group(0)
                index = probe
    while index < len(text) and text[index] in ' \t\r\n':
        index += 1
    if not _PARAM_OPEN.match(text, index):
        # 参数标签还在路上（`<tool_call>` 刚写完、`<function=read>` 已到齐、
        # 或标签名只写了一半）：先扣住，等下一帧。
        if (_flat_prefix(text[start:])
                or (name and index >= len(text))
                or (name and _flat_prefix(text[index:]))):
            return ('need', '', '', 0)
        return None
    if not final:
        return ('need', '', '', 0)
    params: dict[str, Any] = {}
    position = index
    while True:
        pair = _PARAM_PAIR.match(text, position)
        if not pair:
            break
        params[pair.group('key').strip()] = pair.group('value').strip()
        position = pair.end()
        while position < len(text) and text[position] in ' \t\r\n':
            position += 1
    # 收尾的 `</tool_call>` 可有可无；后面若还挂着半个参数标签，说明这条调用
    # 被截断了——整段丢掉，别把半截标签留在正文里。
    close = _CLOSE.match(text, position)
    if close:
        position = close.end()
        while position < len(text) and text[position] in ' \t\r\n':
            position += 1
    if position < len(text):
        if _PARAM_OPEN.match(text, position) or _flat_prefix(text[position:]):
            position = len(text)
    if not params:
        # 只有半个参数标签、没有完整参数：同样是脚手架。
        return ('drop', '', '', max(position, len(text)))
    if not name:
        return ('drop', '', '', position)
    arguments = dict(params)
    if resolver:
        target = resolver.resolve_marker(name)
        if target and target != name:
            arguments = resolver.normalize_args(name, target, arguments)
            name = target
    return ('ok', name, json.dumps(arguments, ensure_ascii=False), position)


def _container_name(attrs: str) -> str:
    """从容器开标签的属性里取工具名（`name="bash"` / `name=bash`）。"""
    match = re.search(r'\bname\s*=\s*'
                      r'(?P<q>["\']?)(?P<name>[^"\'\s>]+)(?P=q)', attrs or '', re.I)
    if not match:
        return ''
    return match.group('name').strip()


def _scan_container(text: str, start: int,
                    resolver: _Resolver | None = None, *,
                    final: bool = True) -> tuple[str, str, str, int] | None:
    """从 `start`（指向容器开标签的 `<`）解析一个「无名容器 + 参数」调用。

    `<tool_call>` 的扁平形态由 `_scan_flat` 处理；这里补上 `<call>` / `<calls>` /
    `<tool_calls>` / `<invoke>` / `<function_call>` 这一族（2026-11-03 线上样本
    整段漏成正文，见文件头注释）。

    - 返回 None 表示「这里不是容器」；
    - `('ok', name, args, end)`：有工具名，照常下发；
    - `('drop', '', '', end)`：没有工具名（或块被截断），整段丢弃，绝不当正文；
    - `('need', …)`：流式下还等参数/闭合；
    - `('no', …)`：容器里没有参数标签，按正文透传。
    """
    head = _CONTAINER.match(text, start)
    if not head:
        return None
    name = _container_name(head.group('attrs'))
    index = head.end()
    while index < len(text) and text[index] in ' \t\r\n':
        index += 1
    token = _FUNCTION_TOKEN.match(text, index)
    if token:
        name = name or token.group('name')
        index = token.end()
        while index < len(text) and text[index] in ' \t\r\n':
            index += 1
    elif not name:
        match = _NAME.match(text, index)
        if match:
            probe = match.end()
            while probe < len(text) and text[probe] in ' \t\r\n':
                probe += 1
            if _PARAM_OPEN.match(text, probe):
                name = match.group(0)
                index = probe
            elif not final and (probe >= len(text)          # 名字还没收全 / 只跟了空白
                                or _flat_prefix(text[probe:])
                                or (text[probe] == '<' and '>' not in text[probe:])):
                # 裸名字（`<call>\nread\n<parameter …>`）或参数标签被帧切开：
                # 先扣住。上一版在这里判成「不是容器」，`<call>` 就先当正文漏了。
                return ('need', '', '', 0)
            else:
                # `<call>\nread\n</call>`：有名字、没参数 —— 畸形调用块，不是正文。
                # 参数缺失没法执行，但开标签也绝不能漏出去。
                close = _CONTAINER_ANY_CLOSE.match(text, probe)
                if close:
                    return ('drop', '', '', close.end())
    if not _PARAM_OPEN.match(text, index):
        # 空对 `<call></call>`：纯噪音，整段丢掉（否则开标签会当正文漏出去）。
        empty = _CONTAINER_ANY_CLOSE.match(text, index)
        if empty:
            return ('drop', '', '', empty.end())
        # 包裹层（`<tool_calls>` / `<calls>` 里套 `<invoke name="…">`）：交给内层
        # 容器。外层自己不带参数，`_CONTAINER_ANY_CLOSE` 会把它的收尾清掉。
        # 外层**带 name 也要往下找**：`<invoke name="write">\n<invoke name="write">`
        # 是模型把同一个开头连写了两次，外层的开标签不交给内层就会漏成正文。
        # 但只有「两层标签之间只有空白」才算包裹/重复——中间隔着正文时是散文里
        # 提到的标签，不能把正文一起吞掉。
        inner = _CONTAINER_OPEN.search(text, index)
        if inner and not text[index:inner.start()].strip():
            nested = _scan_container(text, inner.start(), resolver, final=final)
            if nested is not None and nested[0] != 'no':
                return nested
        if not final:
            # 参数/名字/内层可能还没到（帧恰好切在开标签之后）：先扣住。
            # 不扣的话 `<call>` 会先当正文发出去，后面的参数再也接不回来。
            if (index >= len(text) or _flat_prefix(text[index:])
                    or _container_prefix(text[index:])
                    or _CONTAINER_OPEN.search(text, index)):
                return ('need', '', '', 0)
        return ('no', '', '', 0)
    if not final:
        return ('need', '', '', 0)
    params: dict[str, Any] = {}
    position = index
    while True:
        pair = _PARAM_PAIR.match(text, position)
        if not pair:
            break
        params[pair.group('key').strip()] = pair.group('value').strip()
        position = pair.end()
        while position < len(text) and text[position] in ' \t\r\n':
            position += 1
    close = _CONTAINER_ANY_CLOSE.search(text, position)
    if close is None:
        # 没有闭合：被截断的调用块。有名字也不下发（参数可能只写了一半），
        # 整段丢弃后由退化判定报错，避免客户端拿到半条文本 + `stop` 停住。
        return ('drop', '', '', len(text))
    end = close.end()
    if not name:
        # 没有工具名：无法执行，但绝不能当正文漏出去。
        return ('drop', '', '', end)
    arguments: dict[str, Any] = dict(params)
    if resolver:
        target = resolver.resolve_marker(name)
        if target and target != name:
            arguments = resolver.normalize_args(name, target, arguments)
            name = target
    return ('ok', name, json.dumps(arguments, ensure_ascii=False), end)


def _scan(text: str, start: int,
          resolver: _Resolver | None = None, *,
          final: bool = True) -> tuple[str, str, str, int]:
    """从 `start` 试着解析一个 marker 形态的调用。

    返回值 `('ok', name, arguments, end)` / `('need', '', '', 0)`（还差字符，
    要等下一帧）/ `('no', '', '', 0)`（不是调用，按正文原样放行）/
    `('drop', '', '', end)`（认得出是无名调用块，整段丢弃）。

    `arguments` 保留模型写的原文（已校验是合法 JSON 对象），不重新序列化：
    改动会替模型「重排」参数，长参数下还可能引入转义差异。

    `resolver` 给了就顺带做工具名映射：同名直配原样，同义组唯一命中就换成
    客户端真正声明的名字（`[tool_call] execute({...})` → `bash`），参数名
    一并改名。都对不上仍原样下发（客户端会报 unknown tool，但 Agent 循环
    不会因为「网关擅自丢掉一条调用」而停住）。
    """
    index = start
    marker = _marker_at(text, index)
    if marker:
        index += len(marker)
    while index < len(text) and text[index] in ' \t\r\n':
        index += 1
    if index >= len(text):
        return ('need', '', '', 0) if marker else ('no', '', '', 0)
    if text[index] == '<' and _partial_close(text[index:]):
        # 闭合标签被帧切开（`</ca` + `ll>`）：先别把开标签放成正文。
        return ('need', '', '', 0)
    if marker:
        # `<tool_call>` 后面直接跟参数标签 / 裸工具名 + 参数标签：扁平形态。
        # 不是这种形态时 `_scan_flat` 返回 None，继续走 `name(...)` 解析。
        flat = _scan_flat(text, index, resolver, final=final)
        if flat is not None:
            return flat
    if text[index] == '{':
        # JSON 载荷形态只有跟着 marker 才认；单独一段 JSON 太容易和正文混淆。
        return _scan_json(text, index)
    match = _NAME.match(text, index)
    if not match:
        return ('no', '', '', 0)
    name = match.group(0)
    cursor = match.end()
    while cursor < len(text) and text[cursor] in ' \t\r\n':
        cursor += 1
    if cursor >= len(text):
        return ('need', '', '', 0)          # 还可能有 `(`
    if text[cursor] != '(':
        return ('no', '', '', 0)
    end = _balanced(text, cursor)
    if end is None:
        return ('need', '', '', 0)
    raw = text[cursor + 1:end - 1].strip()
    if not raw:
        return ('ok', *_marker_map(resolver, name, '{}'), end)   # 无参调用
    try:
        value = json.loads(raw)
    except ValueError:
        return ('no', '', '', 0)
    if not isinstance(value, dict):
        return ('no', '', '', 0)
    return ('ok', *_marker_map(resolver, name, raw), end)


def _marker_map(resolver: _Resolver | None, name: str,
                arguments: str) -> tuple[str, str]:
    """正文形态的工具名/参数名映射。

    只在这条调用**换过工具名**时才改参数名（`execute` → `bash` 时
    `code` → `command`）。同名直配时参数原样保留，不替模型重排 JSON。
    """
    if not resolver:
        return name, arguments
    target = resolver.resolve_marker(name) or name
    if target == name:
        return name, arguments
    try:
        value = json.loads(arguments or '{}')
    except ValueError:
        return target, arguments
    if not isinstance(value, dict):
        return target, arguments
    value = resolver.normalize_args(name, target, value)
    return target, json.dumps(value, ensure_ascii=False)


def _tag_attrs(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for match in _TAG_ATTR.finditer(text or ''):
        key = match.group(1)
        raw = match.group(2).strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in '"\'':
            raw = raw[1:-1]
        out[key] = raw
    return out


def _tag_arguments(args: dict[str, Any], body: str,
                   primary: str) -> dict[str, Any] | None:
    body = (body or '').strip()
    if not body:
        return args
    nested = _NESTED_TAG.fullmatch(body)
    if nested:
        key = nested.group(1)
        args.setdefault(key, nested.group(2).strip())
        return args
    if body.startswith('{'):
        try:
            value = json.loads(body)
        except ValueError:
            value = None
        if isinstance(value, dict):
            for key, item in value.items():
                args.setdefault(key, item)
            return args
    args.setdefault(primary, body)
    return args


def _scan_tag(text: str, start: int,
              resolver: _Resolver | None) -> tuple[str, str, str, int]:
    """从 `start`（指向 `<`）试着解析一个标签形态的调用。"""
    if not resolver:
        return ('no', '', '', 0)
    match = _TAG_NAME.match(text, start)
    if not match:
        # 只有一个孤零零的 `<`：可能是标签被帧切开，等下一帧。
        return ('need', '', '', 0) if text[start:] == '<' else ('no', '', '', 0)
    tag = match.group(1)
    close = text.find('>', match.end())
    if close < 0:
        # 名字可能还没收全（`<exe` → `<execute`），此时不能急着定性。
        return ('need', '', '', 0)
    name = resolver.resolve(tag)
    if not name:
        return ('no', '', '', 0)
    head = text[match.end():close]
    self_closing = head.rstrip().endswith('/')
    args = _tag_attrs(head.rstrip().rstrip('/'))
    if self_closing:
        return ('ok', name, json.dumps(args, ensure_ascii=False) if args else '{}',
                close + 1)
    closing = f'</{tag}>'
    end = text.lower().find(closing.lower(), close + 1)
    if end < 0:
        return ('need', '', '', 0)
    body = text[close + 1:end]
    merged = _tag_arguments(args, body, resolver.primary_arg(tag, name))
    merged = resolver.normalize_args(tag, name, merged)
    if merged is None:
        return ('no', '', '', 0)
    return ('ok', name,
            json.dumps(merged, ensure_ascii=False) if merged else '{}',
            end + len(closing))


def _dsml_at(text: str, index: int):
    """`index` 处是不是一个 DSML 令牌（开或闭都算）。"""
    return _DSML_TAG.match(text, index)


def _next_dsml(text: str, start: int) -> int:
    """下一个 DSML **开**令牌的下标；闭合令牌与孤立的 `parameter` 跳过。"""
    match = _DSML_OPEN.search(text, start)
    return match.start() if match else -1


def _dsml_invoke_end(text: str, start: int):
    """从 invoke 开令牌往后找它的结束令牌。

    线上不一定会写 `</｜｜DSML｜｜invoke>`——样本里模型用
    `</｜｜DSML｜｜tool_call>` 收尾（甚至把 `invoke`/`parameter` 的闭合全省了）。
    所以 `</…invoke>` 与 `</…tool_call(s)>` 谁先到就算谁，剩下的交给
    `_dsml_incomplete()` 判退化。
    """
    closing = None
    for pattern in (_DSML_INVOKE_CLOSE, _DSML_CALLS_CLOSE):
        match = pattern.search(text, start)
        if match and (closing is None or match.start() < closing.start()):
            closing = match
    return closing


def _scan_dsml(text: str, start: int,
               resolver: _Resolver | None = None, *,
               final: bool = True) -> tuple[str, str, str, int]:
    """从 `start`（指向 DSML 开令牌的 `<`）解析一条 DSML 调用。

    返回 `('ok', name, arguments, end)` / `('need', …)`（等下一帧）/
    `('no', …)`（不是可执行调用）/ `('drop', …)`（没有工具名的调用块，
    整段丢弃）。一个 `calls` 块里有多条 `invoke` 时只返回第一条，`end` 落在
    它的 `</…invoke>` 之后——调用方继续往后扫，剩下的 `invoke` 会各自被认出，
    末尾的 `</…calls>` 由 `_strip_tags` 清掉。

    这里不复用「找带 name 的 invoke」的旧写法：块里只要有一条 `invoke` 没写
    `name=`，旧逻辑就跳过它、把开标签当正文漏出去（2026-11-03 线上样本：
    整串 `<｜｜DSML｜｜tool_calls>` / `<｜｜DSML｜｜invoke>` 出现在回复里）。
    现在按令牌顺序扫：无名的 `invoke` 整段丢弃，有名字的才还原。
    """
    head = _DSML_TAG.match(text, start)
    if not head or head.group('close'):
        return ('no', '', '', 0)
    kind = head.group('kind').lower()
    if kind == 'parameter':
        return ('no', '', '', 0)

    position = head.start() if kind == 'invoke' else head.end()
    for token in _DSML_TAG.finditer(text, position):
        token_kind = token.group('kind').lower()
        if token.group('close'):
            # 块已经闭合：里面只剩参数（没有 invoke 头）就是无名调用块，
            # 一个参数都没有的纯空块只是噪音。
            if 'call' not in token_kind and token_kind != 'invoke':
                continue
            if _DSML_PARAM_OPEN.search(text, head.end(), token.start()):
                return ('drop', '', '', token.end()) if final else (
                    'need', '', '', 0)
            return ('no', '', '', 0)
        if token_kind != 'invoke':
            continue
        named = _DSML_INVOKE_OPEN.match(text, token.start())
        close = _dsml_invoke_end(text, token.end())
        if close is None:
            return ('need', '', '', 0)
        if named is None:
            # `invoke` 没写 name：无法执行，也不能当正文，整段丢掉。
            return ('drop', '', '', close.end()) if final else (
                'need', '', '', 0)
        name = named.group('name').strip()
        params: dict[str, Any] = {}
        for match in _DSML_PARAM.finditer(text, token.end(), close.start()):
            params[match.group('key').strip()] = match.group('value').strip()
        if resolver:
            target = resolver.resolve_marker(name)
            if target and target != name:
                params = resolver.normalize_args(name, target, params)
                name = target
        return ('ok', name, json.dumps(params, ensure_ascii=False), close.end())
    return ('need', '', '', 0)


def _dsml_incomplete(text: str) -> bool:
    """文本里有没有「只开了头、没等到闭合」的 DSML 调用块。

    用来判退化：完整块会被 `_scan_dsml` 解析成调用（`calls` 非空，提前返回），
    走到这里的只剩被截断的块——它既没有可执行调用，也不能当正文透传。
    """
    position = 0
    while True:
        head = _DSML_OPEN.search(text, position)
        if not head:
            return False
        kind = head.group('kind').lower()
        if kind == 'parameter':
            position = head.end()
            continue
        if 'call' in kind:
            close = _DSML_CALLS_CLOSE.search(text, head.end())
        else:
            close = _dsml_invoke_end(text, head.end())
        if close is None:
            return True
        position = close.end()


def _scan_at(text: str, index: int, resolver: _Resolver | None, *,
             final: bool = True) -> tuple[str, str, str, int]:
    if _dsml_at(text, index):
        return _scan_dsml(text, index, resolver, final=final)
    if _marker_at(text, index):
        return _scan(text, index, resolver, final=final)
    if _CONTAINER_OPEN.match(text, index):
        container = _scan_container(text, index, resolver, final=final)
        if container is not None:
            return container
    if text.startswith('<', index):
        return _scan_tag(text, index, resolver)
    return _scan(text, index, resolver, final=final)


def parse_calls(text: str, tool_names: object = (), *,
                strict_tail: bool = True) -> tuple[list[dict], str]:
    """扫完整段正文，返回 (调用列表, 剩下的正文)。

    调用列表是 `[{'name':…, 'arguments':…}]`，顺序与正文一致；剩下的正文里
    已经剔除了被还原的那几段，其余一字不动。

    `tool_names` 是客户端本轮声明的工具名。给了才会启用标签形态的还原；
    不给就只认三种 `tool_call` 文本形态（与旧行为一致）。

    `strict_tail` 开启时，调用后面还接着正文就视为「散文里的举例」，不还原。
    """
    return _parse_calls(text, _Resolver(tool_names), strict_tail=strict_tail)


def _parse_calls(text: str, resolver: _Resolver, *,
                 masked: str | None = None,
                 strict_tail: bool = True) -> tuple[list[dict], str]:
    """`text` 是原文（参数/正文都从这里取）；`masked` 是等长的掩码文本。

    只在 `masked` 上定位 marker —— 围栏里被屏蔽的区间永远找不到 marker，
    所以代码块里的示例不会被还原，而原文一个字都不会被破坏。
    """
    calls, remainder, _dropped = _parse_calls_detail(
        text, resolver, masked=masked, strict_tail=strict_tail)
    return calls, remainder


def _parse_calls_detail(text: str, resolver: _Resolver, *,
                        masked: str | None = None,
                        strict_tail: bool = True,
                        line_start: bool | None = None
                        ) -> tuple[list[dict], str, bool]:
    """同 `_parse_calls`，额外返回「是否丢弃过无名调用块」。

    无名调用块（`<tool_call>` + `<parameter …>`、`invoke` 缺 `name` 的 DSML）
    整段从正文里删掉；调用方据此判定「整轮只剩脚手架」并报退化错误。
    """
    if masked is None:
        masked, _ = _mask_spans(text)
    calls: list[dict] = []
    kept: list[str] = []
    dropped = False
    position = 0
    while position < len(text):
        index = _next_start(masked, position, resolver)
        if index < 0:
            kept.append(text[position:])
            break
        kept.append(text[position:index])
        kind, name, arguments, end = _scan_at(text, index, resolver)
        if kind == 'drop':
            # 没有工具名的调用块：整段丢掉，绝不当正文漏出去。
            dropped = True
            position = end if end > index else index + 1
            continue
        if kind == 'ok':
            # 占位符（`{"command": "..."}`）：模型在讲解格式 / 复述协议提示，
            # 不是真要执行。上一版只在流式路径拦了它，非流式仍会执行。
            if _placeholder_args(arguments):
                kept.append(text[index:end])
                position = end
                continue
            # 独占一行才执行：前后同一行还有别的正文，说明这是夹在句子里的
            # **举例**（模型在讲解格式），还原它等于执行一条没人要求的调用。
            # 判据只看块的两侧、不看后面还有没有正文——流式在块闭合后立刻就能
            # 定性，于是同一段文本在流式/非流式下结论一致。
            stop = end
            close = _CLOSE.match(text, end)
            if close:
                stop = close.end()
            if strict_tail and _own_line(masked, index, stop, pending=False,
                                         line_start=line_start) != 'yes':
                kept.append(text[index:stop])
                position = stop
                continue
            calls.append({'name': name, 'arguments': arguments})
            position = stop
            continue
        marker = _marker_at(masked, index)
        if not marker:
            kept.append(text[index])
            position = index + 1
            continue
        after = index + len(marker)
        close = _CLOSE.match(text, after)
        if close:
            # 空对：只有开头、紧跟闭合标签，没有可解析的调用 → 整段丢掉。
            position = close.end()
            continue
        if _marker_tail(masked, after) in ('marker', 'prefix'):
            # 连写的重复开头（`<tool_call>\n<tool_call>\ntool_call write(…)`）：
            # 多出来的 marker 是脚手架，删掉；正文里提到这个词的情况走不到这里
            # （后面跟的是句子，`_marker_tail` 判 `text`）。
            position = after
            continue
        kept.append(marker)
        position = after
    remainder = _strip_tags(''.join(kept))
    # 正文只剩空白（DSML 块自带换行的典型情况）：别下发一个只有空行的 content。
    # 有真正文时一个字都不动。
    if not remainder.strip():
        remainder = ''
    return calls, remainder, dropped


def _attempt_from(text: str, index: int) -> bool:
    """从 `index`（指向 marker）开始，到结尾是不是一条没解析出来的调用。"""
    marker = _marker_at(text, index)
    if not marker:
        return False
    cursor = index + len(marker)
    while cursor < len(text) and text[cursor] in ' \t\r\n':
        cursor += 1
    if cursor >= len(text):
        return True
    if text[cursor] == '{':
        end = _balanced(text, cursor, '{', '}')
        raw = text[cursor:end] if end is not None else ''
    else:
        match = _NAME.match(text, cursor)
        if not match:
            return False
        cursor = match.end()
        while cursor < len(text) and text[cursor] in ' \t\r\n':
            cursor += 1
        if cursor >= len(text):
            return True
        if text[cursor] != '(':
            return False
        end = _balanced(text, cursor)
        # 参数取括号内部，才能按 JSON 判占位符（`({"command": "..."})`）。
        raw = text[cursor + 1:end - 1] if end is not None else ''
    if end is not None and _placeholder_args(raw):
        return False                    # 完整但只是占位符 → 示例，按正文
    return end is None or not text[end:].strip()


def _attempt_only(text: str, masked: str | None = None) -> bool:
    """正文**末尾**是不是「一条没解析出来的调用」（参数截断 / 非法 JSON）。

    只在 `_parse_calls` 已经确认没有可执行调用之后调用。只看最后一个
    marker：前面可能是正文（`先看目录。\\n[tool_call] bash({…` 被截断），
    这种回合同样既没有可执行调用、也没有完整正文，必须报错误而不是让
    客户端拿到半条文本 + `stop` 后停住。

    调用后面还有别的正文就不算——那种情况按「宁可漏」原样透传。
    """
    source = masked if masked is not None else text
    index = -1
    position = 0
    while True:
        found = _next_marker(source, position)
        if found < 0:
            break
        index = found
        position = found + 1
    if index < 0:
        return False
    return _attempt_from(text, index)


def _open_tag_only(text: str, resolver: _Resolver | None,
                   masked: str | None = None) -> bool:
    """正文**末尾**是不是「一个能对上已声明工具、但没闭合的标签」。

    线上样本：模型只写出 `<execute code="ls -la">` 就停了（没有闭合标签）。
    `_scan_tag` 返回 `need`，`flush` 把它当正文放行 → 客户端拿到一段没人能用的
    标签 + `stop`，Agent 循环停住。这正是上一轮要修的原始症状，只是换了外壳。

    只看最后一个 `<`，所以「先看目录。\\n<execute code="ls">」同样算退化；
    而 `用 <read> 这种标签表示读取文件` 后面还有正文，不算。
    """
    if not resolver:
        return False
    source = masked if masked is not None else text
    start = source.rfind('<')
    if start < 0:
        return False
    tail = text[start:]
    mtail = source[start:]
    match = _TAG_NAME.match(mtail)
    if not match:
        return False
    if not resolver.resolve(match.group(1)):
        return False
    close = mtail.find('>')
    if close < 0:
        return True                     # `<execute code="ls"` 被截断
    return not mtail[close + 1:].strip()


def _degenerate_text(text: str, calls: list[dict],
                     resolver: _Resolver | None = None,
                     masked: str | None = None, *,
                     dropped: bool = False, rest: str = '') -> bool:
    """`text` 是否只剩调用脚手架；`calls` 是已经解析出的可执行调用。

    `dropped` / `rest` 由 `_parse_calls_detail` 给出：丢过无名调用块、且剩下
    的正文是空的，说明整轮就只有那一段脚手架（2026-11-03 线上样本）。
    """
    if calls:
        return False
    if dropped and not (rest or '').strip():
        return True
    text = text or ''
    stripped = text.strip()
    if not stripped:
        return False
    if _NOISE.search(stripped) and not _NOISE.sub('', stripped).strip():
        return True
    # 只剩 DSML 闭合残片（`</｜｜DSML｜｜parameter>`）：同样是脚手架，不能当正文。
    if not _strip_tags(stripped).strip():
        return True
    # 掩码与原文等长；裁掉两侧空白后再用，下标才能对上 `stripped`。
    if masked is None or len(masked) != len(text):
        masked, _ = _mask_spans(stripped)
    else:
        left = len(text) - len(text.lstrip())
        masked = masked[left:left + len(stripped)]
    if _open_tag_only(stripped, resolver, masked):
        return True
    # DSML 令牌漏进正文、调用块却没等到闭合：这是一条被截断的调用，
    # 既不能执行也不能当正文透传（2026-11-02 实测会原样漏给客户端）。
    if _dsml_incomplete(masked):
        return True
    return _attempt_only(stripped, masked)


def is_degenerate_call(text: str, tool_names: object = ()) -> bool:
    """整轮是否只剩工具调用的标签/半截调用，没有任何可读内容。

    这种回合以前会以「空正文 + `finish_reason: stop`」收尾，客户端把它当成
    一条完整回复，Agent 循环当场停住。调用方据此报错，让客户端能看到原因
    （并自行重试），而不是静默中断。

    `tool_names` 与 `parse_calls` 同义：先确认没有可执行调用，再判退化。

    判据只认 **tool_call 家族**的标签与半截调用：普通 HTML（`</div>`）不是
    脚手架，模型正常回答一段 HTML 时不该被判成错误（2026-11-02 实测）。
    """
    stripped = (text or '').strip()
    if not stripped:
        return False
    resolver = _Resolver(tool_names)
    masked, _ = _mask_spans(stripped)
    calls, rest, dropped = _parse_calls_detail(
        stripped, masked=masked, resolver=resolver)
    return _degenerate_text(stripped, calls, resolver, masked,
                            dropped=dropped, rest=rest)


class Sniffer:
    """流式版：把还没法定性的尾巴留在自己手里。

    `feed()` 吐出 `('content', str)` / `('tool_calls', [{'name','arguments'}])`
    两种片段；`flush()` 在流结束时收掉残留。判据只有一条：**文本还有可能变成
    调用，就等；不可能了，立刻放行**——所以正常回答不会因为它在正文里出现
    「tool_call 这个字段」而被扣住。

    `tool_names` 非空时，标签形态（`<execute code="...">` 等）也参与嗅探；
    标签名对不上已声明工具就按正文放行。
    """

    def __init__(self, *, enabled: bool = True, limit: int = _LIMIT,
                 tool_names: object = (), strict_tail: bool = True) -> None:
        self.enabled = enabled
        self._limit = limit
        self._resolver = _Resolver(tool_names)
        self._strict_tail = strict_tail
        self._buf = ''
        # 已经放行/消费掉的文本是否停在 ``` 围栏内部（围栏里的内容不扫）。
        self._fence = False
        # 已经消费掉的文本，最后一行是否只有空白（块从缓冲区开头开始时用来
        # 判断「同一行前面有没有别的正文」）。
        self._line_start = True
        # 只含空白的正文先扣住：调用后面的那个换行不该单独变成一条空白回复，
        # 后面真还有正文时再原样补到它前面。
        self._pending = ''
        # 本轮是否已经下发过正文/调用。已经出过正文的回合，收尾的脚手架不算
        # 「整轮只剩脚手架」——否则前面说过的话会被一条退化错误抹掉。
        self._emitted = False
        # 收尾时发现整段缓冲区只有调用脚手架（没有可执行调用/正文）。
        self.degenerate = False

    def _content(self, out: list[tuple[str, Any]], text: str) -> None:
        """放行一段正文；纯空白的尾巴留到确认后面还有内容再发。"""
        text = _strip_tags(self._pending + text)
        self._pending = ''
        if not text:
            return
        if not text.strip():
            self._pending = text
            return
        out.append(('content', text))

    def _flush_pending(self, out: list[tuple[str, Any]]) -> None:
        if self._pending:
            out.append(('content', self._pending))
            self._pending = ''

    def _consume(self, size: int) -> str:
        """从缓冲区头部取出 `size` 个字符，并推进围栏/行首状态。"""
        text, self._buf = self._buf[:size], self._buf[size:]
        masked, self._fence = _mask_spans(text, self._fence)
        newline = masked.rfind('\n')
        if newline >= 0:
            self._line_start = not masked[newline + 1:].strip()
        else:
            self._line_start = self._line_start and not masked.strip()
        return text

    def _masked(self) -> str:
        masked, _ = _mask_spans(self._buf, self._fence)
        return masked

    # 已经认得出的调用块（`<call>` / `tool_call(…)` / DSML）给更宽的上限：
    # 一次 `write` 把整个文件塞进参数时，64 KB 根本不够（2026-11-04 实测：
    # 超过 `_LIMIT` 的调用块在流式下整段当正文放行，非流式却能还原）。
    _CALL_LIMIT = 1024 * 1024

    def _buffer_limit(self) -> int:
        """当前缓冲区允许多大：普通正文 64 KB，调用块 1 MB。"""
        if self._limit >= self._CALL_LIMIT:
            return self._limit
        head = self._buf
        if (_CONTAINER_OPEN.match(head) or _FLAT_HEAD.match(head)
                or _dsml_at(head, 0) or _marker_at(head, 0)):
            return self._CALL_LIMIT
        return self._limit

    def feed(self, text: str) -> list[tuple[str, Any]]:
        if not text:
            return []
        if not self.enabled:
            return [('content', text)]
        self._buf += text
        out: list[tuple[str, Any]] = []
        while self._buf:
            if len(self._buf) > self._buffer_limit():
                self._content(out, self._consume(len(self._buf)))
                break
            masked = self._masked()
            index = _next_start(masked, 0, self._resolver, final=False)
            if index < 0:
                cut = len(self._buf) - max(
                    _tail_hold(masked, self._resolver), self._fence_hold())
                if cut > 0:
                    self._content(out, self._consume(cut))
                break
            if index > 0:
                self._content(out, self._consume(index))
                continue
            kind, name, arguments, end = _scan_at(self._buf, 0, self._resolver,
                                                  final=False)
            if kind == 'drop':
                # 无名调用块（收尾时才会判成 drop）：整段吞掉，不留标签。
                self._consume(end if end > 0 else 1)
                continue
            if kind == 'need':
                if (_dsml_at(self._buf, 0) or _FLAT_HEAD.match(self._buf)
                        or _CONTAINER_OPEN.match(self._buf)):
                    # DSML / 扁平参数块 / 容器块还没闭合：参数可能很长（写文件、
                    # 跑命令），不能像散文里的半截标签那样 2KB 就放弃；上限交给
                    # `_LIMIT`。
                    break
                give_up = self._give_up_on_tag(masked)
                if not give_up:
                    break
                self._content(out, self._consume(give_up))
                continue
            if kind == 'ok':
                if _placeholder_args(arguments):
                    # 占位符（`{"command": "..."}`）：这是示例，不是调用。
                    self._content(out, self._consume(end))
                    continue
                stop = end
                close = _CLOSE.match(self._buf, end)
                if close:
                    stop = close.end()
                if self._strict_tail:
                    line = _own_line(masked, 0, stop, pending=True,
                                     line_start=self._line_start)
                    if line == 'need':
                        # 块后还没换行：可能后面还接着说明，扣住等下一帧。
                        break
                    if line == 'no':
                        # 同一行还有别的正文 → 夹在句子里的举例，原样放行。
                        self._content(out, self._consume(stop))
                        continue
                # 独占一行 → 立刻下发调用，不必等到收尾。
                self._consume(stop)
                self._flush_pending(out)
                out.append(('tool_calls',
                            [{'name': name, 'arguments': arguments}]))
                continue
            marker = _marker_at(masked, 0)
            if marker:
                close = _CLOSE.match(self._buf, len(marker))
                if close:
                    # 空对：开头后面紧跟闭合标签 → 整段丢掉，不留标签噪音。
                    self._consume(close.end())
                    continue
                tail = _marker_tail(masked, len(marker))
                if tail == 'marker':
                    # 重复的开头（`<tool_call>\n<tool_call>`）：连同中间的空白
                    # 一起吞掉，别把多出来的 marker 当正文发出去。
                    nxt = _next_start(masked, len(marker), self._resolver,
                                      final=False)
                    self._consume(nxt if nxt > 0 else len(marker))
                    continue
                if tail in ('prefix', 'empty'):
                    break               # 还可能是重复开头，等下一帧
                if _attempt_from(self._buf, 0):
                    # 完整但解析不出来的调用（非法 JSON）：先扣住，收尾时按
                    # 退化处理；后面真出现正文再放行。
                    break
            marker = marker or self._buf[0]
            self._content(out, self._consume(len(marker)))

        cleaned = self._clean(out)
        if cleaned:
            self._emitted = True
        return cleaned

    def _fence_hold(self) -> int:
        """缓冲区末尾是不是半个 ```（跨帧切开）：是就扣住。

        围栏状态只记「在不在围栏里」，一旦把半个 ``` 消费掉，那个分隔符就永远
        补不回来了：开口丢失 → 围栏里的示例被当调用执行；闭合丢失 → 后面的
        正文全被当成围栏内容吞掉。扣住 1–2 个反引号，等下一帧凑齐三个再判。
        """
        ticks = len(self._buf) - len(self._buf.rstrip('`'))
        return ticks if 0 < ticks < len(_FENCE) else 0

    def _give_up_on_tag(self, masked: str) -> bool:
        """`need` 是不是「散文里写了个没闭合的标签」——是就放行，别一直扣住。

        线上样本：模型在正文里写「用 `<read>` 这种标签表示读取文件」，上一版
        会把**后面整段流**扣在缓冲区里直到收尾（用户看到的是卡住不动，且每帧
        重扫导致 O(n²)）。这里给一个上限：超过 `_TAG_HOLD_LIMIT` 还没闭合，
        就把这个标签当正文吐出去，继续扫后面的内容。
        """
        if len(self._buf) <= _TAG_HOLD_LIMIT:
            return False
        close = masked.find('>')
        size = close + 1 if close >= 0 else len(self._buf)
        return bool(size)

    def disable(self) -> list[tuple[str, Any]]:
        """上游给了结构化调用：关掉嗅探，缓冲区里的文本按正文放行。

        不能再走 `flush()`——那条路会把残留文本重新解析成调用，而原生通道
        已经给出准的结论了。返回的内容由调用方按**原顺序**补发，避免缓冲区
        里的半截文本排到后面的正文之后。
        """
        self.enabled = False
        text = self._pending + self._consume(len(self._buf))
        self._pending = ''
        text = _strip_tags(text)
        return [('content', text)] if text else []

    def flush(self) -> list[tuple[str, Any]]:
        if not self._buf:
            return []
        inside = self._fence
        start_line = self._line_start
        text = self._consume(len(self._buf))
        if not self.enabled:
            return [('content', text)]
        masked, _ = _mask_spans(text, inside)
        calls, kept, dropped = _parse_calls_detail(
            text, self._resolver, masked=masked,
            strict_tail=self._strict_tail,
            line_start=start_line)
        if (not self._emitted
                and _degenerate_text(text, calls, self._resolver, masked,
                                     dropped=dropped, rest=kept)):
            # 整轮只剩调用脚手架：既没有可执行调用，也没有可读正文。
            # 不把它当正文下发，交给 translate 以错误收尾。
            self.degenerate = True
            return []
        kept = self._pending + kept
        self._pending = ''
        items: list[tuple[str, Any]] = [
            ('tool_calls', [call]) for call in calls]
        if kept.strip():
            items.append(('content', kept))
        cleaned = self._clean(items)
        if cleaned:
            self._emitted = True
        return cleaned

    @staticmethod
    def _clean(out: list[tuple[str, Any]]) -> list[tuple[str, Any]]:
        cleaned: list[tuple[str, Any]] = []
        for kind, payload in out:
            if kind == 'content':
                payload = _strip_tags(payload)
                if not payload:
                    continue
            cleaned.append((kind, payload))
        return cleaned
