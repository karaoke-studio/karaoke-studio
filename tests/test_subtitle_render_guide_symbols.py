from __future__ import annotations

from dataclasses import replace

import pytest
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
from krok_helper.subtitle_render.engine.guide.metrics import vector_glyph_width
from krok_helper.subtitle_render.engine.guide.semantics import guide_symbol_is_bitmap
from krok_helper.subtitle_render.sources.guide_symbols import (
    GuideSymbolImportError,
    guide_symbol_path,
    import_bitmap_guide_symbol,
    import_svg_guide_symbol,
    is_vector_guide_symbol_file,
)
from krok_helper.subtitle_render.domain.models import (
    GuideSymbol,
    Style,
    SubtitleStyleScheme,
    TimingChar,
    TimingLine,
    TimingTrack,
    guide_symbol_from_dict,
    guide_symbol_has_visual,
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


def _write_png(path, color: str = "#FF0000") -> str:
    image = QImage(10, 6, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor(color))
    assert image.save(str(path))
    return str(path)


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


def test_bitmap_guide_import_wraps_before_and_after_images(tmp_path):
    before = _write_png(tmp_path / "before.png", "#FF0000")
    after = _write_png(tmp_path / "after.png", "#0000FF")

    symbol = import_bitmap_guide_symbol(before, after, duration_ms=800, count=2)

    assert symbol.kind == "bitmap"
    assert symbol.name == "before"
    assert symbol.bitmap_before_path == before
    assert symbol.bitmap_after_path == after
    assert symbol.duration_ms == 800
    assert symbol.count == 2
    assert not symbol.path_commands

    after_only = import_bitmap_guide_symbol(None, after)
    assert after_only.kind == "bitmap"
    assert after_only.bitmap_before_path is None
    assert after_only.bitmap_after_path == after
    assert after_only.name == "after"

    with pytest.raises(GuideSymbolImportError):
        import_bitmap_guide_symbol(None, None)
    with pytest.raises(GuideSymbolImportError):
        import_bitmap_guide_symbol(tmp_path / "missing.png", None)


def test_vector_dispatch_follows_svg_suffix(tmp_path):
    assert is_vector_guide_symbol_file(tmp_path / "lead.svg")
    assert is_vector_guide_symbol_file("a.SVG")
    assert not is_vector_guide_symbol_file(tmp_path / "avatar.png")


def test_after_only_bitmap_guide_keeps_visual_and_persists(tmp_path):
    """「走字前图片留空」是合法状态：走字前透明、走字后显示，不能被校验丢掉。"""
    after = _write_png(tmp_path / "after.png", "#00FF00")
    symbol = import_bitmap_guide_symbol(None, after)

    assert guide_symbol_has_visual(symbol)
    assert guide_symbol_is_bitmap(symbol)
    assert guide_symbol_from_dict(guide_symbol_to_dict(symbol)) == symbol


def test_guide_bitmap_settings_dialog_requires_at_least_one_image(tmp_path):
    guide_replacement_module.remember_bitmap_settings({})
    before = _write_png(tmp_path / "before.png", "#FF0000")
    dialog = guide_replacement_module.GuideBitmapSettingsDialog()

    assert not dialog.ok_button.isEnabled()
    dialog.before_edit.setText(before)
    assert dialog.ok_button.isEnabled()
    dialog.before_clear_button.click()
    assert not dialog.ok_button.isEnabled()
    dialog.after_edit.setText(str(tmp_path / "after.png"))
    assert dialog.ok_button.isEnabled()
    assert dialog.before_path() == ""
    assert dialog.after_path() == str(tmp_path / "after.png")
    dialog.close()


def test_guide_bitmap_options_row_round_trips_settings():
    row = guide_replacement_module.GuideBitmapOptionsRow(
        defaults={
            "zoom_mode": "Fix",
            "zoom_value": 250,
            "no_decor": True,
            "margin_left_px": 3,
            "margin_right_px": -170,
            "margin_bottom_px": 4,
        }
    )

    assert row.options() == {
        "zoom_mode": "Fix",
        "zoom_value": 250,
        "no_decor": True,
        "margin_left_px": 3,
        "margin_right_px": -170,
        "margin_bottom_px": 4,
    }
    assert not row.zoom_value_edit.isEnabled()

    kwargs = guide_replacement_module.bitmap_options_kwargs(row.options())
    assert kwargs == {
        "zoom_percent": 250,
        "fix_size": True,
        "no_decor": True,
        "margin_left_px": 3,
        "margin_right_px": -170,
        "margin_bottom_px": 4,
    }

    row.set_options(
        {"zoom_mode": "Zoom", "zoom_value": 80, "no_decor": False,
         "margin_left_px": 0, "margin_right_px": 0, "margin_bottom_px": 0}
    )
    assert guide_replacement_module.bitmap_options_kwargs(row.options())[
        "fix_size"
    ] is False
    assert row.zoom_value_edit.isEnabled()


def test_guide_bitmap_settings_dialog_prefills_from_session_memory(tmp_path):
    before = _write_png(tmp_path / "mem.png", "#FF0000")
    guide_replacement_module.remember_bitmap_settings(
        {
            "before_path": before,
            "after_path": "",
            "zoom_mode": "Zoom",
            "zoom_value": 90,
            "no_decor": True,
            "margin_left_px": 0,
            "margin_right_px": -20,
            "margin_bottom_px": 0,
        }
    )
    try:
        dialog = guide_replacement_module.GuideBitmapSettingsDialog()

        assert dialog.before_path() == before
        assert dialog.after_path() == ""
        assert dialog.ok_button.isEnabled()
        assert dialog.options_row.zoom_value_edit.text() == "90"
        assert dialog.options_row.no_decor_check.isChecked()
        assert dialog.options_row.margin_right_px_edit.text() == "-20"
        dialog.close()
    finally:
        guide_replacement_module.remember_bitmap_settings({})


def test_bitmap_guide_import_applies_emoji_style_options(tmp_path):
    before = _write_png(tmp_path / "before.png", "#FF0000")
    after = _write_png(tmp_path / "after.png", "#0000FF")

    symbol = import_bitmap_guide_symbol(
        before,
        after,
        zoom_percent=40,
        fix_size=True,
        no_decor=True,
        margin_left_px=1,
        margin_right_px=-170,
        margin_bottom_px=2,
    )

    assert symbol.bitmap_zoom_percent == 40
    assert symbol.bitmap_fix_size is True
    assert symbol.bitmap_no_decor is True
    assert (symbol.bitmap_margin_left_px, symbol.bitmap_margin_right_px,
            symbol.bitmap_margin_bottom_px) == (1, -170, 2)
    assert guide_symbol_from_dict(guide_symbol_to_dict(symbol)) == symbol


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


def _count_color_pixels(image: QImage, color: str) -> int:
    target = QColor(color)
    count = 0
    for y in range(image.height()):
        for x in range(image.width()):
            pixel = image.pixelColor(x, y)
            if pixel.alpha() <= 0:
                continue
            if all(
                abs(getattr(pixel, channel)() - getattr(target, channel)())
                <= 24
                for channel in ("red", "green", "blue")
            ):
                count += 1
    return count


def _count_magenta_halo_pixels(image: QImage) -> int:
    """半透明光晕与深色背景合成后不再是纯色，按「红蓝同强、绿弱」判晕。"""
    count = 0
    for y in range(image.height()):
        for x in range(image.width()):
            pixel = image.pixelColor(x, y)
            red, green, blue = pixel.red(), pixel.green(), pixel.blue()
            if red > 90 and blue > 60 and red > green + 50 and blue > green + 30:
                count += 1
    return count


def _decor_frames(tmp_path, *, decoration_kind: str):
    """同一条位图导唱符行在「有装饰 / NoDecor」两帧下的着色计数。"""
    before = _write_png(tmp_path / "avatar.png", "#FF0000")
    symbol = GuideSymbol(
        kind="bitmap",
        bitmap_before_path=before,
        prefix_timing="anchored",
    )
    line = TimingLine(
        chars=[TimingChar("a", 1000), TimingChar("b", 1800)],
        end_ms=2600,
        guide_symbol=symbol,
    )
    style = Style(
        font_family="Arial",
        font_size_px=48,
        line_y_position="center",
        line_lead_in_ms=0,
        decoration_kind=decoration_kind,
        shadow_color="#FF00FF",
        shadow_offset_x=6,
        shadow_offset_y=6,
        glow_before_radius_px=8,
        glow_after_radius_px=8,
    )

    def frame(guide) -> QImage:
        image = QImage(640, 360, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor("#101010"))
        paint_frame(
            image,
            TimingTrack(lines=[replace(line, guide_symbol=guide)]),
            1200,
            style,
        )
        return image

    return frame(symbol), frame(replace(symbol, bitmap_no_decor=True))


def test_bitmap_guide_shadow_decor_paints_tinted_silhouette(tmp_path):
    """shadow 装饰：图片 Alpha 剪影按偏移平移、飾り色着色；NoDecor 跳过。"""
    decor_frame, no_decor_frame = _decor_frames(
        tmp_path, decoration_kind="shadow"
    )

    decor_count = _count_color_pixels(decor_frame, "#FF00FF")
    no_decor_count = _count_color_pixels(no_decor_frame, "#FF00FF")

    assert decor_count > no_decor_count
    assert decor_frame.constBits().asstring(
        decor_frame.sizeInBytes()
    ) != no_decor_frame.constBits().asstring(no_decor_frame.sizeInBytes())


def test_bitmap_guide_glow_decor_paints_blurred_halo(tmp_path):
    """glow 装饰：剪影按发光半径模糊出光晕；NoDecor 跳过。"""
    decor_frame, no_decor_frame = _decor_frames(
        tmp_path, decoration_kind="glow"
    )

    decor_count = _count_magenta_halo_pixels(decor_frame)
    no_decor_count = _count_magenta_halo_pixels(no_decor_frame)

    assert decor_count > no_decor_count
    assert decor_count - no_decor_count > 100


def test_bitmap_guide_gradient_decor_uses_fill_brush(tmp_path):
    """飾り画刷为渐变时，剪影按整行跨度取渐变色，而不是主色纯色。"""
    from krok_helper.subtitle_render.domain.paint import (
        KaraokeColors,
        KaraokeColorState,
        PaintFill,
    )

    avatar = _write_png(tmp_path / "avatar.png", "#00FF00")
    gradient = PaintFill(
        mode="gradient_horizontal",
        color="#FF0000",
        start_color="#FF0000",
        end_color="#FF00FF",
        gradient_stops=[(0, "#FF0000"), (100, "#FF00FF")],
    )
    colors = KaraokeColors(
        before=KaraokeColorState(shadow=gradient),
        after=KaraokeColorState(shadow=gradient),
    )
    line = TimingLine(
        chars=[TimingChar("a", 1000), TimingChar("b", 1800)],
        end_ms=2600,
    )
    style = Style(
        font_family="Arial",
        font_size_px=48,
        line_y_position="center",
        line_lead_in_ms=0,
        decoration_kind="shadow",
        shadow_offset_x=6,
        shadow_offset_y=6,
        karaoke_colors=colors,
    )

    def warm_green_counts(no_decor: bool) -> tuple[int, int]:
        symbol = GuideSymbol(
            kind="bitmap",
            bitmap_before_path=avatar,
            bitmap_no_decor=no_decor,
            prefix_timing="anchored",
        )
        frame = QImage(640, 360, QImage.Format.Format_ARGB32_Premultiplied)
        frame.fill(QColor("#101010"))
        paint_frame(
            frame,
            TimingTrack(lines=[replace(line, guide_symbol=symbol)]),
            1200,
            style,
        )
        warm = 0
        green = 0
        for y in range(frame.height()):
            for x in range(frame.width()):
                pixel = frame.pixelColor(x, y)
                red, g, blue = pixel.red(), pixel.green(), pixel.blue()
                if red > 120 and blue > 80 and red > g + 60 and blue > g + 40:
                    warm += 1
                elif g > 120 and g > red + 50 and g > blue + 50:
                    green += 1
        return warm, green

    decor_warm, decor_green = warm_green_counts(False)
    plain_warm, plain_green = warm_green_counts(True)

    assert decor_warm > plain_warm
    assert decor_green == plain_green


def test_bitmap_guide_without_decor_style_keeps_plain_image(tmp_path):
    """decoration_kind=none 时位图导唱符保持纯图片，不画任何装饰。"""
    before = _write_png(tmp_path / "avatar.png", "#FF0000")
    symbol = GuideSymbol(
        kind="bitmap",
        bitmap_before_path=before,
        prefix_timing="anchored",
    )
    line = TimingLine(
        chars=[TimingChar("a", 1000), TimingChar("b", 1800)],
        end_ms=2600,
        guide_symbol=symbol,
    )
    style = Style(
        font_family="Arial",
        font_size_px=48,
        line_y_position="center",
        line_lead_in_ms=0,
        decoration_kind="none",
        shadow_color="#FF00FF",
        shadow_offset_x=6,
        shadow_offset_y=6,
    )
    image = QImage(640, 360, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor("#101010"))

    paint_frame(image, TimingTrack(lines=[line]), 1200, style)

    assert _count_color_pixels(image, "#FF00FF") == 0
    assert _count_red_pixels(image) > 0


def _transparent_png(path, size: int = 8) -> None:
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor(0, 0, 0, 0))
    assert image.save(str(path))


def _red_png(path, size: int = 64) -> None:
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor(255, 0, 0, 255))
    assert image.save(str(path))


def test_bitmap_inline_avatar_wipes_across_own_interval(tmp_path):
    """带 after 图的行内位图头像必须按自身字符区间做走字 wipe。

    头像字符没有文字轮廓，旧实现墨水范围恒为零宽：after 图要么不出现、
    要么等后继文字扫过时整块弹出（间奏 ``[ts]【朵】`` 应援行两者必占其一）。
    现在以头像内容矩形为墨水，reveal 随自身区间渐进推进。
    """
    before_path = tmp_path / "透明图像.png"
    after_path = tmp_path / "chieru.png"
    _transparent_png(before_path)
    _red_png(after_path)
    symbol = replace(
        _bitmap_symbol(tmp_path),
        bitmap_before_path=str(before_path),
        bitmap_after_path=str(after_path),
        prefix_timing="pre_roll",
    )
    line = TimingLine(
        chars=[TimingChar("朵", 1000), TimingChar("朵", 2000)],
        end_ms=3000,
        inline_guide_symbols={0: symbol, 1: symbol},
    )
    track = TimingTrack(lines=[line])
    style = Style(
        font_family="Arial",
        font_size_px=48,
        line_y_position="center",
        line_lead_in_ms=0,
    )

    def red_at(t_ms: int) -> int:
        image = QImage(640, 360, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor("#101010"))
        paint_frame(image, track, t_ms, style)
        return _count_red_pixels(image)

    start = red_at(1000)
    early = red_at(1500)
    late_first = red_at(1995)
    mid_second = red_at(2500)
    done = red_at(2999)

    assert start == 0
    # 第一个头像在自己的窗口内渐进显现，窗口结束时接近完整（单个头像
    # 48×48 ≈ 2304 px，第二个尚未开始）。
    assert 0 < early < late_first <= 2600
    # 第二个头像同样渐进，最终两个都完整。
    assert late_first < mid_second < done
    assert done <= 5000


def test_bitmap_inline_avatar_content_size_prefers_after_image(tmp_path):
    """窄长透明占位 before 图 + 真图 after：格子尺寸按 after 图计算。

    旧实现按 before 图定宽高比，2×400 的透明占位会把头像格子压成
    像素级细条——after 图被挤进 1px 宽的竖线里，表现为「一条细线」。
    """
    narrow = QImage(2, 400, QImage.Format.Format_ARGB32_Premultiplied)
    narrow.fill(QColor(0, 0, 0, 0))
    assert narrow.save(str(tmp_path / "spacer.png"))
    square = QImage(64, 64, QImage.Format.Format_ARGB32_Premultiplied)
    square.fill(QColor(255, 0, 0, 255))
    assert square.save(str(tmp_path / "avatar.png"))
    symbol = GuideSymbol(
        name="avatar",
        kind="bitmap",
        bitmap_before_path=str(tmp_path / "spacer.png"),
        bitmap_after_path=str(tmp_path / "avatar.png"),
        bitmap_no_decor=True,
    )
    style = Style(font_family="Arial", font_size_px=64)

    assert vector_glyph_width(symbol, style) == 64

    # 无 after 图的符号（分色 1x1 占位）仍按 before 图定尺寸。
    before_only = replace(symbol, bitmap_after_path=None)
    spacer_square = QImage(1, 1, QImage.Format.Format_ARGB32_Premultiplied)
    spacer_square.fill(QColor(0, 0, 0, 0))
    assert spacer_square.save(str(tmp_path / "sq.png"))
    before_only = replace(before_only, bitmap_before_path=str(tmp_path / "sq.png"))
    assert vector_glyph_width(before_only, style) == 64


def test_bitmap_inline_avatar_before_image_wipes_to_unsung_side(tmp_path):
    """不透明 before + after 的头像走严格双侧遮罩。

    未唱侧显示 before 图、已唱侧显示 after 图；全部唱完后 before 图
    必须消失，不能继续垫在 after 图下面（半透明 after 图时会露底）。
    """
    before_path = tmp_path / "before.png"
    after_path = tmp_path / "after.png"
    blue = QImage(64, 64, QImage.Format.Format_ARGB32_Premultiplied)
    blue.fill(QColor(0, 80, 255, 255))
    assert blue.save(str(before_path))
    red = QImage(64, 64, QImage.Format.Format_ARGB32_Premultiplied)
    red.fill(QColor(255, 0, 0, 255))
    assert red.save(str(after_path))
    symbol = GuideSymbol(
        name="avatar",
        kind="bitmap",
        bitmap_before_path=str(before_path),
        bitmap_after_path=str(after_path),
        bitmap_no_decor=True,
    )
    line = TimingLine(
        chars=[TimingChar("朵", 1000), TimingChar("朵", 2000)],
        end_ms=3000,
        inline_guide_symbols={0: symbol, 1: symbol},
    )
    track = TimingTrack(lines=[line])
    style = Style(
        font_family="Arial",
        font_size_px=48,
        line_y_position="center",
        line_lead_in_ms=0,
    )

    def blue_pixels(t_ms: int) -> int:
        image = QImage(640, 360, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor("#101010"))
        paint_frame(image, track, t_ms, style)
        count = 0
        for y in range(image.height()):
            for x in range(image.width()):
                c = image.pixelColor(x, y)
                if c.blue() > 200 and c.green() < 150 and c.red() < 80:
                    count += 1
        return count

    idle = blue_pixels(900)
    wiping = blue_pixels(1500)
    done = blue_pixels(2999)

    # 未开唱：两个 before 图完整可见（各 48×48）。
    assert idle > 2 * 1800
    # 走字中：第一个头像的 before 只剩未唱侧（严格小于完整）。
    assert 0 < wiping < idle
    # 全部唱完：before 图彻底消失。
    assert done == 0


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
    # 出厂预设（74f5c7b）默认走字动画为 utopia——其图层逐帧动态填充、不经
    # _TEXT_RUN_LAYER_CACHE 烘焙；本测试的对象是常规走字的图层缓存，显式
    # 锁定 inherit 路径，不随出厂默认漂移。
    style = Style(font_family="Arial", font_size_px=72, karaoke_anim="inherit")
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


def _fake_bitmap_settings_dialog(
    monkeypatch, module, *, before="", after="", options=None
):
    """把模块里的图片导唱符设置对话框换成一个已确认的假对话框。"""

    class _SettingsDialog:
        def __init__(self, *, start_dir: str = "", parent=None) -> None:
            pass

        def exec(self) -> int:
            return QDialog.DialogCode.Accepted.value

        def before_path(self) -> str:
            return before

        def after_path(self) -> str:
            return after

        def options(self) -> dict:
            return dict(options or {})

        def settings(self) -> dict:
            return {
                "before_path": before,
                "after_path": after,
                **(options or {}),
            }

    monkeypatch.setattr(module, "GuideBitmapSettingsDialog", _SettingsDialog)
    return _SettingsDialog


def test_char_role_dialog_replaces_only_selected_source_chars_with_svg(
    tmp_path, monkeypatch
):
    prefix_symbol = _symbol(tmp_path)
    replacement_path = tmp_path / "replacement.svg"
    replacement_path.write_text(
        '<svg viewBox="0 0 20 20"><path d="M2 2 L18 10 L2 18 Z"/></svg>',
        encoding="utf-8",
    )
    _fake_bitmap_settings_dialog(
        monkeypatch,
        lyrics_list_module,
        before=str(replacement_path),
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

    dialog._replace_selected_with_symbol()

    symbols = dialog.char_vector_symbols()
    assert symbols[0] == prefix_symbol
    assert symbols[1] is None
    assert symbols[2] is not None
    assert symbols[2].name == "replacement"
    assert symbols[2].kind == "vector"
    dialog.close()


def test_char_role_dialog_replaces_selected_chars_with_bitmap_image(
    tmp_path, monkeypatch
):
    """走字前/后图片由用户在设置对话框里自选槽位，选项随符号写入。"""
    before = _write_png(tmp_path / "before.png", "#FF0000")
    after = _write_png(tmp_path / "after.png", "#0000FF")
    _fake_bitmap_settings_dialog(
        monkeypatch,
        lyrics_list_module,
        before=before,
        after=after,
        options={
            "zoom_mode": "Zoom",
            "zoom_value": 130,
            "no_decor": True,
            "margin_left_px": 2,
            "margin_right_px": -170,
            "margin_bottom_px": 0,
        },
    )
    dialog = _CharRoleDialog(
        0,
        ["歌", "詞"],
        [None, None],
        [],
        Style(),
        vector_symbols=[None, None],
    )
    dialog._chips._selected = {1}
    dialog._chips.selectionChanged.emit()

    dialog._replace_selected_with_symbol()

    symbols = dialog.char_vector_symbols()
    assert symbols[0] is None
    assert symbols[1] is not None
    assert symbols[1].kind == "bitmap"
    assert symbols[1].bitmap_before_path == before
    assert symbols[1].bitmap_after_path == after
    assert symbols[1].name == "before"
    assert symbols[1].bitmap_zoom_percent == 130
    assert symbols[1].bitmap_no_decor is True
    assert symbols[1].bitmap_margin_left_px == 2
    assert symbols[1].bitmap_margin_right_px == -170
    dialog.close()


def test_char_role_dialog_after_only_bitmap_replacement(tmp_path, monkeypatch):
    """走字前留空是合法用法：只有走字后图片也能替换（走字前透明）。"""
    after = _write_png(tmp_path / "after.png", "#0000FF")
    _fake_bitmap_settings_dialog(
        monkeypatch,
        lyrics_list_module,
        after=after,
    )
    dialog = _CharRoleDialog(
        0,
        ["歌"],
        [None],
        [],
        Style(),
        vector_symbols=[None],
    )
    dialog._chips._selected = {0}
    dialog._chips.selectionChanged.emit()

    dialog._replace_selected_with_symbol()

    symbols = dialog.char_vector_symbols()
    assert symbols[0] is not None
    assert symbols[0].kind == "bitmap"
    assert symbols[0].bitmap_before_path is None
    assert symbols[0].bitmap_after_path == after
    dialog.close()


def test_char_role_dialog_bitmap_replacement_cancel_keeps_chars(
    tmp_path, monkeypatch
):
    """图片导唱符设置取消时不应替换任何字符。"""

    class _CancelledSettingsDialog:
        def __init__(self, *, start_dir: str = "", parent=None) -> None:
            pass

        def exec(self) -> int:
            return QDialog.DialogCode.Rejected.value

    monkeypatch.setattr(
        lyrics_list_module, "GuideBitmapSettingsDialog", _CancelledSettingsDialog
    )
    dialog = _CharRoleDialog(
        0,
        ["歌", "詞"],
        [None, None],
        [],
        Style(),
        vector_symbols=[None, None],
    )
    dialog._chips._selected = {1}
    dialog._chips.selectionChanged.emit()

    dialog._replace_selected_with_symbol()

    assert dialog.char_vector_symbols() == [None, None]
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

    assert not dialog._restore_symbol_button.isEnabled()

    dialog._chips._selected = {0}
    dialog._chips.selectionChanged.emit()
    assert not dialog._restore_symbol_button.isEnabled()

    dialog._chips._selected = {1, 3}
    dialog._chips.selectionChanged.emit()
    assert dialog._restore_symbol_button.isEnabled()
    dialog._restore_selected_symbol()

    symbols = dialog.char_vector_symbols()
    assert symbols[0] == prefix_symbol
    assert symbols[1] is None
    assert symbols[2] == second_symbol
    assert symbols[3] is None
    assert not dialog._restore_symbol_button.isEnabled()
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
    dialog.set_before_path(tmp_path / "lead.svg")

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
    dialog.set_before_path(tmp_path / "lead.svg")
    assert dialog.ok_button.isEnabled()
    dialog.close()


def test_prefix_replace_dialog_accepts_after_only_image_symbol(tmp_path):
    """走字前可留空：只填走字后图片也应满足应用条件（走字前透明）。"""
    track = TimingTrack(
        lines=[TimingLine(chars=[TimingChar("h", 1000), TimingChar("歌", 2000)])]
    )
    dialog = GuidePrefixReplaceDialog(track)

    assert dialog.before_path() is None
    assert dialog.after_path() is None
    assert not dialog.ok_button.isEnabled()

    after = tmp_path / "after.png"
    dialog.set_after_path(after)
    assert dialog.after_path() == after
    assert dialog.ok_button.isEnabled()

    dialog.after_clear_button.click()
    assert not dialog.ok_button.isEnabled()

    dialog.set_before_path(tmp_path / "before.png")
    assert dialog.ok_button.isEnabled()

    dialog.before_clear_button.click()
    dialog.after_edit.setText(str(after))
    assert dialog.ok_button.isEnabled()
    assert dialog.bitmap_options()["zoom_mode"] == "Zoom"
    dialog.close()


def test_batch_role_button_does_not_require_svg_or_cached_role_options():
    track = TimingTrack(
        lines=[TimingLine(chars=[TimingChar("h", 1000), TimingChar("歌", 2000)])]
    )
    dialog = GuidePrefixReplaceDialog(track)

    assert dialog.selected_matches()
    assert dialog.before_path() is None
    assert dialog.after_path() is None
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

    class AcceptedBitmapDialog:
        def __init__(self, *_args, **_kwargs):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def before_path(self):
            return str(svg_path)

        def after_path(self):
            return ""

        def options(self):
            return {}

        def settings(self):
            return {"before_path": str(svg_path), "after_path": ""}

    monkeypatch.setattr(
        main_window_module, "GuideBitmapSettingsDialog", AcceptedBitmapDialog
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
    assert "行内导唱符：1 个" in item.toolTip()
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

        def before_path(self):
            return tmp_path / "lead.svg"

        def after_path(self):
            return None

        def bitmap_options(self):
            return {}

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

        def before_path(self):
            return tmp_path / "lead.svg"

        def after_path(self):
            return None

        def bitmap_options(self):
            return {}

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

        def before_path(self):
            return tmp_path / "lead.svg"

        def after_path(self):
            return None

        def bitmap_options(self):
            return {}

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

        def before_path(self):
            return tmp_path / "lead.svg"

        def after_path(self):
            return None

        def bitmap_options(self):
            return {}

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

        def before_path(self):
            return tmp_path / "lead.svg"

        def after_path(self):
            return None

        def bitmap_options(self):
            return {}

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
