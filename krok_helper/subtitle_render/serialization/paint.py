"""Persistence codec for backend-neutral paint domain values."""

from __future__ import annotations

import math
from typing import Optional

from krok_helper.subtitle_render.domain.paint import (
    KaraokeColors,
    KaraokeColorState,
    PaintFill,
    _paint_fill,
)


def karaoke_colors_to_dict(colors: KaraokeColors) -> dict:
    return {
        "before": karaoke_color_state_to_dict(colors.before),
        "after": karaoke_color_state_to_dict(colors.after),
    }


def karaoke_colors_from_dict(payload: object) -> Optional[KaraokeColors]:
    if not isinstance(payload, dict):
        return None
    return KaraokeColors(
        before=karaoke_color_state_from_dict(payload.get("before")),
        after=karaoke_color_state_from_dict(payload.get("after")),
    )


def karaoke_color_state_to_dict(state: KaraokeColorState) -> dict:
    return {
        "text": paint_fill_to_dict(state.text),
        "stroke": paint_fill_to_dict(state.stroke),
        "stroke2": paint_fill_to_dict(state.stroke2),
        "shadow": paint_fill_to_dict(state.shadow),
    }


def karaoke_color_state_from_dict(payload: object) -> KaraokeColorState:
    if not isinstance(payload, dict):
        return KaraokeColorState()
    return KaraokeColorState(
        text=paint_fill_from_dict(payload.get("text")),
        stroke=paint_fill_from_dict(payload.get("stroke"), fallback="#222222"),
        stroke2=paint_fill_from_dict(payload.get("stroke2"), fallback="#000000"),
        shadow=paint_fill_from_dict(payload.get("shadow"), fallback="#000000"),
    )


def paint_fill_to_dict(fill: PaintFill) -> dict:
    return {
        "mode": fill.mode,
        "color": fill.color,
        "start_color": fill.start_color,
        "end_color": fill.end_color,
        "gradient_stops": list(fill.gradient_stops),
        "split_top_color": fill.split_top_color,
        "split_bottom_color": fill.split_bottom_color,
        "split_position_pct": fill.split_position_pct,
        "split_stops": list(fill.split_stops),
        "image_path": fill.image_path,
        "image_scale_pct": fill.image_scale_pct,
    }


def paint_fill_from_dict(payload: object, *, fallback: str = "#FFFFFF") -> PaintFill:
    if not isinstance(payload, dict):
        return _paint_fill(fallback)
    default = _paint_fill(fallback)
    mode = str(payload.get("mode", default.mode))
    if mode not in {
        "solid",
        "gradient_horizontal",
        "gradient_vertical",
        "split_vertical",
        "image",
    }:
        mode = default.mode
    color = str(payload.get("color", default.color))
    start_color = str(payload.get("start_color", color))
    end_color = str(payload.get("end_color", color))
    stops = payload.get("gradient_stops", [(0, start_color), (100, end_color)])
    split_top_color = str(payload.get("split_top_color", start_color))
    split_bottom_color = str(payload.get("split_bottom_color", end_color))
    split_position_pct = _gradient_stop_position(
        payload.get("split_position_pct"), 50
    )
    split_stops_payload = payload.get("split_stops")
    if not split_stops_payload:
        split_stops_payload = [
            (0, split_top_color),
            (split_position_pct, split_bottom_color),
            (100, split_bottom_color),
        ]
    return PaintFill(
        mode=mode,  # type: ignore[arg-type]
        color=color,
        start_color=start_color,
        end_color=end_color,
        gradient_stops=_gradient_stops_from_payload(stops, start_color, end_color),
        split_top_color=split_top_color,
        split_bottom_color=split_bottom_color,
        split_position_pct=split_position_pct,
        split_stops=_gradient_stops_from_payload(
            split_stops_payload, split_top_color, split_bottom_color
        ),
        image_path=str(payload.get("image_path", "")),
        image_scale_pct=max(
            1, min(_int_value(payload.get("image_scale_pct"), 100), 1000)
        ),
    )


def _gradient_stops_from_payload(
    payload: object,
    start_color: str,
    end_color: str,
) -> list[tuple[float, str]]:
    if not isinstance(payload, list):
        return [(0, start_color), (100, end_color)]
    result: list[tuple[float, str]] = []
    for item in payload:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        result.append((_gradient_stop_position(item[0], 0), str(item[1])))
    if not result:
        return [(0, start_color), (100, end_color)]
    positions = {position for position, _color in result}
    if 0 not in positions:
        result.append((0, start_color))
    if 100 not in positions:
        result.append((100, end_color))
    # Stable sorting preserves source order for equal-position color bands.
    return sorted(result, key=lambda item: item[0])


def _gradient_stop_position(value: object, fallback: float) -> float:
    try:
        position = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        position = fallback
    if not math.isfinite(position):
        position = fallback
    position = max(0.0, min(100.0, position))
    # Keep old integer projects byte-stable while retaining imported fractions.
    return int(position) if position.is_integer() else position


def _int_value(value: object, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback
