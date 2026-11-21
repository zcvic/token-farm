"""Kilo 模型目录。

`ai-gateway.kilo.ai/api/v1/models` 免鉴权可读（OpenRouter schema），所以在线
拉取不需要账号；拿不到时退回 `constants.DEFAULT_MODELS`（只含自动路由与免费档）。
"""
from __future__ import annotations

from . import constants as C


def normalize(models: object) -> list[dict]:
    """把任意来源的模型条目归一成内部结构（坏条目丢掉）。"""
    out: list[dict] = []
    seen: set[str] = set()
    for raw in models if isinstance(models, list) else []:
        if not isinstance(raw, dict):
            continue
        model_id = str(raw.get('id') or raw.get('model') or '').strip()
        if not model_id or model_id.lower() in seen:
            continue
        seen.add(model_id.lower())
        modalities = raw.get('modalities')
        if not isinstance(modalities, list) or not modalities:
            modalities = ['text']
        out.append({
            'id': model_id,
            'name': str(raw.get('name') or model_id),
            'context_length': _int(raw.get('context_length')),
            'max_tokens': _int(raw.get('max_tokens')),
            'modalities': [str(x) for x in modalities],
            'reasoning': bool(raw.get('reasoning')),
            'tool_call': bool(raw.get('tool_call', True)),
            'free': bool(raw.get('free')),
            'type': 'chat',
        })
    return out


def _int(value: object) -> int:
    try:
        return int(value or 0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def default_models() -> list[dict]:
    return normalize([dict(m) for m in C.DEFAULT_MODELS])
