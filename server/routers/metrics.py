"""Prometheus 抓取端点：`/api/metrics` 与 `/metrics`。

## 鉴权

指标里有按模型的调用量、Token 与积分消耗 —— 这些是运营数据，不应对未认证
访问者公开。因此两条路径都要求管理面身份：会话 Cookie（浏览器打开）或
作用域化 API Token（`Authorization: Bearer wbt_...`，Prometheus 用这个）。

只读 token（scope=readonly）即可抓取；写入类 token 没有必要，但也不会拒绝
（`current_user` 只解析身份，端点本身不写任何东西）。

## 为什么同时注册两个路径

Prometheus 的默认配置是 `metrics_path: /metrics`，而本应用的管理面约定是
`/api/*`。给两个别名比让每个用户去改抓取配置更省事；两者是**同一个
处理函数**，不存在口径分叉。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse

from .. import security
from ..services import metrics

router = APIRouter(tags=['metrics'])


@lru_cache(maxsize=1)
def _version() -> str:
    """从仓库根的 `.version` 读取版本。

    不走 `server.main.app.version`：那需要反向导入入口模块，循环导入在
    `python -m server.main` 与测试的两种导入方式下行为一致但很脆。版本文件
    是发布流程的单一来源（见 docs/release-process.md），读它更直接。
    """
    try:
        return (Path(__file__).resolve().parents[2] / '.version').read_text(
            encoding='utf-8').strip()
    except OSError:
        return ''


@router.get('/api/metrics')
def api_metrics(user: dict = Depends(security.current_user)) -> PlainTextResponse:
    return _render()


@router.get('/metrics')
def root_metrics(user: dict = Depends(security.current_user)) -> PlainTextResponse:
    return _render()


def _render() -> PlainTextResponse:
    body = metrics.render(version=_version())
    return PlainTextResponse(
        body,
        media_type='text/plain; version=0.0.4; charset=utf-8',
        headers={'Cache-Control': 'no-store'},
    )
