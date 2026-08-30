from __future__ import annotations

from dataclasses import replace

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QAbstractItemView, QApplication, QDialog

from krok_helper.subtitle_render.engine import painter as subtitle_painter
from krok_helper.subtitle_render.frontend.dialogs import guide_replacement as guide_replacement_module
from krok_helper.subtitle_render.frontend.editor import lyrics_list as lyrics_list_module
from krok_helper.subtitle_render.frontend import main_window as main_window_module
from krok_helper.subtitle_render.engine.painter import (
    _layout_line_uncached,
    _resolve_display_baselines,
    paint_frame,
)
from krok_helper.subtitle_render.engine.timing.timeline import compute_display_lines, find_active_line
from krok_helper.subtitle_render.frontend.dialogs.guide_replacement import (
    GuidePrefixReplaceDialog,
    GuideRoleSchemeDialog,
    detect_guide_prefix_matches,
    guide_marker_options,
    replacement_symbol_for_match,
)
from krok_helper.subtitle_render.frontend.editor.lyrics_list import _CharRoleDialog
from krok_helper.subtitle_render.frontend.editor.lyrics_list import (
    COL_CONTENT,
    LyricsPanel,
    _line_content_text,
)
from krok_helper.subtitle_render.frontend.main_window import (
    SubtitleRenderWindow,
    _GuideSymbolSettingsDialog,
)
from krok_helper.subtitle_render.domain.timing import assign_role_to_track_rows
from krok_helper.subtitle_render.sources.guide_symbols import (
    guide_symbol_path,
    import_svg_guide_symbol,
)
from krok_helper.subtitle_render.domain.models import (
    GuideSymbol,
    Style,
    SubtitleStyleScheme,
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
from krok_helper.subtitle_render.project.store import project_payload
from krok_helper.subtitle_render.project.load import apply_track_project_data


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


def _bitmap_symbol(tmp_path, *, color: str = "#FF0000") -> GuideSymbol:
    image_path = tmp_path / "avatar.png"
    image = QImage(12, 8, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor(color))
    assert image.save(str(image_path))
    return GuideSymbol(
        name="avatar",
        kind="bitmap",
        bitmap_before_path=str(image_path),
        bitmap_zoom_percent=100,
        bitmap_no_decor=True,
        prefix_timing="anchored",
    )


def test_line_content_text_marks_bitmap_inline_symbols(tmp_path):
    """@Emoji 位图头像（无 path_commands）在歌词列表内容列也显示 ◆ 占位，
    不能把原始触发标签【演唱者名】当正文漏出来。"""
    symbol = _bitmap_symbol(tmp_path)
    line = TimingLine(
        chars=[
            TimingChar("【A】", 1000, role_label="A"),
            TimingChar("あ", 1000, role_label="A"),
        ],
        end_ms=1500,
        inline_guide_symbols={0: symbol},
    )

    assert _line_content_text(line) == "◆あ"


def test_char_chips_paint_bitmap_avatar_thumbnail(qapp, tmp_path):
    """逐字符角色对话框的字符块必须画出 @Emoji 头像缩略图（不再整块空白）。"""
    image_path = tmp_path / "avatar.png"
    image = QImage(24, 16, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor(255, 0, 0, 255))
    assert image.save(str(image_path))
    view = lyrics_list_module._CharChipsView(
        ["【A】", "あ"],
        ["A", "A"],
        Style(),
        vector_symbols=[
            GuideSymbol(kind="bitmap", bitmap_before_path=str(image_path)),
            None,
        ],
    )
    view.resize(2 * view._CHIP_W + view._GAP, view._CHIP_H)

    canvas = QImage(view.size(), QImage.Format.Format_ARGB32_Premultiplied)
    canvas.fill(Qt.GlobalColor.transparent)
    view.render(canvas)

    red = sum(
        1
        for y in range(canvas.height())
        for x in range(canvas.width())
        if canvas.pixelColor(x, y).red() > 180
        and canvas.pixelColor(x, y).green() < 80
        and canvas.pixelColor(x, y).blue() < 80
    )
    assert red > 10


def test_char_chips_paint_placeholder_for_blank_bitmap(qapp, tmp_path):
    """全透明分色占位图没有可见内容：字符块退 ◆ 占位，不能什么都不画。"""
    image_path = tmp_path / "blank.png"
    image = QImage(1, 1, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor(0, 0, 0, 0))
    assert image.save(str(image_path))
    view = lyrics_list_module._CharChipsView(
        ["【A】"],
        ["A"],
        Style(),
        vector_symbols=[
            GuideSymbol(kind="bitmap", bitmap_before_path=str(image_path)),
        ],
    )
    view.resize(view._CHIP_W, view._CHIP_H)

    canvas = QImage(view.size(), QImage.Format.Format_ARGB32_Premultiplied)
    canvas.fill(Qt.GlobalColor.transparent)
    view.render(canvas)

    visible = sum(
        1
        for y in range(canvas.height())
        for x in range(canvas.width())
        if canvas.pixelColor(x, y).alpha() > 0
    )
    # ◆ 文字 + chip 边框都算可见内容；纯空白（旧行为）远低于此
    assert visible > 30


def _count_red_pixels(image: QImage) -> int:
    count = 0
    for y in range(image.height()):
        for x in range(image.width()):
            color = image.pixelColor(x, y)
            if color.red() > 220 and color.green() < 40 and color.blue() < 40:
                count += 1
    return count


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


def test_bitmap_guide_symbol_round_trips_optional_fields(tmp_path):
    symbol = replace(
        _bitmap_symbol(tmp_path),
        bitmap_after_path=str(tmp_path / "after.png"),
        bitmap_zoom_percent=75,
        bitmap_fix_size=True,
        bitmap_force_wipe_decor=True,
        bitmap_margin_left_px=3,
        bitmap_margin_right_px=-12,
        bitmap_margin_bottom_px=4,
    )

    restored = guide_symbol_from_dict(guide_symbol_to_dict(symbol))

    assert restored == symbol


def test_guide_symbol_cache_signature_reuses_frozen_outline_model(tmp_path):
    symbol = _symbol(tmp_path)

    signature = subtitle_painter._value_signature(symbol)

    assert signature is symbol


def test_bitmap_prefix_guide_is_anchored_to_first_lyric_timestamp(tmp_path):
    symbol = _bitmap_symbol(tmp_path)
    line = TimingLine(
        chars=[TimingChar("a", 2000), TimingChar("b", 2500)],
        end_ms=3000,
        guide_symbol=symbol,
    )
    track = TimingTrack(lines=[line])
    style = Style(font_family="Arial", font_size_px=72)

    layout = _layout_line_uncached(track, line, style, 800, 450)

    assert layout is not None
    assert layout.render_line is not None
    assert layout.render_line.chars[0].vector_glyph == symbol
    assert layout.intervals[0][0] == 2000
    display = compute_display_lines(
        track,
        lead_in_ms=0,
        tail_ms=0,
        lane_gap_ms=0,
    )
    assert display[0].display_start_ms == 2000


def test_bitmap_prefix_guide_paints_image_pixels(tmp_path):
    symbol = _bitmap_symbol(tmp_path)
    line = TimingLine(
        chars=[TimingChar("a", 1000), TimingChar("b", 1800)],
        end_ms=2600,
        guide_symbol=symbol,
    )
    track = TimingTrack(lines=[line])
    style = Style(
        font_family="Arial",
        font_size_px=48,
        line_y_position="center",
        line_lead_in_ms=0,
    )
    image = QImage(640, 360, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor("#101010"))

    paint_frame(image, track, 1200, style)

    assert _count_red_pixels(image) > 20


def test_bitmap_prefix_guide_does_not_consume_text_wipe_segment(tmp_path):
    symbol = _bitmap_symbol(tmp_path)
    line = TimingLine(
        chars=[TimingChar("a", 1000), TimingChar("b", 1800)],
        end_ms=2600,
        guide_symbol=symbol,
    )
    track = TimingTrack(lines=[line])
    style = Style(font_family="Arial", font_size_px=48)

    layout = _layout_line_uncached(track, line, style, 640, 360)

    assert layout is not None
    assert layout.render_line is not None
    assert layout.render_line.chars[0].vector_glyph == symbol
    assert all(0 not in segment.indices for segment in layout.fill_segments)
    assert layout.fill_segments[0].indices == (1,)
    assert layout.fill_segments[0].start_ms == 1000


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


def test_large_guide_role_keeps_shared_lane_baselines(tmp_path):
    symbol = replace(_symbol(tmp_path), role_label="导唱符")
    upper = TimingLine(
        chars=[TimingChar("歌", 1200)],
        end_ms=1600,
        guide_symbol=symbol,
    )
    lower = TimingLine(chars=[TimingChar("詞", 1200)], end_ms=1600)
    track = TimingTrack(lines=[upper, lower])
    style = Style(
        font_family="Arial",
        font_family_latin="Arial",
        font_size_px=48,
        stroke_width_px=0,
        stroke2_enabled=False,
        decoration_kind="none",
        dual_line_layout=True,
        line_y_position="center",
        line_gap_px=24,
        custom_style_schemes={
            "导唱符": SubtitleStyleScheme(
                font_family="Arial",
                font_size_px=260,
                affects_ruby_anchor=False,
            )
        },
    )
    for layout_semantics in ("legacy", "n3_1074"):
        scenario_style = replace(style, layout_semantics=layout_semantics)
        baselines = _resolve_display_baselines(360, track, [], scenario_style)
        upper_layout = _layout_line_uncached(
            track,
            upper,
            scenario_style,
            640,
            360,
            baseline_y=baselines[0],
            lane=0,
        )
        lower_layout = _layout_line_uncached(
            track,
            lower,
            scenario_style,
            640,
            360,
            baseline_y=baselines[1],
            lane=1,
        )

        assert upper_layout is not None and lower_layout is not None
        assert upper_layout.baseline_y == baselines[0]
        assert lower_layout.baseline_y == baselines[1]
        assert lower_layout.baseline_y - upper_layout.baseline_y == (
            baselines[1] - baselines[0]
        )


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


def test_detection_can_include_consecutive_markers_inside_a_line():
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("歌", 1000),
                    TimingChar("h", 1500),
                    TimingChar("h", 1800),
                    TimingChar("詞", 2300),
                    TimingChar("h", 2800),
                ],
                end_ms=3200,
            )
        ]
    )

    assert detect_guide_prefix_matches(track, "h") == []
    matches = detect_guide_prefix_matches(
        track, "h", include_non_prefix=True
    )

    assert [match.start_index for match in matches] == [1, 4]
    assert [match.count for match in matches] == [2, 1]
    assert [match.replacement_text for match in matches] == [
        "歌◆◆詞h",
        "歌hh詞◆",
    ]
    assert matches[0].intervals_ms == (300, 500)
    assert matches[1].intervals_ms == (400,)


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


def test_existing_prefix_guide_can_be_replaced_again(tmp_path):
    line = TimingLine(
        chars=[
            TimingChar("h", 1000, role_label="导唱"),
            TimingChar("歌", 2000),
        ],
        end_ms=2500,
    )
    old_symbol = replace(
        _symbol(tmp_path),
        name="old",
        replacement_prefix=("h",),
        role_labels=("导唱",),
    )
    line.guide_symbol = old_symbol
    match = detect_guide_prefix_matches(TimingTrack(lines=[line]), "h")[0]

    replacement = replacement_symbol_for_match(
        replace(_symbol(tmp_path), name="new"),
        line,
        match,
    )

    assert match.has_guide_symbol
    assert replacement is not None
    assert replacement.name == "new"
    assert guide_symbol_role_labels(replacement) == ("导唱",)


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


def test_char_role_dialog_replaces_only_selected_source_chars_with_svg(
    tmp_path, monkeypatch
):
    prefix_symbol = _symbol(tmp_path)
    replacement_path = tmp_path / "replacement.svg"
    replacement_path.write_text(
        '<svg viewBox="0 0 20 20"><path d="M2 2 L18 10 L2 18 Z"/></svg>',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        lyrics_list_module.QFileDialog,
        "getOpenFileName",
        lambda *_args, **_kwargs: (str(replacement_path), "SVG 文件 (*.svg)"),
    )
    dialog = _CharRoleDialog(
        0,
        ["导", "歌", "詞"],
        [None, None, None],
        [],
        Style(),
        vector_symbols=[prefix_symbol, None, None],
        protected_prefix_count=1,
    )
    dialog._chips._selected = {0, 2}
    dialog._chips.selectionChanged.emit()

    dialog._replace_selected_with_svg()

    symbols = dialog.char_vector_symbols()
    assert symbols[0] == prefix_symbol
    assert symbols[1] is None
    assert symbols[2] is not None
    assert symbols[2].name == "replacement"
    dialog.close()


def test_char_role_dialog_restores_selected_svg_replacements_only(tmp_path):
    prefix_symbol = _symbol(tmp_path)
    first_symbol = replace(_symbol(tmp_path), name="first")
    second_symbol = replace(_symbol(tmp_path), name="second")
    dialog = _CharRoleDialog(
        0,
        ["导", "歌", "詞", "音"],
        [None, None, None, None],
        [],
        Style(),
        vector_symbols=[prefix_symbol, first_symbol, second_symbol, None],
        protected_prefix_count=1,
    )

    assert not dialog._restore_svg_button.isEnabled()

    dialog._chips._selected = {0}
    dialog._chips.selectionChanged.emit()
    assert not dialog._restore_svg_button.isEnabled()

    dialog._chips._selected = {1, 3}
    dialog._chips.selectionChanged.emit()
    assert dialog._restore_svg_button.isEnabled()
    dialog._restore_selected_svg()

    symbols = dialog.char_vector_symbols()
    assert symbols[0] == prefix_symbol
    assert symbols[1] is None
    assert symbols[2] == second_symbol
    assert symbols[3] is None
    assert not dialog._restore_svg_button.isEnabled()
    dialog.close()


def test_inline_svg_replacement_keeps_middle_character_timing_and_layout(tmp_path):
    symbol = _symbol(tmp_path)
    line = TimingLine(
        chars=[
            TimingChar("歌", 1000),
            TimingChar("h", 1500, pause_release_ms=1750),
            TimingChar("詞", 2000),
        ],
        end_ms=2500,
        inline_guide_symbols={1: symbol},
    )
    track = TimingTrack(lines=[line])

    layout = _layout_line_uncached(
        track, line, Style(font_family="Arial", font_size_px=72), 900, 450
    )

    assert layout is not None and layout.render_line is not None
    assert [char.start_ms for char in layout.render_line.chars] == [1000, 1500, 2000]
    assert layout.render_line.chars[1].pause_release_ms == 1750
    assert layout.render_line.chars[1].vector_glyph == symbol
    assert layout.render_line.chars[0].text == "歌"
    assert layout.render_line.chars[2].text == "詞"
    assert layout.char_x_ranges[0][1] == layout.char_x_ranges[1][0]
    assert layout.char_x_ranges[1][1] == layout.char_x_ranges[2][0]


def test_inline_svg_replacement_coexists_with_prefix_marker_replacement(tmp_path):
    prefix_symbol = replace(
        _symbol(tmp_path), count=1, replacement_prefix=("h",), role_labels=(None,)
    )
    inline_symbol = replace(_symbol(tmp_path), name="inline")
    line = TimingLine(
        chars=[TimingChar("h", 1000), TimingChar("歌", 1500), TimingChar("x", 2000)],
        end_ms=2500,
        guide_symbol=prefix_symbol,
        inline_guide_symbols={2: inline_symbol},
    )

    layout = _layout_line_uncached(
        TimingTrack(lines=[line]),
        line,
        Style(font_family="Arial", font_size_px=72),
        900,
        450,
    )

    assert layout is not None and layout.render_line is not None
    assert [char.start_ms for char in layout.render_line.chars] == [1000, 1500, 2000]
    assert layout.render_line.chars[0].vector_glyph == prefix_symbol
    assert layout.render_line.chars[2].vector_glyph == inline_symbol


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

    assert dialog.windowTitle() == "批量识别导唱标记"
    assert dialog.non_prefix_check.text() == "允许搜索非行首字符"
    assert dialog.marker_edit.width() == 105
    assert "2 行" in dialog.summary_label.text()
    assert [match.row for match in dialog.selected_matches()] == [0, 1]
    dialog._row_checks[1].setCheckState(Qt.CheckState.Unchecked)
    assert [match.row for match in dialog.selected_matches()] == [0]
    dialog.close()


def test_existing_guide_candidate_checkbox_and_batch_role_button_are_available(
    tmp_path,
):
    symbol = replace(
        _symbol(tmp_path),
        replacement_prefix=("h",),
    )
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("h", 1000), TimingChar("歌", 2000)],
                guide_symbol=symbol,
            )
        ]
    )
    dialog = GuidePrefixReplaceDialog(track, role_options=["角色A", "角色B"])

    check_item = dialog._row_checks[0]
    assert check_item.flags() & Qt.ItemFlag.ItemIsUserCheckable
    assert check_item.checkState() == Qt.CheckState.Unchecked
    assert "可重新替换" in dialog.table.item(0, 6).text()
    assert "可替换 1 处" in dialog.summary_label.text()
    assert not dialog.batch_role_button.isEnabled()

    check_item.setCheckState(Qt.CheckState.Checked)
    QApplication.processEvents()

    assert dialog.selected_matches() == [dialog._matches[0]]
    assert dialog.batch_role_button.isEnabled()
    assert not dialog.ok_button.isEnabled()
    dialog.set_svg_path(tmp_path / "lead.svg")
    assert dialog.ok_button.isEnabled()
    dialog.close()


def test_batch_role_button_does_not_require_svg_or_cached_role_options():
    track = TimingTrack(
        lines=[TimingLine(chars=[TimingChar("h", 1000), TimingChar("歌", 2000)])]
    )
    dialog = GuidePrefixReplaceDialog(track)

    assert dialog.selected_matches()
    assert dialog.svg_path() is None
    assert dialog.batch_role_button.isEnabled()
    assert not dialog.ok_button.isEnabled()
    dialog.close()


def test_track_role_assignment_returns_plain_role_history_values():
    track = TimingTrack(
        lines=[
            TimingLine(chars=[TimingChar("甲", 1000), TimingChar("乙", 1500)]),
            TimingLine(chars=[], is_blank=True),
        ]
    )

    assignment = assign_role_to_track_rows(track, [1, 0, 0, 99], " 主唱 ")

    assert assignment is not None
    assert assignment.role_label == "主唱"
    assert assignment.rows == (0,)
    assert assignment.old_values == ((None, None),)
    assert assignment.new_values == (("主唱", "主唱"),)
    assert assignment.includes_guide_symbols is False
    assert [char.role_label for char in track.lines[0].chars] == ["主唱", "主唱"]


def test_track_role_assignment_updates_guide_and_character_roles_together():
    symbol = GuideSymbol(count=2)
    track = TimingTrack(
        lines=[TimingLine(chars=[TimingChar("甲", 1000)], guide_symbol=symbol)]
    )

    assignment = assign_role_to_track_rows(track, [0], "和声")

    assert assignment is not None
    assert assignment.includes_guide_symbols is True
    assert assignment.old_values == ((symbol, (None,)),)
    updated_symbol, updated_labels = assignment.new_values[0]
    assert updated_symbol.role_labels == ("和声", "和声")
    assert updated_labels == ("和声",)
    assert track.lines[0].guide_symbol == updated_symbol
    assert track.lines[0].chars[0].role_label == "和声"


def test_batch_role_button_reads_project_roles_when_clicked(monkeypatch):
    track = TimingTrack(
        lines=[TimingLine(chars=[TimingChar("h", 1000), TimingChar("歌", 2000)])]
    )
    roles: list[str] = []
    dialog = GuidePrefixReplaceDialog(
        track,
        role_options_provider=lambda: list(roles),
    )
    received: list[tuple[list, str]] = []
    observed_options: list[list[str]] = []
    dialog.roleSchemeApplyRequested.connect(
        lambda matches, role: received.append((matches, role))
    )
    monkeypatch.setattr(
        guide_replacement_module,
        "choose_guide_role_scheme",
        lambda options, **_kwargs: observed_options.append(options)
        or (options[0] if options else None),
    )

    roles.append("稍后创建的角色")
    dialog.batch_role_button.click()

    assert observed_options == [["稍后创建的角色"]]
    assert received == [(dialog.selected_matches(), "稍后创建的角色")]
    dialog.close()


def test_empty_role_scheme_choice_still_opens_dialog(monkeypatch):
    opened: list[list[str]] = []

    class RejectedDialog:
        def __init__(self, role_names, **_kwargs):
            opened.append(role_names)

        def exec(self):
            return QDialog.DialogCode.Rejected

    monkeypatch.setattr(
        guide_replacement_module, "GuideRoleSchemeDialog", RejectedDialog
    )

    assert (
        guide_replacement_module.choose_guide_role_scheme(
            [],
            prompt="请选择角色方案。",
        )
        is None
    )
    assert opened == [[]]


def test_guide_role_scheme_dialog_uses_existing_project_roles():
    dialog = GuideRoleSchemeDialog(
        ["角色A", "角色B"],
        prompt="请选择角色方案。",
    )

    assert dialog.windowTitle() == "批量应用角色方案"
    assert dialog.role_combo.count() == 2
    assert dialog.role_name() == "角色A"
    dialog.role_combo.setCurrentIndex(1)
    assert dialog.role_name() == "角色B"
    dialog.close()


def test_batch_role_button_emits_checked_matches_from_shared_role_dialog(
    monkeypatch,
):
    track = TimingTrack(
        lines=[TimingLine(chars=[TimingChar("h", 1000), TimingChar("歌", 2000)])]
    )
    dialog = GuidePrefixReplaceDialog(track, role_options=["角色A", "角色B"])
    received: list[tuple[list, str]] = []
    dialog.roleSchemeApplyRequested.connect(
        lambda matches, role: received.append((matches, role))
    )
    monkeypatch.setattr(
        guide_replacement_module,
        "choose_guide_role_scheme",
        lambda roles, **_kwargs: "角色B" if roles == ["角色A", "角色B"] else None,
    )

    dialog.batch_role_button.click()

    assert len(received) == 1
    assert received[0][0] == dialog.selected_matches()
    assert received[0][1] == "角色B"
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


def test_dialog_checkbox_enables_non_prefix_detection():
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("歌", 1000),
                    TimingChar("h", 1500),
                    TimingChar("詞", 2000),
                ],
                end_ms=2500,
            )
        ]
    )
    dialog = GuidePrefixReplaceDialog(track)
    dialog.marker_edit.setText("h")
    dialog.refresh_matches()
    assert dialog.table.rowCount() == 0

    dialog.non_prefix_check.setChecked(True)
    QApplication.processEvents()

    assert dialog.table.rowCount() == 1
    assert dialog.selected_matches()[0].start_index == 1
    assert "1 处" in dialog.summary_label.text()
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


def test_direct_guide_import_refreshes_lyrics_and_preview_immediately(
    tmp_path, monkeypatch
):
    svg_path = tmp_path / "lead.svg"
    _symbol(tmp_path)
    track = TimingTrack(
        lines=[TimingLine(chars=[TimingChar("歌", 1000)], end_ms=2000)]
    )

    class AcceptedSettingsDialog:
        def __init__(self, *_args, **_kwargs):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def settings(self):
            return 1, 1000

    monkeypatch.setattr(
        main_window_module.QFileDialog,
        "getOpenFileName",
        lambda *_args, **_kwargs: (str(svg_path), "SVG 文件 (*.svg)"),
    )
    monkeypatch.setattr(
        main_window_module, "_GuideSymbolSettingsDialog", AcceptedSettingsDialog
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
    preview_tracks = []
    monkeypatch.setattr(window._preview_panel, "set_track", preview_tracks.append)

    window._on_guide_symbol_import_requested([0])

    item = window._lyrics_panel.table_widget.item(0, COL_CONTENT)
    assert track.lines[0].guide_symbol is not None
    assert item is not None
    assert not item.icon().isNull()
    assert preview_tracks == [track]
    window.close()


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


def test_lyrics_preview_marks_inline_svg_without_changing_source_text(tmp_path):
    symbol = _symbol(tmp_path)
    line = TimingLine(
        chars=[TimingChar("歌", 1000), TimingChar("h", 1500), TimingChar("詞", 2000)],
        end_ms=2500,
        inline_guide_symbols={1: symbol},
    )
    panel = LyricsPanel()
    panel.set_track(TimingTrack(lines=[line]))

    item = panel.table_widget.item(0, COL_CONTENT)
    assert item is not None
    assert item.text() == "歌◆詞"
    assert not item.icon().isNull()
    assert "行内 SVG 导唱符：1 个" in item.toolTip()
    assert "".join(char.text for char in line.chars) == "歌h詞"
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

    result = apply_track_project_data(
        track,
        Style(),
        {"line_guide_symbols": [payload, payload]},
    )

    assert result.guide_symbol_mismatches == (1,)
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
        main_window_module,
        "choose_guide_role_scheme",
        lambda *_args, **_kwargs: None,
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


def test_batch_non_prefix_replacement_uses_inline_svg_and_is_undoable(
    tmp_path, monkeypatch
):
    _symbol(tmp_path)
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("歌", 1000),
                    TimingChar("h", 1500),
                    TimingChar("h", 1800),
                    TimingChar("詞", 2300),
                ],
                end_ms=2800,
            )
        ]
    )
    matches = detect_guide_prefix_matches(
        track, "h", include_non_prefix=True
    )

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
        main_window_module,
        "choose_guide_role_scheme",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        main_window_module.SubtitleRenderWindow,
        "_resolve_ffprobe_path",
        lambda self: "ffprobe",
    )
    monkeypatch.setenv("KARAOKE_STUDIO_SETTINGS_DIR", str(tmp_path / "settings"))
    window = main_window_module.SubtitleRenderWindow(embedded=False)
    window._timing_track = track
    window._lyrics_panel.set_track(track)

    window._on_guide_prefix_replace_requested()

    assert set(track.lines[0].inline_guide_symbols) == {1, 2}
    assert track.lines[0].guide_symbol is None
    assert len(window._undo_stack) == 1
    window._undo_edit()
    assert track.lines[0].inline_guide_symbols == {}
    window._redo_edit()
    assert set(track.lines[0].inline_guide_symbols) == {1, 2}
    window.close()


def test_batch_replacement_overwrites_existing_prefix_and_inline_guides(
    tmp_path, monkeypatch
):
    base = _symbol(tmp_path)
    old = replace(base, name="old")
    prefix_old = replace(old, replacement_prefix=("h",))
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("h", 1000), TimingChar("歌", 2000)],
                guide_symbol=prefix_old,
            ),
            TimingLine(
                chars=[
                    TimingChar("詞", 3000),
                    TimingChar("h", 3500),
                    TimingChar("終", 4000),
                ],
                inline_guide_symbols={1: old},
            ),
        ]
    )
    matches = detect_guide_prefix_matches(track, "h", include_non_prefix=True)

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
        main_window_module,
        "choose_guide_role_scheme",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        main_window_module.SubtitleRenderWindow,
        "_resolve_ffprobe_path",
        lambda self: "ffprobe",
    )
    monkeypatch.setenv("KARAOKE_STUDIO_SETTINGS_DIR", str(tmp_path / "settings"))
    window = main_window_module.SubtitleRenderWindow(embedded=False)
    window._timing_track = track
    window._lyrics_panel.set_track(track)

    window._on_guide_prefix_replace_requested()

    assert track.lines[0].guide_symbol is not None
    assert track.lines[0].guide_symbol.name == "lead"
    assert track.lines[1].inline_guide_symbols[1].name == "lead"
    window._undo_edit()
    assert track.lines[0].guide_symbol == prefix_old
    assert track.lines[1].inline_guide_symbols == {1: old}
    window.close()


def test_batch_role_scheme_only_changes_selected_marker_spans_and_is_undoable(
    tmp_path, monkeypatch
):
    prefix_symbol = replace(
        _symbol(tmp_path),
        replacement_prefix=("h",),
        role_labels=("旧角色",),
    )
    inline_symbol = replace(_symbol(tmp_path), name="inline")
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("h", 1000, role_label="旧角色"),
                    TimingChar("歌", 2000, role_label="正文"),
                ],
                guide_symbol=prefix_symbol,
            ),
            TimingLine(
                chars=[
                    TimingChar("h", 3000),
                    TimingChar("詞", 4000, role_label="正文"),
                ]
            ),
            TimingLine(
                chars=[
                    TimingChar("句", 5000, role_label="正文"),
                    TimingChar("h", 5500),
                    TimingChar("末", 6000, role_label="正文"),
                ],
                inline_guide_symbols={1: inline_symbol},
            ),
        ]
    )
    matches = detect_guide_prefix_matches(track, "h", include_non_prefix=True)
    monkeypatch.setattr(
        main_window_module.SubtitleRenderWindow,
        "_resolve_ffprobe_path",
        lambda self: "ffprobe",
    )
    monkeypatch.setenv("KARAOKE_STUDIO_SETTINGS_DIR", str(tmp_path / "settings"))
    window = main_window_module.SubtitleRenderWindow(embedded=False)
    window._timing_track = track
    window._lyrics_panel.set_track(track)

    assert window._apply_guide_match_role_scheme(matches, "角色A")

    assert track.lines[0].chars[0].role_label == "角色A"
    assert guide_symbol_role_labels(track.lines[0].guide_symbol) == ("角色A",)
    assert track.lines[1].chars[0].role_label == "角色A"
    assert track.lines[2].chars[1].role_label == "角色A"
    assert track.lines[0].chars[1].role_label == "正文"
    assert track.lines[1].chars[1].role_label == "正文"
    assert track.lines[2].chars[0].role_label == "正文"
    assert track.lines[2].chars[2].role_label == "正文"
    assert window._undo_stack[-1][0] == "inline_roles_batch"

    window._undo_edit()
    assert track.lines[0].chars[0].role_label == "旧角色"
    assert guide_symbol_role_labels(track.lines[0].guide_symbol) == ("旧角色",)
    assert track.lines[1].chars[0].role_label is None
    assert track.lines[2].chars[1].role_label is None
    window._redo_edit()
    assert track.lines[0].chars[0].role_label == "角色A"
    assert track.lines[1].chars[0].role_label == "角色A"
    assert track.lines[2].chars[1].role_label == "角色A"
    window.close()


def test_batch_replacement_prompts_to_apply_existing_role_scheme(
    tmp_path, monkeypatch
):
    _symbol(tmp_path)
    track = TimingTrack(
        lines=[
            TimingLine(chars=[TimingChar("h", 1000), TimingChar("歌", 2000)])
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

    prompts: list[str] = []
    monkeypatch.setattr(
        main_window_module, "GuidePrefixReplaceDialog", AcceptedDialog
    )
    monkeypatch.setattr(
        main_window_module,
        "choose_guide_role_scheme",
        lambda _roles, **kwargs: prompts.append(kwargs["prompt"]) or "角色A",
    )
    monkeypatch.setattr(
        main_window_module.SubtitleRenderWindow,
        "_resolve_ffprobe_path",
        lambda self: "ffprobe",
    )
    monkeypatch.setenv("KARAOKE_STUDIO_SETTINGS_DIR", str(tmp_path / "settings"))
    window = main_window_module.SubtitleRenderWindow(embedded=False)
    window._style = replace(
        window._style,
        custom_style_schemes={
            **window._style.custom_style_schemes,
            "角色A": SubtitleStyleScheme(),
        },
    )
    window._property_panel.set_style(window._style)
    window._timing_track = track
    window._lyrics_panel.set_track(track)

    window._on_guide_prefix_replace_requested()

    assert prompts and "刚刚批量替换" in prompts[0]
    assert track.lines[0].guide_symbol is not None
    assert guide_symbol_role_labels(track.lines[0].guide_symbol) == ("角色A",)
    assert track.lines[0].chars[0].role_label == "角色A"
    assert [command[0] for command in window._undo_stack[-2:]] == [
        "guide_replacements",
        "inline_roles_batch",
    ]
    window.close()


def test_inline_char_replacement_is_one_undoable_command(tmp_path, monkeypatch):
    symbol = _symbol(tmp_path)
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("歌", 1000), TimingChar("x", 1500)], end_ms=2000
            )
        ]
    )
    monkeypatch.setattr(
        main_window_module.SubtitleRenderWindow,
        "_resolve_ffprobe_path",
        lambda self: "ffprobe",
    )
    monkeypatch.setenv("KARAOKE_STUDIO_SETTINGS_DIR", str(tmp_path / "settings"))
    window = main_window_module.SubtitleRenderWindow(embedded=False)
    window._timing_track = track
    window._lyrics_panel.set_track(track)

    window._on_inline_char_edit_changed(0, None, [None, None], [None, symbol])

    assert track.lines[0].inline_guide_symbols == {1: symbol}
    assert len(window._undo_stack) == 1
    window._undo_edit()
    assert track.lines[0].inline_guide_symbols == {}
    window._redo_edit()
    assert track.lines[0].inline_guide_symbols == {1: symbol}
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


def test_project_payload_and_reload_keep_inline_guide_symbols(tmp_path):
    symbol = _symbol(tmp_path)
    row = {"1": guide_symbol_to_dict(symbol)}

    payload = project_payload(
        subtitle_path=None,
        video_path=None,
        audio_path=None,
        style={},
        screen={},
        selected_scheme_key="default",
        output={},
        line_inline_guide_symbols=[row, None],
    )
    track = TimingTrack(
        lines=[
            TimingLine(chars=[TimingChar("歌", 1000), TimingChar("h", 1500)]),
            TimingLine(chars=[TimingChar("詞", 2000)]),
        ]
    )

    apply_track_project_data(
        track,
        Style(),
        {"line_inline_guide_symbols": payload["line_inline_guide_symbols"]},
    )

    assert track.lines[0].inline_guide_symbols == {1: symbol}
    assert track.lines[1].inline_guide_symbols == {}


def test_project_reload_keeps_bitmap_inline_guide_symbols(tmp_path):
    symbol = _bitmap_symbol(tmp_path)
    payload = [{"0": guide_symbol_to_dict(symbol)}]
    track = TimingTrack(lines=[TimingLine(chars=[TimingChar("x", 1000)])])

    apply_track_project_data(
        track,
        Style(),
        {"line_inline_guide_symbols": payload},
    )

    assert track.lines[0].inline_guide_symbols == {0: symbol}


def test_batch_prefix_replacement_keeps_bitmap_avatar_guide(tmp_path, monkeypatch):
    """@Emoji 小头像占着行前槽位时，行首标记改走行内替换，小头像不能被顶掉。"""
    _symbol(tmp_path)
    avatar = _bitmap_symbol(tmp_path)
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("h", 1000), TimingChar("歌", 2000)],
                end_ms=2600,
                guide_symbol=avatar,
            )
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
        main_window_module,
        "choose_guide_role_scheme",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        main_window_module.SubtitleRenderWindow,
        "_resolve_ffprobe_path",
        lambda self: "ffprobe",
    )
    monkeypatch.setenv("KARAOKE_STUDIO_SETTINGS_DIR", str(tmp_path / "settings"))
    window = main_window_module.SubtitleRenderWindow(embedded=False)
    window._timing_track = track
    window._lyrics_panel.set_track(track)

    window._on_guide_prefix_replace_requested()

    line = track.lines[0]
    assert line.guide_symbol == avatar
    assert line.inline_guide_symbols[0].name == "lead"

    style = Style(
        font_family="Arial",
        font_size_px=48,
        line_y_position="center",
        line_lead_in_ms=0,
    )
    image = QImage(640, 360, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor("#101010"))
    paint_frame(image, track, 1500, style)
    assert _count_red_pixels(image) > 20

    window._undo_edit()
    assert line.guide_symbol == avatar
    assert line.inline_guide_symbols == {}
    window._redo_edit()
    assert line.guide_symbol == avatar
    assert line.inline_guide_symbols[0].name == "lead"
    window.close()


def test_prefix_match_with_avatar_guide_stays_replaceable(tmp_path):
    """不占字符的行前小头像不该被标成"已有导唱符"（那会默认取消勾选）。"""
    avatar = _bitmap_symbol(tmp_path)
    prefix_symbol = replace(_symbol(tmp_path), replacement_prefix=("h",))
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("h", 1000), TimingChar("歌", 2000)],
                guide_symbol=avatar,
            ),
            TimingLine(
                chars=[TimingChar("h", 3000), TimingChar("詞", 4000)],
                guide_symbol=prefix_symbol,
            ),
        ]
    )

    matches = detect_guide_prefix_matches(track, "h")

    assert [match.has_guide_symbol for match in matches] == [False, True]


def test_char_dialog_svg_keeps_inline_bitmap_avatar(tmp_path, monkeypatch):
    """行内已有小头像时，逐字符 SVG 替换不能整条被拒（旧行为：什么也没发生）。"""
    svg = _symbol(tmp_path)
    avatar = replace(_bitmap_symbol(tmp_path), prefix_timing="pre_roll")
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("歌", 1000), TimingChar("詞", 2000)],
                end_ms=2600,
                inline_guide_symbols={0: avatar},
            )
        ]
    )
    monkeypatch.setattr(
        main_window_module.SubtitleRenderWindow,
        "_resolve_ffprobe_path",
        lambda self: "ffprobe",
    )
    monkeypatch.setenv("KARAOKE_STUDIO_SETTINGS_DIR", str(tmp_path / "settings"))
    window = main_window_module.SubtitleRenderWindow(embedded=False)
    window._timing_track = track
    window._lyrics_panel.set_track(track)

    window._on_inline_char_edit_changed(0, None, [None, None], [avatar, svg])

    assert track.lines[0].inline_guide_symbols == {0: avatar, 1: svg}
    window._undo_edit()
    assert track.lines[0].inline_guide_symbols == {0: avatar}
    window._redo_edit()
    assert track.lines[0].inline_guide_symbols == {0: avatar, 1: svg}
    window.close()


def test_project_payload_keeps_bitmap_inline_guide_symbols(tmp_path):
    """位图小头像没有轮廓数据，保存时不能被当成空导唱符丢掉。"""
    avatar = _bitmap_symbol(tmp_path)
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("歌", 1000), TimingChar("詞", 2000)],
                inline_guide_symbols={1: avatar},
            )
        ]
    )

    rows = SubtitleRenderWindow._inline_guide_symbol_rows(track)

    assert rows == [{"1": guide_symbol_to_dict(avatar)}]
