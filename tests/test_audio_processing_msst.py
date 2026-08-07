"""MSST 模式：环境探测、桥接脚本、后端行为与页面选型。

这些用例不需要真实 MSST 安装——用构造出来的目录结构覆盖判定逻辑。真实环境上的
端到端验证在开发期单独跑过（见文档 §8.10 记录的耗时数据）。
"""

from __future__ import annotations

import json
import time

from krok_helper.audio_processing.separation.msst_env import (
    check_environment,
    find_python,
    locate_root,
)
from krok_helper.audio_processing.separation.msst_service import write_bridge
from krok_helper.audio_processing.separation.states import ServiceState, TaskType


def _msst_tree(tmp_path, *, with_map=True, with_infer=True):
    root = tmp_path / "MSST"
    (root / "workenv").mkdir(parents=True)
    (root / "workenv" / "python.exe").write_bytes(b"fake")
    (root / "utils").mkdir(parents=True)
    (root / "utils" / "constant.py").write_text("MODEL_TYPE = []\n", encoding="utf-8")
    if with_infer:
        (root / "inference").mkdir(parents=True)
        (root / "inference" / "msst_infer.py").write_text("", encoding="utf-8")
    if with_map:
        (root / "data").mkdir(parents=True)
        (root / "data" / "msst_model_map.json").write_text(
            json.dumps({"vocal_models": []}), encoding="utf-8"
        )
    (root / "pretrain" / "vocal_models").mkdir(parents=True)
    return root


def _backend(settings=None):
    from krok_helper.audio_processing.separation.msst_backend import (
        MsstSeparationBackend,
    )

    return MsstSeparationBackend(settings if settings is not None else {})


class TestEnvironmentDetection:
    def test_locates_root_from_the_root_itself(self, tmp_path) -> None:
        root = _msst_tree(tmp_path)
        assert locate_root(root) == root.resolve()

    def test_locates_root_from_a_subdirectory(self, tmp_path) -> None:
        """用户很可能选中 pretrain 而不是根目录。"""
        root = _msst_tree(tmp_path)
        assert locate_root(root / "pretrain" / "vocal_models") == root.resolve()

    def test_rejects_an_unrelated_directory(self, tmp_path) -> None:
        (tmp_path / "random").mkdir()
        assert locate_root(tmp_path / "random") is None

    def test_finds_bundled_interpreter(self, tmp_path) -> None:
        root = _msst_tree(tmp_path)
        assert find_python(root) == root / "workenv" / "python.exe"

    def test_missing_inference_module_is_reported(self, tmp_path) -> None:
        root = _msst_tree(tmp_path, with_infer=False)
        checks = check_environment(root)
        assert not all(ok for _n, ok, _d in checks)

    def test_missing_model_map_is_reported(self, tmp_path) -> None:
        root = _msst_tree(tmp_path, with_map=False)
        assert locate_root(root) == root.resolve()
        results = {name: ok for name, ok, _d in check_environment(root)}
        assert results["模型映射"] is False


class TestBridgeScript:
    def test_bridge_is_written_into_our_own_workdir(self, tmp_path) -> None:
        """§4.4：绝不能把脚本写进用户的 MSST 目录。"""
        work = tmp_path / "krok-work"
        path = write_bridge(work)
        assert path.parent == work
        assert path.name == "msst_bridge.py"

    def test_bridge_source_is_valid_python(self, tmp_path) -> None:
        path = write_bridge(tmp_path / "work")
        compile(path.read_text(encoding="utf-8"), str(path), "exec")

    def test_protocol_is_isolated_from_polluted_stdout(self, tmp_path) -> None:
        """MSST 的日志与 tqdm 会写 stdout，协议必须与之分离。"""
        source = write_bridge(tmp_path / "work").read_text(encoding="utf-8")
        assert "_protocol = sys.stdout" in source
        assert "sys.stdout = sys.stderr" in source

    def test_rewrite_is_idempotent(self, tmp_path) -> None:
        work = tmp_path / "work"
        first = write_bridge(work).read_text(encoding="utf-8")
        assert write_bridge(work).read_text(encoding="utf-8") == first


class TestMsstBackend:
    def test_starts_unconfigured_without_a_root(self) -> None:
        backend = _backend()
        try:
            assert backend.snapshot().state is ServiceState.UNCONFIGURED
        finally:
            backend.shutdown()

    def test_restores_a_saved_root(self, tmp_path) -> None:
        root = _msst_tree(tmp_path)
        backend = _backend({"msst_root": str(root)})
        try:
            assert backend.snapshot().state is ServiceState.INSTALLED_STOPPED
            assert backend.snapshot().install_dir == str(root.resolve())
        finally:
            backend.shutdown()

    def test_unbound_task_says_so(self, tmp_path) -> None:
        backend = _backend({"msst_root": str(_msst_tree(tmp_path))})
        try:
            dep = backend.snapshot().dependencies[TaskType.VOCAL]
            assert not dep.ready
            assert "选择 MSST 模型" in dep.reason
        finally:
            backend.shutdown()

    def test_missing_weight_file_is_reported(self, tmp_path) -> None:
        backend = _backend(
            {
                "msst_root": str(_msst_tree(tmp_path)),
                "msst_bindings": {
                    "vocal": {
                        "name": "gone.ckpt",
                        "model_type": "mel_band_roformer",
                        "model_path": str(tmp_path / "gone.ckpt"),
                        "config_path": "",
                        "stem": "vocals",
                    }
                },
            }
        )
        try:
            dep = backend.snapshot().dependencies[TaskType.VOCAL]
            assert not dep.ready
            assert "找不到模型文件" in dep.reason
        finally:
            backend.shutdown()

    def test_binding_without_a_stem_is_not_runnable(self, tmp_path) -> None:
        """轨名定不下来就不能跑，否则 MSST 会直接拒绝。"""
        weight = tmp_path / "m.ckpt"
        weight.write_bytes(b"w")
        backend = _backend(
            {
                "msst_root": str(_msst_tree(tmp_path)),
                "msst_bindings": {
                    "vocal": {
                        "name": "m.ckpt",
                        "model_type": "mel_band_roformer",
                        "model_path": str(weight),
                        "config_path": "",
                        "stem": "",
                    }
                },
            }
        )
        try:
            dep = backend.snapshot().dependencies[TaskType.VOCAL]
            assert not dep.ready
            assert "输出轨" in dep.reason
        finally:
            backend.shutdown()

    def test_download_is_not_offered_in_this_mode(self, tmp_path) -> None:
        backend = _backend({"msst_root": str(_msst_tree(tmp_path))})
        try:
            backend.start_model_download()
            snapshot = backend.snapshot()
            assert snapshot.state is ServiceState.ERROR
            assert "不下载模型" in snapshot.error
        finally:
            backend.shutdown()

    def test_removing_configuration_keeps_the_users_msst_intact(self, tmp_path) -> None:
        root = _msst_tree(tmp_path)
        settings = {"msst_root": str(root), "mode": "msst"}
        backend = _backend(settings)
        try:
            backend.remove_configuration()
            assert backend.snapshot().state is ServiceState.UNCONFIGURED
            assert "msst_root" not in settings
            assert (root / "inference" / "msst_infer.py").is_file(), "不得删改用户目录"
        finally:
            backend.shutdown()


class TestPageBackendSelection:
    def test_mode_selects_the_msst_backend(self) -> None:
        from krok_helper.audio_processing.separation.msst_backend import (
            MsstSeparationBackend,
        )
        from krok_helper.audio_processing.separation.page import AudioSeparationPage
        from krok_helper.settings import AppSettings

        settings = AppSettings()
        settings.pymss["mode"] = "msst"
        page = AudioSeparationPage(settings, lambda: None)
        try:
            assert isinstance(page._backend, MsstSeparationBackend)
        finally:
            page._backend.shutdown()

    def test_default_mode_keeps_the_pymss_backend(self) -> None:
        from krok_helper.audio_processing.separation.page import AudioSeparationPage
        from krok_helper.audio_processing.separation.real_backend import (
            RealSeparationBackend,
        )
        from krok_helper.settings import AppSettings

        page = AudioSeparationPage(AppSettings(), lambda: None)
        try:
            assert isinstance(page._backend, RealSeparationBackend)
        finally:
            page._backend.shutdown()


class TestWizardCopyMatchesTheMode:
    """MSST 流程里的文案不能写死 PyMSS。"""

    def test_environment_label_follows_the_flow(self) -> None:
        from krok_helper.audio_processing.separation.backend import (
            FLOW_EXISTING,
            FLOW_FULL,
            FLOW_MSST,
        )
        from krok_helper.audio_processing.separation.wizard import environment_label

        assert environment_label(FLOW_MSST) == "MSST"
        assert environment_label(FLOW_EXISTING) == "PyMSS"
        assert environment_label(FLOW_FULL) == "PyMSS"

    def test_states_shared_by_both_modes_do_not_name_pymss(self) -> None:
        """MSST 模式会经过这些状态，状态条文案必须两种模式都成立。"""
        from krok_helper.audio_processing.separation.states import STATE_META

        shared = [
            ServiceState.UNCONFIGURED,
            ServiceState.INSTALLED_STOPPED,
            ServiceState.SERVICE_STARTING,
            ServiceState.SERVICE_READY,
            ServiceState.SERVICE_STOPPING,
            ServiceState.MODEL_LOADING,
            ServiceState.PROCESSING,
            ServiceState.ERROR,
        ]
        for state in shared:
            meta = STATE_META[state]
            assert "PyMSS" not in meta.detail, f"{state.value} 的说明写死了 PyMSS"
            assert "PyMSS" not in meta.label, f"{state.value} 的标题写死了 PyMSS"


class TestSeparationProgress:
    """MSST 自身只把分块进度打到终端，工作台在桥接进程内替换 tqdm 取出来。"""

    def test_bridge_installs_a_progress_hook(self, tmp_path) -> None:
        source = write_bridge(tmp_path / "work").read_text(encoding="utf-8")
        assert "_install_progress_hook" in source
        assert "_mss_utils.tqdm = _ProgressBar" in source
        assert '"event": "progress"' in source

    def test_progress_is_reported_in_seconds(self, tmp_path) -> None:
        """面板用 format_elapsed 显示，单位必须是秒而不是样本数。"""
        source = write_bridge(tmp_path / "work").read_text(encoding="utf-8")
        assert "self.total / rate" in source
        assert 'sample_rate' in source

    def test_progress_never_exceeds_one_hundred_percent(self, tmp_path) -> None:
        """回归：demix 的 tqdm 建在补零之后，最后一块会冲过 total（实测 102%）。"""
        source = write_bridge(tmp_path / "work").read_text(encoding="utf-8")
        assert "(self.total * ratio) / rate" in source, "上报前必须按 ratio 截断"

    def test_progress_is_throttled(self, tmp_path) -> None:
        """每个分块都发会刷屏，必须节流。"""
        source = write_bridge(tmp_path / "work").read_text(encoding="utf-8")
        assert "0.005" in source and "0.3" in source


class TestCancellation:
    """取消必须立刻生效，不能排在正在跑的推理后面。"""

    class _FakeWorker:
        def __init__(self) -> None:
            self.stopped_force = None
            self.running = True

        def stop(self, timeout_seconds=5.0, *, force=False):
            self.stopped_force = force
            self.running = False
            return True

    def test_cancel_does_not_queue_behind_the_running_task(self, tmp_path) -> None:
        """回归：停止提交给了只有一个工作线程的任务执行器，任务跑完才真的停。"""
        import threading

        backend = _backend({"msst_root": str(_msst_tree(tmp_path))})
        worker = self._FakeWorker()
        backend._worker = worker
        try:
            # 占满任务执行器，模拟正在跑的推理
            blocking = threading.Event()
            backend._submit(lambda: blocking.wait(30))

            backend.cancel_task()
            for _ in range(200):  # 最多等 2 秒
                if worker.stopped_force is not None:
                    break
                time.sleep(0.01)
            assert worker.stopped_force is True, "取消时应强制终止，且不等任务执行器空闲"
        finally:
            blocking.set()
            backend.shutdown()

    def test_cancel_clears_the_queue(self, tmp_path) -> None:
        backend = _backend({"msst_root": str(_msst_tree(tmp_path))})
        backend._worker = self._FakeWorker()
        try:
            backend._task_queue = [TaskType.INSTRUMENTAL, TaskType.HARMONY]
            backend._queue_active = True
            backend.cancel_task()
            assert backend._task_queue == []
            assert backend.snapshot().queued_tasks == ()
            assert backend.snapshot().pending_task is None
        finally:
            backend.shutdown()

    def test_force_stop_skips_the_graceful_handshake(self, tmp_path) -> None:
        """桥接正卡在推理里不会读 stdin，礼貌关闭只会白等一个超时。"""
        source = write_bridge(tmp_path / "work").read_text(encoding="utf-8")
        assert "action" in source  # 协议里仍保留正常关闭
        from krok_helper.audio_processing.separation import msst_service

        import inspect

        stop_src = inspect.getsource(msst_service.MsstWorker.stop)
        assert "force" in stop_src
        assert "if not force" in stop_src, "force 时必须跳过 shutdown 握手"


class TestPyMSSCancellationIsDetached:
    def test_cancel_task_stops_off_the_task_executor(self) -> None:
        """PyMSS 侧同样不能让停止排在任务后面。"""
        import inspect

        from krok_helper.audio_processing.separation.real_backend import (
            RealSeparationBackend,
        )

        source = inspect.getsource(RealSeparationBackend.cancel_task)
        assert "detached=True" in source
