"""Video encoder selection for subtitle MP4 export."""

from __future__ import annotations

import subprocess
from functools import lru_cache
from typing import Literal

EncoderMode = Literal["cpu", "auto", "nvenc", "qsv", "amf"]

ENCODER_CPU = "cpu"
ENCODER_AUTO = "auto"
ENCODER_NVENC = "nvenc"
ENCODER_QSV = "qsv"
ENCODER_AMF = "amf"
ENCODER_MODES: set[str] = {
    ENCODER_CPU,
    ENCODER_AUTO,
    ENCODER_NVENC,
    ENCODER_QSV,
    ENCODER_AMF,
}

CPU_PRESETS: tuple[str, ...] = (
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
    "slow",
    "slower",
    "veryslow",
)


def normalize_encoder_mode(mode: str) -> str:
    """Return a supported encoder mode, falling back to CPU."""
    return mode if mode in ENCODER_MODES else ENCODER_CPU


def normalize_cpu_preset(preset: str) -> str:
    """Return a supported x264 preset, falling back to ``veryfast``."""
    return preset if preset in CPU_PRESETS else "veryfast"


def video_encoder_options(
    ffmpeg_path: str,
    mode: str,
    *,
    crf: int,
    preset: str,
) -> list[str]:
    """Build ffmpeg video encoder options for the selected mode."""
    selected = normalize_encoder_mode(mode)
    if selected == ENCODER_AUTO:
        selected = _best_available_hardware_encoder(ffmpeg_path) or ENCODER_CPU

    crf = max(0, min(51, int(crf)))
    if selected == ENCODER_NVENC:
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", str(crf)]
    if selected == ENCODER_QSV:
        return ["-c:v", "h264_qsv", "-global_quality", str(crf)]
    if selected == ENCODER_AMF:
        return [
            "-c:v",
            "h264_amf",
            "-quality",
            "balanced",
            "-qp_i",
            str(crf),
            "-qp_p",
            str(crf),
            "-qp_b",
            str(crf),
        ]

    return ["-c:v", "libx264", "-preset", normalize_cpu_preset(preset), "-crf", str(crf)]


def resolved_encoder_label(ffmpeg_path: str, mode: str) -> str:
    """Human-readable encoder label for status logs."""
    selected = normalize_encoder_mode(mode)
    if selected == ENCODER_AUTO:
        selected = _best_available_hardware_encoder(ffmpeg_path) or ENCODER_CPU
    return {
        ENCODER_CPU: "CPU(libx264)",
        ENCODER_NVENC: "NVIDIA NVENC",
        ENCODER_QSV: "Intel QSV",
        ENCODER_AMF: "AMD AMF",
    }.get(selected, "CPU(libx264)")


def _best_available_hardware_encoder(ffmpeg_path: str) -> str | None:
    encoders = _available_encoders(ffmpeg_path)
    for mode, encoder_name in (
        (ENCODER_NVENC, "h264_nvenc"),
        (ENCODER_QSV, "h264_qsv"),
        (ENCODER_AMF, "h264_amf"),
    ):
        if encoder_name in encoders:
            return mode
    return None


@lru_cache(maxsize=8)
def _available_encoders(ffmpeg_path: str) -> frozenset[str]:
    try:
        completed = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-encoders"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except Exception:
        return frozenset()
    return frozenset(
        token
        for line in completed.stdout.splitlines()
        for token in line.split()
        if token.startswith("h264_") or token == "libx264"
    )
