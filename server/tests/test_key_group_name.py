"""密钥的「自定义分组名」：存储、归一化、路由校验、迁移四条链路各钉一遍。

功能语义：一把密钥可以贴一个自由填写的标签（如「客服组」「客户A」），密钥列表
按它筛选与归类。它**只用于显示与筛选**，不参与任何调用判定 —— 与 `upstream_id`
（账号池分组，决定请求走哪个池）是两个维度，本文件也把这条边界钉住。

锁定的性质：

  * 创建 / 更新 / 列表 / 网关解析四条路径都要带上这一列（否则界面看不到，
    或者列表与编辑框对不上）；
  * 显式传 `''` = 改回未分组，与「没提交这个字段」必须区分开（PATCH 语义）；
  * 非法值（换行 / 制表符 / 超长 / 非字符串）**抛 ValueError**，由路由翻成 400
    —— 而不是像 `_norm_upstream_id` 那样静默吃掉：分组名是用户自己起的标签，
    存成和输入不一样的字符串只会让人对着列表怀疑自己；
  * 存量库升级后必须有这一列，且老密钥一律是「未分组」（行为与升级前一致）。
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi import HTTPException  # noqa: E402

from server import config, db, keysvc  # noqa: E402
from server.routers import keys  # noqa: E402


class GroupNameStoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        d = Path(cls._tmp.name)
        cls._orig = (config.DB_PATH, config.USERS_FILE, config.STATIC_DIR)
        config.DB_PATH = d / 'k.db'
        config.USERS_FILE = d / 'users.json'
        config.STATIC_DIR = d / 'no-static'
        config.USERS_FILE.write_text(json.dumps({
            'secret': 'S' * 64,
            'users': [{'username': 'admin', 'role': 'admin', 'pwd_hash': 'x'}],
            'api_keys': [],
        }), encoding='utf-8')
        db._conn = None
        db.connect()

    @classmethod
    def tearDownClass(cls) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH, config.USERS_FILE, config.STATIC_DIR = cls._orig
        try:
            cls._tmp.cleanup()
        except PermissionError:
            pass

    # ── 默认值与创建 ─────────────────────────────────────
    def test_default_is_ungrouped(self) -> None:
        made = keysvc.create_key('grp-default')
        self.assertEqual(made['group_name'], '')
        self.assertEqual(keysvc.resolve(made['key'])['group_name'], '')

    def test_create_keeps_group_on_every_read_path(self) -> None:
        """创建响应、网关解析、列表三条路径都要带这一列。"""
        made = keysvc.create_key('grp', group_name='客服组')
        self.assertEqual(made['group_name'], '客服组')
        # 网关解析（数据面）也走 _parse：漏了它不影响调用，但会让日志 / 上下文
        # 拿不到分组，后续想按分组统计就得再查一次库。
        self.assertEqual(keysvc.resolve(made['key'])['group_name'], '客服组')
        listed = {k['id']: k for k in keysvc.list_keys()}
        self.assertEqual(listed[made['id']]['group_name'], '客服组')

    def test_whitespace_is_trimmed(self) -> None:
        made = keysvc.create_key('grp-trim', group_name='  客户A  ')
        self.assertEqual(made['group_name'], '客户A', '前后空白要去掉（否则「客户A」会变成两组）')

    def test_blank_means_ungrouped(self) -> None:
        for blank in ('', '   ', '\t', None):
            with self.subTest(blank=blank):
                made = keysvc.create_key('grp-blank', group_name=blank)  # type: ignore[arg-type]
                self.assertEqual(made['group_name'], '')

    # ── 更新与清除 ───────────────────────────────────────
    def test_update_then_clear(self) -> None:
        made = keysvc.create_key('grp-upd')
        updated = keysvc.update_key(made['id'], {'group_name': '客户B'})
        self.assertEqual(updated['group_name'], '客户B')

        # 显式空串 = 移出分组（与「没提交该字段」必须区分开）
        cleared = keysvc.update_key(made['id'], {'group_name': ''})
        self.assertEqual(cleared['group_name'], '')

        # 显式 null 同样算清除（PATCH 语义与 upstream_id / realm 一致）
        keysvc.update_key(made['id'], {'group_name': '客户C'})
        self.assertEqual(
            keysvc.update_key(made['id'], {'group_name': None})['group_name'], '')

    def test_omitting_field_keeps_group(self) -> None:
        """只改名字不能顺手把分组清空 —— 这是 PATCH 最容易写错的地方。"""
        made = keysvc.create_key('grp-keep', group_name='客服组')
        kept = keysvc.update_key(made['id'], {'name': 'renamed'})
        self.assertEqual(kept['group_name'], '客服组')
        self.assertEqual(kept['name'], 'renamed')

    def test_group_does_not_touch_upstream(self) -> None:
        """分组名与账号池绑定互不影响（两个维度，别写串了）。"""
        made = keysvc.create_key('grp-iso', group_name='客服组')
        self.assertIsNone(made['upstream_id'], '设分组不该顺手绑定上游')
        updated = keysvc.update_key(made['id'], {'group_name': '客户A'})
        self.assertIsNone(updated['upstream_id'])

    # ── 归一化 ───────────────────────────────────────────
    def test_invalid_values_raise(self) -> None:
        for bad in ['a\nb', 'a\rb', 'a\tb', 'x' * (keysvc.GROUP_NAME_MAX + 1), ['x'], 12]:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    keysvc._norm_group_name(bad)

    def test_valid_values_pass(self) -> None:
        for good, want in [
            ('客服组', '客服组'),
            ('  Client A  ', 'Client A'),
            ('a.b_c-9:0', 'a.b_c-9:0'),
            ('', ''),
            (None, ''),
            ('x' * keysvc.GROUP_NAME_MAX, 'x' * keysvc.GROUP_NAME_MAX),
        ]:
            with self.subTest(good=good):
                self.assertEqual(keysvc._norm_group_name(good), want)

    def test_router_turns_valueerror_into_400(self) -> None:
        for bad in ['a\nb', 'x' * (keysvc.GROUP_NAME_MAX + 1), 12]:
            with self.subTest(bad=bad):
                with self.assertRaises(HTTPException) as ctx:
                    keys._check_group_name(bad)
                self.assertEqual(ctx.exception.status_code, 400)
        keys._check_group_name('')        # 清除是合法的
        keys._check_group_name(None)      # PATCH 不带该字段
        keys._check_group_name('客服组')


class GroupNameMigrationTest(unittest.TestCase):
    """老库升级后要有这一列，且存量密钥一律「未分组」。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH

    def tearDown(self) -> None:
        try:
            if db._conn is not None:
                db._conn.close()
        except Exception:  # noqa: BLE001
            pass
        db._conn = None
        config.DB_PATH = self._orig
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _reopen(self, path: pathlib.Path) -> None:
        db._conn = None
        config.DB_PATH = path
        db.connect()

    def test_fresh_db_has_the_column(self) -> None:
        self._reopen(pathlib.Path(self._tmp.name) / 'new.db')
        cols = {r[1] for r in db.query('PRAGMA table_info(api_keys)')}
        self.assertIn('group_name', cols)

    def test_old_db_gets_the_column_and_keeps_data(self) -> None:
        legacy = pathlib.Path(self._tmp.name) / 'legacy.db'
        conn = sqlite3.connect(str(legacy))
        conn.executescript(
            '''
            CREATE TABLE api_keys (
              id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
              key_hash TEXT NOT NULL, prefix TEXT NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 1, expires_at INTEGER,
              max_ips INTEGER NOT NULL DEFAULT 0,
              ip_allowlist TEXT NOT NULL DEFAULT '[]',
              models TEXT NOT NULL DEFAULT '[]',
              quota INTEGER NOT NULL DEFAULT 0,
              used_tokens INTEGER NOT NULL DEFAULT 0,
              created_at INTEGER NOT NULL, last_used_at INTEGER);
            INSERT INTO api_keys(name, key_hash, prefix, created_at)
            VALUES('old', 'h', 'p', 111);
            '''
        )
        conn.commit()
        conn.close()

        self._reopen(legacy)
        cols = {r[1] for r in db.query('PRAGMA table_info(api_keys)')}
        self.assertIn('group_name', cols, '升级后应补上 group_name 列')
        row = db.query_one('SELECT name, group_name FROM api_keys WHERE id = 1')
        self.assertEqual(row['name'], 'old', '迁移不应丢数据')
        self.assertEqual(row['group_name'], '', '存量密钥一律未分组（行为不变）')

    def test_migration_is_idempotent(self) -> None:
        path = pathlib.Path(self._tmp.name) / 'again.db'
        self._reopen(path)
        for _ in range(3):
            db._conn.close()
            self._reopen(path)
        cols = [r[1] for r in db.query('PRAGMA table_info(api_keys)')]
        self.assertEqual(cols.count('group_name'), 1, '重复迁移不应产生重复列')


class GroupNameRequestModelTest(unittest.TestCase):
    """请求体字段本身：旧前端不带该字段不能因此被拒。"""

    def test_keyin_defaults_to_empty(self) -> None:
        body = keys.KeyIn(name='x')
        self.assertEqual(body.group_name, '')

    def test_keypatch_defaults_to_none(self) -> None:
        """None 表示「本次没提交」，与 ''（清除）必须区分开。"""
        self.assertIsNone(keys.KeyPatch().group_name)
        self.assertEqual(keys.KeyPatch(group_name='').group_name, '')

    def test_exclude_unset_keeps_the_difference(self) -> None:
        patch = keys.KeyPatch(name='y').model_dump(exclude_unset=True)
        self.assertNotIn('group_name', patch, '没提交就不能出现在 patch 里（否则会被清空）')
        patch = keys.KeyPatch(group_name='').model_dump(exclude_unset=True)
        self.assertIn('group_name', patch)


class GroupNameApiTest(unittest.TestCase):
    """走真实 HTTP 路由：字段名、校验、PATCH 语义端到端钉一遍。

    单元测试直接调 keysvc 时用的是 Python 参数名，而前端发的是 JSON 字段名；
    两者对不上（拼错一个下划线）单元测试照样全绿，界面却永远存不进去。
    """

    @classmethod
    def setUpClass(cls) -> None:
        from fastapi.testclient import TestClient

        from server import security

        cls._tmp = tempfile.TemporaryDirectory()
        d = Path(cls._tmp.name)
        cls._orig = (config.DB_PATH, config.USERS_FILE, config.STATIC_DIR)
        config.DB_PATH = d / 'api.db'
        config.USERS_FILE = d / 'users.json'
        config.STATIC_DIR = d / 'no-static'
        config.USERS_FILE.write_text(json.dumps({
            'secret': 'S' * 64,
            'users': [{'username': 'admin', 'role': 'admin',
                       'pwd_hash': security.make_hash('pw-123456')}],
            'api_keys': [],
        }), encoding='utf-8')
        db._conn = None
        db.connect()
        from server.main import app
        cls.c = TestClient(app)

    @classmethod
    def tearDownClass(cls) -> None:
        from server import security
        security._fail.clear()
        security._user_fail.clear()
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH, config.USERS_FILE, config.STATIC_DIR = cls._orig
        try:
            cls._tmp.cleanup()
        except PermissionError:
            pass

    def setUp(self) -> None:
        from server import security
        security._fail.clear()
        security._user_fail.clear()
        self.c.cookies.clear()
        r = self.c.post('/api/login',
                        json={'username': 'admin', 'password': 'pw-123456'})
        self.assertEqual(r.status_code, 200, r.text)
        self.c.cookies.update(dict(r.cookies))

    def tearDown(self) -> None:
        from server import security
        security._fail.clear()
        security._user_fail.clear()
        self.c.cookies.clear()

    def test_create_list_and_patch(self) -> None:
        r = self.c.post('/api/keys', json={'name': 'api-grp', 'group_name': '客服组'})
        self.assertEqual(r.status_code, 200, r.text)
        created = r.json()
        self.assertEqual(created['group_name'], '客服组')
        kid = created['id']

        listed = next(k for k in self.c.get('/api/keys').json() if k['id'] == kid)
        self.assertEqual(listed['group_name'], '客服组', '列表接口要带出分组（界面靠它筛选）')

        r = self.c.patch(f'/api/keys/{kid}', json={'group_name': '客户A'})
        self.assertEqual(r.json()['group_name'], '客户A')

        # 只改名字：分组不能被顺手清空
        r = self.c.patch(f'/api/keys/{kid}', json={'name': 'renamed'})
        self.assertEqual(r.json()['group_name'], '客户A')

        # 显式空串 = 移出分组
        r = self.c.patch(f'/api/keys/{kid}', json={'group_name': ''})
        self.assertEqual(r.json()['group_name'], '')

        self.c.delete(f'/api/keys/{kid}')

    def test_bad_group_name_rejected_with_400(self) -> None:
        r = self.c.post('/api/keys', json={'name': 'api-bad', 'group_name': 'a\nb'})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn('换行', r.json()['detail'])

        created = self.c.post('/api/keys', json={'name': 'api-bad2'}).json()
        r = self.c.patch(f'/api/keys/{created["id"]}',
                         json={'group_name': 'x' * (keysvc.GROUP_NAME_MAX + 1)})
        self.assertEqual(r.status_code, 400, r.text)
        self.c.delete(f'/api/keys/{created["id"]}')

    def test_old_client_without_the_field_still_works(self) -> None:
        """旧前端不带这个字段时不能因此被拒（升级期两边可能不同步）。"""
        r = self.c.post('/api/keys', json={'name': 'api-legacy'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['group_name'], '')
        self.c.delete(f'/api/keys/{r.json()["id"]}')


class GroupNameFrontendWiringTest(unittest.TestCase):
    """前端接线守卫：字段真的接到表单、提交体、列表与筛选上了。

    渲染行为由浏览器验收脚本覆盖，这里只钉住「漏一处就静默失效」的接线点
    ——比如表单加了输入框但提交体没带，用户改完保存后看起来一切正常，实际没存。
    """

    def _page(self) -> str:
        return (Path(__file__).resolve().parents[2]
                / 'web/app/(main)/keys/page.tsx').read_text(encoding='utf-8')

    def test_form_state_carries_the_field(self) -> None:
        src = self._page()
        self.assertIn('groupName: string;', src)
        self.assertIn("groupName: k.group_name || ''", src, '编辑弹窗要回填已有分组')
        self.assertIn('group_name: form.groupName.trim()', src, '提交体必须带上分组名')

    def test_list_shows_and_filters_by_group(self) -> None:
        src = self._page()
        self.assertIn("t('keys.group')", src, '表格要有分组列')
        self.assertIn('groupFilter', src, '要有分组筛选状态')
        self.assertIn('GROUP_UNGROUPED', src, '「未分组」是一个可筛的取值，不能被当成「全部」')

    def test_filtered_empty_state_is_not_no_keys(self) -> None:
        """筛着分组时说「暂无 API 密钥」是假话 —— 密钥可能只是不在这组。"""
        src = self._page()
        self.assertIn("t('keys.groupGoneTitle'", src)
        self.assertIn("t('keys.groupEmptyTitle'", src)
        self.assertIn("t('keys.groupUngroupedEmptyTitle'", src,
                      '「未分组」为空时不能说成「分组「未分组」已经不在了」')
        self.assertIn("t('keys.groupShowAll')", src)

    def test_group_input_offers_existing_names(self) -> None:
        """已有分组名要做成候选：手打容易差一个字符，变成两个几乎一样的组。"""
        src = self._page()
        self.assertIn('<datalist', src)
        self.assertIn('list="key-group-names"', src)


if __name__ == '__main__':
    unittest.main()
