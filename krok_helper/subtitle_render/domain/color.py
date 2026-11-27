"""Lossless linear-sRGB colors alongside the existing RGB/ARGB hex values.

The explicit ``linear(r,g,b,a)`` value fits existing color slots without
reinterpreting legacy hex colors. Only display boundaries quantize or clip it.
"""
from __future__ import annotations

import colorsys
import math

MAX_LINEAR_CHANNEL = 65504.0  # Largest finite value supported by the F16 paint surfaces.


def srgb_to_linear(value: float) -> float:
    return value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4


def linear_to_srgb(value: float) -> float:
    return value * 12.92 if value <= .0031308 else 1.055 * value ** (1 / 2.4) - .055


def linear_rgba(value: object) -> tuple[float, float, float, float] | None:
    if not isinstance(value, str) or not value.startswith('linear('):
        return None
    try:
        if not value.endswith(')'):
            raise ValueError
        channels = tuple(float(part) for part in value[7:-1].split(','))
        if (len(channels) != 4 or not all(math.isfinite(c) for c in channels)
                or any(not 0 <= c <= MAX_LINEAR_CHANNEL for c in channels[:3])
                or not 0 <= channels[3] <= 1):
            raise ValueError
    except (ValueError, OverflowError) as exc:
        raise ValueError('线性颜色必须包含有限的 RGB 强度和 0–1 的不透明度') from exc
    return channels


def linear_color(channels) -> str:
    result = 'linear(' + ','.join(repr(float(c) if c else 0.0) for c in channels) + ')'
    linear_rgba(result)
    return result


def rgba_float(value: str) -> tuple[float, float, float, float]:
    """Linear RGB with straight alpha; no quantization or gamut clipping."""
    linear = linear_rgba(value)
    if linear is not None:
        return linear
    digits = value.removeprefix('#')
    if len(digits) == 6:
        digits = 'FF' + digits
    if len(digits) != 8:
        raise ValueError('颜色必须为 RGB/ARGB 色号或显式线性颜色')
    a, r, g, b = (int(digits[i:i+2], 16) / 255 for i in range(0, 8, 2))
    return srgb_to_linear(r), srgb_to_linear(g), srgb_to_linear(b), a


def display_hex(value: str, exposure: float = 0) -> str:
    """SDR sRGB preview. Exposure affects preview only; alpha is never gamma-corrected."""
    r, g, b, a = rgba_float(value)
    gain = 2 ** exposure
    channels = [round(min(1., max(0., linear_to_srgb(c * gain))) * 255) for c in (r, g, b)]
    return '#%02X%02X%02X%02X' % (round(a * 255), *channels)


def linear_hsv(channels, remembered_hue: float = 0, remembered_saturation: float = 0):
    h, s, v = colorsys.rgb_to_hsv(*channels[:3])
    return (h * 360 if s > 0 else remembered_hue,
            s if v > 0 else remembered_saturation, v)


def hsv_linear(hue: float, saturation: float, value: float, alpha: float):
    return (*colorsys.hsv_to_rgb((hue % 360) / 360, saturation, value), alpha)
