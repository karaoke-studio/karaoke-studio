"""Pure main/Latin slot semantics shared by Qt adapters and canonical text."""
from dataclasses import replace
from krok_helper.subtitle_render.domain.models import Style


def latin_font_size(style: Style) -> int:
    value = style.latin_font_size_px
    return int(value) if value is not None and int(value) > 0 else int(style.font_size_px)


def latin_font_weight(style: Style) -> int:
    value = style.latin_font_weight
    return int(value) if value is not None and int(value) > 0 else int(style.font_weight)


def latin_italic(style: Style) -> bool:
    """英数槽斜体的生效值：``None`` = 跟随主文字·日文（与描边 2 同链）。"""
    value = style.latin_italic
    return bool(style.italic if value is None else value)


def is_n3_latin_text(text: str) -> bool:
    return bool(text) and all(
        ("0" <= char <= "9" or "A" <= char <= "Z" or "a" <= char <= "z")
        or ("\u00c0" <= char <= "\u00d6"
            or "\u00d8" <= char <= "\u00f6"
            or "\u00f8" <= char <= "\u00ff")
        for char in text
    )


def is_emoji_text(text: str) -> bool:
    return any(
        0x1F000 <= ord(char) <= 0x1FAFF or 0x2600 <= ord(char) <= 0x27BF
        for char in text
    )


def font_style_for_cell(style: Style, text: str) -> tuple[Style, float]:
    """Keep the existing whole-authored-cell Latin classification and inheritance."""
    if not is_n3_latin_text(text):
        return style, 1.0
    return replace(style, font_family=style.font_family_latin or style.font_family,
                   font_size_px=latin_font_size(style), font_weight=latin_font_weight(style),
                   italic=latin_italic(style)), max(50, min(200, int(style.latin_font_stretch_pct))) / 100


