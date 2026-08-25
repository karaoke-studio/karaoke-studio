"""Background-source domain model and image-sequence path semantics."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Literal, Optional


@dataclass
class BackgroundSource:
    """Video, image, image-sequence, or solid subtitle background.

    Image-sequence ``path`` values may point at the first frame or an ffmpeg
    number pattern; ``source_fps`` controls frame selection. ``image_fit``
    applies only to image sources: ``cover`` preserves legacy crop-to-fill
    behavior while ``contain`` letterboxes. Video backgrounds always use the
    product's fixed contain semantics in preview and export.
    """

    kind: Literal["video", "image", "image_sequence", "solid"] = "solid"
    path: Optional[str] = None
    color: str = "#000000"
    source_fps: Optional[int] = None
    sequence_start_number: int = 0
    video_offset_ms: int = 0
    image_fit: Literal["cover", "contain"] = "cover"


def background_sequence_frame_path(
    source: BackgroundSource,
    t_ms: int,
) -> Optional[Path]:
    """Resolve the current image-sequence frame from an ffmpeg number pattern."""
    if source.kind != "image_sequence" or not source.path:
        return None
    index = (
        max(int(t_ms), 0) * max(int(source.source_fps or 60), 1) // 1000
        + max(int(source.sequence_start_number), 0)
    )
    raw = str(source.path)
    match = re.search(r"%0?(\d*)d", raw)
    if match:
        width = int(match.group(1) or 0)
        number = f"{index:0{width}d}" if width else str(index)
        return Path(raw[: match.start()] + number + raw[match.end() :])
    return Path(raw)


def infer_image_sequence_pattern(path: Path) -> tuple[Path, int]:
    """Convert ``frame_0001.png`` to an ffmpeg pattern and start number."""
    match = re.search(r"(\d+)$", path.stem)
    if not match:
        return path, 0
    start = int(match.group(1))
    pattern = path.with_name(
        path.stem[: match.start()] + f"%0{len(match.group(1))}d" + path.suffix
    )
    return pattern, start


# Historical public alias retained by ``models`` and external callers.
Background = BackgroundSource
