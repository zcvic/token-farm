"""Trae 4008（免费额度用尽）→ 429 + `Retry-After` 的契约。

4008 **不是**风控：额度会随套餐重置恢复，账号本身可用。所以这里钉住三件事：

  1. 上游放在 `code` 或 `message` 里的 4008 都能认出来；
  2. 它被改判成 429（而不是 502 / 400 / 403），并带 `Retry-After: 60`；
  3. 4015 / 4019 这类真正的风控**不被**误判成额度用尽。

全部离线：错误帧用固定结构喂给 `aggregate`，网关那层用一个直接抛错的假适配器。
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from server import config, db
from server.platforms.base import PlatformError
from server.platforms.trae import chat as chatmod
from server.platforms.trae import client
from server.routers import anthropic, gateway, responses


def _run(awaitable):  # noqa: ANN001
    return asyncio.run(awaitable)


class QuotaDetectTest(unittest.TestCase):
    def test_code_field_variants(self) -> None:
        for code in (4008, '4008', ' 4008 '):
            self.assertTrue(chatmod.is_quota_error({'code': code}), code)

    def test_message_only(self) -> None:
        self.assertTrue(chatmod.is_quota_error(
            {'message': 'quota exceeded'}))
        self.assertTrue(chatmod.is_quota_error(
            {'message': 'You have exceeded the quota for this plan'}))
        self.assertTrue(chatmod.is_quota_error('[4008] quota exceeded'))
        self.assertTrue(chatmod.is_quota_error('4008'))

    def test_risk_control_is_not_quota(self) -> None:
        for data in ({'code': 4015, 'message': 'risk control'},
                     {'code': 4019, 'message': 'risk_control'},
                     {'code': 4023, 'message': 'model is unknown'},
                     {'message': 'not able to authenticate you'},
                     {}, None, ''):
            self.assertFalse(chatmod.is_quota_error(data), data)

    def test_error_from_event_carries_429(self) -> None:
        err = chatmod.error_from_event({'code': 4008,
                                        'message': 'quota exceeded'})
        self.assertEqual(err.status, 429)
        self.assertEqual(err.code, 'quota_exceeded')
        self.assertEqual(err.retry_after, chatmod.QUOTA_RETRY_AFTER)
        self.assertIn('4008', err.message)
        self.assertIn('quota exceeded', err.message.lower())

    def test_other_errors_keep_502(self) -> None:
        err = chatmod.error_from_event({'code': 4015, 'message': 'risk control'})
        self.assertEqual(err.status, 502)
        self.assertEqual(err.code, 'upstream_error')
        self.assertIsNone(err.retry_after)


class HttpErrorTest(unittest.TestCase):
    def test_4008_in_403_body_becomes_429(self) -> None:
        err = client._http_error(
            403, json.dumps({'code': 4008, 'message': 'quota exceeded'}))
        self.assertEqual(err.status, 429)
        self.assertEqual(err.code, 'quota_exceeded')
        self.assertEqual(err.retry_after, 60)

    def test_4008_nested_in_response_metadata(self) -> None:
        body = json.dumps({'ResponseMetadata': {
            'Error': {'Code': '4008', 'Message': 'exceeded the quota'}}})
        err = client._http_error(400, body)
        self.assertEqual(err.status, 429)
        self.assertEqual(err.retry_after, 60)

    def test_other_http_errors_unchanged(self) -> None:
        err = client._http_error(403, json.dumps(
            {'code': 4015, 'message': 'risk control'}))
        self.assertEqual(err.status, 403)
        self.assertEqual(err.code, '4015')
        self.assertIsNone(err.retry_after)


class AggregateQuotaTest(unittest.TestCase):
    def _aggregate(self, payload: object):  # noqa: ANN202
        async def events():  # noqa: ANN202
            yield 'error', payload

        return _run(chatmod.aggregate(
            events(), model='trae/m', state=chatmod.StreamState()))

    def test_error_frame_without_content_raises_429(self) -> None:
        with self.assertRaises(PlatformError) as ctx:
            self._aggregate({'code': 4008, 'message': 'quota exceeded'})
        self.assertEqual(ctx.exception.status, 429)
        self.assertEqual(ctx.exception.code, 'quota_exceeded')
        self.assertEqual(ctx.exception.retry_after, 60)

    def test_non_quota_error_stays_upstream_error(self) -> None:
        with self.assertRaises(PlatformError) as ctx:
            self._aggregate({'code': 4015, 'message': 'risk control'})
        self.assertEqual(ctx.exception.status, 502)
        self.assertEqual(ctx.exception.code, 'upstream_error')


class _QuotaAdapter:
    """直接抛 4008 的假适配器：只覆盖 `_platform_chat` 用到的接口。"""

    name = 'trae'
    label = 'Trae'

    def record_name(self, model: str) -> str:
        return f'trae/{model}'

    def realm_of_model(self, model: str) -> str:
        return 'cn'

    async def chat(self, model, body, ctx=None):  # noqa: ANN001, ANN202
        raise chatmod.error_from_event({'code': 4008,
                                        'message': 'quota exceeded'})


class GatewayQuotaContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._orig_db = config.DB_PATH
        config.DB_PATH = Path(self.tmp.name) / 'q.db'
        db._conn = None
        db.connect()

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db

    def test_chat_completions_returns_429_with_retry_after(self) -> None:
        resp = _run(gateway._platform_chat(
            adapter=_QuotaAdapter(), bare_model='m', body={}, key=None,
            ip='127.0.0.1', requested_model='trae/m', ua=None, stream=False,
            effort=None, ctx=None))
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.headers['retry-after'], '60')
        body = json.loads(resp.body)
        self.assertEqual(body['error']['code'], 'quota_exceeded')
        self.assertEqual(body['error']['type'], 'insufficient_quota')
        self.assertIn('4008', body['error']['message'])

    def test_platform_err_type_keeps_other_429_untouched(self) -> None:
        self.assertEqual(gateway._platform_err_type(429, 'pool_busy'),
                         'invalid_request_error')
        self.assertEqual(gateway._platform_err_type(429, 'quota_exceeded'),
                         'insufficient_quota')
        self.assertEqual(gateway._platform_err_type(502, 'upstream_error'),
                         'api_error')

    def test_responses_and_anthropic_forward_retry_after(self) -> None:
        oai = responses._failed('额度用尽', 429, 'insufficient_quota',
                                'quota_exceeded', retry_after=60)
        self.assertEqual(oai.headers['retry-after'], '60')
        anth = anthropic._err('额度用尽', 429, 'insufficient_quota',
                              retry_after=60)
        self.assertEqual(anth.headers['retry-after'], '60')
        self.assertEqual(json.loads(anth.body)['error']['type'],
                         'insufficient_quota')


if __name__ == '__main__':
    unittest.main()
