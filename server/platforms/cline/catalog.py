"""Cline 模型目录。

两个免鉴权接口，各补一块：

  · `GET /models` —— 全量目录（含付费档、`:batch` 变体）；
  · `GET /ai/cline/recommended-models` 的 `free` 分组 —— 官方标记的免费档。

`recommended` 里的免费条目优先（它们的 `free` 标记是权威的），`/models` 补上
其余条目。合并后按 id 去重，免费档排在前面：账号没余额时，用户最该先看到
能用的那几条。

拿不到时退回 `constants.DEFAULT_MODELS`（只含免费档）。
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


def merge(free: object, live: object) -> list[dict]:
    """合并「免费档」与「全量目录」：免费在前，同 id 以免费档的标记为准。"""
    merged: dict[str, dict] = {}
    order: list[str] = []
    for group, is_free in ((free, True), (live, False)):
        for raw in normalize(group):
            key = raw['id'].lower()
            if key in merged:
                continue
            if is_free:
                raw['free'] = True
            merged[key] = raw
            order.append(key)
    rows = [merged[key] for key in order]
    rows.sort(key=lambda m: 0 if m.get('free') else 1)
    return rows


def default_models() -> list[dict]:
    return normalize([dict(m) for m in C.DEFAULT_MODELS])
