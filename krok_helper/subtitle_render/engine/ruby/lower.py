"""Qt/settings adapters for pure render-only lower readings."""

from __future__ import annotations

from dataclasses import replace

from krok_helper.subtitle_render.domain.models import (
    Style, SubtitleStyleScheme, materialize_ruby_decoration_changes,
    subtitle_style_scheme_to_dict,
)
from krok_helper.subtitle_render.domain.timing import RubyAnnotation, TimingLine
from .lower_semantics import (
    LowerRuby, LOWER_APPEARANCE_FIELDS, lower_style,
    lower_rubies_for_line as derive_lower_rubies,
)
from .style import build_ruby_font, build_ruby_font_for_text
from krok_helper.subtitle_render.engine.style.style_semantics import effective_ruby_karaoke_colors
from strange_uta_game.backend.infrastructure.parsers.romaji import RomajiOptions


def read_sug_romaji_options() -> RomajiOptions:
    from strange_uta_game.frontend.settings.app_settings import AppSettings
    return RomajiOptions.from_mapping(AppSettings().get("auto_check", {}))


def snapshot_lower_romaji_options(style: Style | None) -> Style | None:
    """Freeze the embedded SUG choices before handing a scene/job to workers."""
    if style is None or not style.lower_ruby_enabled or style._lower_romaji_options is not None:
        return style
    options = read_sug_romaji_options()
    return replace(style, _lower_romaji_options=(options.repeat_long_vowels,
        options.link_long_vowels, options.link_sokuon, options.uppercase))


def lower_style_snapshot(style: Style) -> dict:
    """Freeze the effective upper reading appearance for independent editing."""
    japanese = build_ruby_font(style)
    latin = build_ruby_font_for_text(style, "a")
    frozen = replace(
        style,
        **materialize_ruby_decoration_changes(style),
        ruby_font_follow_main=False,
        ruby_font_family=japanese.family(),
        ruby_font_weight=int(japanese.weight()),
        ruby_font_family_latin=latin.family(),
        ruby_latin_font_size_px=latin.pixelSize(),
        ruby_latin_font_weight=int(latin.weight()),
        ruby_italic=japanese.italic(), ruby_latin_italic=latin.italic(),
        ruby_colors_follow_main=False,
        ruby_karaoke_colors=effective_ruby_karaoke_colors(style),
    )
    snapshot = SubtitleStyleScheme(**{key: getattr(frozen, key) for key in LOWER_APPEARANCE_FIELDS})
    return {key: value for key, value in subtitle_style_scheme_to_dict(snapshot).items()
            if key in LOWER_APPEARANCE_FIELDS}


def lower_rubies_for_line(
    line: TimingLine, rubies: list[RubyAnnotation],
    intervals: list[tuple[int, int]], style: Style,
) -> list[LowerRuby]:
    """Keep legacy settings lookup lazy and outside the shared semantics."""
    return derive_lower_rubies(line, rubies, intervals, style,
                              read_options=read_sug_romaji_options)
