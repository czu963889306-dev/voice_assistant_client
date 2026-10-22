#!/usr/bin/env python3
"""Test the same MCP transport used by the voice assistant."""
import argparse
import json
from pathlib import Path
from mcp_client import VisionMCPClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(Path(__file__).with_name("config.yaml")))
    parser.add_argument("--image", help="images 目录内的图片名")
    parser.add_argument("--current", action="store_true", help="获取当前画面")
    parser.add_argument("--question", default="描述主要物体并输出像素框及中心点")
    args = parser.parse_args()
    try:
        with VisionMCPClient(args.config) as client:
            name = "analyze_image" if args.image else "inspect_current_view" if args.current else "vision_status"
            params = {"question": args.question} if args.image or args.current else {}
            if args.image:
                params["image_path"] = args.image
            result = client.call(name, params)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 1 if result.get("isError") else 0
    except (OSError, RuntimeError) as exc:
        print(f"MCP 错误：{exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
