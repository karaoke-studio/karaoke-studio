"""Canonical bounds adapter for the shared upper/lower interference policy."""
from .interference import RubySpacingSpan, resolve_ruby_spacing
from .unit_layout import measured_ruby_origins


def reading_origins(target, metrics, left, width):
    rtl = target.style.right_to_left
    units = metrics.unit_layouts[::-1] if rtl else metrics.unit_layouts
    origins = measured_ruby_origins(units, left, width, style=target.style, base_text=target.effective.kanji)
    return origins[::-1] if rtl else origins


def resolve_line_spacing(main, appearance, targets, readings):
    spans = [RubySpacingSpan(min(t.indices), max(t.indices), t.lower, i) for i, t in enumerate(targets)]

    def bounds(span, left, width, *, ink=False):
        metrics = readings[span.key]
        origins = reading_origins(targets[span.key], metrics, left, width)
        if not origins:
            return left, left
        boxes = ([(o[1]+u[3], o[1]+u[4]) for o, u in zip(origins, metrics.unit_layouts, strict=True) if u[4] > u[3]]
                 if ink else [])
        if not boxes:
            boxes = [(c[1], c[1]+c[2]) for c in origins]
        return min(b[0] for b in boxes), max(b[1] for b in boxes)

    return resolve_ruby_spacing(main.widths, appearance, spans,
        draw_bounds=bounds, lower_ink_bounds=lambda *args: bounds(*args, ink=True))
