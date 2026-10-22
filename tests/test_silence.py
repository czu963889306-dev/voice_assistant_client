import math
import struct
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parents[1]))
from assistant import BailianClient, Config, audio_has_voice, ask_once, _pcm_rms, suspicious_wake_list


def write_wav(path, values):
    with wave.open(str(path), 'wb') as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000)
        wav.writeframes(struct.pack('<%dh' % len(values), *values))


class SilenceTests(unittest.TestCase):
    def test_silence_noise_click_and_dc_never_reach_cloud(self):
        for values in ([0] * 16000, [2000] * 16000,
                       [int(100 * math.sin(i * 0.13)) for i in range(16000)],
                       [2000, -2000] * 800 + [0] * 14400):
            with tempfile.TemporaryDirectory() as folder:
                audio = Path(folder) / 'test.wav'; write_wav(audio, values)
                client = BailianClient(Config(api_key=''))
                client._json_post = Mock(side_effect=AssertionError('Silence must not be uploaded'))
                self.assertEqual(client.transcribe(audio), '')
                client._json_post.assert_not_called()

    def test_real_audio_payload_has_no_wake_or_point_hint(self):
        with tempfile.TemporaryDirectory() as folder:
            audio = Path(folder) / 'test.wav'
            write_wav(audio, [int(2000 * math.sin(i * 0.13)) for i in range(16000)])
            client = BailianClient(Config(api_key='', wake_word='你好小云'))
            client._json_post = Mock(return_value={'choices': [{'message': {'content': '导航到一号位'}}]})
            self.assertTrue(audio_has_voice(audio, client.cfg))
            self.assertEqual(client.transcribe(audio), '导航到一号位')
            messages = client._json_post.call_args.args[1]['messages']
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0]['role'], 'user')
            self.assertEqual(messages[0]['content'][0]['type'], 'input_audio')

    def test_dc_offset_does_not_count_as_speech_energy(self):
        self.assertEqual(_pcm_rms(struct.pack('<320h', *([5000] * 320))), 0)
        self.assertAlmostEqual(_pcm_rms(struct.pack('<hh', 5000 + 2000, 5000 - 2000)), 2000)

    def test_prompt_echo_cannot_wake_extend_session_or_speak(self):
        cfg = Config(api_key='', wake_word='你好小云')
        for active in [False, True]:
            for text in ['你好小云、一号位、二号位、三号位。', '你好，小云，1号位、2号位、3号位。']:
                client = Mock(); client.transcribe.return_value = text
                history = [{'role': 'system', 'content': 'test'}]
                with patch('assistant.play_audio') as play:
                    self.assertEqual(ask_once(cfg, client, history, Path('unused.wav'), active), ('', False, False))
                    client.chat_stream.assert_not_called(); client.speak.assert_not_called(); play.assert_not_called()
                self.assertEqual(len(history), 1)
        for text in ['你好小云', '你好小云，导航到一号位', '导航到一号位', '你好小云有哪些导航点']:
            self.assertFalse(suspicious_wake_list(text, cfg.wake_word))
