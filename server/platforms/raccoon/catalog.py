"""小浣熊模型目录的归一化与离线兜底。

在线目录由 `client.parse_catalog` 解析成内部结构后落库（`platforms.raccoon.models`）；
本模块负责：

  · 把缓存读回来的条目重新归一（旧版本缓存 / 手工改过的值都能容忍）；
  · 在线目录拿不到时退回 `constants.DEFAULT_MODELS`（新部署、目录接口失败）。
"""
from __future__ import annotations

from . import client
from . import constants as C


def _num(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _int(value: object) -> int:
    number = _num(value)
    return int(number) if number is not None else 0


def normalize(models: object) -> list[dict]:
    """把任意来源的条目归一成内部结构（坏条目丢掉、按 id 去重）。"""
    out: list[dict] = []
    seen: set[str] = set()
    for raw in models if isinstance(models, list) else []:
        if not isinstance(raw, dict):
            continue
        model_id = str(raw.get('id') or raw.get('name')
                       or raw.get('model_name') or '').strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        tags = raw.get('tags')
        tags = [str(tag) for tag in tags if str(tag or '').strip()] \
            if isinstance(tags, list) else []
        modalities = raw.get('modalities')
        if not isinstance(modalities, list) or not modalities:
            modalities = client._modalities_of(model_id, tags)
        visible = raw.get('visible')
        out.append({
            'id': model_id,
            'name': str(raw.get('name') or model_id),
            'description': str(raw.get('description') or ''),
            'context_length': _int(raw.get('context_length')
                                   or raw.get('context_window')),
            'max_tokens': _int(raw.get('max_tokens')),
            'tags': tags,
            'visible': True if visible is None else bool(visible),
            'reasoning': bool(raw.get('reasoning')),
            'modalities': [str(item) for item in modalities],
            'tool_call': bool(raw.get('tool_call', True)),
            'billing_multiplier': _num(raw.get('billing_multiplier')),
            'billing_effective_multiplier': _num(
                raw.get('billing_effective_multiplier')),
            'billing_status': str(raw.get('billing_status') or ''),
            'free': bool(raw.get('free')),
            'thinking': raw.get('thinking')
            if isinstance(raw.get('thinking'), dict) else {},
        })
    return out


def default_models() -> list[dict]:
    """内置兜底目录（在线目录可用后会被覆盖）。

    `DEFAULT_MODELS` 用的是内部结构（`id` + 展示名 `name`），而
    `parse_catalog` 的输入是上游结构（`name` = 模型 id，`display_description`
    = 展示名）。这里做一次翻译，避免把展示名当成模型 id。
    """
    translated: list[dict] = []
    for model in C.DEFAULT_MODELS:
        entry = dict(model)
        entry['model_name'] = entry.get('id') or entry.get('model_name')
        entry['display_description'] = (entry.get('name')
                                        or entry['model_name'])
        translated.append(entry)
    return client.parse_catalog({
        'categories': [{'models': translated}],
    })
