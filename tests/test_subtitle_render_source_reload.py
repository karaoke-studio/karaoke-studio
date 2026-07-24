from __future__ import annotations

from copy import deepcopy

from krok_helper.subtitle_render.models import (
    GuideSymbol,
    TimingChar,
    TimingLine,
    TimingTrack,
)
from krok_helper.subtitle_render.source_reload import merge_reloaded_track


def _track(*, first_ms: int = 1000, role: str | None = "源角色") -> TimingTrack:
    return TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("a", first_ms, role_label=role),
                    TimingChar("b", first_ms + 500, role_label=role),
                ],
                end_ms=first_ms + 1000,
            )
        ]
    )


def _guide(*, prefix: tuple[str, ...] = ()) -> GuideSymbol:
    return GuideSymbol(
        path_commands=(("M", 0.0, 0.0), ("L", 1.0, 1.0)),
        replacement_prefix=prefix,
        count=max(len(prefix), 1),
    )


def test_timestamp_only_reload_preserves_all_renderer_overlays() -> None:
    baseline = _track()
    current = deepcopy(baseline)
    line = current.lines[0]
    line.layout_index = 2
    line.break_before = "page"
    line.display_start_override_ms = 700
    line.guide_symbol = _guide()
    line.inline_guide_symbols = {1: _guide()}
    line.chars[0].role_label = "手工角色"

    result = merge_reloaded_track(current, baseline, _track(first_ms=2200))

    assert result.timing_only is True
    assert result.structure_changed is False
    assert result.conflicts == ()
    assert [char.start_ms for char in result.track.lines[0].chars] == [2200, 2700]
    assert result.track.lines[0].end_ms == 3200
    assert result.track.lines[0].layout_index == 2
    assert result.track.lines[0].break_before == "page"
    assert result.track.lines[0].display_start_override_ms == 700
    assert result.track.lines[0].guide_symbol is not None
    assert 1 in result.track.lines[0].inline_guide_symbols
    assert [char.role_label for char in result.track.lines[0].chars] == [
        "手工角色",
        "源角色",
    ]


def test_source_roles_update_except_for_sparse_user_overrides() -> None:
    baseline = _track(role="旧源角色")
    current = deepcopy(baseline)
    current.lines[0].chars[1].role_label = "手工角色"
    candidate = _track(first_ms=1200, role="新源角色")

    result = merge_reloaded_track(current, baseline, candidate)

    assert result.conflicts == ()
    assert [char.role_label for char in result.track.lines[0].chars] == [
        "新源角色",
        "手工角色",
    ]


def test_changed_text_reports_unmappable_guide_symbols() -> None:
    baseline = _track()
    current = deepcopy(baseline)
    current.lines[0].guide_symbol = _guide(prefix=("a",))
    current.lines[0].inline_guide_symbols = {1: _guide()}
    candidate = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("x", 1000), TimingChar("b", 1500)],
                end_ms=2000,
            )
        ]
    )

    result = merge_reloaded_track(current, baseline, candidate)

    assert result.structure_changed is True
    assert len(result.conflicts) == 2
    assert result.track.lines[0].guide_symbol is None
    assert result.track.lines[0].inline_guide_symbols == {}


def test_inserted_line_keeps_overlays_on_exactly_matched_lines() -> None:
    baseline = TimingTrack(
        lines=[
            TimingLine(chars=[TimingChar("a", 1000)], end_ms=1500),
            TimingLine(chars=[TimingChar("b", 2000)], end_ms=2500),
        ]
    )
    current = deepcopy(baseline)
    current.lines[1].layout_index = 3
    candidate = TimingTrack(
        lines=[
            TimingLine(chars=[TimingChar("x", 500)], end_ms=900),
            TimingLine(chars=[TimingChar("a", 1200)], end_ms=1700),
            TimingLine(chars=[TimingChar("b", 2200)], end_ms=2700),
        ]
    )

    result = merge_reloaded_track(current, baseline, candidate)

    assert result.structure_changed is True
    assert result.conflicts == ()
    assert [line.layout_index for line in result.track.lines] == [0, 0, 3]


def test_structural_change_does_not_guess_between_duplicate_lyric_lines() -> None:
    baseline = TimingTrack(
        lines=[
            TimingLine(chars=[TimingChar("a", 1000)], end_ms=1500),
            TimingLine(chars=[TimingChar("a", 2000)], end_ms=2500),
        ]
    )
    current = deepcopy(baseline)
    current.lines[1].layout_index = 4
    candidate = TimingTrack(
        lines=[
            TimingLine(chars=[TimingChar("x", 500)], end_ms=900),
            TimingLine(chars=[TimingChar("a", 1200)], end_ms=1700),
            TimingLine(chars=[TimingChar("a", 2200)], end_ms=2700),
        ]
    )

    result = merge_reloaded_track(current, baseline, candidate)

    assert result.structure_changed is True
    assert any("无法唯一定位" in conflict for conflict in result.conflicts)
    assert all(line.layout_index == 0 for line in result.track.lines)


def test_explicit_refresh_drops_only_the_legacy_page_projection() -> None:
    baseline = _track()
    current = deepcopy(baseline)
    current.lines[0].layout_index = 3
    current.lines[0].break_before = "page"
    current.lines[0].display_start_override_ms = 700
    current.lines[0].chars[0].role_label = "手工角色"

    result = merge_reloaded_track(
        current,
        baseline,
        _track(first_ms=2200),
        preserve_page_structure=False,
    )

    assert result.conflicts == ()
    assert result.track.page_plan is None
    assert result.track.lines[0].layout_index == 0
    assert result.track.lines[0].break_before == "none"
    assert result.track.lines[0].display_start_override_ms == 700
    assert result.track.lines[0].chars[0].role_label == "手工角色"
