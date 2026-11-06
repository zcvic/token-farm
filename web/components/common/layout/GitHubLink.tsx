'use client';

import {IconBrandGithub} from '@tabler/icons-react';
import Link from 'next/link';

import {useT} from '@/lib/i18n/provider';
import {REPO_URL} from '@/lib/repo';
import {cn} from '@/lib/utils';

/**
 * 顶部 GitHub 图标入口。
 *
 * 与语言 / 平台 / 版本切换同排（页面右上角），但只放图标：那几项是「状态」，
 * 它是一条「出口」，不占文字宽度才不会把窄屏的控件排挤到换行。
 */
export function GitHubLink({className}: {className?: string}) {
  const t = useT();
  const label = t('common.githubTitle');

  return (
    <Link
      href={REPO_URL}
      target="_blank"
      rel="noopener noreferrer"
      aria-label={label}
      title={label}
      className={cn(
        'inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-full border border-border/60 bg-muted/60 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground',
        className,
      )}
    >
      <IconBrandGithub className="h-3.5 w-3.5 shrink-0" />
    </Link>
  );
}
