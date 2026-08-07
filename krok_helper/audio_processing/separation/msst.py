"""Read-only discovery and registration of existing MSST-WebUI models."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from pathlib import Path

from .backend import ExternalModelCandidate
from .stems import parse_model_stems
from .runtime import sha256_file
from .states import TaskType

_SUPPORTED_TYPES = {
    "apollo",
    "bandit",
    "bandit_v2",
    "bs_conformer",
    "bs_roformer",
    "bs_roformer_hyperace",
    "demucs",
    "htdemucs",
    "legacy_demucs",
    "legacy_tasnet",
    "mdx23c",
    "mel_band_conformer",
    "mel_band_roformer",
    "scnet",
    "tasnet",
    "vr",
}
_MODEL_SUFFIXES = {".ckpt", ".chpt", ".th", ".pth", ".pt"}


def _read_json(path: Path):
    with path.open("r", encoding="utf-8-sig") as stream:
        return json.load(stream)


def _first_existing(root: Path, *relative_paths: str) -> Path | None:
    return next(
        (root / relative for relative in relative_paths if (root / relative).is_file()),
        None,
    )


def stable_candidate_id(model_path: Path, task: TaskType, *, source: str = "msst") -> str:
    """同一个文件 + 同一个任务恒定得到同一个 id；``source`` 区分发现方式。"""
    normalized = os.path.normcase(str(model_path.resolve()))
    digest = hashlib.sha256(f"{normalized}|{task.value}".encode("utf-8")).hexdigest()[:20]
    return f"{source}:{task.value}:{digest}"


def _stable_candidate_id(model_path: Path, task: TaskType) -> str:
    return stable_candidate_id(model_path, task)


def _registry_name(candidate_id: str) -> str:
    return "krok_" + candidate_id.replace(":", "_")


def _normalize_model_type(value: str) -> str:
    model_type = str(value or "").strip().lower()
    if model_type == "scnet_unofficial":
        return "scnet"
    return model_type


def _config_instruments(path: Path) -> tuple[str, ...]:
    """读取 ``training.instruments``（委托给统一实现）。

    这里原先自带一份解析，但它要求序列项比 ``instruments:`` 缩进更深，因而读不出
    「序列项与键同缩进」的写法——而 PyMSS catalog 下发的配置正是这种。统一到
    :func:`stems.parse_model_stems` 后两种写法都支持。
    """
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ()
    return parse_model_stems(text)


def _suggest_tasks(name: str, category: str, instruments: tuple[str, ...], *, karaoke=False):
    text = f"{name} {category}".lower()
    stems = {stem.lower() for stem in instruments}
    if karaoke or any(token in text for token in ("karaoke", "backing", "chorus", "bve")):
        return (TaskType.HARMONY,)
    tasks: list[TaskType] = []
    if "vocals" in stems or "vocal" in stems or "vocal" in text:
        tasks.append(TaskType.VOCAL)
    if (
        "instrumental" in stems
        or "instrument" in stems
        or any(token in text for token in ("instrumental", "inst_", "inst-", "instvoc"))
    ):
        tasks.append(TaskType.INSTRUMENTAL)
    return tuple(dict.fromkeys(tasks or (TaskType.VOCAL,)))


def build_candidate(
    *,
    name: str,
    category: str,
    model_type: str,
    model_path: Path,
    config_path: Path | None,
    task: TaskType,
    instruments: tuple[str, ...],
    cancelled=None,
    source: str = "msst",
) -> ExternalModelCandidate:
    """由一份权重 + 配置构造候选（MSST 扫描与本地文件导入共用）。

    只做静态可读性判定；能否真正跑起来仍以后续的真实加载验证为准。
    """
    normalized_type = _normalize_model_type(model_type)
    missing_model = not model_path.is_file()
    config_required = normalized_type not in {
        "vr",
        "demucs",
        "tasnet",
        "legacy_demucs",
        "legacy_tasnet",
    }
    missing_config = config_required and (config_path is None or not config_path.is_file())
    if missing_model:
        status, bindable = "模型文件缺失", False
    elif normalized_type not in _SUPPORTED_TYPES:
        status, bindable = "暂不支持", False
    elif missing_config:
        status, bindable = "配置缺失", False
    else:
        status, bindable = "结构兼容，待首次加载", True
    model_stat = model_path.stat() if model_path.is_file() else None
    config_stat = config_path.stat() if config_path and config_path.is_file() else None
    size = model_stat.st_size if model_stat else 0
    digest = sha256_file(model_path, cancelled=cancelled) if bindable else ""
    config_digest = (
        sha256_file(config_path, cancelled=cancelled)
        if bindable and config_path and config_stat
        else ""
    )
    target_stem = "/".join(instruments)
    detail_parts = []
    if bindable:
        detail_parts.append("权重、配置和模型类型可由 PyMSS 读取；首次使用时执行真实加载验证。")
    elif missing_model:
        detail_parts.append(
            "映射中存在记录，但模型权重不在预期位置。"
            if source == "msst"
            else "所选权重文件不存在。"
        )
    elif missing_config:
        detail_parts.append("该架构需要 YAML 配置文件。")
    else:
        detail_parts.append(f"PyMSS {normalized_type or model_type} 加载器不受支持。")
    return ExternalModelCandidate(
        candidate_id=stable_candidate_id(model_path, task, source=source),
        display_name=name,
        task=task,
        status=status,
        architecture=normalized_type or "未知",
        model_path=str(model_path.resolve()),
        config_path=str(config_path.resolve()) if config_path and config_path.is_file() else "",
        detail="".join(detail_parts),
        bindable=bindable,
        model_type=normalized_type,
        target_stem=target_stem,
        size_bytes=size,
        mtime_ns=model_stat.st_mtime_ns if model_stat else 0,
        sha256=digest,
        config_size_bytes=config_stat.st_size if config_stat else 0,
        config_mtime_ns=config_stat.st_mtime_ns if config_stat else 0,
        config_sha256=config_digest,
    )


def scan_msst_models(root: str | os.PathLike, *, cancelled=None) -> list[ExternalModelCandidate]:
    base = Path(root)
    if not base.is_dir():
        raise FileNotFoundError("所选 MSST-WebUI 目录不存在。")
    candidates: list[ExternalModelCandidate] = []
    map_paths = [
        _first_existing(base, "data/msst_model_map.json", "data_backup/msst_model_map.json"),
        _first_existing(base, "config_unofficial/unofficial_msst_model.json"),
    ]
    for map_path in (path for path in map_paths if path is not None):
        payload = _read_json(map_path)
        if not isinstance(payload, dict):
            continue
        for category, models in payload.items():
            if not isinstance(models, list):
                continue
            for item in models:
                if cancelled is not None and cancelled.is_set():
                    raise InterruptedError("MSST 模型扫描已取消。")
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", "")).strip()
                if not name or Path(name).suffix.lower() not in _MODEL_SUFFIXES:
                    continue
                model_path = base / "pretrain" / str(category) / name
                raw_config = str(item.get("config_path", "")).strip()
                config_path = (base / raw_config) if raw_config else None
                instruments = _config_instruments(config_path) if config_path else ()
                for task in _suggest_tasks(name, str(category), instruments):
                    candidates.append(
                        build_candidate(
                            name=name,
                            category=str(category),
                            model_type=str(item.get("model_type", "")),
                            model_path=model_path,
                            config_path=config_path,
                            task=task,
                            instruments=instruments,
                            cancelled=cancelled,
                        )
                    )
    candidates.extend(_scan_vr_models(base, cancelled=cancelled))
    # A user map may repeat an official model. Prefer the first complete entry.
    unique: dict[str, ExternalModelCandidate] = {}
    for candidate in candidates:
        previous = unique.get(candidate.candidate_id)
        if previous is None or (candidate.bindable and not previous.bindable):
            unique[candidate.candidate_id] = candidate
    return sorted(
        unique.values(),
        key=lambda item: (item.task.value, not item.bindable, item.display_name.lower()),
    )


def _scan_vr_models(base: Path, *, cancelled=None) -> list[ExternalModelCandidate]:
    map_paths = [
        _first_existing(base, "data/vr_model_map.json", "data_backup/vr_model_map.json"),
        _first_existing(base, "config_unofficial/unofficial_vr_model.json"),
    ]
    config_path = _first_existing(base, "data/webui_config.json", "data_backup/webui_config.json")
    vr_root = base / "pretrain" / "VR_Models"
    if config_path:
        try:
            configured = _read_json(config_path).get("settings", {}).get("uvr_model_dir", "")
            if configured:
                candidate_root = Path(configured)
                vr_root = candidate_root if candidate_root.is_absolute() else base / candidate_root
        except (OSError, ValueError, AttributeError):
            pass
    found: list[ExternalModelCandidate] = []
    for map_path in (path for path in map_paths if path is not None):
        payload = _read_json(map_path)
        if not isinstance(payload, dict):
            continue
        for name, item in payload.items():
            if cancelled is not None and cancelled.is_set():
                raise InterruptedError("MSST 模型扫描已取消。")
            if not isinstance(item, dict) or Path(name).suffix.lower() != ".pth":
                continue
            stems = tuple(
                str(item.get(key, "")).strip()
                for key in ("primary_stem", "secondary_stem")
                if str(item.get(key, "")).strip()
            )
            for task in _suggest_tasks(
                str(name), "VR_Models", stems, karaoke=bool(item.get("is_karaoke"))
            ):
                found.append(
                    build_candidate(
                        name=str(name),
                        category="VR_Models",
                        model_type="vr",
                        model_path=vr_root / str(name),
                        config_path=None,
                        task=task,
                        instruments=stems,
                        cancelled=cancelled,
                    )
                )
    return found


class ExternalModelRegistry:
    """Own the PyMSS user registry without altering the source MSST tree."""

    def __init__(self, path: str | os.PathLike) -> None:
        self.path = Path(path)

    def bind(self, task: TaskType, candidate: ExternalModelCandidate) -> str:
        if not candidate.bindable:
            raise ValueError("该 MSST 模型尚未通过兼容性检查，不能绑定。")
        payload = self.load()
        name = _registry_name(candidate.candidate_id)
        models = [
            model
            for model in payload["models"]
            if model.get("name") != name
            and model.get("krok", {}).get("task") != task.value
        ]
        models.append(
            {
                "name": name,
                "model_type": candidate.model_type,
                "model_path": candidate.model_path,
                "config_path": candidate.config_path or None,
                "aliases": [],
                "architecture": candidate.architecture,
                "target_stem": candidate.target_stem,
                "krok": {
                    "task": task.value,
                    "candidate_id": candidate.candidate_id,
                    "source": candidate.candidate_id.split(":", 1)[0] or "msst",
                    "validation_status": "pending",
                    "validation_error": "",
                    "size": candidate.size_bytes,
                    "mtime_ns": candidate.mtime_ns,
                    "sha256": candidate.sha256,
                    "config_size": candidate.config_size_bytes,
                    "config_mtime_ns": candidate.config_mtime_ns,
                    "config_sha256": candidate.config_sha256,
                },
            }
        )
        payload["models"] = models
        self._save(payload)
        return name

    def load(self) -> dict:
        if not self.path.is_file():
            return {"version": 1, "models": []}
        payload = _read_json(self.path)
        if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
            raise ValueError("外部模型清单格式无效。")
        return {"version": 1, "models": list(payload["models"])}

    def validate(self) -> dict[TaskType, str]:
        statuses: dict[TaskType, str] = {}
        payload = self.load()
        metadata_updated = False
        for model in payload["models"]:
            krok = model.get("krok", {}) if isinstance(model, dict) else {}
            try:
                task = TaskType(str(krok.get("task", "")))
            except ValueError:
                continue
            model_path = Path(str(model.get("model_path", "")))
            config_raw = str(model.get("config_path") or "")
            config_path = Path(config_raw) if config_raw else None
            if not model_path.is_file() or (config_path is not None and not config_path.is_file()):
                statuses[task] = "missing"
                continue
            model_status, model_updates = self._validate_fingerprint(
                model_path,
                size=int(krok.get("size", 0)),
                mtime_ns=int(krok.get("mtime_ns", 0)),
                digest=str(krok.get("sha256", "")),
            )
            config_status, config_updates = "ready", {}
            if config_path is not None:
                config_status, config_updates = self._validate_fingerprint(
                    config_path,
                    size=int(krok.get("config_size", 0)),
                    mtime_ns=int(krok.get("config_mtime_ns", 0)),
                    digest=str(krok.get("config_sha256", "")),
                )
            if model_status == "changed" or config_status == "changed":
                statuses[task] = "changed"
            else:
                saved_status = str(krok.get("validation_status", "pending"))
                statuses[task] = (
                    saved_status
                    if saved_status in {"pending", "ready", "unsupported"}
                    else "pending"
                )
                if model_updates or config_updates:
                    krok.update(model_updates)
                    krok.update({f"config_{key}": value for key, value in config_updates.items()})
                    metadata_updated = True
        if metadata_updated:
            self._save(payload)
        return statuses

    def mark_verified(self, task: TaskType) -> None:
        self._set_validation_status(task, "ready", "")

    def mark_unsupported(self, task: TaskType, message: str) -> None:
        self._set_validation_status(task, "unsupported", str(message).strip())

    def validation_error(self, task: TaskType) -> str:
        for model in self.load()["models"]:
            krok = model.get("krok", {}) if isinstance(model, dict) else {}
            if krok.get("task") == task.value:
                return str(krok.get("validation_error", ""))
        return ""

    def candidate_id(self, task: TaskType) -> str:
        for model in self.load()["models"]:
            krok = model.get("krok", {}) if isinstance(model, dict) else {}
            if krok.get("task") == task.value:
                return str(krok.get("candidate_id", ""))
        return ""

    def unbind(self, task: TaskType) -> None:
        payload = self.load()
        payload["models"] = [
            model
            for model in payload["models"]
            if not (
                isinstance(model, dict)
                and model.get("krok", {}).get("task") == task.value
            )
        ]
        self._save(payload)

    def _set_validation_status(self, task: TaskType, status: str, message: str) -> None:
        payload = self.load()
        found = False
        for model in payload["models"]:
            krok = model.get("krok", {}) if isinstance(model, dict) else {}
            if krok.get("task") != task.value:
                continue
            krok["validation_status"] = status
            krok["validation_error"] = message
            found = True
        if not found:
            raise KeyError(f"没有找到 {task.value} 的外部模型映射。")
        self._save(payload)

    @staticmethod
    def _validate_fingerprint(
        path: Path,
        *,
        size: int,
        mtime_ns: int,
        digest: str,
    ) -> tuple[str, dict[str, int | str]]:
        """Validate cheaply until metadata changes, then confirm with SHA-256.

        Old registry entries without the newer timestamp fields are migrated only
        after their recorded digest has been checked.  If metadata changed but the
        bytes did not (for example after a file restore), the fresh metadata is
        persisted so later starts stay inexpensive.
        """
        stat_result = path.stat()
        metadata_matches = (
            size == stat_result.st_size
            and mtime_ns > 0
            and mtime_ns == stat_result.st_mtime_ns
        )
        if metadata_matches:
            return "ready", {}
        if not digest or sha256_file(path) != digest:
            return "changed", {}
        return "ready", {
            "size": stat_result.st_size,
            "mtime_ns": stat_result.st_mtime_ns,
            "sha256": digest,
        }

    def _save(self, payload: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)


__all__ = ["ExternalModelRegistry", "scan_msst_models"]
