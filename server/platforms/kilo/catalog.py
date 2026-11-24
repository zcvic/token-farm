"""Kilo 模型目录。

`ai-gateway.kilo.ai/api/v1/models` 免鉴权可读（OpenRouter schema），所以在线
拉取不需要账号；拿不到时退回 `constants.DEFAULT_MODELS`（只含自动路由与免费档）。

上游 2026-11 起用 `opencode.reasoning_options` 描述推理档位。旧的 `variants` 不取：
它的语义是「变体名 → 参数覆盖」，与「档位列表」不是一回事。
"""
from __future__ import annotations

from . import constants as C

# 上游 `ReasoningEffortSchema` 的合法档位（与 Kilo-Org/cloud 的 schema-types 一致）。
REASONING_EFFORTS = ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max')


def _effort_option(entry: object) -> dict | None:
    """认一条 `reasoning_options` 条目：`{type:'toggle'}` / `{type:'effort', values:[…]}`。"""
    if not isinstance(entry, dict):
        return None
    kind = str(entry.get('type') or '').strip()
    if kind == 'toggle':
        return {'type': 'toggle'}
    if kind == 'effort':
        values = entry.get('values')
        return {'type': 'effort', 'values': values if isinstance(values, list) else []}
    return None


def reasoning_efforts(options: object) -> list[str]:
    """把 `opencode.reasoning_options` 翻成档位列表（对齐上游 `legacyVariants`）。

    三条规则，逐条对应上游实现：

      · `[]`（推理常开）→ `['high']`：上游给一个 `thinking` 变体，档位就是 high；
      · 只有 toggle → `['none', 'high']`：`instant` / `thinking` 两档；
      · 有 effort 档位 → 去重后的档位；带 toggle 且没显式给 `none` 时补一档
        `none`（toggle 的语义就是「关掉」）。

    认不出的条目（如 `budget_tokens`）**逐条丢弃**，不让整份清单失效。
    """
    if not isinstance(options, list):
        return []
    parsed = [opt for opt in (_effort_option(e) for e in options) if opt]
    toggle = any(opt['type'] == 'toggle' for opt in parsed)
    efforts: list[str] = []
    for opt in parsed:
        if opt['type'] != 'effort':
            continue
        for value in opt['values']:
            if isinstance(value, str) and value in REASONING_EFFORTS \
                    and value not in efforts:
                efforts.append(value)
    if not parsed:
        # `[]` 与「全是认不出的条目」都是「没有可用选项」：前者推理常开，
        # 后者按上游口径同样退回单一 thinking 档。
        return ['high'] if isinstance(options, list) and not options else []
    if not efforts:
        return ['none', 'high'] if toggle else []
    if toggle and 'none' not in efforts:
        return ['none', *efforts]
    return efforts


def _efforts_of(raw: dict) -> list[str]:
    """模型条目上的档位：优先已归一的 `efforts`，其次读原始 `reasoning_options`。"""
    efforts = raw.get('efforts')
    if isinstance(efforts, list):
        return [str(x) for x in efforts if str(x or '').strip()]
    opencode = raw.get('opencode')
    if isinstance(opencode, dict):
        return reasoning_efforts(opencode.get('reasoning_options'))
    return []


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
        efforts = _efforts_of(raw)
        out.append({
            'id': model_id,
            'name': str(raw.get('name') or model_id),
            'context_length': _int(raw.get('context_length')),
            'max_tokens': _int(raw.get('max_tokens')),
            'modalities': [str(x) for x in modalities],
            'reasoning': bool(raw.get('reasoning')) or bool(efforts),
            'efforts': efforts,
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
