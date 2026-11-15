"""User-decision orchestration for subtitle project crash recovery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from krok_helper.subtitle_render.project.recovery import RecoveryScan
from krok_helper.subtitle_render.project.store import (
    RecoveryCandidate,
    load_render_project,
    save_discarded_project_backup,
)


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
        discard_backup_root: Optional[Path] = None,
    ) -> bool:
        """Prompt over corrupt and valid snapshots; report a successful restore.

        ``discard_backup_root`` 传入备份根目录时，「放弃」的快照先落一份
        ``discarded-backup`` 再删除——挂死/崩溃类缺陷的现场往往只存在于引发
        问题的那份快照里，直接删除后无法定向复现（2026-10 4.3.7 更新后
        恢复快照挂死即因快照被删而无法追因）。备份写失败时保留原文件并提示
        （现场保全优先）；快照本身解析失败（已损坏）则照删，无可备份内容。
        """
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
                if discard_backup_root is not None:
                    try:
                        payload = load_render_project(candidate.path, resolve_resources=False)
                    except ValueError:
                        payload = None  # 快照已损坏，无可备份内容
                    if payload is not None:
                        payload.pop("recovery", None)
                        try:
                            save_discarded_project_backup(
                                discard_backup_root,
                                payload,
                                source_project_path=source,
                            )
                        except OSError as exc:
                            show_error(
                                parent,
                                "备份恢复文件失败",
                                "未能为放弃的快照创建备份，原文件已保留，"
                                f"可手动复制后再处理：\n{candidate.path}\n\n{exc}",
                            )
                            continue
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
