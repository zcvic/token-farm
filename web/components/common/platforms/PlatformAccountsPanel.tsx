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
  Gift,
  Globe,
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
import {PageHeader} from '@/components/common/layout/PageHeader';
import {CreditCountdown} from '@/components/common/accounts/CreditCountdown';
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
  creditUnit,
  formatCreditValue,
  platformCreditExpiries,
  platformCreditPacks,
} from '@/lib/platform-credits';
import {
  PLATFORM_GROUPS,
  platformAvailabilityOf,
  platformExpiryOf,
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
import {PlatformImportExportButton} from './PlatformImportExportButton';
import {PlatformProxyDialog} from './PlatformProxyDialog';
import {PlatformTasksPanel} from './PlatformTasksPanel';
import {PlatformWeightDialog} from './PlatformWeightDialog';
import type {
  CheckinLogPage,
  PlatformAccount,
  PlatformCheckinResult,
  PlatformCredits,
  PlatformImportResult,
  PlatformInfo,
  PlatformLoginPoll,
  PlatformLoginStatus,
  PlatformSettings,
  PlatformUsage,
} from '@/lib/types';

/** 一页显示多少个账号（与 WorkBuddy 账号页同口径）。 */
const PAGE_SIZE = 20;

/**
 * 余额显示：加载中 `…`，拿不到实时值就退到「上次登记」（`~` 前缀），都没有给 `—`。
 *
 * 按 `unit` 走：Kilo 的余额是**美元**（`$12.34`），按积分画会被读成「12 积分」，
 * 数量级完全错。`fallback` 是整列的兜底口径：条目自己没带 `unit`（旧后端 /
 * 刚加载完的空表）时用它，免得同一列里数字与列头单位不一致。
 */
function creditText(entry: PlatformCredits | undefined, loading: boolean,
                    fallback?: 'usd' | 'credits'): string {
  const unit = creditUnit(entry) === 'usd' || fallback === 'usd'
    ? 'usd' as const
    : 'credits' as const;
  if (entry && typeof entry.credits === 'number') {
    return formatCreditValue(entry.credits, unit);
  }
  if (entry && typeof entry.last_credits === 'number') {
    return `~${formatCreditValue(entry.last_credits, unit)}`;
  }
  return loading ? '…' : '—';
}

/** 积分单元格的 tooltip：失败原因；有旧值时补一句「上次登记于 …」。 */
function creditTitle(entry: PlatformCredits | undefined, hint: string,
                     lastRecorded: (when: string) => string): string {
  if (!entry?.error) return hint;
  if (typeof entry.last_credits === 'number' && entry.last_registered_at) {
    const when = fmtDateTime(entry.last_registered_at * 1000);
    return `${entry.error} · ${lastRecorded(when)}`;
  }
  return entry.error;
}

/** 数值展示：拿不到给 `—`；美元按 `$` 口径，积分小数保留两位（上游金额是浮点）。 */
function creditNum(value: number | null | undefined,
                   unit: 'usd' | 'credits' = 'credits'): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
  if (unit === 'usd') return formatCreditValue(value, 'usd');
  return Number.isInteger(value) ? String(value) : value.toFixed(2);
}

/**
 * 非 WorkBuddy 平台的账号管理（原「多平台」页里的账号部分）。
 *
 * 这个组件连页面外壳一起渲染：PageHeader（标题 / 说明 / 操作按钮）+ 账号列表。
 * 统一放在这里，是因为所有操作按钮都要用到组件内部的状态与接口，版式又与
 * WorkBuddy 账号页保持一致——按钮单独成行在卡片上方，不挤进账号卡片。
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
  const {isAdmin, canAddAccount} = useAuth();
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

  // ── 平台调度：单账号并发上限 ──────────────────────────
  //
  // 一个数字，按**账号**生效：每个账号最多同时跑几个上游请求，0 = 不限。
  // 总吞吐随账号数增长（Trae 严格按账号占位；Qoder 由内置网关选号，按
  // 「上限 × 可用账号数」折算总闸门，见后端 `platforms/limiter.py`）。
  const [pool, setPool] = useState<PlatformSettings | null>(null);
  const [poolDraft, setPoolDraft] = useState('');
  const [poolBusy, setPoolBusy] = useState(false);

  /**
   * 批量签到的节奏（并发 + 账号间隔）。
   *
   * 与上面的单账号并发是两个口径：那个限制推理请求的在途数，这个决定「全部
   * 签到」时几个账号同时跑、相邻两个账号隔多久开始。默认并发 2、间隔 2–5
   * 秒；间隔填 0 = 不间隔。
   */
  const [paceDraft, setPaceDraft] = useState({
    concurrency: '',
    min: '',
    max: '',
  });
  const [paceBusy, setPaceBusy] = useState(false);

  function resetPaceDraft(value: PlatformSettings | null = pool) {
    if (!value) return;
    setPaceDraft({
      concurrency: String(value.checkin_concurrency),
      min: String(value.checkin_delay_min),
      max: String(value.checkin_delay_max),
    });
  }

  useEffect(() => {
    let alive = true;
    platformApi.settings(platform)
      .then((value) => {
        if (!alive) return;
        setPool(value);
        setPoolDraft(String(value.max_in_flight));
        setPaceDraft({
          concurrency: String(value.checkin_concurrency),
          min: String(value.checkin_delay_min),
          max: String(value.checkin_delay_max),
        });
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

  /**
   * 提交签到节奏。
   *
   * 三个数字一起提交（后端按最小 / 最大间隔整体校验）。非法时**保留草稿**
   * 而不是回滚：改「4–8 秒」时必然先经历 `min > max` 的中间态，立刻回滚
   * 会让用户永远改不到目标值；提示一次错误，等另一个输入框填完再提交即可。
   */
  async function commitPace() {
    if (!pool || paceBusy) return;
    const lim = pool.checkin_limits;
    if (!lim) return;
    const concurrency = Number(paceDraft.concurrency.trim());
    const min = Number(paceDraft.min.trim());
    const max = Number(paceDraft.max.trim());
    const values = [concurrency, min, max];
    const valid = values.every(Number.isInteger)
      && concurrency >= lim.concurrency_min && concurrency <= lim.concurrency_max
      && min >= lim.delay_min && min <= lim.delay_max
      && max >= lim.delay_min && max <= lim.delay_max
      && min <= max;
    if (!valid) {
      notify.err(t('platforms.checkinPaceInvalid', {
        cmin: lim.concurrency_min,
        cmax: lim.concurrency_max,
        dmin: lim.delay_min,
        dmax: lim.delay_max,
      }));
      return;
    }
    if (concurrency === pool.checkin_concurrency
        && min === pool.checkin_delay_min
        && max === pool.checkin_delay_max) {
      return;
    }
    setPaceBusy(true);
    try {
      const next = await platformApi.saveSettings(platform, {
        checkin_concurrency: concurrency,
        checkin_delay_min: min,
        checkin_delay_max: max,
      });
      setPool(next);
      resetPaceDraft(next);
      notify.ok(t('platforms.checkinPaceSaved'));
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setPaceBusy(false);
    }
  }

  // ── 账号调度弹窗 / 列表筛选 ─────────────────────────────
  //
  // 权重与冷却、冻结都改成弹窗 / 图标操作（与 WorkBuddy 账号页同一套交互）：
  // 表格只负责展示，避免「一个输入框逐键提交」那种中间态请求。
  const [weightTarget, setWeightTarget] = useState<PlatformAccount | null>(null);
  const [coolingTarget, setCoolingTarget] = useState<PlatformAccount | null>(null);
  const [proxyTarget, setProxyTarget] = useState<PlatformAccount | null>(null);
  /**
   * 当前视图：`accounts`（账号列表）/ `tasks`（平台任务）。
   *
   * 用页面内的分段切换而不是新增路由：任务页的一切都依赖「当前平台 + 这
   * 份账号列表」，独立路由要么重复取数、要么得把列表塞进 URL 之外的全局状态。
   */
  const [view, setView] = useState<'accounts' | 'tasks'>('accounts');
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
  /** 用量弹窗里的到期明细：与列表同一口径（丢掉用满 / 已过期的包）。 */
  const usageExpiries = platformCreditPacks(usageCredit, now);

  /** 批量 / 单账号操作进行中标记（按 uid 或 'all'），用于禁用按钮。 */
  const [busy, setBusy] = useState('');

  const [login, setLogin] = useState<{
    id: string;
    url: string;
    /** 扫码登录的二维码（Loomy 微信扫码）；有值时面板内扫码，不自动开浏览器。 */
    qr?: string;
    status: PlatformLoginStatus;
    message: string;
    /** 登录方式：二维码 / 短信验证码（Loomy）。微信补绑时保持 qr。 */
    mode?: 'qr' | 'sms';
    /** 已经下发过验证码（输入框可用） */
    codeSent?: boolean;
    /** 已填手机号（后端脱敏后回传） */
    phone?: string;
  } | null>(null);
  const [loginStarting, setLoginStarting] = useState(false);
  /** Loomy 登录方式：扫码 / 短信验证码。 */
  const [loginMode, setLoginMode] = useState<'qr' | 'sms'>('qr');
  /** 验证码链路：手机号、验证码与发码冷却（秒）。 */
  const [smsPhone, setSmsPhone] = useState('');
  const [smsCode, setSmsCode] = useState('');
  const [codeBusy, setCodeBusy] = useState(false);
  const [codeCooldown, setCodeCooldown] = useState(0);
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

  /** 把一次登录接口的返回合并进弹窗状态（轮询 / 发码 / 提交码共用）。 */
  const applyLoginView = useCallback((id: string, out: PlatformLoginPoll) => {
    setLogin((prev) => (prev && prev.id === id
      ? {
        ...prev,
        status: out.status,
        message: out.message,
        qr: out.qr || prev.qr,
        mode: out.mode || prev.mode,
        codeSent: out.code_sent ?? prev.codeSent,
        phone: out.phone || prev.phone,
      }
      : prev));
    if (typeof out.retry_after === 'number' && out.retry_after > 0) {
      setCodeCooldown(out.retry_after);
    }
  }, []);

  /** 网页登录轮询：2 秒一次，直到状态不再是 pending / exchanging。 */
  const pollLogin = useCallback(async (name: string, id: string) => {
    const token = ++loginToken.current;
    const tick = async (): Promise<void> => {
      if (token !== loginToken.current) return;
      try {
        const out = await platformApi.loginPoll(name, id);
        if (token !== loginToken.current) return;
        applyLoginView(id, out);
        if (out.status === 'success') {
          notify.ok(t('platforms.loginSuccess'),
            out.account?.nickname || out.account?.uid || undefined);
          void reloadAll();
          return;
        }
        // `needs_bind`（微信未绑手机号）之后没有后台线程了：停轮询，改由
        // 用户填手机号 + 验证码推进。
        if (out.status === 'needs_bind') return;
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
  }, [applyLoginView, reloadAll, t]);

  /** 发码冷却倒计时（后端 60 秒内只允许发一次，这里只做界面提示）。 */
  useEffect(() => {
    if (codeCooldown <= 0) return;
    const timer = setTimeout(
      () => setCodeCooldown((n) => (n > 0 ? n - 1 : 0)), 1000);
    return () => clearTimeout(timer);
  }, [codeCooldown]);

  async function startWebLogin(mode: 'qr' | 'sms' = 'qr') {
    if (loginStarting) return;
    setLoginStarting(true);
    setLoginMode(mode);
    setCallbackUrl('');
    setSmsCode('');
    setCodeCooldown(0);
    if (login) void platformApi.loginCancel(platform, login.id).catch(() => {});
    try {
      const out = await platformApi.loginStart(
        platform, realmAware ? realmScope : 'cn', mode);
      setLogin({
        id: out.login_id,
        url: out.url,
        qr: out.qr,
        status: 'pending',
        message: out.message || '',
        mode,
        codeSent: false,
      });
      if (mode === 'sms') return;      // 等用户填手机号，没有可轮询的后台任务
      // 扫码平台（Loomy）在面板内扫码；其它平台开浏览器授权页。二维码是后台
      // 线程去取的，`start` 返回时还给不出来，所以这里不能按 `qr` 判断。
      // DuMate 走百度 Passport 扫码，`url` 为空，同样不需要开浏览器。
      if (out.url && platform !== 'loomy') {
        window.open(out.url, '_blank', 'noopener,noreferrer');
      }
      void pollLogin(platform, out.login_id);
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setLoginStarting(false);
    }
  }

  /** 切换登录方式：同一方式且会话还活着时不动（避免把正在扫的码换掉）。 */
  function switchLoginMode(mode: 'qr' | 'sms') {
    if (mode === loginMode && login
        && login.status !== 'error' && login.status !== 'expired') {
      return;
    }
    void startWebLogin(mode);
  }

  /** 下发短信验证码（验证码登录，或微信未绑手机号时的补绑）。 */
  async function sendLoginCode() {
    if (!login || codeBusy) return;
    const phone = smsPhone.trim();
    if (!phone) {
      notify.warn(t('platforms.loginPhoneRequired'));
      return;
    }
    setCodeBusy(true);
    try {
      const out = await platformApi.loginSendCode(platform, login.id, phone);
      applyLoginView(login.id, out);
      notify.ok(t('platforms.loginCodeSent'));
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setCodeBusy(false);
    }
  }

  /** 提交验证码；成功即完成登录 / 微信绑定。 */
  async function submitLoginCode() {
    if (!login || codeBusy) return;
    const code = smsCode.trim();
    if (!code) {
      notify.warn(t('platforms.loginCodeRequired'));
      return;
    }
    setCodeBusy(true);
    try {
      const out = await platformApi.loginSubmitCode(platform, login.id, code);
      applyLoginView(login.id, out);
      setSmsCode('');
      if (out.status === 'success') {
        notify.ok(t('platforms.loginSuccess'),
          out.account?.nickname || out.account?.uid || undefined);
        void reloadAll();
      } else if (out.status !== 'error' && out.status !== 'expired') {
        void pollLogin(platform, login.id);
      }
    } catch (e) {
      // 验证码错了：后端已把会话状态改回可重试，这里只提示。
      const message = errText(e);
      setLogin((prev) => (prev && prev.id === login.id
        ? {...prev, message} : prev));
      notify.err(message);
    } finally {
      setCodeBusy(false);
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
      applyLoginView(login.id, out);
      setCallbackUrl('');
      if (out.status === 'success') {
        notify.ok(t('platforms.loginSuccess'),
          out.account?.nickname || out.account?.uid || undefined);
        void reloadAll();
      } else if (out.status === 'needs_bind') {
        // 微信没绑手机号：弹窗转入验证码阶段，不再轮询。
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
    setLoginMode('qr');
    setSmsPhone('');
    setSmsCode('');
    setCodeCooldown(0);
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
      // DuMate 的凭证本身就是一长行 Cookie，要求用户先包成 JSON 太反人类：
      // 粘不进去的整段 Cookie 直接按 `{cookie: "..."}` 提交（后端认这个形状）。
      if (platform === 'dumate' && raw.includes('=')) {
        body = {cookie: raw};
      } else {
        notify.err(t('platforms.importInvalid'));
        return;
      }
    }
    // 同理：粘贴的是 JSON 字符串（带引号的整行 Cookie）时也包一层。
    if (platform === 'dumate' && typeof body === 'string') {
      body = {cookie: body};
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

  /** 仅签到：退出推理选号（不消耗积分），签到与平台任务照常。 */
  async function toggleCheckinOnly(account: PlatformAccount) {
    if (busy) return;
    const next = !account.checkin_only;
    setBusy(account.uid);
    try {
      await platformApi.updateAccount(platform, account.uid, {checkin_only: next});
      notify.ok(next ? t('platforms.checkinOnlyOn') : t('platforms.checkinOnlyOff'));
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
  // 仅签到：旧后端没有这个字段，按「不支持」处理（不显示点了必然 400 的按钮）。
  const supportsCheckinOnly = info?.supports_checkin_only === true;
  // 出口线路：国际站账号常需要境外出口。旧后端没有这个字段 → 不显示入口。
  const supportsProxy = info?.supports_proxy === true;
  const supportsLoginCallback = info?.supports_login_callback !== false;
  // 短信验证码登录（Loomy）：验证码直接登录 + 微信未绑手机号时的补绑。
  const supportsLoginCode = !!info?.supports_login_code;
  // 平台任务（Loomy：签到 / 绑定邀请码 / 兑换兑换码）。没有这套概念的平台
  // 不显示分段切换，整块任务页也不渲染。
  const supportsTasks = !!info?.supports_tasks;
  // 能否导出账号（含明文凭据）。虚拟平台没有账号文件，不给导出入口。
  const supportsExport = !!info?.supports_export;
  // 回调固定指向 `127.0.0.1:<端口>`（Trae 的授权页强校验），只有这类平台
  // 才需要提示「面板与浏览器必须同机」。Loomy 的回调落在 loomy.xunfei.cn，
  // 走的也是「粘链接」，不受这条约束。
  const loginNeedsLoopback = supportsLoginCallback && platform !== 'loomy';
  const busyAny = busy !== '';
  // 积分来源按平台区分：Trae 走权益接口，Qoder 走内置网关的余额快照。
  // 以前这里只有一句写死「Trae 权益接口」的文案，Qoder 页会显示错误的说明。
  const creditsHint = platform === 'qoder'
    ? t('platforms.creditsHintQoder')
    : platform === 'trae'
      ? t('platforms.creditsHintTrae')
      : platform === 'loomy'
        ? t('platforms.creditsHintLoomy')
        : platform === 'kilo'
          ? t('platforms.creditsHintKilo')
          : t('platforms.creditsHint');

  /**
   * 余额单位：Kilo 的余额是美元（`unit='usd'`），Trae 国际版也是。
   *
   * 列头与数字必须一起换：只把数字画成 `$12.34` 而列头还写「积分」，用户仍会
   * 以为那是积分。同平台账号单位一致，取到一个 `usd` 就整列按美元；没有
   * `unit` 的旧数据按积分（改造前的口径）。
   *
   * 余额还没取回时（首次加载 / 查询失败清空）用平台的固定口径兜底：Kilo 只有
   * 美元，否则列头会先写「积分」再跳成「余额」。Trae 的单位随版本变，只能等
   * 数据到了再定。
   */
  const creditUnitOf = Object.values(credits).some((c) => c?.unit === 'usd')
      || (Object.keys(credits).length === 0 && platform === 'kilo')
    ? 'usd' as const
    : 'credits' as const;
  const creditColumnLabel = creditUnitOf === 'usd'
    ? t('metric.balance')
    : t('platforms.colCredits');

  const loginStatusText = (() => {
    if (!login) return '';
    switch (login.status) {
      case 'pending':
        // 后端会给出更具体的状态（「等待微信扫码」/「已扫码，请在手机上确认」）。
        return login.message || t('platforms.loginWaiting');
      case 'exchanging':
        return t('platforms.loginExchanging');
      case 'needs_bind':
        // 微信授权过了但没绑手机号：后端会给一句具体说明。
        return login.message || t('platforms.loginBindRequired');
      case 'success':
        return t('platforms.loginSuccess');
      case 'expired':
        return t('platforms.loginExpired');
      default:
        return login.message || t('platforms.loginFailed');
    }
  })();
  /** 扫码二维码只在等扫码 / 换令牌时展示；成功或过期后收起，避免误导。 */
  const loginQrVisible = !!login?.qr
    && (login.status === 'pending' || login.status === 'exchanging');
  /**
   * 验证码输入区：短信登录的 `pending`，或微信 `bind=0` 转进来的 `needs_bind`。
   * 注意 `needs_bind` 仍属于扫码链路（mode 是 qr），只是不再轮询二维码。
   */
  const codeFlowVisible = supportsLoginCode && !!login
    && (login.status === 'needs_bind'
      || (loginMode === 'sms' && login.status === 'pending'));
  const codeFormBusy = codeBusy || login?.status === 'exchanging';
  /**
   * 空状态里的操作按钮：只放这个平台真正能用的入口。
   *
   * 以前固定渲染「从本机导入」：没有桌面客户端的平台（DuMate）点了必然失败；
   * 单区域平台（MiniMax）在「账号都属于另一个版本」的空态下也一样点不动。
   * 三个入口各自按能力出现，手工导入始终可用。
   */
  const emptyActions = canAddAccount ? (
    <div className="mt-1 flex flex-wrap items-center justify-center gap-2">
      {supportsWebLogin && (
        <Button
          size="sm"
          className="rounded-full"
          disabled={busyAny || loginStarting || login !== null}
          onClick={() => void startWebLogin()}
        >
          {loginStarting ? <Loader2 className="animate-spin" /> : <LogIn />}
          {t('platforms.login')}
        </Button>
      )}
      {supportsDesktop && (
        <Button
          variant={supportsWebLogin ? 'outline' : 'default'}
          size="sm"
          className="rounded-full"
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
        className="rounded-full"
        disabled={busyAny}
        onClick={() => {
          setImportText('');
          setImportOpen(true);
        }}
      >
        <FileJson />
        {t('platforms.importManual')}
      </Button>
    </div>
  ) : null;

  /**
   * 顶部操作行。
   *
   * 与 WorkBuddy 账号页同一版式：所有操作都放进 PageHeader 的 actions 行，
   * 不再塞进账号卡片头部。卡片里只留表格与分页，视线不会被一排按钮切断；
   * 窄屏下这行会自然折行，不会把标题挤扁。
   */
  const headerActions = (
    <>
      {/* 单账号并发：每个账号最多几个在途请求（0 = 不限）。
          非管理员只读展示——它影响的是整体吞吐，不是单个账号的开关。 */}
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
      {/* 批量签到节奏：几个账号同时跑 + 相邻两个账号隔多久开始。
          默认并发 2、间隔 2–5 秒；间隔 0 = 不间隔。非管理员只读展示。 */}
      {pool?.checkin_limits && (
        <div
          className="flex items-center gap-1.5 rounded-full border border-border/60 px-2.5 py-1 text-[11px] text-muted-foreground"
          title={t('platforms.checkinPaceHint')}
        >
          <CalendarClock className="h-3.5 w-3.5" />
          <span className="whitespace-nowrap">{t('platforms.checkinPace')}</span>
          {isAdmin ? (
            <>
              <Input
                value={paceDraft.concurrency}
                onChange={(e) => setPaceDraft((d) => ({...d, concurrency: e.target.value}))}
                onBlur={() => void commitPace()}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    e.preventDefault();
                    void commitPace();
                  }
                }}
                inputMode="numeric"
                disabled={paceBusy}
                aria-label={t('platforms.checkinConcurrency')}
                className="h-6 w-8 border-0 bg-transparent px-1 text-center text-[11px] tabular-nums shadow-none focus-visible:ring-0"
              />
              <span className="whitespace-nowrap">{t('platforms.checkinConcurrency')}</span>
              <span className="opacity-40">·</span>
              <Input
                value={paceDraft.min}
                onChange={(e) => setPaceDraft((d) => ({...d, min: e.target.value}))}
                onBlur={() => void commitPace()}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    e.preventDefault();
                    void commitPace();
                  }
                }}
                inputMode="numeric"
                disabled={paceBusy}
                aria-label={t('platforms.checkinDelayMin')}
                className="h-6 w-8 border-0 bg-transparent px-1 text-center text-[11px] tabular-nums shadow-none focus-visible:ring-0"
              />
              <span className="opacity-60">–</span>
              <Input
                value={paceDraft.max}
                onChange={(e) => setPaceDraft((d) => ({...d, max: e.target.value}))}
                onBlur={() => void commitPace()}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    e.preventDefault();
                    void commitPace();
                  }
                }}
                inputMode="numeric"
                disabled={paceBusy}
                aria-label={t('platforms.checkinDelayMax')}
                className="h-6 w-8 border-0 bg-transparent px-1 text-center text-[11px] tabular-nums shadow-none focus-visible:ring-0"
              />
              <span className="whitespace-nowrap">{t('platforms.checkinSeconds')}</span>
            </>
          ) : (
            <span className="tabular-nums text-foreground">
              {pool.checkin_concurrency} · {pool.checkin_delay_min}–{pool.checkin_delay_max}
              {t('platforms.checkinSeconds')}
            </span>
          )}
        </div>
      )}
      <Button
        variant="outline"
        size="sm"
        className="rounded-full"
        disabled={accountsState.isRefreshing}
        onClick={() => void reloadAll()}
      >
        {accountsState.isRefreshing ? <Loader2 className="animate-spin" /> : <RefreshCw />}
        {t('common.refresh')}
      </Button>
      {canAddAccount && (
        <>
          {supportsWebLogin && (
            <Button
              size="sm"
              className="rounded-full"
              disabled={busyAny || loginStarting || login !== null}
              onClick={() => void startWebLogin()}
              title={platform === 'dumate'
                ? t('platforms.loginHintDumate')
                : supportsLoginCallback
                  ? t('platforms.loginHint')
                  : t('platforms.loginHintDevice')}
            >
              {loginStarting ? <Loader2 className="animate-spin" /> : <LogIn />}
              {t('platforms.login')}
            </Button>
          )}
          {supportsDesktop && (
            <Button
              variant="outline"
              size="sm"
              className="rounded-full"
              disabled={busyAny}
              onClick={() => void importDesktop()}
              title={t('platforms.importDesktopHint')}
            >
              {busy === 'import' ? <Loader2 className="animate-spin" /> : <MonitorDown />}
              {t('platforms.importDesktop')}
            </Button>
          )}
          <PlatformImportExportButton
            platform={platform}
            label={label}
            supportsExport={supportsExport}
            onSuccess={reloadAll}
            onPasteImport={() => {
              setImportText('');
              setImportOpen(true);
            }}
          />
          {/* 批量签到会改动每个账号的签到状态，仍只对管理员开放。 */}
          {isAdmin && supportsCheckin && (
            <Button
              variant="outline"
              size="sm"
              className="rounded-full"
              disabled={busyAny || !accounts.length}
              onClick={() => void checkinAll()}
            >
              {busy === 'all' ? <Loader2 className="animate-spin" /> : <CircleCheck />}
              {t('platforms.checkinAll')}
            </Button>
          )}
        </>
      )}
    </>
  );

  // 视图切换：账号列表 / 平台任务。只有声明了 `supports_tasks` 的平台才出现
  // ——其余平台没有任务概念，多一个永远空的页签只是噪音。
  const viewSwitch = supportsTasks ? (
    <div className="flex w-fit gap-1 rounded-full bg-muted p-1 text-[11px]">
      {([
        ['accounts', t('platforms.viewAccounts')],
        ['tasks', t('platforms.viewTasks')],
      ] as const).map(([id, text]) => (
        <button
          key={id}
          type="button"
          onClick={() => setView(id)}
          aria-pressed={view === id}
          className={cn(
            'rounded-full px-3 py-1.5 transition-colors',
            view === id
              ? 'bg-background font-medium text-foreground shadow-sm'
              : 'text-muted-foreground hover:text-foreground',
          )}
        >
          {text}
        </button>
      ))}
    </div>
  ) : null;

  // 任务视图整页替换账号表格：两者依赖的数据不同，混在一起只会在每一行
  // 多出三个按钮和两个输入框。调度参数弹窗只服务账号行，这里不必渲染。
  if (supportsTasks && view === 'tasks') {
    return (
      <div className="flex flex-col gap-4 md:gap-6">
        <PageHeader
          stacked
          title={`${t('platforms.accountsTitle')} · ${label}`}
          description={t('platforms.accountsDesc', {label})}
          actions={headerActions}
        />
        {viewSwitch}
        <PlatformTasksPanel
          platform={platform}
          label={label}
          accounts={scopedAccounts}
        />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4 md:gap-6" aria-busy={accountsState.isRefreshing}>
      <PageHeader
        stacked
        title={`${t('platforms.accountsTitle')} · ${label}`}
        description={t('platforms.accountsDesc', {label})}
        actions={headerActions}
      />
      {viewSwitch}
      {/* 检索 / 状态筛选 / 排序：与 WorkBuddy 账号页同一套语义。
          账号还没取到时整块不渲染——那时计数全是 0，摆出来等于说
          「一个账号都没有」。独立成一块（而不是塞进账号卡片），与 WorkBuddy
          的检索区版式一致。 */}
      {scopedAccounts.length > 0 && (
        <section className="rounded-[20px] bg-muted p-3.5">
          <div className="flex flex-col gap-3 lg:flex-row lg:items-center">
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
        </section>
      )}

      <section className="overflow-hidden rounded-[20px] bg-muted">
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
            description={platform === 'dumate'
              ? t('platforms.noAccountsDescDumate')
              : t('platforms.noAccountsDesc')}
            className="flex flex-col items-center justify-center p-8 text-center"
          >
            {/* 空态里给的是**这个平台真正能用**的入口：没有本机客户端可导入的
                平台（DuMate）不能只放一个必然失败的「从本机导入」。 */}
            {emptyActions}
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
            {emptyActions}
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
                  {realmAware && (
                    <TableHead className="text-[11px] text-muted-foreground">
                      {t('platforms.colRealm')}
                    </TableHead>
                  )}
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
                      {creditColumnLabel}
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
                  // 会自动续期的平台（MiniMax Code）看 refresh token 的寿命：
                  // 它的 access token 只有约 1 小时且由后端按需换新，拿它判
                  // 「已过期」会把正常账号整片标红。
                  const expiryMs = platformExpiryOf(a);
                  const expired = Number.isFinite(expiryMs) && expiryMs <= now;
                  const group = platformAvailabilityOf(a, now);
                  // 积分到期明细：后端已经筛过一遍，这里按**当前时刻**再筛一次
                  // —— 页面停留期间包会到期 / 被用满，列表不会每分钟重新请求。
                  const creditExpiries = platformCreditExpiries(credits[a.uid], now);
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
                              a.checkin_only
                                ? t('platforms.checkinOnlyHint')
                                : group === 'stopped'
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
                                  ? (a.checkin_only
                                    ? t('platforms.checkinOnlyBadge')
                                    : t('accounts.badgeDisabledByPanel'))
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
                      {/* 单区域平台（Loomy / DuMate）没有版本概念，不显示「国内版」
                          这一列，否则会让人以为它们也有国际版。 */}
                      {realmAware && (
                        <TableCell>
                          <Badge variant="secondary" className="rounded-md text-[10px]">
                            {a.realm_label || a.realm}
                          </Badge>
                        </TableCell>
                      )}
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
                              title={creditTitle(credits[a.uid], creditsHint,
                                (when) => t('platforms.creditsLastRecorded', {when}))}
                            >
                              {creditText(credits[a.uid], creditsLoading, creditUnitOf)}
                              {typeof credits[a.uid]?.checkin_credits === 'number'
                                && (credits[a.uid]?.checkin_credits as number) > 0 && (
                                  <span className="ml-1 text-[10px] text-muted-foreground">
                                    {t('platforms.creditsCheckinShort', {
                                      n: creditNum(credits[a.uid]?.checkin_credits, creditUnitOf),
                                    })}
                                  </span>
                                )}
                            </span>
                            {/* 到期倒计时与 WorkBuddy 账号页**同一个组件**：
                                「多少分 · 还有几天到期」，悬停给出逐包明细与合计。
                                只画最近一笔（单元格放不下多行），但金额必须带上 ——
                                只写日期会被读成「余额整体那天到期」。 */}
                            <CreditCountdown expiries={creditExpiries} unit={creditUnitOf} />
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
                            {fmtDateTime(Number.isFinite(expiryMs) ? expiryMs : null)}
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
                              {supportsProxy && (
                                <Button
                                  variant="ghost"
                                  size="icon"
                                  className={cn(
                                    'h-7 w-7 rounded-md',
                                    a.proxy
                                      ? 'text-primary hover:text-primary'
                                      : 'text-muted-foreground',
                                  )}
                                  title={a.proxy === '__direct__'
                                    ? `${t('platforms.proxyLine')}: ${t('platforms.proxyDirect')}`
                                    : `${t('platforms.proxyLine')}: ${a.proxy || t('platforms.proxyFollowPool')}`}
                                  disabled={rowBusy || busyAny}
                                  onClick={() => setProxyTarget(a)}
                                >
                                  <Globe className="h-3.5 w-3.5" />
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
                              {supportsCheckinOnly && (
                                <Button
                                  variant="ghost"
                                  size="icon"
                                  className={cn(
                                    'h-7 w-7 rounded-md',
                                    a.checkin_only
                                      ? 'text-amber-600 hover:text-amber-700 dark:text-amber-400'
                                      : 'text-muted-foreground hover:text-amber-600',
                                  )}
                                  title={a.checkin_only
                                    ? t('platforms.checkinOnlyDisable')
                                    : t('platforms.checkinOnlyEnable')}
                                  disabled={rowBusy || busyAny}
                                  onClick={() => void toggleCheckinOnly(a)}
                                >
                                  <Gift className="h-3.5 w-3.5" />
                                </Button>
                              )}
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

      {/* 调度参数弹窗：权重 / 强制冷却 / 出口线路。与 WorkBuddy 账号页同一套交互。 */}
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
      <PlatformProxyDialog
        platform={platform}
        account={proxyTarget}
        open={proxyTarget !== null}
        onOpenChange={(open) => { if (!open) setProxyTarget(null); }}
        onSaved={() => void reloadAll()}
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
                          {creditUnitOf === 'usd'
                            ? t('metric.balance')
                            : t('platforms.creditsBalance')}
                        </span>
                        <span
                          className="text-sm font-medium tabular-nums"
                          title={creditTitle(usageCredit, creditsHint,
                            (when) => t('platforms.creditsLastRecorded', {when}))}
                        >
                          {creditText(usageCredit, creditsLoading, creditUnitOf)}
                        </span>
                      </div>
                      <div className="mt-1.5 grid grid-cols-2 gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
                        <span>{t('platforms.creditsLimit')}</span>
                        <span className="text-right tabular-nums text-foreground">
                          {creditNum(usageCredit?.limit, creditUnitOf)}
                        </span>
                        <span>{t('platforms.creditsUsed')}</span>
                        <span className="text-right tabular-nums text-foreground">
                          {creditNum(usageCredit?.used, creditUnitOf)}
                        </span>
                        <span>{t('platforms.creditsCheckin')}</span>
                        <span className="text-right tabular-nums text-foreground">
                          {creditNum(usageCredit?.checkin_credits, creditUnitOf)}
                          {usageCredit?.checked_in
                            ? ` · ${t('platforms.checkinAlready')}`
                            : ''}
                        </span>
                      </div>
                      {usageExpiries.length > 0 && (
                        <div className="mt-2 border-t border-border/60 pt-2">
                          <div className="mb-1 flex items-center gap-1 text-[11px] text-muted-foreground">
                            <CalendarClock className="h-3.5 w-3.5" />
                            {t('platforms.creditsExpiry')}
                          </div>
                          <ul className="space-y-0.5">
                            {usageExpiries.map((pack, i) => (
                              <li
                                key={i}
                                className="flex items-center justify-between gap-2 text-[11px]"
                              >
                                <span className="truncate text-muted-foreground">
                                  {pack.label || t('platforms.creditsPack')}
                                </span>
                                <span className="shrink-0 tabular-nums text-foreground">
                                  {creditNum(pack.amount, creditUnitOf)} · {fmtDateTime(pack.expire_at * 1000)}
                                </span>
                              </li>
                            ))}
                          </ul>
                        </div>
                      )}
                    </div>
                  )}
                  {usage.consumed && (
                    <div className="rounded-[12px] border border-border/60 bg-muted/30 px-3 py-2">
                      <div className="flex items-center gap-1 text-xs text-muted-foreground">
                        <Activity className="h-3.5 w-3.5" />
                        {t('platforms.usageConsumedTitle')}
                      </div>
                      <div className="mt-1.5 grid grid-cols-2 gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
                        <span>{t('platforms.usageConsumedToday')}</span>
                        <span className="text-right tabular-nums text-foreground">
                          {creditNum(usage.consumed.today)}
                          {' · '}
                          {t('platforms.usageConsumedRecords', {
                            n: usage.consumed.today_records,
                          })}
                        </span>
                        <span>{t('platforms.usageConsumedWeek')}</span>
                        <span className="text-right tabular-nums text-foreground">
                          {creditNum(usage.consumed.week)}
                          {' · '}
                          {t('platforms.usageConsumedRecords', {
                            n: usage.consumed.week_records,
                          })}
                        </span>
                      </div>
                      {!!usage.consumed.recent?.length && (
                        <div className="mt-2 border-t border-border/60 pt-2">
                          <div className="mb-1 text-[11px] text-muted-foreground">
                            {t('platforms.usageConsumedRecent')}
                          </div>
                          <ul className="space-y-0.5">
                            {usage.consumed.recent.slice(0, 8).map((row, i) => (
                              <li
                                key={i}
                                className="flex items-center justify-between gap-2 text-[11px]"
                              >
                                <span className="truncate text-muted-foreground">
                                  {row.conversation || t('platforms.usageConsumedTitle')}
                                </span>
                                <span className="shrink-0 tabular-nums text-foreground">
                                  {creditNum(row.points)}
                                  {row.at ? ` · ${fmtDateTime(row.at * 1000)}` : ''}
                                </span>
                              </li>
                            ))}
                          </ul>
                        </div>
                      )}
                      <p className="mt-2 text-[10px] leading-4 text-muted-foreground">
                        {t('platforms.usageConsumedNote')}
                      </p>
                    </div>
                  )}
                  {usage.session_usage === null && usage.entitlement === null
                    && typeof usageCredit?.credits !== 'number' && !usage.consumed && (
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
              {loginMode === 'sms' && supportsLoginCode
                ? t('platforms.loginDescSms')
                : login?.status === 'needs_bind'
                  ? t('platforms.loginBindDesc')
                  : loginQrVisible
                    ? platform === 'dumate'
                      ? t('platforms.loginDumateQrDesc')
                      : t('platforms.loginQrDesc')
                    : platform === 'loomy'
                      ? t('platforms.loginDescLoomy')
                      : platform === 'dumate'
                        ? t('platforms.loginDescDumate')
                        : loginNeedsLoopback
                          ? t('platforms.loginDesc')
                          : t('platforms.loginDeviceDesc')}
            </DialogDescription>
          </DialogHeader>
          <DialogBody>
            <div className="space-y-3 px-6 pb-2">
              {supportsLoginCode && login?.status !== 'success' && (
                <div className="flex gap-1 rounded-full bg-muted p-1 text-[11px]">
                  {(['qr', 'sms'] as const).map((mode) => (
                    <button
                      key={mode}
                      type="button"
                      className={`flex-1 rounded-full px-3 py-1.5 transition-colors ${
                        loginMode === mode
                          ? 'bg-background font-medium text-foreground shadow-sm'
                          : 'text-muted-foreground hover:text-foreground'
                      }`}
                      disabled={loginStarting}
                      onClick={() => switchLoginMode(mode)}
                    >
                      {mode === 'qr'
                        ? t('platforms.loginModeQr')
                        : t('platforms.loginModeSms')}
                    </button>
                  ))}
                </div>
              )}
              {loginNeedsLoopback && (
                <div className="flex items-start gap-2 rounded-[12px] bg-muted/60 p-3 text-[11px] leading-5 text-muted-foreground">
                  <CircleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-500" />
                  <span>{t('platforms.loginSameMachine')}</span>
                </div>
              )}
              {loginQrVisible && (
                <div className="flex justify-center">
                  <img
                    src={login.qr}
                    alt={platform === 'dumate'
                      ? t('platforms.loginQrAltDumate')
                      : t('platforms.loginQrAlt')}
                    className="h-44 w-44 rounded-xl border border-border/60 bg-white p-1"
                  />
                </div>
              )}
              <div className="flex items-start gap-2 text-xs">
                {login?.status === 'success'
                  ? <CircleCheck className="mt-0.5 h-4 w-4 shrink-0 text-emerald-500" />
                  : login?.status === 'error' || login?.status === 'expired'
                    ? <CircleAlert className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
                    : login?.status === 'needs_bind'
                      ? <CircleAlert className="mt-0.5 h-4 w-4 shrink-0 text-amber-500" />
                      : <Loader2 className="mt-0.5 h-4 w-4 shrink-0 animate-spin text-muted-foreground" />}
                <span className="break-all leading-5">{loginStatusText}</span>
              </div>
              {codeFlowVisible && login && (
                <div className="space-y-2">
                  <div className="space-y-1.5">
                    <div className="text-[11px] font-medium text-foreground">
                      {t('platforms.loginPhoneLabel')}
                    </div>
                    <div className="flex items-center gap-1.5">
                      <Input
                        value={smsPhone}
                        onChange={(e) => setSmsPhone(e.target.value)}
                        placeholder={t('platforms.loginPhonePlaceholder')}
                        className="h-8 rounded-xl text-xs"
                        inputMode="tel"
                        autoComplete="tel"
                        disabled={codeFormBusy}
                      />
                      <Button
                        type="button"
                        size="sm"
                        className="h-8 shrink-0 rounded-full px-3 text-[11px]"
                        disabled={codeFormBusy || codeCooldown > 0 || !smsPhone.trim()}
                        onClick={() => void sendLoginCode()}
                      >
                        {codeBusy ? <Loader2 className="animate-spin" /> : null}
                        {codeCooldown > 0
                          ? t('platforms.loginResendIn', {s: codeCooldown})
                          : t('platforms.loginSendCode')}
                      </Button>
                    </div>
                  </div>
                  <div className="space-y-1.5">
                    <div className="text-[11px] font-medium text-foreground">
                      {t('platforms.loginCodeLabel')}
                    </div>
                    <form
                      className="flex items-center gap-1.5"
                      onSubmit={(e) => { e.preventDefault(); void submitLoginCode(); }}
                    >
                      <Input
                        value={smsCode}
                        onChange={(e) => setSmsCode(e.target.value)}
                        placeholder={t('platforms.loginCodePlaceholder')}
                        className="h-8 rounded-xl text-xs"
                        inputMode="numeric"
                        autoComplete="one-time-code"
                        maxLength={8}
                        disabled={codeFormBusy || !login.codeSent}
                      />
                      <Button
                        type="submit"
                        size="sm"
                        className="h-8 shrink-0 rounded-full px-3 text-[11px]"
                        disabled={codeFormBusy || !login.codeSent || !smsCode.trim()}
                      >
                        {codeBusy ? <Loader2 className="animate-spin" /> : null}
                        {t('platforms.loginCodeSubmit')}
                      </Button>
                    </form>
                  </div>
                  <p className="text-[11px] leading-5 text-muted-foreground">
                    {login.status === 'needs_bind'
                      ? t('platforms.loginBindHint')
                      : t('platforms.loginCodeHint')}
                  </p>
                </div>
              )}
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
              {login?.status === 'pending' && loginMode === 'qr'
                && supportsLoginCallback && (
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
                      placeholder={platform === 'loomy'
                        ? t('platforms.loginCallbackPlaceholderLoomy')
                        : t('platforms.loginCallbackPlaceholder')}
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
                    {platform === 'loomy'
                      ? t('platforms.loginCallbackHintLoomy')
                      : t('platforms.loginCallbackHint')}
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
                onClick={() => void startWebLogin(loginMode)}
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
            <DialogDescription>
              {platform === 'loomy'
                ? t('platforms.importDescLoomy')
                : platform === 'dumate'
                  ? t('platforms.importDescDumate')
                  : t('platforms.importDesc')}
            </DialogDescription>
          </DialogHeader>
          <DialogBody className="max-h-[min(640px,calc(100dvh-12rem))]">
            <div className="px-6 pb-2">
              <Textarea
                value={importText}
                onChange={(e) => setImportText(e.target.value)}
                placeholder={platform === 'loomy'
                  ? t('platforms.importPlaceholderLoomy')
                  : platform === 'dumate'
                    ? t('platforms.importPlaceholderDumate')
                    : t('platforms.importPlaceholder')}
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
