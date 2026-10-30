"""Trae 双区协议常量。

事实来源：`shuishuipingan/trae2api-hub`（MIT）的 `trae_client.py`，并在
`docs/multi-platform.md` 第 7 节做了摘要。改这里的值等于改上游契约：
主机、路径、UA、版本号任意一项都可能触发风控或 404，改动前先核对。

**不要为了让「看起来更整齐」而修改**：
  · 国内版核心数据面的域名是 `trae-api-cn.mchost.guru` 而不是 `api.trae.com.cn`
    —— 后者只承接 CloudIDE / OAuth / 支付，`/api/ide/v1/*` 打过去是 404；
  · 签到接口只在 `api.trae.cn` 注册（`api.trae.com.cn` 同样是 404）；
  · 国际版的 `model_list` 是 403（no schema found），模型清单只能走本地目录。
"""
from __future__ import annotations

APP_ID = '6eefa01c-1036-4c7e-9ca5-d891f63bfcd8'
PLUGIN_CHANNEL = 'icube-ai'
IDE_VERSION_CODE = '20260212'
CLIENT_ID = 'ono9krqynydwx5'
CLIENT_SECRET = '-'

PATH_CHAT = '/api/ide/v1/llm_raw_chat'
PATH_MODEL_LIST = '/api/ide/v1/model_list'
PATH_SESSION_USAGE = '/api/v1/commercial/get_session_usage'
PATH_ENT_USAGE_V1 = '/trae/api/v1/pay/ide_user_ent_usage'
PATH_ENT_USAGE_V2 = '/trae/api/v2/pay/ide_user_ent_usage'
PATH_PAY_STATUS = '/trae/api/v1/pay/ide_user_pay_status'
PATH_CHECKIN_STATUS = '/trae/api/v2/ug/checkin_credits/status'
PATH_CHECKIN_CLAIM = '/trae/api/v2/ug/checkin_credits/claim'
PATH_EXCHANGE_TOKEN = '/cloudide/api/v3/trae/oauth/ExchangeToken'

# 国际版对话必须带这一组「当前客户端版本」头，否则 4015。
INTL_CHAT_HEADERS = {
    'x-ide-version-code': '20260924',
    'x-ide-version': '3.6.2',
    'x-app-version': '3.6.2',
    'x-app-version-code': '20260924',
    'store-country-code': 'us',
}
INTL_CHAT_UA = 'TraeClient/TTNet'

REALMS: dict[str, dict] = {
    'cn': {
        'key': 'cn',
        'label': '国内版',
        'core': 'https://trae-api-cn.mchost.guru',
        'core_fallbacks': (),
        'account': 'https://api.trae.com.cn',
        'account_fallbacks': (),
        'pay': 'https://api.trae.com.cn',
        'pay_fallbacks': (),
        # 签到 / 活动只在 api.trae.cn 注册
        'ug': 'https://api.trae.cn',
        'ug_fallbacks': (),
        'ua': 'Trae CN/3.4.3',
        'version_code': IDE_VERSION_CODE,
        'region': 'CN',
        'model_list_supported': True,
        'supports_checkin': True,
    },
    'intl': {
        'key': 'intl',
        'label': '国际版',
        'core': 'https://core-normal.traeapi.us',
        'core_fallbacks': ('https://core-normal.trae.ai', 'https://coresg-normal.trae.ai'),
        'account': 'https://grow-normal.trae.ai',
        'account_fallbacks': ('https://grow-normal.traeapi.us',),
        'pay': 'https://grow-normal.traeapi.us',
        'pay_fallbacks': ('https://grow-normal.trae.ai',),
        'ug': 'https://grow-normal.traeapi.us',
        'ug_fallbacks': (),
        'ua': 'Trae/3.6.2',
        'version_code': IDE_VERSION_CODE,
        'region': 'US',
        'model_list_supported': False,
        'supports_checkin': False,
    },
}

DEFAULT_REALM = 'cn'


def normalize_realm(value: object) -> str:
    """归一化版本归属；认不出的值一律回到默认（国内版）。"""
    v = str(value or '').strip().lower()
    return v if v in REALMS else DEFAULT_REALM


def realm_hosts(realm: str, base: str = 'core') -> tuple[str, ...]:
    """某区域某类接口的主机列表（主 + 备选），按顺序尝试。"""
    conf = REALMS[normalize_realm(realm)]
    primary = str(conf.get(base) or conf['core'])
    fallbacks = tuple(conf.get(f'{base}_fallbacks') or ())
    return (primary, *fallbacks)
