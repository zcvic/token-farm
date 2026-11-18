"""手动刷新时同步账号昵称（issue #94 同步）。

上游改名后，本地凭证文件里的 `account.nickname` 不会自己更新——以前只能等用户
重新扫码登录一次。这里在面板**手动**「刷新积分」的收尾阶段补一轮只读资料查询，
把显示名追平。

边界：
  · 只在手动刷新（`force=true`）触发；后台余额定时器/页面加载不调用——资料接口
    没必要高频触达，也少一份账号级请求。
  · 单号失败（网络 / 业务码 / uid 不一致 / 文件只读）**静默跳过**：昵称只是显示
    名，不该让整批余额刷新报错，也不该把失败写进用户能看到的积分结果。
  · 不写日志：资料响应含手机号等敏感字段，这里不打印任何 body（见
    `tencent.fetch_account_profile`）。
  · 昵称未变化不写盘（`wb2api.set_account_nickname`），避免无谓刷新 mtime 触发
    上游热加载。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from .. import config
from . import tencent, wb2api


async def sync_nicknames(accounts: list[dict], auth_dir: Path) -> list[str]:
    """逐号拉资料并回写变化的昵称，返回实际更新过的 uid 列表。

    `accounts` 是 `wb2api.list_auth_accounts` 的产物（至少需要 `file` / `uid`）。
    并发上限见 `config.PROFILE_CONCURRENCY`。
    """
    if not accounts:
        return []
    sem = asyncio.Semaphore(config.PROFILE_CONCURRENCY)

    async def one(acc: dict) -> str:
        filename = str(acc.get('file') or '')
        if not filename:
            return ''
        try:
            raw = wb2api.read_account_file(filename, auth_dir)
            auth = wb2api.account_auth_dict(raw, auth_dir)
            async with sem:
                nickname = await tencent.fetch_account_profile(auth)
            if wb2api.set_account_nickname(filename, nickname, auth_dir):
                return str(acc.get('uid') or '')
        except Exception:  # noqa: BLE001
            # 静默：账号保护性暂停、网络失败、uid 不一致、文件只读都只影响
            # 「昵称是不是最新」，不影响余额结果，也不该打断同批其他账号。
            return ''
        return ''

    updated = await asyncio.gather(*(one(a) for a in accounts))
    return [uid for uid in updated if uid]
