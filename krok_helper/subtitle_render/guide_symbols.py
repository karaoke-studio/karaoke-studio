"""SVG 导唱符导入与 QPainterPath 轮廓转换。"""

from __future__ import annotations

from functools import lru_cache
import math
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from fontTools.pens.basePen import BasePen
from fontTools.pens.transformPen import TransformPen
from fontTools.svgLib.path import parse_path
from PyQt6.QtGui import QPainterPath, QTransform

from krok_helper.subtitle_render.models import GuideSymbol


class GuideSymbolImportError(ValueError):
    """SVG 无法转换成单色闭合字形轮廓。"""


class _PainterPathPen(BasePen):
    def __init__(self, path: QPainterPath) -> None:
        super().__init__(None)
        self.path = path

    def _moveTo(self, point) -> None:
        self.path.moveTo(*point)

    def _lineTo(self, point) -> None:
        self.path.lineTo(*point)

    def _curveToOne(self, p1, p2, p3) -> None:
        self.path.cubicTo(*p1, *p2, *p3)

    def _qCurveToOne(self, p1, p2) -> None:
        self.path.quadTo(*p1, *p2)

    def _closePath(self) -> None:
        self.path.closeSubpath()


def import_svg_guide_symbol(
    path: Path,
    *,
    duration_ms: int = 1000,
    role_label: str | None = None,
) -> GuideSymbol:
    """把常见 SVG path/shape 展开并归一化为 1000 em 的行内字形。"""
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise GuideSymbolImportError(f"无法读取 SVG：{exc}") from exc

    outline = QPainterPath()
    pen = _PainterPathPen(outline)
    count = 0
    for element, transform in _iter_drawables(root):
        path_data = _element_to_path_data(element)
        if not path_data:
            continue
        try:
            parse_path(path_data, TransformPen(pen, transform))
        except Exception as exc:  # fontTools exposes parser-specific exceptions
            raise GuideSymbolImportError(f"{path.name} 的轮廓解析失败：{exc}") from exc
        count += 1
    bounds = outline.boundingRect()
    if count == 0 or outline.isEmpty() or bounds.width() <= 0 or bounds.height() <= 0:
        raise GuideSymbolImportError(
            "SVG 中没有可用的 path/shape；仅描边图形请先在矢量软件中扩展描边。"
        )

    units_per_em = 1000
    side_margin = 60.0
    target_height = 860.0
    baseline_offset = 20.0
    scale = min(
        target_height / bounds.height(),
        (units_per_em - side_margin * 2) / bounds.width(),
    )
    fitted_width = bounds.width() * scale
    left = (units_per_em - fitted_width) / 2.0
    transform = QTransform()
    transform.translate(
        left - bounds.left() * scale,
        baseline_offset - bounds.bottom() * scale,
    )
    transform.scale(scale, scale)
    fitted = transform.map(outline)
    commands = _path_commands(fitted)
    if not commands:
        raise GuideSymbolImportError("SVG 转换后的字形轮廓为空。")
    return GuideSymbol(
        name=path.stem or "导唱符",
        path_commands=commands,
        units_per_em=units_per_em,
        advance_width=float(units_per_em),
        duration_ms=max(int(duration_ms), 0),
        role_label=role_label or None,
    )


@lru_cache(maxsize=128)
def guide_symbol_path(symbol: GuideSymbol) -> QPainterPath:
    path = QPainterPath()
    for command in symbol.path_commands:
        if not command:
            continue
        kind = str(command[0]).upper()
        values = [float(value) for value in command[1:]]
        if kind == "M" and len(values) == 2:
            path.moveTo(*values)
        elif kind == "L" and len(values) == 2:
            path.lineTo(*values)
        elif kind == "C" and len(values) == 6:
            path.cubicTo(*values)
        elif kind == "Q" and len(values) == 4:
            path.quadTo(*values)
        elif kind == "Z":
            path.closeSubpath()
    return path


def scaled_guide_symbol_path(
    symbol: GuideSymbol,
    *,
    pixel_size: float,
    left: float = 0.0,
    baseline_y: float = 0.0,
) -> QPainterPath:
    scale = max(float(pixel_size), 1.0) / max(int(symbol.units_per_em), 1)
    transform = QTransform()
    transform.translate(float(left), float(baseline_y))
    transform.scale(scale, scale)
    return transform.map(guide_symbol_path(symbol))


def _path_commands(path: QPainterPath) -> tuple[tuple[object, ...], ...]:
    commands: list[tuple[object, ...]] = []
    index = 0
    while index < path.elementCount():
        element = path.elementAt(index)
        if element.isMoveTo():
            commands.append(("M", element.x, element.y))
        elif element.isLineTo():
            commands.append(("L", element.x, element.y))
        elif element.type == QPainterPath.ElementType.CurveToElement:
            if index + 2 >= path.elementCount():
                break
            c2 = path.elementAt(index + 1)
            end = path.elementAt(index + 2)
            commands.append(("C", element.x, element.y, c2.x, c2.y, end.x, end.y))
            index += 2
        index += 1
    # QPainterPath does not expose close markers. The imported SVG parser has
    # already produced coincident final/initial points; fill winding remains valid.
    return tuple(commands)


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _iter_drawables(root: ET.Element):
    yield from _walk_svg(root, (1.0, 0.0, 0.0, 1.0, 0.0, 0.0))


def _walk_svg(element: ET.Element, inherited: tuple[float, ...]):
    current = _compose_transform(inherited, _parse_transform(element.attrib.get("transform", "")))
    tag = _strip_ns(element.tag)
    if tag in {"path", "rect", "circle", "ellipse", "line", "polyline", "polygon"}:
        style = _parse_style(element.attrib.get("style", ""))
        if (element.attrib.get("display") or style.get("display")) != "none" and (
            element.attrib.get("visibility") or style.get("visibility")
        ) != "hidden":
            yield element, current
    for child in list(element):
        yield from _walk_svg(child, current)


def _element_to_path_data(element: ET.Element) -> str:
    tag = _strip_ns(element.tag)
    if tag == "path":
        return element.attrib.get("d", "").strip()
    if tag == "rect":
        x, y = _number(element, "x"), _number(element, "y")
        width, height = _number(element, "width"), _number(element, "height")
        if width <= 0 or height <= 0:
            return ""
        rx = min(_number(element, "rx"), width / 2)
        ry = min(_number(element, "ry"), height / 2)
        if rx <= 0 and ry <= 0:
            return f"M{x},{y} H{x + width} V{y + height} H{x} Z"
        rx = rx or ry
        ry = ry or rx
        return (
            f"M{x + rx},{y} H{x + width - rx} A{rx},{ry} 0 0 1 {x + width},{y + ry} "
            f"V{y + height - ry} A{rx},{ry} 0 0 1 {x + width - rx},{y + height} "
            f"H{x + rx} A{rx},{ry} 0 0 1 {x},{y + height - ry} V{y + ry} "
            f"A{rx},{ry} 0 0 1 {x + rx},{y} Z"
        )
    if tag in {"circle", "ellipse"}:
        cx, cy = _number(element, "cx"), _number(element, "cy")
        rx = _number(element, "r") if tag == "circle" else _number(element, "rx")
        ry = rx if tag == "circle" else _number(element, "ry")
        if rx <= 0 or ry <= 0:
            return ""
        return f"M{cx + rx},{cy} A{rx},{ry} 0 1 0 {cx - rx},{cy} A{rx},{ry} 0 1 0 {cx + rx},{cy} Z"
    if tag == "line":
        return f"M{_number(element, 'x1')},{_number(element, 'y1')} L{_number(element, 'x2')},{_number(element, 'y2')}"
    if tag in {"polyline", "polygon"}:
        values = [float(item) for item in re.findall(_NUMBER_RE, element.attrib.get("points", ""))]
        points = list(zip(values[0::2], values[1::2], strict=False))
        if not points:
            return ""
        result = [f"M{points[0][0]},{points[0][1]}"]
        result.extend(f"L{x},{y}" for x, y in points[1:])
        if tag == "polygon":
            result.append("Z")
        return " ".join(result)
    return ""


_NUMBER_RE = r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?"


def _number(element: ET.Element, name: str) -> float:
    match = re.match(_NUMBER_RE, element.attrib.get(name, "").strip())
    return float(match.group(0)) if match else 0.0


def _parse_style(value: str) -> dict[str, str]:
    return {
        key.strip(): item.strip()
        for part in value.split(";")
        if ":" in part
        for key, item in [part.split(":", 1)]
    }


def _compose_transform(outer: tuple[float, ...], inner: tuple[float, ...]) -> tuple[float, ...]:
    a1, b1, c1, d1, e1, f1 = outer
    a2, b2, c2, d2, e2, f2 = inner
    return (
        a1 * a2 + c1 * b2,
        b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2,
        b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1,
        b1 * e2 + d1 * f2 + f1,
    )


def _parse_transform(value: str) -> tuple[float, ...]:
    result: tuple[float, ...] = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    for name, raw_args in re.findall(r"([A-Za-z]+)\s*\(([^)]*)\)", value or ""):
        args = [float(item) for item in re.findall(_NUMBER_RE, raw_args)]
        kind = name.lower()
        local: tuple[float, ...]
        if kind == "matrix" and len(args) >= 6:
            local = tuple(args[:6])
        elif kind == "translate":
            local = (1.0, 0.0, 0.0, 1.0, args[0] if args else 0.0, args[1] if len(args) > 1 else 0.0)
        elif kind == "scale":
            sx = args[0] if args else 1.0
            local = (sx, 0.0, 0.0, args[1] if len(args) > 1 else sx, 0.0, 0.0)
        elif kind == "rotate":
            angle = math.radians(args[0] if args else 0.0)
            rotation = (math.cos(angle), math.sin(angle), -math.sin(angle), math.cos(angle), 0.0, 0.0)
            if len(args) >= 3:
                cx, cy = args[1], args[2]
                local = _compose_transform(
                    _compose_transform((1.0, 0.0, 0.0, 1.0, cx, cy), rotation),
                    (1.0, 0.0, 0.0, 1.0, -cx, -cy),
                )
            else:
                local = rotation
        elif kind == "skewx" and args:
            local = (1.0, 0.0, math.tan(math.radians(args[0])), 1.0, 0.0, 0.0)
        elif kind == "skewy" and args:
            local = (1.0, math.tan(math.radians(args[0])), 0.0, 1.0, 0.0, 0.0)
        else:
            local = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
        result = _compose_transform(result, local)
    return result
