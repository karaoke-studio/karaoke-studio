"""Qt-independent canvas and export screen settings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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
    fps: int = 60


SCREEN_FPS_OPTIONS = (60, 120)
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
    )


def _int_setting(value: object, minimum: int, maximum: int, fallback: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return min(max(number, minimum), maximum)


def _normalize_screen_fps(value: object) -> int:
    try:
        fps = int(value)
    except (TypeError, ValueError):
        return ScreenSettings.fps
    return fps if fps in SCREEN_FPS_OPTIONS else ScreenSettings.fps


def match_screen_preset_key(width: int, height: int, par: str) -> str:
    for preset in SCREEN_PRESETS:
        if preset.width == width and preset.height == height and preset.par == par:
            return preset.key
    return "custom"
