from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PROJECT_ROOT.parents[1]
SUG_ROOT = REPO_ROOT / "krok_helper" / "lyrics_timing"


def pyinstaller_args(*, clean: bool = False) -> list[str]:
    args = [
        str(PROJECT_ROOT / "main.py"),
        "--name=Updater",
        "--onefile",
        "--windowed",
        "--noconfirm",
        "--distpath",
        str(PROJECT_ROOT / "dist"),
        "--workpath",
        str(PROJECT_ROOT / "build"),
        "--specpath",
        str(PROJECT_ROOT),
        "--paths",
        str(REPO_ROOT),
        "--paths",
        str(SUG_ROOT),
        "--collect-submodules=requests",
        "--hidden-import=updater_app.main",
        "--hidden-import=updater_app.gui",
        "--hidden-import=PyQt6.QtCore",
        "--hidden-import=PyQt6.QtGui",
        "--hidden-import=PyQt6.QtWidgets",
        "--hidden-import=qfluentwidgets",
        "--hidden-import=requests",
        "--hidden-import=urllib3",
        "--hidden-import=charset_normalizer",
        "--hidden-import=idna",
        "--hidden-import=certifi",
        "--hidden-import=colorsys",
        "--hidden-import=encodings",
        "--hidden-import=encodings.idna",
        "--hidden-import=encodings.utf_8",
        "--hidden-import=encodings.utf_8_sig",
        "--hidden-import=encodings.cp1252",
        "--hidden-import=encodings.cp437",
        "--hidden-import=encodings.cp65001",
        "--hidden-import=encodings.gbk",
        "--hidden-import=encodings.mbcs",
        "--hidden-import=hashlib",
        "--hidden-import=zipfile",
        "--hidden-import=tempfile",
        "--hidden-import=ssl",
        "--hidden-import=_ssl",
        "--exclude-module=numpy",
        "--exclude-module=sounddevice",
        "--exclude-module=soundfile",
        "--exclude-module=pedalboard",
        "--exclude-module=av",
        "--exclude-module=pykakasi",
        "--exclude-module=sudachipy",
        "--exclude-module=sudachidict_core",
        "--exclude-module=jaconv",
        "--exclude-module=matplotlib",
        "--exclude-module=scipy",
        "--exclude-module=tkinter",
    ]
    icon = REPO_ROOT / "krok_helper" / "lyrics_timing" / "src" / "strange_uta_game" / "resource" / "icon.ico"
    if icon.exists():
        args.append(f"--icon={icon}")
    if clean:
        args.append("--clean")
    return args


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Karaoke Studio GUI Updater.exe")
    parser.add_argument("--clean", action="store_true", default=False)
    args = parser.parse_args()

    try:
        import PyInstaller.__main__ as pyinstaller
    except ImportError:
        print("Missing pyinstaller. Please install it before building Updater.exe.", file=sys.stderr)
        return 1

    pyinstaller.run(pyinstaller_args(clean=args.clean))
    exe = PROJECT_ROOT / "dist" / "Updater.exe"
    if not exe.exists():
        print("Build finished, but dist/Updater.exe was not found.", file=sys.stderr)
        return 1
    print(f"Built {exe} ({exe.stat().st_size / 1024 / 1024:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

