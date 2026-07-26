from krok_helper.subtitle_render.engine.page_placement import (
    LineVisualBand,
    PageVisualBands,
    solve_page_axis_offset_windows,
    solve_page_axis_offsets,
    time_windows_overlap,
)


def _band(line, page, start, end, top, bottom):
    return LineVisualBand(line, page, start, end, top, bottom)


def test_half_open_time_windows_do_not_overlap_at_shared_boundary():
    first = _band("a", "p1", 0, 1000, 0, 20)
    second = _band("b", "p2", 1000, 2000, 0, 20)

    assert not time_windows_overlap(first, second)


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
    assert offsets["p2"] == -95


def test_solver_checks_all_still_visible_previous_pages():
    pages = [
        PageVisualBands(
            "p1", (_band("a", "p1", 0, 5000, 20, 40),), anchor="start"
        ),
        PageVisualBands(
            "p2", (_band("b", "p2", 1000, 4000, 45, 65),), anchor="start"
        ),
        PageVisualBands(
            "p3",
            (_band("c", "p3", 2000, 3000, 20, 65),),
            gap_px=5,
            anchor="start",
        ),
    ]

    offsets = solve_page_axis_offsets(pages, viewport_min=0, viewport_max=160)

    assert offsets["p3"] == 50


def test_center_page_chooses_nearest_valid_direction():
    pages = [
        PageVisualBands(
            "p1", (_band("a", "p1", 0, 3000, 40, 60),), anchor="center"
        ),
        PageVisualBands(
            "p2",
            (_band("b", "p2", 1000, 2000, 50, 70),),
            gap_px=5,
            anchor="center",
        ),
    ]

    offsets = solve_page_axis_offsets(pages, viewport_min=0, viewport_max=120)

    assert offsets["p2"] == 15


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
            "p1", (_band("old", "p1", 0, 100, 0, 30),), anchor="center"
        ),
        PageVisualBands(
            "p2",
            (_band("new", "p2", 0, 100, 10, 40),),
            gap_px=10,
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
            "p1", (_band("old", "p1", 0, 100, 70, 100),), anchor="center"
        ),
        PageVisualBands(
            "p2",
            (_band("new", "p2", 0, 100, 60, 90),),
            gap_px=10,
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
        "p1", (_band("old", "p1", 0, 100, 200, 290),), anchor="center"
    )
    near_bottom = PageVisualBands(
        "p2",
        (_band("new", "p2", 0, 100, 240, 260),),
        gap_px=10,
        anchor="end",
    )
    at_bottom = PageVisualBands(
        "p3",
        (_band("new", "p3", 0, 100, 280, 300),),
        gap_px=10,
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
            "p1", (_band("old", "p1", 0, 100, 40, 60),), anchor="center"
        ),
        PageVisualBands(
            "p2",
            (_band("new", "p2", 0, 100, 50, 70),),
            gap_px=20,
            anchor="center",
        ),
    ]

    offsets = solve_page_axis_offsets(pages, viewport_min=20, viewport_max=80)

    shifted_min = 50 + offsets["p2"]
    shifted_max = 70 + offsets["p2"]
    assert shifted_max <= 40 or shifted_min >= 60
