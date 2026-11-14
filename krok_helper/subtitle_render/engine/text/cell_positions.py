"""Pure authored-cell flow shared by Qt and canonical ruby layout."""

def char_left_positions(
    char_widths: list[int],
    base_x: int,
    rtl: bool,
    letter_spacing_px: int = 0,
    char_gaps: list[int] | None = None,
    n3_no_backtracking: bool = False,
) -> list[int]:
    """Return each character's left edge for LTR or RTL text flow."""
    # 零宽单元格（负余白压瘪的 guide 占位）不占字间距：它后面的字符
    # 直接贴上前一个可见字符，与 line_text_width 的口径保持一致。
    lefts: list[int] = []
    total_width = sum(char_widths) + letter_spacing_px * sum(
        1 for width in char_widths[:-1] if int(width) > 0
    )
    if rtl:
        cursor = base_x + total_width
        for width in char_widths:
            cursor -= width
            lefts.append(cursor)
            if int(width) <= 0:
                continue
            advance = width + letter_spacing_px
            cursor -= (
                max(advance, 0) - width
                if n3_no_backtracking
                else letter_spacing_px
            )
    else:
        cursor = base_x
        for index, width in enumerate(char_widths):
            if char_gaps is not None and index < len(char_gaps):
                cursor += char_gaps[index]
            lefts.append(cursor)
            if int(width) <= 0:
                continue
            advance = width + letter_spacing_px
            cursor += max(advance, 0) if n3_no_backtracking else advance
    return lefts
