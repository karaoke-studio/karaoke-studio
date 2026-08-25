"""Focused contracts for native project command prompts."""

from __future__ import annotations

from pathlib import Path

import pytest

from krok_helper.subtitle_render.frontend.project.project_commands import (
    ProjectCommandController,
    ProjectSaveAction,
)


@pytest.mark.parametrize(
    ("dirty", "choice", "save_result", "expected", "save_calls", "discard_calls"),
    [
        (False, 2, False, True, 0, 0),
        (True, 0, True, True, 1, 0),
        (True, 0, False, False, 1, 0),
        (True, 1, False, True, 0, 1),
        (True, 2, True, False, 0, 0),
        (True, -1, True, False, 0, 0),
    ],
)
def test_project_command_controller_preserves_unsaved_decisions(
    dirty: bool,
    choice: int,
    save_result: bool,
    expected: bool,
    save_calls: int,
    discard_calls: int,
) -> None:
    prompts: list[tuple[tuple, dict]] = []
    saves: list[None] = []
    discards: list[None] = []

    result = ProjectCommandController.confirm_discard(
        "parent",
        dirty=dirty,
        choose=lambda *args, **kwargs: (
            prompts.append((args, kwargs)),
            choice,
        )[1],
        save=lambda: (saves.append(None), save_result)[1],
        discard=lambda: discards.append(None),
    )

    assert result is expected
    assert len(saves) == save_calls
    assert len(discards) == discard_calls
    assert len(prompts) == int(dirty)
    if dirty:
        assert prompts[0][0][1:] == (
            "未保存的改动",
            "当前项目有未保存的改动，是否先保存？",
            ["保存", "放弃", "取消"],
        )
        assert prompts[0][1] == {"default": 2}


def test_project_command_controller_chooses_open_path_from_current_directory(
    tmp_path: Path,
) -> None:
    controller = ProjectCommandController("project filter", ".yurika")
    current = tmp_path / "current" / "old.yurika"
    selected = tmp_path / "next" / "new.yurika"
    calls: list[tuple] = []

    result = controller.choose_open_path(
        "parent",
        current_project_path=current,
        choose_file=lambda *args: (calls.append(args), (str(selected), ""))[1],
    )

    assert result == selected
    assert calls == [
        ("parent", "打开字幕渲染项目", str(current.parent), "project filter")
    ]


def test_project_command_controller_builds_save_default_and_suffix(
    tmp_path: Path,
) -> None:
    controller = ProjectCommandController("project filter", ".yurika")
    subtitle = tmp_path / "lyrics.sug"
    selected = tmp_path / "saved" / "song"
    calls: list[tuple] = []

    result = controller.choose_save_path(
        "parent",
        current_project_path=None,
        subtitle_path=subtitle,
        video_path=tmp_path / "video.mp4",
        current_directory=tmp_path,
        choose_file=lambda *args: (calls.append(args), (str(selected), ""))[1],
    )

    assert result == Path(f"{selected}.yurika")
    assert calls == [
        (
            "parent",
            "保存字幕渲染项目",
            str(tmp_path / "lyrics.yurika"),
            "project filter",
        )
    ]


def test_project_command_controller_keeps_named_path_and_cancel(tmp_path: Path) -> None:
    controller = ProjectCommandController("project filter", ".yurika")
    current = tmp_path / "current.yurika"
    calls: list[tuple] = []

    result = controller.choose_save_path(
        None,
        current_project_path=current,
        subtitle_path=None,
        video_path=None,
        current_directory=tmp_path,
        choose_file=lambda *args: (calls.append(args), ("", ""))[1],
    )

    assert result is None
    assert calls[0][2] == str(current)


def test_project_save_preflight_skips_inspection_for_a_new_path(tmp_path: Path) -> None:
    inspections: list[Path] = []
    result = ProjectCommandController.preflight_save(
        None,
        path=tmp_path / "new.yurika",
        current_project_path=tmp_path / "current.yurika",
        known_disk_revision=object(),
        inspect=lambda path: inspections.append(path),
        choose=lambda *_args, **_kwargs: pytest.fail("new path must not prompt"),
    )

    assert result.action == ProjectSaveAction.CONTINUE
    assert inspections == []


def test_project_save_preflight_reports_inspection_failure(tmp_path: Path) -> None:
    path = tmp_path / "current.yurika"

    def fail_inspection(_path):
        raise OSError("inspection failed")

    result = ProjectCommandController.preflight_save(
        None,
        path=path,
        current_project_path=path,
        known_disk_revision=object(),
        inspect=fail_inspection,
        choose=lambda *_args, **_kwargs: pytest.fail("failed inspection must not prompt"),
    )

    assert result.action == ProjectSaveAction.INSPECTION_FAILED
    assert result.error == "inspection failed"


def test_project_save_preflight_accepts_the_known_disk_revision(tmp_path: Path) -> None:
    path = tmp_path / "current.yurika"
    result = ProjectCommandController.preflight_save(
        None,
        path=path,
        current_project_path=path,
        known_disk_revision="same-revision",
        inspect=lambda _path: "same-revision",
        choose=lambda *_args, **_kwargs: pytest.fail("matching revision must not prompt"),
    )

    assert result.action == ProjectSaveAction.CONTINUE


@pytest.mark.parametrize(
    ("choice", "expected"),
    [
        (0, ProjectSaveAction.CONTINUE),
        (1, ProjectSaveAction.SAVE_AS),
        (2, ProjectSaveAction.CANCEL),
        (-1, ProjectSaveAction.CANCEL),
    ],
)
def test_project_save_preflight_preserves_external_change_choices(
    tmp_path: Path,
    choice: int,
    expected: ProjectSaveAction,
) -> None:
    path = tmp_path / "current.yurika"
    prompts: list[tuple[tuple, dict]] = []
    result = ProjectCommandController.preflight_save(
        "parent",
        path=path,
        current_project_path=path,
        known_disk_revision="opened",
        inspect=lambda _path: "external",
        choose=lambda *args, **kwargs: (
            prompts.append((args, kwargs)),
            choice,
        )[1],
    )

    assert result.action == expected
    assert prompts[0][0] == (
        "parent",
        "项目文件已被外部修改",
        f"磁盘上的项目文件在打开或上次保存后发生了变化：\n"
        f"{path}\n\n直接覆盖可能丢失其他程序的修改。",
        ("覆盖", "另存为", "取消"),
    )
    assert prompts[0][1] == {"default": 2}
