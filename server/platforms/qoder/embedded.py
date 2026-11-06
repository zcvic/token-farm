"""内置 Qoder 网关：把 qoder2api-hub 跑在 token-farm 进程里。

## 为什么是「进程内 + 回环端口」

Qoder 的上游协议包含 COSY 签名、设备指纹、OAuth 设备授权、模型目录与签到
风控，`qoder2api-hub`（`_vendor/` 里的那份副本）已经把它们实现成一个完整的
网关。它的对话管线有相当多的细节（SSE 心跳、工具调用泄漏回收、队列等待、
账号熔断与换号、usage 记账），把这些逻辑抄一遍既容易走样、也难跟上游同步。

所以这里不复刻协议：把上游的 `Handler` 挂在 `127.0.0.1` 的一个**随机空闲
端口**上，只在本进程内可达。对外仍然是 `QoderClient` 的 HTTP 形状（`/v1/*`、
`/accounts*`），但不再需要用户单独部署第二个项目。

## 初始化做了哪些事

上游 `main()` 里除了命令行解析与打印之外，真正必要的初始化是：

  1. 账号目录 / usage 目录（决定凭据、机器身份、用量落在哪）；
  2. 账号池 `AccountPool(...).load()` 与持久化的区域状态；
  3. API Key（每个进程随机）与面板口令（首次启动随机落一个哈希，别留默认
     的 `admin`——同机其他进程也能访问回环端口）；
  4. 余额刷新器（用过的账号才会被查询，空闲不轮询）。

调度器（`Scheduler`）不启动：token-farm 自己有签到入口，两套定时任务同时跑
会重复签到。上游的看板 / 一键更新路由也没有使用场景，端口不对局域网暴露。

## 生命周期

首次用到时（`runtime()`）才导入上游模块并启动，因此：

  · 服务器启动不会被这 0.6s 的导入拖慢；
  · 没配 Qoder 账号的部署完全不会加载它；
  · 启动失败只影响 Qoder 平台，错误以 `PlatformError` 返回给界面。
"""
from __future__ import annotations

import atexit
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ... import config
from ..base import PlatformError

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
_RUNTIME: 'Runtime | None' = None
_SERVER = None
_PROXY = None
_ATEXIT_REGISTERED = False


@dataclass(frozen=True)
class Runtime:
    """内置网关的运行期参数（全部只在内存里，不落盘、不下发浏览器）。"""

    base: str
    api_key: str
    accounts_dir: str
    usage_dir: str


def data_dir() -> Path:
    """Qoder 的持久化目录：`<DATA_DIR>/platforms/qoder`。"""
    path = config.DATA_DIR / 'platforms' / 'qoder'
    path.mkdir(parents=True, exist_ok=True)
    return path


def accounts_dir() -> Path:
    path = data_dir() / 'accounts'
    path.mkdir(parents=True, exist_ok=True)
    if os.name != 'nt':
        # 目录里是明文凭据：POSIX 上只给属主读写（Windows 由 ACL 决定）。
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    return path


def usage_dir() -> Path:
    path = data_dir() / 'usage'
    path.mkdir(parents=True, exist_ok=True)
    return path


def runtime() -> Runtime:
    """取（必要时启动）内置网关。失败抛 `PlatformError`。"""
    global _RUNTIME
    if _RUNTIME is not None:
        return _RUNTIME
    with _LOCK:
        if _RUNTIME is not None:
            return _RUNTIME
        try:
            _RUNTIME = _boot()
        except Exception:
            # 启动到一半失败（端口占用 / 面板密钥写不进去）时，别把已经拉起的
            # 后台线程和回环服务留在进程里。
            shutdown()
            raise
        return _RUNTIME


def _make_log(msg, level=None, tag=None, *, proxy) -> None:
    """把上游的 `log()` 接到标准 logging（不打印到 stderr）。

    上游的 `log()` 既写 stderr 又进环形缓冲；这里保留环形缓冲（面板日志
    接口仍然能读到），但输出走 logging：每个请求一行的访问日志降级到
    debug，其余按级别走 info / warning。
    """
    entry = proxy.add_log_entry(msg, level=level, tag=tag)
    text = str(msg)
    lvl = str((entry or {}).get('level') or 'INFO').upper()
    if text.lstrip().startswith('"'):
        logger.debug('qoder: %s', text)
    elif lvl in ('ERROR', 'WARN'):
        logger.warning('qoder: %s', text)
    else:
        logger.info('qoder: %s', text)


def _boot() -> Runtime:
    global _PROXY
    accounts = accounts_dir()
    usage = usage_dir()

    # 上游有一批「没有显式参数」的路径推导（机器身份缓存、usage.jsonl）在调用
    # 时才读环境变量；在导入之前设好，避免把状态写进 _vendor/ 源码目录。
    os.environ['ACCOUNTS_DIR'] = str(accounts)
    os.environ['QD_PROXY_USAGE_DIR'] = str(usage)

    try:
        from ._vendor import qoder_accounts, qoder_settings
        from ._vendor import qoder_proxy as proxy
    except Exception as exc:  # noqa: BLE001 —— 依赖缺失 / 副本损坏
        raise PlatformError(
            f'Qoder 内置网关加载失败：{exc}', status=500,
            code='qoder_gateway_load_failed') from exc

    api_key = secrets.token_urlsafe(32)

    # 面板口令：内置网关只监听回环，但同机其他进程也能访问。上游默认口令是
    # `admin`，留着等于把账号池管理接口交给本机任何人。这里首次启动换成随机
    # 口令（只落 PBKDF2 哈希，不存明文）；我们自己用 `PANEL.create()` 签 token，
    # 不需要知道这个口令。
    try:
        if qoder_settings.panel_password_is_default(str(accounts)):
            qoder_settings.set_panel_password(
                str(accounts), secrets.token_urlsafe(32))
    except Exception as exc:  # noqa: BLE001 —— 只影响本机面板登录
        logger.warning('qoder: panel password hardening failed: %s', exc)

    proxy.log = lambda msg, level=None, tag=None: _make_log(
        msg, level=level, tag=tag, proxy=proxy)
    try:
        proxy.qoder_anthropic._bind_log(proxy.log)
    except Exception:  # noqa: BLE001 —— 上游没有这个钩子也不影响主链路
        pass
    try:
        # 上游的模型目录用 print 打降级告警；接进 logging，别污染服务器 stdout。
        proxy.qoder_catalog._warn = lambda msg: logger.warning(
            'qoder-catalog: %s', msg)
    except Exception:  # noqa: BLE001
        pass

    proxy.ACCOUNTS_DIR = str(accounts)
    proxy.REALM_STATE_FILE = str(accounts / 'active_realm.json')
    proxy.USAGE_DIR = str(usage)
    proxy.USAGE_LOG = str(usage / 'usage.jsonl')
    proxy.USAGE_SUMMARY = str(usage / 'usage-summary.json')
    proxy.API_KEY = api_key
    proxy.SYSTEM_PROMPT = proxy.DEFAULT_SYSTEM_PROMPT

    try:
        pool = qoder_accounts.AccountPool(str(accounts), log=proxy.log)
        pool.load()
        proxy.POOL = pool
        # 账号池一就绪就登记到模块全局：后面的启动步骤失败时，shutdown() 才能
        # 找到它并停掉已经启动的刷新线程。
        _PROXY = proxy
        proxy.load_persisted_realm()
        try:
            pool.apply_settings(
                limits_data=qoder_settings.limits_data(str(accounts)),
                free_models=proxy.free_model_names(proxy.CURRENT_REALM))
        except Exception as exc:  # noqa: BLE001 —— 护栏配置坏了不该阻断对话
            logger.warning('qoder: quota limits apply failed: %s', exc)
    except Exception as exc:  # noqa: BLE001
        raise PlatformError(
            f'Qoder 账号池加载失败（{accounts}）：{exc}', status=500,
            code='qoder_gateway_init_failed') from exc

    try:
        refresher = qoder_accounts.CreditsUsageRefresher(pool, log=proxy.log)
        refresher.start()
        proxy.CREDITS_USAGE_REFRESHER = refresher
    except Exception as exc:  # noqa: BLE001 —— 只影响余额展示
        proxy.CREDITS_USAGE_REFRESHER = None
        logger.warning('qoder: credits refresher not started: %s', exc)

    base = _serve(proxy)
    # 面板签名密钥缓存在 PANEL 里。测试 / 进程回收会换账号目录，这里清掉缓存，
    # 让密钥从**当前** accounts/settings.json 重新读取（首次即生成并落盘）。
    try:
        proxy.PANEL._secret = None
    except Exception:  # noqa: BLE001 —— 上游改过内部结构时不影响主链路
        pass
    _issue_panel_token(proxy)
    logger.info('qoder: embedded gateway ready on %s (accounts=%d, realm=%s)',
                base, len(pool.accounts), proxy.CURRENT_REALM)
    return Runtime(base=base, api_key=api_key,
                   accounts_dir=str(accounts), usage_dir=str(usage))


# 面板会话 token 的有效期是 7 天；这里提前 24 小时换一次，避免长驻进程过期。
_TOKEN_TTL = 24 * 3600
_TOKEN = ''
_TOKEN_AT = 0.0


def _issue_panel_token(proxy) -> str:
    """签发并缓存一个面板会话 token（调用方负责持锁）。"""
    global _TOKEN, _TOKEN_AT
    _TOKEN = proxy.PANEL.create()
    _TOKEN_AT = time.time()
    return _TOKEN


def panel_token() -> str:
    """给内置网关管理接口用的面板会话 token（`X-Panel-Token`）。

    不用密码登录：`PanelSessions.create()` 直接签发一个签名 token，签名密钥
    持久化在 `accounts/settings.json`。这样多 worker 部署下每个进程各签一个
    token 都能通过校验，也不需要知道面板口令。

    首次调用会顺带拉起内置网关（`runtime()`），所以调用方不必先手动初始化。
    """
    runtime()
    with _LOCK:
        if _TOKEN and time.time() - _TOKEN_AT < _TOKEN_TTL:
            return _TOKEN
        proxy = _PROXY
        if proxy is None:
            raise PlatformError('Qoder 内置网关尚未启动', status=500,
                                code='qoder_gateway_not_ready')
        return _issue_panel_token(proxy)


def _serve(proxy) -> str:
    """在回环地址的随机空闲端口上启动上游 Handler。"""
    global _SERVER
    from http.server import ThreadingHTTPServer

    try:
        server = ThreadingHTTPServer(('127.0.0.1', 0), proxy.Handler)
    except Exception as exc:  # noqa: BLE001
        raise PlatformError(
            f'Qoder 内置网关无法监听回环端口：{exc}', status=500,
            code='qoder_gateway_start_failed') from exc
    # 请求线程必须跟着进程退出；serve_forever 自己再起一个前台线程。
    server.daemon_threads = True
    host, port = server.server_address[:2]
    thread = threading.Thread(target=server.serve_forever,
                              name='qoder-gateway', daemon=True)
    thread.start()
    _SERVER = server
    global _ATEXIT_REGISTERED
    if not _ATEXIT_REGISTERED:
        # 只注册一次：测试里反复重启网关，重复注册只会让退出时多跑几遍空操作。
        atexit.register(shutdown)
        _ATEXIT_REGISTERED = True
    return f'http://{host}:{port}'


def _stop_refreshers(proxy) -> None:
    """停掉上游的两类后台刷新线程（只影响余额/护栏，不影响数据落盘）。"""
    if proxy is None:
        return
    targets = [getattr(proxy, 'CREDITS_USAGE_REFRESHER', None)]
    pool = getattr(proxy, 'POOL', None)
    if pool is not None:
        targets.append(getattr(pool, 'quota_refresher', None))
    for refresher in targets:
        if refresher is None:
            continue
        try:
            refresher.stop()
        except Exception:  # noqa: BLE001 —— 退出路径上不抛
            pass


def shutdown() -> None:
    """停止内置网关与后台刷新线程（测试与进程退出时用）。"""
    global _RUNTIME, _SERVER, _PROXY, _TOKEN, _TOKEN_AT
    with _LOCK:
        server, _SERVER = _SERVER, None
        proxy, _PROXY = _PROXY, None
        # 后台刷新器持有旧账号池：不停掉的话，重启后它们还会用旧池继续打上游，
        # 测试里也会留下悬空线程（进程退出前一直跑）。在锁内停，避免和新实例
        # 的刷新线程同时写同一批账号文件。
        _stop_refreshers(proxy)
        _RUNTIME = None
        _TOKEN = ''
        _TOKEN_AT = 0.0
    if server is not None:
        try:
            server.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            server.server_close()
        except Exception:  # noqa: BLE001
            pass
