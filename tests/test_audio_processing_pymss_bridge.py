"""PyMSS 去 HTTP 桥接：脚本形态与引擎接口。

真实环境上的端到端验证在开发期单独跑过（catalog 326 条、配置解析、真实分离）；
这里覆盖不依赖安装的部分。
"""

from __future__ import annotations

import inspect

from krok_helper.audio_processing.separation.client import PyMSSClient
from krok_helper.audio_processing.separation.pymss_service import (
    PyMSSBridgeEngine,
    PyMSSWorker,
    write_bridge,
)


class TestBridgeScript:
    def test_written_into_our_own_workdir(self, tmp_path) -> None:
        path = write_bridge(tmp_path / "work")
        assert path.parent == tmp_path / "work"
        assert path.name == "pymss_bridge.py"

    def test_source_is_valid_python(self, tmp_path) -> None:
        path = write_bridge(tmp_path / "work")
        compile(path.read_text(encoding="utf-8"), str(path), "exec")

    def test_protocol_is_isolated_from_pymss_logging(self, tmp_path) -> None:
        source = write_bridge(tmp_path / "work").read_text(encoding="utf-8")
        assert "_protocol = sys.stdout" in source
        assert "sys.stdout = sys.stderr" in source

    def test_uses_the_public_separator_class(self, tmp_path) -> None:
        """比补丁 pymss.server.app._run_separation_sync（私有）耦合更小。"""
        source = write_bridge(tmp_path / "work").read_text(encoding="utf-8")
        assert "from pymss import MSSeparator" in source
        assert "_run_separation_sync" not in source

    def test_covers_catalog_and_download_actions(self, tmp_path) -> None:
        """不起 HTTP 后，目录与下载也要由桥接承担。"""
        source = write_bridge(tmp_path / "work").read_text(encoding="utf-8")
        assert '"catalog": _catalog' in source
        assert '"download": _download' in source

    def test_writes_only_the_requested_stem(self, tmp_path) -> None:
        source = write_bridge(tmp_path / "work").read_text(encoding="utf-8")
        assert "separator.store_dirs = {stem: output_dir}" in source

    def test_rejects_a_stem_the_model_does_not_declare(self, tmp_path) -> None:
        source = write_bridge(tmp_path / "work").read_text(encoding="utf-8")
        assert "instruments" in source and "没有名为" in source

    def test_progress_is_throttled(self, tmp_path) -> None:
        source = write_bridge(tmp_path / "work").read_text(encoding="utf-8")
        assert "0.005" in source and "0.3" in source

    def test_rewrite_is_idempotent(self, tmp_path) -> None:
        work = tmp_path / "work"
        first = write_bridge(work).read_text(encoding="utf-8")
        assert write_bridge(work).read_text(encoding="utf-8") == first


class TestEngineIsADropInForTheHttpClient:
    """后端只用到 client 的 8 个方法；桥接引擎同名同形才能少改调用点。"""

    _SHARED = (
        "health",
        "catalog_model",
        "catalog_models",
        "model_config_text",
        "download_model",
    )

    def test_shares_the_client_method_names(self) -> None:
        for name in self._SHARED:
            assert hasattr(PyMSSClient, name), f"客户端应有 {name}"
            assert hasattr(PyMSSBridgeEngine, name), f"桥接引擎缺少 {name}"

    def test_replaces_pcm_upload_with_a_file_path_call(self) -> None:
        """separate_pcm 正是要去掉的整段 PCM 传输，桥接改走文件路径。"""
        assert hasattr(PyMSSClient, "separate_pcm")
        assert not hasattr(PyMSSBridgeEngine, "separate_pcm")
        assert hasattr(PyMSSBridgeEngine, "separate_file")

        signature = inspect.signature(PyMSSBridgeEngine.separate_file)
        for expected in ("model", "stem", "input_path", "output_dir"):
            assert expected in signature.parameters

    def test_health_reflects_the_worker_state(self) -> None:
        class _Worker:
            running = False

        engine = PyMSSBridgeEngine(_Worker())
        assert engine.health()["status"] == "down"
        _Worker.running = True
        assert PyMSSBridgeEngine(_Worker()).health()["status"] == "ok"


class TestCancellation:
    def test_force_stop_skips_the_graceful_handshake(self) -> None:
        """推理不可中断，桥接不会读 stdin，礼貌关闭只会白等一个超时。"""
        source = inspect.getsource(PyMSSWorker.stop)
        assert "force" in source
        assert "if not force" in source
