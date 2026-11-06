"""同步自 workbuddy2api-panel 的四项能力的单元测试。

覆盖范围刻意只压**纯逻辑**，不打真实数据库 / 网络：

  1. `X-Request-Id` 中间件：生成、沿用、拒绝非法值、响应头写入、contextvar；
  2. Prometheus 文本：计数、成功率、Token / 缓存 / 积分口径、转义；
  3. 隐藏模型：裸名与 `cn:` / `global:` 前缀的匹配、列表裁剪；
  4. 通用 Webhook：请求体契约、HMAC 签名、未启用时跳过、失败不抛。
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.services import metrics, modelpolicy, requestctx  # noqa: E402
from server.services import watchdog  # noqa: E402


class RequestIdTest(unittest.TestCase):
    def _run(self, inbound: list[tuple[bytes, bytes]]) -> tuple[dict, list[dict]]:
        seen: dict = {}

        async def app(scope, receive, send):
            seen['id'] = requestctx.current()
            seen['state'] = scope.get('state', {}).get('request_id')
            await send({'type': 'http.response.start', 'status': 200,
                        'headers': [(b'content-type', b'text/plain')]})
            await send({'type': 'http.response.body', 'body': b'ok'})

        messages: list[dict] = []

        async def send(message):
            messages.append(message)

        middleware = requestctx.RequestIdMiddleware(app)
        scope = {'type': 'http', 'path': '/v1/models', 'headers': inbound}
        asyncio.run(middleware(scope, lambda: None, send))
        return seen, messages

    def _header(self, messages: list[dict]) -> str:
        start = next(m for m in messages if m['type'] == 'http.response.start')
        values = [v.decode() for k, v in start['headers']
                  if k.lower() == b'x-request-id']
        self.assertEqual(len(values), 1)
        return values[0]

    def test_generates_when_absent(self) -> None:
        seen, messages = self._run([])
        rid = self._header(messages)
        self.assertEqual(len(rid), 32)
        self.assertEqual(seen['id'], rid)
        self.assertEqual(seen['state'], rid)

    def test_reuses_valid_inbound(self) -> None:
        seen, messages = self._run([(b'x-request-id', b'req-0123456789')])
        self.assertEqual(seen['id'], 'req-0123456789')
        self.assertEqual(self._header(messages), 'req-0123456789')

    def test_replaces_invalid_inbound(self) -> None:
        seen, messages = self._run([(b'x-request-id', b'bad\r\ninjected')])
        self.assertNotEqual(seen['id'], 'bad\r\ninjected')
        self.assertEqual(self._header(messages), seen['id'])

    def test_prefers_custom_header_order(self) -> None:
        seen, _ = self._run([
            (b'x-correlation-id', b'corr-0123456789'),
            (b'x-request-id', b'req-0123456789'),
        ])
        self.assertEqual(seen['id'], 'req-0123456789')


class MetricsTest(unittest.TestCase):
    def setUp(self) -> None:
        metrics.reset()

    def tearDown(self) -> None:
        metrics.reset()

    def test_counters_and_rates(self) -> None:
        metrics.note_request(
            model='glm-5.2', status=200, stream=True,
            prompt_tokens=10, completion_tokens=20, first_token_ms=100,
            latency_ms=1100, cache_hit=5, cache_miss=3, credit=1.5)
        metrics.note_request(
            model='glm-5.2', status=429, prompt_tokens=0, completion_tokens=0,
            latency_ms=50)
        out = metrics.render(version='1.2.3')
        self.assertIn('tokenfarm_build_info{version="1.2.3"} 1', out)
        self.assertIn('tokenfarm_requests_total{status="success"} 1', out)
        self.assertIn('tokenfarm_requests_total{status="failed"} 1', out)
        self.assertIn('tokenfarm_model_requests_total{model="glm-5.2",status="success"} 1', out)
        self.assertIn('tokenfarm_model_tokens_total{model="glm-5.2",type="completion"} 20', out)
        self.assertIn('tokenfarm_model_prompt_cache_tokens_total{model="glm-5.2",type="hit"} 5', out)
        self.assertIn('tokenfarm_model_credit_total{model="glm-5.2"} 1.5', out)
        # TTFB 只取有观测的那一次；延迟均值取全部两次。
        self.assertIn('tokenfarm_model_avg_ttfb_seconds{model="glm-5.2"} 0.1', out)
        self.assertIn('tokenfarm_model_avg_latency_seconds{model="glm-5.2"} 0.575', out)

    def test_escapes_labels(self) -> None:
        metrics.note_request(model='a"b\\c', status=200)
        out = metrics.render()
        self.assertIn('model="a\\"b\\\\c"', out)

    def test_empty_model_is_labelled_dash(self) -> None:
        metrics.note_request(model='', status=200)
        self.assertIn('tokenfarm_model_requests_total{model="-",status="success"} 1',
                      metrics.render())


class ModelPolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self._patch = mock.patch.object(
            modelpolicy.db, 'get_setting',
            return_value={'hidden': ['glm-5.2', 'global:gpt-9']})
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()

    def test_bare_name_hides_both_realms(self) -> None:
        self.assertTrue(modelpolicy.is_hidden('glm-5.2'))
        self.assertTrue(modelpolicy.is_hidden('cn:glm-5.2'))
        self.assertTrue(modelpolicy.is_hidden('global:glm-5.2'))

    def test_realm_prefix_hides_only_that_realm(self) -> None:
        self.assertFalse(modelpolicy.is_hidden('gpt-9'))
        self.assertFalse(modelpolicy.is_hidden('cn:gpt-9'))
        self.assertTrue(modelpolicy.is_hidden('global:gpt-9'))

    def test_filter_payload_keeps_unknown_shape(self) -> None:
        payload = {'data': [
            {'id': 'glm-5.2'}, {'id': 'global:gpt-9'}, {'id': 'kimi-k2'}],
            'object': 'list'}
        out = modelpolicy.filter_payload(payload)
        self.assertEqual([m['id'] for m in out['data']], ['kimi-k2'])
        self.assertEqual(out['object'], 'list')
        weird = ['not-a-dict']
        self.assertIs(modelpolicy.filter_payload(weird), weird)

    def test_save_normalizes_duplicates_and_empty(self) -> None:
        with mock.patch.object(modelpolicy.db, 'set_setting') as setter:
            result = modelpolicy.save({'hidden': [' A ', 'a', '', 3, 'B']})
        self.assertEqual(result, {'hidden': ['A', 'B']})
        setter.assert_called_once_with(modelpolicy.SETTING_KEY, {'hidden': ['A', 'B']})


class _FakeResponse:
    status_code = 204
    text = ''


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, *, content, headers):
        self.calls.append({'url': url, 'content': content, 'headers': headers})
        return _FakeResponse()


class WebhookTest(unittest.TestCase):
    def test_payload_and_signature(self) -> None:
        client = _FakeClient()
        cfg = {'webhook': {'enabled': True, 'url': 'https://hooks.example/x',
                           'secret': 's3cret', 'timeout_seconds': 5}}
        with mock.patch.object(watchdog.config, 'http_client',
                               lambda *a, **k: client):
            result = asyncio.run(watchdog.send_webhook(
                'subj', 'body', cfg=cfg, extra={'target': 'plans-usage'}))
        self.assertTrue(result['ok'])
        self.assertEqual(result['status'], 204)
        call = client.calls[0]
        self.assertEqual(call['url'], 'https://hooks.example/x')
        payload = json.loads(call['content'])
        self.assertEqual(payload['source'], 'token-farm')
        self.assertEqual(payload['event'], 'watchdog.change')
        self.assertEqual(payload['subject'], 'subj')
        self.assertEqual(payload['target'], 'plans-usage')
        expected = hmac.new(b's3cret', call['content'],
                            hashlib.sha256).hexdigest()
        self.assertEqual(call['headers']['X-WorkBuddy-Signature'],
                         f'sha256={expected}')

    def test_disabled_is_skipped(self) -> None:
        result = asyncio.run(watchdog.send_webhook(
            's', 'b', cfg={'webhook': {'enabled': False, 'url': ''}}))
        self.assertFalse(result['ok'])
        self.assertIn('未启用', result['skipped'])

    def test_network_error_does_not_raise(self) -> None:
        class Boom:
            async def __aenter__(self):
                raise OSError('boom')

            async def __aexit__(self, *exc):
                return False

        cfg = {'webhook': {'enabled': True, 'url': 'https://x/y',
                           'secret': '', 'timeout_seconds': 5}}
        with mock.patch.object(watchdog.config, 'http_client', lambda *a, **k: Boom()):
            result = asyncio.run(watchdog.send_webhook('s', 'b', cfg=cfg))
        self.assertFalse(result['ok'])
        self.assertIn('boom', result['error'])


if __name__ == '__main__':
    unittest.main()
