import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1]))
from assistant import Config, load_yaml
from model_settings import public_settings, save_settings
from model_catalog import catalog


class ModelSettingsTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / 'config.yaml'
        self.path.write_text('# existing settings\nDASHSCOPE_API_KEY: "test-existing-secret"\nCHAT_MODEL: qwen-plus\nWAKE_WORD: "你好小云"\nVAD_RMS_THRESHOLD: 500\nVISION_SOURCE: snapshot\n', encoding='utf-8')

    def tearDown(self):
        self.folder.cleanup()

    def test_public_response_never_contains_secret_and_empty_key_retains_it(self):
        for result in [public_settings(self.path), save_settings(self.path, {'DASHSCOPE_API_KEY': '', 'CHAT_MODEL': 'qwen-turbo'})]:
            self.assertNotIn('test-existing-secret', json.dumps(result))
            self.assertNotIn('DASHSCOPE_API_KEY', result['settings'])
            self.assertTrue(result['api_key_configured'])
        cfg = Config.from_file(self.path)
        self.assertEqual(cfg.api_key, 'test-existing-secret')
        self.assertEqual(cfg.chat_model, 'qwen-turbo')
        self.assertEqual(cfg.wake_word, '你好小云')
        self.assertEqual(cfg.vad_rms_threshold, 500)
        self.assertIn('# existing settings', self.path.read_text(encoding='utf-8'))
        self.assertEqual(load_yaml(self.path)['VISION_SOURCE'], 'snapshot')

    def test_replace_key_and_urls_load_in_actual_assistant(self):
        result = save_settings(self.path, {'DASHSCOPE_API_KEY': 'test-new-secret', 'BAILIAN_BASE_URL': 'https://example.com/v1/',
                                           'BAILIAN_TTS_URL': 'https://example.com/tts', 'VISION_MODEL': 'qwen3-vl-plus'})
        self.assertTrue(result['changed'])
        self.assertNotIn('test-new-secret', json.dumps(result))
        cfg = Config.from_file(self.path)
        self.assertEqual(cfg.api_key, 'test-new-secret')
        self.assertEqual(cfg.base_url, 'https://example.com/v1')
        self.assertEqual(cfg.tts_url, 'https://example.com/tts')
        if os.name != 'nt': self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_invalid_values_never_modify_file(self):
        original = self.path.read_bytes()
        for body in [{'SYSTEM_PROMPT': 'unexpected'}, {'BAILIAN_BASE_URL': 'javascript:alert(1)'},
                     {'BAILIAN_BASE_URL': 'https://user:password@example.com'},
                     {'BAILIAN_BASE_URL': 'https://example.com/v1/chat/completions'},
                     {'CHAT_MODEL': 'qwen\nDASHSCOPE_API_KEY: injected'}, {'DASHSCOPE_API_KEY': 'short'}]:
            with self.assertRaises(ValueError): save_settings(self.path, body)
            self.assertEqual(self.path.read_bytes(), original)

    def test_atomic_failure_preserves_original_and_removes_temporary(self):
        original = self.path.read_bytes()
        with patch('model_settings.os.replace', side_effect=OSError('simulated write failure')):
            with self.assertRaises(OSError): save_settings(self.path, {'CHAT_MODEL': 'qwen-turbo'})
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob('.model-config-*')), [])

    def test_unchanged_save_does_not_rewrite(self):
        with patch('model_settings.os.replace') as replace:
            self.assertFalse(save_settings(self.path, {'CHAT_MODEL': 'qwen-plus', 'DASHSCOPE_API_KEY': ''})['changed'])
            replace.assert_not_called()

    def test_catalog_covers_existing_defaults_and_matching_endpoint_regions(self):
        result = public_settings(self.path)
        for field in ['CHAT_MODEL', 'STT_MODEL', 'TTS_MODEL', 'VISION_MODEL', 'BAILIAN_REGION', 'BAILIAN_BASE_URL', 'BAILIAN_TTS_URL']:
            self.assertIn(result['settings'][field], [item['value'] for item in result['choices'][field]])
        self.assertIn(Config.tts_voice, [item['value'] for item in result['voices'][Config.tts_model]])
        for values in catalog()['endpoints'].values():
            Config.from_file(self.path)
            self.assertTrue(values['base_url'].endswith('/compatible-mode/v1'))
            self.assertTrue(values['tts_url'].endswith('/SpeechSynthesizer'))
