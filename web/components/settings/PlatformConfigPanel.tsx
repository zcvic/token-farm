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
  Timer,
  Wand2,
  Zap,
} from 'lucide-react';

import {LoadError} from '@/components/common/states/LoadError';
import {SkeletonBar} from '@/components/common/states/SkeletonBar';
import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {Switch} from '@/components/ui/switch';
import {errText, platformApi} from '@/lib/api';
import {useAuth} from '@/lib/auth-context';
import {useI18n} from '@/lib/i18n/provider';
import {notify} from '@/lib/toast';
import type {
  PlatformEnumField,
  PlatformNumericField,
  PlatformSettings,
  PlatformSwitchField,
} from '@/lib/types';

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
/**
 * 表单里的数值键：平台的数值参数 + 池级并发 / 签到节奏。
 *
 * 后四个属于别的模块（`limiter.py` / `checkinpacing.py`），不在
 * `PlatformNumericField` 里，但它们同一张表单、同一套「字符串 + 校验」流程。
 */
type NumericKey =
  | PlatformNumericField
  | 'max_in_flight' | 'checkin_concurrency'
  | 'checkin_delay_min' | 'checkin_delay_max';

/**
 * 表单草稿：数值统一存字符串（用户可能正停在「1」这种中间态），布尔与枚举
 * 原样存。用映射类型而不是逐个字段抄一遍，后端加参数时前端只要补类型。
 */
type Draft = Record<NumericKey, string>
  & Record<PlatformSwitchField, boolean>
  & Record<PlatformEnumField, string>;

/** 枚举设置里「不选任何值」对应空串；Radix 的 SelectItem 不接受空串，用哨兵转发。 */
const GATEWAY_DEFAULT = '__gateway_default__';

const NUMERIC_KEYS: readonly NumericKey[] = [
  'max_in_flight', 'checkin_concurrency', 'checkin_delay_min', 'checkin_delay_max',
  'request_timeout', 'idle_timeout', 'max_accounts_try', 'session_affinity_ttl',
  'catalog_ttl', 'catalog_retry_ttl', 'catalog_timeout',
  'catalog_backoff_base', 'catalog_backoff_max',
  'credits_ttl', 'credits_timeout', 'cooldown_429', 'cooldown_5xx',
  'solo_max_tokens',
];

const SWITCH_KEYS: readonly PlatformSwitchField[] = [
  'session_affinity', 'desktop_sync', 'web_tools',
  'native_tools', 'text_tool_rescue', 'text_tool_tags', 'tool_protocol_hint',
  'degenerate_call_error', 'rescue_tail_strict',
  'reasoning_effort', 'reasoning_as_content',
  'empty_output_error', 'strict_stream_end',
];

const ENUM_KEYS: readonly PlatformEnumField[] = [
  'tool_echo_mode', 'anthropic_thinking', 'structured_tool_history',
  'image_transcode', 'credit_meter',
];

/** 数值字段的界面名（校验报错要用），值是 i18n key 的后半段。 */
const NUMERIC_LABEL: Record<PlatformNumericField, string> = {
  request_timeout: 'configRequestTimeout',
  idle_timeout: 'configIdleTimeout',
  max_accounts_try: 'configMaxAccountsTry',
  session_affinity_ttl: 'configAffinityTtl',
  catalog_ttl: 'configCatalogTtl',
  catalog_retry_ttl: 'configCatalogRetryTtl',
  catalog_timeout: 'configCatalogTimeout',
  catalog_backoff_base: 'configCatalogBackoffBase',
  catalog_backoff_max: 'configCatalogBackoffMax',
  credits_ttl: 'configCreditsTtl',
  credits_timeout: 'configCreditsTimeout',
  cooldown_429: 'configCooldown429',
  cooldown_5xx: 'configCooldown5xx',
  solo_max_tokens: 'configSoloMaxTokens',
};

/** Trae 的协议开关：这些原先只能靠 `TF_TRAE_*` 环境变量改。 */
const PROTOCOL_SWITCHES: readonly (readonly [PlatformSwitchField, string])[] = [
  ['native_tools', 'configNativeTools'],
  ['text_tool_rescue', 'configTextToolRescue'],
  ['text_tool_tags', 'configTextToolTags'],
  ['tool_protocol_hint', 'configToolProtocolHint'],
  ['degenerate_call_error', 'configDegenerateCallError'],
  ['rescue_tail_strict', 'configRescueTailStrict'],
  ['reasoning_effort', 'configReasoningEffort'],
  ['reasoning_as_content', 'configReasoningAsContent'],
  ['empty_output_error', 'configEmptyOutputError'],
  ['strict_stream_end', 'configStrictStreamEnd'],
];

function draftOf(value: PlatformSettings): Draft {
  const draft = {} as Draft;
  for (const key of NUMERIC_KEYS) draft[key] = String(value[key]);
  for (const key of SWITCH_KEYS) draft[key] = !!value[key];
  for (const key of ENUM_KEYS) draft[key] = value[key] ?? '';
  return draft;
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

    // 数值字段：只校验本平台支持、且界面上真的会显示的那些。哪些字段进请求体
    // 也由能力表决定，新增字段（缓存 TTL / 冷却 / SOLO 预算…）不用回来补元组。
    const values: Partial<Record<PlatformNumericField, number>> = {};
    if (limits) {
      for (const key of Object.keys(limits) as PlatformNumericField[]) {
        if (!caps?.[key]) continue;
        // 会话亲和关掉时它的 TTL 不显示、也不提交：否则会把用户看不到的值写回库。
        if (key === 'session_affinity_ttl' && !draft.session_affinity) continue;
        const value = asInt(draft[key]);
        const range = limits[key];
        if (value === null || value < range.min || value > range.max) {
          errors.push(t('platforms.configRange', {
            label: t(`platforms.${NUMERIC_LABEL[key]}`),
            min: range.min,
            max: range.max,
          }));
        } else {
          values[key] = value;
        }
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
      // 布尔与枚举同理：能力表说支持才提交，避免「保存成功但后端根本没这个字段」。
      for (const key of SWITCH_KEYS) {
        if (caps?.[key]) body[key] = draft[key];
      }
      for (const key of ENUM_KEYS) {
        if (caps?.[key]) body[key] = draft[key];
      }
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

  const input = (key: NumericKey, range: {min: number; max: number},
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

  /** 只画「本平台支持且有范围」的数值字段，缺一个条件就整块不出现。 */
  const numField = (key: PlatformNumericField, label: string, hint?: string) =>
    (caps?.[key] && limits ? input(key, limits[key], label, hint) : null);

  const enumSelect = (
    key: PlatformEnumField,
    label: string, hint: string,
    optionLabel: (choice: string) => string = (choice) =>
      t(`platforms.opt_${choice || 'default'}`),
  ) => {
    const choices = settings.enum_choices?.[key] ?? [''];
    return (
      <div className="rounded-2xl bg-background/60 px-3 py-2">
        <Label className="text-xs">{label}</Label>
        <Select
          value={draft[key] || GATEWAY_DEFAULT}
          onValueChange={(value) => set(key, value === GATEWAY_DEFAULT ? '' : value)}
          disabled={!isAdmin || saving}
        >
          <SelectTrigger size="sm" className="mt-1 w-full" aria-label={label}>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {choices.map((choice) => (
              <SelectItem key={choice || GATEWAY_DEFAULT}
                          value={choice || GATEWAY_DEFAULT}>
                {optionLabel(choice)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <p className="mt-1 text-[11px] leading-4 text-muted-foreground">{hint}</p>
      </div>
    );
  };

  /** 一行开关：左侧标题 + 说明，右侧 Switch。协议开关用它排成两列。 */
  const switchRow = (key: PlatformSwitchField, label: string, hint?: string) => (
    <div className="rounded-2xl bg-background/60 px-3 py-2">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <Label className="text-xs">{label}</Label>
          {hint && (
            <p className="mt-0.5 text-[11px] leading-4 text-muted-foreground">{hint}</p>
          )}
        </div>
        <Switch
          checked={draft[key]}
          disabled={!isAdmin || saving}
          aria-label={label}
          onCheckedChange={(value) => set(key, value)}
        />
      </div>
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

      {/* ── Trae：目录 / 积分缓存与限流冷却 ── */}
      {(caps?.catalog_ttl || caps?.credits_ttl || caps?.cooldown_429) && (
        <section className="rounded-[20px] bg-muted p-4">
          <div className="mb-3 flex items-center gap-2">
            <Timer className="h-3.5 w-3.5 text-muted-foreground" />
            <span className="text-sm font-medium">{t('platforms.configSectionCache')}</span>
          </div>
          <p className="mb-2.5 text-[11px] leading-4 text-muted-foreground">
            {t('platforms.configCacheHint')}
          </p>
          <div className="grid grid-cols-1 gap-2.5 lg:grid-cols-3">
            {numField('catalog_ttl', t('platforms.configCatalogTtl'),
                      t('platforms.configCatalogTtlHint'))}
            {numField('catalog_retry_ttl', t('platforms.configCatalogRetryTtl'),
                      t('platforms.configCatalogRetryTtlHint'))}
            {numField('catalog_timeout', t('platforms.configCatalogTimeout'),
                      t('platforms.configCatalogTimeoutHint'))}
            {numField('catalog_backoff_base', t('platforms.configCatalogBackoffBase'),
                      t('platforms.configCatalogBackoffBaseHint'))}
            {numField('catalog_backoff_max', t('platforms.configCatalogBackoffMax'),
                      t('platforms.configCatalogBackoffMaxHint'))}
            {numField('credits_ttl', t('platforms.configCreditsTtl'),
                      t('platforms.configCreditsTtlHint'))}
            {numField('credits_timeout', t('platforms.configCreditsTimeout'),
                      t('platforms.configCreditsTimeoutHint'))}
            {numField('cooldown_429', t('platforms.configCooldown429'),
                      t('platforms.configCooldown429Hint'))}
            {numField('cooldown_5xx', t('platforms.configCooldown5xx'),
                      t('platforms.configCooldown5xxHint'))}
          </div>
        </section>
      )}

      {/* ── Trae：SOLO 输出预算与实扣积分口径 ── */}
      {(caps?.solo_max_tokens || caps?.credit_meter) && (
        <section className="rounded-[20px] bg-muted p-4">
          <div className="mb-3 flex items-center gap-2">
            <Zap className="h-3.5 w-3.5 text-muted-foreground" />
            <span className="text-sm font-medium">{t('platforms.configSectionMeter')}</span>
          </div>
          <div className="grid grid-cols-1 gap-2.5 lg:grid-cols-2">
            {numField('solo_max_tokens', t('platforms.configSoloMaxTokens'),
                      t('platforms.configSoloMaxTokensHint'))}
            {caps?.credit_meter && enumSelect(
              'credit_meter',
              t('platforms.configCreditMeter'),
              t('platforms.configCreditMeterHint'),
              (choice) => t(`platforms.creditMeter_${choice || 'default'}`))}
          </div>
        </section>
      )}

      {/* ── Qoder 协议兼容：网关侧的兜底开关 ── */}
      {(caps?.tool_echo_mode || caps?.anthropic_thinking
        || caps?.structured_tool_history || caps?.image_transcode
        || caps?.web_tools) && (
        <section className="rounded-[20px] bg-muted p-4">
          <div className="mb-3 flex items-center gap-2">
            <Wand2 className="h-3.5 w-3.5 text-muted-foreground" />
            <span className="text-sm font-medium">
              {t('platforms.configSectionCompat')}
            </span>
          </div>
          <p className="mb-2.5 text-[11px] leading-4 text-muted-foreground">
            {t('platforms.configCompatHint')}
          </p>
          <div className="grid grid-cols-1 gap-2.5 lg:grid-cols-2">
            {caps?.tool_echo_mode && enumSelect(
              'tool_echo_mode',
              t('platforms.configToolEchoMode'),
              t('platforms.configToolEchoModeHint'))}
            {caps?.anthropic_thinking && enumSelect(
              'anthropic_thinking',
              t('platforms.configAnthropicThinking'),
              t('platforms.configAnthropicThinkingHint'))}
            {caps?.structured_tool_history && enumSelect(
              'structured_tool_history',
              t('platforms.configStructuredToolHistory'),
              t('platforms.configStructuredToolHistoryHint'))}
            {caps?.image_transcode && enumSelect(
              'image_transcode',
              t('platforms.configImageTranscode'),
              t('platforms.configImageTranscodeHint'))}
          </div>
          {caps?.web_tools && (
            <div className="mt-2.5 rounded-2xl bg-background/60 px-3 py-2">
              <div className="flex items-center justify-between gap-3">
                <div className="min-w-0">
                  <Label className="text-xs">{t('platforms.configWebTools')}</Label>
                  <p className="mt-0.5 text-[11px] leading-4 text-muted-foreground">
                    {t('platforms.configWebToolsHint')}
                  </p>
                </div>
                <Switch
                  checked={draft.web_tools}
                  disabled={!isAdmin || saving}
                  aria-label={t('platforms.configWebTools')}
                  onCheckedChange={(value) => set('web_tools', value)}
                />
              </div>
              {draft.web_tools && (
                <p className="mt-1.5 border-t border-border/50 pt-1.5 text-[11px] leading-4 text-amber-600 dark:text-amber-400">
                  {t('platforms.configWebToolsWarning')}
                </p>
              )}
            </div>
          )}
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
            {caps?.desktop_sync && switchRow(
              'desktop_sync',
              t('platforms.configDesktopSync'),
              t('platforms.configDesktopSyncHint'))}
          </div>
        </section>
      )}

      {/* ── Trae 协议开关：原先只能用 TF_TRAE_* 环境变量改 ── */}
      {caps?.native_tools && (
        <section className="rounded-[20px] bg-muted p-4">
          <div className="mb-3 flex items-center gap-2">
            <Wand2 className="h-3.5 w-3.5 text-muted-foreground" />
            <span className="text-sm font-medium">
              {t('platforms.configSectionProtocol')}
            </span>
          </div>
          <p className="mb-2.5 text-[11px] leading-4 text-muted-foreground">
            {t('platforms.configProtocolHint')}
          </p>
          <div className="grid grid-cols-1 gap-2.5 lg:grid-cols-2">
            {PROTOCOL_SWITCHES.map(([key, label]) => (
              caps?.[key] ? (
                <div key={key}
                     className="flex items-center justify-between gap-3 rounded-2xl bg-background/60 px-3 py-2">
                  <Label className="text-xs">{t(`platforms.${label}`)}</Label>
                  <Switch
                    checked={draft[key]}
                    disabled={!isAdmin || saving}
                    aria-label={t(`platforms.${label}`)}
                    onCheckedChange={(value) => set(key, value)}
                  />
                </div>
              ) : null
            ))}
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
