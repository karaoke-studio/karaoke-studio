"""Focused construction contracts for the subtitle title-property page."""

from __future__ import annotations

from krok_helper.subtitle_render.frontend.properties.pages.title import (
    TITLE_TIME_MAX_MS,
    TitlePropertyPageBuilder,
)


class _Host:
    def __init__(self) -> None:
        self.updates: list[dict[str, object]] = []

    def _on_title_enabled_toggled(self, _checked: bool) -> None:
        pass

    def _on_title_text_changed(self) -> None:
        pass

    def _commit_title_text_edit(self) -> None:
        pass

    def _on_title_layout_changed(self, _index: int) -> None:
        pass

    def _open_title_scheme(self) -> None:
        pass

    def _update_title(self, **changes) -> None:
        self.updates.append(changes)


def test_title_property_builder_preserves_sections_labels_and_controls(qapp) -> None:
    host = _Host()
    builder = TitlePropertyPageBuilder(host)

    text = builder.make_text_section()
    style = builder.make_style_section()
    timing = builder.make_time_section()

    assert text.header.text() == "标题"
    assert text.header_switch is host._title_enabled_switch
    assert host._title_text_edit.placeholderText() == "{title} / {artist}"
    assert style.header.text() == "外观"
    assert host._title_appearance_grid._max_columns == 2
    assert host._title_scheme_edit_btn.text() == "编辑标题配色"
    assert timing.header.text() == "显示时段"
    assert host._title_mode_combo.count() == 4
    assert host._title_head_row_label.text() == "开头"
    assert host._title_tail_row_label.text() == "片尾"
    assert host._title_head_grid._max_columns == 4
    assert host._title_tail_grid._max_columns == 4
    assert host._title_head_edit.maximum() == TITLE_TIME_MAX_MS
    assert host._title_tail_edit.maximum() == TITLE_TIME_MAX_MS


def test_title_property_builder_routes_each_timecode_to_its_model_field(qapp) -> None:
    host = _Host()
    builder = TitlePropertyPageBuilder(host)
    builder.make_time_section()

    host._title_fade_in_edit.setValue(300)
    host._title_head_edit.setValue(1_000)
    host._title_tail_duration_edit.setValue(2_000)

    assert host.updates == [
        {"fade_in_ms": 300},
        {"head_offset_ms": 1_000},
        {"tail_duration_ms": 2_000},
    ]
