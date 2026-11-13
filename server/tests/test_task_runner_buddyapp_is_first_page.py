"""`buddyapp_enter_click` 的 `isFirstPage` 必须是布尔（上游格式校验）。

背景：服务端把 `/v2/report` 的格式校验收紧后，`isFirstPage` 传字符串 `"1"` 的整条
事件会被**丢弃**，而响应仍是 2xx，只在 body 里说明：

    {"code":10001,"msg":"partial success: kept=4 events, dropped=1 events by format validation",
     "data":{"dropped":1,"invalidIndices":[2],"kept":4}}

`invalidIndices=[2]` 正是五连里的第三条（discover → show → enter_click）。任务判据
吃不到这条，`Buddy_App` / `Buddy_App_QQ` 就永远停在 0/1，日志里只有一行 WARN，
看起来像「上报过了但没点亮」。

这里直接加载真实的 `upstream/scripts/task_runner.py`（不复制事件构造），钉住两处
`isFirstPage` 的类型：改回字符串即红。
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCRIPTS = _ROOT / 'upstream' / 'scripts'


def _load_task_runner():
    """加载 upstream/scripts/task_runner.py（模块名唯一，避免与其他测试串味）。"""
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        'task_runner_isfirstpage_probe', _SCRIPTS / 'task_runner.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class BuddyAppIsFirstPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tr = _load_task_runner()

    def _enter_event(self, seq):
        for ev in seq:
            if ev.get('eventCode') == 'buddyapp_enter_click':
                return ev
        return None

    def test_sequence_enter_click_is_first_page_is_bool(self) -> None:
        """五连里的 enter_click：isFirstPage 必须是 bool，字符串会被整条丢弃。"""
        seq = self.tr.desktop_buddy5_sequence({'uid': 'u1'}, 'cb_x', '测试 Buddy')
        self.assertEqual(len(seq), 5, '五连事件应为 5 条')
        enter = self._enter_event(seq)
        self.assertIsNotNone(enter, '事件链里没有 buddyapp_enter_click')
        self.assertIn('isFirstPage', enter, 'buddyapp_enter_click 缺少 isFirstPage')
        value = enter['isFirstPage']
        self.assertIsInstance(
            value, bool,
            f'isFirstPage 必须是 bool，实际 {type(value).__name__}({value!r})：'
            '字符串会被上游格式校验丢弃，响应仍 2xx，任务永远 0/1')

    def test_sequence_true_means_first_page(self) -> None:
        """五连上报的是「进入应用」首屏，语义上是 True（不只是「是布尔」）。"""
        enter = self._enter_event(
            self.tr.desktop_buddy5_sequence({'uid': 'u1'}, 'cb_x', 'B'))
        self.assertIs(enter['isFirstPage'], True)

    def test_fallback_single_event_is_first_page_is_bool(self) -> None:
        """降级单发事件同样必须是布尔——否则兜底路径一样点不亮。"""
        single = self.tr.desktop_buddy_enter_single('cb_x', 'B', 'u1', 1700000000000)
        self.assertEqual(single['eventCode'], 'buddyapp_enter_click')
        self.assertIn('isFirstPage', single)
        value = single['isFirstPage']
        self.assertIsInstance(
            value, bool,
            f'降级单发事件的 isFirstPage 必须是 bool，实际 {type(value).__name__}({value!r})')
        self.assertIs(value, False, '降级单发是「不在首屏」的场景，应为 False')
        self.assertEqual(single['userId'], 'u1', '降级单发必须带 userId（report 必填）')


if __name__ == '__main__':
    unittest.main()
