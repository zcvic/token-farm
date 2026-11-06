'use client';

import {createContext, useCallback, useContext, useEffect, useMemo, useState} from 'react';

import {errText, platformApi} from './api';
import type {PlatformInfo} from './types';

/**
 * 平台切换。
 *
 * 面板同时接入了多套平台（WorkBuddy、Trae…），而「当前在看哪个平台」是一个
 * **全局、跨页面**的选择：账号页看平台账号、模型页看平台模型、仪表盘 / 统计 /
 * 日志按平台统计用量。因此它和版本（realm）一样放在页面右上角，而不是每页各
 * 写一个下拉。
 *
 * ## 为什么 WorkBuddy 不在 `/api/platforms` 里
 *
 * 后端注册表列的是「平台适配器」（目前是 Trae）。WorkBuddy 仍然委托给随包的
 * `workbuddy2api`，不由适配器承载，所以前端把它当成内置的第一项：默认值、
 * 永远存在、也不依赖后端清单是否取到。
 *
 * 后端清单取不到时（网络/权限），仍然只显示 WorkBuddy，不把选择器整个藏掉
 * ——那会让用户以为功能消失了。
 */
export const WORKBUDDY_PLATFORM = 'workbuddy';

const STORAGE_KEY = 'wb.platform';

export type PlatformOption = {
  name: string;
  label: string;
  /** 内置平台（WorkBuddy），不在后端清单里 */
  builtin: boolean;
  accountCount?: number;
};

interface PlatformCtx {
  /** 当前平台名；默认 `workbuddy` */
  platform: string;
  setPlatform: (name: string) => void;
  /** 当前平台显示名 */
  label: string;
  isWorkbuddy: boolean;
  /** 下拉里的全部选项（WorkBuddy 永远第一） */
  options: PlatformOption[];
  /** 当前平台的能力清单；WorkBuddy / 清单未取到时为 null */
  platformInfo: PlatformInfo | null;
  /** 平台清单是否还在加载 */
  loading: boolean;
  /** 平台清单取不到的原因（空串 = 没有错误） */
  error: string;
  /** 重新拉取平台清单 */
  refresh: () => Promise<void>;
}

const Ctx = createContext<PlatformCtx | null>(null);

const BUILTIN_OPTION: PlatformOption = {
  name: WORKBUDDY_PLATFORM,
  label: 'WorkBuddy',
  builtin: true,
};

function readSavedPlatform(): string {
  if (typeof window === 'undefined') return WORKBUDDY_PLATFORM;
  try {
    return window.localStorage.getItem(STORAGE_KEY) || WORKBUDDY_PLATFORM;
  } catch {
    /* 隐私模式下 localStorage 可能抛错：用默认值 */
    return WORKBUDDY_PLATFORM;
  }
}

export function PlatformProvider({children}: {children: React.ReactNode}) {
  const [platform, setPlatformState] = useState(WORKBUDDY_PLATFORM);
  const [platforms, setPlatforms] = useState<PlatformInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  /**
   * 清单是否**成功**取到过一次。
   *
   * 失败时不能拿「空清单」去校正当前平台：那会把用户上次选的 Trae 悄悄改回
   * WorkBuddy，而失败原因只是一次网络抖动。
   */
  const [loaded, setLoaded] = useState(false);

  // 首屏后读 localStorage：直接读会让服务端渲染与客户端不一致
  useEffect(() => {
    setPlatformState(readSavedPlatform());
  }, []);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const {platforms: list} = await platformApi.list();
      setPlatforms(list ?? []);
      setError('');
      setLoaded(true);
    } catch (e) {
      setError(errText(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // 保存的平台已经不在后端清单里（平台被移除 / 改名）：退回默认值。
  useEffect(() => {
    if (!loaded || platform === WORKBUDDY_PLATFORM) return;
    if (platforms.some((p) => p.name === platform)) return;
    setPlatformState(WORKBUDDY_PLATFORM);
    try {
      window.localStorage.setItem(STORAGE_KEY, WORKBUDDY_PLATFORM);
    } catch {
      /* 存不进就算了，本次会话内已经切回默认值 */
    }
  }, [loaded, platform, platforms]);

  const setPlatform = useCallback((name: string) => {
    setPlatformState(name);
    try {
      window.localStorage.setItem(STORAGE_KEY, name);
    } catch {
      /* 同上：不影响本次会话内的切换 */
    }
  }, []);

  const options = useMemo<PlatformOption[]>(() => [
    BUILTIN_OPTION,
    ...platforms
      .filter((p) => p.name !== WORKBUDDY_PLATFORM)
      .map((p) => ({
        name: p.name,
        label: p.label || p.name,
        builtin: false,
        accountCount: p.account_count,
      })),
  ], [platforms]);

  const value = useMemo<PlatformCtx>(() => {
    const current = options.find((p) => p.name === platform) ?? BUILTIN_OPTION;
    return {
      platform,
      setPlatform,
      label: current.label,
      isWorkbuddy: platform === WORKBUDDY_PLATFORM,
      options,
      platformInfo: platforms.find((p) => p.name === platform) ?? null,
      loading,
      error,
      refresh,
    };
  }, [error, loading, options, platform, platforms, refresh, setPlatform]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function usePlatform(): PlatformCtx {
  const v = useContext(Ctx);
  if (!v) throw new Error('usePlatform 必须在 PlatformProvider 内使用');
  return v;
}
