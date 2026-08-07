"""手动放进 models/ 的模型自动导入。"""

from __future__ import annotations

import json

from krok_helper.audio_processing.separation.local_models import (
    load_catalog,
    scan_local_models,
)
from krok_helper.audio_processing.separation.wizard import resolve_install_path

CATALOG = {
    "models": [
        {
            "name": "inst_v1e.ckpt",
            "aliases": ["inst_v1e.ckpt", "inst_v1e"],
            "relpath": "vocal/vocal_instrumental_dual/inst_v1e.ckpt",
            "config_relpath": "vocal/vocal_instrumental_dual/inst_v1e.yaml",
            "size_bytes": 64,
        },
        {
            "name": "karaoke_x.ckpt",
            "aliases": ["karaoke_x.ckpt", "karaoke_x"],
            "relpath": "karaoke/karaoke_x.ckpt",
            "config_relpath": "karaoke/karaoke_x.yaml",
            "size_bytes": 32,
        },
    ]
}


def _install(tmp_path, *, weight_sizes: dict[str, int], configs: set[str]):
    root = tmp_path / "pymss"
    resources = root / "runtime" / "Lib" / "site-packages" / "pymss" / "resources"
    resources.mkdir(parents=True)
    (resources / "model_catalog.json").write_text(json.dumps(CATALOG), encoding="utf-8")

    models = root / "models"
    for entry in CATALOG["models"]:
        name = entry["name"]
        if name in weight_sizes:
            path = models / entry["relpath"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x" * weight_sizes[name])
        if name in configs:
            path = models / entry["config_relpath"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("training:\n  instruments:\n  - other\n", encoding="utf-8")
    return root


class TestAutoImport:
    def test_complete_models_are_detected(self, tmp_path) -> None:
        root = _install(
            tmp_path,
            weight_sizes={"inst_v1e.ckpt": 64, "karaoke_x.ckpt": 32},
            configs={"inst_v1e.yaml", "inst_v1e.ckpt", "karaoke_x.ckpt"},
        )
        assert scan_local_models(root, {"inst_v1e", "karaoke_x"}) == {"inst_v1e", "karaoke_x"}

    def test_returns_names_in_callers_vocabulary(self, tmp_path) -> None:
        """预设用不带后缀的别名；只回 catalog 正式名会对不上，功能等于失效。"""
        root = _install(
            tmp_path, weight_sizes={"inst_v1e.ckpt": 64}, configs={"inst_v1e.ckpt"}
        )
        assert scan_local_models(root, {"inst_v1e"}) == {"inst_v1e"}
        assert scan_local_models(root, {"inst_v1e.ckpt"}) == {"inst_v1e.ckpt"}

    def test_truncated_weight_is_rejected(self, tmp_path) -> None:
        """拷贝一半的文件不能被当成可用模型（§8.4）。"""
        root = _install(
            tmp_path, weight_sizes={"inst_v1e.ckpt": 10}, configs={"inst_v1e.ckpt"}
        )
        assert scan_local_models(root, {"inst_v1e"}) == set()

    def test_missing_config_is_rejected(self, tmp_path) -> None:
        root = _install(tmp_path, weight_sizes={"inst_v1e.ckpt": 64}, configs=set())
        assert scan_local_models(root, {"inst_v1e"}) == set()

    def test_missing_models_dir_or_catalog_is_safe(self, tmp_path) -> None:
        assert scan_local_models(tmp_path / "nope", {"inst_v1e"}) == set()
        bare = tmp_path / "bare"
        (bare / "models").mkdir(parents=True)
        assert scan_local_models(bare, {"inst_v1e"}) == set()
        assert load_catalog(bare) == {}


class TestInstallPathResolution:
    def test_appends_pymss_for_a_plain_directory(self, tmp_path) -> None:
        assert resolve_install_path(tmp_path / "tools").name == "pymss"

    def test_does_not_nest_when_directory_is_already_pymss(self, tmp_path) -> None:
        """回归：选中已有的 pymss 目录曾被套成 pymss/pymss，模型再也扫不到。"""
        target = tmp_path / "pymss"
        assert resolve_install_path(target) == target

    def test_does_not_nest_into_an_existing_install(self, tmp_path) -> None:
        root = tmp_path / "audio-tools"
        (root / "manifests").mkdir(parents=True)
        (root / "manifests" / "runtime-manifest.json").write_text("{}", encoding="utf-8")
        assert resolve_install_path(root) == root
