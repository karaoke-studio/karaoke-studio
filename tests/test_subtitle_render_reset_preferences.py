"""「文件管理 → 恢复默认偏好…」：应用级习惯记忆回到出厂默认。

等价于清空 settings.json 的 ``subtitle_render`` 命名空间（保留最近打开列表
与样式预设库）后重新装载：习惯记忆回出厂，当前工程的内容（样式、布局、
标题、画面尺寸）不被修改。样式预设库是跨工程积累的内容库，不属于习惯：
原样保留；布局预设库随重置回出厂全集（2026-10 口径：出厂预设经「恢复
默认设置」找回）；按行数记住的「软件默认布局」指向恢复出厂。
"""

from __future__ import annotations

import os
import sys
from dataclasses import replace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from krok_helper.subtitle_render.frontend.main_window import (  # noqa: E402
    SubtitleRenderWindow,
)
from krok_helper.subtitle_render.frontend.dialogs.guide_replacement import (  # noqa: E402
    last_bitmap_settings,
)
from krok_helper.subtitle_render.domain.models import (  # noqa: E402
    DEFAULT_EXPORT_NAME_TEMPLATE,
    DEFAULT_LAYOUT_BY_ROW_COUNT,
    LyricsLayout,
    Style,
    StylePreset,
    TitleOverlay,
    style_to_dict,
)


class _Recorder:
    """一份留在内存里的 subtitle_render 设置命名空间。"""

    def __init__(self) -> None:
        self.data: dict = {}

    def load(self) -> dict:
        return dict(self.data)

    def save(self, data: dict) -> None:
        self.data = dict(data)


@pytest.fixture
def settings() -> _Recorder:
    return _Recorder()


@pytest.fixture
def make_window(settings):
    app = QApplication.instance() or QApplication([])
    built: list[SubtitleRenderWindow] = []

    def factory() -> SubtitleRenderWindow:
        widget = SubtitleRenderWindow.for_embedding(settings_provider=settings)
        built.append(widget)
        return widget

    yield factory
    for widget in built:
        widget.close()
        widget.deleteLater()
    app.processEvents()


@pytest.fixture
def confirm(monkeypatch):
    """替身确认弹窗；传入 False 模拟用户取消。"""

    def stub(result: bool) -> None:
        monkeypatch.setattr(
            "krok_helper.subtitle_render.frontend.main_window.fluent_question",
            lambda *args, **kwargs: result,
        )

    return stub


_CUSTOM_LAYOUT = LyricsLayout(
    name="我的三行布局",
    layout_id="custom-3",
    line_y_position="top",
    line_y_margin_px=40,
    line_gap_px=60,
    horizontal_margin_px=30,
    line_alignments=["left", "center", "right"],
)


def _polluted_payload(recent_project: str) -> dict:
    """把各类记忆都污染成非出厂值，验证重置逐类生效。"""

    polluted_style = replace(
        Style(),
        layouts=[*Style().layouts, _CUSTOM_LAYOUT],
        default_layout_by_row_count={3: "custom-3"},
        layout_reference_height=1440,
    )
    return {
        "recent_projects": [recent_project],
        "style": style_to_dict(polluted_style),
        "style_presets": [
            {
                "id": "preset-1",
                "name": "我的预设",
                "scheme": {"base_color": "#123456"},
            }
        ],
        "new_project_defaults": {
            "title_enabled": True,
            "title_fades": {"fade_in_ms": 250, "fade_out_ms": 180},
            "title_timing": {"show_mode": "whole", "head_offset_ms": 500},
        },
        "auto_chorus": {
            "role": "和声",
            "begin_chars": "【",
            "end_chars": "】",
            "overwrite": True,
            "auto_apply": False,
        },
        "guide_replacement": {"marker": "→", "non_prefix": True},
        "selected_scheme_key": "custom:标题",
        "preview_splitter_ratio": 0.7,
        "auto_save": {"enabled": False, "interval_minutes": 30},
        "backup": {"history_count": 12},
        "output": {
            "directory_mode": "custom",
            "custom_directory": r"D:\成片",
            "name_template": "{video_name}_重制",
            "encoder_mode": "nvenc",
            "codec": "hevc",
            "preset": "slow",
            "crf": 23,
            "bitrate_mbps": 45,
            "render_workers": 4,
            "output_format": "prores",
            "preview_quality": "low",
            "gpu_preview_enabled": True,
            "gpu_preview_default_version": 2,
            "gpu_export_enabled": False,
            "gpu_export_default_version": 1,
        },
    }


def test_reset_restores_every_memory_category(
    make_window, settings, confirm, tmp_path
) -> None:
    recent = tmp_path / "某首歌.yurika"
    recent.write_text("{}", encoding="utf-8")
    settings.data = _polluted_payload(str(recent))
    window = make_window()

    confirm(True)
    window._reset_app_preferences()

    # 各类习惯记忆回出厂；最近打开列表、样式预设库与软件布局库保留。
    assert settings.data["recent_projects"] == [str(recent)]
    assert settings.data["style_presets"] == [
        {
            "id": "preset-1",
            "name": "我的预设",
            "scheme": {"base_color": "#123456"},
        }
    ]
    assert settings.data["new_project_defaults"]["title_fades"] == {
        name: getattr(TitleOverlay(), name)
        for name in (
            "fade_in_ms",
            "fade_out_ms",
            "tail_fade_in_ms",
            "tail_fade_out_ms",
        )
    }
    assert settings.data["new_project_defaults"]["title_timing"]["show_mode"] == (
        TitleOverlay().show_mode
    )
    assert settings.data["auto_chorus"] == {
        "role": "",
        "begin_chars": "（(",
        "end_chars": "）)",
        "overwrite": False,
        "auto_apply": True,
    }
    assert "guide_replacement" not in settings.data
    assert settings.data["selected_scheme_key"] == "global"
    assert window._selected_scheme_key == "global"
    assert window._property_panel.current_scheme_key() == "global"
    assert settings.data["preview_splitter_ratio"] == pytest.approx(0.4)
    assert settings.data["auto_save"] == {"enabled": True, "interval_minutes": 5}
    assert settings.data["backup"]["history_count"] == 5
    output = settings.data["output"]
    assert output["directory_mode"] == "source_video"
    assert output["custom_directory"] == ""
    assert output["name_template"] == DEFAULT_EXPORT_NAME_TEMPLATE
    assert output["encoder_mode"] == "cpu"
    assert output["codec"] == "h264"
    assert output["preset"] == "medium"
    assert output["crf"] == 18
    # 恢复默认后，保存的码率和当前控件均为 10 Mbps。
    assert output["bitrate_mbps"] == 10
    assert window._export_bitrate_spin.value() == 10
    assert output["render_workers"] == 0
    assert output["output_format"] == "mp4"
    assert output["preview_quality"] == "high"
    assert output["gpu_export_enabled"] is (sys.platform == "win32")
    assert last_bitmap_settings() == {}
    assert window._app_default_style.title_overlays[0].fade_in_ms == (
        TitleOverlay().fade_in_ms
    )
    # 预设库在内存与面板两侧都原样保留。
    assert set(window._style_presets) == {"preset-1"}
    assert set(window._property_panel.preset_schemes) == {"preset-1"}


def test_reset_restores_layout_presets_to_factory(
    make_window, settings, confirm, tmp_path
) -> None:
    settings.data = _polluted_payload(str(tmp_path / "skip.yurika"))
    window = make_window()
    # 装载端会把行数映射补全为 1..8 全量字典；种子只改了 3 行那一档。
    assert window._app_default_style.default_layout_by_row_count[3] == "custom-3"
    assert window._app_default_style.layout_reference_height == 1440
    # 首次装载触发一次性升级迁移：旧隐形库（含自定义「我的三行布局」）
    # 全量转入预设库并全部默认加载。
    assert any(
        preset.layout.name == "我的三行布局" for preset in window._layout_presets
    )
    assert all(preset.default_load for preset in window._layout_presets)

    confirm(True)
    window._reset_app_preferences()

    # 按行数记住的「软件默认布局」指向回出厂；布局预设库随重置回出厂
    # 全集 + 全默认加载（自定义条目移除），且迁移闸门不重放。
    assert window._app_default_style.default_layout_by_row_count == (
        DEFAULT_LAYOUT_BY_ROW_COUNT
    )
    assert [preset.layout.layout_id for preset in window._layout_presets] == [
        "title-default",
        "builtin-1",
        "builtin-2",
        "builtin-3",
        "builtin-4",
        "builtin-5",
        "builtin-6",
        "builtin-7",
        "builtin-8",
    ]
    assert all(preset.default_load for preset in window._layout_presets)
    persisted = settings.data["layout_presets"]
    assert not any(
        row.get("layout", {}).get("name") == "我的三行布局" for row in persisted
    )
    assert settings.data["layout_presets_migrated"] is True


def test_a_new_instance_after_reset_starts_from_factory(
    make_window, settings, confirm, tmp_path
) -> None:
    settings.data = _polluted_payload(str(tmp_path / "skip.yurika"))
    first = make_window()
    confirm(True)
    first._reset_app_preferences()

    second = make_window()

    title = second._app_default_style.title_overlays[0]
    assert title.fade_in_ms == TitleOverlay().fade_in_ms
    assert title.show_mode == TitleOverlay().show_mode
    assert second._auto_chorus_role == ""
    assert second._export_name_template == DEFAULT_EXPORT_NAME_TEMPLATE
    assert second._export_dir_mode == "source_video"
    assert second._auto_save_enabled is True
    assert second._preview_splitter_ratio == pytest.approx(0.4)
    # 内容库跨实例保留：样式预设库仍在；布局预设库按重置口径保持出厂
    # 全集（自定义条目不随重置回来）。
    assert set(second._style_presets) == {"preset-1"}
    assert "我的三行布局" not in [
        preset.layout.name for preset in second._layout_presets
    ]
    assert second._app_default_style.default_layout_by_row_count == (
        DEFAULT_LAYOUT_BY_ROW_COUNT
    )


def test_the_open_project_content_survives_the_reset(
    make_window, settings, confirm
) -> None:
    window = make_window()
    title = (window._style.title_overlays or [TitleOverlay()])[0]
    window._property_panel.set_style(
        replace(
            window._style,
            entry_anim="slide",
            sing_fx="sakura",
            fill_gradient_enabled=True,
            fill_gradient_start_color="#FF0000",
            fill_gradient_end_color="#0000FF",
            title_overlays=[
                replace(
                    title,
                    enabled=True,
                    fade_in_ms=250,
                    show_mode="whole",
                    text_template="歌名",
                )
            ],
        ),
        emit=True,
    )
    QApplication.instance().processEvents()
    canvas_before = window._screen_settings

    confirm(True)
    window._reset_app_preferences()

    # 逐曲内容保留：标题文字、画面尺寸不动。
    project_title = window._style.title_overlays[0]
    assert project_title.text_template == "歌名"
    assert window._screen_settings == canvas_before
    # 标题的习惯字段（淡入淡出 / 显示时段）回出厂——它们是习惯不是逐曲
    # 内容，重置后卡片上就应该是出厂值。
    assert project_title.fade_in_ms == TitleOverlay().fade_in_ms
    assert project_title.show_mode == TitleOverlay().show_mode
    # 通用样式（动画 / 唱字特效 / legacy 渐变）真正回到出厂：面板看到的
    # 就是默认值，下一次保存也不会把旧值写回记忆。
    assert window._style.entry_anim == Style().entry_anim
    assert window._style.sing_fx == Style().sing_fx
    assert window._style.fill_gradient_enabled == Style().fill_gradient_enabled
    assert settings.data["style"]["entry_anim"] == Style().entry_anim
    assert settings.data["style"]["sing_fx"] == Style().sing_fx
    assert settings.data["style"]["fill_gradient_enabled"] is False
    # 标题习惯这类显式记忆不回流。
    assert settings.data["new_project_defaults"]["title_fades"]["fade_in_ms"] == (
        TitleOverlay().fade_in_ms
    )


def test_cancelling_the_dialog_changes_nothing(
    make_window, settings, confirm, tmp_path
) -> None:
    settings.data = _polluted_payload(str(tmp_path / "skip.yurika"))
    window = make_window()
    before = dict(settings.data)

    confirm(False)
    window._reset_app_preferences()

    assert settings.data == before
    assert window._app_default_style.title_overlays[0].fade_in_ms == 250
    assert window._auto_chorus_role == "和声"


def test_reset_keeps_the_preset_library_seen_by_the_panel(
    make_window, settings, confirm, tmp_path
) -> None:
    settings.data = _polluted_payload(str(tmp_path / "skip.yurika"))
    window = make_window()
    assert any(
        isinstance(preset, StylePreset) for preset in window._style_presets.values()
    )

    confirm(True)
    window._reset_app_preferences()

    assert set(window._style_presets) == {"preset-1"}
    assert set(window._property_panel.preset_schemes) == {"preset-1"}


def test_reset_resolves_title_layout_against_preserved_library(
    make_window, settings, confirm, tmp_path
) -> None:
    """库首不是タイトル左上（N3 工作流积累）时，重置不能把标题布局指错。

    回归：应用默认标题的 layout_index 曾按出厂布局表算好（恒为 1）再换入
    保留布局库，库首是「下寄せ1行」时新工程/新条目的标题全部指到它。
    """
    from krok_helper.subtitle_render.domain.models import (
        LyricsLayout,
        style_to_dict,
    )

    polluted = _polluted_payload(str(tmp_path / "skip.yurika"))
    polluted["style"] = style_to_dict(
        replace(
            Style(),
            layouts=[
                LyricsLayout(
                    name="下寄せ1行",
                    line_y_position="bottom",
                    line_alignments=["center"],
                ),
                LyricsLayout(
                    name="下寄せ2行",
                    line_y_position="bottom",
                    line_alignments=["left", "right"],
                ),
            ],
        )
    )
    settings.data = polluted
    window = make_window()
    # 工程里标题条目也带着旧习惯：显示时段=全程、布局指向库首。
    title = (window._style.title_overlays or [TitleOverlay()])[0]
    window._property_panel.set_style(
        replace(
            window._style,
            title_overlays=[
                replace(title, enabled=True, show_mode="whole", layout_index=1)
            ],
        ),
        emit=True,
    )
    QApplication.instance().processEvents()

    confirm(True)
    window._reset_app_preferences()

    # 新增标题条目按名解析进**当前工程目录**（布局目录在重置中保留），
    # 落到出厂「タイトル左上」的真实位置，而不是恒为 1 的出厂表位置。
    entry = window._new_title_entry_defaults()
    index = int(entry.layout_index or 0)
    assert 1 <= index <= len(window._style.layouts)
    assert window._style.layouts[index - 1].name == "タイトル左上"
    assert entry.show_mode == TitleOverlay().show_mode

    # 工作区条目同样回到出厂：布局 = タイトル左上、显示时段 = 自定义。
    workspace_title = window._style.title_overlays[0]
    workspace_index = int(workspace_title.layout_index or 0)
    assert window._style.layouts[workspace_index - 1].name == "タイトル左上"
    assert workspace_title.show_mode == TitleOverlay().show_mode


def test_reset_without_open_project_does_not_mark_dirty(
    make_window, settings, confirm, tmp_path
) -> None:
    """未打开工程时工作区只是习惯种子：重置不标脏、不多弹保存询问。"""
    settings.data = _polluted_payload(str(tmp_path / "skip.yurika"))
    window = make_window()
    assert window._project_session.path is None

    confirm(True)
    window._reset_app_preferences()

    assert window._project_session.dirty is False
    # 样式确实复位了（重置本身仍生效）。
    assert window._style.fill_gradient_enabled == Style().fill_gradient_enabled


def test_reset_with_open_project_marks_dirty(make_window, settings, confirm) -> None:
    """已打开的工程被重置通用样式：按未保存改动提示。"""
    from pathlib import Path

    window = make_window()
    window._project_session.adopt_project_identity(
        path=Path("D:/karaoke/某首歌.yurika"), disk_revision=1
    )
    window._project_session.set_dirty(False)

    confirm(True)
    window._reset_app_preferences()

    assert window._project_session.dirty is True
