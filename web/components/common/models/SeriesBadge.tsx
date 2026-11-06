'use client';

import {SERIES_STYLE, seriesLabel} from '@/lib/model-series';
import {useT} from '@/lib/i18n/provider';
import {cn} from '@/lib/utils';

/**
 * 系列徽标：配色与推导规则集中在 `lib/model-series`，两个模型页共用一套。
 *
 * 放在共用组件里而不是各页各写一份：同一个系列在两张表里必须长得一样，
 * 否则「按系列浏览」在两个页签下会给出两套视觉规则。
 */
export function SeriesBadge({series}: {series: string}) {
  const t = useT();
  return (
    <span
      className={cn(
        'inline-flex shrink-0 items-center rounded-md border px-1.5 py-0.5 text-[10px] font-medium',
        SERIES_STYLE[series] ?? 'border-border bg-muted text-muted-foreground',
      )}
    >
      {seriesLabel(series, t)}
    </span>
  );
}
