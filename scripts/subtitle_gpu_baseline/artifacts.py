"""Lossless, content-addressed reference artifacts and source/resource identity."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import platform
import os
import zlib

import numpy as np
from PyQt6.QtGui import QImage

SCHEMA = 1


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                    encoding="utf8", newline="\n")


def canonical_bytes(image: QImage) -> bytes:
    image = image.convertToFormat(QImage.Format.Format_RGBA8888_Premultiplied)
    source = image.constBits().asstring(image.sizeInBytes())
    rows = np.frombuffer(source, np.uint8).reshape(image.height(), image.bytesPerLine())
    return rows[:, :image.width() * 4].copy().tobytes()


def frame_metrics(data: bytes, width: int, height: int) -> dict:
    rgba = np.frombuffer(data, np.uint8).reshape(height, width, 4)
    alpha = rgba[:, :, 3]
    yy, xx = np.nonzero(alpha > 16)
    return {"pixel_sha256": digest(data), "width": width, "height": height,
        "pixel_format": "rgba8888_premultiplied", "stride": width * 4,
        "bbox_alpha_gt_16": [int(xx.min()), int(yy.min()), int(xx.max()) + 1, int(yy.max()) + 1] if len(xx) else None,
        "occupied_pixels": len(xx), "alpha_sum": int(alpha.sum(dtype=np.uint64))}


def save_frame(directory: Path, frame_id: str, image: QImage) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    data = canonical_bytes(image)
    raw = directory / f"{frame_id}.rgba.z"
    raw.write_bytes(zlib.compress(data))
    png = directory / f"{frame_id}.png"
    if not image.save(str(png)):
        raise RuntimeError(f"无法保存 GPU 基线图：{png}")
    return {**frame_metrics(data, image.width(), image.height()),
        "raw_file": raw.name, "raw_sha256": digest(raw.read_bytes()),
        "png_file": png.name, "png_sha256": digest(png.read_bytes())}


def pixel_difference(expected: bytes, actual: bytes, width: int, height: int) -> dict:
    a = np.frombuffer(expected, np.uint8).reshape(height, width, 4)
    b = np.frombuffer(actual, np.uint8).reshape(height, width, 4)
    delta = np.abs(a.astype(np.int16) - b.astype(np.int16))
    changed = np.any(delta, axis=2)
    yy, xx = np.nonzero(changed)
    return {"changed_pixels": int(changed.sum()), "max_channel_delta": int(delta.max()),
        "changed_channel_mae": float(delta[changed].mean()) if changed.any() else 0,
        "changed_bbox": [int(xx.min()), int(yy.min()), int(xx.max()) + 1, int(yy.max()) + 1] if len(xx) else None}


def source_identity(root: Path, renderer: Path | None) -> dict:
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=root).decode("utf8").strip()
    trees = ("native/subtitle_renderer", "krok_helper/subtitle_render")
    files = {}
    for tree in trees:
        for path in sorted((root / tree).rglob("*")):
            if path.suffix in (".py", ".h", ".cpp") or path.name == "CMakeLists.txt":
                files[path.relative_to(root).as_posix()] = digest(path.read_bytes())
    result = {"revision": git("rev-parse", "HEAD"), "worktree_status": git("status", "--porcelain"),
        "source_files": files, "source_tree_sha256": digest(json.dumps(files, sort_keys=True).encode()),
        "native_source_revision": git("log", "-1", "--format=%H", "--", "native/subtitle_renderer"),
        "platform": platform.platform(), "python": platform.python_version()}
    from PyQt6.QtCore import qVersion
    result["qt_runtime"] = qVersion()
    if os.name == "nt":
        try:
            query = subprocess.check_output(["powershell.exe", "-NoProfile", "-Command",
                "Get-CimInstance Win32_VideoController | Select-Object Name,DriverVersion,PNPDeviceID | ConvertTo-Json -Compress"],
                timeout=30)
            result["gpu_driver_inventory"] = json.loads(query.decode("utf8"))
        except (subprocess.SubprocessError, ValueError, UnicodeError) as exc:
            result["gpu_driver_inventory_error"] = str(exc)
    if renderer is not None:
        result["renderer_binary"] = {"path": str(renderer), "sha256": digest(renderer.read_bytes()),
            "size": renderer.stat().st_size, "mtime_ns": renderer.stat().st_mtime_ns,
            "source_revision_embedded": False}
        cache = renderer.parent / "CMakeCache.txt"
        if cache.is_file():
            selected = ("CMAKE_CXX_COMPILER:", "CMAKE_BUILD_TYPE:", "Qt6_DIR:", "KROK_EXPECTED_QT_VERSION:")
            result["cmake_cache"] = {"sha256": digest(cache.read_bytes()), "settings": [
                line for line in cache.read_text(encoding="utf8").splitlines() if line.startswith(selected)]}
        build_log = root / "build/subtitle-gpu-baseline-rebuild.log"
        if build_log.is_file():
            result["external_build_log_sha256"] = digest(build_log.read_bytes())
    return result


class ResourceStore:
    def __init__(self, root: Path):
        self.root = root
        self.records = {}
        self._stat_cache = {}

    def portable_ir(self, value):
        """Preserve semantic numbers; replace actual file resources by stored paths.

        Exact sent IR is stored separately, including volatile cache metadata.
        Only explicit file-change timestamps are omitted from portable identity.
        """
        if isinstance(value, list):
            return [self.portable_ir(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {}
        for key, item in value.items():
            if key.endswith(("_modified_ms", "_mtime_ns")):
                continue
            path_field = key.endswith(("_path", "_image")) or key == "resource_path"
            if path_field and isinstance(item, str) and item:
                path = Path(item)
                if path.is_file():
                    stat = path.stat()
                    token = (str(path.resolve()), stat.st_size, stat.st_mtime_ns)
                    if token not in self._stat_cache:
                        data = path.read_bytes()
                        sha = digest(data)
                        relative = f"resources/{sha}{path.suffix.lower()}"
                        destination = self.root / relative
                        destination.parent.mkdir(exist_ok=True)
                        if not destination.exists():
                            destination.write_bytes(data)
                        self.records[str(path)] = {"sha256": sha, "size": len(data), "stored_path": relative}
                        self._stat_cache[token] = relative
                    result[key] = self._stat_cache[token]
                    continue
            result[key] = self.portable_ir(item)
        return result


def semantic_summary(ir: dict) -> dict:
    sources = [ir.get("track", {}), *ir.get("extra_tracks", [])]
    lines = []
    kinds = set()
    for source_index, source in enumerate(sources):
        for index, line in enumerate(source.get("lines", [])):
            kinds.update(b["kind"] for b in line.get("fx_bursts", []))
            lines.append({"source_index": source_index, "line_index": index,
                **{key: line[key] for key in ("resolved_intervals", "display_start_ms", "display_end_ms",
                    "page_index", "lane", "layout_lane", "layout_id", "layout", "entry_anim", "exit_anim",
                    "karaoke_anim", "scanline", "wipe_reverse") if key in line}})
    return {"lines": lines, "particle_kinds": sorted(kinds),
        "line_layout_table": ir.get("line_layout_table", []),
        "geometry_scope": "resolved IR semantics and rendered alpha bbox; native per-glyph coordinates are not exposed"}
