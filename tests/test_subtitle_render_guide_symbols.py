from __future__ import annotations

from dataclasses import replace

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QAbstractItemView, QApplication, QDialog

from krok_helper.subtitle_render.engine import painter as subtitle_painter
from krok_helper.subtitle_render.frontend import guide_replacement as guide_replacement_module
from krok_helper.subtitle_render.frontend import main_window as main_window_module
from krok_helper.subtitle_render.engine.painter import _layout_line_uncached, paint_frame
from krok_helper.subtitle_render.engine.timeline import compute_display_lines, find_active_line
from krok_helper.subtitle_render.frontend.guide_replacement import (
    GuidePrefixReplaceDialog,
    detect_guide_prefix_matches,
    guide_marker_options,
    replacement_symbol_for_match,
)
from krok_helper.subtitle_render.frontend.lyrics_list import _CharRoleDialog
from krok_helper.subtitle_render.frontend.lyrics_list import COL_CONTENT, LyricsPanel
from krok_helper.subtitle_render.frontend.main_window import (
    SubtitleRenderWindow,
    _GuideSymbolSettingsDialog,
)
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
    guide_symbol_replacement_count,
    guide_symbol_role_labels,
    guide_symbol_to_dict,
    guide_symbol_with_role_labels,
    line_visible_chars,
    timing_line_start_ms,
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


def test_detects_one_or_more_consecutive_timed_prefix_markers():
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("h", 1000), TimingChar("歌", 2000)],
                end_ms=2500,
            ),
            TimingLine(
                chars=[
                    TimingChar("h", 3000),
                    TimingChar("h", 3400),
                    TimingChar("詞", 4100),
                ],
                end_ms=4500,
            ),
            TimingLine(
                chars=[TimingChar("t", 5000), TimingChar("est", 5300)],
                end_ms=5800,
            ),
        ]
    )

    matches = detect_guide_prefix_matches(track, "h")

    assert [match.row for match in matches] == [0, 1]
    assert [match.prefix for match in matches] == [("h",), ("h", "h")]
    assert [match.intervals_ms for match in matches] == [(1000,), (400, 700)]
    assert guide_marker_options(track)[0] == ("h", 2)


def test_prefix_replacement_reuses_original_timing_and_hides_marker(tmp_path):
    line = TimingLine(
        chars=[
            TimingChar("h", 1000, role_label="导唱A"),
            TimingChar("h", 1800, role_label="导唱B"),
            TimingChar("歌", 2500),
            TimingChar("詞", 2900),
        ],
        end_ms=3300,
    )
    match = detect_guide_prefix_matches(TimingTrack(lines=[line]), "h")[0]
    symbol = replacement_symbol_for_match(_symbol(tmp_path), line, match)
    assert symbol is not None
    line.guide_symbol = symbol

    layout = _layout_line_uncached(
        TimingTrack(lines=[line]),
        line,
        Style(font_family="Arial", font_size_px=72),
        900,
        450,
    )

    assert guide_symbol_replacement_count(line) == 2
    assert "".join(char.text for char in line_visible_chars(line)) == "歌詞"
    assert timing_line_start_ms(line) == 1000
    assert layout is not None and layout.render_line is not None
    assert [char.start_ms for char in layout.render_line.chars] == [
        1000,
        1800,
        2500,
        2900,
    ]
    assert [char.role_label for char in layout.render_line.chars[:2]] == [
        "导唱A",
        "导唱B",
    ]
    assert all(char.vector_glyph == symbol for char in layout.render_line.chars[:2])


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


def test_prefix_replace_dialog_lists_candidates_and_keeps_ambiguous_rows_selectable(
    tmp_path,
):
    track = TimingTrack(
        lines=[
            TimingLine(chars=[TimingChar("h", 1000), TimingChar("歌", 2000)]),
            TimingLine(chars=[TimingChar("h", 3000), TimingChar("詞", 4100)]),
        ]
    )
    dialog = GuidePrefixReplaceDialog(track)
    dialog.set_svg_path(tmp_path / "lead.svg")

    assert dialog.windowTitle() == "行首导唱符替换"
    assert "2 行" in dialog.summary_label.text()
    assert [match.row for match in dialog.selected_matches()] == [0, 1]
    dialog._row_checks[1].setCheckState(Qt.CheckState.Unchecked)
    assert [match.row for match in dialog.selected_matches()] == [0]
    dialog.close()


def test_marker_enter_only_refreshes_results_and_table_has_no_row_selection(
    monkeypatch,
):
    track = TimingTrack(
        lines=[TimingLine(chars=[TimingChar("h", 1000), TimingChar("歌", 2000)])]
    )
    browse_calls = 0

    def browse(*_args, **_kwargs):
        nonlocal browse_calls
        browse_calls += 1
        return "", ""

    monkeypatch.setattr(
        guide_replacement_module.QFileDialog, "getOpenFileName", browse
    )
    dialog = GuidePrefixReplaceDialog(track)
    dialog.show()
    QApplication.processEvents()
    dialog.marker_edit.setText("t")
    dialog.marker_edit.setFocus()

    QTest.keyClick(dialog.marker_edit, Qt.Key.Key_Return)
    QApplication.processEvents()

    assert browse_calls == 0
    assert "0 行" in dialog.summary_label.text()
    assert dialog.table.selectionMode() == QAbstractItemView.SelectionMode.NoSelection
    assert not dialog.table.selectedItems()
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


def test_lyrics_preview_hides_replaced_prefix_and_preserves_source_chars(tmp_path):
    symbol = replace(
        _symbol(tmp_path), count=1, replacement_prefix=("h",), role_labels=(None,)
    )
    line = TimingLine(
        chars=[TimingChar("h", 1000), TimingChar("歌词", 2000)],
        end_ms=2500,
        guide_symbol=symbol,
    )
    panel = LyricsPanel()
    panel.set_track(TimingTrack(lines=[line]))

    item = panel.table_widget.item(0, COL_CONTENT)
    assert item is not None
    assert item.text() == "歌词"
    assert "行首标记替换" in item.toolTip()
    assert "".join(char.text for char in line.chars) == "h歌词"
    panel.close()


def test_project_reload_skips_replacement_when_source_prefix_changed(tmp_path):
    symbol = replace(_symbol(tmp_path), count=1, replacement_prefix=("h",))
    payload = guide_symbol_to_dict(symbol)
    track = TimingTrack(
        lines=[
            TimingLine(chars=[TimingChar("h", 1000), TimingChar("歌", 2000)]),
            TimingLine(chars=[TimingChar("t", 3000), TimingChar("詞", 4000)]),
        ]
    )

    mismatches = SubtitleRenderWindow._apply_guide_symbol_rows(
        track, [payload, payload]
    )

    assert mismatches == [1]
    assert track.lines[0].guide_symbol == symbol
    assert track.lines[1].guide_symbol is None


def test_batch_prefix_replacement_is_one_undoable_command(tmp_path, monkeypatch):
    _symbol(tmp_path)
    track = TimingTrack(
        lines=[
            TimingLine(chars=[TimingChar("h", 1000), TimingChar("歌", 2000)]),
            TimingLine(chars=[TimingChar("h", 3000), TimingChar("詞", 4100)]),
        ]
    )
    matches = detect_guide_prefix_matches(track, "h")

    class AcceptedDialog:
        def __init__(self, *_args, **_kwargs):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def svg_path(self):
            return tmp_path / "lead.svg"

        def selected_matches(self):
            return matches

    monkeypatch.setattr(
        main_window_module, "GuidePrefixReplaceDialog", AcceptedDialog
    )
    monkeypatch.setattr(
        main_window_module.SubtitleRenderWindow,
        "_resolve_ffprobe_path",
        lambda self: "ffprobe",
    )
    monkeypatch.setenv(
        "KARAOKE_STUDIO_SETTINGS_DIR", str(tmp_path / "settings")
    )
    window = main_window_module.SubtitleRenderWindow(embedded=False)
    window._timing_track = track
    window._lyrics_panel.set_track(track)

    window._on_guide_prefix_replace_requested()

    assert all(line.guide_symbol is not None for line in track.lines)
    assert len(window._undo_stack) == 1
    window._undo_edit()
    assert all(line.guide_symbol is None for line in track.lines)
    window._redo_edit()
    assert all(line.guide_symbol is not None for line in track.lines)
    window.close()


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
