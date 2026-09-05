"""Focused construction contracts for the multi-title card-list page."""

from __future__ import annotations

from krok_helper.subtitle_render.domain.models import Style, TitleOverlay, TitleTimeWindow
from krok_helper.subtitle_render.frontend.properties.pages.title import (
    TITLE_TIME_MAX_MS,
    TitleCard,
    TitlePropertyPageBuilder,
)


class _Host:
    """Minimal PropertyPanel stand-in recording every card callback."""

    def __init__(self) -> None:
        self.updates: list[tuple[int, dict[str, object]]] = None
        self.style = Style()
        self.calls: list[tuple[str, object]] = []

    # ---- TitleCard / shell callbacks（与 PropertyPanel 同名同签名）----
    def _on_title_enabled_toggled(self, index: int, checked: bool) -> None:
        self.calls.append(("enabled", (index, checked)))

    def _on_title_card_text_changed(self, index: int) -> None:
        self.calls.append(("text_changed", index))

    def _commit_title_text_edit(self) -> None:
        self.calls.append(("text_committed", None))

    def _on_title_card_layout_changed(self, index: int) -> None:
        self.calls.append(("layout_changed", index))

    def _on_title_card_scheme_changed(self, index: int) -> None:
        self.calls.append(("scheme_changed", index))

    def _open_title_scheme(self, index: int) -> None:
        self.calls.append(("open_scheme", index))

    def _on_title_card_renamed(self, index: int, name: str) -> None:
        self.calls.append(("renamed", (index, name)))

    def _on_title_card_delete_requested(self, index: int) -> None:
        self.calls.append(("delete", index))

    def _on_title_add_requested(self) -> None:
        self.calls.append(("add", None))

    def _on_title_card_windows_changed(self, index: int) -> None:
        self.calls.append(("windows_changed", index))

    def _on_title_card_window_added(self, index: int) -> None:
        self.calls.append(("window_added", index))

    def _on_title_card_window_removed(self, index: int, row: object) -> None:
        self.calls.append(("window_removed", (index, row)))

    def _on_title_tags_requested(self, index: int) -> None:
        self.calls.append(("tags", index))

    def _update_title(self, index: int, **changes) -> None:
        self.calls.append(("update", (index, changes)))

    # ---- TitleCard 依赖的宿主工具 ----
    def _layout_display_name(self, style: Style, key: str) -> str:
        return "默认布局" if key == "default" else str(key)

    @property
    def _title_timecode_factory(self):
        from krok_helper.subtitle_render.frontend.properties.controls.inputs import (
            TimecodeEdit,
        )

        return TimecodeEdit


def _make_card(host: _Host, overlay: TitleOverlay | None = None) -> TitleCard:
    return TitleCard(
        host,
        0,
        overlay if overlay is not None else TitleOverlay(name="标题 1"),
        timecode_factory=host._title_timecode_factory,
    )


def test_title_shell_builds_cards_list_add_button_and_empty_state(qapp) -> None:
    host = _Host()
    builder = TitlePropertyPageBuilder(host)

    page = builder.make_page()

    assert builder.cards_layout is not None
    assert builder.add_button.text() == "＋ 添加标题"
    assert builder.empty_label.text() == "暂无标题条目。"
    assert builder.empty_label.isHidden()
    assert page is not None


def test_title_shell_add_button_routes_to_host(qapp) -> None:
    host = _Host()
    builder = TitlePropertyPageBuilder(host)
    page = builder.make_page()  # noqa: F841 — 持有引用，避免控件被 GC

    builder.add_button.click()

    assert host.calls == [("add", None)]


def test_title_card_header_carries_switch_name_and_delete(qapp) -> None:
    host = _Host()
    card = _make_card(host)

    assert card.name_edit.text() == "标题 1"
    assert card.section.header_switch is not None
    card.section.header_switch.setChecked(True)
    assert host.calls == [("enabled", (0, True))]
    card.delete_button.click()
    assert host.calls[-1] == ("delete", 0)

    card.name_edit.setText("新名字")
    card.name_edit.editingFinished.emit()
    assert host.calls[-1] == ("renamed", (0, "新名字"))


def test_title_card_text_routes_through_host(qapp) -> None:
    host = _Host()
    card = _make_card(host)

    assert card.text_edit.placeholderText() == "{title} / {artist}"
    card.text_edit.setPlainText("曲名")
    assert host.calls == [("text_changed", 0)]
    card.text_edit.editingFinished.emit()
    assert host.calls[-1] == ("text_committed", None)


def test_title_card_name_is_first_body_row_and_header_follows(qapp) -> None:
    host = _Host()
    card = _make_card(host, TitleOverlay(name="主标题"))

    # 名称在卡片主体第一行（不在头部），头部折叠标题跟随名称文本。
    assert card.name_edit.text() == "主标题"
    assert card.section.header.text() == "主标题"
    card.sync(TitleOverlay(name="改名后"))
    assert card.name_edit.text() == "改名后"
    assert card.section.header.text() == "改名后"


def test_title_card_tags_button_routes_to_host(qapp) -> None:
    host = _Host()
    card = _make_card(host)

    card.tags_button.click()

    assert host.calls == [("tags", 0)]


def test_title_card_tags_button_stays_inside_text_row(qapp) -> None:
    """回归：行内文字框(Ignored)与按钮不能同为 Ignored，否则 Qt 盒式
    分配会把按钮挤出父级矩形（宽仍 84 但完全不可见）。
    """
    from PyQt6.QtWidgets import QApplication, QVBoxLayout, QWidget

    for width in (280, 320, 360):
        host = _Host()
        card = _make_card(host)
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.addWidget(card.section)
        holder.resize(width, 400)
        holder.show()
        for _ in range(10):
            QApplication.processEvents()

        row = card.text_edit.parentWidget()
        button = card.tags_button
        assert button.width() >= 60, f"按钮宽度被挤压为 {button.width()} (width={width})"
        assert button.x() >= 0, f"按钮被布局到行外 (width={width})"
        assert button.geometry().right() <= row.width(), (
            f"按钮右缘 {button.geometry().right()} 超出行宽 {row.width()} (width={width})"
        )
        holder.hide()
        holder.deleteLater()


def test_title_card_insert_tag_placeholder_at_cursor(qapp) -> None:
    host = _Host()
    card = _make_card(host)
    card.text_edit.setPlainText("前缀 ")
    cursor = card.text_edit.textCursor()
    cursor.movePosition(cursor.MoveOperation.End)
    card.text_edit.setTextCursor(cursor)

    card.insert_tag_placeholder("Credit")

    assert card.text_edit.toPlainText() == "前缀 {Credit}"


def test_title_card_time_section_keeps_legacy_fields_and_limits(qapp) -> None:
    host = _Host()
    card = _make_card(host, TitleOverlay(show_mode="whole"))

    assert card.mode_combo.count() == 5
    assert [card.mode_combo.itemText(i) for i in range(card.mode_combo.count())] == [
        "全程显示", "仅开头", "仅片尾", "开始和片尾", "自定义",
    ]
    # whole：行标签显示「全程」，片尾行隐藏（沿用旧页可见性规则）。
    assert card.head_row_label.text() == "全程"
    assert card.tail_row.isHidden()
    for editor in (*card.head_edits.values(), *card.tail_edits.values()):
        assert editor.maximum() in (TITLE_TIME_MAX_MS, 10_000)
    assert card.head_edits["head_offset_ms"].maximum() == TITLE_TIME_MAX_MS
    assert card.tail_edits["tail_offset_ms"].maximum() == TITLE_TIME_MAX_MS
    card.sync(TitleOverlay(show_mode="head_tail"))
    assert card.head_row_label.text() == "开头"
    assert card.tail_row_label.text() == "片尾"


def test_title_card_routes_each_timecode_to_its_model_field(qapp) -> None:
    host = _Host()
    card = _make_card(host)

    card.head_edits["fade_in_ms"].setValue(320)
    card.head_edits["head_offset_ms"].setValue(1_000)
    card.tail_edits["tail_duration_ms"].setValue(2_000)

    assert host.calls == [
        ("update", (0, {"fade_in_ms": 320})),
        ("update", (0, {"head_offset_ms": 1_000})),
        ("update", (0, {"tail_duration_ms": 2_000})),
    ]


def test_title_card_mode_combo_emits_show_mode(qapp) -> None:
    host = _Host()
    card = _make_card(host, TitleOverlay(show_mode="whole"))
    card.mode_combo.setCurrentIndex(
        max(0, card.mode_combo.findData("custom"))
    )
    assert host.calls == [("update", (0, {"show_mode": "custom"}))]


def test_title_card_custom_windows_editor(qapp) -> None:
    host = _Host()
    overlay = TitleOverlay(
        enabled=True,
        name="自定义卡",
        show_mode="custom",
        custom_windows=[TitleTimeWindow(1_000, 4_000, 200, 100)],
    )
    card = _make_card(host, overlay)

    assert len(card.window_rows) == 1
    assert not card.windows_container.isHidden()
    assert not card.add_window_button.isHidden()
    row = card.window_rows[0]
    assert row.edits["begin_ms"].value() == 1_000
    assert row.edits["end_ms"].value() == 4_000

    row.edits["end_ms"].setValue(4_500)
    assert host.calls == [("windows_changed", 0)]

    card.add_window_button.click()
    assert host.calls[-1] == ("window_added", 0)

    card.window_rows[0].edits["fade_in_ms"].setValue(50)
    assert host.calls[-1] == ("windows_changed", 0)


def test_title_window_fades_are_ms_inputs_with_500_default(qapp) -> None:
    host = _Host()
    overlay = TitleOverlay(show_mode="custom", custom_windows=[TitleTimeWindow()])
    card = _make_card(host, overlay)

    fade_in = card.window_rows[0].edits["fade_in_ms"]
    fade_out = card.window_rows[0].edits["fade_out_ms"]
    assert fade_in.value() == 500
    assert fade_out.value() == 500
    assert fade_in.suffix() == " ms"
    assert fade_out.suffix() == " ms"


def test_title_card_custom_visibility_follows_mode(qapp) -> None:
    host = _Host()
    card = _make_card(host, TitleOverlay(show_mode="whole"))

    assert card.windows_container.isHidden()
    assert card.add_window_button.isHidden()
    assert card.head_row.isVisibleTo(card.head_row.parentWidget()) or not card.head_row.isHidden()
    assert card.tail_row.isHidden()
    card.sync(TitleOverlay(show_mode="tail"))
    assert card.head_row.isHidden()
    assert not card.tail_row.isHidden()
    assert card.head_row_label.text() == "开头"


def test_title_card_sync_keeps_tail_none_inheritance_display(qapp) -> None:
    host = _Host()
    card = _make_card(host)
    overlay = TitleOverlay(
        show_mode="head_tail",
        duration_ms=1_234,
        fade_in_ms=260,
        fade_out_ms=210,
        tail_duration_ms=None,
        tail_fade_in_ms=None,
        tail_fade_out_ms=None,
    )

    card.sync(overlay)

    assert card.tail_edits["tail_duration_ms"].value() == 1_234
    assert card.tail_edits["tail_fade_in_ms"].value() == 260
    assert card.tail_edits["tail_fade_out_ms"].value() == 210


def test_title_card_scheme_combo_lists_builtin_first(qapp) -> None:
    from dataclasses import replace

    from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme

    host = _Host()
    card = _make_card(host)
    style = replace(
        Style(),
        custom_style_schemes={
            **Style().custom_style_schemes,
            "自定义甲": SubtitleStyleScheme(),
        },
    )

    card.sync_scheme_combo(TitleOverlay(scheme_name="自定义甲"), style)

    assert card.scheme_combo.count() == 2
    assert card.scheme_combo.itemText(0).startswith("内置")
    assert card.scheme_combo.currentData() == "自定义甲"

    card.scheme_combo.setCurrentIndex(0)
    assert host.calls == [("scheme_changed", 0)]
