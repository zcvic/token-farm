/**
 * `web/lib/platform-account-list.ts` 的行为测试（Node 直接跑 .ts 源码）。
 *
 * 跑法（Node ≥ 22.6）：
 *     node --experimental-strip-types web/lib/platform-account-list.test.mjs
 * 或走 Python 包装：`python -m unittest server.tests.test_platform_account_list`
 *
 * 为什么值得单独测：平台账号页的搜索 / 筛选 / 排序全在客户端做，写错了不报错，
 * 只是列表看起来不对——例如把「冻结」显示成「令牌过期」、把没有积分数据的账号
 * 当成 0 分排到最前、或者排序顺手改掉入参数组导致界面不重渲染。
 */
import {
  PLATFORM_GROUPS,
  platformAvailabilityOf,
  platformGroupCounts,
  platformMatchesQuery,
  selectPlatformAccounts,
} from './platform-account-list.ts';

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

const NOW = Date.parse('2026-11-01T00:00:00Z');
const iso = (ms) => new Date(ms).toISOString();

/** 造一个平台账号；只填这一批测试真正用到的字段 */
const acct = (uid, extra = {}) => ({
  platform: 'trae',
  uid,
  nickname: uid,
  email: '',
  realm: 'cn',
  realm_label: '国内版',
  proxy: '',
  expiredAt: iso(NOW + 86400_000),
  refreshExpiredAt: '',
  has_token: true,
  has_refresh_token: true,
  token_preview: 'abc…1234',
  source: 'manual',
  createdAt: 0,
  updatedAt: 0,
  refreshedAt: '',
  lastRefreshError: '',
  fingerprint: {},
  weight: 0,
  disabled: false,
  disabled_reason: '',
  disabled_at: 0,
  cooling: false,
  cooling_until: 0,
  cool_remaining_sec: 0,
  cooling_reason: '',
  ...extra,
});

/* ── 状态分档 ─────────────────────────────────────────────────── */

check('正常账号 → usable',
  platformAvailabilityOf(acct('a'), NOW), 'usable');
check('临时冻结优先于一切 → stopped',
  platformAvailabilityOf(acct('a', {
    disabled: true, cooling: true, cooling_until: Math.floor(NOW / 1000) + 600,
    has_token: false,
  }), NOW), 'stopped');
check('冷却中 → cooling',
  platformAvailabilityOf(acct('a', {
    cooling: true, cooling_until: Math.floor(NOW / 1000) + 600,
  }), NOW), 'cooling');
check('冷却截止已过 → 回到正常',
  platformAvailabilityOf(acct('a', {
    cooling: true, cooling_until: Math.floor(NOW / 1000) - 1,
  }), NOW), 'usable');
check('令牌已过期 → attention',
  platformAvailabilityOf(acct('a', {expiredAt: iso(NOW - 1000)}), NOW), 'attention');
check('没有 access token → attention',
  platformAvailabilityOf(acct('a', {has_token: false}), NOW), 'attention');
check('刷新失败留痕 → attention',
  platformAvailabilityOf(acct('a', {lastRefreshError: 'refresh failed'}), NOW), 'attention');

/* ── 检索 ─────────────────────────────────────────────────────── */

check('空关键词命中一切',
  platformMatchesQuery(acct('a'), ''), true);
check('按昵称搜索',
  platformMatchesQuery(acct('a', {nickname: 'Alice'}), 'ali'), true);
check('按邮箱搜索',
  platformMatchesQuery(acct('a', {email: 'alice@example.com'}), 'example'), true);
check('按版本名搜索',
  platformMatchesQuery(acct('a', {realm_label: '国际版'}), '国际'), true);
check('不命中无关关键词',
  platformMatchesQuery(acct('a', {nickname: 'Alice'}), 'bob'), false);

/* ── 计数 ─────────────────────────────────────────────────────── */

const mixed = [
  acct('ok'),
  acct('frozen', {disabled: true}),
  acct('cool', {cooling: true, cooling_until: Math.floor(NOW / 1000) + 600}),
  acct('bad', {has_token: false}),
];
check('4 组计数',
  platformGroupCounts(mixed, NOW),
  {all: 4, usable: 1, attention: 1, cooling: 1, stopped: 1});
check('分组顺序固定',
  [...PLATFORM_GROUPS], ['usable', 'attention', 'cooling', 'stopped']);

/* ── 筛选 / 排序 ──────────────────────────────────────────────── */

const filters = {q: '', group: 'all', sort: 'default'};
const ids = (rows) => rows.map((a) => a.uid);
const creditOf = (a) => (typeof a.credits === 'number' ? a.credits : null);

check('默认排序不动顺序',
  ids(selectPlatformAccounts(mixed, filters, creditOf, NOW)),
  ['ok', 'frozen', 'cool', 'bad']);
check('状态筛选只留冷冻',
  ids(selectPlatformAccounts(mixed, {...filters, group: 'stopped'}, creditOf, NOW)),
  ['frozen']);

const byExpiry = [
  acct('later', {expiredAt: iso(NOW + 10 * 86400_000)}),
  acct('never', {expiredAt: ''}),
  acct('soon', {expiredAt: iso(NOW + 1000)}),
];
check('令牌最早过期在前，无有效期排最后',
  ids(selectPlatformAccounts(byExpiry, {...filters, sort: 'expires'}, creditOf, NOW)),
  ['soon', 'later', 'never']);

const byCredits = [
  acct('rich', {credits: 100}),
  acct('unknown'),
  acct('poor', {credits: 1}),
];
check('积分少的在前，未知积分排最后',
  ids(selectPlatformAccounts(byCredits, {...filters, sort: 'credits'}, creditOf, NOW)),
  ['poor', 'rich', 'unknown']);

const original = [...byExpiry];
selectPlatformAccounts(byExpiry, {...filters, sort: 'expires'}, creditOf, NOW);
check('排序不改入参数组', byExpiry.map((a) => a.uid), original.map((a) => a.uid));

if (failed) {
  console.log(`\n${failed} check(s) failed`);
  process.exit(1);
}
console.log('\nall passed');
