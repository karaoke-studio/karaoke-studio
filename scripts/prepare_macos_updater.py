"""Fetch the pinned official Sparkle SDK and embed/sign it after PyInstaller."""

from __future__ import annotations

import argparse
import hashlib
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from krok_helper.updater.macos_metadata import validate_ed_key

SDK_VERSION = "2.9.6"
SDK_SHA256 = "52bf9e88cdd972fc0c81501377a880e90d47031bd8ca5462488f843e2609e192"
SDK_URL = f"https://github.com/sparkle-project/Sparkle/releases/download/{SDK_VERSION}/Sparkle-{SDK_VERSION}.tar.xz"


def prepare_sdk(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    archive = Path(os.environ.get("KROK_SPARKLE_SDK_ARCHIVE", directory / f"Sparkle-{SDK_VERSION}.tar.xz"))
    if not archive.exists():
        from krok_helper.network import requests_session_for_proxy

        session, proxies = requests_session_for_proxy("system", "")
        try:
            with session.get(SDK_URL, proxies=proxies, stream=True, timeout=(15, 60)) as response:
                response.raise_for_status()
                with archive.open("wb") as stream:
                    for chunk in response.iter_content(256 * 1024):
                        stream.write(chunk)
        except BaseException:
            archive.unlink(missing_ok=True)
            raise
        finally:
            session.close()
    with archive.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != SDK_SHA256:
            raise ValueError("Sparkle SDK checksum mismatch; remove the cached archive and retry.")
    subprocess.run(["tar", "-xf", str(archive.resolve()), "-C", str(directory.resolve())], check=True)
    if not (directory / "Sparkle.framework/Versions/B/Sparkle").is_file():
        raise ValueError("Sparkle SDK is missing its framework.")
    return directory


def configure_bundle(app: Path, sdk: Path, public_key: str):
    public_key = validate_ed_key(public_key)
    frameworks = app / "Contents/Frameworks"
    frameworks.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ditto", str(sdk / "Sparkle.framework"), str(frameworks / "Sparkle.framework")], check=True)
    resources = app / "Contents/Resources"
    resources.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(sdk / "LICENSE", resources / "Sparkle-LICENSE.txt")
    plist = app / "Contents/Info.plist"
    metadata = plistlib.loads(plist.read_bytes())
    metadata.update({
        "SUPublicEDKey": public_key,
        "SUEnableAutomaticChecks": False,
        "SUAutomaticallyUpdate": False,
        "SUEnableInstallerLauncherService": True,
        "SUVerifyUpdateBeforeExtraction": True,
    })
    # Only loopback is used by Sparkle; Python owns all external network traffic.
    metadata.setdefault("NSAppTransportSecurity", {})["NSAllowsLocalNetworking"] = True
    plist.write_bytes(plistlib.dumps(metadata))


def disable_bundle_updates(app: Path):
    """Remove updater resources and metadata when rebuilding without a key."""
    framework = app / "Contents/Frameworks/Sparkle.framework"
    if framework.is_symlink():
        framework.unlink()
    elif framework.exists():
        shutil.rmtree(framework)
    (app / "Contents/Resources/Sparkle-LICENSE.txt").unlink(missing_ok=True)
    plist = app / "Contents/Info.plist"
    metadata = plistlib.loads(plist.read_bytes())
    for key in ("SUPublicEDKey", "SUEnableAutomaticChecks", "SUAutomaticallyUpdate",
                "SUEnableInstallerLauncherService", "SUVerifyUpdateBeforeExtraction"):
        metadata.pop(key, None)
    plist.write_bytes(plistlib.dumps(metadata))


def sign_bundle(app: Path):
    common = ["/usr/bin/codesign", "--force", "--sign", "-"]
    # Sign inside out. --deep is verification only, never signing.
    paths = sorted(app.rglob("*"), key=lambda p: len(p.parts), reverse=True)
    for path in paths:
        if path.is_symlink() or not path.is_file():
            continue
        with path.open("rb") as stream:
            magic = stream.read(4)
        if magic in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"):
            subprocess.run([*common, str(path)], check=True)
    for path in paths:
        if not path.is_symlink() and path.is_dir() and path.suffix in (".framework", ".app", ".xpc"):
            subprocess.run([*common, str(path)], check=True)
    subprocess.run([*common, str(app)], check=True)
    subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-dir", type=Path, required=True)
    parser.add_argument("--app", type=Path)
    args = parser.parse_args()
    public_key = os.environ.get("KROK_SPARKLE_PUBLIC_ED_KEY", "").strip()
    if public_key:
        public_key = validate_ed_key(public_key)
        sdk = prepare_sdk(args.sdk_dir)
        if args.app:
            configure_bundle(args.app, sdk, public_key)
    else:
        print("No Sparkle public key configured; building without macOS automatic installation.")
        if args.app:
            disable_bundle_updates(args.app)
    if args.app:
        sign_bundle(args.app)


if __name__ == "__main__":
    main()
