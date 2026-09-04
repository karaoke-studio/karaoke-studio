from __future__ import annotations

from copy import deepcopy

import pytest

from krok_helper.subtitle_render.domain.models import (
    GuideSymbol,
    TimingChar,
    TimingLine,
    TimingTrack,
)
from krok_helper.subtitle_render.sources.reload import (
    apply_reloaded_tracks,
    merge_reloaded_track,
    plan_reloaded_tracks,
    prepare_reloaded_tracks,
    source_file_digest,
)


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


def test_source_owned_inline_emoji_symbols_update_on_reload() -> None:
    """@Emoji 参数变化（如 Zoom）热重载后必须生效：与基线一致的行内符号是
    源拥有的状态，不能被 current 里的旧值盖回去。"""

    def _emoji_track(zoom: int) -> TimingTrack:
        symbol = GuideSymbol(
            kind="bitmap",
            bitmap_before_path="avatar.png",
            bitmap_zoom_percent=zoom,
        )
        track = _track()
        track.lines[0].chars.insert(0, TimingChar("【A】", 1000, role_label="A"))
        track.lines[0].inline_guide_symbols = {0: symbol}
        return track

    baseline = _emoji_track(100)
    current = deepcopy(baseline)  # 渲染器未改动该符号
    candidate = _emoji_track(50)  # 源里 @Emoji Zoom=50

    result = merge_reloaded_track(current, baseline, candidate)

    assert result.conflicts == ()
    merged = result.track.lines[0].inline_guide_symbols[0]
    assert merged.bitmap_zoom_percent == 50


def test_source_owned_inline_emoji_symbols_removed_on_reload() -> None:
    """源里删除 @Emoji 后，未手工改过的行内符号应随源消失。"""

    def _emoji_track(with_symbol: bool) -> TimingTrack:
        track = _track()
        if with_symbol:
            track.lines[0].chars.insert(0, TimingChar("【A】", 1000, role_label="A"))
            track.lines[0].inline_guide_symbols = {
                0: GuideSymbol(kind="bitmap", bitmap_before_path="avatar.png")
            }
        return track

    baseline = _emoji_track(True)
    current = deepcopy(baseline)
    candidate = _emoji_track(False)

    result = merge_reloaded_track(current, baseline, candidate)

    assert result.conflicts == ()
    assert result.track.lines[0].inline_guide_symbols == {}
    assert [char.text for char in result.track.lines[0].chars] == ["a", "b"]


def test_locally_edited_inline_symbols_still_win_over_source() -> None:
    """用户在渲染器里改过的行内符号仍是本地覆盖，热重载不回退。"""
    baseline = _track()
    baseline.lines[0].inline_guide_symbols = {
        1: GuideSymbol(kind="bitmap", bitmap_before_path="old.png")
    }
    current = deepcopy(baseline)
    current.lines[0].inline_guide_symbols = {
        1: GuideSymbol(kind="bitmap", bitmap_before_path="user.png")
    }
    candidate = deepcopy(baseline)
    candidate.lines[0].inline_guide_symbols = {
        1: GuideSymbol(kind="bitmap", bitmap_before_path="new.png")
    }

    result = merge_reloaded_track(current, baseline, candidate)

    assert result.conflicts == ()
    assert (
        result.track.lines[0].inline_guide_symbols[1].bitmap_before_path == "user.png"
    )


def test_source_owned_inline_symbols_do_not_block_structure_migration() -> None:
    """结构变化时，仅含源 @Emoji 符号的行不算本地状态，不产生迁移冲突。"""
    baseline = _track()
    baseline.lines[0].inline_guide_symbols = {
        0: GuideSymbol(kind="bitmap", bitmap_before_path="avatar.png")
    }
    current = deepcopy(baseline)
    candidate = TimingTrack(
        lines=[TimingLine(chars=[TimingChar("x", 900)], end_ms=1400)]
    )

    result = merge_reloaded_track(current, baseline, candidate)

    assert result.conflicts == ()


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


def test_inserted_line_before_duplicate_lyric_lines_keeps_overlays() -> None:
    """重复份数未变时等值块对齐可信任：他处插入新行不再让重复行整体拒绝。"""
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
    assert result.conflicts == ()
    assert [line.layout_index for line in result.track.lines] == [0, 0, 4]
    assert [line.chars[0].start_ms for line in result.track.lines] == [500, 1200, 2200]


def test_unrelated_edit_keeps_overlays_on_stable_duplicate_sections() -> None:
    """回归用户反馈：重复副歌段 + 他处一行文本修改 → 静默合并，不连片弹冲突。"""
    baseline = TimingTrack(
        lines=[
            TimingLine(chars=[TimingChar("i", 1000)], end_ms=1500),
            TimingLine(chars=[TimingChar("a", 2000)], end_ms=2500),
            TimingLine(chars=[TimingChar("b", 2600)], end_ms=3100),
            TimingLine(chars=[TimingChar("m", 3200)], end_ms=3700),
            TimingLine(chars=[TimingChar("a", 4000)], end_ms=4500),
            TimingLine(chars=[TimingChar("b", 4600)], end_ms=5100),
        ]
    )
    current = deepcopy(baseline)
    current.lines[4].layout_index = 2
    current.lines[5].display_end_override_ms = 9000
    candidate = TimingTrack(
        lines=[
            TimingLine(chars=[TimingChar("x", 1000)], end_ms=1500),
            TimingLine(chars=[TimingChar("a", 2000)], end_ms=2500),
            TimingLine(chars=[TimingChar("b", 2600)], end_ms=3100),
            TimingLine(chars=[TimingChar("m", 3200)], end_ms=3700),
            TimingLine(chars=[TimingChar("a", 4200)], end_ms=4700),
            TimingLine(chars=[TimingChar("b", 4800)], end_ms=5300),
        ]
    )

    result = merge_reloaded_track(current, baseline, candidate)

    assert result.structure_changed is True
    assert result.conflicts == ()
    assert result.track.lines[4].layout_index == 2
    assert result.track.lines[5].display_end_override_ms == 9000
    assert result.track.lines[4].chars[0].start_ms == 4200


def test_deleted_duplicate_copy_still_requires_confirmation() -> None:
    """删除了一份重复行后，幸存副本与被删副本无法分辨，仍需用户确认。"""
    baseline = TimingTrack(
        lines=[
            TimingLine(chars=[TimingChar("a", 1000)], end_ms=1500),
            TimingLine(chars=[TimingChar("a", 2000)], end_ms=2500),
        ]
    )
    current = deepcopy(baseline)
    current.lines[0].layout_index = 4
    candidate = TimingTrack(
        lines=[TimingLine(chars=[TimingChar("a", 1200)], end_ms=1700)]
    )

    result = merge_reloaded_track(current, baseline, candidate)

    assert result.structure_changed is True
    assert any("无法唯一定位" in conflict for conflict in result.conflicts)
    assert all(line.layout_index == 0 for line in result.track.lines)


def test_added_duplicate_copy_still_requires_confirmation() -> None:
    """新增了一份重复行时，新旧副本的对应关系无法分辨，仍需用户确认。"""
    baseline = TimingTrack(
        lines=[TimingLine(chars=[TimingChar("a", 1000)], end_ms=1500)]
    )
    current = deepcopy(baseline)
    current.lines[0].layout_index = 4
    candidate = TimingTrack(
        lines=[
            TimingLine(chars=[TimingChar("a", 1000)], end_ms=1500),
            TimingLine(chars=[TimingChar("a", 2000)], end_ms=2500),
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


def test_multi_track_reload_plan_aggregates_project_source_merges() -> None:
    baseline = _track()
    primary = deepcopy(baseline)
    primary.lines[0].chars[0].role_label = "主轨角色"
    extra = deepcopy(baseline)
    extra.lines[0].chars[1].role_label = "附加轨角色"

    plan = plan_reloaded_tracks(
        baseline,
        _track(first_ms=2200),
        primary_track=primary,
        extra_tracks=((3, extra),),
    )

    assert plan.primary_merge is not None
    assert plan.primary_merge.track.lines[0].chars[0].role_label == "主轨角色"
    assert len(plan.extra_merges) == 1
    assert plan.extra_merges[0][0] == 3
    assert plan.extra_merges[0][1].track.lines[0].chars[1].role_label == "附加轨角色"
    assert plan.conflicts == ()
    assert plan.structure_changed is False
    assert plan.timing_only is True


def test_multi_track_reload_plan_deduplicates_shared_conflicts() -> None:
    baseline = _track()
    current = deepcopy(baseline)
    current.lines[0].inline_guide_symbols = {1: _guide()}
    candidate = TimingTrack(
        lines=[TimingLine(chars=[TimingChar("x", 1000)], end_ms=1500)]
    )

    plan = plan_reloaded_tracks(
        baseline,
        candidate,
        primary_track=current,
        extra_tracks=((0, deepcopy(current)),),
    )

    assert len(plan.conflicts) == 1
    assert plan.structure_changed is True
    assert plan.timing_only is False


def test_stable_reload_skips_parsing_when_digest_is_unchanged(tmp_path) -> None:
    path = tmp_path / "source.lrc"
    path.write_text("unchanged", encoding="utf-8")

    prepared = prepare_reloaded_tracks(
        path,
        seen_digest=source_file_digest(path),
        baseline=_track(),
        load_candidate=lambda _path: pytest.fail("unchanged source must not parse"),
    )

    assert prepared.candidate is None
    assert prepared.plan is None


def test_stable_reload_returns_project_wide_merge_plan(tmp_path) -> None:
    path = tmp_path / "source.lrc"
    path.write_text("changed", encoding="utf-8")
    baseline = _track()
    primary = deepcopy(baseline)
    primary.lines[0].chars[0].role_label = "主轨角色"

    prepared = prepare_reloaded_tracks(
        path,
        seen_digest="old-digest",
        baseline=baseline,
        load_candidate=lambda _path: _track(first_ms=2200),
        primary_track=primary,
    )

    assert prepared.candidate is not None
    assert prepared.plan is not None
    assert prepared.plan.timing_only is True
    assert prepared.plan.primary_merge is not None
    assert (
        prepared.plan.primary_merge.track.lines[0].chars[0].role_label
        == "主轨角色"
    )


def test_stable_reload_rejects_a_source_still_being_written(tmp_path) -> None:
    path = tmp_path / "source.lrc"
    path.write_text("first", encoding="utf-8")

    def mutate_while_loading(source_path):
        source_path.write_text("a different size", encoding="utf-8")
        return _track(first_ms=2200)

    with pytest.raises(OSError, match="字幕文件仍在写入"):
        prepare_reloaded_tracks(
            path,
            seen_digest="old-digest",
            baseline=_track(),
            load_candidate=mutate_while_loading,
            primary_track=_track(),
        )


def test_reload_plan_installs_tracks_through_project_contract() -> None:
    baseline = _track()
    primary_candidate = _track(first_ms=2200)
    extra_candidate = _track(first_ms=3200)
    plan = plan_reloaded_tracks(
        baseline,
        primary_candidate,
        primary_track=deepcopy(baseline),
        extra_tracks=((0, extra_candidate),),
    )

    class Target:
        def __init__(self):
            self.replacements = []

        def replace_track(self, index, track):
            self.replacements.append((index, track))
            return index in {0, 1}

    target = Target()
    applied = apply_reloaded_tracks(target, plan)

    assert [index for index, _track_value in target.replacements] == [0, 1]
    assert applied == tuple(track for _index, track in target.replacements)


def _rewrap_tracks():
    """SUG 换行重排：行数不变、每行行首仍是 hh、正文重新分配。"""

    def line(text, start):
        return TimingLine(
            chars=[TimingChar(ch, start + i * 600) for i, ch in enumerate(text)],
            end_ms=start + len(text) * 600,
        )

    baseline = TimingTrack(
        lines=[
            line("hhあかさたな", 0),
            line("hhはまやらわ", 8000),
            line("hhをん", 16000),
        ]
    )
    rewrapped = TimingTrack(
        lines=[
            line("hhあかさ", 0),
            line("hhたなはまや", 8000),
            line("hhらわをん", 16000),
        ]
    )
    return baseline, rewrapped


def _anchored_guide(anchor: tuple[str, ...]) -> GuideSymbol:
    return GuideSymbol(
        path_commands=(("M", 0.0, 0.0), ("L", 1.0, 1.0)),
        replacement_prefix=("h", "h"),
        replacement_anchor=anchor,
        count=2,
    )


def test_anchored_prefix_guide_survives_unchanged_reload() -> None:
    baseline, _rewrapped = _rewrap_tracks()
    current = deepcopy(baseline)
    current.lines[0].guide_symbol = _anchored_guide(("あ", "か", "さ", "た"))

    result = merge_reloaded_track(current, baseline, deepcopy(baseline))

    assert result.conflicts == ()
    assert result.track.lines[0].guide_symbol is not None


def test_anchored_prefix_guide_kept_when_visible_text_extends() -> None:
    """行尾续字（换行把下一行开头并进来）不丢导唱符。"""
    baseline, _rewrapped = _rewrap_tracks()
    current = deepcopy(baseline)
    current.lines[0].guide_symbol = _anchored_guide(("あ", "か", "さ", "た"))
    extended = deepcopy(baseline)
    extended.lines[0].chars.append(TimingChar("に", 9000))
    extended.lines[0].end_ms = 10000

    result = merge_reloaded_track(current, baseline, extended)

    assert result.conflicts == ()
    assert result.track.lines[0].guide_symbol is not None


def test_anchored_prefix_guide_kept_through_positional_rewrap_pairing() -> None:
    """换行重排但按位置配对的行锚点仍吻合：正常跟随迁移。"""
    baseline, rewrapped = _rewrap_tracks()
    current = deepcopy(baseline)
    current.lines[0].guide_symbol = _anchored_guide(("あ", "か", "さ", "た"))

    result = merge_reloaded_track(current, baseline, deepcopy(rewrapped))

    assert result.conflicts == ()
    assert result.track.lines[0].guide_symbol is not None


def test_anchored_prefix_guide_conflicts_when_head_matches_other_content() -> None:
    """行首仍是 hh 但正文已是另一句：必须报冲突丢弃，不能静默替换错位。"""
    baseline, _rewrapped = _rewrap_tracks()
    current = deepcopy(baseline)
    current.lines[0].guide_symbol = _anchored_guide(("た", "な", "は", "ま"))
    candidate = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("h", 0),
                    TimingChar("h", 600),
                    TimingChar("あ", 1200),
                    TimingChar("か", 1800),
                ],
                end_ms=3000,
            )
        ]
    )

    result = merge_reloaded_track(current, baseline, candidate)

    assert result.track.lines[0].guide_symbol is None
    assert result.conflicts
    assert "前缀导唱符与新歌词不匹配" in result.conflicts[0]


def test_legacy_prefix_guide_without_anchor_keeps_positional_carry() -> None:
    """旧数据（无锚点）保持宽松迁移：行首匹配即跟随。"""
    baseline, rewrapped = _rewrap_tracks()
    current = deepcopy(baseline)
    current.lines[0].guide_symbol = _guide(prefix=("h", "h"))

    result = merge_reloaded_track(current, baseline, deepcopy(rewrapped))

    assert result.conflicts == ()
    assert result.track.lines[0].guide_symbol is not None
