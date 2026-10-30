"""Trae 模型目录兜底。

为什么需要本地目录：国际版的 `model_list` 接口返回 403（no schema found），
国内版在未授权 / 网络异常时也拿不到。目录缺失会让客户端看到一个空模型列表，
比「列表略旧」糟糕得多——用户会以为网关没接上。

数据整理自参考项目 `trae2api-hub` 的 `trae_catalog_cn.json` /
`trae_catalog_intl.json`（MIT），**只保留 `chat: true` 的条目**：其余是
code_reviewer / refactor 等内部角色模型，经 `llm_raw_chat` 调不通，列出来
只会让客户端选中后报错。

运行时先用缓存（`DB` 设置 `platforms.trae.models`）覆盖这里的默认值，因此
上游新增模型时不需要改代码。
"""
from __future__ import annotations

# id 必须与 llm_raw_chat 的 model_name 完全一致（大小写敏感）。
_DEFAULT_MODELS: tuple[dict, ...] = (
    # ── 国内版 ──────────────────────────────────────────────
    {'id': 'DeepSeek-V4-Flash-Official', 'name': 'DeepSeek-V4-Flash',
     'realm': 'cn', 'context_window': 116000, 'max_tokens': 64000,
     'multimodal': False, 'thinking': True},
    {'id': 'DeepSeek-V4-Pro-Official', 'name': 'DeepSeek-V4-Pro',
     'realm': 'cn', 'context_window': 116000, 'max_tokens': 64000,
     'multimodal': False, 'thinking': True},
    {'id': 'deepseek-v4.1-flash', 'name': 'DeepSeek-V4.1-Flash',
     'realm': 'cn', 'context_window': 116000, 'max_tokens': 64000,
     'multimodal': True, 'thinking': True},
    {'id': 'glm-5.2', 'name': 'GLM-5.2',
     'realm': 'cn', 'context_window': 116000, 'max_tokens': 64000,
     'multimodal': False, 'thinking': True},
    {'id': 'glm-5.3', 'name': 'GLM-5.3',
     'realm': 'cn', 'context_window': 116000, 'max_tokens': 64000,
     'multimodal': False, 'thinking': True},
    {'id': 'glm-5.3-flash', 'name': 'GLM-5.3-Flash',
     'realm': 'cn', 'context_window': 116000, 'max_tokens': 64000,
     'multimodal': True, 'thinking': True},
    {'id': 'glm-5.3-flashx', 'name': 'GLM-5.3-FlashX',
     'realm': 'cn', 'context_window': 116000, 'max_tokens': 64000,
     'multimodal': True, 'thinking': True},
    {'id': 'kimi-k2.8-preview', 'name': 'Kimi-K2.8-Preview',
     'realm': 'cn', 'context_window': 200000, 'max_tokens': 64000,
     'multimodal': True, 'thinking': True},
    {'id': 'kimi-k3', 'name': 'Kimi-K3',
     'realm': 'cn', 'context_window': 200000, 'max_tokens': 64000,
     'multimodal': True, 'thinking': True},
    {'id': 'mimo-v2.6-flash', 'name': 'MiMo-V2.6-Flash',
     'realm': 'cn', 'context_window': 200000, 'max_tokens': 64000,
     'multimodal': True, 'thinking': False},
    {'id': 'mimo-v2.6-pro', 'name': 'MiMo-V2.6-Pro',
     'realm': 'cn', 'context_window': 200000, 'max_tokens': 64000,
     'multimodal': True, 'thinking': False},
    {'id': 'minimax-m3', 'name': 'MiniMax-M3',
     'realm': 'cn', 'context_window': 116000, 'max_tokens': 64000,
     'multimodal': True, 'thinking': True},
    {'id': 'qwen-3.7-plus', 'name': 'Qwen3.7-Plus',
     'realm': 'cn', 'context_window': 200000, 'max_tokens': 64000,
     'multimodal': True, 'thinking': True},
    {'id': 'qwen3.8-flash', 'name': 'Qwen3.8-Flash',
     'realm': 'cn', 'context_window': 200000, 'max_tokens': 64000,
     'multimodal': True, 'thinking': True},
    {'id': 'qwen3.8-max', 'name': 'Qwen3.8-Max',
     'realm': 'cn', 'context_window': 200000, 'max_tokens': 64000,
     'multimodal': True, 'thinking': True},
    {'id': 'Doubao-Seed-Code', 'name': 'Seed-Code',
     'realm': 'cn', 'context_window': 256000, 'max_tokens': 32000,
     'multimodal': True, 'thinking': True},
    {'id': 'Doubao-Seed-2.1-Pro', 'name': 'Seed-2.1-Pro',
     'realm': 'cn', 'context_window': 256000, 'max_tokens': 64000,
     'multimodal': True, 'thinking': True},
    {'id': 'Doubao-Seed-2.1-Turbo', 'name': 'Seed-2.1-Turbo',
     'realm': 'cn', 'context_window': 256000, 'max_tokens': 32000,
     'multimodal': True, 'thinking': True},
    {'id': 'Doubao-Seed-Evolving', 'name': 'Seed-Evolving',
     'realm': 'cn', 'context_window': 256000, 'max_tokens': 64000,
     'multimodal': True, 'thinking': False},
    {'id': 'step-5-preview', 'name': 'Step-5-Preview',
     'realm': 'cn', 'context_window': 116000, 'max_tokens': 64000,
     'multimodal': True, 'thinking': True},
    # ── 国际版 ──────────────────────────────────────────────
    {'id': 'aws_claude_4.7_opus', 'name': 'Claude-Opus-4.7',
     'realm': 'intl', 'context_window': 200000, 'max_tokens': 32000,
     'multimodal': True, 'thinking': True},
    {'id': 'aws_claude_4.6_sonnet', 'name': 'Claude-Sonnet-4.6',
     'realm': 'intl', 'context_window': 200000, 'max_tokens': 32000,
     'multimodal': True, 'thinking': True},
    {'id': 'gemini-2.5-pro-latest', 'name': 'Gemini-2.5-Pro',
     'realm': 'intl', 'context_window': 200000, 'max_tokens': 32000,
     'multimodal': True, 'thinking': True},
    {'id': 'gemini_2.5_flash_premium', 'name': 'Gemini-2.5-Flash',
     'realm': 'intl', 'context_window': 200000, 'max_tokens': 32000,
     'multimodal': True, 'thinking': True},
    {'id': 'gemini_3_flash', 'name': 'Gemini-3-Flash-Preview',
     'realm': 'intl', 'context_window': 200000, 'max_tokens': 32000,
     'multimodal': True, 'thinking': True},
    {'id': 'gemini_3.1_pro', 'name': 'Gemini-3.1-Pro-Preview',
     'realm': 'intl', 'context_window': 200000, 'max_tokens': 32000,
     'multimodal': True, 'thinking': True},
    {'id': 'gpt-5.5', 'name': 'GPT-5.5',
     'realm': 'intl', 'context_window': 272000, 'max_tokens': 32000,
     'multimodal': True, 'thinking': True},
    {'id': 'gpt-6-astra', 'name': 'GPT-6-Astra',
     'realm': 'intl', 'context_window': 272000, 'max_tokens': 64000,
     'multimodal': True, 'thinking': True},
    {'id': 'grok-4', 'name': 'Grok-4',
     'realm': 'intl', 'context_window': 128000, 'max_tokens': 16000,
     'multimodal': True, 'thinking': True},
    {'id': 'kimi-k2', 'name': 'Kimi-K2-0905',
     'realm': 'intl', 'context_window': 200000, 'max_tokens': 16000,
     'multimodal': False, 'thinking': True},
)


def default_models() -> list[dict]:
    """目录默认值（副本，调用方可以安全修改）。"""
    return [dict(m) for m in _DEFAULT_MODELS]


def find(model_id: str) -> dict | None:
    target = str(model_id or '').strip().lower()
    for m in _DEFAULT_MODELS:
        if m['id'].lower() == target:
            return dict(m)
    return None
