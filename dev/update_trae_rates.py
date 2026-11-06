#!/usr/bin/env python3
"""用本机 Trae 客户端快照刷新内置倍率表。

背景：Trae 的「积分消耗速度」（每模型扣费倍率）只在客户端 UI 里显示，服务端
接口的目录比客户端慢一代（详见 `server/platforms/trae/rates.py`）。所以仓库里
带了一份 `server/platforms/trae/model_rates.json` 作为服务器部署的默认值——
这个脚本负责把它和本机客户端对齐。

用法（在仓库根目录、任意 Python 3.9+ 环境）：

    python dev/update_trae_rates.py            # 有变化就写回 JSON
    python dev/update_trae_rates.py --check    # 只检查，有变化时退出码 1（CI 用）
    python dev/update_trae_rates.py --dry-run  # 打印差异，不落盘
    python dev/update_trae_rates.py --prune    # 同时删掉客户端已下架的模型

退出码：
    0  成功（`--check` 时为「没有变化」）
    1  `--check` 模式下发现变化
    2  没有检测到本机 Trae，或客户端快照里没有倍率
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.platforms.trae import desktop, rates  # noqa: E402

APP_NAMES = ('Trae CN', 'Trae')


def _app_roots() -> list[Path]:
    """可能的安装根目录（只看 `<root>/<Trae>/resources/app/package.json`）。"""
    home = Path.home()
    roots: list[Path] = []
    if sys.platform == 'win32':
        for env in ('LOCALAPPDATA', 'PROGRAMFILES', 'PROGRAMW6432'):
            base = os.environ.get(env)
            if base:
                roots.append(Path(base) / 'Programs')
                roots.append(Path(base))
    elif sys.platform == 'darwin':
        roots.append(Path('/Applications'))
    else:
        roots.extend([Path('/opt'), home / '.local' / 'share'])
    return roots


def client_version(realm: str) -> str:
    """客户端版本号（读 `package.json`）；读不到返回空串。"""
    names = ('Trae CN',) if realm == 'cn' else ('Trae',)
    for root in _app_roots():
        for name in names:
            pkg = root / name / 'resources' / 'app' / 'package.json'
            if not pkg.is_file():
                continue
            try:
                data = json.loads(pkg.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            version = str(data.get('version') or '').strip()
            if version:
                return version
    return ''


def detect_install(realm: str) -> list[str]:
    """本地是否装了 Trae（返回「探测到的证据」，空列表 = 没装）。"""
    evidence: list[str] = []
    names = ('Trae CN',) if realm == 'cn' else ('Trae',)
    for root in _app_roots():
        for name in names:
            if (root / name).is_dir():
                evidence.append(str(root / name))
    state_db = desktop._state_db(realm)
    if state_db is not None:
        evidence.append(str(state_db))
    return evidence


def _find(table: dict[str, float], name: str) -> str | None:
    """模型 id 大小写不敏感地找到已有键。"""
    lowered = name.lower()
    for key in table:
        if key.lower() == lowered:
            return key
    return None


def _display(path: Path) -> str:
    """仓库内路径显示成相对路径（更短），仓库外原样显示。"""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def snapshot(realm: str) -> dict[str, float]:
    """客户端快照里的模型 id → 倍率。"""
    table: dict[str, float] = {}
    for model in desktop.read_models(realm):
        model_id = str(model.get('id') or '').strip()
        ratio = model.get('credit_ratio')
        if model_id and isinstance(ratio, (int, float)) and not isinstance(ratio, bool):
            table[model_id] = float(ratio)
    return table


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--realm', choices=('cn', 'intl'), default='cn',
                        help='读取哪个客户端（默认国内版 Trae CN）')
    parser.add_argument('--check', action='store_true',
                        help='只检查是否有变化，不写文件（有变化退出码 1）')
    parser.add_argument('--dry-run', action='store_true',
                        help='打印差异但不写文件')
    parser.add_argument('--prune', action='store_true',
                        help='删除客户端快照里已经没有的模型（默认保留）')
    parser.add_argument('--file', default=str(rates.BUNDLED_PATH),
                        help='要更新的 JSON（默认内置默认值文件）')
    args = parser.parse_args()

    evidence = detect_install(args.realm)
    if not evidence:
        searched = ', '.join(str(p) for p in _app_roots()) or '默认安装位置'
        print(f'未检测到本机 Trae 客户端（查过 {searched}）。')
        return 2

    fresh = snapshot(args.realm)
    if not fresh:
        print(f'已检测到 Trae（{evidence[0]}），但客户端快照里没有模型倍率。\n'
              '请先启动一次客户端并打开模型选择器，让本地缓存写入 rate。')
        return 2

    target = Path(args.file)
    try:
        data = json.loads(target.read_text(encoding='utf-8'))
    except FileNotFoundError:
        data = {}
    except ValueError as exc:
        print(f'{target} 不是合法 JSON：{exc}')
        return 2

    current = rates.normalize(data)
    added: list[tuple[str, float]] = []
    changed: list[tuple[str, float, float]] = []
    for name, ratio in fresh.items():
        key = _find(current, name)
        if key is None:
            added.append((name, ratio))
        elif abs(current[key] - ratio) > 1e-9:
            changed.append((key, current[key], ratio))
    removed = [key for key in current if _find(fresh, key) is None]

    if not added and not changed and not (removed and args.prune):
        print(f'倍率已是最新（{len(fresh)} 个模型，{_display(target)}）。')
        return 0

    for name, ratio in sorted(added, key=lambda kv: kv[0].lower()):
        print(f'  + {name}: {ratio}')
    for name, old, new in sorted(changed, key=lambda kv: kv[0].lower()):
        print(f'  ~ {name}: {old} -> {new}')
    for name in sorted(removed, key=str.lower):
        print(f'  - {name}（{"删除" if args.prune else "保留"}）')
    if args.check:
        print('发现变化；`--check` 模式不写文件。')
        return 1
    if args.dry_run:
        print('干跑模式，未写文件。')
        return 0

    merged = dict(current)
    for name, ratio in fresh.items():
        key = _find(merged, name)
        merged[key or name] = ratio
    if args.prune:
        merged = {k: v for k, v in merged.items() if _find(fresh, k) is not None}

    version = client_version(args.realm)
    source = ('Trae CN' if args.realm == 'cn' else 'Trae')
    source += f' {version}' if version else ''
    body = {
        'version': 1,
        'updated_at': time.strftime('%Y-%m-%d'),
        'source': f'{source} 客户端模型列表快照',
        'note': data.get('note') if isinstance(data, dict) else None,
        'rates': dict(sorted(merged.items(), key=lambda kv: kv[0].lower())),
    }
    if not body['note']:
        body.pop('note')
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + '.tmp')
    tmp.write_text(json.dumps(body, ensure_ascii=False, indent=2) + '\n',
                   encoding='utf-8')
    tmp.replace(target)
    print(f'已更新 {_display(target)}（{len(merged)} 个模型）。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
