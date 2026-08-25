"""Project-resource inspection independent from the subtitle frontend."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from krok_helper.subtitle_render.domain.background import (
    BackgroundSource,
    background_sequence_frame_path,
)
from krok_helper.subtitle_render.project.store import split_project_paths


def find_missing_project_resources(data: dict) -> list[tuple[str, Path]]:
    """Collect unavailable project assets in stable user-facing order."""
    missing: list[tuple[str, Path]] = []
    seen: set[str] = set()

    def add(label: str, path: Optional[Path], *, exists: Optional[bool] = None) -> None:
        if path is None:
            return
        key = str(path)
        if key in seen or (path.is_file() if exists is None else exists):
            return
        seen.add(key)
        missing.append((label, path))

    paths = split_project_paths(data)
    add("主字幕", paths["subtitle_path"])

    background = (
        data.get("background") if isinstance(data.get("background"), dict) else None
    )
    if background is not None:
        kind = str(background.get("kind") or "solid")
        raw_path = str(background.get("path") or "").strip()
        path = Path(raw_path) if raw_path else None
        if kind == "video":
            add("背景视频", path)
        elif kind == "image":
            add("背景图片", path)
        elif kind == "image_sequence" and path is not None:
            try:
                sequence_start = max(
                    int(background.get("sequence_start_number") or 0),
                    0,
                )
            except (TypeError, ValueError):
                sequence_start = 0
            source = BackgroundSource(
                kind="image_sequence",
                path=str(path),
                sequence_start_number=sequence_start,
            )
            first_frame = background_sequence_frame_path(source, 0)
            add(
                "背景图片序列",
                path,
                exists=first_frame is not None and first_frame.is_file(),
            )
    else:
        add("背景视频", paths["video_path"])

    add("独立音频", paths["audio_path"])

    extras = data.get("extra_subtitle_sources")
    if isinstance(extras, list):
        for index, item in enumerate(extras, start=1):
            if not isinstance(item, dict):
                continue
            path_text = str(item.get("path") or "").strip()
            if not path_text:
                continue
            name = str(item.get("name") or "").strip() or str(index)
            add(f"副字幕「{name}」", Path(path_text))
    return missing
