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


class TestLocalFileImport:
    """从任意文件夹导入模型：候选构造与失败拦截。"""

    def _model(self, tmp_path, *, config: str | None = "training:\n  instruments:\n  - karaoke\n  - other\n"):
        folder = tmp_path / "my-models"
        folder.mkdir(parents=True, exist_ok=True)
        weight = folder / "third_party.ckpt"
        weight.write_bytes(b"w" * 256)
        if config is not None:
            (folder / "third_party.yaml").write_text(config, encoding="utf-8")
        return weight

    def test_guesses_sibling_config(self, tmp_path) -> None:
        from krok_helper.audio_processing.separation.local_import import guess_config_path

        weight = self._model(tmp_path)
        assert guess_config_path(weight).name == "third_party.yaml"

    def test_no_config_returns_none_instead_of_guessing(self, tmp_path) -> None:
        from krok_helper.audio_processing.separation.local_import import guess_config_path

        weight = self._model(tmp_path, config=None)
        (weight.parent / "unrelated.yaml").write_text("training:\n", encoding="utf-8")
        assert guess_config_path(weight) is None, "不能把不相干的配置套到模型上"

    def test_builds_bindable_candidate_with_real_stems(self, tmp_path) -> None:
        from krok_helper.audio_processing.separation.local_import import build_local_candidate
        from krok_helper.audio_processing.separation.states import TaskType

        weight = self._model(tmp_path)
        candidate = build_local_candidate(
            weight_path=weight,
            config_path=weight.with_suffix(".yaml"),
            model_type="mel_band_roformer",
            task=TaskType.HARMONY,
        )
        assert candidate.bindable
        assert candidate.candidate_id.startswith("local:harmony:")
        assert candidate.target_stem == "karaoke/other"
        assert candidate.model_path == str(weight.resolve())
        assert candidate.sha256, "应记录权重摘要以便检测文件变化"

    def test_rejects_unsupported_architecture(self, tmp_path) -> None:
        import pytest

        from krok_helper.audio_processing.separation.local_import import build_local_candidate
        from krok_helper.audio_processing.separation.states import TaskType

        weight = self._model(tmp_path)
        with pytest.raises(ValueError):
            build_local_candidate(
                weight_path=weight,
                config_path=weight.with_suffix(".yaml"),
                model_type="not_a_real_arch",
                task=TaskType.VOCAL,
            )

    def test_rejects_missing_weight(self, tmp_path) -> None:
        import pytest

        from krok_helper.audio_processing.separation.local_import import build_local_candidate
        from krok_helper.audio_processing.separation.states import TaskType

        with pytest.raises(FileNotFoundError):
            build_local_candidate(
                weight_path=tmp_path / "nope.ckpt",
                config_path=None,
                model_type="vr",
                task=TaskType.VOCAL,
            )

    def test_missing_config_is_not_bindable_for_config_required_arch(self, tmp_path) -> None:
        from krok_helper.audio_processing.separation.local_import import build_local_candidate
        from krok_helper.audio_processing.separation.states import TaskType

        weight = self._model(tmp_path, config=None)
        candidate = build_local_candidate(
            weight_path=weight,
            config_path=None,
            model_type="mel_band_roformer",
            task=TaskType.VOCAL,
        )
        assert not candidate.bindable
        assert "配置" in candidate.status


class TestMsstParserConsolidation:
    """msst 与 stems 两处解析已统一（原实现读不出同缩进写法）。"""

    def test_msst_reads_pymss_style_config(self, tmp_path) -> None:
        from krok_helper.audio_processing.separation.msst import _config_instruments

        path = tmp_path / "c.yaml"
        path.write_text(
            "training:\n  instruments:\n  - other\n  - vocals\n", encoding="utf-8"
        )
        assert _config_instruments(path) == ("other", "vocals")

    def test_msst_still_reads_indented_style(self, tmp_path) -> None:
        from krok_helper.audio_processing.separation.msst import _config_instruments

        path = tmp_path / "c.yaml"
        path.write_text(
            "training:\n  instruments:\n    - vocals\n    - other\n", encoding="utf-8"
        )
        assert _config_instruments(path) == ("vocals", "other")


class TestFolderOneClickImport:
    """一键导入：扫描文件夹并为三个任务各匹配一个模型。"""

    def _msst_tree(self, tmp_path):
        """最小 MSST-WebUI 结构：映射文件 + pretrain 权重 + configs 配置。"""
        import json as _json

        root = tmp_path / "MSST"
        (root / "data").mkdir(parents=True)
        (root / "configs").mkdir(parents=True)
        (root / "pretrain" / "vocal_models").mkdir(parents=True)

        (root / "configs" / "inst.yaml").write_text(
            "training:\n  instruments:\n  - other\n  - vocals\n", encoding="utf-8"
        )
        (root / "configs" / "kara.yaml").write_text(
            "training:\n  instruments:\n  - karaoke\n  - other\n", encoding="utf-8"
        )
        (root / "pretrain" / "vocal_models" / "inst_v1e.ckpt").write_bytes(b"w" * 64)
        (root / "pretrain" / "vocal_models" / "mel_band_roformer_karaoke_test.ckpt").write_bytes(b"w" * 64)
        (root / "data" / "msst_model_map.json").write_text(
            _json.dumps(
                {
                    "vocal_models": [
                        {
                            "name": "inst_v1e.ckpt",
                            "model_type": "mel_band_roformer",
                            "config_path": "configs/inst.yaml",
                        },
                        {
                            "name": "mel_band_roformer_karaoke_test.ckpt",
                            "model_type": "mel_band_roformer",
                            "config_path": "configs/kara.yaml",
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        return root

    def test_finds_msst_root_from_a_nested_folder(self, tmp_path) -> None:
        from krok_helper.audio_processing.separation.folder_import import find_msst_root

        root = self._msst_tree(tmp_path)
        assert find_msst_root(root / "pretrain" / "vocal_models") == root.resolve()
        assert find_msst_root(root / "pretrain") == root.resolve()

    def test_scans_msst_pretrain_and_matches_tasks(self, tmp_path) -> None:
        from krok_helper.audio_processing.separation.folder_import import (
            match_tasks,
            scan_folder,
        )
        from krok_helper.audio_processing.separation.states import TaskType

        root = self._msst_tree(tmp_path)
        candidates = scan_folder(root / "pretrain")
        matched = match_tasks(candidates, {TaskType.VOCAL: "inst_v1e"})

        assert matched[TaskType.VOCAL].display_name == "inst_v1e.ckpt"
        assert matched[TaskType.HARMONY].display_name == "mel_band_roformer_karaoke_test.ckpt"
        assert all(item.model_type == "mel_band_roformer" for item in matched.values())

    def test_only_returns_models_under_the_chosen_folder(self, tmp_path) -> None:
        """选了子目录就不该把同一 MSST 树里别处的模型也算进来。"""
        from krok_helper.audio_processing.separation.folder_import import scan_folder

        root = self._msst_tree(tmp_path)
        (root / "pretrain" / "other_models").mkdir()
        names = {
            item.display_name
            for item in scan_folder(root / "pretrain" / "other_models")
        }
        assert names == set()

    def test_prefers_the_recommended_model_when_several_fit(self, tmp_path) -> None:
        from krok_helper.audio_processing.separation.folder_import import (
            match_tasks,
            scan_folder,
        )
        from krok_helper.audio_processing.separation.states import TaskType

        root = self._msst_tree(tmp_path)
        candidates = scan_folder(root / "pretrain")
        vocal_options = [c for c in candidates if c.task is TaskType.VOCAL and c.bindable]
        assert len(vocal_options) >= 1
        matched = match_tasks(candidates, {TaskType.VOCAL: "inst_v1e"})
        assert matched[TaskType.VOCAL].display_name == "inst_v1e.ckpt"

    def test_missing_folder_raises(self, tmp_path) -> None:
        import pytest

        from krok_helper.audio_processing.separation.folder_import import scan_folder

        with pytest.raises(FileNotFoundError):
            scan_folder(tmp_path / "nope")
