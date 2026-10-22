import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch, Mock
sys.path.insert(0, str(Path(__file__).parents[1]))
from navigation_tools import call_navigation, navigation_intent, navigation_reply
from assistant import BailianClient, Config


class NavigationTests(unittest.TestCase):
    def test_explicit_commands_and_no_invented_alias(self):
        for phrase, target in [('导航到A点。', 'A点'), ('请帮我导航到Ｂ点。', 'B点'),
                               ('前往B点', 'B点'), ('导航到一号位', '一号位')]:
            self.assertEqual(navigation_intent(phrase), ('navigate_to_point', {'name': target}))
        for phrase in ['不要导航到A点', '怎么导航到B点', '导航到A点吗', '如果导航到A点', '导航到A点然后去B点']:
            self.assertEqual(navigation_intent(phrase)[0], 'help')
        self.assertIsNone(navigation_intent('给我背一首诗'))

    def test_navigation_never_asks_llm_to_invent_acknowledgement(self):
        bridge = Mock()
        bridge.call.return_value = {'isError': False, 'structuredContent': {'success': True, 'submitted': True, 'point': {'name': 'A点'}}}
        client = BailianClient(Config(api_key=''))
        client.connect_tools = Mock(return_value=bridge)
        client._chat_stream_once = Mock(side_effect=AssertionError('Must not ask model to decide whether to execute navigation'))
        history = [{'role': 'assistant', 'content': '目标已下发'}, {'role': 'user', 'content': '导航到A点。'}]
        reply = ''.join(client.chat_stream(history))
        bridge.call.assert_called_once_with('navigate_to_point', {'name': 'A点'})
        self.assertIn('已下发前往A点', reply)
        client._chat_stream_once.assert_not_called()

    def test_failed_or_unconfirmed_tool_cannot_claim_submitted(self):
        bridge = Mock()
        bridge.call.return_value = {'isError': True, 'structuredContent': {'message': '当前地图没有该点位。可用点位：A点、B点'}}
        reply = navigation_reply('导航到一号位', bridge)
        self.assertIn('未成功', reply)
        self.assertIn('A点、B点', reply)
        self.assertNotIn('已下发', reply)
        bridge.call.return_value = {'isError': False, 'structuredContent': {'success': True}}
        self.assertIn('未确认', navigation_reply('导航到A点', bridge))
        bridge.call.side_effect = RuntimeError('timeout')
        self.assertIn('不要假定', navigation_reply('导航到B点', bridge))

    def test_list_cancel_status_and_no_motion_for_questions(self):
        bridge = Mock()
        bridge.call.return_value = {'isError': False, 'structuredContent': {'success': True, 'submitted': True}}
        self.assertIn('停止', navigation_reply('停止导航', bridge))
        bridge.call.assert_called_once_with('cancel_navigation', {})
        bridge.reset_mock()
        self.assertIn('没有下发', navigation_reply('怎么导航到A点', bridge))
        bridge.call.assert_not_called()
        bridge.call.return_value = {'structuredContent': {'success': True, 'status': 'LOCALIZED', 'distance_remaining': 99}}
        self.assertIn('没有执行导航', navigation_reply('导航状态', bridge))
        self.assertNotIn('99', navigation_reply('导航状态', bridge))

    def test_only_name_sent_no_coordinates(self):
        with patch('navigation_tools.urlopen', return_value=io.BytesIO(b'{"success":true,"submitted":true}')) as request:
            self.assertTrue(call_navigation({}, 'navigate_to_point', {'name': '厨房', 'x': 10})['submitted'])
            req = request.call_args.args[0]
            self.assertEqual(json.loads(req.data), {'name': '厨房'})
            self.assertEqual(req.full_url, 'http://127.0.0.1:3000/api/navigation/go-to')

    def test_suffix_correction_only_for_existing_current_map_point(self):
        for names, active, expected in [(['C点'], 'room', 'C点'), (['C顶', 'C点'], 'room', 'C顶'),
                                        (['C点'], 'other', 'C顶'), ([], 'room', 'C顶')]:
            catalog = {'success': True, 'activeMap': active, 'points': [{'name': n, 'mapPath': 'room'} for n in names]}
            with patch('navigation_tools.urlopen', side_effect=[io.BytesIO(json.dumps(catalog).encode()), io.BytesIO(b'{"success":true,"submitted":true}')]) as request:
                call_navigation({}, 'navigate_to_point', {'name': 'C顶'})
                self.assertEqual(json.loads(request.call_args.args[0].data)['name'], expected)
        for heard in ['沸点', '黑井']:
            with patch('navigation_tools.urlopen', return_value=io.BytesIO(b'{"success":true}')) as request:
                call_navigation({}, 'navigate_to_point', {'name': heard})
                self.assertEqual(json.loads(request.call_args.args[0].data)['name'], heard)
                self.assertEqual(request.call_count, 1)

    def test_asr_digit_normalization_matches_same_number_only(self):
        catalog = {'success': True, 'activeMap': 'room', 'points': [{'name': '一号位', 'mapPath': 'room'}, {'name': 'A点', 'mapPath': 'room'}]}
        for heard, expected in [('1号位', '一号位'), ('一号位', '一号位'), ('2号位', '2号位')]:
            with patch('navigation_tools.urlopen', side_effect=[io.BytesIO(json.dumps(catalog).encode()), io.BytesIO(b'{"success":true}')]) as request:
                call_navigation({}, 'navigate_to_point', {'name': heard})
                self.assertEqual(json.loads(request.call_args.args[0].data)['name'], expected)

    def test_errors_report_real_reason(self):
        error = urllib.error.HTTPError('http://localhost', 400, '', {}, io.BytesIO('{"error":"定位尚未就绪"}'.encode()))
        with patch('navigation_tools.urlopen', side_effect=error), self.assertRaisesRegex(ValueError, '定位尚未就绪'):
            call_navigation({}, 'navigate_to_point', {'name': '厨房'})

    def test_timeout_and_disabled_do_not_claim_success(self):
        with patch('navigation_tools.urlopen', side_effect=TimeoutError), self.assertRaisesRegex(ValueError, '不要假定'):
            call_navigation({}, 'cancel_navigation', {})
        with self.assertRaisesRegex(ValueError, '禁用'):
            call_navigation({'NAVIGATION_MCP_ENABLED': False}, 'navigate_to_point', {'name': '厨房'})
