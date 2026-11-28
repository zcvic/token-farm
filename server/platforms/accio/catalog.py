"""Accio 模型目录归一。

线上目录来自 `POST /api/llm/config/v2`（请求体带 `token` 与团队 / 区域头）：

    → {"success": true, "data": [{"modelList": [...]}, ...]}

条目字段（参考实现口径，逐项见 `deploy/accio-ref.txt`）：

  · `modelCode`         —— 上游真正认的模型名（形如 `1Drift-N8kP2Q7wR5mX`，
                           **不是**人类可读的展示名）；
  · `modelDisplayName`  —— 展示名，可能带倍率后缀（`名称 · x1.5`）；
  · `visible`           —— **必须显式为 `true`**，其余（缺失 / `false`）一律剔除；
  · `multimodal`        —— 是否支持图片输入（本项目本轮只声明模态，不上行图片）；
  · `effectiveMultiple` —— 积分消耗倍率，目录按它升序（便宜在前）；
  · `contextWindow` / `maxTokens`；
  · `reasoningEfforts` / `defaultReasoningEffort` —— 思考档位（见 `_efforts_of`）。

**不内置离线快照**：`modelCode` 无法凭名字维护，某区没有可用账号时目录就是
空表，**不要**把另一个区域的目录借过来。

## 对外 id 的两层

用户看到 / 回传的是**展示名归一后的 slug**（`deepseek-v4-1-flash`），上行要用
上游的 `modelCode`。本模块同时产出两者：`slug` 是对外裸模型名，`id` 是
`modelCode`。适配器负责反查（见 `adapter.upstream_model`）。

展示名里的倍率后缀（`· x1.5`）与裸「免费」标记会污染 slug，所以先剥掉第一个
`·` 之后的内容；slug 为空时退回 `modelCode` 的小写形态，绝不留空。
"""
from __future__ import annotations

import math
import re
from typing import Any

from . import constants as C
from .protocol import normalize_realm

# slug 只保留 URL / 模型名安全的字符；其余（中文、空格、标点）折叠成 `-`。
_SLUG_BAD = re.compile(r'[^0-9a-z._-]+')
_SLUG_DASHES = re.compile(r'-{2,}')
# 展示名里 `·` 之后是倍率 / 备注（`名称 · x1.5`、`名称 · 免费`）。
_NAME_SEP = '·'
# 只剥「倍率后缀」：`名称 · x1.5` / `名称·x15`。其它备注（`· 免费`）保留。
_MULTIPLIER_SUFFIX = re.compile(r'\s*·\s*x\s*\d+(?:\.\d+)?\s*$', re.I)


def _num(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _int(value: object) -> int:
    number = _num(value)
    return max(0, int(number)) if number is not None else 0


def slugify(name: object, fallback: str = '') -> str:
    """展示名 → 对外裸模型名（slug）。

    先剥第一个 `·` 之后的内容（倍率 / 免费标记），再做字符折叠。结果为空时
    退回 `fallback`（通常是 `modelCode`）的小写安全形态；两者都空就返回空串，
    由调用方丢弃该条目。
    """
    text = str(name or '').split(_NAME_SEP, 1)[0].strip().lower()
    text = _SLUG_BAD.sub('-', text)
    text = _SLUG_DASHES.sub('-', text).strip('-._')
    if text:
        return text
    scratch = str(fallback or '').strip().lower()
    scratch = _SLUG_BAD.sub('-', scratch)
    scratch = _SLUG_DASHES.sub('-', scratch).strip('-._')
    return scratch


def _efforts_of(raw: dict) -> tuple[list[str], str | None]:
    """思考档位：`(efforts, default_effort)`。

    实测三态（缺一不可）：

      1. `reasoningEfforts` 可能是 `null`（多数条目）—— 与空数组同义，「无档位」；
      2. 元素是字符串（`low` / `medium` / `high` / …），顺序由服务端决定（由弱到
         强），**原样保留**；
      3. `reasoningEfforts` 为 `null` 但 `defaultReasoningEffort` 有值 ⇒ 它是
         **一个可用的单档**（实测发过去服务端接受）。

    `defaultReasoningEffort` 只有落在档位集合里才声明（否则选择器会显示一个
    不存在的项）。
    """
    raw_efforts = raw.get('reasoningEfforts')
    efforts: list[str] = []
    if isinstance(raw_efforts, list):
        for item in raw_efforts:
            text = str(item or '').strip() if isinstance(item, str) else ''
            if text and text not in efforts:
                efforts.append(text)
    default = raw.get('defaultReasoningEffort')
    default = str(default or '').strip() if isinstance(default, str) else ''
    if not efforts and default:
        efforts = [default]
    if default and default not in efforts:
        default = ''
    return efforts, (default or None)


def _modalities_of(raw: dict) -> list[str]:
    """输入模态：`multimodal === true` 才声明图片，否则**显式**只声明文本。

    ⚠️ 不能省略：`multimodal:false` 的模型如果只写「未知」，客户端会把图片
    发给一个必然拒绝的模型。目录里逐模型声明 `['text']` 才是如实登记。
    """
    return ['text', 'image'] if raw.get('multimodal') is True else ['text']


def _label_of(name: str, multiple: float | None) -> str:
    """展示名 + 倍率后缀（0 与 1 都拼，非有限值不拼）。

    展示名通常**已经**带了一个 `· x1.5` 后缀（上游下发），先剥掉再按
    `effectiveMultiple` 重拼，否则会出现 `… · x1.5 · x1.5`。其它备注（`· 免费`）
    不动。
    """
    base = _MULTIPLIER_SUFFIX.sub('', str(name or '')).strip()
    base = base or str(name or '').strip()
    if multiple is None:
        return base
    text = f'{multiple:g}'
    return f'{base} {_NAME_SEP} x{text}'


def normalize_one(raw: object, realm: object = C.DEFAULT_REALM) -> dict[str, Any] | None:
    """一条远端目录 → 内部条目；不合格（不可见 / 无 code）返回 None。"""
    if not isinstance(raw, dict):
        return None
    if raw.get('visible') is not True:
        return None
    code = str(raw.get('modelCode') or raw.get('model_code') or '').strip()
    if not code:
        return None
    region = normalize_realm(realm)
    display = str(raw.get('modelDisplayName')
                  or raw.get('model_display_name') or '').strip()
    name = display or code
    slug = slugify(display, code)
    if not slug:
        return None
    efforts, default_effort = _efforts_of(raw)
    multiple = _num(raw.get('effectiveMultiple'))
    modalities = _modalities_of(raw)
    return {
        'id': code,
        'slug': slug,
        'name': name,
        'label': _label_of(name, multiple),
        'context_length': _int(raw.get('contextWindow')),
        'max_tokens': _int(raw.get('maxTokens')),
        'modalities': modalities,
        'reasoning': bool(efforts),
        'efforts': efforts,
        'default_effort': default_effort,
        'multiple': multiple,
        'tool_call': True,
        'attachment': 'image' in modalities,
        'credit_level': multiple if multiple is not None else '',
        'type': 'chat',
        'realm': region,
    }


def _is_internal(raw: dict) -> bool:
    """缓存里已经是本模块产出的内部条目（有 `slug` + `id`）。"""
    return bool(str(raw.get('slug') or '').strip()
                and str(raw.get('id') or '').strip())


def _sort_key(item: dict) -> tuple[int, float, int]:
    multiple = _num(item.get('multiple'))
    # 无倍率的排最后；有倍率的按升序。第三位是原始下标之外的可比兜底。
    return (0 if multiple is not None else 1,
            multiple if multiple is not None else 0.0,
            0)


def _finish(items: list[dict], realm: object) -> list[dict]:
    """按倍率升序 + 按 slug 去重（先出现者胜出）。"""
    region = normalize_realm(realm)
    out: list[dict] = []
    seen: set[str] = set()
    for item in sorted(items, key=_sort_key):
        slug = str(item.get('slug') or '').strip().lower()
        if not slug or slug in seen:
            continue
        seen.add(slug)
        item = dict(item)
        item['realm'] = region
        out.append(item)
    return out


def normalize(models: object, *, realm: object = C.DEFAULT_REALM) -> list[dict]:
    """内部条目列表（缓存回读）→ 重新校验 + 排序 + 去重。"""
    out: list[dict] = []
    for raw in models if isinstance(models, list) else []:
        if not isinstance(raw, dict):
            continue
        if _is_internal(raw):
            out.append(dict(raw))
            continue
        item = normalize_one(raw, realm)
        if item is not None:
            out.append(item)
    return _finish(out, realm)


def parse(payload: object, *, realm: object = C.DEFAULT_REALM) -> list[dict]:
    """解析目录响应（`data[].modelList[]`）；不是该形状返回空表。"""
    if not isinstance(payload, dict):
        return []
    data = payload.get('data')
    groups = data if isinstance(data, list) else []
    rows: list[dict] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        model_list = group.get('modelList')
        if not isinstance(model_list, list):
            continue
        for raw in model_list:
            item = normalize_one(raw, realm)
            if item is not None:
                rows.append(item)
    return _finish(rows, realm)


def default_models() -> list[dict]:
    """没有离线快照，刻意返回空表（理由见模块 docstring）。"""
    return []
