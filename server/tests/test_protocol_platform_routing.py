"""`/v1/responses` 与 `/v1/messages` 的平台 / 组合路由（补缺口）。

历史缺口：这两条协议层各自把请求转成 OpenAI Chat Completions 后**直接反代
上游**，从不经过 `platforms.route_model`。于是 Claude Code 用不了 Trae 模型，
也用不了组合——而 `/v1/chat/completions` 早就支持。

本文件锁定的性质：

  · 平台 / 组合模型名在这两条协议上都能被解析，鉴权阶段跳过版本检查
    （`platform_model=True`），否则限定版本的密钥会被 `combo/fast` 误拒；
  · 适配器返回的 OpenAI 结果被**同一套**翻译层消费（非流式 / dict chunk 流 /
    原始 SSE 字节流三种形态），协议语义不会因为走了组合而变成第二套；
  · 记账只做一次，且 `mapped_model` 写**实际去向**（组合成员名），日志的
    `platform` / `realm` 两列才与直连一致；
  · `PlatformError` 按各协议的错误体返回（OpenAI / Anthropic 结构不同）；
  · `/v1/messages/count_tokens` 同样要认平台模型（客户端发消息前会先数 token）。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from server import config, db, keysvc, platforms, security  # noqa: E402
from server.platforms.base import ChatContext, PlatformChat, PlatformError  # noqa: E402
from server.routers import gateway  # noqa: E402


def _completion(text: str = 'hello', usage: dict | None = None) -> dict:
    return {
        'id': 'chatcmpl-test',
        'object': 'chat.completion',
        'created': 0,
        'model': 'upstream',
        'choices': [{
            'index': 0,
            'message': {'role': 'assistant', 'content': text},
            'finish_reason': 'stop',
        }],
        'usage': usage or {'prompt_tokens': 3, 'completion_tokens': 2},
    }


def _chunks(text: str = 'hello', usage: dict | None = None) -> list[dict]:
    return [
        {'id': 'c1', 'object': 'chat.completion.chunk', 'created': 0,
         'choices': [{'index': 0, 'delta': {'role': 'assistant'}}]},
        {'id': 'c1', 'object': 'chat.completion.chunk', 'created': 0,
         'choices': [{'index': 0, 'delta': {'content': text}}]},
        {'id': 'c1', 'object': 'chat.completion.chunk', 'created': 0,
         'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}],
         'usage': usage or {'prompt_tokens': 5, 'completion_tokens': 2}},
    ]


def _raw_sse(text: str = 'hello', usage: dict | None = None) -> list[bytes]:
    objs = _chunks(text, usage)
    out = [f'data: {json.dumps(o, ensure_ascii=False)}\n\n'.encode() for o in objs]
    out.append(b'data: [DONE]\n\n')
    return out


async def _aiter(items):
    for item in items:
        yield item


class _BaseCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = (config.DB_PATH, config.USERS_FILE, config.WB2API_BASE)
        config.DB_PATH = Path(self._tmp.name) / 'proto.db'
        config.USERS_FILE = Path(self._tmp.name) / 'users.json'
        config.WB2API_BASE = 'http://127.0.0.1:1'
        db._conn = None
        db.connect()
        security.save_users({
            'secret': 'S',
            'users': [{'username': 'admin', 'role': 'admin',
                       'pwd_hash': security.make_hash('p')}],
            'api_keys': [],
        })
        from server.main import app
        self.client = TestClient(app)
        self.key = keysvc.create_key('t')['key']

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH, config.USERS_FILE, config.WB2API_BASE = self._orig
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _post(self, path: str, payload: dict, *, key: str | None = None,
              anthropic: bool = False):
        headers = {'Authorization': f'Bearer {key or self.key}'}
        if anthropic:
            headers = {'x-api-key': key or self.key,
                       'anthropic-version': '2023-06-01'}
        return self.client.post(path, headers=headers, json=payload)


class ResponsesPlatformRoutingTest(_BaseCase):
    def test_non_stream_trae_uses_adapter_and_records_real_target(self) -> None:
        seen: dict = {}
        adapter = platforms.get('trae')

        async def fake_chat(model, body, ctx=None):
            seen['model'] = model
            seen['body'] = body
            seen['ctx'] = ctx
            return PlatformChat(status=200, payload=_completion(),
                                usage={'prompt_tokens': 3, 'completion_tokens': 2},
                                model='trae/deepseek-v3')

        with mock.patch.object(adapter, 'chat', fake_chat), \
                mock.patch.object(gateway, '_record') as record:
            resp = self._post('/v1/responses',
                              {'model': 'trae/deepseek-v3', 'input': 'hi'})

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn('hello', resp.text)
        self.assertEqual(seen['model'], 'deepseek-v3', '适配器只拿裸模型名')
        self.assertEqual(seen['ctx'].upstream_path, '/v1/chat/completions')
        self.assertIsInstance(seen['ctx'], ChatContext)
        record.assert_called_once()
        self.assertEqual(record.call_args.args[2], 'trae/deepseek-v3',
                         '请求名照原样记')
        self.assertEqual(record.call_args.args[3], 'trae/deepseek-v3',
                         'mapped_model 是实际去向')
        self.assertEqual(record.call_args.args[4], 200)

    def test_dict_chunk_stream_is_translated(self) -> None:
        adapter = platforms.get('combo')

        async def fake_chat(model, body, ctx=None):
            return PlatformChat(status=200, stream=_aiter(_chunks()),
                                usage={'prompt_tokens': 5, 'completion_tokens': 2},
                                model='trae/deepseek-v3')

        with mock.patch.object(adapter, 'chat', fake_chat), \
                mock.patch.object(gateway, '_record') as record:
            resp = self._post('/v1/responses',
                              {'model': 'combo/fast', 'input': 'hi', 'stream': True})

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn('response.output_text.delta', resp.text)
        self.assertIn('hello', resp.text)
        self.assertIn('response.completed', resp.text)
        record.assert_called_once()
        self.assertEqual(record.call_args.args[3], 'trae/deepseek-v3')
        self.assertEqual(record.call_args.kwargs['usage'],
                         {'prompt_tokens': 5, 'completion_tokens': 2})

    def test_raw_byte_stream_is_translated(self) -> None:
        """WorkBuddy 成员是原始 SSE 字节：不能当成 dict chunk 再编码一遍。"""
        adapter = platforms.get('combo')
        usage = {'prompt_tokens': 5, 'completion_tokens': 2}

        async def fake_chat(model, body, ctx=None):
            chat = PlatformChat(status=200, model='glm-5.2', usage=usage)

            async def relay():
                for chunk in _raw_sse('hello', usage):
                    yield chunk

            chat.raw_stream = relay()
            return chat

        with mock.patch.object(adapter, 'chat', fake_chat), \
                mock.patch.object(gateway, '_record') as record:
            resp = self._post('/v1/responses',
                              {'model': 'combo/fast', 'input': 'hi', 'stream': True})

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn('hello', resp.text)
        self.assertIn('response.completed', resp.text)
        record.assert_called_once()
        self.assertEqual(record.call_args.args[3], 'glm-5.2',
                         '成员裸名 → 日志 platform 列为空（WorkBuddy 归属）')

    def test_platform_error_keeps_status_and_code(self) -> None:
        adapter = platforms.get('trae')

        async def fake_chat(model, body, ctx=None):
            raise PlatformError('并发已满', status=429, code='pool_busy')

        with mock.patch.object(adapter, 'chat', fake_chat), \
                mock.patch.object(gateway, '_record') as record:
            resp = self._post('/v1/responses',
                              {'model': 'trae/deepseek-v3', 'input': 'hi'})

        self.assertEqual(resp.status_code, 429, resp.text)
        body = resp.json()
        self.assertEqual(body['error']['code'], 'pool_busy')
        record.assert_called_once()
        self.assertEqual(record.call_args.args[4], 429)

    def test_upstream_error_payload_message_is_extracted(self) -> None:
        """WorkBuddy 的失败是「payload + error 原文」，客户端该看到 message 而不是 JSON 串。"""
        adapter = platforms.get('combo')

        async def fake_chat(model, body, ctx=None):
            return PlatformChat(
                status=429,
                payload={'error': {'message': '上游限流：请稍后重试', 'code': '6004'}},
                error="{'error': {'message': '上游限流：请稍后重试', 'code': '6004'}}",
                model='glm-5.2')

        with mock.patch.object(adapter, 'chat', fake_chat), \
                mock.patch.object(gateway, '_record') as record:
            resp = self._post('/v1/responses',
                              {'model': 'combo/fast', 'input': 'hi'})

        self.assertEqual(resp.status_code, 429, resp.text)
        self.assertEqual(resp.json()['error']['message'], '上游限流：请稍后重试')
        record.assert_called_once()
        self.assertEqual(record.call_args.args[3], 'glm-5.2')

    def test_global_key_can_use_platform_model(self) -> None:
        """平台模型不属于任何版本：限定国际版的密钥不该在鉴权阶段被拒。"""
        token = keysvc.create_key('g', realm='global')['key']
        adapter = platforms.get('combo')

        async def fake_chat(model, body, ctx=None):
            return PlatformChat(status=200, payload=_completion(), model='trae/x')

        with mock.patch.object(adapter, 'chat', fake_chat):
            resp = self._post('/v1/responses',
                              {'model': 'combo/fast', 'input': 'hi'}, key=token)

        self.assertEqual(resp.status_code, 200, resp.text)


class AnthropicPlatformRoutingTest(_BaseCase):
    def _messages(self, payload: dict, key: str | None = None):
        return self._post('/v1/messages', payload, key=key, anthropic=True)

    def test_non_stream_trae_translates_to_messages(self) -> None:
        seen: dict = {}
        adapter = platforms.get('trae')

        async def fake_chat(model, body, ctx=None):
            seen['model'] = model
            return PlatformChat(status=200, payload=_completion(),
                                usage={'prompt_tokens': 3, 'completion_tokens': 2},
                                model='trae/deepseek-v3')

        with mock.patch.object(adapter, 'chat', fake_chat), \
                mock.patch.object(gateway, '_record') as record:
            resp = self._messages({'model': 'trae/deepseek-v3', 'max_tokens': 16,
                                   'messages': [{'role': 'user', 'content': 'hi'}]})

        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body['type'], 'message')
        self.assertEqual(body['content'][0]['text'], 'hello')
        self.assertEqual(seen['model'], 'deepseek-v3')
        record.assert_called_once()
        self.assertEqual(record.call_args.args[3], 'trae/deepseek-v3')

    def test_dict_chunk_stream_is_translated(self) -> None:
        adapter = platforms.get('combo')

        async def fake_chat(model, body, ctx=None):
            return PlatformChat(status=200, stream=_aiter(_chunks()),
                                usage={'prompt_tokens': 5, 'completion_tokens': 2},
                                model='trae/deepseek-v3')

        with mock.patch.object(adapter, 'chat', fake_chat), \
                mock.patch.object(gateway, '_record') as record:
            resp = self._messages({'model': 'combo/fast', 'max_tokens': 16,
                                   'stream': True,
                                   'messages': [{'role': 'user', 'content': 'hi'}]})

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn('content_block_delta', resp.text)
        self.assertIn('hello', resp.text)
        self.assertIn('message_stop', resp.text)
        record.assert_called_once()
        self.assertEqual(record.call_args.args[3], 'trae/deepseek-v3')

    def test_raw_byte_stream_is_translated(self) -> None:
        adapter = platforms.get('combo')
        usage = {'prompt_tokens': 5, 'completion_tokens': 2}

        async def fake_chat(model, body, ctx=None):
            chat = PlatformChat(status=200, model='glm-5.2', usage=usage)

            async def relay():
                for chunk in _raw_sse('hello', usage):
                    yield chunk

            chat.raw_stream = relay()
            return chat

        with mock.patch.object(adapter, 'chat', fake_chat), \
                mock.patch.object(gateway, '_record') as record:
            resp = self._messages({'model': 'combo/fast', 'max_tokens': 16,
                                   'stream': True,
                                   'messages': [{'role': 'user', 'content': 'hi'}]})

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn('hello', resp.text)
        self.assertIn('message_stop', resp.text)
        record.assert_called_once()
        self.assertEqual(record.call_args.args[3], 'glm-5.2')

    def test_platform_error_uses_anthropic_error_body(self) -> None:
        adapter = platforms.get('trae')

        async def fake_chat(model, body, ctx=None):
            raise PlatformError('并发已满', status=429, code='pool_busy')

        with mock.patch.object(adapter, 'chat', fake_chat), \
                mock.patch.object(gateway, '_record') as record:
            resp = self._messages({'model': 'trae/deepseek-v3', 'max_tokens': 16,
                                   'messages': [{'role': 'user', 'content': 'hi'}]})

        self.assertEqual(resp.status_code, 429, resp.text)
        body = resp.json()
        self.assertEqual(body['type'], 'error')
        self.assertEqual(body['error']['message'], '并发已满')
        record.assert_called_once()
        self.assertEqual(record.call_args.args[4], 429)

    def test_global_key_can_use_combo(self) -> None:
        token = keysvc.create_key('g', realm='global')['key']
        adapter = platforms.get('combo')

        async def fake_chat(model, body, ctx=None):
            return PlatformChat(status=200, payload=_completion(), model='trae/x')

        with mock.patch.object(adapter, 'chat', fake_chat):
            resp = self._messages({'model': 'combo/fast', 'max_tokens': 16,
                                   'messages': [{'role': 'user', 'content': 'hi'}]},
                                  key=token)

        self.assertEqual(resp.status_code, 200, resp.text)

    def test_count_tokens_accepts_platform_model(self) -> None:
        """客户端发消息前先数 token，同一个模型名不能在这一步被版本检查打回。"""
        token = keysvc.create_key('g', realm='global')['key']
        resp = self._post('/v1/messages/count_tokens', {
            'model': 'combo/fast',
            'messages': [{'role': 'user', 'content': 'hi'}],
        }, key=token, anthropic=True)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertGreater(resp.json()['input_tokens'], 0)


if __name__ == '__main__':
    unittest.main()
