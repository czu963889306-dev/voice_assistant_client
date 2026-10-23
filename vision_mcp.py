#!/usr/bin/env python3
"""Local MCP stdio server for Bailian image analysis and camera snapshots."""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from assistant import AssistantError, Config, load_yaml
from image_assistant import DEFAULT_PROMPT, VisionClient, extract_json, image_dimensions, normalize_result
from http_transport import urlopen
from navigation_tools import NAVIGATION_TOOLS, call_navigation
from network_tools import NETWORK_TOOLS, get_wifi_ip


PROTOCOL = "2025-11-25"
VERSIONS = {PROTOCOL, "2025-06-18", "2025-03-26", "2024-11-05"}
TOOLS = [
    {"name": "analyze_image", "description": "识别已上传到 images 目录的图片，返回描述、像素矩形框和中心点。不指定 image_path 时选择最新上传图片（排除自带 mcp_test.png）；没有上传图片但已启用摄像头或 snapshot 接口时获取当前画面。明确要求识别测试图时可指定 mcp_test.png。",
     "inputSchema": {"type": "object", "properties": {
         "image_path": {"type": "string", "description": "images 目录内的相对路径，例如 test.png；也可用该目录内的绝对路径"},
         "question": {"type": "string", "description": "希望观察或定位的内容"}},
         "additionalProperties": False}},
    {"name": "inspect_current_view", "description": "从配置的 snapshot HTTP 接口或摄像头获取当前一帧并分析。没有配置画面来源时返回明确错误；file 模式仅分析测试图片，不能当成实时画面。",
     "inputSchema": {"type": "object", "properties": {
         "question": {"type": "string", "description": "希望观察或定位的内容"}}, "additionalProperties": False}},
    {"name": "vision_status", "description": "查询图片目录和当前画面来源的配置状态，不进行拍摄。",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
]

TOOLS += NAVIGATION_TOOLS
TOOLS += NETWORK_TOOLS

class ToolError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


class VisionService:
    def __init__(self, config_path):
        self.config_path = Path(config_path).resolve()
        self.cfg = Config.from_file(self.config_path)
        self.raw = load_yaml(self.config_path)
        folder = Path(str(self.raw.get("VISION_IMAGE_DIR", "images")))
        self.image_dir = (self.config_path.parent / folder).resolve()
        self.source = str(self.raw.get("VISION_SOURCE", "none")).lower()
        self.client = VisionClient(self.cfg, self.cfg.vision_model)

    def image_path(self, value):
        if not isinstance(value, str) or not value.strip():
            raise ToolError("INVALID_ARGUMENT", "image_path 必须是非空字符串")
        path = (self.image_dir / value).resolve()
        if not path.is_relative_to(self.image_dir):
            raise ToolError("PATH_NOT_ALLOWED", "只能识别 VISION_IMAGE_DIR 目录内的图片")
        if not path.is_file():
            raise ToolError("IMAGE_NOT_FOUND", f"找不到图片：{path.name}，请先将图片放入 {self.image_dir}")
        return path

    def latest_image(self):
        candidates = [path for path in self.image_dir.glob("*")
                      if path.is_file() and path.name != "mcp_test.png"
                      and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
                      and path.resolve().is_relative_to(self.image_dir)]
        if not candidates:
            raise ToolError("NO_UPLOADED_IMAGE", "还没有上传图片。请先把图片放入 images 目录；如要体验演示，可说‘识别测试图片’。")
        return max(candidates, key=lambda path: (path.stat().st_mtime_ns, path.name))

    def analyze(self, path, question, source_kind):
        if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}:
            raise ToolError("INVALID_IMAGE", "支持 PNG/JPEG/WebP/BMP/GIF 图片")
        if path.stat().st_size > 10 * 1024 * 1024:
            raise ToolError("IMAGE_TOO_LARGE", "图片不能超过 10 MiB")
        width, height = image_dimensions(path)
        if width <= 0 or height <= 0 or width * height > 40000000:
            raise ToolError("INVALID_IMAGE", "图片尺寸无效或超过 4000 万像素")
        prompt = DEFAULT_PROMPT + "\n用户观察要求（保持上述 JSON 和坐标格式）：" + question
        raw = extract_json(self.client.describe(path, prompt))
        result = normalize_result(raw, width, height)
        result.update(source_kind=source_kind, analyzed_at=datetime.now(timezone.utc).isoformat(),
                      coordinate_note="左上角原点，bbox=[x1,y1,x2,y2] 为像素边界，center=[x,y]；位置由模型估计。")
        return result

    def camera_device(self):
        device = str(self.raw.get("VISION_CAMERA_DEVICE", "/dev/video0"))
        path = Path(device)
        # Resolve stable /dev/v4l/by-id links on every call so USB replugging
        # does not leave us using yesterday's /dev/videoN enumeration.
        resolved = path.resolve()
        if not re.fullmatch(r"/dev/video\d+", resolved.as_posix()) or not path.is_char_device():
            raise ToolError("CAMERA_UNAVAILABLE", "摄像头未连接或设备路径无效，请检查 USB 连接和 VISION_CAMERA_DEVICE")
        return device

    def snapshot_view(self, question):
        url = str(self.raw.get("VISION_SNAPSHOT_URL", "http://127.0.0.1:8080/snapshot"))
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ToolError("INVALID_SNAPSHOT_URL", "VISION_SNAPSHOT_URL 必须是 HTTP 或 HTTPS 图片接口地址")
        timeout = max(1, min(30, int(self.raw.get("VISION_SNAPSHOT_TIMEOUT_SECONDS", 8))))
        request = urllib.request.Request(url, headers={"Accept": "image/*", "Cache-Control": "no-cache"})
        print("正在从 snapshot 接口获取画面…", file=sys.stderr, flush=True)
        try:
            with urlopen(request, timeout=timeout, prefer_ipv4=self.cfg.prefer_ipv4,
                         connect_timeout=self.cfg.http_connect_timeout_seconds) as response:
                content_type = response.headers.get_content_type()
                suffix = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
                          "image/bmp": ".bmp", "image/gif": ".gif"}.get(content_type)
                if suffix is None:
                    raise ToolError("INVALID_SNAPSHOT", "snapshot 接口未返回支持的图片类型，请检查视频服务")
                data = response.read(10 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            raise ToolError("SNAPSHOT_HTTP_ERROR", f"snapshot 接口返回 HTTP {exc.code}，请检查视频服务") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ToolError("SNAPSHOT_UNAVAILABLE", "无法连接 snapshot 接口或取图超时，请检查视频服务和 VISION_SNAPSHOT_URL") from None
        if not data or len(data) > 10 * 1024 * 1024:
            raise ToolError("INVALID_SNAPSHOT", "snapshot 图片为空或超过 10 MiB")
        fetched_at = datetime.now(timezone.utc).isoformat()
        with tempfile.TemporaryDirectory(prefix="vision-snapshot-") as directory:
            path = Path(directory) / ("frame" + suffix)
            path.write_bytes(data)
            print("snapshot 取图完成，正在识别画面…", file=sys.stderr, flush=True)
            result = self.analyze(path, question, "snapshot")
            # This is fetch time; an HTTP endpoint cannot prove camera exposure time.
            result["fetched_at"] = fetched_at
            return result

    def current_view(self, question):
        if self.source == "snapshot":
            return self.snapshot_view(question)
        if self.source == "none":
            raise ToolError("CAMERA_NOT_CONFIGURED", "当前尚未配置摄像头，无法查看实时画面。可以上传图片后调用 analyze_image。")
        if self.source == "file":
            result = self.analyze(self.image_path(str(self.raw.get("VISION_FRAME_FILE", ""))), question, "test_file")
            result["notice"] = "这是配置的测试图片，不是摄像头实时画面。"
            return result
        if self.source != "camera":
            raise ToolError("INVALID_SOURCE", "VISION_SOURCE 只支持 none、file、camera、snapshot")
        device = self.camera_device()
        if not shutil.which("ffmpeg"):
            raise ToolError("CAPTURE_DEPENDENCY_MISSING", "摄像头采集需要安装 ffmpeg")
        command = ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "v4l2"]
        for key, flag in [("VISION_CAMERA_FORMAT", "-input_format"),
                          ("VISION_CAMERA_SIZE", "-video_size"), ("VISION_CAMERA_FPS", "-framerate")]:
            value = str(self.raw.get(key, "")).strip()
            if value:
                command.extend([flag, value])
        with tempfile.TemporaryDirectory(prefix="vision-frame-") as directory:
            path = Path(directory) / "frame.jpg"
            try:
                print("摄像头采集开始…", file=sys.stderr, flush=True)
                completed = subprocess.run(command + ["-i", device, "-ss", "0.5", "-frames:v", "1", "-q:v", "2", "-y", str(path)],
                                           capture_output=True, timeout=12)
            except subprocess.TimeoutExpired:
                raise ToolError("CAPTURE_TIMEOUT", "摄像头采集超时，请检查连接及占用情况") from None
            if completed.returncode or not path.is_file():
                print("摄像头采集错误：" + completed.stderr.decode("utf-8", "replace")[:500], file=sys.stderr, flush=True)
                raise ToolError("CAPTURE_FAILED", "无法获取摄像头画面，请检查连接、设备格式及占用情况")
            captured_at = datetime.now(timezone.utc).isoformat()
            print("摄像头采集完成，正在识别画面…", file=sys.stderr, flush=True)
            result = self.analyze(path, question, "camera")
            result["captured_at"] = captured_at
            return result

    def call(self, name, arguments):
        try:
            schema = next((tool["inputSchema"] for tool in TOOLS if tool["name"] == name), None)
            if schema is None:
                raise ToolError("UNKNOWN_TOOL", "未知工具")
            if not isinstance(arguments, dict) or set(arguments) - set(schema["properties"]):
                raise ToolError("INVALID_ARGUMENT", "工具参数不符合 inputSchema")
            if any(key not in arguments for key in schema.get("required", [])):
                raise ToolError("INVALID_ARGUMENT", "缺少必要参数")
            if any(not isinstance(value, str) for value in arguments.values()):
                raise ToolError("INVALID_ARGUMENT", "参数值必须是字符串")
            question = arguments.get("question", "描述画面里的主要物体和位置")
            if len(question) > 2000:
                raise ToolError("INVALID_ARGUMENT", "question 不能超过 2000 个字符")
            if name in {t["name"] for t in NAVIGATION_TOOLS}:
                result = call_navigation(self.raw, name, arguments)
            elif name == "get_wifi_ip":
                result = get_wifi_ip()
            elif name == "vision_status":
                device = str(self.raw.get("VISION_CAMERA_DEVICE", "/dev/video0"))
                try:
                    self.camera_device()
                    device_present = True
                except ToolError:
                    device_present = False
                result = {"source": self.source, "image_directory": str(self.image_dir),
                          "camera_configured": self.source == "camera", "model": self.cfg.vision_model,
                          "camera_device": device, "camera_device_present": device_present,
                          "capture_dependency_available": bool(shutil.which("ffmpeg")),
                          "snapshot_configured": self.source == "snapshot",
                          "current_view_configured": self.source in {"camera", "snapshot", "file"}}
            elif name == "analyze_image":
                try:
                    path = self.image_path(arguments["image_path"]) if "image_path" in arguments else self.latest_image()
                except ToolError as exc:
                    if exc.code != "NO_UPLOADED_IMAGE" or self.source not in {"camera", "snapshot"}:
                        raise
                    path = None
                if path is None:
                    result = self.current_view(question)
                else:
                    is_demo = path.name == "mcp_test.png"
                    result = self.analyze(path, question, "test_file" if is_demo else "uploaded_image")
                    result["image_name"] = path.name
                    if is_demo:
                        result["notice"] = "这是自带测试图，不是摄像头画面或用户上传的照片。"
            else:
                result = self.current_view(question)
            return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                    "structuredContent": result, "isError": False}
        except (ToolError, AssistantError, OSError, ValueError) as exc:
            result = {"error": getattr(exc, "code", "VISION_ERROR"), "message": str(exc)}
            return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                    "structuredContent": result, "isError": True}


class MCPServer:
    def __init__(self, service):
        self.service = service
        self.initialized = False

    @staticmethod
    def error(id_, code, message):
        return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}

    def handle(self, request):
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
            return self.error(None, -32600, "Invalid Request")
        method = request["method"]
        if "id" not in request:
            if method == "notifications/initialized":
                self.initialized = True
            return None
        id_ = request["id"]
        params = request.get("params", {})
        if not isinstance(params, dict):
            return self.error(id_, -32602, "Invalid params")
        if method == "initialize":
            version = params.get("protocolVersion")
            result = {"protocolVersion": version if version in VERSIONS else PROTOCOL,
                      "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "rk3566-vision", "version": "1.0.0"}}
        elif method == "ping":
            result = {}
        elif not self.initialized:
            return self.error(id_, -32002, "Server not initialized")
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            result = self.service.call(params.get("name"), params.get("arguments", {}))
        else:
            return self.error(id_, -32601, "Method not found")
        return {"jsonrpc": "2.0", "id": id_, "result": result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(Path(__file__).with_name("config.yaml")))
    args = parser.parse_args()
    # stdout is reserved exclusively for newline-delimited JSON-RPC.
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    server = MCPServer(VisionService(args.config))
    for line in sys.stdin:
        try:
            response = server.handle(json.loads(line))
        except json.JSONDecodeError:
            response = server.error(None, -32700, "Parse error")
        except Exception:
            response = server.error(None, -32603, "Internal error")
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
