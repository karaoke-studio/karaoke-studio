"""ZIP archives that keep POSIX file modes and symlinks (Linux packages).

PyInstaller's Linux output contains executables and relative symlinks.
``zipfile.ZipFile.extractall`` drops both, so the Linux package is written
with Unix ``external_attr`` and restored here; Info-ZIP ``unzip`` reads the
same attributes when users unpack the archive by hand.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import stat
import zipfile

_UNIX = 3  # ZipInfo.create_system for Unix


def write_tree(root: Path, archive: Path, *, prefix: str) -> int:
    """Zip ``root`` under ``prefix/``, keeping modes and relative symlinks."""
    root = Path(root)
    count = 0
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(root.rglob("*")):
            name = str(PurePosixPath(prefix, path.relative_to(root).as_posix()))
            info_stat = path.lstat()
            if path.is_symlink():
                target = os.readlink(path)
                _check_link(root, path, target)
                info = zipfile.ZipInfo(name)
                info.create_system = _UNIX
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                zf.writestr(info, target)
            elif path.is_dir():
                info = zipfile.ZipInfo(name + "/")
                info.create_system = _UNIX
                info.external_attr = ((stat.S_IFDIR | 0o755) << 16) | 0x10
                zf.writestr(info, b"")
            else:
                info = zipfile.ZipInfo.from_file(path, name)
                info.create_system = _UNIX
                info.external_attr = (stat.S_IFREG | stat.S_IMODE(info_stat.st_mode)) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                with path.open("rb") as source, zf.open(info, "w") as target:
                    while chunk := source.read(1 << 20):
                        target.write(chunk)
            count += 1
    return count


def extract(archive: Path, destination: Path) -> None:
    """Extract ``archive``, restoring Unix modes and in-tree relative symlinks."""
    destination = Path(destination).resolve()
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            target = (destination / info.filename).resolve()
            if target != destination and destination not in target.parents:
                raise OSError(f"压缩包路径越界: {info.filename}")
            mode = info.external_attr >> 16
            if info.create_system == _UNIX and stat.S_ISLNK(mode):
                link = zf.read(info).decode("utf-8")
                _check_link(destination, target, link)
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.is_symlink() or target.exists():
                    target.unlink()
                os.symlink(link, target)
                continue
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as source, target.open("wb") as sink:
                while chunk := source.read(1 << 20):
                    sink.write(chunk)
            if info.create_system == _UNIX and stat.S_IMODE(mode):
                os.chmod(target, stat.S_IMODE(mode))


def _check_link(root: Path, link_path: Path, target: str) -> None:
    if os.path.isabs(target):
        raise OSError(f"拒绝绝对路径符号链接: {link_path} -> {target}")
    resolved = (Path(link_path).parent / target).resolve()
    base = Path(root).resolve()
    if resolved != base and base not in resolved.parents:
        raise OSError(f"拒绝指向包外的符号链接: {link_path} -> {target}")


if __name__ == "__main__":  # pragma: no cover - build-time entry point
    import argparse

    parser = argparse.ArgumentParser(description="Write a POSIX-faithful package ZIP")
    parser.add_argument("root", type=Path)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--prefix", required=True)
    options = parser.parse_args()
    print(f"{write_tree(options.root, options.archive, prefix=options.prefix)} entries")
