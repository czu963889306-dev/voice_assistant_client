"""Regressions for punctuation-only TTS breaking a later MCP vision answer."""
import base64
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parents[1]))
from assistant import AssistantError, BailianClient, Config
from assistant import sentence_chunks, speech_text, stream_answer_and_speak


class SpeechTextTests(unittest.TestCase):
    def test_ascii_ellipsis_is_one_sentence_across_sse_deltas(self):
        text = "正在查看前方画面...前方有一个人。"
        for fragments in ([text], list(text), ["正在查看前方画面", ".", ".", ".", "前方有一个人。"]):
            with self.subTest(fragments=fragments):
                self.assertEqual(list(sentence_chunks(fragments)),
                                 ["正在查看前方画面...", "前方有一个人。"])

    def test_chinese_ellipsis_and_repeated_punctuation_are_not_spoken_alone(self):
        self.assertEqual(list(sentence_chunks(["正在查看……", "……！\n", "画面里有纸箱。", "？！"])),
                         ["正在查看……", "画面里有纸箱。"])

    def test_punctuation_markup_and_hidden_characters_are_not_sentences(self):
        self.assertEqual(list(sentence_chunks(["\u200b...。\n**\n```\n", "你好。", "！"])), ["你好。"])

    def test_markdown_cleanup_retains_readable_words_numbers_and_math(self):
        self.assertEqual(speech_text("## **前方**有 2 个人。\u200b\x00"), "前方有 2 个人。")
        self.assertEqual(speech_text("`test.png`，3.14，1+1=2。"), "test.png，3.14，1+1=2。")

    def test_speak_rejects_empty_or_symbol_only_text_without_cloud_request(self):
        client = BailianClient(Config(api_key="test-key"))
        client._json_post = Mock()
        for text in ("", " \n", ".", "...", "？！", "**", "```", "\u200b\ufeff", "😀"):
            with self.subTest(text=text), self.assertRaisesRegex(AssistantError, "没有可朗读"):
                client.speak(text, Path("unused.wav"))
        client._json_post.assert_not_called()

    def test_speak_posts_clean_plain_text_and_saves_audio(self):
        client = BailianClient(Config(api_key="test-key"))
        client._json_post = Mock(return_value={"output": {"audio": {
            "data": base64.b64encode(b"test-audio").decode()}}})
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "voice.wav"
            client.speak("**前方**有纸箱。\u200b", output)
            self.assertEqual(output.read_bytes(), b"test-audio")
        self.assertEqual(client._json_post.call_args.args[1]["input"]["text"], "前方有纸箱。")


class VisionSpeechFlowTests(unittest.TestCase):
    def test_preamble_plays_before_tool_finishes_then_final_answer_is_spoken(self):
        spoken = []
        played = []
        preamble_played = threading.Event()
        client = Mock()

        def fragments(_history):
            yield "正在查看前方画面"
            yield "."
            yield "."
            yield "."
            # Simulate waiting for the vision MCP result. The status sentence
            # must already reach the speaker while that tool is still running.
            if not preamble_played.wait(timeout=3):
                raise AssertionError("Vision preamble was buffered until after the tool result")
            yield "\n**\n"
            yield "前方有一个人，右侧有手推车。"

        def synthesize(text, output):
            if not any(char.isalnum() for char in text):
                raise AssistantError("InvalidParameter: Please ensure input text is valid.")
            spoken.append(text)
            output.write_text(text, encoding="utf-8")

        def play(_cfg, path):
            played.append(path.read_text(encoding="utf-8"))
            if len(played) == 1:
                preamble_played.set()

        client.chat_stream.side_effect = fragments
        client.speak.side_effect = synthesize
        with patch("assistant.play_audio", side_effect=play):
            answer = stream_answer_and_speak(Config(api_key=""), client, [])
        self.assertEqual(spoken, ["正在查看前方画面...", "前方有一个人，右侧有手推车。"])
        self.assertEqual(played, spoken)
        self.assertIn("前方有一个人，右侧有手推车。", answer)

    def test_symbol_only_response_never_reaches_tts(self):
        client = Mock()
        client.chat_stream.return_value = iter(["...", "！\n", "**"])
        with patch("assistant.play_audio") as player, self.assertRaisesRegex(AssistantError, "没有返回文字"):
            stream_answer_and_speak(Config(api_key=""), client, [])
        client.speak.assert_not_called()
        player.assert_not_called()


if __name__ == "__main__":
    unittest.main()
