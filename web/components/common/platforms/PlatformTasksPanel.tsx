'use client';

import {useCallback, useEffect, useMemo, useState} from 'react';
import {
  CalendarCheck,
  Check,
  CircleAlert,
  Coins,
  Copy,
  Gift,
  Loader2,
  RefreshCw,
  Ticket,
  UserPlus,
} from 'lucide-react';

import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {SkeletonBar} from '@/components/common/states/SkeletonBar';
import {errText, platformApi} from '@/lib/api';
import {useAuth} from '@/lib/auth-context';
import {useT} from '@/lib/i18n/provider';
import {copyText, fmtDateTime} from '@/lib/format';
import {notify} from '@/lib/toast';
import {cn} from '@/lib/utils';
import type {
  PlatformAccount,
  PlatformInviteCode,
  PlatformTaskStatus,
} from '@/lib/types';

/**
 * 平台「任务」页（Loomy：签到 / 绑定邀请码 / 兑换兑换码）。
 *
 * 为什么单独一页而不是塞进账号表格：这三件事都是**按账号、低频、带输入**
 * 的动作（邀请码是一次性的，绑错无法解绑），和「看账号状态」不是一个
 * 心智模型；塞进表格只会让每行多出三个按钮和两个输入框。
 *
 * 账号选择放在这里而不是从外部传入：任务页要按账号切换，账号页的分页 /
 * 搜索 / 状态筛选对它没有意义，传进来反而要处理「当前页没有这个账号」。
 */
export function PlatformTasksPanel({
  platform,
  label,
  accounts,
}: {
  platform: string;
  label: string;
  accounts: PlatformAccount[];
}) {
  const t = useT();
  const {isAdmin} = useAuth();

  /** 有凭据的账号才能跑任务；冻结账号也允许（用户可能就是想签完再解冻）。 */
  const candidates = useMemo(
    () => accounts.filter((a) => a.has_token),
    [accounts],
  );

  const [uid, setUid] = useState('');
  // 账号列表变化（新增 / 删除 / 切平台）后，选中的号可能已经不在了。
  useEffect(() => {
    if (candidates.some((a) => a.uid === uid)) return;
    setUid(candidates[0]?.uid ?? '');
  }, [candidates, uid]);

  const [status, setStatus] = useState<PlatformTaskStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  /** 正在执行的动作名（checkin / bind_invite / redeem），空 = 空闲。 */
  const [busy, setBusy] = useState('');
  const [invite, setInvite] = useState('');
  const [redeem, setRedeem] = useState('');
  const [copied, setCopied] = useState('');

  const load = useCallback(async (target: string) => {
    if (!target) {
      setStatus(null);
      setError('');
      return;
    }
    setLoading(true);
    setError('');
    try {
      const {task} = await platformApi.taskStatus(platform, target);
      setStatus(task);
    } catch (e) {
      // 状态取不到不是致命错误：下面仍可以显式签到 / 绑定，只是看不到现状。
      setStatus(null);
      setError(errText(e));
    } finally {
      setLoading(false);
    }
  }, [platform]);

  useEffect(() => {
    setInvite('');
    setRedeem('');
    void load(uid);
  }, [uid, load]);

  const activation = status?.activation;
  const activated = activation?.activated === true;

  async function runAction(
    action: 'checkin' | 'bind_invite' | 'redeem',
    body?: {invite_code?: string; code?: string},
  ) {
    if (!uid || busy) return;
    setBusy(action);
    const pending = notify.info(t('common.loading'));
    try {
      const {task} = await platformApi.runTask(platform, uid, action, body);
      notify.dismiss(pending);
      if (action === 'bind_invite') {
        notify.ok(t('platforms.tasks.bindDone'), task.desc || undefined);
        setInvite('');
      } else if (action === 'redeem') {
        notify.ok(t('platforms.tasks.redeemDone'), task.desc || undefined);
        setRedeem('');
      } else if (task.checkin?.already) {
        notify.info(t('platforms.tasks.checkinAlready'));
      } else {
        notify.ok(t('platforms.tasks.checkinDone'),
          typeof task.checkin?.earned_credit === 'number'
            && task.checkin.earned_credit > 0
            ? t('platforms.checkinEarned', {n: task.checkin.earned_credit})
            : task.checkin?.message || undefined);
      }
      // 动作返回里带着最新状态（激活 / 签到缓存 / 邀请码），直接用，省一次往返。
      if (task.uid) setStatus((prev) => ({...(prev || {uid}), ...task} as PlatformTaskStatus));
      else void load(uid);
    } catch (e) {
      notify.dismiss(pending);
      notify.err(errText(e));
      // 绑定失败（已绑 / 邀请码无效）时刷新一次，让界面回到真实状态。
      void load(uid);
    } finally {
      setBusy('');
    }
  }

  async function copyCode(code: string) {
    if (!code) return;
    const ok = await copyText(code);
    if (ok) {
      setCopied(code);
      window.setTimeout(() => setCopied((v) => (v === code ? '' : v)), 1500);
      notify.ok(t('platforms.tasks.copied'));
    } else {
      notify.warn(t('platforms.tasks.copyFailed'));
    }
  }

  if (!candidates.length) {
    return (
      <section className="rounded-[20px] bg-muted p-8 text-center">
        <Gift className="mx-auto h-6 w-6 text-muted-foreground" />
        <p className="mt-2 text-xs font-medium">{t('platforms.tasks.noAccounts')}</p>
        <p className="mt-1 text-[11px] text-muted-foreground">
          {t('platforms.tasks.noAccountsDesc')}
        </p>
      </section>
    );
  }

  const checkin = status?.checkin;
  const codes: PlatformInviteCode[] = status?.invite_codes ?? [];

  return (
    <div className="flex flex-col gap-4">
      {/* 账号选择 + 刷新 */}
      <section className="flex flex-wrap items-center justify-between gap-2 rounded-[20px] bg-muted px-3.5 py-3">
        <div className="flex min-w-0 items-center gap-2">
          <Gift className="h-4 w-4 shrink-0 text-muted-foreground" />
          <h2 className="shrink-0 text-xs font-medium">
            {t('platforms.tasks.title')}
          </h2>
          <span className="truncate text-[11px] text-muted-foreground">· {label}</span>
        </div>
        <div className="flex items-center gap-1.5">
          <Select value={uid} onValueChange={setUid}>
            <SelectTrigger
              className="h-8 w-[190px] rounded-full text-[11px]"
              aria-label={t('platforms.tasks.selectAccount')}
            >
              <SelectValue placeholder={t('platforms.tasks.selectAccount')} />
            </SelectTrigger>
            <SelectContent>
              {candidates.map((a) => (
                <SelectItem key={a.uid} value={a.uid} className="text-xs">
                  {a.nickname || a.uid}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Button
            variant="ghost"
            size="sm"
            className="h-8 rounded-full px-2 text-[11px]"
            title={t('common.refresh')}
            disabled={loading}
            onClick={() => void load(uid)}
          >
            {loading
              ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
              : <RefreshCw className="h-3.5 w-3.5" />}
          </Button>
        </div>
      </section>

      {loading && !status ? (
        <section className="space-y-2 rounded-[20px] bg-muted px-3.5 py-4">
          <SkeletonBar className="h-4 w-40" />
          <SkeletonBar className="h-4 w-64" />
          <SkeletonBar className="h-4 w-52" />
        </section>
      ) : null}

      {error ? (
        <section className="flex items-start gap-2 rounded-[20px] bg-amber-500/10 px-3.5 py-3 text-[11px] leading-5 text-amber-700 ring-1 ring-amber-500/20 dark:text-amber-400">
          <CircleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span className="break-all">{error}</span>
        </section>
      ) : null}

      {/* ── 邀请码绑定 ─────────────────────────────────── */}
      <section className="overflow-hidden rounded-[20px] bg-muted">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border/60 px-3.5 py-3">
          <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
            <UserPlus className="h-3.5 w-3.5" />
            <h3 className="text-xs font-medium text-foreground">
              {t('platforms.tasks.activationTitle')}
            </h3>
            {activation && !activation.error && (
              <Badge
                variant="secondary"
                className={cn(
                  'rounded-md px-1.5 text-[10px]',
                  activated
                    ? 'text-emerald-700 dark:text-emerald-400'
                    : 'text-muted-foreground',
                )}
              >
                {activated
                  ? t('platforms.tasks.activationBound')
                  : t('platforms.tasks.activationUnbound')}
              </Badge>
            )}
          </div>
          {activated && activation?.applied_invitation_code ? (
            <span className="font-mono text-[11px] text-muted-foreground">
              {t('platforms.tasks.activationCode')}：
              {activation.applied_invitation_code}
            </span>
          ) : null}
        </div>
        <div className="space-y-2 px-3.5 py-3">
          <form
            className="flex flex-wrap items-center gap-1.5"
            onSubmit={(e) => {
              e.preventDefault();
              if (!isAdmin || activated) return;
              void runAction('bind_invite', {invite_code: invite.trim()});
            }}
          >
            <Input
              value={invite}
              onChange={(e) => setInvite(e.target.value)}
              placeholder={t('platforms.tasks.invitePlaceholder')}
              className="h-8 min-w-[180px] flex-1 rounded-xl text-xs"
              disabled={!isAdmin || activated || busy !== ''}
              autoComplete="off"
              spellCheck={false}
            />
            <Button
              type="submit"
              size="sm"
              className="h-8 shrink-0 rounded-full px-3 text-[11px]"
              disabled={!isAdmin || activated || busy !== '' || !invite.trim()}
            >
              {busy === 'bind_invite' ? <Loader2 className="animate-spin" /> : null}
              {activated ? t('platforms.tasks.activationBound') : t('platforms.tasks.bind')}
            </Button>
          </form>
          <p className="text-[11px] leading-5 text-muted-foreground">
            {activated
              ? t('platforms.tasks.activationBoundHint')
              : isAdmin
                ? t('platforms.tasks.activationHint')
                : t('platforms.tasks.adminOnly')}
          </p>
        </div>
      </section>

      {/* ── 签到 ───────────────────────────────────────── */}
      <section className="overflow-hidden rounded-[20px] bg-muted">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border/60 px-3.5 py-3">
          <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
            <CalendarCheck className="h-3.5 w-3.5" />
            <h3 className="text-xs font-medium text-foreground">
              {t('platforms.tasks.checkinTitle')}
            </h3>
            {status?.synced_at ? (
              <span className="text-[10px]">
                {t('platforms.tasks.syncedAt', {
                  when: fmtDateTime(status.synced_at * 1000),
                })}
              </span>
            ) : (
              <span className="text-[10px]">{t('platforms.tasks.neverSynced')}</span>
            )}
          </div>
          <Button
            size="sm"
            variant="outline"
            className="h-8 rounded-full px-3 text-[11px]"
            disabled={!isAdmin || busy !== ''}
            title={isAdmin ? t('platforms.tasks.checkinDesc') : t('platforms.tasks.adminOnly')}
            onClick={() => void runAction('checkin')}
          >
            {busy === 'checkin'
              ? <Loader2 className="animate-spin" />
              : <CalendarCheck />}
            {t('platforms.tasks.checkinRun')}
          </Button>
        </div>
        <div className="px-3.5 py-3">
          {checkin ? (
            <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-[11px] sm:grid-cols-4">
              <Metric label={t('platforms.tasks.balance')} value={checkin.balance} />
              <Metric label={t('platforms.tasks.permanent')} value={checkin.permanent} />
              <Metric
                label={t('platforms.tasks.daily')}
                value={
                  checkin.daily == null
                    ? null
                    : `${checkin.daily}${checkin.daily_quota == null ? '' : ` / ${checkin.daily_quota}`}`
                }
              />
              <Metric
                label={t('platforms.tasks.lastResult')}
                value={
                  checkin.already
                    ? t('platforms.tasks.checkinAlready')
                    : checkin.message || (checkin.ok
                      ? t('platforms.tasks.checkinDone')
                      : t('platforms.checkinFailed'))
                }
              />
            </div>
          ) : (
            <p className="text-[11px] text-muted-foreground">
              {t('platforms.tasks.checkinDesc')}
            </p>
          )}
        </div>
      </section>

      {/* ── 我的邀请码 ─────────────────────────────────── */}
      <section className="overflow-hidden rounded-[20px] bg-muted">
        <div className="flex items-center gap-2 border-b border-border/60 px-3.5 py-3 text-[11px] text-muted-foreground">
          <Ticket className="h-3.5 w-3.5" />
          <h3 className="text-xs font-medium text-foreground">
            {t('platforms.tasks.codesTitle')}
          </h3>
          {codes.length > 0 && (
            <Badge variant="secondary" className="rounded-md px-1.5 text-[10px] tabular-nums">
              {codes.length}
            </Badge>
          )}
        </div>
        {codes.length ? (
          <div className="divide-y divide-border/40">
            {codes.map((item, i) => {
              const code = String(item.inviteCode || '');
              const used = item.usedCount;
              const max = item.maxUses;
              return (
                <div
                  key={`${code}-${i}`}
                  className="flex flex-wrap items-center justify-between gap-2 px-3.5 py-2"
                >
                  <div className="flex min-w-0 items-center gap-2">
                    <span className="font-mono text-xs font-medium">{code || '—'}</span>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-6 rounded-full px-1.5 text-[10px]"
                      disabled={!code}
                      title={t('platforms.tasks.copyCode')}
                      onClick={() => void copyCode(code)}
                    >
                      {copied === code
                        ? <Check className="h-3 w-3 text-emerald-600" />
                        : <Copy className="h-3 w-3" />}
                    </Button>
                  </div>
                  <div className="flex shrink-0 items-center gap-2 text-[11px] text-muted-foreground">
                    {typeof used === 'number' || typeof max === 'number' ? (
                      <span className="tabular-nums">
                        {t('platforms.tasks.codeUsed', {
                          used: used ?? 0,
                          max: max ?? 0,
                        })}
                      </span>
                    ) : null}
                    {item.status ? (
                      <Badge
                        variant="secondary"
                        className={cn(
                          'rounded-md text-[10px]',
                          item.status === 'active'
                            ? 'text-emerald-700 dark:text-emerald-400'
                            : 'text-muted-foreground',
                        )}
                      >
                        {item.status === 'active'
                          ? t('platforms.tasks.codeStatusActive')
                          : item.status}
                      </Badge>
                    ) : null}
                  </div>
                </div>
              );
            })}
          </div>
        ) : (
          <div className="px-3.5 py-6 text-center text-[11px] text-muted-foreground">
            {t('platforms.tasks.codesEmpty')}
          </div>
        )}
      </section>

      {/* ── 兑换码 ─────────────────────────────────────── */}
      <section className="overflow-hidden rounded-[20px] bg-muted">
        <div className="flex items-center gap-2 border-b border-border/60 px-3.5 py-3 text-[11px] text-muted-foreground">
          <Coins className="h-3.5 w-3.5" />
          <h3 className="text-xs font-medium text-foreground">
            {t('platforms.tasks.redeemTitle')}
          </h3>
        </div>
        <div className="space-y-2 px-3.5 py-3">
          <form
            className="flex flex-wrap items-center gap-1.5"
            onSubmit={(e) => {
              e.preventDefault();
              if (!isAdmin) return;
              void runAction('redeem', {code: redeem.trim()});
            }}
          >
            <Input
              value={redeem}
              onChange={(e) => setRedeem(e.target.value)}
              placeholder={t('platforms.tasks.redeemPlaceholder')}
              className="h-8 min-w-[180px] flex-1 rounded-xl text-xs"
              disabled={!isAdmin || busy !== ''}
              autoComplete="off"
              spellCheck={false}
            />
            <Button
              type="submit"
              size="sm"
              className="h-8 shrink-0 rounded-full px-3 text-[11px]"
              disabled={!isAdmin || busy !== '' || !redeem.trim()}
            >
              {busy === 'redeem' ? <Loader2 className="animate-spin" /> : null}
              {t('platforms.tasks.redeem')}
            </Button>
          </form>
          <p className="text-[11px] leading-5 text-muted-foreground">
            {isAdmin ? t('platforms.tasks.redeemDesc') : t('platforms.tasks.adminOnly')}
          </p>
        </div>
      </section>
    </div>
  );
}

/** 一格指标：值缺失时显示「—」而不是 0（0 会被读成「真的没有了」）。 */
function Metric({label, value}: {label: string; value: string | number | null | undefined}) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-muted-foreground">{label}</span>
      <span className="tabular-nums text-foreground">
        {value === null || value === undefined || value === '' ? '—' : value}
      </span>
    </div>
  );
}
