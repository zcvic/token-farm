'use client';

import {Info, Layers, Loader2} from 'lucide-react';
import {usePathname} from 'next/navigation';

import {BASE_PATH} from '@/lib/base-path';
import {useT} from '@/lib/i18n/provider';
import {usePlatform} from '@/lib/platform-context';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';

/**
 * 顶部平台切换（下拉）。
 *
 * 放在页面右上角与语言 / 版本并列：底栏是照 LDC 原样保留的，不往上加控件。
 * 默认 WorkBuddy；其它平台来自后端注册表，选中后「账号」与「模型」页换成
 * 该平台的数据（见 `@/lib/platform-context`）。
 *
 * compact 供移动端使用：只显示图标，避免窄屏被它占掉一行。
 */
export function PlatformToggle({compact = false}: {compact?: boolean}) {
  const {platform, setPlatform, options, loading, label} = usePlatform();
  const t = useT();

  return (
    <Select value={platform} onValueChange={setPlatform}>
      <SelectTrigger
        aria-label={t('platforms.switch')}
        title={t('platforms.switch')}
        className="h-6 w-auto gap-1.5 rounded-full border-border/60 bg-muted/60 px-2.5 text-[11px] font-medium"
      >
        {loading ? (
          <Loader2 className="h-3 w-3 shrink-0 animate-spin opacity-70" />
        ) : (
          <Layers className="h-3 w-3 shrink-0 opacity-70" />
        )}
        {compact ? <span className="sr-only">{label}</span> : <SelectValue />}
      </SelectTrigger>
      <SelectContent align="end" className="min-w-[11rem]">
        {options.map((option) => (
          <SelectItem key={option.name} value={option.name} className="text-xs">
            {option.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

/**
 * 非 WorkBuddy 平台下，给「不分平台」的页面一条说明。
 *
 * 为什么需要它：选了 Trae 之后，密钥、设置等页面仍是全局 / WorkBuddy 数据，
 * 但用户看到顶部写着 Trae，很容易以为整页都在看 Trae。与其把页面伪装成分平台，
 * 不如直接说清楚边界。
 *
 * 账号 / 模型 / 测试台 / 统计 / 日志 / 仪表盘已经按平台取数，不再显示这条。
 * 「设置 → 上游配置」也随平台切换（WorkBuddy 是上游 `config.json`，Trae /
 * Qoder 是各自平台的池级设置），因此同样不显示——其余设置 Tab 仍是全局数据。
 */
export function PlatformScopeNote() {
  const {isWorkbuddy, label} = usePlatform();
  const t = useT();
  const pathname = usePathname();

  if (isWorkbuddy) return null;

  const raw = (pathname || '').replace(/\/+$/, '');
  const prefix = (BASE_PATH || '').replace(/\/+$/, '');
  const path = prefix && raw.startsWith(prefix) ? raw.slice(prefix.length) : raw;
  if (['/accounts', '/models', '/playground', '/stats', '/logs', '/dashboard',
    '/settings/upstream'].includes(path)) {
    return null;
  }

  return (
    <div className="flex items-start gap-2 rounded-[16px] border border-sky-500/30 bg-sky-500/10 px-3.5 py-2.5 text-[11px] leading-5 text-sky-700 dark:text-sky-300">
      <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" />
      <span>{t('platforms.scopeNote', {platform: label})}</span>
    </div>
  );
}
