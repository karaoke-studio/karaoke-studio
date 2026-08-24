"""Shared NicoKaraMaker3 ``LyricsFontModel`` visual conversion.

Both saved project snapshots and ``TemplateFont/*.tpl`` files use this module.
The caller supplies the size resolver because projects store current pixels,
while templates resolve ``SizeAndRatio`` against a target output height.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from krok_helper.subtitle_render.paint import (
    KaraokeColorState,
    KaraokeColors,
    PaintFill,
    _paint_fill,
)
from krok_helper.subtitle_render.models import (
    normalize_glow_concentration_level,
)
from krok_helper.subtitle_render.n3.font_fallback import resolve_n3_font_slots
from krok_helper.subtitle_render.n3.font_catalog import resolve_qt_font_family


SizeResolver = Callable[[object], int]

_FACE_WEIGHT_KEYWORDS: tuple[tuple[str, int], ...] = (
    ("extrabold", 800),
    ("ultrabold", 800),
    ("semibold", 600),
    ("demibold", 600),
    ("black", 900),
    ("heavy", 900),
    ("medium", 500),
    ("light", 300),
    ("thin", 100),
    ("bold", 700),
    ("negreta", 700),
    ("negrita", 700),
    ("grassetto", 700),
    ("fett", 700),
    ("gras", 700),
    ("太字", 700),
    ("ボールド", 700),
)


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list:
    return value if isinstance(value, list) else []


def _int(value: object, fallback: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback


def project_snapshot_size(value: object) -> int:
    """Read the already-updated pixel value stored in an N3 project."""
    return _int(_dict(value).get("Size"), 0)


def hex_from_dxcolor(value: object, fallback: str) -> str:
    color = _dict(value)
    if not color:
        return fallback

    def channel(key: str, default: float = 0.0) -> int:
        try:
            number = float(color.get(key, default))
        except (TypeError, ValueError):
            number = default
        return max(0, min(255, round(number * 255)))

    alpha = channel("A", 1.0)
    rgb = "%02X%02X%02X" % (channel("R"), channel("G"), channel("B"))
    return f"#{alpha:02X}{rgb}" if alpha < 255 else f"#{rgb}"


def hex_from_colorbind(value: object, fallback: str = "#FFFFFF") -> str:
    """Convert N3 ``ColorBindModel`` to ``#RRGGBB`` or ``#AARRGGBB``."""
    bind = _dict(value)
    if "DxColor" in bind:
        return hex_from_dxcolor(bind.get("DxColor"), fallback)
    if {"R", "G", "B"} <= bind.keys():
        return hex_from_dxcolor(bind, fallback)
    web = str(bind.get("Web16") or "").strip()
    if len(web) == 6 and all(char in "0123456789abcdefABCDEF" for char in web):
        return f"#{web.upper()}"
    return fallback


def _gradient_stop_list(brush: dict) -> list[tuple[float, str]]:
    stops: list[tuple[float, str]] = []
    for stop in _list(brush.get("GradientStops")):
        stop = _dict(stop)
        try:
            position = float(stop.get("Position", 0.0))
        except (TypeError, ValueError):
            position = 0.0
        pct = round(max(0.0, min(100.0, position * 100.0)), 9)
        if pct.is_integer():
            pct = int(pct)
        stops.append((pct, hex_from_dxcolor(stop.get("Color"), "#FFFFFF")))
    stops.sort(key=lambda item: item[0])
    return stops


def _mille_feuille_band_stops(
    stops: list[tuple[float, str]],
) -> list[tuple[float, str]]:
    """Convert N3's duplicated gradient stops to exact hard color bands."""
    if not stops:
        return []
    if len(stops) == 1:
        return [(0, stops[0][1]), (100, stops[0][1])]
    result = list(stops[:-1])
    if result[0][0] > 0:
        result.insert(0, (0, result[0][1]))
    last_position, last_color = result[-1]
    if last_position < 100:
        result.append((100, last_color))
    return result


def _fill_from_brush(
    brush: object,
    lyrics_dir: Path,
    warnings: list[str],
    context: str,
) -> PaintFill:
    brush = _dict(brush)
    solid = hex_from_colorbind(brush.get("SolidColor"))
    brush_type = _int(brush.get("SelectedBrushTypeIndex"), 0)
    if brush_type == 0:
        return _paint_fill(solid)
    if brush_type in (1, 2):
        stops = _gradient_stop_list(brush) or [(0, solid), (100, solid)]
        if brush_type == 2:
            bands = _mille_feuille_band_stops(stops)
            start_color = bands[0][1]
            end_color = bands[-1][1]
            interior = [position for position, _color in bands if position not in {0, 100}]
            return PaintFill(
                mode="split_vertical",
                color=start_color,
                start_color=start_color,
                end_color=end_color,
                gradient_stops=stops,
                split_top_color=start_color,
                split_bottom_color=bands[-2][1] if len(bands) > 1 else end_color,
                split_position_pct=interior[0] if interior else 50,
                split_stops=bands,
            )
        start_color = stops[0][1]
        end_color = stops[-1][1]
        return PaintFill(
            mode="gradient_vertical",
            color=start_color,
            start_color=start_color,
            end_color=end_color,
            gradient_stops=stops,
            split_top_color=start_color,
            split_bottom_color=end_color,
        )
    if brush_type == 3:
        image = str(brush.get("BitmapPath") or "").strip()
        candidate = Path(image) if image else None
        if candidate is not None and not candidate.is_absolute():
            candidate = lyrics_dir / image
        scale = _int(brush.get("BitmapScale"), 100)
        if scale == 0:
            scale = 100
        scale = max(1, min(scale, 1000))
        if candidate is None or not candidate.is_file():
            warnings.append(
                f"{context}：贴图填充素材不存在（{image or '未设置'}），"
                "已保留图片设置并暂用底色显示"
            )
        fill = _paint_fill(solid)
        fill.mode = "image"
        fill.image_path = str(candidate) if candidate is not None else image
        fill.image_scale_pct = scale
        return fill
    warnings.append(f"{context}：未知笔刷类型（{brush_type}），已回退纯色")
    return _paint_fill(solid)


def _karaoke_colors_from_brushes(
    brushes: list,
    lyrics_dir: Path,
    warnings: list[str],
    context: str,
) -> KaraokeColors:
    fills: list[PaintFill] = []
    for index in range(8):
        brush = brushes[index] if index < len(brushes) else None
        name = str(_dict(brush).get("SettingsName") or f"笔刷{index}")
        fills.append(_fill_from_brush(brush, lyrics_dir, warnings, f"{context}·{name}"))
    return KaraokeColors(
        before=KaraokeColorState(
            text=fills[4], stroke=fills[5], stroke2=fills[6], shadow=fills[7]
        ),
        after=KaraokeColorState(
            text=fills[0], stroke=fills[1], stroke2=fills[2], shadow=fills[3]
        ),
    )


def _face_weight_from_name(face_name: str) -> int:
    if not face_name:
        return 400
    lowered = face_name.lower()
    for keyword, value in _FACE_WEIGHT_KEYWORDS:
        if keyword in lowered:
            return value
    return 400


def _font_face_weight(family: str, face_name: str) -> int:
    if not face_name:
        return 400
    try:
        from PyQt6.QtGui import QFontDatabase, QGuiApplication

        if QGuiApplication.instance() is not None:
            qt_family = resolve_qt_font_family(family)
            for style_name in QFontDatabase.styles(qt_family):
                if style_name.casefold() == face_name.casefold():
                    return int(QFontDatabase.weight(qt_family, style_name))
    except (ImportError, RuntimeError, TypeError, ValueError):
        pass
    return _face_weight_from_name(face_name)


def _n3_default_font_family() -> str:
    candidates = ("HGP明朝E", "游明朝", "ＭＳ Ｐ明朝")
    try:
        from PyQt6.QtGui import QGuiApplication

        if QGuiApplication.instance() is not None:
            from krok_helper.subtitle_render.n3.font_catalog import (
                installed_qt_font_families,
            )

            installed = {name.casefold() for name in installed_qt_font_families()}
            for candidate in candidates:
                if candidate.casefold() in installed:
                    return candidate
    except (ImportError, RuntimeError):
        pass
    return "游明朝"


def convert_n3_font_scheme(
    font: dict,
    lyrics_dir: Path,
    warnings: list[str],
    context: str,
    *,
    size_resolver: SizeResolver = project_snapshot_size,
    preserve_inheritance: bool = False,
) -> dict[str, Any]:
    """Convert one complete N3 font/color scheme to shared style fields.

    The Japanese lyrics slot is materialized because it is the root slot.  When
    ``preserve_inheritance`` is true, child slots keep N3's empty/zero values as
    ``None`` so the property panel can show the inheritance state instead of
    pretending the resolved fallback was saved.  Standalone/custom schemes keep
    materialized values because their ``None`` fields otherwise inherit another
    scheme rather than their own Japanese root.
    """
    font_infos = _list(font.get("FontInfos"))
    slots = resolve_n3_font_slots(
        font_infos,
        size_resolver=size_resolver,
        default_family=_n3_default_font_family(),
        face_weight_resolver=_font_face_weight,
    )
    kanji, alnum, ruby_kanji, ruby_alnum = slots[0], slots[2], slots[3], slots[5]

    def raw_info(index: int) -> dict:
        return _dict(font_infos[index]) if index < len(font_infos) else {}

    def raw_family(index: int) -> str | None:
        value = str(raw_info(index).get("FontName") or "").strip()
        return value or None

    def raw_weight(index: int) -> int | None:
        family = raw_family(index)
        if family is None:
            return None
        face = str(raw_info(index).get("FontFaceName") or "").strip()
        return _font_face_weight(family, face)

    def raw_size(index: int, key: str) -> int | None:
        value = int(size_resolver(raw_info(index).get(key)))
        return value if value > 0 else None

    def raw_edge2(index: int) -> bool | None:
        value = raw_info(index).get("UseEdge2")
        return value if isinstance(value, bool) else None

    alnum_family = raw_family(2)
    ruby_family = raw_family(3)
    ruby_alnum_family = raw_family(5)
    changes: dict[str, Any] = {
        "font_family": kanji.family,
        "font_family_latin": alnum_family if preserve_inheritance else alnum.family,
        "font_size_px": kanji.char_size,
        "font_weight": kanji.weight,
        "italic": False,
        "stroke_width_px": kanji.edge_size,
        "stroke2_enabled": kanji.use_edge2,
        "stroke2_width_px": kanji.edge2_size,
        "latin_font_size_px": (
            raw_size(2, "CharSize") if preserve_inheritance else alnum.char_size
        ),
        "latin_font_weight": raw_weight(2) if preserve_inheritance else alnum.weight,
        "latin_stroke_width_px": (
            raw_size(2, "EdgeSize") if preserve_inheritance else alnum.edge_size
        ),
        "latin_stroke2_enabled": (
            raw_edge2(2) if preserve_inheritance else alnum.use_edge2
        ),
        "latin_stroke2_width_px": (
            raw_size(2, "EdgeSize2") if preserve_inheritance else alnum.edge2_size
        ),
    }
    colors = _karaoke_colors_from_brushes(
        _list(font.get("BrushInfos")), lyrics_dir, warnings, context
    )
    changes.update(
        karaoke_colors=colors,
        base_color=colors.before.text.color,
        fill_color=colors.after.text.color,
        stroke_color=colors.after.stroke.color,
        shadow_color=colors.after.shadow.color,
    )
    decor_kind = _int(font.get("DecorKind"), 0)
    decor_size = size_resolver(font.get("DecorSize"))
    if decor_kind == 2:
        concentration = normalize_glow_concentration_level(font.get("BlurLevel"))
        changes.update(
            decoration_kind="glow",
            glow_radius_px=decor_size,
            glow_before_radius_px=decor_size,
            glow_after_radius_px=decor_size,
            glow_concentration_level=concentration,
        )
    elif decor_kind == 1:
        changes.update(
            decoration_kind="shadow",
            shadow_offset_x=decor_size,
            shadow_offset_y=decor_size,
        )
    else:
        changes.update(decoration_kind="none", shadow_offset_x=0, shadow_offset_y=0)

    changes.update(
        ruby_font_follow_main=(
            ruby_family is None and raw_weight(3) is None
            if preserve_inheritance
            else False
        ),
        ruby_font_family=ruby_family if preserve_inheritance else ruby_kanji.family,
        ruby_font_family_latin=(
            ruby_alnum_family if preserve_inheritance else ruby_alnum.family
        ),
        ruby_font_weight=raw_weight(3) if preserve_inheritance else ruby_kanji.weight,
        ruby_font_size_px=raw_size(3, "CharSize") or ruby_kanji.char_size,
        ruby_stroke_width_px=(
            raw_size(3, "EdgeSize") if preserve_inheritance else ruby_kanji.edge_size
        ),
        ruby_stroke2_enabled=(
            raw_edge2(3) if preserve_inheritance else ruby_kanji.use_edge2
        ),
        ruby_stroke2_width_px=(
            raw_size(3, "EdgeSize2") if preserve_inheritance else ruby_kanji.edge2_size
        ),
        ruby_latin_font_size_px=(
            raw_size(5, "CharSize") if preserve_inheritance else ruby_alnum.char_size
        ),
        ruby_latin_font_weight=(
            raw_weight(5) if preserve_inheritance else ruby_alnum.weight
        ),
        ruby_latin_stroke_width_px=(
            raw_size(5, "EdgeSize") if preserve_inheritance else ruby_alnum.edge_size
        ),
        ruby_latin_stroke2_enabled=(
            raw_edge2(5) if preserve_inheritance else ruby_alnum.use_edge2
        ),
        ruby_latin_stroke2_width_px=(
            raw_size(5, "EdgeSize2")
            if preserve_inheritance
            else ruby_alnum.edge2_size
        ),
        ruby_color=colors.after.text.color,
        ruby_colors_follow_main=True,
        ruby_karaoke_colors=deepcopy(colors),
    )
    return changes
