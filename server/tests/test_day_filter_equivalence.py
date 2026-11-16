"""按天过滤的两套写法必须**逐行等价**（否则索引优化会静默改掉统计口径）。

背景：统计接口里有几处「只按天过滤」的查询写成了

    WHERE strftime('%Y-%m-%d', ts, 'unixepoch', 'localtime') >= '2026-09-15'

这种把 `ts` 包在函数里的写法让 `ts` 上的索引失效 —— 实测 5 万行时查询计划是
`SCAN request_logs`（全表扫描），一次 13.8ms；而换成等价的

    WHERE ts >= <2026-09-15 的本地零点时间戳>

就能走 `idx_logs_ts`（`SEARCH ... USING INDEX`），6.0ms，且数据越多差距越大。
总览页 30 秒轮询一次、账号多时更明显，所以值得优化。

**但这个优化会静默改错数据**：如果两套写法的边界差哪怕一秒，统计数字就会漂移，
而界面上完全看不出来（数字只是略有不同）。所以本文件用边界样本逐行比对，
把「等价」这件事钉死。

这里刻意不用「构造一个大表统计条数」来断言（那种断言在差一秒时仍可能碰巧相等），
而是**把边界附近的每一行都取出来逐条比**。
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db  # noqa: E402
from server.routers import stats  # noqa: E402


class DayFilterEquivalenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'eq.db'
        db._conn = None
        db.connect()
        self._day = '2026-09-15'
        self._midnight = db.day_start_ts(self._day)
        # 边界样本：目标日的前一天零点 / 当天零点前一秒 / 当天零点 / 正午 /
        # 当天最后一秒 / 次日零点 / 次日一点
        self._samples = [
            self._midnight - 86400,        # 前一天零点
            self._midnight - 1,            # 当天零点**前一秒**（关键边界）
            self._midnight,                # 当天零点
            self._midnight + 1,
            self._midnight + 43200,        # 当天正午
            self._midnight + 86399,        # 当天**最后一秒**
            self._midnight + 86400,        # 次日零点
            self._midnight + 90000,
        ]
        for i, ts in enumerate(self._samples):
            db.execute(
                'INSERT INTO request_logs(ts,key_id,ip,model,mapped_model,status,'
                'prompt_tokens,completion_tokens,latency_ms,ua,error,stream,credit,realm) '
                'VALUES(?,1,?,?,?,200,1,1,1,?,NULL,1,NULL,?)',
                (ts, '1.1.1.1', 'm', 'm', 'UA', 'cn'),
            )

    def tearDown(self) -> None:
        try:
            if db._conn is not None:
                db._conn.close()
        except Exception:  # noqa: BLE001
            pass
        db._conn = None
        config.DB_PATH = self._orig
        self._tmp.cleanup()

    def _rows_expr(self, day: str) -> set[int]:
        """旧写法：day_sql 表达式比较。"""
        return {r['ts'] for r in db.query(
            f"SELECT ts FROM request_logs WHERE {db.day_sql('ts')} >= ?", (day,))}

    def _rows_range(self, day: str) -> set[int]:
        """新写法：换算成时间戳范围。"""
        return {r['ts'] for r in db.query(
            'SELECT ts FROM request_logs WHERE ts >= ?', (db.day_start_ts(day),))}

    def test_ge_filter_is_row_equivalent(self) -> None:
        """`day_sql(ts) >= X` 与 `ts >= 当地零点` 必须逐行相同。"""
        old = self._rows_expr(self._day)
        new = self._rows_range(self._day)
        self.assertEqual(
            old, new,
            f'两套写法结果不同！仅旧有新无={sorted(old - new)} 仅新有旧无={sorted(new - old)}',
        )
        # 顺带确认边界语义：当天零点算在内、前一秒不算
        self.assertIn(self._midnight, old, '当天零点应被包含')
        self.assertNotIn(self._midnight - 1, old, '当天零点前一秒不该被包含')
        # 两套写法都**只有下界**（没有上界）：次日零点同样被包含
        self.assertIn(self._midnight + 86400, old, '两套写法都无上界，次日零点应被包含')

    def test_eq_filter_is_row_equivalent(self) -> None:
        """`day_sql(ts) = X` 与「ts 落在 [X零点, X+1零点) 」必须逐行相同。

        `_usage_health` 用的是 `=` 形态，所以这里同时给出它需要的**上下界**
        写法（等值必须有上界，不能只给下界）。
        """
        old = {r['ts'] for r in db.query(
            f"SELECT ts FROM request_logs WHERE {db.day_sql('ts')} = ?", (self._day,))}
        new = {r['ts'] for r in db.query(
            'SELECT ts FROM request_logs WHERE ts >= ? AND ts < ?',
            (self._midnight, self._midnight + 86400))}
        self.assertEqual(old, new, f'等值过滤不等价：old={sorted(old)} new={sorted(new)}')
        self.assertIn(self._midnight, old)
        self.assertIn(self._midnight + 86399, old)
        self.assertNotIn(self._midnight - 1, old)
        self.assertNotIn(self._midnight + 86400, old, '次日零点不属于这一天')

    def test_range_filter_uses_index(self) -> None:
        """新写法必须真的走索引（否则这次优化毫无意义）。

        断言查询计划里出现 INDEX —— 这是「优化生效」的**唯一**可证方式，
        光看耗时会在小表上碰巧通过。
        """
        plan = db.query(
            f"EXPLAIN QUERY PLAN SELECT COUNT(*) FROM request_logs WHERE ts >= ?",
            (self._midnight,),
        )
        detail = ' '.join(str(r['detail']) for r in plan)
        # 判据是 **SEARCH**（按范围定位）而不是「用没用索引」——
        # 实测旧写法是 `SCAN request_logs USING COVERING INDEX idx_logs_ts`：
        # 它也在"用索引"，但方式是**从头扫整个索引**，命中多少都要过一遍。
        # 范围写法才是 `SEARCH ... USING COVERING INDEX idx_logs_ts (ts>?)`，
        # 直接定位到区间。性能差距就在 SEARCH 与 SCAN 之间，故断言 SEARCH。
        self.assertIn('SEARCH', detail.upper(),
                      f'范围写法没有按范围搜索（仍是扫描）：{detail}')

    def test_expression_filter_does_not_use_index(self) -> None:
        """反证：旧的表达式写法**确实**不走索引。

        若哪天 SQLite 能对 strftime 建表达式索引（或这个断言失效），
        说明前提变了，这次优化可以重新评估 —— 所以要钉住。
        """
        plan = db.query(
            f"EXPLAIN QUERY PLAN SELECT COUNT(*) FROM request_logs "
            f"WHERE {db.day_sql('ts')} >= ?", (self._day,),
        )
        detail = ' '.join(str(r['detail']) for r in plan)
        # 旧写法必然是 SCAN（扫整个索引 / 全表）——这正是本次优化的依据。
        # 若哪天它变成 SEARCH，说明前提变了，可以重新评估这次改动。
        self.assertIn('SCAN', detail.upper(),
                      f'旧写法竟然不再扫描了，前提已变：{detail}')

    def test_day_start_ts_matches_day_sql_semantics(self) -> None:
        """`day_start_ts` 与该日的 `day_sql` 归类必须一致（多种日期）。"""
        for day in ('2026-01-01', '2026-06-15', '2026-12-31', '2026-09-15'):
            with self.subTest(day=day):
                ts = db.day_start_ts(day)
                got = db.query_one(
                    f"SELECT {db.day_sql('ts')} AS d FROM (SELECT ? AS ts)", (ts,))['d']
                self.assertEqual(got, day, f'{day} 的零点被 day_sql 归到了 {got}')
                # 前一秒应归到前一天
                prev = db.query_one(
                    f"SELECT {db.day_sql('ts')} AS d FROM (SELECT ? AS ts)", (ts - 1,))['d']
                self.assertNotEqual(prev, day, f'{day} 的前一秒不该归到当天')

    def test_day_start_ts_is_local_not_utc(self) -> None:
        """零点必须按**本地**时区算（与 day_sql 的 localtime 同口径）。

        若误用 UTC，UTC+8 机器上会差 8 小时 —— 凌晨的调用会被算到前一天。
        这里用「该零点再格式化成日期字符串应等于原日期」来验证（跨时区都成立）。
        """
        day = db.day_of()
        ts = db.day_start_ts(day)
        self.assertEqual(time.strftime('%Y-%m-%d', time.localtime(ts)), day)
        # 再加一秒仍应落在同一天（证明它是当天最早的时刻，而不是别处的零点）
        self.assertEqual(time.strftime('%Y-%m-%d', time.localtime(ts + 1)), day)
        self.assertNotEqual(time.strftime('%Y-%m-%d', time.localtime(ts - 1)), day)


if __name__ == '__main__':
    unittest.main()


class UsageHealthUpperBoundTest(unittest.TestCase):
    """`_usage_health` 的当日统计**必须有上界**（否则会把未来算进今天）。

    这条单独写是因为反证时发现：等价性测试只验证了两套 SQL 写法的等价，
    **没有**验证 `_usage_health` 真的用了半开区间。于是「去掉上界」这个改动
    一路全绿 —— 而它的后果比慢严重得多：`ts >= 今天零点` 会把明天、下月、
    明年的日志全算成「今天的调用」，`usage_health` 据此判断「统计没在累计」
    时会给出完全错误的结论。

    单靠 SQL 写法的等价性测试抓不到这类「实现没用对写法」的问题，所以这里
    直接对函数行为下断言。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'ub.db'
        db._conn = None
        db.connect()
        db.execute("INSERT INTO api_keys(name,key_hash,prefix,enabled,created_at,"
                   "quota,used_tokens) VALUES('k','h','wbk_u',1,?,0,0)",
                   (int(time.time()),))
        self.kid = db.query_one('SELECT id FROM api_keys LIMIT 1')['id']
        self.today = db.day_of()
        self.t0 = db.day_start_ts(self.today)

    def tearDown(self) -> None:
        try:
            if db._conn is not None:
                db._conn.close()
        except Exception:  # noqa: BLE001
            pass
        db._conn = None
        config.DB_PATH = self._orig
        self._tmp.cleanup()

    def _add(self, ts: int) -> None:
        db.add_request_log(ts=ts, key_id=self.kid, ip='1.1.1.1', model='m',
                           mapped_model='m', status=200, prompt_tokens=5,
                           completion_tokens=5, latency_ms=1, first_token_ms=None,
                           ua='UA', error=None, stream=1, credit=None, realm='cn')

    def test_tomorrow_is_not_counted_as_today(self) -> None:
        """明天的调用**不能**算进「今天」——这是上界的意义。"""
        self._add(self.t0 + 100)         # 今天
        self._add(self.t0 + 86400)       # 明天零点
        self._add(self.t0 + 86400 + 60)  # 明天
        got = stats._usage_health(self.today, 0, None)['logs_today']
        self.assertEqual(got, 1, f'上界缺失：把明天的日志也算成今天了（得到 {got}）')

    def test_yesterday_is_not_counted_as_today(self) -> None:
        """昨天的也不算（下界）。"""
        self._add(self.t0 - 1)
        self._add(self.t0)
        got = stats._usage_health(self.today, 0, None)['logs_today']
        self.assertEqual(got, 1, f'下界有误（得到 {got}）')

    def test_boundary_second(self) -> None:
        """边界一秒：当天零点算今天、前一秒不算。"""
        self._add(self.t0)
        self._add(self.t0 - 1)
        self.assertEqual(stats._usage_health(self.today, 0, None)['logs_today'], 1)

    def test_zero_token_failure_still_excluded(self) -> None:
        """0 token 且无扣费的请求不算「本该累计」（口径与 bump_usage 对齐）。"""
        db.add_request_log(ts=self.t0 + 10, key_id=self.kid, ip='1.1.1.1', model='m',
                           mapped_model='m', status=503, prompt_tokens=0,
                           completion_tokens=0, latency_ms=1, first_token_ms=None,
                           ua='UA', error=None, stream=1, credit=None, realm='cn')
        self.assertEqual(stats._usage_health(self.today, 0, None)['logs_today'], 0)


if __name__ == '__main__':
    unittest.main()


class StatsCoveringIndexTest(unittest.TestCase):
    """统计页的几条热查询必须**走覆盖索引**，否则会静默退化回秒级。

    为什么这条值得单独钉住：`idx_logs_ts` 只能把范围定位到区间，之后每一行
    都要回表取 status / token / 耗时。20 万行实测单条查询 1 秒级，加上
    `idx_logs_cover` 后降到十几到两百毫秒（失败计数 966→17ms，按天成功数
    1199→194ms，吞吐量 1064→113ms）。而**退化是完全静默的**：数字照旧正确，
    只是总览页每 30 秒的轮询开始卡。没有这条断言，任何一次「顺手改改 SQL」
    （比如把条件写成函数包住 ts、或往 SELECT 里加一个不在索引里的列）都会
    让性能悄悄回到原点，且不会有任何测试变红。

    断言方式是**捕获实现真正执行的 SQL** 再 `EXPLAIN`，不是抄一份 SQL 写死
    在测试里 —— 抄写的那份会随实现漂移，最后测的是一段没人跑的语句。
    """

    # 这些函数都在总览 / 统计页的热路径上（30 秒轮询或切页即触发）
    HOT = ('summary', '_failures', '_daily_failures', '_daily_success',
           '_hourly_failures', '_hourly_success', '_throughput_by_model')

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'cover.db'
        db._conn = None
        db.connect()
        db.execute("INSERT INTO api_keys(name,key_hash,prefix,enabled,created_at,"
                   "quota,used_tokens) VALUES('k','h','wbk_u',1,?,0,0)",
                   (int(time.time()),))
        self.kid = db.query_one('SELECT id FROM api_keys LIMIT 1')['id']
        self.today = db.day_of()
        self.t0 = db.day_start_ts(self.today)
        for i in range(20):
            db.add_request_log(ts=self.t0 + i * 60, key_id=self.kid, ip='1.1.1.1',
                               model='m', mapped_model='m',
                               status=200 if i % 5 else 503,
                               prompt_tokens=5, completion_tokens=5, latency_ms=100,
                               first_token_ms=10, ua='UA', error=None, stream=1,
                               credit=None, realm='cn')

    def tearDown(self) -> None:
        try:
            if db._conn is not None:
                db._conn.close()
        except Exception:  # noqa: BLE001
            pass
        db._conn = None
        config.DB_PATH = self._orig
        self._tmp.cleanup()

    def _captured_log_sql(self) -> list[tuple[str, tuple]]:
        """跑一遍热路径函数，收集它们对 `request_logs` 真正发出的 SQL。"""
        seen: list[tuple[str, tuple]] = []
        orig_query, orig_one = db.query, db.query_one

        def rec_query(sql, args=()):  # noqa: ANN001
            seen.append((sql, tuple(args)))
            return orig_query(sql, args)

        def rec_one(sql, args=()):  # noqa: ANN001
            seen.append((sql, tuple(args)))
            return orig_one(sql, args)

        db.query, db.query_one = rec_query, rec_one
        try:
            for name in self.HOT:
                fn = getattr(stats, name)
                if name == 'summary':
                    fn()
                elif name == '_throughput_by_model':
                    # 签名是 (days, realm, ...)：realm 没有默认值
                    fn(7, None)
                elif name in ('_daily_failures', '_daily_success'):
                    fn(7)
                elif name in ('_hourly_failures', '_hourly_success'):
                    fn(self.today)
                else:
                    fn()
        finally:
            db.query, db.query_one = orig_query, orig_one
        return [(sql, args) for sql, args in seen if 'request_logs' in sql]

    def _plans(self) -> dict[str, str]:
        """对捕获到的每条 SQL 取「强制走本索引」时的查询计划。

        为什么要 `INDEXED BY` 而不是让优化器自己选：小表上（测试库里只有几十
        行）优化器会挑更窄的 `idx_logs_ts` —— 两者都是零点几毫秒，选谁都不算
        错。可那样断言就变成「表够大时才通过」，在 CI 上会随机假红。

        `INDEXED BY` 把「这条查询需要的列是否全在索引里」单独问出来，与表大小、
        代价估算完全无关：计划里是 `USING COVERING INDEX` 就代表不用回表。
        """
        out = {}
        for sql, args in self._captured_log_sql():
            forced = sql.replace('FROM request_logs',
                                 'FROM request_logs INDEXED BY idx_logs_cover', 1)
            detail = ' | '.join(str(r['detail']) for r in
                                 db.query('EXPLAIN QUERY PLAN ' + forced, args))
            out[' '.join(sql.split())[:80]] = detail
        return out

    def test_hot_queries_are_covered_by_the_index(self) -> None:
        plans = self._plans()
        self.assertTrue(plans, '没捕获到任何 request_logs 查询，断言前提不成立')
        bad = {k: v for k, v in plans.items()
               if 'COVERING INDEX' not in v.upper() or 'SCAN' in v.upper()}
        self.assertFalse(
            bad,
            '这些热查询没被索引完整覆盖（每行都要回表，20 万行时是秒级）：\n'
            + '\n'.join(f'  {k}\n    {v}' for k, v in bad.items()),
        )

    def test_assertion_is_not_vacuous(self) -> None:
        """反证：把索引换成更窄的 `(ts, status)` 后，断言必须**真的会红**。

        没有这一步，断言可能因为「计划字符串里碰巧总有 COVERING 字样」而永远
        通过 —— 那样它就不保护任何东西了。这里换成真实存在但不够宽的索引，
        模拟的正是「有人删掉几列」这个退化方向。
        """
        db.execute('DROP INDEX idx_logs_cover')
        db.execute('CREATE INDEX idx_logs_cover ON request_logs(ts, status)')
        plans = self._plans()
        self.assertTrue(plans, '没捕获到任何 request_logs 查询')
        self.assertTrue(
            any('COVERING INDEX' not in v.upper() for v in plans.values()),
            '索引变窄后计划里仍全是 COVERING，说明这条断言测不出列缺失',
        )
