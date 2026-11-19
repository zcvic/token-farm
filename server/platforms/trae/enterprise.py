"""识别并拒绝 Trae 企业版 / SaaS 租户账号（个人版协议不适用）。

参考自 `shuishuipingan/trae2api-hub` 的 `c724a65`（v1.6.1）：
企业版使用独立租户协议，继续按个人版的 ExchangeToken / 设备证明去刷新
只会得到难以解释的上游错误。这里在**发请求之前**把已知的企业身份挡下来，
返回明确错误，而不是把两套运行时耦在一起。

判据只来自显式的租户元数据，**绝不根据裸 token 猜**：

  · `realm` 为 `enterprise` / `saas`（大小写不敏感）；
  · `scope` / `loginScope` 为 `saas`；
  · 存在非空 `saasBootConfig`；
  · 响应包装里的 `TenantInfoBase` 非空。

只检查账号 / profile / extra / UserInfo 这些具名节点和 Data / Result 包装，
不递归扫描用户正文，避免把普通聊天内容里的同名字段误判成租户身份。
"""
from __future__ import annotations

from ..base import PlatformError

ENTERPRISE_UNSUPPORTED_MESSAGE = (
    'Trae 企业版 / SaaS 使用独立租户协议，本面板只支持个人版，未向上游发送请求'
)

_ENTERPRISE_REALMS = ('enterprise', 'saas')
_ACCOUNT_NODES = ('account', 'profile', 'extra', 'UserInfo', 'userInfo')
_WRAPPERS = ('Data', 'data', 'Result', 'result')


def is_enterprise_account(value: object) -> bool:
    """判断账号 / 登录响应是否明确属于企业版或 SaaS 租户。"""
    if not isinstance(value, dict):
        value = getattr(value, '__dict__', {})
    if not isinstance(value, dict):
        return False

    # 只沿具名节点（账号 / 包装）向下看，且限制层数：不递归扫描用户正文，
    # 避免把聊天内容里的同名字段误判成租户身份。官方响应常见两层包装
    # （`Result.Data.UserInfo`），所以包装键也要逐层展开。
    nodes: list[dict] = [value]
    seen: set[int] = {id(value)}
    frontier: list[dict] = [value]
    for _ in range(4):
        following: list[dict] = []
        for node in frontier:
            for key in _ACCOUNT_NODES + _WRAPPERS:
                child = node.get(key)
                if isinstance(child, dict) and id(child) not in seen:
                    seen.add(id(child))
                    nodes.append(child)
                    following.append(child)
        if not following:
            break
        frontier = following

    for node in nodes:
        tenant = node.get('TenantInfoBase')
        if isinstance(tenant, dict) and tenant:
            return True
        if str(node.get('realm') or '').strip().lower() in _ENTERPRISE_REALMS:
            return True
        if any(str(node.get(key) or '').strip().lower() == 'saas'
               for key in ('scope', 'Scope', 'loginScope')):
            return True
        config = node.get('saasBootConfig')
        if isinstance(config, dict) and config:
            return True
    return False


def ensure_personal_account(value: object) -> object:
    """企业版 / SaaS 账号直接拒绝；个人版原样返回。"""
    if is_enterprise_account(value):
        raise PlatformError(
            ENTERPRISE_UNSUPPORTED_MESSAGE,
            status=400,
            code='unsupported_enterprise_account',
        )
    return value