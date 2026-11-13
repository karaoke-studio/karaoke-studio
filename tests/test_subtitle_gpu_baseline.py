"""The migration reference is GPU-only, explicit and complete as an input inventory."""
from dataclasses import fields
import ast
import json
from pathlib import Path

import pytest

from scripts.subtitle_gpu_baseline.cases import build_cases
from scripts.subtitle_gpu_baseline.inventory import contract, inventory, FEATURE_SOURCES, ROOT
from scripts.subtitle_gpu_baseline.model import sequence_times, seek_order
from krok_helper.subtitle_render.native.protocol import gpu_unsupported_features


@pytest.fixture(scope="module")
def cases(qapp, tmp_path_factory):
    return build_cases(tmp_path_factory.mktemp("subtitle-gpu-corpus"))


def test_model_and_enum_inventory_requires_explicit_review_for_new_fields():
    frozen = json.loads((ROOT / "tests/data/subtitle_gpu_baseline_contract.json").read_text(encoding="utf8"))
    assert contract() == frozen
    assert len(frozen["models"]["Style"]) == 279


def test_cases_cover_every_declared_animation_and_paint_variant(cases):
    identifiers = {case.case_id for case in cases}
    assert len(identifiers) == len(cases)
    variants = contract()["variants"]
    for family, prefix in (("EntryAnimation", "entry_"), ("ExitAnimation", "exit_"),
                           ("KaraokeAnimation", "karaoke_"), ("SingFx", "sing_"),
                           ("ColorFillMode", "paint_")):
        assert {prefix + name for name in variants[family]} <= identifiers
    assert {f"F{i:02d}" for i in range(1, 26)} <= {f for c in cases for f in c.features}
    for case in cases:
        assert case.timestamps_ms and all(t >= 0 for t in case.timestamps_ms)
        assert not gpu_unsupported_features(case.track, case.style, list(case.extra_tracks)), case.case_id


def test_input_sources_have_no_render_or_reference_painter_calls():
    for relative in ("scripts/subtitle_render_corpus.py", "scripts/subtitle_gpu_baseline/cases.py",
                     "scripts/subtitle_gpu_baseline/effects.py"):
        tree = ast.parse((ROOT / relative).read_text(encoding="utf8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module != "krok_helper.subtitle_render.engine.painter"
            if isinstance(node, ast.Name):
                assert node.id not in {"paint_frame", "paint_frame_to_painter", "QPainter"}


def test_inventory_links_product_suites_without_claiming_overlay_coverage(cases):
    result = inventory(cases)
    assert len(result["features"]) == 29
    for number in (26, 27, 28, 29):
        assert result["features"][f"F{number:02d}"]["scope"] == "product_suite"
        assert not result["features"][f"F{number:02d}"]["implemented_by_skia"]
    for paths in FEATURE_SOURCES.values():
        assert all((ROOT / path).is_file() for path in paths)
    assert result["suites"]["export"]["status"].startswith("specified_not_executed")
    assert result["suites"]["long_run"]["duration_seconds"] == 1800
    assert all(set(suite["cases"]) <= {c.case_id for c in cases} for suite in result["suites"].values())


@pytest.mark.parametrize("fps,count", [(60, 360), (120, 720)])
def test_sequence_and_seek_sampling_is_explicit_and_repeatable(fps, count):
    times = sequence_times(start_ms=0, duration_ms=6000, fps=fps)
    assert len(times) == count
    assert times[0] == 0 and times[-1] < 6000
    assert len(set(times)) == count
    assert seek_order(times) == seek_order(times)
    assert set(seek_order(times)) == set(times)
    assert seek_order(times) != times
