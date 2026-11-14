"""One-command Skia hardware check on a real Mac, using a bundle built by CI.

Usage (from the repository root, with a logged-in desktop session):
    python3 scripts/mac_hardware_check.py <mac-validation-ARCH.zip from GitHub Actions>

Nothing is compiled here: the bundle carries the Skia smoke tools, backend tests and the Qt Quick
preview module built by the pinned CI toolchain. This script only checks the machine's real
Metal GPU and writes build/mac-hardware-check-results.zip to send back. It installs PyQt6 into
build/mac-hardware-check/venv and leaves the rest of the system untouched.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import zipfile

REPO = Path(__file__).resolve().parents[1]
WORK = REPO / "build/mac-hardware-check"
FONT = Path("/System/Library/Fonts/Supplemental/Arial.ttf")
PYQT = ["PyQt6==6.11.0", "PyQt6-Qt6==6.11.0", "pytest", "numpy"]
# Only these system_profiler display fields are kept: no serial numbers or hardware UUIDs.
GPU_FIELDS = ("sppci_model", "sppci_cores", "spdisplays_mtlgpufamilysupport", "spdisplays_vendor",
              "sppci_device_type", "spdisplays_metal")


def run(command, log: Path, env=None, timeout=900) -> int:
    with log.open("w", encoding="utf-8") as out:
        out.write("$ " + " ".join(str(c) for c in command) + "\n")
        out.flush()
        try:
            return subprocess.run([str(c) for c in command], stdout=out, stderr=subprocess.STDOUT,
                                  env=env, timeout=timeout, cwd=REPO).returncode
        except subprocess.TimeoutExpired:
            out.write(f"\nTIMEOUT after {timeout}s\n")
            return 124


def output(command) -> str:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=120).stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"unavailable: {error}"


def unpack(archive: Path, destination: Path) -> Path:
    """Accept the GitHub artifact zip (which wraps the bundle) or the inner .tar.gz itself."""
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as outer:
            inner = [n for n in outer.namelist() if n.endswith(".tar.gz")]
            if len(inner) != 1:
                raise SystemExit(f"Expected one .tar.gz inside {archive.name}, found {inner}")
            archive = destination / Path(inner[0]).name
            archive.write_bytes(outer.read(inner[0]))
    with tarfile.open(archive) as bundle:
        kwargs = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
        bundle.extractall(destination / "bundle", **kwargs)
    # Files from a browser download may carry Gatekeeper's quarantine flag.
    try:
        subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(destination / "bundle")],
                       capture_output=True)
    except OSError:
        pass  # not macOS (unit tests)
    return destination / "bundle"


def gpu_summary(displays_json: str) -> list[dict]:
    try:
        return [{k: gpu[k] for k in GPU_FIELDS if k in gpu}
                for gpu in json.loads(displays_json).get("SPDisplaysDataType", [])]
    except (json.JSONDecodeError, AttributeError):
        return [{"error": "system_profiler output unreadable"}]


def verify(bundle: Path) -> dict:
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    for name, digest in manifest["files"].items():
        if hashlib.sha256((bundle / name).read_bytes()).hexdigest() != digest:
            raise SystemExit(f"Bundle file corrupted or modified: {name}")
    return manifest


def environment() -> dict:
    gpus = gpu_summary(output(["system_profiler", "-json", "SPDisplaysDataType"]))
    return {"macos": output(["sw_vers", "-productVersion"]), "build": output(["sw_vers", "-buildVersion"]),
            "machine": platform.machine(), "cpu": output(["sysctl", "-n", "machdep.cpu.brand_string"]),
            "python": sys.version.split()[0], "gpus": gpus,
            "repo_commit": output(["git", "-C", str(REPO), "rev-parse", "HEAD"])}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("bundle", type=Path, help="mac-validation-ARCH.zip (or the .tar.gz inside it)")
    parser.add_argument("--allow-virtual-gpu", action="store_true",
                        help="CI only: a virtual Metal device is not a failure (hardware gate stays open)")
    options = parser.parse_args(argv)
    if sys.platform != "darwin":
        parser.error("Run this on the Mac being checked")
    if not (REPO / "krok_helper/lyrics_timing/src").is_dir():
        parser.error("Submodule missing: run `git submodule update --init --recursive` first")
    WORK.mkdir(parents=True, exist_ok=True)
    logs = WORK / "logs"
    shutil.rmtree(logs, ignore_errors=True)
    logs.mkdir()
    bundle = unpack(options.bundle.expanduser().resolve(), WORK / "unpacked")
    manifest = verify(bundle)
    arch = "arm64" if platform.machine() == "arm64" else "x64"
    if manifest["arch"] != arch:
        parser.error(f"This Mac is {arch}; download mac-validation-{arch} instead of {manifest['arch']}")
    env_info = environment()
    if env_info["repo_commit"] != manifest["commit"]:
        print(f"WARNING: repository is at {env_info['repo_commit'][:10]}, bundle was built from "
              f"{manifest['commit'][:10]}; run `git checkout {manifest['commit']}` for matching tests.")

    venv = WORK / "venv"
    python = venv / "bin/python"
    if not python.exists():
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    if run([python, "-m", "pip", "install", "--quiet", *PYQT], logs / "pip.log") != 0:
        raise SystemExit("pip install failed; see build/mac-hardware-check/logs/pip.log")

    native = bundle / "native"
    gpu = native / f"macos-{arch}-metal"
    cpu = native / f"macos-{arch}-raster"
    env = {k: v for k, v in os.environ.items() if k != "QT_QPA_PLATFORM"} | {
        "KROK_SKIA_NATIVE_TEST_ROOT": str(native), "KROK_SKIA_NATIVE_TEST_ARCH": arch,
        "KROK_SKIA_NATIVE_TEST_FONT": str(FONT), "PYTHONPATH": str(REPO)}
    steps = {
        "metal_hardware_smoke": [gpu / "krok_skia_gpu_smoke", FONT, logs / "metal", "--require-hardware", "--frames", "120"],
        "raster_smoke": [cpu / "krok_skia_raster_smoke", FONT, logs / "raster", "--frames", "120"],
        "owned_metal_backend": [gpu / "subtitle_skia_backend_tests", "--gpu", "--allow-software"],
        "quick_preview": [python, "-m", "pytest", "--noconftest", "-rs", "-v", "tests/test_subtitle_skia_preview.py"],
        "native_capabilities": [python, "-m", "pytest", "--noconftest", "-rs", "-v", "tests/test_subtitle_skia_native.py"],
    }
    results = {name: run(command, logs / f"{name}.log", env) for name, command in steps.items()}
    # Metal API validation: the same preview tests with Apple's debug layer on.
    results["quick_preview_metal_validation"] = run(
        steps["quick_preview"], logs / "quick_preview_metal_validation.log",
        env | {"MTL_DEBUG_LAYER": "1", "MTL_DEBUG_LAYER_WARNING_MODE": "nslog"})

    metal_report = logs / "metal.json"
    hardware = json.loads(metal_report.read_text()).get("hardware") if metal_report.exists() else None
    hardware_steps = ("metal_hardware_smoke", "native_capabilities")
    functional_ok = all(code == 0 for name, code in results.items() if name not in hardware_steps)
    hardware_ok = hardware is True and all(results[name] == 0 for name in hardware_steps)
    summary = {"bundle": manifest, "environment": env_info, "exit_codes": results,
               "metal_hardware": hardware, "functional_passed": functional_ok, "hardware_gate_passed": hardware_ok}
    (WORK / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    archive = REPO / "build/mac-hardware-check-results.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as out:
        out.write(WORK / "summary.json", "summary.json")
        for path in sorted(logs.rglob("*")):
            if path.is_file():
                out.write(path, f"logs/{path.relative_to(logs)}")
    print(json.dumps({k: summary[k] for k in ("exit_codes", "metal_hardware", "functional_passed",
                                               "hardware_gate_passed")}, indent=2))
    print(f"\nSend back: {archive}")
    if not functional_ok:
        return 1
    return 0 if hardware_ok or options.allow_virtual_gpu else 2


if __name__ == "__main__":
    sys.exit(main())
