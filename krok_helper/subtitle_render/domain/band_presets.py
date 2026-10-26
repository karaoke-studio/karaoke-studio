"""Built-in presets for the per-line background strip (行背景带).

The shipped assets live under ``assets/subtitle_render/band_presets``. Their
metadata is kept here so the property panel, the QPainter renderer and the native
IR serializer all read the same numbers.

静态预设是 PNG；动图预设（五线谱/星火/水泡）是**循环 animated WebP**——GIF 只有
1 bit 透明，水泡那层 alpha≈70 的淡填充、星火的柔光只能靠点阵近似，还会撞上
调色板/透明下标的老坑；WebP 是真 8 bit alpha，CPU 预览与 sidecar 都用
``QImageReader`` 解码，两边像素一致。用户导入的素材仍然可以是 GIF/PNG。
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
    # 独立谱号层（白色，运行时按 line_band_clef_color 上色）。
    clef_file_name: str = ""
    default_clef_color: str = ""
    # 静态变体（不带中段动态音符）：与主素材同尺寸、同端点；空 = 没有变体。
    static_file_name: str = ""
    # 预设自带的上色强度（100 = 满强度）。
    default_tint_strength_pct: int = 100
    # 预设默认填充方式：花纹类用 cover（裁切），避免平铺让中段变密。
    default_fill_mode: str = "three_slice"
    # 预设默认不透明度（五线谱/星火保持不透明，其余 50%）。
    default_opacity_pct: int = 50

    @property
    def path(self) -> Path:
        return BAND_PRESET_DIR / self.file_name

    @property
    def lines_path(self) -> Optional[Path]:
        if not self.lines_file_name:
            return None
        return BAND_PRESET_DIR / self.lines_file_name

    @property
    def clef_path(self) -> Optional[Path]:
        if not self.clef_file_name:
            return None
        return BAND_PRESET_DIR / self.clef_file_name

    @property
    def static_path(self) -> Optional[Path]:
        if not self.static_file_name:
            return None
        return BAND_PRESET_DIR / self.static_file_name

    @property
    def image_spec(self) -> str:
        """Value stored in ``Style.line_band_image`` for this preset."""
        return PRESET_SCHEME + self.preset_id


BAND_PRESETS: tuple[BandPreset, ...] = (
    BandPreset("staff_scroll", "五线谱 · 卷轴", "staff_scroll.webp",
               190, 160, 26, 26, 110, 120,
               lines_file_name="staff_scroll_lines.png",
               default_line_color="#FFFFFF",
               clef_file_name="staff_scroll_clef.png",
               default_clef_color="#5FD8E6",
               static_file_name="staff_scroll_static.png",
               default_opacity_pct=100),
    BandPreset("stars", "星火点阵", "stars.webp",
               120, 120, 14, 14, 54, 54, default_fill_mode="cover",
               default_opacity_pct=100),
    BandPreset("diagonal", "斜纹", "diagonal.png",
               96, 96, 10, 10, 42, 42, tintable=True, default_tint="#3EC6E0",
               default_tint_strength_pct=50, default_fill_mode="cover"),
    BandPreset("bubbles", "水泡", "bubbles.webp",
               160, 160, 18, 18, 78, 78, default_fill_mode="cover",
               default_opacity_pct=100),
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


def resolve_band_static_image(image: str) -> Optional[Path]:
    """静态变体路径（不带中段动态音符，如五线谱）；没有变体返回 ``None``。"""
    preset = band_preset(preset_id_for_image(image) or "")
    if preset is None:
        return None
    static_path = preset.static_path
    return static_path if static_path is not None and static_path.is_file() else None


def resolve_band_render_image(
    image: str, *, dynamic_notes_enabled: bool = True
) -> Optional[Path]:
    """渲染用素材：关闭「中间动态音符」时优先用预设的静态变体。"""
    if not dynamic_notes_enabled:
        static_path = resolve_band_static_image(image)
        if static_path is not None:
            return static_path
    return resolve_band_image(image)


def resolve_band_lines_image(image: str) -> Optional[Path]:
    """Resolve the preset's independent line layer (empty for uploads)."""
    preset = band_preset(preset_id_for_image(image) or "")
    if preset is None:
        return None
    lines_path = preset.lines_path
    return lines_path if lines_path is not None and lines_path.is_file() else None


def resolve_band_clef_image(image: str) -> Optional[Path]:
    """Resolve the preset's independent clef layer (empty for uploads)."""
    preset = band_preset(preset_id_for_image(image) or "")
    if preset is None:
        return None
    clef_path = preset.clef_path
    return clef_path if clef_path is not None and clef_path.is_file() else None


def default_clef_color_for(image: str) -> str:
    """Default clef colour for presets with a clef layer."""
    preset = band_preset(preset_id_for_image(image) or "")
    return preset.default_clef_color if preset is not None else ""


def default_tint_for(image: str) -> str:
    """Default user colour for a tintable preset (empty otherwise)."""
    preset = band_preset(preset_id_for_image(image) or "")
    return preset.default_tint if preset is not None else ""


def default_line_color_for(image: str) -> str:
    """Default staff-line colour for presets with a line layer."""
    preset = band_preset(preset_id_for_image(image) or "")
    return preset.default_line_color if preset is not None else ""


def preset_caps(image: str) -> tuple[int, int] | None:
    """预设素材的端点边界（源图像素）；非预设返回 ``None``。

    端点滑条不能把切割点挪进素材端点内部：中段切片从 ``cap_left`` 开始，
    一旦小于素材的端点宽度，中段就会把高音谱号/装饰再铺一遍——成片表现为
    「左侧的图出现两遍」。所以渲染前用这里的下界把端点夹住。
    """
    preset = band_preset(preset_id_for_image(image) or "")
    if preset is None:
        return None
    return max(int(preset.cap_left_px), 0), max(int(preset.cap_right_px), 0)


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
    "resolve_band_render_image",
    "resolve_band_static_image",
    "default_clef_color_for",
    "resolve_band_clef_image",
    "resolve_band_lines_image",
]
