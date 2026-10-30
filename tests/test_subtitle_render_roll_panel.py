"""前黑/后黑属性页构建与信号测试。"""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication

from krok_helper.subtitle_render.domain.background import BackgroundSource
from krok_helper.subtitle_render.domain.roll import (
    ROLL_FILL_TILE,
    ROLL_MAIN_TRANSITION_CROSSFADE,
    RollSegmentConfig,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _panel(qapp):
    from krok_helper.subtitle_render.frontend.properties.property_panel import (
        PropertyPanel,
    )

    return PropertyPanel()


def test_rolls_source_duration_hint_without_touching_duration(qapp):
    panel = _panel(qapp)
    pre = RollSegmentConfig(
        enabled=True,
        source=BackgroundSource(kind="video", path="D:/intro.mp4"),
        source_duration_ms=65_500,
        duration_ms=3000,
    )
    panel.set_rolls_state(pre, None)
    hint = panel._roll_duration_hints["pre"]["video"]
    assert not hint.isHidden()
    assert hint.text() == "【素材时长： 00:01:05.500（65.500秒）】"
    # 用户填写的持续时长保持不变。
    assert panel._roll_duration_spins["pre"].value() == 3.0
    # 静态图片/纯色无固有时长 → 不显示提示。
    panel.set_rolls_state(
        RollSegmentConfig(
            enabled=True,
            source=BackgroundSource(kind="image", path="D:/pic.png"),
            source_duration_ms=0,
        ),
        None,
    )
    assert panel._roll_duration_hints["pre"]["image"].isHidden()
    assert panel._roll_duration_hints["pre"]["image"].text() == ""


def test_roll_defaults_dialog_round_trips_values(qapp):
    from krok_helper.subtitle_render.frontend.dialogs.roll_defaults import (
        RollDefaultsDialog,
    )

    dialog = RollDefaultsDialog(
        {
            "duration_ms": 4200,
            "fill": "tile",
            "main_transition_style": "crossfade",
            "main_transition_ms": 500,
            "self_transition_style": "white",
            "self_transition_ms": 200,
            "auto_offset_subtitles": False,
        }
    )
    try:
        values = dialog.selection()
        assert values["duration_ms"] == 4200
        assert values["fill"] == "tile"
        assert values["main_transition_style"] == "crossfade"
        assert values["main_transition_ms"] == 500
        assert values["self_transition_style"] == "white"
        assert values["self_transition_ms"] == 200
        assert values["auto_offset_subtitles"] is False
    finally:
        dialog.deleteLater()


def test_rolls_page_defaults_match_product_spec(qapp):
    panel = _panel(qapp)
    assert panel._roll_duration_spins["pre"].value() == 3.0
    assert panel._roll_duration_spins["post"].value() == 3.0
    assert panel._roll_main_transition_spins["pre"].value() == 300
    assert panel._roll_main_transition_spins["post"].value() == 300
    assert panel._roll_self_transition_spins["pre"].value() == 300
    assert panel._roll_self_transition_spins["post"].value() == 300
    assert panel._roll_fill_combos["pre"].currentData() == "fit"
    assert panel._roll_main_transition_combos["pre"].currentData() == "crossfade"
    assert panel._roll_main_transition_combos["post"].currentData() == "crossfade"


def test_rolls_page_registered_in_navigation(qapp):
    panel = _panel(qapp)
    assert ("rolls", "前黑/后黑") in panel._PAGE_SPECS


def test_roll_config_change_signals(qapp):
    panel = _panel(qapp)
    emitted: list[tuple[str, dict]] = []
    panel.rollConfigChanged.connect(lambda key, changes: emitted.append((key, dict(changes))))
    panel._roll_duration_spins["pre"].setValue(2.5)
    assert emitted[-1] == ("pre", {"duration_ms": 2500})
    panel._roll_main_transition_spins["post"].setValue(400)
    assert emitted[-1] == ("post", {"main_transition_ms": 400})
    panel._roll_fill_combos["pre"].setCurrentIndex(1)  # 平铺
    assert emitted[-1] == ("pre", {"fill": "tile"})
    panel._on_roll_kind_pill_changed("pre", "solid")
    assert emitted[-1] == ("pre", {"source_kind": "solid"})
    # 总开关。
    panel._roll_enabled_switches["post"].setChecked(True)
    assert emitted[-1] == ("post", {"enabled": True})
    # 自动偏移开关只在前黑段存在。
    panel._roll_auto_offset_switch.setChecked(False)
    assert emitted[-1] == ("pre", {"auto_offset_subtitles": False})


def test_roll_browse_and_audio_signals(qapp):
    panel = _panel(qapp)
    browse: list[tuple[str, str]] = []
    audio: list[str] = []
    panel.rollBrowseRequested.connect(lambda key, kind: browse.append((key, kind)))
    panel.rollAudioBrowseRequested.connect(audio.append)
    panel.rollAudioClearRequested.connect(audio.append)
    panel._roll_path_edits["pre"]["video"].parentWidget()  # 控件已构建
    # 段内 kind 胶囊切换会发 source_kind。
    panel._on_roll_segment_pill_changed("post")
    assert panel._roll_segment_stack.currentIndex() == 1


def test_set_rolls_state_round_trip(qapp):
    panel = _panel(qapp)
    pre = RollSegmentConfig(
        enabled=True,
        source=BackgroundSource(kind="video", path="D:/intro.mp4"),
        source_has_audio=True,
        audio_path="D:/intro.wav",
        duration_ms=3000,
        fill=ROLL_FILL_TILE,
        main_transition_ms=500,
        main_transition_style=ROLL_MAIN_TRANSITION_CROSSFADE,
        self_transition_ms=200,
    )
    post = RollSegmentConfig(
        enabled=True,
        source=BackgroundSource(kind="solid", color="#305070"),
        duration_ms=1500,
    )
    emitted: list[tuple[str, dict]] = []
    panel.rollConfigChanged.connect(lambda key, changes: emitted.append((key, dict(changes))))
    panel.set_rolls_state(pre, post)
    assert emitted == []  # 程序回填不发信号
    assert panel._roll_duration_spins["pre"].value() == 3.0
    assert panel._roll_enabled_switches["post"].isChecked()
    assert panel._roll_fill_combos["pre"].currentData() == "tile"
    assert panel._roll_main_transition_spins["pre"].value() == 500
    assert panel._roll_main_transition_combos["pre"].currentData() == "crossfade"
    assert panel._roll_audio_edits["pre"].text() == "D:/intro.wav"
    assert panel._roll_auto_offset_switch.isChecked()
