"""`/v1/models` 里同域同名的优惠模型补标签（issue #286）。

上游把限时免费的 `hy4flash-free` 与付费的 `hy4flash` 的显示名都写成
「Hy4 flash」。只认 `name` 的客户端里这两行长一样：用户以为选了免费那条，
实际发出去的是付费 id，积分照扣。

钉住的约束：
  · 同域（国内版 / 国际版 / 各平台分开算）内真有同名兄弟，且该条目带
    `promo_label` → 显示名补「（标签）」；
  · 名字唯一的优惠模型不碰（`glm-5.2` 的夜间折扣照旧只叫 GLM-5.2）；
  · 跨域同名不碰（`global:` 与国内版是两条不同路由）；
  · **`id` 一个字节不动** —— 客户端发的是 id，改 id 等于改路由；
  · 同名同标签的兄弟只改一条，够区分即可；
  · 认不出的结构原样透传。
"""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.routers import gateway as G  # noqa: E402

# 建单之后的形态：`cn:` 已被 `_bare_cn_model_ids` 剥掉
LISTING = {'object': 'list', 'data': [
    {'id': 'hy4flash-free', 'name': 'Hy4 flash', 'promo_label': '限时免费',
     'promo_factor': 0},
    {'id': 'hy4flash', 'name': 'Hy4 flash', 'credits': 'x0.03'},
    {'id': 'glm-5.2', 'name': 'GLM-5.2', 'promo_label': '夜间折扣'},
    {'id': 'global:hy4flash-free', 'name': 'Hy4 flash', 'promo_label': 'Free now'},
]}


class PromoNameDisambiguationTest(unittest.TestCase):
    def test_free_variant_gets_label_suffix(self) -> None:
        out = G._disambiguate_promo_names(copy.deepcopy(LISTING))
        self.assertEqual(out['data'][0]['name'], 'Hy4 flash（限时免费）')

    def test_paid_sibling_keeps_original_name(self) -> None:
        out = G._disambiguate_promo_names(copy.deepcopy(LISTING))
        self.assertEqual(out['data'][1]['name'], 'Hy4 flash')

    def test_unique_promo_model_untouched(self) -> None:
        out = G._disambiguate_promo_names(copy.deepcopy(LISTING))
        self.assertEqual(out['data'][2]['name'], 'GLM-5.2')

    def test_cross_realm_same_name_untouched(self) -> None:
        out = G._disambiguate_promo_names(copy.deepcopy(LISTING))
        self.assertEqual(out['data'][3]['name'], 'Hy4 flash')

    def test_ids_never_change(self) -> None:
        before = [m['id'] for m in LISTING['data']]
        out = G._disambiguate_promo_names(copy.deepcopy(LISTING))
        self.assertEqual([m['id'] for m in out['data']], before)

    def test_only_one_of_identically_labelled_twins_is_renamed(self) -> None:
        payload = {'data': [
            {'id': 'a-free', 'name': 'Same', 'promo_label': '限时免费'},
            {'id': 'b-free', 'name': 'Same', 'promo_label': '限时免费'},
        ]}
        out = G._disambiguate_promo_names(copy.deepcopy(payload))
        names = [m['name'] for m in out['data']]
        self.assertEqual(names, ['Same（限时免费）', 'Same'])

    def test_platform_model_is_its_own_domain(self) -> None:
        payload = {'data': [
            {'id': 'glm-5.2', 'name': 'GLM-5.2', 'promo_label': '夜间折扣'},
            {'id': 'trae/glm-5.2', 'name': 'GLM-5.2'},
        ]}
        out = G._disambiguate_promo_names(copy.deepcopy(payload))
        self.assertEqual([m['name'] for m in out['data']], ['GLM-5.2', 'GLM-5.2'])

    def test_missing_label_never_renamed(self) -> None:
        payload = {'data': [
            {'id': 'x', 'name': 'Same'},
            {'id': 'y', 'name': 'Same'},
        ]}
        out = G._disambiguate_promo_names(copy.deepcopy(payload))
        self.assertEqual([m['name'] for m in out['data']], ['Same', 'Same'])

    def test_unrecognised_payload_passes_through(self) -> None:
        self.assertEqual(G._disambiguate_promo_names('nope'), 'nope')
        self.assertEqual(G._disambiguate_promo_names({'error': 'x'}), {'error': 'x'})
        odd = {'data': ['raw', 3, None]}
        self.assertEqual(G._disambiguate_promo_names(copy.deepcopy(odd))['data'],
                         odd['data'])

    def test_input_payload_not_mutated(self) -> None:
        src = copy.deepcopy(LISTING)
        G._disambiguate_promo_names(src)
        self.assertEqual(src, LISTING)


if __name__ == '__main__':
    unittest.main()
