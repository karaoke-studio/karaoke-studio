"""LaunchPlan 与随包 Updater CLI 的交接契约测试。"""

import os
import sys
from pathlib import Path

from krok_helper.updater import installer
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


def test_launch_updater_uses_temp_cwd_and_fresh_pyinstaller_environment(
    tmp_path: Path, monkeypatch
):
    app_dir = tmp_path / "app"
    temp_dir = tmp_path / "temp"
    app_dir.mkdir()
    temp_dir.mkdir()
    installed_updater = app_dir / installer.UPDATER_EXE_NAME
    temp_updater = temp_dir / installer.UPDATER_EXE_NAME
    installed_updater.write_bytes(b"installed")
    temp_updater.write_bytes(b"temp")
    captured = {}

    class FakeProcess:
        pid = 4321

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured.update(kwargs)
        return FakeProcess()

    plan = _plan(app_dir=app_dir)
    monkeypatch.setattr(installer, "_update_updater_from_remote", lambda *args, **kwargs: False)
    monkeypatch.setattr(installer, "_copy_updater_to_temp", lambda path: temp_updater)
    monkeypatch.setattr(installer.subprocess, "Popen", fake_popen)

    result = installer.launch_updater(plan)

    assert result.launched is True
    assert result.pid == 4321
    assert captured["cwd"] == str(temp_dir)
    assert captured["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert captured["env"][installer.UPDATE_DESCENDANTS_ENV].startswith("[")
    assert captured["env"][installer.UPDATE_SOURCE_VERSION_ENV] == installer.APP_VERSION
    assert (
        captured["env"][installer.UPDATE_BOOTSTRAP_RESULT_ENV]
        == "fallback_old_updater"
    )
    assert captured["env"] is not os.environ
