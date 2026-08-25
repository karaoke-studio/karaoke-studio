"""Qt-independent controllers behind the subtitle property-panel facade."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Callable

from krok_helper.subtitle_render.domain.models import (
    DecorationKind,
    HORIZONTAL_ALIGNS,
    LYRICS_LAYOUT_FIELDS,
    HorizontalAlign,
    LineHorizontalLayout,
    LineYPosition,
    LyricsLayout,
    N3_FONT_INHERITANCE_FIELDS,
    Style,
    StylePreset,
    SubtitleStyleScheme,
    TITLE_SHOW_MODES,
    TitleOverlay,
    VIEWPORT_ALIGNS,
    ViewportAlign,
    migrate_title_char_role_labels,
)
from krok_helper.subtitle_render.domain.timing import (
    EntryAnimation,
    ExitAnimation,
    KaraokeAnimation,
)
from krok_helper.subtitle_render.domain.paint import (
    KaraokeColors,
    KaraokeColorState,
    PaintFill,
)


SCHEME_ONLY_FIELDS = frozenset({"n3_font_inheritance"})

SCHEME_FIELDS = frozenset(
    {
        "font_family",
        "font_family_latin",
        "font_size_px",
        "latin_font_size_px",
        "latin_font_weight",
        "latin_stroke_width_px",
        "latin_stroke2_enabled",
        "latin_stroke2_width_px",
        "letter_spacing_px",
        "space_width_percent",
        "allow_biting",
        "font_weight",
        "italic",
        "affects_ruby_anchor",
        "base_color",
        "fill_color",
        "fill_gradient_enabled",
        "fill_gradient_start_color",
        "fill_gradient_end_color",
        "fill_gradient_angle_deg",
        "stroke_color",
        "stroke_width_px",
        "stroke2_enabled",
        "stroke2_width_px",
        "decoration_kind",
        "glow_radius_px",
        "glow_before_radius_px",
        "glow_after_radius_px",
        "glow_concentration_level",
        "shadow_color",
        "shadow_offset_x",
        "shadow_offset_y",
        "ruby_font_size_px",
        "ruby_font_family",
        "ruby_font_family_latin",
        "ruby_font_weight",
        "ruby_latin_font_size_px",
        "ruby_latin_font_weight",
        "ruby_font_follow_main",
        "ruby_color",
        "ruby_gap_px",
        "ruby_stroke_width_px",
        "ruby_stroke2_enabled",
        "ruby_stroke2_width_px",
        "ruby_latin_stroke_width_px",
        "ruby_latin_stroke2_enabled",
        "ruby_latin_stroke2_width_px",
        "ruby_decoration_kind",
        "ruby_glow_radius_px",
        "ruby_glow_before_radius_px",
        "ruby_glow_after_radius_px",
        "ruby_glow_concentration_level",
        "ruby_shadow_offset_x",
        "ruby_shadow_offset_y",
        "ruby_colors_follow_main",
        "ruby_horizontal_gradient_with_main",
        "karaoke_colors",
        "ruby_karaoke_colors",
    }
)


@dataclass(frozen=True)
class StyleUpdateResult:
    """One normalized property edit and the fields the UI must resync."""

    style: Style
    changed_fields: frozenset[str]


class PropertyStyleController:
    """Route normalized property edits into global or role-specific style state."""

    @staticmethod
    def value(style: Style, role_name: str | None, field_name: str):
        """Resolve one effective property value through role inheritance."""
        if role_name is not None:
            scheme = style.custom_style_schemes.get(role_name)
            if (
                field_name == "karaoke_colors"
                and scheme is not None
                and scheme.karaoke_colors is None
                and scheme_has_legacy_color_values(scheme)
            ):
                return None
            if field_name == "ruby_karaoke_colors" and scheme is not None:
                return scheme.ruby_karaoke_colors
            value = getattr(scheme, field_name, None) if scheme is not None else None
            if (
                scheme is not None
                and scheme.n3_font_inheritance
                and field_name in N3_FONT_INHERITANCE_FIELDS
            ):
                return value
            if value is not None:
                return value
        return getattr(style, field_name)

    @staticmethod
    def own_value(style: Style, role_name: str | None, field_name: str):
        """Return the selected scheme's stored value without inheritance."""
        if role_name is None:
            return getattr(style, field_name)
        scheme = style.custom_style_schemes.get(role_name)
        return getattr(scheme, field_name, None) if scheme is not None else None

    def current_karaoke_colors(
        self,
        style: Style,
        role_name: str | None,
    ) -> KaraokeColors:
        """Return a detached color matrix, migrating legacy fields on demand."""
        value = self.value(style, role_name, "karaoke_colors")
        if isinstance(value, KaraokeColors):
            return deepcopy(value)
        return self._legacy_colors(style, role_name)

    def snapshot(
        self,
        style: Style,
        role_name: str | None,
    ) -> SubtitleStyleScheme:
        """Detach the selected effective scheme without materializing inheritance."""
        values = {
            field: deepcopy(
                self.own_value(style, role_name, field)
                if field in N3_FONT_INHERITANCE_FIELDS
                else self.value(style, role_name, field)
            )
            for field in SCHEME_FIELDS
        }
        values["decoration_kind"] = normalize_decoration_kind(
            values["decoration_kind"]
        )
        values["glow_radius_px"] = int(values["glow_before_radius_px"])
        values["karaoke_colors"] = self.current_karaoke_colors(style, role_name)
        current = (
            style.custom_style_schemes.get(role_name)
            if role_name is not None
            else None
        )
        values["n3_font_inheritance"] = bool(
            current is not None and current.n3_font_inheritance
        )
        return SubtitleStyleScheme(**values)

    @staticmethod
    def changes_from_scheme(scheme: SubtitleStyleScheme) -> dict[str, object]:
        """Project a reusable scheme onto the editable global style fields."""
        return {
            field: value
            for field in SCHEME_FIELDS
            if (value := getattr(scheme, field)) is not None
        }

    def _legacy_colors(
        self,
        style: Style,
        role_name: str | None,
    ) -> KaraokeColors:
        stroke = _solid_fill(str(self.value(style, role_name, "stroke_color")))
        shadow = _solid_fill(str(self.value(style, role_name, "shadow_color")))
        before = KaraokeColorState(
            text=_solid_fill(str(self.value(style, role_name, "base_color"))),
            stroke=stroke,
            stroke2=_solid_fill("#000000"),
            shadow=shadow,
        )
        after = KaraokeColorState(
            text=self._legacy_after_text_fill(style, role_name),
            stroke=deepcopy(stroke),
            stroke2=_solid_fill("#000000"),
            shadow=deepcopy(shadow),
        )
        return KaraokeColors(before=before, after=after)

    def _legacy_after_text_fill(
        self,
        style: Style,
        role_name: str | None,
    ) -> PaintFill:
        fill_color = str(self.value(style, role_name, "fill_color"))
        if not bool(self.value(style, role_name, "fill_gradient_enabled")):
            return _solid_fill(fill_color)
        start = str(self.value(style, role_name, "fill_gradient_start_color"))
        end = str(self.value(style, role_name, "fill_gradient_end_color"))
        angle = int(self.value(style, role_name, "fill_gradient_angle_deg"))
        return PaintFill(
            mode="gradient_vertical" if angle in {90, 270} else "gradient_horizontal",
            color=fill_color,
            start_color=start,
            end_color=end,
            gradient_stops=[(0, start), (100, end)],
            split_top_color=start,
            split_bottom_color=end,
        )

    def update(
        self,
        style: Style,
        changes: dict[str, object],
        *,
        role_name: str | None = None,
        force_global: bool = False,
        scheme_factory: Callable[[], SubtitleStyleScheme] | None = None,
    ) -> StyleUpdateResult:
        normalized = dict(changes)
        if (
            not force_global
            and normalized
            and set(normalized).issubset(SCHEME_FIELDS | SCHEME_ONLY_FIELDS)
            and role_name is not None
        ):
            schemes = dict(style.custom_style_schemes)
            scheme = schemes.get(role_name)
            if scheme is None:
                if scheme_factory is None:
                    raise ValueError("scheme_factory is required for a missing role scheme")
                scheme = scheme_factory()
            schemes[role_name] = replace(scheme, **normalized)
            normalized = {"custom_style_schemes": schemes}

        if SCHEME_ONLY_FIELDS.intersection(normalized):
            normalized = {
                key: value
                for key, value in normalized.items()
                if key not in SCHEME_ONLY_FIELDS
            }
        normalized = normalize_style_changes(normalized)
        return StyleUpdateResult(
            style=replace(style, **normalized),
            changed_fields=frozenset(normalized),
        )


def normalize_style_changes(changes: dict[str, object]) -> dict[str, object]:
    """Normalize untrusted UI values using the panel's historical fallbacks."""
    normalized = dict(changes)
    if "line_y_position" in normalized:
        normalized["line_y_position"] = normalize_line_position(
            normalized["line_y_position"]
        )
    if "line_horizontal_layout" in normalized:
        normalized["line_horizontal_layout"] = normalize_horizontal_layout(
            normalized["line_horizontal_layout"]
        )
    for align_field in ("row1_align", "row2_align"):
        if align_field in normalized:
            normalized[align_field] = normalize_horizontal_align(
                normalized[align_field]
            )
    if "viewport_align" in normalized:
        normalized["viewport_align"] = normalize_viewport_align(
            normalized["viewport_align"]
        )
    if "section_ending_mode" in normalized:
        normalized["section_ending_mode"] = (
            normalized["section_ending_mode"]
            if normalized["section_ending_mode"] in {"hold", "clear"}
            else "hold"
        )
    if "decoration_kind" in normalized:
        normalized["decoration_kind"] = normalize_decoration_kind(
            normalized["decoration_kind"]
        )
    if "ruby_decoration_kind" in normalized:
        normalized["ruby_decoration_kind"] = (
            None
            if normalized["ruby_decoration_kind"] is None
            else normalize_decoration_kind(normalized["ruby_decoration_kind"])
        )
    if "entry_anim" in normalized:
        normalized["entry_anim"] = normalize_entry_animation(
            normalized["entry_anim"]
        )
    if "exit_anim" in normalized:
        normalized["exit_anim"] = normalize_exit_animation(
            normalized["exit_anim"]
        )
    if "karaoke_anim" in normalized:
        normalized["karaoke_anim"] = normalize_karaoke_animation(
            normalized["karaoke_anim"]
        )
    if "lit_style" in normalized:
        normalized["lit_style"] = normalize_lit_style(normalized["lit_style"])
    if "lit_transition_mode" in normalized:
        normalized["lit_transition_mode"] = normalize_lit_transition_mode(
            normalized["lit_transition_mode"]
        )
    return normalized


def scheme_has_legacy_color_values(scheme: SubtitleStyleScheme) -> bool:
    """Return whether a pre-matrix scheme still owns legacy color fields."""
    return any(
        getattr(scheme, field) is not None
        for field in (
            "base_color",
            "fill_color",
            "fill_gradient_enabled",
            "fill_gradient_start_color",
            "fill_gradient_end_color",
            "fill_gradient_angle_deg",
            "stroke_color",
            "shadow_color",
        )
    )


def _solid_fill(color: str) -> PaintFill:
    return PaintFill(
        mode="solid",
        color=color,
        start_color=color,
        end_color=color,
        gradient_stops=[(0, color), (100, color)],
        split_top_color=color,
        split_bottom_color=color,
    )


def normalize_line_position(value: object) -> LineYPosition:
    if value in {"top", "center", "bottom"}:
        return value  # type: ignore[return-value]
    return "bottom"


def normalize_horizontal_layout(value: object) -> LineHorizontalLayout:
    if value in {"asymmetric", "center", "per_row"}:
        return value  # type: ignore[return-value]
    return "asymmetric"


def normalize_horizontal_align(value: object) -> HorizontalAlign:
    if value in HORIZONTAL_ALIGNS:
        return value  # type: ignore[return-value]
    return "left"


def normalize_viewport_align(value: object) -> ViewportAlign:
    if value in VIEWPORT_ALIGNS:
        return value  # type: ignore[return-value]
    return "center"


def normalize_decoration_kind(value: object) -> DecorationKind:
    if value in {"none", "shadow", "glow"}:
        return value  # type: ignore[return-value]
    return "shadow"


def normalize_entry_animation(value: object) -> EntryAnimation:
    if value in {
        "none",
        "fade",
        "slide_in",
        "rise",
        "char_fade",
        "char_drip",
        "spin_flip",
        "utopia",
    }:
        return value  # type: ignore[return-value]
    return "none"


def normalize_exit_animation(value: object) -> ExitAnimation:
    if value in {
        "none",
        "fade",
        "slide_out",
        "rise",
        "char_fade",
        "char_drip",
        "spin_flip",
        "utopia",
    }:
        return value  # type: ignore[return-value]
    return "none"


def normalize_karaoke_animation(value: object) -> KaraokeAnimation:
    if value in {"inherit", "none", "utopia"}:
        return value  # type: ignore[return-value]
    return "inherit"


def normalize_lit_style(value: object):
    if value in {"volume", "circle", "square", "rounded"}:
        return value
    return "volume"


def normalize_lit_transition_mode(value: object) -> str:
    if value in {"none", "fade", "slide"}:
        return str(value)
    return "fade"


class RoleSchemeController:
    """Own the project role registry and ensure every role has a style scheme."""

    def __init__(self) -> None:
        self._names: list[str] = []

    @property
    def names(self) -> list[str]:
        return list(self._names)

    def replace(self, role_names: list[str]) -> None:
        """Replace roles using the panel's historical normalization semantics."""
        self._names = [str(name) for name in role_names if name]

    def merge(self, role_names: list[str]) -> None:
        """Append trimmed, non-empty roles without removing earlier entries."""
        seen = set(self._names)
        for value in role_names:
            name = str(value or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            self._names.append(name)

    def add(self, name: str) -> None:
        if name and name not in self._names:
            self._names.append(name)

    def rename(self, old: str, new: str) -> None:
        self._names = [new if name == old else name for name in self._names]

    def remove(self, name: str) -> None:
        self._names = [candidate for candidate in self._names if candidate != name]

    def rename_changes(
        self,
        style: Style,
        old: str,
        new: str,
        fallback: SubtitleStyleScheme,
    ) -> dict[str, object]:
        """Rename one project role and its detached style scheme together."""
        schemes = dict(style.custom_style_schemes)
        scheme = schemes.pop(old, fallback)
        schemes[new] = scheme
        self.rename(old, new)
        return {"custom_style_schemes": schemes}

    def delete_changes(self, style: Style, name: str) -> dict[str, object]:
        """Delete one project role without touching the reusable preset library."""
        schemes = dict(style.custom_style_schemes)
        schemes.pop(name, None)
        self.remove(name)
        return {"custom_style_schemes": schemes}

    def ensure_style_schemes(
        self,
        style: Style,
        presets: dict[str, StylePreset],
        fallback: Callable[[int], SubtitleStyleScheme],
    ) -> tuple[Style, bool]:
        """Return a style containing one scheme for every registered role."""
        schemes = dict(style.custom_style_schemes)
        changed = False
        for index, name in enumerate(self._names):
            if name in schemes:
                continue
            matches = [preset for preset in presets.values() if preset.name == name]
            schemes[name] = (
                deepcopy(matches[0].scheme) if len(matches) == 1 else fallback(index)
            )
            changed = True
        if not changed:
            return style, False
        return replace(style, custom_style_schemes=schemes), True


class LayoutCatalogController:
    """Perform layout-catalog model edits without knowing about panel widgets."""

    @staticmethod
    def source(style: Style, index: int):
        return style.default_layout if index <= 0 else style.layouts[index - 1]

    def resolved_values(self, style: Style, index: int) -> dict:
        source = self.source(style, index)
        values = {}
        for name in LYRICS_LAYOUT_FIELDS:
            value = getattr(source, name)
            if value is None:
                value = getattr(style, name)
            values[name] = deepcopy(value)
        return values

    @staticmethod
    def field_changes(style: Style, index: int, changes: dict) -> dict:
        if index <= 0:
            return dict(changes)
        layouts = list(style.layouts)
        layouts[index - 1] = replace(layouts[index - 1], **changes)
        return {"layouts": layouts}

    def add_changes(self, style: Style, source_index: int) -> tuple[dict, int]:
        values = self.resolved_values(style, source_index)
        existing = {layout.name for layout in style.layouts}
        number = len(style.layouts) + 1
        name = f"布局 {number}"
        while name in existing:
            number += 1
            name = f"布局 {number}"
        layouts = list(style.layouts) + [LyricsLayout(name=name, **values)]
        return {"layouts": layouts}, len(layouts)

    @staticmethod
    def rename_changes(style: Style, index: int, name: str) -> dict:
        layouts = list(style.layouts)
        layouts[index - 1] = replace(layouts[index - 1], name=name)
        return {"layouts": layouts}

    @staticmethod
    def delete_changes(style: Style, index: int) -> dict:
        layouts = list(style.layouts)
        del layouts[index - 1]
        changes: dict = {"layouts": layouts}
        title = style.title_overlay
        if title is not None and title.layout_index is not None:
            title_index = int(title.layout_index)
            if title_index == index:
                changes["title_overlay"] = replace(title, layout_index=0)
            elif title_index > index:
                changes["title_overlay"] = replace(
                    title,
                    layout_index=title_index - 1,
                )
        return changes


class TitleOverlayController:
    """Apply title-overlay edits while preserving per-character role labels."""

    @staticmethod
    def current(style: Style) -> TitleOverlay:
        return style.title_overlay if style.title_overlay is not None else TitleOverlay()

    def update(self, style: Style, changes: dict) -> Style:
        title = self.current(style)
        normalized = dict(changes)
        if "text_template" in normalized:
            new_text = str(normalized["text_template"])
            normalized["text_template"] = new_text
            normalized["char_role_labels"] = migrate_title_char_role_labels(
                title.text_template,
                title.char_role_labels,
                new_text,
            )
        if (
            "show_mode" in normalized
            and normalized["show_mode"] not in TITLE_SHOW_MODES
        ):
            normalized["show_mode"] = "whole"
        return replace(style, title_overlay=replace(title, **normalized))
