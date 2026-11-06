"""把 release.yml 的「组装发布目录」步骤**真跑一遍**。

为什么值得单独跑：那段 shell 里既有文件搬运，又有「把仓库内的 upstream/ 导出到
包内并写入 .upstream-id」的逻辑 —— 它是用户拿到上游源码的唯一常规渠道。写错了
不会报错，只会让包里的 `upstream/` 悄悄少掉（新装用户于是装不上），或者让
`.upstream-id` 与内容不一致（面板永远提示「随包上游与本机不一致」）。

做法：在**临时目录**（仓库外）搭一个真实的 git 仓库（打包步骤用
`git archive HEAD:upstream` 与 `git rev-parse HEAD:upstream`），把 release.yml 的
pack 脚本原样执行，只跳过本机没有的 `zip` 那几行。跑完连临时目录一起删掉 ——
留在仓库里会被其它测试扫到（例如「.cmd 必须 CRLF」会把这份快照当成真文件）。

    python dev/check_release_pack.py
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ['git', '-c', 'user.email=pack@test', '-c', 'user.name=pack',
         '-c', 'commit.gpgsign=false', '-c', 'core.autocrlf=false', *args],
        cwd=repo, capture_output=True, text=True, encoding='utf-8', errors='replace')


def main() -> int:
    import yaml

    try:  # Windows 控制台默认 GBK，打印 ✓ / ✗ 会直接抛 UnicodeEncodeError
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:  # noqa: BLE001
        pass

    wf_path = ROOT / '.github' / 'workflows' / 'release.yml'
    data = yaml.safe_load(wf_path.read_text(encoding='utf-8'))
    job = next(iter(data['jobs'].values()))
    step = next(s for s in job['steps'] if s.get('id') == 'pack')
    script = step['run']
    # 去掉 GitHub 表达式与本机没有的 zip，其余**原样执行**
    script = script.replace('${{ steps.vars.outputs.tag }}', 'v9.9.9-test')
    script = script.replace('${{ github.repository }}', 'zcvic/token-farm')
    script = '\n'.join(
        l for l in script.splitlines()
        if 'zip -qr' not in l and not ('ls -lh' in l and '.zip' in l)
    )

    work = Path(tempfile.mkdtemp(prefix='tf-pack-'))
    try:
        # 搭一个「checkout 现场」：照 CI 的 checkout 结果复制需要的路径。
        # 不用 git archive + tar 解：本机 tar 对中文文件名会报 Invalid empty pathname
        # （Windows 上的编码怪癖，CI 跑在 Linux 没这问题）。
        repo = work / 'repo'
        repo.mkdir()
        for rel in ('server', 'deploy', 'docs', 'upstream'):
            shutil.copytree(ROOT / rel, repo / rel,
                            ignore=shutil.ignore_patterns('__pycache__', '.pytest_cache', '.git'))
        shutil.copytree(ROOT / 'web' / 'out', repo / 'web' / 'out')
        for name in ('.env.example', '.gitignore', 'README.md', 'README.en.md', 'CHANGELOG.md',
                     'LICENSE', 'Dockerfile', 'docker-compose.yml'):
            src = ROOT / name
            if src.is_file():
                shutil.copyfile(src, repo / name)

        # 打包步骤依赖真实仓库（git archive / rev-parse），所以现场必须提交一次。
        for args in (('init', '-q'), ('add', '-A'), ('commit', '-q', '-m', 'fixture')):
            r = _git(repo, *args)
            if r.returncode != 0:
                print(f'git {" ".join(args)} 失败：{r.stderr}', file=sys.stderr)
                return 1

        print('=== 执行组装步骤（zip 那几行跳过）===')
        env = os.environ.copy()
        # GITHUB_OUTPUT 在 CI 里由 runner 注入；本机不设的话
        # `echo x >> $GITHUB_OUTPUT` 会报 ambiguous redirect。
        env['GITHUB_OUTPUT'] = str(work / 'github-output')
        # 本机没有 bash 时给出可执行的提示，而不是一个看不懂的 FileNotFoundError
        try:
            r = subprocess.run(['bash', '-c', script], cwd=repo, capture_output=True,
                               text=True, encoding='utf-8', errors='replace', env=env)
        except FileNotFoundError:
            print('找不到 bash：Windows 上把 Git 的 bin 目录加进 PATH 再跑，'
                  '例如 $env:PATH = "C:\\Program Files\\Git\\bin;" + $env:PATH',
                  file=sys.stderr)
            return 1
        out = r.stdout or ''
        print('\n'.join(out.splitlines()[-12:]))
        if r.stderr:
            print(r.stderr[-1200:], file=sys.stderr)
        if r.returncode != 0:
            print(f'组装步骤退出码 {r.returncode}', file=sys.stderr)
            return 1

        problems: list[str] = []

        def check(ok: bool, label: str, detail: str = '') -> None:
            print(f'  {"✓" if ok else "✗"}  {label}' + (f'\n       {detail}' if detail else ''))
            if not ok:
                problems.append(label)

        stage = repo / 'token-farm-v9.9.9-test'
        up = stage / 'upstream'
        check(stage.is_dir(), '组装出发布目录', stage.name)

        files = sorted(p for p in up.rglob('*') if p.is_file()) if up.is_dir() else []
        check(up.is_dir() and len(files) > 200, 'upstream/ 已内嵌（来自仓库内的 upstream/）',
              f'文件数={len(files)}')
        for name in ('docker-compose.yml', 'Dockerfile', 'LICENSE', 'scripts/task_runner.py'):
            check((up / name).is_file(), f'upstream/{name} 就位')
        check(not (up / '.git').exists(), '内嵌的 upstream/ 不含 .git')
        check(not (up / 'config.json').exists() and not (up / 'auths').exists()
              and not (up / 'data').exists(), '内嵌的 upstream/ 不含运行时数据与凭据')

        tree = _git(repo, 'rev-parse', 'HEAD:upstream').stdout.strip()
        want_id = hashlib.sha256(tree.encode('ascii')).hexdigest()
        got_id = ((up / '.upstream-id').read_text(encoding='utf-8').strip()
                  if (up / '.upstream-id').is_file() else '')
        check(got_id == want_id, '.upstream-id 由 upstream 树对象 id 派生',
              f'{got_id[:12] or "(缺失)"} / 期望 {want_id[:12]}')

        check((stage / '.version').is_file(), '.version 已写入')
        check((stage / 'server' / 'main.py').is_file(), 'server/ 已打包')
        check((stage / 'deploy' / 'install.sh').is_file(), 'deploy/install.sh 已打包')

        # 打成 tar 再验一次（发布产物就是这个）。用 tarfile 读清单：本机 tar 的输出
        # 含非 UTF-8 文件名时会让 text 解码失败（harness 的坑，不是包的问题）。
        subprocess.run(['tar', 'czf', f'{stage.name}.tar.gz', stage.name], cwd=repo, check=True)
        import tarfile
        with tarfile.open(repo / f'{stage.name}.tar.gz', 'r:gz') as tf:
            names = set(tf.getnames())
        check(f'{stage.name}/upstream/scripts/task_runner.py' in names,
              '发布 tar 包内含上游源码')
        check(f'{stage.name}/upstream/.upstream-id' in names,
              '发布 tar 包内含 .upstream-id（更新器据此判断是否需要同步上游）')

        size = (repo / f'{stage.name}.tar.gz').stat().st_size
        print(f'\n发布包大小：{size / 1024 / 1024:.1f} MB')
        print('=== 结果 ===')
        if problems:
            print(f'✗ {len(problems)} 项未通过：' + '、'.join(problems))
            return 1
        print('ALL CHECKS PASSED')
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
