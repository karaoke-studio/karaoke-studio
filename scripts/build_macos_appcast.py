"""Sign the final macOS ZIP and generate the per-release Sparkle appcast."""

from __future__ import annotations

import argparse
import base64
import mmap
import os
import plistlib
import subprocess
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from krok_helper.updater.macos_feed import ASSET_NAME, FEED_NAME, MacFeed, macos_versions, render_feed, validate_ed_key
from krok_helper.updater.sources import build_download_url
from scripts.update_macos_version import read_app_version


def validate_signing_keys(public_key: str, private_key: str) -> str:
    public_key = validate_ed_key(public_key)
    # Modern generate_keys exports a 32-byte seed. Older exports contain the
    # expanded 64-byte private key followed by the 32-byte public key.
    try:
        key_bytes = base64.b64decode(private_key.strip(), validate=True)
    except ValueError:
        raise ValueError("Invalid Sparkle private key encoding") from None
    if len(key_bytes) not in (32, 96):
        raise ValueError("Expected a Sparkle generate_keys export (32 or 96 bytes).")
    if len(key_bytes) == 96 and base64.b64encode(key_bytes[64:]).decode() != public_key:
        raise ValueError("Sparkle private/public keys do not match.")
    # Derive modern keys independently to catch misconfigured CI secrets before
    # publishing an archive that every installed client would reject.
    if len(key_bytes) == 32:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        derived = Ed25519PrivateKey.from_private_bytes(key_bytes).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        if base64.b64encode(derived).decode() != public_key:
            raise ValueError("Sparkle private/public keys do not match.")
    return public_key


def sign_file(archive: Path, sdk: Path, public_key: str, private_key: str) -> str:
    public_key = validate_signing_keys(public_key, private_key)
    signed = subprocess.run(
        [str(sdk / "bin/sign_update"), "--ed-key-file", "-", "-p", str(archive)],
        input=private_key.strip() + "\n", text=True, capture_output=True,
    )
    if signed.returncode:
        # Never print tool diagnostics: invalid key errors can contain key bytes.
        raise ValueError("Sparkle signing failed; check the private key and SDK.")
    signature = signed.stdout.strip()
    if len(base64.b64decode(signature, validate=True)) != 64:
        raise ValueError("Sparkle returned an invalid signature.")
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.exceptions import InvalidSignature

    try:
        with archive.open("rb") as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as contents:
            Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key)).verify(base64.b64decode(signature), contents)
    except InvalidSignature:
        raise ValueError("The signed archive failed verification with the packaged public key.") from None
    return signature


def sign_archive(archive: Path, sdk: Path, version: str, public_key: str, private_key: str) -> bytes:
    public_key = validate_signing_keys(public_key, private_key)
    _, build = macos_versions(version)
    if archive.name != ASSET_NAME:
        raise ValueError(f"Archive must be named {ASSET_NAME}")
    with zipfile.ZipFile(archive) as package:
        metadata = plistlib.loads(package.read("Lin-K Lyrics.app/Contents/Info.plist"))
    if metadata.get("SUPublicEDKey") != public_key or metadata.get("CFBundleVersion") != build:
        raise ValueError("The packaged public key or CFBundleVersion differs from this release.")
    signature = sign_file(archive, sdk, public_key, private_key)
    return render_feed(MacFeed(version, build, archive.stat().st_size, signature, build_download_url("github", f"v{version}", ASSET_NAME)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--sdk-dir", type=Path, required=True)
    args = parser.parse_args()
    public_key = os.environ.get("KROK_SPARKLE_PUBLIC_ED_KEY", "").strip()
    private_key = os.environ.get("KROK_SPARKLE_PRIVATE_ED_KEY", "").strip()
    if not public_key or not private_key:
        args.archive.with_name(FEED_NAME).unlink(missing_ok=True)
        print("Sparkle keys are incomplete; skipping macOS update signing and appcast generation.")
        return
    data = sign_archive(
        args.archive, args.sdk_dir, read_app_version(),
        public_key, private_key,
    )
    args.archive.with_name(FEED_NAME).write_bytes(data)


if __name__ == "__main__":
    main()
