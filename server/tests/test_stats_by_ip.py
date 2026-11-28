"""按「IP × 密钥」的用量聚合（`/api/stats/by-ip`）。"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db  # noqa: E402
from server.routers import stats  # noqa: E402

TODAY = time.strftime('%Y-%m-%d')
USER = {'role': 'admin'}


def _ts(day: str, hour: int = 12) -> int:
    return int(time.mktime(time.strptime(f'{day} {hour:02d}:00:00', '%Y-%m-%d %H:%M:%S')))


class ByIpTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        config.DB_PATH = Path(self._tmp.name) / 's.db'
        db._conn = None
        db.connect()
        self.addCleanup(self._cleanup)
        db.execute(
            'INSERT INTO api_keys(id, name, key_hash, prefix, created_at) '
            'VALUES(1, ?, ?, ?, ?)',
            ('主密钥', 'hash-1', 'sk-1', int(time.time())),
        )
        db.execute(
            'INSERT INTO api_keys(id, name, key_hash, prefix, created_at) '
            'VALUES(2, ?, ?, ?, ?)',
            ('备用密钥', 'hash-2', 'sk-2', int(time.time())),
        )

    def _cleanup(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        self._tmp.cleanup()

    def _log(self, *, day: str = TODAY, ip: str = '1.1.1.1', key_id: int | None = 1,
             credit: float = 1.0, prompt: int = 100, completion: int = 20,
             realm: str = 'cn', status: int = 200,
             platform: str = '', cache_hit: int = 0,
             cache_miss: int = 0) -> None:
        db.add_request_log(ts=_ts(day), ip=ip, model='glm-5.2', key_id=key_id,
                           credit=credit, prompt_tokens=prompt,
                           completion_tokens=completion, realm=realm,
                           status=status, platform=platform,
                           cache_hit_tokens=cache_hit,
                           cache_miss_tokens=cache_miss)

    def test_groups_by_ip_and_key(self) -> None:
        self._log(ip='1.1.1.1', key_id=1, credit=1.5)
        self._log(ip='1.1.1.1', key_id=1, credit=2.5, prompt=50, completion=10)
        self._log(ip='1.1.1.1', key_id=2, credit=3.0)
        self._log(ip='2.2.2.2', key_id=1, credit=4.0)

        out = stats.by_ip(days=1, user=USER)
        rows = {(r['ip'], r['key_id']): r for r in out['items']}
        first = rows[('1.1.1.1', 1)]
        self.assertEqual(first['key'], '主密钥')
        self.assertEqual(first['requests'], 2)
        self.assertEqual(first['tokens'], 180)
        self.assertAlmostEqual(first['credit'], 4.0)
        self.assertEqual(rows[('1.1.1.1', 2)]['key'], '备用密钥')
        self.assertEqual(rows[('2.2.2.2', 1)]['requests'], 1)

    def test_ip_missing_falls_back_to_unknown(self) -> None:
        self._log(ip='')
        self._log(ip='   ', key_id=1)
        out = stats.by_ip(days=1, user=USER)
        self.assertEqual([r['ip'] for r in out['items']], ['未知'])

    def test_requests_without_key_are_excluded(self) -> None:
        """没有密钥的扫描器流量不是真实用量，不能混进来。"""
        self._log(ip='9.9.9.9', key_id=None)
        self._log(ip='1.1.1.1', key_id=1)
        out = stats.by_ip(days=1, user=USER)
        self.assertEqual([r['ip'] for r in out['items']], ['1.1.1.1'])

    def test_filter_by_realm_and_platform(self) -> None:
        self._log(ip='1.1.1.1', realm='cn')
        self._log(ip='2.2.2.2', realm='global')
        self._log(ip='3.3.3.3', platform='trae')

        self.assertEqual([r['ip'] for r in stats.by_ip(days=1, realm='global', user=USER)['items']],
                         ['2.2.2.2'])
        self.assertEqual([r['ip'] for r in stats.by_ip(days=1, platform='trae', user=USER)['items']],
                         ['3.3.3.3'])

    def test_custom_range_and_cache_fields(self) -> None:
        self._log(day='2026-01-01', ip='1.1.1.1', cache_hit=30, cache_miss=10)
        self._log(day=TODAY, ip='2.2.2.2', key_id=2)
        out = stats.by_ip(start='2026-01-01', end='2026-01-01', user=USER)
        self.assertEqual([r['ip'] for r in out['items']], ['1.1.1.1'])
        self.assertEqual(out['items'][0]['cache_hit_tokens'], 30)
        self.assertEqual(out['items'][0]['cache_miss_tokens'], 10)
        self.assertAlmostEqual(out['items'][0]['cache_hit_rate'], 0.75)
        self.assertEqual((out['from'], out['to']), ('2026-01-01', '2026-01-01'))


if __name__ == '__main__':
    unittest.main()
