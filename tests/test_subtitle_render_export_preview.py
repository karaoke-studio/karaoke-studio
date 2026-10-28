"""Tests for the export monitor's preview sizing and DPR handling."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, QSize  # noqa: E402
from PyQt6.QtGui import QImage, QPixmap  # noqa: E402
from PyQt6.QtTest import QSignalSpy  # noqa: E402
from PyQt6.QtWidgets import QApplication, QFileDialog, QWidget  # noqa: E402

from krok_helper.subtitle_render.frontend.main_window import (  # noqa: E402
    SubtitleRenderWindow,
    _AspectRatioBox,
    _ExportLocationDialog,
    _ExportMonitorView,
    _export_preview_width,
    _physical_preview_size,
    _scaled_preview_pixmap,
)
from krok_helper.subtitle_render.frontend.workflow.export_view import (  # noqa: E402
    ExportWorkspaceView,
    nearest_existing_directory,
)


class _SettingsProvider:
    def __init__(self):
        self.data = {}

    def load(self):
        return dict(self.data)

    def save(self, data):
        self.data = dict(data)


@pytest.mark.parametrize("remembered", [None, 720, 2160])
def test_empty_controls_are_usable_and_new_video_defaults_to_source(qapp, monkeypatch, tmp_path, remembered):
    from dataclasses import replace
    from krok_helper.models import MediaInfo

    monkeypatch.setattr(SubtitleRenderWindow, "_request_preview_window", lambda self: None)
    provider = _SettingsProvider()
    if remembered:
        provider.data["screen"] = {"width": remembered * 16 // 9, "height": remembered, "fps": 120, "resolution_mode": "custom", "fps_mode": "fixed"}
    window = SubtitleRenderWindow(embedded=True, settings_provider=provider)
    try:
        controls = window._export_controls
        assert controls.size_preset_combo.currentData() == "1080p"
        assert controls.fps_combo.currentData() == 60
        assert (controls.width_spin.value(), controls.height_spin.value()) == (1920, 1080)
        assert window._style.font_reference_height == window._style.layout_reference_height == 1080
        assert window._style.font_size_px == window._app_default_style.font_size_px
        assert not controls.width_spin.isEnabled() and not controls.height_spin.isEnabled()
        assert (controls.width_spin._maximum, controls.height_spin._maximum) == (7680, 4320)
        for combo in (controls.size_preset_combo, controls.fps_combo):
            assert not combo.items[combo.findData("source")].isEnabled
            assert not combo.items[combo.findData("source_2x")].isEnabled

        path = tmp_path / "video.mp4"
        path.write_bytes(b"fake")
        info = MediaInfo(path, 1, 1, 0, 0, video_width=1280, video_height=720, video_fps=30000 / 1001)
        window.load_video(path, info)
        assert controls.size_preset_combo.currentData() == "source"
        assert controls.fps_combo.currentData() == "source"
        assert (window._screen_settings.width, window._screen_settings.height, window._export_fps_value()) == (1280, 720, 30000 / 1001)

        window.set_solid_background("#000000")
        assert controls.size_preset_combo.currentData() == "720p"
        assert controls.fps_combo.currentData() == "custom"
        window.load_video(path, replace(info, video_fps=24))
        assert controls.size_preset_combo.currentData() == controls.fps_combo.currentData() == "source"
        assert window._export_fps_value() == 24
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_editing_one_empty_axis_keeps_other_axis_default_to_source(qapp, monkeypatch, tmp_path):
    from krok_helper.models import MediaInfo

    monkeypatch.setattr(SubtitleRenderWindow, "_request_preview_window", lambda self: None)
    window = SubtitleRenderWindow(embedded=True, settings_provider=_SettingsProvider())
    try:
        controls = window._export_controls
        controls.size_preset_combo.setCurrentIndex(controls.size_preset_combo.findData("720p"))
        path = tmp_path / "video.mp4"
        path.write_bytes(b"fake")
        window.load_video(path, MediaInfo(path, 1, 1, 0, 0, video_width=1920, video_height=1080, video_fps=25))
        assert controls.size_preset_combo.currentData() == "720p"
        assert controls.fps_combo.currentData() == "source"
        assert window._export_fps_value() == 25
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_export_source_modes_custom_values_and_project_round_trip(qapp, monkeypatch, tmp_path):
    from dataclasses import replace
    from krok_helper.models import MediaInfo
    from krok_helper.subtitle_render.settings.screen import screen_settings_from_dict

    monkeypatch.setattr(SubtitleRenderWindow, "_request_preview_window", lambda self: None)
    window = SubtitleRenderWindow(embedded=True, settings_provider=_SettingsProvider())
    try:
        path = tmp_path / "video.mp4"
        path.write_bytes(b"fake")
        info = MediaInfo(path, 1, 1, 0, 0, video_width=1920, video_height=1080, video_fps=30000 / 1001)
        window.load_video(path, info)
        controls = window._export_controls
        controls.size_preset_combo.setCurrentIndex(controls.size_preset_combo.findData("source_2x"))
        controls.fps_combo.setCurrentIndex(controls.fps_combo.findData("source_2x"))
        assert (window._screen_settings.width, window._screen_settings.height) == (3840, 2160)
        assert window._export_fps_value() == 60000 / 1001
        assert controls.fps_spin.decimals() == 2
        assert controls.fps_spin.value() == 59.94
        assert window._property_panel.screen_size() == (3840, 2160, 60000 / 1001)
        assert not controls.width_spin.isEnabled()
        assert not controls.fps_spin.isEnabled()

        # The persisted screen is shared by .yurika, undo and user settings.
        saved = window._current_project_data()["screen"]
        assert screen_settings_from_dict(saved) == window._screen_settings
        window._restore_screen(saved, window._current_project_data()["style"])
        assert controls.size_preset_combo.currentData() == "source_2x"
        assert controls.fps_combo.currentData() == "source_2x"
        assert window._export_fps_value() == 60000 / 1001

        window.load_video(path, replace(info, video_width=1280, video_height=720, video_fps=25))
        assert (window._screen_settings.width, window._screen_settings.height, window._export_fps_value()) == (2560, 1440, 50)
        controls.size_preset_combo.setCurrentIndex(controls.size_preset_combo.findData("custom"))
        controls.width_spin.setValue(2048)
        controls.fps_combo.setCurrentIndex(controls.fps_combo.findData("custom"))
        controls.fps_spin.setValue(47.567)
        assert controls.width_spin.isEnabled()
        assert controls.fps_spin.isEnabled()
        window.load_video(path, info)
        assert window._screen_settings.width == 2048
        assert window._export_fps_value() == 47.57
        window.set_solid_background("#000000")
        assert controls.size_preset_combo.currentData() == "custom"
        assert window._export_fps_value() == 47.57
        window.load_video(path, replace(info, video_fps=600))
        controls.fps_combo.setCurrentIndex(controls.fps_combo.findData("source_2x"))
        assert window._export_fps_value() == 1200
        assert controls.fps_spin.value() == 1200
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_legacy_project_screen_does_not_follow_replacement_video(qapp, monkeypatch, tmp_path):
    from krok_helper.models import MediaInfo
    from krok_helper.subtitle_render.settings.screen import screen_settings_from_dict

    monkeypatch.setattr(SubtitleRenderWindow, "_request_preview_window", lambda self: None)
    window = SubtitleRenderWindow(embedded=True, settings_provider=_SettingsProvider())
    try:
        legacy = screen_settings_from_dict({"width": 2560, "height": 1440, "fps": 120})
        window._screen_settings = legacy
        window._set_export_screen_controls(legacy)
        path = tmp_path / "video.mp4"
        path.write_bytes(b"fake")
        window.load_video(path, MediaInfo(path, 1, 1, 0, 0, video_width=1920, video_height=1080, video_fps=24))
        assert (window._screen_settings.width, window._screen_settings.height, window._export_fps_value()) == (2560, 1440, 120)
        assert window._export_controls.size_preset_combo.currentData() == "custom"
        assert window._export_fps_combo.currentData() == "custom"
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


@pytest.mark.parametrize("n3_import", [True, False], ids=["n3", "yurika"])
def test_project_import_canvas_policy_preserves_font_pixels(qapp, monkeypatch, tmp_path, n3_import):
    from krok_helper.models import MediaInfo
    from krok_helper.subtitle_render.domain.models import Style, style_to_dict

    path = tmp_path / "video.mp4"
    path.write_bytes(b"fake")
    info = MediaInfo(path, 1, 1, 0, 0, video_width=3840, video_height=2160, video_fps=24)
    monkeypatch.setattr(SubtitleRenderWindow, "_request_preview_window", lambda self: None)
    monkeypatch.setattr(SubtitleRenderWindow, "_probe", lambda self, *_args: info)
    data = {
        "screen": {"width": 1920, "height": 1080, "fps": 60},
        "style": style_to_dict(Style(
            font_size_px=180, font_reference_height=1080, layout_reference_height=1080,
        )),
        "background": {"kind": "video", "path": str(path)},
    }
    window = SubtitleRenderWindow(embedded=True, settings_provider=_SettingsProvider())
    try:
        if n3_import:
            window._finish_n3_import(
                SimpleNamespace(project_data=data, warnings=[]), tmp_path / "song.n3proj",
            )
        else:
            window._apply_project_data(data)
        expected_size = (3840, 2160) if n3_import else (1920, 1080)
        assert (window._screen_settings.width, window._screen_settings.height) == expected_size
        assert window._screen_settings.fps == 60
        assert window._style.font_size_px == 180
        assert window._style.font_reference_height == expected_size[1]
        assert window._style.layout_reference_height == expected_size[1]
        assert (window._export_width_spin.value(), window._export_height_spin.value()) == expected_size
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_default_screen_still_allows_transparent_export_without_video(qapp, tmp_path):
    from krok_helper.errors import ProcessingError
    from krok_helper.subtitle_render.domain.timing import TimingChar, TimingLine, TimingTrack

    window = SubtitleRenderWindow(embedded=True, settings_provider=_SettingsProvider())
    try:
        window._timing_track = TimingTrack(lines=[TimingLine(chars=[TimingChar("a", 0)], end_ms=1000)])
        combo = window._export_format_combo
        combo.setCurrentIndex(combo.findData("png_transparent"))
        window._export_dir_edit.setText(str(tmp_path))
        window._export_name_edit.setText("transparent")
        job = window._build_render_job()
        assert (job.width, job.height, job.fps) == (1920, 1080, 60)
        assert job.output_format == "png_transparent"
        combo.setCurrentIndex(combo.findData("mp4"))
        with pytest.raises(ProcessingError, match="请先选择背景源"):
            window._build_render_job()
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.mark.parametrize(
    ("view_size", "dpr", "output_size", "expected"),
    [
        (QSize(566, 275), 1.0, QSize(1920, 1080), 489),
        (QSize(566, 275), 1.25, QSize(1920, 1080), 611),
        (QSize(), 1.0, QSize(1920, 1080), 640),
        (QSize(566, 275), 0.0, QSize(1920, 1080), 640),
        (QSize(2000, 1200), 2.0, QSize(1920, 1080), 1920),
        (QSize(100, 100), 1.0, QSize(160, 90), 160),
    ],
)
def test_export_preview_width_matches_fitted_physical_pixels(
    view_size, dpr, output_size, expected
):
    assert (
        _export_preview_width(
            view_size,
            dpr,
            output_size.width(),
            output_size.height(),
        )
        == expected
    )


def test_physical_preview_size_scales_both_dimensions():
    assert _physical_preview_size(QSize(489, 275), 1.25) == QSize(611, 344)


def test_scaled_preview_pixmap_preserves_physical_pixels_and_dpr(qapp):
    image = QImage(1920, 1080, QImage.Format.Format_ARGB32)
    image.fill(0xFF336699)

    pixmap = _scaled_preview_pixmap(QPixmap.fromImage(image), QSize(489, 275), 1.25)

    assert pixmap.devicePixelRatioF() == pytest.approx(1.25)
    assert pixmap.size() == QSize(611, 344)


def test_scaled_preview_pixmap_crops_rounding_mismatch_to_fill_stage(qapp):
    frame = QPixmap(640, 362)  # ffmpeg ``-2`` can round the calculated height
    frame.fill(0xFF336699)

    pixmap = _scaled_preview_pixmap(frame, QSize(533, 300), 1.0)

    assert pixmap.size() == QSize(533, 300)


def test_export_monitor_displays_frame_at_active_screen_dpr(qapp):
    view = _ExportMonitorView()
    view.resize(489, 275)
    image = QImage(1920, 1080, QImage.Format.Format_ARGB32)
    image.fill(0xFF336699)

    view.set_frame(image)

    pixmap = view.pixmap()
    assert pixmap is not None
    assert pixmap.devicePixelRatioF() == pytest.approx(view.devicePixelRatioF())


def test_aspect_ratio_box_can_switch_to_export_ratio(qapp):
    child = QWidget()
    frame = _AspectRatioBox(child)
    frame.resize(1000, 700)
    frame.show()
    qapp.processEvents()

    frame.set_aspect_ratio(1440, 1080)
    qapp.processEvents()

    geometry = child.geometry()
    assert geometry.size() == QSize(933, 700)
    assert geometry.x() == pytest.approx(33, abs=1)
    assert geometry.y() == 0
    assert geometry.width() / geometry.height() == pytest.approx(4 / 3, rel=0.002)


def test_sync_preview_output_size_updates_export_monitor_ratio():
    calls: list[tuple[object, ...]] = []
    host = SimpleNamespace(
        _preview_panel=SimpleNamespace(
            set_output_size=lambda width, height: calls.append(("preview", width, height))
        ),
        _export_monitor_frame=SimpleNamespace(
            set_aspect_ratio=lambda width, height: calls.append(("monitor", width, height))
        ),
        _sync_export_monitor_card_size=lambda width, height: calls.append(
            ("card", width, height)
        ),
        _export_width_spin=SimpleNamespace(value=lambda: 1440),
        _export_height_spin=SimpleNamespace(value=lambda: 1080),
    )

    SubtitleRenderWindow._sync_preview_output_size(host)

    assert calls == [
        ("preview", 1440, 1080),
        ("monitor", 1440, 1080),
        ("card", 1440, 1080),
    ]


def test_export_monitor_card_fits_image_and_tracks_window_width(qapp):
    window = SubtitleRenderWindow(embedded=True)
    try:
        window._stack.setCurrentWidget(window._export_tab)
        window.show()
        for width, height in ((1280, 800), (1100, 760), (1400, 1000)):
            window.resize(width, height)
            for _ in range(3):
                qapp.processEvents()
            frame_width = window._export_monitor_frame.width()
            view_geometry = window._export_monitor_view.geometry()
            assert view_geometry.y() == 0
            assert view_geometry.width() >= frame_width * 0.95
            assert view_geometry.width() / view_geometry.height() == pytest.approx(16 / 9, rel=0.005)
            image_bottom = window._export_monitor_view.mapTo(window._export_monitor_card, QPoint()).y() + view_geometry.height()
            footer_top = window._export_format_label.y()
            assert footer_top - image_bottom == pytest.approx(window._export_monitor_layout.spacing(), abs=2)
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_export_workspace_reports_user_actions_through_its_contract(qapp):
    view = ExportWorkspaceView(
        fps_options=(60, 120),
        render_worker_options=(0, 4, 8, 12, 16),
        gpu_preview_checked=True,
        gpu_controls_visible=True,
    )
    try:
        spies = {
            "location": QSignalSpy(view.locationSettingsRequested),
            "directory": QSignalSpy(view.directoryEditingFinished),
            "browse": QSignalSpy(view.browseRequested),
            "encoder": QSignalSpy(view.encoderChanged),
            "codec": QSignalSpy(view.codecChanged),
            "start": QSignalSpy(view.startRequested),
            "stop": QSignalSpy(view.stopRequested),
        }
        controls = view.controls

        controls.location_settings_button.click()
        controls.directory_edit.editingFinished.emit()
        controls.browse_button.click()
        controls.encoder_combo.setCurrentIndex(1)
        controls.codec_combo.setCurrentIndex(1)
        controls.start_button.click()
        controls.stop_button.setEnabled(True)
        controls.stop_button.click()

        assert {name: len(spy) for name, spy in spies.items()} == {
            name: 1 for name in spies
        }
    finally:
        view.close()
        view.deleteLater()
        qapp.processEvents()


def test_export_workspace_actions_reach_window_coordinator(qapp, monkeypatch):
    calls = []
    handlers = {
        "_open_export_location_settings": "location",
        "_on_export_directory_edited": "directory",
        "_browse_export_output": "browse",
        "_start_render_export": "start",
        "_stop_render_export": "stop",
    }
    for method_name, call_name in handlers.items():
        monkeypatch.setattr(
            SubtitleRenderWindow,
            method_name,
            lambda self, name=call_name: calls.append(name),
        )

    window = SubtitleRenderWindow(
        embedded=True,
        settings_provider=_SettingsProvider(),
    )
    try:
        window._export_location_settings_button.click()
        window._export_dir_edit.editingFinished.emit()
        window._export_browse_button.click()
        window._export_start_button.click()
        window._export_stop_button.setEnabled(True)
        window._export_stop_button.click()

        assert calls == ["location", "directory", "browse", "start", "stop"]
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_export_workspace_preset_combo_syncs_with_size_fields(qapp):
    view = ExportWorkspaceView(
        fps_options=(60, 120),
        render_worker_options=(0, 4, 8, 12, 16),
        gpu_preview_checked=True,
        gpu_controls_visible=True,
    )
    try:
        controls = view.controls
        combo = controls.size_preset_combo

        # 与「CPU preset」下拉是两个控件：后者在硬编时被禁用，前者不受影响。
        assert combo.isEnabled()
        assert "常用画布格式" in combo.toolTip()
        assert "CPU preset" not in combo.toolTip()
        assert combo.currentData() == "1080p"
        combo.setCurrentIndex(combo.findData("8k"))
        assert (controls.width_spin.value(), controls.height_spin.value()) == (
            7680,
            4320,
        )

        controls.width_spin.setValue(2000)
        assert combo.currentData() == "custom"

        controls.width_spin.setValue(3840)
        controls.height_spin.setValue(2160)
        assert combo.currentData() == "4k"
    finally:
        view.close()
        view.deleteLater()
        qapp.processEvents()


def test_export_page_omits_title_block_and_initial_status(qapp):
    window = SubtitleRenderWindow(embedded=True, settings_provider=_SettingsProvider())
    try:
        assert not hasattr(window, "_export_title_label")
        assert not hasattr(window, "_export_caption_label")
        assert window._export_status_label.text() == ""
        assert (
            f"{window._export_width_spin.value()}×{window._export_height_spin.value()}"
            f" @ {window._export_fps_value()}fps"
            in window._export_format_label.text()
        )
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_export_format_label_tracks_idle_screen_controls_without_starting_export(qapp):
    window = SubtitleRenderWindow(embedded=True, settings_provider=_SettingsProvider())
    try:
        window._export_width_spin.setValue(3840)
        window._export_height_spin.setValue(2160)
        fps_index = window._export_fps_combo.findData(120)
        assert fps_index >= 0
        window._export_fps_combo.setCurrentIndex(fps_index)
        qapp.processEvents()

        assert window._export_start_button.isEnabled()
        assert "3840×2160 @ 120fps" in window._export_format_label.text()
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_export_format_label_keeps_active_job_snapshot(qapp):
    window = SubtitleRenderWindow(embedded=True, settings_provider=_SettingsProvider())
    try:
        window._export_start_button.setEnabled(False)
        window._export_format_label.setText(
            "输出格式: MP4 · H.264 (AVC) · 1920×1080 @ 60fps"
        )

        window._export_width_spin.setValue(3840)
        window._export_height_spin.setValue(2160)
        qapp.processEvents()

        assert "1920×1080 @ 60fps" in window._export_format_label.text()
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_export_cards_are_vertically_centered_above_actions(qapp):
    window = SubtitleRenderWindow(embedded=True)
    try:
        window.resize(1280, 800)
        window._stack.setCurrentWidget(window._export_tab)
        window.show()
        qapp.processEvents()

        column = window.findChild(QWidget, "SrExportColumn")
        assert column is not None
        settings_viewport = window._export_settings_col.parentWidget()
        settings_top = settings_viewport.mapTo(column, QPoint()).y()
        monitor_top = window._export_monitor_card.mapTo(column, QPoint()).y()
        progress_top = window._export_progress.mapTo(column, QPoint()).y()
        gap_below_cards = progress_top - (
            settings_top + settings_viewport.height()
        )

        # 「输出格式」下拉加入输出卡片后设置列更高，800px 窗口下上下留白收窄；
        # 只要仍然近似上下对称（gap ≈ top）就算垂直居中。VideoToolbox 改动后
        # 设置列高 669px，顶部留白压到个位数（约 6px，近乎贴顶）——经确认
        # 视觉可接受，只要求不溢出且上下近似对称。
        assert settings_top >= 0
        assert monitor_top == settings_top
        assert gap_below_cards == pytest.approx(settings_top, abs=16)
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_export_progress_tracks_frame_updates_without_animation(qapp):
    window = SubtitleRenderWindow(embedded=True)
    try:
        window.show()
        qapp.processEvents()

        window._apply_render_progress(3002, 3003)

        assert window._export_progress.value() == 3002
        assert window._export_progress.getVal() == 3002
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_export_workspace_format_combo_reports_changes(qapp):
    from krok_helper.subtitle_render.frontend.workflow.export_view import (
        EXPORT_FORMAT_CHOICES,
    )

    view = ExportWorkspaceView(
        fps_options=(60, 120),
        render_worker_options=(0, 4, 8, 12, 16),
        gpu_preview_checked=True,
        gpu_controls_visible=True,
    )
    try:
        combo = view.controls.format_combo
        assert combo.count() == len(EXPORT_FORMAT_CHOICES)
        assert [combo.itemData(i) for i in range(combo.count())] == [
            value for value, _text in EXPORT_FORMAT_CHOICES
        ]
        assert combo.currentData() == "mp4"
        assert view.controls.name_suffix_label.text() == ".mp4"

        spy = QSignalSpy(view.formatChanged)
        combo.setCurrentIndex(combo.findData("png_transparent"))

        assert len(spy) == 1
        assert combo.currentData() == "png_transparent"
    finally:
        view.close()
        view.deleteLater()
        qapp.processEvents()


def test_export_format_switch_updates_badge_encoder_state_and_label(qapp):
    window = SubtitleRenderWindow(embedded=True, settings_provider=_SettingsProvider())
    try:
        # 初始 MP4：编码参数可用，后缀徽标为 .mp4。
        assert window._export_encoder_combo.isEnabled()
        assert window._export_codec_combo.isEnabled()
        assert window._export_preset_combo.isEnabled()
        assert window._export_crf_spin.isEnabled()
        assert window._export_name_suffix_label.text() == ".mp4"

        combo = window._export_format_combo
        combo.setCurrentIndex(combo.findData("png_transparent"))
        qapp.processEvents()
        assert window._export_name_suffix_label.text() == "\\ PNG 序列文件夹"
        assert not window._export_encoder_combo.isEnabled()
        assert not window._export_codec_combo.isEnabled()
        assert not window._export_preset_combo.isEnabled()
        assert not window._export_crf_spin.isEnabled()
        assert "PNG 序列（透明字幕）" in window._export_format_label.text()

        combo.setCurrentIndex(combo.findData("mov_transparent"))
        qapp.processEvents()
        assert window._export_name_suffix_label.text() == ".mov"
        assert "ProRes 4444" in window._export_format_label.text()

        # 切回 MP4 后编码控件恢复可用。
        combo.setCurrentIndex(combo.findData("mp4"))
        qapp.processEvents()
        assert window._export_encoder_combo.isEnabled()
        assert window._export_name_suffix_label.text() == ".mp4"
        assert "MP4 · H.264" in window._export_format_label.text()
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_nearest_existing_directory_passes_an_existing_folder_through(tmp_path):
    assert nearest_existing_directory(tmp_path) == str(tmp_path)


def test_nearest_existing_directory_climbs_from_a_missing_folder(tmp_path):
    """Windows 原生对话框对不存在的起始目录会静默回退到「上次访问目录」。"""
    dead = tmp_path / "deleted" / "deeper"
    assert nearest_existing_directory(dead) == str(tmp_path)


def test_nearest_existing_directory_uses_the_parent_of_a_file(tmp_path):
    target = tmp_path / "song.mp4"
    target.write_text("x", encoding="utf-8")
    assert nearest_existing_directory(target) == str(tmp_path)


def test_nearest_existing_directory_survives_a_missing_drive():
    result = nearest_existing_directory("Q:/missing/folder")
    assert Path(result).is_dir()


def test_nearest_existing_directory_falls_back_to_home_when_empty():
    assert nearest_existing_directory("") == str(Path.home())
    assert nearest_existing_directory("   ") == str(Path.home())


def test_browse_export_output_starts_from_nearest_existing_directory(
    qapp, monkeypatch, tmp_path
):
    """输出目录显示的路径在磁盘上已失效时，「浏览」从最近现存祖先目录打开。"""
    captured = {}
    monkeypatch.setattr(
        QFileDialog,
        "getExistingDirectory",
        staticmethod(
            lambda parent, title, start: (captured.update(start=start), "")[1]
        ),
    )
    window = SubtitleRenderWindow(
        embedded=True,
        settings_provider=_SettingsProvider(),
    )
    try:
        window._export_dir_edit.setText(str(tmp_path / "deleted" / "deeper"))
        window._browse_export_output()
        assert captured["start"] == str(tmp_path)
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_export_location_dialog_browses_from_nearest_existing_ancestor(
    qapp, monkeypatch, tmp_path
):
    """「导出视频位置与命名」设置对话框里的浏览同样不能从失效目录起跳。"""
    captured = {}
    monkeypatch.setattr(
        QFileDialog,
        "getExistingDirectory",
        staticmethod(
            lambda parent, title, start: (captured.update(start=start), "")[1]
        ),
    )
    dialog = _ExportLocationDialog(
        "custom", str(tmp_path / "gone" / "deeper"), tmp_path, None
    )
    dialog._browse()
    assert captured["start"] == str(tmp_path)


@pytest.mark.parametrize("encoders,h264_label", [
    ({"h264_videotoolbox"}, "平均码率（Mbps）"),
    (None, "质量值"),
    ({"h264_videotoolbox", "h264_qsv", "hevc_videotoolbox"}, "质量值"),
])
def test_auto_quality_controls_follow_available_encoders_and_codec(
    qapp, monkeypatch, encoders, h264_label,
):
    """自动硬编按当前格式的可用编码器显示码率或质量值，FFmpeg 缺失时显示质量值。"""
    from krok_helper.subtitle_render.engine.export import encoder_select
    from krok_helper.subtitle_render.frontend import main_window

    def find_ffmpeg(*_args):
        if encoders is None:
            from krok_helper.errors import ProcessingError
            raise ProcessingError("找不到 ffmpeg。")
        return "ffmpeg"

    monkeypatch.setattr(main_window, "find_tool", find_ffmpeg)
    monkeypatch.setattr(encoder_select, "_available_encoders", lambda _: frozenset(encoders or ()))
    window = SubtitleRenderWindow(embedded=True, settings_provider=_SettingsProvider())
    try:
        controls = window._export_controls
        controls.encoder_combo.setCurrentIndex(controls.encoder_combo.findData("auto"))
        assert controls.quality_label.text() == h264_label
        assert controls.bitrate_spin.isHidden() == (h264_label == "质量值")
        # 切换到 HEVC 后，输入框随该格式的可用编码器更新。
        controls.codec_combo.setCurrentIndex(controls.codec_combo.findData("hevc"))
        expected_label = "平均码率（Mbps）" if "hevc_videotoolbox" in (encoders or ()) else "质量值"
        assert controls.quality_label.text() == expected_label
        assert controls.crf_spin.isHidden() == (expected_label == "平均码率（Mbps）")
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_video_bitrate_round_trips_through_preferences_and_project(qapp):
    """检查质量值与码率分别保留，并能经过偏好、工程和新建工程流程恢复。"""
    settings = _SettingsProvider()
    settings.data = {"output": {"encoder_mode": "videotoolbox", "crf": 22, "bitrate_mbps": 35}}
    window = SubtitleRenderWindow(embedded=True, settings_provider=settings)
    try:
        assert window._export_bitrate_spin.value() == 35
        assert window._export_controls.quality_label.text() == "平均码率（Mbps）"
        window._export_bitrate_spin.setValue(28)
        controls = window._export_controls
        # CPU 显示质量值，VideoToolbox 显示码率，切换后各自保留原值。
        controls.encoder_combo.setCurrentIndex(controls.encoder_combo.findData("cpu"))
        assert controls.quality_label.text() == "质量值"
        assert controls.crf_spin.value() == 22
        assert controls.bitrate_spin.isHidden()
        controls.encoder_combo.setCurrentIndex(controls.encoder_combo.findData("videotoolbox"))
        assert controls.quality_label.text() == "平均码率（Mbps）"
        assert controls.bitrate_spin.value() == 28
        assert controls.crf_spin.isHidden()
        window._save_persisted_state()
        output = window._current_project_data()["output"]
        assert output["bitrate_mbps"] == 28
        assert output["crf"] == 22
        assert settings.data["output"]["bitrate_mbps"] == 28
        # 缺少码率字段时使用默认值，加载带有该字段的工程时恢复保存值。
        window._apply_output_settings({"encoder_mode": "cpu", "crf": 18})
        assert window._export_bitrate_spin.value() == 10
        window._apply_output_settings(output)
        assert window._export_bitrate_spin.value() == 28
        assert window._export_crf_spin.value() == 22
        window._reset_export_settings_for_new_project()
        assert window._export_bitrate_spin.value() == 28
        assert window._export_controls.quality_label.text() == "平均码率（Mbps）"
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_background_canvas_source_modes_sync_export_and_restore(qapp, monkeypatch, tmp_path):
    from krok_helper.models import MediaInfo

    monkeypatch.setattr(SubtitleRenderWindow, "_request_preview_window", lambda self: None)
    window = SubtitleRenderWindow(embedded=True, settings_provider=_SettingsProvider())
    try:
        panel = window._property_panel
        size = panel._screen_size_preset_combo
        fps = panel._screen_size_fps_combo
        assert not size.items[size.findData("source")].isEnabled
        path = tmp_path / "video.mp4"
        path.write_bytes(b"fake")
        window.load_video(path, MediaInfo(path, 1, 1, 0, 0, video_width=1920, video_height=1080, video_fps=30000 / 1001))
        assert panel.screen_modes() == ("source", "source")
        size.setCurrentIndex(size.findData("source_2x"))
        fps.setCurrentIndex(fps.findData("source_2x"))
        assert panel.screen_size() == (3840, 2160, 60000 / 1001)
        assert window._export_controls.size_preset_combo.currentData() == "source_2x"
        assert window._export_fps_value() == 60000 / 1001
        saved = window._current_project_data()
        size.setCurrentIndex(size.findData("720p"))
        assert panel.screen_size() == (1280, 720, 60000 / 1001)
        assert window._screen_settings.resolution_mode == "720p"
        window._restore_screen(saved["screen"], saved["style"])
        assert panel.screen_modes() == ("source_2x", "source_2x")
        size.setCurrentIndex(size.findData("custom"))
        panel._screen_size_width_spin.setValue(2048)
        fps.setCurrentIndex(fps.findData("custom"))
        panel._screen_size_fps_spin.setValue(47.57)
        assert window._screen_settings.width == 2048
        assert window._export_fps_value() == 47.57
        assert panel._screen_size_fps_spin.isEnabled()
        panel._screen_size_fps_spin.lineEdit().setText("48.25 fps")
        assert window._current_project_data()["screen"]["fps"] == 48.25
        window._export_fps_combo.setCurrentIndex(window._export_fps_combo.findData(120))
        assert panel.screen_size()[2] == 120
        window.set_solid_background("#000000")
        assert not size.items[size.findData("source")].isEnabled
        assert not fps.items[fps.findData("source")].isEnabled
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()
