/**
 * 平台账号列表的**检索 / 筛选 / 排序**（纯函数）。
 *
 * 为什么单独一个模块：平台账号页与 WorkBuddy 账号页是两套数据，但用户对
 * 「搜索、状态筛选、排序」的期待完全一样。把这些判定写进组件里，两边就会
 * 各写一份、各自漂移；抽成纯函数还能用 `platform-account-list.test.mjs`
 * 直接测「停用 / 冷却 / 过期」这些容易被漏掉的边界。
 *
 * ⚠️ 本模块**不得引入任何运行时 import**（只有 `import type`，编译后被抹掉）。
 * 这是仓库给「可在 Node 里直接测的前端逻辑」定的规矩，详见 lib/account-status.ts
 * 开头的说明：测试用 Node 的 type stripping 直接 import `.ts`，一旦这里 import
 * 了别的模块的**值**，测试就会因找不到模块而失败。
 */
import type {PlatformAccount} from './types';

/** 平台账号的 4 组展示语义（与 WorkBuddy 账号页同构，便于用户迁移直觉）。 */
export type PlatformGroup = 'usable' | 'attention' | 'cooling' | 'stopped';

/** 固定顺序：先正常、再要处理的、最后是用户自己关的。 */
export const PLATFORM_GROUPS: readonly PlatformGroup[] = [
  'usable',
  'attention',
  'cooling',
  'stopped',
];

export type PlatformGroupFilter = PlatformGroup | 'all';

/**
 * 状态分档。优先级即「谁该被先说明」：
 *
 *   disabled → cooling → 凭据问题 → 正常
 *
 * 临时冻结放在最前：它必然导致账号不参与调度，不先判就会落到「过期 /
 * 异常」，把用户自己点的停用显示成故障。冷却只暂停调度、到期自愈，所以
 * 排在凭据问题之前——令牌过期是必须人工处理的事，不能被一个定时冷却盖住。
 *
 * `token_auto_refresh`（MiniMax Code）：它的 access token 只有约 1 小时，但
 * 适配器会在每次使用前自己换一份，所以 `expiredAt` 过期**不代表**账号不可用。
 * 这类账号的「会不会掉线」看的是 refresh token 的 `refreshExpiredAt`——拿
 * access token 判会让闲置一小时的号全被标成「需处理」，用户白折腾一轮。
 */
export function platformAvailabilityOf(
  account: PlatformAccount,
  now: number = Date.now(),
): PlatformGroup {
  if (account.disabled) return 'stopped';
  // 「仅签到」同样不参与推理调度，归到「已停用」组（但徽章文案不同：
  // 它不是故障也不是全停，签到与平台任务照常）。
  if (account.checkin_only) return 'stopped';
  if (account.cooling && account.cooling_until * 1000 > now) return 'cooling';
  const expiryField = account.token_auto_refresh
    ? account.refreshExpiredAt
    : account.expiredAt;
  const expiredAt = Date.parse(expiryField);
  const tokenBad = !account.has_token
    || (Number.isFinite(expiredAt) && expiredAt <= now);
  if (tokenBad || account.lastRefreshError) return 'attention';
  return 'usable';
}

/** 关键词是否命中：昵称 / uid / 邮箱 / 版本名。 */
export function platformMatchesQuery(account: PlatformAccount, q: string): boolean {
  const kw = q.trim().toLowerCase();
  if (!kw) return true;
  return (
    (account.nickname || '').toLowerCase().includes(kw) ||
    account.uid.toLowerCase().includes(kw) ||
    (account.email || '').toLowerCase().includes(kw) ||
    (account.realm || '').toLowerCase().includes(kw) ||
    (account.realm_label || '').toLowerCase().includes(kw)
  );
}

/** 排序键 */
export type PlatformSortKey =
  /** 后端返回的顺序（版本 + uid），不动 */
  | 'default'
  /** 令牌最早过期的在前 —— 找「该去刷新」的账号 */
  | 'expires'
  /** 积分少的在前；**没有积分数据的排最后** */
  | 'credits';

/** 该账号真正决定「会不会掉线」的到期时间（epoch 毫秒，拿不到返回 NaN）。 */
export function platformExpiryOf(account: PlatformAccount): number {
  // 与 platformAvailabilityOf 同一口径：会自动续期的平台看 refresh token。
  const value = account.token_auto_refresh
    ? account.refreshExpiredAt
    : account.expiredAt;
  const ms = Date.parse(value);
  return Number.isFinite(ms) ? ms : NaN;
}

export interface PlatformListQuery {
  q: string;
  group: PlatformGroupFilter;
  sort: PlatformSortKey;
}

/** 筛选 + 排序。分页由调用方复用 `paginate`。 */
export function selectPlatformAccounts(
  accounts: readonly PlatformAccount[],
  query: PlatformListQuery,
  creditOf: (a: PlatformAccount) => number | null,
  now: number = Date.now(),
): PlatformAccount[] {
  const rows = accounts.filter(
    (a) =>
      (query.group === 'all' || platformAvailabilityOf(a, now) === query.group)
      && platformMatchesQuery(a, query.q),
  );
  if (query.sort === 'default') return rows;

  if (query.sort === 'expires') {
    return [...rows].sort((a, b) => {
      const at = platformExpiryOf(a);
      const bt = platformExpiryOf(b);
      if (Number.isNaN(at) && Number.isNaN(bt)) return 0;
      if (Number.isNaN(at)) return 1;
      if (Number.isNaN(bt)) return -1;
      return at - bt;
    });
  }

  return [...rows].sort((a, b) => {
    const ca = creditOf(a);
    const cb = creditOf(b);
    if (ca === null && cb === null) return 0;
    if (ca === null) return 1;
    if (cb === null) return -1;
    return ca - cb;
  });
}

/** 各状态组的条数（含 `all`），给筛选按钮上的数字用。 */
export function platformGroupCounts(
  accounts: readonly PlatformAccount[],
  now: number = Date.now(),
): Record<PlatformGroupFilter, number> {
  const out: Record<PlatformGroupFilter, number> = {
    all: accounts.length,
    usable: 0,
    attention: 0,
    cooling: 0,
    stopped: 0,
  };
  for (const a of accounts) out[platformAvailabilityOf(a, now)] += 1;
  return out;
}

/** 分档对应的文案键（与 WorkBuddy 账号页共用同一套组名）。 */
export function platformGroupLabelKey(group: PlatformGroup): string {
  switch (group) {
    case 'usable':
      return 'accounts.groupUsable';
    case 'attention':
      return 'accounts.groupAttention';
    case 'cooling':
      return 'accounts.groupCooling';
    case 'stopped':
      return 'accounts.groupStopped';
  }
}
