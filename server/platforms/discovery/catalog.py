"""Discovery 模型目录。

上游只暴露一个聚合模型（`auto` 之类），`GET /v1/models` 能给出准确名字时用它，
拿不到时退回 `constants.DEFAULT_MODELS`（与参考实现 `knownDiscoveryModels`
同源）。两级降级，任何一级失败都不抛。
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
            'context_length': int(raw.get('context_length') or 0),
            'max_tokens': int(raw.get('max_tokens') or 0),
            'modalities': [str(x) for x in modalities],
            'reasoning': bool(raw.get('reasoning')),
            'type': 'chat',
        })
    return out


def default_models() -> list[dict]:
    return normalize([dict(m) for m in C.DEFAULT_MODELS])
