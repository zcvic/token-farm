"""CodeArts 模型目录：按账号缓存 + 三路来源退避。

与其它平台不同，CodeArts 的模型目录是**按账号**的：

  · agent-center 与 `/v1/model/builtin` 列的是「这个账号能用什么」；
  · 限时福利网关列的是「这个账号领到了哪些免费模型」，对话时必须带
    `maas_type: benefit`，否则上游回 `InferHub.002002009.404 model is not
    registered`。

所以缓存键是 `uid`，落库在 `platforms.codearts.catalog`：

```json
{"<uid>": {"fetched_at": 1700000000, "models": [{"id": "GLM-5.2", ...}]}}
```

对外仍然只给一份**并集**（`/v1/models` 是全局列表）；某个模型在任一账号上
被标成福利，并集里就标福利——真正决定带不带头的仍是**选中的那个账号**
（`is_benefit(uid, model)`）。

来源退避：某一路连续失败后 5 分钟内不再打它（`src_fail`），避免每次刷新目录
都等一次超时。福利来源失败时保留上一轮的福利标记（`keep_benefit`）：漏标
就是漏头，宁可多带。
"""
from __future__ import annotations

import time
from typing import Any, Iterable

from ... import db
from ..base import PlatformError
from . import client
from . import constants as C

SETTING_CATALOG = 'platforms.codearts.catalog'

# (uid, 来源) -> 退避截止时刻（进程内；重启后重试一次是合理的）
_src_fail: dict[tuple[str, str], float] = {}

SOURCES = ('agent', 'builtin', 'benefit')


def _num(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _int(value: object) -> int:
    number = _num(value)
    return int(number) if number is not None else 0


# ── 归一化 ──────────────────────────────────────────────────
def normalize(models: object) -> list[dict]:
    """把任意来源的条目归一成内部结构（坏条目丢掉、按小写 id 去重）。"""
    out: list[dict] = []
    seen: set[str] = set()
    for raw in models if isinstance(models, list) else []:
        if not isinstance(raw, dict):
            continue
        model_id = str(raw.get('id') or raw.get('model_name')
                       or raw.get('name') or '').strip()
        if not model_id or model_id.lower() in seen:
            continue
        seen.add(model_id.lower())
        out.append({
            'id': model_id,
            'name': str(raw.get('name') or model_id),
            'description': str(raw.get('description') or ''),
            'context_length': _int(raw.get('context_length')
                                   or raw.get('context_window')),
            'max_tokens': _int(raw.get('max_tokens')),
            'benefit': bool(raw.get('benefit')),
        })
    out.sort(key=lambda m: m['id'].lower())
    return out


def seed_models() -> list[dict]:
    """冷启动兜底：内置模型 + 福利种子（福利标记随种子一起给出）。"""
    entries: list[dict] = []
    for model_id in C.SEED_MODELS:
        entries.append({'id': model_id, 'name': model_id, 'benefit': False})
    for model_id in C.SEED_BENEFIT_MODELS:
        entries.append({'id': model_id, 'name': model_id, 'benefit': True})
    return normalize(entries)


def known_index(models: Iterable[dict]) -> dict[str, str]:
    """小写 id → 精确拼写（上游按注册名**区分大小写**匹配）。"""
    out: dict[str, str] = {}
    for raw in models or ():
        model_id = str(raw.get('id') or '').strip()
        if model_id:
            out.setdefault(model_id.lower(), model_id)
    return out


# ── 缓存 ────────────────────────────────────────────────────
def _read_cache() -> dict[str, dict]:
    try:
        raw = db.get_setting(SETTING_CATALOG, None)
    except Exception:  # noqa: BLE001 —— 缓存读不到就当没有
        return {}
    return raw if isinstance(raw, dict) else {}


def cache_get(uid: str, *, ttl: int | None = None) -> list[dict] | None:
    """读某账号的目录；`ttl` 为 None 表示不看新鲜度（过期也用）。"""
    entry = _read_cache().get(str(uid or ''))
    if not isinstance(entry, dict):
        return None
    models = normalize(entry.get('models'))
    if not models:
        return None
    if ttl is not None:
        fetched = _num(entry.get('fetched_at')) or 0.0
        if fetched + ttl < time.time():
            return None
    return models


def cache_put(uid: str, models: list[dict]) -> None:
    key = str(uid or '').strip()
    if not key or not models:
        return
    try:
        data = _read_cache()
        data[key] = {'fetched_at': int(time.time()),
                     'models': normalize(models)}
        db.set_setting(SETTING_CATALOG, data)
    except Exception:  # noqa: BLE001 —— 缓存写不进去不影响本次返回
        pass


def cache_drop(uid: str) -> None:
    key = str(uid or '').strip()
    if not key:
        return
    try:
        data = _read_cache()
        if data.pop(key, None) is not None:
            db.set_setting(SETTING_CATALOG, data)
    except Exception:  # noqa: BLE001
        pass


def all_models() -> list[dict]:
    """所有账号目录的并集（过期也用）；一个都没有时退回种子。"""
    groups: list[list[dict]] = []
    for entry in _read_cache().values():
        if isinstance(entry, dict):
            models = normalize(entry.get('models'))
            if models:
                groups.append(models)
    merged = client.merge_models(*groups)
    return merged or seed_models()


def models_for(uid: str) -> list[dict]:
    """某账号的目录（过期也用）；没有就退回种子。"""
    return cache_get(uid, ttl=None) or seed_models()


def has_live_catalog() -> bool:
    """是否已经成功发现过至少一个账号的目录。

    冷启动时只有种子，「目录里没有这个模型」不等于「上游没有」——那只是
    「还没问过上游」。所以本地模型名校验只在有真目录时才启用，否则把合法
    模型误拒成 400，比让上游回一次 `not registered` 更难排查。
    """
    for entry in _read_cache().values():
        if isinstance(entry, dict) and normalize(entry.get('models')):
            return True
    return False


# ── 来源退避 ────────────────────────────────────────────────
def src_blocked(uid: str, source: str) -> bool:
    until = _src_fail.get((str(uid or ''), source)) or 0.0
    return until > time.time()


def note_src_fail(uid: str, source: str) -> None:
    _src_fail[(str(uid or ''), source)] = (
        time.time() + C.CATALOG_SOURCE_BACKOFF)


def clear_src_fail(uid: str, source: str) -> None:
    _src_fail.pop((str(uid or ''), source), None)


def reset_source_backoff() -> None:
    """测试用：清掉进程内退避。"""
    _src_fail.clear()


# ── 发现 ────────────────────────────────────────────────────
async def discover(cred: Any, uid: str, *, proxy: str | None = None,
                   timeout: float = C.CATALOG_TIMEOUT) -> list[dict]:
    """三路拉一次并按账号合并。

    任一路失败只丢那一路（并记退避）；全部失败则抛错，让调用方保留旧缓存
    而不是把目录清空。
    """
    previous = cache_get(uid, ttl=None) or []
    fetchers = (
        ('agent', client.fetch_agent_models),
        ('builtin', client.fetch_builtin_models),
        ('benefit', client.fetch_benefit_models),
    )
    groups: list[list[dict]] = []
    failed = 0
    benefit_ok = True
    for name, fetcher in fetchers:
        if src_blocked(uid, name):
            failed += 1
            if name == 'benefit':
                benefit_ok = False
            continue
        try:
            groups.append(await fetcher(cred, proxy=proxy, timeout=timeout))
            clear_src_fail(uid, name)
        except PlatformError:
            failed += 1
            note_src_fail(uid, name)
            if name == 'benefit':
                benefit_ok = False
            continue
    if failed == len(fetchers):
        raise PlatformError('CodeArts 模型目录三路来源都不可用', status=502,
                            code='empty_catalog')
    merged = client.merge_models(*groups)
    if not merged:
        raise PlatformError('CodeArts 模型目录为空', status=502,
                            code='empty_catalog')
    if not benefit_ok:
        merged = _keep_benefit(merged, previous)
    return merged


def _keep_benefit(models: list[dict], previous: list[dict]) -> list[dict]:
    """福利来源本轮失败：上一轮标了福利、本轮没提到的，继续按福利路由。"""
    known = {str(m.get('id') or '').lower() for m in models}
    extra: list[dict] = []
    for raw in previous:
        model_id = str(raw.get('id') or '').strip()
        if not model_id or not raw.get('benefit'):
            continue
        if model_id.lower() in known:
            continue
        extra.append({'id': model_id, 'name': str(raw.get('name') or model_id),
                      'description': str(raw.get('description') or ''),
                      'context_length': _int(raw.get('context_length')),
                      'max_tokens': _int(raw.get('max_tokens')),
                      'benefit': True})
    if not extra:
        return models
    return client.merge_models(models, extra)


# ── 福利判定 ────────────────────────────────────────────────
def is_benefit(uid: str, model: str) -> bool:
    """该账号下的这个模型是否走限时福利路由（大小写不敏感）。

    判定顺序与参考实现一致：

      1. 目录里标了福利 → True；
      2. 目录里有但没标 → False（已以内置身份注册，套餐轮换后转正靠这条）；
      3. 目录里没有（还没发现 / 福利来源失败 / 未领取）→ 退回种子，
         宁可多带一次头，也不要少带一次变成 404。
    """
    key = str(model or '').strip().lower()
    if not key:
        return False
    entry = _read_cache().get(str(uid or ''))
    if isinstance(entry, dict):
        models = normalize(entry.get('models'))
        if models:
            for raw in models:
                if str(raw.get('id') or '').lower() == key:
                    return bool(raw.get('benefit'))
    return key in {item.lower() for item in C.SEED_BENEFIT_MODELS}


def known_spellings() -> dict[str, str]:
    """小写 → 精确拼写（目录并集 + 种子 + 静态别名）。"""
    index = known_index(all_models())
    for model_id in (*C.SEED_MODELS, *C.SEED_BENEFIT_MODELS):
        index.setdefault(model_id.lower(), model_id)
    for alias, target in C.STATIC_ALIASES.items():
        index.setdefault(alias.lower(), target)
    return index
