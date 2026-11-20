"""DuMate 模型目录：桌面客户端真相源 → 内置快照的降级链。

与 `dumate2api` 的 `src/dumate-catalog.js` 同一套纪律：

  · 只导入**模型元数据**——桌面端 `opencode.json` 里的 endpoint / apiKey /
    headers / plugins 一律不跨这条边界，那些是客户端自己的运行时配置；
  · 三级降级：桌面端配置 → 进程缓存（`platforms.dumate.models`）→ 包内
    `catalog.json`。任何一级失败都不抛，只降级；
  · 清单存在 ≠ 有调用权限：这里给的是候选集，能不能用只能靠实测。

路径（社区三仓一致）：

  `%APPDATA%\\qianfan-desktop-app\\qianfan_desk_xdg\\<激活账号>\\config\\opencode\\opencode.json`
    → `provider.QianfanPersonalQuota.models`

`DUMATE_OPENCODE_CONFIG` 可直接指定文件，`DUMATE_USER_DATA_DIR` 可覆盖
`%APPDATA%\\qianfan-desktop-app`。两者都只为本机部署留的逃生口。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from . import constants as C

# 内部任务路由 / mock / OCR / 向量 / 重排 / 专用生图：都不是对话入口。
_EXCLUDE = ('embedding', 'reranker', 'ocr', 'llm-mock', 'dumate-image')

_INPUT_MODALITIES = {'text', 'image', 'audio', 'video', 'pdf'}
_MAX_FILE_BYTES = 2 * 1024 * 1024


def _valid_id(value: object) -> bool:
    text = str(value or '')
    if not text or len(text) > 160:
        return False
    if not (text[0].isalnum() and text[0].isascii()):
        return False
    return all(ch.isalnum() or ch in '._/-' for ch in text)


def _pos_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _modalities(value: object) -> list[str]:
    if not isinstance(value, list):
        return ['text']
    out: list[str] = []
    for item in value:
        text = str(item or '').strip().lower()
        if text in _INPUT_MODALITIES and text not in out:
            out.append(text)
    return out or ['text']


def import_models(config: object) -> list[dict]:
    """把桌面端 `opencode.json` 过滤成纯模型清单（不做任何网络 / 落盘）。"""
    if not isinstance(config, dict):
        return []
    provider = config.get('provider')
    quota = provider.get('QianfanPersonalQuota') if isinstance(provider, dict) else None
    source = quota.get('models') if isinstance(quota, dict) else None
    if not isinstance(source, dict):
        return []

    models: list[dict] = []
    for key, value in source.items():
        if not isinstance(value, dict):
            continue
        model_id = str(value.get('id') or key)
        if not _valid_id(key) or not _valid_id(model_id):
            continue
        # 显示别名指向源里另一个真实条目 → 重复，丢掉别名那份。
        if key != model_id and model_id in source:
            continue
        lower = key.lower()
        if (lower.startswith('model-') and lower != 'model-text') \
                or any(token in lower for token in _EXCLUDE):
            continue
        limit = value.get('limit') if isinstance(value.get('limit'), dict) else {}
        context = _pos_int(limit.get('context') or limit.get('input'))
        output = _pos_int(limit.get('output'))
        if not context or not output:
            continue
        modalities = _modalities((value.get('modalities') or {}).get('input')
                                 if isinstance(value.get('modalities'), dict) else None)
        if 'text' not in modalities:
            continue
        models.append({
            'id': model_id,
            'wire_id': model_id,
            'name': key if key == model_id else f'{key} (API: {model_id})',
            'type': 'chat',
            'context_length': context,
            'max_tokens': output,
            'modalities': modalities,
            'tool_call': value.get('tool_call') is not False,
            'reasoning': (model_id == 'deepseek-v41-flash'
                          or value.get('reasoning') is True
                          or (isinstance(value.get('capabilities'), dict)
                              and value['capabilities'].get('reasoning') is True)),
            'attachment': any(x != 'text' for x in modalities),
        })
    return models


def source_path() -> Path | None:
    """桌面端 `opencode.json` 的绝对路径；找不到返回 None。"""
    override = str(os.environ.get('DUMATE_OPENCODE_CONFIG') or '').strip()
    if override:
        path = Path(override).expanduser()
        return path if path.is_file() else None

    root = str(os.environ.get('DUMATE_USER_DATA_DIR') or '').strip()
    base = Path(root) if root else Path(os.environ.get('APPDATA') or '') \
        / 'qianfan-desktop-app'
    xdg = base / 'qianfan_desk_xdg'
    tail = Path('config') / 'opencode' / 'opencode.json'

    account = ''
    try:
        auth = json.loads((base / 'auth.json').read_text(encoding='utf-8'))
        profiles = auth.get('accountProfiles') if isinstance(auth, dict) else None
        profiles = profiles if isinstance(profiles, list) else []
        active = next((p for p in profiles
                       if isinstance(p, dict)
                       and p.get('profileId') == auth.get('activeProfileId')),
                      profiles[0] if len(profiles) == 1 else None)
        if isinstance(active, dict):
            account = str(active.get('bceAccountId') or '').strip()
    except (OSError, ValueError):
        account = ''

    if account:
        path = xdg / account / tail
        return path if path.is_file() else None
    try:
        dirs = [item for item in xdg.iterdir() if item.is_dir()]
    except OSError:
        return None
    # 多个账号目录时**不猜**（社区同口径）：只有唯一一个才自动选。
    if len(dirs) != 1:
        return None
    path = dirs[0] / tail
    return path if path.is_file() else None


def read_desktop_models() -> list[dict]:
    """读一次桌面端模型清单；文件不存在 / 太大 / 格式坏都返回空表。"""
    path = source_path()
    if path is None:
        return []
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            return []
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    return import_models(data)


def default_models() -> list[dict]:
    """包内快照（`catalog.json`）→ 统一内部条目。"""
    out: list[dict] = []
    for item in C.CATALOG:
        model_id = str(item.get('id') or '').strip()
        if not model_id:
            continue
        modalities = item.get('input')
        if not isinstance(modalities, list) or not modalities:
            modalities = ['text']
        modalities = [str(x) for x in modalities if str(x).strip()]
        if 'text' not in modalities:
            modalities.insert(0, 'text')
        out.append({
            'id': model_id,
            'wire_id': model_id,
            'name': str(item.get('name') or model_id),
            'type': 'chat',
            'context_length': _pos_int(item.get('context')),
            'max_tokens': _pos_int(item.get('output')),
            'modalities': modalities,
            'tool_call': item.get('tool_call') is not False,
            'reasoning': bool(item.get('reasoning')),
            'attachment': bool(item.get('attachment')),
        })
    return out


def normalize(items: object) -> list[dict]:
    """缓存 / 外部传入的条目 → 只保留需要的字段（防篡改注入）。"""
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get('id') or '').strip()
        if not model_id:
            continue
        modalities = _modalities(item.get('modalities'))
        out.append({
            'id': model_id,
            'wire_id': str(item.get('wire_id') or model_id),
            'name': str(item.get('name') or model_id),
            'type': 'chat',
            'context_length': _pos_int(item.get('context_length')),
            'max_tokens': _pos_int(item.get('max_tokens')),
            'modalities': modalities,
            'tool_call': item.get('tool_call') is not False,
            'reasoning': bool(item.get('reasoning')),
            'attachment': bool(item.get('attachment')),
        })
    return out


__all__ = ['import_models', 'source_path', 'read_desktop_models',
           'default_models', 'normalize']
