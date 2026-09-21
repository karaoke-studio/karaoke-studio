"""Focused contracts for typed subtitle project load planning."""

from __future__ import annotations

from pathlib import Path

from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.domain.timing import TimingChar, TimingLine, TimingTrack
from krok_helper.subtitle_render.project.load import (
    ProjectLoadPlan,
    apply_track_project_data,
)


def test_project_load_plan_resolves_legacy_style_reference_height() -> None:
    plan = ProjectLoadPlan.from_data(
        {
            "style": {"font_size_px": 96, "title_overlay": None},
            "screen": {"width": 3840, "height": 2160, "fps": 120},
            "selected_scheme_key": "custom:瑞",
        }
    )

    assert plan.style.font_size_px == 96
    assert plan.style.font_reference_height == 2160
    # 旧工程 ``title_overlay: null`` 现在加载为默认一条（禁用）条目
    assert plan.style.title_overlays
    assert (plan.screen.width, plan.screen.height, plan.screen.fps) == (3840, 2160, 120)
    assert plan.selected_scheme_key == "custom:瑞"


def test_project_load_plan_preserves_explicit_reference_height() -> None:
    plan = ProjectLoadPlan.from_data(
        {
            "style": {
                "font_size_px": 72,
                "font_reference_height": 1080,
            },
            "screen": {"height": 2160},
        }
    )

    assert plan.style.font_reference_height == 1080
    assert plan.selected_scheme_key is None


def test_project_load_plan_parses_paths_and_track_payloads(tmp_path: Path) -> None:
    subtitle = tmp_path / "main.sug"
    video = tmp_path / "video.mp4"
    audio = tmp_path / "audio.flac"
    background = {"kind": "image", "path": str(tmp_path / "background.png")}
    plan = ProjectLoadPlan.from_data(
        {
            "subtitle_path": str(subtitle),
            "video_path": str(video),
            "audio_path": str(audio),
            "background": background,
            "output": {"codec": "h264"},
            "line_breaks_before": ["none", "page"],
            "line_layout_indices": [0, 1],
            "char_role_labels": [["主唱"]],
            "project_role_names": ["主唱"],
        }
    )

    assert plan.subtitle_path == subtitle
    assert plan.fallback_video_path == video
    assert plan.audio_path == audio
    assert plan.background is background
    assert plan.output == {"codec": "h264"}
    assert plan.line_breaks_before == ["none", "page"]
    assert plan.line_layout_indices == [0, 1]
    assert plan.char_role_labels == [["主唱"]]
    assert plan.project_role_names == ["主唱"]


def test_project_load_plan_builds_detached_deferred_assets(tmp_path: Path) -> None:
    background = {"kind": "image", "path": str(tmp_path / "background.png")}
    extras = [{"name": "和声", "path": str(tmp_path / "chorus.lrc")}]
    roles = ["主唱", "和声"]
    audio = tmp_path / "missing-but-deferred.flac"
    plan = ProjectLoadPlan.from_data(
        {
            "background": background,
            "audio_path": str(audio),
            "extra_subtitle_sources": extras,
            "project_role_names": roles,
        }
    )

    loads = plan.deferred_assets()
    background["path"] = "changed"
    extras[0]["name"] = "changed"
    roles.append("changed")

    assert [load.kind for load in loads] == [
        "background",
        "audio",
        "extra_subtitle_sources",
    ]
    assert loads[0].payload["path"] != "changed"
    deferred_extras, deferred_roles = loads[2].payload
    assert deferred_extras[0]["name"] == "和声"
    assert deferred_roles == ["主唱", "和声"]


def test_project_load_plan_uses_existing_legacy_video_for_deferred_load(
    tmp_path: Path,
) -> None:
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    plan = ProjectLoadPlan.from_data({"video_path": str(video)})

    assert [(load.kind, load.payload) for load in plan.deferred_assets()] == [
        ("video", video)
    ]


def test_apply_track_project_data_restores_all_line_projections() -> None:
    track = TimingTrack(
        lines=[
            TimingLine(chars=[TimingChar("甲", 1000)]),
            TimingLine(chars=[TimingChar("乙", 2000)]),
        ]
    )

    result = apply_track_project_data(
        track,
        Style(),
        {
            "line_breaks_before": ["page", "invalid"],
            "line_layout_indices": [1, 99],
            "char_role_labels": [["主唱"], ["和声"]],
            "line_display_overrides": [[500, None], [None, 2600]],
            "line_animation_overrides": [
                {
                    "entry_anim": "fade",
                    "entry_duration_ms": 120,
                    "exit_anim": "slide_out",
                    "exit_duration_ms": 240,
                    "karaoke_anim": "utopia",
                },
                None,
            ],
        },
    )

    assert result.char_role_labels_changed is True
    assert result.guide_symbol_mismatches == ()
    assert [line.break_before for line in track.lines] == ["none", "page"]
    assert [line.layout_index for line in track.lines] == [1, 0]
    assert [line.chars[0].role_label for line in track.lines] == ["主唱", "和声"]
    assert track.lines[0].display_start_override_ms == 500
    assert track.lines[1].display_end_override_ms == 2600
    assert track.lines[0].animation_override is not None
    assert track.lines[0].animation_override.entry_anim == "fade"
    assert track.lines[1].animation_override is None
    assert track.page_plan is not None
    assert track.loading_settings_mode == "custom"


def test_apply_track_project_data_applies_wipe_reverse_overrides() -> None:
    track = TimingTrack(
        lines=[
            TimingLine(chars=[TimingChar("甲", 1000)], end_ms=2000),
            TimingLine(chars=[TimingChar("乙", 3000)], end_ms=4000),
            TimingLine(chars=[TimingChar("丙", 5000)], end_ms=6000),
        ]
    )

    apply_track_project_data(
        track,
        Style(),
        {"line_wipe_reverse_overrides": [True, False, "invalid"]},
    )

    # 手动覆盖回放时同时改写渲染消费的有效标记；非布尔项忽略
    assert track.lines[0].wipe_reverse is True
    assert track.lines[0].wipe_reverse_override is True
    assert track.lines[1].wipe_reverse is False
    assert track.lines[1].wipe_reverse_override is False
    assert track.lines[2].wipe_reverse is False
    assert track.lines[2].wipe_reverse_override is None


def test_wipe_reverse_overrides_round_trip_through_project_data() -> None:
    from krok_helper.subtitle_render.project.session import _track_project_data

    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("甲", 1000)], end_ms=2000, wipe_reverse=True
            ),
            TimingLine(
                chars=[TimingChar("乙", 3000)], end_ms=4000, wipe_reverse=False
            ),
        ]
    )
    track.lines[0].wipe_reverse_override = True
    # 源逆序自动判定为反向、用户手动取消的行也保留覆盖值
    track.lines[1].wipe_reverse_override = False

    data = _track_project_data(track)
    assert data["line_wipe_reverse_overrides"] == [True, False]

    # 全部行为自动判定时不写项目字段
    auto = TimingTrack(
        lines=[
            TimingLine(chars=[TimingChar("甲", 1000)], end_ms=2000, wipe_reverse=True)
        ]
    )
    assert _track_project_data(auto)["line_wipe_reverse_overrides"] is None

    # 重新解析出的顺序行回放覆盖后恢复手动反向
    restored = TimingTrack(
        lines=[TimingLine(chars=[TimingChar("甲", 1000)], end_ms=2000)]
    )
    apply_track_project_data(restored, Style(), data)
    assert restored.lines[0].wipe_reverse is True
    assert restored.lines[0].wipe_reverse_override is True


def test_display_timing_round_trips_through_project_data() -> None:
    from krok_helper.subtitle_render.project.session import _track_project_data

    track = TimingTrack(
        lines=[TimingLine(chars=[TimingChar("甲", 1000)], end_ms=2000)]
    )
    # 全默认（跟随、无覆盖）不写键——旧工程字段面保持逐字节不变。
    assert "display_timing" not in _track_project_data(track)

    track.display_timing.follow_main = False
    track.display_timing.overrides.update(
        {
            "line_lead_in_ms": 900,
            "sync_entry": False,
            "allow_inter_page_line_overlap": True,
            "overlap_fallback_mode": "displace",
        }
    )
    data = _track_project_data(track)
    assert data["display_timing"] == {
        "follow_main": False,
        "overrides": {
            "line_lead_in_ms": 900,
            "sync_entry": False,
            "allow_inter_page_line_overlap": True,
            "overlap_fallback_mode": "displace",
        },
    }

    restored = TimingTrack(
        lines=[TimingLine(chars=[TimingChar("甲", 1000)], end_ms=2000)]
    )
    apply_track_project_data(restored, Style(), data)
    assert restored.display_timing.follow_main is False
    assert restored.display_timing.overrides == {
        "line_lead_in_ms": 900,
        "sync_entry": False,
        "allow_inter_page_line_overlap": True,
        "overlap_fallback_mode": "displace",
    }


def test_display_timing_defaults_for_missing_or_broken_payloads() -> None:
    """旧工程缺失 / 空键 / 损坏负载一律回落默认：副轴跟随主轴。"""

    def fresh() -> TimingTrack:
        track = TimingTrack(
            lines=[TimingLine(chars=[TimingChar("甲", 1000)], end_ms=2000)]
        )
        track.display_timing.follow_main = False
        track.display_timing.overrides["line_lead_in_ms"] = 900
        return track

    for payload in (
        None,  # 旧 .yurika：无 display_timing 键
        {},
        {"follow_main": None, "overrides": None},
        "garbage",
        42,
    ):
        track = fresh()
        apply_track_project_data(track, Style(), {"display_timing": payload})
        assert track.display_timing.follow_main is True
        assert track.display_timing.overrides == {}

    # 防御解析：未知字段 / 类型不合法 / 非法枚举丢弃，负数钳为 0，合法项保留。
    track = fresh()
    apply_track_project_data(
        track,
        Style(),
        {
            "display_timing": {
                "follow_main": False,
                "overrides": {
                    "line_tail_ms": 1500,
                    "bogus_field": 5,
                    "sync_entry": "yes",
                    "line_protect_ms": -3,
                    "section_ending_mode": "explode",
                    "auto_fill_section_time": False,
                    "ruby_main_progress_mode": "reading_units",
                    "allow_inter_page_line_overlap": "on",
                    "overlap_fallback_mode": "fly",
                },
            },
        },
    )
    assert track.display_timing.follow_main is False
    assert track.display_timing.overrides == {
        "line_tail_ms": 1500,
        "line_protect_ms": 0,
        "auto_fill_section_time": False,
        "ruby_main_progress_mode": "reading_units",
    }


def test_guide_symbol_table_round_trips_through_project_data() -> None:
    """同一符号应用到多行时，.yurika 只存一份轮廓 + 行数据引用 ID。"""
    from krok_helper.subtitle_render.domain.timing import GuideSymbol
    from krok_helper.subtitle_render.project.session import _track_project_data

    symbol = GuideSymbol(
        path_commands=tuple(
            ("C", float(i), 1.0, float(i) + 1.0, 2.0, float(i) + 2.0, 3.0)
            for i in range(200)
        ),
        duration_ms=400,
        count=1,
    )
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("歌", 1000)],
                guide_symbol=symbol,
            )
            for _row in range(20)
        ]
    )

    data = _track_project_data(track)
    rows = data["line_guide_symbols"]
    table = data["guide_symbol_table"]

    assert len(table) == 1
    glyph_id = next(iter(table))
    assert rows == [glyph_id] * 20
    assert len(table[glyph_id]["path_commands"]) == 200

    restored = TimingTrack(
        lines=[TimingLine(chars=[TimingChar("歌", 1000)]) for _row in range(20)]
    )
    result = apply_track_project_data(restored, Style(), data)

    assert result.guide_symbol_mismatches == ()
    assert all(line.guide_symbol == symbol for line in restored.lines)


def test_single_use_guide_symbol_stays_inline_in_project_data() -> None:
    """只被引用一次的符号保持内嵌，兼容旧版本读取。"""
    from krok_helper.subtitle_render.domain.timing import GuideSymbol
    from krok_helper.subtitle_render.project.session import _track_project_data
    from krok_helper.subtitle_render.serialization.timing import guide_symbol_to_dict

    symbol = GuideSymbol(
        path_commands=(("M", 0.0, 0.0), ("L", 5.0, -5.0), ("Z",)),
        duration_ms=400,
    )
    track = TimingTrack(lines=[TimingLine(chars=[TimingChar("歌", 1000)], guide_symbol=symbol)])

    data = _track_project_data(track)

    assert data["line_guide_symbols"] == [guide_symbol_to_dict(symbol)]
    assert "guide_symbol_table" not in data


def test_session_to_project_data_accepts_guide_symbol_table() -> None:
    """符号表键必须能穿过 ``project_payload``（恢复自动保存的崩溃路径）。

    回归：``_track_project_data`` 产出 ``guide_symbol_table`` 后经 ``**track_data``
    展开，而 ``project_payload`` 参数表没有该键时保存链路直接 TypeError。
    """
    from krok_helper.subtitle_render.domain.timing import GuideSymbol
    from krok_helper.subtitle_render.project.session import SubtitleProjectDocument

    symbol = GuideSymbol(
        path_commands=(("M", 0.0, 0.0), ("L", 5.0, -5.0), ("Z",)),
        duration_ms=400,
    )
    session = SubtitleProjectDocument()
    session.timing_track = TimingTrack(
        lines=[
            TimingLine(chars=[TimingChar("歌", 1000)], guide_symbol=symbol)
            for _row in range(3)
        ]
    )

    payload = session.to_project_data(
        screen={"width": 1920, "height": 1080, "fps": 60},
        selected_scheme_key="",
        output={"encoder_mode": "cpu"},
    )

    assert payload["line_guide_symbols"] == ["g0", "g0", "g0"]
    from krok_helper.subtitle_render.serialization.timing import guide_symbol_to_dict

    assert payload["guide_symbol_table"]["g0"] == guide_symbol_to_dict(symbol)


def test_anchored_guide_rows_report_mismatch_after_source_rewrap() -> None:
    """保存后源被换行重排：行号错位 + 锚点对不上时报 mismatch，不静默回放。"""
    from krok_helper.subtitle_render.domain.timing import GuideSymbol
    from krok_helper.subtitle_render.serialization.timing import guide_symbol_to_dict

    symbol = GuideSymbol(
        path_commands=(("M", 0.0, 0.0), ("L", 5.0, -5.0), ("Z",)),
        replacement_prefix=("h", "h"),
        replacement_anchor=("歌", "词"),
        count=2,
    )
    # 新源的第一行行首仍是 hh，但正文换成了别句。
    restored = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("h", 0),
                    TimingChar("h", 500),
                    TimingChar("别", 1000),
                    TimingChar("句", 1500),
                ],
                end_ms=2000,
            )
        ]
    )

    result = apply_track_project_data(
        restored,
        Style(),
        {"line_guide_symbols": [guide_symbol_to_dict(symbol)]},
    )

    assert result.guide_symbol_mismatches == (0,)
    assert restored.lines[0].guide_symbol is None


def _emoji_label_avatar(name: str = "N3 Emoji 【主唱】") -> GuideSymbol:
    from krok_helper.subtitle_render.domain.timing import GuideSymbol

    return GuideSymbol(
        name=name,
        kind="bitmap",
        bitmap_before_path="avatar.png",
        bitmap_no_decor=True,
    )


def _user_vector_symbol() -> GuideSymbol:
    from krok_helper.subtitle_render.domain.timing import GuideSymbol

    return GuideSymbol(
        name="风车",
        path_commands=(("M", 0.0, 0.0), ("L", 5.0, -5.0), ("Z",)),
    )


def _line_with_synthetic_label() -> TimingLine:
    """模拟 dcb1cc0 起 .sug/.lrc 加载产出的行：行首合成【主唱】标签字符。"""

    return TimingLine(
        chars=[
            TimingChar("【主唱】", 1000, role_label="主唱"),
            TimingChar("甲", 1000, role_label="主唱"),
            TimingChar("乙", 1200, role_label="主唱"),
            TimingChar("丙", 1400, role_label="副唱"),
        ],
        inline_guide_symbols={0: _emoji_label_avatar()},
        end_ms=1600,
    )


def test_pre_emoji_project_replay_keeps_label_avatars_and_remaps_indices() -> None:
    """dcb1cc0 之前保存的工程：行内符号/逐字角色按「无合成标签字符」坐标回放。

    回归：整体替换行内符号会把源解析带入的透明头像抹掉，【主唱】标签字符
    随即以正文文字露出；逐字角色 zip 也会整体右移一位。
    """
    from krok_helper.subtitle_render.serialization.timing import guide_symbol_to_dict

    track = TimingTrack(lines=[_line_with_synthetic_label()])

    apply_track_project_data(
        track,
        Style(),
        {
            # 保存时行内只有用户加的风车（落在真实首字「甲」上，旧坐标 0）
            "line_inline_guide_symbols": [
                {0: guide_symbol_to_dict(_user_vector_symbol())}
            ],
            # 保存时逐字角色 3 条 = 真实字符数（无合成标签字符）
            "char_role_labels": [["主唱", "主唱", "副唱"]],
        },
    )

    line = track.lines[0]
    # 标签头像保留（标签字符不会以文字露出），用户符号重映射到「甲」而非标签字符
    assert set(line.inline_guide_symbols) == {0, 1}
    assert line.inline_guide_symbols[0].name.startswith("N3 Emoji ")
    assert line.inline_guide_symbols[1].name == "风车"
    # 逐字角色按旧坐标对齐：丙拿到「副唱」，乙不再被顶位
    assert [ch.role_label for ch in line.chars] == ["主唱", "主唱", "主唱", "副唱"]


def test_pre_emoji_project_empty_symbol_rows_keep_label_avatars() -> None:
    """旧行数据为空（None/空字典）时同样不能抹掉源解析的标签头像。"""

    track = TimingTrack(
        lines=[_line_with_synthetic_label(), _line_with_synthetic_label()]
    )

    apply_track_project_data(
        track,
        Style(),
        {"line_inline_guide_symbols": [None, {}]},
    )

    for line in track.lines:
        assert set(line.inline_guide_symbols) == {0}
        assert line.inline_guide_symbols[0].name.startswith("N3 Emoji ")


def test_pre_emoji_project_replay_maps_indices_past_label_but_not_visible_avatars() -> None:
    """替换词头像挂在真实字符上不移动坐标：旧坐标只跳过标签位。

    区分两种 ``N3 Emoji`` 符号：标签位（合成【角色名】字符，插入后坐标右移）
    与替换词位（真实字符原位替换，坐标不变）。旧行数据按旧坐标回放时只有
    前者需要跳过。
    """
    from krok_helper.subtitle_render.serialization.timing import guide_symbol_to_dict

    line = TimingLine(
        chars=[
            TimingChar("【主唱】", 1000, role_label="主唱"),
            TimingChar("♪", 1000, role_label="主唱"),
            TimingChar("愛", 1300, role_label="主唱"),
        ],
        inline_guide_symbols={
            0: _emoji_label_avatar(),
            1: _emoji_label_avatar("N3 Emoji ♪"),
        },
        end_ms=1600,
    )
    track = TimingTrack(lines=[line])

    # 旧坐标 0 = 真实首字 ♪（保存时没有标签字符也没有替换词头像）
    apply_track_project_data(
        track,
        Style(),
        {"line_inline_guide_symbols": [{0: guide_symbol_to_dict(_user_vector_symbol())}]},
    )

    restored = track.lines[0]
    # 标签头像保留；用户符号落在 ♪（旧坐标 0 → 新坐标 1，仅跳过标签位）
    assert set(restored.inline_guide_symbols) == {0, 1}
    assert restored.inline_guide_symbols[0].name == "N3 Emoji 【主唱】"
    assert restored.inline_guide_symbols[1].name == "风车"


def test_zip_shift_drifted_roles_self_heal_on_replay() -> None:
    """中间版本固化的「zip 漂移」角色签名命中时不回放，保留源解析角色。

    回归：dcb1cc0（标签字符插入）与旧工程对齐修复之间保存过的工程，加载时
    旧 n_real 条角色 zip 到新字符序列上产生错位并再次存盘；重开时错位被原样
    回放，sv1/sv2 交替行的演唱者肉眼可见地漂移。
    """
    track = TimingTrack(lines=[_line_with_synthetic_label()])
    # fresh 角色 [主唱, 主唱, 主唱, 副唱]，real（剔除标签位）[主唱, 主唱, 副唱]；
    # 漂移产物 = real 逐位 + fresh 尾部
    drifted = ["主唱", "主唱", "副唱", "副唱"]

    result = apply_track_project_data(
        track, Style(), {"char_role_labels": [drifted]}
    )

    # 漂移行自愈：乙不再被错标为「副唱」，保留源解析角色
    assert [ch.role_label for ch in track.lines[0].chars] == [
        "主唱",
        "主唱",
        "主唱",
        "副唱",
    ]
    assert result.char_role_labels_changed is False


def test_user_edited_roles_not_mistaken_for_zip_shift_drift() -> None:
    """与签名不符的用户编辑照常回放（只有精确命中漂移形状才自愈）。"""
    track = TimingTrack(lines=[_line_with_synthetic_label()])
    edited = ["主唱", "副唱", "主唱", "副唱"]

    apply_track_project_data(track, Style(), {"char_role_labels": [edited]})

    assert [ch.role_label for ch in track.lines[0].chars] == edited


def test_zip_shift_drift_with_overflow_tail_self_heals() -> None:
    """旧序列行尾空白被后续版本丢弃：多出的尾部条目按末位新鲜角色对齐签名。"""
    track = TimingTrack(lines=[_line_with_synthetic_label()])
    # fresh [主唱,主唱,主唱,副唱]，real [主唱,主唱,副唱]；
    # 漂移 + 一条超出当前字符数的尾巴（值同末位角色）
    drifted = ["主唱", "主唱", "副唱", "副唱", "副唱"]

    result = apply_track_project_data(
        track, Style(), {"char_role_labels": [drifted]}
    )

    assert [ch.role_label for ch in track.lines[0].chars] == [
        "主唱",
        "主唱",
        "主唱",
        "副唱",
    ]
    assert result.char_role_labels_changed is False


def test_post_emoji_project_replay_still_replaces_wholesale() -> None:
    """dcb1cc0 起保存的工程快照含 N3 Emoji 符号：整体替换语义不变（删除受尊重）。"""
    from krok_helper.subtitle_render.serialization.timing import guide_symbol_to_dict

    track = TimingTrack(lines=[_line_with_synthetic_label()])

    apply_track_project_data(
        track,
        Style(),
        {
            # 新快照坐标与加载期一致：保留标签头像，抹掉过期的第 3 位符号
            "line_inline_guide_symbols": [
                {
                    0: guide_symbol_to_dict(_emoji_label_avatar()),
                    1: guide_symbol_to_dict(_user_vector_symbol()),
                }
            ],
            # 新快照逐字角色长度 = 完整字符数（含合成标签字符）
            "char_role_labels": [["主唱", "主唱", "主唱", "主唱"]],
        },
    )

    line = track.lines[0]
    assert set(line.inline_guide_symbols) == {0, 1}
    assert line.inline_guide_symbols[1].name == "风车"
    assert [ch.role_label for ch in line.chars] == ["主唱"] * 4
