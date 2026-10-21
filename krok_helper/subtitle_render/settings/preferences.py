"""Application-preference projections for subtitle rendering.

Project snapshots keep the complete :class:`Style`. Application preferences
remember only reusable defaults and must not inherit project-specific roles or
title content. Keeping that boundary here prevents settings persistence from
depending on Qt widgets.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, fields, replace
from typing import Any, Optional
from uuid import uuid4

from krok_helper.subtitle_render.engine.export.encoder_select import (
    DEFAULT_VIDEO_BITRATE_MBPS,
    normalize_video_bitrate_mbps,
)
from krok_helper.subtitle_render.serialization.compat import merge_extensible_value
from krok_helper.subtitle_render.settings.layout_presets import (
    LayoutPreset,
    default_layout_presets,
    layout_presets_from_dict,
    layout_presets_to_dict,
    merge_layout_preset_library,
    migrated_layout_presets,
    style_for_new_project,
)
from krok_helper.subtitle_render.domain.models import (
    LYRICS_LAYOUT_FIELDS,
    PRESET_REFERENCE_HEIGHT,
    STYLE_APPEARANCE_FIELDS,
    TITLE_LAYOUT_NAME,
    TITLE_SCHEME_NAME,
    Style,
    StylePreset,
    SubtitleStyleScheme,
    TitleOverlay,
    migrate_legacy_app_title_default,
    style_from_dict,
    style_to_dict,
    rescale_font_sizes,
    rescale_layout_sizes,
    subtitle_style_scheme_from_dict,
    subtitle_style_scheme_to_dict,
)
from krok_helper.subtitle_render.domain.timing import SubtitleLoadingSettings
from krok_helper.subtitle_render.n3.font_catalog import (
    get_n3_font_catalog,
    normalize_scheme_font_families,
    normalize_style_font_families,
)
from krok_helper.subtitle_render.serialization.timing import (
    subtitle_loading_settings_from_dict,
)
from krok_helper.subtitle_render.settings.screen import (
    ScreenSettings,
    screen_settings_from_dict,
)


BUILTIN_SCHEME_STYLE_FIELDS = frozenset(STYLE_APPEARANCE_FIELDS)
LAYOUT_DEFAULT_VALUE_FIELDS = frozenset(
    (*LYRICS_LAYOUT_FIELDS, "upper_line_left_margin_px", "lower_line_right_margin_px")
)
LAYOUT_DEFAULT_STYLE_FIELDS = frozenset(
    (*LAYOUT_DEFAULT_VALUE_FIELDS, "layouts", "layout_reference_height")
)
FONT_DEFAULT_STYLE_FIELDS = frozenset({"font_reference_height"})
PROJECT_ONLY_STYLE_FIELDS = frozenset(
    {
        "custom_style_schemes",
        "singer_style_overrides",
        "title_overlays",
        # 软件预设布局的删除标记只属于当前工程，不能污染新项目的出厂布局。
        "hidden_builtin_layout_ids",
    }
)
SIGNAL_MODULE_STYLE_FIELDS: frozenset[str] = frozenset(
    field.name
    for field in fields(Style)
    if field.name.startswith(("lit_", "volume_", "signals_"))
)
"""指示灯/音量柱（SignalsLits 模块）的全部 :class:`Style` 字段。

这些字段是「改一次就一直沿用」的用户习惯（与标题同一口径）：只经
``_remember_style_preferences`` 的编辑差分写进应用默认样式，不随随手
打开的工程经 :func:`merge_common_style_preferences` 覆盖——否则打开任
何音量柱/指示灯关闭的旧工程，习惯立刻被冲回出厂。按字段前缀动态推导，
将来 SignalsLits 加新字段自动归队。
"""
PARTICLE_MODULE_STYLE_FIELDS: frozenset[str] = frozenset(
    {"fx_particle_color_layers"}
)
"""粒子装饰模块的「改一次一直沿用」习惯字段（2026-10 用户口径）。

粒子「取色层级」（仅实色 / +描边 / +装饰 / 全有）是跨工程的口味偏好，
与指示灯/音量柱同口径：只经 ``_remember_style_preferences`` 的编辑差分
写进应用默认样式，不随随手打开的工程经
:func:`merge_common_style_preferences` 覆盖。其余 ``fx_particle_*``
（颜色模式/双色槽/角色来源）仍逐工程随行样式走，不进本集合。
"""
APP_STYLE_EXPLICIT_DEFAULT_FIELDS = (
    BUILTIN_SCHEME_STYLE_FIELDS
    | LAYOUT_DEFAULT_STYLE_FIELDS
    | FONT_DEFAULT_STYLE_FIELDS
    | PROJECT_ONLY_STYLE_FIELDS
    # 行数→布局映射属于软件级选择（「保存为软件默认布局」按行数写入），
    # 不能被随手打开的工程经 merge_common_style_preferences 覆盖。
    | frozenset({"default_layout_by_row_count"})
    | SIGNAL_MODULE_STYLE_FIELDS
    | PARTICLE_MODULE_STYLE_FIELDS
)
APP_LOCAL_ONLY_OUTPUT_FIELDS = frozenset(
    {
        "gpu_preview_enabled",
        "gpu_preview_default_version",
        "preview_quality",
        "gpu_export_enabled",
        "gpu_export_default_version",
        "directory_mode",
        "custom_directory",
        "name_template",
        "render_workers",
    }
)
TITLE_FADE_FIELDS = (
    "fade_in_ms",
    "fade_out_ms",
    "tail_fade_in_ms",
    "tail_fade_out_ms",
)
"""标题「淡入淡出时长」记忆字段（四档开头/片尾行）。

自定义时间段的淡入淡出编辑也写回这份习惯（新建时间段直接沿用）；它的
**时间**才逐曲，不在此列。``tail_*`` 是 ``Optional``，``None`` 表示"跟随
开头"，原样记住。
"""
TITLE_TIMING_FIELDS = (
    "show_mode",
    "head_offset_ms",
    "duration_ms",
    "tail_offset_ms",
    "tail_duration_ms",
)
"""标题「显示时段」里跟着用户习惯走的字段。

自定义时间段的**时间**（开始/结束）是逐曲的，不在此列——新建条目/时间段
时按工程时长现铺；``tail_duration_ms`` 是 ``Optional``，``None`` 表示
"跟随开头"，原样记住。
"""
_TITLE_SHOW_MODES = frozenset({"custom", "whole", "head", "tail", "head_tail"})
DEFAULT_AUTO_SAVE_INTERVAL_MINUTES = 5
DEFAULT_PROJECT_BACKUP_COUNT = 5
DEFAULT_PREVIEW_SPLITTER_RATIO = 0.4
DISCARDED_BACKUP_RETENTION_DAYS = 7


@dataclass(frozen=True)
class LoadedAppStylePreferences:
    """Normalized reusable style defaults loaded from ``settings.json``."""

    style: Style
    layout_assignment: Optional[dict]
    changed: bool
    title_layout_name: Optional[str] = None
    """记忆的新建工程标题布局名（``new_project_defaults.title_layout_name``）。

    可指向仅存在于布局预设库的布局——index 空间解析不到它，物化新建工程
    时按这个名字在预设库里解析。键缺失（旧版 / 全新安装）时为 ``None``。
    """


@dataclass(frozen=True)
class LoadedAppRuntimePreferences:
    """Validated non-style application preferences consumed by the window."""

    output: dict
    auto_chorus_role: str
    auto_chorus_begin_chars: str
    auto_chorus_end_chars: str
    auto_chorus_overwrite: bool
    auto_chorus_auto_apply: bool
    guide_replacement: dict
    selected_scheme_key: str
    preview_splitter_ratio: float
    auto_save_enabled: bool
    auto_save_interval_minutes: int
    project_backup_count: int


@dataclass(frozen=True)
class LoadedAppPreferences:
    """Complete normalized application state adopted by the frontend."""

    subtitle_loading_defaults: SubtitleLoadingSettings
    output: dict
    app_default_style: Style
    project_style: Style
    layout_assignment: Optional[dict]
    auto_chorus_role: str
    auto_chorus_begin_chars: str
    auto_chorus_end_chars: str
    auto_chorus_overwrite: bool
    auto_chorus_auto_apply: bool
    guide_replacement: dict
    style_presets: dict[str, StylePreset]
    layout_presets: list[LayoutPreset]
    screen: ScreenSettings
    selected_scheme_key: str
    preview_splitter_ratio: float
    auto_save_enabled: bool
    auto_save_interval_minutes: int
    project_backup_count: int
    changed: bool
    title_layout_name: Optional[str] = None
    """记忆的新建工程标题布局名（透传自 ``new_project_defaults``）。"""


@dataclass(frozen=True)
class AppOutputPreferenceValues:
    """Widget-independent values persisted in the app-local output section."""

    gpu_preview_enabled: bool
    gpu_preview_default_version: int
    preview_quality: str
    gpu_export_enabled: bool
    gpu_export_default_version: int
    directory_mode: str
    custom_directory: str
    name_template: str
    encoder_mode: str
    codec: str
    preset: str
    crf: object
    render_workers: object
    allowed_render_workers: tuple[int, ...]
    output_format: str = "mp4"
    gpu_direct_present: bool = False
    bitrate_mbps: object = DEFAULT_VIDEO_BITRATE_MBPS


@dataclass(frozen=True)
class AppPreferenceSaveInput:
    """Complete non-Qt contract for projecting current app preferences."""

    app_default_style: Style
    project_style: Style
    layout_assignment: Optional[dict]
    subtitle_loading_defaults: dict
    style_presets: dict
    screen: dict
    auto_chorus_role: str
    auto_chorus_begin_chars: str
    auto_chorus_end_chars: str
    auto_chorus_overwrite: bool
    selected_scheme_key: str
    preview_splitter_ratio: float
    auto_save_enabled: bool
    auto_save_interval_minutes: int
    project_backup_count: int
    output: Optional[AppOutputPreferenceValues] = None
    #: 新歌词源落位后是否自动识别括号和声；默认开启，未传时按出厂语义处理。
    auto_chorus_auto_apply: bool = True
    guide_replacement: Optional[dict] = None
    """「批量识别导唱标记」对话框的上次设置；``None``/空保持磁盘现状。"""
    style_presets_baseline: Optional[dict] = None
    """本实例上次见到的磁盘预设库；给出后只提交自己的增量，见
    :func:`merge_style_preset_library`。``None`` 保持整份覆盖的旧行为。"""
    layout_presets: Optional[list[LayoutPreset]] = None
    """布局预设库当前内容；``None`` 保持磁盘现值不动。"""
    layout_presets_baseline: Optional[list[LayoutPreset]] = None
    """本实例上次见到的磁盘布局库；给出后只提交自己的增量（多实例共享库），
    ``None`` 保持整份覆盖（旧行为）。"""
    cleared_layout_rows: tuple[int, ...] = ()
    """本实例刚把哪些行数档位显式保存为「全局默认布局」。

    这些档位的行数接线要跨实例清空：别的实例新增的同档位接线本实例并不
    知道，只清本地已知条目的话，物化时它仍会盖掉全局默认。"""
    asserted_layout_rows: Optional[dict[int, str]] = None
    """本实例刚在布局页显式指定的「行数 → 预设 id」默认布局。

    与旧基线值可能相同，不能靠差异判断；给出后合并器无条件把该档位指向
    该预设（并清掉同档位的其他接线）。"""
    title_layout_name: Optional[str] = None
    """记忆的新建工程标题布局名；给出时覆盖 index 推导的名字（可指向仅在
    布局预设库中的布局），``None`` 时按 ``app_default_style`` 的标题 index
    推导（旧行为）。"""


@dataclass(frozen=True)
class PreparedAppPreferences:
    """Projected settings payload plus the normalized reusable style state."""

    data: dict
    app_default_style: Style
    style_presets: dict = field(default_factory=dict)
    """磁盘与本实例增量合并后的预设库；调用方应据此更新自己的内存与基线。"""
    layout_presets: Optional[list[LayoutPreset]] = None
    """规范化后的布局预设库；输入为 ``None``（本次未提供）时为 ``None``，
    调用方应保留自己的内存现值。"""


def load_app_runtime_preferences(
    data: dict,
    *,
    chorus_begin_default: str,
    chorus_end_default: str,
) -> LoadedAppRuntimePreferences:
    """Normalize app-local runtime preferences without touching UI state."""

    output = (
        dict(data.get("output")) if isinstance(data.get("output"), dict) else {}
    )
    auto_chorus = (
        data.get("auto_chorus") if isinstance(data.get("auto_chorus"), dict) else {}
    )
    selected_scheme_key = data.get("selected_scheme_key")
    if not isinstance(selected_scheme_key, str) or not selected_scheme_key:
        selected_scheme_key = "global"
    ratio = data.get("preview_splitter_ratio")
    preview_splitter_ratio = (
        min(max(float(ratio), 0.15), 0.85)
        if isinstance(ratio, (int, float))
        else DEFAULT_PREVIEW_SPLITTER_RATIO
    )
    auto_save = data.get("auto_save")
    if not isinstance(auto_save, dict):
        auto_save = {}
    backup = data.get("backup")
    if not isinstance(backup, dict):
        backup = {}
    return LoadedAppRuntimePreferences(
        output=output,
        auto_chorus_role=str(auto_chorus.get("role") or ""),
        auto_chorus_begin_chars=(
            str(auto_chorus.get("begin_chars") or "") or chorus_begin_default
        ),
        auto_chorus_end_chars=(
            str(auto_chorus.get("end_chars") or "") or chorus_end_default
        ),
        auto_chorus_overwrite=bool(auto_chorus.get("overwrite", False)),
        # 两个开关的缺失回落都按出厂语义：覆盖默认关（保留打轴模块逐字点出来的
        # 歌手分配），自动应用默认开（导入即按记忆参数识别括号和声）。
        auto_chorus_auto_apply=bool(auto_chorus.get("auto_apply", True)),
        guide_replacement=_guide_replacement_memory(data.get("guide_replacement")),
        selected_scheme_key=selected_scheme_key,
        preview_splitter_ratio=preview_splitter_ratio,
        auto_save_enabled=bool(auto_save.get("enabled", True)),
        auto_save_interval_minutes=_bounded_int(
            auto_save.get("interval_minutes"),
            default=DEFAULT_AUTO_SAVE_INTERVAL_MINUTES,
            minimum=1,
            maximum=60,
        ),
        project_backup_count=_bounded_int(
            backup.get("history_count"),
            default=DEFAULT_PROJECT_BACKUP_COUNT,
            minimum=1,
            maximum=20,
        ),
    )


def _bounded_int(value: object, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


_GUIDE_REPLACEMENT_TEXT_FIELDS = ("before_path", "after_path", "marker")
_GUIDE_REPLACEMENT_MARGIN_FIELDS = (
    "margin_left_px",
    "margin_right_px",
    "margin_bottom_px",
)


def _guide_replacement_memory(value: object) -> dict:
    """Normalize the remembered「批量识别导唱标记」dialog state.

    字段名与 :class:`~krok_helper.subtitle_render.frontend.dialogs.\
guide_replacement.GuidePrefixReplaceDialog` 的 ``batch_settings()`` 一致；
    坏值逐项回落默认，整个段缺失或非法时返回空 dict（等于没有记忆）。
    """

    if not isinstance(value, dict) or not value:
        return {}
    memory: dict[str, object] = {
        name: str(value.get(name) or "").strip()
        for name in _GUIDE_REPLACEMENT_TEXT_FIELDS
    }
    memory["non_prefix"] = bool(value.get("non_prefix"))
    memory["no_decor"] = bool(value.get("no_decor"))
    # Zoom / Fix 与 GuideBitmapOptionsRow 的字面量一致；这里不 import 前端模块。
    memory["zoom_mode"] = "Fix" if value.get("zoom_mode") == "Fix" else "Zoom"
    memory["zoom_value"] = _bounded_int(
        value.get("zoom_value"), default=100, minimum=1, maximum=1000
    )
    for name in _GUIDE_REPLACEMENT_MARGIN_FIELDS:
        try:
            memory[name] = int(value.get(name))
        except (TypeError, ValueError):
            memory[name] = 0
    return memory


def update_app_runtime_preferences(
    existing: dict,
    *,
    auto_chorus_role: str,
    auto_chorus_begin_chars: str,
    auto_chorus_end_chars: str,
    auto_chorus_overwrite: bool,
    auto_chorus_auto_apply: bool,
    selected_scheme_key: str,
    preview_splitter_ratio: float,
    auto_save_enabled: bool,
    auto_save_interval_minutes: int,
    project_backup_count: int,
    guide_replacement: Optional[dict] = None,
) -> dict:
    """Project current runtime preferences onto a preserved settings payload."""

    data = deepcopy(existing)
    data["auto_chorus"] = merge_app_setting_field(
        data.get("auto_chorus"),
        {
            "role": auto_chorus_role,
            "begin_chars": auto_chorus_begin_chars,
            "end_chars": auto_chorus_end_chars,
            "overwrite": bool(auto_chorus_overwrite),
            "auto_apply": bool(auto_chorus_auto_apply),
        },
        key="auto_chorus",
    )
    data["selected_scheme_key"] = (
        selected_scheme_key
        if selected_scheme_key in {"global", f"custom:{TITLE_SCHEME_NAME}"}
        else "global"
    )
    data["preview_splitter_ratio"] = round(preview_splitter_ratio, 4)
    data["auto_save"] = merge_app_setting_field(
        data.get("auto_save"),
        {
            "enabled": bool(auto_save_enabled),
            "interval_minutes": int(auto_save_interval_minutes),
        },
        key="auto_save",
    )
    data["backup"] = merge_app_setting_field(
        data.get("backup"),
        {
            "history_count": int(project_backup_count),
            "discarded_retention_days": DISCARDED_BACKUP_RETENTION_DAYS,
        },
        key="backup",
    )
    if guide_replacement:
        data["guide_replacement"] = merge_app_setting_field(
            data.get("guide_replacement"),
            guide_replacement,
            key="guide_replacement",
        )
    return data


def load_app_style_preferences(
    data: dict,
    *,
    font_catalog: Optional[Any] = None,
) -> LoadedAppStylePreferences:
    """Load app defaults while migrating legacy title and font settings."""
    loaded_style = migrate_legacy_app_title_default(style_from_dict(data.get("style")))
    normalized_style, changed = normalize_style_font_families(
        loaded_style,
        font_catalog if font_catalog is not None else get_n3_font_catalog(),
    )
    title_scheme = normalized_style.custom_style_schemes.get(
        TITLE_SCHEME_NAME,
        Style().custom_style_schemes[TITLE_SCHEME_NAME],
    )
    persisted_style = data.get("style")
    had_persisted_title = isinstance(persisted_style, dict) and (
        "title_overlay" in persisted_style or "title_overlays" in persisted_style
    )
    defaults = (
        dict(data.get("new_project_defaults"))
        if isinstance(data.get("new_project_defaults"), dict)
        else {}
    )
    # 应用偏好只记忆一份「新标题默认值」：取（可能来自旧版单标题 key 的）
    # 首条目。
    legacy_title = (normalized_style.title_overlays or [TitleOverlay()])[0]
    title_enabled = (
        bool(defaults.get("title_enabled"))
        if "title_enabled" in defaults
        else bool(legacy_title.enabled) if had_persisted_title else False
    )
    def _default_title_layout_index(style: Style) -> int:
        """出厂回落：优先「タイトル左上」，找不到才用条目默认的 index 1。"""
        seeded = _layout_index_for_name(style, TITLE_LAYOUT_NAME)
        return seeded if seeded > 0 else int(TitleOverlay().layout_index or 0)

    if "title_layout_name" in defaults:
        raw_title_name = defaults.get("title_layout_name")
        if isinstance(raw_title_name, str) and raw_title_name:
            title_layout_index = _layout_index_for_name(
                normalized_style,
                raw_title_name,
            )
            if title_layout_index <= 0:
                # 记忆的布局名不在旧库（升级迁移冻结的目录）里：与悬空标题引用
                # 同口径，回落出厂「タイトル左上」，而不是让标题挂到全局默认
                # 布局（index 0）。记忆名本身经 title_layout_name 原样保留，
                # 新建工程物化时按名在预设库里再解析一次。
                title_layout_index = _default_title_layout_index(normalized_style)
        else:
            # 记忆为「全局默认布局」（index 0，保存时名字为 None）：显式保留
            # index 0，不做出厂回落——否则用户选的默认布局习惯会被冲掉。
            title_layout_index = 0
    elif had_persisted_title:
        title_layout_index = int(legacy_title.layout_index or 0)
    else:
        title_layout_index = _default_title_layout_index(normalized_style)
    if "title_scheme_name" in defaults:
        scheme_value = defaults.get("title_scheme_name")
        title_scheme_name = (
            scheme_value if isinstance(scheme_value, str) and scheme_value else None
        )
    elif had_persisted_title:
        title_scheme_name = legacy_title.scheme_name
    else:
        title_scheme_name = TitleOverlay().scheme_name
    persisted_fades = defaults.get("title_fades")
    title_fades: dict[str, object] = {}
    if isinstance(persisted_fades, dict):
        for name in TITLE_FADE_FIELDS:
            if name not in persisted_fades:
                continue
            value = persisted_fades[name]
            if value is None:
                title_fades[name] = None
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                title_fades[name] = max(int(value), 0)
    title_timing = _title_timing_from_payload(defaults.get("title_timing"))
    app_default_style = replace(
        normalized_style,
        custom_style_schemes={TITLE_SCHEME_NAME: deepcopy(title_scheme)},
        singer_style_overrides={},
        title_overlays=[
            replace(
                TitleOverlay(),
                enabled=title_enabled,
                layout_index=title_layout_index,
                scheme_name=title_scheme_name,
                **title_fades,
                **title_timing,
            )
        ],
    )
    changed |= had_persisted_title or replace(
        app_default_style,
        title_overlays=normalized_style.title_overlays,
    ) != normalized_style
    assignment = defaults.get("layout_assignment")
    layout_assignment = (
        deepcopy(assignment)
        if isinstance(assignment, dict) and assignment.get("mode") in {"all", "auto"}
        else None
    )
    remembered_title_layout = defaults.get("title_layout_name")
    return LoadedAppStylePreferences(
        style=app_default_style,
        layout_assignment=layout_assignment,
        changed=changed,
        title_layout_name=(
            remembered_title_layout
            if isinstance(remembered_title_layout, str) and remembered_title_layout
            else None
        ),
    )


def _title_timing_from_payload(payload: object) -> dict[str, object]:
    """Read remembered show-mode/offset/duration habits; garbage falls back.

    与 ``title_fades`` 同一套口径：模式必须是已知档位，时长/偏移钳到非负
    整数；``tail_duration_ms`` 的 ``None`` 是"跟随开头"的合法值。坏值逐字段
    丢弃（回落条目默认），不让手改坏的 settings.json 拖垮整个标题。
    """
    timing: dict[str, object] = {}
    if not isinstance(payload, dict):
        return timing
    mode = payload.get("show_mode")
    if mode in _TITLE_SHOW_MODES:
        timing["show_mode"] = mode
    for name in ("head_offset_ms", "duration_ms", "tail_offset_ms"):
        value = payload.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            timing[name] = max(int(value), 0)
    tail = payload.get("tail_duration_ms")
    if tail is None:
        timing["tail_duration_ms"] = None
    elif isinstance(tail, (int, float)) and not isinstance(tail, bool):
        timing["tail_duration_ms"] = max(int(tail), 0)
    return timing


def _layout_index_for_name(style: Style, name: object) -> int:
    if not isinstance(name, str) or not name:
        return 0
    return next(
        (
            index
            for index, layout in enumerate(style.layouts, start=1)
            if layout.name == name
        ),
        0,
    )


def _layout_name_for_index(style: Style, index: object) -> Optional[str]:
    try:
        resolved = int(index)
    except (TypeError, ValueError):
        resolved = 0
    if 1 <= resolved <= len(style.layouts):
        return style.layouts[resolved - 1].name
    return None


def merge_common_style_preferences(
    app_default_style: Style,
    project_style: Style,
) -> Style:
    """Copy reusable edits into app defaults without leaking project content.

    Built-in scheme and layout defaults change only through their explicit
    "save as default" actions. Project roles, singer mappings, and title
    content are never application defaults.
    """
    common_changes = {
        field.name: deepcopy(getattr(project_style, field.name))
        for field in fields(Style)
        if field.name not in APP_STYLE_EXPLICIT_DEFAULT_FIELDS
    }
    title_scheme = app_default_style.custom_style_schemes.get(
        TITLE_SCHEME_NAME,
        Style().custom_style_schemes[TITLE_SCHEME_NAME],
    )
    return replace(
        app_default_style,
        **common_changes,
        custom_style_schemes={TITLE_SCHEME_NAME: deepcopy(title_scheme)},
        singer_style_overrides={},
    )


def app_default_style_to_dict(style: Style) -> dict:
    """Serialize an app default without the per-project title overlays."""
    payload = style_to_dict(style)
    payload.pop("title_overlay", None)
    payload.pop("title_overlays", None)
    return payload


def merge_app_setting_field(existing: object, current: object, *, key: str) -> Any:
    """Overlay one app setting while retaining nested fields from newer versions."""
    if isinstance(current, (dict, list)) and isinstance(existing, type(current)):
        source = deepcopy(existing)
        if key == "style" and isinstance(source, dict):
            # Application defaults deliberately exclude per-project title text.
            source.pop("title_overlay", None)
            source.pop("title_overlays", None)
        return merge_extensible_value(source, current, path=(str(key),))
    return deepcopy(current)


def update_app_output_preferences(
    existing: object,
    *,
    gpu_preview_enabled: bool,
    gpu_preview_default_version: int,
    preview_quality: str,
    gpu_export_enabled: bool,
    gpu_export_default_version: int,
    gpu_direct_present: bool = False,
    directory_mode: str,
    custom_directory: str,
    name_template: str,
    encoder_mode: str,
    codec: str,
    preset: str,
    crf: object,
    render_workers: object,
    allowed_render_workers: tuple[int, ...],
    output_format: str = "mp4",
    bitrate_mbps: object = DEFAULT_VIDEO_BITRATE_MBPS,
) -> dict:
    """Return the app-local output settings while preserving unknown keys."""
    from krok_helper.subtitle_render.engine.export.render_job import OUTPUT_FORMATS

    output = dict(existing) if isinstance(existing, dict) else {}
    output.update(
        {
            "native_export_enabled": False,
            "gpu_preview_enabled": bool(gpu_preview_enabled),
            "gpu_preview_default_version": int(gpu_preview_default_version),
            "preview_quality": str(preview_quality),
            "gpu_export_enabled": bool(gpu_export_enabled),
            "gpu_export_default_version": int(gpu_export_default_version),
            "directory_mode": str(directory_mode),
            "custom_directory": str(custom_directory),
            "name_template": str(name_template),
            "encoder_mode": str(encoder_mode),
            "codec": str(codec),
            "preset": str(preset),
            "crf": int(crf) if isinstance(crf, int) and 0 <= crf <= 51 else 18,
            "bitrate_mbps": normalize_video_bitrate_mbps(bitrate_mbps),
            "render_workers": (
                int(render_workers)
                if isinstance(render_workers, int)
                and render_workers in allowed_render_workers
                else 0
            ),
            "gpu_direct_present": bool(gpu_direct_present),
            "output_format": (
                str(output_format)
                if str(output_format) in OUTPUT_FORMATS
                else "mp4"
            ),
        }
    )
    return output


def prepare_app_preferences(
    existing: dict,
    values: AppPreferenceSaveInput,
) -> PreparedAppPreferences:
    """Project current application state onto a preserved settings payload."""

    app_default_style = merge_common_style_preferences(
        values.app_default_style,
        values.project_style,
    )
    data = deepcopy(existing)
    data["style"] = merge_app_setting_field(
        data.get("style"),
        app_default_style_to_dict(app_default_style),
        key="style",
    )
    default_title = (app_default_style.title_overlays or [TitleOverlay()])[0]
    new_project_defaults = (
        dict(data.get("new_project_defaults"))
        if isinstance(data.get("new_project_defaults"), dict)
        else {}
    )
    new_project_defaults["title_enabled"] = bool(default_title.enabled)
    remembered_title_name = (
        values.title_layout_name
        if isinstance(values.title_layout_name, str) and values.title_layout_name
        else _layout_name_for_index(app_default_style, default_title.layout_index)
    )
    new_project_defaults["title_layout_name"] = remembered_title_name
    new_project_defaults["title_scheme_name"] = (
        default_title.scheme_name
        if isinstance(default_title.scheme_name, str) and default_title.scheme_name
        else None
    )
    new_project_defaults["title_fades"] = merge_app_setting_field(
        new_project_defaults.get("title_fades"),
        {name: getattr(default_title, name) for name in TITLE_FADE_FIELDS},
        key="title_fades",
    )
    new_project_defaults["title_timing"] = merge_app_setting_field(
        new_project_defaults.get("title_timing"),
        {name: getattr(default_title, name) for name in TITLE_TIMING_FIELDS},
        key="title_timing",
    )
    if values.layout_assignment is None:
        new_project_defaults.pop("layout_assignment", None)
    else:
        new_project_defaults["layout_assignment"] = deepcopy(
            values.layout_assignment
        )
    data["new_project_defaults"] = new_project_defaults
    data["subtitle_loading_defaults"] = merge_app_setting_field(
        data.get("subtitle_loading_defaults"),
        values.subtitle_loading_defaults,
        key="subtitle_loading_defaults",
    )
    if values.style_presets_baseline is None:
        merged_presets = style_presets_from_dict(values.style_presets)
        data["style_presets"] = merge_app_setting_field(
            data.get("style_presets"),
            values.style_presets,
            key="style_presets",
        )
    else:
        # 预设库是多实例共享的：只提交自己的增量，别把别人的新增覆盖掉。
        rows = merge_style_preset_library(
            data.get("style_presets"),
            style_presets_from_dict(values.style_presets),
            style_presets_from_dict(values.style_presets_baseline),
        )
        data["style_presets"] = rows
        merged_presets = style_presets_from_dict(rows)
    if values.layout_presets is not None:
        if values.layout_presets_baseline is None:
            data["layout_presets"] = layout_presets_to_dict(values.layout_presets)
        else:
            # 布局库是多实例共享的：与样式预设库同口径，只提交本实例的
            # 增量，别把别的实例新增的条目整库覆盖掉。
            data["layout_presets"] = merge_layout_preset_library(
                data.get("layout_presets"),
                values.layout_presets,
                values.layout_presets_baseline,
                cleared_rows=values.cleared_layout_rows,
                asserted_rows=values.asserted_layout_rows,
            )
        # 迁移闸门一并落盘：之后即使 layout_presets 键被外部清掉，也不会
        # 再回放升级迁移（缺键 + 闸门为真 = 出厂库）。
        data["layout_presets_migrated"] = True
    merged_layout_presets = (
        layout_presets_from_dict(data.get("layout_presets"))
        if values.layout_presets is not None
        else None
    )
    data["screen"] = merge_app_setting_field(
        data.get("screen"),
        values.screen,
        key="screen",
    )
    data = update_app_runtime_preferences(
        data,
        auto_chorus_role=values.auto_chorus_role,
        auto_chorus_begin_chars=values.auto_chorus_begin_chars,
        auto_chorus_end_chars=values.auto_chorus_end_chars,
        auto_chorus_overwrite=values.auto_chorus_overwrite,
        auto_chorus_auto_apply=values.auto_chorus_auto_apply,
        selected_scheme_key=values.selected_scheme_key,
        preview_splitter_ratio=values.preview_splitter_ratio,
        auto_save_enabled=values.auto_save_enabled,
        auto_save_interval_minutes=values.auto_save_interval_minutes,
        project_backup_count=values.project_backup_count,
        guide_replacement=values.guide_replacement,
    )
    if values.output is not None:
        output = values.output
        data["output"] = update_app_output_preferences(
            data.get("output"),
            gpu_preview_enabled=output.gpu_preview_enabled,
            gpu_preview_default_version=output.gpu_preview_default_version,
            preview_quality=output.preview_quality,
            gpu_export_enabled=output.gpu_export_enabled,
            gpu_export_default_version=output.gpu_export_default_version,
            gpu_direct_present=output.gpu_direct_present,
            directory_mode=output.directory_mode,
            custom_directory=output.custom_directory,
            name_template=output.name_template,
            encoder_mode=output.encoder_mode,
            codec=output.codec,
            preset=output.preset,
            crf=output.crf,
            bitrate_mbps=output.bitrate_mbps,
            render_workers=output.render_workers,
            allowed_render_workers=output.allowed_render_workers,
            output_format=output.output_format,
        )
    return PreparedAppPreferences(
        data=data,
        app_default_style=app_default_style,
        style_presets=merged_presets,
        layout_presets=merged_layout_presets,
    )


def _preset_reference_height(value: object) -> int:
    """Load one preset's reference height; bad or missing values fall back.

    旧库数据从未记录保存时的高度，统一按 :data:`PRESET_REFERENCE_HEIGHT`
    （1080）解释——多数工程即 1080p，其余来源的偏差无法事后挽回。
    """
    try:
        height = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return PRESET_REFERENCE_HEIGHT
    return height if height > 0 else PRESET_REFERENCE_HEIGHT


def style_presets_from_dict(payload: object) -> dict[str, StylePreset]:
    """Load stable-ID presets and migrate the legacy name-to-scheme mapping."""

    result: dict[str, StylePreset] = {}

    def add(raw_id: object, raw_name: object, value: object) -> None:
        name = str(raw_name or "").strip()
        if not name:
            return
        preset_id = str(raw_id or "").strip()
        if not preset_id or preset_id in result:
            preset_id = uuid4().hex
        if isinstance(value, StylePreset):
            resolved_name = str(value.name).strip() or name
            resolved_id = str(value.preset_id).strip() or preset_id
            if resolved_id in result:
                resolved_id = uuid4().hex
            result[resolved_id] = StylePreset(
                name=resolved_name,
                group=str(value.group).strip(),
                scheme=deepcopy(value.scheme),
                preset_id=resolved_id,
                source_type=str(value.source_type).strip(),
                source_data=deepcopy(value.source_data),
                reference_height=_preset_reference_height(value.reference_height),
            )
            return
        if isinstance(value, SubtitleStyleScheme):
            result[preset_id] = StylePreset(
                name=name,
                scheme=deepcopy(value),
                preset_id=preset_id,
            )
            return
        source_type = ""
        source_data: dict = {}
        reference_height = PRESET_REFERENCE_HEIGHT
        if isinstance(value, dict) and isinstance(value.get("scheme"), dict):
            group = str(value.get("group") or "").strip()
            scheme_payload = value["scheme"]
            source_type = str(value.get("source_type") or "").strip()
            if isinstance(value.get("source_data"), dict):
                source_data = deepcopy(value["source_data"])
            reference_height = _preset_reference_height(value.get("reference_height"))
        else:
            group = ""
            scheme_payload = value
        result[preset_id] = StylePreset(
            name=name,
            group=group,
            scheme=subtitle_style_scheme_from_dict(scheme_payload),
            preset_id=preset_id,
            source_type=source_type,
            source_data=source_data,
            reference_height=reference_height,
        )

    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            add(item.get("id"), item.get("name"), item)
        return result
    if not isinstance(payload, dict):
        return result
    for raw_key, value in payload.items():
        if isinstance(value, StylePreset):
            add(value.preset_id or raw_key, value.name or raw_key, value)
        else:
            add(raw_key, raw_key, value)
    return result


def merge_style_preset_library(
    existing: object,
    current: dict[str, StylePreset],
    baseline: dict[str, StylePreset],
) -> list[dict]:
    """把**本实例自己的**预设改动叠加到盘上的预设库，返回要写回的行。

    预设库是应用级共享状态，可以同时有多个实例在编辑。整份覆盖会让最后关闭的
    实例把别人新增的预设一起抹掉——它内存里那份是启动时读的，早就过期了。

    ``baseline`` 是本实例上次见到的磁盘内容。用它和 ``current`` 求差，只把新增、
    修改、删除三类**自己动过的**条目落到盘上那份的副本上；自己没动过的条目一律
    原样保留磁盘上的行，于是别人的新增得以保留。预设按稳定 ``preset_id`` 归并，
    因此改名是「修改」而不是「删一个加一个」。

    合并发生在**原始行**上而不是重新序列化整份对象，这样更高版本写入的未知字段
    不会在本版本保存时被抹掉。
    """

    if isinstance(existing, list):
        disk_rows = [deepcopy(row) for row in existing if isinstance(row, dict)]
    else:
        disk_rows = style_presets_to_dict(style_presets_from_dict(existing))

    removed = {
        preset_id for preset_id in baseline if preset_id not in current
    }
    rows_by_id: dict[str, dict] = {}
    order: list[str] = []
    for row in disk_rows:
        preset_id = str(row.get("id") or "").strip()
        if not preset_id or preset_id in rows_by_id:
            preset_id = uuid4().hex
            row = {**row, "id": preset_id}
        rows_by_id[preset_id] = row
        order.append(preset_id)

    for preset_id in removed:
        rows_by_id.pop(preset_id, None)

    for preset_id, preset in current.items():
        previous = baseline.get(preset_id)
        if previous is not None and previous == preset:
            # 自己没动过：磁盘上的那行才是最新的，原样保留。
            continue
        serialized = style_presets_to_dict({preset_id: preset})
        if not serialized:
            continue
        row = serialized[0]
        stored = rows_by_id.get(preset_id)
        if isinstance(stored, dict):
            row = merge_extensible_value(
                stored, row, path=("style_presets",)
            )
        else:
            order.append(preset_id)
        rows_by_id[preset_id] = row

    return [
        rows_by_id[preset_id]
        for preset_id in dict.fromkeys(order)
        if preset_id in rows_by_id
    ]


def style_presets_to_dict(presets: dict[str, StylePreset]) -> list[dict]:
    """Serialize stable-ID style presets without losing source metadata."""

    return [
        {
            "id": str(preset.preset_id or preset_id),
            "name": str(preset.name).strip(),
            "group": str(preset.group).strip(),
            "scheme": subtitle_style_scheme_to_dict(preset.scheme),
            "source_type": str(preset.source_type).strip(),
            "source_data": deepcopy(preset.source_data),
            "reference_height": _preset_reference_height(preset.reference_height),
        }
        for preset_id, preset in presets.items()
        if str(preset.name).strip()
    ]


def load_app_preferences(
    data: dict,
    *,
    chorus_begin_default: str,
    chorus_end_default: str,
    font_catalog: Optional[Any] = None,
) -> LoadedAppPreferences:
    """Normalize the complete persisted application state for adoption."""

    runtime = load_app_runtime_preferences(
        data,
        chorus_begin_default=chorus_begin_default,
        chorus_end_default=chorus_end_default,
    )
    catalog = font_catalog if font_catalog is not None else get_n3_font_catalog()
    loaded_style = load_app_style_preferences(data, font_catalog=catalog)
    app_default_style = deepcopy(loaded_style.style)
    screen = screen_settings_from_dict(data.get("screen"))

    style_presets: dict[str, StylePreset] = {}
    presets_changed = False
    for name, preset in style_presets_from_dict(data.get("style_presets")).items():
        scheme, changed = normalize_scheme_font_families(preset.scheme, catalog)
        style_presets[name] = replace(preset, scheme=scheme) if changed else preset
        presets_changed |= changed

    # 布局预设库三态：正常读回 / 缺键但迁移闸门已置（重置等路径 → 出厂库，
    # 不回放迁移）/ 缺键且未迁移（一次性升级迁移：旧隐形库全量转入并全部
    # 默认加载）。迁移与出厂兜底都置 changed，让调用方落盘。
    raw_layout_presets = data.get("layout_presets")
    if isinstance(raw_layout_presets, list):
        layout_presets = layout_presets_from_dict(raw_layout_presets)
    elif data.get("layout_presets_migrated"):
        layout_presets = default_layout_presets()
        presets_changed = True
    else:
        layout_presets = migrated_layout_presets(app_default_style)
        presets_changed = True

    # 启动工作区种子与「新建工程」同一口径：从预设库物化（勾选默认加载的
    # 条目 + 出厂隐藏清单 + 标题记忆名重定位），再按画布高度重缩放——
    # 否则预设库清空/收敛后启动仍会出现旧库目录，与新建工程不一致。
    workspace_style = style_for_new_project(
        app_default_style,
        layout_presets,
        title_layout_name=loaded_style.title_layout_name,
    )
    project_style = rescale_layout_sizes(workspace_style, screen.height)
    project_style = rescale_font_sizes(project_style, screen.height)
    # 再过一次与工程装配相同的序列化往返（_migrate_title_references 等
    # 装载迁移在此物化），保证启动工作区与「新建工程」的目录逐条一致。
    project_style = style_from_dict(style_to_dict(project_style))

    return LoadedAppPreferences(
        subtitle_loading_defaults=subtitle_loading_settings_from_dict(
            data.get("subtitle_loading_defaults")
        ),
        output=runtime.output,
        app_default_style=app_default_style,
        project_style=project_style,
        layout_assignment=(
            deepcopy(loaded_style.layout_assignment)
            if loaded_style.layout_assignment is not None
            else None
        ),
        auto_chorus_role=runtime.auto_chorus_role,
        auto_chorus_begin_chars=runtime.auto_chorus_begin_chars,
        auto_chorus_end_chars=runtime.auto_chorus_end_chars,
        auto_chorus_overwrite=runtime.auto_chorus_overwrite,
        auto_chorus_auto_apply=runtime.auto_chorus_auto_apply,
        guide_replacement=deepcopy(runtime.guide_replacement),
        style_presets=style_presets,
        layout_presets=layout_presets,
        screen=screen,
        selected_scheme_key=runtime.selected_scheme_key,
        preview_splitter_ratio=runtime.preview_splitter_ratio,
        auto_save_enabled=runtime.auto_save_enabled,
        auto_save_interval_minutes=runtime.auto_save_interval_minutes,
        project_backup_count=runtime.project_backup_count,
        changed=loaded_style.changed or presets_changed,
        title_layout_name=loaded_style.title_layout_name,
    )
