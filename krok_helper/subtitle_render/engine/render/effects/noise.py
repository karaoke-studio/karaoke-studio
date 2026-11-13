"""Deterministic noise dissolve on composed, cached subtitle layers.

The grain removes coverage; narrow downward echoes make short vertical streaks.
Neither part emits particles or changes the text layout. Keep the integer field,
64 coverage steps and echoes in sync with direct2d/d2d_fx.cpp.
"""

from __future__ import annotations

from functools import lru_cache
import threading

import numpy as np

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QBrush, QImage, QPainter, QTransform


NOISE_TILE_SIZE = 128
NOISE_STEPS = 64
NOISE_ECHOES = ((0.05, 0.50), (0.10, 0.30), (0.16, 0.18), (0.23, 0.10))
_LOCAL = threading.local()


def noise_level(entry: str, exit: str, entry_ms: int, exit_ms: int,
                start: int, end: int, line_end: int, t: int) -> int:
    """Coverage in [0, 64]; exit wins overlapping entry/exit windows."""
    if exit == "noise" and exit_ms > 0:
        exit_start = max(line_end, end - min(max(exit_ms, 120), 3000))
        if t >= exit_start:
            duration = max(end - exit_start, 1)
            return max(0, min(NOISE_STEPS, ((end - t) * NOISE_STEPS + duration // 2) // duration))
    if entry == "noise" and entry_ms > 0:
        duration = min(max(entry_ms, 120), 3000)
        return max(0, min(NOISE_STEPS, ((t - start) * NOISE_STEPS + duration // 2) // duration))
    return NOISE_STEPS


def _hash(index: int, salt: int) -> int:
    h = (index * 0x85EBCA77 + salt * 0xC2B2AE3D) & 0xFFFFFFFF
    h ^= h >> 15
    h = (h * 0x2545F491) & 0xFFFFFFFF
    return (h ^ (h >> 13)) & 0xFFFFFF


def noise_rank(x: int, y: int) -> int:
    # Some columns use horizontal cells, matching the sliced patches among
    # the fine grain in the reference. The field stays fixed while revealing.
    if _hash(x // 32, 91) % 4 == 0:
        index = (y // 2) * NOISE_TILE_SIZE + x // 24
    else:
        index = y * NOISE_TILE_SIZE + x
    return _hash(index, 37) % NOISE_STEPS


@lru_cache(maxsize=66)
def noise_mask(level: int, streaks: bool = False) -> QImage:
    ranks, columns = _noise_fields()
    visible = np.broadcast_to(columns, ranks.shape) if streaks else ranks < level
    pixels = np.where(visible, np.uint32(0xFFFFFFFF), np.uint32(0))
    return QImage(pixels.data, NOISE_TILE_SIZE, NOISE_TILE_SIZE,
                  NOISE_TILE_SIZE * 4, QImage.Format.Format_ARGB32_Premultiplied).copy()


@lru_cache(maxsize=1)
def _noise_fields():
    ranks = np.fromiter((noise_rank(x, y) for y in range(NOISE_TILE_SIZE)
                         for x in range(NOISE_TILE_SIZE)), dtype=np.uint8).reshape(NOISE_TILE_SIZE, NOISE_TILE_SIZE)
    columns = np.array([_hash(x, 73) % 8 < 3 for x in range(NOISE_TILE_SIZE)])
    return ranks, columns


def noise_buffers(width: int, height: int, dpr: float) -> tuple[QImage, QImage]:
    key = (width, height, dpr)
    if getattr(_LOCAL, "key", None) != key:
        _LOCAL.key = key
        _LOCAL.images = tuple(QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied) for _ in range(2))
        for image in _LOCAL.images:
            image.setDevicePixelRatio(dpr)
    return _LOCAL.images


def paint_noise_layer(painter: QPainter, draw, level: int, font_px: float, canvas_height: int) -> None:
    if level >= NOISE_STEPS:
        draw(painter)
        return
    if level <= 0:
        return
    device = painter.device()
    dpr = device.devicePixelRatioF()
    source, scratch = noise_buffers(device.width(), device.height(), dpr)
    source.fill(0)
    inner = QPainter(source)
    try:
        inner.setRenderHints(painter.renderHints())
        inner.setTransform(painter.transform())
        draw(inner)
    finally:
        inner.end()
    paint_noise(painter, source, scratch, level, font_px,
                max(int(canvas_height * dpr / 1080 + 0.5), 1))


def paint_noise(painter: QPainter, source: QImage, scratch: QImage,
                level: int, font_px: float, cell_px: int) -> None:
    """Composite device-space bitmaps; source glyph/glow baking stays cached."""
    dpr = source.devicePixelRatio()
    brush_scale = max(cell_px, 1) / dpr
    bits = source.constBits()
    bits.setsize(source.sizeInBytes())
    pixels = np.frombuffer(bits, np.uint32).reshape(source.height(), source.bytesPerLine() // 4)
    rows = np.flatnonzero(np.any(pixels, axis=1))
    if not rows.size:
        return
    columns = np.flatnonzero(np.any(pixels[rows[0]:rows[-1] + 1], axis=0))
    rect = QRectF(float(columns[0]) / dpr, float(rows[0]) / dpr,
                  float(columns[-1] - columns[0] + 1) / dpr,
                  float(rows[-1] - rows[0] + 1) / dpr)

    def mask(image: QImage, texture: QImage) -> None:
        target = QPainter(image)
        try:
            target.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
            brush = QBrush(texture)
            brush.setTransform(QTransform.fromScale(brush_scale, brush_scale))
            target.fillRect(rect, brush)
        finally:
            target.end()

    painter.save()
    try:
        painter.resetTransform()
        # Restrict texture fills and bitmap blits to actual coverage. Scanning
        # the composed alpha costs much less than five full-frame blits and
        # handles ruby, mixed fonts, signals and arbitrary viewport transforms.
        painter.setClipRect(rect.adjusted(0, 0, 0, int(font_px * dpr * 0.23 + 0.5) / dpr),
                            Qt.ClipOperation.IntersectClip)
        if 0 < level < NOISE_STEPS:
            scratch.fill(0)
            target = QPainter(scratch)
            target.setClipRect(rect)
            target.drawImage(QPointF(), source)
            target.end()
            mask(scratch, noise_mask(0, True))
            coverage = level / NOISE_STEPS
            envelope = 4 * coverage * (1 - coverage)
            original_opacity = painter.opacity()
            for distance, alpha in NOISE_ECHOES:
                painter.setOpacity(original_opacity * envelope * alpha)
                # Integer device offsets avoid a CPU/GPU half-pixel discrepancy.
                offset = int(font_px * dpr * distance + 0.5) / dpr
                painter.drawImage(QPointF(0, offset), scratch)
            painter.setOpacity(original_opacity)
        mask(source, noise_mask(level))
        painter.drawImage(QPointF(), source)
    finally:
        painter.restore()
