import {redirect} from 'next/navigation';

/**
 * 旧「多平台」页的墓碑。
 *
 * 平台切换已经并进顶部下拉，账号与模型页会跟着当前平台换数据源；原来这一页
 * 不再有独立入口。保留这个路由只为旧书签 / 深链不落到 404：直接送去账号页，
 * 用户在那里用顶部的平台下拉继续操作。
 */
export default function PlatformsPage() {
  redirect('/accounts');
}
