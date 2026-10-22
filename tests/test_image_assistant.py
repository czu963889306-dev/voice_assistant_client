import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
from image_assistant import extract_json, image_dimensions, normalize_result


class ImageAssistantTests(unittest.TestCase):
    def test_png_dimensions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + struct.pack(">II", 640, 480) + b"\x08\x02\x00\x00\x00")
            self.assertEqual(image_dimensions(path), (640, 480))

    def test_extract_fenced_json(self):
        value = extract_json("说明如下：```json\n{\"description\":\"一只猫\",\"objects\":[]}\n```\n")
        self.assertEqual(value["description"], "一只猫")

    def test_normalize_bbox_and_center(self):
        result = normalize_result({
            "description": "测试",
            "objects": [{"label": "猫", "bbox": [100, 200, 500, 800], "confidence": 0.91}],
        }, 640, 480)
        self.assertEqual(result["objects"][0]["bbox"], [64, 96, 320, 384])
        self.assertEqual(result["objects"][0]["center"], [192, 240])
        self.assertEqual(result["objects"][0]["confidence"], 0.91)


if __name__ == "__main__":
    unittest.main()
