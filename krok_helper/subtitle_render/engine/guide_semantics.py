"""Frame-independent conversion of authored guide symbols into render glyphs."""

from __future__ import annotations

from dataclasses import replace

from krok_helper.subtitle_render.timing import (
    GuideSymbol,
    TimingChar,
    TimingLine,
    guide_symbol_has_visual,
    guide_symbol_replacement_count,
    guide_symbol_role_labels,
)


def render_line_with_guide_symbols(line: TimingLine) -> TimingLine:
    """Return a render-only line with prefix and inline guides materialized."""
    if not line.chars:
        return line
    symbol = line.guide_symbol
    replacement_count = guide_symbol_replacement_count(line, symbol)
    chars = list(line.chars)
    inline_changed = False
    for index, inline_symbol in line.inline_guide_symbols.items():
        if (
            isinstance(index, int)
            and 0 <= index < len(chars)
            and isinstance(inline_symbol, GuideSymbol)
            and guide_symbol_has_visual(inline_symbol)
        ):
            chars[index] = replace(
                chars[index], text="\uFFFC", vector_glyph=inline_symbol
            )
            inline_changed = True
    render_line = (
        replace(line, chars=chars, inline_guide_symbols={})
        if inline_changed
        else line
    )
    symbol = render_line.guide_symbol
    if symbol is None or not guide_symbol_has_visual(symbol):
        return render_line
    if symbol.replacement_prefix:
        if replacement_count == 0:
            return render_line
        labels = guide_symbol_role_labels(symbol)
        guides = [
            TimingChar(
                text="\uFFFC",
                start_ms=int(source.start_ms),
                pause_release_ms=source.pause_release_ms,
                explicit_start=source.explicit_start,
                explicit_end=source.explicit_end,
                role_label=(
                    labels[index] if index < len(labels) else source.role_label
                ),
                vector_glyph=symbol,
            )
            for index, source in enumerate(render_line.chars[:replacement_count])
        ]
        return replace(
            render_line,
            chars=[*guides, *render_line.chars[replacement_count:]],
            guide_symbol=None,
            inline_guide_symbols={},
        )
    first_start = int(render_line.chars[0].start_ms)
    interval = max(int(symbol.duration_ms), 0)
    labels = guide_symbol_role_labels(symbol)
    guides = [
        TimingChar(
            text="\uFFFC",
            start_ms=(
                first_start
                if symbol.prefix_timing == "anchored"
                else first_start - interval * (len(labels) - index)
            ),
            role_label=label,
            vector_glyph=symbol,
        )
        for index, label in enumerate(labels)
    ]
    return replace(
        render_line,
        chars=[*guides, *render_line.chars],
        guide_symbol=None,
        inline_guide_symbols={},
    )


__all__ = ["render_line_with_guide_symbols"]
