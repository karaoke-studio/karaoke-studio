"""Linux packages keep executable bits and in-tree symlinks through update ZIPs."""

from __future__ import annotations

import os
import stat
import sys
import zipfile
from pathlib import Path

import pytest

from krok_helper.updater import posix_zip

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes and symlinks")


@posix_only
def test_round_trip_keeps_modes_and_relative_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "dist"
    (root / "_internal" / "lib").mkdir(parents=True)
    exe = root / "Lin-K Lyrics"
    exe.write_bytes(b"\x7fELFbinary")
    exe.chmod(0o755)
    data = root / "_internal" / "data.txt"
    data.write_text("data")
    data.chmod(0o644)
    (root / "_internal" / "lib" / "libfoo.so.1").write_bytes(b"\x7fELFlib")
    os.symlink("lib/libfoo.so.1", root / "_internal" / "libfoo.so")

    archive = tmp_path / "pkg.zip"
    posix_zip.write_tree(root, archive, prefix="Lin-K Lyrics")
    out = tmp_path / "out"
    posix_zip.extract(archive, out)

    restored = out / "Lin-K Lyrics"
    assert stat.S_IMODE((restored / "Lin-K Lyrics").stat().st_mode) == 0o755
    assert stat.S_IMODE((restored / "_internal" / "data.txt").stat().st_mode) == 0o644
    link = restored / "_internal" / "libfoo.so"
    assert link.is_symlink() and os.readlink(link) == "lib/libfoo.so.1"
    assert link.read_bytes() == b"\x7fELFlib"


@posix_only
def test_symlinks_escaping_the_package_are_rejected(tmp_path: Path) -> None:
    root = tmp_path / "dist"
    root.mkdir()
    os.symlink("../../etc/passwd", root / "escape")
    with pytest.raises(OSError):
        posix_zip.write_tree(root, tmp_path / "pkg.zip", prefix="app")


def test_extract_rejects_path_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../outside.txt", "x")
    with pytest.raises(OSError):
        posix_zip.extract(archive, tmp_path / "out")
    assert not (tmp_path / "outside.txt").exists()


def test_extract_rejects_symlink_entries_pointing_outside(tmp_path: Path) -> None:
    archive = tmp_path / "evil.zip"
    info = zipfile.ZipInfo("app/link")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(info, "../../outside")
    with pytest.raises(OSError):
        posix_zip.extract(archive, tmp_path / "out")
