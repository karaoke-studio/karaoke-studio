"""Platform decisions and reproducibility fingerprints, without build side effects."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import shutil

from .sources import output, sha256, windows_llvm


def host_platform() -> tuple[str, str]:
    system = {"Windows": "windows", "Linux": "linux", "Darwin": "macos"}[platform.system()]
    arch = "arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "x64"
    return system, arch


def validate_selection(system: str, arch: str, gpu: str | None, cpu_only: bool) -> None:
    # Linux supports native x64 and arm64 (host must match; cross-OS/cross-arch
    # compilation is rejected in build_skia_renderer.arguments()).
    # Windows stays x64-only: configure() hardcodes MSVC lib/x64 + INCLUDE/LIB
    # paths, and skia.lock.json has no windows-arm64 toolchain entry.
    if system == "windows" and arch != "x64":
        raise ValueError("P01 Windows currently supports x64 only")
    if system == "linux" and arch not in {"x64", "arm64"}:
        raise ValueError("P01 Linux supports x64 and arm64 only")
    if cpu_only and gpu:
        raise ValueError("--cpu-only cannot be combined with --gpu-api")
    expected = "metal" if system == "macos" else "vulkan"
    if not cpu_only and gpu != expected:
        raise ValueError(f"{system} requires --gpu-api {expected}")


def gn_text(arguments: dict) -> str:
    return "\n".join(f"{key} = {json.dumps(value)}" for key, value in sorted(arguments.items())) + "\n"


def digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def configure(lock: dict, root: Path, system: str, arch: str, gpu: str | None,
              cpu_only: bool, llvm_root: Path | None) -> tuple[dict, dict, dict]:
    args = lock["common_args"] | {
        "target_cpu": arch, "skia_enable_ganesh": not cpu_only,
        "skia_use_vulkan": gpu == "vulkan", "skia_use_metal": gpu == "metal",
    }
    # m150's Ganesh fallback allocator remains guarded by this upstream macro;
    # GN's skia_use_vma adds the sources but no longer defines it for Ganesh.
    args["extra_cflags"] = ["-DSK_USE_VMA"] if gpu == "vulkan" else []
    cmake = {}
    identity = {"platform": system, "arch": arch, "gpu_api": gpu or "raster",
                "qt_contract": lock["qt_version"]}
    spec = lock["toolchains"][f"{system}-{arch}"]
    if system == "windows":
        # CI runners may carry a newer servicing build of the pinned toolset; an
        # explicit override selects it and is recorded in the build identity.
        msvc = os.environ.get("KROK_SKIA_MSVC_VERSION", "").strip() or spec["msvc"]
        sdk_version = os.environ.get("KROK_SKIA_WINDOWS_SDK", "").strip() or spec["sdk"]
        vswhere = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / (
            "Microsoft Visual Studio/Installer/vswhere.exe")
        installations = json.loads(output([str(vswhere), "-all", "-products", "*", "-format", "json", "-utf8"]))
        vc = next((Path(i["installationPath"]) / "VC" for i in installations if (
            Path(i["installationPath"]) / "VC/Tools/MSVC" / msvc / "include"
        ).exists()), None)
        if vc is None:
            raise RuntimeError(f"MSVC {msvc} from skia.lock.json not installed")
        sdk = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Windows Kits/10"
        if not (sdk / "Include" / sdk_version / "um/Windows.h").exists():
            raise RuntimeError(f"Windows SDK {sdk_version} not installed")
        llvm = windows_llvm(root, spec, llvm_root)
        compiler = llvm / "bin/clang-cl.exe"
        args |= {"win_vc": vc.as_posix(), "win_toolchain_version": msvc,
                 "win_sdk": sdk.as_posix(), "win_sdk_version": sdk_version,
                 "clang_win": llvm.as_posix(), "clang_win_version": "21"}
        args["extra_cflags"] = ["/MD"] + (["/DSK_USE_VMA"] if gpu == "vulkan" else [])
        cmake |= {"CMAKE_CXX_COMPILER": compiler.as_posix(),
                  "CMAKE_LINKER": (llvm / "bin/lld-link.exe").as_posix(),
                  "CMAKE_MSVC_RUNTIME_LIBRARY": "MultiThreadedDLL"}
        # Use the selected STL/SDK even when the calling terminal has no vcvars environment.
        os.environ["INCLUDE"] = os.pathsep.join(str(p) for p in [
            vc / "Tools/MSVC" / msvc / "include",
            *[sdk / "Include" / sdk_version / part for part in ("ucrt", "shared", "um", "winrt")]])
        os.environ["LIB"] = os.pathsep.join(str(p) for p in [
            vc / "Tools/MSVC" / msvc / "lib/x64",
            *[sdk / "Lib" / sdk_version / part / "x64" for part in ("ucrt", "um")]])
        os.environ["PATH"] = str(sdk / "bin" / sdk_version / "x64") + os.pathsep + os.environ["PATH"]
        identity |= {"msvc": msvc, "sdk": sdk_version, "crt": "MD"}
        identity["sdk_windows_h_sha256"] = sha256(sdk / "Include" / sdk_version / "um/Windows.h")
        identity["stl_yvals_sha256"] = sha256(vc / "Tools/MSVC" / msvc / "include/yvals_core.h")
    else:
        if system == "macos":
            compiler = Path(output(["xcrun", "--find", "clang++"]))
        elif llvm_root is not None:
            # Pinned LLVM handed over by the caller (e.g. the arm64 bootstrap script).
            # Never fall back to PATH: a foreign "clang++-21" wrapper can win there
            # and fail to exec (broken shebang => FileNotFoundError).
            compiler = llvm_root / "bin/clang++"
            if not compiler.exists():
                compiler = next((path for path in sorted(llvm_root.glob("**/clang++"))
                                 if path.is_file()), None)
                if compiler is None:
                    raise RuntimeError(f"--llvm-root does not contain bin/clang++: {llvm_root}")
        else:
            compiler = Path(shutil.which("clang++-21") or shutil.which("clang++") or "clang++")
        version = output([str(compiler), "--version"])
        c_compiler = compiler.with_name(compiler.name.replace("clang++", "clang"))
        args |= {"cc": str(c_compiler), "cxx": str(compiler)}
        cmake["CMAKE_CXX_COMPILER"] = str(compiler)
        if system == "linux":
            if f"clang version {spec['version']}" not in version:
                raise RuntimeError(f"Linux requires Clang {spec['version']}: {version}")
            identity["libc"] = platform.libc_ver()
        else:
            xcode = output(["xcodebuild", "-version"])
            if not xcode.startswith(f"Xcode {spec['xcode']}\n"):
                raise RuntimeError(f"Xcode differs from lock: {xcode}")
            identity |= {"xcode": xcode, "sdk": output(["xcrun", "--sdk", "macosx", "--show-sdk-version"])}
            if identity["sdk"] != spec["sdk"]:
                raise RuntimeError(f"macOS SDK differs from lock: {identity['sdk']}")
            cmake["CMAKE_OBJCXX_COMPILER"] = str(compiler)
            cmake["CMAKE_OSX_ARCHITECTURES"] = "arm64" if arch == "arm64" else "x86_64"
    identity |= {"compiler": output([str(compiler), "--version"]), "compiler_sha256": sha256(compiler)}
    return args, cmake, identity
