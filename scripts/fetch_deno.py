"""Fetch the pinned, verified Deno runtime for desktop builds."""
from __future__ import annotations

import hashlib
import io
import platform
import urllib.request
import zipfile
from pathlib import Path

VERSION = "2.5.4"
ARCHIVES = {
    ("Windows", "AMD64"): ("x86_64-pc-windows-msvc", "1c40c73193960fcbed9341764d43bbd35e381eddbcf7d6cba7c2f3c0cabe96fb"),
    ("Darwin", "arm64"): ("aarch64-apple-darwin", "ff301a55e9f51306c02eb6da76ceba6ac98109fd11dbd7a1f5b71f5794ad725c"),
    ("Darwin", "x86_64"): ("x86_64-apple-darwin", "bda20c2edb39d614830b9dc4a7dcbfaa0043ae5c246bae2f90b82e943a735e66"),
}


def download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as response:
        return response.read()


def fetch(root: Path) -> Path:
    target, digest = ARCHIVES[(platform.system(), platform.machine())]
    vendor = root / "build" / "vendor" / "deno"
    vendor.mkdir(parents=True, exist_ok=True)
    archive = vendor / f"deno-{target}.zip"
    payload = archive.read_bytes() if archive.exists() else b""
    if hashlib.sha256(payload).hexdigest() != digest:
        payload = download(f"https://github.com/denoland/deno/releases/download/v{VERSION}/{archive.name}")
    if hashlib.sha256(payload).hexdigest() != digest:
        raise RuntimeError("Deno archive SHA-256 mismatch")
    archive.write_bytes(payload)
    name = "deno.exe" if platform.system() == "Windows" else "deno"
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        executable = zf.read(name)
    # Only extract the expected executable; never trust archive paths.
    destination = vendor / name
    destination.write_bytes(executable)
    destination.chmod(0o755)
    license_path = vendor / "LICENSE"
    license_path.write_bytes(download(f"https://raw.githubusercontent.com/denoland/deno/v{VERSION}/LICENSE.md"))
    return destination


if __name__ == "__main__":
    print(fetch(Path(__file__).resolve().parents[1]))
