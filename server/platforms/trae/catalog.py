"""Trae 模型目录：三来源合并 + 面板导入。

目录和「积分消耗速度」一样，服务端接口给不全（`model_list` 只回 5 个老模型、
`batch_get_detail_param` 是另一套配置明细接口），所以维护方式也照倍率来：

1. **面板导入的 JSON** —— `<DATA_DIR>/platforms/trae/model_catalog.json`。
   显式覆盖，服务器部署想补模型、或客户端快照里还没有的模型，都靠它；
2. **本机客户端快照** —— `%APPDATA%\\Trae CN\\...\\state.vscdb` 的
   `model_list_map`（见 `desktop.read_models`）。装了客户端就永远是最新的一份；
3. **内置默认值** —— 与本模块同目录的 `model_catalog.json`。随代码发布，
   服务器上没有客户端时也能列出模型。

合并口径与 `rates` 模块一致：**模型 id 大小写不敏感**，先出现的来源胜出，
因此「导入 > 客户端快照 > 内置默认值」。条目按 `chat: true` 过滤——其余是
code_reviewer / refactor 等内部角色模型，列出来只会让客户端选中后报错。

每条带一个 `channel`（见 `_channel`）：国内版走 Agent 通道（
`llm_utils_chat`），国际版走 IDE 数据面（`llm_raw_chat`）。选错通道的表现是
`[4023] model is unknown`，适配器会自动换另一条通道重试一次并把结果记住。

运行时缓存（`DB` 设置 `platforms.trae.models`）记录「上一次实际合并出来的结果」，
TTL 6 小时，`refresh=true` 可绕过。缓存命中时适配器仍会把导入表再叠一次
（见 `TraeAdapter._prefer_imported`），所以导入新的目录不需要等缓存过期。
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
from .constants import DEFAULT_REALM, normalize_realm

logger = logging.getLogger('workbuddy.platforms.trae.catalog')

BUNDLED_PATH = Path(__file__).with_name('model_catalog.json')
IMPORTED_NAME = 'model_catalog.json'

_lock = threading.RLock()
#: 路径 → ((mtime, size), 解析结果)。`find()` 在每次对话都会调用，
#: 不能每次都读盘；文件一变签名就变，缓存自然失效。
_cache: dict[str, tuple[tuple[float, int], Any]] = {}


def store_path() -> Path:
    """面板导入的目录（与倍率表、账号目录同级，随数据目录备份）。"""
    return config.DATA_DIR / 'platforms' / 'trae' / IMPORTED_NAME


# ── 解析 / 校验 ─────────────────────────────────────────────
def _int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        number = int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def _context(raw: dict) -> int:
    """上下文窗口：兼容我们自己的写法和客户端 `context_window_size` 结构。"""
    value = raw.get('context_window')
    if not value:
        size = raw.get('context_window_size')
        if isinstance(size, dict):
            value = size.get('default')
            if not value:
                maximum = size.get('max')
                if isinstance(maximum, list) and maximum:
                    value = maximum[0]
                elif isinstance(maximum, (int, float)):
                    value = maximum
    if not value:
        value = raw.get('context_window_tokens')
    return _int(value)


def _thinking(raw: dict) -> bool:
    """是否思考模型：兼容 `thinking: {supported}`、`thinking: bool` 与客户端字段。"""
    node = raw.get('thinking')
    if isinstance(node, dict) and 'supported' in node:
        return bool(node.get('supported'))
    if isinstance(node, bool):
        return node
    return bool(raw.get('thinking_enable')
                or raw.get('model_type') == 'reasoning_model')


def _channel(model: dict) -> str:
    """这个模型该走哪条通道：`chat`（IDE 数据面）还是 `solo`（Agent）。

    **国内版的新模型只在 Agent 通道上可用**：`llm_raw_chat` 对它们一律回
    `[4023] model is unknown`（实测 deepseek-v4.1-flash / glm-5.3 / qwen3.8-max
    全部如此），而 `llm_utils_chat` + `function=solo_work_lite` 能正常出字。
    国际版没有这条通道，只能走 IDE 数据面。
    """
    return 'solo' if str(model.get('realm') or '') == 'cn' else 'chat'


def _item(raw: object, default_realm: str) -> dict | None:
    """一条原始条目 → 我们的目录形状；不是可列出的聊天模型返回 None。"""
    if not isinstance(raw, dict):
        return None
    if raw.get('chat') is False:
        return None
    if raw.get('status') is False or raw.get('selectable') is False:
        return None
    model_id = str(raw.get('upstream_key') or raw.get('key') or raw.get('id')
                   or raw.get('model') or raw.get('model_name')
                   or raw.get('name') or '').strip()
    if not model_id:
        return None
    realm = normalize_realm(raw.get('realm') or raw.get('region') or default_realm)
    channel = raw.get('channel')
    if channel not in ('solo', 'chat'):
        channel = _channel({'realm': realm})
    return {
        'id': model_id,
        'name': str(raw.get('display_name') or raw.get('display')
                    or raw.get('name') or model_id),
        'realm': realm,
        'context_window': _context(raw),
        'max_tokens': _int(raw.get('max_tokens')),
        'multimodal': bool(raw.get('multimodal')),
        'thinking': _thinking(raw),
        'channel': channel,
    }


def normalize(payload: object, default_realm: str = DEFAULT_REALM) -> list[dict]:
    """把多种形状的输入归一成模型列表（按小写 id 去重，先出现者胜出）。

    接受这些写法（都为导入服务，谁也不用先改格式）：

    · `{"models": [...]}` —— 本模块导出的 JSON；
    · `{"catalog": [...]}` / `{"data": [...]}`；
    · 裸数组 `[...]`；
    · `{"glm-5.3": {...}}` 这种 id → 字段 的映射；
    · 参考项目 `trae2api-hub` 的 `trae_catalog_*.json`（含 `upstream_key`、
      `display_name`、`context_window_size`、`thinking.supported`、`chat`）。

    无法识别的条目直接丢掉：一份外部文件里混进一条坏数据，不该让整次导入失败。
    """
    items: object = payload
    if isinstance(payload, dict):
        for key in ('models', 'catalog', 'data'):
            value = payload.get(key)
            if isinstance(value, (list, dict)):
                items = value
                break
        else:
            items = payload
    if isinstance(items, dict):
        rows: list[object] = []
        for name, value in items.items():
            if isinstance(value, dict):
                rows.append({**value, 'id': value.get('id') or name})
        items = rows
    if not isinstance(items, list):
        return []

    out: list[dict] = []
    seen: set[str] = set()
    for raw in items:
        model = _item(raw, default_realm)
        if model is None:
            continue
        key = model['id'].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(model)
    return out


# ── 三个来源 ────────────────────────────────────────────────
def _read(path: Path) -> object:
    """读 JSON（按 mtime + 大小缓存；解析失败当作不存在）。"""
    try:
        stat = path.stat()
    except OSError:
        return None
    signature = (stat.st_mtime, stat.st_size)
    with _lock:
        hit = _cache.get(str(path))
        if hit and hit[0] == signature:
            return hit[1]
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    with _lock:
        _cache[str(path)] = (signature, raw)
    return raw


def _forget(path: Path) -> None:
    with _lock:
        _cache.pop(str(path), None)


def bundled() -> list[dict]:
    """内置默认目录（随代码发布）。"""
    return normalize(_read(BUNDLED_PATH))


def imported() -> list[dict]:
    """面板导入的目录（没有导入过就是空）。"""
    return normalize(_read(store_path()))


def local() -> list[dict]:
    """本机客户端快照里的目录（服务器上没有客户端时为空）。

    再过一遍 `normalize`：客户端条目带 `credit_ratio`，那是倍率模块的字段，
    留在目录里会让「导出目录」多出一列，回灌导入时又会被丢掉。
    """
    try:
        models = desktop.read_models('cn')
    except Exception as exc:  # noqa: BLE001 —— 读不到就当作没有
        logger.warning('读取客户端模型目录失败：%s', exc)
        return []
    return normalize(models, default_realm='cn')


def merged() -> list[dict]:
    """合并后的目录（优先级：导入 > 客户端快照 > 内置默认值）。"""
    out: list[dict] = []
    seen: set[str] = set()
    for group in (imported(), local(), bundled()):
        for model in group:
            key = str(model.get('id') or '').strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(dict(model))
    return out


def default_models() -> list[dict]:
    """目录默认值（副本，调用方可以安全修改）。"""
    return merged()


def find(model_id: str) -> dict | None:
    """按 id 找一条目录（大小写不敏感）。

    只查「导入 + 内置」这一层，不读客户端 sqlite：它在对话热路径上（决定走
    哪条通道），每次请求都开一次库不值得。客户端快照里的模型本来就固定走
    `solo`，适配器在 `_channel` 里另有兜底。
    """
    target = str(model_id or '').strip().lower()
    if not target:
        return None
    for group in (imported(), bundled()):
        for model in group:
            if model['id'].lower() == target:
                return dict(model)
    return None


# ── 导入 / 导出 / 状态 ──────────────────────────────────────
def save_imported(payload: object) -> dict[str, Any]:
    """落盘一次导入。空列表视为非法：那多半是文件选错了，不该清空现有配置。"""
    models = normalize(payload)
    if not models:
        raise ValueError('没有解析到任何模型（需要 {"models": [...]} 或模型数组）')
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        'version': 1,
        'updated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'source': 'imported',
        'models': models,
    }
    tmp = path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(body, ensure_ascii=False, indent=2) + '\n',
                   encoding='utf-8')
    tmp.replace(path)
    _forget(path)
    with _lock:
        return summary()


def clear_imported() -> dict[str, Any]:
    """删掉导入的目录，回到「客户端快照 + 内置默认值」。"""
    path = store_path()
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise ValueError(f'删除导入的模型目录失败：{exc}') from exc
    _forget(path)
    return summary()


def summary() -> dict[str, Any]:
    """面板需要的状态：各来源条数 + 合并后的总数（不含明细）。"""
    imp = imported()
    loc = local()
    bun = bundled()
    return {
        'sources': {
            'imported': len(imp),
            'desktop': len(loc),
            'bundled': len(bun),
        },
        'total': len(merged()),
        'imported_path': str(store_path()),
        'imported_at': _imported_at(),
    }


def _imported_at() -> str:
    raw = _read(store_path())
    value = raw.get('updated_at') if isinstance(raw, dict) else None
    return str(value or '')


def export_payload() -> dict[str, Any]:
    """导出成可直接再导入的 JSON（面板「导出目录」按钮）。"""
    return {
        'version': 1,
        'updated_at': time.strftime('%Y-%m-%d'),
        'source': 'trae-token-farm',
        'note': '可直接在面板「导入目录」里再次导入。',
        'models': merged(),
    }
