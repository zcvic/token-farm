import type {Metadata} from 'next';
import {Inter, Noto_Sans_SC} from 'next/font/google';
import {Toaster} from '@/components/ui/sonner';
import {ThemeProvider} from '@/components/common/layout/ThemeProvider';
import {AuthProvider} from '@/lib/auth-context';
import {I18nProvider} from '@/lib/i18n/provider';
import {withBasePath} from '@/lib/base-path';
import './globals.css';

const inter = Inter({
  variable: '--font-inter',
  subsets: ['latin'],
  display: 'swap',
});

// Noto Sans SC 是**可变字体**：不写 `weight` 时 Next 只声明一条
// `font-weight: 100 900`，浏览器按需插值出 300/400/500/600/700 全部字重。
//
// 之前写了 `weight: ['300','400','500','600','700']`，Next 会为**每个字重**
// 各生成 101 条 @font-face —— 共 506 条，而其中 5 个字重引用的是**同一批
// woff2 文件**（实测：101 段 unicode-range 全部「5 个字重 → 同一个文件」，
// 0 段指向不同文件）。所以那 4 份重复声明既不增加可用的字形，也不减少下载量，
// 只是把 CSS 从约 93KB 撑到 464KB（gzip 31KB → 158KB），首屏白白多传 127KB。
// 去掉之后字体渲染结果不变（同一份字体文件、同样的 unicode-range 切片）。
const notoSansSC = Noto_Sans_SC({
  variable: '--font-noto-sans-sc',
  subsets: ['latin'],
  display: 'swap',
});

export const metadata: Metadata = {
  title: {
    template: '%s - token-farm',
    default: 'token-farm',
  },
  /**
   * 站点描述用英文：它是构建期写进静态 HTML 的元数据，无法跟随运行时语言切换
   * （管理端本身不做语言路由，见 lib/i18n/config.ts 的说明）。选英文是因为
   * 分享卡片 / 搜索引擎抓取时它面向的受众最广；界面内的文案全部走 i18n。
   */
  description: 'token-farm - multi-platform account console and OpenAI-compatible gateway (WorkBuddy + Trae)',
  // 图标 / 清单的 URL 也要带部署前缀（PR #60 评审补漏）：Next 只给 `_next`
  // 静态资源与 next/link 跳转补 basePath，**不会**改写 metadata 里的这些字符串
  // ——实测子路径部署下产物里仍是 `href="/favicon/..."`，会被请求到域名根（通常是
  // 另一个站点）而 404，表现为「站点图标不见了、PWA 清单取不到」。根路径部署时
  // withBasePath 原样返回，行为不变。
  manifest: withBasePath('/favicon/site.webmanifest'),
  icons: {
    icon: [
      {url: withBasePath('/favicon/favicon-32x32.png'), sizes: '32x32', type: 'image/png'},
      {url: withBasePath('/favicon/favicon-16x16.png'), sizes: '16x16', type: 'image/png'},
    ],
    shortcut: withBasePath('/favicon/favicon.ico'),
    apple: withBasePath('/favicon/apple-touch-icon.png'),
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="zh-CN"
      className={`${inter.variable} ${notoSansSC.variable} hide-scrollbar font-sans`}
      suppressHydrationWarning
    >
      <body
        className={`${inter.variable} ${notoSansSC.variable} hide-scrollbar font-sans antialiased`}
      >
        <ThemeProvider
          attribute="class"
          defaultTheme="system"
          enableSystem
          disableTransitionOnChange
        >
          <I18nProvider>
            <AuthProvider>
              {children}
              <Toaster />
            </AuthProvider>
          </I18nProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
