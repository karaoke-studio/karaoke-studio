"""AI 打轴宿主能力（工作台 → SUG 嵌入注入，阶段 G）。

实现 SUG 的 AiTimingHost 协议（纯鸭子类型，见 SUG 仓库
docs/EMBEDDING.md §6）：把工作台现有的人声分离后端适配成 SUG
「AI 打轴」可用的能力——会话人声零分离复用、缺人声时调用一次
现有分离任务、AI 缓存目录注入。

不复制 PyMSS Runtime、不复制模型、不保存第二套分离参数；
分离始终跟随工作台当前「分离人声」设置（backend 自身状态即真相）。
"""

from __future__ import annotations

import threading
from pathlib import Path

from krok_helper.audio_processing.separation.backend import SeparationBackend
from krok_helper.audio_processing.separation.states import (
    ServiceState,
    TaskType,
)

_VOCAL_SUFFIX = "_人声"
_VOCAL_EXTENSIONS = (".wav", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".aac", ".wma")


class AiTimingHostError(RuntimeError):
    """宿主能力错误（中文消息，SUG 直接展示）。"""


class KaraokeAiTimingHost:
    """把分离后端适配为 SUG AiTimingHost 协议。

    Args:
        backend: 工作台分离后端（真实或 Mock；信号驱动）。
        cache_root: AI 缓存根目录（宿主注入给 SUG 的 ``.cache`` 范围）。
    """

    def __init__(self, backend: SeparationBackend, cache_root: Path):
        self._backend = backend
        self._cache_root = Path(cache_root)
        self._session_results: list = []
        self._lock = threading.Lock()
        try:
            backend.resultReady.connect(self._on_result_ready)
        except TypeError:
            # 测试桩可能没有信号
            pass

    # ── 协议实现 ──

    def separation_status(self) -> dict:
        snap = self._backend.snapshot()
        if snap.pending_task:
            return {
                "available": False,
                "model": str(snap.current_model or ""),
                "message": "分离环境正在执行任务，请稍候",
            }
        available = snap.state in (
            ServiceState.SERVICE_READY,
            ServiceState.EXTERNAL_MODEL_READY,
        )
        return {
            "available": bool(available),
            "model": str(snap.current_model or ""),
            "message": "" if available else (snap.error or "分离环境未就绪"),
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

    def separate_vocal(self, source_path, on_progress, is_cancelled):
        """阻塞执行一次工作台人声分离，返回人声文件路径。"""
        source = Path(source_path)
        status = self.separation_status()
        if not status["available"]:
            raise AiTimingHostError(
                "工作台分离环境未就绪，请先在第 2 步「音频分离」完成环境配置后重试"
            )
        if not source.is_file():
            raise AiTimingHostError(f"音频文件不存在：{source}")

        done = threading.Event()
        outcome: dict = {}

        def _on_progress(progress) -> None:
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
            outcome["result"] = result
            done.set()

        try:
            self._backend.taskProgressChanged.connect(_on_progress)
            self._backend.resultReady.connect(_on_result)
        except TypeError:
            pass
        try:
            self._backend.request_task(
                TaskType.VOCAL,
                input_path=str(source),
                output_dir=str(source.parent),
                output_format="wav",
            )
            while not done.wait(0.2):
                if is_cancelled():
                    try:
                        self._backend.cancel_task()
                    except Exception:
                        pass
                    raise AiTimingHostError("已取消人声分离")

            result = outcome.get("result")
            if result is None:
                raise AiTimingHostError("分离任务没有返回结果")
            if getattr(result, "failed", False):
                raise AiTimingHostError(str(result.error or "人声分离失败"))
            for f in getattr(result, "files", []) or []:
                p = Path(f.path)
                if p.suffix.lower() == ".wav" and p.is_file():
                    return p
            raise AiTimingHostError("分离完成但未找到输出的人声文件")
        finally:
            try:
                self._backend.taskProgressChanged.disconnect(_on_progress)
                self._backend.resultReady.disconnect(_on_result)
            except (TypeError, RuntimeError):
                pass

    def ai_cache_dir(self):
        return self._cache_root / "ai_timing"

    # ── 内部 ──

    def _on_result_ready(self, result) -> None:
        with self._lock:
            self._session_results.append(result)
            # 会话产物记录有界，防长会话内存增长
            if len(self._session_results) > 200:
                self._session_results = self._session_results[-100:]
