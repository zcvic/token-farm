"""模型可见性策略：把指定模型从下游清单里隐藏（同步自 workbuddy2api-panel）。

## 它解决什么问题

上游的模型目录里有些条目**能调但不该让下游看到**：内测模型、价格异常高的
模型、给内部任务专用的模型。逐个密钥写白名单很笨重（新密钥都要记得配），
而且白名单是「只允许这些」，与「藏掉这一个」是相反的表达。

这里维护一份**全局隐藏名单**，列表与点名调用同时生效：

  * `/v1/models` 不返回被隐藏的模型；
  * 直接点名调用返回 404（与「模型不存在」同一个形状）——只藏列表不拦调用
    等于没藏，客户端存下 id 后照样能用。

## 写法

  * `glm-5.2`：两个版本都隐藏（裸名 = 国内版，同时也是「不分版本」的写法）；
  * `cn:glm-5.2`：只隐藏国内版；
  * `global:glm-5.2`：只隐藏国际版。

匹配时忽略大小写与模型名前的 `cn:` / `global:` 前缀差异：面板展示的是裸名，
用户照着界面填，不该因为版本前缀写法不同就匹配不上。

## 为什么不用密钥白名单

白名单是**每把密钥**的授权，藏模型是**全局展示策略**。混用会让「某把密钥
能不能调某模型」与「控制台里看不看得见」耦合成同一份配置，排查时很难说清
到底是哪一条在起作用。
"""
from __future__ import annotations

from .. import db

SETTING_KEY = 'model_policy'
MAX_HIDDEN = 500
_MAX_LEN = 128

_REALMS = ('cn', 'global')


def _split(model: object) -> tuple[str, str]:
    """把模型名拆成 (realm, 裸名)。无前缀按 cn（与 db.realm_of_model 一致）。"""
    text = str(model or '').strip().lower()
    if ':' in text:
        realm, _, rest = text.partition(':')
        if realm in _REALMS and rest:
            return realm, rest
    return 'cn', text


def load() -> dict:
    raw = db.get_setting(SETTING_KEY, {}) or {}
    hidden = raw.get('hidden') if isinstance(raw, dict) else None
    return {'hidden': _clean_hidden(hidden)}


def _clean_hidden(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if not text or len(text) > _MAX_LEN:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out[:MAX_HIDDEN]


def save(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise ValueError('配置必须是对象')
    hidden = _clean_hidden(raw.get('hidden'))
    # 重复项与空串在这里被静默归一化：它们不是用户错误，没必要挡住保存。
    db.set_setting(SETTING_KEY, {'hidden': hidden})
    return {'hidden': hidden}


def is_hidden(model: object) -> bool:
    """该模型是否被隐藏。空模型（/v1/models 等）恒为 False。"""
    if not model:
        return False
    return _matches(model, load().get('hidden') or [])


def _entry_has_realm(entry: str) -> bool:
    return ':' in entry and entry.split(':', 1)[0].strip().lower() in _REALMS


def _matches(model: object, entries: list[str]) -> bool:
    realm, bare = _split(model)
    exact = str(model).strip().lower()
    for entry in entries:
        e_realm, e_bare = _split(entry)
        if e_bare != bare and e_bare != exact:
            continue
        # 裸名（无版本前缀）= 两个版本都隐藏；带前缀则只藏对应版本。
        if not _entry_has_realm(entry) or e_realm == realm:
            return True
    return False


def filter_payload(payload: object) -> object:
    """从 `{data:[{id:...}]}` 里剔除隐藏模型；认不出的结构原样返回。"""
    if not isinstance(payload, dict):
        return payload
    items = payload.get('data')
    if not isinstance(items, list):
        return payload
    entries = load().get('hidden') or []
    if not entries:
        return payload
    kept = [m for m in items
            if not (isinstance(m, dict) and m.get('id') and _matches(m.get('id'), entries))]
    if len(kept) == len(items):
        return payload
    return {**payload, 'data': kept}

