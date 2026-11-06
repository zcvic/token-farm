"""Qoder 的账号回填（读内置网关自己的 usage.jsonl）。

为什么单独一个文件：Qoder 与 WorkBuddy 的账号来源完全不同——

  · WorkBuddy 在容器日志里直接写 `昵称(uid)`；
  · Qoder 只能读 `data/platforms/qoder/usage/usage.jsonl` 里的 `account`，
    而且模型名是**上游裸名**，请求日志里却是 `qoder/<模型>`。

这两点任一处理错都不报错：账号列要么一直是空的（模型名对不上），要么被填到
别的平台同名的记录上（没限定平台）。这里把三条性质钉住：

  1. 裸模型名 + `qoder/` 前缀两个候选都要能匹配；
  2. 只在 `platform='qoder'` 的行里匹配，不能串到 WorkBuddy；
  3. 文件不存在 / 半行 JSON 只是少填几行，不抛异常。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db  # noqa: E402
from server.services import qoderaccountlog  # noqa: E402


class EntriesTest(unittest.TestCase):
    def test_parses_account_and_model(self) -> None:
        line = json.dumps({'at': 1767000000.9, 'model': 'Qwen3.8-Max',
                           'account': 'u-xxxx'})
        got = qoderaccountlog._entries([line])
        self.assertEqual(got, [{'ts': 1767000000, 'model': 'Qwen3.8-Max',
                                'account': 'u-xxxx'}])

    def test_skips_broken_or_incomplete_rows(self) -> None:
        lines = [
            '{"at": 1, "model": "m", "account": "u-1"',
            'not json',
            '[]',
            json.dumps({'at': 1, 'model': 'm'}),            # 没有 account
            json.dumps({'at': 1, 'account': 'u-1'}),        # 没有 model
            json.dumps({'model': 'm', 'account': 'u-1'}),   # 没有 at
            json.dumps({'at': True, 'model': 'm', 'account': 'u-1'}),
        ]
        self.assertEqual(qoderaccountlog._entries(lines), [])

    def test_missing_file_is_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'usage.jsonl'
            with mock.patch.object(qoderaccountlog, '_usage_path', return_value=path), \
                    mock.patch.object(qoderaccountlog.db, 'attach_request_accounts') as attach:
                self.assertEqual(qoderaccountlog._collect_once(), 0)
            attach.assert_not_called()
            self.assertEqual(qoderaccountlog._last['error'], '')

    def test_collect_passes_platform_and_model_prefix(self) -> None:
        rows = [
            {'at': 1767000000, 'model': 'Qwen3.8-Max', 'account': 'u-1'},
            {'at': 1767000001, 'model': 'Qwen3.8-Max', 'account': 'u-2'},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'usage.jsonl'
            path.write_text('\n'.join(json.dumps(r) for r in rows) + '\n',
                            encoding='utf-8')
            with mock.patch.object(qoderaccountlog, '_usage_path', return_value=path), \
                    mock.patch.object(qoderaccountlog.db, 'attach_request_accounts',
                                      return_value=1) as attach:
                self.assertEqual(qoderaccountlog._collect_once(), 1)
        attach.assert_called_once()
        entries = attach.call_args.args[0]
        self.assertEqual([e['account'] for e in entries], ['u-1', 'u-2'])
        self.assertEqual(attach.call_args.kwargs,
                         {'platform': 'qoder', 'model_prefix': 'qoder/'})
        self.assertEqual(qoderaccountlog._last['lines'], 2)
        self.assertEqual(qoderaccountlog._last['parsed'], 2)

    def test_status_names_the_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'usage.jsonl'
            with mock.patch.object(qoderaccountlog, '_usage_path', return_value=path):
                info = qoderaccountlog.status()
        self.assertFalse(info['exists'])
        self.assertIn('usage.jsonl', info['hint'])


class AttachByPlatformTest(unittest.TestCase):
    """db.attach_request_accounts 的 Qoder 口径（双候选模型名 + 平台过滤）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls._orig = config.DB_PATH
        config.DB_PATH = Path(cls._tmp.name) / 'qoder-acct.db'
        db._conn = None
        db.connect()

    @classmethod
    def tearDownClass(cls) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = cls._orig
        try:
            cls._tmp.cleanup()
        except PermissionError:
            pass

    def setUp(self) -> None:
        db.execute('DELETE FROM request_logs')
        # 回填用事件台账（issue #381 / ba0ebc23）也按事件去重：不清掉的话，上一个
        # 用例用过的 (ts, model, account) 会被当成「已消费」，本用例填 0 行。
        db.execute('DELETE FROM request_account_matches')

    def _log(self, ts: int, model: str, platform: str) -> int:
        db.add_request_log(ts=ts, model=model, mapped_model=model, status=200,
                           stream=1, latency_ms=1000, platform=platform)
        return db.query_one('SELECT id FROM request_logs ORDER BY id DESC')['id']

    def _account(self, row_id: int) -> str | None:
        return db.query_one('SELECT account FROM request_logs WHERE id = ?',
                            (row_id,))['account']

    def test_matches_prefixed_record_name(self) -> None:
        """账本里是 `Qwen3.8-Max`，日志里是 `qoder/Qwen3.8-Max`。"""
        row = self._log(10_000, 'qoder/Qwen3.8-Max', 'qoder')
        filled = db.attach_request_accounts(
            [{'ts': 10_000, 'model': 'Qwen3.8-Max', 'account': 'u-1'}],
            platform='qoder', model_prefix='qoder/')
        self.assertEqual(filled, 1)
        self.assertEqual(self._account(row), 'u-1')

    def test_platform_filter_does_not_cross_fill(self) -> None:
        """WorkBuddy 的同名模型不能被 Qoder 的账本填上。"""
        qoder_row = self._log(10_000, 'qoder/Qwen3.8-Max', 'qoder')
        wb_row = self._log(10_000, 'Qwen3.8-Max', '')
        filled = db.attach_request_accounts(
            [{'ts': 10_000, 'model': 'Qwen3.8-Max', 'account': 'u-1'}],
            platform='qoder', model_prefix='qoder/')
        self.assertEqual(filled, 1)
        self.assertEqual(self._account(qoder_row), 'u-1')
        self.assertIsNone(self._account(wb_row))

    def test_without_platform_keeps_old_behavior(self) -> None:
        """不传 platform / prefix 时行为与改造前一致（WorkBuddy 仍按裸名匹配）。"""
        row = self._log(10_000, 'Qwen3.8-Max', '')
        filled = db.attach_request_accounts(
            [{'ts': 10_000, 'model': 'Qwen3.8-Max', 'account': 'u-1'}])
        self.assertEqual(filled, 1)
        self.assertEqual(self._account(row), 'u-1')


if __name__ == '__main__':
    unittest.main()
