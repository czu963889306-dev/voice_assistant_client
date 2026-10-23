import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parents[1]))
from assistant import BailianClient, Config, command_reply
from network_tools import NetworkToolError, get_wifi_ip, network_reply, wifi_ip_query
from vision_mcp import VisionService


def interface(name, addresses, up=True):
    return {'ifname': name, 'flags': ['UP', 'LOWER_UP'] if up else [],
            'operstate': 'UP' if up else 'DOWN',
            'addr_info': [{'family': 'inet', 'scope': 'global', 'local': value} for value in addresses]}


def output(data):
    return Mock(returncode=0, stdout=json.dumps(data), stderr='')


class WifiAddressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'eth0').mkdir()
        (self.root / 'tun0').mkdir()
        (self.root / 'lo').mkdir()

    def wireless(self, name='wlan0', marker='wireless'):
        (self.root / name / marker).mkdir(parents=True)

    def test_returns_wifi_instead_of_wired_loopback_or_vpn(self):
        self.wireless()
        rows = [interface('eth0', ['192.168.2.2']), interface('lo', ['127.0.0.1']),
                interface('tun0', ['10.0.0.2']), interface('wlan0', ['192.168.31.250'])]
        with patch('network_tools.shutil.which', return_value='/sbin/ip'), \
                patch('network_tools.subprocess.run', return_value=output(rows)) as run:
            data = get_wifi_ip(self.root)
        self.assertEqual(data['ipv4'], '192.168.31.250')
        self.assertEqual(data['interface'], 'wlan0')
        self.assertEqual(data['ipv4_addresses'], ['192.168.31.250'])
        self.assertEqual(data['speech_text'], '当前 Wi-Fi 的 IP 地址是，一九二，点，一六八，点，三一，点，二五零。')
        self.assertNotIn('192.168.2.2', json.dumps(data))
        self.assertEqual(run.call_args.args[0], ['/sbin/ip', '-j', '-4', 'address', 'show'])
        self.assertEqual(run.call_args.kwargs['timeout'], 3)
        self.assertNotIn('shell', run.call_args.kwargs)

    def test_multiple_wifi_prefers_wifi_default_route_and_lowest_metric(self):
        self.wireless()
        self.wireless('wlp3s0', marker='phy80211')
        rows = [interface('wlan0', ['192.168.1.10']), interface('wlp3s0', ['192.168.2.10'])]
        routes = [{'dev': 'eth0', 'metric': 0}, {'dev': 'wlan0', 'metric': 600},
                  {'dev': 'wlp3s0', 'metric': 100}]
        with patch('network_tools.shutil.which', return_value='ip'), \
                patch('network_tools.subprocess.run', side_effect=[output(rows), output(routes)]):
            data = get_wifi_ip(self.root)
        self.assertEqual(data['interface'], 'wlp3s0')
        self.assertEqual(len(data['interfaces']), 2)

    def test_route_query_failure_does_not_hide_valid_local_wifi_address(self):
        self.wireless()
        self.wireless('wlan1')
        rows = [interface('wlan1', ['192.168.2.10']), interface('wlan0', ['192.168.1.10'])]
        with patch('network_tools.shutil.which', return_value='ip'), \
                patch('network_tools.subprocess.run', side_effect=[output(rows), output(None)]):
            self.assertEqual(get_wifi_ip(self.root)['interface'], 'wlan0')

    def test_address_is_refreshed_on_every_call(self):
        self.wireless()
        rows = [output([interface('wlan0', [value])]) for value in ['192.168.1.10', '192.168.1.11']]
        with patch('network_tools.shutil.which', return_value='ip'), \
                patch('network_tools.subprocess.run', side_effect=rows):
            self.assertEqual(get_wifi_ip(self.root)['ipv4'], '192.168.1.10')
            self.assertEqual(get_wifi_ip(self.root)['ipv4'], '192.168.1.11')

    def test_invalid_link_local_and_duplicate_addresses_are_excluded(self):
        self.wireless()
        row = interface('wlan0', ['invalid', '127.0.0.1', '169.254.1.1', '0.0.0.0',
                                  '224.0.0.1', '192.168.1.10', '192.168.1.10'])
        row['addr_info'].append({'family': 'inet6', 'scope': 'global', 'local': '2001:db8::1'})
        with patch('network_tools.shutil.which', return_value='ip'), \
                patch('network_tools.subprocess.run', return_value=output([row])):
            self.assertEqual(get_wifi_ip(self.root)['ipv4_addresses'], ['192.168.1.10'])

    def test_missing_wifi_never_falls_back_to_other_interfaces(self):
        with patch('network_tools.subprocess.run') as run, self.assertRaises(NetworkToolError) as error:
            get_wifi_ip(self.root)
        self.assertEqual(error.exception.code, 'WIFI_NOT_FOUND')
        run.assert_not_called()

    def test_disconnected_or_no_ipv4_is_an_explicit_error(self):
        self.wireless()
        for row in [interface('wlan0', ['192.168.1.10'], up=False), interface('wlan0', [])]:
            with self.subTest(row=row), patch('network_tools.shutil.which', return_value='ip'), \
                    patch('network_tools.subprocess.run', return_value=output([row])), \
                    self.assertRaises(NetworkToolError) as error:
                get_wifi_ip(self.root)
            self.assertEqual(error.exception.code, 'WIFI_NO_IPV4')

    def test_missing_ip_command_and_timeouts_report_read_failures(self):
        self.wireless()
        with patch('network_tools.shutil.which', return_value=None), self.assertRaises(NetworkToolError) as error:
            get_wifi_ip(self.root)
        self.assertEqual(error.exception.code, 'IPROUTE2_NOT_FOUND')
        with patch('network_tools.shutil.which', return_value='ip'), \
                patch('network_tools.subprocess.run', side_effect=subprocess.TimeoutExpired('ip', 3)), \
                self.assertRaises(NetworkToolError) as error:
            get_wifi_ip(self.root)
        self.assertEqual(error.exception.code, 'NETWORK_QUERY_FAILED')


class WifiConversationTests(unittest.TestCase):
    def test_common_queries_and_unrelated_questions(self):
        for text in ['当前 Wi-Fi 的 IP 是多少？', '播报一下无线网络IP地址', '你的IP地址是什么',
                     '本机IP是多少', 'IP地址是多少', '你好小云，当前wifi的ip是多少']:
            self.assertTrue(wifi_ip_query(text), text)
        for text in ['什么是IP地址', '怎么修改Wi-Fi的IP', '网关IP是多少', '你的公网IP是什么',
                     '导航到A点', 'IP路由原理', '你的Wi-Fi密码是多少', '当前天气怎么样']:
            self.assertFalse(wifi_ip_query(text), text)
        self.assertIsNone(command_reply('你好小云，当前wifi的ip是多少'))

    def test_ip_answer_uses_mcp_instead_of_cloud_chat(self):
        bridge = Mock()
        bridge.call.return_value = {'isError': False, 'structuredContent': {
            'success': True, 'ipv4': '192.168.1.10', 'speech_text': '当前 Wi-Fi 的 IP 是，一九二点一六八点一点一零。'}}
        client = BailianClient(Config(api_key='test-key'))
        client.connect_tools = Mock(return_value=bridge)
        client._chat_stream_once = Mock(side_effect=AssertionError('IP lookup must not call cloud chat'))
        reply = ''.join(client.chat_stream([{'role': 'user', 'content': '当前WiFi的IP是多少'}]))
        self.assertIn('一九二点一六八', reply)
        bridge.call.assert_called_once_with('get_wifi_ip', {})
        client._chat_stream_once.assert_not_called()

    def test_tool_failure_or_missing_tool_does_not_invent_an_address(self):
        self.assertIn('未启用', network_reply('本机IP是多少', None))
        bridge = Mock()
        bridge.call.return_value = {'isError': True, 'structuredContent': {'message': '没有发现 Wi-Fi 网卡'}}
        self.assertIn('没有发现 Wi-Fi 网卡', network_reply('本机IP是多少', bridge))
        bridge.call.return_value = {'isError': False, 'structuredContent': {
            'success': True, 'ipv4': 'not-an-ip', 'speech_text': 'invalid'}}
        self.assertIn('无法读取', network_reply('本机IP是多少', bridge))

    def test_mcp_registration_dispatch_and_argument_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.yaml'
            config.write_text('DASHSCOPE_API_KEY: ""\n', encoding='utf-8')
            service = VisionService(config)
            with patch('vision_mcp.get_wifi_ip', return_value={'success': True, 'ipv4': '192.168.1.10'}) as tool:
                self.assertFalse(service.call('get_wifi_ip', {})['isError'])
                tool.assert_called_once_with()
                result = service.call('get_wifi_ip', {'interface': 'eth0'})
                self.assertEqual(result['structuredContent']['error'], 'INVALID_ARGUMENT')
                tool.assert_called_once()
            with patch('vision_mcp.get_wifi_ip', side_effect=NetworkToolError('WIFI_NO_IPV4', '未获得地址')):
                result = service.call('get_wifi_ip', {})
                self.assertTrue(result['isError'])
                self.assertEqual(result['structuredContent']['error'], 'WIFI_NO_IPV4')


if __name__ == '__main__':
    unittest.main()
