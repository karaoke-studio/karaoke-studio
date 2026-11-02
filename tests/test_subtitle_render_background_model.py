from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import pytest

from krok_helper.subtitle_render.domain.background import (
    IMAGE_SEQUENCE_FPS_PRESETS,
    BackgroundSource,
    background_sequence_frame_index,
    background_sequence_frame_path,
    infer_image_sequence_pattern,
    normalize_sequence_fps,
)


def test_background_sequence_contract_resolves_frame_number(tmp_path: Path) -> None:
    source = BackgroundSource(
        kind="image_sequence",
        path=str(tmp_path / "frame_%04d.png"),
        source_fps=60,
        sequence_start_number=1,
    )

    assert background_sequence_frame_path(source, 500) == tmp_path / "frame_0031.png"


@pytest.mark.parametrize(
    ("fps", "t_ms", "expected_index"),
    [
        (0.5, 1999, 0),
        (0.5, 2000, 1),
        (float(Fraction(1, 30)), 29999, 0),
        (float(Fraction(1, 30)), 30000, 1),
        (float(Fraction(1, 30)), 89999, 2),
        (23.976, 125000, 2997),
        (29.97, 10000, 299),
        (60, 500, 30),
    ],
)
def test_background_sequence_frame_index_is_exact_for_fractional_fps(
    fps: float, t_ms: int, expected_index: int
) -> None:
    source = BackgroundSource(kind="image_sequence", path="f_%04d.png", source_fps=fps)

    assert background_sequence_frame_index(source, t_ms) == expected_index


def test_background_sequence_frame_path_keeps_start_number_for_slow_fps(
    tmp_path: Path,
) -> None:
    source = BackgroundSource(
        kind="image_sequence",
        path=str(tmp_path / "frame_%04d.png"),
        source_fps=0.5,
        sequence_start_number=1,
    )

    assert background_sequence_frame_path(source, 1999) == tmp_path / "frame_0001.png"
    assert background_sequence_frame_path(source, 2000) == tmp_path / "frame_0002.png"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (24, 24),
        (24.0, 24),
        (0.5, 0.5),
        ("0.5", 0.5),
        ("23.976", 23.976),
        (59.94, 59.94),
        (float(Fraction(1, 30)), float(Fraction(1, 30))),
        (-1, 60),
        (0, 60),
        (None, 60),
        ("invalid", 60),
        (float("nan"), 60),
        (float("inf"), 60),
    ],
)
def test_normalize_sequence_fps(value: object, expected: float) -> None:
    assert normalize_sequence_fps(value) == expected


def test_normalize_sequence_fps_honors_custom_fallback() -> None:
    assert normalize_sequence_fps(None, fallback=30) == 30
    assert normalize_sequence_fps("bad", fallback=29.97) == 29.97


def test_normalize_sequence_fps_returns_int_for_whole_values() -> None:
    normalized = normalize_sequence_fps("24")

    assert normalized == 24
    assert isinstance(normalized, int)
    assert isinstance(normalize_sequence_fps(29.97), float)


def test_sequence_fps_presets_cover_slow_and_ntsc_rates() -> None:
    assert 0.5 in IMAGE_SEQUENCE_FPS_PRESETS
    assert 23.976 in IMAGE_SEQUENCE_FPS_PRESETS
    assert 29.97 in IMAGE_SEQUENCE_FPS_PRESETS
    assert 59.94 in IMAGE_SEQUENCE_FPS_PRESETS
    assert 60 in IMAGE_SEQUENCE_FPS_PRESETS
    assert 120 in IMAGE_SEQUENCE_FPS_PRESETS
    assert all(normalize_sequence_fps(fps) == fps for fps in IMAGE_SEQUENCE_FPS_PRESETS)


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
