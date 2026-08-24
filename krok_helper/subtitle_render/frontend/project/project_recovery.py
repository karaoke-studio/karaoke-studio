"""User-decision orchestration for subtitle project crash recovery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Protocol

from krok_helper.subtitle_render.project_recovery import RecoveryScan
from krok_helper.subtitle_render.project_store import RecoveryCandidate


class RecoveryScanner(Protocol):
    """Minimum policy contract needed by the recovery prompt controller."""

    def scan(self) -> RecoveryScan:
        """Return recovery entries that still require a user decision."""


ChoicePrompt = Callable[..., int]
ErrorPrompt = Callable[..., Any]
RestoreCandidate = Callable[[RecoveryCandidate], bool]


@dataclass(frozen=True)
class ProjectRecoveryController:
    """Translate recovery inventory into the existing Chinese prompt flow."""

    scanner: RecoveryScanner

    def has_pending(self) -> bool:
        """Return whether startup recovery requires user attention."""
        return self.scanner.scan().requires_attention

    def check(
        self,
        parent: Any,
        *,
        choose: ChoicePrompt,
        show_error: ErrorPrompt,
        restore: RestoreCandidate,
    ) -> bool:
        """Prompt over corrupt and valid snapshots; report a successful restore."""
        scan = self.scanner.scan()
        for path in scan.invalid_paths:
            choice = choose(
                parent,
                "字幕项目恢复文件损坏",
                f"无法读取以下恢复文件：\n{path}\n\n可以删除该文件，或保留以便手动检查。",
                ("删除", "保留"),
                default=1,
            )
            if choice == 0:
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    show_error(parent, "删除恢复文件失败", f"{path}\n\n{exc}")

        for candidate in scan.candidates:
            source = candidate.source_project_path
            source_text = str(source) if source is not None else "未命名字幕项目"
            saved_at = datetime.fromtimestamp(candidate.created_at_unix).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            choice = choose(
                parent,
                "发现字幕项目恢复数据",
                f"项目：{source_text}\n恢复快照时间：{saved_at}\n\n是否恢复？",
                ("恢复", "放弃", "稍后处理"),
                default=2,
            )
            if choice == 1:
                try:
                    candidate.path.unlink(missing_ok=True)
                except OSError as exc:
                    show_error(
                        parent,
                        "删除恢复文件失败",
                        f"{candidate.path}\n\n{exc}",
                    )
                continue
            if choice != 0:
                continue
            if restore(candidate):
                return True
        return False
