"""MiniMax Code 模型目录。

线上目录来自客户端启动时读的模型配置：

  GET <llm>/mavis/api/v1/models?region=<cn|en>&buildEnv=prod
  → {providers: [{providerId: 'minimax', config: {models: {...}, model_order: [...]}}]}

拿不到时用 `constants.DEFAULT_MODELS`（离线快照）。`limit.output` 常被客户端
写得比端点真正允许的小，M3 家族统一用实测值覆盖（见 `constants.OUTPUT_CAPS`）。
"""
from __future__ import annotations

from typing import Any

from . import constants as C


def _num(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _inputs(raw: dict) -> list[str]:
    modalities = raw.get('modalities')
    values: list[object] = []
    if isinstance(modalities, dict):
        values = modalities.get('input') if isinstance(modalities.get('input'), list) else []
    elif isinstance(modalities, list):
        values = modalities
    out = ['text']
    for item in values:
        text = str(item or '').strip().lower()
        if text and text != 'text' and text not in out:
            out.append(text)
    if raw.get('attachment') is True and 'image' not in out:
        out.append('image')
    return out


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
        context = _num(raw.get('context_length')) or _num(raw.get('context')) or 0
        output = _num(raw.get('max_tokens')) or _num(raw.get('output')) or 0
        context = int(C.CONTEXT_CAPS.get(model_id, context) or 0)
        output = int(C.OUTPUT_CAPS.get(model_id, output) or 0)
        efforts = raw.get('efforts')
        if not isinstance(efforts, list):
            efforts = []
        out.append({
            'id': model_id,
            'name': str(raw.get('name') or model_id),
            'context_length': context,
            'max_tokens': output,
            'modalities': _inputs(raw),
            'reasoning': bool(raw.get('reasoning', True)),
            'efforts': [str(x) for x in efforts if str(x or '').strip()],
            'tool_call': bool(raw.get('tool_call', True)),
            'attachment': bool(raw.get('attachment', False)),
            'type': 'chat',
        })
    return out


def parse_models(payload: object) -> list[dict]:
    """解析在线模型配置；不是该形状返回空表（调用方降级）。"""
    if not isinstance(payload, dict):
        return []
    providers = payload.get('providers')
    if not isinstance(providers, list):
        return []
    provider = next((p for p in providers if isinstance(p, dict)
                     and str(p.get('providerId') or '') == 'minimax'), None)
    if provider is None:
        return []
    config = provider.get('config') if isinstance(provider.get('config'), dict) else {}
    models = config.get('models') if isinstance(config.get('models'), dict) else {}
    order = config.get('model_order') if isinstance(config.get('model_order'), list) else []
    ids = [str(x) for x in order if str(x or '') in models]
    ids += [str(x) for x in models if str(x) not in ids]

    out: list[dict] = []
    for model_id in ids:
        raw = models.get(model_id)
        if not isinstance(raw, dict):
            continue
        thinking = raw.get('thinking_config') if isinstance(raw.get('thinking_config'), dict) else {}
        mode = str(thinking.get('mode') or '')
        efforts = raw.get('efforts')
        if not isinstance(efforts, list):
            detail = raw.get('thinking') if isinstance(raw.get('thinking'), dict) else {}
            efforts = (detail.get('effortOptions')
                       if isinstance(detail.get('effortOptions'), list)
                       else detail.get('effort_options')
                       if isinstance(detail.get('effort_options'), list) else [])
        efforts = [str(x) for x in efforts
                   if isinstance(x, str) and x not in ('', 'default', 'none')]
        limit = raw.get('limit') if isinstance(raw.get('limit'), dict) else {}
        item: dict[str, Any] = {
            'id': model_id,
            'name': str(raw.get('name') or model_id),
            'context_length': _num(limit.get('context')) or 0,
            'max_tokens': _num(limit.get('output')) or 0,
            'modalities': _inputs(raw),
            'reasoning': mode != 'forced_off' and raw.get('reasoning') is not False,
            'efforts': efforts,
            'tool_call': True,
            'attachment': bool(raw.get('attachment')),
            'type': 'chat',
        }
        out.append(item)
    return normalize(out)


def default_models() -> list[dict]:
    return normalize([dict(m) for m in C.DEFAULT_MODELS])
