"""布局库 UI 接线冒烟：真实控件逐一点击，任何异常都算失败。

2026-10 用户反馈「手动随便点点就报错」：本文件把布局导航条、布局库
对话框的每个可点控件都实际触发一遍，确保槽函数与宿主接线没有运行期
异常（Qt 槽里的异常会中断交互，必须由测试兜住）。
"""

from __future__ import annotations

import os
from dataclasses import replace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from krok_helper.subtitle_render.domain.models import LyricsLayout, Style  # noqa: E402
from krok_helper.subtitle_render.frontend import main_window as mw  # noqa: E402
from krok_helper.subtitle_render.frontend.properties import (  # noqa: E402
    layout_library_dialog as lld,
)
from krok_helper.subtitle_render.frontend.properties import (  # noqa: E402
    property_panel as pp,
)
from krok_helper.subtitle_render.frontend.properties.layout_library_dialog import (  # noqa: E402
    LayoutLibraryDialog,
)
from krok_helper.subtitle_render.settings.layout_presets import (  # noqa: E402
    factory_layout_definitions,
    layout_presets_to_dict,
)


class _Recorder:
    def __init__(self, data: dict | None = None) -> None:
        self.data = dict(data or {})

    def load(self) -> dict:
        return dict(self.data)

    def save(self, data: dict) -> None:
        self.data = dict(data)


@pytest.fixture
def answer_dialogs(monkeypatch):
    """把三处模块级 fluent_* 全部替身（各模块各自持有引用）。"""

    state = {"confirm": True, "text": "自检预设"}

    def question(*args, **kwargs):
        return state["confirm"]

    def get_text(*args, **kwargs):
        return state["text"], state["confirm"]

    for module in (mw, pp, lld):
        if hasattr(module, "fluent_question"):
            monkeypatch.setattr(module, "fluent_question", question)
        if hasattr(module, "fluent_get_text"):
            monkeypatch.setattr(module, "fluent_get_text", get_text)
        if hasattr(module, "fluent_info"):
            monkeypatch.setattr(module, "fluent_info", lambda *a, **k: None)
        if hasattr(module, "fluent_warning"):
            monkeypatch.setattr(module, "fluent_warning", lambda *a, **k: None)
        if hasattr(module, "fluent_confirm_dont_ask"):
            monkeypatch.setattr(
                module, "fluent_confirm_dont_ask", lambda *a, **k: True
            )
        if hasattr(module, "dont_ask_active"):
            monkeypatch.setattr(module, "dont_ask_active", lambda key: False)
    monkeypatch.setattr(mw.InfoBar, "success", lambda *a, **k: None)
    monkeypatch.setattr(mw.InfoBar, "warning", lambda *a, **k: None)
    return state


def test_layout_navigation_buttons_are_all_wired(
    qapp, answer_dialogs, monkeypatch
) -> None:
    """导航条五个按钮点到不报错，且行为符合预期。"""

    # 库按钮会开模态窗：替身 exec，避免测试阻塞在嵌套事件循环。
    monkeypatch.setattr(LayoutLibraryDialog, "exec", lambda self: 0)
    window = mw.SubtitleRenderWindow(embedded=True, settings_provider=_Recorder())
    panel = window._property_panel
    base = len(panel.subtitle_style.layouts)

    panel._add_layout_btn.click()
    assert len(panel.subtitle_style.layouts) == base + 1

    panel._rename_layout_btn.click()  # 替身返回「自检预设」
    assert panel.subtitle_style.layouts[-1].name == "自检预设"

    panel._save_layout_btn.click()
    assert any(
        preset.layout.name == "自检预设" for preset in window._layout_presets
    )

    panel._delete_layout_btn.click()
    assert len(panel.subtitle_style.layouts) == base

    panel._layout_library_btn.click()  # 触发宿主开窗（exec 已替身）

    window.close()
    window.deleteLater()


def test_layout_library_dialog_every_control_is_wired(
    qapp, answer_dialogs
) -> None:
    """对话框每个可点控件都触发一遍：勾选 / 全选 / 过滤 / 改名 / 保存 /
    载入 / 删除 / 恢复出厂 / 关闭。"""

    window = mw.SubtitleRenderWindow(embedded=True, settings_provider=_Recorder())
    dialog = LayoutLibraryDialog(
        presets=window._layout_presets,
        project_layout_names={layout.name for layout in window._style.layouts},
        library_reference_height=window._app_default_style.layout_reference_height,
        project_usage=window._layout_usage_summary(),
        parent=window,
    )
    dialog.layoutPresetsChanged.connect(window._adopt_layout_presets)
    dialog.layoutImportRequested.connect(window._import_layout_presets)

    first_id = dialog._presets[0].layout.layout_id
    dialog._set_selected(first_id, True)
    dialog._set_selected(first_id, False)
    dialog._set_default_load(first_id, False)
    dialog._set_default_load(first_id, True)
    dialog._select_all_btn.click()
    dialog._clear_checks_btn.click()
    dialog._select_all_btn.click()

    dialog._filter_edit.setText("行布局")
    dialog._rows_filter.setCurrentIndex(3)
    dialog._filter_edit.setText("")
    dialog._rows_filter.setCurrentIndex(0)

    last_id = dialog._presets[-1].layout.layout_id
    dialog._set_selected(last_id, True)
    dialog._rename_btn.click()
    dialog._import_btn.click()
    dialog._delete_btn.click()

    factory = next(
        layout
        for layout in factory_layout_definitions()
        if layout.layout_id == "builtin-3"
    )
    dialog._restore_factory(factory)
    dialog.accept()

    window.close()
    window.deleteLater()


def test_host_layout_actions_do_not_raise(qapp, answer_dialogs) -> None:
    """宿主侧动作：保存默认布局、载入预设、新建工程、重置偏好、用量统计。"""

    window = mw.SubtitleRenderWindow(embedded=True, settings_provider=_Recorder())
    window._save_layout_default(0)
    window._save_layout_default(1)
    window._import_layout_preset(window._layout_presets[0])
    window._layout_usage_summary()
    window._project_dirty = False
    window._new_project()
    window._reset_app_preferences()
    window.close()
    window.deleteLater()


def test_open_library_dialog_from_panel_signal(qapp, answer_dialogs, monkeypatch):
    """面板信号到宿主开窗的真实链路（不进入阻塞 exec）。"""

    opened: list[object] = []
    monkeypatch.setattr(
        LayoutLibraryDialog,
        "exec",
        lambda self: opened.append(self) or 0,
    )
    window = mw.SubtitleRenderWindow(embedded=True, settings_provider=_Recorder())

    window._property_panel._layout_library_btn.click()

    assert len(opened) == 1
    assert opened[0].windowTitle() == "布局预设库"
    window.close()
    window.deleteLater()


def test_layout_catalog_with_reference_height_mismatch_imports(
    qapp, answer_dialogs
) -> None:
    """工程参考高度与库条目不同时，载入按条目基准换算且不报错。"""

    window = mw.SubtitleRenderWindow(embedded=True, settings_provider=_Recorder())
    window._style = replace(
        window._style,
        layout_reference_height=540,
        layouts=[
            layout
            for layout in window._style.layouts
            if layout.name != "3 行布局"
        ],
    )
    preset = next(
        preset
        for preset in window._layout_presets
        if preset.layout.name == "3 行布局"
    )
    window._import_layout_preset(preset)

    imported = next(
        layout
        for layout in window._style.layouts
        if layout.name == "3 行布局"
    )
    assert imported.line_gap_px == 30  # 60 (1080) → 30 (540)
    window.close()
    window.deleteLater()
