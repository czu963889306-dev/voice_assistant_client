import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parents[1]))
from assistant import BailianClient, Config, command_reply, extract_wake_command, load_yaml, sentence_chunks
from assistant import AssistantError, stream_answer_and_speak
from assistant import alsa_card_id, missing_audio_cards, configure_capture, record_audio


class CommandReplyTests(unittest.TestCase):
    def test_time(self):
        self.assertIn("12点34分", command_reply("现在几点", datetime(2026, 1, 2, 12, 34)))

    def test_date(self):
        self.assertIn("2026年01月02日", command_reply("今天几号", datetime(2026, 1, 2)))

    def test_greeting(self):
        self.assertEqual(command_reply("你好"), "你好，我在。")

    def test_unknown(self):
        self.assertIsNone(command_reply("讲个故事"))


class ConfigTests(unittest.TestCase):
    def test_yaml_and_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text('DASHSCOPE_API_KEY: "test-key"\nRECORD_SECONDS: 9\n', encoding="utf-8")
            cfg = Config.from_file(path)
        self.assertEqual(cfg.api_key, "test-key")
        self.assertEqual(cfg.record_seconds, 9)
        self.assertEqual(cfg.stt_model, "qwen3-asr-flash")
        self.assertIn("compatible-mode", cfg.base_url)

    def test_workspace_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text('DASHSCOPE_API_KEY: x\nBAILIAN_WORKSPACE_ID: ws-test\n', encoding="utf-8")
            cfg = Config.from_file(path)
        self.assertIn("ws-test.cn-beijing.maas.aliyuncs.com", cfg.base_url)
        self.assertIn("ws-test.cn-beijing.maas.aliyuncs.com", cfg.tts_url)


class AudioDeviceTests(unittest.TestCase):
    def test_card_name_and_legacy_number(self):
        self.assertEqual(alsa_card_id("plughw:CARD=UACDemoV10,DEV=0"), "UACDemoV10")
        self.assertEqual(alsa_card_id("hw:2,0"), "2")
        self.assertIsNone(alsa_card_id("default"))

    def test_card_id_survives_number_change_and_hotplug(self):
        cfg = Config(api_key="")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "card2").mkdir()
            (root / "UACDemoV10").mkdir()
            self.assertEqual(missing_audio_cards(cfg, root), [])
            (root / "card2").rename(root / "card1")
            self.assertEqual(missing_audio_cards(cfg, root), [])
            (root / "UACDemoV10").rmdir()
            self.assertEqual(missing_audio_cards(cfg, root), ["UACDemoV10"])
            (root / "UACDemoV10").mkdir()
            self.assertEqual(missing_audio_cards(cfg, root), [])

    def test_mixer_handles_separate_named_input_and_output(self):
        cfg = Config(api_key="", input_device="plughw:CARD=Mic,DEV=0", output_device="plughw:CARD=Speaker,DEV=0")
        with patch("assistant.shutil.which", return_value="amixer"), patch("assistant.run_command") as run:
            configure_capture(cfg)
        self.assertEqual([call.args[0][2] for call in run.call_args_list], ["Mic", "Mic", "Speaker"])

    def test_capture_failure_includes_alsa_reason(self):
        import io
        process = Mock(stdout=io.BytesIO(), stderr=io.BytesIO(b"Unknown PCM plughw:2,0"), returncode=1)
        process.poll.return_value = 1
        with patch("assistant.configure_capture"), patch("assistant.subprocess.Popen", return_value=process), \
                patch("assistant.select.select", return_value=([process.stdout], [], [])):
            with self.assertRaisesRegex(AssistantError, "Unknown PCM"):
                record_audio(Config(api_key=""), Path("unused.wav"), 1)


class WakeWordTests(unittest.TestCase):
    def test_asr_punctuation_is_ignored(self):
        self.assertEqual(extract_wake_command("你好，小云。今天天气怎么样？", "你好小云"), "今天天气怎么样")

    def test_wrong_wake_word_is_ignored(self):
        self.assertIsNone(extract_wake_command("你好，助手。", "你好小云"))


class BailianResponseTests(unittest.TestCase):
    def test_message_text(self):
        self.assertEqual(BailianClient._message_text({"choices": [{"message": {"content": "回答"}}]}), "回答")
        self.assertEqual(BailianClient._message_text({"choices": [{"message": {"content": [{"text": "答"}, {"text": "复"}]}}]}), "答复")

    def test_sse_stream_events_and_deltas(self):
        events = list(BailianClient._stream_events([
            b'data: {"choices":[{"delta":{"content":"\\u4f60\\u597d"}}]}\n',
            b'\n',
            b'data: {"choices":[{"delta":{"content":"\\uff0c\\u5c0f\\u4e91\\u3002"}}]}\n',
            b'\n',
            b'data: [DONE]\n',
            b'\n',
        ]))
        self.assertEqual([BailianClient._delta_text(event) for event in events], ["你好", "，小云。"])

    def test_sentence_chunks_preserve_order(self):
        self.assertEqual(list(sentence_chunks(["第一句。第二", "句！第三句"])), ["第一句。", "第二句！", "第三句"])

    def test_done_does_not_wait_for_socket_eof(self):
        def connection():
            yield b'data: {"choices":[{"delta":{"content":"ok"}}]}\n'
            yield b'\n'
            yield b'data: [DONE]\n'
            raise AssertionError("Must not read the idle connection after DONE")
        events = list(BailianClient._stream_events(connection()))
        self.assertEqual(BailianClient._delta_text(events[0]), "ok")

    def test_filename_and_decimal_across_fragments(self):
        self.assertEqual(list(sentence_chunks(["识别 test.", "png，数字3.", "14。下一句。"])),
                         ["识别 test.png，数字3.14。", "下一句。"])

    def test_tts_failure_returns_instead_of_hanging(self):
        client = Mock()
        client.chat_stream.return_value = iter(["第一句。", "第二句。"])
        client.speak.side_effect = AssistantError("测试超时")
        with patch("assistant.play_audio") as player:
            with self.assertRaisesRegex(AssistantError, "测试超时"):
                stream_answer_and_speak(Config(api_key=""), client, [])
            player.assert_not_called()


if __name__ == "__main__":
    unittest.main()
