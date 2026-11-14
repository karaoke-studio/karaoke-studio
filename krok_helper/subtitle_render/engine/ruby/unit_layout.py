"""Backend-neutral placement of already-measured ruby units.

Each unit is (text, integer box width, pen offset, ink left, ink right).
The Qt adapter and Skia metrics use these exact same alignment/rounding rules.
"""
from collections.abc import Sequence
from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.engine.text.font_slots import is_n3_latin_text
from krok_helper.subtitle_render.engine.layout.line.width_policy import truncate_div
from .style_semantics import ruby_stroke_width, ruby_stroke2_width


def ruby_interval_px(style: Style | None) -> int:
    return int(getattr(style, "ruby_interval_px", 0) or 0)


def resolve_ruby_alignment(
    style: Style | None,
    base_text: str | None,
    reading: str,
) -> str:
    mode = str(getattr(style, "ruby_alignment", "auto") or "auto")
    if mode in {"center", "equal_space"}:
        return mode
    if _ruby_auto_prefers_center(base_text or "") or _ruby_auto_prefers_center(
        reading
    ):
        return "center"
    return "equal_space"


# Same script-letter ranges as D2D text_semantics.cpp rubyAutoCenterLayout.
_RUBY_SCRIPT_LETTER_RANGES = (
    (0x0370, 0x04FF),
    (0x1100, 0x11FF),
    (0x3040, 0x30FF),
    (0x3130, 0x318F),
    (0x3400, 0x9FFF),
    (0xAC00, 0xD7A3),
    (0xF900, 0xFAFF),
)


def _ruby_auto_prefers_center(text: str) -> bool:
    """auto 模式的居中判定（CPU/D2D 同口径，字符区间枚举）。

    N3 原判定要求全文纯 ASCII 字母数字，含一个符号（如 ``e-bay`` 的连
    字符）的英数读音会整体落进均分、字母被撑开。现改为两条规则：

    1. 拉丁字母数字（含 Latin-1 带调字母）多于其他可见字符 → 居中；
    2. 全部可见字符均为符号（既非拉丁字母数字、也非脚本字母区间，
       即「全由符号和空格组成」）→ 居中。

    空白不参与计数；平手（拉丁 == 其他）按均分处理。
    """
    latin = 0
    other = 0
    script_letters = 0
    for char in text:
        if char.isspace():
            continue
        if is_n3_latin_text(char):
            latin += 1
        else:
            other += 1
        if any(
            low <= ord(char) <= high
            for low, high in _RUBY_SCRIPT_LETTER_RANGES
        ):
            script_letters += 1
    if latin > other:
        return True
    return text.strip() != "" and latin == 0 and script_letters == 0


def ruby_layout_gap(
    natural_width: float,
    unit_count: int,
    target_width: float,
    style: Style | None,
    base_text: str | None,
    reading: str,
) -> float:
    """Base inter-unit gap before per-pair stroke clearance floors."""
    if unit_count <= 1:
        return 0.0
    interval = float(ruby_interval_px(style))
    if resolve_ruby_alignment(style, base_text, reading) == "center":
        return interval
    if target_width <= natural_width:
        gap = (target_width - natural_width) / (unit_count - 1)
    else:
        gap = (target_width - natural_width) / (unit_count + 1)
    return max(gap, interval)


def ruby_pair_gap_floors(
    unit_layouts: Sequence[tuple[str, float, float, float, float]],
    style: Style | None,
) -> list[float]:
    """相邻注音单元的最小净空下限，保证两层描边不互相叠压。

    N3 的 advance 契约只预留第一层描边；当二重描边宽于一重、且相邻单元
    几乎没有 side bearing（英数窄字母）时，描边外环会压到邻字。此处按
    「两层描边需要量 − 该对单元在 gap=0 时的墨迹间距」逐对补足缺口，
    墨迹间距按单元盒内墨迹边（控制点多边形口径）计算。注音字间距为负
    表示用户主动压紧（允许叠压），净空整体停用；假名对通常有足够的
    side bearing，下限为 0，布局不受影响。CPU/D2D 两侧同口径
    （d2d_backend_configure.cpp 的 ruby 排布段）。
    """
    pair_count = max(len(unit_layouts) - 1, 0)
    if pair_count == 0 or style is None:
        return [0.0] * pair_count
    if ruby_interval_px(style) < 0:
        return [0.0] * pair_count
    needed = float(ruby_stroke_width(style) + ruby_stroke2_width(style))
    floors: list[float] = []
    for (_unit, width, _offset, _ink_left, ink_right), (
        _next_unit,
        _next_width,
        _next_offset,
        next_ink_left,
        _next_ink_right,
    ) in zip(unit_layouts, unit_layouts[1:]):
        existing = max(width - ink_right, 0.0) + max(next_ink_left, 0.0)
        floors.append(max(needed - existing, 0.0))
    return floors


def _ruby_pair_gaps(
    unit_layouts: Sequence[tuple[str, float, float, float, float]],
    style: Style | None,
    base_gap: float,
) -> list[float]:
    # floor <= 0 时净空不参与，base_gap（含负值 interval）原样生效；
    # 只有正净空才作为下限，而正净空只可能出现在 interval >= 0 时。
    return [
        base_gap if floor <= 0.0 else max(base_gap, floor)
        for floor in ruby_pair_gap_floors(unit_layouts, style)
    ]


def measured_ruby_width(
    unit_layouts: Sequence[tuple[str, float, float, float, float]],
    target_width: int | float | None,
    style: Style | None = None,
    base_text: str | None = None,
) -> float:
    units = [unit[0] for unit in unit_layouts]
    reading = "".join(units)
    natural = sum(
        width for _unit, width, _offset, _ink_left, _ink_right in unit_layouts
    )
    interval = float(ruby_interval_px(style))
    if target_width is None:
        gaps = _ruby_pair_gaps(unit_layouts, style, interval)
        return natural + sum(gaps)
    target = float(max(target_width, 0))
    if len(units) <= 1:
        return max(target, natural)
    gap = ruby_layout_gap(
        natural,
        len(units),
        target,
        style,
        base_text,
        reading,
    )
    gaps = _ruby_pair_gaps(unit_layouts, style, gap)
    return max(target, natural + sum(gaps))


def measured_ruby_left_offset(
    unit_layouts: Sequence[tuple[str, float, float, float, float]],
    target_width: int | float | None,
    style: Style | None = None,
    base_text: str | None = None,
) -> float:
    if target_width is None:
        return 0.0
    units = [unit[0] for unit in unit_layouts]
    reading = "".join(units)
    if not units:
        return 0.0
    natural = sum(
        width for _unit, width, _offset, _ink_left, _ink_right in unit_layouts
    )
    target = float(target_width)
    if len(units) <= 1:
        content_width = natural
    else:
        gap = ruby_layout_gap(
            natural,
            len(units),
            target,
            style,
            base_text,
            reading,
        )
        content_width = natural + sum(_ruby_pair_gaps(unit_layouts, style, gap))
    return min((target - content_width) / 2.0, 0.0)


def measured_ruby_origins(
    unit_layouts: Sequence[tuple[str, float, float, float, float]],
    x: int | float,
    target_width: int | float | None,
    *,
    style: Style | None = None,
    base_text: str | None = None,
) -> list[tuple[str, float, float, float]]:
    """Return N3 ``CharPoint.X`` positions before path-bearing correction."""
    units = [unit[0] for unit in unit_layouts]
    if not units:
        return []
    natural = sum(
        width for _unit, width, _offset, _ink_left, _ink_right in unit_layouts
    )
    interval = float(ruby_interval_px(style))
    if target_width is None:
        gaps = _ruby_pair_gaps(unit_layouts, style, interval)
        cursor = float(x)
        result: list[tuple[str, float, float, float]] = []
        for index, (unit, width, offset, _ink_left, _ink_right) in enumerate(
            unit_layouts
        ):
            result.append((unit, cursor, width, offset))
            cursor += width + (gaps[index] if index < len(gaps) else 0.0)
        return result

    target = float(target_width)
    if len(units) <= 1:
        unit, width, offset, _ink_left, _ink_right = unit_layouts[0]
        unit_left = float(x) + float(truncate_div(int(target - width), 2))
        return [(unit, unit_left, width, offset)]

    reading = "".join(units)
    alignment = resolve_ruby_alignment(style, base_text, reading)
    gap = ruby_layout_gap(
        natural,
        len(units),
        target,
        style,
        base_text,
        reading,
    )
    gaps = _ruby_pair_gaps(unit_layouts, style, gap)
    content_width = natural + sum(gaps)
    if alignment == "center":
        cursor = float(x) + float(truncate_div(int(target - content_width), 2))
    else:
        cursor = float(x) + (target - content_width) / 2.0
    result = []
    for index, (unit, width, offset, _ink_left, _ink_right) in enumerate(
        unit_layouts
    ):
        origin = float(int(cursor)) if alignment == "equal_space" else cursor
        result.append((unit, origin, width, offset))
        cursor += width + (gaps[index] if index < len(gaps) else 0.0)
    return result
