"""AutoClaw 模型目录归一。

线上目录来自客户端启动时读的模型配置：

    GET <推理面>/autoclaw-proxy/proxy/autoclaw-model-config
    → {"models": [...]} 或 {"data": {"models": [...]}}

字段（参考实现口径）：`id` / `name` / `tooltip` / `contextWindow` /
`maxTokens` / `reasoning` / `input[]` / `creditConsumptionLevel`。这里把
驼峰、下划线与几种旧写法都归一到本仓库的目录形状，供适配器转成对外的
`/v1/models` 条目。

**不内置离线快照**：模型上下线以实时目录为准，写死一份名单会让客户端照着
已经下线的名字发请求。某区没有可用账号时目录就是空表，**不要**把另一个区域
的目录借过来——那会让用户以为模型在本区可用。
"""
from __future__ import annotations

from typing import Any

from . import constants as C
from .protocol import normalize_realm


def _int(value: object) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return max(0, int(float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _truthy(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        for key in ('supported', 'enabled', 'value'):
            if key in value:
                return _truthy(value[key], default)
        return default
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'yes', 'on')
    if isinstance(value, (int, float)):
        return value != 0
    return default


def _modalities(raw: dict) -> list[str]:
    """输入模态；`input` 兼容 list / dict / 单个字符串三种写法。"""
    values: list[object] = []
    for key in ('input', 'modalities', 'modality'):
        node = raw.get(key)
        if isinstance(node, dict):
            if isinstance(node.get('input'), list):
                node = node['input']
            else:
                node = [name for name, enabled in node.items() if enabled]
        if isinstance(node, str):
            node = [node]
        if isinstance(node, list):
            values.extend(node)

    out = ['text']
    for value in values:
        text = str(value or '').strip().lower()
        if not text or text in out:
            continue
        if text in ('vision', 'image_url', 'image-input'):
            text = 'image'
        if text not in out:
            out.append(text)
    return out


def _item(raw: object, realm: str) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    model_id = str(raw.get('id') or raw.get('model') or raw.get('model_id')
                   or '').strip()
    if not model_id:
        return None

    context = (raw.get('contextWindow') or raw.get('context_window')
               or raw.get('context_length') or raw.get('context'))
    output = (raw.get('maxTokens') or raw.get('max_tokens')
              or raw.get('max_output_tokens') or raw.get('max_completion_tokens')
              or raw.get('output'))
    modalities = _modalities(raw)
    efforts = raw.get('efforts')
    if not isinstance(efforts, list):
        efforts = raw.get('effort_options')
    if not isinstance(efforts, list):
        efforts = raw.get('reasoning_efforts')
    if not isinstance(efforts, list):
        efforts = []
    efforts = [str(x) for x in efforts if str(x or '').strip()]

    credit = raw.get('creditConsumptionLevel')
    if credit is None:
        credit = raw.get('credit_consumption_level')
    if credit is None:
        credit = raw.get('credit_level')

    return {
        'id': model_id,
        'name': str(raw.get('name') or raw.get('display_name') or model_id),
        'tooltip': str(raw.get('tooltip') or raw.get('description')
                       or raw.get('desc') or ''),
        'context_length': _int(context),
        'max_tokens': _int(output),
        'modalities': modalities,
        'reasoning': _truthy(raw.get('reasoning', raw.get('thinking')), False),
        'efforts': efforts,
        'tool_call': _truthy(raw.get('tool_call', raw.get('tools')), True),
        'attachment': _truthy(raw.get('attachment'), False)
                      or 'image' in modalities,
        'credit_level': credit if credit is not None else '',
        'type': 'chat',
        'realm': realm,
    }


def normalize(models: object, *, realm: object = C.DEFAULT_REALM) -> list[dict]:
    """模型条目列表 → 内部目录形状（按小写 id 去重，先出现者胜出）。"""
    region = normalize_realm(realm)
    out: list[dict] = []
    seen: set[str] = set()
    for raw in models if isinstance(models, list) else []:
        item = _item(raw, region)
        if item is None:
            continue
        key = item['id'].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def parse(payload: object, *, realm: object = C.DEFAULT_REALM) -> list[dict]:
    """解析模型配置响应；不是该形状返回空表（调用方保留上一份真实目录）。"""
    if isinstance(payload, dict):
        models = payload.get('models')
        if not isinstance(models, list):
            data = payload.get('data')
            if isinstance(data, dict):
                models = data.get('models')
            elif isinstance(data, list):
                models = data
        return normalize(models, realm=realm)
    if isinstance(payload, list):
        return normalize(payload, realm=realm)
    return []


def default_models() -> list[dict]:
    """没有离线快照，刻意返回空表（理由见模块 docstring）。"""
    return []
