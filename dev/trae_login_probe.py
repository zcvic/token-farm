"""Trae 网页登录链路验证脚本（一次性）。

关键结论（来自授权页 chunk）：
  · 带 code_challenge  → 走 AuthCode 流程，回调给 authCodeInfo，
    但用 AuthCode 换 Token 需要 DeviceProof（实测 20405），本机没有 iCube 私钥；
  · 不带 code_challenge → 走 refreshToken 流程，回调直接给 refreshToken，
    用项目已有的 ExchangeToken 刷新流程即可，无需设备证明。

所以本脚本默认不生成 PKCE，验证「拿 refreshToken → 换 Token → GetUserInfo」。

用法：
  .venv\\Scripts\\python.exe dev\\trae_login_probe.py [--no-browser] [--pkce]

凭据只写到临时目录，不打印明文。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import secrets
import socket
import sys
import threading
import urllib.parse
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TRAE_LOGIN = 'https://www.trae.cn/authorization'
CLIENT_ID = 'ono9krqynydwx5'
PLUGIN_VERSION = '2.3.98528'
APP_VERSION = '3.4.11'
OUT_DIR = Path(r'C:\Users\li\AppData\Local\Temp\opencode')
TIMEOUT_SECONDS = 600

state: dict = {'params': None, 'done': threading.Event(),
               'verifier': '', 'trace': '', 'machine': '', 'device': ''}
SUCCESS_HTML = ('<!doctype html><meta charset="utf-8"><title>授权完成</title>'
                '<body style="font-family:sans-serif;padding:40px">'
                '<h2>授权完成</h2><p>可以回到面板继续了，此窗口可关闭。</p></body>')


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        parts = urllib.parse.urlsplit(self.path)
        if parts.path != '/authorize':
            self.send_response(404)
            self.end_headers()
            return
        params = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
        state['params'] = {k: (v[0] if v else '') for k, v in params.items()}
        state['done'].set()
        body = SUCCESS_HTML.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # noqa: D102
        pass


def mask(value: str) -> str:
    value = str(value or '')
    if len(value) <= 8:
        return '*' * len(value)
    return value[:4] + '*' * (len(value) - 8) + value[-4:]


def fingerprint() -> None:
    import platform
    seed = '%s|%s' % (socket.gethostname(), platform.node())
    state['machine'] = hashlib.sha256(('trae-machine:%s' % seed).encode()).hexdigest()
    digest = int(hashlib.sha256(('trae-device:%s' % seed).encode()).hexdigest()[:14], 16)
    state['device'] = str(10 ** 15 + digest % (9 * 10 ** 15))


def build_url(port: int, pkce: bool) -> str:
    fingerprint()
    state['trace'] = str(uuid.uuid4())
    from server.platforms.trae.fingerprint import _hardware_brand, _os_version
    q = {
        'login_version': '1',
        'auth_from': 'trae',
        'login_channel': 'native_ide',
        'plugin_version': PLUGIN_VERSION,
        'auth_type': 'local',
        'client_id': CLIENT_ID,
        'redirect': '0',
        'login_trace_id': state['trace'],
        'auth_callback_url': f'http://127.0.0.1:{port}/authorize',
        'machine_id': state['machine'],
        'device_id': state['device'],
        'x_device_id': state['device'],
        'x_machine_id': state['machine'],
        'x_device_brand': _hardware_brand(),
        'x_device_type': 'windows',
        'x_os_version': _os_version(),
        'x_env': '',
        'x_app_version': APP_VERSION,
        'x_app_type': 'stable',
        'channel_name': 'common',
    }
    if pkce:
        state['verifier'] = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b'=').decode()
        q['code_challenge'] = base64.urlsafe_b64encode(
            hashlib.sha256(state['verifier'].encode()).digest()).rstrip(b'=').decode()
        q['code_challenge_method'] = 'S256'
    return TRAE_LOGIN + '?' + urllib.parse.urlencode(q, quote_via=urllib.parse.quote)


def exchange_refresh(refresh: str) -> dict:
    from server.platforms.trae import client

    async def run() -> dict:
        account = {'realm': 'cn', 'refreshToken': refresh, 'uid': ''}
        fresh = await client.exchange_token(account)
        info = await client.fetch_user_info(fresh, fresh['token'])
        return {'fresh': fresh, 'info': info}

    return asyncio.run(run())


def exchange_auth_code(auth_code: str) -> dict:
    from server.platforms.trae.fingerprint import _hardware_brand, _os_version
    payload = {
        'ClientID': CLIENT_ID,
        'AuthCode': auth_code,
        'CodeVerifier': state['verifier'],
        'DeviceInfo': {
            'DeviceID': state['device'], 'MachineID': state['machine'],
            'PlatformCode': 'IDE_PC', 'DeviceType': 'PC',
            'DeviceName': socket.gethostname(), 'DeviceModel': _hardware_brand(),
            'ClientVersion': APP_VERSION, 'DevicePublicKey': '',
            'DeviceBrand': _hardware_brand(), 'DeviceCPU': 'Intel',
            'OSInfo': 'Windows', 'OSVersion': _os_version(),
        },
        'IDEVersion': APP_VERSION,
    }
    out = {}
    for url in ('https://api.trae.com.cn/trae/api/v3/oauth/ExchangeToken',
                'https://api.trae.cn/cloudide/api/v3/trae/oauth/ExchangeToken'):
        try:
            r = httpx.post(url, json=payload, headers={'Content-Type': 'application/json'},
                           timeout=30)
            out[url] = {'status': r.status_code, 'body': r.json()}
        except Exception as exc:  # noqa: BLE001
            out[url] = {'error': repr(exc)}
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--pkce', action='store_true')
    args = parser.parse_args()

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    url = build_url(port, args.pkce)
    (OUT_DIR / 'trae_probe_url.txt').write_text(url, encoding='utf-8')
    print('callback: http://127.0.0.1:%d/authorize' % port)
    print('URL:', url)
    print('请在打开的页面里点「登录」按钮（已登录时只差这一次点击）...')

    if not args.no_browser:
        webbrowser.open(url)

    if not state['done'].wait(TIMEOUT_SECONDS):
        print('超时：没有收到回调')
        raise SystemExit(2)

    params = state['params'] or {}
    print('\n回调参数：')
    for key, value in params.items():
        print('  %-18s %s' % (key, value if key in ('scope', 'host', 'userRegion', 'isRedirect')
                              else mask(value)))

    result: dict = {'params': params, 'trace': state['trace'],
                    'device': {'machine': state['machine'], 'device': state['device']}}

    if params.get('loginTraceID') and params['loginTraceID'] != state['trace']:
        print('警告：loginTraceID 与请求不一致')

    refresh = params.get('refreshToken') or params.get('data') or ''
    if refresh:
        try:
            out = exchange_refresh(refresh)
            result['exchange'] = out
            fresh = out['fresh']
            print('ExchangeToken 成功：Token=%s refreshToken=%s' % (
                mask(fresh['token']), mask(fresh['refreshToken'])))
            info = out['info']
            inner = info.get('Result') or info.get('Data') or {}
            user = (inner.get('Data') or {}).get('UserInfo') or {}
            print('GetUserInfo: UserID=%s Name=%s Email=%s' % (
                user.get('UserID'), user.get('Name') or user.get('ScreenName'), user.get('Email')))
        except Exception as exc:  # noqa: BLE001
            print('换 Token 失败:', repr(exc))

    auth_code = ''
    try:
        auth_code = str(json.loads(params.get('authCodeInfo') or '{}').get('AuthCode') or '')
    except Exception:  # noqa: BLE001
        pass
    if auth_code:
        result['exchange'] = exchange_auth_code(auth_code)

    path = OUT_DIR / 'trae_probe_result.json'
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print('\n完整结果（含凭据，勿外传）:', path)


if __name__ == '__main__':
    main()
