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

import json
import logging

from .. import config, db
from .filelock import file_lock

logger = logging.getLogger(__name__)

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
    # 镜像到上游 config.json（Go 侧 models.hidden）。失败只告警，不回滚 DB：
    # 面板自己的行为（列表 + 点名拒绝）已经由上面那行生效，镜像只是让「直连上游
    # 端口」这条路径也一致。为一个旁路同步失败把用户刚保存的名单丢掉更糟。
    mirror_to_upstream(hidden)
    return {'hidden': hidden}


def mirror_to_upstream(hidden: list[str]) -> bool:
    """把隐藏名单写进上游 config.json 的 `models.hidden`（Go 侧热加载读取）。

    为什么要镜像：对外流量通常走本面板，但上游端口是**发布到宿主机**的
    （docker-compose 的 ports 段），直连 Go 完全可行。只存在数据库里，Go 就
    完全看不到这份名单——「面板上藏了、直连端口还看得见」是最难排查的一类问题。

    三条必须守住的性质（与 proxyroutes 的 `proxies` 段同一套约定）：

      1. **只动 `models.hidden`**：config.json 里还有 api_key / schedule / prompt /
         proxies 等，其余键一个都不能碰；
      2. **原子替换**：Go 侧每 10 秒重读一次，`write_text` 的「先截断」窗口会被
         读到空文件——复用 tencent 的 `_atomic_write_json`（临时文件 + os.replace）；
      3. **失败不致命**：返回 False 并记 WARN，由调用方决定要不要提示。写不进去
         的常见原因是面板侧把上游目录挂成了只读、或文件属主不允许面板写，属于
         部署问题，不该让面板功能不可用。

    返回是否真的写成功（文件不存在时视为失败但不抛：说明这套部署没有上游配置文件，
    纯面板模式下这条镜像本来就不适用）。
    """
    path = config.UPSTREAM_CONFIG
    try:
        with file_lock(path):
            try:
                raw = json.loads(path.read_text(encoding='utf-8'))
            except FileNotFoundError:
                logger.debug('上游配置文件不存在，跳过隐藏名单镜像：%s', path)
                return False
            except (OSError, ValueError) as exc:
                logger.warning('读取上游配置文件失败，隐藏名单未镜像：%s（%s）', path, exc)
                return False
            if not isinstance(raw, dict):
                logger.warning('上游配置文件顶层不是对象，隐藏名单未镜像：%s', path)
                return False
            models = raw.get('models')
            if not isinstance(models, dict):
                models = {}
            if hidden:
                models['hidden'] = list(hidden)
            else:
                # 空名单就把键删掉，保持文件干净（读侧两种都认，与 proxies 同约定）。
                models.pop('hidden', None)
            if models:
                raw['models'] = models
            else:
                raw.pop('models', None)
            # 延迟导入：tencent 模块很重（httpx 客户端、realm 解析），而这份镜像
            # 只在管理员点保存时才跑一次，不该让每次 `import modelpolicy` 都带上它。
            from .tencent import _atomic_write_json
            _atomic_write_json(path, raw)
            return True
    except Exception as exc:  # noqa: BLE001 —— 旁路同步：任何失败都只告警，绝不影响保存
        logger.warning('写入上游配置文件失败，隐藏名单未镜像：%s（%s）'
                       '；面板内行为不受影响，直连上游端口看不到本次变更', path, exc)
        return False


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

