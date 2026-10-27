"""Filesystem path classification shared by export UI and watchdogs."""

from __future__ import annotations

import os
from pathlib import Path


def is_remote_path(path: Path | None) -> bool:
    """True when ``path`` is on a UNC / mapped / disconnected network drive.

    网络位置上的 ``exists()`` / ``disk_usage()`` / 输出写入可能在 SMB 断开时
    阻塞几十秒。导出页的「磁盘可用」直接跳过这类位置；ffmpeg 写盘停滞时也
    不能盲目判定为编码器楔死并切 CPU。
    """

    if path is None or os.name != "nt":
        return False
    text = str(path)
    if text.startswith("\\\\") or text.startswith("//"):
        return True
    drive = path.drive
    if not drive:
        return False
    try:
        import ctypes

        drive_type = int(ctypes.windll.kernel32.GetDriveTypeW(drive + "\\"))
    except Exception:  # noqa: BLE001 - 拿不到类型时按本地处理，保持旧行为
        return False
    # 0=unknown / 1=no root（断开的映射盘）/ 4=remote
    return drive_type in {0, 1, 4}
