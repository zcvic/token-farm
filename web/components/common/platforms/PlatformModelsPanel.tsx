'use client';

import {useEffect, useMemo, useRef, useState} from 'react';
import {
  Boxes,
  Brain,
  Download,
  Gauge,
  Loader2,
  Maximize2,
  RefreshCw,
  RotateCcw,
  Search,
  Upload,
} from 'lucide-react';

import {EmptyState} from '@/components/common/layout/EmptyState';
import {StatCard} from '@/components/common/layout/StatCard';
import {LoadError} from '@/components/common/states/LoadError';
import {SkeletonBar} from '@/components/common/states/SkeletonBar';
import {
  ModelFilterBar,
  type ModelCapability,
  type ModelSort,
} from '@/components/common/models/ModelFilterBar';
import {SeriesBadge} from '@/components/common/models/SeriesBadge';
import {Button} from '@/components/ui/button';
import {Badge} from '@/components/ui/badge';
import {CopyButton} from '@/components/ui/copy-button';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import {errText, platformApi} from '@/lib/api';
import {useT} from '@/lib/i18n/provider';
import {seriesOfModelId} from '@/lib/model-series';
import {filterByPlatformRealm} from '@/lib/platform-realm';
import {useRealm} from '@/lib/realm-context';
import {notify} from '@/lib/toast';
import {useAsyncAll} from '@/lib/use-async-data';
import type {PlatformModel, PlatformModelCatalog, PlatformModelRates} from '@/lib/types';

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

/** 平台目录没有 series 字段，按 id 前缀推导（规则与模型中心同源）。 */
function modelSeries(model: PlatformModel): string {
  return seriesOfModelId(model.model_name || model.id);
}

/**
 * 非 WorkBuddy 平台的模型清单（原「多平台」页里的模型部分）。
 *
 * 页面外壳（标题、说明）由调用方提供。这里只负责一个平台的数据：
 * `platformApi.models(platform, force)`，其中 `force` 绕过**后端缓存**。
 *
 * 用 ref 而不是 state 记录 `force`：它描述的是「下一次请求要不要绕缓存」，
 * 不参与渲染，也不该触发重渲染。
 */
export function PlatformModelsPanel({platform, realm, supportsModelRates = false,
                                     supportsModelCatalog = false}: {
  platform: string;
  /**
   * 当前版本（平台口径：`cn` / `intl`）。分国内版 / 国际版的平台（Trae）
   * 只列这个版本的模型；不支持版本的平台不传，行为与升级前一致。
   */
  realm?: string | null;
  /**
   * 该平台是否支持维护「积分消耗倍率」表（Trae 支持）。
   *
   * 服务器部署拿不到本机客户端快照时，倍率只能来自内置默认值或用户导入的
   * JSON，所以入口要跟着平台能力走，而不是写死平台名。
   */
  supportsModelRates?: boolean;
  /**
   * 该平台是否支持维护「模型目录」（Trae 支持）。
   *
   * 与倍率同构的三层来源：面板导入 > 本机客户端快照 > 内置默认值。服务器
   * 部署靠内置默认值或导入的 JSON 提供完整目录。
   */
  supportsModelCatalog?: boolean;
}) {
  const t = useT();
  const {label: realmName} = useRealm();
  const forceModels = useRef(false);
  const rateFile = useRef<HTMLInputElement>(null);
  const catalogFile = useRef<HTMLInputElement>(null);

  const [q, setQ] = useState('');
  const [series, setSeries] = useState('all');
  const [cap, setCap] = useState<ModelCapability>('all');
  const [sort, setSort] = useState<ModelSort>('default');

  /**
   * 切平台 / 切版本时清掉筛选状态：上一套目录里的系列在新范围可能一个模型
   * 都没有，留着会让列表看起来像「这个平台没有模型」，而数据其实是好的。
   */
  useEffect(() => {
    setQ('');
    setSeries('all');
    setCap('all');
    setSort('default');
  }, [platform, realm]);

  /**
   * `supportsModelRates` / `supportsModelCatalog` 来自 `/api/platforms`，首屏
   * 可能还没到（先渲染成 false）。它一变就要把对应数据补上，所以放进
   * refreshDeps：只重取、不清空已显示的模型清单（`deps` 才是「换平台」那种
   * 要清空的上下文）。
   */
  const modelsState = useAsyncAll({
    models: () => platformApi.models(platform, forceModels.current).then((r) => r.models),
    ...(supportsModelRates
      ? {rates: () => platformApi.modelRates(platform)}
      : {}),
    ...(supportsModelCatalog
      ? {catalog: () => platformApi.modelCatalog(platform)}
      : {}),
  }, [platform], [supportsModelRates, supportsModelCatalog]);

  // 稳定引用：系列/统计/筛选三个 useMemo 都以它为依赖。
  const models = useMemo(() => modelsState.values.models ?? [], [modelsState.values.models]);
  /**
   * 版本过滤后的清单：分版本的平台只展示当前版本。缺 `realm` 的条目按
   * 国内版算（与后端目录归一化的默认值一致）。
   */
  const listed = useMemo(
    () => (realm ? filterByPlatformRealm(models, realm) : models),
    [models, realm],
  );
  const rates: PlatformModelRates | undefined = modelsState.values.rates;
  const importedCount = rates?.sources.imported ?? 0;
  const catalog: PlatformModelCatalog | undefined = modelsState.values.catalog;
  const importedCatalog = catalog?.sources.imported ?? 0;

  /** 系列选项按当前目录现算：平台适配器不下发 series，前端推导即可。 */
  const seriesOptions = useMemo(
    () => Array.from(new Set(listed.map(modelSeries))).sort((a, b) => a.localeCompare(b)),
    [listed],
  );

  /** 一个倍率都没有时（非 Trae 平台）不显示「积分倍率 ↑」排序。 */
  const hasCreditRatio = useMemo(
    () => listed.some((m) => typeof m.credit_ratio === 'number'),
    [listed],
  );

  /**
   * 统计卡数据，全部由当前清单真实计算（与模型中心同一口径）：
   * 「支持推理」认 `supports_reasoning`，「大上下文」按 128K 起算。
   */
  const summary = useMemo(() => {
    let reasoning = 0;
    let large = 0;
    let maxContext = 0;
    for (const m of listed) {
      if (m.supports_reasoning) reasoning += 1;
      if ((m.context_window || 0) >= 131072) large += 1;
      if ((m.context_window || 0) > maxContext) maxContext = m.context_window || 0;
    }
    return {total: listed.length, reasoning, large, maxContext};
  }, [listed]);

  const filtered = useMemo(() => {
    const kw = q.trim().toLowerCase();
    const list = listed.filter((m) => {
      const s = modelSeries(m);
      if (series !== 'all' && s !== series) return false;
      if (cap === 'reasoning' && !m.supports_reasoning) return false;
      if (cap === 'large' && (m.context_window || 0) < 131072) return false;
      if (cap === 'vision' && !m.modalities?.input?.includes('image')) return false;
      if (!kw) return true;
      return (
        m.id.toLowerCase().includes(kw) ||
        (m.name || '').toLowerCase().includes(kw) ||
        (m.model_name || '').toLowerCase().includes(kw) ||
        s.toLowerCase().includes(kw)
      );
    });
    if (sort === 'credits') {
      // 倍率从低到高。**没有倍率的排在最后**——不是 0，不能当成「免费」。
      const num = (v?: number | null) => (typeof v === 'number' ? v : null);
      return [...list].sort((a, b) => {
        const na = num(a.credit_ratio);
        const nb = num(b.credit_ratio);
        if (na === null && nb === null) return 0;
        if (na === null) return 1;
        if (nb === null) return -1;
        return na - nb;
      });
    }
    return list;
  }, [listed, q, series, cap, sort]);

  async function refreshModels() {
    forceModels.current = true;
    const pending = notify.info(t('common.loading'));
    try {
      const ok = await modelsState.reload();
      notify.dismiss(pending);
      if (ok) notify.ok(t('platforms.modelsDone'));
      else notify.err(t('platforms.noModelsDesc'), errText(modelsState.errors.models));
    } finally {
      forceModels.current = false;
    }
  }

  /**
   * 导入倍率 JSON。
   *
   * 后端接受 `{模型: 倍率}`、`{rates: {...}}` 和数组三种形状（见
   * `server/platforms/trae/rates.py`），所以这里只校验「是不是合法 JSON」，
   * 形状交给后端统一判断——前端再抄一份规则只会两边漂移。
   */
  async function importRates(file: File) {
    const pending = notify.info(t('common.loading'));
    try {
      let body: unknown;
      try {
        body = JSON.parse(await file.text());
      } catch {
        notify.dismiss(pending);
        notify.err(t('platforms.ratesImportBad'));
        return;
      }
      await platformApi.importModelRates(platform, body);
      notify.dismiss(pending);
      notify.ok(t('platforms.ratesImportDone'));
      // 导入后倍率列要立刻变，所以顺带重拉一次模型目录（后端会重新合并）。
      await modelsState.reload();
    } catch (e) {
      notify.dismiss(pending);
      notify.err(t('platforms.ratesImportFail'), errText(e));
    }
  }

  async function exportRates() {
    try {
      const data = await platformApi.exportModelRates(platform);
      const blob = new Blob([`${JSON.stringify(data, null, 2)}\n`],
        {type: 'application/json'});
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `${platform}-model-rates-${new Date().toISOString().slice(0, 10)}.json`;
      link.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      notify.err(t('platforms.ratesExportFail'), errText(e));
    }
  }

  async function resetRates() {
    try {
      await platformApi.resetModelRates(platform);
      notify.ok(t('platforms.ratesResetDone'));
      await modelsState.reload();
    } catch (e) {
      notify.err(t('platforms.ratesImportFail'), errText(e));
    }
  }

  /**
   * 导入目录 JSON。
   *
   * 后端接受 `{models: [...]}`、裸数组、id→字段 映射，以及参考项目的
   * `trae_catalog_*.json`（见 `server/platforms/trae/catalog.py`）。前端只
   * 校验「是不是合法 JSON」，形状交给后端统一判断。
   */
  async function importCatalog(file: File) {
    const pending = notify.info(t('common.loading'));
    try {
      let body: unknown;
      try {
        body = JSON.parse(await file.text());
      } catch {
        notify.dismiss(pending);
        notify.err(t('platforms.catalogImportBad'));
        return;
      }
      await platformApi.importModelCatalog(platform, body);
      notify.dismiss(pending);
      notify.ok(t('platforms.catalogImportDone'));
      await modelsState.reload();
    } catch (e) {
      notify.dismiss(pending);
      notify.err(t('platforms.catalogImportFail'), errText(e));
    }
  }

  async function exportCatalog() {
    try {
      const data = await platformApi.exportModelCatalog(platform);
      const blob = new Blob([`${JSON.stringify(data, null, 2)}\n`],
        {type: 'application/json'});
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `${platform}-model-catalog-${new Date().toISOString().slice(0, 10)}.json`;
      link.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      notify.err(t('platforms.catalogExportFail'), errText(e));
    }
  }

  async function resetCatalog() {
    try {
      await platformApi.resetModelCatalog(platform);
      notify.ok(t('platforms.catalogResetDone'));
      await modelsState.reload();
    } catch (e) {
      notify.err(t('platforms.catalogImportFail'), errText(e));
    }
  }

  const hasFilter = q.trim() !== '' || series !== 'all' || cap !== 'all';

  return (
    <>
      {/* 统计卡：与模型中心同一口径；首屏还没拿到目录时不显示，避免用 0 误导 */}
      {!modelsState.isInitialLoading && listed.length > 0 && (
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <StatCard icon={Boxes} label={t('models.available')} value={String(summary.total)} />
          <StatCard
            icon={Brain}
            label={t('models.reasoning')}
            value={String(summary.reasoning)}
          />
          <StatCard
            icon={Maximize2}
            label={t('models.largeContext')}
            value={String(summary.large)}
            hint="≥128K"
          />
          <StatCard
            icon={Gauge}
            label={t('models.maxContext')}
            value={fmtCtx(summary.maxContext)}
            hint={
              seriesOptions.length
                ? t('models.seriesCount', {count: seriesOptions.length, n: seriesOptions.length})
                : undefined
            }
          />
        </div>
      )}

      <section className="overflow-hidden rounded-[20px] bg-muted">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border/60 px-3.5 py-3">
          <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
            <Boxes className="h-3.5 w-3.5" />
            <h2 className="text-xs font-medium text-foreground">{t('platforms.modelsTitle')}</h2>
            <Badge variant="secondary" className="rounded-md px-1.5 text-[10px] tabular-nums">
              {listed.length}
            </Badge>
          </div>
          <div className="flex flex-wrap items-center gap-1.5">
            {supportsModelRates && (
              <>
                <input
                  ref={rateFile}
                  type="file"
                  accept="application/json,.json"
                  className="hidden"
                  onChange={(e) => {
                    const file = e.target.files?.[0];
                    // 清空 value：同一个文件改完再选一次也要能触发 onChange。
                    e.target.value = '';
                    if (file) void importRates(file);
                  }}
                />
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-7 rounded-full px-2.5 text-[11px]"
                  onClick={() => rateFile.current?.click()}
                  title={t('platforms.ratesImportTitle')}
                >
                  <Upload />
                  {t('platforms.ratesImport')}
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-7 rounded-full px-2.5 text-[11px]"
                  onClick={() => void exportRates()}
                  title={t('platforms.ratesExportTitle')}
                >
                  <Download />
                  {t('platforms.ratesExport')}
                </Button>
                {importedCount > 0 && (
                  <Button
                    variant="ghost"
                    size="sm"
                    className="h-7 rounded-full px-2.5 text-[11px] text-muted-foreground"
                    onClick={() => void resetRates()}
                    title={t('platforms.ratesResetTitle')}
                  >
                    <RotateCcw />
                    {t('platforms.ratesReset')}
                  </Button>
                )}
              </>
            )}
            <Button
              variant="outline"
              size="sm"
              className="h-7 rounded-full px-2.5 text-[11px]"
              disabled={modelsState.isRefreshing}
              onClick={() => void refreshModels()}
            >
              {modelsState.isRefreshing ? <Loader2 className="animate-spin" /> : <RefreshCw />}
              {t('platforms.modelsRefresh')}
            </Button>
          </div>
        </div>

        {supportsModelRates && rates && (
          <p className="border-b border-border/60 px-3.5 py-2 text-[11px] text-muted-foreground">
            {t('platforms.ratesSource', {
              imported: rates.sources.imported,
              desktop: rates.sources.desktop,
              bundled: rates.sources.bundled,
            })}
            {rates.imported_at ? ` · ${rates.imported_at}` : ''}
          </p>
        )}

        {supportsModelCatalog && catalog && (
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border/60 px-3.5 py-2 text-[11px] text-muted-foreground">
            <span>
              {t('platforms.catalogSource', {
                total: catalog.total,
                imported: catalog.sources.imported,
                desktop: catalog.sources.desktop,
                bundled: catalog.sources.bundled,
              })}
              {catalog.imported_at ? ` · ${catalog.imported_at}` : ''}
            </span>
            <div className="flex flex-wrap items-center gap-1.5">
              <input
                ref={catalogFile}
                type="file"
                accept="application/json,.json"
                className="hidden"
                onChange={(e) => {
                  const file = e.target.files?.[0];
                  // 清空 value：同一个文件改完再选一次也要能触发 onChange。
                  e.target.value = '';
                  if (file) void importCatalog(file);
                }}
              />
              <Button
                variant="ghost"
                size="sm"
                className="h-7 rounded-full px-2.5 text-[11px]"
                onClick={() => catalogFile.current?.click()}
                title={t('platforms.catalogImportTitle')}
              >
                <Upload />
                {t('platforms.catalogImport')}
              </Button>
              <Button
                variant="ghost"
                size="sm"
                className="h-7 rounded-full px-2.5 text-[11px]"
                onClick={() => void exportCatalog()}
                title={t('platforms.catalogExportTitle')}
              >
                <Download />
                {t('platforms.catalogExport')}
              </Button>
              {importedCatalog > 0 && (
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-7 rounded-full px-2.5 text-[11px] text-muted-foreground"
                  onClick={() => void resetCatalog()}
                  title={t('platforms.catalogResetTitle')}
                >
                  <RotateCcw />
                  {t('platforms.catalogReset')}
                </Button>
              )}
            </div>
          </div>
        )}

        {listed.length > 0 && (
          <div className="border-b border-border/60 p-3.5">
            <ModelFilterBar
              q={q}
              onQChange={setQ}
              series={series}
              seriesOptions={seriesOptions}
              onSeriesChange={setSeries}
              cap={cap}
              onCapChange={setCap}
              sort={sort}
              onSortChange={setSort}
              showCreditSort={hasCreditRatio}
            />
          </div>
        )}

        {!!modelsState.errors.models && (
          <div className="px-3.5 pt-3">
            <LoadError
              variant="inline"
              message={errText(modelsState.errors.models)}
              onRetry={modelsState.reload}
            />
          </div>
        )}

        {modelsState.isInitialLoading ? (
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
        ) : listed.length === 0 ? (
          /* 有模型，但都属于另一个版本：说「没有模型」会让人去重复导入。 */
          <EmptyState
            icon={Boxes}
            title={t('platforms.noModelsRealm', {realm: realmName})}
            description={t('platforms.noModelsRealmDesc')}
            className="flex flex-col items-center justify-center p-8 text-center"
          />
        ) : filtered.length === 0 ? (
          <EmptyState
            icon={Search}
            title={t('models.noMatch')}
            description={t('models.noMatchDesc')}
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
                  <TableHead
                    className="hidden text-[11px] text-muted-foreground md:table-cell"
                    title={t('platforms.colCreditTitle')}
                  >
                    {t('platforms.colCredit')}
                  </TableHead>
                  <TableHead className="pr-4 text-right text-[11px] text-muted-foreground">
                    {t('models.series')}
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {filtered.map((m: PlatformModel) => (
                  <TableRow key={m.id} className="border-b border-border/40">
                    <TableCell className="pl-4">
                      <div className="flex flex-col gap-0.5 py-0.5">
                        <span className="text-xs font-medium">{m.name || m.model_name}</span>
                        <div className="flex min-w-0 items-center gap-1">
                          <span className="min-w-0 truncate font-mono text-[11px] text-muted-foreground">
                            {m.id}
                          </span>
                          <CopyButton
                            value={m.id}
                            title={t('platforms.copyModelId')}
                            className="h-6 w-6"
                          />
                        </div>
                      </div>
                    </TableCell>
                    <TableCell className="text-xs tabular-nums text-muted-foreground">
                      {fmtCtx(m.context_window)}
                    </TableCell>
                    <TableCell className="hidden text-xs tabular-nums text-muted-foreground sm:table-cell">
                      {fmtCtx(m.max_tokens)}
                    </TableCell>
                    <TableCell className="hidden text-xs tabular-nums text-muted-foreground md:table-cell">
                      {typeof m.credit_ratio === 'number' ? `${m.credit_ratio} ×` : '—'}
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
                        <SeriesBadge series={modelSeries(m)} />
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}

        {hasFilter && (
          <div className="border-t border-border/40 px-4 py-2 text-[11px] text-muted-foreground">
            {t('models.filteredCount', {n: filtered.length, total: listed.length})}
          </div>
        )}
      </section>
    </>
  );
}
