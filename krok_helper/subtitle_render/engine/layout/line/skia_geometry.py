"""Canonical cell placement shared by final IR and collision measurements."""
from dataclasses import dataclass
import math
from .skia_placement import place_horizontal_line
from ...text.cell_positions import char_left_positions


@dataclass(frozen=True)
class LocalLineGeometry:
    cells: tuple
    ink: tuple | None
    layout_width: float


def local_line_geometry(measured, style, *, char_gaps=None):
    cursor = 0
    cells, ink_boxes = [], []
    lefts = None
    if char_gaps is not None:
        if len(char_gaps) != len(measured.cells):
            raise ValueError('Skia 注音间距与正文单元数量不一致')
        lefts = char_left_positions(measured.widths, 0, False, style.letter_spacing_px,
            char_gaps=char_gaps, n3_no_backtracking=style.layout_semantics == 'n3_1074')
    for i, (metrics, width, offset) in enumerate(zip(measured.cells, measured.widths, measured.offsets, strict=True)):
        if lefts is not None:
            cursor = lefts[i]
        x = cursor + offset
        ink = metrics['ink']
        visible = ink[2] > ink[0] and ink[3] > ink[1]
        if visible:
            ink_boxes.append((x + ink[0], ink[1], x + ink[2], ink[3]))
        cells.append({'metrics': metrics, 'run_index': measured.run_indices[i], 'origin_x': x,
                      'wipe_left': x + ink[0] if visible else cursor,
                      'wipe_right': x + ink[2] if visible else cursor + width})
        if width > 0:
            cursor += (max(width + style.letter_spacing_px, 0)
                       if lefts is None and i + 1 < len(measured.cells) else width)
    if style.right_to_left:
        # D2D prepares each unmirrored glyph in logical order, then moves its
        # complete cell box to width-oldRight. Glyph bearings stay unchanged.
        ink_boxes = []
        for index, cell in enumerate(cells):
            left = cell['origin_x'] - measured.offsets[index]
            shift = cursor - (left + measured.widths[index]) - left
            cell.update({key: cell[key] + shift for key in ('origin_x', 'wipe_left', 'wipe_right')})
            bounds = cell['metrics']['ink']
            if bounds[2] > bounds[0] and bounds[3] > bounds[1]:
                ink_boxes.append((cell['origin_x']+bounds[0], bounds[1],
                                  cell['origin_x']+bounds[2], bounds[3]))
    ink = (min(b[0] for b in ink_boxes), min(b[1] for b in ink_boxes),
           max(b[2] for b in ink_boxes), max(b[3] for b in ink_boxes)) if ink_boxes else None
    return LocalLineGeometry(tuple(cells), ink, cursor)


def line_position(item, measured, local, width, height):
    return place_horizontal_line(item, measured.baseline_metrics, width=width, height=height,
        ink_left=local.ink[0] if local.ink else 0,
        ink_right=local.ink[2] if local.ink else max(local.layout_width, 1), layout_width=local.layout_width,
        row_frame=measured.row_frame)


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
