#!/usr/bin/env python3
"""百炼 Qwen-VL 图片理解工具：描述图片并输出对象的像素框与中心点。"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import re
import struct
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from assistant import AssistantError, Config
from http_transport import urlopen


DEFAULT_PROMPT = """请分析这张图片并严格只输出 JSON，不要 Markdown 代码围栏或额外解释。
输出格式：
{"description":"用中文简要描述整张图片","objects":[{"label":"对象名称","bbox":[x1,y1,x2,y2],"confidence":0.0}]}
要求：objects 列出图片中清晰可见的主要实体；bbox 是左上角和右下角坐标，必须使用相对于图片宽高的 0 到 1000 归一化坐标；confidence 为 0 到 1 的估计值。不要输出图片外的对象。"""


def image_dimensions(path: Path) -> tuple[int, int]:
    """Read common image dimensions without requiring Pillow on the RK3566."""
    data = path.read_bytes()
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    if data[:6] in {b"GIF87a", b"GIF89a"} and len(data) >= 10:
        return struct.unpack("<HH", data[6:10])
    if data.startswith(b"BM") and len(data) >= 26:
        width, height = struct.unpack("<ii", data[18:26])
        return abs(width), abs(height)
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP" and len(data) >= 30:
        chunk = data[12:16]
        if chunk == b"VP8X" and len(data) >= 30:
            return (1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little"))
        if chunk == b"VP8 " and len(data) >= 30:
            marker = data.find(b"\x9d\x01\x2a", 20, min(len(data), 64))
            if marker >= 0 and marker + 7 <= len(data):
                return struct.unpack("<HH", data[marker + 3:marker + 7])
    if data.startswith(b"\xff\xd8"):
        index = 2
        sof_markers = set(range(0xC0, 0xC4)) | set(range(0xC5, 0xC8)) | set(range(0xC9, 0xCC)) | set(range(0xCD, 0xD0))
        while index + 4 <= len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            while index < len(data) and data[index] == 0xFF:
                index += 1
            if index >= len(data):
                break
            marker = data[index]
            index += 1
            if marker in {0xD8, 0xD9}:
                continue
            if index + 2 > len(data):
                break
            length = struct.unpack(">H", data[index:index + 2])[0]
            if length < 2 or index + length > len(data):
                break
            if marker in sof_markers and length >= 7:
                height, width = struct.unpack(">HH", data[index + 3:index + 7])
                return width, height
            index += length
    raise AssistantError(f"无法读取图片尺寸（支持 PNG/JPEG/GIF/BMP/WebP）：{path}")


def extract_json(text: str) -> dict[str, Any]:
    """Extract the first JSON object even when a model adds a code fence."""
    decoder = json.JSONDecoder()
    for match in re.finditer(r"[\[{]", text):
        try:
            value, _ = decoder.raw_decode(text[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(value, list):
            return {"description": "", "objects": value}
        if isinstance(value, dict):
            return value
    raise AssistantError(f"视觉模型没有返回可解析的 JSON：{text[:500]}")


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize_result(raw: dict[str, Any], width: int, height: int) -> dict[str, Any]:
    """Convert model [0,1000] boxes into integer pixel boxes and centers."""
    result: dict[str, Any] = {
        "description": str(raw.get("description", raw.get("caption", ""))).strip(),
        "width": width,
        "height": height,
        "coordinate_system": "pixels",
        "objects": [],
    }
    objects = raw.get("objects", raw.get("items", []))
    if not isinstance(objects, list):
        objects = []
    for item in objects:
        if not isinstance(item, dict):
            continue
        coords = item.get("bbox", item.get("box", item.get("bounding_box")))
        if not isinstance(coords, (list, tuple)) or len(coords) < 4:
            continue
        # The prompt fixes the model coordinate system to 0..1000.
        x1, y1, x2, y2 = (_number(value) for value in coords[:4])
        x1 = round(max(0, min(width, x1 / 1000 * width)))
        y1 = round(max(0, min(height, y1 / 1000 * height)))
        x2 = round(max(0, min(width, x2 / 1000 * width)))
        y2 = round(max(0, min(height, y2 / 1000 * height)))
        if x2 < x1:
            x1, x2 = x2, x1
        if y2 < y1:
            y1, y2 = y2, y1
        result["objects"].append({
            "label": str(item.get("label", item.get("name", "未知对象"))),
            "bbox": [x1, y1, x2, y2],
            "center": [round((x1 + x2) / 2), round((y1 + y2) / 2)],
            "confidence": round(max(0.0, min(1.0, _number(item.get("confidence"), 0.0))), 3),
        })
    return result


class VisionClient:
    def __init__(self, cfg: Config, model: str | None = None, timeout: int | None = None):
        self.cfg = cfg
        self.model = model or "qwen3-vl-plus"
        self.timeout = timeout if timeout is not None else cfg.http_read_timeout_seconds

    def describe(self, image: Path, prompt: str) -> str:
        mime = mimetypes.guess_type(image.name)[0] or "application/octet-stream"
        encoded = base64.b64encode(image.read_bytes()).decode("ascii")
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
                {"type": "text", "text": prompt},
            ]}],
            "temperature": 0.1,
        }
        request = urllib.request.Request(
            self.cfg.base_url + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.cfg.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "rk3566-image-assistant/1.0",
            },
        )
        if not self.cfg.api_key:
            raise AssistantError("请在 config.yaml 中填写 DASHSCOPE_API_KEY")
        try:
            began = time.monotonic()
            print("正在请求百炼视觉模型…", file=sys.stderr, flush=True)
            with urlopen(request, timeout=self.timeout, prefer_ipv4=self.cfg.prefer_ipv4,
                         connect_timeout=self.cfg.http_connect_timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
            print(f"百炼视觉识别完成：{time.monotonic() - began:.1f} 秒", file=sys.stderr, flush=True)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:800]
            raise AssistantError(f"百炼视觉接口返回 HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise AssistantError(f"百炼视觉接口调用失败: {exc}") from exc
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AssistantError(f"百炼视觉响应缺少 message.content: {data!r}") from exc
        if isinstance(content, list):
            content = "".join(str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in content)
        return str(content).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path, help="本地图片路径（PNG/JPEG/GIF/BMP/WebP）")
    parser.add_argument("--config", default=str(Path(__file__).with_name("config.yaml")))
    parser.add_argument("--model", help="百炼视觉模型，默认读取 config.yaml 的 VISION_MODEL")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="附加分析要求")
    parser.add_argument("--json-out", type=Path, help="将结果另外保存为 JSON 文件")
    args = parser.parse_args()
    try:
        if not args.image.is_file():
            raise AssistantError(f"找不到图片文件: {args.image}")
        width, height = image_dimensions(args.image)
        cfg = Config.from_file(args.config)
        raw = extract_json(VisionClient(cfg, args.model or cfg.vision_model).describe(args.image, args.prompt))
        result = normalize_result(raw, width, height)
        rendered = json.dumps(result, ensure_ascii=False, indent=2)
        print(rendered)
        if args.json_out:
            args.json_out.write_text(rendered + "\n", encoding="utf-8")
        return 0
    except (AssistantError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
