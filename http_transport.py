"""Application-local HTTP transport with bounded address fallback.

Keep TLS certificate verification and urllib proxy handling. Prefer IPv4 on
networks where DNS advertises an intermittently unreachable IPv6 endpoint.
Never change the operating system's IPv6 or resolver configuration.
"""
from __future__ import annotations

import http.client
import socket
import time
import urllib.request


def connect_socket(address, timeout=30, source_address=None, *, prefer_ipv4=True, connect_timeout=5):
    host, port = address
    addresses = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    if prefer_ipv4:
        addresses.sort(key=lambda item: item[0] != socket.AF_INET)
    # DNS uses the system resolver; this deadline bounds the subsequent TCP
    # address attempts. Each failed address gets at most two seconds.
    deadline = time.monotonic() + connect_timeout
    last_error = OSError("域名未返回可连接的地址")
    for family, kind, protocol, _, sockaddr in addresses:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("连接服务超时") from last_error
        sock = socket.socket(family, kind, protocol)
        try:
            sock.settimeout(min(2.0, remaining, timeout))
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            # HTTPS performs its TLS handshake before restoring read timeout.
            sock.settimeout(min(connect_timeout, timeout))
            return sock
        except OSError as exc:
            last_error = exc
            sock.close()
    raise last_error


class _ConnectionOptions:
    def __init__(self, *args, prefer_ipv4=True, connect_timeout=5, **kwargs):
        super().__init__(*args, **kwargs)
        self._prefer_ipv4 = prefer_ipv4
        self._connect_timeout = connect_timeout
        self._create_connection = self._bounded_connect

    def _bounded_connect(self, address, timeout, source_address=None):
        return connect_socket(address, timeout, source_address, prefer_ipv4=self._prefer_ipv4,
                              connect_timeout=self._connect_timeout)

    def connect(self):
        super().connect()
        self.sock.settimeout(self.timeout)


class HTTPConnection(_ConnectionOptions, http.client.HTTPConnection):
    pass


class HTTPSConnection(_ConnectionOptions, http.client.HTTPSConnection):
    pass


class HTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, prefer_ipv4, connect_timeout):
        super().__init__()
        self.options = {"prefer_ipv4": prefer_ipv4, "connect_timeout": connect_timeout}

    def http_open(self, request):
        def factory(*args, **kwargs):
            return HTTPConnection(*args, **self.options, **kwargs)
        return self.do_open(factory, request)


class HTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, prefer_ipv4, connect_timeout):
        super().__init__()
        self.options = {"prefer_ipv4": prefer_ipv4, "connect_timeout": connect_timeout}

    def https_open(self, request):
        def factory(*args, **kwargs):
            return HTTPSConnection(*args, **self.options, **kwargs)
        return self.do_open(factory, request, context=self._context)


def urlopen(request, *, timeout=30, prefer_ipv4=True, connect_timeout=5):
    opener = urllib.request.build_opener(HTTPHandler(prefer_ipv4, connect_timeout),
                                        HTTPSHandler(prefer_ipv4, connect_timeout))
    return opener.open(request, timeout=timeout)
