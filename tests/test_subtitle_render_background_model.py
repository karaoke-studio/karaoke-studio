from __future__ import annotations

from pathlib import Path

from krok_helper.subtitle_render.background import (
    BackgroundSource,
    background_sequence_frame_path,
    infer_image_sequence_pattern,
)


def test_background_sequence_contract_resolves_frame_number(tmp_path: Path) -> None:
    source = BackgroundSource(
        kind="image_sequence",
        path=str(tmp_path / "frame_%04d.png"),
        source_fps=60,
        sequence_start_number=1,
    )

    assert background_sequence_frame_path(source, 500) == tmp_path / "frame_0031.png"


def test_background_sequence_contract_infers_pattern_and_start(tmp_path: Path) -> None:
    source = tmp_path / "frame_0042.png"

    assert infer_image_sequence_pattern(source) == (
        tmp_path / "frame_%04d.png",
        42,
    )


def test_models_keeps_background_compatibility_exports() -> None:
    from krok_helper.subtitle_render import models

    assert models.BackgroundSource is BackgroundSource
    assert models.Background is BackgroundSource
    assert models.background_sequence_frame_path is background_sequence_frame_path
    assert models.infer_image_sequence_pattern is infer_image_sequence_pattern
