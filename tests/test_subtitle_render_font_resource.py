"""Physical face transfer, including fonts unavailable in the system catalog."""
from pathlib import Path
import json
import os
import subprocess
import sys

import pytest
from PyQt6.QtGui import QFont, QFontDatabase, QRawFont

from krok_helper.subtitle_render.engine.text.font_resource import snapshot_raw_font


@pytest.fixture(scope="module")
def localized_resource_probe():
    """Use the Windows catalog: offscreen cannot reproduce localized aliases."""
    if os.name != "nt":
        pytest.skip("Windows font resources")
    root = Path(__file__).resolve().parent.parent
    script = r'''
import json, sys
from pathlib import Path
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QRawFont
app = QApplication([])
from krok_helper.subtitle_render.engine.text.font_weight import (
    build_weight_font, resolve_font_instance,
)
from krok_helper.subtitle_render.engine.text.font_capabilities import get_capabilities
from krok_helper.subtitle_render.engine.text.font_fallback import (
    prewarm_fallback, lookup_fallback_instance, clear_font_caches,
)
from krok_helper.subtitle_render.native.protocol import apply_resolved_font_faces
samples = [
    ("UD デジタル 教科書体 NP-B", "UD Digi Kyokasho NP-B", "例え運命が真逆にABC0123456789"),
    ("UD デジタル 教科書体 N-B", "UD Digi Kyokasho N-B", "さだめまぎゃくABC0123456789"),
    ("メイリオ", "Meiryo", "重ね合う二つの輪廻ABC0123456789"),
    ("맑은 고딕", "Malgun Gothic", "한글노래ABC0123456789"),
    ("Malgun Gothic", "Malgun Gothic", "한글노래ABC0123456789"),
    ("Arial", "Arial", "English0123456789"),
    ("Comic Sans MS", "Comic Sans MS", "English0123456789"),
]
results = []
for alias, canonical, text in samples:
    cap = get_capabilities(canonical)
    if cap is None or not cap.faces:
        results.append({"alias": alias, "unavailable": True})
        continue
    for weight in (400, 700):
        selected = resolve_font_instance(alias, weight)
        reference = resolve_font_instance(canonical, weight)
        raw = QRawFont(selected.resource_path, 64)
        painter_raw = QRawFont.fromFont(build_weight_font(alias, 64, weight))
        expected_raw = QRawFont.fromFont(build_weight_font(canonical, 64, weight))
        results.append({
            "alias": alias, "weight": weight, "actual": raw.familyName(),
            "same_resource": selected.resource_path == reference.resource_path,
            "covered": all(raw.glyphIndexesForString(text)),
            "painter_same_face": bytes(painter_raw.fontTable("cmap")) == bytes(expected_raw.fontTable("cmap")),
        })
missing = resolve_font_instance("LinK Nonexistent Font 8b17c9", 700)
payload = {"font_family": missing.family, "font_weight": 700}
apply_resolved_font_faces(payload)
prewarm_fallback("Arial", 400, False, ["한"])
fallback = lookup_fallback_instance("Arial", 400, False, "한")
fallback_covered = bool(fallback and all(QRawFont(fallback.resource_path, 64).glyphIndexesForString("한")))
shared_fallback = []
for texts in (["한", "한A", "한한"], ["한한", "한A", "한"]):
    clear_font_caches()
    count = prewarm_fallback("Arial", 400, False, texts)
    instances = [lookup_fallback_instance("Arial", 400, False, text) for text in texts]
    shared_fallback.append({
        "count": count,
        "covered": all(instance and all(QRawFont(instance.resource_path, 64)
                       .glyphIndexesForString(text)) for instance, text in zip(instances, texts)),
    })

# Main, Latin/digit and ruby slots must render identically for both spellings.
sys.path.insert(0, str(Path.cwd() / "tests"))
from test_subtitle_render_gpu_backend import _render_g1_frames, _g1_style
from krok_helper.subtitle_render.native.backend import NativeRendererProcess, resolve_native_renderer_path
from krok_helper.subtitle_render.domain.models import TimingTrack, TimingLine, TimingChar, RubyAnnotation
gpu = []
fallback_gpu = []
if resolve_native_renderer_path() is not None:
    for alias, canonical, text in (samples[0], samples[3], samples[5]):
        if not get_capabilities(canonical):
            continue
        text = text[:8] + "A09"
        track = TimingTrack(
            lines=[TimingLine(chars=[TimingChar(c, i * 100) for i, c in enumerate(text)], end_ms=3000)],
            rubies=[RubyAnnotation(kanji=text[0], reading=text[1:3], reading_part_ms=[],
                                  pos_start_ms=0, pos_end_ms=100, target_line_index=0,
                                  target_char_start=0, target_char_end=1)],
        )
        frames = []
        for family in (alias, canonical):
            style = _g1_style(font_family=family, font_family_latin=family,
                              ruby_font_family=family, ruby_font_family_latin=family,
                              font_size_px=32, ruby_font_size_px=16)
            with NativeRendererProcess(response_timeout_s=60) as renderer:
                _, rendered = _render_g1_frames(renderer, style, (500, 1500),
                                                force_warp=True, track=track)
                frames.append(rendered)
        gpu.append({"alias": alias, "equal": frames[0] == frames[1],
                    "nonempty": any(frames[0][0][3::4])})
    # Supply an explicit fallback face, as the protocol does after Qt resolves
    # a missing glyph. Its glyph IDs must never be measured on Tahoma.
    import krok_helper.subtitle_render.native.protocol as protocol
    original_fallbacks = protocol._line_char_fallbacks
    try:
        for family, text in (("UD Digi Kyokasho NP-B", "例え運命が真逆に"),
                             ("Malgun Gothic", "한글노래")):
            if not get_capabilities(family):
                continue
            protocol._line_char_fallbacks = lambda style, line: [
                {"family": family, "hint": {}} for ch in line.chars]
            track = TimingTrack(lines=[TimingLine(
                chars=[TimingChar(c, i * 100) for i, c in enumerate(text)], end_ms=5000)])
            frames = []
            for requested in ("Tahoma", family):
                style = _g1_style(font_family=requested, font_family_latin=requested,
                    font_size_px=40, stroke_width_px=0, stroke2_enabled=False,
                    decoration_kind="none", custom_style_schemes={})
                with NativeRendererProcess(response_timeout_s=60) as renderer:
                    _, rendered = _render_g1_frames(renderer, style, (2000,),
                        force_warp=True, track=track, width=1920)
                    frames.append(rendered[0])
            # Baseline metrics may differ vertically by requested font; compare
            # occupied columns to check advances and horizontal clipping.
            columns = [sorted({i % 1920 for i, a in enumerate(frame[3::4]) if a > 128})
                       for frame in frames]
            fallback_gpu.append({"family": family, "equal": columns[0] == columns[1],
                                 "nonempty": bool(columns[0])})
    finally:
        protocol._line_char_fallbacks = original_fallbacks
print(json.dumps({"samples": results, "missing_resource": missing.resource_path,
                  "missing_hint": "font_resolved" in payload,
                  "fallback_covered": fallback_covered, "gpu": gpu,
                  "shared_fallback": shared_fallback, "fallback_gpu": fallback_gpu}))
'''
    env = dict(os.environ, QT_QPA_PLATFORM="windows", PYTHONPATH=str(root),
               PYTHONIOENCODING="utf-8")
    completed = subprocess.run([sys.executable, "-c", script], cwd=root, env=env,
                               capture_output=True, timeout=180)
    stdout = completed.stdout.decode("utf-8", errors="replace")
    assert completed.returncode == 0, stdout + completed.stderr.decode("utf-8", errors="replace")
    return json.loads(stdout.strip().splitlines()[-1])


def test_localized_font_resources_preserve_selected_face_and_script(localized_resource_probe):
    rows = [row for row in localized_resource_probe["samples"] if not row.get("unavailable")]
    assert rows
    for row in rows:
        assert row["same_resource"], row
        assert row["covered"], row
        assert row["painter_same_face"], row


def test_missing_font_does_not_pin_unrelated_default_resource(localized_resource_probe):
    assert not localized_resource_probe["missing_resource"]
    assert not localized_resource_probe["missing_hint"]


def test_korean_fallback_resource_covers_selected_character(localized_resource_probe):
    assert localized_resource_probe["fallback_covered"]


def test_shared_missing_cluster_populates_every_text_resource(localized_resource_probe):
    for row in localized_resource_probe["shared_fallback"]:
        assert row["count"] == 3
        assert row["covered"]


def test_gpu_fallback_advances_match_actual_face(localized_resource_probe):
    if not localized_resource_probe["fallback_gpu"]:
        pytest.skip("sidecar or fonts unavailable")
    for row in localized_resource_probe["fallback_gpu"]:
        assert row["nonempty"], row
        assert row["equal"], row


def test_localized_font_gpu_main_latin_and_ruby_match_canonical(localized_resource_probe):
    if not localized_resource_probe["gpu"]:
        pytest.skip("sidecar unavailable")
    for row in localized_resource_probe["gpu"]:
        assert row["equal"], row
        assert row["nonempty"], row


@pytest.mark.skipif(os.name != "nt", reason="Windows font resources")
@pytest.mark.parametrize("filename", ["arial.ttf", "meiryo.ttc", "bahnschrift.ttf"])
def test_snapshot_preserves_face_tables(qapp, filename):
    path = Path(os.environ["WINDIR"]) / "Fonts" / filename
    if not path.is_file():
        pytest.skip("font unavailable")
    raw = QRawFont(str(path), 64)
    if not raw.isValid():
        pytest.skip("Qt cannot load font directly")
    snapshot = snapshot_raw_font(raw, ("roundtrip", filename))
    restored = QRawFont(snapshot, 64)
    assert restored.isValid()
    for tag in ("cmap", "glyf", "loca", "CFF ", "fvar", "gvar", "hmtx"):
        assert bytes(restored.fontTable(tag)) == bytes(raw.fontTable(tag)), tag
    assert list(restored.glyphIndexesForString("Ag")) == list(raw.glyphIndexesForString("Ag"))


@pytest.mark.skipif(os.name != "nt", reason="Windows font resources")
def test_application_only_font_is_transferred_to_gpu(qapp, tmp_path):
    from fontTools.ttLib import TTFont
    from krok_helper.subtitle_render.engine.text.font_weight import clear_font_weight_cache
    from krok_helper.subtitle_render.native.backend import NativeRendererProcess, resolve_native_renderer_path
    from krok_helper.subtitle_render.domain.models import Style, TimingTrack, TimingLine, TimingChar
    original = Path(os.environ["WINDIR"]) / "Fonts" / "arial.ttf"
    family = "LinK Application Resource Probe"
    font = TTFont(original)
    for entry in font["name"].names:
        if entry.nameID in (1, 4, 6, 16, 21):
            value = family.replace(" ", "") if entry.nameID == 6 else family
            entry.string = value.encode(entry.getEncoding())
    destination = tmp_path / "application-only.ttf"
    font.save(destination)
    font.close()
    font_id = QFontDatabase.addApplicationFont(str(destination))
    assert font_id >= 0
    clear_font_weight_cache()
    renderer_path = resolve_native_renderer_path()
    if renderer_path is None:
        pytest.skip("sidecar unavailable")
    try:
        track = TimingTrack(lines=[TimingLine(chars=[TimingChar("W", 0)], end_ms=1000)])
        from test_subtitle_render_gpu_backend import _render_g1_frames, _g1_style
        frames = []
        for selected in ("Arial", family):
            with NativeRendererProcess(renderer_path, response_timeout_s=60) as renderer:
                _, rendered = _render_g1_frames(renderer,
                    _g1_style(font_family=selected, font_family_latin=selected),
                    (500,), force_warp=True, track=track)
                frames.append(rendered[0])
        # The renamed face has identical outlines. System substitution would
        # draw a different W; matching whole frames proves actual face transfer.
        assert frames[0] == frames[1]
    finally:
        QFontDatabase.removeApplicationFont(font_id)
        clear_font_weight_cache()
