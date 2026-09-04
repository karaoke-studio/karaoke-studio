"""导唱符导入：SVG 矢量轮廓转换 + 位图图片（走字前后双态）。"""

from __future__ import annotations

from functools import lru_cache
import math
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from fontTools.pens.basePen import BasePen
from fontTools.pens.transformPen import TransformPen
from fontTools.svgLib.path import parse_path
from PyQt6.QtGui import QImage, QPainterPath, QTransform

from krok_helper.subtitle_render.domain.timing import GuideSymbol


class GuideSymbolImportError(ValueError):
    """导唱符文件无法转换成可渲染的矢量轮廓或位图。"""


GUIDE_SYMBOL_IMAGE_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif", ".ico", ".tiff", ".tif"}
)
GUIDE_SYMBOL_FILE_FILTER = (
    "导唱符 (*.svg *.png *.jpg *.jpeg *.bmp *.webp *.gif *.ico *.tiff *.tif)"
)


def is_vector_guide_symbol_file(path: Path | str) -> bool:
    """SVG 走原有矢量轮廓路径；其余图片一律按位图导唱符导入。"""
    return Path(path).suffix.lower() == ".svg"


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
    count: int = 1,
    role_label: str | None = None,
) -> GuideSymbol:
    """把常见 SVG path/shape 展开并归一化为 1000 em 的行内字形。"""
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise GuideSymbolImportError(f"无法读取 SVG：{exc}") from exc

    outline = QPainterPath()
    pen = _PainterPathPen(outline)
    drawable_count = 0
    for element, transform in _iter_drawables(root):
        path_data = _element_to_path_data(element)
        if not path_data:
            continue
        try:
            parse_path(path_data, TransformPen(pen, transform))
        except Exception as exc:  # fontTools exposes parser-specific exceptions
            raise GuideSymbolImportError(f"{path.name} 的轮廓解析失败：{exc}") from exc
        drawable_count += 1
    bounds = outline.boundingRect()
    if drawable_count == 0 or outline.isEmpty() or bounds.width() <= 0 or bounds.height() <= 0:
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
    if any(
        not math.isfinite(float(value))
        for command in commands
        for value in command[1:]
    ):
        # NaN/∞ 坐标能穿过 boundingRect 的宽度/高度校验（NaN 比较恒为 False），
        # 但会让渲染 IR 序列化出非法 JSON、GPU 几何与 QPainter 路径全部失效。
        raise GuideSymbolImportError(
            "SVG 轮廓包含非法坐标（NaN 或无穷大），无法导入；请检查源文件。"
        )
    return GuideSymbol(
        name=path.stem or "导唱符",
        path_commands=commands,
        units_per_em=units_per_em,
        advance_width=float(units_per_em),
        duration_ms=max(int(duration_ms), 0),
        count=max(int(count), 1),
        role_label=role_label or None,
    )


def import_bitmap_guide_symbol(
    before_path: Path | str | None,
    after_path: Path | str | None = None,
    *,
    duration_ms: int = 1000,
    count: int = 1,
    role_label: str | None = None,
    zoom_percent: int = 100,
    fix_size: bool = False,
    no_decor: bool = False,
    margin_left_px: int = 0,
    margin_right_px: int = 0,
    margin_bottom_px: int = 0,
) -> GuideSymbol:
    """把位图图片包装成「走字前 / 走字后」双态导唱符。

    ``before_path`` 是走字到达前显示的图片，``after_path`` 是走字经过后显示的
    图片；两者都允许留空，留空的一侧渲染为透明（两侧都为空则无法导入）。
    其余选项与 ``@Emoji`` 标签对应：``zoom_percent``（按字幕行高缩放，
    默认 100）、``fix_size``（``Fix`` 保持原图尺寸）、``no_decor``（不套用
    样式方案的文字装饰 shadow / glow）与三个方向的留白像素（允许负值；
    ``ForceWipeDecor`` 渲染端未实现，不暴露）。与 SVG 不同，位图不提取矢量
    轮廓，按图片原样缩放绘制，布局宽度跟随图片。
    """
    before = str(before_path).strip() if before_path else ""
    after = str(after_path).strip() if after_path else ""
    if not before and not after:
        raise GuideSymbolImportError("走字前后图片不能都为空。")
    for label, value in (("走字前图片", before), ("走字后图片", after)):
        if not value:
            continue
        if not Path(value).is_file():
            raise GuideSymbolImportError(f"{label}不存在：{value}")
        if QImage(str(value)).isNull():
            raise GuideSymbolImportError(f"{label}无法读取：{value}")
    primary = Path(before if before else after)
    return GuideSymbol(
        name=primary.stem or "导唱符",
        kind="bitmap",
        bitmap_before_path=before or None,
        bitmap_after_path=after or None,
        duration_ms=max(int(duration_ms), 0),
        count=max(int(count), 1),
        role_label=role_label or None,
        bitmap_zoom_percent=max(int(zoom_percent), 1),
        bitmap_fix_size=bool(fix_size),
        bitmap_no_decor=bool(no_decor),
        bitmap_margin_left_px=int(margin_left_px),
        bitmap_margin_right_px=int(margin_right_px),
        bitmap_margin_bottom_px=int(margin_bottom_px),
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
    # 不在此处按 (symbol, pixel_size) 缓存缩放结果：GuideSymbol 的哈希要
    # 遍历整份轮廓命令（可达数万条），每次查缓存的代价与变换本身相当，
    # 大符号实测反而显著变慢。跨帧复用由上层字形层缓存负责。
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
