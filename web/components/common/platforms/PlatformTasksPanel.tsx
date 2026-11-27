'use client';

import {useCallback, useEffect, useMemo, useState} from 'react';
import {
  ArrowUpRight,
  CalendarCheck,
  Check,
  CircleAlert,
  Coins,
  Copy,
  Dices,
  Gift,
  ListChecks,
  Loader2,
  RefreshCw,
  Ticket,
  Trophy,
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
  PlatformTaskItem,
  PlatformTaskResult,
  PlatformTaskStatus,
} from '@/lib/types';

/**
 * 平台「任务」页（Loomy：签到 / 绑定邀请码 / 兑换兑换码；
 * DuMate：签到 / 成长任务 / 抽奖 / 邀请码；
 * 灵犀：签到 / 任务中心 / 邀请码 / 兑换权益）。
 *
 * 为什么单独一页而不是塞进账号表格：这些事都是**按账号、低频、带输入**
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
  /** DuMate 的任务体系与 Loomy 完全不同（成长任务 / 抽奖 / 邀请），分段渲染。 */
  const isDumate = platform === 'dumate';
  /** 灵犀同样是「签到 + 任务 + 邀请」结构，但没有抽奖，另有兑换码。 */
  const isLingxi = platform === 'lingxi';
  const structured = isDumate || isLingxi;

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
  /** DuMate 抽奖：最近一次结果 + 实物奖收货信息。 */
  const [drawResult, setDrawResult] = useState<PlatformTaskResult | null>(null);
  const [contact, setContact] = useState('');

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
    setDrawResult(null);
    setContact('');
    void load(uid);
  }, [uid, load]);

  const activation = status?.activation;
  const activated = activation?.activated === true;

  async function runAction(
    action:
      | 'checkin'
      | 'bind_invite'
      | 'redeem'
      | 'complete_task'
      | 'draw'
      | 'claim_prize',
    body?: {
      invite_code?: string;
      code?: string;
      task_id?: string;
      draw_record_id?: string;
      contact?: string;
    },
    /** 按钮粒度：同一动作可能按任务逐条触发，用 `key` 区分转圈的是哪一个。 */
    key?: string,
  ) {
    if (!uid || busy) return;
    setBusy(key || action);
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
      } else if (action === 'complete_task') {
        const done = task.done_count ?? 0;
        const failed = task.fail_count ?? 0;
        if (failed > 0) {
          notify.warn(
            t('platforms.tasks.growthResult', {done, failed}),
            task.results?.find((r) => !r.ok)?.error || undefined,
          );
        } else {
          notify.ok(t('platforms.tasks.growthResult', {done, failed: 0}));
        }
      } else if (action === 'draw') {
        setDrawResult(task);
        if (task.prize_name) {
          notify.ok(
            t('platforms.tasks.drawPrize', {name: task.prize_name}),
          );
        } else {
          notify.info(t('platforms.tasks.drawNoPrize'));
        }
      } else if (action === 'claim_prize') {
        notify.ok(t('platforms.tasks.drawClaimDone'));
        setDrawResult(null);
        setContact('');
      } else {
        // 「本次到账」优先（DuMate 重复点击时为 0，不会把上次的奖励再说一遍）；
        // Loomy 没有该字段，退回 earned_credit。
        const earned =
          task.checkin?.earned_this_run ?? task.checkin?.earned_credit ?? 0;
        if (task.checkin?.already) {
          notify.info(
            t('platforms.tasks.checkinAlready'),
            earned > 0 ? t('platforms.checkinEarned', {n: earned}) : undefined,
          );
        } else {
          notify.ok(
            t('platforms.tasks.checkinDone'),
            earned > 0
              ? t('platforms.checkinEarned', {n: earned})
              : task.checkin?.message || undefined,
          );
        }
      }
      // DuMate / 灵犀的写接口只回动作结果（不含整页快照），统一重读一次；
      // Loomy 的动作返回里带着最新状态，直接合并省一次往返。
      if (structured) {
        void load(uid);
      } else if (task.uid) {
        setStatus((prev) => ({...(prev || {uid}), ...task} as PlatformTaskStatus));
      } else {
        void load(uid);
      }
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
  // ── DuMate 任务面板的数据切片 ──────────────────────────
  const allTasks: PlatformTaskItem[] = status?.tasks ?? [];
  // 上游正常会给分组；万一只回了扁平任务，也别把整段显示成"暂无任务"。
  const taskGroups = (status?.task_groups ?? []).filter(
    (group) => (group.tasks?.length ?? 0) > 0,
  );
  if (!taskGroups.length && allTasks.length) {
    taskGroups.push({group_code: '', group_name: '', tasks: allTasks});
  }
  const taskTotal = allTasks.length;
  const taskDone = allTasks.filter((item) => item.done).length;
  const autoPending = allTasks.some((item) => item.auto && !item.done);
  const inviteInfo = status?.invite;
  const draw = status?.draw;
  const endpointErrors = status?.errors ?? [];

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

      {endpointErrors.length ? (
        <section className="space-y-1 rounded-[20px] bg-amber-500/10 px-3.5 py-2.5 text-[10px] leading-4 text-amber-700 ring-1 ring-amber-500/20 dark:text-amber-400">
          {endpointErrors.map((item) => (
            <p key={`${item.endpoint}-${item.code || item.message}`} className="break-all">
              {item.endpoint}: {item.message}
            </p>
          ))}
        </section>
      ) : null}

      {!structured ? (
        <>
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
        </>
      ) : (
        <>
          {/* ── 每日签到 ─────────────────────────────────── */}
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
            <div className="space-y-2 px-3.5 py-3">
              {checkin ? (
                <>
                  {isLingxi ? (
                    <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-[11px] sm:grid-cols-3">
                      <Metric
                        label={t('platforms.tasks.checkinStreak')}
                        value={checkin.continuous_days}
                      />
                      <Metric
                        label={t('platforms.tasks.checkinEarnedLabel')}
                        value={
                          typeof checkin.earned_credit === 'number' && checkin.earned_credit > 0
                            ? `+${checkin.earned_credit}`
                            : 0
                        }
                        hint={checkin.earned_at
                          ? fmtDateTime(checkin.earned_at * 1000)
                          : undefined}
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
                    <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-[11px] sm:grid-cols-4">
                      <Metric label={t('platforms.tasks.balance')} value={checkin.balance} />
                      {/* 每天 500 分是服务端 00:00 自动发的，所以「已签到」时
                          也可能有到账额；这里展示的是最近一次真正到账的那笔。 */}
                      <Metric
                        label={t('platforms.tasks.checkinEarnedLabel')}
                        value={
                          typeof checkin.earned_credit === 'number' && checkin.earned_credit > 0
                            ? `+${checkin.earned_credit}`
                            : 0
                        }
                        hint={checkin.earned_at
                          ? fmtDateTime(checkin.earned_at * 1000)
                          : undefined}
                      />
                      <Metric
                        label={t('platforms.tasks.checkinMonthPoints')}
                        value={checkin.month_points}
                      />
                      <Metric
                        label={t('platforms.tasks.checkinTotalTimes')}
                        value={checkin.total_times}
                      />
                    </div>
                  )}
                  <p className="text-[11px] text-muted-foreground">
                    {checkin.already
                      ? t('platforms.tasks.checkinAlready')
                      : checkin.message || (checkin.ok
                        ? t('platforms.tasks.checkinDone')
                        : t('platforms.checkinFailed'))}
                  </p>
                </>
              ) : (
                <p className="text-[11px] text-muted-foreground">
                  {t('platforms.tasks.checkinDesc')}
                </p>
              )}
            </div>
          </section>

          {/* ── 成长任务 ─────────────────────────────────── */}
          <section className="overflow-hidden rounded-[20px] bg-muted">
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border/60 px-3.5 py-3">
              <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
                <ListChecks className="h-3.5 w-3.5" />
                <h3 className="text-xs font-medium text-foreground">
                  {isLingxi
                    ? t('platforms.tasks.lingxiTasksTitle')
                    : t('platforms.tasks.growthTitle')}
                </h3>
                {taskTotal > 0 ? (
                  <Badge
                    variant="secondary"
                    className="rounded-md px-1.5 text-[10px] tabular-nums"
                  >
                    {t('platforms.tasks.growthProgress', {
                      done: taskDone,
                      total: taskTotal,
                    })}
                  </Badge>
                ) : null}
              </div>
              {/* 灵犀的任务必须逐条领取（上游没有批量接口），不显示「自动完成」。 */}
              {!isLingxi ? (
                <Button
                  size="sm"
                  variant="outline"
                  className="h-8 rounded-full px-3 text-[11px]"
                  disabled={!isAdmin || busy !== '' || !autoPending}
                  title={
                    isAdmin
                      ? t('platforms.tasks.growthRunAutoHint')
                      : t('platforms.tasks.adminOnly')
                  }
                  onClick={() => void runAction('complete_task')}
                >
                  {busy === 'complete_task'
                    ? <Loader2 className="animate-spin" />
                    : <ListChecks />}
                  {t('platforms.tasks.growthRunAuto')}
                </Button>
              ) : null}
            </div>
            {taskGroups.length ? (
              <div className="divide-y divide-border/40">
                {taskGroups.map((group) => (
                  <div key={group.group_code || group.group_name || 'group'}>
                    {group.group_name ? (
                      <p className="bg-background/40 px-3.5 py-1.5 text-[10px] font-medium text-muted-foreground">
                        {group.group_name}
                      </p>
                    ) : null}
                    {group.tasks.map((task) => {
                      const key = `complete_task:${task.task_id}`;
                      const running = busy === key;
                      const repeat = Math.max(1, task.repeat_count ?? 1);
                      const rewardPoints = task.reward_points ?? 0;
                      const rewardDraws = task.reward_count ?? 0;
                      const link = /^https?:\/\//i.test(task.jump_link || '')
                        ? task.jump_link
                        : '';
                      return (
                        <div
                          key={`${group.group_code || ''}-${task.task_id}`}
                          className="flex flex-wrap items-center justify-between gap-2 px-3.5 py-2"
                        >
                          <div className="min-w-0 flex-1">
                            <div className="flex flex-wrap items-center gap-1.5">
                              <span className="truncate text-xs font-medium">
                                {task.title || task.task_id}
                              </span>
                              {task.task_type ? (
                                <Badge
                                  variant="secondary"
                                  className="rounded-md px-1 text-[9px] font-normal text-muted-foreground"
                                >
                                  {task.task_type}
                                </Badge>
                              ) : null}
                              {task.done ? (
                                <Badge
                                  variant="secondary"
                                  className="rounded-md px-1.5 text-[10px] text-emerald-700 dark:text-emerald-400"
                                >
                                  {t('platforms.tasks.growthDone')}
                                </Badge>
                              ) : null}
                            </div>
                            {task.sub_title ? (
                              <p className="mt-0.5 truncate text-[10px] text-muted-foreground">
                                {task.sub_title}
                              </p>
                            ) : null}
                            <div className="mt-0.5 flex flex-wrap items-center gap-2 text-[10px] text-muted-foreground">
                              {rewardPoints > 0 ? (
                                <span className="tabular-nums text-emerald-700 dark:text-emerald-400">
                                  {t('platforms.tasks.growthReward', {n: rewardPoints})}
                                </span>
                              ) : null}
                              {rewardDraws > 0 ? (
                                <span className="tabular-nums">
                                  {t('platforms.tasks.growthRewardDraws', {n: rewardDraws})}
                                </span>
                              ) : null}
                              {repeat > 1 ? (
                                <span className="tabular-nums">
                                  {t('platforms.tasks.growthProgress', {
                                    done: task.completed_count ?? 0,
                                    total: repeat,
                                  })}
                                </span>
                              ) : null}
                              {task.error ? (
                                <span className="break-all text-amber-700 dark:text-amber-400">
                                  {task.error}
                                </span>
                              ) : null}
                            </div>
                          </div>
                          <div className="flex shrink-0 items-center gap-1.5">
                            {task.done ? (
                              <Check className="h-4 w-4 text-emerald-600" />
                            ) : task.auto ? (
                              <Button
                                size="sm"
                                variant="outline"
                                className="h-7 rounded-full px-2.5 text-[10px]"
                                disabled={!isAdmin || busy !== ''}
                                title={isLingxi
                                  ? t('platforms.tasks.claimNowHint')
                                  : t('platforms.tasks.growthQueryHint')}
                                onClick={() =>
                                  void runAction(
                                    'complete_task',
                                    {task_id: task.task_id},
                                    key,
                                  )
                                }
                              >
                                {running
                                  ? <Loader2 className="animate-spin" />
                                  : <Check />}
                                {isLingxi
                                  ? t('platforms.tasks.claimNow')
                                  : t('platforms.tasks.growthComplete')}
                              </Button>
                            ) : (
                              <>
                                <span className="text-[10px] text-muted-foreground">
                                  {t('platforms.tasks.growthNeedClient')}
                                </span>
                                {link ? (
                                  <a
                                    href={link}
                                    target="_blank"
                                    rel="noreferrer"
                                    className="text-muted-foreground hover:text-foreground"
                                    title={t('platforms.tasks.growthOpen')}
                                  >
                                    <ArrowUpRight className="h-3.5 w-3.5" />
                                  </a>
                                ) : null}
                              </>
                            )}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                ))}
              </div>
            ) : (
              <div className="px-3.5 py-6 text-center text-[11px] text-muted-foreground">
                {loading ? <Loader2 className="mx-auto h-3.5 w-3.5 animate-spin" /> : null}
                {t('platforms.tasks.growthEmpty')}
              </div>
            )}
          </section>

          {/* ── 邀请码 ───────────────────────────────────── */}
          <section className="overflow-hidden rounded-[20px] bg-muted">
            <div className="flex items-center gap-2 border-b border-border/60 px-3.5 py-3 text-[11px] text-muted-foreground">
              <UserPlus className="h-3.5 w-3.5" />
              <h3 className="text-xs font-medium text-foreground">
                {t('platforms.tasks.inviteTitle')}
              </h3>
            </div>
            <div className="space-y-2 px-3.5 py-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex min-w-0 items-center gap-2">
                  <span className="text-[11px] text-muted-foreground">
                    {t('platforms.tasks.inviteMine')}：
                  </span>
                  <span className="font-mono text-xs font-medium">
                    {inviteInfo?.invitation_code || '—'}
                  </span>
                  <Button
                    variant="ghost"
                    size="sm"
                    className="h-6 rounded-full px-1.5 text-[10px]"
                    disabled={!inviteInfo?.invitation_code}
                    title={t('platforms.tasks.copyCode')}
                    onClick={() => void copyCode(inviteInfo?.invitation_code || '')}
                  >
                    {copied === inviteInfo?.invitation_code
                      ? <Check className="h-3 w-3 text-emerald-600" />
                      : <Copy className="h-3 w-3" />}
                  </Button>
                </div>
                <div className="flex items-center gap-3 text-[11px] text-muted-foreground">
                  <span className="tabular-nums">
                    {t('platforms.tasks.inviteCount', {
                      n: inviteInfo?.invited_count ?? 0,
                    })}
                  </span>
                  <span className="tabular-nums">
                    {t('platforms.tasks.invitePoints', {
                      n: inviteInfo?.total_points ?? 0,
                    })}
                  </span>
                </div>
              </div>
              {inviteInfo?.invite_message ? (
                <p className="break-all text-[10px] text-muted-foreground">
                  {inviteInfo.invite_message}
                </p>
              ) : null}
              <form
                className="flex flex-wrap items-center gap-1.5 pt-1"
                onSubmit={(e) => {
                  e.preventDefault();
                  if (!isAdmin || !invite.trim()) return;
                  void runAction('bind_invite', {invite_code: invite.trim()});
                }}
              >
                <Input
                  value={invite}
                  onChange={(e) => setInvite(e.target.value)}
                  placeholder={isLingxi
                    ? t('platforms.tasks.invitePlaceholderLingxi')
                    : t('platforms.tasks.inviteRedeemPlaceholder')}
                  className="h-8 min-w-[180px] flex-1 rounded-xl text-xs"
                  disabled={!isAdmin || busy !== ''}
                  autoComplete="off"
                  spellCheck={false}
                />
                <Button
                  type="submit"
                  size="sm"
                  className="h-8 shrink-0 rounded-full px-3 text-[11px]"
                  disabled={!isAdmin || busy !== '' || !invite.trim()}
                >
                  {busy === 'bind_invite'
                    ? <Loader2 className="animate-spin" />
                    : null}
                  {t('platforms.tasks.inviteRedeem')}
                </Button>
              </form>
              <p className="text-[11px] leading-5 text-muted-foreground">
                {isAdmin
                  ? isLingxi
                    ? t('platforms.tasks.inviteRedeemHintLingxi')
                    : t('platforms.tasks.inviteRedeemHint')
                  : t('platforms.tasks.adminOnly')}
              </p>
            </div>
          </section>

          {/* ── 兑换码（灵犀任务中心的「兑换权益」）────────── */}
          {isLingxi ? (
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
          ) : null}

          {/* ── 抽奖（仅 DuMate）──────────────────────────── */}
          {isDumate ? (
          <section className="overflow-hidden rounded-[20px] bg-muted">
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border/60 px-3.5 py-3">
              <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
                <Dices className="h-3.5 w-3.5" />
                <h3 className="text-xs font-medium text-foreground">
                  {t('platforms.tasks.drawTitle')}
                </h3>
                <Badge
                  variant="secondary"
                  className="rounded-md px-1.5 text-[10px] tabular-nums"
                >
                  {t('platforms.tasks.drawRemaining', {
                    n: draw?.remaining_draws ?? '—',
                  })}
                </Badge>
              </div>
              <Button
                size="sm"
                variant="outline"
                className="h-8 rounded-full px-3 text-[11px]"
                disabled={
                  !isAdmin
                  || busy !== ''
                  || (typeof draw?.remaining_draws === 'number'
                    && draw.remaining_draws <= 0)
                }
                onClick={() => void runAction('draw')}
              >
                {busy === 'draw'
                  ? <Loader2 className="animate-spin" />
                  : <Dices />}
                {t('platforms.tasks.drawRun')}
              </Button>
            </div>
            <div className="space-y-2 px-3.5 py-3">
              {drawResult ? (
                <div className="rounded-xl bg-background/60 px-3 py-2">
                  <div className="flex items-center gap-1.5 text-xs">
                    <Trophy className="h-3.5 w-3.5 text-amber-600" />
                    <span className="font-medium">
                      {drawResult.prize_name
                        ? t('platforms.tasks.drawPrize', {
                            name: drawResult.prize_name,
                          })
                        : t('platforms.tasks.drawNoPrize')}
                    </span>
                  </div>
                  {drawResult.needs_claim ? (
                    <form
                      className="mt-1.5 flex flex-wrap items-center gap-1.5"
                      onSubmit={(e) => {
                        e.preventDefault();
                        if (!isAdmin || !drawResult.draw_record_id) return;
                        void runAction('claim_prize', {
                          draw_record_id: drawResult.draw_record_id,
                          contact: contact.trim(),
                        });
                      }}
                    >
                      <Input
                        value={contact}
                        onChange={(e) => setContact(e.target.value)}
                        placeholder={t('platforms.tasks.drawClaimPlaceholder')}
                        className="h-7 min-w-[160px] flex-1 rounded-lg text-[11px]"
                        disabled={!isAdmin || busy !== ''}
                        autoComplete="off"
                      />
                      <Button
                        type="submit"
                        size="sm"
                        className="h-7 rounded-full px-2.5 text-[10px]"
                        disabled={!isAdmin || busy !== ''}
                      >
                        {busy === 'claim_prize'
                          ? <Loader2 className="animate-spin" />
                          : <Trophy />}
                        {t('platforms.tasks.drawClaim')}
                      </Button>
                    </form>
                  ) : null}
                  {drawResult.needs_claim ? (
                    <p className="mt-1 text-[10px] leading-4 text-muted-foreground">
                      {t('platforms.tasks.drawPhysicalHint')}
                    </p>
                  ) : null}
                </div>
              ) : null}
              {draw && draw.my_prizes && draw.my_prizes.length > 0 ? (
                <div className="border-t border-border/40 pt-2">
                  <p className="text-[10px] font-medium text-muted-foreground">
                    {t('platforms.tasks.drawMyPrizes')}
                  </p>
                  <ul className="mt-1 space-y-0.5">
                    {draw.my_prizes.slice(0, 5).map((prize, index) => (
                      <li
                        key={index}
                        className="flex items-center justify-between gap-2 text-[11px]"
                      >
                        <span className="truncate">
                          {String(prize.prize_name ?? prize.name ?? '—')}
                        </span>
                        <span className="shrink-0 text-[10px] text-muted-foreground">
                          {String(prize.draw_time ?? prize.created_at ?? '')}
                        </span>
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}
              {!drawResult && (!draw || !draw.my_prizes || draw.my_prizes.length === 0) ? (
                <p className="text-[11px] text-muted-foreground">
                  {t('platforms.tasks.drawDesc')}
                </p>
              ) : null}
            </div>
          </section>
          ) : null}
        </>
      )}
    </div>
  );
}

/** 一格指标：值缺失时显示「—」而不是 0（0 会被读成「真的没有了」）。 */
function Metric({
  label,
  value,
  hint,
}: {
  label: string;
  value: string | number | null | undefined;
  /** 可选副标题（例如到账时刻），用于解释这个数字是哪一次的。 */
  hint?: string;
}) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-muted-foreground">{label}</span>
      <span className="tabular-nums text-foreground">
        {value === null || value === undefined || value === '' ? '—' : value}
      </span>
      {hint ? <span className="text-[10px] text-muted-foreground">{hint}</span> : null}
    </div>
  );
}
