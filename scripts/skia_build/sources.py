"""Verified downloads and detached, immutable dependency checkouts."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import shutil
import subprocess
import urllib.request
import zipfile


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def output(command, *, cwd=None) -> str:
    return subprocess.check_output(command, cwd=cwd, text=True, encoding="utf-8").strip()


def checkout(path: Path, spec: dict) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if not (path / ".git").exists():
        subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", spec["url"]], check=True)
    git = ["git", "-C", str(path)]
    if output(git + ["remote", "get-url", "origin"]) != spec["url"]:
        raise RuntimeError(f"Unexpected dependency remote: {path}")
    if output(git + ["status", "--porcelain", "--untracked-files=no"]):
        raise RuntimeError(f"Modified dependency source; refusing to overwrite: {path}")
    head = subprocess.run(git + ["rev-parse", "HEAD"], capture_output=True, text=True)
    if head.returncode or head.stdout.strip() != spec["revision"]:
        subprocess.run(git + ["fetch", "--depth", "1", "origin", spec["revision"]], check=True)
        subprocess.run(git + ["checkout", "--detach", spec["revision"]], check=True)
    if output(git + ["rev-parse", "HEAD"]) != spec["revision"]:
        raise RuntimeError(f"Dependency revision mismatch: {path}")


def download(cache: Path, name: str, spec: dict) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / name
    if not path.exists():
        temporary = path.with_suffix(path.suffix + ".partial")
        print(f"Downloading {name}", flush=True)
        urllib.request.urlretrieve(spec["url"], temporary)
        if sha256(temporary) != spec["sha256"]:
            raise RuntimeError(f"Download digest mismatch: {temporary}")
        temporary.replace(path)
    if sha256(path) != spec["sha256"]:
        raise RuntimeError(f"Cached download digest mismatch: {path}")
    return path


def tools(root: Path, lock: dict, host: str) -> dict[str, Path]:
    result = {}
    for name, spec in lock["tools"][host].items():
        archive = download(root / "downloads", f"{name}-{host}.zip", spec)
        binary = root / "tools" / Path(spec["member"]).name
        binary.parent.mkdir(parents=True, exist_ok=True)
        if not binary.exists() or sha256(binary) != spec["binary_sha256"]:
            with zipfile.ZipFile(archive) as bundle:
                data = bundle.read(spec["member"])
            if hashlib.sha256(data).hexdigest() != spec["binary_sha256"]:
                raise RuntimeError(f"Tool digest mismatch: {name}")
            binary.write_bytes(data)
            binary.chmod(0o755)
        result[name] = binary
    return result


def fetch_sources(root: Path, lock: dict, cpu_only: bool) -> Path:
    source = root / "src" / "skia"
    checkout(source, lock["skia"])
    if sha256(source / "DEPS") != lock["skia"]["deps_sha256"]:
        raise RuntimeError("Upstream DEPS digest mismatch")
    dependencies = [d for d in lock["dependencies"] if not (
        cpu_only and d["path"].split("/")[-1] in {"vulkan-headers", "vulkanmemoryallocator"}
    )]
    with ThreadPoolExecutor(max_workers=5) as executor:
        list(executor.map(lambda d: checkout(source / d["path"], d), dependencies))
    return source


def windows_llvm(root: Path, spec: dict, supplied: Path | None) -> Path:
    llvm = supplied or root / "tools" / "llvm"
    if not (llvm / "bin" / "clang-cl.exe").exists():
        if supplied:
            raise RuntimeError(f"clang-cl.exe missing: {supplied}")
        extractor = shutil.which("7z") or shutil.which("7z.exe")
        if not extractor:
            candidate = Path("C:/Program Files/7-Zip/7z.exe")
            extractor = str(candidate) if candidate.exists() else None
        if not extractor:
            raise RuntimeError("Install 7-Zip or supply --llvm-root for LLVM 21.1.8")
        archive = download(root / "downloads", "LLVM-21.1.8-win64.exe", {
            "url": spec["llvm_url"], "sha256": spec["llvm_sha256"],
        })
        subprocess.run([extractor, "x", str(archive), f"-o{llvm}", "-y"], check=True,
                       stdout=subprocess.DEVNULL)
    version = output([str(llvm / "bin" / "clang-cl.exe"), "--version"])
    if f"clang version {spec['llvm_version']}\n" not in version + "\n":
        raise RuntimeError(f"LLVM version differs from lock: {version}")
    return llvm
