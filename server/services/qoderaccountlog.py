"""把 Qoder 调用实际用了哪个账号回填进请求日志。

为什么不能像 WorkBuddy 那样复用 `services/accountlog.py`
--------------------------------------------------------
WorkBuddy 的账号信息在它的**容器日志**里（一行一个请求，带 `昵称(uid)`）。
Qoder 是内置网关型平台，适配器只能通过回环 HTTP 跟它对话，拿不到「这次用了
哪个号」；它也不在响应头里回传。唯一的来源是内置网关自己写的用量账本：

    data/platforms/qoder/usage/usage.jsonl

每行形如（`_vendor/qoder_proxy.py:record_usage`）：

    {"at": 1767..., "model": "Qwen3.8-Max", "stream": true,
     "prompt_tokens": 123, "completion_tokens": 45, "credit": 0.002,
     "account": "u-xxxx", "realm": "cn", ...}

`account` 就是真实账号 uid。这里读它的**尾部**，按「同模型 + 时间最接近」
回填到 `request_logs.account`（复用 `db.attach_request_accounts`）。

两个 Qoder 特有的坑
-------------------
  · **模型名口径不同**：账本里是上游裸名（`Qwen3.8-Max`），请求日志里是
    记账名（`qoder/Qwen3.8-Max`，见 `PlatformAdapter.record_name`）。所以
    `attach_request_accounts` 必须带 `model_prefix='qoder/'`；只比裸名永远对不上。
  · **必须限定平台**：不同平台可能有同名模型（WorkBuddy 与 Qoder 都有
    `glm-5.2`），不加 `platform='qoder'` 会把账号填到另一个平台的记录上。

失败时的行为
------------
与 `accountlog` / 用量统计同口径：回填是**旁路**，文件不存在、半行 JSON、
库被锁都只是这一轮少填几行，绝不影响转发，也不影响内置网关自己记账。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from .. import db
from ..platforms.qoder import embedded

logger = logging.getLogger('workbuddy.qoderaccountlog')

# 轮询周期（秒）。内置网关在请求**结束时**才写这一行，晚几秒回填对「看谁在
# 跳账号」没有影响；间隔与 accountlog 保持一致，便于一起排查。
_POLL_SECONDS = 15

# 每次读文件尾部多少字节。一行约 300-600 字节，256 KiB 足以覆盖 15 秒内的
# 活动（即使高峰期每 15 秒上百条），又不会把几十 MB 的历史全读进来。
_TAIL_BYTES = 256 * 1024

# 从尾部最多取多少行参与回填。窗口只有 3 秒，实际能命中的远少于此；
# 取多一点只是为了容忍日志里夹着较早的行。
_TAIL_LINES = 500

# 最近一次采集的事实快照，供 `status()` 自诊断用（与 accountlog 同构）。
_last: dict = {
    'checked_at': 0, 'lines': 0, 'parsed': 0, 'filled': 0, 'error': '',
}


def _usage_path() -> Path:
    """内置网关的用量账本路径。

    `embedded.usage_dir()` 会顺手建目录，但**不会**拉起内置网关：没配 Qoder
    账号的部署跑这个采集器也不该多起一个回环端口。
    """
    return embedded.usage_dir() / 'usage.jsonl'


def _tail_lines(path: Path) -> list[str]:
    """读文件末尾若干行（不读全文件）。文件不存在 / 读失败返回空列表。"""
    try:
        with open(path, 'rb') as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            start = max(0, size - _TAIL_BYTES)
            fh.seek(start)
            data = fh.read()
    except OSError:
        return []
    if start:
        # 从中间切开时第一条大概率是半行，丢掉；否则会把半截 JSON 当坏行。
        cut = data.find(b'\n')
        data = data[cut + 1:] if cut >= 0 else b''
    text = data.decode('utf-8', 'replace')
    return [line for line in text.splitlines() if line.strip()][-_TAIL_LINES:]


def _entries(lines: list[str]) -> list[dict]:
    """把 JSONL 行解析成 `attach_request_accounts` 需要的条目。

    非 JSON / 缺 `at` / 缺 `account` 的行直接跳过：宁可漏填，不可瞎填。
    `at` 是浮点秒，本端 `request_logs.ts` 是 `int(time.time())`，截断到秒即可
    （匹配窗口有 3 秒，两边不可能精确相等）。
    """
    out: list[dict] = []
    for line in lines:
        try:
            row = json.loads(line)
        except Exception:  # noqa: BLE001 —— 写到一半的行 / 非本格式
            continue
        if not isinstance(row, dict):
            continue
        at = row.get('at')
        account = row.get('account')
        model = row.get('model')
        if not isinstance(at, (int, float)) or isinstance(at, bool):
            continue
        account = str(account or '').strip()
        model = str(model or '').strip()
        if not account or not model:
            continue
        out.append({'ts': int(at), 'model': model, 'account': account})
    return out


def _collect_once() -> int:
    """读一次用量账本并回填账号。返回回填条数（同步，供 to_thread 调用）。"""
    path = _usage_path()
    _last['checked_at'] = int(time.time())
    if not path.is_file():
        # 还没调用过 Qoder（内置网关只在第一次对话时才写第一行）。这不是错误，
        # 界面该看到的是「暂无数据」而不是一条采集故障。
        _last.update(lines=0, parsed=0, filled=0, error='')
        return 0
    lines = _tail_lines(path)
    _last['lines'] = len(lines)
    entries = _entries(lines)
    _last['parsed'] = len(entries)
    if not entries:
        _last['error'] = ''
        return 0
    # 裸模型名 + `qoder/<模型>` 双候选，且只在本平台的行里匹配 —— 见模块
    # docstring 里的「两个 Qoder 特有的坑」。
    filled = db.attach_request_accounts(
        entries, platform='qoder', model_prefix='qoder/')
    _last.update(filled=filled, error='')
    return filled


def status() -> dict:
    """账号回填的自诊断：账本在哪、读到几行、解析出几条、为什么可能是空的。"""
    path = _usage_path()
    snap = dict(_last)
    info: dict = {'source': str(path), **snap}
    if not path.is_file():
        info['exists'] = False
        info['hint'] = ('还没有 Qoder 的用量账本：只有在内置网关真正跑过一次对话之后'
                        '才会生成 usage.jsonl。先发一条 Qoder 请求再看。')
        return info
    info['exists'] = True
    try:
        info['size'] = path.stat().st_size
    except OSError as exc:
        info['hint'] = f'读不到用量账本：{exc}'
        return info
    if not info['size']:
        info['hint'] = '用量账本是空的：内置网关还没写完第一行。'
    elif not snap['checked_at']:
        info['hint'] = f'采集还没跑过第一轮（启动后最多 {_POLL_SECONDS} 秒）。稍等再看。'
    elif snap['lines'] and not snap['parsed']:
        info['hint'] = ('读到了行，但没有一行是可解析的用量记录。多半是内置网关'
                        '改了行格式 —— 请把 usage.jsonl 最后一行贴出来。')
    elif snap['parsed'] and not snap['filled']:
        # 与 accountlog 同口径：稳态下 filled 恒为 0（只统计本轮新填），不能
        # 据此报警；真正「卡住」的判据是库里还有待回填的行，由界面按需判断。
        info['hint'] = ''
    else:
        info['hint'] = ''
    return info


async def _loop() -> None:
    while True:
        try:
            # 读文件是阻塞 IO；放线程池里跑，别卡住网关的事件循环。
            filled = await asyncio.to_thread(_collect_once)
            if filled:
                logger.debug('回填 Qoder 账号 %d 条', filled)
        except Exception as exc:  # noqa: BLE001
            # 采集失败只是这一轮没有数据，下一轮继续；不影响任何请求。
            _last['error'] = str(exc)[:200]
            logger.warning('Qoder 账号回填失败（不影响请求）: %s', exc)
        await asyncio.sleep(_POLL_SECONDS)


def start_collector() -> bool:
    """启动后台回填任务（幂等）。需要运行中的事件循环。"""
    global _collector
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    if _collector is None or _collector.done():
        _collector = loop.create_task(_loop())
    return True


def stop_collector() -> None:
    global _collector
    if _collector and not _collector.done():
        _collector.cancel()
    _collector = None


_collector: asyncio.Task | None = None
