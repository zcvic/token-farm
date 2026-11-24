'use client';

/**
 * 单个组合的请求日志弹窗。
 *
 * 组合是虚拟平台，日志里有两层名字：`model` 是客户端请求的 `combo/<name>`，
 * `mapped_model` 是这次实际落到的成员。用户在组合页最常问的就是「这个组合
 * 到底把请求分给了谁、有没有一直失败」，而日志页的模型筛选是模糊匹配
 * （`combo/fast` 会连带 `combo/fast2`），也没法给出「整个时间范围」的汇总。
 * 所以这里走后端专门的等值查询，并展示成功率 / 平均耗时。
 */

import {useCallback, useEffect, useState} from 'react';
import {Download, RefreshCw, ScrollText} from 'lucide-react';

import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/animate-ui/radix/dialog';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {LoadError} from '@/components/common/states/LoadError';
import {SkeletonBar} from '@/components/common/states/SkeletonBar';
import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import {errText, logApi} from '@/lib/api';
import {fmtCredit, fmtDateTimeMarked, fmtLatency, fmtNumber} from '@/lib/format';
import {useT} from '@/lib/i18n/provider';
import {notify} from '@/lib/toast';
import type {ComboLogsResponse, RequestLog} from '@/lib/types';

/** 一次最多展示多少条。汇总由后端按整段时间算，不受这个上限影响。 */
const LOG_LIMIT = 100;

export function ComboLogsDialog({
  name,
  open,
  onOpenChange,
}: {
  name: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const t = useT();
  const [days, setDays] = useState('7');
  const [data, setData] = useState<ComboLogsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    if (!name) return;
    setLoading(true);
    setError('');
    // 先清空再取：否则切换到另一个组合、或改时间范围时，标题已经是新的，
    // 汇总却还是上一次的，短暂显示一组不属于当前组合的数字。
    setData(null);
    try {
      setData(await logApi.comboLogs(name, {days: Number(days) || 7, limit: LOG_LIMIT}));
    } catch (e) {
      // 读失败就清空：对着上一次的旧数据看「成功率」比空着更误导。
      setData(null);
      setError(errText(e));
    } finally {
      setLoading(false);
    }
  }, [name, days]);

  useEffect(() => {
    if (!open) return;
    void load();
  }, [open, load]);

  /**
   * 导出当前时间范围的**全部**记录（不受列表 100 条限制）。
   *
   * 后端有行数上限，超出时只给最近的一段并用响应头说明；这里必须把
   * 「没导全」如实说出来，否则用户会拿一份不完整的数据做统计。
   */
  const exportLogs = useCallback(async () => {
    if (!name || exporting) return;
    setExporting(true);
    try {
      const out = await logApi.comboLogsExport(name, {days: Number(days) || 7});
      const url = URL.createObjectURL(out.blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = out.filename;
      link.click();
      URL.revokeObjectURL(url);
      if (out.truncated) {
        notify.info(t('combos.logExportTruncated', {
          rows: fmtNumber(out.rows), total: fmtNumber(out.total),
        }));
      } else {
        notify.ok(t('combos.logExportDone', {rows: fmtNumber(out.rows)}));
      }
    } catch (e) {
      notify.err(t('combos.logExportFail'), errText(e));
    } finally {
      setExporting(false);
    }
  }, [name, days, exporting, t]);

  const comboId = `combo/${name}`;
  const items = data?.items ?? [];
  const summary = data?.summary;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-[960px]" showCloseButton>
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <ScrollText className="h-4 w-4 text-muted-foreground" />
            {t('combos.logTitle', {name})}
          </DialogTitle>
          <DialogDescription>
            {t('combos.logDesc', {name})}
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="max-h-[min(680px,calc(100dvh-12rem))]">
          <div className="space-y-3 px-6 pb-4">
            <div className="flex flex-wrap items-end justify-between gap-2">
              <div className="flex items-end gap-2">
                <Select value={days} onValueChange={setDays}>
                  <SelectTrigger
                    size="sm"
                    className="w-[150px] bg-background"
                    aria-label={t('logs.filterRange')}
                  >
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="1">{t('logs.last24h')}</SelectItem>
                    <SelectItem value="7">{t('stats.last7')}</SelectItem>
                    <SelectItem value="30">{t('stats.last30')}</SelectItem>
                  </SelectContent>
                </Select>
                <Button
                  variant="outline"
                  size="sm"
                  className="rounded-full"
                  disabled={loading}
                  onClick={() => void load()}
                >
                  <RefreshCw className={loading ? 'animate-spin' : undefined} />
                  {t('common.refresh')}
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  className="rounded-full"
                  disabled={loading || exporting || !summary || summary.total <= 0}
                  onClick={() => void exportLogs()}
                >
                  <Download className={exporting ? 'animate-pulse' : undefined} />
                  {t('combos.logExport')}
                </Button>
              </div>
              {summary && (
                <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-muted-foreground">
                  <Stat label={t('combos.logRequests')} value={fmtNumber(summary.total)} />
                  <Stat
                    label={t('combos.logSuccessRate')}
                    value={summary.success_rate == null
                      ? '—'
                      : `${Math.round(summary.success_rate * 100)}%`}
                    tone={summary.success_rate != null && summary.success_rate < 0.9
                      ? 'warn'
                      : 'ok'}
                  />
                  <Stat
                    label={t('combos.logAvgLatency')}
                    value={summary.avg_latency_ms == null
                      ? '—'
                      : fmtLatency(summary.avg_latency_ms)}
                  />
                  <Stat label={t('combos.logTokens')} value={fmtNumber(summary.total_tokens)} />
                  <Stat label={t('metric.paid')} value={fmtCredit(summary.total_credit)} />
                </div>
              )}
            </div>

            {loading && (
              <div className="space-y-2">
                <SkeletonBar className="h-9 w-full rounded-xl" />
                <SkeletonBar className="h-9 w-full rounded-xl" />
                <SkeletonBar className="h-9 w-full rounded-xl" />
              </div>
            )}

            {!loading && error && (
              <LoadError variant="inline" message={error} onRetry={load} />
            )}

            {!loading && !error && data && (
              <div className="overflow-x-auto rounded-[14px] border border-border/60">
                <Table>
                  <TableHeader>
                    <TableRow className="hover:bg-transparent">
                      <TableHead className="pl-3 text-[11px] text-muted-foreground">
                        {t('combos.logColTime')}
                      </TableHead>
                      <TableHead className="text-[11px] text-muted-foreground">
                        {t('combos.logColMember')}
                      </TableHead>
                      <TableHead className="text-[11px] text-muted-foreground">
                        {t('combos.logColStatus')}
                      </TableHead>
                      <TableHead className="text-[11px] text-muted-foreground">
                        {t('combos.logTokens')}
                      </TableHead>
                      <TableHead className="text-[11px] text-muted-foreground">
                        {t('combos.logColLatency')}
                      </TableHead>
                      <TableHead className="pr-3 text-[11px] text-muted-foreground">
                        {t('combos.logColError')}
                      </TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {items.map((log) => (
                      <Row key={log.id} log={log} comboId={comboId} />
                    ))}
                  </TableBody>
                </Table>
                {!items.length && (
                  <EmptyState
                    icon={ScrollText}
                    title={t('combos.logEmpty')}
                    className="flex flex-col items-center justify-center px-4 py-10 text-center"
                  />
                )}
              </div>
            )}

            {!loading && !error && data && data.total > items.length && (
              <p className="text-[11px] text-muted-foreground">
                {t('combos.logTruncated', {shown: fmtNumber(items.length), total: fmtNumber(data.total)})}
              </p>
            )}
          </div>
        </DialogBody>
      </DialogContent>
    </Dialog>
  );
}

function Stat({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: 'ok' | 'warn';
}) {
  return (
    <span className="inline-flex items-baseline gap-1">
      <span>{label}</span>
      <span
        className={
          'font-mono text-xs tabular-nums ' +
          (tone === 'warn'
            ? 'text-amber-600 dark:text-amber-400'
            : tone === 'ok'
              ? 'text-emerald-600 dark:text-emerald-400'
              : 'text-foreground')
        }
      >
        {value}
      </span>
    </span>
  );
}

function Row({log, comboId}: {log: RequestLog; comboId: string}) {
  const t = useT();
  // 成员全失败时 `mapped_model` 仍是组合名本身——那不是「实际成员」。
  const member = log.mapped_model && log.mapped_model !== comboId
    ? log.mapped_model
    : '';
  const realm = log.realm === 'cn' || log.realm === 'global' ? log.realm : '';
  const tokens = log.prompt_tokens + log.completion_tokens;

  return (
    <TableRow className="border-b border-border/40">
      <TableCell className="whitespace-nowrap pl-3 text-xs text-muted-foreground">
        {fmtDateTimeMarked(log.ts)}
      </TableCell>
      <TableCell className="text-xs">
        <span className="inline-flex flex-wrap items-center gap-1">
          <span className="font-mono">{member || '—'}</span>
          {member && realm ? (
            <span className="rounded-full border border-border/70 px-1.5 py-0.5 text-[10px] text-muted-foreground">
              {t(`realm.${realm}`)}
            </span>
          ) : null}
        </span>
      </TableCell>
      <TableCell>
        {log.status >= 200 && log.status < 300 ? (
          <Badge variant="secondary" className="rounded-full text-emerald-600 dark:text-emerald-400">
            {log.status}
          </Badge>
        ) : log.status >= 400 && log.status < 500 ? (
          <Badge variant="secondary" className="rounded-full bg-amber-500/12 text-amber-600 dark:text-amber-400">
            {log.status || 'ERR'}
          </Badge>
        ) : (
          <Badge variant="destructive" className="rounded-full">{log.status || 'ERR'}</Badge>
        )}
      </TableCell>
      <TableCell className="text-xs tabular-nums">
        {tokens > 0 ? fmtNumber(tokens) : <span className="text-muted-foreground/60">—</span>}
      </TableCell>
      <TableCell className="whitespace-nowrap text-xs tabular-nums text-muted-foreground">
        {fmtLatency(log.latency_ms)}
      </TableCell>
      <TableCell className="max-w-[280px] pr-3 text-xs text-muted-foreground">
        {log.error ? (
          <span className="line-clamp-2 break-all" title={log.error}>{log.error}</span>
        ) : (
          <span className="text-muted-foreground/50">—</span>
        )}
      </TableCell>
    </TableRow>
  );
}
