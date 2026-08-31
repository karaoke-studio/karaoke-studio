"""Display-line resolution orchestration independent from concrete rendering."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Hashable, Sequence
from dataclasses import dataclass, replace

from krok_helper.subtitle_render.engine.layout.display.diagnostics import (
    TimingCollisionAdjustment,
)
from krok_helper.subtitle_render.engine.layout.display.schedule import (
    SyncCollisionBands,
    apply_constrained_page_sync,
    collision_time_window_name,
    display_line_collision_time_window,
    display_line_static_collision_window,
)
from krok_helper.subtitle_render.engine.layout.line.style import (
    auto_entry_reserve_resolver,
    auto_exit_reserve_resolver,
    bottom_align_resolver,
    entry_animation_resolver,
    exit_animation_ms,
    exit_animation_resolver,
    force_top_bottom_resolver,
    lane_count,
    line_end_ms,
    line_start_ms,
    row_count_resolver,
    style_for_line,
    style_for_line_display_window,
    vertical_position_resolver,
)
from krok_helper.subtitle_render.engine.layout.page.placement import (
    LineVisualBand,
    PageVisualBands,
    bands_require_separation,
    solve_page_axis_offsets,
    time_windows_overlap,
)
from krok_helper.subtitle_render.engine.layout.display.section_edges import (
    section_edge_context,
)
from krok_helper.subtitle_render.engine.layout.display.signal import (
    signal_head_context,
    signal_lead_in_ms,
)
from krok_helper.subtitle_render.engine.timing.timeline import DisplayLine
from krok_helper.subtitle_render.engine.timing.show_time import (
    compression_floor_ms,
    protect_time_ms,
)
from krok_helper.subtitle_render.engine.value_signature import value_signature
from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.domain.timing import TimingLine, TimingTrack


DisplayLines = list[DisplayLine]
CollisionPairs = tuple[tuple[int, int], ...]
MeasuredCollisionBand = tuple[int, Hashable, LineVisualBand, float]
MeasuredCollisionBands = list[MeasuredCollisionBand]


@dataclass(frozen=True)
class CollisionLineGeometry:
    """Renderer-supplied static ink geometry for one display line."""

    axis_min: float
    axis_max: float
    cross_min: float
    cross_max: float
    axis_anchor: float | None
    gap_px: float


def build_measured_collision_bands(
    display_lines: DisplayLines,
    style: Style,
    geometries: Sequence[CollisionLineGeometry | None],
    *,
    time_window: str | None = None,
) -> MeasuredCollisionBands:
    """Attach layout-owned timing and page identity to renderer geometry."""

    if time_window is None:
        time_window = (
            "stable" if style.allow_entry_exit_animation_overlap else "display"
        )
    measured: MeasuredCollisionBands = []
    for render_index, display_line in enumerate(display_lines):
        geometry = geometries[render_index] if render_index < len(geometries) else None
        if geometry is None:
            continue
        collision_start, collision_end = display_line_collision_time_window(
            display_line,
            style,
            time_window=time_window,
        )
        if collision_end <= collision_start:
            continue
        page_id = (
            int(display_line.section_index),
            int(display_line.page_index),
        )
        measured.append(
            (
                render_index,
                page_id,
                LineVisualBand(
                    line_id=render_index,
                    page_id=page_id,
                    display_start_ms=collision_start,
                    display_end_ms=collision_end,
                    axis_min=float(geometry.axis_min),
                    axis_max=float(geometry.axis_max),
                    entry_start_ms=int(display_line.display_start_ms),
                    axis_anchor=(
                        None
                        if geometry.axis_anchor is None
                        else float(geometry.axis_anchor)
                    ),
                    cross_min=float(geometry.cross_min),
                    cross_max=float(geometry.cross_max),
                ),
                max(float(geometry.gap_px), 0.0),
            )
        )
    return measured


def collision_squeeze_pairs(
    measured: MeasuredCollisionBands,
) -> CollisionPairs:
    """Return authored-position conflicts across distinct lyric pages."""

    conflicts: list[tuple[int, int]] = []
    for incoming_pos, (
        incoming_index,
        incoming_page,
        incoming_band,
        _incoming_gap,
    ) in enumerate(measured):
        for previous_index, previous_page, previous_band, _previous_gap in measured[
            :incoming_pos
        ]:
            if previous_page == incoming_page:
                continue
            if not time_windows_overlap(incoming_band, previous_band):
                continue
            if not bands_require_separation(incoming_band, previous_band, 0.0):
                continue
            pair = (previous_index, incoming_index)
            if pair not in conflicts:
                conflicts.append(pair)
    return tuple(conflicts)


def secondary_displacement_squeeze_pairs(
    measured: MeasuredCollisionBands,
    display_lines: DisplayLines,
    style: Style,
    *,
    viewport_max: float,
) -> CollisionPairs:
    """Return cascade conflicts introduced by rigid page displacement."""

    if not measured:
        return ()

    page_order: list[Hashable] = []
    page_entries: dict[Hashable, list[tuple[int, LineVisualBand, float]]] = {}
    page_styles: dict[Hashable, Style] = {}
    for render_index, page_id, band, gap in measured:
        if page_id not in page_entries:
            page_order.append(page_id)
            page_entries[page_id] = []
        page_entries[page_id].append((render_index, band, gap))
        page_styles.setdefault(
            page_id,
            style_for_line(style, display_lines[render_index].line),
        )

    pages: list[PageVisualBands] = []
    for page_id in page_order:
        page_style = page_styles[page_id]
        position = page_style.line_y_position
        anchor = (
            "start"
            if position == "top"
            else "center"
            if position == "center"
            else "end"
        )
        if style.vertical:
            anchor = "end"
        pages.append(
            PageVisualBands(
                page_id=page_id,
                bands=tuple(
                    band for _render_index, band, _gap in page_entries[page_id]
                ),
                gap_px=max(float(page_style.line_gap_px), 0.0),
                anchor=anchor,
            )
        )

    offsets = solve_page_axis_offsets(
        pages,
        viewport_min=0.0,
        viewport_max=float(viewport_max),
    )
    if not any(float(offset) != 0.0 for offset in offsets.values()):
        return ()

    conflicts: list[tuple[int, int]] = []
    for incoming_pos, (
        incoming_index,
        incoming_page,
        incoming_band,
        _incoming_gap,
    ) in enumerate(measured):
        incoming_offset = float(offsets.get(incoming_page, 0.0))
        if incoming_offset == 0.0:
            continue
        for (
            previous_index,
            previous_page,
            previous_band,
            _previous_gap,
        ) in measured[:incoming_pos]:
            if previous_page == incoming_page:
                continue
            if not time_windows_overlap(incoming_band, previous_band):
                continue
            previous_offset = float(offsets.get(previous_page, 0.0))
            if previous_offset == 0.0:
                continue
            if bands_require_separation(incoming_band, previous_band, 0.0):
                continue
            shifted_previous = previous_band.shifted(previous_offset)
            if not bands_require_separation(
                incoming_band,
                shifted_previous,
                0.0,
            ):
                continue
            pair = (previous_index, incoming_index)
            if pair not in conflicts:
                conflicts.append(pair)
    return tuple(conflicts)


def retime_measured_collision_bands(
    measured: MeasuredCollisionBands,
    display_lines: DisplayLines,
    style: Style,
    changed_indices: tuple[int, ...],
    *,
    time_window: str = "stable",
) -> MeasuredCollisionBands | None:
    """Reuse measured rectangles when only display boundaries changed."""

    changed = set(changed_indices)
    measured_indices = {
        render_index for render_index, _page, _band, _gap in measured
    }
    if not changed.issubset(measured_indices):
        return None
    retimed: MeasuredCollisionBands = []
    for render_index, page_id, band, gap in measured:
        if render_index not in changed:
            retimed.append((render_index, page_id, band, gap))
            continue
        collision_start, collision_end = display_line_collision_time_window(
            display_lines[render_index],
            style,
            time_window=time_window,
        )
        if collision_end <= collision_start:
            continue
        retimed.append(
            (
                render_index,
                page_id,
                replace(
                    band,
                    display_start_ms=int(collision_start),
                    display_end_ms=int(collision_end),
                    entry_start_ms=int(
                        display_lines[render_index].display_start_ms
                    ),
                ),
                gap,
            )
        )
    return retimed


def fill_section_time_from_measurements(
    display_lines: DisplayLines,
    style: Style,
    measured: MeasuredCollisionBands,
    *,
    viewport_max: float,
    time_window: str,
) -> DisplayLines:
    """Extend automatic exits using final measured page placement."""

    if not style.auto_fill_section_time or not display_lines:
        return display_lines
    bands = {
        render_index: band
        for render_index, _page_id, band, _gap in measured
    }
    if not bands:
        return display_lines

    page_order: list[tuple[int, int]] = []
    page_indices: dict[tuple[int, int], list[int]] = {}
    for index, item in enumerate(display_lines):
        page_id = (int(item.section_index), int(item.page_index))
        if page_id not in page_indices:
            page_order.append(page_id)
            page_indices[page_id] = []
        page_indices[page_id].append(index)
    next_page: dict[tuple[int, int], tuple[int, int] | None] = {}
    for position, page_id in enumerate(page_order):
        following = (
            page_order[position + 1]
            if position + 1 < len(page_order)
            else None
        )
        next_page[page_id] = (
            following
            if following is not None and following[0] == page_id[0]
            else None
        )

    page_entries: dict[tuple[int, int], list[tuple[LineVisualBand, float]]] = {}
    for _render_index, page_id, band, gap in measured:
        page_entries.setdefault(page_id, []).append((band, gap))
    pages: list[PageVisualBands] = []
    for page_id in page_order:
        entries = page_entries.get(page_id, [])
        if not entries:
            continue
        page_style = style_for_line(
            style,
            display_lines[page_indices[page_id][0]].line,
        )
        position = page_style.line_y_position
        anchor = (
            "start"
            if position == "top"
            else "center"
            if position == "center"
            else "end"
        )
        if style.vertical:
            anchor = "end"
        pages.append(
            PageVisualBands(
                page_id=page_id,
                bands=tuple(band for band, _gap in entries),
                gap_px=max((gap for _band, gap in entries), default=0.0),
                anchor=anchor,
            )
        )
    page_offsets = solve_page_axis_offsets(
        pages,
        viewport_min=0.0,
        viewport_max=float(viewport_max),
    )
    bands = {
        index: band.shifted(float(page_offsets.get(band.page_id, 0.0)))
        for index, band in bands.items()
    }

    def match_page_bands(
        source_indices: list[int],
        candidate_indices: list[int],
    ) -> dict[int, int]:
        sources = [index for index in source_indices if index in bands]
        candidates = [index for index in candidate_indices if index in bands]
        if not sources or not candidates:
            return {}
        costs: dict[tuple[int, int], float] = {}
        for source_pos, source_index in enumerate(sources):
            source = bands[source_index]
            source_height = max(float(source.axis_max - source.axis_min), 1.0)
            source_center = (source.axis_min + source.axis_max) / 2.0
            for candidate_pos, candidate_index in enumerate(candidates):
                candidate = bands[candidate_index]
                candidate_height = max(
                    float(candidate.axis_max - candidate.axis_min),
                    1.0,
                )
                candidate_center = (candidate.axis_min + candidate.axis_max) / 2.0
                center_distance = abs(candidate_center - source_center)
                tolerance = max(source_height, candidate_height)
                if center_distance > tolerance:
                    continue
                height_delta = abs(candidate_height - source_height)
                costs[(source_pos, candidate_pos)] = (
                    center_distance / tolerance
                    + 0.25 * height_delta / tolerance
                )

        memo: dict[
            tuple[int, int],
            tuple[int, float, tuple[tuple[int, int], ...]],
        ] = {}

        def solve(
            source_pos: int,
            used_mask: int,
        ) -> tuple[int, float, tuple[tuple[int, int], ...]]:
            key = source_pos, used_mask
            cached = memo.get(key)
            if cached is not None:
                return cached
            if source_pos >= len(sources):
                return 0, 0.0, ()
            best = solve(source_pos + 1, used_mask)
            for candidate_pos in range(len(candidates)):
                bit = 1 << candidate_pos
                cost = costs.get((source_pos, candidate_pos))
                if used_mask & bit or cost is None:
                    continue
                count, total, pairs = solve(source_pos + 1, used_mask | bit)
                proposal = (
                    count + 1,
                    total + cost,
                    ((source_pos, candidate_pos),) + pairs,
                )
                if (
                    proposal[0] > best[0]
                    or (
                        proposal[0] == best[0]
                        and proposal[1] < best[1] - 1e-9
                    )
                    or (
                        proposal[0] == best[0]
                        and abs(proposal[1] - best[1]) <= 1e-9
                        and proposal[2] < best[2]
                    )
                ):
                    best = proposal
            memo[key] = best
            return best

        _count, _cost, pairs = solve(0, 0)
        return {
            sources[source_pos]: candidates[candidate_pos]
            for source_pos, candidate_pos in pairs
        }

    changed = list(display_lines)
    gap_ms = max(int(style.line_lane_gap_ms), 0)
    for page_id in page_order:
        indices = page_indices[page_id]
        following = next_page[page_id]
        if following is None:
            page_collision_end = max(
                (
                    int(bands[index].display_end_ms)
                    for index in indices
                    if index in bands
                ),
                default=None,
            )
            if page_collision_end is None:
                continue
            targets = {index: page_collision_end for index in indices}
        else:
            candidates = [
                index for index in page_indices[following] if index in bands
            ]
            if not candidates:
                continue
            matches = match_page_bands(indices, candidates)
            targets = {
                index: int(bands[matched].display_start_ms) - gap_ms
                for index, matched in matches.items()
            }

        for index, collision_end in targets.items():
            item = changed[index]
            if item.line.display_end_override_ms is not None:
                continue
            full_end = int(collision_end)
            if time_window == "stable":
                full_end += exit_animation_ms(style, item.line)
            new_end = max(int(item.display_end_ms), full_end)
            if new_end != item.display_end_ms:
                changed[index] = replace(item, display_end_ms=new_end)
    return changed


class DisplayResolutionCache:
    """Bounded LRU cache that retains each display line's track owner."""

    def __init__(self, max_items: int = 24) -> None:
        self._max_items = max(int(max_items), 1)
        self._entries: OrderedDict[
            Hashable, tuple[object, tuple[DisplayLine, ...]]
        ] = OrderedDict()

    def get(self, key: Hashable) -> DisplayLines | None:
        cached = self._entries.get(key)
        if cached is None:
            return None
        self._entries.move_to_end(key)
        return list(cached[1])

    def put(
        self,
        key: Hashable,
        owner: object,
        display_lines: DisplayLines,
    ) -> None:
        self._entries[key] = (owner, tuple(display_lines))
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_items:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        self._entries.clear()


_DISPLAY_LINE_RESOLUTION_CACHE = DisplayResolutionCache(max_items=24)


def cached_display_line_resolution(key: Hashable) -> DisplayLines | None:
    return _DISPLAY_LINE_RESOLUTION_CACHE.get(key)


def store_display_line_resolution(
    key: Hashable,
    owner: object,
    display_lines: DisplayLines,
) -> None:
    _DISPLAY_LINE_RESOLUTION_CACHE.put(key, owner, display_lines)


def clear_display_line_resolution_cache() -> None:
    _DISPLAY_LINE_RESOLUTION_CACHE.clear()


@dataclass(frozen=True)
class DisplayResolutionPorts:
    """Concrete geometry and timing operations required by the resolver."""

    compute: Callable[..., DisplayLines]
    resolve_timing: Callable[[DisplayLines, bool], DisplayLines]
    collision_pairs: Callable[[DisplayLines], CollisionPairs]
    secondary_collision_pairs: Callable[[DisplayLines], CollisionPairs]
    fill_section_time: Callable[[DisplayLines], DisplayLines]
    apply_animation_guard: Callable[[DisplayLines, bool], DisplayLines]


@dataclass(frozen=True)
class StyleDisplayResolutionPorts:
    """Backend factory for one concrete canvas display-resolution pass."""

    build: Callable[[int, int, dict[str, object]], DisplayResolutionPorts]


def style_compression_floor_ms(style: Style) -> int:
    """Resolve this style's protect time into a concrete compression floor."""

    return compression_floor_ms(
        style.line_lead_in_ms,
        style.line_tail_ms,
        style.line_protect_ms,
    )


def _reserve_with_floor(
    resolver: Callable[[TimingLine], int], floor_ms: int
) -> Callable[[TimingLine], int]:
    if floor_ms <= 0:
        return resolver
    return lambda line: max(int(resolver(line)), floor_ms)


def display_line_compute_kwargs(style: Style) -> dict[str, object]:
    """Build the frame-independent timeline configuration for one style."""

    return {
        "lead_in_ms": style.line_lead_in_ms,
        "tail_ms": style.line_tail_ms,
        "lane_gap_ms": style.line_lane_gap_ms,
        "section_gap_ms": style.section_gap_ms,
        "sync_entry": style.sync_entry,
        "sync_ending": style.sync_ending,
        "sync_each_page": style.sync_each_page,
        "auto_fill_section_time": style.auto_fill_section_time,
        "section_ending_mode": style.section_ending_mode,
        "protect_ms": protect_time_ms(
            style.line_lead_in_ms,
            style.line_tail_ms,
            style.line_protect_ms,
        ),
        "lane_count": lane_count(style),
        "row_count_of": row_count_resolver(style),
        "bottom_align_of": bottom_align_resolver(style),
        "vertical_position_of": vertical_position_resolver(style),
        "force_bottom_of": force_top_bottom_resolver(style),
        # 「保护时间」与入场/退场动画的自动下限取大：两者都是自动压缩必须在走字
        # 两侧留下的余量，求解器只认一个数。
        "auto_entry_reserve_ms_of": _reserve_with_floor(
            auto_entry_reserve_resolver(style), style_compression_floor_ms(style)
        ),
        "auto_exit_reserve_ms_of": _reserve_with_floor(
            auto_exit_reserve_resolver(style), style_compression_floor_ms(style)
        ),
        "entry_animation_ms_of": entry_animation_resolver(style),
        "exit_animation_ms_of": exit_animation_resolver(style),
    }


@dataclass(frozen=True)
class AnimationGuardPorts:
    """Geometry and animation measurements needed by the timing guard."""

    entry_animation_ms: Callable[[TimingLine], int]
    exit_animation_ms: Callable[[TimingLine], int]
    measure: Callable[[DisplayLines, str], MeasuredCollisionBands]
    retime: Callable[
        [MeasuredCollisionBands, DisplayLines, tuple[int, ...], str],
        MeasuredCollisionBands | None,
    ]


def apply_animation_time_guard(
    style: Style,
    display_lines: DisplayLines,
    ports: AnimationGuardPorts,
    *,
    enforce_inter_page_gap: bool,
    adjustments: list[TimingCollisionAdjustment] | None = None,
) -> DisplayLines:
    """Restore animation windows and enforce measured collision separation."""

    if not display_lines:
        return display_lines

    guarded = list(display_lines)
    changed = False
    # 「保护时间」：自动压缩不得越过走字两侧的这个余量。
    floor_ms = style_compression_floor_ms(style)
    entry_durations: list[int] = []
    line_starts: list[int] = []
    line_ends: list[int] = []
    for index, item in enumerate(guarded):
        entry_duration = ports.entry_animation_ms(item.line)
        exit_duration = ports.exit_animation_ms(item.line)
        entry_durations.append(entry_duration)
        line_start = line_start_ms(item.line)
        line_end = line_end_ms(item.line)
        line_starts.append(line_start)
        line_ends.append(line_end)

        start = int(item.display_start_ms)
        end = int(item.display_end_ms)
        if item.line.display_start_override_ms is None and entry_duration > 0:
            start = min(start, max(line_start - entry_duration, 0))
        if item.line.display_end_override_ms is None and exit_duration > 0:
            end = max(end, line_end + exit_duration)
        if start != item.display_start_ms or end != item.display_end_ms:
            guarded[index] = replace(
                item,
                display_start_ms=start,
                display_end_ms=max(start, end),
            )
            changed = True

    if not enforce_inter_page_gap or style.allow_inter_page_line_overlap:
        return guarded if changed else display_lines

    time_window = (
        "stable" if style.allow_entry_exit_animation_overlap else "display"
    )
    measured = ports.measure(guarded, time_window)
    for _pass in range(max(len(guarded) * 3, 1)):
        adjusted = False
        changed_index: int | None = None
        for incoming_pos, (
            incoming_index,
            incoming_page,
            incoming_band,
            _incoming_gap,
        ) in enumerate(measured):
            incoming = guarded[incoming_index]
            for previous_index, previous_page, previous_band, _previous_gap in measured[
                :incoming_pos
            ]:
                if previous_page == incoming_page:
                    continue
                previous = guarded[previous_index]
                same_lane = int(previous.lane) == int(incoming.lane)
                if (
                    not same_lane
                    and not bands_require_separation(
                        incoming_band,
                        previous_band,
                        0.0,
                    )
                ):
                    continue
                required_gap = (
                    max(int(style.line_lane_gap_ms), 0) if same_lane else 0
                )
                required_start = int(previous_band.display_end_ms) + required_gap
                if int(incoming_band.display_start_ms) >= required_start:
                    continue
                overlap_ms = required_start - int(incoming_band.display_start_ms)

                if previous.line.display_end_override_ms is None:
                    if time_window == "stable":
                        stable_tail = max(
                            int(previous_band.display_end_ms)
                            - line_ends[previous_index]
                            - floor_ms,
                            0,
                        )
                    else:
                        stable_tail = max(
                            int(previous.display_end_ms)
                            - ports.exit_animation_ms(previous.line)
                            - line_ends[previous_index]
                            - floor_ms,
                            0,
                        )
                    delta = min(overlap_ms, stable_tail)
                    new_end = int(previous.display_end_ms) - delta
                    if new_end < previous.display_end_ms:
                        if adjustments is not None:
                            adjustments.append(
                                TimingCollisionAdjustment(
                                    previous_index=previous_index,
                                    incoming_index=incoming_index,
                                    boundary="exit",
                                    before_ms=int(previous.display_end_ms),
                                    after_ms=int(new_end),
                                )
                            )
                        guarded[previous_index] = replace(
                            previous,
                            display_end_ms=max(
                                int(previous.display_start_ms),
                                new_end,
                            ),
                        )
                        adjusted = True
                        changed_index = previous_index
                        changed = True
                        break

                if time_window == "stable":
                    stable_lead = max(
                        line_starts[incoming_index]
                        - int(incoming_band.display_start_ms)
                        - floor_ms,
                        0,
                    )
                else:
                    stable_lead = max(
                        line_starts[incoming_index]
                        - entry_durations[incoming_index]
                        - int(incoming.display_start_ms)
                        - floor_ms,
                        0,
                    )
                if incoming.line.display_start_override_ms is None:
                    delta = min(overlap_ms, stable_lead)
                    new_start = int(incoming.display_start_ms) + delta
                    latest_entry_start = max(
                        line_starts[incoming_index]
                        - max(entry_durations[incoming_index], floor_ms),
                        0,
                    )
                    new_start = min(new_start, latest_entry_start)
                    if new_start != incoming.display_start_ms:
                        if adjustments is not None:
                            adjustments.append(
                                TimingCollisionAdjustment(
                                    previous_index=previous_index,
                                    incoming_index=incoming_index,
                                    boundary="entry",
                                    before_ms=int(incoming.display_start_ms),
                                    after_ms=int(new_start),
                                )
                            )
                        guarded[incoming_index] = replace(
                            incoming,
                            display_start_ms=new_start,
                        )
                        adjusted = True
                        changed_index = incoming_index
                        changed = True
                        break
            if adjusted:
                break
        if not adjusted:
            break
        if changed_index is not None:
            retimed = ports.retime(
                measured,
                guarded,
                (changed_index,),
                time_window,
            )
            measured = (
                retimed
                if retimed is not None
                else ports.measure(guarded, time_window)
            )
    return guarded if changed else display_lines


def _sync_collision_bands(
    style: Style,
    display_lines: DisplayLines,
    animation_ports: AnimationGuardPorts,
    *,
    enforce_inter_page_gap: bool,
) -> SyncCollisionBands | None:
    """Measure static ink geometry so entry sync can see cross-lane overlap.

    Only the geometry is consumed; the clamp reads its timing from the lines
    being resolved, so measuring once against the pre-sync windows is enough.
    """

    if not style.sync_entry:
        # Only entry synchronization consults the clamp; measuring for the
        # ending side would cost a full ink pass per resolution round for
        # nothing.
        return None
    if not enforce_inter_page_gap or style.allow_inter_page_line_overlap:
        return None
    if not display_lines:
        return None
    measured = animation_ports.measure(
        display_lines,
        collision_time_window_name(style),
    )
    return {
        int(index): (band, float(gap))
        for index, _page_id, band, gap in measured
    }


def resolve_display_timing(
    style: Style,
    display_lines: DisplayLines,
    animation_ports: AnimationGuardPorts,
    *,
    enforce_inter_page_gap: bool,
    adjustments: list[TimingCollisionAdjustment] | None = None,
) -> DisplayLines:
    """Apply page synchronization before measured animation-window guarding."""

    synchronized = apply_constrained_page_sync(
        display_lines,
        style,
        collision_bands=_sync_collision_bands(
            style,
            display_lines,
            animation_ports,
            enforce_inter_page_gap=enforce_inter_page_gap,
        ),
        enforce_inter_page_gap=enforce_inter_page_gap,
    )
    return apply_animation_time_guard(
        style,
        synchronized,
        animation_ports,
        enforce_inter_page_gap=enforce_inter_page_gap,
        adjustments=adjustments,
    )


def resolve_display_lines(
    *,
    avoid_collisions: bool,
    auto_fill_section_time: bool,
    ports: DisplayResolutionPorts,
) -> DisplayLines:
    """Run the stable multi-pass display-line resolution policy.

    The caller supplies rendering-specific measurement operations.  This module
    owns only the ordering and data flow between those operations, so layout
    policy no longer depends on the Painter implementation.
    """

    ideal = ports.compute(
        adjust_same_position=False,
        dynamic_single_page_reflow=not avoid_collisions,
        independent_line_entry=True,
    )
    resolved = ideal
    timing_resolved = False
    if avoid_collisions:
        # ForceBottom inspects the schedule only after automatic timing has
        # separated pages; measuring the raw lead/tail windows latches stale
        # spatial reflow decisions.
        resolved = ports.resolve_timing(resolved, True)
        timing_resolved = True
        force_bottom_pairs = ports.collision_pairs(resolved)
        if force_bottom_pairs:
            resolved = ports.compute(
                adjust_same_position=False,
                force_bottom_pairs=force_bottom_pairs,
                dynamic_single_page_reflow=True,
                independent_line_entry=True,
            )
            resolved = ports.resolve_timing(resolved, True)
        squeeze_pairs = ports.collision_pairs(resolved)
        if squeeze_pairs:
            resolved = ports.compute(
                adjust_same_position=False,
                squeeze_pairs=squeeze_pairs,
                force_bottom_pairs=force_bottom_pairs,
                dynamic_single_page_reflow=True,
                independent_line_entry=True,
            )
            resolved = ports.resolve_timing(resolved, True)
        secondary_pairs = ports.secondary_collision_pairs(resolved)
        if secondary_pairs:
            combined_pairs = tuple(dict.fromkeys((*squeeze_pairs, *secondary_pairs)))
            resolved = ports.compute(
                adjust_same_position=False,
                squeeze_pairs=combined_pairs,
                force_bottom_pairs=force_bottom_pairs,
                dynamic_single_page_reflow=True,
                independent_line_entry=True,
            )
            timing_resolved = False
    if not timing_resolved:
        resolved = ports.resolve_timing(resolved, avoid_collisions)
    # Geometry-dependent section filling is the final layout pass.  It may
    # extend windows, so restore the animation guard without changing geometry.
    if auto_fill_section_time:
        filled = ports.fill_section_time(resolved)
        if filled != resolved:
            resolved = ports.apply_animation_guard(filled, avoid_collisions)
    return resolved


def resolve_display_lines_for_style(
    track: TimingTrack,
    style: Style,
    compute_kwargs: dict[str, object],
    ports: StyleDisplayResolutionPorts,
    *,
    logical_w: int | None = None,
    logical_h: int | None = None,
) -> DisplayLines:
    """Resolve and cache one style's display lines on a normalized canvas."""

    base_kwargs = {
        **compute_kwargs,
        "sync_entry": False,
        "sync_ending": False,
        "auto_fill_section_time": False,
    }
    signal_heads = signal_head_context(track, style)
    if signal_heads is not None:
        base_kwargs["signal_head_indexes"] = signal_heads
        base_kwargs["signal_lead_ms"] = signal_lead_in_ms(style)
    # 段首/段尾页标记供逐行动画解析（style_for_line）读取；此处注册后，
    # 本函数产出的显示窗口与后续布局计划看到的替换结果保持一致。
    section_edge_context(track, style)
    if logical_w is None or logical_h is None:
        default_h = max(int(style.layout_reference_height), 1)
        default_w = max(int(round(default_h * 16 / 9)), 1)
        logical_w = default_w if logical_w is None else logical_w
        logical_h = default_h if logical_h is None else logical_h
    logical_w = max(int(logical_w), 1)
    logical_h = max(int(logical_h), 1)
    cache_key = (
        logical_w,
        logical_h,
        id(track),
        value_signature(track),
        value_signature(style),
    )
    cached = cached_display_line_resolution(cache_key)
    if cached is not None:
        return cached
    resolved = resolve_display_lines(
        avoid_collisions=not style.allow_inter_page_line_overlap,
        auto_fill_section_time=style.auto_fill_section_time,
        ports=ports.build(logical_w, logical_h, base_kwargs),
    )
    store_display_line_resolution(cache_key, track, resolved)
    return resolved


__all__ = [
    "AnimationGuardPorts",
    "DisplayResolutionCache",
    "DisplayResolutionPorts",
    "StyleDisplayResolutionPorts",
    "apply_animation_time_guard",
    "cached_display_line_resolution",
    "clear_display_line_resolution_cache",
    "display_line_compute_kwargs",
    "resolve_display_lines",
    "resolve_display_lines_for_style",
    "resolve_display_timing",
    "store_display_line_resolution",
]
