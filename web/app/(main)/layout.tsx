'use client';

import {memo, useEffect} from 'react';
import {useRouter} from 'next/navigation';
import {ManagementBar} from '@/components/common/layout/ManagementBar';
import {CommandPalette} from '@/components/common/layout/CommandPalette';
import {WhatsNewDialog} from '@/components/common/layout/WhatsNewDialog';
import {LanguageToggle} from '@/components/common/layout/LanguageToggle';
import {GitHubLink} from '@/components/common/layout/GitHubLink';
import {PlatformScopeNote, PlatformToggle} from '@/components/common/layout/PlatformToggle';
import {RealmToggle} from '@/components/common/layout/RealmToggle';
import {PlatformProvider} from '@/lib/platform-context';
import {RealmProvider} from '@/lib/realm-context';
import {useAuth} from '@/lib/auth-context';

const MemoizedManagementBar = memo(ManagementBar);

export default function MainLayout({
  children,
}: {
  children: React.ReactNode
}) {
  const {me, loading} = useAuth();
  const router = useRouter();

  // 仅在确认未登录时跳转；不阻塞内容渲染，避免每次切页闪一下
  useEffect(() => {
    if (!loading && !me) router.replace('/login');
  }, [loading, me, router]);

  return (
    <PlatformProvider>
      <RealmProvider>
        <div className="min-h-screen flex flex-col">
          <MemoizedManagementBar />
          <div className="flex flex-1 flex-col">
            <div className="@container/main flex flex-1 flex-col gap-2">
              <div className="flex min-h-0 flex-1 flex-col px-4 pt-12 py-8 sm:px-6 md:px-8 lg:px-12">
                <div className="mx-auto flex min-h-0 w-full max-w-7xl flex-1 flex-col gap-4 pb-24 md:gap-6">
                  {/*
                    平台切换与版本切换都固定在右上角：底栏是照 LDC 原样保留的，
                    控件不往那里加。移动端只显示图标（compact），避免窄屏被它占掉一行。

                    命令面板（⌘K）挂在同一行，且放在最左：它是**入口**，语言 / 平台 /
                    版本是**状态**，入口排在状态前面才符合扫读顺序。放在布局里而不是
                    某一页里，是因为它必须在每一页都能按得出来。

                    GitHub 图标放在最右（真正意义上的右上角）：它指向项目仓库，是
                    一条外链而不是状态，和左侧那几个切换控件分开才不会误读成开关。
                  */}
                  <div className="flex flex-wrap items-center justify-end gap-2">
                    <CommandPalette />
                    <LanguageToggle />
                    <PlatformToggle />
                    <RealmToggle />
                    <GitHubLink />
                  </div>
                  {/* 非 WorkBuddy 时提示「哪些页面会跟着平台切换」：只在全局页出现，
                      账号 / 模型页自己已经换成平台数据。 */}
                  <PlatformScopeNote />
                  {children}
                </div>
              </div>
            </div>
          </div>
        </div>
        {/* 升级后的「本次更新」公告：每个版本只弹一次（按 localStorage 记录的版本号）。 */}
        <WhatsNewDialog />
      </RealmProvider>
    </PlatformProvider>
  );
}
