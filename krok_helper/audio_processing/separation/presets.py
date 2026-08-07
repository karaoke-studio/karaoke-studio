"""Versioned, user-facing task presets for the first PyMSS release."""

from __future__ import annotations

from dataclasses import dataclass

from .states import TaskType

PRESET_VERSION = 2


@dataclass(frozen=True)
class SeparationStep:
    model: str
    stems: tuple[str, ...]
    output_labels: tuple[str, ...]
    size_bytes: int
    input_from_previous: str = ""
    inference_params: tuple[tuple[str, object], ...] = ()

    def params(self) -> dict:
        return dict(self.inference_params)


@dataclass(frozen=True)
class TaskPreset:
    preset_id: str
    version: int
    task: TaskType
    steps: tuple[SeparationStep, ...]

    @property
    def download_bytes(self) -> int:
        return sum(step.size_bytes for step in self.steps)


TASK_PRESETS: dict[TaskType, TaskPreset] = {
    TaskType.VOCAL: TaskPreset(
        "karaoke-vocal-big-beta5e",
        PRESET_VERSION,
        TaskType.VOCAL,
        (
            SeparationStep(
                "big_beta5e",
                ("vocals",),
                ("人声",),
                1_479_749_810,
            ),
        ),
    ),
    TaskType.INSTRUMENTAL: TaskPreset(
        "karaoke-instrumental-inst-v1e",
        PRESET_VERSION,
        TaskType.INSTRUMENTAL,
        (
            SeparationStep(
                "inst_v1e",
                ("other",),
                ("伴奏",),
                913_102_724,
            ),
        ),
    ),
    TaskType.HARMONY: TaskPreset(
        "karaoke-harmony-becruily-inst-v1e",
        PRESET_VERSION,
        TaskType.HARMONY,
        (
            SeparationStep(
                "mel_band_roformer_karaoke_becruily",
                ("Vocals", "Instrumental"),
                ("主唱", ""),
                1_719_139_254,
            ),
            # The karaoke residual contains accompaniment plus backing
            # vocals.  A vocal/instrumental model extracts the backing vocal
            # from that residual while the lead output above remains intact.
            SeparationStep(
                "inst_v1e",
                ("vocals",),
                ("和声",),
                913_102_724,
                input_from_previous="Instrumental",
            ),
        ),
    ),
}


__all__ = ["PRESET_VERSION", "SeparationStep", "TASK_PRESETS", "TaskPreset"]
