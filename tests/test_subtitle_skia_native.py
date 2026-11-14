"""Opt-in native Skia capability checks; GPU and Skia Raster only.

Set KROK_SKIA_NATIVE_TEST_ROOT to build_skia_renderer.py's cache/native directory.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import numpy as np
import pytest


@pytest.fixture(scope="module")
def native():
    configured = os.environ.get("KROK_SKIA_NATIVE_TEST_ROOT")
    if not configured:
        pytest.skip("Set KROK_SKIA_NATIVE_TEST_ROOT after building the pinned Skia targets")
    root = Path(configured)
    system = {"win32": "windows", "linux": "linux", "darwin": "macos"}[sys.platform]
    arch = os.environ.get("KROK_SKIA_NATIVE_TEST_ARCH", "x64")
    api = "metal" if system == "macos" else "vulkan"
    suffix = ".exe" if system == "windows" else ""
    font = Path(os.environ.get("KROK_SKIA_NATIVE_TEST_FONT", "C:/Windows/Fonts/arial.ttf"))
    assert font.is_file()
    return {"gpu_dir": root / f"{system}-{arch}-{api}",
            "cpu_dir": root / f"{system}-{arch}-raster", "font": font,
            "api": api, "suffix": suffix}


def run(native, directory, target, prefix, *, font=None, args=(), missing_loader=False):
    env = os.environ.copy()
    if missing_loader:
        env["KROK_SKIA_VULKAN_LIBRARY"] = str(prefix.parent / "absent-vulkan-loader.dll")
    return subprocess.run([str(native[directory] / (target + native["suffix"])),
        str(font or native["font"]), str(prefix), *args], env=env, capture_output=True,
        text=True, encoding="utf-8", timeout=60)


@pytest.mark.parametrize("directory", ["gpu_dir", "cpu_dir"])
def test_raster_runs_with_vulkan_loader_unavailable(native, tmp_path, directory):
    prefix = tmp_path / directory
    result = run(native, directory, "krok_skia_raster_smoke", prefix, missing_loader=True)
    assert result.returncode == 0, result.stderr
    report = json.loads(prefix.with_suffix(".json").read_text())
    assert report["api"] == "raster" and report["hardware"] is False
    assert {"runtime_effect", "blur", "font", "glyph_outline", "png"} <= set(report["checks"])


def test_missing_vulkan_loader_is_gpu_failure_without_raster_fallback(native, tmp_path):
    if native["api"] != "vulkan":
        pytest.skip("Vulkan-specific loader failure injection")
    prefix = tmp_path / "missing"
    result = run(native, "gpu_dir", "krok_skia_gpu_smoke", prefix,
                 missing_loader=True, args=("--require-hardware",))
    assert result.returncode != 0
    assert "Vulkan loader unavailable" in result.stderr
    assert not prefix.with_suffix(".png").exists()


@pytest.mark.parametrize("directory,target", [
    ("cpu_dir", "krok_skia_raster_smoke"), ("gpu_dir", "krok_skia_gpu_smoke"),
])
def test_unicode_font_and_output_paths_and_repeated_frames(native, tmp_path, directory, target):
    font = tmp_path / "字幕 字体.ttf"
    shutil.copyfile(native["font"], font)
    prefix = tmp_path / "透明 字幕"
    args = ("--frames", "120") + (("--require-hardware",) if directory == "gpu_dir" else ())
    result = run(native, directory, target, prefix, font=font, args=args)
    assert result.returncode == 0, result.stderr
    report = json.loads(prefix.with_suffix(".json").read_text())
    assert report["frames"] == 120
    if directory == "gpu_dir":
        assert report["api"] == native["api"] and report["hardware"] is True
    rgba = np.fromfile(prefix.with_suffix(".rgba"), dtype=np.uint8).reshape(360, 640, 4)
    assert (rgba[0, 0] == 0).all()
    assert (rgba[:, :, :3] <= rgba[:, :, 3:]).all()


def test_gpu_and_cpu_builds_publish_the_same_renderer_source_identity(native):
    identities = []
    for key in ("gpu_dir", "cpu_dir"):
        record = json.loads((native[key] / "build-fingerprint.json").read_text())
        assert record["build_status"] == "passed"
        identities.append(record["native_identity"]["renderer_sources_sha256"])
    assert identities[0] == identities[1]


def test_same_core_gpu_and_raster_sample_interiors(native, tmp_path):
    images = {}
    for key, target in (("gpu_dir", "krok_skia_gpu_smoke"), ("cpu_dir", "krok_skia_raster_smoke")):
        prefix = tmp_path / key
        args = ("--require-hardware",) if key == "gpu_dir" else ()
        result = run(native, key, target, prefix, args=args)
        assert result.returncode == 0, result.stderr
        images[key] = np.fromfile(prefix.with_suffix(".rgba"), dtype=np.uint8).reshape(360, 640, 4)
    delta = np.abs(images["gpu_dir"].astype(int) - images["cpu_dir"].astype(int))
    # Interior checks have no AA ambiguity. Float-to-8-bit shader rounding allows one unit.
    assert delta[30:85, 30:300].max() <= 1
    assert delta[185:255, 475:545].max() == 0
    # Keep edge/filter differences visible; this capability sample is not subtitle parity approval.
    diagnostic = {"mean_absolute_rgba": float(delta.mean()), "max_absolute_rgba": int(delta.max()),
                  "different_pixels": int(np.any(delta, axis=2).sum())}
    (tmp_path / "skia-gpu-raster-difference.json").write_text(json.dumps(diagnostic, indent=2))
