'use client';

import {useMemo, useRef, useState} from 'react';
import {Check, ChevronsUpDown, Search, X} from 'lucide-react';
import {accountApi, modelApi, platformApi, upstreamsApi} from '@/lib/api';
import type {CatalogModel} from '@/lib/types';
import type {Realm} from '@/lib/realm-context';
import {useT} from '@/lib/i18n/provider';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {Popover, PopoverContent, PopoverTrigger} from '@/components/ui/popover';
import {cn} from '@/lib/utils';

/**
 * 模型白名单输入（issue #141）：把「手打模型名」换成「从清单里勾」。
 *
 * 为什么保留输入框：清单里暂时没有的模型（新模型、私有部署、还没同步到的目录）仍然
 * 要能填 —— 候选只解决「记不住名字、写错了要等调用失败才发现」，不该变成新的门槛。
 *
 * 存储形态没变（仍是逗号/换行分隔的字符串），所以后端与既有密钥数据一行都不用动：
 * 勾选 = 往那个字符串里追加一个名字，取消 = 移除。
 *
 * 清单分两层展示：
 *
 *   · **WorkBuddy 按版本分组**（国内版 / 国际版）：白名单是在所选版本之内再收窄的，
 *     放在一起看不出哪个名字属于哪一版；
 *   · **平台模型按平台分组**（Trae / Qoder / 组合）：它们的名字自带平台前缀
 *     （`qodercn/Qwen3.8-Max`），与 `cn:` / `global:` 是两个维度，混进版本组里
 *     只会让人以为「国际版里有 qoder 模型」。
 *
 * 平台分组是**后取的**：先出 WorkBuddy 两组，平台目录回来一组补一组，某个平台
 * 取不到只在它那一组下面说明，不挡住手输、也不影响其他分组。
 *
 * 校验仍由父组件在输入框失焦时触发（与本组件引入前一致）；勾选进来的名字来自清单，
 * 本身不可能写错，因此不必再查一次。
 */
export function ModelWhitelistInput({
  value,
  onChange,
  onEdit,
  onBlur,
  realm,
  placeholder,
  disabled = false,
}: {
  value: string;
  onChange: (next: string) => void;
  /**
   * 正在编辑（新值还没提交到 `value`）。
   *
   * 父组件用它**作废上一次的校验结论**：输入框里的草稿只有失焦/回车才提交，若不作废，
   * 用户改了内容而提示还是旧的 —— 显示的「都对」可能对应的是改之前那一版，
   * 比不显示更误导（issue #46 那条不变量的原话）。
   */
  onEdit?: () => void;
  /** 失焦：参数是**提交后**的白名单字符串（父组件据此校验已知/未知模型） */
  onBlur?: (next: string) => void;
  /** 当前密钥的限定版本：清单里与它一致的那一组排在前面，便于先挑本版模型 */
  realm?: Realm | '';
  placeholder?: string;
  disabled?: boolean;
}) {
  const t = useT();
  const names = useMemo(() => splitNames(value), [value]);
  const [draft, setDraft] = useState('');
  const [open, setOpen] = useState(false);
  /** 弹窗内使用时，浮层要挂进弹窗子树（issue #438） */
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const [portalContainer, setPortalContainer] = useState<HTMLElement | null>(null);
  const [query, setQuery] = useState('');
  const [catalog, setCatalog] = useState<{cn: CatalogModel[]; global: CatalogModel[]; realms: Set<Realm> | null} | null>(null);
  /** 平台模型分组：按平台名逐个补齐（某个平台取不到时带 `error`） */
  const [platformGroups, setPlatformGroups] = useState<PlatformGroup[] | null>(null);
  /** 平台清单还在补（逐组回来，不能靠 `platformGroups === null` 判断） */
  const [platformsBusy, setPlatformsBusy] = useState(false);
  /** 只看上游标注为「限时免费」的模型（issue #208）；tags 缺失时按不在免费目录处理 */
  const [freeOnly, setFreeOnly] = useState(false);
  const [failed, setFailed] = useState(false);

  /** 勾选/取消：按当前顺序重建字符串（去重，保持用户看到的次序） */
  function emit(next: string[]) {
    onChange(Array.from(new Set(next)).join(', '));
  }

  function add(name: string) {
    const clean = name.trim().replace(/,+$/, '');
    if (!clean) return;
    emit([...names, clean]);
    setDraft('');
  }

  function remove(name: string) {
    emit(names.filter((n) => n !== name));
  }

  /** 打开时（只拉一次）取两个版本的模型清单；取不到就如实说，不挡住手输 */
  async function ensureCatalog() {
    if (catalog || failed) return;
    try {
      // 用模型目录而不是 /api/models：只有目录条目带 tags（含「限时免费」），
      // 白名单的免费筛选依赖它。目录有后端缓存，打开弹窗时拉一次即可。
      const [cn, global, groups] = await Promise.all([
        modelApi.catalog('cn'),
        modelApi.catalog('global'),
        // 没有配置国际版账号时，选择器里不该冒出一堆用不了的国际版模型
        // （issue #364）。本 fork 支持多分组，所以要合并**所有分组**的账号，
        // 只看默认分组会把别的组里的国际版账号漏掉。任何一步取不到就退回
        // 「两组都显示」，不挡住手输。
        upstreamsApi.list().catch(() => null),
      ]);
      let realms: Set<Realm> | null = null;
      if (groups) {
        const lists = await Promise.all(
          groups.items.map((g) => accountApi.list(g.id).catch(() => null)),
        );
        if (lists.every((list) => list !== null)) {
          const all = lists.flatMap((list) => list!.accounts);
          if (all.length > 0) {
            realms = new Set<Realm>(all.map((a) => (a.realm === 'global' ? 'global' : 'cn')));
          }
        }
      }
      setCatalog({cn: cn.models, global: global.models, realms});
    } catch {
      setFailed(true);
    }
  }

  /**
   * 平台模型（Trae / Qoder / 组合）：与 WorkBuddy 的版本目录是两套清单。
   *
   * 为什么还要逐个平台再取一次 `/models`：跨平台的 `/api/platforms/model-catalog`
   * 会按**裸模型名**去重（组合只能选裸名），双区同名模型（Qoder）于是只剩一条，
   * 而白名单要的是**带区域前缀的对外名**（`qodercn/x` 与 `qodercom/x` 是两个
   * 不同的名字，放行一个不等于放行另一个）。分组标签与错误说明仍用那份聚合清单。
   *
   * 失败**不影响** WorkBuddy 两组，也不挡住手输：逐组补齐、逐组说明原因。
   */
  async function ensurePlatforms() {
    if (platformGroups) return;
    setPlatformsBusy(true);
    try {
      const cat = await platformApi.catalog();
      // WorkBuddy 由上面两个版本组承担（它的模型名就是裸名 / `global:`），
      // 这里只列真正的平台。
      const targets = cat.platforms.filter((p) => p.name !== 'workbuddy');
      setPlatformGroups(targets.map((p) => ({
        key: p.name, label: p.label || p.name, models: [],
        error: p.error ? p.error : undefined,
      })));
      await Promise.all(targets.map(async (p) => {
        let models: string[] = [];
        let error = p.error;
        if (!error) {
          try {
            const r = await platformApi.models(p.name);
            models = r.models.map((m) => String(m.id || '').trim()).filter(Boolean);
          } catch {
            error = t('keys.modelPickPlatformError');
          }
        }
        setPlatformGroups((prev) => (prev || []).map(
          (g) => (g.key === p.name ? {...g, models, error} : g)));
      }));
    } catch {
      // 平台清单都取不到：只显示 WorkBuddy 两组，不挡住手输
      setPlatformGroups([]);
    } finally {
      // 逐组补齐是「一组回来先显示一组」，所以「还在读」不能看 `platformGroups`
      // 是不是 null（第一组回来就变成非 null 了，提示会提前消失、列表还在长）。
      setPlatformsBusy(false);
    }
  }

  // 与密钥「限定版本」一致的那一组排前面：国内版密钥最可能选的是国内版模型。
  const isFree = (m: CatalogModel) => (m.tags || []).some((tag) => tag.includes('免费'));
  const sections: PickSection[] = [];
  if (catalog) {
    const hasAccounts = (r: Realm) => !catalog.realms || catalog.realms.has(r);
    const wb: PickSection[] = [];
    if (hasAccounts('cn')) {
      wb.push({
        key: 'cn', label: t('realm.cn'),
        items: catalog.cn.map((m) => ({
          value: m.id, free: isFree(m), context: m.context_length,
        })),
      });
    }
    if (hasAccounts('global')) {
      wb.push({
        key: 'global', label: t('realm.global'),
        // 国际版条目在目录里是裸名（目录按版本取），但调用时必须带 `global:` 前缀
        // 才能路由到国际版账号池；白名单是按**请求里的实际名字**比对的。存裸名会出现
        // 「列表里能选、一调用就 400 不在白名单内」（issue #357），所以勾选时存限定名。
        // 密钥已锁定版本时后端会把两种前缀都归一，带不带前缀都能用。
        items: catalog.global.map((m) => ({
          value: qualifyGlobal(m.id), free: isFree(m), context: m.context_length,
        })),
      });
    }
    // 排序要就地进行：写成 `[...].sort(...)` 会让 TS 把字面量的 key 推宽成 string，
    // 整个数组就不再是声明的那个类型（tsc 报过）。
    wb.sort((a, b) => (a.key === realm ? -1 : b.key === realm ? 1 : 0));
    sections.push(...wb);
  }
  for (const g of platformGroups ?? []) {
    sections.push({
      key: `platform:${g.key}`,
      label: g.label,
      items: g.models.map((id) => ({value: id})),
      note: g.error,
    });
  }
  const needle = query.trim().toLowerCase();
  const filtered = sections
    .map((s) => ({
      ...s,
      items: s.items.filter((it) =>
        (!needle || it.value.toLowerCase().includes(needle))
        && (!freeOnly || it.free)),
    }))
    .filter((s) => s.items.length > 0 || (s.note && !needle && !freeOnly));
  const loadingPlatforms = platformsBusy || platformGroups === null;
  // 三种「列表里什么都没有」的原因分开说，别混成一句：WorkBuddy 目录读失败、
  // 还在读、读到了但确实没有匹配项。平台分组还在补时也不说「没有匹配」——
  // 那时列表还会继续变长，说「没有」是错的。
  const nothingToShow = filtered.length === 0;
  const showLoading = nothingToShow && !failed && !catalog;
  // WorkBuddy 目录失败时**始终**说一句：平台分组可能照样有内容，但用户要知道
  // 那两个版本组为什么是空的。
  const showUnavailable = failed;
  const showEmpty = nothingToShow && catalog && !loadingPlatforms;

  return (
    <div className="space-y-1.5">
      {names.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {names.map((name) => (
            <span
              key={name}
              className="inline-flex items-center gap-1 rounded-full bg-muted px-2 py-0.5 font-mono text-[10px]"
            >
              {name}
              {!disabled && (
                <button
                  type="button"
                  aria-label={t('keys.modelRemove', {name})}
                  onClick={() => remove(name)}
                  className="text-muted-foreground hover:text-foreground"
                >
                  <X className="h-3 w-3" />
                </button>
              )}
            </span>
          ))}
        </div>
      )}
      <div className="flex gap-1.5">
        <Input
          value={draft}
          disabled={disabled}
          placeholder={placeholder}
          onChange={(e) => {
            onEdit?.();            // 值还没提交，先把上一次的校验结论作废
            // 逗号是既有的分隔写法：粘贴一串名字时按它就拆开
            if (e.target.value.includes(',')) {
              // 粘贴「a, b, c」时**一次算好再提交**：逐个调用 add() 会各自基于同一份
              // 旧 names 计算，最后只剩最后一个（复审时发现的真 bug）。
              const parts = e.target.value.split(',').map((s) => s.trim()).filter(Boolean);
              if (parts.length) emit([...names, ...parts]);
              setDraft('');
              return;
            }
            setDraft(e.target.value);
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault();
              add(draft);
            } else if (e.key === 'Backspace' && !draft && names.length) {
              remove(names[names.length - 1]);
            }
          }}
          onBlur={() => {
            // 把提交后的新值一起给出去：父组件拿它做「这些名字存在吗」的校验，
            // 而它自己的 state 此刻还没更新（读旧值会漏掉刚输入的这一个）。
            const merged = Array.from(new Set([...names, draft.trim()].filter(Boolean)));
            if (draft.trim()) onChange(merged.join(', '));
            setDraft('');
            onBlur?.(merged.join(', '));
          }}
        />
        <Popover
          open={open}
          onOpenChange={(next) => {
            setOpen(next);
            if (next) {
              // Portal 落点要放进当前弹窗内容里（issue #438）：Radix Dialog 的
              // RemoveScroll 只放行「被锁元素子树内」的滚轮，默认挂 body 的浮层
              // 会被判为外部、滚轮被拦截（拖滚动条不受影响，所以很容易漏掉）。
              setPortalContainer(
                triggerRef.current?.closest('[data-slot="dialog-content"]') as HTMLElement | null);
              void ensureCatalog();
              void ensurePlatforms();
            }
          }}
        >
          <PopoverTrigger asChild>
            <Button
              ref={triggerRef}
              type="button"
              variant="outline"
              size="sm"
              disabled={disabled}
              className="shrink-0 rounded-full"
            >
              <ChevronsUpDown className="h-3.5 w-3.5" />
              {t('keys.modelPick')}
            </Button>
          </PopoverTrigger>
          <PopoverContent align="end" container={portalContainer} className="w-[320px] p-2">
            <div className="mb-2 flex items-center gap-1.5 rounded-full bg-muted px-2.5 py-1">
              <Search className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
              <input
                autoFocus
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder={t('keys.modelPickSearch')}
                className="w-full bg-transparent text-xs outline-none"
              />
            </div>
            <div className="mb-2 flex items-center gap-1.5">
              <button
                type="button"
                aria-pressed={freeOnly}
                onClick={() => setFreeOnly((v) => !v)}
                className={cn(
                  'rounded-full px-2 py-0.5 text-[11px] transition-colors',
                  freeOnly ? 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300'
                    : 'bg-muted text-muted-foreground hover:text-foreground',
                )}
              >
                {t('keys.freeOnly')}
              </button>
              {freeOnly && filtered.length > 0 && (
                <button
                  type="button"
                  onClick={() => emit([...names, ...filtered.flatMap(
                    (g) => g.items.filter((it) => it.free).map((it) => it.value))])}
                  className="rounded-full bg-muted px-2 py-0.5 text-[11px] text-muted-foreground hover:text-foreground"
                >
                  {t('keys.freeSelectAll')}
                </button>
              )}
            </div>
            <div className="max-h-[260px] overflow-y-auto">
              {showUnavailable && (
                <p className="px-2 py-6 text-center text-xs text-muted-foreground">
                  {t('keys.modelPickUnavailable')}
                </p>
              )}
              {showLoading && (
                <p className="px-2 py-6 text-center text-xs text-muted-foreground">
                  {t('keys.modelPickLoading')}
                </p>
              )}
              {showEmpty && (
                <p className="px-2 py-6 text-center text-xs text-muted-foreground">
                  {t('keys.modelPickEmpty')}
                </p>
              )}
              {filtered.map((group) => (
                <div key={group.key} className="mb-1">
                  <div className="px-2 py-1 text-[11px] text-muted-foreground">{group.label}</div>
                  {group.note && (
                    <p className="px-2 py-1 text-[11px] leading-4 text-amber-600 dark:text-amber-400">
                      {group.note}
                    </p>
                  )}
                  {group.items.map((item) => {
                    const picked = names.includes(item.value);
                    return (
                      <button
                        key={item.value}
                        type="button"
                        role="checkbox"
                        aria-checked={picked}
                        onClick={() => (picked ? remove(item.value) : add(item.value))}
                        className={cn(
                          'flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs',
                          'hover:bg-muted',
                          picked && 'bg-muted/70',
                        )}
                      >
                        <span
                          className={cn(
                            'flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded border',
                            picked ? 'border-primary bg-primary text-primary-foreground' : 'border-border',
                          )}
                        >
                          {picked && <Check className="h-2.5 w-2.5" />}
                        </span>
                        <span className="min-w-0 flex-1 truncate font-mono">{item.value}</span>
                        {item.free && (
                          <span className="shrink-0 rounded-full bg-emerald-500/15 px-1.5 text-[10px] text-emerald-700 dark:text-emerald-300">
                            {t('keys.freeBadge')}
                          </span>
                        )}
                        {typeof item.context === 'number' && item.context > 0 && (
                          <span className="shrink-0 text-[11px] text-muted-foreground">
                            {Math.round(item.context / 1000)}k
                          </span>
                        )}
                      </button>
                    );
                  })}
                </div>
              ))}
              {loadingPlatforms && (
                <p className="px-2 py-2 text-center text-[11px] text-muted-foreground">
                  {t('keys.modelPickPlatformLoading')}
                </p>
              )}
            </div>
          </PopoverContent>
        </Popover>
      </div>
    </div>
  );
}

/** 平台分组：某个平台取不到目录时 `models` 为空、`error` 写明原因。 */
interface PlatformGroup {
  key: string;
  label: string;
  models: string[];
  error?: string;
}

/** 清单里的一行（值就是白名单里要存的字面名）。 */
interface PickItem {
  value: string;
  /** 上游标注「限时免费」；平台模型没有这个标注，按非免费处理 */
  free?: boolean;
  /** 上下文窗口（token，0/缺省 = 未知） */
  context?: number;
}

interface PickSection {
  key: string;
  label: string;
  items: PickItem[];
  /** 这一组的说明 / 失败原因（如平台目录取不到） */
  note?: string;
}

/** 国际版条目要带 `global:` 前缀（issue #357）；已带前缀的原样返回。 */
function qualifyGlobal(id: string): string {
  return id.toLowerCase().startsWith('global:') ? id : `global:${id}`;
}

function splitNames(value: string): string[] {
  return Array.from(new Set(
    value.split(/[\n,]/).map((s) => s.trim()).filter(Boolean),
  ));
}
