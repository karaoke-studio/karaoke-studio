"""Import NicoKaraMaker3 ``TemplateFont/*.tpl`` files as style presets.

N3 templates are ZIP files containing one UTF-8 JSON entry named ``0``.  In
contrast to a saved ``.n3proj`` snapshot, every ``SizeAndRatio`` value must be
resolved against the target output height when the template is used.
"""

from __future__ import annotations

import json
import os
import uuid
import zipfile
from copy import deepcopy
from dataclasses import dataclass, fields as dataclass_fields
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from krok_helper.subtitle_render.models import StylePreset, SubtitleStyleScheme
from krok_helper.subtitle_render.n3_font_scheme import convert_n3_font_scheme


N3_TEMPLATE_SUFFIX = ".tpl"
N3_TEMPLATE_FILTER = "NicoKaraMaker3 字体模板 (*.tpl);;所有文件 (*.*)"
N3_TEMPLATE_SOURCE_TYPE = "n3_font_template"


@dataclass(frozen=True)
class N3TemplateLoadResult:
    path: Path
    guid: str
    name: str
    preset: StylePreset
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class N3TemplateBatchResult:
    templates: tuple[N3TemplateLoadResult, ...]
    skipped: tuple[tuple[Path, str], ...]
    failed: tuple[tuple[Path, str], ...]


@dataclass(frozen=True)
class N3TemplateMergeResult:
    presets: dict[str, StylePreset]
    imported_names: tuple[str, ...]
    skipped_names: tuple[str, ...]
    renamed: tuple[tuple[str, str], ...]


class N3TemplateNotSynchronized(ValueError):
    """Raised when a deleted/non-synchronized N3 template is filtered out."""


def n3_template_size(value: object, target_height: int) -> int:
    """Resolve one N3 template ``SizeAndRatio`` using ``UpdateSizes`` rules."""

    data = value if isinstance(value, Mapping) else {}
    try:
        size = int(data.get("Size", 0))
    except (TypeError, ValueError):
        size = 0
    try:
        ratio = float(data.get("Ratio", 0.0))
    except (TypeError, ValueError):
        ratio = 0.0
    height = max(0, int(target_height))
    if height != 0 and ratio != 0.0:
        return int(height * ratio)
    return size


def default_n3_template_directories(
    *,
    appdata: str | Path | None = None,
    localappdata: str | Path | None = None,
) -> list[Path]:
    """Return existing normal-install and MSIX ``TemplateFont`` directories."""

    roaming_raw = str(appdata) if appdata is not None else os.environ.get("APPDATA", "")
    local_raw = (
        str(localappdata)
        if localappdata is not None
        else os.environ.get("LOCALAPPDATA", "")
    )
    candidates: list[Path] = []
    if roaming_raw:
        roaming = Path(roaming_raw)
        candidates.append(roaming / "SHINTA" / "NicoKaraMaker3" / "TemplateFont")
    if local_raw:
        local = Path(local_raw)
        packages = local / "Packages"
        if packages.is_dir():
            candidates.extend(
                package / "Settings" / "TemplateFont"
                for package in packages.iterdir()
                if package.is_dir()
            )
    result: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate.is_dir():
            continue
        key = str(candidate.resolve()).casefold()
        if key not in seen:
            seen.add(key)
            result.append(candidate)
    return result


def find_n3_template_files(inputs: Iterable[str | Path] | None = None) -> list[Path]:
    """Expand files/directories recursively and return unique ``.tpl`` files."""

    roots = list(inputs) if inputs is not None else default_n3_template_directories()
    result: list[Path] = []
    seen: set[str] = set()
    for raw in roots:
        path = Path(raw)
        candidates = [path] if path.is_file() else (
            sorted(candidate for candidate in path.rglob("*") if candidate.is_file())
            if path.is_dir()
            else []
        )
        for candidate in candidates:
            if candidate.suffix.lower() != N3_TEMPLATE_SUFFIX:
                continue
            key = str(candidate.resolve()).casefold()
            if key not in seen:
                seen.add(key)
                result.append(candidate)
    return result


def _read_n3_template_payload(path: Path) -> dict:
    try:
        with zipfile.ZipFile(path) as archive:
            if "0" not in archive.namelist():
                raise ValueError("模板压缩包缺少固定条目 0")
            raw = archive.read("0")
    except zipfile.BadZipFile as exc:
        raise ValueError("不是有效的 NicoKaraMaker3 字体模板（zip 解包失败）") from exc
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"模板内容不是合法 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("模板内容不是对象")
    if not isinstance(payload.get("FontInfos"), list):
        raise ValueError("模板缺少 FontInfos 字段")
    if not isinstance(payload.get("BrushInfos"), list):
        raise ValueError("模板缺少 BrushInfos 字段")
    if not str(payload.get("SettingsName") or "").strip():
        raise ValueError("模板缺少 SettingsName 字段")
    return payload


def _validate_template_guid(path: Path, payload: Mapping[str, object]) -> str:
    guid = str(payload.get("Guid") or "").strip()
    try:
        file_guid = str(uuid.UUID(path.stem))
    except ValueError:
        return guid
    if not guid:
        raise ValueError("模板文件名是 GUID，但内容缺少 Guid 字段")
    try:
        payload_guid = str(uuid.UUID(guid))
    except ValueError as exc:
        raise ValueError(f"模板 Guid 格式不正确：{guid}") from exc
    if payload_guid.casefold() != file_guid.casefold():
        raise ValueError(f"模板 Guid 与文件名不一致：{guid} != {path.stem}")
    return guid


def _scheme_from_payload(
    payload: dict,
    *,
    target_height: int,
    lyrics_dir: Path,
    warnings: list[str],
) -> SubtitleStyleScheme:
    name = str(payload.get("SettingsName") or "N3 模板").strip()
    changes = convert_n3_font_scheme(
        payload,
        lyrics_dir,
        warnings,
        name,
        size_resolver=lambda value: n3_template_size(value, target_height),
    )
    field_names = {item.name for item in dataclass_fields(SubtitleStyleScheme)}
    return SubtitleStyleScheme(
        **{key: value for key, value in changes.items() if key in field_names}
    )


def load_n3_font_template(
    path: str | Path,
    *,
    target_height: int,
    include_unsynchronized: bool = False,
    lyrics_dir: str | Path | None = None,
) -> N3TemplateLoadResult:
    """Read one ``.tpl`` and materialize a preset for ``target_height``."""

    template_path = Path(path)
    payload = _read_n3_template_payload(template_path)
    if payload.get("Synchronize") is not True and not include_unsynchronized:
        raise N3TemplateNotSynchronized("Synchronize=false，已按 N3 删除态模板跳过")
    guid = _validate_template_guid(template_path, payload)
    name = str(payload.get("SettingsName") or "").strip()
    warnings: list[str] = []
    scheme = _scheme_from_payload(
        payload,
        target_height=target_height,
        lyrics_dir=Path(lyrics_dir) if lyrics_dir is not None else template_path.parent,
        warnings=warnings,
    )
    preset = StylePreset(
        name=name,
        group="N3",
        scheme=scheme,
        source_type=N3_TEMPLATE_SOURCE_TYPE,
        source_data={
            "payload": deepcopy(payload),
            "path": str(template_path),
            "guid": guid,
        },
    )
    return N3TemplateLoadResult(
        path=template_path,
        guid=guid,
        name=name,
        preset=preset,
        warnings=tuple(warnings),
    )


def resolve_n3_template_preset(
    preset: StylePreset,
    *,
    target_height: int,
    lyrics_dir: str | Path | None = None,
) -> tuple[StylePreset, tuple[str, ...]]:
    """Re-resolve retained N3 template ratios for a target project height."""

    if preset.source_type != N3_TEMPLATE_SOURCE_TYPE:
        return deepcopy(preset), ()
    payload = preset.source_data.get("payload")
    if not isinstance(payload, dict):
        return deepcopy(preset), (f"预设“{preset.name}”缺少 N3 原始模板数据，已使用保存值",)
    source_path = Path(str(preset.source_data.get("path") or "."))
    warnings: list[str] = []
    scheme = _scheme_from_payload(
        payload,
        target_height=target_height,
        lyrics_dir=Path(lyrics_dir) if lyrics_dir is not None else source_path.parent,
        warnings=warnings,
    )
    return (
        StylePreset(
            name=preset.name,
            group=preset.group,
            scheme=scheme,
            source_type=preset.source_type,
            source_data=deepcopy(preset.source_data),
        ),
        tuple(warnings),
    )


def load_n3_font_templates(
    inputs: Iterable[str | Path] | None,
    *,
    target_height: int,
    include_unsynchronized: bool = False,
    lyrics_dir: str | Path | None = None,
) -> N3TemplateBatchResult:
    """Load a batch while isolating skipped and malformed template files."""

    loaded: list[N3TemplateLoadResult] = []
    skipped: list[tuple[Path, str]] = []
    failed: list[tuple[Path, str]] = []
    for path in find_n3_template_files(inputs):
        try:
            loaded.append(
                load_n3_font_template(
                    path,
                    target_height=target_height,
                    include_unsynchronized=include_unsynchronized,
                    lyrics_dir=lyrics_dir,
                )
            )
        except N3TemplateNotSynchronized as exc:
            skipped.append((path, str(exc)))
        except (OSError, ValueError) as exc:
            failed.append((path, str(exc)))
    loaded.sort(key=lambda item: (item.name.casefold(), str(item.path).casefold()))
    return N3TemplateBatchResult(tuple(loaded), tuple(skipped), tuple(failed))


def merge_n3_template_presets(
    existing: Mapping[str, StylePreset],
    incoming: Sequence[N3TemplateLoadResult],
    *,
    conflict_policy: str,
) -> N3TemplateMergeResult:
    """Merge imported templates using ``overwrite``, ``rename`` or ``skip``."""

    if conflict_policy not in {"overwrite", "rename", "skip"}:
        raise ValueError(f"未知同名处理策略：{conflict_policy}")
    presets = {name: deepcopy(preset) for name, preset in existing.items()}
    imported: list[str] = []
    skipped: list[str] = []
    renamed: list[tuple[str, str]] = []
    for item in incoming:
        name = item.name
        if name in presets:
            if conflict_policy == "skip":
                skipped.append(name)
                continue
            if conflict_policy == "rename":
                base = name
                suffix = 2
                while name in presets:
                    name = f"{base} ({suffix})"
                    suffix += 1
                renamed.append((item.name, name))
        preset = deepcopy(item.preset)
        preset.name = name
        presets[name] = preset
        imported.append(name)
    return N3TemplateMergeResult(
        presets=presets,
        imported_names=tuple(imported),
        skipped_names=tuple(skipped),
        renamed=tuple(renamed),
    )
