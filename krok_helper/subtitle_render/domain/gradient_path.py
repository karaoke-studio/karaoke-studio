"""Bake editable HSV disk paths into ordinary, backend-neutral RGBA stops."""

from __future__ import annotations

import colorsys
import heapq
import math
from bisect import bisect_right

from .paint import GradientPath

Point = tuple[float, float]
MAX_PATH_POINTS = 1024
MAX_BAKED_STOPS = 256


def rgba(color: str) -> tuple[int, int, int, int]:
    digits = color.lstrip("#")
    if len(digits) in (3, 4):
        digits = "".join(c * 2 for c in digits)
    if len(digits) == 6:
        digits = "FF" + digits
    if len(digits) != 8:
        raise ValueError("Invalid RGBA color")
    a, r, g, b = (int(digits[i:i + 2], 16) for i in range(0, 8, 2))
    return r, g, b, a


def color_hex(color: tuple[int, ...]) -> str:
    r, g, b, a = color
    return (f"#{a:02X}" if a != 255 else "#") + f"{r:02X}{g:02X}{b:02X}"


def disk_point(color: str, fallback_hue: float = 0) -> Point:
    r, g, b, _a = rgba(color)
    hue, saturation, _value = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    angle = (hue if saturation > 1e-9 else fallback_hue) * math.tau
    return math.cos(angle) * saturation, math.sin(angle) * saturation


def clamp_point(point: Point) -> Point:
    x, y = point
    distance = math.hypot(x, y)
    scale = max(1, distance)
    return x / scale, y / scale


def hue_route(start: str, end: str, *, long: bool = False) -> list[Point]:
    """Interpolate hue around the disk, retaining endpoint saturation."""
    first, last = rgba(start), rgba(end)
    h0, s0, _ = colorsys.rgb_to_hsv(*(c / 255 for c in first[:3]))
    h1, s1, _ = colorsys.rgb_to_hsv(*(c / 255 for c in last[:3]))
    if s0 < 1e-9:
        h0 = h1
    if s1 < 1e-9:
        h1 = h0
    delta = (h1 - h0 + 0.5) % 1 - 0.5
    if long:
        delta = delta - 1 if delta >= 0 else delta + 1
    count = max(2, math.ceil(abs(delta) * 72) + 1)
    result = []
    for i in range(count):
        t = i / (count - 1)
        angle = (h0 + delta * t) * math.tau
        saturation = s0 + (s1 - s0) * t
        result.append((math.cos(angle) * saturation, math.sin(angle) * saturation))
    return result


def simplify_points(points: list[Point], tolerance: float = .012) -> list[Point]:
    """Remove pointer jitter using bounded, iterative Ramer–Douglas–Peucker."""
    if len(points) <= 2:
        return list(points)
    keep = {0, len(points) - 1}
    pending = [(0, len(points) - 1)]
    while pending:
        left, right = pending.pop()
        ax, ay = points[left]
        bx, by = points[right]
        dx, dy = bx - ax, by - ay
        length_sq = dx * dx + dy * dy
        worst, selected = tolerance, None
        for i in range(left + 1, right):
            x, y = points[i]
            t = min(1, max(0, ((x - ax) * dx + (y - ay) * dy) / length_sq)) if length_sq else 0
            error = math.hypot(x - ax - t * dx, y - ay - t * dy)
            if error > worst:
                worst, selected = error, i
        if selected is not None:
            keep.add(selected)
            pending.extend(((left, selected), (selected, right)))
    return [points[i] for i in sorted(keep)]


def smooth_points(points: list[Point]) -> list[Point]:
    """One corner-cutting pass stays inside the disk and retains both ends."""
    controls = simplify_points(points)
    result = [controls[0]]
    for a, b in zip(controls, controls[1:]):
        result.extend(((a[0] * .75 + b[0] * .25, a[1] * .75 + b[1] * .25),
                       (a[0] * .25 + b[0] * .75, a[1] * .25 + b[1] * .75)))
    result.append(controls[-1])
    return simplify_points(result, .003)


def path_samples(points: list[Point], start: str, end: str, count: int = 1025) -> list[tuple[int, int, int, int]]:
    """Sample by arc length, independent of drawing speed and event density."""
    if not 2 <= len(points) <= MAX_PATH_POINTS:
        raise ValueError("Invalid gradient path length")
    lengths = [0.0]
    for a, b in zip(points, points[1:]):
        lengths.append(lengths[-1] + math.dist(a, b))
    first, last = rgba(start), rgba(end)
    v0, v1 = max(first[:3]) / 255, max(last[:3]) / 255
    result = []
    for i in range(count):
        t = i / (count - 1)
        distance = lengths[-1] * t
        index = min(bisect_right(lengths, distance) - 1, len(points) - 2)
        span = lengths[index + 1] - lengths[index]
        u = (distance - lengths[index]) / span if span > 1e-12 else t
        x = points[index][0] + (points[index + 1][0] - points[index][0]) * u
        y = points[index][1] + (points[index + 1][1] - points[index][1]) * u
        hue = math.atan2(y, x) / math.tau % 1
        rgb = colorsys.hsv_to_rgb(hue, min(1, math.hypot(x, y)), v0 + (v1 - v0) * t)
        result.append(tuple(round(c * 255) for c in rgb) + (round(first[3] + (last[3] - first[3]) * t),))
    result[0], result[-1] = first, last
    return result


def bake_path(route: GradientPath, start: str, end: str) -> list[tuple[float, str]]:
    """Subdivide on RGBA error, rather than only at geometric turning points."""
    samples = path_samples(route.points, start, end)
    keep = {0, len(samples) - 1}

    def interval_error(left: int, right: int) -> tuple[float, int, int, int]:
        worst, selected = 0.0, left
        for i in range(left + 1, right):
            t = (i - left) / (right - left)
            error = max(abs(samples[i][c] - (samples[left][c] + (samples[right][c] - samples[left][c]) * t)) for c in range(4))
            if error > worst:
                worst, selected = error, i
        return -worst, left, right, selected

    # Split the worst interval first so a complex scribble cannot exhaust the
    # budget on its first half and leave the rest as one inaccurate chord.
    pending = [interval_error(0, len(samples) - 1)]
    while pending and len(keep) < MAX_BAKED_STOPS:
        negative_error, left, right, selected = heapq.heappop(pending)
        if negative_error >= -2:
            break
        keep.add(selected)
        heapq.heappush(pending, interval_error(left, selected))
        heapq.heappush(pending, interval_error(selected, right))
    width = route.end_pct - route.start_pct
    return [(round(route.start_pct + width * i / (len(samples) - 1), 6), color_hex(samples[i])) for i in sorted(keep)]


def replace_path_stops(stops: list[tuple[float, str]], route: GradientPath) -> list[tuple[float, str]]:
    """Replace only the requested segment; keep all other stops verbatim."""
    left, right = path_segment_indices(stops, route.start_pct, route.end_pct)
    start, end = stops[left][1], stops[right][1]
    baked = bake_path(route, start, end)
    return stops[:left] + baked + stops[right + 1:]


def path_segment_indices(stops: list[tuple[float, str]], start: float, end: float) -> tuple[int, int]:
    # Equal-position stops can represent a discontinuity. Preserve the side
    # belonging to the adjacent segment instead of silently recoloring it.
    return (max(i for i, (p, _) in enumerate(stops) if p == start),
            next(i for i, (p, _) in enumerate(stops) if p == end))
