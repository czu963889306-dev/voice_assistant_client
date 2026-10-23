import contextlib
import io
import signal
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parents[1]))
import assistant
from assistant import AssistantError, Config, play_startup_sound, startup_sound_path


class StartupSoundTests(unittest.TestCase):
    def test_config_and_path_follow_config_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.yaml"
            path.write_text('DASHSCOPE_API_KEY: ""\nSTARTUP_SOUND_ENABLED: false\n'
                            'STARTUP_SOUND_FILE: "sounds/ready.wav"\n', encoding="utf-8")
            cfg = Config.from_file(path)
            self.assertFalse(cfg.startup_sound_enabled)
            self.assertEqual(startup_sound_path(cfg), (path.parent / "sounds/ready.wav").resolve())

    def test_disabled_sound_never_opens_file_or_audio(self):
        with patch("assistant.wave.open") as read, patch("assistant.play_audio") as play:
            self.assertFalse(play_startup_sound(Config(api_key="", startup_sound_enabled=False)))
        read.assert_not_called()
        play.assert_not_called()

    def test_valid_pcm_plays_after_mixer_and_before_echo_delay(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "ready.wav"
            with wave.open(str(path), "wb") as wav:
                wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                wav.writeframes(b"\x00\x00" * 1600)
            cfg = Config(api_key="", startup_sound_file=str(path))
            calls = Mock()
            with patch("assistant.configure_capture", calls.mixer), patch("assistant.play_audio", calls.play), \
                    patch("assistant.time.sleep", calls.sleep), contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(play_startup_sound(cfg))
            self.assertEqual([c[0] for c in calls.mock_calls], ["mixer", "play", "sleep"])
            calls.play.assert_called_once_with(cfg, path.resolve())

    def test_missing_invalid_empty_or_long_files_do_not_start_player(self):
        with tempfile.TemporaryDirectory() as folder, patch("assistant.play_audio") as play:
            path = Path(folder) / "bad.wav"
            cfg = Config(api_key="", startup_sound_file=str(path))
            with self.assertRaisesRegex(AssistantError, "无法读取"):
                play_startup_sound(cfg)
            path.write_bytes(b"not a WAV")
            with self.assertRaisesRegex(AssistantError, "无法读取"):
                play_startup_sound(cfg)
            for count in (0, 16000 * 16):
                with wave.open(str(path), "wb") as wav:
                    wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                    wav.writeframes(b"\x00\x00" * count)
                with self.assertRaisesRegex(AssistantError, "时长"):
                    play_startup_sound(cfg)
            # A valid header without its promised audio must also be rejected.
            with wave.open(str(path), "wb") as wav:
                wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                wav.writeframes(b"\x00\x00" * 1600)
            path.write_bytes(path.read_bytes()[:-100])
            with self.assertRaisesRegex(AssistantError, "不完整"):
                play_startup_sound(cfg)
            play.assert_not_called()

    def test_standalone_playback_needs_no_key_mcp_or_microphone(self):
        cfg = Config(api_key="")
        with patch.object(sys, "argv", ["assistant.py", "--test-startup-sound"]), \
                patch("assistant.Config.from_file", return_value=cfg), \
                patch("assistant.play_startup_sound", return_value=True) as play, \
                patch("assistant.BailianClient") as cloud, patch("assistant.record_audio") as record:
            self.assertEqual(assistant.main(), 0)
        play.assert_called_once_with(cfg)
        cloud.assert_not_called()
        record.assert_not_called()

    def test_standalone_failure_returns_nonzero(self):
        with patch.object(sys, "argv", ["assistant.py", "--test-startup-sound"]), \
                patch("assistant.Config.from_file", return_value=Config(api_key="")), \
                patch("assistant.play_startup_sound", side_effect=AssistantError("test failure")), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(assistant.main(), 1)

    def run_service_loop(self, playback_error=False):
        cfg = Config(api_key="test", wake_word="你好小云")
        events, handlers = [], {}

        def register(sig, handler):
            handlers[sig] = handler

        def play(_cfg):
            events.append("startup")
            if playback_error:
                raise AssistantError("test failure")

        def ask(*args, **kwargs):
            events.append("listen")
            if events.count("listen") == 3:
                handlers[signal.SIGTERM](signal.SIGTERM, None)
            return "", False, False

        with patch.object(sys, "argv", ["assistant.py"]), \
                patch("assistant.Config.from_file", return_value=cfg), \
                patch("assistant.BailianClient"), patch("assistant.signal.signal", side_effect=register), \
                patch("assistant.missing_audio_cards", side_effect=[["USB"], [], [], ["USB"], []]), \
                patch("assistant.play_startup_sound", side_effect=play), \
                patch("assistant.ask_once", side_effect=ask), patch("assistant.time.sleep"), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(assistant.main(), 0)
        return events

    def test_startup_waits_for_usb_and_plays_once_across_reconnect(self):
        self.assertEqual(self.run_service_loop(), ["startup", "listen", "listen", "listen"])

    def test_failed_startup_sound_does_not_block_or_repeat_in_dialogue(self):
        self.assertEqual(self.run_service_loop(playback_error=True),
                         ["startup", "listen", "listen", "listen"])


if __name__ == "__main__":
    unittest.main()
