"""启动期面包屑：在日志系统就绪**之前**也能留下痕迹的一行式记录。

为什么不能靠 :mod:`krok_helper.logging_config`：应用在用户机器上出现过「窗口起来了、
Qt 全加载了，但整个会话在日志里一条记录都没有」的情况，还伴随 ``Qt6Core.dll`` 里
``abort()``（``0xc0000409`` / ``__fastfail(FAST_FAIL_FATAL_APP_EXIT)``，也就是 ``qFatal``）
的崩溃转储。日志本身都没写出来，自然也就问不出它死在哪一步。

所以这里刻意只用最底层的手段：

* 落点固定在 ``%TEMP%/LinKLyrics/startup-trace.log`` —— 不碰设置目录，免得设置目录
  本身就是出问题的那一环；
* 每次 ``mark()`` 都是 open→write→close，不留缓冲，进程被 ``abort()`` 打死也不丢；
* 全程 ``except Exception``：诊断代码绝不能变成新的崩溃源。

另外 :func:`install_qt_message_capture` 把 Qt 自己的 warning/fatal 也抄一份进来。
无控制台的 GUI 构建里 Qt 默认把这些丢给 ``OutputDebugString``，没人接就等于消失，
而 ``qFatal`` 的那句话恰恰是崩溃原因本身。
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

__all__ = ["trace_path", "mark", "install_qt_message_capture"]

_MAX_BYTES = 256 * 1024
_ENV_OVERRIDE = "KARAOKE_STUDIO_STARTUP_TRACE"
_started = time.monotonic()


def trace_path() -> Path:
    override = os.getenv(_ENV_OVERRIDE)
    if override:
        return Path(override).expanduser()
    return Path(tempfile.gettempdir()) / "LinKLyrics" / "startup-trace.log"


def _append(line: str) -> None:
    try:
        path = trace_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # 只留最近一段：这是诊断文件，不值得为它做轮转机制，涨过头就从头再来。
        if path.exists() and path.stat().st_size > _MAX_BYTES:
            path.unlink()
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:  # noqa: BLE001 —— 诊断代码不许把应用带崩
        pass


def mark(step: str, detail: str = "") -> None:
    """记一条启动面包屑。``step`` 用简短英文标识，``detail`` 可选补充。"""

    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    elapsed = time.monotonic() - _started
    suffix = f" {detail}" if detail else ""
    _append(f"{stamp} +{elapsed:6.2f}s pid={os.getpid()} {step}{suffix}\n")


def install_qt_message_capture() -> bool:
    """把 Qt 的 warning/critical/fatal 也抄进面包屑文件；返回是否装上了。

    必须在 ``QApplication`` 构造**之前**装：qFatal 一响进程就没了，晚一步就抄不到。
    这里不替代 :mod:`logging_config` 里那个正式处理器 —— 那个装得晚，而且依赖日志系统
    本身是好的；两者可以并存，正式处理器装上后会覆盖这一个。
    """

    try:
        from PyQt6.QtCore import QtMsgType, qInstallMessageHandler
    except Exception:  # noqa: BLE001
        return False

    names = {
        QtMsgType.QtDebugMsg: "debug",
        QtMsgType.QtInfoMsg: "info",
        QtMsgType.QtWarningMsg: "warning",
        QtMsgType.QtCriticalMsg: "critical",
        QtMsgType.QtFatalMsg: "FATAL",
    }

    def handler(message_type, context, message) -> None:
        level = names.get(message_type, "?")
        where = ""
        if context is not None and getattr(context, "file", None):
            where = f" ({context.file}:{getattr(context, 'line', 0)})"
        mark(f"qt.{level}", f"{message}{where}")

    try:
        qInstallMessageHandler(handler)
    except Exception:  # noqa: BLE001
        return False
    return True
