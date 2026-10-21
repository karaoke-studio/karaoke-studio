"""应用级「布局预设库」。

2026-10 起布局库从挂在 ``Style.layouts`` 上的隐形积累库
（``_app_default_style.layouts`` + ``_merge_layouts_into_library``）迁为
**纯手动管理**的预设库：条目只能经「保存到布局库」与布局库管理对话框
进入，打开工程 / N3 导入 / 手动新建布局都不再自动入库。唯一的一次性
例外是升级迁移（:func:`migrated_layout_presets`）。

预设只服务两处：

1. 新建工程的初始布局目录物化（:func:`style_for_new_project`）；
2. 布局库管理对话框的浏览 / 整理 / 「载入到当前工程」。

已保存工程（``.yurika``）的布局目录自包含，加载时从不查本库。

每条预设自带 ``reference_height``（像素字段的保存基准，入库时记录）：
库是多实例共享的，不同实例的应用参考高度可能不同，靠单一全局高度解释
整库会在跨实例保存后互相破坏——按条目解释才能保证任意混合都正确。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Optional
from uuid import uuid4

from krok_helper.subtitle_render.domain.models import (
    LyricsLayout,
    Style,
    TITLE_LAYOUT_NAME,
    TitleOverlay,
    builtin_preset_layout_ids,
    default_page_layouts,
    ensure_page_layout_defaults,
    lyrics_layout_from_dict,
    lyrics_layout_to_dict,
    rescale_layout_sizes,
)
from krok_helper.subtitle_render.serialization.compat import merge_extensible_value

#: 旧库数据从未记录保存时的高度，统一按 1080 解释（与样式预设同口径）。
LEGACY_PRESET_REFERENCE_HEIGHT = 1080

#: 出厂标题布局缺席时的**稳定悬空引用**。
#:
#: 标题必须永远解析到出厂「タイトル左上」（Top / 左对齐），但工程布局目录
#: 只物化默认加载条目。``None`` 会被装载迁移物化成目录里的新条目（违反
#: 目录约定），``len+1`` 这类越界正数又会被之后新增的布局接管。负数任何
#: 时候都不会命中 ``1..len``，``title_layout_source`` 与标题下拉都按既有
#: 口径回退出厂定义——渲染外观与 v4 完全一致，目录保持干净、引用也不会
#: 漂移到别的布局上。
DANGLING_TITLE_LAYOUT_INDEX = -1


def _normalize_preset_reference_height(value: object) -> int:
    try:
        height = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return LEGACY_PRESET_REFERENCE_HEIGHT
    return height if height > 0 else LEGACY_PRESET_REFERENCE_HEIGHT


#: 不属于预设的保留 id：``default`` 是 ``Style`` 自身（全局默认布局）的身份，
#: 预设占用它会在 ``ensure_page_layout_defaults`` 里被改号、行数映射指回
#: 全局默认——永远不会指到该预设。
RESERVED_LAYOUT_PRESET_IDS = frozenset({"default"})


def layout_id_in_use(layout_id: object, used: set[str]) -> bool:
    """该 id 是否不能用作预设身份（空 / 保留字 / 已占用）。"""

    candidate = str(layout_id or "").strip()
    return (
        not candidate
        or candidate in RESERVED_LAYOUT_PRESET_IDS
        or candidate in used
    )


def make_layout_id(layout_id: object, used: set[str]) -> str:
    """返回可用作预设身份的 id；不可用时发新的 ``layout-xxxx``。"""

    candidate = str(layout_id or "").strip()
    if layout_id_in_use(candidate, used):
        return f"layout-{uuid4().hex}"
    return candidate


@dataclass(frozen=True)
class LayoutPreset:
    """一条布局预设：完整 ``LyricsLayout`` 拷贝 + 库管理标记。"""

    layout: LyricsLayout
    #: 勾选后新建工程的布局目录自动带入该条目（唯一消费点）。
    default_load: bool = True
    #: 「该行数的页面默认用它」的行数档位（1..8）；``None`` = 不接线。
    #: 行数映射随条目存储而不是挂在应用样式上——应用样式的布局目录是
    #: 升级迁移时冻结的旧库，``style_from_dict`` 末尾的 ensure 会按它把
    #: 指向预设 id 的映射当悬空清掉。
    default_for_rows: int | None = None
    #: ``layout`` 像素字段的保存基准高度。多实例库各自高度不同，必须按
    #: 条目解释；消费端（物化 / 载入工程）据此换算。
    reference_height: int = LEGACY_PRESET_REFERENCE_HEIGHT

    @property
    def is_builtin(self) -> bool:
        """是否出厂预设（タイトル左上 / builtin-N，按稳定 id 判定）。"""

        return str(self.layout.layout_id or "") in builtin_preset_layout_ids()


def factory_layout_definitions() -> list[LyricsLayout]:
    """出厂布局全集（タイトル左上 + 1..8 行布局，共 9 条，1080 基准）。

    两行出厂预设（``builtin-2``，左右对齐 / 行距 90，与工程目录的
    ``_DEFAULT_PAGE_LAYOUT_SPECS`` 同规格）独立成条——全局默认布局改成
    其他行数后，仍能从布局库直接载入出厂两行布局。
    """

    layouts: list[LyricsLayout] = []
    for layout in default_page_layouts():
        layouts.append(deepcopy(layout))
        if layout.layout_id == "builtin-1":
            layouts.append(
                LyricsLayout(
                    name="2 行布局",
                    layout_id="builtin-2",
                    line_gap_px=90,
                    line_alignments=["left", "right"],
                )
            )
    return layouts


def default_layout_presets() -> list[LayoutPreset]:
    """出厂预设库（9 条：タイトル左上 + 1..8 行布局，全部默认加载）。

    偏好「恢复默认设置」、迁移闸门兜底与对话框「恢复出厂预设」都以这份
    为基准。
    """

    return [
        LayoutPreset(
            layout=deepcopy(layout),
            default_load=True,
            reference_height=LEGACY_PRESET_REFERENCE_HEIGHT,
        )
        for layout in factory_layout_definitions()
    ]


def layout_presets_from_dict(payload: object) -> list[LayoutPreset]:
    """读回预设库；行结构 ``{"layout": {...}, "default_load": bool}``。

    兼容裸布局 dict（视为默认加载）。坏行（空名 / 非法布局）逐行丢弃；
    重名丢弃（合并端维护库内名称唯一，这里的去重只是手工改库的保险网）；
    重复或空 ``layout_id`` 改发新 uuid——**出厂 id 也一样**：同名改写入库
    产生的重复 ``builtin-*`` 会让物化目录/行数映射指错条目。
    """

    if not isinstance(payload, list):
        return []
    presets: list[LayoutPreset] = []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    for row in payload:
        if isinstance(row, dict) and isinstance(row.get("layout"), dict):
            layout_payload = row["layout"]
            default_load = bool(row.get("default_load", True))
            raw_rows = row.get("default_for_rows")
            try:
                default_for_rows = (
                    int(raw_rows) if raw_rows is not None else None
                )
            except (TypeError, ValueError):
                default_for_rows = None
            if default_for_rows is not None and not 1 <= default_for_rows <= 8:
                default_for_rows = None
            reference_height = _normalize_preset_reference_height(
                row.get("reference_height")
            )
        else:
            layout_payload = row
            default_load = True
            default_for_rows = None
            reference_height = LEGACY_PRESET_REFERENCE_HEIGHT
        layout = lyrics_layout_from_dict(layout_payload)
        name = str(layout.name or "").strip()
        if not name or name in seen_names:
            continue
        layout_id = make_layout_id(layout.layout_id, seen_ids)
        if layout_id != str(layout.layout_id or "").strip():
            layout = replace(layout, layout_id=layout_id)
        seen_ids.add(layout_id)
        seen_names.add(name)
        presets.append(
            LayoutPreset(
                layout=layout,
                default_load=default_load,
                default_for_rows=default_for_rows,
                reference_height=reference_height,
            )
        )
    return presets


def layout_presets_to_dict(presets: list[LayoutPreset]) -> list[dict]:
    # ``default_for_rows`` / ``reference_height`` 恒写（None 也写）：合并走
    # merge_extensible_value，省略的键会保留磁盘旧值——被清除的接线会
    # 借此「复活」。
    return [
        {
            "layout": lyrics_layout_to_dict(preset.layout),
            "default_load": bool(preset.default_load),
            "default_for_rows": (
                int(preset.default_for_rows)
                if preset.default_for_rows is not None
                else None
            ),
            "reference_height": _normalize_preset_reference_height(
                preset.reference_height
            ),
        }
        for preset in presets
    ]


def migrated_layout_presets(legacy_style: Style) -> list[LayoutPreset]:
    """一次性升级迁移：旧隐形库（``_app_default_style.layouts``）全量转入。

    全部条目 ``default_load=True``——升级后新建工程的布局目录与升级前
    整库带入的行为一致，用户零感知；之后经对话框勾选来收敛。旧库的
    「按行数默认」映射同步折算进对应条目（``default_for_rows``）。旧库
    缺失的出厂预设（含独立两行条目）按出厂定义补齐，保证迁移后就是
    完整 9 条出厂 + 旧自定义。
    """

    legacy_height = max(int(legacy_style.layout_reference_height), 1)
    rows_by_id = {
        str(raw_id): rows
        for rows, raw_id in legacy_style.default_layout_by_row_count.items()
    }
    presets = [
        LayoutPreset(
            layout=deepcopy(layout),
            default_load=True,
            default_for_rows=rows_by_id.get(
                str(layout.layout_id or "").strip()
            ),
            reference_height=legacy_height,
        )
        for layout in legacy_style.layouts
    ]
    known_ids = {str(preset.layout.layout_id or "") for preset in presets}
    for factory in factory_layout_definitions():
        if str(factory.layout_id) in known_ids:
            continue
        presets.append(
            LayoutPreset(
                layout=deepcopy(factory),
                default_load=True,
                reference_height=LEGACY_PRESET_REFERENCE_HEIGHT,
            )
        )
    return presets


def style_for_new_project(
    app_default_style: Style,
    presets: list[LayoutPreset],
    title_layout_name: str | None = None,
) -> Style:
    """从应用默认样式 + 布局预设库物化新建工程的初始样式。

    - 布局目录 = 库中 ``default_load`` 条目的拷贝（layout_id 保留），各条
      按自身 ``reference_height`` 换算到应用基准高度；**目录里只有这些条
      目**——任何回退都不得往目录里补种新条目；
    - 不在目录中的出厂预设写入 ``hidden_builtin_layout_ids``，防止
      :func:`ensure_page_layout_defaults` 补种复活；
    - 标题 ``layout_index`` 重定位：``title_layout_name``（应用记忆的布局名，
      可指向仅存在于预设库的布局）优先按名解析；缺省时退回旧库 index 按
      名跟随。名字在物化目录里找不到时回落目录中的出厂「タイトル左上」；
      连它都不在（被取消默认加载/删除）则回落
      :data:`DANGLING_TITLE_LAYOUT_INDEX`——稳定悬空引用，渲染与标题下拉
      按既有口径回退出厂「タイトル左上」外观（v4 口径不变），而目录里
      不补种任何条目；
    - 行数默认映射：冻结旧映射里只保留 ``"default"`` 目标（index-0 显式
      保存），其余条目一律以预设的 ``default_for_rows`` 为准——被用户清
      除的接线不能从冻结旧映射里「复活」。
    """

    target_height = max(int(app_default_style.layout_reference_height), 1)
    loaded = [
        rescale_preset_layout(
            preset.layout, preset.reference_height, target_height
        )
        for preset in presets
        if preset.default_load
    ]
    loaded_builtin_ids = {
        str(layout.layout_id or "")
        for layout in loaded
        if str(layout.layout_id or "") in builtin_preset_layout_ids()
    }
    hidden = sorted(builtin_preset_layout_ids() - loaded_builtin_ids)

    loaded_names = {layout.name for layout in loaded}

    def _resolve_title(title: TitleOverlay) -> TitleOverlay:
        name: str | None = title_layout_name
        if not name:
            index = title.layout_index
            if index is None:
                return title
            index = int(index)
            if index == 0:
                return title
            if not 1 <= index <= len(app_default_style.layouts):
                return title
            name = app_default_style.layouts[index - 1].name
        if name not in loaded_names:
            for position, layout in enumerate(loaded, start=1):
                if layout.name == TITLE_LAYOUT_NAME:
                    return replace(title, layout_index=position)
            # 出厂标题也不在目录：稳定悬空引用（渲染回退出厂外观，目录不补种）。
            return replace(title, layout_index=DANGLING_TITLE_LAYOUT_INDEX)
        return replace(
            title,
            layout_index=next(
                position
                for position, layout in enumerate(loaded, start=1)
                if layout.name == name
            ),
        )

    titles = [_resolve_title(title) for title in app_default_style.title_overlays]
    row_mapping = {
        rows: target
        for rows, target in app_default_style.default_layout_by_row_count.items()
        if target == "default"
    }
    for preset in presets:
        if not preset.default_load or preset.default_for_rows is None:
            continue
        row_mapping[int(preset.default_for_rows)] = str(
            preset.layout.layout_id or ""
        )
    style = replace(
        app_default_style,
        layouts=loaded,
        hidden_builtin_layout_ids=hidden,
        title_overlays=titles,
        default_layout_by_row_count=row_mapping,
    )
    return ensure_page_layout_defaults(style)


def rescale_preset_layout(
    layout: LyricsLayout, source_height: int, target_height: int
) -> LyricsLayout:
    """把单条布局按参考高度等比换算（复用 Style 级缩放的字段口径）。"""

    source_height = max(int(source_height), 1)
    target_height = max(int(target_height), 1)
    if source_height == target_height:
        return deepcopy(layout)
    return rescale_layout_sizes(
        replace(
            Style(),
            layouts=[deepcopy(layout)],
            layout_reference_height=source_height,
        ),
        target_height,
    ).layouts[0]


def merge_layout_preset_library(
    existing: object,
    current: list[LayoutPreset],
    baseline: list[LayoutPreset],
    cleared_rows: tuple[int, ...] = (),
    asserted_rows: Optional[dict[int, str]] = None,
) -> list[dict]:
    """把**本实例自己的**布局库改动叠加到盘上的库，返回要写回的行。

    与 :func:`~krok_helper.subtitle_render.settings.preferences.\
merge_style_preset_library` 同口径：库是多实例共享的，整份覆盖会让最后
    保存的实例抹掉别人新增的条目。``baseline`` 是本实例上次见到的磁盘内
    容；按 ``layout_id`` 归并，只落自己新增 / 修改 / 删除的条目，没动过的
    条目保留磁盘原行（更高版本写入的未知字段不丢）。

    合并同时维护三条库内不变量（多实例并发写入时逐条归并无法自然保持）：

    1. **名称唯一**：本实例新增/改名撞上磁盘上别的实例的同名条目时，本
       实例的条目自动加后缀让名（磁盘先落，视为在先）；本实例自己改名的
       条目会先释放它原先占用的名字——否则「恢复出厂让名」会连规范名
       一起占死，出厂项只能拿到后缀名；
    2. **行数接线唯一**：同一行数档位出现两条接线时，本实例**本次真的改了
       该接线字段**的条目赢（与保存成功提示一致）；本实例只是存量接线、
       磁盘上是别的实例新接的，磁盘赢。只改名称/参数的条目不算改接线；
    3. **显式清除优先**：``cleared_rows`` 是本实例刚显式保存为「全局默认
       布局」的行数档位——不管磁盘上是谁新增的接线，这些档位一律清空
       （否则远端新接的接线会在物化时把全局默认盖掉）；
    4. **显式声明优先**：``asserted_rows``（行数 → 预设 id）是本实例刚在
       布局页点「保存到布局库」选定的默认布局。它与旧基线值可能相同，
       不能靠「相对基线的差异」判断——必须无条件把该档位指到该预设。
    """

    if isinstance(existing, list):
        disk_rows = [deepcopy(row) for row in existing if isinstance(row, dict)]
    else:
        disk_rows = layout_presets_to_dict(layout_presets_from_dict(existing))
    asserted_rows = {
        int(rows): str(preset_id)
        for rows, preset_id in (asserted_rows or {}).items()
    }
    # 改动条目的接线回读以**磁盘原值**为准，需在写入前留一份快照。
    disk_rows_by_id = {
        str((row.get("layout") or {}).get("layout_id") or ""): row
        for row in disk_rows
        if isinstance(row.get("layout"), dict)
    }

    current_by_id = {
        str(preset.layout.layout_id or ""): preset for preset in current
    }
    baseline_by_id = {
        str(preset.layout.layout_id or ""): preset for preset in baseline
    }
    removed_ids = {
        preset_id
        for preset_id in baseline_by_id
        if preset_id not in current_by_id
    }
    # 只有接线字段**真的**变了才算「本次改了接线」：新建条目设置了接线
    # 也算（否则新增预设的默认会输给远端存量接线）；只改名/改参数不算。
    wiring_changed_ids: set[str] = set()
    for preset_id, preset in current_by_id.items():
        previous = baseline_by_id.get(preset_id)
        if previous is None:
            if preset.default_for_rows is not None:
                wiring_changed_ids.add(preset_id)
        elif previous.default_for_rows != preset.default_for_rows:
            wiring_changed_ids.add(preset_id)

    rows_by_id: dict[str, dict] = {}
    order: list[str] = []
    for row in disk_rows:
        layout_payload = row.get("layout")
        preset_id = (
            str(layout_payload.get("layout_id") or "").strip()
            if isinstance(layout_payload, dict)
            else ""
        )
        if not preset_id or preset_id in rows_by_id:
            preset_id = f"layout-{uuid4().hex}"
            row = deepcopy(row)
            if isinstance(row.get("layout"), dict):
                row["layout"] = {**row["layout"], "layout_id": preset_id}
        rows_by_id[preset_id] = row
        order.append(preset_id)

    for preset_id in removed_ids:
        rows_by_id.pop(preset_id, None)

    # ── 名称：两阶段分配，避免条目处理顺序影响结果 ──────────────────────
    # 先算出「本次改名」的条目并释放它们占用的旧名，再统一分配新名。否则
    # 恢复出厂这类「A 让名、B 接手」的组合会因处理顺序不同而互相加后缀
    # （出厂在前时，自定义条目还占着旧名，出厂反而被改成 2 号）。
    changed_ids = [
        preset_id
        for preset_id, preset in current_by_id.items()
        if baseline_by_id.get(preset_id) != preset
    ]
    # 「改名意图」只按**相对基线**的变化判断：本地没改名的条目（哪怕内容
    # 改了）不得把远端刚改好的名字覆盖回去。
    renamed_ids = set()
    for preset_id in changed_ids:
        preset = current_by_id[preset_id]
        baseline_preset = baseline_by_id.get(preset_id)
        if baseline_preset is None or (
            str(preset.layout.name or "").strip()
            != str(baseline_preset.layout.name or "").strip()
        ):
            renamed_ids.add(preset_id)
    # 不改名的条目（含未改动 + 内容改动但名字不变）先占住磁盘上的名字。
    # 注意：本地**未修改**的条目必须始终占用磁盘名——远端可能刚把它改名，
    # 本地的旧名与磁盘名不同，若据此判成「改名」而释放磁盘名，本实例新增
    # 的同名条目就会与它撞成两个同名行（读回时后者被名称去重丢弃）。
    claimed_names: dict[str, str] = {}
    for preset_id in order:
        row = rows_by_id.get(preset_id)
        layout_payload = row.get("layout") if isinstance(row, dict) else None
        disk_name = (
            str(layout_payload.get("name") or "").strip()
            if isinstance(layout_payload, dict)
            else ""
        )
        if not disk_name:
            continue
        if preset_id not in renamed_ids:
            claimed_names[disk_name] = preset_id

    def _allocate_name(preset_id: str, desired: str) -> str:
        """给条目分配库内唯一名字（撞名加后缀），并登记占位。"""

        claimant = claimed_names.get(desired)
        if claimant is not None and claimant != preset_id:
            suffix = 2
            while f"{desired} {suffix}" in claimed_names:
                suffix += 1
            desired = f"{desired} {suffix}"
        claimed_names[desired] = preset_id
        return desired

    # 改名的条目按 current 顺序统一分配（此时所有旧名都已释放）。
    for preset_id in changed_ids:
        preset = current_by_id[preset_id]
        row = layout_presets_to_dict([preset])[0]
        stored = rows_by_id.get(preset_id)
        if isinstance(stored, dict):
            row = merge_extensible_value(
                stored, row, path=("layout_presets",)
            )
        else:
            order.append(preset_id)
        if isinstance(row.get("layout"), dict):
            if preset_id in renamed_ids:
                row["layout"] = {
                    **row["layout"],
                    "name": _allocate_name(
                        preset_id, str(preset.layout.name or "").strip()
                    ),
                }
            else:
                # 没改名：名字以磁盘为准（远端可能刚改过），只落内容变更。
                disk_layout = (
                    stored.get("layout") if isinstance(stored, dict) else None
                )
                if isinstance(disk_layout, dict) and disk_layout.get("name"):
                    row["layout"] = {
                        **row["layout"],
                        "name": disk_layout["name"],
                    }
        rows_by_id[preset_id] = row
    ours_written = set(changed_ids)

    # 未改动条目的接线以磁盘为准；改动条目里没动接线的也回读磁盘值
    # （远端可能已清空/改接，整条回写会把旧接线悄悄还原）。
    for preset_id, preset in current_by_id.items():
        if preset_id not in changed_ids:
            continue
        stored = disk_rows_by_id.get(preset_id)
        if (
            isinstance(stored, dict)
            and preset_id not in wiring_changed_ids
            and isinstance(rows_by_id.get(preset_id), dict)
        ):
            rows_by_id[preset_id] = {
                **rows_by_id[preset_id],
                "default_for_rows": stored.get("default_for_rows"),
            }

    for rows in cleared_rows:
        for preset_id in list(rows_by_id):
            row = rows_by_id[preset_id]
            try:
                wired = int(row.get("default_for_rows"))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            if wired == int(rows):
                rows_by_id[preset_id] = {**row, "default_for_rows": None}

    _resolve_row_wiring_conflicts(
        rows_by_id,
        current_by_id,
        baseline_by_id,
        wiring_changed_ids,
        ours_written,
    )

    # 显式声明「该行数确实用这个预设」：覆盖上面所有推断（用户刚点过保存，
    # 哪怕值与旧基线相同也必须生效）。
    for rows, preset_id in asserted_rows.items():
        if str(preset_id) not in rows_by_id:
            continue
        for other_id in list(rows_by_id):
            if other_id == str(preset_id):
                continue
            other = rows_by_id[other_id]
            try:
                wired = int(other.get("default_for_rows"))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            if wired == int(rows):
                rows_by_id[other_id] = {**other, "default_for_rows": None}
        target = rows_by_id[str(preset_id)]
        rows_by_id[str(preset_id)] = {
            **target,
            "default_for_rows": int(rows),
        }

    return [
        rows_by_id[preset_id]
        for preset_id in dict.fromkeys(order)
        if preset_id in rows_by_id
    ]


def _resolve_row_wiring_conflicts(
    rows_by_id: dict[str, dict],
    current_by_id: dict[str, LayoutPreset],
    baseline_by_id: dict[str, LayoutPreset],
    wiring_changed_ids: set[str],
    ours_written: set[str],
) -> None:
    """同一行数档位只留一条接线（见 merge docstring 的裁决规则 2）。"""

    wired: dict[int, list[str]] = {}
    for preset_id, row in rows_by_id.items():
        raw = row.get("default_for_rows")
        try:
            rows = int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if 1 <= rows <= 8:
            wired.setdefault(rows, []).append(preset_id)

    for rows, preset_ids in wired.items():
        if len(preset_ids) <= 1:
            continue
        mine_changed = [
            preset_id
            for preset_id in preset_ids
            if preset_id in wiring_changed_ids
            and current_by_id[preset_id].default_for_rows == rows
        ]
        if mine_changed:
            # 本实例本次改了接线：赢（与保存提示一致），其余清掉。
            winners = mine_changed
        else:
            # 本实例没动接线：优先让「磁盘上相对我这份基线新增的接线」赢
            # （那是别的实例后来接的）；其次让未被本实例重写的磁盘行赢；
            # 全都被本实例写过时才退回顺序第一。
            remote_new = [
                preset_id
                for preset_id in preset_ids
                if preset_id not in ours_written
                and baseline_by_id.get(preset_id) is not None
                and baseline_by_id[preset_id].default_for_rows != rows
            ]
            disk_owned = [
                preset_id
                for preset_id in preset_ids
                if preset_id not in ours_written
            ]
            winners = remote_new[:1] or disk_owned[:1] or [preset_ids[0]]
        for preset_id in preset_ids:
            if preset_id in winners:
                continue
            row = rows_by_id[preset_id]
            rows_by_id[preset_id] = {**row, "default_for_rows": None}
