import json
import io
from email.message import Message
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parents[1]))
from assistant import BailianClient, Config, ask_once, ignored_speech, record_audio
from mcp_client import VisionMCPClient
from vision_mcp import MCPServer, VisionService, ToolError


def sample_png(path):
    # A valid, deterministic fixture: red rectangle on a white 320x240 image.
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    rows = b"".join(b"\0" + b"".join(b"\xff\0\0" if 80 <= x < 240 and 60 <= y < 180 else b"\xff\xff\xff"
                                   for x in range(320)) for y in range(240))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 320, 240, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


class SpeechTests(unittest.TestCase):
    def test_filler_and_commands(self):
        cfg = Config(api_key="")
        for text in ["嗯。", "嗯嗯", "en", "EN", "呃", "对。", "好的。", "，"]:
            self.assertTrue(ignored_speech(text, cfg), text)
        for text in ["嗯，背一首诗", "继续", "停止", "开灯", "对，再背一首", "你好小云"]:
            self.assertFalse(ignored_speech(text, cfg), text)
        cfg.ignore_standalone_ack = False
        self.assertFalse(ignored_speech("对", cfg))

    def test_filler_does_not_call_llm_tts_or_change_history(self):
        client = Mock()
        client.transcribe.return_value = "嗯。"
        history = [{"role": "system", "content": "test"}]
        self.assertEqual(ask_once(Config(api_key=""), client, history, Path("unused.wav"), active=True), ("", False, False))
        self.assertEqual(len(history), 1)
        client.chat_stream.assert_not_called()
        client.speak.assert_not_called()

    def test_vad_rejects_click_then_records_real_phrase(self):
        import io
        high = struct.pack("<hh", 2000, -2000) * 160
        low = b"\0" * 640
        # Short 100 ms sound is below minimum, followed by 400 ms speech.
        frames = high * 5 + low * 40 + high * 20 + low * 40
        proc = Mock(stdout=io.BytesIO(frames))
        proc.poll.return_value = 0
        with tempfile.TemporaryDirectory() as directory, patch("assistant.configure_capture"), \
                patch("assistant.subprocess.Popen", return_value=proc), \
                patch("assistant.select.select", return_value=([proc.stdout], [], [])):
            output = Path(directory) / "voice.wav"
            self.assertTrue(record_audio(Config(api_key=""), output, 10))
            import wave
            with wave.open(str(output)) as wav:
                # Rejected noise is not included in the WAV.
                self.assertLessEqual(wav.getnframes(), 70 * 320)


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config = self.root / "config.yaml"
        self.config.write_text('DASHSCOPE_API_KEY: test\nVISION_SOURCE: none\n', encoding="utf-8")
        (self.root / "images").mkdir()
        sample_png(self.root / "images" / "test.png")
        self.service = VisionService(self.config)

    def test_no_camera_does_not_call_cloud(self):
        self.service.client = Mock()
        result = self.service.call("inspect_current_view", {})
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["error"], "CAMERA_NOT_CONFIGURED")
        self.service.client.describe.assert_not_called()

    def test_image_coordinates(self):
        self.service.client.describe = Mock(return_value=json.dumps({"description": "红色矩形", "objects": [
            {"label": "矩形", "bbox": [250, 250, 750, 750], "confidence": 0.9}]}))
        result = self.service.call("analyze_image", {"image_path": "test.png"})
        self.assertFalse(result["isError"])
        obj = result["structuredContent"]["objects"][0]
        self.assertEqual(obj["bbox"], [80, 60, 240, 180])
        self.assertEqual(obj["center"], [160, 120])

    def test_paths_and_invalid_args(self):
        for arguments in [{"image_path": "../config.yaml"}, {"image_path": "missing.png"},
                          {"image_path": 1}, [], {"image_path": "test.png", "extra": "x"}]:
            self.assertTrue(self.service.call("analyze_image", arguments)["isError"])

    def test_default_image_excludes_demo_and_selects_upload(self):
        self.service.client.describe = Mock(return_value='{"description":"测试","objects":[]}')
        path = self.root / "images" / "test.png"
        path.rename(path.with_name("mcp_test.png"))
        result = self.service.call("analyze_image", {})
        self.assertEqual(result["structuredContent"]["error"], "NO_UPLOADED_IMAGE")
        self.service.client.describe.assert_not_called()
        sample_png(path)
        result = self.service.call("analyze_image", {})
        self.assertEqual(result["structuredContent"]["image_name"], "test.png")
        self.assertEqual(result["structuredContent"]["source_kind"], "uploaded_image")
        result = self.service.call("analyze_image", {"image_path": "mcp_test.png"})
        self.assertEqual(result["structuredContent"]["source_kind"], "test_file")

    def test_file_source_is_explicitly_labelled(self):
        self.service.source = "file"
        self.service.raw["VISION_FRAME_FILE"] = "test.png"
        self.service.client.describe = Mock(return_value='{"description":"测试图片","objects":[]}')
        result = self.service.call("inspect_current_view", {})["structuredContent"]
        self.assertEqual(result["source_kind"], "test_file")
        self.assertIn("不是", result["notice"])

    def test_camera_stable_device_link(self):
        device = "/dev/v4l/by-id/usb-Test-video-index0"
        self.service.raw["VISION_CAMERA_DEVICE"] = device
        with patch.object(Path, "resolve", return_value=Path("/dev/video7")), \
                patch.object(Path, "is_char_device", return_value=True):
            self.assertEqual(self.service.camera_device(), device)
        with patch.object(Path, "is_char_device", return_value=False):
            with self.assertRaises(ToolError):
                self.service.camera_device()

    def test_camera_capture_uses_config_and_cleans_frame(self):
        self.service.source = "camera"
        self.service.raw.update(VISION_CAMERA_FORMAT="mjpeg", VISION_CAMERA_SIZE="1280x720", VISION_CAMERA_FPS=10)
        self.service.camera_device = Mock(return_value="/dev/v4l/by-id/usb-Test-video-index0")
        captured = []
        def capture(command, **kwargs):
            self.assertIn("mjpeg", command)
            self.assertIn("1280x720", command)
            self.assertEqual(kwargs["timeout"], 12)
            path = Path(command[-1])
            path.write_bytes(b"test-frame")
            captured.append(path)
            return Mock(returncode=0, stderr=b"")
        self.service.analyze = Mock(return_value={"source_kind":"camera", "objects":[]})
        with patch("vision_mcp.shutil.which", return_value="ffmpeg"), \
                patch("vision_mcp.subprocess.run", side_effect=capture):
            result = self.service.call("inspect_current_view", {})
        self.assertFalse(result["isError"])
        self.assertIn("captured_at", result["structuredContent"])
        self.assertFalse(captured[0].exists())

    def test_default_image_uses_camera_only_without_upload(self):
        self.service.source = "camera"
        self.service.current_view = Mock(return_value={"source_kind":"camera", "objects":[]})
        self.service.client.describe = Mock(return_value='{"description":"上传图片","objects":[]}')
        result = self.service.call("analyze_image", {})
        self.assertEqual(result["structuredContent"]["source_kind"], "uploaded_image")
        self.service.current_view.assert_not_called()
        (self.root / "images" / "test.png").unlink()
        result = self.service.call("analyze_image", {})
        self.assertEqual(result["structuredContent"]["source_kind"], "camera")
        self.service.current_view.assert_called_once()
        result = self.service.call("analyze_image", {"image_path":"missing.png"})
        self.assertTrue(result["isError"])
        self.assertEqual(self.service.current_view.call_count, 1)

    def test_snapshot_gets_image_without_opening_usb(self):
        self.service.source = "snapshot"
        data = (self.root / "images" / "test.png").read_bytes()
        response = io.BytesIO(data)
        response.headers = Message()
        response.headers["Content-Type"] = "image/png"
        self.service.client.describe = Mock(return_value='{"description":"快照","objects":[]}')
        self.service.camera_device = Mock(side_effect=AssertionError("Snapshot must not open USB"))
        with patch("vision_mcp.urlopen", return_value=response) as fetch, \
                patch("vision_mcp.subprocess.run") as capture:
            result = self.service.call("inspect_current_view", {})
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"]["source_kind"], "snapshot")
        self.assertEqual(result["structuredContent"]["width"], 320)
        self.assertIn("fetched_at", result["structuredContent"])
        self.assertNotIn("captured_at", result["structuredContent"])
        self.assertEqual(fetch.call_args.kwargs["timeout"], 8)
        self.assertTrue(response.closed)
        self.assertFalse(self.service.client.describe.call_args.args[0].exists())
        capture.assert_not_called()

    def test_snapshot_errors_do_not_call_vision(self):
        import urllib.error
        self.service.source = "snapshot"
        self.service.client.describe = Mock()
        with patch("vision_mcp.urlopen", side_effect=urllib.error.URLError("offline")):
            result = self.service.call("inspect_current_view", {})
        self.assertEqual(result["structuredContent"]["error"], "SNAPSHOT_UNAVAILABLE")
        response = io.BytesIO(b"<html>error</html>")
        response.headers = Message()
        response.headers["Content-Type"] = "text/html"
        with patch("vision_mcp.urlopen", return_value=response):
            result = self.service.call("inspect_current_view", {})
        self.assertEqual(result["structuredContent"]["error"], "INVALID_SNAPSHOT")
        self.service.client.describe.assert_not_called()

    def test_no_upload_uses_snapshot(self):
        self.service.source = "snapshot"
        (self.root / "images" / "test.png").unlink()
        self.service.snapshot_view = Mock(return_value={"source_kind":"snapshot", "objects":[]})
        result = self.service.call("analyze_image", {})
        self.assertEqual(result["structuredContent"]["source_kind"], "snapshot")
        self.service.snapshot_view.assert_called_once()

    def test_stdio_lifecycle_and_tool_errors(self):
        with VisionMCPClient(self.config, timeout=5) as client:
            self.assertEqual({t['function']['name'] for t in client.tools()}, {'analyze_image', 'inspect_current_view', 'vision_status', 'list_navigation_points', 'navigate_to_point', 'cancel_navigation', 'get_navigation_status', 'get_wifi_ip'})
            self.assertEqual(client.request("ping", {}), {})
            self.assertFalse(client.call("vision_status", {})["isError"])
            self.assertTrue(client.call("inspect_current_view", {})["isError"])
            self.assertTrue(client.call("unknown", {})["isError"])
        self.assertIsNotNone(client.process.poll())

    def test_protocol_errors(self):
        server = MCPServer(self.service)
        self.assertEqual(server.handle([])["error"]["code"], -32600)
        self.assertEqual(server.handle({"jsonrpc":"2.0", "id":1, "method":"tools/list"})["error"]["code"], -32002)


class ToolStreamingTests(unittest.TestCase):
    def test_fragmented_tool_call_and_followup_answer(self):
        import io
        client = BailianClient(Config(api_key="test"))
        bridge = Mock()
        bridge.tools.return_value = [{"type": "function", "function": {"name": "inspect_current_view"}}]
        bridge.call.return_value = {"isError": True, "content": [{"type":"text", "text":"未配置摄像头"}]}
        client.connect_tools = Mock(return_value=bridge)
        def sse(deltas):
            return io.BytesIO(b"".join(('data: ' + json.dumps({"choices":[{"delta": d}]}) + '\n\n').encode()
                                     for d in deltas) + b'data: [DONE]\n\n')
        first = sse([{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"inspect_current_view","arguments":'{"question":'}}]},
                     {"tool_calls":[{"index":0,"function":{"arguments":'"查看画面"}'}}]}])
        second = sse([{"content":"尚未配置"}, {"content":"摄像头。"}])
        with patch("assistant.urlopen", side_effect=[first, second]) as urlopen:
            self.assertEqual("".join(client.chat_stream([{"role":"user","content":"你看到了什么"}])), "尚未配置摄像头。")
            payload = json.loads(urlopen.call_args_list[1].args[0].data)
            self.assertEqual(payload["messages"][-1]["tool_call_id"], "call_1")
        bridge.call.assert_called_once_with("inspect_current_view", {"question":"查看画面"})


if __name__ == "__main__":
    unittest.main()
