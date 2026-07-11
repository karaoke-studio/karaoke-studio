"""LaunchPlan 与随包 Updater CLI 的交接契约测试。"""

import sys
from pathlib import Path

from krok_helper.updater.installer import LaunchPlan


def _plan(**overrides):
    values = dict(
        app_dir=Path("C:/Karaoke Studio"),
        app_exe_name="Karaoke Studio.exe",
        target_version="3.2.0",
        target_tag="v3.2.0",
        asset_name="KaraokeStudio-windows.zip",
        download_urls=[("github", "https://example.invalid/full.zip"), ("ghproxy", "https://mirror.invalid/full.zip")],
        proxy_url="http://127.0.0.1:7890",
        expected_sha256="deadbeef",
    )
    values.update(overrides)
    return LaunchPlan(**values)


def test_command_args_preserve_exe_layout_tag_and_asset_contract():
    args = _plan().command_args(Path("C:/Temp/Updater.exe"), current_pid=1234)
    assert args[args.index("--app-exe") + 1] == "Karaoke Studio.exe"
    assert args[args.index("--internal-name") + 1] == "_internal"
    assert args[args.index("--target-tag") + 1] == "v3.2.0"
    assert args[args.index("--asset-name") + 1] == "KaraokeStudio-windows.zip"
    assert args[args.index("--pid") + 1] == "1234"


def test_each_source_is_serialized_as_an_independent_url():
    args = _plan().command_args(Path("C:/Temp/Updater.exe"), current_pid=1)
    positions = [index for index, value in enumerate(args) if value == "--url"]
    assert [args[index + 1] for index in positions] == [
        "github|https://example.invalid/full.zip",
        "ghproxy|https://mirror.invalid/full.zip",
    ]


def test_optional_flags_are_omitted_when_disabled():
    args = _plan(proxy_url="", expected_sha256="", launch_after_update=False).command_args(
        Path("C:/Temp/Updater.exe"), current_pid=1
    )
    assert "--proxy" not in args
    assert "--sha256" not in args
    assert "--no-launch" in args


def test_shipped_updater_parser_roundtrip():
    submodule_root = Path(__file__).resolve().parents[1] / "krok_helper" / "lyrics_timing"
    sys.path.insert(0, str(submodule_root))
    try:
        from updater_app.main import parse_args

        command = _plan().command_args(Path("C:/Temp/Updater.exe"), current_pid=777)
        parsed = parse_args(command[1:])
    finally:
        sys.path.remove(str(submodule_root))
    assert parsed.target_version == "3.2.0"
    assert parsed.target_tag == "v3.2.0"
    assert parsed.asset_name == "KaraokeStudio-windows.zip"
    assert parsed.pid == 777
    assert parsed.sha256 == "deadbeef"
