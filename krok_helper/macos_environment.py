from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
from functools import lru_cache


@lru_cache(maxsize=1)
def macos_shell_path() -> str | None:
    """Read the user's login/interactive shell PATH once, on demand."""
    if sys.platform != "darwin":
        return

    import pwd

    log = logging.getLogger(__name__)
    try:
        shell = os.environ.get("SHELL") or pwd.getpwuid(os.getuid()).pw_shell
        # A login shell reads profiles; interactive mode also reads .zshrc.
        with subprocess.Popen(
            [shell, "-i", "-l", "-c", "/usr/bin/printf '\\0'; exec /usr/bin/env -0"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=os.path.expanduser("~"),
            start_new_session=True,
        ) as process:
            try:
                output, _ = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                # Shell startup scripts can leave children holding stdout open.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
                log.warning("读取 shell PATH 超时，继续使用应用启动时的 PATH。")
                return
            if process.returncode:
                log.warning("读取 shell PATH 失败，继续使用应用启动时的 PATH。")
                return
    except (OSError, KeyError) as exc:
        log.warning("无法读取 shell PATH，继续使用应用启动时的 PATH：%s", exc)
        return

    for entry in output.split(b"\0"):
        if entry.startswith(b"PATH="):
            return os.fsdecode(entry[5:]) or None
    log.warning("shell 未返回 PATH，继续使用应用启动时的 PATH。")
