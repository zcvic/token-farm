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
