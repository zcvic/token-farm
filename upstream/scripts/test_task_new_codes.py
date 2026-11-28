"""上游 0061（issue #429）在本仓的结构性回归：动态专家解析与明确提示。

本仓的 expert_use_* 走既有的 expertx 链（EXPERT_EXPERIENCE 静态表），
这里只覆盖上游新增的两件事：
  - 未登记 slug 从任务 jump_url 动态解析专家 id，解析不到明确 unsupported；
  - 「不认识 / 需人工 / 需抓包 / 服务端未下发」四类措辞不再混成「非映射任务」。

运行：cd upstream/scripts && python -S -m unittest test_task_new_codes -v
"""
import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace

import task_runner as runner


class ResolveTaskSpecTest(unittest.TestCase):
    def test_known_expert_campaign_codes_map_to_expertx(self):
        expected = {
            "expert_use_maidehao": "ex_GDl31WahyIH0",
            "expert_use_douyinyin": "ex_FbY5nlWTqDod",
            "expert_use_shangjia_xuanchuan": "ex_Weo3x2EZuSDm",
            "expert_use_bowangwang": "ex_WZY136wV5aMt",
        }
        for code, eid in expected.items():
            with self.subTest(code=code):
                spec = runner.resolve_task_spec(code)
                self.assertEqual(spec["kind"], "expertx")
                self.assertEqual(spec["expert"][0], eid)
                self.assertFalse(spec.get("unsupported"))

    def test_unknown_expert_slug_uses_jump_url_expert_id(self):
        task = {"jump_url": "workbuddy://experts?expertId=ex_FbY5nlWTqDod"}
        spec = runner.resolve_task_spec("expert_use_douyinjin", task)
        self.assertEqual(spec["kind"], "expertx")
        self.assertEqual(spec["expert"][0], "ex_FbY5nlWTqDod")

    def test_unknown_expert_slug_without_id_is_explicit_unsupported(self):
        code = "expert_use_future_slug"
        spec = runner.resolve_task_spec(code)
        self.assertTrue(spec["unsupported"])
        self.assertEqual(spec["reason_kind"], "capture")
        notice = runner.describe_task_code(code, spec)
        self.assertIn("unsupported task code", notice)
        self.assertIn("抓包", notice)

    def test_legacy_codes_unchanged(self):
        self.assertEqual(runner.resolve_task_spec("create_canvas")["kind"], "canvas")
        self.assertEqual(runner.resolve_task_spec("chat_5")["kind"], "chat")
        self.assertEqual(runner.resolve_task_spec("chat_5")["target"], 5)
        self.assertEqual(runner.resolve_task_spec("expert_5")["kind"], "expert")
        self.assertEqual(runner.resolve_task_spec("Expert_lighthouse")["kind"], "lighthouse")
        # 开学季的 expert_use 是 school 域，不能被 expert_use_ 前缀家族误吞
        self.assertEqual(runner.resolve_task_spec("expert_use")["kind"], "school")
        self.assertTrue(runner.resolve_task_spec("Expert_Philanthropy")["unforgeable"])
        # 本仓已实现的活动专家任务不能被上游的「需抓包」名单盖掉
        self.assertEqual(runner.resolve_task_spec("WorkBuddy_Valorant")["kind"], "expertx")

    def test_unknown_code_is_unknown_not_unsupported(self):
        self.assertIsNone(runner.resolve_task_spec("totally_new_task"))
        notice = runner.describe_task_code("totally_new_task", None)
        self.assertIn("unknown task code", notice)
        self.assertIn("不认识", notice)
        self.assertNotIn("unsupported", notice)

    def test_manual_codes_are_explicit(self):
        for code in ("task_student_verify",):
            with self.subTest(code=code):
                notice = runner.describe_task_code(code, runner.resolve_task_spec(code))
                self.assertIn("unsupported task code", notice)
                self.assertIn("需人工", notice)
        # 本仓已实现的 claimonly / expertx 任务不能被「需人工」名单盖掉
        self.assertEqual(runner.resolve_task_spec("wb_wechat_oa_subscribe_task")["kind"],
                         "claimonly")

    def test_missing_known_task_says_not_issued(self):
        notice = runner.describe_missing_task(
            "create_canvas", runner.resolve_task_spec("create_canvas"))
        self.assertIn("服务端未下发", notice)
        self.assertNotIn("unknown task code", notice)


class JumpUrlTest(unittest.TestCase):
    def test_direct_deeplink(self):
        self.assertEqual(
            runner._expert_id_from_jump_url("workbuddy://experts?expertId=ex_GDl31WahyIH0"),
            "ex_GDl31WahyIH0")

    def test_trusted_http_wrapper(self):
        url = ("https://www.workbuddy.cn/launch/?deeplink="
               "workbuddy%3A%2F%2Fexperts%3FexpertId%3Dex_GDl31WahyIH0")
        self.assertEqual(runner._expert_id_from_jump_url(url), "ex_GDl31WahyIH0")

    def test_untrusted_host_rejected(self):
        url = ("https://evil.example/launch/?deeplink="
               "workbuddy%3A%2F%2Fexperts%3FexpertId%3Dex_GDl31WahyIH0")
        self.assertEqual(runner._expert_id_from_jump_url(url), "")

    def test_malformed_expert_id_rejected(self):
        self.assertEqual(
            runner._expert_id_from_jump_url("workbuddy://experts?expertId=ContentCreator"), "")


class ProcessTaskMessageTest(unittest.TestCase):
    def _run(self, code):
        stats = {"total": 0, "skip": 0, "pending": 0, "already": 0, "fail": 0, "ok": 0}
        opts = SimpleNamespace(only_claim=False, yes=False, gap=1.0)
        buf = io.StringIO()
        with redirect_stdout(buf):
            runner.process_task(
                {"uid": "u12345678"}, code,
                {"accept_status": "not_accepted",
                 "progress": {"current": 0, "target": 1}},
                opts, stats)
        return buf.getvalue(), stats

    def test_unknown_code_prints_unknown(self):
        out, stats = self._run("totally_new_task")
        self.assertIn("unknown task code: totally_new_task", out)
        self.assertEqual(stats["skip"], 1)

    def test_manual_code_prints_unsupported(self):
        out, stats = self._run("task_student_verify")
        self.assertIn("unsupported task code: task_student_verify", out)
        self.assertIn("需人工", out)
        self.assertEqual(stats["skip"], 1)


if __name__ == "__main__":
    unittest.main()
