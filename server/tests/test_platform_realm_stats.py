"""Trae 国际版的用量必须记到 global，而不是国内版。

## 问题

Trae 的模型名是 `trae/<model>`，版本由**选中的账号**决定（同一条目在国内 /
国际两个池子里都可能存在）。但网关原先只按模型名前缀判 `realm`，平台名里
永远没有 `global:` —— 于是国际版账号的调用全被记成国内版：统计页切到国际版
只能看到 Blur 的（国内版）数字，切回国内版反而把国际版用量也算了进去。

## 修法

  · 适配器在 `PlatformChat.realm` 里回报**实际账号**的版本（cn / global）；
  · `gateway._record` 优先用这个值，没有时才回退到模型名前缀；
  · 历史行按 `request_logs.account` 与账号版本回填，并重建该平台的汇总
    （`db.reassign_platform_realms`）。

这里钉住：显式版本优先、失败路径用适配器的目录兜底、历史行能回填且汇总
不会把两个版本混在一起。
"""
from __future__ import annotations

import asyncio
import importlib
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db, platforms  # noqa: E402
from server.platforms.base import PlatformChat, PlatformError  # noqa: E402
from server.routers import gateway  # noqa: E402

# 包级 `adapter` 导出的是**实例**；这里要模块本身（`_db_realm`、`catalog`）。
_trae_mod = importlib.import_module('server.platforms.trae.adapter')
_combo_mod = importlib.import_module('server.platforms.combo.adapter')


class _DbCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'realm.db'
        db._conn = None
        db.connect()
        db.execute(
            "INSERT INTO api_keys(name,key_hash,prefix,enabled,created_at,quota,used_tokens) "
            "VALUES('k','h','wbk_test',1,?,0,0)",
            (int(time.time()),),
        )
        self.key = {'id': db.query_one('SELECT id FROM api_keys LIMIT 1')['id'],
                    'realm': '', 'models': [], 'upstream_id': None}

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass


class RecordRealmTest(_DbCase):
    """`_record` 的版本优先级：显式 hint > 模型名前缀。"""

    def _record(self, **kw) -> None:
        gateway._record(self.key, '10.0.0.1', 'trae/glm-5.3', 'trae/glm-5.3',
                        200, 10, 5, 12, 'UA', None, False, **kw)

    def test_explicit_realm_wins(self) -> None:
        self._record(realm='global')
        log = db.query_one('SELECT realm, platform FROM request_logs')
        self.assertEqual(log['realm'], 'global')
        self.assertEqual(log['platform'], 'trae')
        usage = db.query_one('SELECT realm, platform FROM usage_daily')
        self.assertEqual((usage['realm'], usage['platform']), ('global', 'trae'),
                         '用量汇总也必须落在国际版，而不是按模型名判成 cn')

    def test_without_hint_falls_back_to_prefix(self) -> None:
        """WorkBuddy / 组合成员不传 realm：旧口径保持不变。"""
        self._record()
        self.assertEqual(db.query_one('SELECT realm FROM request_logs')['realm'], 'cn')

    def test_workbuddy_global_prefix_still_works(self) -> None:
        gateway._record(self.key, '10.0.0.1', 'global:gpt-5.6-sol',
                        'global:gpt-5.6-sol', 200, 1, 1, 1, 'UA', None, False)
        self.assertEqual(db.query_one('SELECT realm FROM request_logs')['realm'], 'global')


class _FakeAdapter:
    name = 'trae'
    label = 'Trae'
    realm_aware = True

    def __init__(self, realm: str | None, fail: PlatformError | None = None) -> None:
        self._realm = realm
        self._fail = fail

    def record_name(self, bare: str) -> str:
        return f'trae/{bare}'

    def realm_of_model(self, bare: str) -> str | None:
        return self._realm

    async def chat(self, model, body, ctx):
        if self._fail is not None:
            raise self._fail
        return PlatformChat(payload={'choices': []}, usage={'prompt_tokens': 1,
                                                            'completion_tokens': 2},
                            model=f'trae/{model}', realm=self._realm,
                            account_hint='uid-intl')


class PlatformChatRealmPlumbingTest(_DbCase):
    """`_platform_chat` 把适配器回报的版本带进记账（成功与失败两条路）。"""

    def _call(self, adapter) -> None:
        asyncio.run(gateway._platform_chat(
            adapter=adapter, bare_model='glm-5.3',
            body={'model': 'trae/glm-5.3', 'messages': []}, key=self.key,
            ip='10.0.0.1', requested_model='trae/glm-5.3', ua=None,
            stream=False, effort=None,
            ctx=platforms.ChatContext(key=self.key, ip='10.0.0.1', ua=None,
                                      upstream_path='/v1/chat/completions')))

    def test_success_passes_account_realm(self) -> None:
        with mock.patch.object(gateway, '_record') as record:
            self._call(_FakeAdapter('global'))
        self.assertEqual(record.call_args.kwargs['realm'], 'global')

    def test_failure_uses_catalog_realm(self) -> None:
        with mock.patch.object(gateway, '_record') as record:
            self._call(_FakeAdapter('global', PlatformError('boom', status=502)))
        self.assertEqual(record.call_args.kwargs['realm'], 'global',
                         '还没选到账号就失败时，至少按目录把日志归到正确的版本')

    def test_combo_passes_through_member_realm(self) -> None:
        """组合不自己判版本：成员适配器报什么就传什么。"""
        _install_combo('trae', 'glm-5.3')
        adapter = platforms.get('combo')

        def fake_get(name: str):
            return _FakeAdapter('global') if name == 'trae' else platforms.get(name)

        with mock.patch.object(_combo_mod, 'get_platform', fake_get), \
                mock.patch.object(gateway, '_record') as record:
            asyncio.run(gateway._platform_chat(
                adapter=adapter, bare_model='fast',
                body={'model': 'combo/fast', 'messages': []}, key=self.key,
                ip='10.0.0.1', requested_model='combo/fast', ua=None,
                stream=False, effort=None,
                ctx=platforms.ChatContext(key=self.key, ip='10.0.0.1', ua=None,
                                          upstream_path='/v1/chat/completions')))
        self.assertEqual(record.call_args.kwargs['realm'], 'global')


def _install_combo(platform: str, model: str) -> None:
    db.set_setting('model_combos', [{
        'name': 'fast', 'enabled': True,
        'members': [{'platform': platform, 'model': model, 'weight': 1,
                     'enabled': True}],
    }])


class TraeRealmMappingTest(unittest.TestCase):
    """Trae 的 `intl` → 统计口径的 `global`，转换只在一处。"""

    def test_db_realm_mapping(self) -> None:
        self.assertEqual(_trae_mod._db_realm('intl'), 'global')
        self.assertEqual(_trae_mod._db_realm('global'), 'global')
        self.assertEqual(_trae_mod._db_realm('cn'), 'cn')
        self.assertEqual(_trae_mod._db_realm(None), 'cn')

    def test_realm_of_model_uses_catalog(self) -> None:
        with mock.patch.object(_trae_mod.catalog, 'find',
                               return_value={'realm': 'intl'}):
            self.assertEqual(_trae_mod.adapter.realm_of_model('glm-5.3'), 'global')
        with mock.patch.object(_trae_mod.catalog, 'find', return_value=None):
            self.assertIsNone(_trae_mod.adapter.realm_of_model('unknown'))


class HistoryBackfillTest(_DbCase):
    """旧行按账号版本回填，并按平台重建汇总。"""

    def _log(self, account: str, realm: str, model: str = 'trae/glm-5.3') -> None:
        db.add_request_log(
            ts=int(time.time()), key_id=self.key['id'], ip='1.1.1.1', model=model,
            mapped_model=model, status=200, prompt_tokens=10, completion_tokens=5,
            latency_ms=10, first_token_ms=None, ua='UA', error=None, stream=0,
            credit=0.2, realm=realm, account=account, platform='trae',
        )

    def test_reassigns_logs_and_rebuilds_usage(self) -> None:
        self._log('uid-intl', 'cn')
        self._log('uid-cn', 'cn')
        # 旧汇总：两个账号被合到了一起（同名模型、同一个 cn）
        db.bump_usage(self.key['id'], 'trae/glm-5.3', 20, 10, credit=0.4,
                      realm='cn', platform='trae')

        out = db.reassign_platform_realms([('trae', 'uid-intl', 'global')])
        self.assertEqual(out['logs'], 1)

        by_account = {r['account']: r['realm']
                      for r in db.query('SELECT account, realm FROM request_logs')}
        self.assertEqual(by_account, {'uid-intl': 'global', 'uid-cn': 'cn'})

        rows = {r['realm']: r for r in db.query(
            'SELECT realm, requests, prompt_tokens, completion_tokens '
            'FROM usage_daily WHERE platform = ?', ('trae',))}
        self.assertEqual(set(rows), {'cn', 'global'},
                         '两个版本必须在汇总层分开，不能继续挤在 cn 一行')
        self.assertEqual(int(rows['global']['requests']), 1)
        self.assertEqual(int(rows['cn']['requests']), 1)
        self.assertEqual(int(rows['global']['prompt_tokens']), 10)

        hourly = {r['realm']: int(r['requests']) for r in db.query(
            'SELECT realm, SUM(requests) AS requests FROM usage_hourly '
            'WHERE platform = ? GROUP BY realm', ('trae',))}
        self.assertEqual(hourly, {'cn': 1, 'global': 1})

    def test_unknown_account_is_a_no_op(self) -> None:
        self._log('uid-cn', 'cn')
        out = db.reassign_platform_realms([('trae', 'nobody', 'global')])
        self.assertEqual(out['logs'], 0)
        self.assertEqual(db.query_one('SELECT realm FROM request_logs')['realm'], 'cn')

    def test_other_platforms_are_untouched(self) -> None:
        self._log('uid-intl', 'cn')
        db.bump_usage(self.key['id'], 'glm-5.3', 1, 1, credit=0.1,
                      realm='cn', platform='other')
        db.reassign_platform_realms([('trae', 'uid-intl', 'global')])
        self.assertEqual(
            db.query_one("SELECT COUNT(*) AS c FROM usage_daily WHERE platform = 'other'")['c'], 1)


if __name__ == '__main__':
    unittest.main()
