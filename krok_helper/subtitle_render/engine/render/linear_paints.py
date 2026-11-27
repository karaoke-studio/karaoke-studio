"""Project explicit linear solid colors onto the existing floating-point paint path."""
from dataclasses import fields, is_dataclass
from copy import deepcopy

from ...domain.color import linear_rgba
from ...domain.paint import PaintFill


def linear_paint_style(style):
    """Keep the authored solid slot; render a constant float gradient without byte conversion.

    This runs before layout/paint feature selection, so every role and reading
    takes the same established painted path as an ordinary gradient. Legacy
    styles are returned unchanged, including their cache identity.
    """
    def children(value):
        if is_dataclass(value):
            return (getattr(value, f.name) for f in fields(value))
        if isinstance(value, dict):
            return value.values()
        if isinstance(value, (list, tuple)):
            return value
        return ()

    def needs(value):
        if isinstance(value, PaintFill) and value.mode == 'solid' and linear_rgba(value.color) is not None:
            return True
        if isinstance(value, dict) and value.get('mode', 'solid') == 'solid' and linear_rgba(value.get('color')) is not None:
            return True
        return any(needs(child) for child in children(value))

    if not needs(style):
        return style
    result = deepcopy(style)

    def prepare(value):
        if isinstance(value, PaintFill) and value.mode == 'solid' and linear_rgba(value.color) is not None:
            value.mode = 'gradient_vertical'
            value.start_color = value.end_color = value.color
            value.gradient_stops = [(0, value.color), (100, value.color)]
        if isinstance(value, dict) and value.get('mode', 'solid') == 'solid' and linear_rgba(value.get('color')) is not None:
            value.update(mode='gradient_vertical', start_color=value['color'], end_color=value['color'],
                         gradient_stops=[(0, value['color']), (100, value['color'])])
        for child in children(value):
            prepare(child)

    prepare(result)
    return result
