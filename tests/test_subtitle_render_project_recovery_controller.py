"""Focused interaction contracts for the project recovery controller."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from krok_helper.subtitle_render.frontend.project.project_recovery import (
    ProjectRecoveryController,
)
from krok_helper.subtitle_render.project.recovery import RecoveryScan
from krok_helper.subtitle_render.project.store import RecoveryCandidate


def _candidate(path: Path, *, source: Path | None = None) -> RecoveryCandidate:
    return RecoveryCandidate(
        path=path,
        source_project_path=source,
        created_at_unix=1_725_148_800.0,
        snapshot_id=10,
    )


def test_recovery_controller_reports_pending_inventory(tmp_path: Path) -> None:
    invalid = tmp_path / "broken.recovery"
    scanner = SimpleNamespace(scan=lambda: RecoveryScan((), (invalid,)))

    assert ProjectRecoveryController(scanner).has_pending() is True


def test_recovery_controller_preserves_corrupt_then_restore_prompt_order(
    tmp_path: Path,
) -> None:
    invalid = tmp_path / "broken.recovery"
    invalid.write_text("broken", encoding="utf-8")
    recovery = tmp_path / "song.recovery"
    recovery.write_text("snapshot", encoding="utf-8")
    source = tmp_path / "song.yurika"
    candidate = _candidate(recovery, source=source)
    scanner = SimpleNamespace(scan=lambda: RecoveryScan((candidate,), (invalid,)))
    prompts: list[tuple[tuple, dict]] = []
    choices = iter((0, 0))
    restored: list[RecoveryCandidate] = []

    result = ProjectRecoveryController(scanner).check(
        "parent",
        choose=lambda *args, **kwargs: (
            prompts.append((args, kwargs)),
            next(choices),
        )[1],
        show_error=lambda *_args, **_kwargs: None,
        restore=lambda item: (restored.append(item), True)[1],
    )

    assert result is True
    assert not invalid.exists()
    assert restored == [candidate]
    assert prompts[0][0][1] == "字幕项目恢复文件损坏"
    assert prompts[0][0][3] == ("删除", "保留")
    assert prompts[0][1] == {"default": 1}
    assert prompts[1][0][1] == "发现字幕项目恢复数据"
    assert str(source) in prompts[1][0][2]
    assert prompts[1][0][3] == ("恢复", "放弃", "稍后处理")
    assert prompts[1][1] == {"default": 2}


def test_recovery_controller_discard_deletes_snapshot_without_restoring(
    tmp_path: Path,
) -> None:
    recovery = tmp_path / "untitled.recovery"
    recovery.write_text("snapshot", encoding="utf-8")
    candidate = _candidate(recovery)
    scanner = SimpleNamespace(scan=lambda: RecoveryScan((candidate,), ()))
    restore_calls: list[RecoveryCandidate] = []

    result = ProjectRecoveryController(scanner).check(
        None,
        choose=lambda *_args, **_kwargs: 1,
        show_error=lambda *_args, **_kwargs: None,
        restore=lambda item: (restore_calls.append(item), True)[1],
    )

    assert result is False
    assert not recovery.exists()
    assert restore_calls == []
