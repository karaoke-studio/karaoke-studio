from __future__ import annotations

from dataclasses import replace

from PyQt6.QtGui import QImage

from krok_helper.subtitle_render.engine import painter as subtitle_painter
from krok_helper.subtitle_render.engine.painter import _layout_line_uncached, paint_frame
from krok_helper.subtitle_render.engine.timeline import compute_display_lines, find_active_line
from krok_helper.subtitle_render.frontend.lyrics_list import _CharRoleDialog
from krok_helper.subtitle_render.frontend.lyrics_list import COL_CONTENT, LyricsPanel
from krok_helper.subtitle_render.frontend.main_window import _GuideSymbolSettingsDialog
from krok_helper.subtitle_render.guide_symbols import (
    guide_symbol_path,
    import_svg_guide_symbol,
)
from krok_helper.subtitle_render.models import (
    GuideSymbol,
    Style,
    TimingChar,
    TimingLine,
    TimingTrack,
    guide_symbol_from_dict,
    guide_symbol_role_labels,
    guide_symbol_to_dict,
    guide_symbol_with_role_labels,
)
from krok_helper.subtitle_render.project_store import project_payload


def _symbol(tmp_path, *, duration_ms: int = 1000, count: int = 1) -> GuideSymbol:
    path = tmp_path / "lead.svg"
    path.write_text(
        '<svg viewBox="0 0 100 100">'
        '<g transform="translate(5 0)">'
        '<path d="M5 50 L85 10 L65 50 L85 90 Z"/>'
        "</g></svg>",
        encoding="utf-8",
    )
    return import_svg_guide_symbol(path, duration_ms=duration_ms, count=count)


def test_svg_import_becomes_embedded_glyph_outline(tmp_path):
    symbol = _symbol(tmp_path)
    bounds = guide_symbol_path(symbol).boundingRect()

    assert symbol.name == "lead"
    assert symbol.duration_ms == 1000
    assert symbol.path_commands
    assert 0 < bounds.width() <= symbol.units_per_em
    assert 0 < bounds.height() <= symbol.units_per_em

    restored = guide_symbol_from_dict(guide_symbol_to_dict(symbol))
    assert restored == symbol


def test_guide_symbol_cache_signature_reuses_frozen_outline_model(tmp_path):
    symbol = _symbol(tmp_path)

    signature = subtitle_painter._value_signature(symbol)

    assert signature is symbol


def test_guide_symbol_is_first_timed_inline_glyph(tmp_path):
    symbol = _symbol(tmp_path)
    line = TimingLine(
        chars=[TimingChar("歌", 2000), TimingChar("詞", 2500)],
        end_ms=3000,
        guide_symbol=symbol,
    )
    track = TimingTrack(lines=[line])
    style = Style(font_family="Arial", font_size_px=72)

    layout = _layout_line_uncached(track, line, style, 800, 450)

    assert layout is not None
    assert layout.render_line is not None
    assert len(layout.render_line.chars) == 3
    assert layout.render_line.chars[0].vector_glyph == symbol
    assert layout.intervals == [(1000, 2000), (2000, 2500), (2500, 3000)]
    assert layout.text_layout.glyphs[0].left == layout.x0
    assert layout.char_x_ranges[0][1] == layout.char_x_ranges[1][0]
    assert layout.total_w > sum(layout.char_widths[1:])
    assert find_active_line(track, 1500) is line
    display = compute_display_lines(
        track,
        lead_in_ms=0,
        tail_ms=0,
        lane_gap_ms=0,
        max_hold_ms=0,
        continuity_snap_ms=0,
    )
    assert display[0].display_start_ms == 1000


def test_multiple_guides_are_independent_evenly_timed_inline_glyphs(tmp_path):
    symbol = _symbol(tmp_path, duration_ms=1000, count=3)
    line = TimingLine(
        chars=[TimingChar("歌", 5000), TimingChar("詞", 5500)],
        end_ms=6000,
        guide_symbol=symbol,
    )
    track = TimingTrack(lines=[line])

    layout = _layout_line_uncached(
        track, line, Style(font_family="Arial", font_size_px=72), 900, 450
    )

    assert layout is not None and layout.render_line is not None
    assert len(layout.render_line.chars) == 5
    assert [char.start_ms for char in layout.render_line.chars] == [
        2000,
        3000,
        4000,
        5000,
        5500,
    ]
    assert layout.intervals[:3] == [(2000, 3000), (3000, 4000), (4000, 5000)]
    assert all(char.vector_glyph == symbol for char in layout.render_line.chars[:3])
    assert find_active_line(track, 2500) is line


def test_multiple_guide_line_hits_layout_and_layer_caches(tmp_path):
    symbol = _symbol(tmp_path, duration_ms=1000, count=3)
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("歌", 5000), TimingChar("詞", 5500)],
                end_ms=6000,
                guide_symbol=symbol,
            )
        ]
    )
    style = Style(font_family="Arial", font_size_px=72)
    image = QImage(900, 450, QImage.Format.Format_ARGB32_Premultiplied)
    subtitle_painter.clear_before_layer_cache()

    paint_frame(image, track, 2500, style)
    warm_sizes = (
        len(subtitle_painter._LINE_LAYOUT_CACHE),
        len(subtitle_painter._TEXT_RUN_LAYER_CACHE),
    )
    paint_frame(image, track, 2600, style)

    assert warm_sizes[0] == 1
    assert warm_sizes[1] > 0
    assert (
        len(subtitle_painter._LINE_LAYOUT_CACHE),
        len(subtitle_painter._TEXT_RUN_LAYER_CACHE),
    ) == warm_sizes
    subtitle_painter.clear_before_layer_cache()


def test_multiple_guides_keep_independent_role_labels_in_project(tmp_path):
    symbol = guide_symbol_with_role_labels(
        _symbol(tmp_path, count=3), ["角色A", "角色B", None]
    )

    restored = guide_symbol_from_dict(guide_symbol_to_dict(symbol))

    assert restored == symbol
    assert guide_symbol_role_labels(restored) == ("角色A", "角色B", None)
    assert TimingTrack(
        lines=[TimingLine(chars=[TimingChar("歌", 5000)], guide_symbol=restored)]
    ).role_options == ["角色A", "角色B"]


def test_legacy_guide_symbol_payload_defaults_to_one_glyph(tmp_path):
    legacy = guide_symbol_to_dict(_symbol(tmp_path))
    assert legacy is not None
    legacy.pop("count")
    legacy.pop("role_labels")
    legacy["role_label"] = "角色A"

    restored = guide_symbol_from_dict(legacy)

    assert restored is not None
    assert restored.count == 1
    assert guide_symbol_role_labels(restored) == ("角色A",)


def test_guide_symbol_renders_during_its_own_karaoke_interval(tmp_path):
    symbol = _symbol(tmp_path)
    line = TimingLine(
        chars=[TimingChar("歌", 2000), TimingChar("詞", 2500)],
        end_ms=3000,
        guide_symbol=symbol,
    )
    image = QImage(800, 450, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)

    paint_frame(
        image,
        TimingTrack(lines=[line]),
        1500,
        Style(font_family="Arial", font_size_px=72),
    )

    assert any(image.constBits().asstring(image.sizeInBytes()))


def test_guide_symbol_renders_in_direct_and_vertical_paths(tmp_path, monkeypatch):
    symbol = _symbol(tmp_path)
    line = TimingLine(
        chars=[TimingChar("歌", 2000), TimingChar("詞", 2500)],
        end_ms=3000,
        guide_symbol=symbol,
    )
    track = TimingTrack(lines=[line])
    for vertical in (False, True):
        monkeypatch.setenv("KROK_SUBTITLE_HORIZONTAL_LAYER", "0")
        image = QImage(800, 450, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0)
        paint_frame(
            image,
            track,
            1500,
            Style(font_family="Arial", font_size_px=72, vertical=vertical),
        )
        assert any(image.constBits().asstring(image.sizeInBytes()))


def test_char_role_dialog_exposes_guide_as_first_selectable_character(tmp_path):
    symbol = _symbol(tmp_path)
    dialog = _CharRoleDialog(
        0,
        ["导", "歌"],
        ["导唱", None],
        ["导唱"],
        Style(),
        vector_symbols=[symbol, None],
    )

    assert dialog.char_labels() == ["导唱", None]
    assert dialog._chips.role_tooltip_text(0) == "导唱符 · 角色方案：导唱"
    dialog.close()


def test_guide_symbol_settings_dialog_returns_count_and_interval():
    dialog = _GuideSymbolSettingsDialog(count=3, interval_ms=750)

    assert dialog.windowTitle() == "导唱符设置"
    assert dialog.settings() == (3, 750)
    dialog.close()


def test_lyrics_preview_shows_embedded_guide_icon(tmp_path):
    symbol = _symbol(tmp_path)
    panel = LyricsPanel()
    panel.set_track(
        TimingTrack(
            lines=[
                TimingLine(
                    chars=[TimingChar("歌", 2000)],
                    end_ms=2500,
                    guide_symbol=symbol,
                )
            ]
        )
    )

    item = panel.table_widget.item(0, COL_CONTENT)
    assert item is not None
    assert not item.icon().isNull()
    assert "行前导唱符" in item.toolTip()
    panel.close()


def test_project_payload_keeps_optional_line_guide_symbols(tmp_path):
    symbol = _symbol(tmp_path)
    row = guide_symbol_to_dict(replace(symbol, role_label="角色A"))

    payload = project_payload(
        subtitle_path=None,
        video_path=None,
        audio_path=None,
        style={},
        screen={},
        selected_scheme_key="default",
        output={},
        line_guide_symbols=[row, None],
    )

    assert guide_symbol_from_dict(payload["line_guide_symbols"][0]) == replace(
        symbol, role_label="角色A"
    )
    assert payload["line_guide_symbols"][1] is None
