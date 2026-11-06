/**
 * 模型「系列」的推导与展示。
 *
 * 两个模型页（WorkBuddy 模型中心、平台模型页）都要按系列分组浏览，但数据来源
 * 不同：WorkBuddy 的系列由后端 `modelcatalog.series_of` 按 id 前缀推导后随目录
 * 下发；平台适配器（Trae）的目录里没有这个字段。为了不让两个页面出现「同一家
 * 厂商在两个页签下分成两个系列」，规则表与配色集中在这里，后端那份是同一批
 * 前缀的镜像（`server/services/modelcatalog.py: _SERIES_RULES`）。
 *
 * 这里只做**命名约定推导**：认不出的归入「其他」，不做猜测性归类。界面会标明
 * 系列仅用于分组浏览。
 */

/**
 * 系列标签配色（按系列名稳定取色，认不出的用中性色）。
 * 键是推导出的系列名，属固定的闭集——新增系列时在这里补一条即可。
 */
export const SERIES_STYLE: Record<string, string> = {
  '智谱 GLM': 'border-sky-500/30 bg-sky-500/10 text-sky-700 dark:text-sky-400',
  DeepSeek: 'border-indigo-500/30 bg-indigo-500/10 text-indigo-700 dark:text-indigo-400',
  Kimi: 'border-violet-500/30 bg-violet-500/10 text-violet-700 dark:text-violet-400',
  MiniMax: 'border-rose-500/30 bg-rose-500/10 text-rose-700 dark:text-rose-400',
  Doubao: 'border-orange-500/30 bg-orange-500/10 text-orange-700 dark:text-orange-400',
  Qwen: 'border-teal-500/30 bg-teal-500/10 text-teal-700 dark:text-teal-400',
  Claude: 'border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-400',
  Gemini: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400',
  GPT: 'border-lime-500/30 bg-lime-500/10 text-lime-700 dark:text-lime-400',
  Grok: 'border-slate-500/30 bg-slate-500/10 text-slate-700 dark:text-slate-400',
  Step: 'border-fuchsia-500/30 bg-fuchsia-500/10 text-fuchsia-700 dark:text-fuchsia-400',
  MiMo: 'border-pink-500/30 bg-pink-500/10 text-pink-700 dark:text-pink-400',
  腾讯混元: 'border-cyan-500/30 bg-cyan-500/10 text-cyan-700 dark:text-cyan-400',
  自动选择: 'border-border bg-muted text-muted-foreground',
};

/**
 * 系列名本地化。
 *
 * 推导出来的系列名是**数据**不是文案，所以在这里做「数据值 → 译文」的映射；
 * 未收录的系列（DeepSeek、Kimi 等本来就是英文品牌名）原样显示。
 */
const SERIES_KEYS: Record<string, string> = {
  '智谱 GLM': 'models.seriesGlm',
  腾讯混元: 'models.seriesHunyuan',
  自动选择: 'models.seriesAuto',
  其他: 'models.seriesOther',
};

export function seriesLabel(series: string, t: (key: string) => string): string {
  const key = SERIES_KEYS[series];
  return key ? t(key) : series;
}

/**
 * id 前缀 → 系列。**顺序即优先级**（先匹配先赢），因此更长的专有前缀要排在
 * 更短的通用前缀之前（例如 `aws_claude` 必须先于 `claude` 之外的规则判断）。
 *
 * 规则表与后端 `_SERIES_RULES` 保持同源，平台侧在此之上补了 Trae 目录里的
 * 其它厂商；后端若新增系列，这里同步补一条即可。
 */
const SERIES_RULES: readonly (readonly [readonly string[], string])[] = [
  [['glm'], '智谱 GLM'],
  [['deepseek'], 'DeepSeek'],
  [['kimi', 'moonshot'], 'Kimi'],
  [['minimax'], 'MiniMax'],
  [['doubao', 'seed'], 'Doubao'],
  [['qwen'], 'Qwen'],
  [['aws_claude', 'claude'], 'Claude'],
  [['gemini'], 'Gemini'],
  [['gpt'], 'GPT'],
  [['grok'], 'Grok'],
  [['step'], 'Step'],
  [['mimo'], 'MiMo'],
  [['hy', 'hunyuan'], '腾讯混元'],
  [['auto'], '自动选择'],
];

/** 按 id 前缀推导系列名。认不出返回「其他」。大小写不敏感。 */
export function seriesOfModelId(modelId: string): string {
  const mid = (modelId || '').trim().toLowerCase();
  // 平台模型对外名带前缀（`trae/deepseek-v4.1-flash`），推导前先剥掉，
  // 否则每条都会落到「其他」。
  const bare = mid.includes('/') ? mid.slice(mid.indexOf('/') + 1) : mid;
  for (const [prefixes, label] of SERIES_RULES) {
    if (prefixes.some((prefix) => bare.startsWith(prefix))) return label;
  }
  return '其他';
}
