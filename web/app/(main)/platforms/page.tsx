'use client';

import {useCallback, useRef, useState} from 'react';
import {
  Activity,
  Boxes,
  CircleAlert,
  CircleCheck,
  FileJson,
  KeyRound,
  Layers,
  Loader2,
  MonitorDown,
  Plus,
  RefreshCw,
  Trash2,
} from 'lucide-react';

import {PageHeader} from '@/components/common/layout/PageHeader';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {StatCard} from '@/components/common/layout/StatCard';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {LoadError} from '@/components/common/states/LoadError';
import {SkeletonBar} from '@/components/common/states/SkeletonBar';
import {Button} from '@/components/ui/button';
import {Badge} from '@/components/ui/badge';
import {Textarea} from '@/components/ui/textarea';
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
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import {errText, platformApi} from '@/lib/api';
import {useAuth} from '@/lib/auth-context';
import {useT} from '@/lib/i18n/provider';
import {notify} from '@/lib/toast';
import {useAsyncAll} from '@/lib/use-async-data';
import {fmtDateTime} from '@/lib/format';
import {cn} from '@/lib/utils';
import type {
  PlatformAccount,
  PlatformCheckinResult,
  PlatformImportResult,
  PlatformModel,
  PlatformUsage,
} from '@/lib/types';

/** 上下文窗口显示：131072 → 128K；1048576 → 1M；0 → —（与模型中心同一口径） */
function fmtCtx(n: number): string {
  if (!n || n <= 0) return '—';
  if (n >= 1024 * 1024) {
    const m = n / (1024 * 1024);
    return `${Number.isInteger(m) ? m : m.toFixed(1)}M`;
  }
  if (n >= 1024) return `${Math.round(n / 1024)}K`;
  return String(n);
}

/** ISO 时间串 → epoch 毫秒（`fmtDateTime` 认两种单位；解析不出来的交给它显示 —）。 */
function isoMs(value: string): number | null {
  if (!value) return null;
  const ms = Date.parse(value);
  return Number.isFinite(ms) ? ms : null;
}

/** 令牌是否已经过期（后端用 ISO 串，前端只在展示时分一次）。 */
function isExpired(value: string): boolean {
  const ms = isoMs(value);
  return ms !== null && ms <= Date.now();
}

/**
 * 多平台页面（M4）。
 *
 * 与「账号」页的关系：那一页管的是 **WorkBuddy 上游账号池**（一个账号 = 一个
 * auth 文件，带积分 / 冷却 / 权重）；这一页管的是**本端自己维护的平台凭据**
 * （目前是 Trae：uid + token + refreshToken）。两者不共用接口，也不共用脱敏
 * 口径——平台账号的明文令牌**从不上行到浏览器**（见 `store.public_account`）。
 *
 * 三个权限档位在后端强制（`require_admin` / `require_session_admin`），这里
 * 只是把不可用的按钮藏掉：只读令牌看不到导入 / 删除 / 刷新 / 签到。
 */
export default function PlatformsPage() {
  const t = useT();
  const {isAdmin} = useAuth();

  /**
   * 模型「强制刷新」= 绕过**后端缓存**再拉一次。用 ref 而不是 state：它描述的
   * 是「下一次请求要不要绕缓存」，不参与渲染，也不该触发重渲染。
   */
  const forceModels = useRef(false);

  const platformsState = useAsyncAll({
    platforms: () => platformApi.list().then((r) => r.platforms),
  });
  const platforms = platformsState.values.platforms ?? [];

  const [activeName, setActiveName] = useState('');
  // 平台列表还没回来时 current 是 null；回来之后默认选第一个。
  const current = platforms.find((p) => p.name === activeName) ?? platforms[0] ?? null;
  const currentName = current?.name ?? '';

  /**
   * 当前平台的账号与模型。依赖 `currentName`：换平台就是换一份数据集，必须
   * 清空重取（`useAsyncAll` 的 deps 语义），否则会拿 A 平台的账号配 B 平台的
   * 模型列——两边的行数对不上时看起来像「这个平台没有账号」。
   */
  const detailState = useAsyncAll({
    accounts: () => currentName
      ? platformApi.accounts(currentName).then((r) => r.accounts)
      : Promise.resolve([] as PlatformAccount[]),
    models: () => currentName
      ? platformApi.models(currentName, forceModels.current).then((r) => r.models)
      : Promise.resolve([] as PlatformModel[]),
  }, [currentName]);

  const accounts = detailState.values.accounts ?? [];
  const models = detailState.values.models ?? [];

  // ── 弹窗状态 ──────────────────────────────────────────
  const [importOpen, setImportOpen] = useState(false);
  const [importText, setImportText] = useState('');
  const [importBusy, setImportBusy] = useState(false);

  const [usageFor, setUsageFor] = useState<PlatformAccount | null>(null);
  const [usage, setUsage] = useState<PlatformUsage | null>(null);
  const [usageLoading, setUsageLoading] = useState(false);
  const [usageError, setUsageError] = useState('');

  /** 批量操作 / 单账号操作的进行中标记（按 uid 或 'all'），用于禁用按钮。 */
  const [busy, setBusy] = useState<string>('');

  const refreshAll = useCallback(async () => {
    await Promise.all([platformsState.reload(), detailState.reload()]);
  }, [platformsState, detailState]);

  const reloadAccounts = useCallback(async () => {
    const ok = await detailState.reload();
    if (!ok) notify.warn(errText(detailState.errors.accounts));
    return ok;
  }, [detailState]);

  /** 把导入结果翻成一句提示；失败时把第一条原因带上，用户才知道是格式问题。 */
  const announceImport = useCallback((result: PlatformImportResult) => {
    const ok = result.imported.length;
    const failed = result.failed.length;
    if (!ok && !failed) {
      notify.info(t('platforms.importEmpty'));
      return;
    }
    const detail = failed ? result.failed[0]?.error : undefined;
    if (failed) {
      notify.warn(t('platforms.importDone', {ok, failed}), detail);
    } else {
      notify.ok(t('platforms.importDone', {ok, failed}));
    }
  }, [t]);

  // ── 操作 ──────────────────────────────────────────────
  async function importDesktop() {
    if (!current || busy) return;
    setBusy('import');
    const pending = notify.info(t('common.loading'));
    try {
      const result = await platformApi.importDesktop(current.name);
      notify.dismiss(pending);
      if (!result.imported.length && !result.failed.length) {
        notify.info(t('platforms.desktopNone'));
      } else if (result.imported.length && !result.failed.length) {
        notify.ok(t('platforms.desktopDone', {n: result.imported.length}));
      } else {
        announceImport(result);
      }
      await refreshAll();
    } catch (e) {
      notify.dismiss(pending);
      notify.err(errText(e));
    } finally {
      setBusy('');
    }
  }

  async function submitImport() {
    if (!current || importBusy) return;
    const raw = importText.trim();
    if (!raw) {
      notify.err(t('platforms.importEmpty'));
      return;
    }
    let body: unknown;
    try {
      body = JSON.parse(raw);
    } catch {
      notify.err(t('platforms.importInvalid'));
      return;
    }
    setImportBusy(true);
    try {
      const result = await platformApi.import(current.name, body);
      announceImport(result);
      setImportOpen(false);
      setImportText('');
      await refreshAll();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setImportBusy(false);
    }
  }

  async function refreshAccount(account: PlatformAccount) {
    if (!current || busy) return;
    setBusy(account.uid);
    const pending = notify.info(t('common.loading'));
    try {
      await platformApi.refresh(current.name, account.uid);
      notify.dismiss(pending);
      notify.ok(t('platforms.refreshDone'));
      await reloadAccounts();
    } catch (e) {
      notify.dismiss(pending);
      notify.err(errText(e));
    } finally {
      setBusy('');
    }
  }

  async function checkin(account: PlatformAccount) {
    if (!current || busy) return;
    setBusy(account.uid);
    const pending = notify.info(t('common.loading'));
    try {
      const {result} = await platformApi.checkin(current.name, account.uid);
      notify.dismiss(pending);
      announceCheckin(result);
    } catch (e) {
      notify.dismiss(pending);
      notify.err(errText(e));
    } finally {
      setBusy('');
    }
  }

  function announceCheckin(result: PlatformCheckinResult) {
    if (result.already) {
      notify.info(t('platforms.checkinAlready'));
      return;
    }
    if (!result.ok) {
      notify.warn(t('platforms.checkinFailed'), result.message);
      return;
    }
    notify.ok(result.earned_credit > 0
      ? t('platforms.checkinEarned', {n: result.earned_credit})
      : t('platforms.checkinOk'));
  }

  async function checkinAll() {
    if (!current || busy) return;
    setBusy('all');
    const pending = notify.info(t('common.loading'));
    try {
      const result = await platformApi.checkinAll(current.name);
      notify.dismiss(pending);
      if (result.ok === result.total) {
        notify.ok(t('platforms.checkinAllDone', {ok: result.ok, total: result.total}));
      } else {
        const first = result.results.find((r) => !r.ok);
        notify.warn(t('platforms.checkinAllDone', {ok: result.ok, total: result.total}),
          first ? `${first.uid}: ${first.message}` : undefined);
      }
    } catch (e) {
      notify.dismiss(pending);
      notify.err(errText(e));
    } finally {
      setBusy('');
    }
  }

  async function removeAccount(account: PlatformAccount) {
    if (!current) return;
    await platformApi.remove(current.name, account.uid);
    notify.ok(t('platforms.deleted'));
    await refreshAll();
  }

  async function openUsage(account: PlatformAccount) {
    if (!current) return;
    setUsageFor(account);
    setUsage(null);
    setUsageError('');
    setUsageLoading(true);
    try {
      const {usage: value} = await platformApi.usage(current.name, account.uid);
      setUsage(value);
    } catch (e) {
      setUsageError(errText(e));
    } finally {
      setUsageLoading(false);
    }
  }

  async function refreshModels() {
    forceModels.current = true;
    const pending = notify.info(t('common.loading'));
    try {
      const ok = await detailState.reload();
      notify.dismiss(pending);
      if (ok) notify.ok(t('platforms.modelsDone'));
      else notify.err(t('platforms.noModelsDesc'));
    } finally {
      forceModels.current = false;
    }
  }

  const header = (
    <PageHeader
      title={t('platforms.title')}
      description={t('platforms.description')}
      actions={
        <Button
          variant="outline"
          size="sm"
          className="rounded-full"
          disabled={platformsState.isInitialLoading || platformsState.isRefreshing
            || detailState.isRefreshing}
          onClick={() => void refreshAll()}
        >
          {platformsState.isRefreshing || detailState.isRefreshing
            ? <Loader2 className="animate-spin" />
            : <RefreshCw />}
          {t('common.refresh')}
        </Button>
      }
    />
  );

  // 首屏：平台清单还没到 / 彻底失败。**必须排在「暂无平台」之前**，否则取数
  // 失败会显示成「后端没有注册任何平台」，把一次网络失败说成配置问题。
  if (platformsState.isInitialFailed || platformsState.isInitialLoading) {
    return (
      <div className="flex flex-col gap-4 md:gap-6">
        {header}
        {platformsState.isInitialFailed
          ? <LoadError variant="page" onRetry={platformsState.reload} />
          : <PlatformsSkeleton />}
      </div>
    );
  }

  if (!platforms.length) {
    return (
      <div className="flex flex-col gap-4 md:gap-6">
        {header}
        <EmptyState
          icon={Layers}
          title={t('platforms.noPlatforms')}
          description={t('platforms.noPlatformsDesc')}
        />
      </div>
    );
  }

  // 账号与模型两份数据全挂了：整页给「重试」，不要渲染两个空态（那会让人以为
  // 平台里确实没有账号和模型）。
  if (detailState.isInitialFailed) {
    return (
      <div className="flex flex-col gap-4 md:gap-6">
        {header}
        <LoadError variant="page" onRetry={detailState.reload} />
      </div>
    );
  }

  const supportsCheckin = !!current?.supports_checkin;
  const supportsDesktop = !!current?.supports_desktop_import;
  const busyAny = busy !== '';

  return (
    <div className="flex flex-col gap-4 md:gap-6" aria-busy={detailState.isRefreshing}>
      {header}

      {/* 平台选择：只有一个平台时也显示，让「模型前缀」这件事在界面上有个落点 */}
      <div className="flex flex-wrap items-center gap-1.5">
        {platforms.map((p) => (
          <Button
            key={p.name}
            variant={p.name === currentName ? 'default' : 'outline'}
            size="sm"
            className="h-8 rounded-full px-3 text-xs"
            onClick={() => setActiveName(p.name)}
            title={p.error}
          >
            {p.label}
            <Badge
              variant={p.name === currentName ? 'secondary' : 'outline'}
              className="ml-1 rounded-md px-1.5 text-[10px] font-mono"
            >
              {p.name === currentName ? accounts.length : p.account_count}
            </Badge>
          </Button>
        ))}
        {current?.model_prefix && (
          <span className="ml-1 font-mono text-[11px] text-muted-foreground">
            {current.model_prefix}&lt;model&gt;
          </span>
        )}
      </div>

      {/* 平台清单本身拿到了、但某个平台的账号目录读不出来：如实标出，不拖垮整页 */}
      {!!platformsState.errors.platforms && (
        <LoadError variant="inline" onRetry={platformsState.reload} />
      )}

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-3">
        <StatCard icon={Layers} label={t('platforms.statPlatforms')} value={String(platforms.length)} />
        <StatCard icon={KeyRound} label={t('platforms.statAccounts')} value={String(accounts.length)} />
        <StatCard icon={Boxes} label={t('platforms.statModels')} value={String(models.length)} />
      </div>

      {!!detailState.errors.accounts && (
        <LoadError variant="inline" message={errText(detailState.errors.accounts)}
          onRetry={detailState.reload} />
      )}

      {/* ── 账号 ───────────────────────────────────────── */}
      <section className="overflow-hidden rounded-[20px] bg-muted">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border/60 px-3.5 py-3">
          <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
            <KeyRound className="h-3.5 w-3.5" />
            <h2 className="text-xs font-medium text-foreground">{t('platforms.accountsTitle')}</h2>
            {isAdmin && current && (
              <span className="hidden sm:inline">· {current.label}</span>
            )}
          </div>
          {isAdmin && current && (
            <div className="flex flex-wrap items-center gap-1.5">
              {supportsDesktop && (
                <Button
                  variant="outline"
                  size="sm"
                  className="h-7 rounded-full px-2.5 text-[11px]"
                  disabled={busyAny}
                  onClick={() => void importDesktop()}
                  title={t('platforms.importDesktopHint')}
                >
                  {busy === 'import' ? <Loader2 className="animate-spin" /> : <MonitorDown />}
                  {t('platforms.importDesktop')}
                </Button>
              )}
              <Button
                variant="outline"
                size="sm"
                className="h-7 rounded-full px-2.5 text-[11px]"
                disabled={busyAny}
                onClick={() => {
                  setImportText('');
                  setImportOpen(true);
                }}
              >
                <FileJson />
                {t('platforms.importManual')}
              </Button>
              {supportsCheckin && (
                <Button
                  variant="outline"
                  size="sm"
                  className="h-7 rounded-full px-2.5 text-[11px]"
                  disabled={busyAny || !accounts.length}
                  onClick={() => void checkinAll()}
                >
                  {busy === 'all' ? <Loader2 className="animate-spin" /> : <CircleCheck />}
                  {t('platforms.checkinAll')}
                </Button>
              )}
            </div>
          )}
        </div>

        {detailState.isInitialLoading ? (
          <div className="space-y-2 p-4">
            <SkeletonBar className="h-9 w-full" />
            <SkeletonBar className="h-9 w-full" />
          </div>
        ) : accounts.length === 0 ? (
          <EmptyState
            icon={KeyRound}
            title={t('platforms.noAccounts')}
            description={t('platforms.noAccountsDesc')}
            className="flex flex-col items-center justify-center p-8 text-center"
          >
            {isAdmin && current && (
              <Button
                size="sm"
                className="mt-1 rounded-full"
                disabled={busyAny}
                onClick={() => void importDesktop()}
              >
                <Plus />
                {t('platforms.importDesktop')}
              </Button>
            )}
          </EmptyState>
        ) : (
          <div className="overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow className="border-b border-border/60 hover:bg-transparent">
                  <TableHead className="pl-4 text-[11px] text-muted-foreground">
                    {t('platforms.colAccount')}
                  </TableHead>
                  <TableHead className="text-[11px] text-muted-foreground">
                    {t('platforms.colRealm')}
                  </TableHead>
                  <TableHead className="hidden text-[11px] text-muted-foreground sm:table-cell">
                    {t('platforms.colToken')}
                  </TableHead>
                  <TableHead className="hidden text-[11px] text-muted-foreground md:table-cell">
                    {t('platforms.colExpires')}
                  </TableHead>
                  <TableHead className="hidden text-[11px] text-muted-foreground lg:table-cell">
                    {t('platforms.colSource')}
                  </TableHead>
                  <TableHead className="pr-4 text-right text-[11px] text-muted-foreground">
                    {t('platforms.colActions')}
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {accounts.map((a) => {
                  const rowBusy = busy === a.uid;
                  const expired = isExpired(a.expiredAt);
                  return (
                    <TableRow key={a.uid} className="border-b border-border/40">
                      <TableCell className="pl-4">
                        <div className="flex flex-col gap-0.5 py-0.5">
                          <span className="text-xs font-medium">
                            {a.nickname || a.uid}
                          </span>
                          <span className="font-mono text-[11px] text-muted-foreground">
                            {a.uid}
                          </span>
                          {a.lastRefreshError && (
                            <span
                              className="text-[10px] text-amber-600 dark:text-amber-400"
                              title={a.lastRefreshError}
                            >
                              {t('platforms.checkinFailed')}: {a.lastRefreshError}
                            </span>
                          )}
                        </div>
                      </TableCell>
                      <TableCell>
                        <Badge variant="secondary" className="rounded-md text-[10px]">
                          {a.realm_label || a.realm}
                        </Badge>
                      </TableCell>
                      <TableCell className="hidden sm:table-cell">
                        <div className="flex flex-wrap items-center gap-1">
                          <span className={cn(
                            'font-mono text-[11px]',
                            a.has_token ? 'text-muted-foreground' : 'text-amber-600 dark:text-amber-400',
                          )}>
                            {a.has_token ? a.token_preview : t('platforms.tokenMissing')}
                          </span>
                          {a.has_refresh_token && (
                            <Badge variant="outline" className="rounded-md text-[10px]">
                              {t('platforms.refreshMissing')}
                            </Badge>
                          )}
                        </div>
                      </TableCell>
                      <TableCell className="hidden md:table-cell">
                        <div className="flex items-center gap-1.5">
                          <span className="text-xs tabular-nums text-muted-foreground">
                            {fmtDateTime(isoMs(a.expiredAt))}
                          </span>
                          {expired && (
                            <Badge variant="secondary" className="rounded-md text-[10px] text-amber-700 dark:text-amber-400">
                              {t('platforms.expired')}
                            </Badge>
                          )}
                        </div>
                      </TableCell>
                      <TableCell className="hidden lg:table-cell">
                        <Badge variant="secondary" className="rounded-md text-[10px]">
                          {a.source === 'desktop'
                            ? t('platforms.sourceDesktop')
                            : t('platforms.sourceManual')}
                        </Badge>
                      </TableCell>
                      <TableCell className="pr-4">
                        <div className="flex items-center justify-end gap-0.5">
                          <Button
                            variant="ghost"
                            size="icon"
                            className="h-7 w-7 rounded-md"
                            title={t('platforms.usage')}
                            onClick={() => void openUsage(a)}
                          >
                            <Activity className="h-3.5 w-3.5" />
                          </Button>
                          {isAdmin && (
                            <>
                              {supportsCheckin && (
                                <Button
                                  variant="ghost"
                                  size="icon"
                                  className="h-7 w-7 rounded-md"
                                  title={t('platforms.checkin')}
                                  disabled={rowBusy || busyAny}
                                  onClick={() => void checkin(a)}
                                >
                                  {rowBusy ? <Loader2 className="animate-spin" /> : <CircleCheck className="h-3.5 w-3.5" />}
                                </Button>
                              )}
                              <Button
                                variant="ghost"
                                size="icon"
                                className="h-7 w-7 rounded-md"
                                title={t('platforms.refresh')}
                                disabled={rowBusy || busyAny}
                                onClick={() => void refreshAccount(a)}
                              >
                                {rowBusy ? <Loader2 className="animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
                              </Button>
                              <ConfirmDialog
                                title={t('platforms.deleteTitle')}
                                description={t('platforms.deleteDesc', {
                                  name: a.nickname || a.uid,
                                })}
                                confirmText={t('platforms.delete')}
                                destructive
                                onConfirm={() => removeAccount(a)}
                                trigger={
                                  <Button
                                    variant="ghost"
                                    size="icon"
                                    className="h-7 w-7 rounded-md text-destructive hover:text-destructive"
                                    title={t('platforms.delete')}
                                    disabled={busyAny}
                                  >
                                    <Trash2 className="h-3.5 w-3.5" />
                                  </Button>
                                }
                              />
                            </>
                          )}
                        </div>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </div>
        )}
      </section>

      {/* ── 模型 ───────────────────────────────────────── */}
      <section className="overflow-hidden rounded-[20px] bg-muted">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border/60 px-3.5 py-3">
          <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
            <Boxes className="h-3.5 w-3.5" />
            <h2 className="text-xs font-medium text-foreground">{t('platforms.modelsTitle')}</h2>
            <span>· {models.length}</span>
          </div>
          <Button
            variant="outline"
            size="sm"
            className="h-7 rounded-full px-2.5 text-[11px]"
            disabled={detailState.isRefreshing}
            onClick={() => void refreshModels()}
          >
            {detailState.isRefreshing ? <Loader2 className="animate-spin" /> : <RefreshCw />}
            {t('platforms.modelsRefresh')}
          </Button>
        </div>

        {!!detailState.errors.models && (
          <div className="px-3.5 pt-3">
            <LoadError variant="inline" message={errText(detailState.errors.models)}
              onRetry={detailState.reload} />
          </div>
        )}

        {detailState.isInitialLoading ? (
          <div className="space-y-2 p-4">
            <SkeletonBar className="h-9 w-full" />
            <SkeletonBar className="h-9 w-full" />
          </div>
        ) : models.length === 0 ? (
          <EmptyState
            icon={Boxes}
            title={t('platforms.noModels')}
            description={t('platforms.noModelsDesc')}
            className="flex flex-col items-center justify-center p-8 text-center"
          />
        ) : (
          <div className="overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow className="border-b border-border/60 hover:bg-transparent">
                  <TableHead className="pl-4 text-[11px] text-muted-foreground">
                    {t('platforms.colModel')}
                  </TableHead>
                  <TableHead className="text-[11px] text-muted-foreground">
                    {t('platforms.colContext')}
                  </TableHead>
                  <TableHead className="hidden text-[11px] text-muted-foreground sm:table-cell">
                    {t('platforms.colMaxOutput')}
                  </TableHead>
                  <TableHead className="pr-4 text-right text-[11px] text-muted-foreground">
                    #
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {models.map((m) => (
                  <TableRow key={m.id} className="border-b border-border/40">
                    <TableCell className="pl-4">
                      <div className="flex flex-col gap-0.5 py-0.5">
                        <span className="text-xs font-medium">{m.name || m.model_name}</span>
                        <span className="font-mono text-[11px] text-muted-foreground">{m.id}</span>
                      </div>
                    </TableCell>
                    <TableCell className="text-xs tabular-nums text-muted-foreground">
                      {fmtCtx(m.context_window)}
                    </TableCell>
                    <TableCell className="hidden text-xs tabular-nums text-muted-foreground sm:table-cell">
                      {fmtCtx(m.max_tokens)}
                    </TableCell>
                    <TableCell className="pr-4">
                      <div className="flex flex-wrap items-center justify-end gap-1">
                        {m.supports_reasoning && (
                          <Badge variant="secondary" className="rounded-md text-[10px]">
                            {t('platforms.reasoning')}
                          </Badge>
                        )}
                        {m.modalities?.input?.includes('image') && (
                          <Badge variant="secondary" className="rounded-md text-[10px]">
                            {t('platforms.multimodal')}
                          </Badge>
                        )}
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </section>

      <p className="text-[11px] leading-4 text-muted-foreground">
        {t('platforms.footnote')}
      </p>

      {/* ── 用量弹窗：原始 JSON 比「猜字段名」更诚实，也够诊断用 ── */}
      <Dialog open={usageFor !== null} onOpenChange={(open) => { if (!open) setUsageFor(null); }}>
        <DialogContent className="max-w-[560px]" showCloseButton>
          <DialogHeader>
            <DialogTitle>{t('platforms.usageTitle')}</DialogTitle>
            <DialogDescription>
              {t('platforms.usageDesc', {
                name: usageFor?.nickname || usageFor?.uid || '',
              })}
            </DialogDescription>
          </DialogHeader>
          <DialogBody className="max-h-[min(640px,calc(100dvh-12rem))]">
            <div className="px-6 pb-2">
              {usageLoading ? (
                <div className="space-y-2">
                  <SkeletonBar className="h-4 w-40" />
                  <SkeletonBar className="h-4 w-64" />
                  <SkeletonBar className="h-4 w-52" />
                </div>
              ) : usageError ? (
                <p className="text-xs text-amber-700 dark:text-amber-400">{usageError}</p>
              ) : !usage ? (
                <p className="text-xs text-muted-foreground">{t('platforms.usageEmpty')}</p>
              ) : (
                <div className="space-y-3">
                  {usage.session_usage === null && usage.entitlement === null && (
                    <p className="text-xs text-muted-foreground">{t('platforms.usageEmpty')}</p>
                  )}
                  {!!usage.errors?.length && (
                    <div className="rounded-[12px] border border-amber-500/30 bg-amber-500/[0.07] px-3 py-2 text-[11px] text-amber-700 dark:text-amber-400">
                      <div className="flex items-center gap-1 font-medium">
                        <CircleAlert className="h-3.5 w-3.5" />
                        {t('platforms.usageErrors')}
                      </div>
                      <ul className="mt-1 space-y-0.5">
                        {usage.errors.map((e, i) => (
                          <li key={i} className="break-all">
                            · {e.endpoint}: {e.message}
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                  <pre className="max-h-[46vh] overflow-auto rounded-[12px] bg-muted/60 p-3 font-mono text-[10px] leading-4">
                    {JSON.stringify(
                      {session_usage: usage.session_usage, entitlement: usage.entitlement},
                      null,
                      2,
                    )}
                  </pre>
                </div>
              )}
            </div>
          </DialogBody>
          <DialogFooter>
            <Button variant="outline" className="rounded-full" onClick={() => setUsageFor(null)}>
              {t('common.close')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* ── 手工导入弹窗 ── */}
      <Dialog open={importOpen} onOpenChange={(open) => { if (!open && !importBusy) setImportOpen(false); }}>
        <DialogContent className="max-w-[560px]" showCloseButton>
          <DialogHeader>
            <DialogTitle>{t('platforms.importTitle')}</DialogTitle>
            <DialogDescription>{t('platforms.importDesc')}</DialogDescription>
          </DialogHeader>
          <DialogBody className="max-h-[min(640px,calc(100dvh-12rem))]">
            <div className="px-6 pb-2">
              <Textarea
                value={importText}
                onChange={(e) => setImportText(e.target.value)}
                placeholder={t('platforms.importPlaceholder')}
                className="min-h-40 font-mono text-[11px]"
                spellCheck={false}
                autoFocus
              />
            </div>
          </DialogBody>
          <DialogFooter>
            <Button
              variant="outline"
              className="rounded-full"
              disabled={importBusy}
              onClick={() => setImportOpen(false)}
            >
              {t('common.cancel')}
            </Button>
            <Button
              className="rounded-full"
              disabled={importBusy || !importText.trim()}
              onClick={() => void submitImport()}
            >
              {importBusy ? <Loader2 className="animate-spin" /> : <Plus />}
              {t('common.save')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

/** 首屏骨架：统计卡 + 平台选择 + 账号表 + 模型表。 */
function PlatformsSkeleton() {
  return (
    <>
      <div className="flex gap-2">
        <SkeletonBar className="h-8 w-24 rounded-full" />
        <SkeletonBar className="h-8 w-24 rounded-full" />
      </div>
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-3">
        {Array.from({length: 3}, (_, i) => (
          <div key={i} className="rounded-[20px] bg-muted p-3.5">
            <SkeletonBar className="h-3 w-16" />
            <SkeletonBar className="mt-2.5 h-5 w-20" />
          </div>
        ))}
      </div>
      {[0, 1].map((section) => (
        <section key={section} className="overflow-hidden rounded-[20px] bg-muted">
          <div className="border-b border-border/60 px-4 py-3">
            <SkeletonBar className="h-3.5 w-24" />
          </div>
          <div className="divide-y divide-border/40">
            {Array.from({length: 3}, (_, i) => (
              <div key={i} className="flex items-center gap-4 px-4 py-3.5">
                <SkeletonBar className="h-3.5 w-36 max-w-[35%]" />
                <SkeletonBar className="h-3.5 w-14" />
                <SkeletonBar className="hidden h-3.5 w-24 sm:block" />
                <SkeletonBar className="hidden h-3.5 w-20 md:block" />
              </div>
            ))}
          </div>
        </section>
      ))}
    </>
  );
}
