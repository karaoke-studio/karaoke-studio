"""波形对齐页的状态初始化与设置读写。

这四段原本散在外壳的 ``__init__`` / ``_load_settings_into_ui`` /
``_save_all_settings`` / ``_bind_shortcuts`` 里，先收成命名入口，再随页面
对象化一起挪到 ``AlignmentPage`` 上。这里钉的是行为：两次搬迁都不该改变它们。
"""

from __future__ import annotations

from types import SimpleNamespace

from PyQt6.QtCore import QObject

import pytest

from krok_helper.alignment.page import AlignmentPage
from krok_helper.settings import (
    ALIGN_OUTPUT_DIR_CUSTOM,
    ALIGN_OUTPUT_DIR_SOURCE_VIDEO,
    ALIGN_TARGET_AUDIO,
    ALIGN_TARGET_VIDEO,
    AppSettings,
    ENCODE_MODE_HARDWARE,
    ENCODE_MODE_SOFTWARE,
)


class _PageStub(QObject):
    """够用的页面替身。

    ``_init_alignment_state`` 会建 ``QTimer(self)``（预览定时器归页面），所以必须
    是 QObject；配置从 ``self._host.settings`` 读，所以要有一个 ``_host``。
    """

    def __init__(self, settings: AppSettings) -> None:
        super().__init__()
        self._host = SimpleNamespace(settings=settings)

    def _poll_alignment_preview(self) -> None: ...


class _Toggle:
    """够用的 radio / checkbox 替身。"""

    def __init__(self, checked: bool = False) -> None:
        self._checked = checked

    def setChecked(self, value: bool) -> None:  # noqa: N802
        self._checked = bool(value)

    def isChecked(self) -> bool:  # noqa: N802
        return self._checked


def _page_host(settings: AppSettings) -> SimpleNamespace:
    return SimpleNamespace(
        _host=SimpleNamespace(settings=settings),
        _restoring_alignment_settings=False,
        align_target_audio_radio=_Toggle(),
        align_target_video_radio=_Toggle(),
        align_encode_hardware_radio=_Toggle(),
        align_encode_software_radio=_Toggle(),
        align_force_1080p60_check=_Toggle(),
        align_use_video_audio_check=_Toggle(),
    )


class TestInitAlignmentState:
    def test_every_alignment_attribute_starts_empty(self) -> None:
        host = _PageStub(AppSettings())

        AlignmentPage._init_alignment_state(host)

        assert host.align_analysis_task is None
        assert host.align_auto_task is None
        assert host.align_export_task is None
        assert host.align_preview_process is None
        assert host.align_preview_started_at == 0.0
        assert host._align_export_expected_outputs == []
        assert host._alignment_handoff_dialog is None
        assert host._alignment_handoff_payload is None
        assert host.align_control_panel is None
        assert host.align_output_custom_dir_text == ""

    def test_encode_mode_falls_back_when_settings_are_garbage(self) -> None:
        host = _PageStub(AppSettings(align_encode_mode="nonsense"))

        AlignmentPage._init_alignment_state(host)

        assert host._align_encode_selection == ENCODE_MODE_SOFTWARE

    def test_encode_mode_is_kept_when_valid(self) -> None:
        host = _PageStub(AppSettings(align_encode_mode=ENCODE_MODE_HARDWARE))

        AlignmentPage._init_alignment_state(host)

        assert host._align_encode_selection == ENCODE_MODE_HARDWARE


class TestLoadAlignmentSettings:
    def test_target_audio_ticks_the_audio_radio(self) -> None:
        host = _page_host(AppSettings(align_target=ALIGN_TARGET_AUDIO))

        AlignmentPage._load_alignment_settings(host)

        assert host.align_target_audio_radio.isChecked()
        assert not host.align_target_video_radio.isChecked()

    def test_anything_but_audio_falls_back_to_video(self) -> None:
        host = _page_host(AppSettings(align_target="nonsense"))

        AlignmentPage._load_alignment_settings(host)

        assert host.align_target_video_radio.isChecked()

    def test_custom_output_dir_is_stripped(self) -> None:
        host = _page_host(
            AppSettings(
                align_output_dir_mode=ALIGN_OUTPUT_DIR_CUSTOM,
                align_output_custom_dir="  D:/tmp  ",
            )
        )

        AlignmentPage._load_alignment_settings(host)

        assert host.align_output_dir_mode_value == ALIGN_OUTPUT_DIR_CUSTOM
        assert host.align_output_custom_dir_text == "D:/tmp"

    def test_invalid_output_dir_mode_falls_back(self) -> None:
        host = _page_host(AppSettings(align_output_dir_mode="nonsense"))

        AlignmentPage._load_alignment_settings(host)

        assert host.align_output_dir_mode_value == ALIGN_OUTPUT_DIR_SOURCE_VIDEO

    def test_empty_name_templates_fall_back_to_defaults(self) -> None:
        host = _page_host(AppSettings(align_video_name_template="", align_audio_name_template=""))

        AlignmentPage._load_alignment_settings(host)

        assert host.align_video_name_template_value
        assert host.align_audio_name_template_value

    @pytest.mark.parametrize("mode", [ENCODE_MODE_HARDWARE, ENCODE_MODE_SOFTWARE])
    def test_encode_radio_follows_the_mode(self, mode: str) -> None:
        host = _page_host(AppSettings(align_encode_mode=mode))

        AlignmentPage._load_alignment_settings(host)

        assert host.align_encode_hardware_radio.isChecked() == (mode == ENCODE_MODE_HARDWARE)
        assert host.align_encode_software_radio.isChecked() == (mode == ENCODE_MODE_SOFTWARE)

    def test_boolean_preferences_reach_their_checkboxes(self) -> None:
        host = _page_host(AppSettings(align_force_1080p60=True, align_export_use_video_audio=True))

        AlignmentPage._load_alignment_settings(host)

        assert host.align_force_1080p60_check.isChecked()
        assert host.align_use_video_audio_check.isChecked()


class TestCollectAlignmentSettings:
    def test_name_templates_are_written_back(self) -> None:
        settings = AppSettings()
        host = SimpleNamespace(
            _host=SimpleNamespace(settings=settings),
            align_video_name_template_value="{video_name}_v",
            align_audio_name_template_value="{audio_name}_a",
        )

        AlignmentPage._collect_alignment_settings(host)

        assert settings.align_video_name_template == "{video_name}_v"
        assert settings.align_audio_name_template == "{audio_name}_a"


def test_settings_round_trip_through_both_seams() -> None:
    """读 -> 写 -> 再读，取值必须稳定。"""
    settings = AppSettings(
        align_target=ALIGN_TARGET_VIDEO,
        align_video_name_template="{video_name}_x",
        align_audio_name_template="{audio_name}_y",
    )
    host = _page_host(settings)

    AlignmentPage._load_alignment_settings(host)
    AlignmentPage._collect_alignment_settings(host)

    assert settings.align_video_name_template == "{video_name}_x"
    assert settings.align_audio_name_template == "{audio_name}_y"


def test_alignment_shortcuts_are_bound_separately(qtbot_free_window) -> None:
    """三个只在对齐页有意义的快捷键归自己的入口；跨模块的 Ctrl+S 留在外壳。"""
    window = qtbot_free_window

    AlignmentPage._bind_alignment_shortcuts(window)

    assert {s.key().toString() for s in (window.shortcut_space, window.shortcut_auto, window.shortcut_drag_mode)} == {
        "Space",
        "Ctrl+D",
        "Alt+V",
    }


@pytest.fixture
def qtbot_free_window():
    from PyQt6.QtWidgets import QWidget

    class _Host(QWidget):
        def _handle_align_space_shortcut(self) -> None: ...

        def _handle_align_auto_shortcut(self) -> None: ...

        def _handle_align_drag_mode_shortcut(self) -> None: ...

    host = _Host()
    yield host
    host.deleteLater()
