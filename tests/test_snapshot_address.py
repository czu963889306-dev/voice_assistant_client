"""Snapshot hosts follow DHCP changes without caching an old Wi-Fi address."""
import base64
import io
import json
import sys
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parents[1]))
from network_tools import NetworkToolError
from vision_mcp import VisionService

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jVLsAAAAASUVORK5CYII=')


def frame():
    response = io.BytesIO(PNG)
    response.headers = Message()
    response.headers['Content-Type'] = 'image/png'
    return response


class SnapshotAddressTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        config = Path(self.directory.name) / 'config.yaml'
        config.write_text('DASHSCOPE_API_KEY: test\nVISION_SOURCE: snapshot\n'
                          'VISION_SNAPSHOT_URL: "http://{wifi_ip}:8080/snapshot"\n', encoding='utf-8')
        self.service = VisionService(config)
        self.service.client.describe = Mock(return_value=json.dumps({'description': '测试图片', 'objects': []}))

    def test_each_capture_uses_current_ip_with_same_service_instance(self):
        with patch('vision_mcp.get_wifi_ip', side_effect=[{'ipv4': '192.168.1.10'}, {'ipv4': '192.168.1.11'}]), \
                patch('vision_mcp.urlopen', side_effect=[frame(), frame()]) as fetch:
            first = self.service.call('inspect_current_view', {})
            second = self.service.call('inspect_current_view', {})
        self.assertFalse(first['isError'])
        self.assertFalse(second['isError'])
        self.assertEqual([call.args[0].full_url for call in fetch.call_args_list],
                         ['http://192.168.1.10:8080/snapshot', 'http://192.168.1.11:8080/snapshot'])
        self.assertEqual(first['structuredContent']['snapshot_host'], '192.168.1.10')
        self.assertEqual(second['structuredContent']['snapshot_host'], '192.168.1.11')
        self.assertEqual(self.service.raw['VISION_SNAPSHOT_URL'], 'http://{wifi_ip}:8080/snapshot')

    def test_wifi_loss_does_not_reuse_previous_address(self):
        with patch('vision_mcp.get_wifi_ip', side_effect=[{'ipv4': '192.168.1.10'},
                   NetworkToolError('WIFI_NO_IPV4', 'Wi-Fi offline')]), \
                patch('vision_mcp.urlopen', return_value=frame()) as fetch:
            self.assertFalse(self.service.call('inspect_current_view', {})['isError'])
            failed = self.service.call('inspect_current_view', {})
        self.assertTrue(failed['isError'])
        self.assertEqual(failed['structuredContent']['error'], 'SNAPSHOT_ADDRESS_UNAVAILABLE')
        fetch.assert_called_once()
        self.service.client.describe.assert_called_once()

    def test_status_reports_current_host_without_capturing_and_handles_offline(self):
        with patch('vision_mcp.get_wifi_ip', side_effect=[{'ipv4': '192.168.1.10'}, {'ipv4': '192.168.1.11'},
                   NetworkToolError('WIFI_NO_IPV4', 'Wi-Fi offline')]), patch('vision_mcp.urlopen') as fetch:
            first = self.service.call('vision_status', {})['structuredContent']
            second = self.service.call('vision_status', {})['structuredContent']
            offline = self.service.call('vision_status', {})
        self.assertTrue(first['snapshot_dynamic_host'])
        self.assertEqual(first['snapshot_host'], '192.168.1.10')
        self.assertEqual(second['snapshot_host'], '192.168.1.11')
        self.assertFalse(offline['isError'])
        self.assertIsNone(offline['structuredContent']['snapshot_host'])
        self.assertEqual(offline['structuredContent']['snapshot_address_error'], 'SNAPSHOT_ADDRESS_UNAVAILABLE')
        fetch.assert_not_called()
        self.service.client.describe.assert_not_called()

    def test_fixed_loopback_remote_and_ipv6_addresses_remain_supported(self):
        urls = ['http://127.0.0.1:8080/snapshot', 'https://camera.example:8090/frame?fresh=1',
                'http://[::1]:8080/snapshot']
        with patch('vision_mcp.get_wifi_ip') as wifi:
            for url in urls:
                with self.subTest(url=url):
                    self.service.raw['VISION_SNAPSHOT_URL'] = url
                    self.assertEqual(self.service.snapshot_url(), url)
        wifi.assert_not_called()

    def test_invalid_urls_ports_and_nonhost_placeholders_do_not_fetch(self):
        urls = ['file:///snapshot', 'http:///snapshot', 'http://camera:invalid/snapshot',
                'http://camera:0/snapshot', 'http://{other_ip}:8080/snapshot',
                'http://camera:8080/{wifi_ip}', 'http://{wifi_ip}:8080/{wifi_ip}']
        with patch('vision_mcp.get_wifi_ip') as wifi, patch('vision_mcp.urlopen') as fetch:
            for url in urls:
                with self.subTest(url=url):
                    self.service.raw['VISION_SNAPSHOT_URL'] = url
                    failed = self.service.call('inspect_current_view', {})
                    self.assertEqual(failed['structuredContent']['error'], 'INVALID_SNAPSHOT_URL')
        wifi.assert_not_called()
        fetch.assert_not_called()
        self.service.client.describe.assert_not_called()

    def test_invalid_dynamic_address_does_not_become_a_request_url(self):
        for data in [{'ipv4': '192.168.1.10/other'}, {'ipv4': 'not-an-ip'}, {}]:
            with self.subTest(data=data), patch('vision_mcp.get_wifi_ip', return_value=data), \
                    patch('vision_mcp.urlopen') as fetch:
                failed = self.service.call('inspect_current_view', {})
                self.assertEqual(failed['structuredContent']['error'], 'SNAPSHOT_ADDRESS_UNAVAILABLE')
                fetch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
