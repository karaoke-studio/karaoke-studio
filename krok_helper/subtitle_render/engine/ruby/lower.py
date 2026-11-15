"""Qt/settings adapters for pure render-only lower readings."""

from __future__ import annotations

import threading
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


# One immutable romaji-option snapshot per layout pass (per thread).  Width
# measurement and painting both derive lower readings inside the same
# ``layout_pass``; reading the mutable embedded settings again per derivation
# could change a reading between the width pass and the paint pass when the
# user saves SUG settings concurrently.
_OPTIONS_SCOPE = threading.local()


def read_sug_romaji_options() -> RomajiOptions:
    from strange_uta_game.frontend.settings.app_settings import AppSettings
    return RomajiOptions.from_mapping(AppSettings().get("auto_check", {}))


def _pass_scoped_romaji_options() -> RomajiOptions:
    """Sample the embedded SUG choices at most once per layout pass."""
    from krok_helper.subtitle_render.engine.layout.layout_context import _LAYOUT_PASS
    # ``ruby_gaps`` is allocated once per outermost pass and released on exit,
    # so its identity is an exact pass token.  The retained dict holds only
    # numeric cache keys/values, so keeping one reference is bounded.
    token = getattr(_LAYOUT_PASS, "ruby_gaps", None)
    if token is None:
        return read_sug_romaji_options()
    scope = _OPTIONS_SCOPE
    if getattr(scope, "pass_token", None) is token:
        return scope.options
    options = read_sug_romaji_options()
    scope.pass_token = token
    scope.options = options
    return options


def _embedded_settings_available() -> bool:
    """Return whether live embedded SUG settings may be sampled here.

    The GUI/main thread of the host has ``AppSettings._default_provider`` set;
    renderer workers (same process, another thread) and export subprocesses do
    not, and must keep using the frozen tuple that was shipped with their
    style/job.
    """
    if threading.current_thread() is not threading.main_thread():
        return False
    from strange_uta_game.frontend.settings.app_settings import AppSettings
    return getattr(AppSettings, "_default_provider", None) is not None


def snapshot_lower_romaji_options(style: Style | None) -> Style | None:
    """Freeze the embedded SUG choices before handing a scene/job to workers.

    On the GUI thread a live provider is authoritative: every new scene/patch
    refreshes the frozen tuple, so changing SUG's 罗马音风格 (促音链接 /
    长音链接 / 长音复写 / 大写) takes effect without reopening the project.
    Worker processes keep the tuple they received.
    """
    if style is None or not style.lower_ruby_enabled:
        return style
    if style._lower_romaji_options is not None and not _embedded_settings_available():
        return style
    options = read_sug_romaji_options()
    current = (options.repeat_long_vowels, options.link_long_vowels,
               options.link_sokuon, options.uppercase)
    if style._lower_romaji_options == current:
        return style
    return replace(style, _lower_romaji_options=current)


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
                              read_options=_pass_scoped_romaji_options)
