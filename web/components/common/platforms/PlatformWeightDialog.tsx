'use client';

import {useEffect, useState} from 'react';
import {notify} from '@/lib/toast';
import {useT} from '@/lib/i18n/provider';
import {errText, platformApi} from '@/lib/api';
import type {PlatformAccount} from '@/lib/types';
import {Button} from '@/components/ui/button';
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/animate-ui/radix/dialog';
import {Input} from '@/components/ui/input';

/**
 * 平台账号权重（与 WorkBuddy 账号页的 `AccountWeightDialog` 同一口径）。
 *
 * 语义是乘数，不是优先级：2 = 同等条件下被选中的机会约为默认的两倍；
 * 0 / 留空 = 恢复默认 1.0。它只影响同一平台内多个账号之间的分摊比例，
 * 不会让低权重账号永远轮不到。
 */
export function PlatformWeightDialog({
  platform,
  account,
  open,
  onOpenChange,
  onSaved,
}: {
  platform: string;
  account: PlatformAccount | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSaved: () => void;
}) {
  const t = useT();
  const [value, setValue] = useState('1');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (open) {
      const w = Number(account?.weight || 0);
      setValue(w > 0 ? String(w) : '1');
      setBusy(false);
    }
  }, [open, account?.uid, account?.weight]);

  async function save() {
    if (!account || busy) return;
    const raw = value.trim();
    const weight = raw === '' ? 0 : Number(raw);
    if (!Number.isFinite(weight)) {
      notify.err(t('platforms.weightInvalid'));
      return;
    }
    if (weight !== 0 && weight !== 1 && (weight < 0.1 || weight > 100)) {
      notify.err(t('platforms.weightInvalid'));
      return;
    }
    setBusy(true);
    try {
      const {account: saved} = await platformApi.updateAccount(platform, account.uid, {
        weight: weight === 1 ? 0 : weight,
      });
      (saved.weight > 0 ? notify.ok : notify.info)(
        saved.weight > 0
          ? t('platforms.weightSavedWith', {weight: String(saved.weight)})
          : t('platforms.weightCleared'),
      );
      onOpenChange(false);
      onSaved();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-[420px]" showCloseButton>
        <DialogHeader>
          <DialogTitle>{t('platforms.weightTitle')}</DialogTitle>
          <DialogDescription>
            {t('platforms.weightDesc', {name: account?.nickname || account?.uid || ''})}
          </DialogDescription>
        </DialogHeader>
        <DialogBody className="max-h-[min(640px,calc(100dvh-12rem))]">
          <div className="space-y-2 px-6 pb-2">
            <Input
              value={value}
              onChange={(e) => setValue(e.target.value)}
              inputMode="decimal"
              placeholder="1"
              autoFocus
              onKeyDown={(e) => {
                if (e.key === 'Enter') void save();
              }}
            />
            <p className="text-[11px] leading-relaxed text-muted-foreground">
              {t('platforms.weightHint')}
            </p>
          </div>
        </DialogBody>
        <DialogFooter>
          <Button variant="outline" className="rounded-full" onClick={() => onOpenChange(false)}>
            {t('common.cancel')}
          </Button>
          <Button className="rounded-full" onClick={save} disabled={busy}>
            {t('common.save')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
