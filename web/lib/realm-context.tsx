'use client';

import {createContext, useCallback, useContext, useEffect, useMemo, useState} from 'react';

import {t as translate} from './i18n';
import {useI18n} from './i18n/provider';
import {WORKBUDDY_PLATFORM, usePlatform} from './platform-context';

/**
 * 版本（realm）切换。
 *
 * 上游是**单实例同时支持国内版与国际版**（共用账号池，按账号 realm 或模型名
 * 前缀路由），所以这里不做两套部署，只是切换「当前在看的/要操作的是哪一套」：
 *
 *   - 账号、模型、测试台、任务记录按版本过滤
 *   - 添加账号跟随：切到国际版时扫码走国际版端点 + 地区注册
 *   - 请求日志与用量是全局记录，不按版本过滤（页面另有标注）
 *
 * ## 每个平台各记各的版本
 *
 * WorkBuddy 的「国际版」和国际版 Trae 是**两件不相干的事**：账号池、端点、
 * 模型都不同。如果共用一个开关，用户把 WorkBuddy 切到国际版后再切到 Trae，
 * 就会莫名其妙地看到 Trae 国际版列表——反之亦然。所以版本按平台分别存储：
 *
 *   · WorkBuddy 沿用 `wb.realm`（升级前的键，老用户的选择不丢）；
 *   · 其它平台用 `wb.realm.<平台名>`。
 *
 * 切换平台时直接读该平台自己的值，不继承上一个平台的选择。
 *
 * ## `workbuddyRealm`：只属于 WorkBuddy 的那些页面
 *
 * `realm` 是**当前平台**的版本。但有些页面只服务 WorkBuddy（API 密钥的版本
 * 限制、签到 / 任务记录、扫码加账号），它们不该跟着顶部当前平台走：用户把
 * Trae 切到国际版后进「API 密钥」新建密钥，若默认版本跟着变成全球版，就会
 * 建出一把只能调 WorkBuddy 国际版模型的密钥——这正是「两个国际版互相干扰」
 * 的一种。所以另外暴露 `workbuddyRealm`（永远读 WorkBuddy 自己的键），
 * 只属于 WorkBuddy 的页面一律用它。
 *
 * 默认国内版：与上游「裸名默认 CN」的口径一致，也保证既有用户升级后
 * 看到的还是原来那套内容。
 */
export type Realm = 'cn' | 'global';

/** WorkBuddy 的存储键（升级前的键，保持不变）。 */
const WORKBUDDY_STORAGE_KEY = 'wb.realm';
/** 其它平台的存储键前缀：`wb.realm.trae`。 */
const PLATFORM_STORAGE_PREFIX = 'wb.realm.';

function storageKey(platform: string): string {
  return platform === WORKBUDDY_PLATFORM
    ? WORKBUDDY_STORAGE_KEY
    : `${PLATFORM_STORAGE_PREFIX}${platform}`;
}

function asRealm(value: string | null): Realm | null {
  return value === 'global' || value === 'cn' ? value : null;
}

interface RealmCtx {
  /** 当前平台的版本。平台相关页面（账号 / 模型 / 测试台 / 平台快照）用它。 */
  realm: Realm;
  setRealm: (r: Realm) => void;
  /** 当前平台版本的显示名，用于界面文案。 */
  label: string;
  /**
   * WorkBuddy 的版本，与当前平台无关。
   *
   * 只属于 WorkBuddy 的页面（密钥版本限制、签到 / 任务记录、扫码加账号）
   * 必须用它：Trae 的国际版不应该改变这些页面的默认值或查询范围。
   */
  workbuddyRealm: Realm;
  /** WorkBuddy 版本的显示名。 */
  workbuddyLabel: string;
}

const Ctx = createContext<RealmCtx | null>(null);

export function RealmProvider({children}: {children: React.ReactNode}) {
  const {platform} = usePlatform();
  /**
   * 各平台自己的版本。一次性把 localStorage 里所有 `wb.realm*` 读进来，
   * 这样切平台时**同步**就能拿到正确值，不会先用上一个平台的版本渲染一帧
   * 再去纠正（那一帧会带着错误的版本发请求 / 过滤列表）。
   */
  const [realms, setRealms] = useState<Record<string, Realm>>({});
  const {t} = useI18n();

  // 首屏后读 localStorage：直接读会让服务端渲染与客户端不一致
  useEffect(() => {
    const next: Record<string, Realm> = {};
    try {
      const wb = asRealm(window.localStorage.getItem(WORKBUDDY_STORAGE_KEY));
      if (wb) next[WORKBUDDY_PLATFORM] = wb;
      for (let i = 0; i < window.localStorage.length; i += 1) {
        const key = window.localStorage.key(i);
        if (!key || !key.startsWith(PLATFORM_STORAGE_PREFIX)) continue;
        const name = key.slice(PLATFORM_STORAGE_PREFIX.length);
        const value = asRealm(window.localStorage.getItem(key));
        if (name && value) next[name] = value;
      }
    } catch {
      /* 隐私模式下 localStorage 可能抛错，忽略即可（用默认值） */
    }
    setRealms(next);
  }, []);

  const realm = realms[platform] ?? 'cn';
  const workbuddyRealm = realms[WORKBUDDY_PLATFORM] ?? 'cn';

  const setRealm = useCallback((r: Realm) => {
    setRealms((prev) => ({...prev, [platform]: r}));
    try {
      window.localStorage.setItem(storageKey(platform), r);
    } catch {
      /* 同上：存不进就算了，不影响本次会话内的切换 */
    }
  }, [platform]);

  const value = useMemo<RealmCtx>(
    () => ({
      realm,
      setRealm,
      label: realm === 'global' ? t('realm.global') : t('realm.cn'),
      workbuddyRealm,
      workbuddyLabel: workbuddyRealm === 'global' ? t('realm.global') : t('realm.cn'),
    }),
    [realm, setRealm, t, workbuddyRealm],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useRealm(): RealmCtx {
  const v = useContext(Ctx);
  if (!v) throw new Error('useRealm 必须在 RealmProvider 内使用');
  return v;
}

/**
 * realm 的显示名（给不带 hook 的地方用，如表格标签）。
 * 读模块级当前语言（由 I18nProvider 同步）；调用它的组件在切换语言时
 * 会随父级重渲染，因此这里不必是 Hook。
 */
export function realmLabel(r: string | null | undefined): string {
  return r === 'global' ? translate('realm.global') : translate('realm.cn');
}
