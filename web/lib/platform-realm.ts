/**
 * 平台账号 / 模型的**版本（realm）映射**（纯函数）。
 *
 * 界面与后端用了两个不同的版本名：
 *
 *   · 界面（`lib/realm-context`）：`cn` / `global` —— 与 WorkBuddy 一致，
 *     切换控件上写的是「国内版 / 国际版」；
 *   · 平台适配器（`server/platforms/trae`）：`cn` / `intl` —— 账号文件里
 *     存的就是它。
 *
 * 所以「当前看哪个版本」在跨过前后端边界时必须翻译一次，而且只能翻译一次：
 * 少了这层映射，国际版会拿 `global` 去比对 `intl`，列表永远是空的；多翻一次
 * 又会把 `intl` 当成未知值退回国内版。这个模块就是那条唯一的边界。
 *
 * ⚠️ 本模块**不得引入任何运行时 import**（只有 `import type`，编译后被抹掉），
 * 原因同 `platform-account-list.ts`：测试用 Node 的 type stripping 直接 import
 * `.ts`，引入别的模块的值会让测试找不到模块。
 */
import type {Realm} from './realm-context';

/** 平台适配器侧实际存储的版本名。 */
export type PlatformRealm = 'cn' | 'intl';

/**
 * 界面版本 / 账号版本 → 平台版本。
 *
 * 认不出的值一律当作 `cn`：与后端 `normalize_realm` 的默认值一致，存量账号
 * 没有 `realm` 字段时也是国内版。
 */
export function platformRealmOf(realm: Realm | string | null | undefined): PlatformRealm {
  const value = String(realm ?? '').trim().toLowerCase();
  return value === 'global' || value === 'intl' ? 'intl' : 'cn';
}

/** 账号 / 模型的 realm 是否属于当前界面版本。 */
export function platformRealmMatches(
  accountRealm: string | null | undefined,
  realm: Realm | string | null | undefined,
): boolean {
  return platformRealmOf(accountRealm) === platformRealmOf(realm);
}

/**
 * 模型声明的 `realms` 数组是否包含当前界面版本。
 *
 * 分版本平台的模型列表**按区域各列一条**：同一条目只会带自己所属的区域
 * （`realms: ['cn']` 或 `['intl']`）。保留数组判断是为了兼容旧接口返回的
 * 双区合并条目（`realms: ['cn', 'intl']`）与手工导入的数据。
 */
export function platformRealmsMatch(
  realms: readonly string[] | null | undefined,
  realm: Realm | string | null | undefined,
): boolean {
  if (!Array.isArray(realms) || realms.length === 0) return false;
  const want = platformRealmOf(realm);
  return realms.some((item) => platformRealmOf(item) === want);
}

/** 按当前界面版本过滤账号或模型（缺 `realm` 的按国内版算）。 */
export function filterByPlatformRealm<
  T extends {realm?: string | null; realms?: readonly string[] | null},
>(
  items: readonly T[],
  realm: Realm | string | null | undefined,
): T[] {
  return items.filter((item) => (
    Array.isArray(item.realms) && item.realms.length > 0
      ? platformRealmsMatch(item.realms, realm)
      : platformRealmMatches(item.realm, realm)
  ));
}

/**
 * 手工导入时给缺失 `realm` 的条目补上当前版本。
 *
 * 后端 `import_account` 会把缺失的 realm 归到国内版；用户在国际版视图里粘贴
 * 一份不带 realm 的账号 JSON 时，若不在前端补上，账号会被悄悄导进国内版池。
 * 已经写了 realm 的条目保持原样——那是用户的显式选择。
 *
 * 接受 `{...}`、`{accounts: [...]}` 与裸数组三种形状，与后端的导入接口一致。
 */
export function withRealmDefault(
  payload: unknown,
  realm: Realm | string | null | undefined,
): unknown {
  const target = platformRealmOf(realm);
  const stamp = (item: unknown): unknown => {
    if (!item || typeof item !== 'object' || Array.isArray(item)) return item;
    const object = item as Record<string, unknown>;
    if (String(object.realm ?? '').trim()) return object;
    return {...object, realm: target};
  };
  if (Array.isArray(payload)) return payload.map(stamp);
  if (payload && typeof payload === 'object') {
    const object = payload as Record<string, unknown>;
    if (Array.isArray(object.accounts)) {
      return {...object, accounts: object.accounts.map(stamp)};
    }
    return stamp(object);
  }
  return payload;
}
