'use client';

import {useCallback, useEffect, useMemo, useRef, useState} from 'react';
import {
  Activity,
  ArrowUpDown,
  CalendarClock,
  ChevronLeft,
  ChevronRight,
  CircleAlert,
  CircleCheck,
  ExternalLink,
  FileJson,
  KeyRound,
  Loader2,
  LogIn,
  MonitorDown,
  Pause,
  Play,
  Plus,
  RefreshCw,
  RotateCcw,
  Scale,
  Search,
  Snowflake,
  Trash2,
} from 'lucide-react';

import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {LoadError} from '@/components/common/states/LoadError';
import {SkeletonBar} from '@/components/common/states/SkeletonBar';
import {Button} from '@/components/ui/button';
import {Badge} from '@/components/ui/badge';
import {Input} from '@/components/ui/input';
import {Textarea} from '@/components/ui/textarea';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
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
import {errText, accountApi, platformApi} from '@/lib/api';
import {useAuth} from '@/lib/auth-context';
import {useT} from '@/lib/i18n/provider';
import {notify} from '@/lib/toast';
import {useAsyncAll} from '@/lib/use-async-data';
import {useNow} from '@/lib/use-now';
import {fmtDateTime, fmtRemain} from '@/lib/format';
import {paginate} from '@/lib/account-list';
import {
  PLATFORM_GROUPS,
  platformAvailabilityOf,
  platformGroupCounts,
  platformGroupLabelKey,
  selectPlatformAccounts,
  type PlatformGroupFilter,
  type PlatformSortKey,
} from '@/lib/platform-account-list';
import {useRealm} from '@/lib/realm-context';
import {
  filterByPlatformRealm,
  platformRealmOf,
  withRealmDefault,
} from '@/lib/platform-realm';
import {cn} from '@/lib/utils';
import {PlatformCoolingDialog} from './PlatformCoolingDialog';
import {PlatformWeightDialog} from './PlatformWeightDialog';
import type {
  CheckinLogPage,
  PlatformAccount,
  PlatformCheckinResult,
  PlatformCredits,
  PlatformImportResult,
  PlatformInfo,
  PlatformLoginStatus,
  PlatformSettings,
  PlatformUsage,
} from '@/lib/types';

/** ISO 时间串 → epoch 毫秒（`fmtDateTime` 认两种单位；解析不出来的交给它显示 —）。 */
function isoMs(value: string): number | null {
  if (!value) return null;
  const ms = Date.parse(value);
  return Number.isFinite(ms) ? ms : null;
}

/** 一页显示多少个账号（与 WorkBuddy 账号页同口径）。 */
const PAGE_SIZE = 20;

/** 令牌是否已经过期（后端用 ISO 串，前端只在展示时分一次）。 */
function isExpired(value: string): boolean {
  const ms = isoMs(value);
  return ms !== null && ms <= Date.now();
}

/** 积分显示：加载中 `…`，拿不到 `—`（失败原因由调用方写进 title）。 */
function creditText(entry: PlatformCredits | undefined, loading: boolean): string {
  if (entry && typeof entry.credits === 'number') return String(entry.credits);
  return loading ? '…' : '—';
}

/** 数值展示：拿不到给 `—`，小数保留两位（上游金额是浮点）。 */
function creditNum(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
  return Number.isInteger(value) ? String(value) : value.toFixed(2);
}

/** epoch 秒 → `MM-DD`（表格里只放得下这么多）。 */
function expiryDay(seconds: number | null | undefined): string {
  if (!seconds || !Number.isFinite(seconds)) return '';
  const date = new Date(seconds * 1000);
  if (Number.isNaN(date.getTime())) return '';
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${month}-${day}`;
}

/** 权益包清单 → 悬浮提示里的多行文本（每个包的金额、到期、来源）。 */
function expiryDetail(entry: PlatformCredits | undefined): string {
  const packs = entry?.expires ?? [];
  if (!packs.length) return '';
  return packs
    .map((pack) => {
      const when = fmtDateTime(pack.expire_at * 1000);
      return `${pack.amount} · ${when}${pack.label ? ` · ${pack.label}` : ''}`;
    })
    .join('\n');
}

/**
 * 非 WorkBuddy 平台的账号管理（原「多平台」页里的账号部分）。
 *
 * 页面外壳（标题、说明）由调用方提供，这里只负责一个平台的数据与操作：
 * 账号列表、积分、签到、令牌刷新、导入与网页登录。
 *
 * 权限档位由后端强制：刷新 / 签到要管理员；导入 / 删除 / 网页登录动的是凭据，
 * 只对会话管理员开放。这里只是把不可用的按钮藏掉。
 */
export function PlatformAccountsPanel({
  platform,
  label,
  info,
}: {
  platform: string;
  label: string;
  info: PlatformInfo | null;
}) {
  const t = useT();
  const {isAdmin} = useAuth();
  const {realm, label: realmName} = useRealm();

  const accountsState = useAsyncAll({
    accounts: () => platformApi.accounts(platform).then((r) => r.accounts),
  }, [platform]);

  const accounts = useMemo(
    () => accountsState.values.accounts ?? [],
    [accountsState.values.accounts],
  );

  /**
   * 版本过滤（Trae 这类分国内版 / 国际版的平台）。
   *
   * 账号一次全部取回，过滤在客户端做：切版本不重新请求，计数 / 分页也跟着
   * 当前版本走。`realm` 是界面口径（`cn` / `global`），账号里存的是平台口径
   * （`cn` / `intl`），翻译只在 `platform-realm` 那一处。
   */
  const realmAware = Boolean(info?.supports_realm);
  const realmScope = platformRealmOf(realm);
  const scopedAccounts = useMemo(
    () => (realmAware ? filterByPlatformRealm(accounts, realm) : accounts),
    [accounts, realm, realmAware],
  );

  // ── 平台调度：池级并发上限 ────────────────────────────
  //
  // 只做「池级」：一个平台上所有账号合计的在途上限。账号级并发**不提供**——
  // 上游没有这个字段，而账号一多，每号一个数字等于没有上限（见后端
  // `platforms/limiter.py`）。
  const [pool, setPool] = useState<PlatformSettings | null>(null);
  const [poolDraft, setPoolDraft] = useState('');
  const [poolBusy, setPoolBusy] = useState(false);

  useEffect(() => {
    let alive = true;
    platformApi.settings(platform)
      .then((value) => {
        if (!alive) return;
        setPool(value);
        setPoolDraft(String(value.max_in_flight));
      })
      .catch(() => {
        // 设置是辅助信息：取不到就不显示这一块，不打断账号列表。
        if (alive) setPool(null);
      });
    return () => { alive = false; };
  }, [platform]);

  async function commitPool() {
    if (!pool || poolBusy) return;
    const value = Number(poolDraft.trim());
    if (!Number.isInteger(value) || value < pool.limits.min || value > pool.limits.max) {
      notify.err(t('platforms.poolInvalid', {min: pool.limits.min, max: pool.limits.max}));
      setPoolDraft(String(pool.max_in_flight));
      return;
    }
    if (value === pool.max_in_flight) return;
    setPoolBusy(true);
    try {
      const next = await platformApi.saveSettings(platform, {max_in_flight: value});
      setPool(next);
      setPoolDraft(String(next.max_in_flight));
      notify.ok(t('platforms.poolSaved'));
    } catch (e) {
      notify.err(errText(e));
      setPoolDraft(String(pool.max_in_flight));
    } finally {
      setPoolBusy(false);
    }
  }

  // ── 账号调度弹窗 / 列表筛选 ─────────────────────────────
  //
  // 权重与冷却、冻结都改成弹窗 / 图标操作（与 WorkBuddy 账号页同一套交互）：
  // 表格只负责展示，避免「一个输入框逐键提交」那种中间态请求。
  const [weightTarget, setWeightTarget] = useState<PlatformAccount | null>(null);
  const [coolingTarget, setCoolingTarget] = useState<PlatformAccount | null>(null);
  /**
   * 检索 / 状态筛选 / 排序 / 页码（与 WorkBuddy 账号页同一套语义）。
   *
   * 平台账号一次全部取回，四件事全在客户端做，所以改条件不重新请求、
   * 也不需要防抖。
   */
  const [q, setQ] = useState('');
  const [statusGroup, setStatusGroup] = useState<PlatformGroupFilter>('all');
  const [sort, setSort] = useState<PlatformSortKey>('default');
  const [page, setPage] = useState(1);

  // ── 积分余额 ──────────────────────────────────────────
  const [credits, setCredits] = useState<Record<string, PlatformCredits>>({});
  const [creditsLoading, setCreditsLoading] = useState(false);
  /** 请求序号：切平台 / 连点刷新时旧请求可能后到，只认最新一次的结果。 */
  const creditsSeq = useRef(0);
  const accountsKey = accounts.map((a) => a.uid).join(',');

  const loadCredits = useCallback(async () => {
    if (!accountsKey) {
      creditsSeq.current += 1;
      setCredits({});
      setCreditsLoading(false);
      return;
    }
    const seq = ++creditsSeq.current;
    setCreditsLoading(true);
    try {
      const {credits: list} = await platformApi.credits(platform);
      if (seq !== creditsSeq.current) return;
      setCredits(Object.fromEntries(list.map((c) => [c.uid, c])));
    } catch {
      // 余额是辅助信息：失败就保持「—」，不弹 toast 打断用户。
      if (seq === creditsSeq.current) setCredits({});
    } finally {
      if (seq === creditsSeq.current) setCreditsLoading(false);
    }
  }, [platform, accountsKey]);

  useEffect(() => {
    void loadCredits();
  }, [loadCredits]);

  // ── 签到记录 ──────────────────────────────────────────
  //
  // 平台签到原先只弹一条 toast，关掉就再也看不到。这里读本端写入的
  // `checkin_logs`（带平台 / 版本 / 到账积分），与看板的「签到收入」同源。
  const [history, setHistory] = useState<CheckinLogPage | null>(null);
  const [historyLoading, setHistoryLoading] = useState(false);

  const loadHistory = useCallback(async () => {
    setHistoryLoading(true);
    try {
      const page = await accountApi.checkinLogs(20, 0, undefined, undefined,
                                                realm, platform);
      setHistory(page);
    } catch {
      // 记录是辅助信息：拿不到就保持空态，不打断账号列表。
      setHistory(null);
    } finally {
      setHistoryLoading(false);
    }
  }, [platform, realm]);

  useEffect(() => {
    void loadHistory();
  }, [loadHistory]);

  // 冷却到期的判定与倒计时需要「当前时间」：60 秒足够（冷却以小时 / 天计），
  // 不必让整页每秒钟重渲染。
  const now = useNow(60000);
  const creditOf = useCallback((account: PlatformAccount): number | null => {
    const value = credits[account.uid]?.credits;
    return typeof value === 'number' && Number.isFinite(value) ? value : null;
  }, [credits]);
  const counts = useMemo(
    () => platformGroupCounts(scopedAccounts, now),
    [scopedAccounts, now],
  );
  const visible = useMemo(
    () => selectPlatformAccounts(scopedAccounts, {q, group: statusGroup, sort}, creditOf, now),
    [scopedAccounts, q, statusGroup, sort, creditOf, now],
  );
  const paged = useMemo(() => paginate(visible, page, PAGE_SIZE), [visible, page]);

  // 切版本 / 切平台后回到第一页：旧页码在过滤后的列表里可能已经越界。
  useEffect(() => { setPage(1); }, [realm, platform]);

  // ── 弹窗状态 ──────────────────────────────────────────
  const [importOpen, setImportOpen] = useState(false);
  const [importText, setImportText] = useState('');
  const [importBusy, setImportBusy] = useState(false);

  const [usageFor, setUsageFor] = useState<PlatformAccount | null>(null);
  const [usage, setUsage] = useState<PlatformUsage | null>(null);
  const [usageLoading, setUsageLoading] = useState(false);
  const [usageError, setUsageError] = useState('');
  const usageCredit = usageFor ? credits[usageFor.uid] : undefined;

  /** 批量 / 单账号操作进行中标记（按 uid 或 'all'），用于禁用按钮。 */
  const [busy, setBusy] = useState('');

  const [login, setLogin] = useState<{
    id: string;
    url: string;
    status: PlatformLoginStatus;
    message: string;
  } | null>(null);
  const [loginStarting, setLoginStarting] = useState(false);
  /** 远程登录兜底：浏览器打不开 127.0.0.1 回调页时，粘贴地址栏里的完整链接。 */
  const [callbackUrl, setCallbackUrl] = useState('');
  const [callbackBusy, setCallbackBusy] = useState(false);
  /** 轮询令牌：关闭弹窗 / 重新发起时自增，让在途的轮询自己退出。 */
  const loginToken = useRef(0);

  const reloadAll = useCallback(async () => {
    const ok = await accountsState.reload();
    void loadCredits();
    if (!ok) notify.warn(errText(accountsState.errors.accounts));
    return ok;
  }, [accountsState, loadCredits]);

  /** 网页登录轮询：2 秒一次，直到状态不再是 pending / exchanging。 */
  const pollLogin = useCallback(async (name: string, id: string) => {
    const token = ++loginToken.current;
    const tick = async (): Promise<void> => {
      if (token !== loginToken.current) return;
      try {
        const out = await platformApi.loginPoll(name, id);
        if (token !== loginToken.current) return;
        setLogin((prev) => prev && prev.id === id
          ? {...prev, status: out.status, message: out.message}
          : prev);
        if (out.status === 'success') {
          notify.ok(t('platforms.loginSuccess'),
            out.account?.nickname || out.account?.uid || undefined);
          void reloadAll();
          return;
        }
        if (out.status === 'error' || out.status === 'expired') {
          if (out.status === 'error') notify.err(out.message || t('platforms.loginFailed'));
          return;
        }
        setTimeout(() => { void tick(); }, 2000);
      } catch (e) {
        if (token !== loginToken.current) return;
        setLogin((prev) => prev && prev.id === id
          ? {...prev, status: 'expired', message: errText(e)}
          : prev);
      }
    };
    void tick();
  }, [reloadAll, t]);

  async function startWebLogin() {
    if (loginStarting) return;
    setLoginStarting(true);
    setCallbackUrl('');
    if (login) void platformApi.loginCancel(platform, login.id).catch(() => {});
    try {
      const out = await platformApi.loginStart(platform, realmAware ? realmScope : 'cn');
      setLogin({id: out.login_id, url: out.url, status: 'pending', message: ''});
      window.open(out.url, '_blank', 'noopener,noreferrer');
      void pollLogin(platform, out.login_id);
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setLoginStarting(false);
    }
  }

  /** 提交粘贴回来的回调地址；成功后继续用轮询等待兑换结果。 */
  async function submitLoginCallback() {
    if (!login || callbackBusy) return;
    const value = callbackUrl.trim();
    if (!value) {
      notify.warn(t('platforms.loginCallbackRequired'));
      return;
    }
    setCallbackBusy(true);
    try {
      const out = await platformApi.loginCallback(platform, login.id, value);
      setLogin((prev) => prev && prev.id === login.id
        ? {...prev, status: out.status, message: out.message}
        : prev);
      setCallbackUrl('');
      if (out.status === 'success') {
        notify.ok(t('platforms.loginSuccess'),
          out.account?.nickname || out.account?.uid || undefined);
        void reloadAll();
      } else if (out.status !== 'error' && out.status !== 'expired') {
        void pollLogin(platform, login.id);
      }
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setCallbackBusy(false);
    }
  }

  function closeLogin() {
    loginToken.current += 1;
    const id = login?.id;
    setLogin(null);
    setCallbackUrl('');
    if (id) void platformApi.loginCancel(platform, id).catch(() => {});
  }

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

  async function importDesktop() {
    if (busy) return;
    setBusy('import');
    const pending = notify.info(t('common.loading'));
    try {
      const result = await platformApi.importDesktop(
        platform, realmAware ? realmScope : undefined);
      notify.dismiss(pending);
      if (!result.imported.length && !result.failed.length) {
        notify.info(t('platforms.desktopNone'));
      } else if (result.imported.length && !result.failed.length) {
        notify.ok(t('platforms.desktopDone', {n: result.imported.length}));
      } else {
        announceImport(result);
      }
      await reloadAll();
    } catch (e) {
      notify.dismiss(pending);
      notify.err(errText(e));
    } finally {
      setBusy('');
    }
  }

  async function submitImport() {
    if (importBusy) return;
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
      // 分版本的平台：当前在国际版时，给没写 realm 的条目补上 intl，
      // 否则后端会按默认值把它们塞进国内版账号池。
      const bodyForImport = realmAware ? withRealmDefault(body, realm) : body;
      const result = await platformApi.import(platform, bodyForImport);
      announceImport(result);
      setImportOpen(false);
      setImportText('');
      await reloadAll();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setImportBusy(false);
    }
  }

  async function refreshAccount(account: PlatformAccount) {
    if (busy) return;
    setBusy(account.uid);
    const pending = notify.info(t('common.loading'));
    try {
      await platformApi.refresh(platform, account.uid);
      notify.dismiss(pending);
      notify.ok(t('platforms.refreshDone'));
      await reloadAll();
    } catch (e) {
      notify.dismiss(pending);
      notify.err(errText(e));
    } finally {
      setBusy('');
    }
  }

  /** 临时冻结 / 恢复：只影响选号与批量任务，不动凭据。 */
  async function toggleDisabled(account: PlatformAccount) {
    if (busy) return;
    const next = !account.disabled;
    setBusy(account.uid);
    try {
      await platformApi.updateAccount(platform, account.uid, {disabled: next});
      notify.ok(next ? t('platforms.freezeDone') : t('platforms.unfreezeDone'));
      await reloadAll();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy('');
    }
  }

  async function clearCooling(account: PlatformAccount) {
    if (busy) return;
    setBusy(account.uid);
    try {
      await platformApi.clearCooling(platform, account.uid);
      notify.ok(t('platforms.clearCoolingDone'));
      await reloadAll();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy('');
    }
  }

  async function checkin(account: PlatformAccount) {
    if (busy) return;
    setBusy(account.uid);
    const pending = notify.info(t('common.loading'));
    try {
      const {result} = await platformApi.checkin(platform, account.uid);
      notify.dismiss(pending);
      if (typeof result.credits === 'number') {
        setCredits((prev) => ({
          ...prev,
          [account.uid]: {
            uid: account.uid,
            credits: result.credits,
            checked_in: result.checked_in,
          },
        }));
      }
      announceCheckin(result);
      void loadCredits();
      void loadHistory();
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
    if (busy) return;
    setBusy('all');
    const pending = notify.info(t('common.loading'));
    try {
      const result = await platformApi.checkinAll(platform);
      notify.dismiss(pending);
      if (result.ok === result.total) {
        notify.ok(t('platforms.checkinAllDone', {ok: result.ok, total: result.total}));
      } else {
        const first = result.results.find((r) => !r.ok);
        notify.warn(t('platforms.checkinAllDone', {ok: result.ok, total: result.total}),
          first ? `${first.uid}: ${first.message}` : undefined);
      }
      void loadCredits();
      void loadHistory();
    } catch (e) {
      notify.dismiss(pending);
      notify.err(errText(e));
    } finally {
      setBusy('');
    }
  }

  async function removeAccount(account: PlatformAccount) {
    await platformApi.remove(platform, account.uid);
    notify.ok(t('platforms.deleted'));
    await reloadAll();
  }

  async function openUsage(account: PlatformAccount) {
    setUsageFor(account);
    setUsage(null);
    setUsageError('');
    setUsageLoading(true);
    try {
      const {usage: value} = await platformApi.usage(platform, account.uid);
      setUsage(value);
    } catch (e) {
      setUsageError(errText(e));
    } finally {
      setUsageLoading(false);
    }
  }

  const supportsCheckin = !!info?.supports_checkin;
  const supportsDesktop = !!info?.supports_desktop_import;
  const supportsWebLogin = !!info?.supports_web_login;
  const supportsCredits = !!info?.supports_credits;
  // 非池化平台（Qoder）没有池内权重 / 强制冷却 API；旧后端没有这两个字段，
  // 缺省按「支持」处理，保持 Trae / WorkBuddy 的现有界面不变。
  const supportsWeight = info?.supports_weight !== false;
  const supportsCooling = info?.supports_cooling !== false;
  const supportsLoginCallback = info?.supports_login_callback !== false;
  const busyAny = busy !== '';
  // 积分来源按平台区分：Trae 走权益接口，Qoder 走内置网关的余额快照。
  // 以前这里只有一句写死「Trae 权益接口」的文案，Qoder 页会显示错误的说明。
  const creditsHint = platform === 'qoder'
    ? t('platforms.creditsHintQoder')
    : platform === 'trae'
      ? t('platforms.creditsHintTrae')
      : t('platforms.creditsHint');

  const loginStatusText = (() => {
    if (!login) return '';
    switch (login.status) {
      case 'pending':
        return t('platforms.loginWaiting');
      case 'exchanging':
        return t('platforms.loginExchanging');
      case 'success':
        return t('platforms.loginSuccess');
      case 'expired':
        return t('platforms.loginExpired');
      default:
        return login.message || t('platforms.loginFailed');
    }
  })();

  return (
    <>
      <section className="overflow-hidden rounded-[20px] bg-muted">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border/60 px-3.5 py-3">
          <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
            <KeyRound className="h-3.5 w-3.5" />
            <h2 className="text-xs font-medium text-foreground">{t('platforms.accountsTitle')}</h2>
            <span>· {label}</span>
            <Badge variant="secondary" className="rounded-md px-1.5 text-[10px] tabular-nums">
              {scopedAccounts.length}
            </Badge>
          </div>
          <div className="flex flex-wrap items-center gap-1.5">
            {/* 池级并发：一个平台上所有账号合计的在途上限（0 = 不限）。
                非管理员只读展示——它影响的是全局吞吐，不是单个账号的开关。 */}
            {pool && (
              <label
                className="flex items-center gap-1.5 rounded-full border border-border/60 px-2.5 py-1 text-[11px] text-muted-foreground"
                title={t('platforms.poolHint')}
              >
                <span className="whitespace-nowrap">{t('platforms.poolLimit')}</span>
                {isAdmin ? (
                  <Input
                    value={poolDraft}
                    onChange={(e) => setPoolDraft(e.target.value)}
                    onBlur={() => void commitPool()}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') {
                        e.preventDefault();
                        void commitPool();
                      }
                    }}
                    inputMode="numeric"
                    disabled={poolBusy}
                    aria-label={t('platforms.poolLimit')}
                    className="h-6 w-12 border-0 bg-transparent px-1 text-center text-[11px] tabular-nums shadow-none focus-visible:ring-0"
                  />
                ) : (
                  <span className="tabular-nums text-foreground">{pool.max_in_flight}</span>
                )}
                <span className="text-[10px] opacity-70">
                  {pool.max_in_flight === 0 ? t('platforms.poolUnlimited') : ''}
                </span>
              </label>
            )}
            <Button
              variant="outline"
              size="sm"
              className="h-7 rounded-full px-2.5 text-[11px]"
              disabled={accountsState.isRefreshing}
              onClick={() => void reloadAll()}
            >
              {accountsState.isRefreshing ? <Loader2 className="animate-spin" /> : <RefreshCw />}
              {t('common.refresh')}
            </Button>
            {isAdmin && (
              <>
                {supportsWebLogin && (
                  <Button
                    size="sm"
                    className="h-7 rounded-full px-2.5 text-[11px]"
                    disabled={busyAny || loginStarting || login !== null}
                    onClick={() => void startWebLogin()}
                    title={t('platforms.loginHint')}
                  >
                    {loginStarting ? <Loader2 className="animate-spin" /> : <LogIn />}
                    {t('platforms.login')}
                  </Button>
                )}
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
              </>
            )}
          </div>
        </div>

        {/* 检索 / 状态筛选 / 排序：与 WorkBuddy 账号页同一套语义。
            账号还没取到时整块不渲染——那时计数全是 0，摆出来等于说
            「一个账号都没有」。 */}
        {scopedAccounts.length > 0 && (
          <div className="flex flex-col gap-3 border-b border-border/60 px-3.5 py-3 lg:flex-row lg:items-center">
            <div className="relative flex-1">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
              <Input
                value={q}
                onChange={(e) => { setQ(e.target.value); setPage(1); }}
                placeholder={t('platforms.searchPlaceholder')}
                className="h-9 bg-background pl-8"
              />
            </div>
            <div className="-mx-0.5 flex flex-wrap items-center gap-1.5 overflow-x-auto px-0.5 pb-0.5">
              <span className="flex shrink-0 items-center gap-1 text-[11px] text-muted-foreground">
                {t('accounts.colStatus')}
              </span>
              {(['all', ...PLATFORM_GROUPS] as const).map((g) => (
                <Button
                  key={g}
                  variant={statusGroup === g ? 'default' : 'outline'}
                  size="sm"
                  className="h-7 shrink-0 rounded-full px-2.5 text-[11px]"
                  onClick={() => { setStatusGroup(g); setPage(1); }}
                >
                  {g === 'all' ? t('common.all') : t(platformGroupLabelKey(g))}
                  <span className="ml-1 tabular-nums opacity-70">{counts[g]}</span>
                </Button>
              ))}
            </div>
            <div className="flex shrink-0 items-center gap-1.5">
              <ArrowUpDown className="h-3.5 w-3.5 text-muted-foreground" />
              <Select value={sort} onValueChange={(v) => { setSort(v as PlatformSortKey); setPage(1); }}>
                <SelectTrigger className="h-9 w-[160px] bg-background" aria-label={t('accounts.sortLabel')}>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="default">{t('accounts.sortDefault')}</SelectItem>
                  <SelectItem value="expires">{t('platforms.sortExpires')}</SelectItem>
                  <SelectItem value="credits">{t('accounts.sortCredits')}</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
        )}

        {!!accountsState.errors.accounts && (
          <div className="px-3.5 pt-3">
            <LoadError
              variant="inline"
              message={errText(accountsState.errors.accounts)}
              onRetry={accountsState.reload}
            />
          </div>
        )}

        {accountsState.isInitialLoading ? (
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
            {isAdmin && (
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
        ) : realmAware && scopedAccounts.length === 0 ? (
          /* 有账号，但都属于另一个版本：这时说「还没有账号」是假话，
             会让用户去重复导入。明确提示换版本。 */
          <EmptyState
            icon={KeyRound}
            title={t('accounts.emptyRealmTitle', {realm: realmName})}
            description={t('accounts.emptyRealmDesc')}
            className="flex flex-col items-center justify-center p-8 text-center"
          >
            {isAdmin && (
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
        ) : paged.total === 0 ? (
          <EmptyState
            icon={Search}
            title={t('accounts.noMatchTitle')}
            description={t('accounts.noMatchDesc')}
            className="flex flex-col items-center justify-center p-8 text-center"
          />
        ) : (
          <>
          <div className="overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow className="border-b border-border/60 hover:bg-transparent">
                  <TableHead className="pl-4 text-[11px] text-muted-foreground">
                    {t('platforms.colAccount')}
                  </TableHead>
                  <TableHead className="text-[11px] text-muted-foreground">
                    {t('accounts.colStatus')}
                  </TableHead>
                  <TableHead className="text-[11px] text-muted-foreground">
                    {t('platforms.colRealm')}
                  </TableHead>
                  {supportsWeight && (
                    <TableHead
                      className="hidden text-[11px] text-muted-foreground md:table-cell"
                      title={t('platforms.weightHint')}
                    >
                      {t('platforms.colWeight')}
                    </TableHead>
                  )}
                  {supportsCredits && (
                    <TableHead className="hidden text-[11px] text-muted-foreground sm:table-cell">
                      {t('platforms.colCredits')}
                    </TableHead>
                  )}
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
                {paged.rows.map((a) => {
                  const rowBusy = busy === a.uid;
                  const expired = isExpired(a.expiredAt);
                  const group = platformAvailabilityOf(a, now);
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
                      {/* 状态：正常 / 需处理（令牌问题）/ 冷却 / 冻结。
                          冻结与冷却的原因直接放在提示里，悬停即可看到。 */}
                      <TableCell>
                        <div className="flex flex-col gap-0.5 py-0.5">
                          <Badge
                            variant="secondary"
                            className={cn(
                              'w-fit rounded-md text-[10px]',
                              group === 'usable' && 'text-emerald-700 dark:text-emerald-400',
                              group === 'attention' && 'text-rose-700 dark:text-rose-400',
                              group === 'cooling' && 'text-amber-700 dark:text-amber-400',
                            )}
                            title={
                              group === 'stopped'
                                ? (a.disabled_reason
                                  ? `${t('platforms.frozenHint')} · ${a.disabled_reason}`
                                  : t('platforms.frozenHint'))
                                : group === 'cooling'
                                  ? t('platforms.coolingHint', {
                                    until: fmtDateTime(a.cooling_until * 1000),
                                  }) + (a.cooling_reason ? ` · ${a.cooling_reason}` : '')
                                  : undefined
                            }
                          >
                            {group === 'usable'
                              ? t('accounts.badgeOnline')
                              : group === 'cooling'
                                ? t('accounts.badgeCooling')
                                : group === 'stopped'
                                  ? t('accounts.badgeDisabledByPanel')
                                  : !a.has_token || expired
                                    ? t('accounts.badgeExpired')
                                    : t('accounts.groupAttention')}
                          </Badge>
                          {group === 'cooling' && a.cool_remaining_sec > 0 && (
                            <span className="text-[10px] text-muted-foreground">
                              {fmtRemain(a.cool_remaining_sec)}
                            </span>
                          )}
                        </div>
                      </TableCell>
                      <TableCell>
                        <Badge variant="secondary" className="rounded-md text-[10px]">
                          {a.realm_label || a.realm}
                        </Badge>
                      </TableCell>
                      {/* 权重：同一平台内多个账号之间的分摊比例。0 / 留空 = 默认 1.0，
                          与 WorkBuddy 账号权重的口径一致；编辑入口在右侧操作列。
                          非池化平台没有池内权重，整列隐藏。 */}
                      {supportsWeight && (
                        <TableCell className="hidden md:table-cell">
                          <span
                            className={cn(
                              'text-xs tabular-nums',
                              a.weight ? 'font-medium text-primary' : 'text-muted-foreground',
                            )}
                            title={t('platforms.weightHint')}
                          >
                            {a.weight ? `×${a.weight}` : t('platforms.weightDefault')}
                          </span>
                        </TableCell>
                      )}
                      {supportsCredits && (
                        <TableCell className="hidden sm:table-cell">
                          <div className="flex flex-col gap-0.5 py-0.5">
                            <span
                              className={cn(
                                'text-xs tabular-nums',
                                credits[a.uid]?.error
                                  ? 'text-muted-foreground'
                                  : 'text-foreground',
                              )}
                              title={credits[a.uid]?.error || creditsHint}
                            >
                              {creditText(credits[a.uid], creditsLoading)}
                              {typeof credits[a.uid]?.checkin_credits === 'number'
                                && (credits[a.uid]?.checkin_credits as number) > 0 && (
                                  <span className="ml-1 text-[10px] text-muted-foreground">
                                    {t('platforms.creditsCheckinShort', {
                                      n: creditNum(credits[a.uid]?.checkin_credits),
                                    })}
                                  </span>
                                )}
                            </span>
                            {!!credits[a.uid]?.next_expire_at && (
                              <span
                                className="text-[10px] text-muted-foreground"
                                title={expiryDetail(credits[a.uid])}
                              >
                                {t('platforms.creditsExpireAt', {
                                  date: expiryDay(credits[a.uid]?.next_expire_at),
                                })}
                              </span>
                            )}
                          </div>
                        </TableCell>
                      )}
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
                            : a.source === 'web'
                              ? t('platforms.sourceWeb')
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
                              {supportsWeight && (
                                <Button
                                  variant="ghost"
                                  size="icon"
                                  className="h-7 w-7 rounded-md"
                                  title={`${t('platforms.colWeight')}${a.weight ? ' ×' + a.weight : ''}`}
                                  disabled={rowBusy || busyAny}
                                  onClick={() => setWeightTarget(a)}
                                >
                                  <Scale className={cn('h-3.5 w-3.5', a.weight && 'text-primary')} />
                                </Button>
                              )}
                              {supportsCooling && (
                                <Button
                                  variant="ghost"
                                  size="icon"
                                  className={cn(
                                    'h-7 w-7 rounded-md',
                                    a.cooling
                                      ? 'text-sky-600 hover:text-sky-700 dark:text-sky-400'
                                      : 'text-sky-500 hover:text-sky-600',
                                  )}
                                  title={a.cooling ? t('accounts.adjustCooling') : t('accounts.forceCooling')}
                                  disabled={rowBusy || busyAny}
                                  onClick={() => setCoolingTarget(a)}
                                >
                                  <Snowflake className="h-3.5 w-3.5" />
                                </Button>
                              )}
                              {supportsCooling && a.cooling && (
                                <ConfirmDialog
                                  title={t('platforms.clearCoolingTitle')}
                                  description={t('platforms.clearCoolingDesc')}
                                  confirmText={t('platforms.clearCooling')}
                                  destructive
                                  onConfirm={() => clearCooling(a)}
                                  trigger={
                                    <Button
                                      variant="ghost"
                                      size="icon"
                                      className="h-7 w-7 rounded-md text-rose-600 hover:text-rose-700 dark:text-rose-400"
                                      title={t('platforms.clearCooling')}
                                      disabled={rowBusy || busyAny}
                                    >
                                      <RotateCcw className="h-3.5 w-3.5" />
                                    </Button>
                                  }
                                />
                              )}
                              {supportsCheckin && !a.disabled && (
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
                              <Button
                                variant="ghost"
                                size="icon"
                                className={cn(
                                  'h-7 w-7 rounded-md',
                                  a.disabled
                                    ? 'text-emerald-600 hover:text-emerald-600 dark:text-emerald-400'
                                    : 'text-amber-600 hover:text-amber-600 dark:text-amber-400',
                                )}
                                title={a.disabled ? t('platforms.unfreeze') : t('platforms.freeze')}
                                disabled={rowBusy || busyAny}
                                onClick={() => void toggleDisabled(a)}
                              >
                                {a.disabled
                                  ? <Play className="h-3.5 w-3.5" />
                                  : <Pause className="h-3.5 w-3.5" />}
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
          {paged.pages > 1 && (
            <div className="flex items-center justify-between gap-2 border-t border-border/60 px-3.5 py-2.5">
              <span className="text-[11px] text-muted-foreground">
                {t('platforms.pageInfo', {
                  page: paged.page,
                  pages: paged.pages,
                  total: paged.total,
                })}
              </span>
              <div className="flex items-center gap-1">
                <Button
                  variant="outline"
                  size="sm"
                  className="h-7 rounded-full px-2 text-[11px]"
                  disabled={paged.page <= 1}
                  onClick={() => setPage(paged.page - 1)}
                >
                  <ChevronLeft className="h-3.5 w-3.5" />
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  className="h-7 rounded-full px-2 text-[11px]"
                  disabled={paged.page >= paged.pages}
                  onClick={() => setPage(paged.page + 1)}
                >
                  <ChevronRight className="h-3.5 w-3.5" />
                </Button>
              </div>
            </div>
          )}
          </>
        )}
      </section>

      {/* ── 签到记录 ──────────────────────────────────────
          平台签到原先只弹一条 toast，关掉就再也看不到。这里读本端写入的
          `checkin_logs`（带平台 / 版本 / 到账积分），与看板的「签到收入」同源。 */}
      <section className="overflow-hidden rounded-[20px] bg-muted">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border/60 px-3.5 py-3">
          <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
            <CalendarClock className="h-3.5 w-3.5" />
            <h2 className="text-xs font-medium text-foreground">
              {t('platforms.checkinHistoryTitle')}
            </h2>
            <span>· {label}</span>
            {history && (
              <Badge variant="secondary" className="rounded-md px-1.5 text-[10px] tabular-nums">
                {history.total}
              </Badge>
            )}
          </div>
          <Button
            variant="ghost"
            size="sm"
            className="h-7 rounded-full px-2 text-[11px]"
            title={t('common.refresh')}
            disabled={historyLoading}
            onClick={() => void loadHistory()}
          >
            {historyLoading
              ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
              : <RefreshCw className="h-3.5 w-3.5" />}
          </Button>
        </div>
        {historyLoading && !history ? (
          <div className="space-y-2 px-3.5 py-3">
            <SkeletonBar className="h-4 w-40" />
            <SkeletonBar className="h-4 w-64" />
          </div>
        ) : history && history.items.length ? (
          <div className="divide-y divide-border/40">
            {history.items.map((item) => (
              <div
                key={item.id}
                className="flex flex-wrap items-center justify-between gap-2 px-3.5 py-2"
              >
                <div className="flex min-w-0 flex-col gap-0.5">
                  <span className="truncate text-xs font-medium">
                    {item.nickname || item.uid || '—'}
                  </span>
                  <span className="text-[11px] tabular-nums text-muted-foreground">
                    {fmtDateTime(item.ts)}
                  </span>
                </div>
                <div className="flex shrink-0 items-center gap-2">
                  {typeof item.credits === 'number' && item.credits > 0 && (
                    <span
                      className="text-[11px] font-medium tabular-nums text-emerald-600 dark:text-emerald-400"
                      title={t('platforms.checkinHistoryCredits')}
                    >
                      +{item.credits}
                    </span>
                  )}
                  <span
                    className={cn(
                      'max-w-[220px] truncate text-[11px]',
                      item.success
                        ? 'text-emerald-600 dark:text-emerald-400'
                        : 'text-red-600 dark:text-red-400',
                    )}
                    title={item.message}
                  >
                    {item.message
                      || (item.success
                        ? t('platforms.checkinHistorySuccess')
                        : t('platforms.checkinHistoryFailed'))}
                  </span>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div className="px-4 py-8 text-center text-xs text-muted-foreground">
            {t('platforms.checkinHistoryEmpty')}
          </div>
        )}
        {history && history.total > history.items.length && (
          <div className="border-t border-border/60 px-3.5 py-2 text-[11px] text-muted-foreground">
            {t('platforms.checkinHistoryMore', {
              shown: history.items.length,
              total: history.total,
            })}
          </div>
        )}
      </section>

      {/* 调度参数弹窗：权重 / 强制冷却。与 WorkBuddy 账号页同一套交互。 */}
      <PlatformWeightDialog
        platform={platform}
        account={weightTarget}
        open={weightTarget !== null}
        onOpenChange={(open) => { if (!open) setWeightTarget(null); }}
        onSaved={() => void reloadAll()}
      />
      <PlatformCoolingDialog
        platform={platform}
        account={coolingTarget}
        open={coolingTarget !== null}
        onOpenChange={(open) => { if (!open) setCoolingTarget(null); }}
        onSuccess={() => void reloadAll()}
      />

      {/* ── 用量弹窗 ── */}
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
                  {supportsCredits && usageFor && (
                    <div className="rounded-[12px] border border-border/60 bg-muted/30 px-3 py-2">
                      <div className="flex items-center justify-between gap-2">
                        <span className="text-xs text-muted-foreground">
                          {t('platforms.creditsBalance')}
                        </span>
                        <span
                          className="text-sm font-medium tabular-nums"
                          title={usageCredit?.error || creditsHint}
                        >
                          {creditText(usageCredit, creditsLoading)}
                        </span>
                      </div>
                      <div className="mt-1.5 grid grid-cols-2 gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
                        <span>{t('platforms.creditsLimit')}</span>
                        <span className="text-right tabular-nums text-foreground">
                          {creditNum(usageCredit?.limit)}
                        </span>
                        <span>{t('platforms.creditsUsed')}</span>
                        <span className="text-right tabular-nums text-foreground">
                          {creditNum(usageCredit?.used)}
                        </span>
                        <span>{t('platforms.creditsCheckin')}</span>
                        <span className="text-right tabular-nums text-foreground">
                          {creditNum(usageCredit?.checkin_credits)}
                          {usageCredit?.checked_in
                            ? ` · ${t('platforms.checkinAlready')}`
                            : ''}
                        </span>
                      </div>
                      {!!usageCredit?.expires?.length && (
                        <div className="mt-2 border-t border-border/60 pt-2">
                          <div className="mb-1 flex items-center gap-1 text-[11px] text-muted-foreground">
                            <CalendarClock className="h-3.5 w-3.5" />
                            {t('platforms.creditsExpiry')}
                          </div>
                          <ul className="space-y-0.5">
                            {usageCredit.expires.map((pack, i) => (
                              <li
                                key={i}
                                className="flex items-center justify-between gap-2 text-[11px]"
                              >
                                <span className="truncate text-muted-foreground">
                                  {pack.label || t('platforms.creditsPack')}
                                </span>
                                <span className="shrink-0 tabular-nums text-foreground">
                                  {creditNum(pack.amount)} · {fmtDateTime(pack.expire_at * 1000)}
                                </span>
                              </li>
                            ))}
                          </ul>
                        </div>
                      )}
                    </div>
                  )}
                  {usage.session_usage === null && usage.entitlement === null
                    && typeof usageCredit?.credits !== 'number' && (
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

      {/* ── 网页登录弹窗 ── */}
      <Dialog open={login !== null} onOpenChange={(open) => { if (!open) closeLogin(); }}>
        <DialogContent className="max-w-[520px]" showCloseButton>
          <DialogHeader>
            <DialogTitle>{t('platforms.loginTitle', {label})}</DialogTitle>
            <DialogDescription>
              {supportsLoginCallback
                ? t('platforms.loginDesc')
                : t('platforms.loginDeviceDesc')}
            </DialogDescription>
          </DialogHeader>
          <DialogBody>
            <div className="space-y-3 px-6 pb-2">
              {supportsLoginCallback && (
                <div className="flex items-start gap-2 rounded-[12px] bg-muted/60 p-3 text-[11px] leading-5 text-muted-foreground">
                  <CircleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-500" />
                  <span>{t('platforms.loginSameMachine')}</span>
                </div>
              )}
              <div className="flex items-start gap-2 text-xs">
                {login?.status === 'success'
                  ? <CircleCheck className="mt-0.5 h-4 w-4 shrink-0 text-emerald-500" />
                  : login?.status === 'error' || login?.status === 'expired'
                    ? <CircleAlert className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
                    : <Loader2 className="mt-0.5 h-4 w-4 shrink-0 animate-spin text-muted-foreground" />}
                <span className="break-all leading-5">{loginStatusText}</span>
              </div>
              {!!login?.url && (
                <Button
                  variant="outline"
                  size="sm"
                  className="rounded-full"
                  onClick={() => {
                    const url = login?.url;
                    if (url) window.open(url, '_blank', 'noopener,noreferrer');
                  }}
                >
                  <ExternalLink />
                  {t('platforms.loginOpen')}
                </Button>
              )}
              {login?.status === 'pending' && supportsLoginCallback && (
                <div className="space-y-1.5">
                  <div className="text-[11px] font-medium text-foreground">
                    {t('platforms.loginCallbackLabel')}
                  </div>
                  <form
                    className="flex items-center gap-1.5"
                    onSubmit={(e) => { e.preventDefault(); void submitLoginCallback(); }}
                  >
                    <Input
                      value={callbackUrl}
                      onChange={(e) => setCallbackUrl(e.target.value)}
                      placeholder={t('platforms.loginCallbackPlaceholder')}
                      className="h-8 rounded-xl text-xs"
                      disabled={callbackBusy}
                      autoComplete="off"
                      spellCheck={false}
                    />
                    <Button
                      type="submit"
                      size="sm"
                      className="h-8 shrink-0 rounded-full px-3 text-[11px]"
                      disabled={callbackBusy}
                    >
                      {callbackBusy ? <Loader2 className="animate-spin" /> : null}
                      {t('platforms.loginCallbackSubmit')}
                    </Button>
                  </form>
                  <p className="text-[11px] leading-5 text-muted-foreground">
                    {t('platforms.loginCallbackHint')}
                  </p>
                </div>
              )}
            </div>
          </DialogBody>
          <DialogFooter>
            {(login?.status === 'error' || login?.status === 'expired') && (
              <Button
                className="rounded-full"
                disabled={loginStarting}
                onClick={() => void startWebLogin()}
              >
                {loginStarting ? <Loader2 className="animate-spin" /> : <LogIn />}
                {t('platforms.loginRetry')}
              </Button>
            )}
            <Button variant="outline" className="rounded-full" onClick={closeLogin}>
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
    </>
  );
}
