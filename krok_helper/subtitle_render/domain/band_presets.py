"""Built-in presets for the per-line background strip (行背景带).

The five shipped PNGs live under ``assets/subtitle_render/band_presets``.
Their metadata is kept here so the property panel, the QPainter renderer and
the native IR serializer all read the same numbers.

2026-10-23：素材改为**宽幅不循环**版本——中段 4096px（斜纹 4080px，保证条纹
周期整除），16:9 画布上正常歌词行不会触发第二次平铺。五线谱的 5 条谱线单独
出 ``staff_scroll_lines.png``（纯白），运行时按用户颜色上色。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

BAND_PRESET_DIR = (
    Path(__file__).resolve().parents[2] / "assets" / "subtitle_render" / "band_presets"
)

PRESET_SCHEME = "preset:"


@dataclass(frozen=True)
class BandPreset:
    """One shipped preset: slicing geometry plus recommended padding."""

    preset_id: str
    label: str
    file_name: str
    cap_left_px: int
    cap_right_px: int
    pad_top_px: int
    pad_bottom_px: int
    pad_left_px: int
    pad_right_px: int
    tintable: bool = False
    default_tint: str = ""
    # 独立谱线层（白色，运行时按 line_band_line_color 上色）；空 = 没有分层。
    lines_file_name: str = ""
    default_line_color: str = ""

    @property
    def path(self) -> Path:
        return BAND_PRESET_DIR / self.file_name

    @property
    def lines_path(self) -> Optional[Path]:
        if not self.lines_file_name:
            return None
        return BAND_PRESET_DIR / self.lines_file_name

    @property
    def image_spec(self) -> str:
        """Value stored in ``Style.line_band_image`` for this preset."""
        return PRESET_SCHEME + self.preset_id


BAND_PRESETS: tuple[BandPreset, ...] = (
    BandPreset("staff_scroll", "五线谱 · 卷轴", "staff_scroll.png",
               190, 160, 26, 26, 110, 120,
               lines_file_name="staff_scroll_lines.png",
               default_line_color="#1A1A1C"),
    BandPreset("stars", "星火点阵", "stars.png",
               120, 120, 14, 14, 54, 54),
    BandPreset("diagonal", "斜纹缎带", "diagonal.png",
               96, 96, 10, 10, 42, 42, tintable=True, default_tint="#3EC6E0"),
    BandPreset("bubbles", "水泡", "bubbles.png",
               160, 160, 18, 18, 78, 78),
    BandPreset("plain_round", "纯色圆角条", "plain_round.png",
               64, 64, 12, 12, 30, 30, tintable=True, default_tint="#2B3A55"),
)

_BY_ID = {preset.preset_id: preset for preset in BAND_PRESETS}


def band_preset(preset_id: str) -> Optional[BandPreset]:
    return _BY_ID.get(str(preset_id))


def preset_id_for_image(image: str) -> Optional[str]:
    """Return the preset id when ``image`` uses the ``preset:`` scheme."""
    if image and image.startswith(PRESET_SCHEME):
        return image[len(PRESET_SCHEME):]
    return None


def resolve_band_image(image: str) -> Optional[Path]:
    """Resolve ``Style.line_band_image`` into an existing file path."""
    if not image:
        return None
    preset = band_preset(preset_id_for_image(image) or "")
    if preset is not None:
        return preset.path if preset.path.is_file() else None
    path = Path(image)
    return path if path.is_file() else None


def resolve_band_lines_image(image: str) -> Optional[Path]:
    """Resolve the preset's independent line layer (empty for uploads)."""
    preset = band_preset(preset_id_for_image(image) or "")
    if preset is None:
        return None
    lines_path = preset.lines_path
    return lines_path if lines_path is not None and lines_path.is_file() else None


def default_tint_for(image: str) -> str:
    """Default user colour for a tintable preset (empty otherwise)."""
    preset = band_preset(preset_id_for_image(image) or "")
    return preset.default_tint if preset is not None else ""


def default_line_color_for(image: str) -> str:
    """Default staff-line colour for presets with a line layer."""
    preset = band_preset(preset_id_for_image(image) or "")
    return preset.default_line_color if preset is not None else ""


__all__ = [
    "BAND_PRESETS",
    "BAND_PRESET_DIR",
    "BandPreset",
    "PRESET_SCHEME",
    "band_preset",
    "default_line_color_for",
    "default_tint_for",
    "preset_id_for_image",
    "resolve_band_image",
    "resolve_band_lines_image",
]
