"""Build official pinned Skia and the Qt-free native renderer foundation.

Windows/Linux use Vulkan, macOS uses Metal; --cpu-only needs no GPU SDK/loader.
This does not replace the production Direct2D sidecar or change app preferences.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig

from skia_build.configuration import configure, digest, gn_text, host_platform, validate_selection
from skia_build.sources import fetch_sources, sha256, tools
from skia_build.provenance import (native_needs_clean, record_request, renderer_digest,
                                   reset_relocated_cmake_cache)

REPO = Path(__file__).resolve().parents[1]
LOCK = REPO / "native/subtitle_renderer/skia.lock.json"


def arguments(argv=None):
    system, arch = host_platform()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=("windows", "linux", "macos"), default=system)
    parser.add_argument("--arch", choices=("x64", "arm64"), default=arch)
    parser.add_argument("--gpu-api", choices=("vulkan", "metal"))
    parser.add_argument("--cpu-only", action="store_true")
    parser.add_argument("--config", choices=("Release",), default="Release")
    # Same location on every OS. Not %LOCALAPPDATA%: MSIX-packaged tools see that folder through
    # a redirection layer that cannot follow a junction onto another drive (ERROR 649).
    default_cache = Path.home() / ".cache/krok-helper/skia-deps"
    parser.add_argument("--cache-dir", type=Path, default=default_cache)
    parser.add_argument("--llvm-root", type=Path)
    parser.add_argument("--qt-root", type=Path,
                        help="Qt 6.11.0 SDK; also builds the Qt Quick preview module (P03)")
    parser.add_argument("--jobs", type=int, default=min(8, os.cpu_count() or 1))
    parser.add_argument("--clean", action="store_true", help="Clean object files without deleting sources")
    parser.add_argument("--fetch-only", action="store_true")
    parser.add_argument("--smoke-font", type=Path, help="Font file for the identical Raster/GPU smoke scene")
    result = parser.parse_args(argv)
    try:
        validate_selection(result.platform, result.arch, result.gpu_api, result.cpu_only)
        if result.platform != system:
            raise ValueError("Run on the selected OS; cross-OS compilation is not implemented")
        if result.platform != "macos" and result.arch != arch:
            raise ValueError("Run on the selected native architecture")
        if result.jobs < 1:
            raise ValueError("--jobs must be positive")
        # GN on Windows corrupts Unicode script arguments. Do not modify upstream to hide it.
        if system == "windows" and not str(result.cache_dir).isascii():
            raise ValueError("Windows GN requires an ASCII --cache-dir path")
    except ValueError as error:
        parser.error(str(error))
    return result


def executable(name: str) -> str:
    candidate = (shutil.which(name) or
                 shutil.which(name, path=str(Path(sys.executable).parent / "Scripts")) or
                 shutil.which(name, path=sysconfig.get_path("scripts", scheme=sysconfig.get_preferred_scheme("user"))))
    if not candidate:
        raise RuntimeError(f"{name} not found (install cmake in this Python environment)")
    return candidate


def run(command, *, cwd=None):
    print(subprocess.list2cmdline([str(c) for c in command]), flush=True)
    subprocess.run([str(c) for c in command], cwd=cwd, check=True)


def main(argv=None) -> int:
    options = arguments(argv)
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    # abspath preserves an explicitly supplied ASCII junction on Windows; resolve() doesn't.
    root = Path(os.path.abspath(options.cache_dir))
    source = fetch_sources(root, lock, options.cpu_only)
    system, host_arch = host_platform()
    host = {"windows": "windows", "linux": "linux", "macos": "mac"}[system]
    binaries = tools(root, lock, host + ("-arm64" if host_arch == "arm64" else "-amd64"))
    if system == "windows":
        # GN's copy tool invokes python3 literally, even with --script-executable.
        (root / "tools/python3.cmd").write_text(f'@"{sys.executable}" %*\n', encoding="utf-8")
        os.environ["PATH"] = str(root / "tools") + os.pathsep + os.environ["PATH"]
    gn_args, cmake_args, identity = configure(lock, root, system, options.arch, options.gpu_api,
                                             options.cpu_only, options.llvm_root)
    qt_identity = {}
    if options.qt_root:
        config = options.qt_root / "lib/cmake/Qt6/Qt6ConfigVersionImpl.cmake"
        if not config.exists() or f'set(PACKAGE_VERSION "{lock["qt_version"]}")' not in config.read_text():
            raise RuntimeError(f"Qt SDK must match {lock['qt_version']}: {options.qt_root}")
        # Qt only affects this project's preview module, never the Skia objects, so it belongs
        # to the native fingerprint: switching Qt must not force a full Skia rebuild.
        qt_identity = {"qt_sdk_config_sha256": sha256(config), "qt_root": options.qt_root.as_posix()}
    identity |= {"lock_sha256": sha256(LOCK), "gn_args": gn_args,
                 "gn_sha256": sha256(binaries["gn"]), "ninja_sha256": sha256(binaries["ninja"]),
                 "python_version": sys.version, "python_sha256": sha256(Path(sys.executable))}
    cmake = executable("cmake")
    native_identity = {"renderer_sources_sha256": renderer_digest(REPO),
                       "cmake_version": subprocess.check_output([cmake, "--version"], text=True).strip(),
                       "cmake_sha256": sha256(Path(cmake))} | qt_identity
    fingerprint = digest(identity)
    key = f"{system}-{options.arch}-{options.gpu_api or 'raster'}"
    output = root / "out" / key
    output.mkdir(parents=True, exist_ok=True)
    fingerprint_file = output / "build-fingerprint.json"
    previous = json.loads(fingerprint_file.read_text()) if fingerprint_file.exists() else None
    if previous and previous["fingerprint"] != fingerprint and not options.clean:
        raise RuntimeError("Dependency/toolchain fingerprint changed; rerun with --clean")
    if options.fetch_only:
        print(json.dumps({"source": str(source), "fingerprint": fingerprint}, indent=2))
        return 0
    record_request(output / "build-request.json", fingerprint, clean=options.clean)
    (output / "args.gn").write_text(gn_text(gn_args), encoding="utf-8")
    run([binaries["gn"], "gen", output, "--fail-on-unused-args",
         "--script-executable=" + sys.executable], cwd=source)
    if options.clean:
        run([binaries["ninja"], "-C", output, "-t", "clean"])
    run([binaries["ninja"], "-C", output, "-j", options.jobs, *lock["libraries"]])
    native_output = root / "native" / key
    native_output.mkdir(parents=True, exist_ok=True)
    query = native_output / ".cmake/api/v1/query"
    query.mkdir(parents=True, exist_ok=True)
    (query / "codemodel-v2").write_text("")
    native_identity["dependency_fingerprint"] = fingerprint
    native_fingerprint = digest(native_identity)
    clean_native = native_needs_clean(native_output / "source-request.json", native_fingerprint)
    if reset_relocated_cmake_cache(native_output):
        print("Native CMake cache was created under another cache path; reconfiguring", flush=True)
        clean_native = True
    cmake_args |= {
        "KROK_BUILD_LEGACY_SIDECAR": "OFF", "KROK_BUILD_SKIA": "ON",
        "KROK_SKIA_GPU_API": options.gpu_api or "none", "KROK_SKIA_SOURCE": source.as_posix(),
        "KROK_SKIA_LIB_DIR": output.as_posix(), "KROK_SKIA_FINGERPRINT": native_fingerprint,
        "CMAKE_BUILD_TYPE": options.config, "CMAKE_MAKE_PROGRAM": binaries["ninja"].as_posix(),
        # Always explicit: a cached ON from an earlier --qt-root run must not survive without Qt.
        "KROK_BUILD_SKIA_PREVIEW": "ON" if options.qt_root else "OFF",
    }
    if options.qt_root:
        cmake_args["CMAKE_PREFIX_PATH"] = options.qt_root.as_posix()
    run([cmake, "-S", REPO / "native/subtitle_renderer", "-B", native_output, "-G", "Ninja",
         *[f"-D{k}={v}" for k, v in cmake_args.items()]])
    run([cmake, "--build", native_output, "--parallel", options.jobs,
         *(["--clean-first"] if options.clean or clean_native else [])])
    ctest = str(Path(cmake).with_name("ctest.exe" if system == "windows" else "ctest"))
    run([ctest, "--test-dir", native_output, "--output-on-failure"])
    if renderer_digest(REPO) != native_identity["renderer_sources_sha256"] or sha256(LOCK) != identity["lock_sha256"]:
        raise RuntimeError("Build inputs changed during compilation; rerun before publishing evidence")
    artifacts = {}
    smoke_evidence = {}
    suffix = ".exe" if system == "windows" else ""
    for name in ("krok_skia_raster_smoke", *(() if options.cpu_only else ("krok_skia_gpu_smoke",))):
        binary = native_output / (name + suffix)
        artifacts[name] = sha256(binary)
        if options.smoke_font:
            smoke = [binary, options.smoke_font.absolute(), native_output / name]
            if name.endswith("gpu_smoke"):
                smoke.append("--require-hardware")
            run(smoke)
            report = json.loads((native_output / f"{name}.json").read_text(encoding="utf-8"))
            if report["fingerprint"] != native_fingerprint or report["status"] != "passed":
                raise RuntimeError("Smoke report identity/status mismatch")
            smoke_evidence[name] = {"report": report, "font_sha256": sha256(options.smoke_font),
                "files": {ext: sha256(native_output / f"{name}.{ext}") for ext in ("json", "rgba", "png")}}
    evidence = {"fingerprint": fingerprint, "identity": identity, "artifacts": artifacts,
                "native_fingerprint": native_fingerprint, "native_identity": native_identity,
                "build_status": "passed", "smoke": smoke_evidence}
    # Publish only after a successful build and all requested smoke runs.
    fingerprint_file.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    (native_output / "build-fingerprint.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(f"Build verified: {native_output}\nFingerprint: {fingerprint}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Skia build failed: {error}", file=sys.stderr)
        raise SystemExit(1)
