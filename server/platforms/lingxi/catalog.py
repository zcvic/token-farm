"""灵犀模型目录：在线 `/sessions/plans` → 内置快照。

上游目录是「模式 + 模型」混排（`plans` 在前、`models` 在后），字段名与官网
展示并不完全一致，归一规则见 `from_raw`：

  · `key` 是上游模型 id，直接作为 `model_id` 发送；
  · `max_new_tokens` 才是输出上限（各模型 131072 ~ 1048576 不等）；
  · **有 `reasoning_efforts` 才算思考模型**，没有就是没有，不猜；
  · 倍率混用 ASCII `~` / 全角 `～` / 日文 `〜`，统一成 ASCII；
  · 徽标上游叫 `tag`，也认 `badge` / `label`。

`default_models()` 是未登录 / 接口失败时的兜底，字段逐条抄自一次真实响应。
"""
from __future__ import annotations

import re
from typing import Any

from . import constants as C

_TILDE = re.compile(r'[\uFF5E\u301C\u223C]')


def _num(value: object, fallback: int) -> int:
    if isinstance(value, bool) or value is None:
        return fallback
    try:
        number = int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    return number if number > 0 else fallback


def _text(value: object) -> str:
    return str(value).strip() if isinstance(value, str) else ''


def _efforts(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value
            if isinstance(item, str) and item.strip()]


def from_raw(raw: object) -> dict | None:
    """上游 `plans[]` / `models[]` 的一项 → 内部模型条目。"""
    if not isinstance(raw, dict):
        return None
    model_id = _text(raw.get('key'))
    if not model_id:
        return None
    efforts = _efforts(raw.get('reasoning_efforts'))
    badge = (_text(raw.get('badge')) or _text(raw.get('tag'))
             or _text(raw.get('label')))
    description = _text(raw.get('desc')) or _text(raw.get('intro'))
    multiplier = _TILDE.sub('~', _text(raw.get('multiplier')))
    return {
        'id': model_id,
        'name': _text(raw.get('name')) or model_id,
        'context_length': _num(raw.get('max_context_tokens'), 1_048_576),
        'max_tokens': _num(raw.get('max_new_tokens'), 131_072),
        'multiplier': multiplier,
        # 有档位声明才是思考模型；没有声明表示「确实没有」，不是「未知」。
        'reasoning': bool(efforts),
        'vision': raw.get('support_multimodal') is True,
        'reasoning_efforts': efforts,
        'badge': badge,
        'description': description,
    }


def normalize(models: object) -> list[dict]:
    """任意来源的模型条目 → 内部结构（坏条目丢掉、按 id 去重）。"""
    out: list[dict] = []
    seen: set[str] = set()
    for raw in models if isinstance(models, list) else []:
        entry = raw if isinstance(raw, dict) else None
        if entry is None:
            continue
        if 'context_length' not in entry and 'max_context_tokens' in entry:
            entry = from_raw(entry)
            if entry is None:
                continue
        model_id = _text(entry.get('id') or entry.get('key'))
        if not model_id or model_id.lower() in seen:
            continue
        seen.add(model_id.lower())
        out.append({
            'id': model_id,
            'name': _text(entry.get('name')) or model_id,
            'context_length': _num(entry.get('context_length'), 1_048_576),
            'max_tokens': _num(entry.get('max_tokens'), 131_072),
            'multiplier': _text(entry.get('multiplier')),
            'reasoning': bool(entry.get('reasoning')),
            'vision': bool(entry.get('vision')),
            'reasoning_efforts': _efforts(entry.get('reasoning_efforts')),
            'badge': _text(entry.get('badge')),
            'description': _text(entry.get('description')),
        })
    return out


def parse_plans(payload: object) -> list[dict]:
    """`GET /sessions/plans` → 模型列表；空列表表示目录不可用。"""
    if not isinstance(payload, dict):
        return []
    data = payload.get('data')
    if not isinstance(data, dict):
        return []
    out: list[dict] = []
    for field in ('plans', 'models'):
        items = data.get(field)
        if not isinstance(items, list):
            continue
        for item in items:
            entry = from_raw(item)
            if entry is not None:
                out.append(entry)
    return normalize(out)


def default_models() -> list[dict]:
    return normalize([dict(m) for m in C.DEFAULT_MODELS])


def model_entry(models: list[dict], model_id: object) -> dict | None:
    text = str(model_id or '').strip().lower()
    for item in models:
        if str(item.get('id') or '').strip().lower() == text:
            return item
    return None


def to_public(model: dict, platform: str) -> dict:
    """内部条目 → `/v1/models` 形状（id 带平台前缀）。"""
    model_id = str(model.get('id') or '')
    modalities = ['text', 'image'] if model.get('vision') else ['text']
    return {
        'id': f'{platform}/{model_id}',
        'object': 'model',
        'created': 0,
        'owned_by': platform,
        'name': str(model.get('name') or model_id),
        'platform': platform,
        'realm': 'cn',
        'context_window': int(model.get('context_length') or 0),
        'max_tokens': int(model.get('max_tokens') or 0),
        'supports_reasoning': bool(model.get('reasoning')),
        'reasoning_efforts': list(model.get('reasoning_efforts') or []),
        'modalities': {'input': modalities},
        'model_name': model_id,
        'tool_call': True,
        'attachment': bool(model.get('vision')),
        'credit_ratio': str(model.get('multiplier') or '') or None,
        'badge': str(model.get('badge') or ''),
        'description': str(model.get('description') or ''),
    }


def describe(model: dict) -> dict[str, Any]:
    """面板 / 日志用的可读元信息（倍率与徽标只在目录页展示）。"""
    return {
        'id': str(model.get('id') or ''),
        'name': str(model.get('name') or ''),
        'multiplier': str(model.get('multiplier') or ''),
        'badge': str(model.get('badge') or ''),
        'reasoning_efforts': list(model.get('reasoning_efforts') or []),
        'description': str(model.get('description') or ''),
    }