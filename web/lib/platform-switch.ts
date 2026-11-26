/**
 * 顶部「平台切换」的下拉清单（纯函数）。
 *
 * Discovery / Kilo / Cline 的适配器仍然注册着：账号页、模型目录、网关前缀
 * （`discovery/…`、`kilo/…`、`cline/…`）与组合都还能用。这里藏掉的只是
 * **右上角下拉里的入口**——不让用户以为它们是本面板主推的平台，也避免
 * 误切过去后落到一个空账号页。
 *
 * ⚠️ 本模块**不得引入任何运行时 import**（只有类型 import，编译后被抹掉），
 * 原因同 `platform-account-list.ts`：测试用 Node 的 type stripping 直接
 * import `.ts`，引入别的模块的值会让测试找不到模块。
 */

/** 不在平台切换里露出的平台名（小写）。 */
export const SWITCH_HIDDEN_PLATFORMS: readonly string[] = [
  'discovery',
  'kilo',
  'cline',
];

/** 该平台是否不该出现在切换下拉里（大小写 / 空白不敏感）。 */
export function isSwitchHidden(name: string | null | undefined): boolean {
  const key = String(name ?? '').trim().toLowerCase();
  return SWITCH_HIDDEN_PLATFORMS.includes(key);
}

/**
 * 过滤平台清单，保持后端给出的顺序。
 *
 * 用泛型而不是 `PlatformInfo`：调用方传什么形状就回什么形状，测试也不必
 * 为了一个过滤函数去构造完整的平台能力对象。
 */
export function filterSwitchablePlatforms<T extends {name: string}>(
  platforms: readonly T[],
): T[] {
  return platforms.filter((platform) => !isSwitchHidden(platform.name));
}