"""Public-key configuration and load checks for the bundled macOS updater."""

from __future__ import annotations

import base64
import plistlib
import sys
from pathlib import Path


def validate_ed_key(value: str) -> str:
    value = value.strip()
    try:
        decoded = base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise ValueError("macOS 更新公钥不是有效的 Base64。") from exc
    if len(decoded) != 32:
        raise ValueError("macOS 更新公钥必须是 32 字节的 Ed25519 公钥。")
    return value


def verify_bundled_sparkle() -> None:
    """Load the real framework from a frozen host without starting an update."""
    if sys.platform != "darwin" or not getattr(sys, "frozen", False):
        raise ValueError("更新框架检查需要从 macOS .app 中运行。")
    bundle = Path(sys.executable).resolve().parent.parent.parent
    if bundle.suffix != ".app":
        raise ValueError("无法定位 macOS 应用包。")
    metadata = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
    validate_ed_key(metadata.get("SUPublicEDKey", ""))
    if not metadata.get("SUVerifyUpdateBeforeExtraction"):
        raise ValueError("应用未配置解压前更新验签。")
    framework = bundle / "Contents/Frameworks/Sparkle.framework"
    for helper in ("Updater.app", "XPCServices/Installer.xpc", "XPCServices/Downloader.xpc"):
        if not (framework / "Versions/B" / helper).exists():
            raise ValueError(f"应用包缺少 Sparkle 辅助程序：{helper}")
    if not (bundle / "Contents/Resources/Sparkle-LICENSE.txt").is_file():
        raise ValueError("应用包缺少 Sparkle 许可文件。")
    import objc
    import AppKit  # Ensure the Cocoa bridge is also available in the frozen app.

    objc.loadBundle("Sparkle", {}, bundle_path=str(framework))
    objc.lookUpClass("SPUUpdater")
