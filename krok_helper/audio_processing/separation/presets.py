"""Versioned, user-facing task presets for the first PyMSS release."""

from __future__ import annotations

from dataclasses import dataclass

from .states import TaskType

#: 预设变更后必须递增：缓存键含预设版本，否则会错误复用旧模型产出的中间/最终文件（§8.2）。
PRESET_VERSION = 3


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
    # 人声与伴奏共用同一个双输出模型：装一次 inst_v1e，两个任务都可用。
    TaskType.VOCAL: TaskPreset(
        "karaoke-vocal-inst-v1e",
        PRESET_VERSION,
        TaskType.VOCAL,
        (
            SeparationStep(
                "inst_v1e",
                ("vocals",),
                ("人声",),
                913_102_724,
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
    # Karaoke 模型直接处理原曲：残余轨 other 是「去掉主唱、保留和声」的伴奏，
    # 因此这里产出的是和声伴奏，不是纯和声——命名与文案必须如实反映（§8.1）。
    TaskType.HARMONY: TaskPreset(
        "karaoke-harmony-inst-aufr33-viperx",
        PRESET_VERSION,
        TaskType.HARMONY,
        (
            SeparationStep(
                "model_mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956",
                ("other",),
                ("和声伴奏",),
                913_096_801,
            ),
        ),
    ),
}


__all__ = ["PRESET_VERSION", "SeparationStep", "TASK_PRESETS", "TaskPreset"]
