"""官方 CLI / 桌面端凭据的 `expiresAt` 是**毫秒**，进出两端都要归一。

现场：`CodeBuddyExtension/Data/Public/auth/*.info` 里写 `expiresAt: 1795254516410`。
本仓自己落盘的文件一律用 epoch **秒**；只有**原文件本来就是毫秒**时才按毫秒写回
（跟随来源单位，见文末）。不归一的话：

  · 面板算出「剩余五万多年」，进度条永远是满格；
  · 续期巡检认为还早得很，**永远不触发**，账号在某个不设防的夜里静默掉线；
  · 上游 `NeedsRefresh` 恒 false（Go 侧同源问题，另有用例守着）。

钉住的约束：
  · 导入时毫秒归一成秒，秒制文件原样保留；
  · 缺失 / 0 / 非法 → 0（= 未知，不是「已过期」）；
  · `expiresIn`（时长）仍按当前时刻累加，不受本规则影响；
  · 来源是毫秒的文件**写回仍是毫秒** —— 续期（就地改字段）与重新登录 / 覆盖
    导入（整份重建）两条路都算；按秒写回会把它缩到 1970 年代，桌面端随即判定
    过期。
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.routers.accounts import _uploaded_account  # noqa: E402
from server.services import tencent, wb2api  # noqa: E402

# 参考仓实测的那份官方凭据值（2026-10-24，已于 2026-11-21 到期）。
# 只用于验证「毫秒 → 秒」的换算，**不用它断言「未过期」** —— 写死的时间点迟早
# 会过期，那条断言会在某天无故变红（参考仓 2026-11-21 就是这么挂的）。
OFFICIAL_MS = 1795254516410
# 「未过期」类断言一律用相对当前时间构造的毫秒值。
FUTURE_MS = (int(time.time()) + 28 * 86400) * 1000


class NormaliseHelperTest(unittest.TestCase):
    def test_millis_becomes_seconds(self) -> None:
        self.assertEqual(tencent._normalise_expires_at(OFFICIAL_MS),
                         OFFICIAL_MS // 1000)

    def test_seconds_untouched(self) -> None:
        self.assertEqual(tencent._normalise_expires_at(4102444800), 4102444800)

    def test_zero_and_garbage_are_unknown(self) -> None:
        for raw in (0, None, '', 'abc', -5):
            self.assertEqual(tencent._normalise_expires_at(raw), 0, raw)

    def test_millis_detector_matches_helper(self) -> None:
        self.assertTrue(tencent._expires_at_millis(OFFICIAL_MS))
        self.assertFalse(tencent._expires_at_millis(4102444800))
        self.assertFalse(tencent._expires_at_millis(0))

    def test_write_back_never_doubles_or_drops_unit(self) -> None:
        """写回函数两个方向都要稳：递秒写秒、递毫秒写秒、按毫秒制再换回去。"""
        self.assertEqual(tencent._write_back_expires_at(4102444800, False), 4102444800)
        self.assertEqual(tencent._write_back_expires_at(OFFICIAL_MS, False),
                         OFFICIAL_MS // 1000)
        self.assertEqual(tencent._write_back_expires_at(4102444800, True),
                         4102444800 * 1000)
        # 已经是毫秒的值不能再乘一次（否则变成六位数年份）；亚秒精度会被截掉，
        # 量级必须保持毫秒。
        self.assertEqual(tencent._write_back_expires_at(OFFICIAL_MS, True),
                         OFFICIAL_MS // 1000 * 1000)
        for raw in (0, None, '', -1):
            self.assertEqual(tencent._write_back_expires_at(raw, True), 0, raw)


class UploadNormalisationTest(unittest.TestCase):
    def test_official_cli_file_imports_as_seconds(self) -> None:
        entry = _uploaded_account(
            {'account': {'uid': 'u1'},
             'auth': {'accessToken': 'at', 'expiresAt': FUTURE_MS}}, 'f.json')
        self.assertEqual(entry['expires_at'], FUTURE_MS // 1000)
        # 剩余时间落在合理区间（官方令牌约 28 天），不是五万年
        remain = entry['expires_at'] - int(time.time())
        self.assertGreater(remain, 0)
        self.assertLess(remain, 400 * 86400)

    def test_expired_official_fixture_still_normalised(self) -> None:
        """换算本身与「有没有过期」无关：写死的那份旧凭据也要正确落到秒。"""
        entry = _uploaded_account(
            {'account': {'uid': 'u1'},
             'auth': {'accessToken': 'at', 'expiresAt': OFFICIAL_MS}}, 'f.json')
        self.assertEqual(entry['expires_at'], OFFICIAL_MS // 1000)

    def test_second_based_export_unchanged(self) -> None:
        entry = _uploaded_account(
            {'account': {'uid': 'u1'},
             'auth': {'accessToken': 'at', 'expiresAt': 4102444800}}, 'f.json')
        self.assertEqual(entry['expires_at'], 4102444800)

    def test_missing_expiry_stays_unknown(self) -> None:
        entry = _uploaded_account(
            {'account': {'uid': 'u1'}, 'auth': {'accessToken': 'at'}}, 'f.json')
        self.assertEqual(entry['expires_at'], 0)

    def test_expires_in_is_still_a_duration(self) -> None:
        entry = _uploaded_account(
            {'account': {'uid': 'u1'},
             'auth': {'accessToken': 'at', 'expiresIn': 3600}}, 'f.json')
        self.assertAlmostEqual(entry['expires_at'] - time.time(), 3600, delta=5)

    def test_non_numeric_expiry_still_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            _uploaded_account(
                {'account': {'uid': 'u1'},
                 'auth': {'accessToken': 'at', 'expiresAt': 'soon'}}, 'f.json')
        self.assertIn('必须是数字', str(ctx.exception))

    def test_negative_expiry_still_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            _uploaded_account(
                {'account': {'uid': 'u1'},
                 'auth': {'accessToken': 'at', 'expiresAt': -1}}, 'f.json')
        self.assertIn('非负', str(ctx.exception))


class ListingNormalisationTest(unittest.TestCase):
    """`list_auth_accounts` 是账号页与续期巡检的共同数据源。"""

    def _write(self, tmp: Path, payload: dict) -> None:
        (tmp / 'workbuddy-u1.json').write_text(json.dumps(payload), encoding='utf-8')

    def test_listing_reports_seconds_for_millis_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            tmp = Path(raw_dir)
            self._write(tmp, {'account': {'uid': 'u1'},
                              'auth': {'accessToken': 'at', 'expiresAt': FUTURE_MS}})
            rows = wb2api.list_auth_accounts(tmp)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['expires_at'], FUTURE_MS // 1000)
        self.assertFalse(rows[0]['is_expired'])
        self.assertLess(rows[0]['remain_seconds'], 400 * 86400)

    def test_listing_keeps_seconds_for_seconds_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            tmp = Path(raw_dir)
            self._write(tmp, {'account': {'uid': 'u1'},
                              'auth': {'accessToken': 'at', 'expiresAt': 4102444800}})
            rows = wb2api.list_auth_accounts(tmp)
        self.assertEqual(rows[0]['expires_at'], 4102444800)


class RenewWriteBackTest(unittest.TestCase):
    """毫秒来源的文件续期写回仍必须是毫秒。"""

    def test_millis_file_written_back_as_millis(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            tmp = Path(raw_dir)
            path = tmp / 'workbuddy-u1.json'
            path.write_text(json.dumps({
                'account': {'uid': 'u1'},
                'auth': {'accessToken': 'at', 'refreshToken': 'rt',
                         'expiresAt': OFFICIAL_MS},
            }), encoding='utf-8')
            new_sec = int(time.time()) + 28 * 86400
            tencent.update_auth_tokens('workbuddy-u1.json',
                                       {'access_token': 'at2', 'expires_at': new_sec},
                                       tmp)
            saved = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(saved['auth']['accessToken'], 'at2')
        self.assertEqual(saved['auth']['expiresAt'], new_sec * 1000)

    def test_seconds_file_written_back_as_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            tmp = Path(raw_dir)
            path = tmp / 'workbuddy-u1.json'
            path.write_text(json.dumps({
                'account': {'uid': 'u1'},
                'auth': {'accessToken': 'at', 'refreshToken': 'rt',
                         'expiresAt': 4102444800},
            }), encoding='utf-8')
            new_sec = int(time.time()) + 28 * 86400
            tencent.update_auth_tokens('workbuddy-u1.json',
                                       {'access_token': 'at2', 'expires_at': new_sec},
                                       tmp)
            saved = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(saved['auth']['expiresAt'], new_sec)

    def test_refresh_fields_accept_millis_too(self) -> None:
        """上游若哪天回毫秒，也不能把毫秒当秒写进秒制文件。"""
        with tempfile.TemporaryDirectory() as raw_dir:
            tmp = Path(raw_dir)
            path = tmp / 'workbuddy-u1.json'
            path.write_text(json.dumps({
                'account': {'uid': 'u1'},
                'auth': {'accessToken': 'at', 'expiresAt': 4102444800},
            }), encoding='utf-8')
            tencent.update_auth_tokens('workbuddy-u1.json',
                                       {'access_token': 'at2',
                                        'expires_at': OFFICIAL_MS}, tmp)
            saved = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(saved['auth']['expiresAt'], OFFICIAL_MS // 1000)


class RewriteUnitTest(unittest.TestCase):
    """重新登录 / 覆盖导入会整份重建文件，但**单位**必须跟随原文件。"""

    def _account(self, expires_at: int) -> dict:
        return {'uid': 'u1', 'access_token': 'at2', 'refresh_token': 'rt2',
                'expires_at': expires_at, 'domain': 'www.codebuddy.ai',
                'realm': 'global', 'enterprise_id': '', 'nickname': 'n1',
                'device_token': '', 'proxy': ''}

    def test_overwrite_keeps_millis_unit(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            tmp = Path(raw_dir)
            path = tmp / 'workbuddy-u1.json'
            path.write_text(json.dumps({
                'account': {'uid': 'u1'},
                'auth': {'accessToken': 'at', 'refreshToken': 'rt',
                         'expiresAt': OFFICIAL_MS},
            }), encoding='utf-8')
            new_sec = int(time.time()) + 28 * 86400
            tencent.write_auth_file(self._account(new_sec), tmp)
            saved = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(saved['auth']['accessToken'], 'at2')
        self.assertEqual(saved['auth']['expiresAt'], new_sec * 1000)

    def test_overwrite_keeps_seconds_unit(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            tmp = Path(raw_dir)
            path = tmp / 'workbuddy-u1.json'
            path.write_text(json.dumps({
                'account': {'uid': 'u1'},
                'auth': {'accessToken': 'at', 'expiresAt': 4102444800},
            }), encoding='utf-8')
            new_sec = int(time.time()) + 28 * 86400
            tencent.write_auth_file(self._account(new_sec), tmp)
            saved = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(saved['auth']['expiresAt'], new_sec)

    def test_new_file_stays_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            tmp = Path(raw_dir)
            new_sec = int(time.time()) + 28 * 86400
            tencent.write_auth_file(self._account(new_sec), tmp)
            saved = json.loads((tmp / 'workbuddy-u1.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['auth']['expiresAt'], new_sec)


if __name__ == '__main__':
    unittest.main()
