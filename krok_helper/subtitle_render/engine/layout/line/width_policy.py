"""Integer cell-box rules shared by existing and canonical font metrics."""

def truncate_div(numerator: int, denominator: int) -> int:
    """Integer division truncated toward zero, matching C# arithmetic."""
    if denominator == 0:
        return 0
    sign = -1 if (numerator < 0) != (denominator < 0) else 1
    return sign * (abs(numerator) // abs(denominator))


def nicokara_layout_width(
    ink_width: int,
    advance: int,
    left_bearing: int,
    right_bearing: int,
    *,
    edge_size: int,
    allow_biting: bool,
) -> int:
    advance = max(int(advance), 1)
    left = int(left_bearing)
    right = int(right_bearing)
    if not allow_biting:
        left = max(left, 0)
        right = max(right, 0)
    body_width = truncate_div(
        max(int(ink_width), 0) * (left + advance + right),
        advance,
    )
    return max(body_width, 0) + max(int(edge_size), 0)


def nicokara_char_geometry_left_offset(
    ink_width: int,
    advance: int,
    left_bearing: int,
    *,
    allow_biting: bool,
) -> int:
    advance = max(int(advance), 1)
    left = int(left_bearing)
    if not allow_biting:
        left = max(left, 0)
    return truncate_div(max(int(ink_width), 0) * left, advance)


