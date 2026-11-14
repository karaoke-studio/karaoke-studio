"""Follow native include edges; a forbidden dependency cannot hide in a helper header."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1] / "native/subtitle_renderer"
SRC = ROOT / "src"
INCLUDE = re.compile(r'^\s*#\s*(?:include|import)\s*[<"]([^">]+)', re.M)
PLATFORM = re.compile(r'^\s*#\s*(?:if|ifdef|ifndef|elif).*\b(?:_WIN32|WIN32|__APPLE__|__linux__|Q_OS_\w+)\b', re.M)


def check_closure(start: Path, *, adapter=False):
    pending = [start]
    seen = set()
    while pending:
        path = pending.pop().resolve()
        if path in seen:
            continue
        seen.add(path)
        text = path.read_text(encoding="utf-8")
        if not adapter:
            assert not PLATFORM.search(text), path
        for name in INCLUDE.findall(text):
            assert not re.match(r"(?:windows\.h|d2d|d3d|vulkan/|Metal/|Foundation/)", name, re.I), (path, name)
            if not adapter:
                assert not re.match(r"(?:Q[A-Z]|Qt|include/gpu/|gpu/|protocol/|runtime/|preview/)", name), (path, name)
            candidates = [path.parent / name, SRC / name, SRC / "skia" / name]
            target = next((p.resolve() for p in candidates if p.is_file()), None)
            if target is not None and target.is_relative_to(SRC.resolve()):
                relative = target.relative_to(SRC.resolve()).as_posix()
                allowed = ("model/", "scene/", "protocol/") if adapter else ("model/", "effects/", "skia/core/", "skia/raster/")
                assert relative.startswith(allowed), (path, relative)
                pending.append(target)


def test_shared_scene_effects_and_skia_core_have_no_transitive_platform_or_qt_dependency():
    for directory in ("model", "effects", "skia/core", "skia/raster"):
        for path in (SRC / directory).rglob("*"):
            if path.suffix in {".h", ".cpp"}:
                check_closure(path)


def test_scene_adapter_does_not_import_backend_private_headers():
    for path in (SRC / "scene").glob("*"):
        if path.suffix in {".h", ".cpp"}:
            check_closure(path, adapter=True)


def test_removed_monolithic_type_header_has_no_live_consumers():
    assert not (SRC / "model/render_types.h").exists()
    for path in SRC.rglob("*"):
        if path.suffix in {".h", ".cpp", ".mm"}:
            assert not any(name.endswith("model/render_types.h") for name in INCLUDE.findall(path.read_text(encoding="utf-8"))), path
