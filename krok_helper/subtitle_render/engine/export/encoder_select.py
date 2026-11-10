"""Video encoder selection for subtitle MP4 export."""

from __future__ import annotations

import subprocess
from functools import lru_cache
from typing import Literal

EncoderMode = Literal[
    "cpu",
    "auto",
    "nvenc",
    "qsv",
    "amf_qvbr",
    "amf_cqp",
    "videotoolbox",
    "adreno_termuxva",
    "mail_termuxva",
]
VideoCodec = Literal["h264", "hevc"]

ENCODER_CPU = "cpu"
ENCODER_AUTO = "auto"
ENCODER_NVENC = "nvenc"
ENCODER_QSV = "qsv"
ENCODER_VIDEOTOOLBOX = "videotoolbox"
ENCODER_AMF_QVBR = "amf_qvbr"
ENCODER_AMF_CQP = "amf_cqp"
# 4.3.x 及更早版本保存的旧值。QVBR 仅在用户显式选择时生效（含 legacy
# 归一与 auto 解析），因此该旧值归一到 CQP。
ENCODER_AMF = "amf"
# Termux 上的 VAAPI 硬编：Adreno 走 kgsl 后端，Mali 走 llvmpipe 软件后端。
# 两者共用 h264_vaapi / hevc_vaapi，区别仅在环境变量与 -vaapi_device。
ENCODER_ADRENO_TERMUXVA = "adreno_termuxva"
ENCODER_MAIL_TERMUXVA = "mail_termuxva"
ENCODER_MODES: set[str] = {
    ENCODER_CPU,
    ENCODER_AUTO,
    ENCODER_NVENC,
    ENCODER_QSV,
    ENCODER_VIDEOTOOLBOX,
    ENCODER_AMF_QVBR,
    ENCODER_AMF_CQP,
    ENCODER_ADRENO_TERMUXVA,
    ENCODER_MAIL_TERMUXVA,
}

CODEC_H264 = "h264"
CODEC_HEVC = "hevc"
VIDEO_CODECS: set[str] = {CODEC_H264, CODEC_HEVC}
DEFAULT_VIDEO_BITRATE_MBPS = 10

# (codec, mode) → ffmpeg 编码器名。auto 解析成具体硬编模式后再查表。
_CODEC_ENCODER_NAMES: dict[str, dict[str, str]] = {
    CODEC_H264: {
        ENCODER_CPU: "libx264",
        ENCODER_NVENC: "h264_nvenc",
        ENCODER_QSV: "h264_qsv",
        ENCODER_VIDEOTOOLBOX: "h264_videotoolbox",
        ENCODER_AMF_QVBR: "h264_amf",
        ENCODER_AMF_CQP: "h264_amf",
        ENCODER_ADRENO_TERMUXVA: "h264_vaapi",
        ENCODER_MAIL_TERMUXVA: "h264_vaapi",
    },
    CODEC_HEVC: {
        ENCODER_CPU: "libx265",
        ENCODER_NVENC: "hevc_nvenc",
        ENCODER_QSV: "hevc_qsv",
        ENCODER_VIDEOTOOLBOX: "hevc_videotoolbox",
        ENCODER_AMF_QVBR: "hevc_amf",
        ENCODER_AMF_CQP: "hevc_amf",
        ENCODER_ADRENO_TERMUXVA: "hevc_vaapi",
        ENCODER_MAIL_TERMUXVA: "hevc_vaapi",
    },
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
    """Return a supported encoder mode, migrating legacy values, falling back to CPU."""
    if mode == ENCODER_AMF:
        return ENCODER_AMF_CQP
    return mode if mode in ENCODER_MODES else ENCODER_CPU


def normalize_video_codec(codec: str) -> str:
    """Return a supported video codec, falling back to H.264."""
    return codec if codec in VIDEO_CODECS else CODEC_H264


def normalize_cpu_preset(preset: str) -> str:
    """Return a supported x264/x265 preset, falling back to ``medium``."""
    return preset if preset in CPU_PRESETS else "medium"


def normalize_video_bitrate_mbps(value: object) -> int:
    """Restore a valid average bitrate, using the default for old preferences."""
    return value if isinstance(value, int) and 1 <= value <= 2000 else DEFAULT_VIDEO_BITRATE_MBPS


def is_termuxva_mode(mode: str) -> bool:
    """True for the Termux VAAPI modes (Adreno / Mali software)."""
    return normalize_encoder_mode(mode) in (
        ENCODER_ADRENO_TERMUXVA,
        ENCODER_MAIL_TERMUXVA,
    )


def resolve_encoder_mode(ffmpeg_path: str, mode: str, codec: str = CODEC_H264) -> str:
    """Resolve automatic selection using the available encoders for this codec."""
    selected = normalize_encoder_mode(mode)
    if selected == ENCODER_AUTO:
        return _best_available_hardware_encoder(ffmpeg_path, codec) or ENCODER_CPU
    return selected


def amf_qvbr_quality_level(crf: int) -> int:
    """Map UI quality to the inverse AMF QVBR scale (UI 18 -> QVBR 26)."""
    normalized_crf = max(0, min(51, int(crf)))
    if normalized_crf <= 18:
        # Preserve the highest-quality endpoint while anchoring the app's
        # visually-near-lossless default in AMF's practical QVBR range.
        return round(51 - normalized_crf * 25 / 18)
    return round(26 - (normalized_crf - 18) * 25 / 33)


def video_encoder_options(
    ffmpeg_path: str,
    mode: str,
    *,
    crf: int,
    preset: str,
    codec: str = CODEC_H264,
    bitrate_mbps: int = DEFAULT_VIDEO_BITRATE_MBPS,
) -> list[str]:
    """Build ffmpeg video encoder options for the selected mode/codec."""
    selected = resolve_encoder_mode(ffmpeg_path, mode, codec)
    codec = normalize_video_codec(codec)

    names = _CODEC_ENCODER_NAMES[codec]
    crf = max(0, min(51, int(crf)))
    # MP4 里 HEVC 打 hvc1 tag（默认 hev1 在 Apple 系播放器上不识别）
    hevc_tag = ["-tag:v", "hvc1"] if codec == CODEC_HEVC else []
    if selected == ENCODER_VIDEOTOOLBOX:
        return [
            "-c:v", names[ENCODER_VIDEOTOOLBOX], "-allow_sw", "0",
            "-b:v", str(normalize_video_bitrate_mbps(bitrate_mbps) * 1_000_000),
            *hevc_tag,
        ]
    if selected == ENCODER_NVENC:
        return ["-c:v", names[ENCODER_NVENC], "-preset", "p4", "-cq", str(crf), *hevc_tag]
    if selected == ENCODER_QSV:
        # QSV ICQ accepts 1-51. A zero value disables ICQ selection in FFmpeg,
        # so preserve the shared UI range while translating only this endpoint.
        return ["-c:v", names[ENCODER_QSV], "-global_quality", str(max(1, crf)), *hevc_tag]
    if selected in (ENCODER_ADRENO_TERMUXVA, ENCODER_MAIL_TERMUXVA):
        # VAAPI 走 CQP：-qp 是全局量化参数，与 QSV 一样把 0 钳到 1（部分驱动
        # 视 0 为「由码控决定」）。不传 -pix_fmt / -b:v——输入格式由滤镜图里的
        # format=nv12,hwupload 决定，由 export_command 负责拼接。
        return [
            "-c:v", names[selected],
            "-rc_mode", "CQP",
            "-qp", str(max(1, crf)),
            *hevc_tag,
        ]
    if selected == ENCODER_AMF_CQP:
        # CQP 与 N3 的 AMD/AMF 导出同构：质量值直通为固定量化参数，
        # 码率是编码结果而非输入，不传 -b:v/-maxrate。hevc_amf 无 B 帧，
        # 没有 -qp_b 选项。
        options = [
            "-c:v",
            names[ENCODER_AMF_CQP],
            "-quality",
            "balanced",
            "-rc",
            "cqp",
            "-qp_i",
            str(crf),
            "-qp_p",
            str(crf),
        ]
        if codec == CODEC_H264:
            options.extend(["-qp_b", str(crf)])
        return [*options, *hevc_tag]
    if selected == ENCODER_AMF_QVBR:
        return [
            "-c:v",
            names[ENCODER_AMF_QVBR],
            "-quality",
            "balanced",
            "-rc",
            "qvbr",
            "-qvbr_quality_level",
            str(amf_qvbr_quality_level(crf)),
            *hevc_tag,
        ]

    return [
        "-c:v",
        names[ENCODER_CPU],
        "-preset",
        normalize_cpu_preset(preset),
        "-crf",
        str(crf),
        *hevc_tag,
    ]


def resolved_encoder_label(ffmpeg_path: str, mode: str, codec: str = CODEC_H264) -> str:
    """Human-readable encoder label for status logs."""
    selected = resolve_encoder_mode(ffmpeg_path, mode, codec)
    codec = normalize_video_codec(codec)
    names = _CODEC_ENCODER_NAMES[codec]
    encoder_name = names.get(selected, names[ENCODER_CPU])
    base = {
        ENCODER_NVENC: "NVIDIA NVENC",
        ENCODER_QSV: "Intel QSV",
        ENCODER_VIDEOTOOLBOX: "Apple VideoToolbox",
        ENCODER_AMF_QVBR: "AMD AMF(QVBR)",
        ENCODER_AMF_CQP: "AMD AMF(CQP)",
        ENCODER_ADRENO_TERMUXVA: "Adreno TermuxVA",
        ENCODER_MAIL_TERMUXVA: "Mali TermuxVA",
    }.get(selected)
    if base is None:
        return f"CPU({encoder_name})"
    return f"{base}({encoder_name})"


def _best_available_hardware_encoder(ffmpeg_path: str, codec: str = CODEC_H264) -> str | None:
    encoders = _available_encoders(ffmpeg_path)
    names = _CODEC_ENCODER_NAMES[normalize_video_codec(codec)]
    # auto 模式解析到 A 卡时走 CQP；QVBR 必须用户显式选择。
    # Termux VAAPI 模式依赖 Termux 专有环境变量与设备名，auto 不自动落。
    for mode in (ENCODER_NVENC, ENCODER_QSV, ENCODER_AMF_CQP, ENCODER_VIDEOTOOLBOX):
        if names[mode] in encoders:
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
        if token.startswith(("h264_", "hevc_")) or token in ("libx264", "libx265")
    )