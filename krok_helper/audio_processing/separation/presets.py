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


#: 用户在设置里覆盖任务模型时写入的 namespace 键。
TASK_MODEL_OVERRIDES_KEY = "task_model_overrides"


def task_override(settings_ns: dict, task: TaskType) -> dict | None:
    """取某个任务的用户模型覆盖；格式不完整一律视为没有覆盖。

    只认「模型 + stem」都齐全的记录：stem 必须是用户从该模型真实输出轨里选出来的，
    缺一半的记录不能拿来跑任务（§8.2 预设必须完整）。
    """
    raw = settings_ns.get(TASK_MODEL_OVERRIDES_KEY)
    if not isinstance(raw, dict):
        return None
    entry = raw.get(task.value)
    if not isinstance(entry, dict):
        return None
    model = str(entry.get("model") or "").strip()
    stem = str(entry.get("stem") or "").strip()
    if not model or not stem:
        return None
    return {
        "model": model,
        "stem": stem,
        "size_bytes": max(0, int(entry.get("size_bytes") or 0)),
    }


def effective_steps(settings_ns: dict, task: TaskType) -> tuple[SeparationStep, ...]:
    """任务实际要跑的步骤：有用户覆盖就用覆盖，否则用推荐预设。

    覆盖一律是单步——设置界面只让用户为一个任务挑一个模型和一个输出轨。
    """
    override = task_override(settings_ns, task)
    if override is None:
        return TASK_PRESETS[task].steps
    label = TASK_PRESETS[task].steps[-1].output_labels[-1]
    return (
        SeparationStep(
            override["model"],
            (override["stem"],),
            (label,),
            override["size_bytes"],
        ),
    )


__all__ = [
    "PRESET_VERSION",
    "TASK_MODEL_OVERRIDES_KEY",
    "SeparationStep",
    "TASK_PRESETS",
    "TaskPreset",
    "effective_steps",
    "task_override",
]
