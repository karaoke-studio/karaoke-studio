"""Application-preference projections for subtitle rendering.

Project snapshots keep the complete :class:`Style`. Application preferences
remember only reusable defaults and must not inherit project-specific roles or
title content. Keeping that boundary here prevents settings persistence from
depending on Qt widgets.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, fields, replace
from typing import Any, Optional

from krok_helper.subtitle_render.serialization.compat import merge_extensible_value
from krok_helper.subtitle_render.domain.models import (
    LYRICS_LAYOUT_FIELDS,
    STYLE_APPEARANCE_FIELDS,
    TITLE_SCHEME_NAME,
    Style,
    SubtitleStyleScheme,
    TitleOverlay,
    migrate_legacy_app_title_default,
    style_from_dict,
    style_to_dict,
)
from krok_helper.subtitle_render.n3.font_catalog import (
    get_n3_font_catalog,
    normalize_style_font_families,
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
    {"custom_style_schemes", "singer_style_overrides", "title_overlay"}
)
APP_STYLE_EXPLICIT_DEFAULT_FIELDS = (
    BUILTIN_SCHEME_STYLE_FIELDS
    | LAYOUT_DEFAULT_STYLE_FIELDS
    | FONT_DEFAULT_STYLE_FIELDS
    | PROJECT_ONLY_STYLE_FIELDS
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


@dataclass(frozen=True)
class LoadedAppRuntimePreferences:
    """Validated non-style application preferences consumed by the window."""

    output: dict
    auto_chorus_role: str
    auto_chorus_begin_chars: str
    auto_chorus_end_chars: str
    auto_chorus_overwrite: bool
    selected_scheme_key: str
    preview_splitter_ratio: float
    auto_save_enabled: bool
    auto_save_interval_minutes: int
    project_backup_count: int


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


@dataclass(frozen=True)
class PreparedAppPreferences:
    """Projected settings payload plus the normalized reusable style state."""

    data: dict
    app_default_style: Style


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
        auto_chorus_overwrite=bool(auto_chorus.get("overwrite")),
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


def update_app_runtime_preferences(
    existing: dict,
    *,
    auto_chorus_role: str,
    auto_chorus_begin_chars: str,
    auto_chorus_end_chars: str,
    auto_chorus_overwrite: bool,
    selected_scheme_key: str,
    preview_splitter_ratio: float,
    auto_save_enabled: bool,
    auto_save_interval_minutes: int,
    project_backup_count: int,
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
    had_persisted_title = (
        isinstance(persisted_style, dict) and "title_overlay" in persisted_style
    )
    defaults = (
        dict(data.get("new_project_defaults"))
        if isinstance(data.get("new_project_defaults"), dict)
        else {}
    )
    legacy_title = normalized_style.title_overlay or TitleOverlay()
    title_enabled = (
        bool(defaults.get("title_enabled"))
        if "title_enabled" in defaults
        else bool(legacy_title.enabled) if had_persisted_title else False
    )
    if "title_layout_name" in defaults:
        title_layout_index = _layout_index_for_name(
            normalized_style,
            defaults.get("title_layout_name"),
        )
    elif had_persisted_title:
        title_layout_index = int(legacy_title.layout_index or 0)
    else:
        title_layout_index = int(TitleOverlay().layout_index or 0)
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
    app_default_style = replace(
        normalized_style,
        custom_style_schemes={TITLE_SCHEME_NAME: deepcopy(title_scheme)},
        singer_style_overrides={},
        title_overlay=replace(
            TitleOverlay(),
            enabled=title_enabled,
            layout_index=title_layout_index,
            **title_fades,
        ),
    )
    changed |= had_persisted_title or replace(
        app_default_style,
        title_overlay=normalized_style.title_overlay,
    ) != normalized_style
    assignment = defaults.get("layout_assignment")
    layout_assignment = (
        deepcopy(assignment)
        if isinstance(assignment, dict) and assignment.get("mode") in {"all", "auto"}
        else None
    )
    return LoadedAppStylePreferences(
        style=app_default_style,
        layout_assignment=layout_assignment,
        changed=changed,
    )


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
    """Serialize an app default without the per-project title overlay."""
    payload = style_to_dict(style)
    payload.pop("title_overlay", None)
    return payload


def merge_app_setting_field(existing: object, current: object, *, key: str) -> Any:
    """Overlay one app setting while retaining nested fields from newer versions."""
    if isinstance(current, (dict, list)) and isinstance(existing, type(current)):
        source = deepcopy(existing)
        if key == "style" and isinstance(source, dict):
            # Application defaults deliberately exclude per-project title text.
            source.pop("title_overlay", None)
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
    directory_mode: str,
    custom_directory: str,
    name_template: str,
    encoder_mode: str,
    codec: str,
    preset: str,
    crf: object,
    render_workers: object,
    allowed_render_workers: tuple[int, ...],
) -> dict:
    """Return the app-local output settings while preserving unknown keys."""
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
            "render_workers": (
                int(render_workers)
                if isinstance(render_workers, int)
                and render_workers in allowed_render_workers
                else 0
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
    default_title = app_default_style.title_overlay or TitleOverlay()
    new_project_defaults = (
        dict(data.get("new_project_defaults"))
        if isinstance(data.get("new_project_defaults"), dict)
        else {}
    )
    new_project_defaults["title_enabled"] = bool(default_title.enabled)
    new_project_defaults["title_layout_name"] = _layout_name_for_index(
        app_default_style,
        default_title.layout_index,
    )
    new_project_defaults["title_fades"] = merge_app_setting_field(
        new_project_defaults.get("title_fades"),
        {name: getattr(default_title, name) for name in TITLE_FADE_FIELDS},
        key="title_fades",
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
    data["style_presets"] = merge_app_setting_field(
        data.get("style_presets"),
        values.style_presets,
        key="style_presets",
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
        selected_scheme_key=values.selected_scheme_key,
        preview_splitter_ratio=values.preview_splitter_ratio,
        auto_save_enabled=values.auto_save_enabled,
        auto_save_interval_minutes=values.auto_save_interval_minutes,
        project_backup_count=values.project_backup_count,
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
            directory_mode=output.directory_mode,
            custom_directory=output.custom_directory,
            name_template=output.name_template,
            encoder_mode=output.encoder_mode,
            codec=output.codec,
            preset=output.preset,
            crf=output.crf,
            render_workers=output.render_workers,
            allowed_render_workers=output.allowed_render_workers,
        )
    return PreparedAppPreferences(
        data=data,
        app_default_style=app_default_style,
    )
