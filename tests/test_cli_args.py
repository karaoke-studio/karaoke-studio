from __future__ import annotations

from pathlib import Path

from krok_helper.cli import parse_args


def test_parse_args_accepts_sug_project_path() -> None:
    args = parse_args(["D:/songs/project.sug"])

    assert args.project == Path("D:/songs/project.sug")
    assert args.video is None


def test_parse_args_accepts_yurika_project_path() -> None:
    args = parse_args(["D:/songs/subtitle project.yurika"])

    assert args.project == Path("D:/songs/subtitle project.yurika")
    assert args.video is None


def test_parse_args_keeps_project_optional_for_default_gui() -> None:
    args = parse_args([])

    assert args.project is None


def test_parse_args_accepts_hidden_package_spawn_smoke() -> None:
    args = parse_args(["--package-spawn-smoke"])

    assert args.package_spawn_smoke is True


def test_parse_args_accepts_hidden_package_updater_smoke() -> None:
    args = parse_args(["--package-updater-smoke"])
    assert args.package_updater_smoke is True


def test_youtube_child_dispatch_does_not_start_main_gui(monkeypatch, tmp_path):
    import sys
    from krok_helper import cli
    from krok_helper.video_download import youtube_login_window
    request = tmp_path / "request.json"
    seen = []
    monkeypatch.setattr(sys, "argv", ["app.py", "--youtube-web-login", "--youtube-login-request", str(request), "--youtube-login-mode", "firefox"])
    monkeypatch.setattr(youtube_login_window, "main", lambda args: seen.append(args) or 0)
    monkeypatch.setattr(cli, "run_gui", lambda args: (_ for _ in ()).throw(AssertionError("main GUI must not start")))
    assert cli.main() == 0
    assert seen == [["--request", str(request), "--mode", "firefox"]]
