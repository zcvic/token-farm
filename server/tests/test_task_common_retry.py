"""任务脚本 HTTP 层的 429 / 5xx 有界重试。

背景（真实故障）：一个账号的任务循环会把任务列表接口打十几二十次（每个任务都要
回读一次进度）。上游频控一触发，第 5 个任务之后全是 `list_tasks http=429`，而当时
的实现是「一次失败就抛」，于是 `process_account` 的整个任务循环被一个 429 带走 ——
前面已经点亮但还没领奖的任务全白跑，后面的任务一项都没碰。

两层修法，这里各钉一条：
  1. `task_common._request` 对 429 / GET 的 5xx 做有界重试（最多 3 次，认
     `Retry-After` 秒数，要求等待超过上限就不干等而是把 429 交回调用方）。
  2. `task_runner.process_account` 的单个任务失败不再中断整批（记 fail 继续）。

为什么 POST 的 5xx 不重试：写请求可能已经在服务端落账，重发会留下重复上报；
429 是「没被处理」，重试安全，所以 POST 的 429 仍然重试。
"""
from __future__ import annotations

import importlib.util
import io
import pathlib
import sys
import types
import unittest
import urllib.error
from unittest import mock

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCRIPTS = _ROOT / 'upstream' / 'scripts'

AUTH = {'uid': 'u1', 'token': 'at', 'nick': 'n'}


def _load(module: str, probe: str):
    """加载真实的 upstream 脚本（模块名唯一，避免与其他测试串味）。"""
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    spec = importlib.util.spec_from_file_location(probe, _SCRIPTS / f'{module}.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _http_error(code: int, retry_after=None):
    headers = {}
    if retry_after is not None:
        headers['Retry-After'] = str(retry_after)
    return urllib.error.HTTPError(
        'https://copilot.tencent.com/x', code, 'boom', headers, io.BytesIO(b'{"msg":"x"}'))


class _Resp:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class RequestRetryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tc = _load('task_common', 'task_common_retry_probe')

    def test_429_retry_after_is_honoured(self) -> None:
        """429 + Retry-After: 0 → 立刻重试，第二次成功。"""
        calls = []

        def fake(req, timeout=0):
            calls.append(req)
            if len(calls) == 1:
                raise _http_error(429, retry_after=0)
            return _Resp(200, b'{"code":0,"data":{"tasks":[]}}')

        with mock.patch.object(self.tc.urllib.request, 'urlopen', side_effect=fake), \
                mock.patch.object(self.tc.time, 'sleep') as sleep:
            st, r = self.tc.do_get(AUTH, 'https://copilot.tencent.com',
                                   self.tc.PATH_LIST_TASKS)
        self.assertEqual(st, 200)
        self.assertEqual(len(calls), 2)
        sleep.assert_called()  # 走的是 sleep(0)，不是没等

    def test_long_retry_after_is_not_awaited(self) -> None:
        """上游要求冷却 120s：不在脚本里干等（面板会按空闲判卡死），直接回 429。"""
        with mock.patch.object(self.tc.urllib.request, 'urlopen',
                               side_effect=_http_error(429, retry_after=120)) as open_, \
                mock.patch.object(self.tc.time, 'sleep') as sleep:
            st, _ = self.tc.do_get(AUTH, 'https://copilot.tencent.com',
                                   self.tc.PATH_LIST_TASKS)
        self.assertEqual(st, 429)
        self.assertEqual(open_.call_count, 1)
        sleep.assert_not_called()

    def test_get_5xx_is_retried_but_post_is_not(self) -> None:
        """GET 的 5xx 是瞬时问题；POST 可能已落账，不能重发。"""
        with mock.patch.object(self.tc.urllib.request, 'urlopen',
                               side_effect=_http_error(503)) as open_, \
                mock.patch.object(self.tc.time, 'sleep'):
            st, _ = self.tc.do_get(AUTH, 'https://copilot.tencent.com', '/x')
        self.assertEqual(st, 503)
        self.assertEqual(open_.call_count, self.tc.RETRY_ATTEMPTS)

        with mock.patch.object(self.tc.urllib.request, 'urlopen',
                               side_effect=_http_error(503)) as open2, \
                mock.patch.object(self.tc.time, 'sleep'):
            st, _ = self.tc.do_post(AUTH, 'https://copilot.tencent.com', '/x', {})
        self.assertEqual(st, 503)
        self.assertEqual(open2.call_count, 1)

    def test_429_on_post_is_retried(self) -> None:
        """429 表示没被处理，写请求重试是安全的（claim 本身幂等）。"""
        calls = []

        def fake(req, timeout=0):
            calls.append(req)
            if len(calls) == 1:
                raise _http_error(429, retry_after=0)
            return _Resp(200, b'{"code":0}')

        with mock.patch.object(self.tc.urllib.request, 'urlopen', side_effect=fake), \
                mock.patch.object(self.tc.time, 'sleep'):
            st, _ = self.tc.do_post(AUTH, 'https://copilot.tencent.com', '/x', {})
        self.assertEqual(st, 200)
        self.assertEqual(len(calls), 2)

    def test_persistent_429_still_raises_from_list_tasks(self) -> None:
        """重试耗尽后仍要抛：调用方（单任务 / mp 段）自己决定跳过，不能吞成空列表。"""
        with mock.patch.object(self.tc.urllib.request, 'urlopen',
                               side_effect=_http_error(429, retry_after=0)), \
                mock.patch.object(self.tc.time, 'sleep'):
            with self.assertRaises(RuntimeError) as ctx:
                self.tc.list_tasks(AUTH)
        self.assertIn('429', str(ctx.exception))


class ProcessAccountSurvivesOneTaskFailureTest(unittest.TestCase):
    """一个任务回读撞 429 不该带走整批：后面还能做，已点亮还能领。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tr = _load('task_runner', 'task_runner_retry_probe')

    def test_task_failure_is_counted_and_loop_continues(self) -> None:
        tasks = [{'task_code': 'skill_1', 'accept_status': 'not_accepted'},
                 {'task_code': 'chat_5', 'accept_status': 'not_accepted'}]
        opts = types.SimpleNamespace(yes=True, gap=0, only_claim=False, only_codes=None)
        stats: dict = {k: 0 for k in
                       ('accounts', 'total', 'ok', 'already', 'skip', 'pending',
                        'fail', 'credit', 'energy')}
        ran: list[str] = []

        def fake_task(_auth, code, _t, _o, _s):
            ran.append(code)
            if code == 'skill_1':
                raise RuntimeError('list_tasks http=429')

        with mock.patch.object(self.tr.tc, 'do_get', return_value=(200, {'code': 0})), \
                mock.patch.object(self.tr.tc, 'list_tasks', return_value=tasks), \
                mock.patch.object(self.tr, 'process_task', side_effect=fake_task), \
                mock.patch.object(self.tr, 'process_school_tasks'), \
                mock.patch.object(self.tr, '_mp_task_status', return_value=None):
            self.tr.process_account(AUTH, opts, stats)

        self.assertEqual(ran, ['skill_1', 'chat_5'], '一个任务 429 就停了整批')
        self.assertEqual(stats['fail'], 1)


class AccountFailureDoesNotStopBatchTest(unittest.TestCase):
    """一个账号的意外异常不该让后面的账号一个都不跑。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tr = _load('task_runner', 'task_runner_batch_probe')

    def test_next_account_still_runs(self) -> None:
        args = types.SimpleNamespace(yes=False, gap=1.0, only_claim=False,
                                     only_codes=None, accounts=['aaa', 'bbb'])
        seen: list[str] = []

        def fake_process(auth, _a, _s):
            seen.append(auth['uid'])
            if auth['uid'] == 'aaa':
                raise RuntimeError('list_tasks http=429')

        fake_parser = mock.MagicMock()
        fake_parser.parse_args.return_value = args
        with mock.patch.object(self.tr.argparse, 'ArgumentParser',
                               return_value=fake_parser), \
                mock.patch.object(self.tr.tc, 'add_account_spacing_arguments'), \
                mock.patch.object(self.tr.tc, 'account_spacing_from_args',
                                  return_value=types.SimpleNamespace(wait=lambda: None)), \
                mock.patch.object(self.tr.tc, 'load_auth',
                                  side_effect=lambda p: {'uid': p, 'nick': p, 'token': 't'}), \
                mock.patch.object(self.tr.tc, 'auth_is_global', return_value=False), \
                mock.patch.object(self.tr, 'process_account', side_effect=fake_process), \
                mock.patch.object(self.tr, 'print_summary'):
            self.tr.main()

        self.assertEqual(seen, ['aaa', 'bbb'], '第一个账号异常就停了整批')


class SchoolRequestRetryTest(unittest.TestCase):
    """school 段（开学季）的 429 也走退避，而不是一次就放弃。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.school = _load('school_open_day_2026', 'school_retry_probe')

    def test_429_is_retried(self) -> None:
        with mock.patch.object(self.school, '_request',
                               side_effect=[(429, {'code': 429}), (200, {'code': 0})]) as req, \
                mock.patch.object(self.school.time, 'sleep') as sleep:
            st, r = self.school.request_retry('tk', 'GET', 'https://x/y')
        self.assertEqual(st, 200)
        self.assertEqual(req.call_count, 2)
        sleep.assert_called()

    def test_429_exhausted_returns_last_response(self) -> None:
        with mock.patch.object(self.school, '_request',
                               return_value=(429, {'code': 429})), \
                mock.patch.object(self.school.time, 'sleep'):
            st, _ = self.school.request_retry('tk', 'GET', 'https://x/y')
        self.assertEqual(st, 429)


class MpListFetchedOncePerAccountTest(unittest.TestCase):
    """一个账号的 mp 查询只拉一次列表（429 的主因就是每个 code 各拉一次）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tr = _load('task_runner', 'task_runner_mp_once_probe')

    def test_one_mp_list_call_for_many_missing_codes(self) -> None:
        opts = types.SimpleNamespace(yes=False, gap=0, only_claim=False, only_codes=None)
        stats: dict = {k: 0 for k in
                       ('accounts', 'total', 'ok', 'already', 'skip', 'pending',
                        'fail', 'credit', 'energy')}
        with mock.patch.object(self.tr.tc, 'do_get', return_value=(200, {'code': 0})), \
                mock.patch.object(self.tr.tc, 'list_tasks', return_value=[]), \
                mock.patch.object(self.tr, '_mp_list_tasks', return_value=[]) as mp, \
                mock.patch.object(self.tr, 'process_task'), \
                mock.patch.object(self.tr, 'process_school_tasks'):
            self.tr.process_account(AUTH, opts, stats)
        # growth 回落一次 + mp 专段一次 —— 两个口径各一份快照，而不是每个 code 一次
        self.assertEqual(mp.call_count, 1, f'mp 列表被拉了 {mp.call_count} 次')

    def test_mp_list_failure_is_tried_once_and_counted_once(self) -> None:
        """列表整体 429 时不该每条 code 再撞一次，也不该谎报「任务不存在」。"""
        opts = types.SimpleNamespace(yes=False, gap=0, only_claim=False, only_codes=None)
        stats: dict = {k: 0 for k in
                       ('accounts', 'total', 'ok', 'already', 'skip', 'pending',
                        'fail', 'credit', 'energy')}
        with mock.patch.object(self.tr.tc, 'do_get', return_value=(200, {'code': 0})), \
                mock.patch.object(self.tr.tc, 'list_tasks', return_value=[]), \
                mock.patch.object(self.tr, '_mp_list_tasks',
                                  side_effect=RuntimeError('mp list_tasks http=429')) as mp, \
                mock.patch.object(self.tr, 'process_task'), \
                mock.patch.object(self.tr, 'process_school_tasks'):
            self.tr.process_account(AUTH, opts, stats)
        self.assertEqual(mp.call_count, 1, '列表失败后又逐条重试了')
        # mp 专段只记一次 fail；growth 回落逐条记 pending（没查清 ≠ 没有）
        self.assertEqual(stats['fail'], 1, stats)
        self.assertGreater(stats['pending'], 0, stats)
        self.assertEqual(stats['skip'], 0, stats)


if __name__ == '__main__':
    unittest.main()
