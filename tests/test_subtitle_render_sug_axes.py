"""SUG 分色分轴（axis_groups）在字幕渲染模块的解析、重载与持久化。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import krok_helper  # noqa: F401 - ensures bundled SUG src is importable
from strange_uta_game.backend.domain import (
    AxisGroup,
    Character,
    Project,
    ProjectMetadata,
    Ruby,
    RubyPart,
    Sentence,
    Singer,
)
from strange_uta_game.backend.infrastructure.persistence.sug_io import SugProjectParser

from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.domain.timing import SubtitleLoadingSettings
from krok_helper.subtitle_render.engine.layout.page.plan import build_page_plan
from krok_helper.subtitle_render.project.load import ProjectLoadPlan
from krok_helper.subtitle_render.project.session import (
    ExtraSubtitleSource,
    SubtitleProjectDocument,
)
from krok_helper.subtitle_render.sources.sug import (
    load_sug_axis_tracks,
    sug_axis_specs_from_project,
    sug_axis_tracks_from_project,
    timing_track_from_sug_project,
)
from krok_helper.subtitle_render.sources.sug_axes import (
    AxisSlotState,
    plan_single_axis_reload,
    plan_split_axis_reload,
)


def _singer(singer_id: str, name: str, *, default: bool = False, enabled: bool = True):
    return Singer(
        id=singer_id,
        name=name,
        color="#123456",
        is_default=default,
        enabled=enabled,
        backend_number=1,
    )


def _timed_char(
    text: str,
    start_ms: int,
    singer_id: str,
    *,
    end_ms: int | None = None,
    ruby_parts: list[str] | None = None,
) -> Character:
    character = Character(
        char=text,
        ruby=Ruby(parts=[RubyPart(part) for part in ruby_parts]) if ruby_parts else None,
        check_count=1 if ruby_parts else 0,
        timestamps=[start_ms],
        sentence_end_ts=end_ms,
        is_sentence_end=end_ms is not None,
        is_line_end=end_ms is not None,
        singer_id=singer_id,
    )
    return character


def _split_project() -> Project:
    """两句主唱、一句和声、一句未入组歌手，穿插空行与混合行。

    行结构（主唱=A / 和声=B / 未入组=C）：

    1. ``A君``       —— 纯 A 行
    2. 空行
    3. ``B酱``       —— 纯 B 行
    4. ``AB``        —— 混合行（句歌手 A，第二个字符属于 B）
    5. 空行
    6. ``C桑``       —— 纯未入组行，任何轴都不该有
    7. 空行
    8. 空行          —— 连续空行：验证分页不会拆出空段
    9. ``君B``       —— 混合行（句歌手 B，第一个字符属于 A）
    """
    main = _singer("a", "主唱", default=True)
    chorus = _singer("b", "和声")
    unassigned = _singer("c", "未入组")
    return Project(
        metadata=ProjectMetadata(title="分轴曲", artist="作者"),
        singers=[main, chorus, unassigned],
        sentences=[
            Sentence(
                singer_id="a",
                characters=[
                    _timed_char("君", 1000, "a", end_ms=1400),
                    _timed_char("A2", 1200, "a"),
                ],
            ),
            Sentence(singer_id="a", characters=[]),
            Sentence(
                singer_id="b",
                characters=[
                    _timed_char("酱", 2000, "b", end_ms=2400),
                    _timed_char("B2", 2200, "b"),
                ],
            ),
            Sentence(
                singer_id="a",
                characters=[
                    _timed_char("A", 3000, "a", end_ms=3200),
                    _timed_char("B", 3100, "b"),
                ],
            ),
            Sentence(singer_id="b", characters=[]),
            Sentence(
                singer_id="c",
                characters=[
                    _timed_char("C", 4000, "c", end_ms=4400),
                    _timed_char("C2", 4200, "c"),
                ],
            ),
            Sentence(singer_id="a", characters=[]),
            Sentence(singer_id="a", characters=[]),
            Sentence(
                singer_id="b",
                characters=[
                    _timed_char("A", 5000, "a"),
                    _timed_char("B", 5200, "b", end_ms=5600),
                ],
            ),
        ],
        audio_duration_ms=6000,
        axis_groups=[
            AxisGroup(name="主轴", singer_ids=["a"], is_primary=True),
            AxisGroup(name="副轴", singer_ids=["b"]),
        ],
    )


def test_axis_specs_normalize_groups() -> None:
    project = _split_project()
    project.singers[2].enabled = False  # 未入组且停用
    project.axis_groups = [
        AxisGroup(name="", singer_ids=["a", "ghost", "a"]),
        AxisGroup(name="副", singer_ids=[]),
    ]

    specs = sug_axis_specs_from_project(project)

    # 空名防御回退、重复/未知 id 去除、无人标记主分组时首组升为主分组
    assert [(spec.name, sorted(spec.singer_ids), spec.is_primary) for spec in specs] == [
        ("轴1", ["a"], True),
        ("副", ["a", "b"], False),
    ]


def test_axis_specs_multiple_primary_flags_keep_first() -> None:
    project = _split_project()
    project.axis_groups = [
        AxisGroup(name="一", singer_ids=["b"], is_primary=True),
        AxisGroup(name="二", singer_ids=["a"], is_primary=True),
    ]
    specs = sug_axis_specs_from_project(project)
    assert [spec.is_primary for spec in specs] == [True, False]


def test_split_axes_primary_first_and_content_filtering() -> None:
    axes = sug_axis_tracks_from_project(_split_project())

    assert [axis.name for axis in axes] == ["主轴", "副轴"]
    assert axes[0].is_split and axes[0].is_primary
    assert axes[0].singer_ids == frozenset({"a"})
    assert axes[1].singer_ids == frozenset({"b"})

    main_texts = [
        "".join(ch.text for ch in line.chars) if not line.is_blank else ""
        for line in axes[0].track.lines
    ]
    chorus_texts = [
        "".join(ch.text for ch in line.chars) if not line.is_blank else ""
        for line in axes[1].track.lines
    ]
    # 混合行保留整行、剔除组外字符；未入组行整行消失；空行无条件保留
    assert main_texts == ["君A2", "", "A", "", "", "", "A"]
    assert chorus_texts == ["", "酱B2", "B", "", "", "", "B"]


def test_split_axes_keep_exact_timing_of_kept_chars() -> None:
    full = timing_track_from_sug_project(_split_project())
    axes = sug_axis_tracks_from_project(_split_project())

    full_chars = [
        (ch.text, ch.start_ms, ch.pause_release_ms, ch.source_span_start_ms)
        for line in full.lines
        for ch in line.chars
    ]
    for axis in axes:
        for line in axis.track.lines:
            for ch in line.chars:
                timing = (ch.text, ch.start_ms, ch.pause_release_ms, ch.source_span_start_ms)
                assert timing in full_chars, (
                    f"轴 {axis.name} 的字符 {ch.text} 时间与未拆分轨道不一致: {timing}"
                )


def test_split_axes_drop_and_remap_ruby_targets() -> None:
    project = _split_project()
    # 混合行 3（句歌手 A，字符 A+B）：注音组横跨两轴歌手 → 两轴都丢弃。
    mixed = project.sentences[3]
    mixed.characters[0].ruby = Ruby(parts=[RubyPart("え")])
    mixed.characters[0].linked_to_next = True
    # 混合行 8（句歌手 B，字符 A+B）：注音只挂在 B 上 → 副轴保留，
    # 且目标下标必须重映射到剔除 A 之后的位置（0 而不是源句下标 1）。
    tail = project.sentences[8]
    tail.characters[1].ruby = Ruby(parts=[RubyPart("び")])
    tail.characters[1].check_count = 1

    axes = sug_axis_tracks_from_project(project)
    assert [ruby.kanji for ruby in axes[0].track.rubies] == []
    chorus_rubies = axes[1].track.rubies
    assert [ruby.kanji for ruby in chorus_rubies] == ["B"]
    chorus_line = next(
        line
        for line in axes[1].track.lines
        if line.chars and line.chars[0].text == "B" and line.chars[0].start_ms == 5200
    )
    assert [ruby.target_line_index for ruby in chorus_rubies] == [
        axes[1].track.lines.index(chorus_line)
    ]
    assert [(ruby.target_char_start, ruby.target_char_end) for ruby in chorus_rubies] == [
        (0, 1)
    ]


def test_split_axes_unknown_singer_normalizes_to_default() -> None:
    project = _split_project()
    # 句歌手缺失 → 归一到默认歌手（主唱），行按主轴保留；字符未知 id 也归一
    sentence = project.sentences[5]
    sentence.singer_id = ""
    sentence.characters[0].singer_id = ""
    sentence.characters[1].singer_id = "ghost-id"

    axes = sug_axis_tracks_from_project(project)
    main_texts = [
        "".join(ch.text for ch in line.chars) if not line.is_blank else ""
        for line in axes[0].track.lines
    ]
    assert main_texts == ["君A2", "", "A", "", "CC2", "", "", "A"]


def test_split_axes_emoji_guides_only_insert_present_singers(tmp_path: Path) -> None:
    axes = sug_axis_tracks_from_project(
        _split_project(),
        nicokara_tags={
            "custom": [
                "@Emoji=【主唱】,lead.png,,NoDecor",
                "@Emoji=【和声】,chorus.png,,NoDecor",
            ]
        },
        base_dir=tmp_path,
    )

    for axis in axes:
        for line in axis.track.lines:
            for symbol in line.inline_guide_symbols.values():
                expected = "lead.png" if axis.name == "主轴" else "chorus.png"
                assert symbol.bitmap_before_path == str(tmp_path / expected)
            inserted = [
                ch.text
                for index, ch in enumerate(line.chars)
                if index in line.inline_guide_symbols
            ]
            assert all(
                f"【{'主唱' if axis.name == '主轴' else '和声'}】" == text
                for text in inserted
            )


def test_consecutive_blank_lines_do_not_create_empty_pages() -> None:
    """过滤后连续空行：分页语义与收敛成单个空行完全一致，不产生空段/空页。"""
    settings = SubtitleLoadingSettings()
    style = Style()

    axes = sug_axis_tracks_from_project(_split_project())
    plan_blanks = build_page_plan(axes[0].track, settings, style)

    collapsed = deepcopy(axes[0].track)
    deduped: list = []
    for line in collapsed.lines:
        if line.is_blank and deduped and deduped[-1].is_blank:
            continue
        deduped.append(line)
    collapsed.lines = deduped
    plan_collapsed = build_page_plan(collapsed, settings, style)

    def _signature(plan) -> tuple:
        return tuple(
            tuple(tuple(page.line_count for page in section.pages) for section in plan.sections)
        )

    assert _signature(plan_blanks) == _signature(plan_collapsed)
    for section in plan_blanks.sections:
        assert section.pages, "过滤产生的连续空行不应制造空段落"
        for page in section.pages:
            assert page.line_count > 0, "过滤产生的连续空行不应制造空页"


def test_split_axes_role_labels_cover_all_rendered_singers() -> None:
    axes = sug_axis_tracks_from_project(_split_project())

    roles_by_axis = {axis.name: axis.track.role_options for axis in axes}
    assert roles_by_axis["主轴"] == ["主唱"]
    assert roles_by_axis["副轴"] == ["和声"]

    union: list[str] = []
    for axis in axes:
        for name in axis.track.role_options:
            if name not in union:
                union.append(name)
    # 未入组歌手（C）不进入任何轴；其余角色在轴并集里都能解析到
    assert union == ["主唱", "和声"]

    # 混合行的保留字符仍带自己的角色标签，逐字配色不受拆轴影响
    chorus_axis = axes[1].track
    mixed = next(line for line in chorus_axis.lines if line.chars and line.chars[0].text == "B")
    assert [ch.role_label for ch in mixed.chars] == ["和声"]


def test_no_axis_groups_yields_single_unfiltered_axis() -> None:
    project = _split_project()
    project.axis_groups = []

    axes = sug_axis_tracks_from_project(project)
    assert len(axes) == 1
    assert not axes[0].is_split
    assert axes[0].singer_ids == frozenset()
    assert axes[0].track == timing_track_from_sug_project(project)


def test_load_sug_axis_tracks_roundtrip_file(tmp_path: Path) -> None:
    sug_path = tmp_path / "split.sug"
    SugProjectParser.save(_split_project(), str(sug_path))

    axes = load_sug_axis_tracks(sug_path)
    assert [axis.name for axis in axes] == ["主轴", "副轴"]

    from krok_helper.subtitle_render.sources.loader import SubtitleSourceLoader

    filtered = SubtitleSourceLoader.load_sug(
        sug_path, software_compensation_ms=0, singer_filter=frozenset({"b"})
    )
    assert [
        "".join(ch.text for ch in line.chars) if not line.is_blank else ""
        for line in filtered.lines
    ] == ["", "酱B2", "B", "", "", "", "B"]


def test_split_axis_reload_merges_adds_and_removes() -> None:
    project = _split_project()
    old_axes = sug_axis_tracks_from_project(project)

    # 副轴带上一点本地编辑（角色覆盖）
    current_extra = deepcopy(old_axes[1].track)
    current_extra.lines[1].chars[0].role_label = "自定义角色"
    slot_track = deepcopy(old_axes[1].track)

    # 新解析：副轴改名（按歌手集合回退匹配）、新增「新轴」、源里微调了和声行时间
    new_project = _split_project()
    new_project.sentences[2].characters[0].timestamps = [2100]
    new_project.axis_groups = [
        AxisGroup(name="主轴", singer_ids=["a"], is_primary=True),
        AxisGroup(name="和声轴-改名", singer_ids=["b"]),
        AxisGroup(name="新轴", singer_ids=["c"]),
    ]
    new_axes = sug_axis_tracks_from_project(new_project)

    plan = plan_split_axis_reload(
        primary=AxisSlotState(
            track=deepcopy(old_axes[0].track),
            baseline=deepcopy(old_axes[0].track),
        ),
        primary_axis=new_axes[0],
        axis_extra_slots=[
            (
                0,
                AxisSlotState(
                    track=current_extra,
                    baseline=slot_track,
                    name="副轴",
                    singer_ids=frozenset({"b"}),
                ),
            )
        ],
        axes=new_axes,
    )

    assert plan.changed
    # 改名的副轴按歌手集合匹配上：新时间生效，本地角色覆盖迁移到了新解析
    assert len(plan.extra_updates) == 1
    merged_extra = plan.extra_updates[0].merge.track
    assert merged_extra.lines[1].chars[0].start_ms == 2100
    assert merged_extra.lines[1].chars[0].role_label == "自定义角色"
    # 新轴追加；没有消失的轴
    assert [addition.name for addition in plan.extra_additions] == ["新轴"]
    assert plan.removed_extra_indices == ()


def test_split_axis_reload_drops_removed_group() -> None:
    project = _split_project()
    old_axes = sug_axis_tracks_from_project(project)
    new_project = _split_project()
    new_project.axis_groups = [AxisGroup(name="主轴", singer_ids=["a"], is_primary=True)]
    new_axes = sug_axis_tracks_from_project(new_project)

    plan = plan_split_axis_reload(
        primary=None,
        primary_axis=new_axes[0],
        axis_extra_slots=[
            (
                0,
                AxisSlotState(
                    track=deepcopy(old_axes[1].track),
                    baseline=deepcopy(old_axes[1].track),
                    name="副轴",
                    singer_ids=frozenset({"b"}),
                ),
            )
        ],
        axes=new_axes,
    )
    assert plan.removed_extra_indices == (0,)
    assert plan.extra_additions == ()
    assert plan.structure_changed


def test_axis_reload_noop_when_source_unchanged() -> None:
    project = _split_project()
    axes = sug_axis_tracks_from_project(project)

    plan = plan_split_axis_reload(
        primary=AxisSlotState(track=deepcopy(axes[0].track), baseline=deepcopy(axes[0].track)),
        primary_axis=axes[0],
        axis_extra_slots=[
            (
                0,
                AxisSlotState(
                    track=deepcopy(axes[1].track),
                    baseline=deepcopy(axes[1].track),
                    name="副轴",
                    singer_ids=frozenset({"b"}),
                ),
            )
        ],
        axes=axes,
    )
    assert not plan.changed


def test_single_axis_reload_updates_primary_and_plain_extra() -> None:
    project = _split_project()
    project.axis_groups = []
    axes = sug_axis_tracks_from_project(project)
    assert len(axes) == 1 and not axes[0].is_split

    candidate = deepcopy(axes[0].track)
    candidate.lines[0].chars[0].start_ms += 500  # 源里改了一个字的时间
    current = deepcopy(axes[0].track)

    plan = plan_single_axis_reload(
        primary=AxisSlotState(track=current, baseline=deepcopy(axes[0].track)),
        candidate=candidate,
        extra_slots=[
            (
                0,
                AxisSlotState(
                    track=deepcopy(axes[0].track),
                    baseline=deepcopy(axes[0].track),
                ),
            )
        ],
    )
    assert plan.changed
    assert plan.timing_only
    assert plan.primary_merge is not None
    assert plan.primary_merge.track.lines[0].chars[0].start_ms == 1500
    assert len(plan.extra_updates) == 1


def test_project_document_persists_axis_filters() -> None:
    document = SubtitleProjectDocument(
        subtitle_axis_singer_ids=frozenset({"a"}),
        extra_sources=[
            ExtraSubtitleSource(
                name="副轴",
                path=Path("song.sug"),
                track=timing_track_from_sug_project(_split_project()),
                sug_axis_singer_ids=frozenset({"b"}),
            )
        ],
    )

    payload = document.to_project_data(
        screen={"width": 1920, "height": 1080, "fps": 60},
        selected_scheme_key="",
        output={},
    )

    assert payload["subtitle_sug_axis_singer_ids"] == ["a"]
    assert payload["extra_subtitle_sources"][0]["sug_axis_singer_ids"] == ["b"]

    plan = ProjectLoadPlan.from_data(payload)
    assert plan.subtitle_sug_axis_singer_ids == frozenset({"a"})


def test_project_load_plan_distinguishes_missing_and_empty_axis_filter() -> None:
    assert ProjectLoadPlan.from_data({}).subtitle_sug_axis_singer_ids is None
    assert (
        ProjectLoadPlan.from_data(
            {"subtitle_sug_axis_singer_ids": []}
        ).subtitle_sug_axis_singer_ids
        == frozenset()
    )
