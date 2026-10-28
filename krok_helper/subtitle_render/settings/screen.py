"""Qt-independent canvas and export screen settings."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any

from krok_helper.subtitle_render.domain.frame_rate import frame_rate


@dataclass(frozen=True)
class ScreenPreset:
    """Sayatoo-compatible screen preset metadata."""

    key: str
    label: str
    width: int
    height: int
    par: str = "1:1"


@dataclass(frozen=True)
class ScreenSettings:
    """Canvas/export screen settings persisted with projects and user settings."""

    preset_key: str = "hdtv_1080"
    par: str = "1:1"
    width: int = 1920
    height: int = 1080
    fps: int | float = 60
    resolution_mode: str = "source"
    fps_mode: str = "source"


SCREEN_FPS_OPTIONS = (60, 120)
SOURCE_SCREEN_MODES = ("source", "source_2x")
SCREEN_PRESETS: tuple[ScreenPreset, ...] = (
    ScreenPreset("hd_540", "HD 540", 960, 540),
    ScreenPreset("hdv_720", "HDV 720", 1280, 720),
    ScreenPreset("hdtv_720", "HDTV 720", 1280, 720),
    ScreenPreset("hdv_1080", "HDV 1080", 1440, 1080, "4:3"),
    ScreenPreset("hdtv_1080", "HDTV 1080", 1920, 1080),
    ScreenPreset("dvcprohd_720", "DVCPROHD 720", 960, 720, "4:3"),
    ScreenPreset("dvcprohd_1080", "DVCPROHD 1080", 1280, 1080, "3:2"),
    ScreenPreset("d1_dv_ntsc", "D1/DV NTSC", 720, 480, "10:11"),
    ScreenPreset("d1_dv_ntsc_wide", "D1/DV NTSC 宽屏", 720, 480, "40:33"),
    ScreenPreset("d1_dv_pal", "D1/DV PAL", 720, 576, "128:117"),
    ScreenPreset("d1_dv_pal_wide", "D1/DV PAL 宽屏", 720, 576, "512:351"),
    ScreenPreset("uhd_4k", "UHD 4K", 3840, 2160),
    ScreenPreset("uhd_8k", "UHD 8K", 7680, 4320),
    ScreenPreset("hd_540_vertical", "HD 540 竖屏", 540, 960),
    ScreenPreset("hd_720_vertical", "HD 720 竖屏", 720, 1280),
    ScreenPreset("hdtv_1080_vertical", "HDTV 1080 竖屏", 1080, 1920),
)

CANVAS_SIZE_PRESETS: tuple[tuple[str, str, int, int], ...] = (
    ("8k", "8K", 7680, 4320),
    ("4k", "4K", 3840, 2160),
    ("2k", "2K", 2560, 1440),
    ("1080p", "1080p", 1920, 1080),
    ("720p", "720p", 1280, 720),
    ("360p", "360p", 640, 360),
)
"""面向用户的常用画布格式下拉（key, 显示名, 宽, 高），与既有 Sayatoo preset 表无关。"""

CANVAS_SIZE_CUSTOM_KEY = "custom"

_CANVAS_SIZE_BY_KEY = {key: (width, height) for key, _label, width, height in CANVAS_SIZE_PRESETS}


def canvas_size_for_key(key: str) -> tuple[int, int] | None:
    """Return the (width, height) of a canvas size choice; None for custom/unknown."""
    return _CANVAS_SIZE_BY_KEY.get(key)


def match_canvas_size_key(width: int, height: int) -> str:
    """Return the choice key matching the given size, or the custom key."""
    for key, _label, preset_width, preset_height in CANVAS_SIZE_PRESETS:
        if preset_width == width and preset_height == height:
            return key
    return CANVAS_SIZE_CUSTOM_KEY


PAR_OPTIONS: tuple[tuple[str, str], ...] = (
    ("方形像素", "1:1"),
    ("HDV 1080 / DVCPROHD 720（4:3）", "4:3"),
    ("DVCPROHD 1080（3:2）", "3:2"),
    ("D1/DV NTSC（10:11）", "10:11"),
    ("D1/DV NTSC 宽屏（40:33）", "40:33"),
    ("D1/DV PAL（128:117）", "128:117"),
    ("D1/DV PAL 宽屏（512:351）", "512:351"),
)

_SCREEN_PRESET_BY_KEY = {preset.key: preset for preset in SCREEN_PRESETS}
_PAR_VALUES = {value for _label, value in PAR_OPTIONS}


def screen_settings_to_dict(settings: ScreenSettings) -> dict[str, Any]:
    return {
        "preset_key": settings.preset_key,
        "par": settings.par,
        "width": settings.width,
        "height": settings.height,
        "fps": settings.fps,
        "resolution_mode": settings.resolution_mode,
        "fps_mode": settings.fps_mode,
    }


def screen_settings_from_dict(payload: object) -> ScreenSettings:
    if not isinstance(payload, dict):
        return ScreenSettings()
    width = _int_setting(payload.get("width"), 160, 7680, ScreenSettings.width)
    height = _int_setting(payload.get("height"), 90, 4320, ScreenSettings.height)
    fps = _normalize_screen_fps(payload.get("fps"))
    par = str(payload.get("par") or ScreenSettings.par)
    if par not in _PAR_VALUES:
        par = ScreenSettings.par
    preset_key = str(payload.get("preset_key") or "")
    if preset_key not in _SCREEN_PRESET_BY_KEY and preset_key != "custom":
        preset_key = match_screen_preset_key(width, height, par)
    return ScreenSettings(
        preset_key=preset_key,
        par=par,
        width=width,
        height=height,
        fps=fps,
        resolution_mode=_screen_mode(payload.get("resolution_mode"), resolution=True),
        fps_mode=_screen_mode(payload.get("fps_mode")),
    )


def _int_setting(value: object, minimum: int, maximum: int, fallback: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return min(max(number, minimum), maximum)


def _normalize_screen_fps(value: object) -> int | float:
    try:
        fps = float(value)
    except (TypeError, ValueError):
        return ScreenSettings.fps
    if not isfinite(fps) or fps <= 0:
        return ScreenSettings.fps
    return int(fps) if fps.is_integer() else fps


def _screen_mode(value: object, *, resolution: bool = False) -> str:
    modes = {"custom", *SOURCE_SCREEN_MODES}
    modes.update(_CANVAS_SIZE_BY_KEY if resolution else {"fixed"})
    return str(value) if isinstance(value, str) and value in modes else "custom"


def resolve_source_screen(settings: ScreenSettings, video_info: Any) -> ScreenSettings:
    """Resolve the two independent source modes without changing fixed values."""
    from dataclasses import replace

    values = {}
    if settings.resolution_mode in SOURCE_SCREEN_MODES:
        width = int(getattr(video_info, "video_width", 0) or 0)
        height = int(getattr(video_info, "video_height", 0) or 0)
        if width <= 0 or height <= 0:
            raise ValueError("无法读取原视频分辨率，请选择自定义分辨率。")
        multiplier = 2 if settings.resolution_mode == "source_2x" else 1
        # Keep the existing even-dimension policy for yuv420p encoders.
        width, height = width * multiplier, height * multiplier
        width, height = width - width % 2, height - height % 2
        if not 160 <= width <= 7680 or not 90 <= height <= 4320:
            raise ValueError(f"输出分辨率 {width}×{height} 超出支持范围。")
        values.update(width=width, height=height)
    if settings.fps_mode in SOURCE_SCREEN_MODES:
        fps = getattr(video_info, "video_fps", None)
        if fps is None or not isfinite(fps) or fps <= 0:
            raise ValueError("无法读取原视频帧率，请选择固定或自定义帧率。")
        rate = frame_rate(fps) * (2 if settings.fps_mode == "source_2x" else 1)
        values["fps"] = rate.numerator if rate.denominator == 1 else float(rate)
    return replace(settings, **values)


def match_screen_preset_key(width: int, height: int, par: str) -> str:
    for preset in SCREEN_PRESETS:
        if preset.width == width and preset.height == height and preset.par == par:
            return preset.key
    return "custom"
