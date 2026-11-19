'use client';

/**
 * 组合模型的「会话粘性」设置弹窗。
 *
 * 组合是虚拟平台，没有自己的账号页，所以这组开关没地方挂；直接放在组合页
 * 更符合使用场景——用户就是在这里编排成员、也会在这里遇到「同一段对话被
 * 拆到多个平台」的问题。
 *
 * 后端把时长存成**秒**（`platforms.combo.session_affinity_ttl`，60–43200），
 * 界面按分钟展示：运维调这个值时想的是「半小时还是一小时」，不是 1800。
 */

import {useCallback, useEffect, useState} from 'react';
import {Loader2, ShieldCheck} from 'lucide-react';

import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/animate-ui/radix/dialog';
import {LoadError} from '@/components/common/states/LoadError';
import {SkeletonBar} from '@/components/common/states/SkeletonBar';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {Switch} from '@/components/ui/switch';
import {errText, platformApi} from '@/lib/api';
import {useT} from '@/lib/i18n/provider';
import {notify} from '@/lib/toast';
import type {PlatformSettings} from '@/lib/types';

/** 与后端 `SESSION_AFFINITY_TTL_MIN/MAX` 同一区间，只是换成分钟。 */
const TTL_MIN_MINUTES = 1;
const TTL_MAX_MINUTES = 12 * 60;

export function ComboAffinityDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const t = useT();
  const [settings, setSettings] = useState<PlatformSettings | null>(null);
  const [enabled, setEnabled] = useState(true);
  const [ttl, setTtl] = useState('30');
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const next = await platformApi.settings('combo');
      setSettings(next);
      setEnabled(next.session_affinity);
      setTtl(String(Math.round(next.session_affinity_ttl / 60)));
    } catch (e) {
      // 读失败就停在错误态：不能让用户对着上一次的旧值保存，
      // 那会把别的管理员刚改过的设置覆盖回去。
      setSettings(null);
      setError(errText(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    setSettings(null);
    setError('');
    void load();
  }, [open, load]);

  async function save() {
    if (saving || !settings) return;
    const minutes = Number(ttl.trim());
    if (!Number.isInteger(minutes)
        || minutes < TTL_MIN_MINUTES || minutes > TTL_MAX_MINUTES) {
      notify.err(t('combos.affinityInvalid', {
        min: TTL_MIN_MINUTES,
        max: TTL_MAX_MINUTES,
      }));
      return;
    }
    setSaving(true);
    try {
      await platformApi.saveSettings('combo', {
        session_affinity: enabled,
        session_affinity_ttl: minutes * 60,
      });
      notify.ok(t('combos.affinitySaved'));
      onOpenChange(false);
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-[460px]" showCloseButton>
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <ShieldCheck className="h-4 w-4 text-muted-foreground" />
            {t('combos.affinityTitle')}
          </DialogTitle>
          <DialogDescription>{t('combos.affinityDesc')}</DialogDescription>
        </DialogHeader>

        <DialogBody className="max-h-[min(640px,calc(100dvh-12rem))]">
          <div className="space-y-3 px-6 pb-2">
            {loading && (
              <div className="space-y-2">
                <SkeletonBar className="h-14 w-full rounded-2xl" />
                <SkeletonBar className="h-20 w-full rounded-2xl" />
              </div>
            )}

            {!loading && error && (
              <LoadError variant="inline" message={error} onRetry={load} />
            )}

            {!loading && settings && (
              <>
                <div className="flex items-start justify-between gap-3 rounded-2xl bg-muted px-3 py-2.5">
                  <div className="min-w-0">
                    <Label className="text-xs">
                      {t('combos.affinityEnable')}
                    </Label>
                    <p className="mt-0.5 text-[11px] leading-4 text-muted-foreground">
                      {t('combos.affinityEnableHint')}
                    </p>
                  </div>
                  <Switch
                    checked={enabled}
                    disabled={saving}
                    aria-label={t('combos.affinityEnable')}
                    onCheckedChange={setEnabled}
                  />
                </div>

                {enabled && (
                  <div className="rounded-2xl bg-muted px-3 py-2.5">
                    <Label className="text-xs">{t('combos.affinityTtl')}</Label>
                    <div className="mt-1.5 flex items-center gap-2">
                      <Input
                        value={ttl}
                        onChange={(e) => setTtl(e.target.value)}
                        inputMode="numeric"
                        disabled={saving}
                        aria-label={t('combos.affinityTtl')}
                        className="h-8 w-20 bg-background text-right text-xs tabular-nums"
                      />
                      <span className="text-[11px] text-muted-foreground">
                        {t('combos.affinityMinutes')}
                      </span>
                    </div>
                    <p className="mt-1 text-[11px] leading-4 text-muted-foreground">
                      {t('combos.affinityTtlHint', {
                        min: TTL_MIN_MINUTES,
                        max: TTL_MAX_MINUTES,
                      })}
                    </p>
                  </div>
                )}
              </>
            )}
          </div>
        </DialogBody>

        <DialogFooter>
          <Button
            variant="outline"
            className="rounded-full"
            disabled={saving}
            onClick={() => onOpenChange(false)}
          >
            {t('common.cancel')}
          </Button>
          <Button
            className="rounded-full"
            disabled={saving || loading || !settings}
            onClick={() => void save()}
          >
            {saving ? <Loader2 className="animate-spin" /> : null}
            {t('common.save')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
