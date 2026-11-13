"""The migration reference is GPU-only, explicit and complete as an input inventory."""
import ast
import json
from pathlib import Path

import pytest

from scripts.subtitle_gpu_baseline.cases import build_cases
from scripts.subtitle_gpu_baseline.inventory import contract, inventory, FEATURE_SOURCES, ROOT
from scripts.subtitle_gpu_baseline.model import sequence_times, seek_order
from krok_helper.subtitle_render.native.protocol import gpu_unsupported_features
from scripts.subtitle_gpu_baseline.capture import require_hardware, RecordingRenderer
from scripts.subtitle_gpu_baseline.artifacts import ResourceStore, canonical_bytes, save_frame, pixel_difference


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


@pytest.mark.parametrize("changes", [{"available": False}, {"hardware": False},
    {"warp": True}, {"backend": "qt"}, {"backend": "skia"}])
def test_reference_rejects_software_or_unexpected_backend(changes):
    caps = {"available": True, "hardware": True, "warp": False, "backend": "direct2d"}
    require_hardware(caps)
    with pytest.raises(RuntimeError):
        require_hardware({**caps, **changes})


def test_lossless_reference_preserves_premultiplied_bytes(tmp_path):
    import zlib
    from PyQt6.QtGui import QImage, QColor
    image = QImage(12, 8, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    image.setPixelColor(4, 3, QColor(120, 40, 200, 91))
    result = save_frame(tmp_path, "frame", image)
    assert zlib.decompress((tmp_path / result["raw_file"]).read_bytes()) == canonical_bytes(image)
    assert result["bbox_alpha_gt_16"] == [4, 3, 5, 4]
    assert result["occupied_pixels"] == 1
    assert result["alpha_sum"] == 91


def test_portable_identity_uses_content_not_path_or_file_timestamp(tmp_path):
    stores = []
    for name in ("first", "second"):
        root = tmp_path / name
        root.mkdir()
        source = root / "font.otf"
        source.write_bytes(b"fixed-test-content")
        stores.append(ResourceStore(root).portable_ir({"font": {"resource_path": str(source)},
            "image_modified_ms": 11 if name == "first" else 99, "start_ms": 1234}))
    assert stores[0] == stores[1]
    assert stores[0]["start_ms"] == 1234


def test_recording_expands_production_vector_dedup_without_changing_sent_ir(monkeypatch):
    from krok_helper.subtitle_render.native.backend import NativeRendererProcess
    sent = []
    monkeypatch.setattr(NativeRendererProcess, "_send", lambda self, payload: sent.append(payload))
    renderer = object.__new__(RecordingRenderer)
    renderer.vector_table = None
    renderer._send({"cmd": "configure", "ir": {"vector_glyphs": {"a": {"path": []}}, "track": {}}})
    second = {"cmd": "configure", "ir": {"track": {"lines": []}}}
    renderer._send(second)
    assert "vector_glyphs" not in renderer.sent_ir
    assert renderer.expanded_ir["vector_glyphs"] == {"a": {"path": []}}
    assert sent[-1] == second


def test_difference_reports_local_change_instead_of_diluting_transparent_background():
    expected = bytes(8 * 6 * 4)
    actual = bytearray(expected)
    actual[(2 * 8 + 3) * 4 + 3] = 70
    result = pixel_difference(expected, bytes(actual), 8, 6)
    assert result["changed_pixels"] == 1
    assert result["changed_bbox"] == [3, 2, 4, 3]
    assert result["max_channel_delta"] == 70
    assert result["changed_channel_mae"] == 17.5
