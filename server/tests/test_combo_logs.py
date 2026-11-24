"""组合卡片的请求日志：必须按 `combo/<name>` **精确**筛选，并给出整段汇总。

组合在日志里有两层名字：`model` 是客户端请求的 `combo/<name>`，`mapped_model`
是最终落到的成员（`platform/model`）。日志页的模型筛选是 LIKE，拿它当组合筛选
会把 `combo/fast` 与 `combo/fast2` 混在一起；组合卡片要回答的是「这一个组合
实际把请求发给了谁、稳不稳」，所以这里单独钉住等值查询与汇总口径。

汇总必须覆盖**整个时间范围**，而不是只算返回的前 N 条：只按一屏算的话，
刷新或换个数就会变，用户没法用它判断组合的稳定性。平均耗时只取成功请求——
429 / 上游 5xx 常常几毫秒就返回，混进去会把均值拉低。
"""
from __future__ import annotations

import csv
import io
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db  # noqa: E402
from server.routers import logs as logs_router  # noqa: E402


class ComboLogsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'combo-logs.db'
        db._conn = None
        db.connect()
        self.now = int(time.time())

    def tearDown(self) -> None:
        try:
            if db._conn is not None:
                db._conn.close()
        except Exception:  # noqa: BLE001
            pass
        db._conn = None
        config.DB_PATH = self._orig
        self._tmp.cleanup()

    def _add(self, *, model: str, mapped: str, status: int, latency: int,
             prompt: int = 0, completion: int = 0, realm: str = 'cn',
             error: str | None = None, ts: int | None = None,
             credit: float | None = None,
             first_token: int | None = None) -> None:
        db.add_request_log(
            ts=self.now if ts is None else ts,
            key_id=None, ip='127.0.0.1', model=model, mapped_model=mapped,
            status=status, prompt_tokens=prompt, completion_tokens=completion,
            latency_ms=latency, first_token_ms=first_token, ua='UA', error=error,
            stream=1, credit=credit, realm=realm, platform='',
        )

    def _seed(self) -> None:
        # 两个成功（一个国内成员、一个国际成员）+ 一个成员全失败
        self._add(model='combo/fast', mapped='trae/x', status=200, latency=100,
                  prompt=10, completion=5, realm='cn', credit=1.5)
        self._add(model='combo/fast', mapped='minimax/y', status=200, latency=300,
                  prompt=20, completion=10, realm='global', credit=2.5)
        self._add(model='combo/fast', mapped='combo/fast', status=503, latency=20,
                  error='组合「fast」全部失败', realm='cn')
        # 诱饵：名字只差一个字符，不能被 LIKE 带出来
        self._add(model='combo/fast2', mapped='trae/z', status=200, latency=50,
                  prompt=1, completion=1)
        # 诱饵：直连成员
        self._add(model='trae/x', mapped='trae/x', status=200, latency=40,
                  prompt=2, completion=2)
        # 超出默认 7 天的旧记录
        self._add(model='combo/fast', mapped='trae/old', status=200, latency=500,
                  prompt=100, completion=100,
                  ts=self.now - 10 * 86400)

    def _call(self, name: str = 'fast', days: int = 7, limit: int = 50) -> dict:
        return logs_router.combo_logs(
            name, days=days, limit=limit, user={'username': 'admin'})

    def test_exact_match_excludes_similar_names_and_direct_calls(self) -> None:
        self._seed()
        out = self._call()
        self.assertEqual(out['model'], 'combo/fast')
        # combo/fast2 与直连 trae/x 都不算；7 天内旧记录也被排除
        self.assertEqual(out['total'], 3)
        self.assertEqual(
            [item['mapped_model'] for item in out['items']],
            ['combo/fast', 'minimax/y', 'trae/x'],
        )

    def test_summary_covers_whole_range(self) -> None:
        self._seed()
        summary = self._call()['summary']
        self.assertEqual(summary['total'], 3)
        self.assertEqual(summary['success'], 2)
        self.assertEqual(summary['failed'], 1)
        self.assertAlmostEqual(summary['success_rate'], 2 / 3)
        # 成功请求平均耗时，不让 503 的 20ms 把均值拉低
        self.assertEqual(summary['avg_latency_ms'], 200)
        # 只统计这一个组合的 token 与扣费（不含诱饵行）
        self.assertEqual(summary['total_tokens'], 45)
        self.assertAlmostEqual(summary['total_credit'], 4.0)

    def test_limit_does_not_shrink_summary(self) -> None:
        self._seed()
        out = self._call(limit=1)
        self.assertEqual(len(out['items']), 1)
        self.assertEqual(out['summary']['total'], 3,
                         '汇总不能只算返回的那一屏')

    def test_days_zero_means_no_time_filter(self) -> None:
        self._seed()
        out = self._call(days=0)
        self.assertEqual(out['total'], 4, 'days=0 应连同旧记录一起返回')
        self.assertEqual(out['summary']['avg_latency_ms'], 300)

    def test_empty_combo_is_well_formed(self) -> None:
        out = self._call('nobody')
        self.assertEqual(out['total'], 0)
        self.assertEqual(out['items'], [])
        self.assertIsNone(out['summary']['success_rate'])
        self.assertIsNone(out['summary']['avg_latency_ms'])

    def test_item_shape_matches_log_page(self) -> None:
        """组合卡片与日志页共用同一份 item 映射，字段不能缺。"""
        self._seed()
        item = self._call()['items'][0]
        for field in ('id', 'ts', 'model', 'mapped_model', 'status', 'realm',
                      'prompt_tokens', 'completion_tokens', 'latency_ms',
                      'error', 'credit', 'cost', 'platform'):
            self.assertIn(field, item)

    # ── CSV 导出 ────────────────────────────────────────────
    def _export(self, name: str = 'fast', days: int = 7):
        return logs_router.export_combo_logs(
            name, days=days, user={'username': 'admin'})

    def _export_rows(self, name: str = 'fast', days: int = 7) -> list[list[str]]:
        """跳过 BOM 后按 CSV 解析，返回数据行（不含表头）。"""
        text = self._export(name, days).body.decode('utf-8-sig')
        return list(csv.reader(io.StringIO(text)))[1:]

    def test_export_is_csv_with_bom_and_exact_match(self) -> None:
        """导出必须是「Excel 能打开」的 CSV，且与列表一样按入口名等值筛选。

        等值这一点最容易被写成另一个 LIKE：`combo/fast2` 与直连 `trae/x`
        一旦混进来，用户按成员做的统计就全错了。
        """
        self._seed()
        resp = self._export()
        self.assertTrue(resp.body.startswith('\ufeff'.encode('utf-8')),
                        '缺 BOM 时 Excel 会把中文显示成乱码')
        self.assertIn('text/csv', resp.headers['content-type'])
        self.assertIn('attachment;', resp.headers['content-disposition'])

        text = resp.body.decode('utf-8-sig')
        self.assertIn('输出速率tok/s', text)
        self.assertNotIn('fast2', text, '名字相近的组合不能被 LIKE 带出来')
        rows = self._export_rows()
        self.assertEqual(len(rows), 3)
        self.assertEqual(
            [row[1] for row in rows], ['trae/x', 'minimax/y', ''],
            '导出按时间正序；成员全失败的行没有「实际成员」，留空而不是写组合名',
        )
        self.assertEqual(resp.headers['x-export-rows'], '3')
        self.assertEqual(resp.headers['x-export-truncated'], '0')
        self.assertEqual(resp.headers['x-export-total'], '3')

    def test_export_computes_generation_speed(self) -> None:
        """生成耗时 / tok/s 必须与统计页同一公式（latency − first_token）。"""
        self._add(model='combo/fast', mapped='trae/x', status=200,
                  latency=1000, prompt=10, completion=10, first_token=200)
        row = self._export_rows()[0]
        header = next(csv.reader(io.StringIO(
            self._export().body.decode('utf-8-sig'))))
        cell = dict(zip(header, row))
        self.assertEqual(cell['首字延迟ms'], '200')
        self.assertEqual(cell['生成耗时ms'], '800')
        self.assertEqual(cell['输出速率tok/s'], '12.5')

    def test_export_truncates_to_latest_rows_honestly(self) -> None:
        """超过上限时只给最近的一段，并把「没导全」写在响应头里。"""
        self._seed()
        original = logs_router.COMBO_EXPORT_MAX_ROWS
        logs_router.COMBO_EXPORT_MAX_ROWS = 2
        try:
            resp = self._export(days=0)
        finally:
            logs_router.COMBO_EXPORT_MAX_ROWS = original
        self.assertEqual(resp.headers['x-export-rows'], '2')
        self.assertEqual(resp.headers['x-export-truncated'], '1')
        self.assertEqual(resp.headers['x-export-total'], '4')
        self.assertEqual(len(self._export_rows(days=0)), 4,
                         '标本上限恢复后应能导全')

    def test_export_neutralizes_csv_formula_injection(self) -> None:
        """错误文案来自上游，不能让它以 `=` 开头变成 Excel 公式。"""
        self._add(model='combo/fast', mapped='trae/x', status=500,
                  latency=10, error='=HYPERLINK("http://evil.example")')
        row = self._export_rows()[0]
        self.assertTrue(row[-1].startswith("'="), row[-1])

    def test_export_filename_rejects_header_injection(self) -> None:
        """URL 里的组合名不能把引号 / 换行带进 Content-Disposition。"""
        resp = self._export(name='fast"\r\nX-Evil: 1')
        disposition = resp.headers['content-disposition']
        self.assertNotIn('\r', disposition)
        self.assertNotIn('\n', disposition)
        self.assertEqual(disposition.count('"'), 2,
                         '只应有包住文件名的两个引号')


if __name__ == '__main__':
    unittest.main()
