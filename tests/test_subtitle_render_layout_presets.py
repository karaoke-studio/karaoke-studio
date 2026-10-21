"""应用级「布局预设库」：条目模型、一次性迁移与新建工程物化。"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from krok_helper.subtitle_render.domain.models import (
    LyricsLayout,
    Style,
    default_page_layouts,
    ensure_page_layout_defaults,
    style_from_dict,
    style_to_dict,
)
from krok_helper.subtitle_render.settings.layout_presets import (
    DANGLING_TITLE_LAYOUT_INDEX,
    LayoutPreset,
    default_layout_presets,
    layout_presets_from_dict,
    layout_presets_to_dict,
    merge_layout_preset_library,
    migrated_layout_presets,
    rescale_preset_layout,
    style_for_new_project,
)
from krok_helper.subtitle_render.settings.preferences import (
    AppPreferenceSaveInput,
    load_app_preferences,
    prepare_app_preferences,
)

# 非权威字体目录：跳过字体规范化，让 ``changed`` 只反映布局预设库的迁移。
_PASSTHROUGH_CATALOG = SimpleNamespace(authoritative=False)


def _custom_layout(name: str, rows: int = 2) -> LyricsLayout:
    return LyricsLayout(
        name=name,
        layout_id=f"layout-{name}",
        line_alignments=["left"] * rows,
        line_gap_px=33,
    )


def test_default_layout_presets_match_factory_catalog() -> None:
    presets = default_layout_presets()

    assert [preset.layout.layout_id for preset in presets] == [
        "title-default",
        "builtin-1",
        "builtin-2",
        "builtin-3",
        "builtin-4",
        "builtin-5",
        "builtin-6",
        "builtin-7",
        "builtin-8",
    ]
    assert all(preset.default_load for preset in presets)
    assert presets[0].is_builtin
    assert not LayoutPreset(layout=_custom_layout("自定义")).is_builtin


def test_layout_preset_roundtrip_keeps_default_load_flag() -> None:
    presets = [
        LayoutPreset(layout=_custom_layout("甲"), default_load=True),
        LayoutPreset(layout=_custom_layout("乙"), default_load=False),
        LayoutPreset(
            layout=_custom_layout("丙"), default_load=True, default_for_rows=5
        ),
    ]

    restored = layout_presets_from_dict(layout_presets_to_dict(presets))

    assert [(p.layout.name, p.default_load, p.default_for_rows) for p in restored] == [
        ("甲", True, None),
        ("乙", False, None),
        ("丙", True, 5),
    ]
    assert [p.layout.layout_id for p in restored] == ["layout-甲", "layout-乙", "layout-丙"]


def test_layout_presets_from_dict_tolerates_bare_rows_and_drops_duplicates() -> None:
    payload = [
        # 裸布局 dict：视为默认加载。
        {"name": "裸行", "layout_id": "layout-bare", "line_alignments": ["left"]},
        # 空名丢弃。
        {"name": "  ", "layout_id": "layout-empty"},
        # 同名丢弃（库内唯一性按名）。
        {"layout": {"name": "裸行", "layout_id": "layout-dup"}, "default_load": False},
        # 重复的自定义 id 改发新 id；出厂 id 保持稳定。
        {"layout": {"name": "重复id", "layout_id": "layout-bare"}},
        {"layout": {"name": "出厂覆盖", "layout_id": "builtin-3"}},
    ]

    restored = layout_presets_from_dict(payload)

    assert [p.layout.name for p in restored] == ["裸行", "重复id", "出厂覆盖"]
    assert restored[0].default_load is True
    first_ids = [p.layout.layout_id for p in restored]
    assert first_ids[0] != first_ids[1]
    assert first_ids[2] == "builtin-3"


def test_migrated_layout_presets_marks_everything_default_load() -> None:
    legacy = ensure_page_layout_defaults(
        replace(Style(), layouts=[*_for_legacy_style(), _custom_layout("私货")])
    )

    presets = migrated_layout_presets(legacy)

    assert [p.layout.name for p in presets] == [
        *[l.name for l in legacy.layouts],
        "2 行布局",  # 旧库缺失的出厂预设按定义补齐（回归 #12/R5）
    ]
    assert all(p.default_load for p in presets)
    # 旧库的「按行数默认」映射折算进对应条目。
    rows_by_name = {
        p.layout.name: p.default_for_rows
        for p in presets
        if p.default_for_rows is not None
    }
    assert rows_by_name.get("3 行布局") == 3
    assert rows_by_name.get("私货") is None


def _for_legacy_style() -> list[LyricsLayout]:
    return default_page_layouts()


def test_style_for_new_project_filters_by_default_load_and_hides_builtins() -> None:
    app_style = ensure_page_layout_defaults(Style())
    presets = [
        *default_layout_presets(),
        LayoutPreset(layout=_custom_layout("常驻"), default_load=True),
        LayoutPreset(layout=_custom_layout("不带走"), default_load=False),
    ]
    # 取消两个出厂预设的默认加载。
    presets = [
        replace(preset, default_load=False)
        if preset.layout.layout_id in {"builtin-4", "builtin-6"}
        else preset
        for preset in presets
    ]

    style = style_for_new_project(app_style, presets)

    ids = [layout.layout_id for layout in style.layouts]
    assert "layout-常驻" in ids
    assert "layout-不带走" not in ids
    assert "builtin-4" not in ids
    assert "builtin-6" not in ids
    # 不在目录中的出厂预设必须进隐藏清单，否则 ensure 会补种复活。
    assert "builtin-4" in style.hidden_builtin_layout_ids
    assert "builtin-6" in style.hidden_builtin_layout_ids
    # ensure 幂等：再跑一遍不复活。
    assert [
        layout.layout_id for layout in ensure_page_layout_defaults(style).layouts
    ] == ids
    # 未勾选的出厂预设被行数默认映射引用时，映射回退「默认布局」。
    assert style.default_layout_by_row_count[4] == "default"


def test_style_for_new_project_remaps_title_reference_by_name() -> None:
    # 旧库（app_default_style.layouts）首条是 タイトル左上，标题默认引用 1。
    app_style = ensure_page_layout_defaults(Style())
    assert app_style.title_overlays[0].layout_index == 1

    # 预设库删掉 タイトル左上、前置一条自定义：记忆名解析不到 → 出厂也
    # 缺席 → 走显式锚点字段（layout_index=None + 出厂上左锚点）。装载端
    # _migrate_title_references 会把它物化成稳定的追加布局——引用不悬空，
    # 不会被之后新增的布局意外接管（回归 #8）。
    presets = [
        LayoutPreset(layout=_custom_layout("前置")),
        *(
            preset
            for preset in default_layout_presets()
            if preset.layout.layout_id != "title-default"
        ),
    ]

    style = style_for_new_project(
        app_style, presets, title_layout_name="不存在的名字"
    )

    # 出厂标题也不在目录：回落稳定悬空引用（渲染回退出厂外观、目录零补种、
    # 也不会被之后新增的布局接管）。
    assert style.title_overlays[0].layout_index == DANGLING_TITLE_LAYOUT_INDEX
    assert style.layouts[0].name == "前置"
    assert "タイトル左上" not in {layout.name for layout in style.layouts}
    # 序列化往返不改变目录与标题引用（无锚点物化）。
    roundtrip = ensure_page_layout_defaults(
        style_from_dict(style_to_dict(style))
    )
    assert roundtrip.title_overlays[0].layout_index == DANGLING_TITLE_LAYOUT_INDEX
    assert [layout.name for layout in roundtrip.layouts] == [
        layout.name for layout in style.layouts
    ]
    # 追加布局不会让悬空引用命中新条目。
    grown = replace(
        roundtrip,
        layouts=[*roundtrip.layouts, _custom_layout("后来者")],
    )
    assert int(grown.title_overlays[0].layout_index or 0) == -1

    # タイトル左上 仍在库但排位后移：引用按名跟随新位置。
    presets_kept = [
        LayoutPreset(layout=_custom_layout("前置")),
        *default_layout_presets(),
    ]
    style_kept = style_for_new_project(
        app_style, presets_kept, title_layout_name="タイトル左上"
    )
    title_source_index = style_kept.title_overlays[0].layout_index
    assert title_source_index is not None
    assert (
        style_kept.layouts[title_source_index - 1].name
        == app_style.layouts[0].name
    )

    # 记忆名解析不到但出厂 タイトル左上 在目录里：回落它的实际位置。
    style_fallback = style_for_new_project(
        app_style, default_layout_presets(), title_layout_name="不存在的名字"
    )
    fallback_index = style_fallback.title_overlays[0].layout_index
    assert fallback_index is not None
    assert style_fallback.layouts[fallback_index - 1].name == "タイトル左上"


def test_load_app_preferences_migrates_legacy_library_once() -> None:
    legacy = ensure_page_layout_defaults(
        replace(Style(), layouts=[*_for_legacy_style(), _custom_layout("迁移件")])
    )
    data = {"style": style_to_dict(legacy)}

    loaded = load_app_preferences(
        data,
        chorus_begin_default="[{",
        chorus_end_default="]",
        font_catalog=_PASSTHROUGH_CATALOG,
    )

    assert [p.layout.name for p in loaded.layout_presets] == [
        *[layout.name for layout in legacy.layouts],
        "2 行布局",
    ]
    assert all(p.default_load for p in loaded.layout_presets)
    assert loaded.changed is True


def test_load_app_preferences_keeps_persisted_presets_without_migration() -> None:
    # 不带 style 键：changed 只反映布局预设逻辑（带 style 的标题投影本就会
    # 置 changed，与迁移无关）。
    data = {
        "layout_presets": layout_presets_to_dict(
            [LayoutPreset(layout=_custom_layout("存量"), default_load=False)]
        ),
        "layout_presets_migrated": True,
    }

    loaded = load_app_preferences(
        data,
        chorus_begin_default="[{",
        chorus_end_default="]",
        font_catalog=_PASSTHROUGH_CATALOG,
    )

    assert [(p.layout.name, p.default_load) for p in loaded.layout_presets] == [
        ("存量", False)
    ]
    assert loaded.changed is False


def test_load_app_preferences_missing_key_after_gate_means_factory() -> None:
    # 迁移闸门已置而库键缺失（重置等路径）：出厂库，不回放旧库迁移。
    loaded = load_app_preferences(
        {"layout_presets_migrated": True},
        chorus_begin_default="[{",
        chorus_end_default="]",
        font_catalog=_PASSTHROUGH_CATALOG,
    )

    assert [p.layout.layout_id for p in loaded.layout_presets] == [
        preset.layout.layout_id for preset in default_layout_presets()
    ]
    assert loaded.changed is True


def _save_input(presets) -> AppPreferenceSaveInput:
    return AppPreferenceSaveInput(
        app_default_style=Style(),
        project_style=Style(),
        layout_assignment=None,
        subtitle_loading_defaults={},
        style_presets={},
        screen={},
        auto_chorus_role="",
        auto_chorus_begin_chars="",
        auto_chorus_end_chars="",
        auto_chorus_overwrite=False,
        selected_scheme_key="global",
        preview_splitter_ratio=0.4,
        auto_save_enabled=True,
        auto_save_interval_minutes=5,
        project_backup_count=5,
        layout_presets=presets,
    )


def test_prepare_app_preferences_writes_library_and_gate() -> None:
    presets = [
        LayoutPreset(layout=_custom_layout("入库"), default_load=False),
    ]

    prepared = prepare_app_preferences({}, _save_input(presets))

    assert prepared.data["layout_presets_migrated"] is True
    restored = layout_presets_from_dict(prepared.data["layout_presets"])
    assert [(p.layout.name, p.default_load) for p in restored] == [("入库", False)]
    assert [(p.layout.name, p.default_load) for p in prepared.layout_presets] == [
        ("入库", False)
    ]

    # 落盘数据再读：正常解析，不再触发迁移。
    loaded = load_app_preferences(
        prepared.data,
        chorus_begin_default="[{",
        chorus_end_default="]",
        font_catalog=_PASSTHROUGH_CATALOG,
    )
    assert [(p.layout.name, p.default_load) for p in loaded.layout_presets] == [
        ("入库", False)
    ]
    assert loaded.changed is False


def test_prepare_app_preferences_without_presets_keeps_disk_value() -> None:
    existing = {
        "layout_presets": layout_presets_to_dict(
            [LayoutPreset(layout=_custom_layout("盘上"))]
        ),
        "layout_presets_migrated": True,
    }

    prepared = prepare_app_preferences(
        existing, _save_input(None)
    )

    assert prepared.data["layout_presets"] == existing["layout_presets"]
    assert prepared.data["layout_presets_migrated"] is True
    assert prepared.layout_presets is None


def test_from_dict_regenerates_duplicate_builtin_ids() -> None:
    """回归 #9：出厂布局改名入库产生的重复 builtin id 不再被容忍。"""

    payload = [
        {"layout": {"name": "3 行布局", "layout_id": "builtin-3"}},
        {"layout": {"name": "我的三行", "layout_id": "builtin-3"}},
    ]

    restored = layout_presets_from_dict(payload)

    ids = [preset.layout.layout_id for preset in restored]
    assert ids[0] == "builtin-3"
    assert ids[1] != "builtin-3" and ids[1].startswith("layout-")


def test_merge_layout_preset_library_submits_only_local_deltas() -> None:
    """回归 #2：多实例共享库，普通偏好保存不再整库覆盖别人的改动。"""

    baseline = [
        LayoutPreset(layout=_custom_layout("共享")),
    ]
    # 磁盘上别的实例新增了一条。
    disk = layout_presets_to_dict(
        [
            LayoutPreset(layout=_custom_layout("共享")),
            LayoutPreset(layout=_custom_layout("别人新增")),
        ]
    )
    # 本实例只改了「共享」的默认加载。
    current = [
        LayoutPreset(layout=_custom_layout("共享"), default_load=False),
    ]

    rows = merge_layout_preset_library(disk, current, baseline)

    names = {row["layout"]["name"] for row in rows}
    assert names == {"共享", "别人新增"}, "别人新增的条目必须保留"
    shared = next(row for row in rows if row["layout"]["name"] == "共享")
    assert shared["default_load"] is False

    # 本实例删除的条目要落盘；没动过的条目保留磁盘原行。
    removed = merge_layout_preset_library(disk, [], current)
    assert {row["layout"]["name"] for row in removed} == {"别人新增"}


def test_prepare_with_baseline_merges_instead_of_wholesale_overwrite() -> None:
    """回归 #2（prepare 侧）：给基线时走增量合并，别人的新增不被抹掉。"""

    disk = {
        "layout_presets": layout_presets_to_dict(
            [
                LayoutPreset(layout=_custom_layout("本实例已知")),
                LayoutPreset(layout=_custom_layout("别人新增")),
            ]
        ),
        "layout_presets_migrated": True,
    }
    values = _save_input([LayoutPreset(layout=_custom_layout("本实例已知"))])
    values = replace(
        values,
        layout_presets_baseline=[LayoutPreset(layout=_custom_layout("本实例已知"))],
    )

    prepared = prepare_app_preferences(disk, values)

    names = {row["layout"]["name"] for row in prepared.data["layout_presets"]}
    assert names == {"本实例已知", "别人新增"}


def test_style_for_new_project_ignores_stale_legacy_row_wiring() -> None:
    """回归 #11：被用户清除的行数接线不能从冻结旧映射里复活。"""

    app_style = ensure_page_layout_defaults(Style())
    # 冻结旧映射把 3 行指向某个自定义 id（迁移时已折算成 default_for_rows，
    # 用户随后在库里清除了接线）。
    stale = replace(
        app_style,
        default_layout_by_row_count={
            **app_style.default_layout_by_row_count,
            3: "layout-stale",
        },
    )
    presets = [
        preset
        for preset in default_layout_presets()
        if preset.layout.layout_id != "builtin-3"
    ]
    # builtin-3 仍在目录（默认加载），但无行数接线。
    assert all(preset.default_for_rows is None for preset in presets)

    style = style_for_new_project(stale, presets)

    assert style.default_layout_by_row_count[3] != "layout-stale"
    assert style.default_layout_by_row_count[3] == "default"

    # index-0 保存的 "default" 目标仍然生效（默认布局须有同行数容量）。
    three_line_default = replace(
        app_style,
        line_alignments=["left", "center", "right"],
        default_layout_by_row_count={
            **app_style.default_layout_by_row_count,
            3: "default",
        },
    )
    kept = style_for_new_project(three_line_default, default_layout_presets())
    assert kept.default_layout_by_row_count[3] == "default"


def test_rescale_preset_layout_scales_pixels_between_references() -> None:
    """回归 #5 的核心换算：1080 基准出厂值 → 1440 库 → 1080 工程还原。"""

    # 取能被 4/3 整除的值，往返无截断损失（缩放本身按 int 截断）。
    layout = LyricsLayout(
        name="换算",
        layout_id="layout-scale",
        line_gap_px=60,
        line_y_margin_px=81,
        horizontal_margin_px=54,
    )

    to_1440 = rescale_preset_layout(layout, 1080, 1440)
    assert to_1440.line_gap_px == 80  # 60 * 1440 / 1080
    assert to_1440.line_y_margin_px == 108  # 81 * 4/3
    back_to_1080 = rescale_preset_layout(to_1440, 1440, 1080)
    assert back_to_1080.line_gap_px == 60
    assert back_to_1080.horizontal_margin_px == 54


def test_merge_renames_same_name_addition_from_other_instance() -> None:
    """回归 R1：多实例新增同名预设，本实例的条目让名（不再被读回时静默隐藏）。"""

    baseline = [
        LayoutPreset(layout=_custom_layout("底垫")),
    ]
    # 磁盘：别的实例已新增同名「我的布局」（id 不同）。
    disk = layout_presets_to_dict(
        [
            LayoutPreset(layout=_custom_layout("底垫")),
            LayoutPreset(layout=_custom_layout("我的布局", rows=1)),
        ]
    )
    # 本实例也新增了「我的布局」（不同 id、不同参数）。
    current = [
        LayoutPreset(layout=_custom_layout("底垫")),
        LayoutPreset(layout=replace(_custom_layout("我的布局"), layout_id="layout-mine", line_gap_px=222)),
    ]

    rows = merge_layout_preset_library(disk, current, baseline)

    restored = layout_presets_from_dict(rows)
    names = [preset.layout.name for preset in restored]
    assert sorted(names) == sorted(["底垫", "我的布局", "我的布局 2"])
    mine = next(
        preset
        for preset in restored
        if preset.layout.layout_id == "layout-mine"
    )
    # 本实例的参数保留，只是让名。
    assert mine.layout.name == "我的布局 2"
    assert mine.layout.line_gap_px == 222
    assert any(
        preset.layout.name == "我的布局" and preset.layout.line_gap_px == 33
        for preset in restored
    )


def test_merge_resolves_conflicting_row_wiring() -> None:
    """回归 R2：同行数双接线——真改了接线的赢；只改名/改别的字段不抢。"""

    # 场景 1：本实例本次把 Y 从「无接线」改为 3 行（真改接线）→ Y 赢，
    # 即使磁盘上别的实例也接了 X。
    baseline1 = [
        LayoutPreset(layout=_custom_layout("Y", rows=3)),
        LayoutPreset(layout=_custom_layout("X", rows=3)),
    ]
    disk1 = layout_presets_to_dict(
        [
            LayoutPreset(layout=_custom_layout("Y", rows=3), default_for_rows=3),
            LayoutPreset(layout=_custom_layout("X", rows=3), default_for_rows=3),
        ]
    )
    current1 = [
        LayoutPreset(layout=_custom_layout("Y", rows=3), default_for_rows=3),
        LayoutPreset(layout=_custom_layout("X", rows=3)),
    ]
    by_id1 = {
        row["layout"]["layout_id"]: row["default_for_rows"]
        for row in merge_layout_preset_library(disk1, current1, baseline1)
    }
    assert by_id1["layout-Y"] == 3
    assert by_id1["layout-X"] is None

    # 场景 2：本实例没动接线（存量 Y=3），磁盘是别的实例新接的 X → X 赢。
    baseline2 = [
        LayoutPreset(layout=_custom_layout("Y", rows=3), default_for_rows=3),
        LayoutPreset(layout=_custom_layout("X", rows=3)),
    ]
    disk2 = layout_presets_to_dict(
        [
            LayoutPreset(layout=_custom_layout("Y", rows=3), default_for_rows=3),
            LayoutPreset(layout=_custom_layout("X", rows=3), default_for_rows=3),
        ]
    )
    by_id2 = {
        row["layout"]["layout_id"]: row["default_for_rows"]
        for row in merge_layout_preset_library(disk2, baseline2, baseline2)
    }
    assert by_id2["layout-X"] == 3
    assert by_id2["layout-Y"] is None


def test_preset_reference_height_roundtrip_and_materialization() -> None:
    """回归 R3：条目自带保存基准，跨高度实例混合库正确物化。"""

    # 库里 1080 基准的条目（B 实例重置写入）与 1440 基准的条目（A 实例
    # 旧高度下保存）并存。
    presets = [
        LayoutPreset(
            layout=LyricsLayout(
                name="B 的",
                layout_id="layout-b",
                line_gap_px=60,
                line_alignments=["left"],
            ),
            default_load=True,
            reference_height=1080,
        ),
        LayoutPreset(
            layout=LyricsLayout(
                name="A 的",
                layout_id="layout-a",
                line_gap_px=80,
                line_alignments=["left", "right"],
            ),
            default_load=True,
            reference_height=1440,
        ),
    ]
    restored = layout_presets_from_dict(layout_presets_to_dict(presets))
    assert [p.reference_height for p in restored] == [1080, 1440]

    # A 实例（应用高度 1440）物化：B 的 60 → 80（1080→1440），A 的不变。
    app_style = replace(
        Style(), layout_reference_height=1440, layouts=[]
    )
    app_style = ensure_page_layout_defaults(app_style)
    style = style_for_new_project(app_style, restored)
    gap_by_name = {layout.name: layout.line_gap_px for layout in style.layouts}
    assert gap_by_name["B 的"] == 80
    assert gap_by_name["A 的"] == 80


def test_merge_releases_renamed_entry_name_for_factory_restore() -> None:
    """回归（本轮 1）：恢复出厂让名后，规范名要还给出厂条目。"""

    # 磁盘：自定义条目占着出厂名「3 行布局」。
    disk = layout_presets_to_dict(
        [
            LayoutPreset(
                layout=_custom_layout("3 行布局"),
            )
        ]
    )
    # 本实例（对话框恢复出厂）：自定义条目改名「3 行布局 2」，出厂条目
    # 拿回规范名「3 行布局」。
    baseline = layout_presets_from_dict(disk)
    custom_renamed = replace(
        baseline[0], layout=replace(baseline[0].layout, name="3 行布局 2")
    )
    factory = LayoutPreset(
        layout=LyricsLayout(
            name="3 行布局", layout_id="builtin-3", line_gap_px=60
        )
    )
    current = [custom_renamed, factory]

    rows = merge_layout_preset_library(disk, current, baseline)
    restored = layout_presets_from_dict(rows)
    names = {preset.layout.name: preset.layout.layout_id for preset in restored}

    assert names.get("3 行布局") == "builtin-3", names
    assert names.get("3 行布局 2") == "layout-3 行布局", names
    assert "3 行布局 3" not in names


def test_merge_rename_only_does_not_reclaim_row_wiring() -> None:
    """回归（本轮 2）：只改名不得抢回别的实例接走的行数默认。"""

    baseline = [
        LayoutPreset(layout=_custom_layout("Y", rows=3), default_for_rows=3),
        LayoutPreset(layout=_custom_layout("X", rows=3)),
    ]
    # 磁盘：A（别的实例）已把 3 行默认从 Y 改到 X。
    disk = layout_presets_to_dict(
        [
            LayoutPreset(layout=_custom_layout("Y", rows=3)),
            LayoutPreset(layout=_custom_layout("X", rows=3), default_for_rows=3),
        ]
    )
    # 本实例只把 Y 改名，接线字段没动（仍是 baseline 的 3）。
    current = [
        replace(
            baseline[0],
            layout=replace(baseline[0].layout, name="Y 改名"),
        ),
        baseline[1],
    ]

    rows = merge_layout_preset_library(disk, current, baseline)
    by_id = {
        row["layout"]["layout_id"]: row["default_for_rows"] for row in rows
    }

    assert by_id["layout-X"] == 3, "别的实例接走的默认不能被改名抢回"
    assert by_id["layout-Y"] is None
    # 改名本身要落盘。
    assert any(
        row["layout"]["name"] == "Y 改名" for row in rows
    )


def test_merge_clears_rows_asserted_as_global_default_across_instances() -> None:
    """回归（本轮 3）：保存全局默认要跨实例清掉同档位接线。"""

    baseline = [LayoutPreset(layout=_custom_layout("本地件"))]
    # 磁盘：远端实例新增了一条两行接线（本实例完全不知道）。
    disk = layout_presets_to_dict(
        [
            LayoutPreset(layout=_custom_layout("本地件")),
            LayoutPreset(
                layout=_custom_layout("远端两行", rows=2),
                default_for_rows=2,
            ),
        ]
    )

    rows = merge_layout_preset_library(
        disk, baseline, baseline, cleared_rows=(2,)
    )
    by_name = {
        row["layout"]["name"]: row["default_for_rows"] for row in rows
    }

    assert by_name["远端两行"] is None, "远端新接的接线必须被显式清除"
    # 条目本身保留（只清接线）。
    assert set(by_name) == {"本地件", "远端两行"}


def test_merge_allocates_names_independent_of_entry_order() -> None:
    """回归：恢复出厂让名与合并顺序无关（出厂项排在前也不抢错名）。"""

    disk = layout_presets_to_dict(
        [LayoutPreset(layout=_custom_layout("3 行布局"))]
    )
    baseline = layout_presets_from_dict(disk)
    factory = LayoutPreset(
        layout=LyricsLayout(
            name="3 行布局", layout_id="builtin-3", line_gap_px=60
        )
    )
    renamed_custom = replace(
        baseline[0],
        layout=replace(baseline[0].layout, name="3 行布局 2"),
    )

    for order in ([factory, renamed_custom], [renamed_custom, factory]):
        rows = merge_layout_preset_library(disk, order, baseline)
        names = {
            row["layout"]["layout_id"]: row["layout"]["name"] for row in rows
        }
        assert names["builtin-3"] == "3 行布局", (order, names)
        assert names[str(baseline[0].layout.layout_id)] == "3 行布局 2", names


def test_merge_honours_explicit_row_assertion_even_without_value_change() -> None:
    """回归：显式保存与旧基线相同的接线也必须生效（不能被远端接线盖掉）。"""

    baseline = [
        LayoutPreset(layout=_custom_layout("X", rows=3), default_for_rows=3),
        LayoutPreset(layout=_custom_layout("Y", rows=3)),
    ]
    # 磁盘：别的实例把 3 行默认改成了 Y。
    disk = layout_presets_to_dict(
        [
            LayoutPreset(layout=_custom_layout("X", rows=3)),
            LayoutPreset(layout=_custom_layout("Y", rows=3), default_for_rows=3),
        ]
    )

    rows = merge_layout_preset_library(
        disk,
        baseline,  # 本实例与旧基线完全相同（没有任何差异可判）
        baseline,
        asserted_rows={3: "layout-X"},
    )
    wired = {
        row["layout"]["layout_id"]: row["default_for_rows"] for row in rows
    }
    assert wired["layout-X"] == 3
    assert wired["layout-Y"] is None


def test_merge_assertion_overrides_cleared_rows_for_same_row() -> None:
    """同一档位同时出现「清空」与「显式声明」时，声明优先。"""

    baseline = [LayoutPreset(layout=_custom_layout("X", rows=3))]
    disk = layout_presets_to_dict(
        [
            LayoutPreset(layout=_custom_layout("X", rows=3), default_for_rows=3),
        ]
    )
    rows = merge_layout_preset_library(
        disk,
        baseline,
        baseline,
        cleared_rows=(3,),
        asserted_rows={3: "layout-X"},
    )
    assert rows[0]["default_for_rows"] == 3


def test_remote_rename_keeps_disk_name_claim_for_unmodified_entry() -> None:
    """回归：远端改名后，本地未修改的条目必须继续占住磁盘名。

    否则本实例新增的同名预设会与它撞成两条同名行，读回时后者被名称去重
    丢弃（界面只剩远端条目）。
    """

    # 磁盘：A 已把 X 从「旧名」改为「新名」。
    disk = layout_presets_to_dict(
        [LayoutPreset(layout=_custom_layout("新名"))]
    )
    baseline = layout_presets_from_dict(disk)
    x_id = baseline[0].layout.layout_id
    # B 尚未刷新：本地 X 仍叫「旧名」，并新增了同名「新名」的 Y。
    stale_x = replace(baseline[0], layout=replace(baseline[0].layout, name="旧名"))
    new_y = LayoutPreset(
        layout=LyricsLayout(
            name="新名",
            layout_id="layout-y",
            line_alignments=["left"] * 3,
            line_gap_px=99,
        )
    )

    rows = merge_layout_preset_library(disk, [stale_x, new_y], [stale_x])
    restored = layout_presets_from_dict(rows)

    names = {
        str(preset.layout.layout_id): preset.layout.name for preset in restored
    }
    assert len(restored) == 2, f"两条都必须保留：{names}"
    assert names[x_id] == "新名"
    assert names["layout-y"] == "新名 2"


def test_content_only_change_keeps_remote_rename() -> None:
    """本地只改内容（没改名）时，不把远端刚改好的名字覆盖回去。"""

    disk = layout_presets_to_dict(
        [LayoutPreset(layout=_custom_layout("新名"))]
    )
    baseline = layout_presets_from_dict(disk)
    stale = replace(
        baseline[0], layout=replace(baseline[0].layout, name="旧名")
    )
    # 本实例只把默认加载关掉（名字仍是旧的「旧名」）。
    current = [replace(stale, default_load=False)]

    rows = merge_layout_preset_library(disk, current, [stale])

    assert rows[0]["layout"]["name"] == "新名"
    assert rows[0]["default_load"] is False
