"""每模型「积分消耗速度」（credit ratio）的持久化与合并。

Trae 的倍率没有稳定的公开接口，实测有三条来源，按优先级从高到低：

1. **面板导入的 JSON** —— `<DATA_DIR>/platforms/trae/model_rates.json`。
   显式覆盖，服务器部署想改价、或客户端快照里没有的模型，都靠它；
2. **本机客户端快照** —— `%APPDATA%\\Trae CN\\...\\state.vscdb` 的
   `model_list_map`（见 `desktop._consumption_rate`）。客户端界面显示的就是
   这份值，是「用户眼里的真相」；
3. **内置默认值** —— 与本模块同目录的 `model_rates.json`。随代码发布，
   服务器上没有客户端时也能显示倍率。

为什么不直接用服务端接口：`POST /api/ide/v1/batch_get_detail_param`
（`{"functions": ["chat_v3"]}`）确实会返回每个模型的
`display_contact_config.consumption_rate.data.rate`，但**它不是模型清单那条
通道**——客户端拉清单走内部 RPC `model/model_list_by_function`（本地
`ai_agent` 服务），落盘条目带 `features` / `icon` / `fee_model_level`；
`batch_get_detail_param` 返回的 `config_info_list` 是另一套「配置明细」结构。
两边的目录也因此不同步：实测 2026-11-01 服务端只有 `glm-5.2` /
`kimi-k2-0905`，没有客户端已上架的 `glm-5.3` / `kimi-k3` / `qwen3.8`，
且同一模型倍率也会不同（`DeepSeek-V4-Pro-Official` 服务端 0.72、客户端
0.36）。所以不能作为主来源。

（试过切换 `mode_type` 0-3、`access_type` 0-2、`agent_type` 与 16 个
`functions`，返回的仍是同一份旧目录；云端 `get_model_list` 系列在 core /
api.trae.com.cn / api.trae.cn 上均 404。）

合并口径：**模型 id 大小写不敏感**（Trae 自己两种写法都有），返回给调用方时
保留原始写法。取不到返回 `None` —— 不要把「不知道」写成 0，界面会读成免费。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from ... import config
from . import desktop

logger = logging.getLogger('workbuddy.platforms.trae.rates')

BUNDLED_PATH = Path(__file__).with_name('model_rates.json')
IMPORTED_NAME = 'model_rates.json'

_lock = threading.RLock()


def store_path() -> Path:
    """面板导入的倍率表（与账号目录同级，随数据目录备份）。"""
    return config.DATA_DIR / 'platforms' / 'trae' / IMPORTED_NAME


# ── 解析 / 校验 ─────────────────────────────────────────────
def _rate(value: object) -> float | None:
    """单个倍率：只接受有限、非负的数字；其余一律视为「不知道」。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number < 0 or number == float('inf'):
        return None
    return number


def normalize(payload: object) -> dict[str, float]:
    """把多种形状的输入归一成 `{模型 id: 倍率}`。

    接受三种写法（都为导入服务，谁也不用先改格式）：

    · `{"glm-5.3": 0.78, ...}`
    · `{"rates": {...}}`（本模块导出的 JSON）
    · `[{"id"/"name"/"model"/"model_name": "glm-5.3", "credit_ratio"/"rate": 0.78}]`

    无法识别的条目直接丢掉：一份外部文件里混进一条坏数据，不该让整次导入失败。
    """
    table: dict[str, float] = {}
    if isinstance(payload, dict):
        source: object = payload.get('rates', payload)
        if isinstance(source, dict):
            for name, value in source.items():
                rate = _rate(value)
                if rate is not None and str(name).strip():
                    table[str(name).strip()] = rate
            return table
        if isinstance(source, list):
            payload = source
        else:
            return table
    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            name = (item.get('id') or item.get('name')
                    or item.get('model') or item.get('model_name'))
            rate = _rate(item.get('credit_ratio', item.get('rate')))
            if rate is not None and str(name or '').strip():
                table[str(name).strip()] = rate
    return table


def _read(path: Path) -> dict[str, float]:
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return normalize(raw)


# ── 三个来源 ────────────────────────────────────────────────
def bundled() -> dict[str, float]:
    """内置默认倍率（随代码发布）。"""
    return _read(BUNDLED_PATH)


def imported() -> dict[str, float]:
    """面板导入的倍率（没有导入过就是空）。"""
    return _read(store_path())


def local() -> dict[str, float]:
    """本机客户端快照里的倍率（服务器上没有客户端时为空）。"""
    try:
        models = desktop.read_models('cn')
    except Exception as exc:  # noqa: BLE001 —— 读不到就当作没有
        logger.warning('读取客户端模型倍率失败：%s', exc)
        return {}
    return {str(m.get('id') or ''): float(m['credit_ratio'])
            for m in models if m.get('credit_ratio') is not None}


def merged() -> dict[str, float]:
    """合并后的倍率表（保留原始 id 写法，优先级见模块说明）。"""
    table: dict[str, float] = {}
    seen: set[str] = set()
    for source in (imported(), local(), bundled()):
        for name, rate in source.items():
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            table[name] = rate
    return table


def lookup() -> dict[str, float]:
    """小写 id → 倍率，给适配器按模型 id 回填用。"""
    return {name.lower(): rate for name, rate in merged().items()}


# ── 导入 / 导出 / 状态 ──────────────────────────────────────
def save_imported(payload: object) -> dict[str, Any]:
    """落盘一次导入。空表视为非法：那多半是文件选错了，不该清空现有配置。"""
    table = normalize(payload)
    if not table:
        raise ValueError('没有解析到任何模型倍率（需要 {"模型": 倍率} 形状）')
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        'version': 1,
        'updated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'source': 'imported',
        'rates': dict(sorted(table.items(), key=lambda kv: kv[0].lower())),
    }
    tmp = path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(body, ensure_ascii=False, indent=2) + '\n',
                   encoding='utf-8')
    tmp.replace(path)
    with _lock:
        return summary()


def clear_imported() -> dict[str, Any]:
    """删掉导入表，回到「客户端快照 + 内置默认值」。"""
    path = store_path()
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise ValueError(f'删除导入的倍率表失败：{exc}') from exc
    return summary()


def summary() -> dict[str, Any]:
    """面板需要的状态：合并结果 + 各来源的条数（不含全部明细，避免泄露）。"""
    imp = imported()
    loc = local()
    bun = bundled()
    return {
        'rates': merged(),
        'sources': {
            'imported': len(imp),
            'desktop': len(loc),
            'bundled': len(bun),
        },
        'imported_path': str(store_path()),
        'imported_at': _imported_at(),
    }


def _imported_at() -> str:
    try:
        raw = json.loads(store_path().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return ''
    value = raw.get('updated_at') if isinstance(raw, dict) else None
    return str(value or '')


def export_payload() -> dict[str, Any]:
    """导出成可直接再导入的 JSON（面板「导出」按钮）。"""
    return {
        'version': 1,
        'updated_at': time.strftime('%Y-%m-%d'),
        'source': 'trae-token-farm',
        'note': '可直接在面板「导入倍率」里再次导入。',
        'rates': dict(sorted(merged().items(), key=lambda kv: kv[0].lower())),
    }
