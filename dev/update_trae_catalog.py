#!/usr/bin/env python3
"""用本机 Trae 客户端快照刷新内置模型目录。

背景：Trae 的模型清单只在客户端里是全的（服务端 `model_list` 只回 5 个老模型，
`batch_get_detail_param` 是另一套配置明细接口），所以仓库里带了一份
`server/platforms/trae/model_catalog.json` 作为服务器部署的默认值——这个脚本
负责把它和本机客户端对齐。

用法（在仓库根目录、任意 Python 3.9+ 环境）：

    python dev/update_trae_catalog.py            # 有变化就写回 JSON
    python dev/update_trae_catalog.py --check    # 只检查，有变化时退出码 1（CI 用）
    python dev/update_trae_catalog.py --dry-run  # 打印差异，不落盘
    python dev/update_trae_catalog.py --prune    # 同时删掉客户端已下架的模型

退出码：
    0  成功（`--check` 时为「没有变化」）
    1  `--check` 模式下发现变化
    2  没有检测到本机 Trae，或客户端快照里没有模型
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

from server.platforms.trae import catalog, desktop  # noqa: E402

APP_NAMES = ('Trae CN', 'Trae')
#: 参与比较的字段。`id` 单独做键，不进这个列表。
FIELDS = ('name', 'realm', 'context_window', 'max_tokens', 'multimodal',
          'thinking', 'channel')


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


def _display(path: Path) -> str:
    """仓库内路径显示成相对路径（更短），仓库外原样显示。"""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def snapshot(realm: str) -> list[dict]:
    """客户端快照里的模型（已经归一成目录形状，按小写 id 去重）。"""
    return catalog.normalize(desktop.read_models(realm), default_realm=realm)


def _index(models: list[dict]) -> dict[str, dict]:
    return {m['id'].lower(): m for m in models}


def _diff(old: dict[str, dict], new: dict[str, dict], realm: str):
    added = [key for key in new if key not in old]
    # 只把「本次读取的那个区」里消失的算下线：跑 `--realm cn` 时，文件里的
    # 国际版条目本来就不在快照里，不该每次都被列成「已下线」。
    removed = [key for key in old
               if key not in new and old[key].get('realm') == realm]
    changed: list[tuple[str, list[str]]] = []
    for key in new:
        before = old.get(key)
        if before is None:
            continue
        fields = [f for f in FIELDS if before.get(f) != new[key].get(f)]
        if fields:
            changed.append((key, fields))
    return added, changed, removed


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--realm', choices=('cn', 'intl'), default='cn',
                        help='读取哪个客户端（默认国内版 Trae CN）')
    parser.add_argument('--check', action='store_true',
                        help='只检查是否有变化，不写文件（有变化退出码 1）')
    parser.add_argument('--dry-run', action='store_true',
                        help='打印差异但不写文件')
    parser.add_argument('--prune', action='store_true',
                        help='删除客户端快照里已经没有的模型（默认保留）')
    parser.add_argument('--file', default=str(catalog.BUNDLED_PATH),
                        help='要更新的 JSON（默认内置默认值文件）')
    args = parser.parse_args()

    evidence = detect_install(args.realm)
    if not evidence:
        searched = ', '.join(str(p) for p in _app_roots()) or '默认安装位置'
        print(f'未检测到本机 Trae 客户端（查过 {searched}）。')
        return 2

    fresh = snapshot(args.realm)
    if not fresh:
        print(f'已检测到 Trae（{evidence[0]}），但客户端快照里没有模型目录。\n'
              '请先启动一次客户端并打开模型选择器，让本地缓存写入 model_list_map。')
        return 2

    target = Path(args.file)
    try:
        data = json.loads(target.read_text(encoding='utf-8'))
    except FileNotFoundError:
        data = {}
    except ValueError as exc:
        print(f'{target} 不是合法 JSON：{exc}')
        return 2

    current = catalog.normalize(data, default_realm=args.realm)
    old, new = _index(current), _index(fresh)
    added, changed, removed = _diff(old, new, args.realm)

    if not added and not changed and not (removed and args.prune):
        print(f'目录已是最新（{len(fresh)} 个模型，{_display(target)}）。')
        return 0

    for key in sorted(added):
        print(f'  + {new[key]["id"]}（{new[key]["name"]}）')
    for key, fields in sorted(changed):
        print(f'  ~ {new[key]["id"]}')
        for field in fields:
            print(f'      {field}: {old[key].get(field)!r} -> {new[key].get(field)!r}')
    for key in sorted(removed):
        print(f'  - {old[key]["id"]}（{"删除" if args.prune else "保留"}）')
    if args.check:
        print('发现变化；`--check` 模式不写文件。')
        return 1
    if args.dry_run:
        print('干跑模式，未写文件。')
        return 0

    merged = {key: dict(model) for key, model in old.items()}
    for key, model in new.items():
        merged[key] = model
    if args.prune:
        merged = {key: model for key, model in merged.items() if key in new}
    models = sorted(merged.values(), key=lambda m: (m.get('realm') != args.realm,
                                                    m['id'].lower()))

    version = client_version(args.realm)
    client = 'Trae CN' if args.realm == 'cn' else 'Trae'
    source = f'{client} {version} 客户端模型列表快照' if version else \
        f'{client} 客户端模型列表快照'
    body = {
        'version': 1,
        'updated_at': time.strftime('%Y-%m-%d'),
        'source': source,
        'note': data.get('note') if isinstance(data, dict) else None,
        'models': models,
    }
    if not body['note']:
        body.pop('note')
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + '.tmp')
    tmp.write_text(json.dumps(body, ensure_ascii=False, indent=2) + '\n',
                   encoding='utf-8')
    tmp.replace(target)
    print(f'已更新 {_display(target)}（{len(models)} 个模型）。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
