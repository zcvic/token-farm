/**
 * `web/lib/platform-realm.ts` 的行为测试（Node 直接跑 .ts 源码）。
 *
 * 跑法（Node ≥ 22.6）：
 *     node --experimental-strip-types web/lib/platform-realm.test.mjs
 * 或走 Python 包装：`python -m unittest server.tests.test_platform_realm`
 *
 * 为什么值得单独测：界面用 `global`、平台适配器用 `intl`，映射写错不会报错
 * ——切换版本后列表只是空的，或者导入的账号被悄悄塞进另一个版本的账号池。
 */
import {
  filterByPlatformRealm,
  platformRealmMatches,
  platformRealmOf,
  platformRealmsMatch,
  withRealmDefault,
} from './platform-realm.ts';

let failed = 0;

function check(name, got, want) {
  const g = JSON.stringify(got);
  const w = JSON.stringify(want);
  if (g === w) {
    console.log(`  ok  ${name}`);
  } else {
    console.log(`  FAIL ${name}\n       got  ${g}\n       want ${w}`);
    failed += 1;
  }
}

/* ── 版本映射 ─────────────────────────────────────────────────── */

check('国内版 → cn', platformRealmOf('cn'), 'cn');
check('界面国际版 → intl', platformRealmOf('global'), 'intl');
check('平台国际版保持不变', platformRealmOf('intl'), 'intl');
check('大小写与空白不影响', platformRealmOf(' Global '), 'intl');
check('缺失 / 未知值退回国内版', platformRealmOf(undefined), 'cn');
check('未知值退回国内版', platformRealmOf('us'), 'cn');

check('intl 账号属于界面国际版',
  platformRealmMatches('intl', 'global'), true);
check('cn 账号不属于界面国际版',
  platformRealmMatches('cn', 'global'), false);
check('cn 账号属于国内版',
  platformRealmMatches('cn', 'cn'), true);
check('缺失 realm 的存量账号按国内版算',
  platformRealmMatches(undefined, 'cn'), true);

/* ── 过滤 ─────────────────────────────────────────────────────── */

const accounts = [
  {uid: 'cn-1', realm: 'cn'},
  {uid: 'intl-1', realm: 'intl'},
  {uid: 'legacy'},
];
const ids = (rows) => rows.map((row) => row.uid);
check('国际版只留 intl',
  ids(filterByPlatformRealm(accounts, 'global')), ['intl-1']);
check('国内版只留 cn 与存量账号',
  ids(filterByPlatformRealm(accounts, 'cn')), ['cn-1', 'legacy']);

/* ── 双区模型（Qoder 的 realms 数组）────────────────────────── */

check('双区模型属于国际版',
  platformRealmsMatch(['cn', 'intl'], 'global'), true);
check('双区模型属于国内版',
  platformRealmsMatch(['cn', 'intl'], 'cn'), true);
check('仅国内版模型不属于国际版',
  platformRealmsMatch(['cn'], 'global'), false);
check('仅国际版模型属于国际版',
  platformRealmsMatch(['intl'], 'global'), true);
check('空数组不匹配',
  platformRealmsMatch([], 'cn'), false);
check('缺失数组不匹配',
  platformRealmsMatch(undefined, 'cn'), false);

const models = [
  {id: 'qoder/dual', realms: ['cn', 'intl']},
  {id: 'qoder/cn-only', realms: ['cn']},
  {id: 'qoder/intl-only', realm: 'intl'},
];
const modelIds = (rows) => rows.map((row) => row.id);
check('国际版模型列表：双区 + 仅国际版',
  modelIds(filterByPlatformRealm(models, 'global')),
  ['qoder/dual', 'qoder/intl-only']);
check('国内版模型列表：双区 + 仅国内版',
  modelIds(filterByPlatformRealm(models, 'cn')),
  ['qoder/dual', 'qoder/cn-only']);

/* ── 导入补版本 ───────────────────────────────────────────────── */

check('单账号补 intl',
  withRealmDefault({uid: 'a', token: 't'}, 'global'),
  {uid: 'a', token: 't', realm: 'intl'});
check('单账号补 cn',
  withRealmDefault({uid: 'a', token: 't'}, 'cn'),
  {uid: 'a', token: 't', realm: 'cn'});
check('已有 realm 不被覆盖',
  withRealmDefault({uid: 'a', realm: 'cn'}, 'global'),
  {uid: 'a', realm: 'cn'});
check('空 realm 视为缺失',
  withRealmDefault({uid: 'a', realm: ''}, 'global'),
  {uid: 'a', realm: 'intl'});
check('批量导入逐条补版本',
  withRealmDefault({accounts: [{uid: 'a'}, {uid: 'b', realm: 'cn'}]}, 'global'),
  {accounts: [{uid: 'a', realm: 'intl'}, {uid: 'b', realm: 'cn'}]});
check('裸数组逐条补版本',
  withRealmDefault([{uid: 'a'}, {uid: 'b'}], 'global'),
  [{uid: 'a', realm: 'intl'}, {uid: 'b', realm: 'intl'}]);
check('非对象原样返回', withRealmDefault('raw', 'global'), 'raw');

if (failed) {
  console.log(`\n${failed} check(s) failed`);
  process.exit(1);
}
console.log('\nall passed');
