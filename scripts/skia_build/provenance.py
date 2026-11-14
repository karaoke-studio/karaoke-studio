"""Keep failed build requests visible and invalidate native headers safely."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def renderer_digest(repo: Path) -> str:
    native = repo / "native/subtitle_renderer"
    paths = [native / "CMakeLists.txt", repo / "scripts/build_skia_renderer.py"]
    paths += list((native / "src/backends").glob("*.h"))
    for directory in (native / "src/skia", native / "src/model", native / "src/effects",
                      native / "src/scene", native / "src/runtime", native / "src/backends/skia",
                      native / "tests", native / "cmake", repo / "scripts/skia_build"):
        paths += [p for p in directory.rglob("*") if p.suffix in {".h", ".cpp", ".mm", ".cmake", ".py"}]
    result = hashlib.sha256()
    # WindowsPath compares case-insensitively; fingerprints must use the same order on every OS.
    for path in sorted(paths, key=lambda p: p.relative_to(repo).as_posix()):
        result.update(path.relative_to(repo).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return result.hexdigest()


def record_request(path: Path, fingerprint: str, *, clean: bool) -> None:
    if path.exists() and json.loads(path.read_text())["fingerprint"] != fingerprint and not clean:
        raise RuntimeError("Dependency/toolchain fingerprint changed; rerun with --clean")
    path.write_text(json.dumps({"fingerprint": fingerprint}) + "\n", encoding="utf-8")


def native_needs_clean(path: Path, source_digest: str) -> bool:
    previous = json.loads(path.read_text()) if path.exists() else {}
    path.write_text(json.dumps({"source_digest": source_digest}) + "\n", encoding="utf-8")
    return previous.get("source_digest") != source_digest
