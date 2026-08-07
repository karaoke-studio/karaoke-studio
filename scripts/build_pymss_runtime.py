"""Package a portable PyMSS runtime and emit a signed-by-hash manifest.

The ZIP byte stream is split while it is written, so CUDA builds do not need
another full archive-sized temporary file and every GitHub Release asset stays
below the 2 GiB per-file limit.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import runpy
import shutil
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_INTEGRATION = runpy.run_path(
    str(
        Path(__file__).resolve().parents[1]
        / "krok_helper"
        / "audio_processing"
        / "separation"
        / "integration.py"
    )
)
PYMSS_PYTHON_VERSION = _INTEGRATION["PYMSS_PYTHON_VERSION"]
PYMSS_EMBEDDED_PYTHON_VERSION = _INTEGRATION["PYMSS_EMBEDDED_PYTHON_VERSION"]
PYMSS_EMBEDDED_PYTHON_SHA256 = _INTEGRATION["PYMSS_EMBEDDED_PYTHON_SHA256"]
PYMSS_CORE_VERSION = _INTEGRATION["PYMSS_CORE_VERSION"]
PYMSS_RUNTIME_VERSION = _INTEGRATION["PYMSS_RUNTIME_VERSION"]
PYMSS_TORCH_VERSION = _INTEGRATION["PYMSS_TORCH_VERSION"]
PYMSS_VERSION = _INTEGRATION["PYMSS_VERSION"]
RUNTIME_ASSET_PREFIX = _INTEGRATION["RUNTIME_ASSET_PREFIX"]
RUNTIME_RELEASE_BASE = _INTEGRATION["RUNTIME_RELEASE_BASE"]
TORCH_WHEELS = _INTEGRATION["TORCH_WHEELS"]

DEFAULT_PART_SIZE = 1_900 * 1024 * 1024
CHUNK_SIZE = 4 * 1024 * 1024


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class PartRecord:
    path: Path
    size: int
    sha256: str


class SplitArchiveWriter(io.RawIOBase):
    """Non-seekable ZIP sink that rotates raw archive parts by byte count."""

    def __init__(self, output_dir: Path, base_name: str, part_size: int) -> None:
        super().__init__()
        if part_size <= 0:
            raise ValueError("part_size must be positive")
        self.output_dir = output_dir
        self.base_name = base_name
        self.part_size = part_size
        self.position = 0
        self._index = 0
        self._stream = None
        self._part_size = 0
        self._part_digest = hashlib.sha256()
        self._archive_digest = hashlib.sha256()
        self._parts: list[PartRecord] = []
        self._open_next()

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def tell(self) -> int:
        return self.position

    def seek(self, *_args):
        raise io.UnsupportedOperation("split archive is not seekable")

    def _part_path(self, index: int) -> Path:
        return self.output_dir / f"{self.base_name}.zip.{index:03d}"

    def _open_next(self) -> None:
        self._index += 1
        self._stream = self._part_path(self._index).open("wb")
        self._part_size = 0
        self._part_digest = hashlib.sha256()

    def _finish_current(self) -> None:
        if self._stream is None:
            return
        path = self._part_path(self._index)
        self._stream.flush()
        os.fsync(self._stream.fileno())
        self._stream.close()
        self._stream = None
        if self._part_size:
            self._parts.append(
                PartRecord(path, self._part_size, self._part_digest.hexdigest())
            )
        else:
            path.unlink(missing_ok=True)

    def write(self, data) -> int:
        view = memoryview(data)
        total = len(view)
        offset = 0
        while offset < total:
            if self._part_size >= self.part_size:
                self._finish_current()
                self._open_next()
            count = min(total - offset, self.part_size - self._part_size)
            chunk = view[offset : offset + count]
            assert self._stream is not None
            self._stream.write(chunk)
            self._part_digest.update(chunk)
            self._archive_digest.update(chunk)
            self._part_size += count
            self.position += count
            offset += count
        return total

    def flush(self) -> None:
        if self._stream is not None:
            self._stream.flush()

    def finish(self) -> tuple[tuple[PartRecord, ...], int, str]:
        self._finish_current()
        return tuple(self._parts), self.position, self._archive_digest.hexdigest()


def _runtime_files(runtime_dir: Path) -> list[Path]:
    files = []
    for path in runtime_dir.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(runtime_dir)
        if "__pycache__" in relative.parts or path.suffix.lower() in {".pyc", ".pyo"}:
            continue
        files.append(path)
    return sorted(files, key=lambda item: item.relative_to(runtime_dir).as_posix().lower())


def package_runtime(
    runtime_dir: Path,
    output_dir: Path,
    *,
    variant: str,
    release_base: str = RUNTIME_RELEASE_BASE,
    part_size: int = DEFAULT_PART_SIZE,
) -> Path:
    runtime_dir = runtime_dir.resolve()
    if not (runtime_dir / "python.exe").is_file():
        raise FileNotFoundError(f"portable runtime is missing python.exe: {runtime_dir}")
    site_packages = runtime_dir / "Lib" / "site-packages"
    forbidden = []
    if site_packages.is_dir():
        for child in site_packages.iterdir():
            name = child.name.lower()
            if name in {"torch", "functorch", "torchgen"} or name.startswith("torch-"):
                forbidden.append(child.name)
    if forbidden:
        raise ValueError(
            "published PyMSS base runtime must not contain torch: "
            + ", ".join(sorted(forbidden))
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    base_name = (
        f"{RUNTIME_ASSET_PREFIX}-{variant}-v{PYMSS_VERSION}-r{PYMSS_RUNTIME_VERSION}"
    )
    for stale in output_dir.glob(f"{base_name}.zip.*"):
        stale.unlink()
    manifest_path = output_dir / f"{base_name}.json"
    manifest_path.unlink(missing_ok=True)

    files = _runtime_files(runtime_dir)
    file_records = []
    for path in files:
        relative = Path("runtime") / path.relative_to(runtime_dir)
        file_records.append(
            {
                "path": relative.as_posix(),
                "size": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )

    writer = SplitArchiveWriter(output_dir, base_name, part_size)
    try:
        with zipfile.ZipFile(
            writer,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=True,
        ) as bundle:
            for path in files:
                archive_name = (Path("runtime") / path.relative_to(runtime_dir)).as_posix()
                info = zipfile.ZipInfo(archive_name, date_time=(2026, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                with path.open("rb") as source, bundle.open(info, "w", force_zip64=True) as target:
                    shutil.copyfileobj(source, target, CHUNK_SIZE)
    finally:
        parts, archive_size, archive_sha256 = writer.finish()
    if not parts:
        raise RuntimeError("runtime archive is empty")

    release_base = release_base.rstrip("/")
    manifest = {
        "schema": 1,
        "runtime_version": PYMSS_RUNTIME_VERSION,
        "pymss_version": PYMSS_VERSION,
        "python_version": PYMSS_PYTHON_VERSION,
        "variant": variant,
        "archive": {
            "size": archive_size,
            "sha256": archive_sha256,
            "parts": [
                {
                    "url": f"{release_base}/{part.path.name}",
                    "size": part.size,
                    "sha256": part.sha256,
                }
                for part in parts
            ],
        },
        "torch": {
            "version": PYMSS_TORCH_VERSION,
            "wheel": dict(TORCH_WHEELS[variant]),
        },
        "files": file_records,
    }
    temporary = manifest_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, manifest_path)
    return manifest_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--print-contract", action="store_true")
    parser.add_argument("--runtime-dir", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--variant", choices=("windows-cu128", "windows-cpu"))
    parser.add_argument("--release-base", default=RUNTIME_RELEASE_BASE)
    parser.add_argument("--part-size-bytes", type=int, default=DEFAULT_PART_SIZE)
    args = parser.parse_args()
    if args.print_contract:
        print(
            json.dumps(
                {
                    "pymss_version": PYMSS_VERSION,
                    "pymss_core_version": PYMSS_CORE_VERSION,
                    "runtime_version": PYMSS_RUNTIME_VERSION,
                    "python_abi_version": PYMSS_PYTHON_VERSION,
                    "embedded_python_version": PYMSS_EMBEDDED_PYTHON_VERSION,
                    "embedded_python_sha256": PYMSS_EMBEDDED_PYTHON_SHA256,
                    "torch_version": PYMSS_TORCH_VERSION,
                }
            )
        )
        return 0
    if args.runtime_dir is None or args.output_dir is None or args.variant is None:
        parser.error("--runtime-dir, --output-dir and --variant are required")
    path = package_runtime(
        args.runtime_dir,
        args.output_dir,
        variant=args.variant,
        release_base=args.release_base,
        part_size=args.part_size_bytes,
    )
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
