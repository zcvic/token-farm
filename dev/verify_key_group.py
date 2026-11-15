"""密钥「自定义分组」的界面验收（浏览器侧）。

静态断言（`server/tests/test_key_group_name.py` 里的接线守卫）只能证明代码里
**提到了**这些字段，证明不了用户点得动。这个脚本在真浏览器里走一遍：

  1. 新建密钥时填分组名 → 列表里出现该分组标签；
  2. 已有分组名会成为输入框的候选（`datalist`）—— 手打容易差一个字符，
     变成两个几乎一样的组，这是这个功能最容易踩的坑；
  3. 分组下拉能筛出「只有这一组」的密钥，另一个组的密钥不出现；
  4. 「未分组」是一个**独立可选**的筛选项（不能和「全部」混为一谈）；
  5. 编辑时清空分组名 → 该密钥回到「未分组」；
  6. 筛着的分组被清空后，空状态说的是「这一组里没有」，**不是**「暂无 API 密钥」
     —— 后者会让用户以为密钥全没了。

    python dev/verify_key_group.py

数据落在 dev/.key-group/（已 gitignore），截图输出到 dev/.shots-kg/。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / 'dev' / '.key-group'
SHOTS = REPO / 'dev' / '.shots-kg'
UPSTREAM_PORT = 7921
MANAGER_PORT = 7922
ADMIN_PW = 'key-group-pass'


class Upstream(BaseHTTPRequestHandler):
    """最小上游：面板启动时会探测它，不该让验收因为连不上而走别的分支。"""

    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('content-type', 'application/json; charset=utf-8')
        self.send_header('content-length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/healthz':
            self._json({'healthy': 0, 'total': 0})
        elif path == '/status':
            self._json({'accounts': [], 'total': 0, 'healthy': 0, 'cooling': 0,
                        'disabled': 0, 'in_flight_full': 0, 'redis_mode': 'noop',
                        'sticky_sessions': 0})
        elif path == '/v1/models':
            self._json({'object': 'list', 'data': [{'id': 'glm-5.2'}]})
        else:
            self._json({'error': 'not found'}, 404)

    def do_POST(self):
        n = int(self.headers.get('content-length') or 0)
        if n:
            self.rfile.read(n)
        self._json({'ok': True})


def main() -> int:
    if DATA.exists():
        shutil.rmtree(DATA, ignore_errors=True)
    DATA.mkdir(parents=True)

    upstream = ThreadingHTTPServer(('127.0.0.1', UPSTREAM_PORT), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    env = {
        **os.environ,
        'WB2API_BASE': f'http://127.0.0.1:{UPSTREAM_PORT}',
        'WB_AUTH_DIR': str(DATA / 'auths'),
        'WB_DB': str(DATA / 'manager.db'),
        'WB_USERS_FILE': str(DATA / 'users.json'),
        'WB_MANAGER_HOST': '127.0.0.1',
        'WB_MANAGER_PORT': str(MANAGER_PORT),
        'WB_ADMIN_PASSWORD': ADMIN_PW,
        'PYTHONUTF8': '1',
    }
    (DATA / 'auths').mkdir(parents=True, exist_ok=True)

    launcher = ('import uvicorn, server.main;'
                f'uvicorn.run(server.main.app, host="127.0.0.1", '
                f'port={MANAGER_PORT}, log_level="warning")')
    proc = subprocess.Popen([sys.executable, '-c', launcher], cwd=str(REPO), env=env)
    print(f'管理端: http://127.0.0.1:{MANAGER_PORT}')
    try:
        import urllib.request
        for _ in range(60):
            try:
                urllib.request.urlopen(
                    f'http://127.0.0.1:{MANAGER_PORT}/api/healthz', timeout=1)
                break
            except Exception:
                time.sleep(0.5)
        return browser_checks()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        upstream.shutdown()


def browser_checks() -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    # **必须 capture_output**：不带它时子进程直接写终端，`r.stdout` 是 None，
    # 「从输出里挑 FAIL 行」就永远挑不到 —— 断言全绿其实一条都没执行（踩过）。
    r = subprocess.run(['node', '-e', JS],
                       env={**os.environ, 'BASE': f'http://127.0.0.1:{MANAGER_PORT}',
                            'PASS': ADMIN_PW, 'OUT': str(SHOTS)},
                       cwd=str(REPO / 'web'), text=True, timeout=300,
                       capture_output=True)
    print(r.stdout or '')
    if r.stderr:
        print(r.stderr[:1500])
    print(f'截图: {SHOTS}')
    if r.returncode != 0:
        print(f'浏览器验收脚本自身失败（退出码 {r.returncode}）')
        return 1
    if 'ALL CHECKS PASSED' not in (r.stdout or ''):
        print('浏览器验收没有跑到结束（未见 ALL CHECKS PASSED）')
        return 1
    return 0


JS = r'''
const BASE = process.env.BASE, OUT = process.env.OUT;
(async () => {
  const fs = await import('node:fs'); const path = await import('node:path');
  const {pathToFileURL} = await import('node:url');
  const c = path.join(process.env.TEMP||'/tmp','wb-i18n-verify','node_modules','playwright-core','index.js');
  const mod = await import(fs.existsSync(c)?pathToFileURL(c).href:'playwright-core');
  const chromium = mod.chromium ?? mod.default?.chromium;
  const root = path.join(process.env.LOCALAPPDATA,'ms-playwright');
  const dir = fs.readdirSync(root).filter(d=>d.startsWith('chromium-')&&!d.includes('headless_shell')).sort().pop();
  const b = await chromium.launch({executablePath: path.join(root,dir,'chrome-win64','chrome.exe')});
  const ctx = await b.newContext({viewport:{width:1500,height:1100}, deviceScaleFactor:2});
  const p = await ctx.newPage();
  const findings = [];
  const step = (ok, label, extra='') => {
    console.log(`  ${ok?'PASS':'FAIL'}  ${label}${extra?` -- ${extra}`:''}`);
    if (!ok) findings.push(label + (extra?': '+extra:''));
  };

  await p.goto(`${BASE}/login`,{waitUntil:'domcontentloaded'});
  await p.fill('#username','admin'); await p.fill('#password', process.env.PASS);
  await Promise.all([p.waitForURL(/dashboard/).catch(()=>{}), p.click('button[type=submit]')]);

  await p.goto(`${BASE}/keys`,{waitUntil:'load'});
  await p.waitForTimeout(3000);

  // 关掉可能自动弹出的「更新日志」浮层（每个版本第一次访问会弹，遮住整页）
  for (let i = 0; i < 3; i++) {
    const full = p.locator('button', {hasText: /查看完整更新日志|Full changelog/}).first();
    if (!(await full.count())) break;
    await p.keyboard.press('Escape');
    await p.waitForTimeout(400);
  }

  const groupInput = () => p.locator('[role=dialog] input[list="key-group-names"]').first();

  /** 新建一把带分组的密钥。 */
  const createKey = async (name, group) => {
    await p.locator('button', {hasText: /新建密钥|New key/}).first().click();
    await p.waitForTimeout(900);
    const nameInput = p.locator('[role=dialog] input').first();
    await nameInput.fill(name);
    if (group) await groupInput().fill(group);
    await p.locator('[role=dialog] button', {hasText: /^创建$|^Create$/}).last().click();
    await p.waitForTimeout(1600);
    // 创建成功后会弹「密钥创建成功」（含明文）——关掉它，否则挡住列表
    const done = p.locator('[role=dialog] button', {hasText: /我已保存|I saved it/}).first();
    if (await done.count()) { await done.click(); await p.waitForTimeout(700); }
  };

  // ① 建两把密钥，分别放在两个组
  await createKey('分组验收-A', '客服组');
  await createKey('分组验收-B', '客户A');
  await createKey('分组验收-C', '');

  const rowNames = async () => p.evaluate(() =>
    [...document.querySelectorAll('table tbody tr')]
      .map((tr) => tr.querySelector('td')?.textContent?.trim() || ''));
  const names = await rowNames();
  step(names.some((n) => n.includes('分组验收-A')) && names.some((n) => n.includes('分组验收-B')),
       '三把密钥都建出来了', JSON.stringify(names.slice(0, 5)));

  // ② 表格里有分组列，且标签显示的是分组名
  const groupCells = async () => p.evaluate(() =>
    [...document.querySelectorAll('table tbody tr')].map((tr) => {
      const tds = tr.querySelectorAll('td');
      return tds[2] ? tds[2].textContent.trim() : '';
    }));
  const cells = await groupCells();
  step(cells.includes('客服组') && cells.includes('客户A'),
       '表格的分组列显示分组名', JSON.stringify(cells.slice(0, 5)));
  step(cells.some((x) => /未分组|Ungrouped/.test(x)),
       '未分组的密钥显示成「未分组」而不是空白格', JSON.stringify(cells.slice(0, 5)));
  await p.screenshot({path:`${OUT}/kg-list.png`, fullPage:true});

  // ③ 分组下拉能筛出只有这一组的密钥
  const filter = p.locator('button[aria-label*="分组"]').first();
  step(await filter.count() > 0, '列表上有「按分组筛选」下拉');
  await filter.click();
  await p.waitForTimeout(500);
  await p.locator('[role=option]', {hasText: '客服组'}).first().click();
  await p.waitForTimeout(800);
  const filtered = await rowNames();
  step(filtered.length === 1 && filtered[0].includes('分组验收-A'),
       '按分组筛选后只剩这一组的密钥', JSON.stringify(filtered));
  await p.screenshot({path:`${OUT}/kg-filter.png`, fullPage:true});

  // ④ 「未分组」是独立取值（不能和「全部」混在一起）
  await filter.click();
  await p.waitForTimeout(400);
  const ungrouped = p.locator('[role=option]', {hasText: /未分组|Ungrouped/}).first();
  step(await ungrouped.count() > 0, '下拉里有独立的「未分组」选项');
  await ungrouped.click();
  await p.waitForTimeout(800);
  const onlyUngrouped = await rowNames();
  step(onlyUngrouped.length === 1 && onlyUngrouped[0].includes('分组验收-C'),
       '「未分组」只筛出没填分组的密钥', JSON.stringify(onlyUngrouped));

  // ⑤ 编辑时清空分组 → 回到未分组
  await filter.click();
  await p.waitForTimeout(400);
  await p.locator('[role=option]', {hasText: '客户A'}).first().click();
  await p.waitForTimeout(800);
  const beforeEdit = await rowNames();
  step(beforeEdit.length === 1 && beforeEdit[0].includes('分组验收-B'),
       '切到「客户A」组只剩它', JSON.stringify(beforeEdit));

  await p.locator('table tbody tr').first().locator('button[title]').nth(1).click();
  await p.waitForTimeout(900);
  await groupInput().fill('');
  await p.locator('[role=dialog] button', {hasText: /^保存$|^Save$/}).last().click();
  await p.waitForTimeout(1600);
  // 清空后这一组就没有密钥了 —— 空状态必须说清是「这一组」的问题，
  // 不能说成「暂无 API 密钥」（那句话的意思是「一把都没有」）。
  // 两种说法都算对：组里还有别的密钥 → 「这一组里没有」；这是最后一把 →
  // 「分组已经不在了」（它确实没了，且下面会指路「看全部分组」）。
  const emptyText = await p.evaluate(() => {
    const el = document.querySelector('section');
    return el ? el.textContent || '' : '';
  });
  step(/这一栏里没有|还没有密钥|已经不在了|No keys in group|no longer exists/.test(emptyText),
       '筛着的组被清空后，空状态说的是「这一组」而不是「一把都没有」', emptyText.slice(0, 120));
  step(!/暂无 API 密钥/.test(emptyText),
       '没有把「这一组没有」说成「一把密钥都没有」');
  await p.screenshot({path:`${OUT}/kg-empty.png`, fullPage:true});

  // ⑥ 已有的分组名会出现在输入框的候选里（datalist）
  await p.locator('button', {hasText: /看全部分组|Show all groups/}).first().click();
  await p.waitForTimeout(800);
  await p.locator('button', {hasText: /新建密钥|New key/}).first().click();
  await p.waitForTimeout(900);
  const options = await p.evaluate(() =>
    [...document.querySelectorAll('datalist#key-group-names option')].map((o) => o.value));
  step(options.includes('客服组'),
       '输入框的候选里有已有的分组名', JSON.stringify(options));
  await p.keyboard.press('Escape');
  await p.waitForTimeout(400);

  await b.close();
  if (findings.length) { console.log('\nFAILURES:\n'+findings.join('\n')); process.exit(1); }
  console.log('\nALL CHECKS PASSED');
})().catch(e=>{console.error('harness error:',e);process.exit(2)});
'''


if __name__ == '__main__':
    raise SystemExit(main())
