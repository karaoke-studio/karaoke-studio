from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import pickle

from krok_helper.subtitle_render.engine.page_plan import (
    build_legacy_page_plan,
    build_page_plan,
    move_page_boundary,
    normalize_page_plan,
    page_plan_has_manual_changes,
    project_page_plan_to_legacy_fields,
    reflow_pages_for_layout_capacity,
    resolve_page_plan,
)
from krok_helper.subtitle_render.engine.timeline import compute_display_lines
from krok_helper.subtitle_render.models import (
    LyricsLayout,
    Style,
    SubtitleLoadingSettings,
    TimingChar,
    TimingLine,
    TimingTrack,
    TrackPage,
    TrackPagePlan,
    TrackSection,
    ensure_page_layout_defaults,
    layout_capacity,
    layout_display_name,
    track_page_plan_from_dict,
    track_page_plan_to_dict,
)


def _line(index: int, *, start: int | None = None, end: int | None = None):
    start = index * 1000 if start is None else start
    return TimingLine(
        chars=[TimingChar(str(index), start)],
        end_ms=start + 500 if end is None else end,
    )


def _track(count: int) -> TimingTrack:
    return TimingTrack(lines=[_line(index) for index in range(count)])


def test_default_style_has_stable_one_to_eight_row_layouts():
    style = ensure_page_layout_defaults(Style())
    assert set(style.default_layout_by_row_count) == set(range(1, 9))
    assert all(
        layout_capacity(style, layout_id) == rows
        for rows, layout_id in style.default_layout_by_row_count.items()
    )
    edited = ensure_page_layout_defaults(
        replace(style, line_alignments=["left", "center", "right"])
    )
    assert layout_capacity(
        edited, edited.default_layout_by_row_count[2]
    ) == 2
    assert layout_display_name(style, "default") == "2 行布局（默认）"
    assert {
        layout.layout_id: layout.name
        for layout in style.layouts
        if layout.layout_id.startswith("builtin-")
    } == {
        "builtin-1": "1 行布局",
        "builtin-3": "3 行布局",
        "builtin-4": "4 行布局",
        "builtin-5": "5 行布局",
        "builtin-6": "6 行布局",
        "builtin-7": "7 行布局",
        "builtin-8": "8 行布局",
    }


def test_legacy_builtin_layout_names_are_normalized_without_renaming_custom_layouts():
    style = ensure_page_layout_defaults(
        Style(
            layouts=[
                LyricsLayout(
                    name="默认 4 行",
                    layout_id="builtin-4",
                    line_alignments=["left"] * 4,
                ),
                LyricsLayout(
                    name="默认 6 行",
                    layout_id="custom-six",
                    line_alignments=["left"] * 6,
                ),
            ]
        )
    )

    names = {layout.layout_id: layout.name for layout in style.layouts}
    assert names["builtin-4"] == "4 行布局"
    assert names["custom-six"] == "默认 6 行"


def test_time_gap_is_strictly_greater_than_threshold():
    style = Style()
    settings = SubtitleLoadingSettings(
        time_gap_section_enabled=True,
        section_gap_ms=3100,
        blank_line_section_enabled=False,
        rows_per_page=4,
    )
    exact = TimingTrack(
        lines=[_line(0, start=0, end=1000), _line(1, start=4100, end=4500)]
    )
    over = deepcopy(exact)
    over.lines[1].chars[0].start_ms = 4101
    assert len(build_page_plan(exact, settings, style).sections) == 1
    assert len(build_page_plan(over, settings, style).sections) == 2


def test_blank_lines_collapse_to_one_section_boundary():
    style = Style()
    track = TimingTrack(
        lines=[
            _line(0),
            TimingLine(is_blank=True),
            TimingLine(is_blank=True),
            _line(1),
        ]
    )
    plan = build_page_plan(
        track,
        SubtitleLoadingSettings(
            time_gap_section_enabled=False,
            blank_line_section_enabled=True,
            rows_per_page=4,
        ),
        style,
    )
    assert [[page.line_count for page in section.pages] for section in plan.sections] == [
        [1],
        [1],
    ]


def test_partial_pages_keep_the_base_row_default_layout():
    base = Style()
    custom_three = LyricsLayout(
        name="三行加载布局",
        layout_id="custom-three",
        line_alignments=["left", "center", "right"],
    )
    style = ensure_page_layout_defaults(
        replace(
            base,
            layouts=base.layouts + [custom_three],
            default_layout_by_row_count={
                **base.default_layout_by_row_count,
                3: "custom-three",
            },
        )
    )
    track = _track(5)
    plan = build_page_plan(
        track,
        SubtitleLoadingSettings(
            time_gap_section_enabled=False,
            blank_line_section_enabled=False,
            rows_per_page=3,
        ),
        style,
    )

    assert [
        (page.line_count, page.layout_id)
        for section in plan.sections
        for page in section.pages
    ] == [(3, "custom-three"), (2, "custom-three")]


def test_partial_pages_can_use_actual_row_default_layout():
    base = Style()
    custom_three = LyricsLayout(
        name="三行加载布局",
        layout_id="custom-three",
        line_alignments=["left", "center", "right"],
    )
    style = ensure_page_layout_defaults(
        replace(
            base,
            layouts=base.layouts + [custom_three],
            default_layout_by_row_count={
                **base.default_layout_by_row_count,
                3: "custom-three",
            },
        )
    )

    plan = build_page_plan(
        _track(5),
        SubtitleLoadingSettings(
            time_gap_section_enabled=False,
            blank_line_section_enabled=False,
            rows_per_page=3,
            allocate_layout_by_actual_rows=True,
        ),
        style,
    )

    assert [
        (page.line_count, page.layout_id)
        for section in plan.sections
        for page in section.pages
    ] == [(3, "custom-three"), (2, "default")]


def test_partial_pages_before_explicit_boundaries_keep_base_row_layout():
    track = _track(4)
    track.lines[1].break_before = "page"
    track.lines[3].break_before = "paragraph"
    style = ensure_page_layout_defaults(Style())

    plan = build_page_plan(
        track,
        SubtitleLoadingSettings(
            time_gap_section_enabled=False,
            blank_line_section_enabled=False,
            rows_per_page=3,
        ),
        style,
    )

    assert [
        [(page.line_count, page.layout_id) for page in section.pages]
        for section in plan.sections
    ] == [
        [(1, "builtin-3"), (2, "builtin-3")],
        [(1, "builtin-3")],
    ]


def test_page_plan_round_trip_has_no_layout_source_state():
    plan = TrackPagePlan(
        [TrackSection([TrackPage(2, "default"), TrackPage(1, "builtin-1")])]
    )
    payload = track_page_plan_to_dict(plan)
    assert "layout_origin" not in repr(payload)
    assert "manual" not in repr(payload)
    assert track_page_plan_from_dict(payload) == plan


def test_legacy_migration_preserves_breaks_and_layout_changes():
    style = Style()
    track = _track(5)
    track.lines[2].break_before = "page"
    track.lines[3].break_before = "paragraph"
    track.lines[3].layout_index = next(
        index
        for index, layout in enumerate(style.layouts, start=1)
        if layout.layout_id == "builtin-1"
    )
    plan = build_legacy_page_plan(track, style, section_gap_ms=0)
    assert [[page.line_count for page in section.pages] for section in plan.sections] == [
        [2, 1],
        [1, 1],
    ]


def test_normalize_repairs_unknown_layout_and_total_line_count():
    style = Style()
    track = _track(5)
    malformed = TrackPagePlan(
        [TrackSection([TrackPage(3, "missing"), TrackPage(99, "missing")])]
    )
    plan = normalize_page_plan(track, style, malformed)
    assert sum(
        page.line_count for section in plan.sections for page in section.pages
    ) == 5
    assert all(
        layout_capacity(style, page.layout_id) >= page.line_count
        for section in plan.sections
        for page in section.pages
    )


def test_projection_and_resolution_share_sections_pages_lanes():
    style = Style()
    track = _track(5)
    track.page_plan = TrackPagePlan(
        [
            TrackSection([TrackPage(2, "default")]),
            TrackSection([TrackPage(3, "builtin-3")]),
        ]
    )
    project_page_plan_to_legacy_fields(track, style)
    assert [line.break_before for line in track.lines] == [
        "none",
        "none",
        "paragraph",
        "none",
        "none",
    ]
    resolved = resolve_page_plan(track, style)
    assert [(line.section_index, line.global_page_index, line.lane) for line in resolved.lines] == [
        (0, 0, 0),
        (0, 0, 1),
        (1, 1, 0),
        (1, 1, 1),
        (1, 1, 2),
    ]


def test_move_boundary_uses_instant_capacity_rule_and_forward_flow():
    base = Style()
    four = LyricsLayout(
        name="四行留白",
        layout_id="custom-four",
        line_alignments=["left", "right", "left", "right"],
    )
    style = ensure_page_layout_defaults(replace(base, layouts=base.layouts + [four]))
    track = _track(6)
    track.page_plan = TrackPagePlan(
        [
            TrackSection(
                [
                    TrackPage(2, "custom-four"),
                    TrackPage(2, "default"),
                    TrackPage(2, "default"),
                ]
            )
        ]
    )
    assert move_page_boundary(track, style, 0, 0, direction=1)
    pages = track.page_plan.sections[0].pages
    assert [(page.line_count, page.layout_id) for page in pages] == [
        (3, "custom-four"),
        (2, "default"),
        (1, "builtin-1"),
    ]
    assert [char.text for line in track.lines for char in line.chars] == list("012345")


def test_move_boundary_crosses_section_and_removes_empty_section():
    style = ensure_page_layout_defaults(Style())
    track = _track(4)
    track.page_plan = TrackPagePlan(
        [
            TrackSection([TrackPage(1, "builtin-1")]),
            TrackSection([TrackPage(1, "builtin-1")]),
            TrackSection([TrackPage(2, "builtin-2")]),
        ]
    )

    assert move_page_boundary(track, style, 0, 0, direction=1)

    assert [
        [(page.line_count, page.layout_id) for page in section.pages]
        for section in track.page_plan.sections
    ] == [
        [(2, "builtin-2")],
        [(2, "builtin-2")],
    ]
    resolved = resolve_page_plan(track, style)
    assert [line.section_index for line in resolved.lines] == [0, 0, 1, 1]
    assert track.lines[2].break_before == "paragraph"


def test_move_last_line_into_next_section_removes_empty_source_section():
    style = ensure_page_layout_defaults(Style())
    track = _track(3)
    track.page_plan = TrackPagePlan(
        [
            TrackSection([TrackPage(1, "builtin-1")]),
            TrackSection([TrackPage(2, "builtin-2")]),
        ]
    )

    assert move_page_boundary(track, style, 0, 0, direction=-1)

    assert len(track.page_plan.sections) == 1
    assert [
        (page.line_count, page.layout_id)
        for page in track.page_plan.sections[0].pages
    ] == [(3, "builtin-3")]
    assert [line.section_index for line in resolve_page_plan(track, style).lines] == [
        0,
        0,
        0,
    ]


def test_all_five_instant_layout_examples():
    base = Style()
    examples = [
        (2, 2, 3, "builtin-3"),
        (2, 4, 3, "custom"),
        (2, 4, 5, "builtin-5"),
        (3, 3, 2, "default"),
        (3, 4, 2, "custom"),
    ]
    from krok_helper.subtitle_render.engine.page_plan import (
        _resize_page_with_instant_layout_rule,
    )

    for old_count, capacity, new_count, expected in examples:
        custom = LyricsLayout(
            name="测试",
            layout_id="custom",
            line_alignments=["left"] * capacity,
        )
        style = ensure_page_layout_defaults(
            replace(base, layouts=base.layouts + [custom])
        )
        page = TrackPage(old_count, "custom")
        _resize_page_with_instant_layout_rule(page, new_count, style)
        assert page.layout_id == expected


def test_timeline_consumes_page_plan_instead_of_legacy_breaks_and_gap():
    track = _track(4)
    track.lines[1].break_before = "paragraph"
    track.page_plan = TrackPagePlan([TrackSection([TrackPage(3), TrackPage(1)])])
    result = compute_display_lines(
        track,
        lead_in_ms=0,
        tail_ms=0,
        lane_gap_ms=0,
        section_gap_ms=1,
        lane_count=2,
    )
    assert [(item.page_index, item.section_index, item.lane) for item in result] == [
        (0, 0, 0),
        (0, 0, 1),
        (0, 0, 2),
        (1, 0, 0),
    ]


def test_manual_change_is_derived_instead_of_stored():
    style = Style()
    track = _track(4)
    settings = SubtitleLoadingSettings(
        time_gap_section_enabled=False,
        blank_line_section_enabled=False,
        rows_per_page=2,
    )
    track.loading_settings_snapshot = settings
    track.page_plan = build_page_plan(track, settings, style)
    project_page_plan_to_legacy_fields(track, style)

    assert not hasattr(track, "page_plan_manual")
    assert page_plan_has_manual_changes(track, style) is False

    track.page_plan.sections[0].pages[0].layout_id = "builtin-3"
    project_page_plan_to_legacy_fields(track, style)
    assert page_plan_has_manual_changes(track, style) is True


def test_timeline_offsets_short_bottom_and_center_pages():
    track = _track(2)
    track.page_plan = TrackPagePlan([TrackSection([TrackPage(2)])])
    base = dict(
        lead_in_ms=0,
        tail_ms=0,
        lane_gap_ms=0,
        lane_count=4,
        row_count_of=lambda _line: 4,
    )
    bottom = compute_display_lines(
        track,
        **base,
        bottom_align_of=lambda _line: True,
        vertical_position_of=lambda _line: "bottom",
    )
    center = compute_display_lines(
        track,
        **base,
        bottom_align_of=lambda _line: False,
        vertical_position_of=lambda _line: "center",
    )
    assert [item.lane for item in bottom] == [2, 3]
    assert [item.lane for item in center] == [1, 2]


def test_layout_shrink_reflows_before_normalizing_the_old_reference():
    custom = LyricsLayout(
        name="四行",
        layout_id="custom-four",
        line_alignments=["left"] * 4,
    )
    old_style = ensure_page_layout_defaults(
        replace(Style(), layouts=Style().layouts + [custom])
    )
    new_style = replace(
        old_style,
        layouts=[
            replace(layout, line_alignments=["left", "right"])
            if layout.layout_id == "custom-four"
            else layout
            for layout in old_style.layouts
        ],
    )
    track = _track(3)
    track.page_plan = TrackPagePlan(
        [TrackSection([TrackPage(3, "custom-four")])]
    )

    assert reflow_pages_for_layout_capacity(
        track, new_style, "custom-four"
    ) == (1, 1)
    assert [
        (page.line_count, page.layout_id)
        for page in track.page_plan.sections[0].pages
    ] == [(2, "custom-four"), (1, "builtin-1")]


def test_page_plan_is_spawn_pickle_safe():
    track = _track(2)
    track.page_plan = TrackPagePlan([TrackSection([TrackPage(2)])])
    assert pickle.loads(pickle.dumps(track)) == track


def test_painter_schedule_and_native_ir_share_authoritative_page_plan():
    from PyQt6.QtWidgets import QApplication
    from krok_helper.subtitle_render.engine.painter import (
        build_track_layout_plan,
        display_schedule_for_style,
    )
    from krok_helper.subtitle_render.native_protocol import track_to_ir

    app = QApplication.instance() or QApplication([])
    style = Style()
    track = _track(3)
    track.page_plan = TrackPagePlan(
        [
            TrackSection([TrackPage(1, "default")]),
            TrackSection([TrackPage(2, "default")]),
        ]
    )
    project_page_plan_to_legacy_fields(track, style)

    schedule = display_schedule_for_style(track, style)
    render_ir = track_to_ir(
        track,
        style,
        layout_plan=build_track_layout_plan(track, style),
    )

    assert [schedule[index][0] for index in range(3)] == [1, 0, 1]
    assert [line["page_index"] for line in render_ir["lines"]] == [0, 1, 1]
    assert [line["section_index"] for line in render_ir["lines"]] == [0, 1, 1]
    assert [line["lane"] for line in render_ir["lines"]] == [1, 0, 1]
    assert app is not None


def test_subtitle_loading_settings_round_trips_sug_export_offset_flag() -> None:
    from krok_helper.subtitle_render.models import (
        subtitle_loading_settings_from_dict,
        subtitle_loading_settings_to_dict,
    )

    payload = subtitle_loading_settings_to_dict(
        SubtitleLoadingSettings(apply_sug_export_offset=False)
    )
    assert payload["apply_sug_export_offset"] is False
    assert subtitle_loading_settings_from_dict(payload) == SubtitleLoadingSettings(
        apply_sug_export_offset=False
    )

    # 旧项目 / 旧 settings.json 没有该字段时回落默认值「应用」，
    # 保证升级后既有行为不变。
    legacy = dict(payload)
    legacy.pop("apply_sug_export_offset")
    assert (
        subtitle_loading_settings_from_dict(legacy).apply_sug_export_offset is True
    )
