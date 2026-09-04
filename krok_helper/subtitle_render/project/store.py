"""``.yurika`` project persistence and crash-recovery helpers.

项目文件是一份带 ``schema_version`` 的 JSON 快照，存放当前 standalone 会话的
全部可复现状态：字幕 / 背景视频 / 音频路径、全局样式、屏幕设置、配色方案选择、
导出参数。standalone 与工作台嵌入模式共用同一格式和安全写入路径。

序列化沿用字段驱动的 :func:`style_to_dict` 等——以后 ``Style`` 加字段，项目文件
自动跟着长，且旧文件用新代码打开会缺字段取默认、新文件用旧代码打开会忽略未知
key（前后兼容）。

路径目前按**绝对路径**存；移动项目文件到别处后素材链接会失效（后续可加
相对路径便携支持）。
"""

from __future__ import annotations

import json
import hashlib
import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from krok_helper.subtitle_render.domain.models import PROJECT_FILE_SUFFIX

PROJECT_SCHEMA_VERSION = 3
"""v3：空格宽度/字间距归属布局域——保存时布局携带显式值，方案槽位不再存空格宽度。"""
_RECOVERY_WRITE_LOCK = threading.Lock()
_RECOVERY_SNAPSHOT_FLOORS: dict[Path, int] = {}


@dataclass(frozen=True)
class RecoveryCandidate:
    path: Path
    source_project_path: Optional[Path]
    created_at_unix: float
    snapshot_id: int


@dataclass(frozen=True)
class ProjectFileRevision:
    """Privacy-safe identity of one on-disk project revision."""

    exists: bool
    mtime_ns: int = 0
    size: int = 0
    sha256: str = ""


def inspect_project_file(path: Path) -> ProjectFileRevision:
    """Return mtime, size, and content digest for external-change detection."""
    path = Path(path)
    try:
        stat_before = path.stat()
    except FileNotFoundError:
        return ProjectFileRevision(exists=False)
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        stat_after = path.stat()
    except FileNotFoundError:
        return ProjectFileRevision(exists=False)
    if (
        stat_before.st_mtime_ns != stat_after.st_mtime_ns
        or stat_before.st_size != stat_after.st_size
    ):
        raise OSError("项目文件在检查期间发生了变化，请重试")
    return ProjectFileRevision(
        exists=True,
        mtime_ns=stat_after.st_mtime_ns,
        size=stat_after.st_size,
        sha256=digest.hexdigest(),
    )


def project_backup_directory(root: Path, source_path: Path) -> Path:
    """Return the stable per-project backup directory without exposing full paths."""
    source = Path(source_path)
    identity = str(source.resolve()).encode("utf-8", errors="surrogatepass")
    suffix = hashlib.sha256(identity).hexdigest()[:12]
    return Path(root) / f"{source.stem}-{suffix}"


def backup_project_file(
    source_path: Path,
    root: Path,
    *,
    max_count: int,
) -> Optional[Path]:
    """Copy the current formal project revision and rotate older backups."""
    source = Path(source_path)
    limit = max(0, int(max_count))
    if limit == 0 or not source.is_file():
        return None
    directory = project_backup_directory(root, source)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / (
        f"{source.stem}.{time.time_ns()}.manual-backup{PROJECT_FILE_SUFFIX}"
    )
    temp_path = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        shutil.copy2(source, temp_path)
        os.replace(temp_path, destination)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
    backups = sorted(
        directory.glob(f"*.manual-backup{PROJECT_FILE_SUFFIX}"),
        key=lambda item: item.stat().st_mtime_ns,
        reverse=True,
    )
    for stale in backups[limit:]:
        try:
            stale.unlink(missing_ok=True)
        except OSError:
            pass
    return destination


def save_discarded_project_backup(
    root: Path,
    data: dict,
    *,
    source_project_path: Optional[Path],
    retention_days: int = 7,
) -> Path:
    """Store an explicitly labelled short-term snapshot before discarding edits."""
    directory = Path(root) / "discarded"
    directory.mkdir(parents=True, exist_ok=True)
    now = time.time()
    cutoff = now - max(int(retention_days), 1) * 24 * 60 * 60
    for stale in directory.glob(f"*.discarded-backup{PROJECT_FILE_SUFFIX}"):
        try:
            if stale.stat().st_mtime < cutoff:
                stale.unlink(missing_ok=True)
        except OSError:
            pass
    source = Path(source_project_path) if source_project_path is not None else None
    stem = source.stem if source is not None else "untitled"
    destination = directory / (
        f"{stem}.{time.time_ns()}.discarded-backup{PROJECT_FILE_SUFFIX}"
    )
    payload = dict(data)
    payload["backup"] = {
        "kind": "discarded_changes",
        "source_project_path": str(source) if source is not None else None,
        "created_at_unix": now,
        "retention_days": max(int(retention_days), 1),
    }
    save_render_project(destination, payload)
    return destination


def save_render_project(path: Path, data: dict) -> None:
    """Atomically write a project snapshot without truncating the old file."""
    payload = {"schema_version": PROJECT_SCHEMA_VERSION}
    payload.update(data)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    try:
        with temp_path.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def load_render_project(path: Path) -> dict:
    """读取并解析 ``.yurika``，返回项目快照 dict。

    解析失败（非法 JSON / 非 dict）抛 :class:`ValueError`，由调用方弹错处理。
    """
    raw = Path(path).read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"项目文件不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("项目文件内容不是对象")
    return data


def save_recovery_project(path: Path, data: dict) -> bool:
    """Write a recovery snapshot unless a newer snapshot already won the race."""
    recovery = data.get("recovery") if isinstance(data.get("recovery"), dict) else {}
    try:
        snapshot_id = int(recovery.get("snapshot_id") or 0)
    except (TypeError, ValueError):
        snapshot_id = 0
    path = Path(path)
    with _RECOVERY_WRITE_LOCK:
        if snapshot_id < _RECOVERY_SNAPSHOT_FLOORS.get(path, 0):
            return False
        if path.is_file():
            try:
                existing = load_render_project(path)
                existing_recovery = (
                    existing.get("recovery")
                    if isinstance(existing.get("recovery"), dict)
                    else {}
                )
                existing_id = int(existing_recovery.get("snapshot_id") or 0)
                if existing_id > snapshot_id:
                    return False
            except (OSError, TypeError, ValueError):
                pass
        save_render_project(path, data)
    return True


def invalidate_recovery_project(
    path: Path,
    snapshot_floor: Optional[int] = None,
    *,
    delete: bool = True,
) -> int:
    """Prevent in-flight older writers from recreating a discarded snapshot."""
    path = Path(path)
    floor = int(snapshot_floor or time.time_ns())
    with _RECOVERY_WRITE_LOCK:
        _RECOVERY_SNAPSHOT_FLOORS[path] = max(
            floor,
            _RECOVERY_SNAPSHOT_FLOORS.get(path, 0),
        )
        if delete:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
    return floor


def scan_recovery_projects(
    root: Path,
) -> tuple[list[RecoveryCandidate], list[Path], list[Path]]:
    """Return valid, invalid, and stale recovery files under ``root``."""
    candidates: list[RecoveryCandidate] = []
    invalid: list[Path] = []
    stale: list[Path] = []
    root = Path(root)
    if not root.is_dir():
        return candidates, invalid, stale
    for path in sorted(root.glob("*.recovery")):
        try:
            data = load_render_project(path)
            recovery = data.get("recovery")
            if not isinstance(recovery, dict):
                raise ValueError("缺少 recovery 元数据")
            source_text = str(recovery.get("source_project_path") or "").strip()
            source_path = Path(source_text) if source_text else None
            created_at = float(recovery.get("created_at_unix") or 0.0)
            snapshot_id = int(recovery.get("snapshot_id") or 0)
            if created_at <= 0 or snapshot_id <= 0:
                raise ValueError("recovery 元数据无效")
            if (
                source_path is not None
                and source_path.is_file()
                and path.stat().st_mtime_ns <= source_path.stat().st_mtime_ns
            ):
                stale.append(path)
                continue
            candidates.append(
                RecoveryCandidate(
                    path=path,
                    source_project_path=source_path,
                    created_at_unix=created_at,
                    snapshot_id=snapshot_id,
                )
            )
        except (OSError, TypeError, ValueError):
            invalid.append(path)
    candidates.sort(key=lambda item: item.snapshot_id, reverse=True)
    return candidates, invalid, stale


def _clean_path(value: object) -> Optional[str]:
    return str(value) if isinstance(value, str) and value.strip() else None


def _guide_symbol_payload_row(row: object) -> object:
    """行级导唱符条目：完整字典、或符号表引用 ID（字符串），其余丢弃。"""
    if isinstance(row, dict):
        return dict(row)
    if isinstance(row, str):
        return str(row)
    return None


def project_payload(
    *,
    subtitle_path: Optional[Path],
    video_path: Optional[Path],
    audio_path: Optional[Path],
    style: dict,
    screen: dict,
    selected_scheme_key: str,
    output: dict,
    background: Optional[dict] = None,
    line_layout_indices: Optional[list[int]] = None,
    line_breaks_before: Optional[list[str]] = None,
    char_role_labels: Optional[list] = None,
    line_guide_symbols: Optional[list] = None,
    line_inline_guide_symbols: Optional[list] = None,
    guide_symbol_table: Optional[dict] = None,
    line_display_overrides: Optional[list] = None,
    line_animation_overrides: Optional[list] = None,
    page_plan: Optional[dict] = None,
    loading_settings_mode: Optional[str] = None,
    loading_settings: Optional[dict] = None,
    loading_settings_snapshot: Optional[dict] = None,
    extra_subtitle_sources: Optional[list] = None,
    project_role_names: Optional[list[str]] = None,
) -> dict:
    """组装项目快照 dict（纯数据，不碰 UI）。便于单测与复用。

    ``line_layout_indices`` 与 ``track.lines`` 对齐（含空行），记录每行引用的
    布局（0 = 默认布局）。LRC 本身不含布局信息，只能存在项目文件里。

    ``line_breaks_before`` 同样与 ``track.lines`` 对齐，保存 N3 等价算法生成或
    N3 项目显式恢复的 ``none/page/paragraph`` 分隔类型。

    ``char_role_labels`` 同样与 ``track.lines`` 对齐：每项为 None（整行无角色）
    或与该行字符对齐的角色名列表。覆盖 UI 手动分配与 N3 导入的逐字配色
    （LRC 内的 ``【N配色】`` 标签解析后也会落到这里，重复应用无害）。

    ``line_guide_symbols`` 同样与 ``track.lines`` 对齐：每项为 None 或已归一化
    的 SVG 导唱符轮廓、走字时长与角色。轮廓嵌入工程，不依赖原 SVG 路径。

    ``line_inline_guide_symbols`` 同样与 ``track.lines`` 对齐：每项为 None 或
    ``{源字符索引: SVG 导唱符}``，用于保持句中字符的原打轴时间与布局位置。

    ``guide_symbol_table``：被多行/多处引用的导唱符轮廓去重表。行数据里的
    字符串 ID（如 ``"g0"``）引用表内条目，同一符号只序列化一份；仅被引用
    一次的符号仍在行数据内嵌完整字典，旧版本按字典解析保持兼容。

    ``line_display_overrides`` 同样与 ``track.lines`` 对齐：每项为 None（该行
    无手动覆盖）或 ``[上屏覆盖毫秒或 None, 消失覆盖毫秒或 None]``（字幕轨道
    把手拖动写入的逐行显示/隐藏时间）。

    ``line_animation_overrides`` 同样与 ``track.lines`` 对齐：每项为 None（继承
    全局特效）或逐行动画覆盖字典。

    ``extra_subtitle_sources``：副字幕源列表（N3 多歌词文件，如コーラス轨），
    每项为 ``{"name", "path", "line_layout_indices", "char_role_labels",
    "line_display_overrides", "line_animation_overrides"}``。

    ``project_role_names`` 保存当前项目的完整角色注册表，包括当前歌词引用的角色、
    旧字幕曾引用的角色，以及用户新建但尚未分配的角色。它与应用级预设库分离，
    避免历史预设污染当前项目的角色菜单；只有用户显式删除角色或切换完整项目时
    才会从注册表移除。
    """
    payload = {
        "subtitle_path": str(subtitle_path) if subtitle_path else None,
        "video_path": str(video_path) if video_path else None,
        "audio_path": str(audio_path) if audio_path else None,
        "style": style,
        "screen": screen,
        "selected_scheme_key": selected_scheme_key,
        "output": output,
    }
    if background is not None:
        payload["background"] = dict(background)
    if line_layout_indices is not None:
        payload["line_layout_indices"] = [int(v) for v in line_layout_indices]
    if line_breaks_before is not None:
        payload["line_breaks_before"] = [
            str(value) if str(value) in {"page", "paragraph"} else "none"
            for value in line_breaks_before
        ]
    if char_role_labels is not None:
        payload["char_role_labels"] = [
            [str(label) if label else None for label in row] if isinstance(row, list) else None
            for row in char_role_labels
        ]
    if line_guide_symbols is not None:
        payload["line_guide_symbols"] = [
            _guide_symbol_payload_row(row) for row in line_guide_symbols
        ]
    if line_inline_guide_symbols is not None:
        payload["line_inline_guide_symbols"] = [
            {
                str(index): _guide_symbol_payload_row(symbol)
                for index, symbol in row.items()
            }
            if isinstance(row, dict)
            else None
            for row in line_inline_guide_symbols
        ]
    if guide_symbol_table is not None:
        payload["guide_symbol_table"] = {
            str(glyph_id): dict(symbol)
            for glyph_id, symbol in guide_symbol_table.items()
            if isinstance(symbol, dict)
        }
    if line_display_overrides is not None:
        payload["line_display_overrides"] = [
            list(row) if isinstance(row, (list, tuple)) else None
            for row in line_display_overrides
        ]
    if line_animation_overrides is not None:
        payload["line_animation_overrides"] = [
            dict(row) if isinstance(row, dict) else None
            for row in line_animation_overrides
        ]
    if page_plan is not None:
        payload["page_plan"] = dict(page_plan)
    if loading_settings_mode in {"global", "custom"}:
        payload["loading_settings_mode"] = str(loading_settings_mode)
    if loading_settings is not None:
        payload["loading_settings"] = dict(loading_settings)
    if loading_settings_snapshot is not None:
        payload["loading_settings_snapshot"] = dict(loading_settings_snapshot)
    if extra_subtitle_sources is not None:
        payload["extra_subtitle_sources"] = [
            dict(item) for item in extra_subtitle_sources if isinstance(item, dict)
        ]
    if project_role_names is not None:
        payload["project_role_names"] = [
            str(name).strip() for name in project_role_names if str(name).strip()
        ]
    return payload


def split_project_paths(data: dict) -> dict[str, Optional[Path]]:
    """从项目快照里取出三个素材路径（清洗后转 ``Path``，空则 None）。"""
    return {
        "subtitle_path": _as_path(data.get("subtitle_path")),
        "video_path": _as_path(data.get("video_path")),
        "audio_path": _as_path(data.get("audio_path")),
    }


def background_payload(
    *,
    kind: str,
    path: Optional[Path] = None,
    color: str = "#000000",
    source_fps: Optional[int] = None,
    sequence_start_number: int = 0,
    video_offset_ms: int = 0,
    image_fit: str = "cover",
) -> dict[str, object]:
    """组装可写入 ``.yurika`` 的背景源快照。"""
    return {
        "kind": kind if kind in {"video", "image", "image_sequence", "solid"} else "solid",
        "path": str(path) if path else None,
        "color": str(color or "#000000"),
        "source_fps": int(source_fps) if source_fps is not None else None,
        "sequence_start_number": int(sequence_start_number),
        "video_offset_ms": int(video_offset_ms),
        "image_fit": image_fit if image_fit in {"cover", "contain"} else "cover",
    }


def _as_path(value: object) -> Optional[Path]:
    cleaned = _clean_path(value)
    return Path(cleaned) if cleaned else None


def is_project_file(path: object) -> bool:
    return isinstance(path, (str, Path)) and str(path).endswith(PROJECT_FILE_SUFFIX)


def project_output_payload(
    *,
    encoder_mode: str,
    crf: int,
    preset: str,
    output_path: str,
    codec: str = "h264",
    native_export_enabled: bool = False,
) -> dict[str, Any]:
    return {
        "encoder_mode": encoder_mode,
        "crf": int(crf),
        "preset": preset,
        "codec": codec,
        "output_path": output_path,
        "native_export_enabled": bool(native_export_enabled),
    }
