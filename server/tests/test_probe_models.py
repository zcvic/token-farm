"""账号 × 模型主动探测（issue #270）。

钉住两件事：

  1. **分类口径**与上游一致：6004 是模型限流、11102 / 14401 / 11158 是「该账号
     用不了这个模型」（不是错误）、401/12153 是凭证失效、402/14018 是额度不足；
  2. 组合数上限与模型名去前缀（`cn:` / `global:` 同名合并）。
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.routers import accounts as accounts_router  # noqa: E402
from server.services import tencent  # noqa: E402


class ClassifyProbeResultTest(unittest.TestCase):
    def test_ok(self) -> None:
        self.assertEqual(
            tencent.classify_probe_result(True, '连通正常（glm-5.2，120ms）'),
            ('ok', None),
        )

    def test_no_model_is_not_an_error(self) -> None:
        status, code = tencent.classify_probe_result(
            False, '上游返回 code=11102 该后端无此模型')
        self.assertEqual((status, code), ('no_model', '11102'))

    def test_model_missing_and_forbidden_are_not_errors(self) -> None:
        """11102 / 14401 是模型 id 不存在，11158 是账号类型无权限。

        三者对该账号都是「用不了这个模型」，不是账号故障：探测结果归
        `no_model`，不计入错误统计（面板据此提示换模型 / 换号，而不是报红）。
        """
        for code in ('11102', '14401', '11158'):
            status, got = tencent.classify_probe_result(
                False, f'上游返回 code={code} 模型不可用')
            self.assertEqual((status, got), ('no_model', code))

    def test_model_rate_limit(self) -> None:
        self.assertEqual(
            tencent.classify_probe_result(False, '上游返回 code=6004 限流')[0],
            'rate',
        )

    def test_account_rate_limit_without_code(self) -> None:
        self.assertEqual(
            tencent.classify_probe_result(False, '上游返回 code=429 too many')[0],
            'rate',
        )

    def test_credit(self) -> None:
        self.assertEqual(
            tencent.classify_probe_result(False, '上游返回 code=14018 积分耗尽')[0],
            'credit',
        )
        self.assertEqual(
            tencent.classify_probe_result(False, '上游返回 code=402 请充值')[0],
            'credit',
        )
        self.assertEqual(
            tencent.classify_probe_result(False, '请求异常: 积分不足，请充值')[0],
            'credit',
        )

    def test_auth(self) -> None:
        self.assertEqual(
            tencent.classify_probe_result(False, '上游返回 code=12153 会话已失效')[0],
            'auth',
        )
        self.assertEqual(
            tencent.classify_probe_result(False, '上游返回 code=401')[0],
            'auth',
        )

    def test_other_errors(self) -> None:
        self.assertEqual(
            tencent.classify_probe_result(False, '上游返回 code=11115 prompt 过长')[0],
            'error',
        )
        self.assertEqual(
            tencent.classify_probe_result(False, '请求异常: timeout')[0],
            'error',
        )


class NestedErrorEnvelopeTest(unittest.TestCase):
    """业务码可能套在 `error` / `error.data` 里，解析器必须挖到它。

    实测上游有这种形态（`{"error":{"data":{"code":14018}}}`）。只读顶层 `code`
    会把它报成 `code=400`，探测结果落 `error` 而不是 `credit`——界面上只说
    「上游返回 code=400」，真正的成因（积分耗尽 / 会话失效）被吞掉。
    """

    def test_top_level_envelope(self) -> None:
        self.assertEqual(
            tencent._parse_error_body('{"code":14018,"msg":"credit exhausted"}', 400),
            (14018, 'credit exhausted'),
        )

    def test_error_wrapped_code(self) -> None:
        code, msg = tencent._parse_error_body(
            '{"error":{"code":14018,"message":"credit exhausted"}}', 400)
        self.assertEqual(code, 14018)
        self.assertEqual(msg, 'credit exhausted')

    def test_error_data_wrapped_code(self) -> None:
        """`error.data.code` —— 上游最深的一种嵌套。"""
        code, msg = tencent._parse_error_body(
            '{"error":{"data":{"code":14018},"message":"credit exhausted"}}', 400)
        self.assertEqual(code, 14018)
        self.assertEqual(msg, 'credit exhausted', 'msg 从父节点继承')

    def test_string_code_preserved(self) -> None:
        """`11-128` 这类带横线的字符串码不能被 int() 化。"""
        code, _ = tencent._parse_error_body('{"code":"11-128","msg":"x"}', 400)
        self.assertEqual(code, '11-128')

    def test_nested_code_classifies_as_credit(self) -> None:
        """端到端：嵌套码经 _explain_code 后必须分类成 credit。"""
        code, msg = tencent._parse_error_body(
            '{"error":{"data":{"code":14018},"message":"credit exhausted"}}', 429)
        status, got = tencent.classify_probe_result(False, tencent._explain_code(code, msg))
        self.assertEqual((status, got), ('credit', '14018'))

    def test_nested_code_classifies_as_auth(self) -> None:
        code, msg = tencent._parse_error_body(
            '{"error":{"code":12153,"message":"session expired"}}', 401)
        status, got = tencent.classify_probe_result(False, tencent._explain_code(code, msg))
        self.assertEqual((status, got), ('auth', '12153'))

    def test_plain_text_falls_back_to_status(self) -> None:
        self.assertEqual(tencent._parse_error_body('upstream boom', 502), (502, 'upstream boom'))

    def test_json_without_code_falls_back_to_status(self) -> None:
        """能解析成 JSON 但没有业务码：用 HTTP 状态码，不把整段 JSON 当文案。"""
        self.assertEqual(tencent._parse_error_body('{"detail":"nope"}', 403), (403, ''))

    def test_nested_message_without_code_is_kept(self) -> None:
        """没有业务码时，嵌套的 message 是唯一可读成因，不能丢。

        丢掉它会把 `{"error":{"message":"insufficient credit"}}` 变成
        「上游返回 code=400」，额度不足就归不到 credit。
        """
        code, msg = tencent._parse_error_body(
            '{"error":{"message":"insufficient credit"}}', 400)
        self.assertEqual(code, 400)
        self.assertEqual(msg, 'insufficient credit')
        self.assertEqual(
            tencent.classify_probe_result(False, tencent._explain_code(code, msg))[0],
            'credit')

    def test_shallow_code_wins_over_deep_one(self) -> None:
        """BFS 而非 DFS：浅层的 code 才是业务码，别先钻进列表里的无关 code。"""
        raw = '{"data":{"items":[{"code":999}]},"code":11158,"msg":"no permission"}'
        self.assertEqual(tencent._parse_error_body(raw, 400), (11158, 'no permission'))


class ProbeEndpointTest(unittest.TestCase):
    RAW = {
        'account': {'uid': 'u1', 'nickname': '猫猫', 'enterpriseId': ''},
        'auth': {'accessToken': 'tok', 'realm': 'cn', 'domain': ''},
        'device_token': '',
    }

    def _run(self, body, outcomes=None, raw=None):
        calls: list[str] = []
        outcomes = outcomes or {}

        async def fake_probe(auth, model):
            calls.append(model)
            status, code = outcomes.get(model, ('ok', None))
            return {'status': status, 'code': code, 'message': f'{model}:{status}',
                    'latency_ms': 1}

        with mock.patch.object(accounts_router, '_load',
                               lambda fn, d: raw or self.RAW), \
             mock.patch.object(accounts_router.tencent, 'probe_model', fake_probe), \
             mock.patch.object(accounts_router, '_PROBE_ACCOUNT_GAP_SECONDS', 0):
            out = asyncio.run(accounts_router.probe_models(
                body, upstream_id=None, user={'role': 'admin'}))
        return out, calls

    def test_matrix_dedupes_prefixed_models(self) -> None:
        out, calls = self._run({
            'accounts': ['a.json'],
            'models': ['cn:glm-5.2', 'global:glm-5.2', 'deepseek-v4.1-flash'],
        }, outcomes={'glm-5.2': ('rate', '6004'),
                     'deepseek-v4.1-flash': ('no_model', '11102')})
        self.assertEqual(calls, ['glm-5.2', 'deepseek-v4.1-flash'],
                         'cn:/global: 同名模型合并成一条')
        self.assertEqual(out['requests'], 2)
        self.assertEqual(out['summary']['rate'], 1)
        self.assertEqual(out['summary']['no_model'], 1)
        self.assertEqual(out['results'][0]['nickname'], '猫猫')
        self.assertEqual(out['results'][0]['realm'], 'cn')

    def test_combo_limit(self) -> None:
        body = {'accounts': [f'{i}.json' for i in range(7)],
                'models': [f'm{i}' for i in range(10)]}
        with self.assertRaises(accounts_router.HTTPException) as ctx:
            self._run(body)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn('组合', str(ctx.exception.detail))

    def test_account_limit(self) -> None:
        body = {'accounts': [f'{i}.json' for i in range(21)], 'models': ['m']}
        with self.assertRaises(accounts_router.HTTPException) as ctx:
            self._run(body)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_requires_both_sides(self) -> None:
        with self.assertRaises(accounts_router.HTTPException) as ctx:
            self._run({'accounts': ['a.json'], 'models': []})
        self.assertEqual(ctx.exception.status_code, 400)


if __name__ == '__main__':
    unittest.main()
