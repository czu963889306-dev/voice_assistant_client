"""Bounded, local stdio MCP client; no network listener or API key in argv."""
from __future__ import annotations

import atexit
import json
import queue
import subprocess
import sys
import threading
from pathlib import Path


class VisionMCPClient:
    def __init__(self, config_path, timeout=120):
        self.timeout = timeout
        self.sequence = 0
        self.lock = threading.Lock()
        self.process = subprocess.Popen([sys.executable, "-u", str(Path(__file__).with_name("vision_mcp.py")),
                                         "--config", str(config_path)], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1)
        self.responses = queue.Queue()
        def read():
            try:
                for line in self.process.stdout:
                    self.responses.put(json.loads(line))
            except (OSError, ValueError):
                pass
            finally:
                self.responses.put(None)
        threading.Thread(target=read, daemon=True).start()
        atexit.register(self.close)
        try:
            self.request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                       "clientInfo": {"name": "rk3566-voice", "version": "1.0.0"}})
            self.notify("notifications/initialized", {})
            self.catalog = self.request("tools/list", {})["tools"]
        except Exception:
            self.close()
            raise

    def _send(self, message):
        try:
            self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
        except (OSError, ValueError):
            raise RuntimeError("图片 MCP 服务已断开") from None

    def notify(self, method, params):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method, params):
        with self.lock:
            self.sequence += 1
            self._send({"jsonrpc": "2.0", "id": self.sequence, "method": method, "params": params})
            try:
                reply = self.responses.get(timeout=self.timeout)
            except queue.Empty:
                self.close()
                raise RuntimeError("图片 MCP 服务响应超时") from None
            if reply is None:
                raise RuntimeError("图片 MCP 服务已退出")
            if reply.get("id") != self.sequence or "error" in reply:
                raise RuntimeError("图片 MCP 协议响应错误")
            return reply["result"]

    def tools(self):
        return [{"type": "function", "function": {"name": tool["name"], "description": tool["description"],
                  "parameters": tool["inputSchema"]}} for tool in self.catalog]

    def call(self, name, arguments):
        return self.request("tools/call", {"name": name, "arguments": arguments})

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        for stream in (self.process.stdin, self.process.stdout):
            if stream and not stream.closed:
                stream.close()
        atexit.unregister(self.close)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
