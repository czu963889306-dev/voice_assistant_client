"""Curated options compatible with the assistant's current HTTP protocols."""
ENDPOINTS = {
    'cn-beijing': {
        'label': '千问百炼 · 北京',
        'base_url': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
        'tts_url': 'https://dashscope.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer',
    },
    'ap-southeast-1': {
        'label': '千问百炼 · 新加坡',
        'base_url': 'https://dashscope-intl.aliyuncs.com/compatible-mode/v1',
        'tts_url': 'https://dashscope-intl.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer',
    },
}


def option(value, label):
    return {'value': value, 'label': label}


MODEL_CHOICES = {
    'CHAT_MODEL': [option('qwen-plus', '千问 Plus · 均衡'), option('qwen-turbo', '千问 Turbo · 快速'), option('qwen-max', '千问 Max · 高能力')],
    'STT_MODEL': [option('qwen3-asr-flash', '千问3 ASR Flash'), option('qwen3-asr-flash-2025-09-08', '千问3 ASR Flash · 2025-09-08 快照')],
    'TTS_MODEL': [option('cosyvoice-v3-flash', 'CosyVoice 3 Flash'), option('cosyvoice-v3-plus', 'CosyVoice 3 Plus')],
    'VISION_MODEL': [option('qwen3-vl-plus', '千问3 VL Plus'), option('qwen3-vl-flash', '千问3 VL Flash'), option('qwen-vl-plus', '千问 VL Plus'), option('qwen-vl-max', '千问 VL Max')],
    'BAILIAN_REGION': [option(region, values['label']) for region, values in ENDPOINTS.items()],
    'BAILIAN_BASE_URL': [option(values['base_url'], values['label']) for values in ENDPOINTS.values()],
    'BAILIAN_TTS_URL': [option(values['tts_url'], values['label'] + ' · 语音合成') for values in ENDPOINTS.values()],
}
VOICE_CHOICES = {
    'cosyvoice-v3-flash': [option('longanhuan_v3', '龙安欢 · 元气女声'), option('longanyang', '龙安洋 · 阳光男声')],
    'cosyvoice-v3-plus': [option('longanhuan', '龙安欢 · 元气女声'), option('longanyang', '龙安洋 · 阳光男声')],
}


def catalog():
    return {'choices': MODEL_CHOICES, 'voices': VOICE_CHOICES, 'endpoints': ENDPOINTS}
