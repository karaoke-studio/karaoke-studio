"""不经 HTTP 直接驱动 PyMSS：常驻桥接进程 + 行分隔 JSON 协议。

需求文档 §9.3 早就把这条路标为 P1：

> 正式产品适配层应保留改用工作台自有任务 sidecar 或文件路径 IPC 的空间，以解决：
> 精确进度、可靠取消、长音频整段 PCM 带来的内存峰值、输出 ZIP 在内存中生成的问题。

走 ``pymss serve`` 时，一首 4 分钟的歌要把整段 f32le PCM（约 170 MB）塞进 HTTP 请求
体，服务端再把输出打成 ZIP 放在内存里返回。改成文件路径 IPC 后这两份内存都不再需要，
同时省掉 uvicorn 启动、端口占用与 ``/health`` 轮询。

``MSSeparator`` 是 pymss 的公开类，比现有桥接补丁 ``pymss.server.app._run_separation_sync``
（私有函数）耦合更小。``from_model_name`` 同时能解析 catalog 模型与用户注册模型，
因此外部 MSST 映射也走同一条路。

**仅用于工作台自己拉起进程的两种模式**（托管安装、外部可执行环境）。
「连接一个已在运行的服务地址」那种模式仍然只能走 HTTP。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

#: 桥接脚本源码。由工作台写入自己的目录，用 PyMSS 运行时的解释器执行。
_BRIDGE_SOURCE = r'''"""Karaoke Studio 的 PyMSS 桥接进程（由工作台生成，请勿手工修改）。"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import traceback

# pymss 的日志会写 stdout，会污染协议：真正的 stdout 只留给协议，其余转 stderr。
_protocol = sys.stdout
sys.stdout = sys.stderr

_separator = None
_signature = None
_job_id = None
_last = {"ratio": -1.0, "at": 0.0}


def _reply(payload):
    _protocol.write(json.dumps(payload, ensure_ascii=False) + "\n")
    _protocol.flush()


def _on_progress(done, total, message="", *args, **kwargs):
    """pymss 的进度回调：(done, total, message)，已按 sample_rate 换算成秒。"""
    import time as _time

    try:
        total = float(total or 0)
        done = float(done or 0)
    except (TypeError, ValueError):
        return
    if total <= 0:
        return
    ratio = min(1.0, done / total)
    now = _time.time()
    # 每块都发会刷屏：变化不足 0.5% 且间隔不到 0.3 秒就跳过。
    if ratio - _last["ratio"] < 0.005 and now - _last["at"] < 0.3:
        return
    _last["ratio"] = ratio
    _last["at"] = now
    _reply({"event": "progress", "id": _job_id, "done": total * ratio, "total": total})


def _ensure_separator(job):
    """同一模型 + 同一输出格式复用已加载实例，避免重复加载权重。"""
    global _separator, _signature
    signature = (job["model"], job.get("format", "wav"), job.get("device", "auto"))
    if _separator is not None and _signature == signature:
        return _separator

    if _separator is not None:
        for name in ("close", "del_cache"):
            try:
                getattr(_separator, name)()
            except Exception:
                pass
        _separator = None
        _signature = None

    from pymss import MSSeparator

    _separator = MSSeparator.from_model_name(
        job["model"],
        model_dir=job.get("model_dir") or None,
        download=False,
        device=job.get("device", "auto"),
        output_format=job.get("format", "wav"),
        store_dirs={},
        progress_callback=_on_progress,
    )
    _signature = signature
    return _separator


def _run(job):
    separator = _ensure_separator(job)
    stem = job["stem"]
    output_dir = job["output_dir"]
    os.makedirs(output_dir, exist_ok=True)

    # MSSeparator 只接受文件夹；把这一个文件放进临时目录喂给它。
    staging = tempfile.mkdtemp(prefix="krok-pymss-")
    try:
        source = job["input"]
        shutil.copy2(source, os.path.join(staging, os.path.basename(source)))

        instruments = list(separator.config.training.get("instruments", []))
        if stem not in instruments:
            raise ValueError(
                "模型没有名为 %r 的输出轨，它声明的是：%s" % (stem, "/".join(instruments))
            )
        separator.store_dirs = {stem: output_dir}
        separator.process_folder(staging)

        name = os.path.splitext(os.path.basename(source))[0]
        produced = os.path.join(
            output_dir, "%s_%s.%s" % (name, stem, job.get("format", "wav"))
        )
        if not os.path.isfile(produced):
            raise RuntimeError("分离结束但没有生成预期的输出文件。")
        return produced
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _catalog(job):
    """列出 catalog 条目；替代 GET /v1/catalog/models。"""
    from pymss.server.models import catalog_model_card, catalog_model_detail
    from pymss.model_registry import list_models

    model_dir = job.get("model_dir") or None
    name = str(job.get("model") or "").strip()
    if name:
        return catalog_model_detail(name, model_dir=model_dir, source=job.get("source", "modelscope"))
    rows = list_models(supported=job.get("supported", True))
    return {
        "object": "list",
        "data": [
            catalog_model_card(entry, model_dir=model_dir, source=job.get("source", "modelscope"))
            for entry in rows
        ],
    }


def _download(job):
    """下载单个模型；替代 POST /v1/models/download。"""
    from pymss import download_model

    def report(done, total, message=""):
        _reply(
            {
                "event": "progress",
                "id": _job_id,
                "done": float(done or 0),
                "total": float(total or 0),
                "bytes": True,
            }
        )

    download_model(
        job["model"],
        model_dir=job.get("model_dir") or None,
        source=job.get("source", "modelscope"),
        verify=True,
        progress_callback=report,
    )
    return {"model": job["model"], "ok": True}


def _capability(job):
    """能力探测：报告版本与关键组件是否可用（替代 HTTP 的端点检查）。"""
    import pymss
    from pymss import MSSeparator  # noqa: F401  仅验证可导入
    from pymss.model_registry import list_models

    try:
        import torch

        torch_version = torch.__version__
        cuda = bool(torch.cuda.is_available())
    except Exception:
        torch_version, cuda = "", False
    return {
        "pymss_version": getattr(pymss, "__version__", ""),
        "models": len(list_models(supported=True)),
        "torch": torch_version,
        "cuda": cuda,
    }


_ACTIONS = {"catalog": _catalog, "download": _download, "capability": _capability}


def main():
    global _job_id
    _reply({"event": "ready"})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            job = json.loads(line)
        except ValueError:
            continue
        if job.get("action") == "shutdown":
            break
        _job_id = job.get("id")
        _last["ratio"] = -1.0
        try:
            handler = _ACTIONS.get(job.get("action"))
            if handler is not None:
                _reply({"event": "done", "id": _job_id, "ok": True, "data": handler(job)})
                continue
            _reply({"event": "stage", "id": _job_id, "stage": "load"})
            path = _run(job)
            _reply({"event": "done", "id": _job_id, "ok": True, "path": path})
        except BaseException as exc:  # 任何失败都要回一条，否则工作台会一直等
            traceback.print_exc()
            _reply(
                {
                    "event": "done",
                    "id": _job_id,
                    "ok": False,
                    "error": str(exc) or type(exc).__name__,
                }
            )


main()
'''


def write_bridge(work_root: str | os.PathLike) -> Path:
    """把桥接脚本写进工作台自己的目录并返回路径。"""
    path = Path(work_root) / "pymss_bridge.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        current = path.read_text(encoding="utf-8")
    except OSError:
        current = ""
    if current != _BRIDGE_SOURCE:
        path.write_text(_BRIDGE_SOURCE, encoding="utf-8")
    return path


@dataclass
class PyMSSWorker:
    """常驻的 PyMSS 桥接进程（不起 HTTP 服务）。"""

    process: subprocess.Popen
    log_stream: object
    _lock: threading.Lock

    @property
    def running(self) -> bool:
        return self.process.poll() is None

    @classmethod
    def start(
        cls,
        executable: str | os.PathLike,
        work_root: str | os.PathLike,
        *,
        model_dir: str | os.PathLike = "",
        user_models: str | os.PathLike = "",
        popen_factory=subprocess.Popen,
        ready_timeout: float = 180.0,
    ) -> "PyMSSWorker":
        python = Path(executable)
        if not python.is_file():
            raise FileNotFoundError(f"找不到 PyMSS 运行时解释器：{python}")

        work = Path(work_root)
        work.mkdir(parents=True, exist_ok=True)
        bridge = write_bridge(work)
        log_path = work / "pymss-bridge.log"
        log_stream = log_path.open("a", encoding="utf-8", errors="replace")

        env = dict(os.environ)
        if model_dir:
            env["PYMSS_MODEL_DIR"] = str(model_dir)
        if user_models:
            env["PYMSS_USER_MODELS"] = str(user_models)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONNOUSERSITE"] = "1"
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0

        process = popen_factory(
            [str(python), str(bridge)],
            cwd=str(work),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=log_stream,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
        worker = cls(process=process, log_stream=log_stream, _lock=threading.Lock())
        worker._await_ready(ready_timeout, log_path)
        return worker

    def _await_ready(self, timeout: float, log_path: Path) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.stop(force=True)
                raise RuntimeError(
                    f"PyMSS 桥接进程启动后立即退出（退出码 {self.process.returncode}）。"
                    f"\n完整日志：{log_path}"
                )
            line = self.process.stdout.readline() if self.process.stdout else ""
            if not line:
                continue
            try:
                payload = json.loads(line.strip())
            except ValueError:
                continue
            if payload.get("event") == "ready":
                return
        self.stop(force=True)
        raise TimeoutError(f"PyMSS 桥接进程在 {timeout:.0f} 秒内没有就绪。详见日志：{log_path}")

    def separate(self, job: dict, *, on_stage=None, on_progress=None) -> str:
        """提交一个分离任务并等待完成，返回产出的文件路径。"""
        with self._lock:
            if not self.running or self.process.stdin is None or self.process.stdout is None:
                raise RuntimeError("PyMSS 桥接进程已退出。")
            self.process.stdin.write(json.dumps(job, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
            while True:
                line = self.process.stdout.readline()
                if not line:
                    raise RuntimeError("PyMSS 桥接进程在任务完成前退出。")
                try:
                    payload = json.loads(line.strip())
                except ValueError:
                    continue
                event = payload.get("event")
                if event == "stage":
                    if on_stage is not None:
                        on_stage(str(payload.get("stage") or ""))
                    continue
                if event == "progress":
                    if on_progress is not None:
                        on_progress(
                            float(payload.get("done") or 0.0),
                            float(payload.get("total") or 0.0),
                        )
                    continue
                if event == "done":
                    if payload.get("ok"):
                        return str(payload.get("path") or "")
                    raise RuntimeError(str(payload.get("error") or "PyMSS 分离失败。"))

    def request(self, job: dict, *, on_progress=None):
        """提交一个非分离动作（catalog / download），返回其 data。"""
        with self._lock:
            if not self.running or self.process.stdin is None or self.process.stdout is None:
                raise RuntimeError("PyMSS 桥接进程已退出。")
            self.process.stdin.write(json.dumps(job, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
            while True:
                line = self.process.stdout.readline()
                if not line:
                    raise RuntimeError("PyMSS 桥接进程在请求完成前退出。")
                try:
                    payload = json.loads(line.strip())
                except ValueError:
                    continue
                event = payload.get("event")
                if event == "progress":
                    if on_progress is not None:
                        on_progress(
                            float(payload.get("done") or 0.0),
                            float(payload.get("total") or 0.0),
                        )
                    continue
                if event == "done":
                    if payload.get("ok"):
                        return payload.get("data")
                    raise RuntimeError(str(payload.get("error") or "PyMSS 请求失败。"))

    def stop(self, timeout_seconds: float = 5.0, *, force: bool = False) -> bool:
        """``force=True`` 用于取消：推理不可中断，桥接不会读 stdin，直接终止。"""
        try:
            if self.process.poll() is None:
                if not force and self.process.stdin is not None:
                    try:
                        self.process.stdin.write(json.dumps({"action": "shutdown"}) + "\n")
                        self.process.stdin.flush()
                    except (OSError, ValueError):
                        pass
                    try:
                        self.process.wait(timeout=timeout_seconds)
                    except subprocess.TimeoutExpired:
                        pass
                if self.process.poll() is None:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
            return self.process.poll() is not None
        finally:
            try:
                if not self.log_stream.closed:
                    self.log_stream.close()
            except Exception:
                pass


class PyMSSBridgeEngine:
    """把桥接进程包装成与 :class:`PyMSSClient` 同名同形的接口。

    ``RealSeparationBackend`` 只用到 client 的 8 个方法，鸭子类型顶替可以让绝大多数
    调用点不必改动。唯一没有对应物的是 ``separate_pcm``——那正是要去掉的 PCM 传输，
    改用 :meth:`separate_file` 直接交换文件路径。
    """

    def __init__(self, worker: "PyMSSWorker", *, model_dir: str = "", source: str = "modelscope") -> None:
        self._worker = worker
        self._model_dir = str(model_dir or "")
        self._source = source
        self._next_id = 0

    def _call(self, payload: dict, *, on_progress=None):
        self._next_id += 1
        payload = {**payload, "id": self._next_id, "model_dir": self._model_dir}
        return self._worker.request(payload, on_progress=on_progress)

    # ── 与 PyMSSClient 同名的方法 ──────────────────────────────
    def health(self) -> dict:
        running = self._worker.running
        return {"status": "ok" if running else "down", "device": "由 PyMSS 自行选择"}

    def catalog_models(self, *, supported: bool = True, category: str = "", query: str = "") -> list[dict]:
        data = self._call({"action": "catalog", "supported": supported, "source": self._source})
        rows = data.get("data") if isinstance(data, dict) else None
        return rows if isinstance(rows, list) else []

    def catalog_model(self, model: str, *, source: str = "modelscope") -> dict:
        return self._call({"action": "catalog", "model": model, "source": source})

    def model_config_text(self, model: str, *, source: str = "modelscope", model_dir=None) -> str:
        detail = self.catalog_model(model, source=source)
        files = (detail.get("pymss") or {}).get("files") or []
        config = next((f for f in files if isinstance(f, dict) and f.get("role") == "config"), None)
        if config is None:
            raise RuntimeError(f"模型 {model} 没有配置文件。")
        relpath = str(config.get("relpath") or "")
        base = Path(model_dir or self._model_dir)
        if config.get("exists") and relpath and base:
            try:
                return (base / relpath).read_text(encoding="utf-8")
            except OSError:
                pass
        import urllib.request

        url = str(config.get("remote_url") or "")
        if not url:
            raise RuntimeError(f"模型 {model} 的配置无下载地址。")
        with urllib.request.urlopen(url, timeout=30) as response:
            return response.read().decode("utf-8", errors="replace")

    def download_model(self, model: str, *, source: str = "modelscope", on_progress=None, **_kwargs) -> dict:
        return self._call(
            {"action": "download", "model": model, "source": source}, on_progress=on_progress
        )

    def capability_checks(self) -> list[tuple[str, bool, str]]:
        """与 :meth:`PyMSSClient.capability_checks` 同名同形。

        桥接模式没有 HTTP 端点可查，改为验证真正决定能否干活的东西：进程在跑、
        pymss 与推理类可导入、模型目录能列出。
        """
        if not self._worker.running:
            return [("PyMSS 推理进程", False, "桥接进程未运行")]
        try:
            info = self._call({"action": "capability"}) or {}
        except Exception as exc:
            return [("PyMSS 推理环境", False, str(exc))]

        models = int(info.get("models") or 0)
        torch_version = str(info.get("torch") or "")
        device = "CUDA 可用" if info.get("cuda") else "仅 CPU"
        # 版本不在这里查：调用方（_probe_executable）已经用解释器直接问过并核对了
        # 兼容范围；pymss 包本身没有 __version__，在这里再查一次只会假失败。
        return [
            ("PyMSS 推理进程", True, "已就绪（不经 HTTP 服务）"),
            ("推理与模型目录", models > 0, f"可用模型 {models} 个" if models else "无法列出模型目录"),
            ("运行设备", bool(torch_version), f"torch {torch_version}，{device}" if torch_version else "无法加载 torch"),
        ]

    def separate_file(self, *, model: str, stem: str, input_path: str, output_dir: str,
                      output_format: str = "wav", device: str = "auto",
                      on_stage=None, on_progress=None) -> str:
        """走文件路径，不再传输整段 PCM，也不生成内存里的 ZIP。"""
        self._next_id += 1
        return self._worker.separate(
            {
                "id": self._next_id,
                "model": model,
                "stem": stem,
                "input": input_path,
                "output_dir": output_dir,
                "format": output_format,
                "device": device,
                "model_dir": self._model_dir,
            },
            on_stage=on_stage,
            on_progress=on_progress,
        )


@dataclass
class BridgeServiceProcess:
    """与 ``ManagedServiceProcess`` 同形，但不起 HTTP 服务。

    后端只用到服务对象的 ``client`` / ``running`` / ``stop`` / ``port`` 四项，
    因此这里可以直接顶替，``start_service`` 那条路几乎不用改。
    """

    worker: PyMSSWorker
    client: PyMSSBridgeEngine
    port: int = 0

    @property
    def running(self) -> bool:
        return self.worker.running

    def stop(self, timeout_seconds: float = 5.0, *, force: bool = False) -> bool:
        return self.worker.stop(timeout_seconds, force=force)

    @classmethod
    def start(
        cls,
        install_dir,
        *,
        executable=None,
        model_dir=None,
        user_models_path=None,
        source: str = "modelscope",
        device: str = "auto",
        startup_timeout: float = 45.0,
        cancelled=None,
        popen_factory=subprocess.Popen,
    ) -> "BridgeServiceProcess":
        root = Path(install_dir)
        if executable is not None:
            # 外部环境：install_dir 传进来的已经是工作台自己的工作目录（§4.4）。
            python = Path(executable)
            models = Path(model_dir) if model_dir else ""
            registry = Path(user_models_path) if user_models_path else ""
            work = root
        else:
            python = root / "runtime" / "python.exe"
            if not python.is_file():
                python = root / "runtime" / "bin" / "python3"
            models = root / "models"
            registry = root / "manifests" / "external-models.json"
            work = root / "manifests"

        worker = PyMSSWorker.start(
            python,
            work,
            model_dir=models,
            user_models=registry,
            popen_factory=popen_factory,
            ready_timeout=max(30.0, startup_timeout * 4),
        )
        if cancelled is not None and cancelled.is_set():
            worker.stop(force=True)
            raise InterruptedError("PyMSS 服务启动已取消。")
        engine = PyMSSBridgeEngine(worker, model_dir=str(models or ""), source=source)
        return cls(worker=worker, client=engine)


__all__ = [
    "BridgeServiceProcess",
    "PyMSSBridgeEngine",
    "PyMSSWorker",
    "write_bridge",
]
