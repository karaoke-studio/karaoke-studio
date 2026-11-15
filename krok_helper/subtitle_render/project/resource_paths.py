"""Resolve portable project media references without searching unrelated trees."""

from __future__ import annotations

import ntpath
import os
import posixpath
from pathlib import Path, PurePosixPath, PureWindowsPath

from krok_helper.subtitle_render.domain.background import (
    BackgroundSource,
    background_sequence_frame_path,
)


def _resource_fields(data: dict) -> list[tuple[dict, str, int | None]]:
    # Callers may stop at the first resource. An eager list avoids closing a
    # partially consumed generator with GeneratorExit under the debugger.
    fields = [(data, key, None) for key in ("subtitle_path", "video_path", "audio_path")]
    background = data.get("background")
    if isinstance(background, dict) and background.get("kind") in (
        "video", "image", "image_sequence",
    ):
        start = None
        if background["kind"] == "image_sequence":
            try:
                start = max(int(background.get("sequence_start_number") or 0), 0)
            except (TypeError, ValueError):
                start = 0
        fields.append((background, "path", start))
    extras = data.get("extra_subtitle_sources")
    if isinstance(extras, list):
        for item in extras:
            if isinstance(item, dict):
                fields.append((item, "path", None))
    return fields


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _path(value: str) -> Path:
    # Accept Windows-relative references when opening a project on macOS/Linux.
    return Path(value.replace("\\", "/"))


def _has_anchor(value: str) -> bool:
    return bool(_path(value).anchor or PureWindowsPath(value).anchor)


def _absolute(path: Path) -> Path:
    # Collapse '..' without dereferencing symlinks out of the portable tree.
    return Path(os.path.abspath(path))


def _resource_base(project_path: Path, data: dict) -> Path:
    # Recovery/discarded snapshots live outside the formal project directory.
    base = _absolute(project_path).parent
    for key in ("recovery", "backup"):
        metadata = data.get(key)
        if isinstance(metadata, dict):
            source = _text(metadata.get("source_project_path"))
            if source:
                source_path = _path(source)
                if not _has_anchor(source):
                    source_path = base / source_path
                return _absolute(source_path).parent
    return base


def with_resource_base(data: dict, project_path: Path) -> dict:
    """Remember the original project directory alongside absolute references."""
    for container, key, _ in _resource_fields(data):
        if _text(container.get(key)):
            return {**data, "resource_base_dir": str(_resource_base(project_path, data))}
    return data


def _available(path: Path, sequence_start: int | None) -> bool:
    try:
        if sequence_start is not None:
            path = background_sequence_frame_path(
                BackgroundSource(
                    kind="image_sequence", path=str(path),
                    sequence_start_number=sequence_start,
                ),
                0,
            )
        return path is not None and path.is_file()
    except (OSError, ValueError):
        return False


def resolve_resource_paths(data: dict, project_path: Path) -> dict:
    """Normalize resources in a freshly read payload; never rewrite its file."""
    base = _resource_base(project_path, data)
    saved_base = _text(data.get("resource_base_dir"))
    for container, key, sequence_start in _resource_fields(data):
        raw = _text(container.get(key))
        if not raw:
            continue
        path = _path(raw)
        if not _has_anchor(raw):
            # Relative references always belong to the project, never the cwd.
            container[key] = str(_absolute(base / path))
            continue
        if path.is_absolute() and _available(path, sequence_start):
            continue
        candidates = []
        if saved_base and _has_anchor(saved_base):
            # The saved base retains exact sibling/parent relationships, even
            # when the whole bundle moves to another drive or operating system.
            path_module = ntpath if PureWindowsPath(raw).drive else posixpath
            try:
                relative = path_module.relpath(raw, saved_base)
            except ValueError:  # Different Windows drives have no relative path.
                pass
            else:
                candidates.append(base / _path(relative))
        if not candidates:
            # Old projects have no saved base. Match suffixes longest first:
            # /old/song/media/bg.mp4 -> <project>/media/bg.mp4 -> .../bg.mp4.
            parts = PurePosixPath(raw.replace("\\", "/")).parts
            if parts and (parts[0] == "/" or PureWindowsPath(raw).drive):
                parts = parts[1:]
            candidates.extend(base.joinpath(*parts[index:]) for index in range(len(parts)))
        for candidate in candidates:
            candidate = _absolute(candidate)
            if _available(candidate, sequence_start):
                container[key] = str(candidate)
                break
    return data
