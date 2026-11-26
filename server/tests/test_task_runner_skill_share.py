"""skill_share（分享技能给好友）任务链。

背景：
  · 上游成长中心会下发 `skill_share`，但旧版动作表里没有这个任务码，脚本只报
    「非映射任务，skip」，50 积分永远拿不到（issue #429 同批任务之一）。
  · 计数只认桌面事件 `skill_share_channel_action`（action=link / panelAction=success
    / panelType=clipboard / isSuccessful=true），技能身份用真实市场技能；
    `skill_share_entry_click` 是打开分享面板的前置事件。
  · 单独走开放平台 `asset-shares/c2c/channels` 建真链接并 resolve，进度不动
    （社区 7 账号实测），所以这里不建渠道、只上报上述双事件。

直接加载真实的 `upstream/scripts/task_runner.py`，钉住：
  1. 任务码登记为 skillshare（不再落到「非映射任务」）；
  2. 事件形状与技能身份来源；
  3. light_up 走专用分支、上报后回读达标才领奖；
  4. 市场列表不可用时回落实测兜底技能。
"""
from __future__ import annotations

import collections
import importlib.util
import pathlib
import sys
import unittest
from unittest import mock

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCRIPTS = _ROOT / 'upstream' / 'scripts'


def _load_task_runner():
    """加载 upstream/scripts/task_runner.py（模块名唯一，避免与其他测试串味）。"""
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        'task_runner_skill_share_probe', _SCRIPTS / 'task_runner.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class SkillShareTaskTest(unittest.TestCase):
    CODE = 'skill_share'

    @classmethod
    def setUpClass(cls) -> None:
        cls.tr = _load_task_runner()
        cls.auth = {'uid': 'u1', 'token': 'at', 'nick': 'n'}

    def test_registered_as_skillshare(self) -> None:
        spec = self.tr.MAPPING[self.CODE]
        self.assertEqual(spec['kind'], 'skillshare')
        self.assertEqual(spec['target'], 1)

    def test_event_shape(self) -> None:
        events = self.tr.desktop_skill_share_events(
            self.auth, ('skill_2097350077599879168', '润泽小馆·日报撰写'))
        self.assertEqual([e['eventCode'] for e in events],
                         ['skill_share_entry_click', 'skill_share_channel_action'])
        action = events[1]
        self.assertEqual(action['action'], 'link')
        self.assertEqual(action['panelAction'], 'success')
        self.assertEqual(action['panelType'], 'clipboard')
        self.assertTrue(action['isSuccessful'])
        self.assertEqual(action['skillId'], 'skill_2097350077599879168')
        self.assertEqual(action['skillName'], '润泽小馆·日报撰写')
        self.assertEqual(action['skillType'], 'market')
        for key in ('id', 'cost', 'actionAt'):
            self.assertIn(key, action)
        self.assertTrue(events[0]['actionSuccessful'])

    def test_identity_uses_market_skill(self) -> None:
        with mock.patch.object(self.tr, '_fetch_skills',
                               return_value=[('skill_real', '真实技能')]):
            self.assertEqual(self.tr._skill_share_identity(self.auth),
                             ('skill_real', '真实技能'))

    def test_identity_falls_back_when_market_empty(self) -> None:
        with mock.patch.object(self.tr, '_fetch_skills', return_value=[]):
            self.assertEqual(self.tr._skill_share_identity(self.auth),
                             self.tr.SKILL_SHARE_FALLBACK)

    def test_identity_uses_share_fallback_when_market_unavailable(self) -> None:
        """市场接口炸了也要用 skill_share 自己实测可点亮的兜底技能。"""
        with mock.patch.object(self.tr.tc, 'do_post', side_effect=RuntimeError('boom')):
            self.assertEqual(self.tr._skill_share_identity(self.auth),
                             self.tr.SKILL_SHARE_FALLBACK)

    def test_ids_for_returns_single_skill_identity(self) -> None:
        with mock.patch.object(self.tr, '_skill_share_identity',
                               return_value=('skill_real', '真实技能')):
            self.assertEqual(self.tr.ids_for('skillshare', self.auth, 1),
                             [('skill_real', '真实技能')])

    def test_light_up_reports_two_events_and_claims(self) -> None:
        opts = mock.Mock(gap=0)
        stats = collections.Counter()
        reported: list[list] = []
        claimed: list[str] = []
        known = {'accept_status': 'accepted', 'progress': {'current': 0, 'target': 1}}

        def _report(_auth, events):
            reported.append(events)
            return 200, 0

        with mock.patch.object(self.tr, '_skill_share_identity',
                               return_value=('skill_real', '真实技能')), \
                mock.patch.object(self.tr, 'report_desktop_events', side_effect=_report), \
                mock.patch.object(self.tr, 'claim_one',
                                  side_effect=lambda a, c, u, s, g: claimed.append(c)), \
                mock.patch.object(self.tr.tc, 'task_status',
                                  return_value={'accept_status': 'completed',
                                                'progress': {'current': 1, 'target': 1}}), \
                mock.patch.object(self.tr.time, 'sleep'):
            self.tr.light_up(self.auth, self.CODE, self.tr.MAPPING[self.CODE],
                             0, 1, 'u1', opts, stats, known=known)

        self.assertEqual(len(reported), 1)
        self.assertEqual([e['eventCode'] for e in reported[0]],
                         ['skill_share_entry_click', 'skill_share_channel_action'])
        self.assertEqual(claimed, [self.CODE])

    def test_light_up_retries_on_web_domain_when_chat_rejected(self) -> None:
        """chat 域拒绝时用同一份事件换 web 域重试一次（社区实测路径）。"""
        opts = mock.Mock(gap=0)
        stats = collections.Counter()
        bases: list = []
        known = {'accept_status': 'accepted', 'progress': {'current': 0, 'target': 1}}

        def _report(_auth, events, base=None):
            bases.append(base)
            return (500, None) if len(bases) == 1 else (200, 0)

        with mock.patch.object(self.tr, '_skill_share_identity',
                               return_value=('skill_real', '真实技能')), \
                mock.patch.object(self.tr, 'report_desktop_events', side_effect=_report), \
                mock.patch.object(self.tr, 'claim_one'), \
                mock.patch.object(self.tr.tc, 'task_status',
                                  return_value={'accept_status': 'accepted',
                                                'progress': {'current': 0, 'target': 1}}), \
                mock.patch.object(self.tr.time, 'sleep'):
            self.tr.light_up(self.auth, self.CODE, self.tr.MAPPING[self.CODE],
                             0, 1, 'u1', opts, stats, known=known)

        self.assertEqual(bases, [None, self.tr.WEB_BASE])

    def test_light_up_does_not_claim_when_progress_stuck(self) -> None:
        opts = mock.Mock(gap=0)
        stats = collections.Counter()
        claimed: list[str] = []
        known = {'accept_status': 'accepted', 'progress': {'current': 0, 'target': 1}}

        with mock.patch.object(self.tr, '_skill_share_identity',
                               return_value=('skill_real', '真实技能')), \
                mock.patch.object(self.tr, 'report_desktop_events',
                                  return_value=(200, 0)), \
                mock.patch.object(self.tr, 'claim_one',
                                  side_effect=lambda a, c, u, s, g: claimed.append(c)), \
                mock.patch.object(self.tr.tc, 'task_status',
                                  return_value={'accept_status': 'accepted',
                                                'progress': {'current': 0, 'target': 1}}), \
                mock.patch.object(self.tr.time, 'sleep'):
            self.tr.light_up(self.auth, self.CODE, self.tr.MAPPING[self.CODE],
                             0, 1, 'u1', opts, stats, known=known)

        self.assertEqual(claimed, [], '进度没动却去领奖（会被服务端 400 拒绝）')


if __name__ == '__main__':
    unittest.main()
