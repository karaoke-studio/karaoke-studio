"""Verify stored GPU evidence without rendering or altering acceptance results."""
from __future__ import annotations

import json
from pathlib import Path
import zlib

from .artifacts import SCHEMA, digest


def verify_baseline(root: Path) -> dict:
    root = root.resolve()

    def resource(relative: str) -> Path:
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError(f"基线文件不存在或超出参考目录：{relative}")
        return path

    manifest = json.loads(resource("manifest.json").read_text(encoding="utf8"))
    if manifest.get("schema") != SCHEMA:
        raise ValueError("不支持的 GPU 基线版本。")
    if manifest.get("status") not in ("passed", "failed"):
        raise ValueError("基线采集尚未完成。")
    cases = manifest["cases"]
    if not cases or len({case["id"] for case in cases}) != len(cases):
        raise ValueError("基线场景为空或 ID 重复。")

    def check_frame(directory, frame):
        raw = resource(f"{directory}/{frame['raw_file']}").read_bytes()
        png = resource(f"{directory}/{frame['png_file']}").read_bytes()
        if digest(raw) != frame["raw_sha256"] or digest(png) != frame["png_sha256"]:
            raise ValueError(f"参考图/像素文件损坏：{directory}")
        expected_size = frame["width"] * frame["height"] * 4
        decoder = zlib.decompressobj()
        pixels = decoder.decompress(raw, expected_size + 1)
        if len(pixels) != expected_size or not decoder.eof or decoder.unused_data:
            raise ValueError("参考像素尺寸/压缩流不一致。")
        if digest(pixels) != frame["pixel_sha256"]:
            raise ValueError("参考像素 SHA256 不一致。")

    count = difference_count = 0
    for record in manifest.get("resources", {}).values():
        if digest(resource(record["stored_path"]).read_bytes()) != record["sha256"]:
            raise ValueError("基线字体/素材内容指纹不一致。")
    for case in cases:
        directory = f"cases/{case['id']}"
        for required in ("sent-ir.json", "scene-ir.json", "semantics.json", "result.json"):
            json.loads(resource(f"{directory}/{required}").read_text(encoding="utf8"))
        if not case["frames"]:
            raise ValueError(f"场景没有参考帧：{case['id']}")
        for frame in case["frames"]:
            check_frame(directory, frame)
            count += 1
        for mismatch in case.get("seek_mismatches", []):
            if "evidence" not in mismatch:
                raise ValueError("跳转差异缺少对应证据帧。")
            check_frame(f"{directory}/seek-differences", mismatch["evidence"])
            difference_count += 1
    return {"scope": "artifact_integrity_only", "integrity": "passed",
        "capture_status": manifest["status"], "cases": len(cases), "frames": count,
        "resources": len(manifest.get("resources", {})),
        "seek_differences": difference_count}
