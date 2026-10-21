"""布局预设库对话框：表格列语义、选中/默认加载两列、过滤、改名、删除、
恢复出厂与批量载入；以及宿主侧布局库接线回归。
"""

from __future__ import annotations

import os
from dataclasses import replace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication, QLabel, QPushButton  # noqa: E402

from krok_helper.subtitle_render.domain.models import LyricsLayout  # noqa: E402
from krok_helper.subtitle_render.frontend.properties import (  # noqa: E402
    layout_library_dialog as lld,
)
from krok_helper.subtitle_render.frontend.properties.layout_library_dialog import (  # noqa: E402
    LayoutLibraryDialog,
)
from krok_helper.subtitle_render.settings.layout_presets import (  # noqa: E402
    LayoutPreset,
    default_layout_presets,
    factory_layout_definitions,
    layout_presets_from_dict,
    rescale_preset_layout,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _custom(name: str, rows: int = 2, **kwargs) -> LayoutPreset:
    return LayoutPreset(
        layout=LyricsLayout(name=name, line_alignments=["left"] * rows, **kwargs),
        default_load=True,
    )


def _preset_id(dialog: LayoutLibraryDialog, name: str) -> str:
    for preset in dialog.presets():
        if preset.layout.name == name:
            return str(preset.layout.layout_id or "")
    raise AssertionError(f"未找到预设：{name}")


def _row_name(dialog: LayoutLibraryDialog, name: str) -> str:
    preset_id = _preset_id(dialog, name)
    return dialog._delegate._texts[preset_id][0]


def _row(dialog: LayoutLibraryDialog, name: str):
    """按名取条目（delegate 版没有行控件，返回条目 + id 的轻量视图）。"""

    class _Row:
        def __init__(self, owner: LayoutLibraryDialog, preset_id: str) -> None:
            self._owner = owner
            self._id = preset_id

        def set_selected(self, selected: bool) -> None:
            self._owner._set_selected(self._id, selected)

        def set_default_load(self, checked: bool) -> None:
            self._owner._set_default_load(self._id, checked)

        @property
        def default_load(self) -> bool:
            return next(
                p.default_load
                for p in self._owner.presets()
                if str(p.layout.layout_id) == self._id
            )

    return _Row(dialog, _preset_id(dialog, name))


def test_list_rows_carry_header_and_full_details(qapp) -> None:
    """列表风格 + 明确表头；行数在最前，其余布局参数单行平铺。"""

    dialog = LayoutLibraryDialog(
        presets=[_custom("我的布局", 3, line_gap_px=42)],
        project_layout_names={"我的布局"},
    )

    header = dialog._header
    header_texts = [
        label.text() for label in header.findChildren(QLabel) if label.text()
    ]
    assert any("选中" == text for text in header_texts)
    assert any("默认加载" == text for text in header_texts)
    assert any("行数" in text and "行间距" in text for text in header_texts)

    preset_id = _preset_id(dialog, "我的布局")
    name_text, details = dialog._delegate._texts[preset_id]
    assert name_text.startswith("我的布局")
    assert "工程内已有同名" in name_text
    assert details.startswith("3 行")
    assert "行间距 42" in details and "位置" in details and "对齐" in details
    # 完整参数（含被省略掉的部分）在悬浮提示里。
    assert "注音与正文" in dialog._items[preset_id].toolTip()
    dialog.close()


def test_select_checkbox_is_session_selection_not_default_load(qapp) -> None:
    """左侧勾选只影响本次选择；右侧勾选才是默认加载。"""

    dialog = LayoutLibraryDialog(presets=default_layout_presets())
    row = _row(dialog, "3 行布局")

    row.set_selected(True)
    assert [p.layout.name for p in dialog.selected_presets()] == ["3 行布局"]
    assert row.default_load is True
    assert dialog._selection_stats.text().startswith("已选 1 /")

    row.set_default_load(False)
    stored = next(
        preset
        for preset in dialog.presets()
        if preset.layout.name == "3 行布局"
    )
    assert stored.default_load is False
    assert dialog._selection_stats.text().startswith("已选 1 /")
    assert dialog._default_stats.text().startswith("默认加载 8 /")
    dialog.close()


def test_select_all_respects_filter(qapp) -> None:
    dialog = LayoutLibraryDialog(
        presets=[*default_layout_presets(), _custom("独有", 3)]
    )
    dialog._rows_filter.setCurrentIndex(3)  # 只看 3 行
    dialog._set_all_selected(True)

    selected_names = {p.layout.name for p in dialog.selected_presets()}
    assert selected_names and selected_names <= {"3 行布局", "独有"}
    dialog.close()


def test_filter_clears_hidden_current_row_and_disables_actions(qapp) -> None:
    dialog = LayoutLibraryDialog(
        presets=[*default_layout_presets(), _custom("独有名词")]
    )
    _row(dialog, "独有名词").set_selected(True)
    assert dialog._delete_btn.isEnabled()

    dialog._filter_edit.setText("不存在的名字")

    # 过滤只影响可见性：勾选仍保留（不误删看不见的条目由「可见性」保证），
    # 但删除/重命名要求可见且唯一勾选，这里仍可操作选中项。
    assert dialog.selected_presets()[0].layout.name == "独有名词"
    dialog.close()


def test_rename_and_delete_custom_preset(qapp, monkeypatch) -> None:
    dialog = LayoutLibraryDialog(
        presets=[*default_layout_presets(), _custom("旧名")]
    )
    _row(dialog, "旧名").set_selected(True)
    monkeypatch.setattr(
        lld, "fluent_get_text", lambda *args, **kwargs: ("新名", True)
    )
    dialog._on_rename()
    assert "新名" in [p.layout.name for p in dialog.presets()]

    _row(dialog, "新名").set_selected(True)
    calls: list[str] = []

    def confirm(*args, **kwargs):
        calls.append(args[1])
        return len(calls) >= 2

    monkeypatch.setattr(lld, "fluent_question", confirm)
    monkeypatch.setattr(lld, "fluent_confirm_dont_ask", lambda *a, **k: True)
    dialog._on_delete()
    assert "新名" in [p.layout.name for p in dialog.presets()], "只确认一次不删"
    assert calls[-1] == "删除布局预设（1/2）"
    dialog._on_delete()
    assert "新名" not in [p.layout.name for p in dialog.presets()]
    dialog.close()


def test_delete_confirm_reports_project_usage(qapp, monkeypatch) -> None:
    dialog = LayoutLibraryDialog(
        presets=[*default_layout_presets(), _custom("待删")],
        project_usage={"待删": (2, 3)},
    )
    _row(dialog, "待删").set_selected(True)
    captured: dict[str, str] = {}
    monkeypatch.setattr(
        lld,
        "fluent_question",
        lambda *args, **kwargs: captured.update(text=args[2]) or False,
    )
    dialog._on_delete()
    assert "2 个页面、3 行歌词" in captured["text"]
    dialog.close()


def test_restore_factory_restores_params_and_canonical_name(qapp) -> None:
    presets = [
        preset
        for preset in default_layout_presets()
        if preset.layout.layout_id not in {"builtin-1", "builtin-3"}
    ]
    presets.append(_custom("3 行布局"))
    dialog = LayoutLibraryDialog(presets=presets)
    factory_3 = next(
        layout
        for layout in factory_layout_definitions()
        if layout.layout_id == "builtin-3"
    )
    factory_1 = next(
        layout
        for layout in factory_layout_definitions()
        if layout.layout_id == "builtin-1"
    )

    dialog._restore_factory(factory_3)
    by_id = {
        str(p.layout.layout_id): p.layout.name for p in dialog.presets()
    }
    assert by_id["builtin-3"] == "3 行布局"
    assert sum(
        1 for p in dialog.presets() if p.layout.name == "3 行布局"
    ) == 1

    dialog._restore_factory(factory_1)
    assert any(
        p.layout.layout_id == "builtin-1" for p in dialog.presets()
    )
    dialog.close()


def test_restore_factory_rescales_to_library_height(qapp) -> None:
    dialog = LayoutLibraryDialog(
        presets=[
            preset
            for preset in default_layout_presets()
            if preset.layout.layout_id != "builtin-3"
        ],
        library_reference_height=1440,
    )
    factory_3 = next(
        layout
        for layout in factory_layout_definitions()
        if layout.layout_id == "builtin-3"
    )
    dialog._restore_factory(factory_3)
    restored = next(
        preset
        for preset in dialog.presets()
        if preset.layout.layout_id == "builtin-3"
    )
    assert restored.layout.line_gap_px == int(60 * 1440 / 1080)
    assert rescale_preset_layout(restored.layout, 1440, 1080).line_gap_px == 60
    dialog.close()


def test_import_emits_selected_batch(qapp) -> None:
    dialog = LayoutLibraryDialog(presets=default_layout_presets())
    dialog._set_all_selected(True)
    imported: list[list] = []
    dialog.layoutImportRequested.connect(imported.append)

    dialog._on_import()

    assert imported and len(imported[0]) == len(default_layout_presets())
    dialog.close()


def test_bottom_row_has_import_and_close_only(qapp) -> None:
    dialog = LayoutLibraryDialog(presets=default_layout_presets())
    texts = [button.text() for button in dialog.findChildren(QPushButton)]
    assert "关闭" in texts
    assert not any(text in {"确定", "取消"} for text in texts)
    first_id = str(dialog._presets[0].layout.layout_id)
    assert not dialog._delegate._icons[first_id].isNull()
    dialog.close()


class _Recorder:
    def __init__(self) -> None:
        self.data: dict = {}

    def load(self) -> dict:
        return dict(self.data)

    def save(self, data: dict) -> None:
        self.data = dict(data)


def _make_window(qapp, monkeypatch, data=None):
    from krok_helper.subtitle_render.frontend import main_window as mw

    monkeypatch.setattr(mw.InfoBar, "success", lambda *args, **kwargs: None)
    monkeypatch.setattr(mw.InfoBar, "warning", lambda *args, **kwargs: None)
    provider = _Recorder()
    provider.data = data or {}
    window = mw.SubtitleRenderWindow(embedded=True, settings_provider=provider)
    return window, provider


def test_startup_workspace_follows_preset_library(qapp, monkeypatch) -> None:
    """启动工作区与「新建工程」同一口径（预设库物化）。"""

    empty_library = {"layout_presets": [], "layout_presets_migrated": True}
    window, provider = _make_window(qapp, monkeypatch, empty_library)
    assert window._style.layouts == []
    window._project_dirty = False
    window._new_project()
    assert window._style.layouts == []
    assert window._style.title_overlays[0].layout_index == -1

    window._apply_style(
        replace(
            window._style,
            layouts=[LyricsLayout(name="启动带入", line_alignments=["left"])],
        )
    )
    window._save_layout_default(1)
    window.close()
    window.deleteLater()

    reloaded, _provider2 = _make_window(qapp, monkeypatch, provider.data)
    assert "启动带入" in {layout.name for layout in reloaded._style.layouts}
    reloaded.close()
    reloaded.deleteLater()


def test_save_default_layout_overrides_same_rows_preset_wiring(
    qapp, monkeypatch
) -> None:
    window, _provider = _make_window(qapp, monkeypatch)
    window._layout_presets = [
        replace(preset, default_for_rows=3)
        if preset.layout.layout_id == "builtin-3"
        else preset
        for preset in window._layout_presets
    ]
    window._apply_style(
        replace(window._style, line_alignments=["left", "center", "right"])
    )
    window._save_layout_default(0)

    wired = next(
        preset
        for preset in window._layout_presets
        if preset.layout.layout_id == "builtin-3"
    )
    assert wired.default_for_rows is None
    window._project_dirty = False
    window._new_project()
    assert window._style.default_layout_by_row_count[3] == "default"
    window.close()
    window.deleteLater()


def test_title_global_default_layout_preference_survives_reload(
    qapp, monkeypatch
) -> None:
    window, provider = _make_window(qapp, monkeypatch)
    title = window._style.title_overlays[0]
    window._property_panel.set_style(
        replace(
            window._style,
            title_overlays=[replace(title, enabled=True, layout_index=0)],
        ),
        emit=True,
    )
    QApplication.instance().processEvents()
    window._flush_persisted_state_save()
    window.close()
    window.deleteLater()

    reloaded, _provider2 = _make_window(qapp, monkeypatch, provider.data)
    assert reloaded._style.title_overlays[0].layout_index == 0
    reloaded.close()
    reloaded.deleteLater()


def test_reset_preferences_keeps_project_layout_catalog(qapp, monkeypatch) -> None:
    window, _provider = _make_window(qapp, monkeypatch)
    window._apply_style(
        replace(
            window._style,
            layouts=[
                *window._style.layouts,
                LyricsLayout(name="工程自定义", line_gap_px=71),
            ],
        )
    )
    monkeypatch.setattr(
        "krok_helper.subtitle_render.frontend.main_window.fluent_question",
        lambda *args, **kwargs: True,
    )
    window._reset_app_preferences()

    assert "工程自定义" in {layout.name for layout in window._style.layouts}
    window.close()
    window.deleteLater()


def test_library_rename_syncs_remembered_title_layout_name(qapp, monkeypatch) -> None:
    window, _provider = _make_window(qapp, monkeypatch)
    window._layout_presets = [
        *window._layout_presets,
        LayoutPreset(
            layout=LyricsLayout(
                name="标题专用", layout_id="layout-title", line_y_position="top"
            )
        ),
    ]
    window._new_project_title_layout_name = "标题专用"
    renamed = [
        replace(preset, layout=replace(preset.layout, name="标题专用改"))
        if preset.layout.layout_id == "layout-title"
        else preset
        for preset in window._layout_presets
    ]

    window._adopt_layout_presets(renamed)

    assert window._new_project_title_layout_name == "标题专用改"
    window._project_dirty = False
    window._new_project()
    title_index = int(window._style.title_overlays[0].layout_index or 0)
    assert window._style.layouts[title_index - 1].name == "标题专用改"
    window.close()
    window.deleteLater()


def test_new_title_entry_follows_remembered_layout_name(qapp, monkeypatch) -> None:
    window, _provider = _make_window(qapp, monkeypatch)
    window._apply_style(
        replace(
            window._style,
            layouts=[
                LyricsLayout(
                    name="标题专用", layout_id="layout-title", line_y_position="top"
                ),
                *window._style.layouts,
            ],
        )
    )
    window._new_project_title_layout_name = "标题专用"

    entry = window._new_title_entry_defaults()

    assert entry.layout_index == 1
    assert window._style.layouts[0].name == "标题专用"
    window.close()
    window.deleteLater()


def test_regular_preference_save_keeps_other_instances_additions(
    qapp, monkeypatch
) -> None:
    window_a, provider = _make_window(qapp, monkeypatch)
    window_a._layout_presets = [
        *window_a._layout_presets,
        LayoutPreset(layout=LyricsLayout(name="A 新增", line_alignments=["left"])),
    ]
    window_a._save_persisted_state()
    window_a.close()
    window_a.deleteLater()

    stale_data = {
        key: value
        for key, value in provider.data.items()
        if key != "layout_presets"
    }
    window_b, _provider_b = _make_window(qapp, monkeypatch, stale_data)
    window_b._save_persisted_state()

    disk_names = {
        preset.layout.name
        for preset in layout_presets_from_dict(provider.data.get("layout_presets"))
    }
    assert "A 新增" in disk_names
    window_b.close()
    window_b.deleteLater()


def test_saving_renamed_builtin_does_not_duplicate_factory_id(
    qapp, monkeypatch
) -> None:
    window, _provider = _make_window(qapp, monkeypatch)
    window._apply_style(
        replace(
            window._style,
            layouts=[
                replace(layout, name="我的三行")
                if layout.layout_id == "builtin-3"
                else layout
                for layout in window._style.layouts
            ],
        )
    )
    index = next(
        i
        for i, layout in enumerate(window._style.layouts, start=1)
        if layout.name == "我的三行"
    )
    window._save_layout_default(index)

    ids = [preset.layout.layout_id for preset in window._layout_presets]
    assert ids.count("builtin-3") == 1
    saved = next(
        preset for preset in window._layout_presets if preset.layout.name == "我的三行"
    )
    assert saved.layout.layout_id != "builtin-3"
    assert saved.default_for_rows == 3
    window.close()
    window.deleteLater()


def test_exit_save_persists_remote_title_rename(qapp, monkeypatch) -> None:
    window, provider = _make_window(qapp, monkeypatch)
    window._layout_presets = [
        *window._layout_presets,
        LayoutPreset(
            layout=LyricsLayout(
                name="old", layout_id="layout-remote2", line_y_position="top"
            )
        ),
    ]
    window._new_project_title_layout_name = "old"
    window._save_persisted_state()

    for row in provider.data["layout_presets"]:
        if row["layout"]["layout_id"] == "layout-remote2":
            row["layout"]["name"] = "new"
    window._persisted_state_dirty = True

    window.shutdown_for_host_exit()

    assert provider.data["new_project_defaults"]["title_layout_name"] == "new"
    window.deleteLater()


def test_title_layout_combo_labels_factory_fallback(qapp, monkeypatch) -> None:
    window, _provider = _make_window(qapp, monkeypatch)
    window._apply_style(
        replace(
            window._style,
            layouts=[],
            hidden_builtin_layout_ids=["title-default"],
            title_overlays=[
                replace(window._style.title_overlays[0], layout_index=-1)
            ],
        )
    )
    combo = window._property_panel._title_cards[0].layout_combo
    texts = [combo.itemText(index) for index in range(combo.count())]
    assert any("出厂回退" in text for text in texts), texts
    window.close()
    window.deleteLater()


def test_layout_navigation_matches_role_navigation_order(qapp) -> None:
    from krok_helper.subtitle_render.frontend.properties.property_panel import (
        PropertyPanel,
    )

    panel = PropertyPanel()
    role_layout = panel._role_navigation_layout
    layout_row = panel._layout_combo.parentWidget().layout()

    assert layout_row.indexOf(panel._layout_library_btn) == (
        layout_row.indexOf(panel._delete_layout_btn) + 1
    )
    assert layout_row.indexOf(panel._layout_library_btn) < (
        layout_row.indexOf(panel._save_layout_btn)
    )
    assert layout_row.indexOf(panel._layout_library_btn) == (
        role_layout.indexOf(panel._manage_presets_button)
    )
    panel.close()

def test_delete_second_step_offers_dont_ask_and_suppresses_later(qapp, monkeypatch):
    """第二步确认带「本次运行不再提示」；勾选后本会话不再询问。"""

    from krok_helper.subtitle_render.frontend.dialogs import fluent_dialogs as fd

    fd.reset_dont_ask_flags()
    dialog = LayoutLibraryDialog(
        presets=[*default_layout_presets(), _custom("待删一"), _custom("待删二")]
    )
    asked: list[str] = []
    monkeypatch.setattr(
        lld, "fluent_question", lambda *a, **k: asked.append(a[1]) or True
    )
    # 模拟用户在第二步勾选了「本次运行不再提示」并确认。
    monkeypatch.setattr(
        fd, "fluent_question_with_checkbox", lambda *a, **k: (True, True)
    )
    try:
        _row(dialog, "待删一").set_selected(True)
        dialog._on_delete()
        assert "待删一" not in [p.layout.name for p in dialog.presets()]
        assert asked == ["删除布局预设（1/2）"]

        # 第二次：两个确认都不再弹（第一步被跳过，第二步直接返回 True）。
        asked.clear()
        _row(dialog, "待删二").set_selected(True)
        dialog._on_delete()
        assert "待删二" not in [p.layout.name for p in dialog.presets()]
        assert asked == [], "勾选不再提示后不应再弹确认"
    finally:
        fd.reset_dont_ask_flags()
        dialog.close()


def test_in_project_entry_can_be_imported_by_name_and_deleted_from_library(
    qapp, monkeypatch
):
    """工程内已有同名布局时：仍可载入（按名覆盖）与删除（只删库）。"""

    from krok_helper.subtitle_render.frontend import main_window as mw

    monkeypatch.setattr(mw.InfoBar, "success", lambda *a, **k: None)
    window = mw.SubtitleRenderWindow(embedded=True, settings_provider=_Recorder())
    project_entry = next(
        layout for layout in window._style.layouts
        if layout.layout_id == "builtin-3"
    )
    imported: list[list] = []
    dialog = LayoutLibraryDialog(
        presets=window._layout_presets,
        project_layout_names={layout.name for layout in window._style.layouts},
    )
    dialog.layoutImportRequested.connect(window._import_layout_presets)
    dialog.layoutImportRequested.connect(imported.append)

    preset = next(
        item
        for item in window._layout_presets
        if item.layout.layout_id == "builtin-3"
    )
    preset = replace(preset, layout=replace(preset.layout, line_gap_px=123))
    dialog._presets = [preset if p.layout.layout_id == "builtin-3" else p
                       for p in dialog._presets]
    dialog._rebuild()
    _row(dialog, "3 行布局").set_selected(True)
    assert dialog._import_btn.isEnabled(), "工程内同名条目必须仍可载入"
    assert dialog._delete_btn.isEnabled(), "工程内同名条目必须仍可删除"

    dialog._on_import()
    replaced = next(
        layout for layout in window._style.layouts
        if layout.name == "3 行布局"
    )
    assert replaced.layout_id == project_entry.layout_id, "按名覆盖且保留工程 id"
    assert replaced.line_gap_px == 123

    # 删除只影响库，工程内保留（两步确认都替身，避免真实模态阻塞）。
    monkeypatch.setattr(lld, "fluent_question", lambda *a, **k: True)
    monkeypatch.setattr(
        lld, "fluent_confirm_dont_ask", lambda *a, **k: True
    )
    dialog._on_delete()
    assert "3 行布局" not in [p.layout.name for p in dialog.presets()]
    assert any(
        layout.name == "3 行布局" for layout in window._style.layouts
    ), "删库不得动工程内布局"
    window.close()
    window.deleteLater()


def test_rows_use_fluent_item_delegate_for_native_hover_and_alignment(qapp) -> None:
    """行由 ListItemDelegate 子类绘制：原生灰底 hover、垂直居中、无行控件。"""

    from qfluentwidgets.components.widgets.list_view import ListItemDelegate

    dialog = LayoutLibraryDialog(presets=default_layout_presets())
    assert isinstance(dialog._list.itemDelegate(), ListItemDelegate)
    assert dialog._list.itemWidget(dialog._list.item(0)) is None, "不再用行控件"

    option_rect = dialog._list.visualItemRect(dialog._list.item(0))
    assert option_rect.height() >= 38, "行高与样式预设库同档"
    # 右侧默认加载勾选与左侧选中勾选同尺寸、同一垂直中心。
    select_rect = dialog._delegate._select_rect(option_rect)
    default_rect = dialog._delegate._default_rect(option_rect)
    assert select_rect.size() == default_rect.size()
    assert select_rect.center().y() == default_rect.center().y()
    dialog.close()


def test_clicking_row_blank_toggles_selection_but_default_box_does_not(
    qapp
) -> None:
    """点行空白切换「选中」；点右侧勾选只改默认加载。"""

    from PyQt6.QtCore import QPoint, QPointF
    from PyQt6.QtGui import QMouseEvent

    dialog = LayoutLibraryDialog(presets=default_layout_presets())
    dialog.show()
    item = dialog._items["builtin-3"]
    rect = dialog._list.visualItemRect(item)
    delegate = dialog._delegate
    index = dialog._list.indexFromItem(item)

    blank = QPoint(rect.center().x(), rect.center().y())
    release = QMouseEvent(
        QMouseEvent.Type.MouseButtonRelease,
        QPointF(blank),
        QPointF(blank),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    assert delegate.editorEvent(
        release, dialog._list.model(), _option(dialog, rect), index
    )
    assert "builtin-3" in dialog._selected_ids, "点空白应选中"
    assert item.checkState() == Qt.CheckState.Checked

    # 再点一次空白取消选中。
    delegate.editorEvent(
        release, dialog._list.model(), _option(dialog, rect), index
    )
    assert "builtin-3" not in dialog._selected_ids

    # 点右侧默认加载勾选：只切换默认加载，不动选中。
    default_rect = delegate._default_rect(rect)
    box_point = QPoint(
        default_rect.center().x(), default_rect.center().y()
    )
    box_release = QMouseEvent(
        QMouseEvent.Type.MouseButtonRelease,
        QPointF(box_point),
        QPointF(box_point),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    before = next(
        p.default_load for p in dialog.presets()
        if p.layout.layout_id == "builtin-3"
    )
    assert delegate.editorEvent(
        box_release, dialog._list.model(), _option(dialog, rect), index
    )
    after = next(
        p.default_load for p in dialog.presets()
        if p.layout.layout_id == "builtin-3"
    )
    assert after is not before
    assert "builtin-3" not in dialog._selected_ids
    dialog.close()


def _option(dialog, rect):
    from PyQt6.QtWidgets import QStyleOptionViewItem

    option = QStyleOptionViewItem()
    option.rect = rect
    option.font = dialog._list.font()
    option.palette = dialog._list.palette()
    option.widget = dialog._list
    return option


def test_startup_workspace_follows_preset_library(qapp, monkeypatch) -> None:
    """启动工作区与「新建工程」同一口径（预设库物化）。"""

    empty_library = {"layout_presets": [], "layout_presets_migrated": True}
    window, provider = _make_window(qapp, monkeypatch, empty_library)
    assert window._style.layouts == []
    window._project_dirty = False
    window._new_project()
    assert window._style.layouts == []
    assert window._style.title_overlays[0].layout_index == -1

    window._apply_style(
        replace(
            window._style,
            layouts=[LyricsLayout(name="启动带入", line_alignments=["left"])],
        )
    )
    window._save_layout_default(1)
    window.close()
    window.deleteLater()

    reloaded, _provider2 = _make_window(qapp, monkeypatch, provider.data)
    assert "启动带入" in {layout.name for layout in reloaded._style.layouts}
    reloaded.close()
    reloaded.deleteLater()


def test_save_default_layout_overrides_same_rows_preset_wiring(
    qapp, monkeypatch
) -> None:
    window, _provider = _make_window(qapp, monkeypatch)
    window._layout_presets = [
        replace(preset, default_for_rows=3)
        if preset.layout.layout_id == "builtin-3"
        else preset
        for preset in window._layout_presets
    ]
    window._apply_style(
        replace(window._style, line_alignments=["left", "center", "right"])
    )
    window._save_layout_default(0)

    wired = next(
        preset
        for preset in window._layout_presets
        if preset.layout.layout_id == "builtin-3"
    )
    assert wired.default_for_rows is None
    window._project_dirty = False
    window._new_project()
    assert window._style.default_layout_by_row_count[3] == "default"
    window.close()
    window.deleteLater()


def test_title_global_default_layout_preference_survives_reload(
    qapp, monkeypatch
) -> None:
    window, provider = _make_window(qapp, monkeypatch)
    title = window._style.title_overlays[0]
    window._property_panel.set_style(
        replace(
            window._style,
            title_overlays=[replace(title, enabled=True, layout_index=0)],
        ),
        emit=True,
    )
    QApplication.instance().processEvents()
    window._flush_persisted_state_save()
    window.close()
    window.deleteLater()

    reloaded, _provider2 = _make_window(qapp, monkeypatch, provider.data)
    assert reloaded._style.title_overlays[0].layout_index == 0
    reloaded.close()
    reloaded.deleteLater()


def test_reset_preferences_keeps_project_layout_catalog(qapp, monkeypatch) -> None:
    window, _provider = _make_window(qapp, monkeypatch)
    window._apply_style(
        replace(
            window._style,
            layouts=[
                *window._style.layouts,
                LyricsLayout(name="工程自定义", line_gap_px=71),
            ],
        )
    )
    monkeypatch.setattr(
        "krok_helper.subtitle_render.frontend.main_window.fluent_question",
        lambda *args, **kwargs: True,
    )
    window._reset_app_preferences()

    assert "工程自定义" in {layout.name for layout in window._style.layouts}
    window.close()
    window.deleteLater()


def test_library_rename_syncs_remembered_title_layout_name(qapp, monkeypatch) -> None:
    window, _provider = _make_window(qapp, monkeypatch)
    window._layout_presets = [
        *window._layout_presets,
        LayoutPreset(
            layout=LyricsLayout(
                name="标题专用", layout_id="layout-title", line_y_position="top"
            )
        ),
    ]
    window._new_project_title_layout_name = "标题专用"
    renamed = [
        replace(preset, layout=replace(preset.layout, name="标题专用改"))
        if preset.layout.layout_id == "layout-title"
        else preset
        for preset in window._layout_presets
    ]

    window._adopt_layout_presets(renamed)

    assert window._new_project_title_layout_name == "标题专用改"
    window._project_dirty = False
    window._new_project()
    title_index = int(window._style.title_overlays[0].layout_index or 0)
    assert window._style.layouts[title_index - 1].name == "标题专用改"
    window.close()
    window.deleteLater()


def test_new_title_entry_follows_remembered_layout_name(qapp, monkeypatch) -> None:
    window, _provider = _make_window(qapp, monkeypatch)
    window._apply_style(
        replace(
            window._style,
            layouts=[
                LyricsLayout(
                    name="标题专用", layout_id="layout-title", line_y_position="top"
                ),
                *window._style.layouts,
            ],
        )
    )
    window._new_project_title_layout_name = "标题专用"

    entry = window._new_title_entry_defaults()

    assert entry.layout_index == 1
    assert window._style.layouts[0].name == "标题专用"
    window.close()
    window.deleteLater()


def test_regular_preference_save_keeps_other_instances_additions(
    qapp, monkeypatch
) -> None:
    window_a, provider = _make_window(qapp, monkeypatch)
    window_a._layout_presets = [
        *window_a._layout_presets,
        LayoutPreset(layout=LyricsLayout(name="A 新增", line_alignments=["left"])),
    ]
    window_a._save_persisted_state()
    window_a.close()
    window_a.deleteLater()

    stale_data = {
        key: value
        for key, value in provider.data.items()
        if key != "layout_presets"
    }
    window_b, _provider_b = _make_window(qapp, monkeypatch, stale_data)
    window_b._save_persisted_state()

    disk_names = {
        preset.layout.name
        for preset in layout_presets_from_dict(provider.data.get("layout_presets"))
    }
    assert "A 新增" in disk_names
    window_b.close()
    window_b.deleteLater()


def test_saving_renamed_builtin_does_not_duplicate_factory_id(
    qapp, monkeypatch
) -> None:
    window, _provider = _make_window(qapp, monkeypatch)
    window._apply_style(
        replace(
            window._style,
            layouts=[
                replace(layout, name="我的三行")
                if layout.layout_id == "builtin-3"
                else layout
                for layout in window._style.layouts
            ],
        )
    )
    index = next(
        i
        for i, layout in enumerate(window._style.layouts, start=1)
        if layout.name == "我的三行"
    )
    window._save_layout_default(index)

    ids = [preset.layout.layout_id for preset in window._layout_presets]
    assert ids.count("builtin-3") == 1
    saved = next(
        preset for preset in window._layout_presets if preset.layout.name == "我的三行"
    )
    assert saved.layout.layout_id != "builtin-3"
    assert saved.default_for_rows == 3
    window.close()
    window.deleteLater()


def test_exit_save_persists_remote_title_rename(qapp, monkeypatch) -> None:
    window, provider = _make_window(qapp, monkeypatch)
    window._layout_presets = [
        *window._layout_presets,
        LayoutPreset(
            layout=LyricsLayout(
                name="old", layout_id="layout-remote2", line_y_position="top"
            )
        ),
    ]
    window._new_project_title_layout_name = "old"
    window._save_persisted_state()

    for row in provider.data["layout_presets"]:
        if row["layout"]["layout_id"] == "layout-remote2":
            row["layout"]["name"] = "new"
    window._persisted_state_dirty = True

    window.shutdown_for_host_exit()

    assert provider.data["new_project_defaults"]["title_layout_name"] == "new"
    window.deleteLater()


def test_title_layout_combo_labels_factory_fallback(qapp, monkeypatch) -> None:
    window, _provider = _make_window(qapp, monkeypatch)
    window._apply_style(
        replace(
            window._style,
            layouts=[],
            hidden_builtin_layout_ids=["title-default"],
            title_overlays=[
                replace(window._style.title_overlays[0], layout_index=-1)
            ],
        )
    )
    combo = window._property_panel._title_cards[0].layout_combo
    texts = [combo.itemText(index) for index in range(combo.count())]
    assert any("出厂回退" in text for text in texts), texts
    window.close()
    window.deleteLater()


def test_layout_navigation_matches_role_navigation_order(qapp) -> None:
    from krok_helper.subtitle_render.frontend.properties.property_panel import (
        PropertyPanel,
    )

    panel = PropertyPanel()
    role_layout = panel._role_navigation_layout
    layout_row = panel._layout_combo.parentWidget().layout()

    assert layout_row.indexOf(panel._layout_library_btn) == (
        layout_row.indexOf(panel._delete_layout_btn) + 1
    )
    assert layout_row.indexOf(panel._layout_library_btn) < (
        layout_row.indexOf(panel._save_layout_btn)
    )
    assert layout_row.indexOf(panel._layout_library_btn) == (
        role_layout.indexOf(panel._manage_presets_button)
    )
    panel.close()

def test_delete_second_step_offers_dont_ask_and_suppresses_later(qapp, monkeypatch):
    """第二步确认带「本次运行不再提示」；勾选后本会话不再询问。"""

    from krok_helper.subtitle_render.frontend.dialogs import fluent_dialogs as fd

    fd.reset_dont_ask_flags()
    dialog = LayoutLibraryDialog(
        presets=[*default_layout_presets(), _custom("待删一"), _custom("待删二")]
    )
    asked: list[str] = []
    monkeypatch.setattr(
        lld, "fluent_question", lambda *a, **k: asked.append(a[1]) or True
    )
    # 模拟用户在第二步勾选了「本次运行不再提示」并确认。
    monkeypatch.setattr(
        fd, "fluent_question_with_checkbox", lambda *a, **k: (True, True)
    )
    try:
        _row(dialog, "待删一").set_selected(True)
        dialog._on_delete()
        assert "待删一" not in [p.layout.name for p in dialog.presets()]
        assert asked == ["删除布局预设（1/2）"]

        # 第二次：两个确认都不再弹（第一步被跳过，第二步直接返回 True）。
        asked.clear()
        _row(dialog, "待删二").set_selected(True)
        dialog._on_delete()
        assert "待删二" not in [p.layout.name for p in dialog.presets()]
        assert asked == [], "勾选不再提示后不应再弹确认"
    finally:
        fd.reset_dont_ask_flags()
        dialog.close()


def test_in_project_entry_can_be_imported_by_name_and_deleted_from_library(
    qapp, monkeypatch
):
    """工程内已有同名布局时：仍可载入（按名覆盖）与删除（只删库）。"""

    from krok_helper.subtitle_render.frontend import main_window as mw

    monkeypatch.setattr(mw.InfoBar, "success", lambda *a, **k: None)
    window = mw.SubtitleRenderWindow(embedded=True, settings_provider=_Recorder())
    project_entry = next(
        layout for layout in window._style.layouts
        if layout.layout_id == "builtin-3"
    )
    imported: list[list] = []
    dialog = LayoutLibraryDialog(
        presets=window._layout_presets,
        project_layout_names={layout.name for layout in window._style.layouts},
    )
    dialog.layoutImportRequested.connect(window._import_layout_presets)
    dialog.layoutImportRequested.connect(imported.append)

    preset = next(
        item
        for item in window._layout_presets
        if item.layout.layout_id == "builtin-3"
    )
    preset = replace(preset, layout=replace(preset.layout, line_gap_px=123))
    dialog._presets = [preset if p.layout.layout_id == "builtin-3" else p
                       for p in dialog._presets]
    dialog._rebuild()
    _row(dialog, "3 行布局").set_selected(True)
    assert dialog._import_btn.isEnabled(), "工程内同名条目必须仍可载入"
    assert dialog._delete_btn.isEnabled(), "工程内同名条目必须仍可删除"

    dialog._on_import()
    replaced = next(
        layout for layout in window._style.layouts
        if layout.name == "3 行布局"
    )
    assert replaced.layout_id == project_entry.layout_id, "按名覆盖且保留工程 id"
    assert replaced.line_gap_px == 123

    # 删除只影响库，工程内保留（两步确认都替身，避免真实模态阻塞）。
    monkeypatch.setattr(lld, "fluent_question", lambda *a, **k: True)
    monkeypatch.setattr(
        lld, "fluent_confirm_dont_ask", lambda *a, **k: True
    )
    dialog._on_delete()
    assert "3 行布局" not in [p.layout.name for p in dialog.presets()]
    assert any(
        layout.name == "3 行布局" for layout in window._style.layouts
    ), "删库不得动工程内布局"
    window.close()
    window.deleteLater()




def test_explicit_save_reasserts_row_default_across_instances(
    qapp, monkeypatch
) -> None:
    """回归：本实例再次保存原预设为默认时，即便与旧值相同也必须落盘生效。"""

    window, provider = _make_window(qapp, monkeypatch)
    window._layout_presets = [
        preset
        for preset in window._layout_presets
        if preset.layout.layout_id not in {"builtin-3", "builtin-4"}
    ] + [
        LayoutPreset(
            layout=LyricsLayout(
                name="X", layout_id="layout-X", line_alignments=["l"] * 3
            ),
            default_load=True,
            default_for_rows=3,
        ),
        LayoutPreset(
            layout=LyricsLayout(
                name="Y", layout_id="layout-Y", line_alignments=["l"] * 3
            ),
            default_load=True,
        ),
    ]
    window._save_persisted_state()  # A 的基线：X = 3 行默认
    window.close()
    window.deleteLater()

    # 另一个实例把 3 行默认改成 Y 后落盘。
    for row in provider.data["layout_presets"]:
        if row["layout"]["name"] == "X":
            row["default_for_rows"] = None
        elif row["layout"]["name"] == "Y":
            row["default_for_rows"] = 3

    reopened, provider2 = _make_window(qapp, monkeypatch, provider.data)
    # 重新打开后 X 仍是 3 行默认（Y 的接线在上次合并里被清掉），本实例
    # 显式再保存一次 X —— 与基线值相同，但必须产生落盘意图。
    index = next(
        i
        for i, layout in enumerate(reopened._style.layouts, start=1)
        if layout.name == "X"
    )
    reopened._save_layout_default(index)

    wired = {
        row["layout"]["name"]: row["default_for_rows"]
        for row in provider2.data["layout_presets"]
    }
    assert wired["X"] == 3, wired
    assert wired["Y"] is None, wired
    assert reopened._layout_rows_asserted == {}, "声明落盘后要清空"
    reopened.close()
    reopened.deleteLater()
