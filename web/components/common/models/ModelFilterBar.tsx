'use client';

import {Layers, Search} from 'lucide-react';

import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {useT} from '@/lib/i18n/provider';
import {seriesLabel} from '@/lib/model-series';

/** 能力筛选：全部 / 支持推理 / 大上下文 / 多模态 */
export type ModelCapability = 'all' | 'reasoning' | 'large' | 'vision';

/** 排序：默认按原顺序；「积分倍率从低到高」用于挑省积分的模型。 */
export type ModelSort = 'default' | 'credits';

/**
 * 模型页的搜索与筛选条（两个模型页共用）。
 *
 * 两张表的数据来源不同（WorkBuddy 目录 / 平台适配器），但「搜什么、怎么筛」
 * 是同一套浏览动作；共用一个组件才能保证切平台后操作位置和文案都不变。
 */
export function ModelFilterBar({
  q,
  onQChange,
  series,
  seriesOptions,
  onSeriesChange,
  cap,
  onCapChange,
  sort,
  onSortChange,
  /** 目录里没有任何积分倍率时不必显示排序入口（点了也不会变化）。 */
  showCreditSort = true,
}: {
  q: string;
  onQChange: (value: string) => void;
  series: string;
  seriesOptions: readonly string[];
  onSeriesChange: (value: string) => void;
  cap: ModelCapability;
  onCapChange: (value: ModelCapability) => void;
  sort: ModelSort;
  onSortChange: (value: ModelSort) => void;
  showCreditSort?: boolean;
}) {
  const t = useT();

  return (
    <div className="flex flex-col gap-3 lg:flex-row lg:items-center">
      <div className="relative flex-1">
        <Search className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
        <Input
          value={q}
          onChange={(e) => onQChange(e.target.value)}
          placeholder={t('models.searchPlaceholder')}
          className="h-9 bg-background pl-8"
        />
      </div>
      <div className="-mx-0.5 flex flex-wrap items-center gap-1.5 overflow-x-auto px-0.5 pb-0.5">
        <span className="flex shrink-0 items-center gap-1 text-[11px] text-muted-foreground">
          <Layers className="h-3.5 w-3.5" />
          {t('models.series')}
        </span>
        <Button
          variant={series === 'all' ? 'default' : 'outline'}
          size="sm"
          className="h-7 shrink-0 rounded-full px-2.5 text-[11px]"
          onClick={() => onSeriesChange('all')}
        >
          {t('common.all')}
        </Button>
        {seriesOptions.map((s) => (
          <Button
            key={s}
            variant={series === s ? 'default' : 'outline'}
            size="sm"
            className="h-7 shrink-0 rounded-full px-2.5 text-[11px]"
            onClick={() => onSeriesChange(s)}
          >
            {seriesLabel(s, t)}
          </Button>
        ))}
        <span className="ml-1 h-4 w-px shrink-0 bg-border" />
        {(
          [
            ['all', t('common.all')],
            ['reasoning', t('models.reasoning')],
            ['large', t('models.largeContext')],
            ['vision', t('models.vision')],
          ] as const
        ).map(([k, label]) => (
          <Button
            key={k}
            variant={cap === k ? 'default' : 'outline'}
            size="sm"
            className="h-7 shrink-0 rounded-full px-2.5 text-[11px]"
            onClick={() => onCapChange(k)}
          >
            {label}
          </Button>
        ))}
        {/* 排序：挑省积分模型时最实用的一项 —— 单看倍率没概念，排一下才清楚 */}
        {showCreditSort && (
          <>
            <span className="ml-1 h-4 w-px shrink-0 bg-border" />
            <Button
              variant={sort === 'credits' ? 'default' : 'outline'}
              size="sm"
              className="h-7 shrink-0 rounded-full px-2.5 text-[11px]"
              title={t('models.sortByRatioTitle')}
              onClick={() => onSortChange(sort === 'credits' ? 'default' : 'credits')}
            >
              {t('models.sortByRatio')}
            </Button>
          </>
        )}
      </div>
    </div>
  );
}
