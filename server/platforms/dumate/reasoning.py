"""DuMate 的思考档位归一（对齐上游 `src/dumate-reasoning.js`）。

## 为什么需要这一层

云端网关只对**同一后端**的模型认 `none / low / high / max` 四档，且 `none`
必须**显式关掉**开关才会真的不思考——只删 `reasoning_effort` 不够（上游会按
默认档继续思考，思考预算照样吃掉输出上限）。其它后端（Qoder 是
`minimal…max` 五档、千问另有 `thinking_config`）的合法集合不同，一律不注入，
保持各自现状。

## 三条边界

1. **只对白名单模型生效**。档位语义挂在**后端**上，不是产品别名上；白名单里
   五个 id 都是 `deepseek-v4.1-flash` 那一族。新加条目先确认真实后端一致。
2. **`low` 归一到 `high`**。百度渠道实测两档同强度，统一成 `high` 让跨渠道
   语义一致。
3. **不碰 messages / tools / 其它字段**。只增删 `reasoning_effort` /
   `thinking` / `enable_thinking` 三个键，其余原样——客户端可能在同一份 body
   里带工具定义，任何深拷贝或重排都可能改变签名。
"""
from __future__ import annotations

from typing import Any

# 档位语义按后端说，键一律小写（`norm_model` 会小写入参）。
SAME_BACKEND = frozenset({
    'deepseek-v41-flash',
    'model-text',
    'dm-auto-model/text.l0',
    'dm-auto-model/mm.l0',
    'dm-auto-model/mm.l2',
})

ALLOWED = frozenset({'none', 'low', 'high', 'max'})

# 对外暴露的档位：`low` 归一成 `high`，所以不再单列，免得客户端选了 `low`
# 却得到 `high` 的强度而以为没生效。
EFFORTS = ('none', 'high', 'max')


def norm_model(name: object) -> str:
    """剥掉 `[1M]` 标记、去空白、转小写。"""
    text = str(name or '').strip()
    if text.lower().endswith('[1m]'):
        text = text[:-4].strip()
    return text.lower()


def supports_effort(model: object) -> bool:
    """该模型是否走 `deepseek-v4.1-flash` 后端（档位语义已知的那一族）。"""
    return norm_model(model) in SAME_BACKEND


def efforts_of(model: object) -> list[str]:
    """该模型对外可选的档位；空数组 = 不显示档位选择器。"""
    return list(EFFORTS) if supports_effort(model) else []


def apply(body: dict, model: object) -> bool:
    """就地归一 `body` 的思考档位，返回是否命中白名单。

    未命中时**不动** body：非白名单模型的 `reasoning_effort` 由调用方决定
    怎么处理（本仓是原样透传，与改造前一致）。
    """
    if not isinstance(body, dict) or not supports_effort(model):
        return False
    raw = body.get('reasoning_effort')
    effort = raw.strip().lower() if isinstance(raw, str) else ''
    if effort not in ALLOWED:
        return False

    enabled = effort != 'none'
    if not enabled:
        # `none`：只删档位不够，必须显式关掉开关。
        body.pop('reasoning_effort', None)
    elif effort == 'low':
        body['reasoning_effort'] = 'high'
    else:
        body['reasoning_effort'] = effort

    # 显式档位优先于 SDK / 变体留下的冲突开关。
    thinking: dict[str, Any] = body.get('thinking') \
        if isinstance(body.get('thinking'), dict) else {}
    body['thinking'] = {**thinking, 'type': 'enabled' if enabled else 'disabled'}
    body['enable_thinking'] = enabled
    return True
