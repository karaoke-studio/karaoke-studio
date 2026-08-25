"""N3-compatible raster blur primitives for subtitle decorations."""

from __future__ import annotations

import math

import numpy as np
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage

def _n3_gaussian_kernel_1d(standard_deviation: float) -> np.ndarray:
    """Return N3/Direct2D's normalized Gaussian kernel for one axis."""
    sigma = max(float(standard_deviation), 1.0)
    support_radius = math.ceil(sigma * 3.0)
    offsets = np.arange(-support_radius, support_radius + 1, dtype=np.float64)
    kernel = np.exp(-(offsets * offsets) / (2.0 * sigma * sigma))
    return kernel / kernel.sum()


def _blur_image(source: QImage, radius: int) -> QImage:
    """Approximate N3's default Direct2D ``Balanced`` Gaussian blur.

    N3 assigns ``DecorSize`` directly to Direct2D's ``StandardDeviation``.
    In the default ``Balanced`` optimization mode, Direct2D pre-scales the
    input before filtering at larger radii, then restores it with filtered
    sampling.  A half-size pass reproduces the radius-10 response used by N3
    projects within one 8-bit alpha value; smaller radii retain the direct
    Gaussian path.  Qt's QGraphicsBlurEffect cannot be used because it applies
    an unrelated exponential blur.
    """
    sigma = max(float(radius), 1.0)
    if sigma < 8.0 or source.width() < 2 or source.height() < 2:
        return _gaussian_blur_image(source, sigma)

    reduced = source.scaled(
        max(source.width() // 2, 1),
        max(source.height() // 2, 1),
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    blurred = _gaussian_blur_image(reduced, sigma / 2.0)
    return blurred.scaled(
        source.size(),
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


def _gaussian_blur_image(source: QImage, standard_deviation: float) -> QImage:
    """Apply a separable ``3 * sigma`` Gaussian with a transparent border."""
    image = source.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    width = image.width()
    height = image.height()
    if width <= 0 or height <= 0:
        return image

    source_bits = image.constBits()
    source_bits.setsize(image.sizeInBytes())
    source_rows = np.frombuffer(source_bits, dtype=np.uint8).reshape(
        height, image.bytesPerLine()
    )
    pixels = source_rows[:, : width * 4].reshape(height, width, 4).astype(np.float32)

    kernel = _n3_gaussian_kernel_1d(standard_deviation).astype(np.float32)
    support_radius = len(kernel) // 2
    horizontal = np.pad(
        pixels,
        ((0, 0), (support_radius, support_radius), (0, 0)),
        mode="constant",
    )
    horizontal_windows = np.lib.stride_tricks.sliding_window_view(
        horizontal, len(kernel), axis=1
    )
    horizontal_blur = np.einsum(
        "...k,k->...", horizontal_windows, kernel, optimize=True
    )
    vertical = np.pad(
        horizontal_blur,
        ((support_radius, support_radius), (0, 0), (0, 0)),
        mode="constant",
    )
    vertical_windows = np.lib.stride_tricks.sliding_window_view(
        vertical, len(kernel), axis=0
    )
    blurred = np.einsum("...k,k->...", vertical_windows, kernel, optimize=True)
    quantized = np.clip(np.rint(blurred), 0, 255).astype(np.uint8)

    result = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    result.fill(0)
    result_bits = result.bits()
    result_bits.setsize(result.sizeInBytes())
    result_rows = np.frombuffer(result_bits, dtype=np.uint8).reshape(
        height, result.bytesPerLine()
    )
    result_rows[:, : width * 4] = quantized.reshape(height, width * 4)
    return result


# Public contracts for render effects. Keep the original private names above
# for compatibility with existing Painter diagnostics and tests.
blur_image = _blur_image
gaussian_blur_image = _gaussian_blur_image
n3_gaussian_kernel_1d = _n3_gaussian_kernel_1d


__all__ = [
    "blur_image",
    "gaussian_blur_image",
    "n3_gaussian_kernel_1d",
]
