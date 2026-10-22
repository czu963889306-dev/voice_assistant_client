"""Read/write the model fields in the existing flat YAML. Never return the key."""
import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from assistant import Config, load_yaml
from model_catalog import catalog

FIELDS = {
    'BAILIAN_BASE_URL': Config.base_url, 'BAILIAN_TTS_URL': Config.tts_url,
    'BAILIAN_WORKSPACE_ID': '', 'BAILIAN_REGION': 'cn-beijing',
    'CHAT_MODEL': Config.chat_model, 'STT_MODEL': Config.stt_model,
    'TTS_MODEL': Config.tts_model, 'TTS_VOICE': Config.tts_voice,
    'VISION_MODEL': Config.vision_model,
}


def public_settings(path):
    raw = load_yaml(path)
    effective = Config.from_file(path)
    return {'settings': {name: str(raw.get(name, default)) for name, default in FIELDS.items()},
            'api_key_configured': bool(raw.get('DASHSCOPE_API_KEY')),
            'effective_base_url': effective.base_url, 'effective_tts_url': effective.tts_url,
            **catalog()}


def save_settings(path, body):
    if not isinstance(body, dict) or set(body) - (set(FIELDS) | {'DASHSCOPE_API_KEY'}):
        raise ValueError('配置字段无效')
    changes = {}
    for name, value in body.items():
        if not isinstance(value, str) or len(value) > (4096 if name == 'DASHSCOPE_API_KEY' else 1024):
            raise ValueError('配置内容格式或长度无效')
        value = value.strip()
        if name == 'DASHSCOPE_API_KEY':
            if not value: continue  # Empty means retain, never erase.
            if len(value) < 10 or re.search(r'\s|[\x00-\x1f\x7f]', value):
                raise ValueError('API Key 格式无效，请粘贴完整密钥')
        elif name.endswith('_URL'):
            parsed = urlsplit(value)
            if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or
                    parsed.username or parsed.password or parsed.query or parsed.fragment or
                    re.search(r'\s|[\x00-\x1f\x7f]', value)):
                raise ValueError('接口 URL 必须是完整的 HTTP/HTTPS 地址，不含账号、查询参数或片段')
            if name == 'BAILIAN_BASE_URL' and parsed.path.rstrip('/').endswith('/chat/completions'):
                raise ValueError('Base URL 不需要包含 /chat/completions，例如填写 /compatible-mode/v1')
            value = value.rstrip('/')
        elif name == 'BAILIAN_WORKSPACE_ID':
            if value and not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
                raise ValueError('业务空间 ID 格式无效')
        elif not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}', value):
            raise ValueError('模型、音色或地域名称格式无效')
        changes[name] = value
    path = Path(path)
    original = path.read_text(encoding='utf-8')
    raw = load_yaml(path)
    changes = {name: value for name, value in changes.items() if str(raw.get(name, FIELDS.get(name, ''))) != value}
    if not changes: return {**public_settings(path), 'changed': False}
    lines, written = [], set()
    for line in original.splitlines():
        match = re.match(r'^\s*([A-Z][A-Z0-9_]*)\s*:', line)
        if match and match[1] in changes:
            name = match[1]
            if name not in written:
                lines.append(name + ': ' + json.dumps(changes[name], ensure_ascii=False))
                written.add(name)
        else:
            lines.append(line)
    lines.extend(name + ': ' + json.dumps(value, ensure_ascii=False) for name, value in changes.items() if name not in written)
    content = '\n'.join(lines) + '\n'
    descriptor, temporary = tempfile.mkstemp(prefix='.model-config-', suffix='.yaml', dir=path.parent)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(content); stream.flush(); os.fsync(stream.fileno())
        Config.from_file(temporary)  # Validate with the same loader the assistant uses.
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
    return {**public_settings(path), 'changed': True}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['read', 'save'])
    parser.add_argument('config')
    args = parser.parse_args()
    try:
        result = public_settings(args.config) if args.mode == 'read' else save_settings(args.config, json.load(sys.stdin))
        print(json.dumps({'success': True, **result}, ensure_ascii=False))
    except ValueError as error:
        print(json.dumps({'success': False, 'error': str(error)}, ensure_ascii=False))
    except Exception:
        print(json.dumps({'success': False, 'error': '无法读写语音助手配置，请检查文件及权限'}, ensure_ascii=False))
