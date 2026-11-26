"""网页 Cookie 类平台的「粘贴即导入」能力位。

## 钉住的问题

面板以前按平台名硬编码「哪些平台允许把整段 Cookie 直接粘进来」。豆包接入
后，已经打包出去的旧静态前端仍然只认 dumate / lingxi：用户粘贴整段 Cookie
会被前端判成「不是合法 JSON」，请求根本到不了后端（账号目录里一条都没有）。

现在改由适配器声明 `supports_cookie_import`，`GET /api/platforms` 下发给
前端；接新的 Cookie 类平台只改后端，不再依赖前端同步发版。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config  # noqa: E402
from server.platforms import PlatformAdapter  # noqa: E402
from server.routers import platforms as platforms_router  # noqa: E402


class CookieImportCapabilityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # 只读能力清单；临时 DATA_DIR 避免把开发机上的真实账号带进测试。
        cls._tmp = tempfile.TemporaryDirectory()
        cls._patch = mock.patch.object(config, 'DATA_DIR',
                                       Path(cls._tmp.name))
        cls._patch.start()
        cls.items = {item['name']: item
                     for item in platforms_router.list_platforms({})['platforms']}

    @classmethod
    def tearDownClass(cls) -> None:
        cls._patch.stop()
        cls._tmp.cleanup()

    def test_cookie_platforms_advertise_cookie_import(self) -> None:
        for name in ('doubao', 'dumate', 'lingxi'):
            with self.subTest(platform=name):
                self.assertTrue(self.items[name].get('supports_cookie_import'),
                                name)

    def test_other_platforms_do_not_advertise_cookie_import(self) -> None:
        for name in ('trae', 'qoder', 'loomy', 'minimax', 'discovery',
                     'kilo', 'cline'):
            with self.subTest(platform=name):
                self.assertFalse(self.items[name].get('supports_cookie_import'),
                                 name)

    def test_virtual_platforms_do_not_advertise_cookie_import(self) -> None:
        for name in ('combo', 'workbuddy'):
            if name in self.items:
                self.assertFalse(self.items[name].get('supports_cookie_import'),
                                 name)

    def test_base_defaults_to_false(self) -> None:
        # 能力位是「选择加入」：漏声明的平台不会被前端当成 Cookie 平台，
        # 否则一段普通 API Key 也会被包成 `{cookie: ...}`。
        self.assertIs(PlatformAdapter.supports_cookie_import, False)


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
