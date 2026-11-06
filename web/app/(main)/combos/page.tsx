'use client';

/**
 * 模型组合：把多个平台/模型按权重挂到同一个下游名（`combo/<name>`）上。
 *
 * ## 它解决什么
 *
 * 一个模型名只能对应一个平台时，积分只能从那一侧扣。组合让同一个下游名字
 * 后面对着一组成员，每个请求按权重挑一个发出去——整体上积分就从多个平台扣。
 * **单个请求仍然只落在一个成员上**：上游的一次对话没法拆成两半计费，
 * 所以这里是请求级分摊，不是单请求拆账。
 *
 * ## 为什么放在「模型」节的第三项
 *
 * 它描述的就是「用哪些模型」：看目录 → 试一下 → 编成一组。独立成一级入口
 * 会让底栏多一个「只在配好之后才有用」的目的地。
 *
 * ## 为什么整份提交
 *
 * 配置量很小（个位数组合），整份替换让「删成员 / 改权重 / 改名字」走同一
 * 条路径，后端也不必为每种局部修改各写一份校验。代价是并发编辑后写覆盖，
 * 但这份配置只有管理员偶尔改一次，用不到更重的方案。
 */
import {useCallback, useEffect, useMemo, useState} from 'react';
import {
  Combine,
  Loader2,
  Pencil,
  Plus,
  RefreshCw,
  Trash2,
} from 'lucide-react';

import {PageHeader} from '@/components/common/layout/PageHeader';
import {PageSectionTabs} from '@/components/common/layout/PageSectionTabs';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {LoadError} from '@/components/common/states/LoadError';
import {SkeletonBar} from '@/components/common/states/SkeletonBar';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
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
import {Switch} from '@/components/ui/switch';
import {comboApi, errText, platformApi} from '@/lib/api';
import {useAuth} from '@/lib/auth-context';
import {useT} from '@/lib/i18n/provider';
import {notify} from '@/lib/toast';
import {cn} from '@/lib/utils';
import type {Combo, ComboMember, PlatformCatalogPlatform} from '@/lib/types';

/** 组合名会成为 `combo/<name>` 的后半段，与后端 `combo.NAME_RE` 同一口径。 */
const NAME_RE = /^[a-z0-9][a-z0-9._-]{0,31}$/;

const WEIGHT_MIN = 0.1;
const WEIGHT_MAX = 100;

type DraftMember = {
  platform: string;
  model: string;
  /** 保留用户输入的原始文本，提交前才转数字（中间态 `0.`、空串都合法） */
  weight: string;
  enabled: boolean;
};

type Draft = {
  /** 编辑已有组合时记住原名：改名等于删除旧名 + 新建，不能被当成一条新记录 */
  originalName: string | null;
  name: string;
  enabled: boolean;
  members: DraftMember[];
};

function toDraft(combo: Combo): Draft {
  return {
    originalName: combo.name,
    name: combo.name,
    enabled: combo.enabled,
    members: combo.members.map((m) => ({
      platform: m.platform,
      model: m.model,
      weight: String(m.weight),
      enabled: m.enabled,
    })),
  };
}

function draftMemberKey(member: DraftMember): string {
  return `${member.platform}/${member.model}`;
}

export default function CombosPage() {
  const t = useT();
  const {isAdmin} = useAuth();

  const [combos, setCombos] = useState<Combo[]>([]);
  const [catalog, setCatalog] = useState<PlatformCatalogPlatform[]>([]);
  const [catalogError, setCatalogError] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState('');
  const [draft, setDraft] = useState<Draft | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const [list, cat] = await Promise.all([
        comboApi.list(),
        platformApi.catalog().catch((e: unknown) => {
          // 目录只是「选成员」的辅助数据：它取不到时仍然要看得到已有组合，
          // 否则一个平台令牌过期就会让整页变成错误页。
          setCatalogError(errText(e));
          return {platforms: [] as PlatformCatalogPlatform[]};
        }),
      ]);
      setCombos(list.combos);
      setCatalog(cat.platforms);
    } catch (e) {
      setError(errText(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const modelsOf = useCallback(
    (platform: string): PlatformCatalogPlatform | undefined =>
      catalog.find((p) => p.name === platform),
    [catalog],
  );

  const firstPlatform = catalog.find((p) => p.models.length > 0)?.name
    ?? catalog[0]?.name
    ?? '';

  function startNew() {
    if (!catalog.length) {
      notify.warn(t('combos.catalogEmpty'));
      return;
    }
    const platform = firstPlatform;
    const model = modelsOf(platform)?.models[0]?.model_name ?? '';
    setDraft({
      originalName: null,
      name: '',
      enabled: true,
      members: [{platform, model, weight: '1', enabled: true}],
    });
  }

  function updateMember(index: number, patch: Partial<DraftMember>) {
    setDraft((prev) => prev && {
      ...prev,
      members: prev.members.map((m, i) => (i === index ? {...m, ...patch} : m)),
    });
  }

  function changeMemberPlatform(index: number, platform: string) {
    // 换平台就换模型：旧模型名在新平台大概率不存在，留着会让保存后的成员
    // 指向一个调不通的名字（后端只校验平台存在，不会替我们查模型目录）。
    const model = modelsOf(platform)?.models[0]?.model_name ?? '';
    updateMember(index, {platform, model});
  }

  function addMember() {
    if (!catalog.length) return;
    const platform = firstPlatform;
    const model = modelsOf(platform)?.models[0]?.model_name ?? '';
    setDraft((prev) => prev && {
      ...prev,
      members: [...prev.members, {platform, model, weight: '1', enabled: true}],
    });
  }

  function removeMember(index: number) {
    setDraft((prev) => prev && {
      ...prev,
      members: prev.members.filter((_, i) => i !== index),
    });
  }

  /** 草稿 → 待保存配置；非法输入返回 null 并已提示（不静默丢字段）。 */
  function normalizeDraft(current: Draft): Combo | null {
    const name = current.name.trim().toLowerCase();
    if (!NAME_RE.test(name)) {
      notify.err(t('combos.nameInvalid'));
      return null;
    }
    if (current.members.length === 0) {
      notify.err(t('combos.noMembers'));
      return null;
    }
    const members: ComboMember[] = [];
    const seen = new Set<string>();
    for (const member of current.members) {
      if (!member.platform || !member.model.trim()) {
        notify.err(t('combos.memberInvalid'));
        return null;
      }
      const key = draftMemberKey(member);
      if (seen.has(key)) {
        notify.err(t('combos.duplicateMember', {member: key}));
        return null;
      }
      seen.add(key);
      const weight = Number(member.weight.trim() || 0);
      if (!Number.isFinite(weight) || weight < WEIGHT_MIN || weight > WEIGHT_MAX) {
        notify.err(t('combos.weightInvalid', {min: WEIGHT_MIN, max: WEIGHT_MAX}));
        return null;
      }
      members.push({
        platform: member.platform,
        model: member.model.trim(),
        weight,
        enabled: member.enabled,
      });
    }
    if (!members.some((m) => m.enabled)) {
      notify.err(t('combos.noEnabledMembers'));
      return null;
    }
    return {name, enabled: current.enabled, members};
  }

  async function saveDraft() {
    if (!draft || busy) return;
    const combo = normalizeDraft(draft);
    if (!combo) return;
    const next = draft.originalName === null
      ? [...combos, combo]
      : combos.map((c) => (c.name === draft.originalName ? combo : c));
    setBusy('save');
    try {
      const saved = await comboApi.save(next);
      setCombos(saved.combos);
      setDraft(null);
      notify.ok(t('combos.saved'), `combo/${combo.name}`);
    } catch (e) {
      notify.err(t('combos.saveFailed'), errText(e));
    } finally {
      setBusy('');
    }
  }

  async function removeCombo(combo: Combo) {
    if (busy) return;
    setBusy(`delete:${combo.name}`);
    try {
      const saved = await comboApi.save(combos.filter((c) => c.name !== combo.name));
      setCombos(saved.combos);
      if (draft?.originalName === combo.name) setDraft(null);
      notify.ok(t('combos.deleted'), combo.name);
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy('');
    }
  }

  const totalMembers = useMemo(
    () => combos.reduce((sum, c) => sum + c.members.length, 0),
    [combos],
  );

  const header = (
    <>
      <PageHeader
        title={t('combos.title')}
        description={t('combos.description')}
        actions={
          <div className="flex items-center gap-1.5">
            <Button
              variant="outline"
              size="sm"
              className="rounded-full"
              disabled={loading}
              onClick={() => void load()}
            >
              {loading ? <Loader2 className="animate-spin" /> : <RefreshCw />}
              {t('common.refresh')}
            </Button>
            {isAdmin && (
              <Button
                size="sm"
                className="rounded-full"
                disabled={busy !== '' || draft !== null}
                onClick={startNew}
              >
                <Plus />
                {t('combos.add')}
              </Button>
            )}
          </div>
        }
      />
      <PageSectionTabs />
    </>
  );

  return (
    <div className="flex flex-col gap-4 md:gap-6">
      {header}

      {loading ? (
        <CombosSkeleton />
      ) : error ? (
        <LoadError variant="page" message={error} onRetry={load} />
      ) : (
        <>
          {!!catalogError && (
            <div className="rounded-[16px] border border-amber-500/30 bg-amber-500/10 px-3.5 py-2.5 text-[11px] text-amber-700 dark:text-amber-400">
              {t('combos.catalogError', {message: catalogError})}
            </div>
          )}

          {!isAdmin && (
            <div className="rounded-[16px] bg-muted/60 px-3.5 py-2.5 text-[11px] text-muted-foreground">
              {t('combos.adminOnly')}
            </div>
          )}

          {draft && (
            <DraftEditor
              draft={draft}
              catalog={catalog}
              busy={busy === 'save'}
              onChange={setDraft}
              onMemberChange={updateMember}
              onMemberPlatformChange={changeMemberPlatform}
              onAddMember={addMember}
              onRemoveMember={removeMember}
              onCancel={() => setDraft(null)}
              onSave={() => void saveDraft()}
            />
          )}

          {combos.length === 0 && !draft ? (
            <section className="overflow-hidden rounded-[20px] bg-muted">
              <EmptyState
                icon={Combine}
                title={t('combos.empty')}
                description={t('combos.emptyDesc')}
                className="flex flex-col items-center justify-center p-8 text-center"
              >
                {isAdmin && (
                  <Button size="sm" className="mt-1 rounded-full" onClick={startNew}>
                    <Plus />
                    {t('combos.add')}
                  </Button>
                )}
              </EmptyState>
            </section>
          ) : (
            <div className="flex flex-col gap-3">
              {combos.map((combo) => (
                <ComboCard
                  key={combo.name}
                  combo={combo}
                  busy={busy === `delete:${combo.name}`}
                  editDisabled={!isAdmin || busy !== '' || draft !== null}
                  deleteDisabled={!isAdmin || busy !== ''}
                  onEdit={() => setDraft(toDraft(combo))}
                  onDelete={() => void removeCombo(combo)}
                />
              ))}
              {combos.length > 0 && (
                <p className="px-1 text-[11px] text-muted-foreground">
                  {t('combos.summary', {combos: combos.length, members: totalMembers})}
                </p>
              )}
            </div>
          )}
        </>
      )}

      <p className="text-[11px] leading-4 text-muted-foreground">
        {t('combos.footnote')}
      </p>
    </div>
  );
}

/** 一张组合卡片：对外名 + 启用状态 + 成员（平台/模型 × 权重）。 */
function ComboCard({
  combo,
  busy,
  editDisabled,
  deleteDisabled,
  onEdit,
  onDelete,
}: {
  combo: Combo;
  busy: boolean;
  editDisabled: boolean;
  deleteDisabled: boolean;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const t = useT();
  const enabledMembers = combo.members.filter((m) => m.enabled).length;

  return (
    <section className="overflow-hidden rounded-[20px] bg-muted">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border/60 px-3.5 py-3">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <Combine className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
          <span className="truncate font-mono text-xs font-medium">combo/{combo.name}</span>
          <Badge
            variant="secondary"
            className={cn(
              'rounded-md text-[10px]',
              combo.enabled
                ? 'text-emerald-700 dark:text-emerald-400'
                : 'text-muted-foreground',
            )}
          >
            {combo.enabled ? t('combos.enabledLabel') : t('combos.disabledLabel')}
          </Badge>
          <span className="text-[11px] text-muted-foreground">
            {t('combos.memberCount', {n: enabledMembers, total: combo.members.length})}
          </span>
        </div>
        <div className="flex items-center gap-1">
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7 rounded-md"
            title={t('combos.edit')}
            disabled={editDisabled}
            onClick={onEdit}
          >
            <Pencil className="h-3.5 w-3.5" />
          </Button>
          <ConfirmDialog
            title={t('combos.deleteTitle')}
            description={t('combos.deleteDesc', {name: combo.name})}
            confirmText={t('combos.delete')}
            destructive
            onConfirm={onDelete}
            trigger={
              <Button
                variant="ghost"
                size="icon"
                className="h-7 w-7 rounded-md text-destructive hover:text-destructive"
                title={t('combos.delete')}
                disabled={deleteDisabled || busy}
              >
                {busy ? <Loader2 className="animate-spin" /> : <Trash2 className="h-3.5 w-3.5" />}
              </Button>
            }
          />
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-1.5 px-3.5 py-3">
        {combo.members.map((m) => (
          <span
            key={`${m.platform}/${m.model}`}
            className={cn(
              'inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px]',
              m.enabled
                ? 'border-border bg-background'
                : 'border-dashed border-border text-muted-foreground line-through',
            )}
          >
            <span className="font-mono">{m.platform}/{m.model}</span>
            <span className="rounded-full bg-muted px-1.5 py-0.5 font-mono text-[10px] tabular-nums">
              ×{m.weight}
            </span>
          </span>
        ))}
      </div>
    </section>
  );
}

function DraftEditor({
  draft,
  catalog,
  busy,
  onChange,
  onMemberChange,
  onMemberPlatformChange,
  onAddMember,
  onRemoveMember,
  onCancel,
  onSave,
}: {
  draft: Draft;
  catalog: PlatformCatalogPlatform[];
  busy: boolean;
  onChange: (next: Draft) => void;
  onMemberChange: (index: number, patch: Partial<DraftMember>) => void;
  onMemberPlatformChange: (index: number, platform: string) => void;
  onAddMember: () => void;
  onRemoveMember: (index: number) => void;
  onCancel: () => void;
  onSave: () => void;
}) {
  const t = useT();

  return (
    <section className="overflow-hidden rounded-[20px] border border-border/70 bg-muted">
      <div className="border-b border-border/60 px-3.5 py-3">
        <h2 className="text-xs font-medium">
          {draft.originalName ? t('combos.editTitle', {name: draft.originalName}) : t('combos.addTitle')}
        </h2>
        <p className="mt-0.5 text-[11px] leading-4 text-muted-foreground">
          {t('combos.weightHint')}
        </p>
      </div>

      <div className="flex flex-col gap-3 px-3.5 py-3">
        <div className="flex flex-wrap items-end gap-2">
          <label className="flex min-w-[200px] flex-1 flex-col gap-1">
            <span className="text-[11px] text-muted-foreground">{t('combos.nameLabel')}</span>
            <Input
              value={draft.name}
              onChange={(e) => onChange({...draft, name: e.target.value})}
              placeholder={t('combos.namePlaceholder')}
              className="h-8 font-mono text-xs"
              spellCheck={false}
              autoComplete="off"
              autoFocus
            />
          </label>
          <label className="flex items-center gap-2 rounded-full border border-border/60 px-2.5 py-1.5 text-[11px]">
            <Switch
              checked={draft.enabled}
              onCheckedChange={(value) => onChange({...draft, enabled: value})}
              aria-label={t('combos.enabledLabel')}
            />
            {draft.enabled ? t('combos.enabledLabel') : t('combos.disabledLabel')}
          </label>
        </div>
        <p className="text-[10px] leading-4 text-muted-foreground">
          {t('combos.nameHint', {name: draft.name.trim().toLowerCase() || '<name>'})}
        </p>

        <div className="flex flex-col gap-2">
          {draft.members.map((member, index) => (
            <MemberRow
              key={index}
              member={member}
              catalog={catalog}
              onChange={(patch) => onMemberChange(index, patch)}
              onPlatformChange={(platform) => onMemberPlatformChange(index, platform)}
              onRemove={() => onRemoveMember(index)}
            />
          ))}
        </div>

        <div>
          <Button
            variant="outline"
            size="sm"
            className="h-7 rounded-full px-2.5 text-[11px]"
            onClick={onAddMember}
          >
            <Plus />
            {t('combos.addMember')}
          </Button>
        </div>
      </div>

      <div className="flex items-center justify-end gap-1.5 border-t border-border/60 px-3.5 py-2.5">
        <Button
          variant="outline"
          size="sm"
          className="rounded-full"
          disabled={busy}
          onClick={onCancel}
        >
          {t('common.cancel')}
        </Button>
        <Button size="sm" className="rounded-full" disabled={busy} onClick={onSave}>
          {busy ? <Loader2 className="animate-spin" /> : null}
          {t('common.save')}
        </Button>
      </div>
    </section>
  );
}

function MemberRow({
  member,
  catalog,
  onChange,
  onPlatformChange,
  onRemove,
}: {
  member: DraftMember;
  catalog: PlatformCatalogPlatform[];
  onChange: (patch: Partial<DraftMember>) => void;
  onPlatformChange: (platform: string) => void;
  onRemove: () => void;
}) {
  const t = useT();
  const platform = catalog.find((p) => p.name === member.platform);
  const models = platform?.models ?? [];

  return (
    <div className="flex flex-wrap items-center gap-2 rounded-[12px] border border-border/60 bg-background/60 px-2.5 py-2">
      <Select value={member.platform || undefined} onValueChange={onPlatformChange}>
        <SelectTrigger
          size="sm"
          className="w-[150px]"
          aria-label={t('combos.platformLabel')}
        >
          <SelectValue placeholder={t('combos.platformLabel')} />
        </SelectTrigger>
        <SelectContent>
          {catalog.map((p) => (
            <SelectItem key={p.name} value={p.name}>
              {p.label} · {p.name}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>

      {models.length > 0 ? (
        <Select
          value={member.model || undefined}
          onValueChange={(model) => onChange({model})}
        >
          <SelectTrigger
            size="sm"
            className="min-w-[220px] flex-1"
            aria-label={t('combos.modelLabel')}
          >
            <SelectValue placeholder={t('combos.modelLabel')} />
          </SelectTrigger>
          <SelectContent>
            {models.map((m) => (
              <SelectItem key={m.id} value={m.model_name}>
                {m.name || m.model_name}
                {m.name && m.name !== m.model_name ? ` · ${m.model_name}` : ''}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      ) : (
        // 目录取不到（令牌过期 / 平台不支持列模型）时退回手填：至少还能保存
        // 一个已知的模型名，而不是把用户卡在「选不出模型」的死角。
        <Input
          value={member.model}
          onChange={(e) => onChange({model: e.target.value})}
          placeholder={platform?.error || t('combos.modelPlaceholder')}
          className="h-8 min-w-[220px] flex-1 font-mono text-xs"
          spellCheck={false}
          autoComplete="off"
        />
      )}

      {platform?.pool_weight ? (
        // 池级权重：账号怎么分流由平台自己的池决定，这里的权重只管「组合内选到
        // 这个成员的概率」。两个口径混在一起会让人以为能在这里调账号权重。
        <span
          className="rounded-full border border-border/70 bg-muted/60 px-1.5 py-0.5 text-[10px] text-muted-foreground"
          title={t('combos.weightPoolHint')}
        >
          {t('combos.weightPoolBadge')}
        </span>
      ) : null}

      <label className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
        {t('combos.weightLabel')}
        <Input
          value={member.weight}
          onChange={(e) => onChange({weight: e.target.value})}
          inputMode="decimal"
          className="h-8 w-16 text-center text-xs tabular-nums"
        />
      </label>

      <label className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
        <Switch
          checked={member.enabled}
          onCheckedChange={(value) => onChange({enabled: value})}
          aria-label={t('combos.memberEnabled')}
        />
        {t('combos.memberEnabled')}
      </label>

      <Button
        variant="ghost"
        size="icon"
        className="h-7 w-7 rounded-md text-destructive hover:text-destructive"
        title={t('combos.removeMember')}
        onClick={onRemove}
      >
        <Trash2 className="h-3.5 w-3.5" />
      </Button>
    </div>
  );
}

function CombosSkeleton() {
  return (
    <div className="flex flex-col gap-3">
      {Array.from({length: 2}, (_, i) => (
        <div key={i} className="rounded-[20px] bg-muted p-3.5">
          <SkeletonBar className="h-4 w-48" />
          <div className="mt-3 flex flex-wrap gap-2">
            <SkeletonBar className="h-6 w-40 rounded-full" />
            <SkeletonBar className="h-6 w-32 rounded-full" />
          </div>
        </div>
      ))}
    </div>
  );
}
