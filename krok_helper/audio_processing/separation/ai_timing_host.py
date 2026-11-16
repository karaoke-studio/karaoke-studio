"""AI 打轴宿主能力（工作台 → SUG 嵌入注入，阶段 G）。

实现 SUG 的 AiTimingHost 协议（纯鸭子类型，见 SUG 仓库
docs/EMBEDDING.md §6）：把工作台现有的人声分离后端适配成 SUG
「AI 打轴」可用的能力——会话人声零分离复用、缺人声时调用一次
现有分离任务、AI 缓存目录注入。

不复制 PyMSS Runtime、不复制模型、不保存第二套分离参数；
分离始终跟随工作台当前「分离人声」设置（backend 自身状态即真相）。

后端通过 ``backend_getter`` **动态解析**：分离页在 PyMSS/MSST 模式切换
时会整体替换 ``self._backend``，构造期捕获的引用会变陈旧（状态过期、
信号失联），因此每次操作前重新取当前后端。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

from krok_helper.audio_processing.separation.backend import SeparationBackend
from krok_helper.audio_processing.separation.states import (
    ServiceState,
    TaskType,
)

_VOCAL_SUFFIX = "_人声"
_VOCAL_EXTENSIONS = (".wav", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".aac", ".wma")


def _ailog(message: str) -> None:
    """把宿主侧分离事件写进 SUG 的 ai_timing.log（与 SUG 同一文件）。

    优先复用 SUG 的 ailog（同路径、同格式、轮转一致）；SUG 不可导入
    （独立测试/旧版嵌入）时按同格式直接追加到工作台数据目录的
    ``lyrics_timing_logs``。任何失败静默——日志不能反过来打断业务。
    """
    try:
        from strange_uta_game.backend.application.ai_timing.ailog import (
            ailog as _sug_ailog,
        )

        _sug_ailog("host", message)
        return
    except Exception:
        pass
    try:
        import os
        from datetime import datetime

        from krok_helper.settings import get_settings_path

        path = get_settings_path().parent / "lyrics_timing_logs" / "ai_timing.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [{os.getpid()}·host] {message}"
        with path.open("a", encoding="utf-8", errors="replace") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


class AiTimingHostError(RuntimeError):
    """宿主能力错误（中文消息，SUG 直接展示）。"""


class KaraokeAiTimingHost:
    """把分离后端适配为 SUG AiTimingHost 协议。

    Args:
        backend_getter: 返回**当前**分离后端的 callable（模式切换后端
            实例会被替换，不能在构造期钉住引用）；也接受后端实例本身
            （测试/固定后端场景）。
        cache_root: AI 缓存根目录（宿主注入给 SUG 的 ``.cache`` 范围）。
    """

    def __init__(self, backend_getter, cache_root: Path, page=None, navigate=None):
        self._backend_getter = (
            backend_getter if callable(backend_getter) else (lambda: backend_getter)
        )
        self._cache_root = Path(cache_root)
        # 分离页引用：http_proxy 需要读工作台网络设置（可为 None）
        self._page_ref = page
        # 页面跳转回调（SUG AI 打轴引导「去音频分离」用；可为 None）
        self._navigate = navigate
        self._session_results: list = []
        self._lock = threading.Lock()

    #: 等待分离结果期间无任何进展（进度/结果/状态信号）的上限：正常
    #: 静默窗口（模型下载有进度、Runtime 全量校验哈希数 GB）远小于此；
    #: 超过即视为宿主分离链路僵死，主动取消并报错，不再永久挂起
    _STALL_TIMEOUT_S = 600.0

    #: 快照轮询周期（每 N 次 0.2s 等待循环读一次快照）
    _SNAPSHOT_POLL_EVERY = 5

    #: 轮询发现这些状态时挂起任务不可能再有结果（后端 B1 已在进入
    #: 这些终态时补发失败结果，此处兜底覆盖未知/旧实现路径）
    _HARD_PARKED_STATES = frozenset(
        {
            ServiceState.EXTERNAL_MODEL_UNSUPPORTED,
            ServiceState.EXTERNAL_OFFLINE,
            ServiceState.INSTALL_DAMAGED,
            ServiceState.INSTALL_MISSING,
            ServiceState.VERSION_INCOMPATIBLE,
            ServiceState.EXTERNAL_VERSION_INCOMPATIBLE,
        }
    )

    @property
    def _backend(self) -> SeparationBackend:
        return self._backend_getter()

    # ── 协议实现 ──

    def separation_status(self) -> dict:
        snap = self._backend.snapshot()
        if snap.pending_task or getattr(snap, "queued_tasks", None):
            return {
                "available": False,
                # SUG（d5edb9fb 起）据此不再回落内置分离，而是提示用户
                # 等宿主空闲后重试；不识别该字段的旧 SUG 行为不变
                "busy": True,
                "model": str(snap.current_model or ""),
                "message": "分离环境正在执行任务，请稍候",
            }
        # INSTALLED_STOPPED / SERVICE_STARTING 也算可用：环境已配置，
        # separate_vocal 会在执行前自动拉起/等待服务（配置好但服务
        # 未运行是正常待机态，此前被误报为「未就绪」）
        available = snap.state in (
            ServiceState.SERVICE_READY,
            ServiceState.EXTERNAL_MODEL_READY,
            ServiceState.INSTALLED_STOPPED,
            ServiceState.SERVICE_STARTING,
        )
        message = ""
        if not available:
            message = snap.error or "分离环境未就绪"
        elif snap.state in (
            ServiceState.INSTALLED_STOPPED,
            ServiceState.SERVICE_STARTING,
        ):
            message = "已配置（服务未运行，执行 AI 打轴时自动启动）"
        return {
            "available": bool(available),
            "model": str(snap.current_model or ""),
            "message": message,
        }

    def effective_identity(self) -> dict:
        snap = self._backend.snapshot()
        return {
            "model": str(snap.current_model or "unknown"),
            "stem": "人声",
            "params": {},
        }

    def find_session_vocal(self, source_path, media_sha256: str):
        """本次会话已分离、与原音频严格匹配的人声。

        依据后端 resultReady 记录的本会话产物：严格 ``<stem>_人声.<ext>``
        命名且位于原音频同目录；不猜相似名称（计划 §6.1）。
        """
        source = Path(source_path)
        with self._lock:
            results = list(self._session_results)
        for result in reversed(results):
            if getattr(result, "task", None) not in (TaskType.VOCAL, None):
                continue
            for f in getattr(result, "files", []) or []:
                p = Path(f.path)
                if (
                    p.parent == source.parent
                    and p.stem == source.stem + _VOCAL_SUFFIX
                    and p.suffix.lower() in _VOCAL_EXTENSIONS
                    and p.is_file()
                ):
                    return p
        return None

    _SERVICE_START_TIMEOUT_S = 300.0

    def _ensure_service_ready(self, on_progress, is_cancelled) -> None:
        """确保分离服务 READY：已安装未运行时自动拉起并等待就绪。

        工作台配置好环境后服务通常处于 INSTALLED_STOPPED 待机态；
        这里按需 ``start_service()``（自包含异步流程：校验→拉起→健康
        检查），轮询快照直到 READY / 失败 / 超时。
        """
        deadline = time.monotonic() + self._SERVICE_START_TIMEOUT_S
        start_attempted = False
        while True:
            if is_cancelled():
                raise AiTimingHostError("已取消人声分离")
            snap = self._backend.snapshot()
            if snap.pending_task:
                raise AiTimingHostError("分离环境正在执行任务，请稍候")
            state = snap.state
            if state in (
                ServiceState.SERVICE_READY,
                ServiceState.EXTERNAL_MODEL_READY,
            ):
                return
            if state is ServiceState.SERVICE_STARTING:
                on_progress("vocal", 10, "正在启动工作台分离服务…")
            elif state is ServiceState.INSTALLED_STOPPED and not start_attempted:
                on_progress("vocal", 8, "工作台分离服务未运行，正在自动启动…")
                _ailog("分离服务未运行，separate_vocal 自动拉起服务")
                self._backend.start_service()
                start_attempted = True
            else:
                raise AiTimingHostError(
                    snap.error
                    or f"工作台分离环境不可用（{getattr(state, 'value', state)}），"
                    "请到第 2 步「音频分离」页检查"
                )
            if time.monotonic() > deadline:
                _ailog(
                    f"自动拉起分离服务超时（{self._SERVICE_START_TIMEOUT_S}s）"
                )
                raise AiTimingHostError(
                    "启动工作台分离服务超时，请到第 2 步「音频分离」页检查环境"
                )
            time.sleep(0.2)

    def separate_vocal(self, source_path, on_progress, is_cancelled):
        """阻塞执行一次工作台人声分离，返回人声文件路径。

        后端 ``request_task`` 的同步失败路径（服务未启动、输入非法等）只置
        ERROR 状态、不发 ``resultReady``——因此除了等结果，还要监听
        ``snapshotChanged``，避免错误后无限空转。服务未运行时先自动拉起
        （见 ``_ensure_service_ready``）。

        B2 加固：后端存在「到达终态但既不发结果信号、快照又不满足
        ERROR 判据」的路径（服务进程中途退出、Runtime 校验失败等，
        2026-09 用户反馈「AI 打轴卡在分离」的根因之一）。因此等待
        循环额外轮询快照：进入错误/硬终态立即报错；``MODEL_REQUIRED``
        停车（后端在等页面上点「下载模型」）自动触发一次下载续跑；
        超过 ``_STALL_TIMEOUT_S`` 无任何进展则看门狗取消并报错。
        """
        source = Path(source_path)
        if not source.is_file():
            raise AiTimingHostError(f"音频文件不存在：{source}")
        self._ensure_service_ready(on_progress, is_cancelled)

        backend = self._backend
        started = time.monotonic()
        done = threading.Event()
        outcome: dict = {}
        last_activity = started

        def _touch() -> None:
            nonlocal last_activity
            last_activity = time.monotonic()

        def _on_progress(progress) -> None:
            _touch()
            try:
                total = int(progress.processing_total or 0)
                cur = int(progress.processing_done or 0)
                percent = int(cur * 100 / total) if total else 0
                on_progress(
                    "separation", percent, progress.stage_name or progress.title or ""
                )
            except Exception:
                pass

        def _on_result(result) -> None:
            _touch()
            # 并发场景（第 2 步批次在跑）下 resultReady 会带来其他任务
            # 类型的结果——只认人声任务，否则会把伴奏产物当人声返回
            if getattr(result, "task", None) not in (TaskType.VOCAL, None):
                return
            outcome["result"] = result
            done.set()

        def _on_snapshot(snap) -> None:
            _touch()
            # 同步失败：进入 ERROR 且没有排队/进行中任务，也没有结果
            if done.is_set():
                return
            if (
                snap.state == ServiceState.ERROR
                and snap.pending_task is None
                and not snap.queued_tasks
            ):
                outcome["error"] = snap.error or "人声分离启动失败"
                done.set()

        def _cancel_backend() -> None:
            try:
                backend.cancel_task()
            except Exception:
                pass

        # 监听当前后端（模式切换后端实例已替换时，以提交时刻的后端为准）
        try:
            backend.taskProgressChanged.connect(_on_progress)
            backend.resultReady.connect(_on_result)
            backend.snapshotChanged.connect(_on_snapshot)
        except TypeError:
            pass
        try:
            _ailog(f"提交人声分离任务：{source.name}")
            backend.request_task(
                TaskType.VOCAL,
                input_path=str(source),
                output_dir=str(source.parent),
                output_format="wav",
            )
            polls = 0
            model_download_attempts = 0
            while not done.wait(0.2):
                if is_cancelled():
                    _cancel_backend()
                    raise AiTimingHostError("已取消人声分离")
                # 后端被整体替换（模式切换） → 当前等待作废
                if self._backend is not backend:
                    raise AiTimingHostError(
                        "分离后端已切换，请重新执行 AI 打轴"
                    )
                if time.monotonic() - last_activity > self._STALL_TIMEOUT_S:
                    _cancel_backend()
                    _ailog(
                        f"人声分离 {int(self._STALL_TIMEOUT_S)}s 无任何进展，"
                        f"看门狗已取消任务：{source.name}"
                    )
                    raise AiTimingHostError(
                        f"人声分离长时间无响应"
                        f"（{int(self._STALL_TIMEOUT_S // 60)} 分钟无进展），已自动取消。"
                        "请到第 2 步「音频分离」页检查分离环境与显卡驱动后重试"
                    )
                # 按周期读一次快照：捕获「不发结果信号的终态」——信号
                # 谓词（_on_snapshot）只认 ERROR 且无挂起任务，服务中途
                # 死掉/Runtime 校验失败等路径满足不了它
                polls += 1
                if polls % self._SNAPSHOT_POLL_EVERY:
                    continue
                snap = backend.snapshot()
                state = snap.state
                if (
                    state is ServiceState.MODEL_REQUIRED
                    and snap.pending_task is not None
                ):
                    if model_download_attempts >= 1:
                        raise AiTimingHostError(
                            "人声模型缺失且自动下载未能恢复，"
                            "请到第 2 步「音频分离」页下载模型后重试 AI 打轴"
                        )
                    model_download_attempts += 1
                    _ailog(
                        f"人声模型缺失（MODEL_REQUIRED），自动触发下载：{source.name}"
                    )
                    on_progress("separation", 9, "人声模型缺失，正在自动下载模型…")
                    backend.start_model_download()
                elif state is ServiceState.ERROR:
                    _ailog(f"分离环境进入错误态：{snap.error or state.value}")
                    raise AiTimingHostError(str(snap.error or "人声分离失败"))
                elif state in self._HARD_PARKED_STATES:
                    _ailog(f"分离环境进入终态 {state.value}：{snap.error}")
                    raise AiTimingHostError(
                        str(snap.error)
                        or f"分离环境不可用（{getattr(state, 'value', state)}），"
                        "请到第 2 步「音频分离」页检查"
                    )

            elapsed = time.monotonic() - started
            if "error" in outcome:
                _ailog(f"人声分离失败（{elapsed:.1f}s）：{outcome['error']}")
                raise AiTimingHostError(str(outcome["error"]))
            result = outcome.get("result")
            if result is None:
                _ailog(f"人声分离失败：任务没有返回结果（{elapsed:.1f}s）")
                raise AiTimingHostError("分离任务没有返回结果")
            if getattr(result, "failed", False):
                message = str(result.error or "人声分离失败")
                _ailog(f"人声分离失败（{elapsed:.1f}s）：{message}")
                raise AiTimingHostError(message)
            # 记录本次产物供后续 find_session_vocal 零分离复用。用户在
            # 分离页手动分离的人声无需记录——它们落在原音频同目录，
            # SUG 的同目录严格匹配（§6.1 ③）天然可以发现。
            self.record_result(result)
            for f in getattr(result, "files", []) or []:
                p = Path(f.path)
                if p.suffix.lower() == ".wav" and p.is_file():
                    _ailog(
                        f"人声分离完成（{elapsed:.1f}s）：{source.name} → {p.name}"
                    )
                    return p
            _ailog(f"人声分离完成但未找到人声 wav（{elapsed:.1f}s）：{source.name}")
            raise AiTimingHostError("分离完成但未找到输出的人声文件")
        finally:
            for signal, slot in (
                (backend.taskProgressChanged, _on_progress),
                (backend.resultReady, _on_result),
                (backend.snapshotChanged, _on_snapshot),
            ):
                try:
                    signal.disconnect(slot)
                except (TypeError, RuntimeError):
                    pass

    def ai_cache_dir(self):
        return self._cache_root / "ai_timing"

    def runtime_python(self):
        """宿主托管 PyMSS Runtime 的 python.exe（SUG AI 打轴增量复用）。

        SUG（方案 B）检测到可用路径后，AI 打轴的「安装/修复」改为向该
        解释器增量安装 AI 依赖：不建 venv、不重复下载 torch，
        torchaudio 按 runtime 已装的 torch 版本自动配对。返回 None 表示
        托管 runtime 未安装（SUG 引导去分离页，或经确认后独立安装兜底）。
        """
        try:
            external = getattr(self._backend, "external_ai_python", None)
            if callable(external):
                prepared = external()
                if prepared:
                    return prepared
            snap = self._backend.snapshot()
            install_dir = str(getattr(snap, "install_dir", "") or "")
            if not install_dir:
                return None
            from krok_helper.audio_processing.separation.service import (
                runtime_python as resolve_runtime_python,
            )

            exe = resolve_runtime_python(install_dir)
            return str(exe) if exe.is_file() else None
        except Exception:
            return None

    def note_runtime_changed(self) -> bool:
        """（可选协议）托管 runtime 被受信增量修改后的清单再登记入口。

        SUG 的 install_shared 装完依赖后调用：pip 会升级/降级清单登记
        在案的共用包，不重登记的话工作台下次启动会报「文件缺失或
        损坏」。后端未提供该能力（旧版/模拟后端）时返回 False，
        SUG 静默跳过。
        """
        note = getattr(self._backend, "note_runtime_changed", None)
        if not callable(note):
            return False
        try:
            return bool(note())
        except Exception:
            return False

    _STOP_SERVICE_TIMEOUT_S = 30.0

    #: 这些状态下必然没有工作台拉起的服务进程，无需停止。
    _NO_SERVICE_STATES = frozenset(
        {
            ServiceState.UNCONFIGURED,
            ServiceState.LOCATION_REQUIRED,
            ServiceState.RUNTIME_DOWNLOADING,
            ServiceState.RUNTIME_VERIFYING,
            ServiceState.INSTALL_MISSING,
            ServiceState.INSTALL_DAMAGED,
            ServiceState.VERSION_INCOMPATIBLE,
            ServiceState.EXTERNAL_VERSION_INCOMPATIBLE,
            ServiceState.INSTALLED_STOPPED,
            ServiceState.EXTERNAL_OFFLINE,
        }
    )

    def stop_separation_service(self, timeout_s: float | None = None) -> dict:
        """（可选协议）停止工作台拉起的分离服务，释放共享解释器的文件锁。

        SUG 方案 B 增量安装**前**调用：桥接进程已加载的 torch 等 .pyd
        会锁住 site-packages 里的文件，pip 覆盖被锁文件可能半失败、
        留下新旧混杂的安装。装完后由 ``note_runtime_changed`` 重启；
        若安装前服务本就没跑，无需显式恢复——AI 打轴执行时
        ``separate_vocal`` 会自动拉起。

        有分离任务在执行时拒绝（不打断任务）。阻塞等待停止完成；
        服务未在运行时立即返回。

        Returns:
            ``{"stopped": bool, "message": str}``（message 中文，可直接展示）。
        """
        snap = self._backend.snapshot()
        if snap.pending_task is not None:
            return {
                "stopped": False,
                "message": "分离环境正在执行任务，无法腾出安装环境，请稍后重试",
            }
        if snap.state not in self._NO_SERVICE_STATES:
            self._backend.stop_service()
        deadline = time.monotonic() + (
            self._STOP_SERVICE_TIMEOUT_S
            if timeout_s is None
            else max(1.0, float(timeout_s))
        )
        while True:
            snap = self._backend.snapshot()
            if snap.state in (
                ServiceState.INSTALLED_STOPPED,
                ServiceState.EXTERNAL_OFFLINE,
            ):
                return {"stopped": True, "message": "分离服务已停止"}
            if snap.state in self._NO_SERVICE_STATES:
                return {"stopped": True, "message": "分离服务未在运行"}
            if snap.state is ServiceState.ERROR:
                return {
                    "stopped": False,
                    "message": snap.error or "停止分离服务失败",
                }
            if time.monotonic() > deadline:
                return {
                    "stopped": False,
                    "message": "停止分离服务超时，请到第 2 步「音频分离」页检查",
                }
            time.sleep(0.2)

    def open_separation_page(self) -> bool:
        """跳转到工作台第 2 步的「分离人声」页（SUG 引导安装入口）。"""
        if callable(self._navigate):
            try:
                self._navigate()
                return True
            except Exception:
                return False
        return False

    def model_root(self):
        """统一 AI 模型根目录（SUG 与工作台共用，嵌入模式复用）。

        对齐模型放在与工作台分离 Runtime/模型同一管理根下的 ``ai_models``；
        返回 None 表示宿主未提供，SUG 回落自身默认目录。
        """
        try:
            from krok_helper.settings import get_settings_path

            return get_settings_path().parent / "ai_models"
        except Exception:
            return None

    def http_proxy(self) -> str:
        """当前生效的下载代理 URL（SUG 模型下载默认走工作台网络设置）。

        返回空串表示不走显式代理（requests 仍会继承系统/环境代理）。
        """
        try:
            from krok_helper.network import proxy_url_for_app_settings

            page = getattr(self, "_page_ref", None)
            if page is None:
                return ""
            settings = getattr(page, "_settings", None)
            if settings is None:
                return ""
            return proxy_url_for_app_settings(settings) or ""
        except Exception:
            return ""

    # ── 内部 ──

    def record_result(self, result) -> None:
        """记录一次分离产物（供 find_session_vocal 零分离复用）。"""
        with self._lock:
            self._session_results.append(result)
            # 会话产物记录有界，防长会话内存增长
            if len(self._session_results) > 200:
                self._session_results = self._session_results[-100:]
