"""Static layout, paint and element inputs; effects are kept in a separate module."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import get_args

from krok_helper.subtitle_render.domain.models import (
    GuideSymbol, LitStyle, LitAppearanceMode, VolumeAppearanceMode, PaintFill,
    RubyAnnotation, Style, TimingChar, TimingLine, TimingTrack,
)
from scripts.subtitle_render_corpus import (
    _base_style, _colors, _ordinary_scenario, _tactic_scenario,
    _heavy_scenario, _pattern_image, _real_dark_spiral_scenario,
)
from .model import BaselineCase


def representative_track() -> TimingTrack:
    return TimingTrack(
        lines=[TimingLine(
            chars=[TimingChar(text, 1200 + index * 550) for index, text in enumerate("星光舞う歌声")],
            end_ms=4500, display_start_override_ms=300, display_end_override_ms=5500,
        )],
        rubies=[RubyAnnotation(kanji="歌声", reading="うたごえ", pos_start_ms=3400, pos_end_ms=4500)],
    )


def representative_style() -> Style:
    return _base_style(font_size_px=90, dual_line_layout=False,
        line_horizontal_layout="center", line_y_position="center",
        entry_lead_ms=600, exit_fade_ms=600)


def build_cases(asset_dir: Path, *, include_real: bool = False) -> list[BaselineCase]:
    asset_dir.mkdir(parents=True, exist_ok=True)
    texture = asset_dir / "four-color.png"
    _pattern_image(texture)
    cases = []
    for original, features in (
        (_ordinary_scenario(), ("F01", "F02", "F04", "F06", "F08", "F09")),
        (_tactic_scenario(), ("F06", "F08", "F25")),
        (_heavy_scenario(texture), ("F07", "F22", "F25")),
    ):
        # Scene inputs only. Historical Painter thresholds are never copied.
        track = replace(original.track, meta=replace(original.track.meta, artist="GPU baseline"))
        cases.append(BaselineCase(original.name, features, track, original.style,
            original.timestamps_ms, original.extra_tracks))

    track, base = representative_track(), representative_style()
    variants = {
        "n3_grid": (("F02", "F03"), dict(layout_semantics="n3_1074")),
        "vertical": (("F05",), dict(vertical=True)),
        "rtl": (("F05", "F09"), dict(right_to_left=True)),
        "lower_ruby": (("F04",), dict(lower_ruby_enabled=True)),
        "latin_stretch": (("F01",), dict(latin_font_stretch_pct=140, font_weight=700, italic=True)),
        "shadow": (("F06", "F08"), dict(decoration_kind="shadow", shadow_offset_x=9, shadow_offset_y=6)),
        "no_edge2": (("F08",), dict(stroke2_enabled=False, stroke2_width_px=12)),
        "viewport": (("F25",), dict(viewport_rotation_deg=12, viewport_scale_pct=85, viewport_offset_x=30)),
        "lamp": (("F23",), dict(lit_enabled=True)),
        "volume": (("F23",), dict(volume_enabled=True)),
        "band": (("F24",), dict(line_band_enabled=True, line_band_image="preset:staff_scroll")),
    }
    for name, (features, changes) in variants.items():
        # Latin stretch needs actual Latin glyphs, not a misleading all-CJK case.
        source = track
        if name == "latin_stretch":
            source = replace(track, lines=[replace(track.lines[0], chars=[
                TimingChar(t, 1200 + i * 550) for i, t in enumerate("日Ab12語")])], rubies=[])
        cases.append(BaselineCase(name, features, source, replace(base, **changes), (600, 1500, 2600, 4400)))

    for mode in ("solid", "gradient_horizontal", "gradient_vertical", "split_vertical", "image"):
        fill = PaintFill(mode=mode, color="#E040A0", start_color="#FF2040", end_color="#20A0FF",
            gradient_stops=[(0, "#FF2040"), (100, "#20A0FF")],
            split_top_color="#FF2040", split_bottom_color="#20A0FF",
            image_path=str(texture.resolve()))
        colors = _colors("#FFFFFF", "#40A0FF")
        colors = replace(colors, before=replace(colors.before, text=fill), after=replace(colors.after, text=fill))
        cases.append(BaselineCase(f"paint_{mode}", ("F07",), track,
            replace(base, karaoke_colors=colors), (1500, 2600)))

    for shape in get_args(LitStyle):
        cases.append(BaselineCase(f"lamp_shape_{shape}", ("F23",), track,
            replace(base, lit_enabled=True, lit_style=shape, volume_enabled=shape == "volume",
                lit_image_path=str(texture.resolve())), (600, 1500, 2600)))
    for channel, annotation in (("lit", LitAppearanceMode), ("volume", VolumeAppearanceMode)):
        for mode in get_args(annotation):
            cases.append(BaselineCase(f"{channel}_appearance_{mode}", ("F23",), track,
                replace(base, **{f"{channel}_enabled": True, f"{channel}_appearance_mode": mode,
                    f"{channel}_role_name": "__global__"}), (600, 1500, 2600)))

    for glyph_kind in ("vector", "bitmap"):
        guide = GuideSymbol(kind=glyph_kind,
            path_commands=(("M", 0, -800), ("L", 800, -800), ("L", 400, 0), ("Z",)),
            bitmap_before_path=str(texture.resolve()), bitmap_after_path=str(texture.resolve()))
        guided = replace(track, lines=[replace(track.lines[0], guide_symbol=guide)])
        cases.append(BaselineCase(f"guide_{glyph_kind}", ("F21",), guided, base, (600, 1500, 2600)))

    checkpoint = TimingTrack(lines=[TimingLine(chars=[TimingChar("歌", 1200), TimingChar("声", 2800)],
        end_ms=4500, display_start_override_ms=300, display_end_override_ms=5500)],
        rubies=[RubyAnnotation(kanji="歌声", reading="うたごえ", reading_parts=["うた", "", "ごえ"],
            reading_part_ms=[2200, 2800], pos_start_ms=1200, pos_end_ms=4500)])
    cases.append(BaselineCase("ruby_pause_checkpoint", ("F04", "F09"), checkpoint, base, (1700, 2400, 3200)))

    graphemes = replace(track, lines=[replace(track.lines[0], chars=[
        TimingChar(text, 1200 + i * 550) for i, text in enumerate(("A\u0301", "e\u0308", "가", "한", "😀", "😄"))])], rubies=[])
    cases.append(BaselineCase("grapheme_fallback", ("F01", "F02"), graphemes, base, (1500, 2600, 4400)))
    variable = replace(track, lines=[replace(track.lines[0], chars=[
        TimingChar(text, 1200 + i * 550) for i, text in enumerate("日Ab12語")])], rubies=[])
    cases.append(BaselineCase("font_variable_latin", ("F01",), variable,
        replace(base, font_family_latin="Bahnschrift", latin_font_weight=700,
            latin_font_stretch_pct=140, latin_italic=True), (1500, 2600, 4400)))

    if include_real:
        original = _real_dark_spiral_scenario()
        if original is None:
            raise FileNotFoundError("本地真实 N3 样本不存在；不能将缺失样本计为通过。")
        cases.append(BaselineCase(original.name, ("F01", "F02", "F03", "F08"), original.track,
            original.style, original.timestamps_ms, original.extra_tracks, source=original.source))

    from .effects import effect_cases
    cases.extend(effect_cases(track, base, asset_dir))
    identifiers = [case.case_id for case in cases]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("GPU 基线场景 ID 重复。")
    return cases
