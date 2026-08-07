from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from krok_helper.audio_processing.separation.msst import (
    ExternalModelRegistry,
    scan_msst_models,
)
from krok_helper.audio_processing.separation.states import TaskType


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _legacy_msst_tree(root: Path) -> Path:
    config = root / "configs" / "vocal.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(
        "training:\n  instruments:\n    - vocals\n    - instrumental\n",
        encoding="utf-8",
    )
    weight = root / "pretrain" / "vocal_models" / "shared.ckpt"
    weight.parent.mkdir(parents=True)
    weight.write_bytes(b"model-weight")
    _write_json(
        root / "data" / "msst_model_map.json",
        {
            "vocal_models": [
                {
                    "name": "shared.ckpt",
                    "config_path": "configs/vocal.yaml",
                    "model_type": "mel_band_roformer",
                },
                {
                    "name": "missing.ckpt",
                    "config_path": "configs/vocal.yaml",
                    "model_type": "mel_band_roformer",
                },
                {
                    "name": "unsupported.ckpt",
                    "config_path": "configs/vocal.yaml",
                    "model_type": "unknown_architecture",
                },
            ]
        },
    )
    (root / "pretrain" / "vocal_models" / "unsupported.ckpt").write_bytes(b"x")
    return weight


def test_scan_legacy_msst_is_read_only_and_returns_stable_candidates(tmp_path) -> None:
    root = tmp_path / "MSST-WebUI"
    weight = _legacy_msst_tree(root)
    before = {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }

    first = scan_msst_models(root)
    second = scan_msst_models(root)

    after = {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert [item.candidate_id for item in first] == [item.candidate_id for item in second]
    shared = [item for item in first if Path(item.model_path) == weight.resolve()]
    assert {item.task for item in shared} == {TaskType.VOCAL, TaskType.INSTRUMENTAL}
    assert all(item.bindable for item in shared)
    assert all(item.model_type == "mel_band_roformer" for item in shared)
    assert all(item.sha256 for item in shared)
    assert any(not item.bindable and "模型文件缺失" in item.status for item in first)
    assert any(not item.bindable and "暂不支持" in item.status for item in first)


def test_scan_vr_models_uses_configured_external_directory(tmp_path) -> None:
    root = tmp_path / "MSST-WebUI"
    external = tmp_path / "uvr-models"
    external.mkdir()
    model = external / "karaoke.pth"
    model.write_bytes(b"vr-weight")
    _write_json(
        root / "data" / "webui_config.json",
        {"settings": {"uvr_model_dir": str(external)}},
    )
    _write_json(
        root / "data" / "vr_model_map.json",
        {
            "karaoke.pth": {
                "primary_stem": "Vocals",
                "secondary_stem": "Instrumental",
                "is_karaoke": True,
            }
        },
    )

    candidates = scan_msst_models(root)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.task is TaskType.HARMONY
    assert candidate.bindable
    assert Path(candidate.model_path) == model.resolve()
    assert candidate.model_type == "vr"


def test_external_registry_does_not_write_to_msst_and_revalidates_files(tmp_path) -> None:
    root = tmp_path / "MSST-WebUI"
    weight = _legacy_msst_tree(root)
    candidate = next(
        item
        for item in scan_msst_models(root)
        if item.task is TaskType.VOCAL and item.bindable
    )
    registry_path = tmp_path / "managed" / "manifests" / "external-models.json"
    registry = ExternalModelRegistry(registry_path)

    registered_name = registry.bind(TaskType.VOCAL, candidate)

    assert registered_name.startswith("krok_msst_vocal_")
    payload = registry.load()
    assert payload["models"][0]["model_path"] == str(weight.resolve())
    fingerprint = payload["models"][0]["krok"]
    assert fingerprint["mtime_ns"] == weight.stat().st_mtime_ns
    assert fingerprint["sha256"] == candidate.sha256
    assert fingerprint["config_sha256"] == candidate.config_sha256
    assert registry.validate() == {TaskType.VOCAL: "pending"}
    assert not list(root.rglob("external-models.json"))

    registry.mark_verified(TaskType.VOCAL)
    assert registry.validate() == {TaskType.VOCAL: "ready"}

    registry.mark_unsupported(TaskType.VOCAL, "加载器拒绝该配置")
    assert registry.validate() == {TaskType.VOCAL: "unsupported"}
    assert registry.validation_error(TaskType.VOCAL) == "加载器拒绝该配置"

    registry.unbind(TaskType.VOCAL)
    assert registry.validate() == {}
    assert weight.is_file()
    registry.bind(TaskType.VOCAL, candidate)

    weight.write_bytes(b"changed-size-and-content")
    assert registry.validate() == {TaskType.VOCAL: "changed"}
    weight.unlink()
    assert registry.validate() == {TaskType.VOCAL: "missing"}


def test_external_registry_detects_same_size_weight_and_config_changes(tmp_path) -> None:
    root = tmp_path / "MSST-WebUI"
    weight = _legacy_msst_tree(root)
    candidate = next(
        item
        for item in scan_msst_models(root)
        if item.task is TaskType.VOCAL and item.bindable
    )
    registry = ExternalModelRegistry(tmp_path / "external-models.json")
    registry.bind(TaskType.VOCAL, candidate)

    original_mtime = weight.stat().st_mtime_ns
    weight.write_bytes(b"changed-byte")
    assert weight.stat().st_size == candidate.size_bytes
    assert weight.stat().st_mtime_ns != original_mtime
    assert registry.validate() == {TaskType.VOCAL: "changed"}

    weight.write_bytes(b"model-weight")
    os.utime(weight, ns=(weight.stat().st_atime_ns, original_mtime + 1))
    assert registry.validate() == {TaskType.VOCAL: "pending"}
    assert registry.load()["models"][0]["krok"]["mtime_ns"] == weight.stat().st_mtime_ns

    config = root / "configs" / "vocal.yaml"
    config.write_text(
        "training:\n  instruments:\n    - violin\n    - instrumental\n",
        encoding="utf-8",
    )
    assert registry.validate() == {TaskType.VOCAL: "changed"}


def test_scan_rejects_non_directory_and_honours_cancellation(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        scan_msst_models(tmp_path / "missing")

    root = tmp_path / "MSST-WebUI"
    _legacy_msst_tree(root)

    class _Cancelled:
        @staticmethod
        def is_set() -> bool:
            return True

    with pytest.raises(InterruptedError):
        scan_msst_models(root, cancelled=_Cancelled())
