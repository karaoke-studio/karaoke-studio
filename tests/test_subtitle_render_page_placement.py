from krok_helper.subtitle_render.engine.layout.page_offset_plan import (
    page_offsets_at_time,
)
from krok_helper.subtitle_render.engine.layout.page.placement import (
    LineVisualBand,
    PageVisualBands,
    solve_page_axis_offset_windows,
    solve_page_axis_offsets,
    time_windows_overlap,
)


def test_page_offset_selector_preserves_half_open_window_semantics():
    windows = {
        0: (
            (0, 100, 1.0, 2.0),
            (100, 200, 3.0, 4.0),
        ),
        1: (),
    }

    assert page_offsets_at_time(windows) == {0: (1.0, 2.0)}
    assert page_offsets_at_time(windows, t_ms=99) == {0: (1.0, 2.0)}
    assert page_offsets_at_time(windows, t_ms=100) == {0: (3.0, 4.0)}
    assert page_offsets_at_time(windows, t_ms=200) == {}


def _band(line, page, start, end, top, bottom):
    return LineVisualBand(line, page, start, end, top, bottom)


def test_cross_axis_separation_prevents_false_page_collision():
    previous = PageVisualBands(
        page_id="previous",
        bands=(
            LineVisualBand(
                "a",
                "previous",
                0,
                2_000,
                100,
                160,
                cross_min=0,
                cross_max=100,
            ),
        ),
    )
    incoming = PageVisualBands(
        page_id="incoming",
        bands=(
            LineVisualBand(
                "b",
                "incoming",
                500,
                1_500,
                120,
                180,
                cross_min=200,
                cross_max=300,
            ),
        ),
    )

    assert solve_page_axis_offsets(
        [previous, incoming],
        viewport_min=0,
        viewport_max=1080,
    ) == {"previous": 0.0, "incoming": 0.0}


def test_half_open_time_windows_do_not_overlap_at_shared_boundary():
    first = _band("a", "p1", 0, 1000, 0, 20)
    second = _band("b", "p2", 1000, 2000, 0, 20)

    assert not time_windows_overlap(first, second)


def test_entry_and_exit_animation_windows_never_trigger_placement():
    previous = PageVisualBands(
        "p1",
        (_band("old", "p1", 0, 100, 40, 60),),
        gap_px=5,
        anchor="center",
        layout_key="same",
    )
    incoming_band = LineVisualBand(
        "new",
        "p2",
        100,
        200,
        50,
        70,
        entry_start_ms=80,
    )
    same_layout = PageVisualBands(
        "p2",
        (incoming_band,),
        gap_px=5,
        anchor="center",
        layout_key="same",
    )
    changed_layout = PageVisualBands(
        "p2",
        (incoming_band,),
        gap_px=5,
        anchor="center",
        layout_key="changed",
    )

    same_offsets = solve_page_axis_offsets(
        [previous, same_layout], viewport_min=0, viewport_max=200
    )
    changed_offsets = solve_page_axis_offsets(
        [previous, changed_layout], viewport_min=0, viewport_max=200
    )
    changed_windows = solve_page_axis_offset_windows(
        [previous, changed_layout], viewport_min=0, viewport_max=200
    )

    # 碰撞箱只在稳定文字窗口生效：[0, 100) 与 [100, 200) 不相交。
    assert same_offsets["p2"] == 0
    # 排版变化也不能把新页的 [80, 100) 入场动画纳入碰撞。
    assert changed_offsets["p2"] == 0
    assert changed_windows["p2"][0].start_ms == 80
    # 位移窗口仍覆盖完整入场到退场生命周期，避免中途发生位置跳变。


def test_solver_snaps_single_page_to_previous_authored_row_anchor():
    previous = PageVisualBands(
        "p1",
        (
            LineVisualBand(
                "p1t1", "p1", 0, 50, 100, 150, axis_anchor=140
            ),
            LineVisualBand(
                "p1t2", "p1", 0, 200, 200, 250, axis_anchor=240
            ),
        ),
        gap_px=10,
        anchor="end",
    )
    incoming = PageVisualBands(
        "p2",
        (
            LineVisualBand(
                "p2t1", "p2", 100, 300, 205, 255, axis_anchor=240
            ),
        ),
        gap_px=10,
        anchor="end",
    )

    offsets = solve_page_axis_offsets(
        [previous, incoming], viewport_min=0, viewport_max=360
    )

    # P2T1 与 P1T2 冲突时优先落到 P1T1 的既有基线，而不是按字形边缘
    # 多移动/少移动几像素，导致后续页面逐级“起飞”。
    assert offsets["p2"] == -100


def test_expired_shifted_line_does_not_make_later_page_take_off():
    pages = [
        PageVisualBands(
            "p1",
            (
                LineVisualBand(
                    "p1t1", "p1", 0, 50, 100, 150, axis_anchor=140
                ),
                LineVisualBand(
                    "p1t2", "p1", 0, 100, 200, 250, axis_anchor=240
                ),
            ),
            gap_px=10,
            anchor="end",
        ),
        PageVisualBands(
            "p2",
            (
                LineVisualBand(
                    "p2t1",
                    "p2",
                    80,
                    150,
                    205,
                    255,
                    entry_start_ms=70,
                    axis_anchor=240,
                ),
            ),
            gap_px=10,
            anchor="end",
        ),
        PageVisualBands(
            "p3",
            (
                LineVisualBand(
                    "p3t1", "p3", 150, 250, 100, 150, axis_anchor=140
                ),
                LineVisualBand(
                    "p3t2", "p3", 150, 250, 200, 250, axis_anchor=240
                ),
            ),
            gap_px=10,
            anchor="end",
        ),
    ]

    windows = solve_page_axis_offset_windows(
        pages, viewport_min=0, viewport_max=360
    )

    assert windows["p2"][0].offset == -100
    assert windows["p2"][0].start_ms == 70
    # P2 的位置保持到显示结束，但碰撞箱只在稳定窗口 [80, 150) 生效；
    # P3 从 150 ms 开始，不能继续继承 P2 的偏移并逐页向上“起飞”。
    assert windows["p3"][0].offset == 0


def test_bottom_page_moves_up_as_one_rigid_block():
    pages = [
        PageVisualBands(
            "p1",
            (
                _band("p1t1", "p1", 0, 2000, 120, 140),
                _band("p1t2", "p1", 0, 2000, 160, 180),
            ),
            gap_px=10,
            anchor="end",
        ),
        PageVisualBands(
            "p2",
            (
                _band("p2t1", "p2", 1000, 3000, 150, 170),
                _band("p2t2", "p2", 1000, 3000, 185, 205),
            ),
            gap_px=10,
            anchor="end",
        ),
    ]

    offsets = solve_page_axis_offsets(pages, viewport_min=0, viewport_max=240)

    assert offsets["p1"] == 0
    assert offsets["p2"] == -85


def test_solver_checks_all_still_visible_previous_pages():
    pages = [
        PageVisualBands(
            "p1",
            (_band("a", "p1", 0, 5000, 20, 40),),
            gap_px=2,
            anchor="start",
        ),
        PageVisualBands(
            "p2",
            (_band("b", "p2", 1000, 4000, 45, 65),),
            gap_px=7,
            anchor="start",
        ),
        PageVisualBands(
            "p3",
            (_band("c", "p3", 2000, 3000, 20, 65),),
            gap_px=99,
            anchor="start",
        ),
    ]

    offsets = solve_page_axis_offsets(pages, viewport_min=0, viewport_max=160)

    assert offsets["p3"] == 52


def test_solver_uses_overlapped_page_gap_not_incoming_page_gap():
    pages = [
        PageVisualBands(
            "p1",
            (_band("a", "p1", 0, 3000, 40, 60),),
            gap_px=5,
            anchor="center",
        ),
        PageVisualBands(
            "p2",
            (_band("b", "p2", 1000, 2000, 50, 70),),
            gap_px=99,
            anchor="center",
        ),
    ]

    offsets = solve_page_axis_offsets(pages, viewport_min=0, viewport_max=120)

    assert offsets["p2"] == 15


def test_solver_does_not_move_when_only_requested_gap_is_missing():
    pages = [
        PageVisualBands(
            "p1",
            (_band("old", "p1", 0, 100, 40, 60),),
            gap_px=30,
            anchor="center",
        ),
        PageVisualBands(
            "p2",
            (_band("new", "p2", 0, 100, 65, 85),),
            gap_px=99,
            anchor="center",
        ),
    ]

    offsets = solve_page_axis_offsets(
        pages, viewport_min=0, viewport_max=120
    )

    # 两行像素之间已有 5 px 空隙；不足旧页要求的 30 px 行间距不能单独
    # 成为碰撞触发条件。
    assert offsets["p2"] == 0


def test_solver_adds_the_overlapped_layout_gap_exactly_once():
    def incoming_offset(gap):
        pages = [
            PageVisualBands(
                "p1",
                (_band("old", "p1", 0, 100, 40, 60),),
                gap_px=gap,
                anchor="center",
            ),
            PageVisualBands(
                "p2",
                (_band("new", "p2", 0, 100, 50, 70),),
                gap_px=999,
                anchor="center",
            ),
        ]
        return solve_page_axis_offsets(
            pages, viewport_min=0, viewport_max=200
        )["p2"]

    without_gap = incoming_offset(0)
    with_gap = incoming_offset(30)

    assert with_gap - without_gap == 30


def test_page_larger_than_viewport_falls_back_to_authored_position():
    pages = [
        PageVisualBands(
            "p1", (_band("a", "p1", 0, 3000, 0, 80),), anchor="end"
        ),
        PageVisualBands(
            "p2", (_band("b", "p2", 1000, 2000, 0, 140),), anchor="end"
        ),
    ]

    first = solve_page_axis_offsets(pages, viewport_min=0, viewport_max=100)
    second = solve_page_axis_offsets(pages, viewport_min=0, viewport_max=100)

    assert first == second
    assert first["p2"] == 0


def test_bottom_page_searches_down_when_upward_position_exceeds_canvas():
    pages = [
        PageVisualBands(
            "p1",
            (_band("old", "p1", 0, 100, 0, 30),),
            gap_px=10,
            anchor="center",
        ),
        PageVisualBands(
            "p2",
            (_band("new", "p2", 0, 100, 10, 40),),
            gap_px=99,
            anchor="end",
        ),
    ]

    offsets = solve_page_axis_offsets(pages, viewport_min=0, viewport_max=100)

    assert offsets["p2"] == 30
    assert 10 + offsets["p2"] == 40
    assert 40 + offsets["p2"] == 70


def test_top_page_searches_up_when_downward_position_exceeds_canvas():
    pages = [
        PageVisualBands(
            "p1",
            (_band("old", "p1", 0, 100, 70, 100),),
            gap_px=10,
            anchor="center",
        ),
        PageVisualBands(
            "p2",
            (_band("new", "p2", 0, 100, 60, 90),),
            gap_px=99,
            anchor="start",
        ),
    ]

    offsets = solve_page_axis_offsets(pages, viewport_min=0, viewport_max=100)

    assert offsets["p2"] == -30
    assert 60 + offsets["p2"] == 30
    assert 90 + offsets["p2"] == 60


def test_solver_falls_back_to_authored_position_when_neither_side_fits():
    pages = [
        PageVisualBands(
            "p1", (_band("old", "p1", 0, 100, 30, 70),), anchor="center"
        ),
        PageVisualBands(
            "p2",
            (_band("new", "p2", 0, 100, 30, 70),),
            gap_px=10,
            anchor="end",
        ),
    ]

    offsets = solve_page_axis_offsets(pages, viewport_min=0, viewport_max=100)

    assert offsets["p2"] == 0


def test_shifted_final_position_does_not_reapply_bottom_margin():
    previous = PageVisualBands(
        "p1",
        (_band("old", "p1", 0, 100, 200, 290),),
        gap_px=10,
        anchor="center",
    )
    near_bottom = PageVisualBands(
        "p2",
        (_band("new", "p2", 0, 100, 240, 260),),
        gap_px=99,
        anchor="end",
    )
    at_bottom = PageVisualBands(
        "p3",
        (_band("new", "p3", 0, 100, 280, 300),),
        gap_px=99,
        anchor="end",
    )

    near_offset = solve_page_axis_offsets(
        [previous, near_bottom], viewport_min=0, viewport_max=300
    )["p2"]
    bottom_offset = solve_page_axis_offsets(
        [previous, at_bottom], viewport_min=0, viewport_max=300
    )["p3"]

    assert (240 + near_offset, 260 + near_offset) == (170, 190)
    assert (280 + bottom_offset, 300 + bottom_offset) == (170, 190)


def test_offset_window_keeps_displacement_until_shifted_page_finishes():
    pages = [
        PageVisualBands(
            "p1",
            (
                _band("p1t1", "p1", 0, 100, 200, 220),
                _band("p1t2", "p1", 0, 100, 230, 250),
                _band("p1t3", "p1", 0, 100, 260, 280),
            ),
            gap_px=10,
            anchor="end",
        ),
        PageVisualBands(
            "p2",
            (_band("p2t1", "p2", 80, 200, 260, 280),),
            gap_px=10,
            anchor="end",
        ),
    ]

    windows = solve_page_axis_offset_windows(
        pages, viewport_min=0, viewport_max=320
    )

    assert [(item.start_ms, item.end_ms) for item in windows["p2"]] == [(80, 200)]
    assert windows["p2"][0].offset < 0


def test_bottom_and_top_anchors_never_fallback_in_the_wrong_direction():
    previous = PageVisualBands(
        "previous",
        (_band("old", "previous", 0, 100, 0, 100),),
        gap_px=20,
        anchor="center",
    )
    bottom = PageVisualBands(
        "bottom",
        (_band("new-bottom", "bottom", 0, 100, 0, 100),),
        gap_px=20,
        anchor="end",
    )
    top = PageVisualBands(
        "top",
        (_band("new-top", "top", 0, 100, 0, 100),),
        gap_px=20,
        anchor="start",
    )

    bottom_offsets = solve_page_axis_offsets(
        [previous, bottom], viewport_min=0, viewport_max=100
    )
    top_offsets = solve_page_axis_offsets(
        [previous, top], viewport_min=0, viewport_max=100
    )

    assert bottom_offsets["bottom"] <= 0
    assert top_offsets["top"] >= 0


def test_solver_prefers_zero_painted_overlap_when_requested_gap_cannot_fit():
    pages = [
        PageVisualBands(
            "p1",
            (_band("old", "p1", 0, 100, 40, 60),),
            gap_px=20,
            anchor="center",
        ),
        PageVisualBands(
            "p2",
            (_band("new", "p2", 0, 100, 50, 70),),
            gap_px=99,
            anchor="center",
        ),
    ]

    offsets = solve_page_axis_offsets(pages, viewport_min=20, viewport_max=80)

    shifted_min = 50 + offsets["p2"]
    shifted_max = 70 + offsets["p2"]
    assert shifted_max <= 40 or shifted_min >= 60
