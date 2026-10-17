"""Locator labels (``S2 P3 T1（第 17 行）``) for every line-numbered message."""

from copy import deepcopy

from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.domain.timing import (
    SubtitleLoadingSettings,
    TimingChar,
    TimingLine,
    TimingTrack,
    TrackPage,
    TrackPagePlan,
    TrackSection,
)
from krok_helper.subtitle_render.engine.layout.display.diagnostics import (
    LayoutMarginBox,
    LayoutMarginPorts,
    LayoutMarginWarning,
    LayoutTimingDiagnostic,
    build_timing_window_diagnostics,
    resolve_layout_margin_warnings,
)
from krok_helper.subtitle_render.engine.layout.line_locator import (
    LineLocator,
    line_label,
    line_locators,
)
from krok_helper.subtitle_render.engine.layout.page.plan import build_page_plan
from krok_helper.subtitle_render.engine.layout.display.resolver import (
    display_line_compute_kwargs,
)
from krok_helper.subtitle_render.engine.timing.timeline import compute_display_lines
from krok_helper.subtitle_render.frontend.workflow.export_view import (
    format_warning_lines,
)
from krok_helper.subtitle_render.sources.reload import merge_reloaded_track


def _track(starts: list[int]) -> TimingTrack:
    return TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar(text=chr(ord("a") + index), start_ms=start)],
                end_ms=start + 1000,
            )
            for index, start in enumerate(starts)
        ]
    )


def test_labels_follow_implicit_sections_and_pages():
    """默认两行一页；演唱间隔超过段间隔时换段并从 P1 重新编号。"""
    track = _track([1000, 5000, 6000, 15000, 16000, 17000])
    style = Style()

    assert [line_label(track, style, i) for i in range(6)] == [
        "S1 P1 T1（第 1 行）",
        "S1 P1 T2（第 2 行）",
        "S1 P2 T1（第 3 行）",
        "S2 P1 T1（第 4 行）",
        "S2 P1 T2（第 5 行）",
        "S2 P2 T1（第 6 行）",
    ]
    # 与渲染期同一套分页：逐行对得上。
    display = compute_display_lines(track, **display_line_compute_kwargs(style))
    locators = line_locators(track, style)
    assert [locators[i].section - 1 for i in range(6)] == [
        item.section_index for item in display
    ]


def test_labels_follow_authoritative_page_plan_and_screen_rows():
    """页计划优先；底部锚定的三行布局里两行页占 T2、T3。"""
    track = _track([1000, 2000, 3000])
    track.page_plan = TrackPagePlan(
        [TrackSection([TrackPage(2, "default"), TrackPage(1, "default")])]
    )
    style = Style(line_alignments=[None, None, None], line_y_position="bottom")

    assert [line_label(track, style, i) for i in range(3)] == [
        "S1 P1 T2（第 1 行）",
        "S1 P1 T3（第 2 行）",
        "S1 P2 T1（第 3 行）",
    ]
    # 单行页恒为天然 T1（ForceBottom 是渲染期机制）。
    assert LineLocator(line_index=2, section=1, page=2, track=1).label() == (
        "S1 P2 T1（第 3 行）"
    )


def test_untimed_lines_fall_back_to_source_line_number():
    track = _track([1000, 2000, 3000])
    track.lines.insert(1, TimingLine(chars=[]))
    style = Style()

    # 空行不占页位：它只保留歌词表行号，后面两行仍是默认页的 T1 / T2。
    assert line_label(track, style, 1) == "第 2 行"
    assert line_label(track, style, 0) == "S1 P1 T1（第 1 行）"
    assert line_label(track, style, 2) == "S1 P1 T2（第 3 行）"


def test_page_plan_from_loading_settings_matches_implicit_locators():
    track = _track([1000, 2000, 3000, 4000, 5000])
    style = Style()
    implicit = [line_label(track, style, i) for i in range(5)]

    track.page_plan = build_page_plan(track, SubtitleLoadingSettings(), style)

    assert [line_label(track, style, i) for i in range(5)] == implicit


def test_margin_warning_carries_locator_label():
    track = _track([1000, 2000, 3000])
    style = Style()

    def resolve_display_lines(source_track, source_style, width):
        from krok_helper.subtitle_render.engine.timing.timeline import DisplayLine

        return [
            DisplayLine(line=line, lane=0, display_start_ms=0, display_end_ms=0)
            for line in source_track.lines
        ]

    ports = LayoutMarginPorts(
        resolve_display_lines=resolve_display_lines,
        measure_line=lambda *_args: LayoutMarginBox(
            left=-5, right=30, margin_left=20, margin_right=20
        ),
    )

    warnings = resolve_layout_margin_warnings(track, style, 100, ports=ports)

    assert [warning.line_index for warning in warnings] == [0, 1, 2]
    assert warnings[1].label() == "S1 P1 T2（第 2 行）"
    assert format_warning_lines(warnings) == (
        "S1 P1 T1（第 1 行）、S1 P1 T2（第 2 行）、S1 P2 T1（第 3 行）"
    )


def test_layout_issues_dialog_shows_locator_for_every_row(qapp):
    """「当前字幕诊断」列表与详情都必须带 S/P/T，不能只报第 N 行。"""
    from krok_helper.subtitle_render.frontend.dialogs.workspace_dialogs import (
        LayoutIssue,
        LayoutIssuesDialog,
        TimingIssue,
    )

    margin = LayoutMarginWarning(
        line_index=4,
        text="第五句",
        level="overflow",
        left=-20,
        right=1940,
        location="S1 P3 T1（第 5 行）",
    )
    timing = TimingIssue(
        track_index=0,
        source_name="主字幕",
        diagnostic=LayoutTimingDiagnostic(
            kind="timing",
            line_indices=(4,),
            title="时间窗口自动压缩",
            summary="S1 P3 T1（第 5 行）「第五句」：退场被提前",
            detail="S1 P3 T1（第 5 行）「第五句」",
        ),
    )
    dialog = LayoutIssuesDialog(
        [LayoutIssue(track_index=0, source_name="主字幕", warning=margin), timing]
    )
    try:
        texts = [
            dialog._list_widget.item(index).text()
            for index in range(dialog._list_widget.count())
        ]
        assert any("主字幕 · S1 P3 T1（第 5 行）" in text for text in texts)
        assert all("S1 P3 T1（第 5 行）" in text for text in texts)
    finally:
        dialog.close()
        dialog.deleteLater()
        qapp.processEvents()


def test_hand_built_margin_warning_keeps_plain_line_number():
    from krok_helper.subtitle_render.engine.layout.display.diagnostics import (
        LayoutMarginWarning,
    )

    warning = LayoutMarginWarning(
        line_index=16, text="x", level="overflow", left=-1, right=10
    )

    assert warning.label() == "第 17 行"
    assert format_warning_lines([warning]) == "第 17 行"


def test_timing_window_diagnostic_references_carry_locators():
    track = _track([1000, 4000, 5200])
    style = Style()
    ideal = compute_display_lines(track, **display_line_compute_kwargs(style))
    final = list(ideal)

    diagnostics = build_timing_window_diagnostics(
        track,
        style,
        ideal=ideal,
        synchronized=ideal,
        animation_candidate=ideal,
        final=final,
        adjustments=[],
        entry_animation_ms_of=lambda *_args: 0,
        auto_exit_reserve_ms_of=lambda *_args: 0,
    )

    assert diagnostics == []  # 无压缩时不产出诊断
    # 人为压缩第 3 行入场窗口，诊断必须带 S/P/T。
    final[2] = type(ideal[2])(
        line=ideal[2].line,
        lane=ideal[2].lane,
        display_start_ms=ideal[2].display_start_ms + 500,
        display_end_ms=ideal[2].display_end_ms,
    )
    diagnostics = build_timing_window_diagnostics(
        track,
        style,
        ideal=ideal,
        synchronized=ideal,
        animation_candidate=ideal,
        final=final,
        adjustments=[],
        entry_animation_ms_of=lambda *_args: 0,
        auto_exit_reserve_ms_of=lambda *_args: 0,
    )

    assert len(diagnostics) == 1
    diagnostic = diagnostics[0]
    assert diagnostic.line_indices == (2,)
    assert diagnostic.summary.startswith("S1 P2 T1（第 3 行）")
    assert "S1 P2 T1（第 3 行）" in diagnostic.detail


def test_reload_conflicts_reference_section_page_and_row():
    baseline = TimingTrack(
        lines=[
            TimingLine(chars=[TimingChar("a", 1000)], end_ms=1500),
            TimingLine(chars=[TimingChar("b", 2000)], end_ms=2500),
            TimingLine(chars=[TimingChar("c", 3000)], end_ms=3500),
        ]
    )
    current = deepcopy(baseline)
    current.lines[2].layout_index = 3
    candidate = TimingTrack(
        lines=[
            TimingLine(chars=[TimingChar("a", 1200)], end_ms=1700),
            TimingLine(chars=[TimingChar("b", 2200)], end_ms=2700),
        ]
    )

    with_style = merge_reloaded_track(current, baseline, candidate, style=Style())
    without_style = merge_reloaded_track(current, baseline, candidate)

    assert with_style.conflicts == ("S1 P2 T1（第 3 行）已被删除，原有字幕设置无法定位",)
    assert without_style.conflicts == ("第 3 行已被删除，原有字幕设置无法定位",)
