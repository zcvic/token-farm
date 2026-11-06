#!/usr/bin/env python3
"""把 qoder2api-hub 的协议层重新 vendor 到 `server/platforms/qoder/_vendor/`。

上游是「脚本目录即工作目录」的写法（`import qoder_accounts`），这里改成包内
相对导入（`from . import qoder_accounts`），其余内容保持逐字节一致，方便和
上游的修复做 diff。

用法：

    python server/platforms/qoder/tools/vendor_qoder.py --source /path/to/qoder2api-hub

    # 只检查现有副本是否仍然「无绝对导入」，不写文件
    python server/platforms/qoder/tools/vendor_qoder.py --check

升级后请一起核对 `server/platforms/qoder/embedded.py`：上游 `main()` 里的初始化
步骤如果新增了必要的模块级状态，这里也要补上。
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(os.path.dirname(HERE), '_vendor')

MODULES = [
    'qoder_accounts.py',
    'qoder_activity.py',
    'qoder_agents.py',
    'qoder_anthropic.py',
    'qoder_catalog.py',
    'qoder_fingerprint.py',
    'qoder_pricing.py',
    'qoder_proxy.py',
    'qoder_scheduler.py',
    'qoder_session_cache.py',
    'qoder_settings.py',
    'qoder_sign.py',
    'qoder_tasks.py',
    'qoder_updater.py',
    'qoder_webtools.py',
]
DATA_FILES = [
    'baseprompt.json',
    'qoder_catalog_cn.json',
    'qoder_catalog_intl.json',
]
EXTRA_FILES = ['LICENSE']

_IMPORT_RE = re.compile(r'^([ \t]*)import qoder_(\w+)([ \t]*(?:#.*)?)$', re.M)
_FROM_RE = re.compile(r'^([ \t]*)from qoder_(\w+) import', re.M)
_LEFTOVER_RE = re.compile(r'^[ \t]*(?:import|from)[ \t]+qoder_\w+', re.M)
_VERSION_RE = re.compile(r'^VERSION = "([^"]+)"', re.M)


def rewrite_imports(text: str) -> str:
    text = _IMPORT_RE.sub(
        lambda m: '%sfrom . import qoder_%s%s' % (m.group(1), m.group(2), m.group(3)),
        text)
    return _FROM_RE.sub(
        lambda m: '%sfrom .qoder_%s import' % (m.group(1), m.group(2)), text)


def upstream_commit(source: str) -> str:
    try:
        out = subprocess.run(
            ['git', '-C', source, 'rev-parse', 'HEAD'],
            capture_output=True, text=True, timeout=10)
    except Exception:
        return ''
    return out.stdout.strip() if out.returncode == 0 else ''


def read_version(source: str) -> str:
    path = os.path.join(source, 'qoder_proxy.py')
    with open(path, encoding='utf-8') as fh:
        match = _VERSION_RE.search(fh.read())
    if not match:
        raise SystemExit('qoder_proxy.py 里找不到 VERSION')
    return match.group(1)


def check() -> int:
    """校验现有副本：没有绝对导入、能编译。"""
    bad = 0
    for name in MODULES:
        path = os.path.join(VENDOR, name)
        with open(path, encoding='utf-8') as fh:
            text = fh.read()
        for match in _LEFTOVER_RE.finditer(text):
            line = text[:match.start()].count('\n') + 1
            print('%s:%d: 残留绝对导入: %s'
                  % (name, line, match.group(0).strip()))
            bad += 1
        try:
            compile(text, path, 'exec')
        except SyntaxError as exc:
            print('%s: 编译失败: %s' % (name, exc))
            bad += 1
    print('check: %s' % ('OK' if not bad else '%d 处问题' % bad))
    return 0 if not bad else 1


def vendor(source: str) -> int:
    version = read_version(source)
    commit = upstream_commit(source)
    changed = []
    for name in MODULES:
        src = os.path.join(source, name)
        with open(src, encoding='utf-8', newline='') as fh:
            original = fh.read()
        text = rewrite_imports(original)
        if _LEFTOVER_RE.search(text):
            raise SystemExit('%s: 仍有绝对导入，改写规则需要补充' % name)
        compile(text, name, 'exec')
        dst = os.path.join(VENDOR, name)
        old = None
        if os.path.isfile(dst):
            with open(dst, encoding='utf-8', newline='') as fh:
                old = fh.read()
        if old != text:
            with open(dst, 'w', encoding='utf-8', newline='') as fh:
                fh.write(text)
            changed.append(name)
    for name in DATA_FILES + EXTRA_FILES:
        shutil.copy2(os.path.join(source, name), os.path.join(VENDOR, name))

    _stamp(version, commit)
    print('vendored %s (%s)' % (version, commit[:12] or 'commit unknown'))
    print('changed: %s' % (', '.join(changed) or '(none)'))
    print('记得核对 server/platforms/qoder/embedded.py 的初始化步骤。')
    return 0


def _stamp(version: str, commit: str) -> None:
    init_path = os.path.join(VENDOR, '__init__.py')
    with open(init_path, encoding='utf-8', newline='') as fh:
        text = fh.read()
    text = re.sub(r"^VERSION = '.*'$", "VERSION = '%s'" % version, text, flags=re.M)
    if commit:
        text = re.sub(r"^UPSTREAM_COMMIT = '.*'$",
                      "UPSTREAM_COMMIT = '%s'" % commit, text, flags=re.M)
    with open(init_path, 'w', encoding='utf-8', newline='') as fh:
        fh.write(text)

    readme_path = os.path.join(VENDOR, 'README.md')
    with open(readme_path, encoding='utf-8', newline='') as fh:
        readme = fh.read()
    readme = re.sub(r'\| 上游版本 \| `[^`]*` \|',
                    '| 上游版本 | `%s` |' % version, readme)
    if commit:
        readme = re.sub(r'\| 上游 commit \| `[^`]*` \|',
                        '| 上游 commit | `%s` |' % commit, readme)
    with open(readme_path, 'w', encoding='utf-8', newline='') as fh:
        fh.write(readme)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--source', default=os.environ.get('QODER2API_SRC', ''),
                    help='qoder2api-hub 仓库目录')
    ap.add_argument('--check', action='store_true',
                    help='只校验现有副本，不改文件')
    args = ap.parse_args(argv)
    if args.check:
        return check()
    if not args.source or not os.path.isdir(args.source):
        raise SystemExit('需要 --source 指向 qoder2api-hub 仓库目录')
    return vendor(os.path.abspath(args.source))


if __name__ == '__main__':
    sys.exit(main())
