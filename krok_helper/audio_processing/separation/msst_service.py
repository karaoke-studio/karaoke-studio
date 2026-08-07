"""用用户已有的 MSST 环境执行分离：常驻桥接进程 + 行分隔 JSON 协议。

为什么是常驻进程而不是每次调 ``scripts/msst_cli.py``：

* 队列里「分离人声」和「分离伴奏」用的是同一个模型，常驻可以只加载一次；
* CLI 只能把**所有** stem 写到同一个目录，而 ``MSSeparator.store_dirs`` 支持按
  instrument 传字典，可以只落我们要的那一条轨；
* 能把阶段与进度回传给界面。

代价是耦合 MSST 的内部 API（``inference.msst_infer.MSSeparator``），因此连接时要做
真实导入探测（见 :mod:`msst_env`），让版本漂移在配置阶段就明确暴露，而不是任务跑到
一半才炸。

桥接脚本写在**工作台自己的工作目录**，绝不落进用户的 MSST 目录（§4.4：不修改外部
环境）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from .msst_env import find_python, locate_root

#: 桥接脚本源码。用 MSST 自带解释器执行，通过 stdin/stdout 收发行分隔 JSON。
_BRIDGE_SOURCE = r'''"""Karaoke Studio 的 MSST 桥接进程（由工作台生成，请勿手工修改）。"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import traceback

# MSST 的日志与 tqdm 会往 stdout 写东西，会污染协议。
# 先把真正的 stdout 收起来只给协议用，其余输出一律转到 stderr（工作台记进日志）。
_protocol = sys.stdout
sys.stdout = sys.stderr

sys.path.insert(0, os.environ["KROK_MSST_ROOT"])

_separator = None
_signature = None


_progress_state = {"job": None, "rate": 44100, "last": 0.0, "ratio": -1.0}


def _reply(payload):
    _protocol.write(json.dumps(payload, ensure_ascii=False) + "\n")
    _protocol.flush()


class _ProgressBar:
    """替换 MSST demix 里的 tqdm，把分块进度回传给工作台。

    ``demix_track`` 只用到 total / update / close，这里实现这几项即可。
    不改 MSST 的代码，只在本进程内替换 ``utils.utils`` 的 tqdm 引用。
    """

    def __init__(self, *args, **kwargs):
        self.total = kwargs.get("total") or 0
        self.n = 0
        _progress_state["ratio"] = -1.0

    def update(self, step=1):
        self.n += step
        self._emit()

    def _emit(self):
        import time as _time

        if not self.total:
            return
        ratio = min(1.0, self.n / float(self.total))
        now = _time.time()
        # 每个分块都发会刷屏：变化不足 0.5% 且间隔不到 0.3 秒就跳过。
        if ratio - _progress_state["ratio"] < 0.005 and now - _progress_state["last"] < 0.3:
            return
        _progress_state["ratio"] = ratio
        _progress_state["last"] = now
        rate = float(_progress_state["rate"] or 44100)
        # demix 的 tqdm 建在补零之后，最后一块的 update 会冲过 total（实测 102%）。
        # 上报前按 ratio 截断，避免界面出现超过 100% 的进度。
        _reply(
            {
                "event": "progress",
                "id": _progress_state["job"],
                "done": (self.total * ratio) / rate,
                "total": self.total / rate,
            }
        )

    def close(self):
        self.n = self.total
        _progress_state["ratio"] = -1.0

    def set_postfix(self, *args, **kwargs):
        return

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _install_progress_hook():
    """把分块进度接出来：MSST 自己只往终端打 tqdm，没有回调接口。"""
    import utils.utils as _mss_utils

    if getattr(_mss_utils, "_krok_patched", False):
        return
    _mss_utils.tqdm = _ProgressBar
    _mss_utils._krok_patched = True


def _ensure_separator(job):
    """同一模型 + 同一输出格式时复用已加载的实例，避免重复加载权重。"""
    global _separator, _signature
    signature = (
        job["model_type"],
        job["model_path"],
        job["config_path"],
        job.get("format", "wav"),
        job.get("device", "auto"),
    )
    if _separator is not None and _signature == signature:
        return _separator

    if _separator is not None:
        try:
            _separator.del_cache()
        except Exception:
            pass
        _separator = None
        _signature = None

    from inference.msst_infer import MSSeparator

    _separator = MSSeparator(
        model_type=job["model_type"],
        config_path=job["config_path"],
        model_path=job["model_path"],
        device=job.get("device", "auto"),
        device_ids=[0],
        output_format=job.get("format", "wav"),
        use_tta=False,
        store_dirs={},
        debug=False,
    )
    _signature = signature
    _install_progress_hook()
    try:
        _progress_state["rate"] = int(_separator.config.audio.get("sample_rate", 44100))
    except Exception:
        _progress_state["rate"] = 44100
    return _separator


def _run(job):
    separator = _ensure_separator(job)
    stem = job["stem"]
    output_dir = job["output_dir"]
    os.makedirs(output_dir, exist_ok=True)

    # MSSeparator 只接受文件夹；把这一个文件放进临时目录喂给它。
    staging = tempfile.mkdtemp(prefix="krok-msst-")
    try:
        source = job["input"]
        staged = os.path.join(staging, os.path.basename(source))
        shutil.copy2(source, staged)

        # 只保存需要的那条轨；非法轨名会被 MSST 自己剔除，因此这里再自查一次。
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


def main():
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
        job_id = job.get("id")
        try:
            _progress_state["job"] = job_id
            _progress_state["ratio"] = -1.0
            _reply({"event": "stage", "id": job_id, "stage": "load"})
            path = _run(job)
            _reply({"event": "done", "id": job_id, "ok": True, "path": path})
        except BaseException as exc:  # 任何失败都要回一条，否则工作台会一直等
            traceback.print_exc()
            _reply(
                {
                    "event": "done",
                    "id": job_id,
                    "ok": False,
                    "error": str(exc) or type(exc).__name__,
                }
            )


main()
'''


def write_bridge(work_root: str | os.PathLike) -> Path:
    """把桥接脚本写进工作台自己的目录并返回路径。"""
    path = Path(work_root) / "msst_bridge.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        current = path.read_text(encoding="utf-8")
    except OSError:
        current = ""
    if current != _BRIDGE_SOURCE:
        path.write_text(_BRIDGE_SOURCE, encoding="utf-8")
    return path


@dataclass
class MsstWorker:
    """常驻的 MSST 桥接进程。"""

    root: Path
    process: subprocess.Popen
    log_stream: object
    _lock: threading.Lock

    @property
    def running(self) -> bool:
        return self.process.poll() is None

    @classmethod
    def start(
        cls,
        msst_root: str | os.PathLike,
        work_root: str | os.PathLike,
        *,
        popen_factory=subprocess.Popen,
        ready_timeout: float = 180.0,
    ) -> "MsstWorker":
        root = locate_root(msst_root)
        if root is None:
            raise FileNotFoundError("所选目录不是可用的 MSST 安装。")
        python = find_python(root)
        if python is None:
            raise FileNotFoundError("MSST 目录里没有找到自带的 Python 解释器。")

        work = Path(work_root)
        work.mkdir(parents=True, exist_ok=True)
        bridge = write_bridge(work)
        log_path = work / "msst.log"
        log_stream = log_path.open("a", encoding="utf-8", errors="replace")

        env = dict(os.environ)
        env["KROK_MSST_ROOT"] = str(root)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONNOUSERSITE"] = "1"
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0

        process = popen_factory(
            [str(python), str(bridge)],
            cwd=str(root),
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
        worker = cls(root=root, process=process, log_stream=log_stream, _lock=threading.Lock())
        worker._await_ready(ready_timeout, log_path)
        return worker

    def _await_ready(self, timeout: float, log_path: Path) -> None:
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.stop()
                raise RuntimeError(
                    f"MSST 桥接进程启动后立即退出（退出码 {self.process.returncode}）。"
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
        self.stop()
        raise TimeoutError(f"MSST 桥接进程在 {timeout:.0f} 秒内没有就绪。详见日志：{log_path}")

    def separate(self, job: dict, *, on_stage=None, on_progress=None) -> str:
        """提交一个分离任务并等待完成，返回产出的文件路径。"""
        with self._lock:
            if not self.running or self.process.stdin is None or self.process.stdout is None:
                raise RuntimeError("MSST 桥接进程已退出。")
            self.process.stdin.write(json.dumps(job, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
            while True:
                line = self.process.stdout.readline()
                if not line:
                    raise RuntimeError("MSST 桥接进程在任务完成前退出。")
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
                    raise RuntimeError(str(payload.get("error") or "MSST 分离失败。"))

    def stop(self, timeout_seconds: float = 5.0) -> bool:
        try:
            if self.process.poll() is None and self.process.stdin is not None:
                try:
                    self.process.stdin.write(json.dumps({"action": "shutdown"}) + "\n")
                    self.process.stdin.flush()
                except (OSError, ValueError):
                    pass
                try:
                    self.process.wait(timeout=timeout_seconds)
                except subprocess.TimeoutExpired:
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


__all__ = ["MsstWorker", "write_bridge"]
