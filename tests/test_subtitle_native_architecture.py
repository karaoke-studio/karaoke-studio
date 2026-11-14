"""Follow native include edges; a forbidden dependency cannot hide in a helper header."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1] / "native/subtitle_renderer"
SRC = ROOT / "src"
INCLUDE = re.compile(r'^\s*#\s*(?:include|import)\s*[<"]([^">]+)', re.M)
# OS, compiler and CPU-architecture branches all belong in adapters, never in shared code.
PLATFORM = re.compile(
    r'^\s*#\s*(?:if|ifdef|ifndef|elif).*\b(?:_WIN32|WIN32|__APPLE__|__linux__|Q_OS_\w+'
    r'|_MSC_VER|__clang__|__GNUC__|_M_X64|_M_ARM64|__x86_64__|__aarch64__|__arm64__|SK_BUILD_FOR_\w+)\b',
    re.M,
)
# wchar_t is 16-bit on Windows and 32-bit elsewhere, so code units mean different things.
WIDE_TEXT = re.compile(r'\bwchar_t\b|\bwstring\b|\bL"')
# Legacy model text fields still use std::wstring until the UTF-8 scene contract lands (P04/P05).
# This ceiling may only go down.
LEGACY_MODEL_WIDE_TEXT = 21


def check_closure(start: Path, *, adapter=False, public_runtime=False, core=False):
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
                if adapter:
                    allowed = ("model/", "scene/", "protocol/")
                elif core:
                    allowed = ("model/", "effects/", "skia/core/")
                else:
                    allowed = ("model/", "effects/", "skia/core/", "skia/raster/")
                extra = {"backends/render_backend.h", "runtime/generation_cancellation.h",
                         "runtime/owner_backend_session.h", "runtime/work_admission.h"} if public_runtime else set()
                assert relative.startswith(allowed) or relative in extra, (path, relative)
                pending.append(target)


def test_shared_scene_effects_and_skia_core_have_no_transitive_platform_or_qt_dependency():
    for directory in ("model", "effects", "skia/core", "skia/raster"):
        for path in (SRC / directory).rglob("*"):
            if path.suffix in {".h", ".cpp"}:
                check_closure(path, core=directory == "skia/core")


def test_shared_numeric_and_drawing_code_uses_no_platform_sized_text():
    for directory in ("effects", "skia/core", "skia/raster"):
        for path in (SRC / directory).rglob("*"):
            if path.suffix in {".h", ".cpp"}:
                assert not WIDE_TEXT.search(path.read_text(encoding="utf-8")), path


def test_legacy_wide_text_in_scene_model_does_not_grow():
    count = sum(len(WIDE_TEXT.findall(path.read_text(encoding="utf-8")))
                for path in (SRC / "model").glob("*.h"))
    assert count <= LEGACY_MODEL_WIDE_TEXT, (
        f"model/ gained platform-sized text ({count} > {LEGACY_MODEL_WIDE_TEXT}); use UTF-8 std::string")


def test_scene_adapter_does_not_import_backend_private_headers():
    for path in (SRC / "scene").glob("*"):
        if path.suffix in {".h", ".cpp"}:
            check_closure(path, adapter=True)


def test_removed_monolithic_type_header_has_no_live_consumers():
    assert not (SRC / "model/render_types.h").exists()
    for path in SRC.rglob("*"):
        if path.suffix in {".h", ".cpp", ".mm"}:
            assert not any(name.endswith("model/render_types.h") for name in INCLUDE.findall(path.read_text(encoding="utf-8"))), path


def test_owner_runtime_uses_only_public_backend_values_and_standard_library():
    for name in ("generation_cancellation.cpp", "owner_backend_session.cpp"):
        check_closure(SRC / "runtime" / name, public_runtime=True)


def test_skia_facade_does_not_depend_on_direct2d_worker_resources_or_qt():
    forbidden = ("Direct2DGpuBackend", "sharedDeviceResources", "RealizationPrewarm", "gpu_preview_worker_pool", "QPainter")
    for path in (SRC / "backends/skia").glob("*"):
        text = path.read_text(encoding="utf-8")
        assert not any(name in text for name in forbidden), path
