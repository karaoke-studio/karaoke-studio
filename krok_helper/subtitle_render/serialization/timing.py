"""Persistence codec for subtitle timing, page-plan, animation, and guide values."""

from __future__ import annotations

from typing import Optional

from krok_helper.subtitle_render.domain.timing import (
    GuideSymbol,
    LineAnimationOverride,
    SubtitleLoadingSettings,
    TrackPage,
    TrackPagePlan,
    TrackSection,
)


def subtitle_loading_settings_to_dict(
    settings: SubtitleLoadingSettings,
) -> dict[str, object]:
    return {
        "time_gap_section_enabled": bool(settings.time_gap_section_enabled),
        "section_gap_ms": max(int(settings.section_gap_ms), 0),
        "blank_line_section_enabled": bool(settings.blank_line_section_enabled),
        "rows_per_page": max(1, min(int(settings.rows_per_page), 4)),
        "allocate_layout_by_actual_rows": bool(
            settings.allocate_layout_by_actual_rows
        ),
        "apply_sug_export_compensation": bool(settings.apply_sug_export_compensation),
    }


def subtitle_loading_settings_from_dict(value: object) -> SubtitleLoadingSettings:
    defaults = SubtitleLoadingSettings()
    if not isinstance(value, dict):
        return defaults
    try:
        gap = max(int(value.get("section_gap_ms", defaults.section_gap_ms)), 0)
    except (TypeError, ValueError):
        gap = defaults.section_gap_ms
    try:
        rows = max(1, min(int(value.get("rows_per_page", defaults.rows_per_page)), 4))
    except (TypeError, ValueError):
        rows = defaults.rows_per_page
    return SubtitleLoadingSettings(
        time_gap_section_enabled=bool(
            value.get("time_gap_section_enabled", defaults.time_gap_section_enabled)
        ),
        section_gap_ms=gap,
        blank_line_section_enabled=bool(
            value.get("blank_line_section_enabled", defaults.blank_line_section_enabled)
        ),
        rows_per_page=rows,
        allocate_layout_by_actual_rows=bool(
            value.get(
                "allocate_layout_by_actual_rows",
                defaults.allocate_layout_by_actual_rows,
            )
        ),
        apply_sug_export_compensation=bool(
            value.get(
                "apply_sug_export_compensation",
                defaults.apply_sug_export_compensation,
            )
        ),
    )


def track_page_plan_to_dict(plan: Optional[TrackPagePlan]) -> Optional[dict[str, object]]:
    if plan is None:
        return None
    return {
        "sections": [
            {
                "pages": [
                    {
                        "line_count": max(1, min(int(page.line_count), 8)),
                        "layout_id": str(page.layout_id or "default"),
                    }
                    for page in section.pages
                    if int(page.line_count) > 0
                ]
            }
            for section in plan.sections
            if any(int(page.line_count) > 0 for page in section.pages)
        ]
    }


def track_page_plan_from_dict(value: object) -> Optional[TrackPagePlan]:
    if not isinstance(value, dict) or not isinstance(value.get("sections"), list):
        return None
    sections: list[TrackSection] = []
    for raw_section in value["sections"]:
        if not isinstance(raw_section, dict) or not isinstance(
            raw_section.get("pages"), list
        ):
            continue
        pages: list[TrackPage] = []
        for raw_page in raw_section["pages"]:
            if not isinstance(raw_page, dict):
                continue
            try:
                count = int(raw_page.get("line_count", 0))
            except (TypeError, ValueError):
                continue
            if count <= 0:
                continue
            pages.append(
                TrackPage(
                    line_count=max(1, min(count, 8)),
                    layout_id=str(raw_page.get("layout_id") or "default"),
                )
            )
        if pages:
            sections.append(TrackSection(pages=pages))
    return TrackPagePlan(sections=sections)


def line_animation_override_to_dict(
    override: Optional[LineAnimationOverride],
) -> Optional[dict[str, object]]:
    if override is None:
        return None
    data: dict[str, object] = {
        "entry_anim": override.entry_anim,
        "entry_duration_ms": max(int(override.entry_duration_ms), 0),
        "exit_anim": override.exit_anim,
        "exit_duration_ms": max(int(override.exit_duration_ms), 0),
    }
    # inherit 不落盘：它不带信息，而且没用这个功能的项目重新保存后文件不该平白多
    # 出一个键（读回时缺键本来就按 inherit 处理）。
    if override.karaoke_anim != "inherit":
        data["karaoke_anim"] = override.karaoke_anim
    return data


def line_animation_override_from_dict(value: object) -> Optional[LineAnimationOverride]:
    if not isinstance(value, dict):
        return None
    entry = value.get("entry_anim")
    exit_ = value.get("exit_anim")
    valid_entry = {
        "none", "fade", "slide_in", "rise", "char_fade", "char_drip", "spin_flip", "utopia"
    }
    valid_exit = {
        "none", "fade", "slide_out", "rise", "char_fade", "char_drip", "spin_flip", "utopia"
    }
    if entry not in valid_entry or exit_ not in valid_exit:
        return None

    def duration(key: str, fallback: int) -> int:
        try:
            return max(int(value.get(key, fallback)), 0)
        except (TypeError, ValueError):
            return fallback

    karaoke = value.get("karaoke_anim")
    if karaoke not in {"inherit", "none", "utopia"}:
        # 旧项目没有这一项，按继承处理——渲染结果与加这个字段之前一致。
        karaoke = "inherit"
    return LineAnimationOverride(
        entry_anim=entry,
        entry_duration_ms=duration("entry_duration_ms", 300),
        exit_anim=exit_,
        exit_duration_ms=duration("exit_duration_ms", 300),
        karaoke_anim=karaoke,
    )


def guide_symbol_to_dict(symbol: Optional[GuideSymbol]) -> Optional[dict[str, object]]:
    if symbol is None:
        return None
    data: dict[str, object] = {
        "name": symbol.name,
        "path_commands": [list(command) for command in symbol.path_commands],
        "units_per_em": max(int(symbol.units_per_em), 1),
        "advance_width": max(float(symbol.advance_width), 0.0),
        "duration_ms": max(int(symbol.duration_ms), 0),
        "count": max(int(symbol.count), 1),
        "role_label": symbol.role_label or None,
        "role_labels": [label or None for label in symbol.role_labels],
        "replacement_prefix": list(symbol.replacement_prefix),
    }
    if symbol.kind != "vector" or symbol.bitmap_before_path:
        data.update(
            {
                "kind": symbol.kind,
                "bitmap_before_path": symbol.bitmap_before_path,
                "bitmap_after_path": symbol.bitmap_after_path,
                "bitmap_zoom_percent": max(int(symbol.bitmap_zoom_percent), 1),
                "bitmap_fix_size": bool(symbol.bitmap_fix_size),
                "bitmap_no_decor": bool(symbol.bitmap_no_decor),
                "bitmap_force_wipe_decor": bool(symbol.bitmap_force_wipe_decor),
                "bitmap_margin_left_px": int(symbol.bitmap_margin_left_px),
                "bitmap_margin_right_px": int(symbol.bitmap_margin_right_px),
                "bitmap_margin_bottom_px": int(symbol.bitmap_margin_bottom_px),
                "prefix_timing": symbol.prefix_timing,
            }
        )
    elif symbol.prefix_timing != "pre_roll":
        data["prefix_timing"] = symbol.prefix_timing
    return data


def guide_symbol_from_dict(value: object) -> Optional[GuideSymbol]:
    if not isinstance(value, dict):
        return None
    kind = str(value.get("kind") or "vector")
    if kind not in {"vector", "bitmap"}:
        kind = "vector"
    raw_commands = value.get("path_commands")
    if not isinstance(raw_commands, list):
        if kind != "bitmap":
            return None
        raw_commands = []
    before_path = str(value.get("bitmap_before_path") or "").strip() or None
    after_path = str(value.get("bitmap_after_path") or "").strip() or None
    if kind == "bitmap" and before_path is None:
        return None
    commands: list[tuple[object, ...]] = []
    expected_lengths = {"M": 3, "L": 3, "C": 7, "Q": 5, "Z": 1}
    try:
        for raw in raw_commands:
            if not isinstance(raw, (list, tuple)) or not raw:
                return None
            command_kind = str(raw[0]).upper()
            if len(raw) != expected_lengths.get(command_kind, -1):
                return None
            commands.append((command_kind, *(float(item) for item in raw[1:])))
        units_per_em = max(int(value.get("units_per_em", 1000)), 1)
        advance_width = max(float(value.get("advance_width", units_per_em)), 0.0)
        duration_ms = max(int(value.get("duration_ms", 1000)), 0)
        count = max(int(value.get("count", 1)), 1)
        zoom_percent = max(int(value.get("bitmap_zoom_percent", 100)), 1)
        margin_left = int(value.get("bitmap_margin_left_px", 0))
        margin_right = int(value.get("bitmap_margin_right_px", 0))
        margin_bottom = int(value.get("bitmap_margin_bottom_px", 0))
    except (TypeError, ValueError):
        return None
    if kind == "vector" and not commands:
        return None
    prefix_timing = str(value.get("prefix_timing") or "pre_roll")
    if prefix_timing not in {"pre_roll", "anchored"}:
        prefix_timing = "pre_roll"
    role = value.get("role_label")
    raw_role_labels = value.get("role_labels")
    role_labels = (
        tuple(str(label).strip() or None if label else None for label in raw_role_labels[:count])
        if isinstance(raw_role_labels, list)
        else ()
    )
    raw_replacement_prefix = value.get("replacement_prefix")
    replacement_prefix = (
        tuple(str(text) for text in raw_replacement_prefix if str(text))
        if isinstance(raw_replacement_prefix, list)
        else ()
    )
    return GuideSymbol(
        name=str(value.get("name") or "导唱符"),
        path_commands=tuple(commands),
        units_per_em=units_per_em,
        advance_width=advance_width,
        duration_ms=duration_ms,
        count=count,
        role_label=str(role).strip() or None if role else None,
        role_labels=role_labels,
        replacement_prefix=replacement_prefix,
        kind=kind,  # type: ignore[arg-type]
        bitmap_before_path=before_path,
        bitmap_after_path=after_path,
        bitmap_zoom_percent=zoom_percent,
        bitmap_fix_size=bool(value.get("bitmap_fix_size", False)),
        bitmap_no_decor=bool(value.get("bitmap_no_decor", False)),
        bitmap_force_wipe_decor=bool(value.get("bitmap_force_wipe_decor", False)),
        bitmap_margin_left_px=margin_left,
        bitmap_margin_right_px=margin_right,
        bitmap_margin_bottom_px=margin_bottom,
        prefix_timing=prefix_timing,  # type: ignore[arg-type]
    )
