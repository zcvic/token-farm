'use client';

import {useEffect, useState} from 'react';
import {notify} from '@/lib/toast';
import {useT} from '@/lib/i18n/provider';
import {accountApi, errText, platformApi} from '@/lib/api';
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';

/**
 * 与 WorkBuddy 账号页同一套取值：空串 = 跟随号池，直连 = `__direct__`。
 *
 * 真实线路名在控件里加 `route:` 前缀，而不是直接当下拉值：线路是可以叫
 * `default` / `__direct__` 的，不加前缀就会和两个内置选项撞成同一个值。
 */
const FOLLOW_POOL = 'default';
const DIRECT = '__direct__';
const ROUTE_PREFIX = 'route:';

/**
 * 平台账号的出口线路（国际站账号常需要境外出口）。
 *
 * 后端在路由层就把线路名对着「设置 → 代理」校验过，这里只负责选：
 *  · 跟随号池 = 交给池策略分配（默认）；
 *  · 直连 = 强制不经任何代理；
 *  · 指定线路 = 「设置 → 代理」里已启用的某条线路。
 *
 * 为什么做成弹窗而不是表格里的一列下拉：平台账号表已经很宽，线路是低频操作；
 * 弹窗还能把「选不到线路时先去代理页添加」讲清楚。
 */
export function PlatformProxyDialog({
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
  const [routes, setRoutes] = useState<string[] | null>(null);
  const [routesError, setRoutesError] = useState('');
  /** 直接存账号里的原始值：空串 = 跟随号池，`__direct__` = 直连，其余 = 线路名。 */
  const [value, setValue] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) return;
    setValue(account?.proxy || '');
    setBusy(false);
    setRoutes(null);
    setRoutesError('');
    let active = true;
    accountApi.proxies()
      .then((data) => { if (active) setRoutes(data.routes); })
      .catch((error) => {
        if (!active) return;
        // 拉不到线路表 ≠ 没有线路：保持 routes=null（不画「线路已失效」），
        // 只把原因说清楚，用户仍可保存「跟随号池 / 直连」。
        setRoutesError(errText(error));
      });
    return () => { active = false; };
  }, [open, account?.uid, account?.proxy]);

  /**
   * 下拉控件的值：内置两项各占一个哨兵，线路名统一加前缀。线路本身可以叫
   * `default` 或 `__direct__`，不加前缀就会和内置项撞成同一个值，选中它等于
   * 悄悄改成了「跟随号池 / 直连」。
   */
  const selectValue = value === '' ? FOLLOW_POOL
    : value === DIRECT ? DIRECT
      : ROUTE_PREFIX + value;

  /**
   * 当前绑定了一条线路，但它不在（或还没读到）线路表里。
   *
   * 仍要在下拉里补一项：Radix 的 Select 只显示命中项的文字，缺了这一项时
   * 弹窗一打开就是空白，用户会以为账号根本没绑线路。
   */
  const currentRoute = value !== '' && value !== DIRECT
    && !(routes ?? []).includes(value);
  /** 线路表读到了才敢说「已失效」；还在加载 / 拉取失败时只显示原名。 */
  const currentRouteLabel = routes === null
    ? value
    : `${value} (${t('platforms.proxyMissing')})`;

  async function save() {
    if (!account || busy) return;
    setBusy(true);
    try {
      await platformApi.updateAccount(platform, account.uid, {proxy: value});
      notify.ok(t('platforms.proxySaved'));
      onOpenChange(false);
      onSaved();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(false);
    }
  }

  /** 把下拉控件的值折回账号里的原始值。 */
  function pick(next: string) {
    if (next === FOLLOW_POOL) setValue('');
    else if (next === DIRECT) setValue(DIRECT);
    else if (next.startsWith(ROUTE_PREFIX)) setValue(next.slice(ROUTE_PREFIX.length));
    else setValue(next);
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-[440px]" showCloseButton>
        <DialogHeader>
          <DialogTitle>{t('platforms.proxyTitle')}</DialogTitle>
          <DialogDescription>
            {t('platforms.proxyDesc', {name: account?.nickname || account?.uid || ''})}
          </DialogDescription>
        </DialogHeader>
        <DialogBody className="max-h-[min(640px,calc(100dvh-12rem))]">
          <div className="space-y-2 px-6 pb-2">
            <Select value={selectValue} onValueChange={pick} disabled={busy}>
              <SelectTrigger className="w-full" aria-label={t('platforms.proxyLine')}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={FOLLOW_POOL}>{t('platforms.proxyFollowPool')}</SelectItem>
                <SelectItem value={DIRECT}>{t('platforms.proxyDirect')}</SelectItem>
                {currentRoute && (
                  <SelectItem value={ROUTE_PREFIX + value}>
                    {currentRouteLabel}
                  </SelectItem>
                )}
                {(routes ?? []).map((route) => (
                  <SelectItem key={route} value={ROUTE_PREFIX + route}>{route}</SelectItem>
                ))}
              </SelectContent>
            </Select>
            {routesError && (
              <p className="text-[11px] leading-relaxed text-amber-600 dark:text-amber-400">
                {t('platforms.proxyRoutesError', {error: routesError})}
              </p>
            )}
            {routes !== null && routes.length === 0 && !currentRoute && (
              <p className="text-[11px] leading-relaxed text-amber-600 dark:text-amber-400">
                {t('platforms.proxyNoRoutes')}
              </p>
            )}
            <p className="text-[11px] leading-relaxed text-muted-foreground">
              {t('platforms.proxyHint')}
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
