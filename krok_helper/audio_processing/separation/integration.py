"""Pinned PyMSS integration contract used by the managed backend."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess

PYMSS_VERSION = "2.0.18"
PYMSS_CORE_VERSION = "0.1.6"
PYMSS_RUNTIME_VERSION = "1"
PYMSS_PYTHON_VERSION = "3.12"
PYMSS_EMBEDDED_PYTHON_VERSION = "3.12.10"
PYMSS_EMBEDDED_PYTHON_SHA256 = (
    "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"
)
PYMSS_TORCH_VERSION = "2.7.1"
PYMSS_CUDA_VARIANT = "windows-cu128"
PYMSS_CPU_VARIANT = "windows-cpu"
CUDA_12_8_MIN_WINDOWS_DRIVER = (570, 65)

TORCH_WHEELS = {
    PYMSS_CPU_VARIANT: {
        "filename": "torch-2.7.1+cpu-cp312-cp312-win_amd64.whl",
        "url": "https://download-r2.pytorch.org/whl/cpu/torch-2.7.1%2Bcpu-cp312-cp312-win_amd64.whl",
        "size": 215_985_616,
        "sha256": "0bc887068772233f532b51a3e8c8cfc682ae62bef74bf4e0c53526c8b9e4138f",
    },
    PYMSS_CUDA_VARIANT: {
        "filename": "torch-2.7.1+cu128-cp312-cp312-win_amd64.whl",
        "url": "https://download-r2.pytorch.org/whl/cu128/torch-2.7.1%2Bcu128-cp312-cp312-win_amd64.whl",
        "size": 3_273_024_349,
        "sha256": "2bb8c05d48ba815b316879a18195d53a6472a03e297d971e916753f8e1053d30",
    },
}

RUNTIME_ASSET_PREFIX = "KaraokeStudio-PyMSS"
RUNTIME_RELEASE_REPOSITORY = "karaoke-studio/karaoke-studio-runtime"
RUNTIME_RELEASE_TAG = f"pymss-runtime-v{PYMSS_VERSION}-r{PYMSS_RUNTIME_VERSION}"
RUNTIME_RELEASE_BASE = (
    f"https://github.com/{RUNTIME_RELEASE_REPOSITORY}/releases/download/"
    f"{RUNTIME_RELEASE_TAG}"
)


def nvidia_driver_available() -> bool:
    """Conservatively detect a GPU driver validated for the CUDA 12.8 wheel."""
    executable = shutil.which("nvidia-smi")
    if not executable:
        return False
    try:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        result = subprocess.run(
            [
                executable,
                "--query-gpu=driver_version",
                "--format=csv,noheader",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            creationflags=creationflags,
            text=True,
        )
        if result.returncode != 0:
            return False
        versions = []
        for line in str(result.stdout or "").splitlines():
            fields = line.strip().split(".")
            if len(fields) < 2 or not fields[0].isdigit() or not fields[1].isdigit():
                continue
            versions.append((int(fields[0]), int(fields[1])))
        return bool(versions) and max(versions) >= CUDA_12_8_MIN_WINDOWS_DRIVER
    except (OSError, subprocess.SubprocessError):
        return False


def managed_runtime_variant(*, prefer_cuda: bool | None = None) -> str:
    """Return the supported managed-runtime variant for this host."""
    if platform.system().lower() != "windows":
        raise RuntimeError("首版 PyMSS 托管运行时仅支持 Windows。")
    override = os.environ.get("KROK_PYMSS_RUNTIME_VARIANT", "").strip()
    if override in {PYMSS_CUDA_VARIANT, PYMSS_CPU_VARIANT}:
        return override
    use_cuda = nvidia_driver_available() if prefer_cuda is None else prefer_cuda
    return PYMSS_CUDA_VARIANT if use_cuda else PYMSS_CPU_VARIANT


def runtime_manifest_url(variant: str) -> str:
    """Return the versioned package-manifest URL, allowing test mirrors."""
    override = os.environ.get("KROK_PYMSS_RUNTIME_MANIFEST_URL", "").strip()
    if override:
        return override
    name = f"{RUNTIME_ASSET_PREFIX}-{variant}-v{PYMSS_VERSION}-r{PYMSS_RUNTIME_VERSION}.json"
    return f"{RUNTIME_RELEASE_BASE}/{name}"


__all__ = [
    "PYMSS_VERSION",
    "PYMSS_CORE_VERSION",
    "PYMSS_RUNTIME_VERSION",
    "PYMSS_PYTHON_VERSION",
    "PYMSS_EMBEDDED_PYTHON_VERSION",
    "PYMSS_EMBEDDED_PYTHON_SHA256",
    "PYMSS_TORCH_VERSION",
    "PYMSS_CUDA_VARIANT",
    "PYMSS_CPU_VARIANT",
    "CUDA_12_8_MIN_WINDOWS_DRIVER",
    "TORCH_WHEELS",
    "RUNTIME_RELEASE_REPOSITORY",
    "managed_runtime_variant",
    "nvidia_driver_available",
    "runtime_manifest_url",
]
