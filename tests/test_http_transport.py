import socket
import ssl
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parents[1]))
from http_transport import connect_socket, HTTPSConnection


V4 = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 443))
V6 = (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::1", 443, 0, 0))


class TransportTests(unittest.TestCase):
    def test_ipv4_precedes_unreachable_ipv6(self):
        sock = Mock()
        with patch("http_transport.socket.getaddrinfo", return_value=[V6, V4]), \
                patch("http_transport.socket.socket", return_value=sock) as create:
            result = connect_socket(("example.com", 443), timeout=90)
        self.assertIs(result, sock)
        create.assert_called_once_with(socket.AF_INET, socket.SOCK_STREAM, 6)
        sock.connect.assert_called_once_with(V4[4])
        self.assertLessEqual(sock.settimeout.call_args_list[0].args[0], 2)

    def test_failed_address_is_closed_and_falls_back(self):
        failed, good = Mock(), Mock()
        failed.connect.side_effect = TimeoutError("unreachable")
        with patch("http_transport.socket.getaddrinfo", return_value=[V4, V6]), \
                patch("http_transport.socket.socket", side_effect=[failed, good]):
            result = connect_socket(("example.com", 443))
        failed.close.assert_called_once()
        self.assertIs(result, good)
        good.connect.assert_called_once_with(V6[4])

    def test_connection_deadline_is_shared_across_addresses(self):
        failed = Mock()
        failed.connect.side_effect = TimeoutError("unreachable")
        with patch("http_transport.socket.getaddrinfo", return_value=[V4, V6]), \
                patch("http_transport.socket.socket", return_value=failed) as create, \
                patch("http_transport.time.monotonic", side_effect=[0, 0, 6]):
            with self.assertRaises(TimeoutError):
                connect_socket(("example.com", 443), connect_timeout=5)
        self.assertEqual(create.call_count, 1)
        failed.close.assert_called_once()

    def test_preference_can_be_disabled(self):
        sock = Mock()
        with patch("http_transport.socket.getaddrinfo", return_value=[V6, V4]), \
                patch("http_transport.socket.socket", return_value=sock):
            connect_socket(("example.com", 443), prefer_ipv4=False)
        sock.connect.assert_called_once_with(V6[4])

    def test_https_still_validates_certificate_and_hostname(self):
        connection = HTTPSConnection("example.com", timeout=30)
        self.assertEqual(connection._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(connection._context.check_hostname)
        connection.close()


if __name__ == "__main__":
    unittest.main()
