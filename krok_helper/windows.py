from __future__ import annotations

import ctypes
import subprocess
import sys
from ctypes import wintypes


if sys.platform == "win32":
    user32 = ctypes.windll.user32
    shell32 = ctypes.windll.shell32
    shcore = getattr(ctypes.windll, "shcore", None)
    user32.GetDpiForSystem.restype = wintypes.UINT


def enable_high_dpi_awareness() -> None:
    if sys.platform != "win32":
        return

    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass

    if shcore is not None:
        try:
            shcore.SetProcessDpiAwareness(2)
            return
        except Exception:
            pass

    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


def set_explicit_app_user_model_id(app_id: str) -> None:
    if sys.platform != "win32":
        return

    try:
        shell32.SetCurrentProcessExplicitAppUserModelID(str(app_id))
    except Exception:
        pass


def hidden_subprocess_kwargs() -> dict[str, object]:
    """Return Windows process flags that prevent console-window flashes.

    ``CREATE_NO_WINDOW`` is the primary guard for console executables.  The
    hidden ``STARTUPINFO`` is retained as defense in depth for launchers that
    do not fully honor that creation flag when called from a frozen GUI app.
    """

    if sys.platform != "win32":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
    return {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
        "startupinfo": startupinfo,
    }
