"""AI 打轴宿主能力（KaraokeAiTimingHost）测试。

验证 SUG AiTimingHost 协议（EMBEDDING.md §6）的工作台实现：
- 协议鸭子类型完整；
- separation_status 跟随后端状态；
- find_session_vocal 严格匹配本会话人声（不猜相似名）；
- separate_vocal：环境未就绪阻断、成功返回产物、失败/取消转中文异常。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PyQt6.QtCore import QObject, pyqtSignal

from krok_helper.audio_processing.separation.ai_timing_host import (
    AiTimingHostError,
    KaraokeAiTimingHost,
)
from krok_helper.audio_processing.separation.backend import (
    ResultFile,
    SeparationSnapshot,
    TaskProgress,
    TaskResult,
)
from krok_helper.audio_processing.separation.states import (
    ServiceState,
    TaskType,
)


class _StubBackend(QObject):
    """最小分离后端：手动控制快照与结果。"""

    resultReady = pyqtSignal(object)
    taskProgressChanged = pyqtSignal(object)
    snapshotChanged = pyqtSignal(object)

    def __init__(self, state=ServiceState.SERVICE_READY, model="inst_v1e"):
        super().__init__()
        self._snap = SeparationSnapshot(state=state, current_model=model)
        self.requests = []
        self.cancelled = False
        self._next_result = None
        self.install_dir = ""
        self.started_service = False
        self.stopped_service = False

    def start_service(self):
        self.started_service = True
        self._snap.state = ServiceState.SERVICE_READY

    def stop_service(self):
        self.stopped_service = True
        self._snap.pending_task = None
        self._snap.state = ServiceState.INSTALLED_STOPPED

    def snapshot(self):
        return SeparationSnapshot(
            state=self._snap.state,
            current_model=self._snap.current_model,
            pending_task=self._snap.pending_task,
            error=self._snap.error,
            install_dir=self.install_dir,
        )

    def request_task(self, task, *, input_path, output_dir, output_format):
        self.requests.append((task, input_path, output_dir, output_format))
        if self._next_result is not None:
            result, self._next_result = self._next_result, None
            self.taskProgressChanged.emit(
                TaskProgress(title="分离人声", stage_name="分离处理中",
                             processing_done=5, processing_total=10)
            )
            self.resultReady.emit(result)

    def cancel_task(self):
        self.cancelled = True
        self._snap.pending_task = None


def _host(tmp_path, backend):
    return KaraokeAiTimingHost(backend, tmp_path / "lyrics_timing_cache")


def _record(task, *files):
    return TaskResult(
        task=task,
        title="分离人声",
        finished_at="12:00:00",
        files=[ResultFile(path=str(f), label="人声") for f in files],
    )


@pytest.fixture
def qapp():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def media(tmp_path):
    source = tmp_path / "song.flac"
    source.write_bytes(b"mix")
    return source


class TestProtocolShape:
    def test_satisfies_sug_protocol_duck_type(self, tmp_path, qapp):
        """全部协议方法存在（与 SUG is_ai_timing_host 同口径）。"""
        host = _host(tmp_path, _StubBackend())
        for name in (
            "separation_status",
            "effective_identity",
            "find_session_vocal",
            "separate_vocal",
            "ai_cache_dir",
            "runtime_python",
            "stop_separation_service",
        ):
            assert callable(getattr(host, name)), name


class TestRuntimePython:
    """方案 B：宿主托管 Runtime 解释器发现（SUG 增量安装用）。"""

    def test_returns_managed_python_when_installed(self, tmp_path, qapp):
        install_dir = tmp_path / "rt"
        (install_dir / "runtime").mkdir(parents=True)
        exe = install_dir / "runtime" / "python.exe"
        exe.write_text("#fake", encoding="utf-8")
        backend = _StubBackend()
        backend.install_dir = str(install_dir)
        assert _host(tmp_path, backend).runtime_python() == str(exe)

    def test_relative_setting_end_to_end_real_backend(
        self, tmp_path, qapp, monkeypatch
    ):
        """相对化口径端到端：settings 存相对 ai_runtime，真实后端还原为
        绝对路径后，宿主仍能把 python.exe 传给 SUG（方案 B 链路不断）。"""
        import sys as _sys

        from krok_helper.audio_processing.separation.real_backend import (
            RealSeparationBackend,
        )

        base = tmp_path / "app"
        exe_file = base / "KaraokeStudio.exe"
        exe_file.parent.mkdir(parents=True)
        exe_file.write_bytes(b"fake-exe")
        monkeypatch.setattr(_sys, "frozen", True, raising=False)
        monkeypatch.setattr("sys.executable", str(exe_file))

        from tests.test_audio_processing_real_backend import _installed_runtime

        _installed_runtime(base / "ai_runtime")
        settings = {"install_dir": "ai_runtime"}  # 新口径相对路径
        backend = RealSeparationBackend(settings)
        try:
            host = _host(tmp_path, backend)
            assert host.runtime_python() == str(
                base / "ai_runtime" / "runtime" / "python.exe"
            )
        finally:
            backend.shutdown()

    def test_msst_style_backend_without_install_dir_returns_none(
        self, tmp_path, qapp
    ):
        """MSST/外部环境模式：快照无 install_dir（属性缺失/为空）时
        返回 None——SUG 走「blocked」引导而不是拿到坏路径。"""
        backend = _StubBackend(state=ServiceState.EXTERNAL_MODEL_READY)
        host = _host(tmp_path, backend)
        assert host.runtime_python() is None
        # 分离能力本身仍可用（跟随工作台 MSST 设置）
        status = host.separation_status()
        assert status["available"] is True

    def test_note_runtime_changed_delegates_to_backend(
        self, tmp_path, qapp
    ):
        """（可选协议）后端无该能力时返回 False 不抛；有则透传结果。"""
        host = _host(tmp_path, _StubBackend())
        assert host.note_runtime_changed() is False  # 旧版/模拟后端

        from krok_helper.audio_processing.separation.real_backend import (
            RealSeparationBackend,
        )
        from tests.test_audio_processing_real_backend import _installed_runtime

        root = tmp_path / "managed"
        _installed_runtime(root)
        (root / "runtime" / "python.exe").write_bytes(b"mutated")
        backend = RealSeparationBackend({"install_dir": str(root)})
        try:
            healed_host = _host(tmp_path, backend)
            assert healed_host.note_runtime_changed() is True
        finally:
            backend.shutdown()

    def test_none_when_not_installed(self, tmp_path, qapp):
        assert _host(tmp_path, _StubBackend()).runtime_python() is None

    def test_none_when_executable_missing(self, tmp_path, qapp):
        backend = _StubBackend()
        backend.install_dir = str(tmp_path / "empty")
        assert _host(tmp_path, backend).runtime_python() is None


class TestSeparationStatus:
    def test_ready_backend_available(self, tmp_path, qapp):
        status = _host(tmp_path, _StubBackend()).separation_status()
        assert status["available"] is True
        assert status["model"] == "inst_v1e"

    def test_busy_backend_unavailable(self, tmp_path, qapp):
        backend = _StubBackend()
        backend._snap.pending_task = TaskType.VOCAL
        status = _host(tmp_path, backend).separation_status()
        assert status["available"] is False
        assert "任务" in status["message"]

    def test_unconfigured_unavailable(self, tmp_path, qapp):
        backend = _StubBackend(state=ServiceState.UNCONFIGURED)
        backend._snap.error = "未配置安装目录"
        status = _host(tmp_path, backend).separation_status()
        assert status["available"] is False
        assert "未配置" in status["message"]


class TestStopSeparationService:
    """方案 B 配套：SUG 增量安装前腾出共享解释器（停服务释放文件锁）。"""

    def test_busy_task_refuses_without_stopping(self, tmp_path, qapp):
        backend = _StubBackend()
        backend._snap.pending_task = TaskType.VOCAL
        result = _host(tmp_path, backend).stop_separation_service(timeout_s=2)
        assert result["stopped"] is False
        assert "任务" in result["message"]
        assert backend.stopped_service is False

    def test_running_service_stops(self, tmp_path, qapp):
        backend = _StubBackend(state=ServiceState.SERVICE_READY)
        result = _host(tmp_path, backend).stop_separation_service(timeout_s=2)
        assert result["stopped"] is True
        assert backend.stopped_service is True

    def test_idle_service_returns_without_stop_call(self, tmp_path, qapp):
        backend = _StubBackend(state=ServiceState.INSTALLED_STOPPED)
        result = _host(tmp_path, backend).stop_separation_service(timeout_s=2)
        assert result["stopped"] is True
        assert backend.stopped_service is False

    def test_stop_failure_reports_backend_error(self, tmp_path, qapp):
        backend = _StubBackend()

        def _fail_stop():
            backend._snap.state = ServiceState.ERROR
            backend._snap.error = "桥接进程退出失败"

        backend.stop_service = _fail_stop
        result = _host(tmp_path, backend).stop_separation_service(timeout_s=2)
        assert result["stopped"] is False
        assert "桥接进程退出失败" in result["message"]

    def test_stop_timeout_reports_chinese_message(self, tmp_path, qapp):
        backend = _StubBackend()
        backend.stop_service = lambda: None  # 状态停留在 SERVICE_READY
        result = _host(tmp_path, backend).stop_separation_service(timeout_s=1)
        assert result["stopped"] is False
        assert "超时" in result["message"]


class TestSessionVocal:
    def test_session_vocal_strict_match(self, tmp_path, qapp, media):
        backend = _StubBackend()
        host = _host(tmp_path, backend)
        vocal = media.parent / "song_人声.wav"
        vocal.write_bytes(b"v")
        host.record_result(_record(TaskType.VOCAL, vocal))

        found = host.find_session_vocal(media, "any-sha")
        assert found == vocal

    def test_session_vocal_rejects_loose_names(self, tmp_path, qapp, media):
        backend = _StubBackend()
        host = _host(tmp_path, backend)
        noise = media.parent / "song_人声演唱会.wav"
        noise.write_bytes(b"x")
        other = media.parent / "other_人声.wav"
        other.write_bytes(b"x")
        host.record_result(_record(TaskType.VOCAL, noise, other))

        assert host.find_session_vocal(media, "sha") is None

    def test_session_vocal_ignores_missing_file(self, tmp_path, qapp, media):
        backend = _StubBackend()
        host = _host(tmp_path, backend)
        host.record_result(
            _record(TaskType.VOCAL, media.parent / "song_人声.wav")
        )
        # 文件已不存在 → 不命中
        assert host.find_session_vocal(media, "sha") is None


class TestSeparateVocal:
    def test_not_available_blocks_with_chinese_error(self, tmp_path, qapp, media):
        backend = _StubBackend(state=ServiceState.UNCONFIGURED)
        with pytest.raises(AiTimingHostError, match="音频分离"):
            _host(tmp_path, backend).separate_vocal(media, lambda *a: None, lambda: False)

    def test_success_returns_vocal_wav(self, tmp_path, qapp, media):
        backend = _StubBackend()
        vocal = media.parent / "song_人声.wav"
        vocal.write_bytes(b"v")
        backend._next_result = _record(TaskType.VOCAL, vocal)
        host = _host(tmp_path, backend)

        progress_events = []
        result = host.separate_vocal(
            media,
            lambda s, p, m: progress_events.append((s, p, m)),
            lambda: False,
        )
        assert result == vocal
        # 只调用一次现有分离任务（验收门槛 §11-G）
        assert len(backend.requests) == 1
        task, input_path, output_dir, fmt = backend.requests[0]
        assert task == TaskType.VOCAL
        assert input_path == str(media)
        assert output_dir == str(media.parent)
        assert fmt == "wav"
        assert progress_events and progress_events[0][0] == "separation"
        # 分离产物进入会话记录：下次 find_session_vocal 零分离命中
        assert host.find_session_vocal(media, "sha") == vocal

    def test_failed_task_raises_error(self, tmp_path, qapp, media):
        backend = _StubBackend()
        backend._next_result = TaskResult(
            task=TaskType.VOCAL, title="分离人声", finished_at="12:00:00", error="推理失败"
        )
        with pytest.raises(AiTimingHostError, match="推理失败"):
            _host(tmp_path, backend).separate_vocal(media, lambda *a: None, lambda: False)

    def test_no_wav_output_raises(self, tmp_path, qapp, media):
        backend = _StubBackend()
        backend._next_result = _record(TaskType.VOCAL, media.parent / "song_人声.flac")
        with pytest.raises(AiTimingHostError, match="未找到输出的人声文件"):
            _host(tmp_path, backend).separate_vocal(media, lambda *a: None, lambda: False)


class TestCacheDir:
    def test_model_root_is_shared_location(self, tmp_path, qapp, monkeypatch):
        """宿主提供统一模型根（对齐模型与分离模型同源管理）。"""
        from krok_helper import settings as settings_module

        class _P:
            def __init__(self, p):
                self.parent = p

        monkeypatch.setattr(
            settings_module, "get_settings_path", lambda: _P(tmp_path)
        )
        host = _host(tmp_path, _StubBackend())
        assert host.model_root() == tmp_path / "ai_models"

    def test_cache_dir_under_host_root(self, tmp_path, qapp):
        host = _host(tmp_path, _StubBackend())
        assert host.ai_cache_dir() == tmp_path / "lyrics_timing_cache" / "ai_timing"


class TestSugIntegration:
    def test_sug_service_uses_host_pieces(self, tmp_path, qapp, media):
        """SUG AiTimingService 端到端（fake worker）：embedded 宿主注入后
        会话人声零分离完成整条链路。"""
        import sys

        sug_src = Path(__file__).resolve().parent.parent / "krok_helper" / "lyrics_timing" / "src"
        if not sug_src.is_dir():
            pytest.skip("SUG submodule 未初始化")
        sys.path.insert(0, str(sug_src))
        try:
            from strange_uta_game.backend.application.ai_timing.host import (
                is_ai_timing_host,
            )
            from strange_uta_game.backend.application.ai_timing.models import (
                ModelManifest,
                ModelRegistry,
            )
            from strange_uta_game.backend.application.ai_timing.resolver import (
                PronunciationResolver,
            )
            from strange_uta_game.backend.application.ai_timing.service import (
                AiTimingService,
            )
            from strange_uta_game.backend.application.ai_timing.settings import (
                AiTimingSettings,
            )
            from strange_uta_game.backend.application.ai_timing.vocals import (
                AiCache,
                VocalPreparationService,
            )
            from strange_uta_game.backend.domain import (
                Character,
                Project,
                Sentence,
            )
            from strange_uta_game.backend.infrastructure.parsers.ruby_analyzer import (
                DummyAnalyzer,
            )
        finally:
            sys.path.remove(str(sug_src))

        backend = _StubBackend()
        host = _host(tmp_path, backend)
        assert is_ai_timing_host(host)

        vocal = media.parent / "song_人声.wav"
        vocal.write_bytes(b"v")
        host.record_result(_record(TaskType.VOCAL, vocal))

        project = Project()
        project.sentences = [
            Sentence(
                singer_id="s1",
                characters=[
                    Character(char="あ", check_count=1, ruby=None, singer_id="s1")
                ],
            )
        ]

        class _FakeWorker:
            calls = []

            def run(self, request, audio_path, model_spec, on_progress=None, timeout_s=None):
                _FakeWorker.calls.append(audio_path)
                from strange_uta_game.backend.application.ai_timing.alignment import (
                    AlignmentResult,
                    EmissionSpan,
                )

                return AlignmentResult(
                    annotation_digest=request.annotation_digest,
                    model_id="fake",
                    spans=[
                        EmissionSpan(t.index, i * 100, i * 100 + 50)
                        for i, t in enumerate(request.tokens)
                    ],
                )

        cache = AiCache(host.ai_cache_dir())
        registry = ModelRegistry(tmp_path / "models")
        registry.register(
            ModelManifest(
                model_id="NextFire/mms-300m-ForcedAligner-karaoke-ja-Latn",
                provider="wav2vec2",
                revision="main",
            )
        )
        service = AiTimingService(
            settings=AiTimingSettings(),
            cache=cache,
            registry=registry,
            vocal_service=VocalPreparationService(
                cache, session_vocal_finder=host.find_session_vocal
            ),
            resolver=PronunciationResolver(analyzer=DummyAnalyzer(), chinese_mode=False),
            worker_factory=lambda python: _FakeWorker(),
            separation_executor=host.separate_vocal,
            separation_identity=host.effective_identity,
        )
        cmd = service.execute(project, str(media))
        assert cmd is not None
        # 会话人声直接复用：没有发起分离任务
        assert backend.requests == []
        # worker 用的是人声文件
        assert _FakeWorker.calls == [str(vocal)]


class TestReviewFixes:
    """2026-08 提交前代码审查修复的回归测试。"""

    def test_dynamic_backend_getter_survives_mode_swap(self, tmp_path, qapp, media):
        """宿主持 getter：分离页切换后端（PyMSS↔MSST）后状态取新实例。"""
        backends = [_StubBackend()]
        host = KaraokeAiTimingHost(lambda: backends[-1], tmp_path / "cache")
        assert host.separation_status()["available"] is True

        # 模式切换：换成一个未就绪的新后端实例
        swapped = _StubBackend(state=ServiceState.UNCONFIGURED)
        swapped._snap.error = "未配置"
        backends.append(swapped)
        assert host.separation_status()["available"] is False

    def test_sync_failure_does_not_hang_wait_loop(self, tmp_path, qapp, media):
        """request_task 同步失败（只置 ERROR 不发 resultReady）不空转。"""
        backend = _StubBackend()
        host = KaraokeAiTimingHost(backend, tmp_path / "cache")

        def _fail_sync(task, *, input_path, output_dir, output_format):
            backend.requests.append((task, input_path, output_dir, output_format))
            backend._snap.pending_task = None
            backend._snap.error = "PyMSS 服务尚未启动。"
            backend._snap.state = ServiceState.ERROR
            backend.snapshotChanged.emit(backend.snapshot())

        backend.request_task = _fail_sync
        with pytest.raises(AiTimingHostError, match="尚未启动"):
            host.separate_vocal(media, lambda *a: None, lambda: False)

    def test_backend_swap_during_wait_aborts(self, tmp_path, qapp, media):
        """等待分离期间后端被整体替换 → 明确报错而非挂死。"""
        backends = [_StubBackend()]
        host = KaraokeAiTimingHost(lambda: backends[-1], tmp_path / "cache")

        def _slow_task(task, *, input_path, output_dir, output_format):
            backends.append(_StubBackend())  # 等待期间后端被替换

        backends[0].request_task = _slow_task
        with pytest.raises(AiTimingHostError, match="已切换"):
            host.separate_vocal(media, lambda *a: None, lambda: False)


class TestServiceAutoStart:
    """已配置但服务未运行（INSTALLED_STOPPED）是正常待机态：可用、执行时自动拉起。"""

    def test_installed_stopped_counts_available(self, tmp_path, qapp):
        backend = _StubBackend(state=ServiceState.INSTALLED_STOPPED)
        status = _host(tmp_path, backend).separation_status()
        assert status["available"] is True
        assert "自动启动" in status["message"]

    def test_service_starting_counts_available(self, tmp_path, qapp):
        backend = _StubBackend(state=ServiceState.SERVICE_STARTING)
        status = _host(tmp_path, backend).separation_status()
        assert status["available"] is True

    def test_separate_auto_starts_stopped_service(self, tmp_path, qapp, media):
        backend = _StubBackend(state=ServiceState.INSTALLED_STOPPED)
        vocal = media.with_name(media.stem + "_人声.wav")
        vocal.write_bytes(b"v")
        backend._next_result = _record(TaskType.VOCAL, vocal)
        host = _host(tmp_path, backend)
        out = host.separate_vocal(media, lambda *a: None, lambda: False)
        assert out == vocal
        assert backend.started_service is True

    def test_start_failure_raises_backend_error(self, tmp_path, qapp, media):
        backend = _StubBackend(state=ServiceState.INSTALLED_STOPPED)

        def _fail_start():
            backend.started_service = True
            backend._snap.state = ServiceState.ERROR
            backend._snap.error = "运行时校验失败"

        backend.start_service = _fail_start
        host = _host(tmp_path, backend)
        with pytest.raises(AiTimingHostError, match="运行时校验失败"):
            host.separate_vocal(media, lambda *a: None, lambda: False)


class TestOpenSeparationPage:
    """SUG AI 打轴引导「去音频分离」的页面跳转能力。"""

    def test_navigate_called_and_returns_true(self, tmp_path, qapp):
        calls = []

        def _nav():
            calls.append(1)

        host = KaraokeAiTimingHost(
            _StubBackend(), tmp_path / "cache", navigate=_nav
        )
        assert host.open_separation_page() is True
        assert calls == [1]

    def test_without_navigate_returns_false(self, tmp_path, qapp):
        assert _host(tmp_path, _StubBackend()).open_separation_page() is False

    def test_navigate_exception_returns_false(self, tmp_path, qapp):
        def _boom():
            raise RuntimeError("nav failed")

        host = KaraokeAiTimingHost(
            _StubBackend(), tmp_path / "cache", navigate=_boom
        )
        assert host.open_separation_page() is False
