from krok_helper.subtitle_render.engine.page_placement import (
    LineVisualBand,
    PageVisualBands,
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


def test_page_larger_than_viewport_uses_deterministic_minimum_overlap():
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
    assert first["p2"] == -20
