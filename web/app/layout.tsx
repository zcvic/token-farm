import type {Metadata} from 'next';
import localFont from 'next/font/local';
import {Toaster} from '@/components/ui/sonner';
import {ThemeProvider} from '@/components/common/layout/ThemeProvider';
import {AuthProvider} from '@/lib/auth-context';
import {I18nProvider} from '@/lib/i18n/provider';
import {withBasePath} from '@/lib/base-path';
import './globals.css';

// 字体改为随仓库分发的本地文件（`next/font/local`）：原来用 `next/font/google`
// 时，构建期必须联网访问 fonts.googleapis.com 拉 CSS 与 woff2。GitHub Actions、
// 国内 Docker 构建经常连不上（next/font 只报一句含糊的 TypeError），整个
// `next build` 直接失败、镜像建不出来。两份字体与原先 Google 版本同源、同 latin
// 子集，外观不变，但构建不再依赖外网。
const inter = localFont({
  src: './fonts/Inter-variable.woff2',
  variable: '--font-inter',
  display: 'swap',
  weight: '100 900',
});

// Noto Sans SC 是**可变字体**：用 `weight` 区间只声明一条
// `font-weight: 300 700`，浏览器按需插值出 300/400/500/600/700 全部字重。
//
// 早先写过 `weight: ['300','400','500','600','700']`，Next 会为**每个字重**
// 各生成 101 条 @font-face —— 共 506 条，而其中 5 个字重引用的是**同一批
// woff2 文件**（实测：101 段 unicode-range 全部「5 个字重 → 同一个文件」，
// 0 段指向不同文件）。所以那 4 份重复声明既不增加可用的字形，也不减少下载量，
// 只是把 CSS 从约 93KB 撑到 464KB（gzip 31KB → 158KB），首屏白白多传 127KB。
// 区间写法与之前的渲染结果一致（同一份字体文件、同样的 unicode-range 切片）。
const notoSansSC = localFont({
  src: './fonts/NotoSansSC-variable.woff2',
  variable: '--font-noto-sans-sc',
  display: 'swap',
  weight: '300 700',
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
  description: 'token-farm - multi-platform account console and OpenAI-compatible gateway (WorkBuddy, Trae, Qoder, Loomy, DuMate, MiniMax Code)',
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
