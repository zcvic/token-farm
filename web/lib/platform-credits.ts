/**
 * 平台积分明细的展示口径（纯函数）。
 *
 * 后端已经按「剩余额度 > 0 且尚未到期」筛过一遍，这里再做一次是刻意的：
 * 页面停留期间积分会继续消耗、包也会到期，而列表不会每分钟重新请求；不
 * 在前端丢掉这些包，用户就会看到「明明已经用完了 / 早过期了，还挂在明细里」。
 *
 * ⚠️ 与 `platform-account-list.ts` 同样的约束：**不得引入运行时 import**
 * （只有 `import type`），否则 `platform-credits.test.mjs` 在 Node 里直接
 * import 这份 `.ts` 会因找不到依赖而失败。
 */
import type {CreditExpiry, PlatformCredits, PlatformCreditsExpiry} from './types';

/** 一个包是否还值得展示：有余额、有到期时刻、且到期时刻还没到。 */
export function creditExpiryVisible(
  pack: PlatformCreditsExpiry,
  now: number = Date.now(),
): boolean {
  if (!pack || typeof pack.amount !== 'number' || !Number.isFinite(pack.amount)) {
    return false;
  }
  if (pack.amount <= 0) return false;
  if (typeof pack.expire_at !== 'number' || !Number.isFinite(pack.expire_at)) {
    return false;
  }
  return pack.expire_at * 1000 > now;
}

/**
 * 可见权益包的来源：实时查询失败时后端会附上上次登记的快照
 * （`last_expires`），这时照常画有效期，只是余额已由 `~` 标成旧值。
 * 实时成功但确实没有包（Loomy 只给快过期金额）时**不**回退，
 * 否则会把上一次查询的旧包当成现在的。
 */
function sourcePacks(
  entry: PlatformCredits | undefined | null,
): PlatformCreditsExpiry[] {
  const live = Array.isArray(entry?.expires) ? entry.expires : [];
  if (live.length) return live;
  const stale = entry
    && (entry.error || typeof entry.last_credits === 'number')
    && Array.isArray(entry.last_expires)
    ? entry.last_expires
    : [];
  return stale.length ? stale : live;
}

/**
 * 主数字的单位：`usd`（Kilo 的美元余额、Trae 国际版）还是 `credits`（积分）。
 *
 * 后端已经用 `unit` 表达了这个区别，前端**必须**照着画：把美元按积分显示
 * （`12.34` 读成「12 积分」）会让用户以为余额是另一个数量级。
 * 缺省 / 认不出的值按积分处理——那是绝大多数平台的单位，也是改造前的行为。
 */
export function creditUnit(
  entry: PlatformCredits | undefined | null,
): 'usd' | 'credits' {
  return entry?.unit === 'usd' ? 'usd' : 'credits';
}

/**
 * 条目自己的单位优先，缺省时才用整列的兜底口径。
 *
 * 为什么不能反着来：Trae 同时有国内版（积分）与国际版（美元）账号，账号一次
 * 全部取回、按版本在前端过滤。整列兜底若压过条目，国内版账号就会被画成
 * `$1234`——它登记的明明是积分。反过来，旧后端没给 `unit` 的条目仍要跟着
 * 整列走，否则同一列里数字与列头单位会不一致。
 */
export function creditUnitWithFallback(
  entry: PlatformCredits | undefined | null,
  fallback: 'usd' | 'credits' = 'credits',
): 'usd' | 'credits' {
  return (entry?.unit ?? fallback) === 'usd' ? 'usd' : 'credits';
}

/**
 * 余额数字的展示文本（纯函数，不依赖 i18n 运行时）。
 *
 * 美元带 `$` 前缀并按量级给位数：余额常是 `0.0x` 量级，截成整数就等于显示
 * 成 0（与 `format.ts` 的 `fmtMoney` 同一口径，但那份依赖浏览器 locale，
 * 这里不能引入运行时 import）。
 * 积分保持原样（整数为主），不额外加千分位——改口径会让既有平台的显示变样。
 */
export function formatCreditValue(
  value: number,
  unit: 'usd' | 'credits' = 'credits',
): string {
  if (!Number.isFinite(value)) return '—';
  if (unit !== 'usd') return String(value);
  const abs = Math.abs(value);
  if (abs === 0) return '$0';
  if (abs >= 1) return `$${value.toFixed(2)}`;
  if (abs >= 0.0001) return `$${value.toFixed(4)}`;
  return `$${value.toExponential(1)}`;
}

/** 平台账号的可见权益包，按到期时间升序（最早的排最前）。 */
export function platformCreditPacks(
  entry: PlatformCredits | undefined | null,
  now: number = Date.now(),
): PlatformCreditsExpiry[] {
  const packs = sourcePacks(entry);
  return packs
    .filter((pack) => creditExpiryVisible(pack, now))
    .sort((a, b) => a.expire_at - b.expire_at);
}

/**
 * 转成账号页共用的 `CreditExpiry`。
 *
 * 为什么转：WorkBuddy 账号页的倒计时组件只认 `{at, amount}`。平台账号用同一份
 * 组件，两边的「多少分、还有多久到期」才是同一套视觉与文案，用户不用重新学。
 */
export function platformCreditExpiries(
  entry: PlatformCredits | undefined | null,
  now: number = Date.now(),
): CreditExpiry[] {
  return platformCreditPacks(entry, now).map((pack) => ({
    at: pack.expire_at,
    amount: pack.amount,
  }));
}
