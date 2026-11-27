"""登录态失效标记（issue #490）。

判定要克制：只认**明确的 401**、连续两次才置位、非 401 的临时失败不动标记、
查询成功或重登成功立即清除。列表侧把它变成布尔字段 `needs_relogin` 并把积分
快照置空 —— 旧快照会让用户以为账号还能用。

## 现场

账号登录态被上游作废后 billing 返回 401，面板拿不到实时积分，只能回退展示
`/status` 里的旧快照。前端原先只在「未加载」分支用 `invalid_reason`，于是池内
账号显示成「在线 + 积分空白」，tooltip 还写「上游不可达」——把「该重登」说成
了「上游坏了」。
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db  # noqa: E402
from server.services import credits, wb2api  # noqa: E402


class SessionDeadTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        self._orig_users = config.USERS_FILE
        config.DB_PATH = Path(self._tmp.name) / 's.db'
        config.USERS_FILE = Path(self._tmp.name) / 'users.json'
        db._conn = None
        db.connect()
        credits._cache.clear()

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db
        config.USERS_FILE = self._orig_users
        credits._cache.clear()
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def test_single_401_does_not_mark(self) -> None:
        """一次 401 不置位：上游纯 401 可能是网关抖动或 token 轮换竞态。"""
        credits.note_session_failure('u1')
        self.assertEqual(set(), credits.session_dead_uids())

    def test_two_consecutive_401_marks(self) -> None:
        credits.note_session_failure('u1')
        credits.note_session_failure('u1')
        self.assertIn('u1', credits.session_dead_uids())

    def test_success_clears(self) -> None:
        credits.note_session_failure('u1')
        credits.note_session_failure('u1')
        credits.clear_session_dead('u1')
        self.assertNotIn('u1', credits.session_dead_uids())

    def test_ttl_expires(self) -> None:
        """超过 7 天的标记自动失效，不再提示重登。"""
        marks = {'u1': {'n': 2, 'at': time.time() - credits._SESSION_DEAD_TTL - 10}}
        credits._save_session_dead(marks)
        self.assertNotIn('u1', credits.session_dead_uids())

    def test_legacy_timestamp_shape_is_honoured(self) -> None:
        """兼容旧的「uid -> 时间戳」形态：当作已置位。"""
        credits._save_session_dead({'u1': time.time()})
        self.assertIn('u1', credits.session_dead_uids())

    def test_only_explicit_401_counts(self) -> None:
        """口径：只认 `查询失败 code=401`；`查询异常` 与代理类文案不算。"""
        self.assertTrue(credits._is_upstream_401('查询失败 code=401'))
        self.assertFalse(credits._is_upstream_401('查询失败 code=4010'))
        self.assertFalse(credits._is_upstream_401('查询异常: proxy auth failed'))
        self.assertFalse(credits._is_upstream_401('查询失败 code=401 · 代理不可用'))
        self.assertFalse(credits._is_upstream_401(''))
        self.assertFalse(credits._is_upstream_401('查询失败 code=500'))

    def test_merge_pool_status_exposes_flag_and_blanks_credits(self) -> None:
        credits.note_session_failure('u1')
        credits.note_session_failure('u1')
        wb2api._SESSION_DEAD_CACHE['at'] = 0.0
        accounts = [{'uid': 'u1', 'file': 'a.json'}, {'uid': 'u2', 'file': 'b.json'}]
        status = {'accounts': [{'uid': 'u1', 'credits': 999},
                               {'uid': 'u2', 'credits': 42}]}
        merged = wb2api.merge_pool_status(accounts, status)
        by_uid = {a['uid']: a for a in merged}
        self.assertTrue(by_uid['u1']['needs_relogin'])
        self.assertIsNone(by_uid['u1']['credits'],
                          '登录态失效时上游那份 credits 是旧快照，必须置空')
        self.assertIn('需重登', by_uid['u1']['invalid_reason'])
        self.assertFalse(by_uid['u2']['needs_relogin'])
        self.assertEqual(42, by_uid['u2']['credits'])

    def test_forget_clears_flag(self) -> None:
        credits.note_session_failure('u1')
        credits.note_session_failure('u1')
        credits.forget('u1')
        self.assertNotIn('u1', credits.session_dead_uids())


if __name__ == '__main__':
    unittest.main()
