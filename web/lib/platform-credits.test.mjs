/**
 * `web/lib/platform-credits.ts` 的行为测试（Node 直接跑 .ts 源码）。
 *
 * 跑法（Node ≥ 22.6）：
 *     node --experimental-strip-types web/lib/platform-credits.test.mjs
 * 或走 Python 包装：`python -m unittest server.tests.test_platform_credits`
 *
 * 为什么值得单独测：账号页的积分倒计时只画「最近一笔」并把其余放进悬停，
 * 筛错一个包不会报错，只会出现「已经用完 / 早过期了还挂在明细里」或者
 * 「合计比余额大」——用户按明细去花就对不上账。
 */
import {
  creditExpiryVisible,
  creditUnit,
  formatCreditValue,
  platformCreditExpiries,
  platformCreditPacks,
} from './platform-credits.ts';

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

const NOW = Date.parse('2026-11-18T00:00:00Z');
const at = (ms) => Math.floor(ms / 1000);

const entry = {
  uid: 'u1',
  expires: [
    {amount: 500, expire_at: at(NOW + 7 * 86400_000), label: '每月登录'},
    {amount: 0, expire_at: at(NOW + 30 * 86400_000), label: '已用完'},
    {amount: 300, expire_at: at(NOW - 86400_000), label: '已过期'},
    {amount: 4000, expire_at: at(NOW + 2 * 86400_000), label: '新用户包'},
    {amount: -5, expire_at: at(NOW + 30 * 86400_000), label: '坏数据'},
  ],
};

check('按到期时间升序', platformCreditPacks(entry, NOW).map((p) => p.expire_at), [
  at(NOW + 2 * 86400_000),
  at(NOW + 7 * 86400_000),
]);

check('丢掉用满 / 过期 / 负数的包', platformCreditPacks(entry, NOW).map((p) => p.amount), [
  4000, 500,
]);

check('转成 CreditExpiry 后字段是 at / amount', platformCreditExpiries(entry, NOW), [
  {at: at(NOW + 2 * 86400_000), amount: 4000},
  {at: at(NOW + 7 * 86400_000), amount: 500},
]);

check('没有明细时返回空数组', platformCreditExpiries({uid: 'u1'}, NOW), []);
check('条目为 null 也不抛', platformCreditExpiries(null, NOW), []);

// 查询失败回退：余额已经是旧值，有效期也该用同一份快照，而不是整块消失。
const stale = {
  uid: 'u1',
  error: '上游超时',
  last_credits: 900,
  last_registered_at: 1700000000,
  last_expires: [
    {amount: 900, expire_at: at(NOW + 3 * 86400_000), label: '每月登录'},
  ],
};
check('失败回退时沿用快照的到期明细', platformCreditExpiries(stale, NOW), [
  {at: at(NOW + 3 * 86400_000), amount: 900},
]);

// 实时成功但没有包（Loomy 只给快过期金额）时不能把旧包翻出来。
check('实时成功但无包时不回退旧明细', platformCreditExpiries({
  uid: 'u1',
  credits: 1200,
  registered_at: at(NOW),
  last_expires: [
    {amount: 900, expire_at: at(NOW + 3 * 86400_000), label: '每月登录'},
  ],
}, NOW), []);

check('恰好在到期的包不算可见', creditExpiryVisible(
  {amount: 1, expire_at: at(NOW), label: ''}, NOW), false);
check('未到期的包可见', creditExpiryVisible(
  {amount: 1, expire_at: at(NOW + 1000), label: ''}, NOW), true);

/* ── 单位：美元 vs 积分 ────────────────────────────────────────────
   Kilo 的余额是美元（`unit='usd'`），Trae 国际版也是。若按积分画，用户会
   把 $12.34 读成「12 积分」——数量级完全错。缺省 / 认不出的值必须退回积分，
   那是既有平台的单位，也是改造前的行为。 */
check('unit=usd 认成美元', creditUnit({uid: 'u1', unit: 'usd'}), 'usd');
check('unit=credits 是积分', creditUnit({uid: 'u1', unit: 'credits'}), 'credits');
check('没有 unit 时按积分', creditUnit({uid: 'u1'}), 'credits');
check('条目为 null 时按积分', creditUnit(null), 'credits');

check('积分原样显示', formatCreditValue(1234, 'credits'), '1234');
check('美元带符号并保留两位', formatCreditValue(12.34, 'usd'), '$12.34');
check('小额美元保留四位', formatCreditValue(0.0034, 'usd'), '$0.0034');
check('零美元不写成空', formatCreditValue(0, 'usd'), '$0');
check('极小额美元用科学计数', formatCreditValue(0.00001, 'usd'), '$1.0e-5');
check('非有限值给破折号', formatCreditValue(Number.NaN, 'usd'), '—');
check('单位缺省时按积分', formatCreditValue(42), '42');

if (failed) {
  console.log(`\n${failed} failed`);
  process.exit(1);
}
console.log('\nall passed');
