'use client';

import {useCallback, useEffect, useMemo, useState} from 'react';
import {
  CalendarClock,
  Gauge,
  Loader2,
  Network,
  RotateCcw,
  Save,
  ShieldCheck,
} from 'lucide-react';

import {LoadError} from '@/components/common/states/LoadError';
import {SkeletonBar} from '@/components/common/states/SkeletonBar';
import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {Switch} from '@/components/ui/switch';
import {errText, platformApi} from '@/lib/api';
import {useAuth} from '@/lib/auth-context';
import {useI18n} from '@/lib/i18n/provider';
import {notify} from '@/lib/toast';
import type {PlatformSettings} from '@/lib/types';

/**
 * 「设置 → 上游配置」在非 WorkBuddy 平台下显示的内容。
 *
 * 为什么放在这个 Tab 而不是新开一个「平台」Tab：用户对「上游配置」的心智是
 * 「这台机器的上游长什么样」。WorkBuddy 的上游是它自己的 `config.json`，Trae /
 * Qoder 的上游是平台池本身——两者是同一件事，只是字段不同。跟着顶部的平台
 * 切换换内容，比让用户记住「WorkBuddy 在这个 Tab、其它平台在另一个 Tab」更顺。
 *
 * 与账号页标题栏里那几个内联输入框的关系：那几项仍然能改（它们在同一份设置
 * 接口上），这里提供的是**完整视图**——池级并发、签到节奏、请求预算与平台
 * 专属开关放在一起，并且有明确的保存 / 撤销。两边改的是同一份数据。
 */
type Draft = {
  max_in_flight: string;
  checkin_concurrency: string;
  checkin_delay_min: string;
  checkin_delay_max: string;
  request_timeout: string;
  idle_timeout: string;
  max_accounts_try: string;
  session_affinity_ttl: string;
  session_affinity: boolean;
  desktop_sync: boolean;
};

type NumericField = Exclude<keyof Draft, 'session_affinity' | 'desktop_sync'>;

function draftOf(value: PlatformSettings): Draft {
  return {
    max_in_flight: String(value.max_in_flight),
    checkin_concurrency: String(value.checkin_concurrency),
    checkin_delay_min: String(value.checkin_delay_min),
    checkin_delay_max: String(value.checkin_delay_max),
    request_timeout: String(value.request_timeout),
    idle_timeout: String(value.idle_timeout),
    max_accounts_try: String(value.max_accounts_try),
    session_affinity_ttl: String(value.session_affinity_ttl),
    session_affinity: value.session_affinity,
    desktop_sync: value.desktop_sync,
  };
}

function asInt(raw: string): number | null {
  const text = raw.trim();
  if (!text) return null;
  const value = Number(text);
  return Number.isInteger(value) ? value : null;
}

export function PlatformConfigPanel({
  platform,
  label,
  reloadSignal = 0,
}: {
  platform: string;
  label: string;
  /** 外部「刷新」信号：值变化时以服务端为准重新拉一次。 */
  reloadSignal?: number;
}) {
  const {t} = useI18n();
  const {isAdmin} = useAuth();
  const [settings, setSettings] = useState<PlatformSettings | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const value = await platformApi.settings(platform);
      setSettings(value);
      setDraft(draftOf(value));
      setError('');
    } catch (e) {
      // 保留上一次的表单会让用户以为「当前平台的配置就是这些」；宁可整块
      // 换成错误 + 重试。切换平台时也会走到这里，旧平台的值必须清掉。
      setSettings(null);
      setDraft(null);
      setError(errText(e));
    } finally {
      setLoading(false);
    }
  }, [platform]);

  useEffect(() => {
    void load();
  }, [load, reloadSignal]);

  const caps = settings?.capabilities;
  const limits = settings?.runtime_limits;

  const dirty = useMemo(() => {
    if (!settings || !draft) return false;
    const base = draftOf(settings);
    return (Object.keys(base) as (keyof Draft)[]).some(
      (key) => base[key] !== draft[key]);
  }, [settings, draft]);

  function set<K extends keyof Draft>(key: K, value: Draft[K]) {
    setDraft((prev) => (prev ? {...prev, [key]: value} : prev));
  }

  function fieldError(message: string): string {
    return t('platforms.configInvalid', {message});
  }

  async function save() {
    if (!settings || !draft || saving || !isAdmin) return;
    const errors: string[] = [];

    const pool = asInt(draft.max_in_flight);
    if (pool === null || pool < settings.limits.min || pool > settings.limits.max) {
      errors.push(t('platforms.poolInvalid', {
        min: settings.limits.min, max: settings.limits.max,
      }));
    }

    const checkinLim = settings.checkin_limits;
    const concurrency = asInt(draft.checkin_concurrency);
    const delayMin = asInt(draft.checkin_delay_min);
    const delayMax = asInt(draft.checkin_delay_max);
    if (checkinLim) {
      const ok = concurrency !== null && delayMin !== null && delayMax !== null
        && concurrency >= checkinLim.concurrency_min
        && concurrency <= checkinLim.concurrency_max
        && delayMin >= checkinLim.delay_min && delayMin <= checkinLim.delay_max
        && delayMax >= checkinLim.delay_min && delayMax <= checkinLim.delay_max
        && delayMin <= delayMax;
      if (!ok) {
        errors.push(t('platforms.checkinPaceInvalid', {
          cmin: checkinLim.concurrency_min,
          cmax: checkinLim.concurrency_max,
          dmin: checkinLim.delay_min,
          dmax: checkinLim.delay_max,
        }));
      }
    }

    const numeric: {key: NumericField; limitKey: keyof PlatformSettings['runtime_limits'];
      shown: boolean; label: string}[] = [
      {key: 'request_timeout', limitKey: 'request_timeout',
       shown: !!caps?.request_timeout, label: t('platforms.configRequestTimeout')},
      {key: 'idle_timeout', limitKey: 'idle_timeout',
       shown: !!caps?.idle_timeout, label: t('platforms.configIdleTimeout')},
      {key: 'max_accounts_try', limitKey: 'max_accounts_try',
       shown: !!caps?.max_accounts_try, label: t('platforms.configMaxAccountsTry')},
      {key: 'session_affinity_ttl', limitKey: 'session_affinity_ttl',
       shown: !!caps?.session_affinity_ttl && draft.session_affinity,
       label: t('platforms.configAffinityTtl')},
    ];
    const values: Partial<Record<NumericField, number>> = {};
    for (const item of numeric) {
      if (!item.shown || !limits) continue;
      const value = asInt(draft[item.key]);
      const range = limits[item.limitKey];
      if (value === null || value < range.min || value > range.max) {
        errors.push(t('platforms.configRange', {
          label: item.label, min: range.min, max: range.max,
        }));
      } else {
        values[item.key] = value;
      }
    }

    if (errors.length) {
      notify.err(fieldError(errors[0]));
      return;
    }

    setSaving(true);
    try {
      const body: Parameters<typeof platformApi.saveSettings>[1] = {
        max_in_flight: pool as number,
        checkin_concurrency: concurrency as number,
        checkin_delay_min: delayMin as number,
        checkin_delay_max: delayMax as number,
        ...values,
      };
      if (caps?.session_affinity) body.session_affinity = draft.session_affinity;
      if (caps?.desktop_sync) body.desktop_sync = draft.desktop_sync;
      const next = await platformApi.saveSettings(platform, body);
      setSettings(next);
      setDraft(draftOf(next));
      notify.ok(t('platforms.configSaved'));
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setSaving(false);
    }
  }

  if (loading && !settings) {
    return (
      <div className="space-y-3">
        <SkeletonBar />
        <SkeletonBar />
        <SkeletonBar />
      </div>
    );
  }

  if (error && !settings) {
    return <LoadError variant="inline" message={error} onRetry={load} />;
  }

  if (!settings || !draft) return null;

  const input = (key: NumericField, range: {min: number; max: number},
                 aria: string, hint?: string) => (
    <div className="rounded-2xl bg-background/60 px-3 py-2">
      <Label className="text-xs">{aria}</Label>
      <div className="mt-1 flex items-center gap-2">
        <Input
          value={draft[key]}
          onChange={(e) => set(key, e.target.value)}
          inputMode="numeric"
          disabled={!isAdmin || saving}
          aria-label={aria}
          className="h-8 w-24 bg-background text-right text-xs tabular-nums"
        />
        <span className="text-[11px] text-muted-foreground">
          {t('platforms.configRangeHint', {min: range.min, max: range.max})}
        </span>
      </div>
      {hint && <p className="mt-1 text-[11px] leading-4 text-muted-foreground">{hint}</p>}
    </div>
  );

  return (
    <div className="space-y-4">
      {/* ── 说明：这些设置只作用于当前平台 ── */}
      <div className="rounded-[20px] bg-muted px-4 py-3">
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant="secondary" className="rounded-full text-[10px]">{label}</Badge>
          <span className="text-sm font-medium">{t('platforms.configTitle')}</span>
          {dirty && (
            <Badge variant="outline" className="rounded-full text-[10px]">
              {t('platforms.configDirty')}
            </Badge>
          )}
        </div>
        <p className="mt-1 text-[11px] leading-5 text-muted-foreground">
          {t('platforms.configDesc', {platform: label})}
        </p>
        {!isAdmin && (
          <p className="mt-1 text-[11px] leading-5 text-amber-600 dark:text-amber-400">
            {t('platforms.configReadonly')}
          </p>
        )}
      </div>

      {/* ── 调度：池级并发 + 签到节奏 ── */}
      <section className="rounded-[20px] bg-muted p-4">
        <div className="mb-3 flex items-center gap-2">
          <Gauge className="h-3.5 w-3.5 text-muted-foreground" />
          <span className="text-sm font-medium">{t('platforms.configSectionSchedule')}</span>
        </div>
        <div className="grid grid-cols-1 gap-2.5 lg:grid-cols-2">
          {input('max_in_flight', settings.limits, t('platforms.poolLimit'),
                 t('platforms.poolHint'))}
          <div className="rounded-2xl bg-background/60 px-3 py-2">
            <div className="flex items-center gap-1.5">
              <CalendarClock className="h-3.5 w-3.5 text-muted-foreground" />
              <Label className="text-xs">{t('platforms.checkinPace')}</Label>
            </div>
            {settings.checkin_limits && (
              <div className="mt-1 flex flex-wrap items-center gap-1.5">
                <Input
                  value={draft.checkin_concurrency}
                  onChange={(e) => set('checkin_concurrency', e.target.value)}
                  inputMode="numeric"
                  disabled={!isAdmin || saving}
                  aria-label={t('platforms.checkinConcurrency')}
                  className="h-8 w-14 bg-background text-right text-xs tabular-nums"
                />
                <span className="text-[11px] text-muted-foreground">
                  {t('platforms.checkinConcurrency')}
                </span>
                <span className="text-muted-foreground/40">·</span>
                <Input
                  value={draft.checkin_delay_min}
                  onChange={(e) => set('checkin_delay_min', e.target.value)}
                  inputMode="numeric"
                  disabled={!isAdmin || saving}
                  aria-label={t('platforms.checkinDelayMin')}
                  className="h-8 w-14 bg-background text-right text-xs tabular-nums"
                />
                <span className="text-muted-foreground/60">–</span>
                <Input
                  value={draft.checkin_delay_max}
                  onChange={(e) => set('checkin_delay_max', e.target.value)}
                  inputMode="numeric"
                  disabled={!isAdmin || saving}
                  aria-label={t('platforms.checkinDelayMax')}
                  className="h-8 w-14 bg-background text-right text-xs tabular-nums"
                />
                <span className="text-[11px] text-muted-foreground">
                  {t('platforms.checkinSeconds')}
                </span>
              </div>
            )}
            <p className="mt-1 text-[11px] leading-4 text-muted-foreground">
              {t('platforms.checkinPaceHint')}
            </p>
          </div>
        </div>
      </section>

      {/* ── 请求预算与换号 ── */}
      {(caps?.request_timeout || caps?.idle_timeout || caps?.max_accounts_try) && (
        <section className="rounded-[20px] bg-muted p-4">
          <div className="mb-3 flex items-center gap-2">
            <Network className="h-3.5 w-3.5 text-muted-foreground" />
            <span className="text-sm font-medium">{t('platforms.configSectionNetwork')}</span>
          </div>
          <div className="grid grid-cols-1 gap-2.5 lg:grid-cols-3">
            {caps?.request_timeout && limits
              && input('request_timeout', limits.request_timeout,
                       t('platforms.configRequestTimeout'),
                       t('platforms.configRequestTimeoutHint'))}
            {caps?.idle_timeout && limits
              && input('idle_timeout', limits.idle_timeout,
                       t('platforms.configIdleTimeout'),
                       t('platforms.configIdleTimeoutHint'))}
            {caps?.max_accounts_try && limits
              && input('max_accounts_try', limits.max_accounts_try,
                       t('platforms.configMaxAccountsTry'),
                       t('platforms.configMaxAccountsTryHint'))}
          </div>
        </section>
      )}

      {/* ── 平台专属：Trae 的会话亲和与桌面端回写 ── */}
      {(caps?.session_affinity || caps?.desktop_sync) && (
        <section className="rounded-[20px] bg-muted p-4">
          <div className="mb-3 flex items-center gap-2">
            <ShieldCheck className="h-3.5 w-3.5 text-muted-foreground" />
            <span className="text-sm font-medium">{t('platforms.configSectionTrae')}</span>
          </div>
          <div className="space-y-2.5">
            {caps?.session_affinity && (
              <div className="rounded-2xl bg-background/60 px-3 py-2">
                <div className="flex items-center justify-between gap-3">
                  <div className="min-w-0">
                    <Label className="text-xs">{t('platforms.configSessionAffinity')}</Label>
                    <p className="mt-0.5 text-[11px] leading-4 text-muted-foreground">
                      {t('platforms.configSessionAffinityHint')}
                    </p>
                  </div>
                  <Switch
                    checked={draft.session_affinity}
                    disabled={!isAdmin || saving}
                    aria-label={t('platforms.configSessionAffinity')}
                    onCheckedChange={(value) => set('session_affinity', value)}
                  />
                </div>
                {draft.session_affinity && caps?.session_affinity_ttl && limits && (
                  <div className="mt-2 border-t border-border/50 pt-2">
                    {input('session_affinity_ttl', limits.session_affinity_ttl,
                           t('platforms.configAffinityTtl'),
                           t('platforms.configAffinityTtlHint'))}
                  </div>
                )}
              </div>
            )}
            {caps?.desktop_sync && (
              <div className="flex items-center justify-between gap-3 rounded-2xl bg-background/60 px-3 py-2">
                <div className="min-w-0">
                  <Label className="text-xs">{t('platforms.configDesktopSync')}</Label>
                  <p className="mt-0.5 text-[11px] leading-4 text-muted-foreground">
                    {t('platforms.configDesktopSyncHint')}
                  </p>
                </div>
                <Switch
                  checked={draft.desktop_sync}
                  disabled={!isAdmin || saving}
                  aria-label={t('platforms.configDesktopSync')}
                  onCheckedChange={(value) => set('desktop_sync', value)}
                />
              </div>
            )}
          </div>
        </section>
      )}

      {/* ── 保存 / 撤销 ── */}
      <div className="flex flex-wrap items-center justify-end gap-2">
        {dirty && (
          <span className="mr-auto text-[11px] text-muted-foreground">
            {t('platforms.configDirty')}
          </span>
        )}
        <Button
          variant="outline"
          className="rounded-full"
          disabled={!dirty || saving || !isAdmin}
          onClick={() => setDraft(draftOf(settings))}
        >
          <RotateCcw />
          {t('platforms.configReset')}
        </Button>
        <Button
          className="rounded-full"
          disabled={!dirty || saving || !isAdmin}
          onClick={() => void save()}
        >
          {saving ? <Loader2 className="animate-spin" /> : <Save />}
          {t('platforms.configSave')}
        </Button>
      </div>
    </div>
  );
}
