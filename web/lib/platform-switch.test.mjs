/**
 * `web/lib/platform-switch.ts` 的行为测试（Node 直接跑 .ts 源码）。
 *
 * 跑法（Node ≥ 22.6）：
 *     node --experimental-strip-types web/lib/platform-switch.test.mjs
 * 或走 Python 包装：`python -m unittest server.tests.test_platform_switch`
 *
 * 为什么值得单独测：平台清单来自后端注册表，过滤写错**不会报错**——
 * Discovery / Kilo / Cline 又出现在下拉里，或者一个正常平台被误伤消失。
 */
import {
  filterSwitchablePlatforms,
  isSwitchHidden,
  SWITCH_HIDDEN_PLATFORMS,
} from './platform-switch.ts';

let failed = 0;

function check(name, got, want) {
  const g = JSON.stringify(got);
  const w = JSON.stringify(want);
  if (g === w) {
    console.log(`  ok  ${name}`);
  } else {
    console.log(`  FAIL ${name}\n       got  ${g}\n       want ${w}`);
    failed += 1;
  }
}

/* ── 单个平台判定 ─────────────────────────────────────────────── */

check('Discovery 被隐藏', isSwitchHidden('discovery'), true);
check('Kilo 被隐藏', isSwitchHidden('kilo'), true);
check('Cline 被隐藏', isSwitchHidden('cline'), true);
check('大小写与空白不影响', isSwitchHidden('  Kilo '), true);
check('WorkBuddy 不隐藏', isSwitchHidden('workbuddy'), false);
check('Trae 不隐藏', isSwitchHidden('trae'), false);
check('Loomy 不隐藏', isSwitchHidden('loomy'), false);
check('DuMate 不隐藏', isSwitchHidden('dumate'), false);
check('MiniMax 不隐藏', isSwitchHidden('minimax'), false);
check('Qoder 不隐藏', isSwitchHidden('qoder'), false);
check('空值不隐藏', isSwitchHidden(undefined), false);
check('隐藏集合只包含三者', [...SWITCH_HIDDEN_PLATFORMS],
  ['discovery', 'kilo', 'cline']);

/* ── 清单过滤 ─────────────────────────────────────────────────── */

const platforms = [
  {name: 'trae', label: 'Trae'},
  {name: 'discovery', label: 'Discovery'},
  {name: 'loomy', label: 'Loomy'},
  {name: 'kilo', label: 'Kilo'},
  {name: 'cline', label: 'Cline'},
  {name: 'dumate', label: 'DuMate'},
];
const names = (rows) => rows.map((row) => row.name);

check('过滤后只剩可见平台',
  names(filterSwitchablePlatforms(platforms)),
  ['trae', 'loomy', 'dumate']);
check('保持后端给出的顺序',
  names(filterSwitchablePlatforms([{name: 'dumate'}, {name: 'trae'}, {name: 'kilo'}])),
  ['dumate', 'trae']);

const original = ['discovery', 'trae'];
const copy = [...original];
filterSwitchablePlatforms(original.map((name) => ({name})));
check('不修改传入数组', original, copy);
check('空清单返回空清单', filterSwitchablePlatforms([]), []);

if (failed) {
  console.log(`\n${failed} check(s) failed`);
  process.exit(1);
}
console.log('\nall passed');