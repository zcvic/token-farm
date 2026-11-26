"""豆包模型目录。

豆包**没有**公开的模型列表接口：可调用模型由客户端版本与账号权益决定，名字
是内部的 `model_item_key`（`person-deepseek-v4-1-flash-261017` 这类）。所以目录
只能是内置快照，没有「在线拉取 → 缓存 → 降级」那三级。

这也意味着 `platforms.doubao.catalog_*` 那几个设置项对本平台没有意义（保存了
也不会生效），能力表里刻意不给它们——见 `platforms/settings.py`。

`normalize()` 仍然保留：面板 / 测试会把它当作「任意来源的模型条目 → 内部结构」
的归一器，将来参考实现给出真实目录接口时接上即可。
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
            # 上游真正的模型 id：对话请求体里 `model_item_key` 用的是它。
            'model_item_key': str(raw.get('model_item_key') or model_id),
            'memory_profile': str(raw.get('memory_profile') or C.MEMORY_PROFILE),
            'total_window_size': str(
                raw.get('total_window_size') or C.TOTAL_WINDOW_SIZE),
        })
    return out


def default_models() -> list[dict]:
    return normalize([dict(m) for m in C.DEFAULT_MODELS])
