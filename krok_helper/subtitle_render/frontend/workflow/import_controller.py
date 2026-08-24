"""Frontend command boundary for NicoKaraMaker3 project imports."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Optional

from krok_helper.subtitle_render.models import Style
from krok_helper.subtitle_render.n3.project_import import (
    N3ImportResult,
    N3_PROJECT_FILTER,
    load_n3proj,
)


FilePicker = Callable[..., tuple[str, str]]


class N3ProjectImportController:
    """Own N3 file selection, loading, and post-load reference-height policy."""

    @staticmethod
    def choose_path(
        parent: Any,
        *,
        current_project_path: Optional[Path],
        choose_file: FilePicker,
    ) -> Optional[Path]:
        start_dir = (
            str(current_project_path.parent) if current_project_path is not None else ""
        )
        path_text, _selected_filter = choose_file(
            parent,
            "导入 NicoKaraMaker3 项目",
            start_dir,
            N3_PROJECT_FILTER,
        )
        return Path(path_text) if path_text else None

    @staticmethod
    def load(path: Path) -> N3ImportResult:
        return load_n3proj(Path(path))

    @staticmethod
    def rebase_style_for_video(style: Style, video_height: int) -> Style:
        """Bind absolute N3 pixel values to the imported video's canvas height."""
        height = int(video_height or 0)
        if height <= 0:
            return style
        return replace(
            style,
            font_reference_height=height,
            layout_reference_height=height,
        )
