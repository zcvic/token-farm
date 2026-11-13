'use client';

import {useMemo} from 'react';
import {CheckCheck, X} from 'lucide-react';
import {Button} from '@/components/ui/button';
import {Badge} from '@/components/ui/badge';
import {useI18n} from '@/lib/i18n/provider';
import {taskName} from '@/lib/i18n/taskname';
import type {TaskRunCatalogItem} from '@/lib/types';

/**
 * 任务勾选列表（账号页弹窗 / 任务页面板共用）。
 *
 * 为什么需要：三种模式都是**整批**语义——一次跑脚本映射表里的全部任务。
 * 但真实需求常是「这个号只差夜猫」「只想补小程序那几项」，为一项把 30 多个
 * 动作全跑一遍既慢，又平白多出几十次写请求（风控面也更大）。
 *
 * 三条约定：
 *   · **空 = 全量**。不勾任何项就是以前的行为，上游新增任务也会跑到；
 *     这不是「什么都没选所以不执行」——那种语义会让老用户误以为功能坏了。
 *   · 清单来自后端解析上游脚本的 `MAPPING`（`/api/task-run/catalog`），
 *     不在这里硬编码：上游增删任务时列表自动跟上，这里只负责翻译与分组。
 *   · 不可伪造的任务（如真实捐款）禁用勾选：勾了也只会打一行 skip，
 *     不如直接标出来，免得用户以为「没勾到才没做」。
 */
const GROUPS = [
  {id: 'desktop', key: 'tasks.runPickGroupDesktop'},
  {id: 'mini', key: 'tasks.runPickGroupMini'},
  {id: 'expert', key: 'tasks.runPickGroupExpert'},
  {id: 'school', key: 'tasks.runPickGroupSchool'},
  {id: 'manual', key: 'tasks.runPickGroupManual'},
  {id: 'other', key: 'tasks.runPickGroupOther'},
] as const;

export function TaskPicker({
  catalog,
  value,
  onChange,
  disabled = false,
}: {
  catalog: TaskRunCatalogItem[];
  /** 已勾选的任务码；空数组 = 全量 */
  value: string[];
  onChange: (codes: string[]) => void;
  disabled?: boolean;
}) {
  const {t} = useI18n();
  const pickable = useMemo(() => catalog.filter((c) => !c.unforgeable), [catalog]);
  const byGroup = useMemo(() => {
    const map = new Map<string, TaskRunCatalogItem[]>();
    for (const item of catalog) {
      const key = GROUPS.some((g) => g.id === item.group) ? item.group : 'other';
      const list = map.get(key) || [];
      list.push(item);
      map.set(key, list);
    }
    return map;
  }, [catalog]);

  const toggle = (code: string) => {
    onChange(value.includes(code) ? value.filter((c) => c !== code) : [...value, code]);
  };

  const allPicked = pickable.length > 0 && pickable.every((c) => value.includes(c.code));

  return (
    <div className="space-y-2 rounded-xl border px-3 py-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-1.5">
          <span className="text-[12px] font-medium">{t('tasks.runPickTitle')}</span>
          <Badge variant="secondary" className="rounded-full text-[10px]">
            {value.length === 0
              ? t('tasks.runPickAllScope')
              : t('tasks.runPickCount', {n: value.length})}
          </Badge>
        </div>
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={disabled || pickable.length === 0}
          className="h-7 cursor-pointer gap-1 rounded-full text-[11px]"
          onClick={() => onChange(allPicked ? [] : pickable.map((c) => c.code))}
        >
          {allPicked ? <X className="size-3" /> : <CheckCheck className="size-3" />}
          {allPicked ? t('tasks.runPickNone') : t('tasks.runPickAll')}
        </Button>
      </div>

      <p className="text-[11px] leading-relaxed text-muted-foreground">
        {catalog.length === 0 ? t('tasks.runPickEmpty') : t('tasks.runPickHint')}
      </p>

      {catalog.length > 0 && (
        <div className="max-h-[190px] space-y-2 overflow-y-auto pr-1">
          {GROUPS.map((group) => {
            const items = byGroup.get(group.id);
            if (!items || items.length === 0) return null;
            return (
              <div key={group.id}>
                <div className="mb-1 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
                  {t(group.key)}
                </div>
                <div className="space-y-0.5">
                  {items.map((item) => (
                    <label
                      key={item.code}
                      className={
                        'flex items-center gap-2 rounded-md px-1.5 py-1 text-[12px] ' +
                        (item.unforgeable
                          ? 'cursor-not-allowed opacity-60'
                          : 'cursor-pointer hover:bg-muted')
                      }
                      title={item.code}
                    >
                      <input
                        type="checkbox"
                        disabled={disabled || item.unforgeable}
                        checked={value.includes(item.code)}
                        onChange={() => toggle(item.code)}
                      />
                      <span className="truncate">{taskName(item.code)}</span>
                      {item.unforgeable && (
                        <span className="shrink-0 text-[10px] text-muted-foreground">
                          {t('tasks.runPickManual')}
                        </span>
                      )}
                    </label>
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
