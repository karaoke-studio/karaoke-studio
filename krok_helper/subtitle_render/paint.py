"""Backend-neutral paint and karaoke color domain model."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional


ColorFillMode = Literal[
    "solid",
    "gradient_horizontal",
    "gradient_vertical",
    "split_vertical",
    "image",
]
ColorStateKey = Literal["before", "after"]
ColorLayerKey = Literal["text", "stroke", "stroke2", "shadow"]


@dataclass
class PaintFill:
    """One fill definition shared by text, stroke, second stroke and shadow."""

    mode: ColorFillMode = "solid"
    color: str = "#FFFFFF"
    start_color: str = "#FFFFFF"
    end_color: str = "#FFFFFF"
    gradient_stops: list[tuple[float, str]] = field(default_factory=list)
    split_top_color: str = "#FFFFFF"
    split_bottom_color: str = "#FFFFFF"
    split_position_pct: float = 50
    # Hard-edged vertical color bands: each item marks where that color starts.
    # The final 100% endpoint repeats the last band color for editor/persistence.
    split_stops: list[tuple[float, str]] = field(default_factory=list)
    image_path: str = ""
    image_scale_pct: int = 100


def _paint_fill(
    color: str,
    *,
    mode: ColorFillMode = "solid",
    end: Optional[str] = None,
) -> PaintFill:
    end_color = end or color
    return PaintFill(
        mode=mode,
        color=color,
        start_color=color,
        end_color=end_color,
        gradient_stops=[(0, color), (100, end_color)],
        split_top_color=color,
        split_bottom_color=end_color,
        split_stops=[(0, color), (50, end_color), (100, end_color)],
    )


@dataclass
class KaraokeColorState:
    """Colors for one karaoke state: before singing or after singing."""

    text: PaintFill = field(default_factory=lambda: _paint_fill("#FFFFFF"))
    stroke: PaintFill = field(default_factory=lambda: _paint_fill("#222222"))
    stroke2: PaintFill = field(default_factory=lambda: _paint_fill("#000000"))
    shadow: PaintFill = field(default_factory=lambda: _paint_fill("#000000"))


@dataclass
class KaraokeColors:
    """NicoKara-style color matrix: before/after x visual layers."""

    before: KaraokeColorState = field(
        default_factory=lambda: KaraokeColorState(
            text=_paint_fill("#FFFFFF"),
            stroke=_paint_fill("#222222"),
            stroke2=_paint_fill("#000000"),
            shadow=_paint_fill("#000000"),
        )
    )
    after: KaraokeColorState = field(
        default_factory=lambda: KaraokeColorState(
            text=_paint_fill("#FF5A6F"),
            stroke=_paint_fill("#222222"),
            stroke2=_paint_fill("#000000"),
            shadow=_paint_fill("#000000"),
        )
    )
