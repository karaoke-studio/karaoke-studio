from __future__ import annotations

from pathlib import Path

from krok_helper.cli import parse_args


def test_parse_args_accepts_sug_project_path() -> None:
    args = parse_args(["D:/songs/project.sug"])

    assert args.project == Path("D:/songs/project.sug")
    assert args.video is None


def test_parse_args_keeps_project_optional_for_default_gui() -> None:
    args = parse_args([])

    assert args.project is None
