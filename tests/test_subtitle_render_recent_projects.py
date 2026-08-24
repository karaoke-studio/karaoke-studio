from __future__ import annotations

from pathlib import Path

from krok_helper.subtitle_render.project.recent import RecentProjectPolicy


def test_recent_project_policy_prunes_invalid_duplicate_and_excess_paths(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.yurika"
    second = tmp_path / "second.yurika"
    foreign = tmp_path / "foreign.n3proj"
    for path in (first, second, foreign):
        path.write_text("content", encoding="utf-8")
    policy = RecentProjectPolicy(project_suffix=".yurika", limit=1)

    normalized = policy.normalize(
        [
            "",
            3,
            str(tmp_path / "missing.yurika"),
            str(foreign),
            str(first),
            str(first),
            str(second),
        ]
    )

    assert normalized == [str(first.absolute())]


def test_recent_project_policy_moves_recorded_path_to_front(tmp_path: Path) -> None:
    first = tmp_path / "first.yurika"
    second = tmp_path / "second.yurika"
    third = tmp_path / "third.yurika"
    policy = RecentProjectPolicy(project_suffix=".yurika", limit=2)

    assert policy.record([str(first), str(second)], second) == [
        str(second.absolute()),
        str(first),
    ]
    assert policy.record([str(first), str(second)], third) == [
        str(third.absolute()),
        str(first),
    ]


def test_recent_project_path_key_uses_platform_normalization(tmp_path: Path) -> None:
    relative = tmp_path / "folder" / ".." / "song.yurika"

    assert RecentProjectPolicy.path_key(relative) == RecentProjectPolicy.path_key(
        tmp_path / "song.yurika"
    )


def test_recent_project_policy_honors_zero_limit(tmp_path: Path) -> None:
    project = tmp_path / "song.yurika"
    project.write_text("content", encoding="utf-8")
    policy = RecentProjectPolicy(project_suffix=".yurika", limit=0)

    assert policy.normalize([str(project)]) == []
    assert policy.record([], project) == []
