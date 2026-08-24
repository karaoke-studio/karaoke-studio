"""Focused contracts for the N3 project import controller."""

from __future__ import annotations

from pathlib import Path

from krok_helper.subtitle_render.frontend.workflow import import_controller as controller_module
from krok_helper.subtitle_render.frontend.workflow.import_controller import (
    N3ProjectImportController,
)
from krok_helper.subtitle_render.models import Style
from krok_helper.subtitle_render.n3.project_import import N3_PROJECT_FILTER


def test_n3_import_controller_chooses_from_current_project_directory(
    tmp_path: Path,
) -> None:
    current = tmp_path / "current" / "song.yurika"
    selected = tmp_path / "imports" / "song.n3proj"
    calls: list[tuple] = []

    result = N3ProjectImportController.choose_path(
        "parent",
        current_project_path=current,
        choose_file=lambda *args: (calls.append(args), (str(selected), ""))[1],
    )

    assert result == selected
    assert calls == [
        (
            "parent",
            "导入 NicoKaraMaker3 项目",
            str(current.parent),
            N3_PROJECT_FILTER,
        )
    ]


def test_n3_import_controller_preserves_cancel() -> None:
    assert (
        N3ProjectImportController.choose_path(
            None,
            current_project_path=None,
            choose_file=lambda *_args: ("", ""),
        )
        is None
    )


def test_n3_import_controller_delegates_file_loading(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "song.n3proj"
    expected = object()
    calls: list[Path] = []
    monkeypatch.setattr(
        controller_module,
        "load_n3proj",
        lambda value: (calls.append(value), expected)[1],
    )

    assert N3ProjectImportController.load(path) is expected
    assert calls == [path]


def test_n3_import_controller_rebases_only_positive_video_height() -> None:
    style = Style(font_reference_height=1080, layout_reference_height=720)

    rebased = N3ProjectImportController.rebase_style_for_video(style, 2160)

    assert rebased.font_reference_height == 2160
    assert rebased.layout_reference_height == 2160
    assert N3ProjectImportController.rebase_style_for_video(style, 0) is style
