"""Verified persistent cache for completed intermediate separation results."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
from pathlib import Path


_CHUNK_SIZE = 4 * 1024 * 1024


def sha256_file(path: Path, *, cancelled=None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(_CHUNK_SIZE), b""):
            if cancelled is not None and cancelled.is_set():
                raise InterruptedError("中间结果缓存检查已取消。")
            digest.update(chunk)
    return digest.hexdigest()


def input_fingerprint(path: Path, *, cancelled=None) -> dict[str, int | str]:
    stat_result = path.stat()
    return {
        "size": stat_result.st_size,
        "sha256": sha256_file(path, cancelled=cancelled),
    }


def cache_key(metadata: dict) -> str:
    encoded = json.dumps(
        metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class IntermediateResultCache:
    """Store a verified PyMSS ZIP keyed by input and versioned step metadata.

    A cache entry is usable only after its completion manifest and archive
    digest both verify.  Interrupted writes therefore remain invisible.
    """

    def __init__(
        self,
        root: str | os.PathLike,
        *,
        max_entries: int = 8,
        max_bytes: int = 12 * 1024**3,
    ) -> None:
        self.root = Path(root)
        self.max_entries = max(1, int(max_entries))
        self.max_bytes = max(1, int(max_bytes))

    def lookup(self, key: str, *, cancelled=None) -> Path | None:
        entry = self.root / key
        manifest_path = entry / "manifest.json"
        archive = entry / "result.zip"
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (
                not isinstance(payload, dict)
                or payload.get("schema") != 1
                or payload.get("complete") is not True
                or payload.get("key") != key
                or not archive.is_file()
                or archive.stat().st_size != int(payload.get("archive_size", -1))
                or sha256_file(archive, cancelled=cancelled)
                != str(payload.get("archive_sha256", ""))
            ):
                return None
            payload["last_used_at"] = int(time.time())
            self._write_json(manifest_path, payload)
            return archive
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def store(self, key: str, archive: Path, metadata: dict, *, cancelled=None) -> Path:
        if not archive.is_file():
            raise FileNotFoundError(archive)
        entry = self.root / key
        entry.mkdir(parents=True, exist_ok=True)
        target = entry / "result.zip"
        partial = entry / f".result.{uuid.uuid4().hex}.zip.part"
        try:
            with archive.open("rb") as source, partial.open("xb") as destination:
                while True:
                    if cancelled is not None and cancelled.is_set():
                        raise InterruptedError("保存中间结果缓存已取消。")
                    chunk = source.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    destination.write(chunk)
                destination.flush()
                os.fsync(destination.fileno())
            digest = sha256_file(partial, cancelled=cancelled)
            os.replace(partial, target)
            now = int(time.time())
            self._write_json(
                entry / "manifest.json",
                {
                    "schema": 1,
                    "complete": True,
                    "key": key,
                    "metadata": metadata,
                    "archive_size": target.stat().st_size,
                    "archive_sha256": digest,
                    "created_at": now,
                    "last_used_at": now,
                },
            )
            self.prune(exclude={key})
            return target
        finally:
            partial.unlink(missing_ok=True)

    def prune(self, *, exclude: set[str] | None = None) -> None:
        """Bound app-owned cache growth; never touches paths outside ``root``."""
        excluded = exclude or set()
        entries: list[tuple[int, int, Path]] = []
        if not self.root.is_dir():
            return
        for entry in self.root.iterdir():
            if not entry.is_dir() or entry.name in excluded:
                continue
            manifest = entry / "manifest.json"
            archive = entry / "result.zip"
            try:
                payload = json.loads(manifest.read_text(encoding="utf-8"))
                touched = int(payload.get("last_used_at", payload.get("created_at", 0)))
                size = archive.stat().st_size
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                touched, size = 0, 0
            entries.append((touched, size, entry))
        entries.sort(key=lambda item: item[0], reverse=True)
        total = 0
        for index, (_touched, size, entry) in enumerate(entries):
            total += size
            if index >= self.max_entries - 1 or total > self.max_bytes:
                shutil.rmtree(entry, ignore_errors=True)

    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


__all__ = [
    "IntermediateResultCache",
    "cache_key",
    "input_fingerprint",
    "sha256_file",
]
