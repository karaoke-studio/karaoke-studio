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
MAX_ANCHORS = 16


def _pchip_slopes(knots: list[float], values: list[float]) -> list[float]:
    """Shape-preserving cubic tangents without adding a SciPy dependency."""
    spans = [b - a for a, b in zip(knots, knots[1:])]
    slopes = [(b - a) / h for a, b, h in zip(values, values[1:], spans)]
    if len(slopes) == 1:
        return slopes * 2
    result = [0.0] * len(values)
    for i in range(1, len(values) - 1):
        a, b = slopes[i - 1:i + 1]
        if a * b > 0:
            w1, w2 = 2 * spans[i] + spans[i - 1], spans[i] + 2 * spans[i - 1]
            result[i] = (w1 + w2) / (w1 / a + w2 / b)
    for index, h0, h1, d0, d1 in ((0, spans[0], spans[1], slopes[0], slopes[1]),
                                    (-1, spans[-1], spans[-2], slopes[-1], slopes[-2])):
        tangent = ((2 * h0 + h1) * d0 - h0 * d1) / (h0 + h1)
        result[index] = 0 if tangent * d0 <= 0 else math.copysign(min(abs(tangent), 3 * abs(d0)), tangent)
    return result


def anchor_route(colors: list[str], *, long: bool = False) -> GradientPath:
    """Smooth unwrapped HSV hue/saturation through every exact color anchor."""
    if not 2 <= len(colors) <= MAX_ANCHORS:
        raise ValueError("Invalid anchor count")
    hsv = [colorsys.rgb_to_hsv(*(c / 255 for c in rgba(color)[:3])) for color in colors]
    chromatic = [i for i, (_, saturation, _) in enumerate(hsv) if saturation > 1e-9]
    hues = []
    for i, (hue, saturation, _) in enumerate(hsv):
        if saturation <= 1e-9 and chromatic:
            hue = hsv[min(chromatic, key=lambda j: abs(j - i))][0]
        if hues:
            delta = (hue - hues[-1] + .5) % 1 - .5
            if long and saturation > 1e-9 and hsv[i - 1][1] > 1e-9:
                delta = delta - 1 if delta >= 0 else delta + 1
            hue = hues[-1] + delta
        hues.append(hue)
    saturations = [item[1] for item in hsv]
    knots = [0.0]
    for i in range(1, len(colors)):
        distance = math.hypot((hues[i] - hues[i - 1]) * math.tau * (saturations[i] + saturations[i - 1]) / 2,
                              saturations[i] - saturations[i - 1])
        # Same disk coordinates can still have different value/alpha.
        knots.append(knots[-1] + max(distance, .02))
    tangents = [_pchip_slopes(knots, values) for values in (hues, saturations)]
    points, anchors = [], []
    for segment in range(len(colors) - 1):
        span = knots[segment + 1] - knots[segment]
        count = max(8, min(64, math.ceil(span * 20)))
        anchors.append((len(points), colors[segment]))
        for i in range(count):
            t = i / count
            interpolated = []
            for values, slopes in zip((hues, saturations), tangents):
                interpolated.append((2*t**3 - 3*t*t + 1)*values[segment]
                    + (t**3 - 2*t*t + t)*span*slopes[segment]
                    + (-2*t**3 + 3*t*t)*values[segment + 1]
                    + (t**3 - t*t)*span*slopes[segment + 1])
            hue, saturation = interpolated
            radius = min(1, max(0, saturation))
            points.append((math.cos(hue * math.tau) * radius, math.sin(hue * math.tau) * radius))
    anchors.append((len(points), colors[-1]))
    points.append(disk_point(colors[-1]))
    return GradientPath(points=points, anchors=anchors, route_mode="long" if long else "short")


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
    if route.anchors:
        samples, positions, mandatory = anchored_samples(route)
    else:
        samples = path_samples(route.points, start, end)
        positions = [i / (len(samples) - 1) for i in range(len(samples))]
        mandatory = {0, len(samples) - 1}
    keep = set(mandatory)

    def interval_error(left: int, right: int) -> tuple[float, int, int, int]:
        worst, selected = 0.0, left
        for i in range(left + 1, right):
            t = (positions[i] - positions[left]) / (positions[right] - positions[left])
            error = max(abs(samples[i][c] - (samples[left][c] + (samples[right][c] - samples[left][c]) * t)) for c in range(4))
            if error > worst:
                worst, selected = error, i
        return -worst, left, right, selected

    # Split the worst interval first so a complex scribble cannot exhaust the
    # budget on its first half and leave the rest as one inaccurate chord.
    ordered = sorted(mandatory)
    pending = [interval_error(a, b) for a, b in zip(ordered, ordered[1:])]
    heapq.heapify(pending)
    while pending and len(keep) < MAX_BAKED_STOPS:
        negative_error, left, right, selected = heapq.heappop(pending)
        if negative_error >= -2:
            break
        keep.add(selected)
        heapq.heappush(pending, interval_error(left, selected))
        heapq.heappush(pending, interval_error(selected, right))
    width = route.end_pct - route.start_pct
    return [(round(route.start_pct + width * positions[i], 6), color_hex(samples[i])) for i in sorted(keep)]


def anchored_samples(route: GradientPath) -> tuple[list, list[float], set[int]]:
    """Sample each anchor interval, with exact colors pinned at its boundaries."""
    lengths = []
    for (a, first), (b, last) in zip(route.anchors, route.anchors[1:]):
        disk_length = sum(math.dist(p, q) for p, q in zip(route.points[a:b], route.points[a + 1:b + 1]))
        start, end = rgba(first), rgba(last)
        # A gray/black or alpha-only segment may have zero disk length but
        # still needs visible room in the ramp for its value/opacity change.
        length = math.hypot(disk_length, (max(end[:3]) - max(start[:3])) / 255,
                            (end[3] - start[3]) / 255)
        lengths.append(max(length, .0001))
    total = sum(lengths)
    samples, positions, mandatory = [], [], set()
    offset = 0.0
    for segment, ((a, first), (b, last)) in enumerate(zip(route.anchors, route.anchors[1:])):
        count = max(3, math.ceil(1024 * lengths[segment] / total) + 1)
        colors = path_samples(route.points[a:b + 1], first, last, count)
        mandatory.add(len(samples))
        for i, color in enumerate(colors[:-1]):
            samples.append(color)
            positions.append((offset + lengths[segment] * i / (count - 1)) / total)
        offset += lengths[segment]
    mandatory.add(len(samples))
    samples.append(rgba(route.anchors[-1][1]))
    positions.append(1.0)
    return samples, positions, mandatory


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
