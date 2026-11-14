"""Canonical cell placement shared by final IR and collision measurements."""
from dataclasses import dataclass
import math
from .skia_placement import place_horizontal_line


@dataclass(frozen=True)
class LocalLineGeometry:
    cells: tuple
    ink: tuple | None
    layout_width: float


def local_line_geometry(measured, style):
    cursor = 0
    cells, ink_boxes = [], []
    for i, (metrics, width, offset) in enumerate(zip(measured.cells, measured.widths, measured.offsets, strict=True)):
        x = cursor + offset
        ink = metrics['ink']
        visible = ink[2] > ink[0] and ink[3] > ink[1]
        if visible:
            ink_boxes.append((x + ink[0], ink[1], x + ink[2], ink[3]))
        cells.append({'metrics': metrics, 'run_index': measured.run_indices[i], 'origin_x': x,
                      'wipe_left': x + ink[0] if visible else cursor,
                      'wipe_right': x + ink[2] if visible else cursor + width})
        if width > 0:
            cursor += max(width + style.letter_spacing_px, 0) if i + 1 < len(measured.cells) else width
    ink = (min(b[0] for b in ink_boxes), min(b[1] for b in ink_boxes),
           max(b[2] for b in ink_boxes), max(b[3] for b in ink_boxes)) if ink_boxes else None
    return LocalLineGeometry(tuple(cells), ink, cursor)


def line_position(item, measured, local, width, height):
    return place_horizontal_line(item, measured.baseline_metrics, width=width, height=height,
        ink_left=local.ink[0] if local.ink else 0,
        ink_right=local.ink[2] if local.ink else max(local.layout_width, 1), layout_width=local.layout_width)


def positioned_cells(local, position):
    return [{**cell, 'baseline_y': position.baseline_y,
             **{key: cell[key] + position.offset_x for key in ('origin_x', 'wipe_left', 'wipe_right')}}
            for cell in local.cells]


def collision_bounds(local, position):
    if local.ink is None:
        return None
    left, top, right, bottom = local.ink
    # The shared collision policy consumes a conservative integer main-ink box,
    # excluding strokes, ruby, decoration and animation trajectories.
    return (math.floor(left + position.offset_x), math.floor(top + position.baseline_y),
            math.ceil(right + position.offset_x), math.ceil(bottom + position.baseline_y))
