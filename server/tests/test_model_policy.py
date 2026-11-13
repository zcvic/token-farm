"""模型可见性：隐藏名单的保存与**上游 config.json 镜像**。

最要紧的三条性质（写成用例而不是只写在注释里）：

  1. **只动 `models.hidden`**：config.json 里还有 api_key / schedule / proxies，
     镜像写一次就丢掉用户口令是不可接受的；
  2. **空名单删键**：把名单清空后文件里不该留下 `"hidden": []` 这种噪声；
  3. **写失败不致命**：面板侧把上游目录挂成只读是可能出现的部署形态，那时面板
     自身的隐藏行为必须照常生效，只是直连上游端口看不到变更。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import config, db
from server.services import modelpolicy


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = self.root / 'config.json'
        self.cfg.write_text(json.dumps({
            # 镜像写 hidden 时这些键必须原样保留
            'api_key': 'wbk_keep-me',
            'schedule': {'checkin_hours': [9, 21]},
            'proxies': {'px-a': 'socks5://u:p@eu.example.dev:10000'},
            'server': {'read_timeout': '300s'},
        }, ensure_ascii=False), encoding='utf-8')
        patch = mock.patch.object(config, 'UPSTREAM_CONFIG', self.cfg)
        patch.start()
        self.addCleanup(patch.stop)

        patch = mock.patch.object(config, 'DB_PATH', self.root / 'manager.db')
        patch.start()
        self.addCleanup(patch.stop)
        db._conn = None
        db.connect()
        self.addCleanup(self._close_db)

    def _close_db(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None

    def _cfg(self) -> dict:
        return json.loads(self.cfg.read_text(encoding='utf-8'))

    def _models(self) -> dict:
        return self._cfg().get('models') or {}


class SaveMirrorTest(_Base):
    def test_mirrors_hidden_into_upstream_config(self) -> None:
        modelpolicy.save({'hidden': ['glm-5.2', 'cn:qwen-3']})
        self.assertEqual(self._models().get('hidden'), ['glm-5.2', 'cn:qwen-3'])
        # 数据库那份也要在（面板自己的行为依赖它）
        self.assertEqual(modelpolicy.load()['hidden'], ['glm-5.2', 'cn:qwen-3'])

    def test_keeps_other_keys(self) -> None:
        modelpolicy.save({'hidden': ['glm-5.2']})
        cfg = self._cfg()
        self.assertEqual(cfg.get('api_key'), 'wbk_keep-me')
        self.assertEqual(cfg.get('schedule'), {'checkin_hours': [9, 21]})
        self.assertEqual(cfg.get('proxies'),
                         {'px-a': 'socks5://u:p@eu.example.dev:10000'})
        self.assertEqual(cfg.get('server'), {'read_timeout': '300s'})

    def test_empty_hidden_removes_key(self) -> None:
        modelpolicy.save({'hidden': ['glm-5.2']})
        self.assertIn('hidden', self._models())
        modelpolicy.save({'hidden': []})
        self.assertNotIn('hidden', self._models())
        # models 段空了就连段一起删，保持文件干净
        self.assertNotIn('models', self._cfg())
        # 其余键仍然在
        self.assertEqual(self._cfg().get('api_key'), 'wbk_keep-me')

    def test_dedup_and_trim(self) -> None:
        modelpolicy.save({'hidden': ['  glm-5.2  ', 'GLM-5.2', '', 'cn:qwen']})
        self.assertEqual(self._models().get('hidden'), ['glm-5.2', 'cn:qwen'])

    def test_rejects_non_object(self) -> None:
        with self.assertRaises(ValueError):
            modelpolicy.save(['glm-5.2'])

    def test_missing_config_file_is_not_fatal(self) -> None:
        self.cfg.unlink()
        # 不该抛：面板自身的隐藏行为已经生效，镜像只是旁路
        result = modelpolicy.save({'hidden': ['glm-5.2']})
        self.assertEqual(result['hidden'], ['glm-5.2'])
        self.assertEqual(modelpolicy.load()['hidden'], ['glm-5.2'])

    def test_unwritable_config_is_not_fatal(self) -> None:
        # 模拟只读挂载：写临时文件必然失败
        with mock.patch('server.services.tencent._atomic_write_json',
                        side_effect=OSError('read-only file system')):
            result = modelpolicy.save({'hidden': ['glm-5.2']})
        self.assertEqual(result['hidden'], ['glm-5.2'])
        self.assertEqual(modelpolicy.load()['hidden'], ['glm-5.2'],
                         '镜像失败不该回滚数据库里的名单')

    def test_broken_config_is_not_fatal(self) -> None:
        self.cfg.write_text('{"api_key":', encoding='utf-8')
        result = modelpolicy.save({'hidden': ['glm-5.2']})
        self.assertEqual(result['hidden'], ['glm-5.2'])
        # 坏文件保持原样（不拿一份残破的 JSON 覆盖用户配置）
        self.assertEqual(self.cfg.read_text(encoding='utf-8'), '{"api_key":')

    def test_non_object_top_level_is_not_fatal(self) -> None:
        self.cfg.write_text('["not", "an", "object"]', encoding='utf-8')
        result = modelpolicy.save({'hidden': ['glm-5.2']})
        self.assertEqual(result['hidden'], ['glm-5.2'])
        self.assertEqual(self.cfg.read_text(encoding='utf-8'),
                         '["not", "an", "object"]')


class MatchSemanticsTest(unittest.TestCase):
    """匹配口径必须与 Go 侧 modelHidden 一致（裸名藏两域、前缀只藏对应域）。"""

    def test_bare_name_covers_both_realms(self) -> None:
        entries = ['glm-5.2']
        for model in ('glm-5.2', 'cn:glm-5.2', 'global:glm-5.2', 'GLM-5.2'):
            self.assertTrue(modelpolicy._matches(model, entries), model)

    def test_realm_prefix_only_hides_that_realm(self) -> None:
        self.assertTrue(modelpolicy._matches('cn:glm-5.2', ['cn:glm-5.2']))
        self.assertTrue(modelpolicy._matches('glm-5.2', ['cn:glm-5.2']),
                        '裸名调用默认按 cn 解释')
        self.assertFalse(modelpolicy._matches('global:glm-5.2', ['cn:glm-5.2']))
        self.assertFalse(modelpolicy._matches('cn:glm-5.2', ['global:glm-5.2']))

    def test_no_false_positive(self) -> None:
        entries = ['glm-5.2', 'cn:qwen-3']
        for model in ('glm-5', 'glm-5.20', 'qwen-3-coder', 'global:qwen-3'):
            self.assertFalse(modelpolicy._matches(model, entries), model)

    def test_empty_model_never_hidden(self) -> None:
        self.assertFalse(modelpolicy.is_hidden(''))
        self.assertFalse(modelpolicy.is_hidden(None))


if __name__ == '__main__':
    unittest.main()
