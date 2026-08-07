"""音频分离（PyMSS）的服务状态、任务定义与展示元数据。

状态清单与页面表现对齐
``docs/音视频处理-PyMSS音频分离需求设计.md`` §3.4 / §3.6 / §6.1。
本模块只描述状态与 UI 归一化文案，不包含任何真实后端逻辑。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from qfluentwidgets import FluentIcon as FIF


class ServiceState(str, Enum):
    """PyMSS 服务状态机（需求文档 §6.1，共 22 个状态）。"""

    UNCONFIGURED = "unconfigured"
    LOCATION_REQUIRED = "location_required"
    RUNTIME_DOWNLOADING = "runtime_downloading"
    RUNTIME_VERIFYING = "runtime_verifying"
    INSTALL_MISSING = "install_missing"
    INSTALL_DAMAGED = "install_damaged"
    VERSION_INCOMPATIBLE = "version_incompatible"
    EXTERNAL_VERSION_INCOMPATIBLE = "external_version_incompatible"
    INSTALLED_STOPPED = "installed_stopped"
    SERVICE_STARTING = "service_starting"
    SERVICE_READY = "service_ready"
    MODEL_REQUIRED = "model_required"
    MODEL_DOWNLOADING = "model_downloading"
    MODEL_LOADING = "model_loading"
    EXTERNAL_MODEL_READY = "external_model_ready"
    EXTERNAL_MODEL_MISSING = "external_model_missing"
    EXTERNAL_MODEL_CHANGED = "external_model_changed"
    EXTERNAL_MODEL_UNSUPPORTED = "external_model_unsupported"
    PROCESSING = "processing"
    SERVICE_STOPPING = "service_stopping"
    EXTERNAL_OFFLINE = "external_offline"
    ERROR = "error"


class StateLevel(str, Enum):
    INFO = "info"
    BUSY = "busy"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


#: 状态条主操作 key（由页面统一分发到后端）。
ACTION_CONFIGURE = "configure"
ACTION_CANCEL_INSTALL = "cancel_install"
ACTION_REPAIR = "repair"
ACTION_RESELECT_ENV = "reselect_env"
ACTION_UPDATE_RUNTIME = "update_runtime"
ACTION_START_SERVICE = "start_service"
ACTION_CANCEL_START = "cancel_start"
ACTION_STOP_SERVICE = "stop_service"
ACTION_CANCEL_DOWNLOAD = "cancel_download"
ACTION_STOP_TASK = "stop_task"
ACTION_RELOCATE_MODEL = "relocate_model"
ACTION_RESCAN_MODEL = "rescan_model"
ACTION_RECONNECT = "reconnect"
ACTION_RETRY = "retry"


@dataclass(frozen=True)
class StateMeta:
    """单个服务状态的 UI 归一化展示。"""

    label: str
    level: StateLevel
    detail: str = ""
    primary_action: str | None = None
    primary_label: str = ""
    secondary_action: str | None = None
    secondary_label: str = ""


STATE_META: dict[ServiceState, StateMeta] = {
    ServiceState.UNCONFIGURED: StateMeta(
        "未配置", StateLevel.INFO, "尚未安装或连接 PyMSS，请先完成首次配置",
        ACTION_CONFIGURE, "开始配置",
    ),
    ServiceState.LOCATION_REQUIRED: StateMeta(
        "待确认安装目录", StateLevel.INFO, "请在向导中确认安装位置",
    ),
    ServiceState.RUNTIME_DOWNLOADING: StateMeta(
        "正在下载运行时", StateLevel.BUSY, "PyMSS 运行时下载中，可随时取消",
        ACTION_CANCEL_INSTALL, "取消",
    ),
    ServiceState.RUNTIME_VERIFYING: StateMeta(
        "正在校验安装", StateLevel.BUSY, "校验文件完整性并切换运行时，请稍候",
    ),
    ServiceState.INSTALL_MISSING: StateMeta(
        "安装缺失", StateLevel.WARNING, "安装目录已被删除或移动",
        ACTION_REPAIR, "修复安装",
    ),
    ServiceState.INSTALL_DAMAGED: StateMeta(
        "安装损坏", StateLevel.WARNING, "运行时文件不完整或被修改",
        ACTION_REPAIR, "修复安装",
    ),
    ServiceState.VERSION_INCOMPATIBLE: StateMeta(
        "PyMSS 需要更新", StateLevel.WARNING, "托管运行时版本与当前工作台不兼容",
        ACTION_UPDATE_RUNTIME, "更新 PyMSS",
    ),
    ServiceState.EXTERNAL_VERSION_INCOMPATIBLE: StateMeta(
        "外部 PyMSS 版本不兼容", StateLevel.WARNING,
        "请先自行升级外部环境，或重新选择兼容环境",
        ACTION_RESELECT_ENV, "重新选择环境",
    ),
    ServiceState.INSTALLED_STOPPED: StateMeta(
        "已安装，服务未启动", StateLevel.INFO, "启动本地 PyMSS 服务后即可开始分离",
        ACTION_START_SERVICE, "启动服务",
    ),
    ServiceState.SERVICE_STARTING: StateMeta(
        "服务启动中", StateLevel.BUSY, "正在等待服务健康检查通过",
        ACTION_CANCEL_START, "取消启动",
    ),
    ServiceState.SERVICE_READY: StateMeta(
        "服务已就绪", StateLevel.SUCCESS, "选择音频与任务即可开始分离",
        None, "",
        ACTION_STOP_SERVICE, "停止服务",
    ),
    ServiceState.MODEL_REQUIRED: StateMeta(
        "需要下载模型", StateLevel.INFO, "当前任务所需模型尚未下载，可在任务卡上按需下载",
    ),
    ServiceState.MODEL_DOWNLOADING: StateMeta(
        "正在下载模型", StateLevel.BUSY, "模型下载中，完成后将自动继续任务",
        ACTION_CANCEL_DOWNLOAD, "取消下载",
    ),
    ServiceState.MODEL_LOADING: StateMeta(
        "正在加载模型", StateLevel.BUSY, "服务正在加载或切换模型",
    ),
    ServiceState.EXTERNAL_MODEL_READY: StateMeta(
        "外部模型已就绪", StateLevel.SUCCESS, "已映射并验证 MSST 模型，可直接开始任务",
        None, "",
        ACTION_STOP_SERVICE, "停止服务",
    ),
    ServiceState.EXTERNAL_MODEL_MISSING: StateMeta(
        "外部模型不可用", StateLevel.WARNING, "外部模型的权重或配置路径已失效",
        ACTION_RELOCATE_MODEL, "重新定位",
        ACTION_STOP_SERVICE, "停止服务",
    ),
    ServiceState.EXTERNAL_MODEL_CHANGED: StateMeta(
        "外部模型已变化", StateLevel.WARNING, "外部模型文件与导入记录不一致，需要重新验证",
        ACTION_RESCAN_MODEL, "重新扫描验证",
        ACTION_STOP_SERVICE, "停止服务",
    ),
    ServiceState.EXTERNAL_MODEL_UNSUPPORTED: StateMeta(
        "外部模型不兼容", StateLevel.WARNING, "模型架构或输出不符合任务要求，请改选模型",
        ACTION_RESCAN_MODEL, "重新选择模型",
        ACTION_STOP_SERVICE, "停止服务",
    ),
    ServiceState.PROCESSING: StateMeta(
        "正在分离处理", StateLevel.BUSY, "任务进行中，可随时停止",
        ACTION_STOP_TASK, "停止任务",
    ),
    ServiceState.SERVICE_STOPPING: StateMeta(
        "正在停止服务", StateLevel.BUSY, "等待服务退出并释放设备",
    ),
    ServiceState.EXTERNAL_OFFLINE: StateMeta(
        "外部服务离线", StateLevel.ERROR, "无法连接外部 PyMSS 服务地址",
        ACTION_RECONNECT, "重新连接",
        ACTION_RESELECT_ENV, "修改地址或凭据",
    ),
    ServiceState.ERROR: StateMeta(
        "出现错误", StateLevel.ERROR, "",
        ACTION_RETRY, "重试",
    ),
}

#: 安装/下载进行中（页面锁定冲突操作）的状态组。
BUSY_STATES = frozenset(
    state for state, meta in STATE_META.items() if meta.level is StateLevel.BUSY
)

#: 服务可用、可以提交任务的状态组。
TASK_CAPABLE_STATES = frozenset(
    {
        ServiceState.SERVICE_READY,
        ServiceState.MODEL_REQUIRED,
        ServiceState.EXTERNAL_MODEL_READY,
        ServiceState.EXTERNAL_MODEL_MISSING,
        ServiceState.EXTERNAL_MODEL_CHANGED,
        ServiceState.EXTERNAL_MODEL_UNSUPPORTED,
    }
)


class TaskType(str, Enum):
    """三类固定音频分离任务（需求文档 §8.1）。"""

    VOCAL = "vocal"
    INSTRUMENTAL = "instrumental"
    HARMONY = "harmony"


@dataclass(frozen=True)
class TaskSpec:
    """面向用户展示的任务说明（不暴露底层模型与推理参数）。"""

    task: TaskType
    title: str
    icon: FIF
    description: str
    expected_outputs: str
    output_labels: tuple[str, ...]


TASK_SPECS: dict[TaskType, TaskSpec] = {
    TaskType.VOCAL: TaskSpec(
        TaskType.VOCAL,
        "分离人声",
        FIF.MICROPHONE,
        "得到去掉伴奏的人声轨。",
        "预计输出 1 个文件：*_人声",
        ("人声",),
    ),
    TaskType.INSTRUMENTAL: TaskSpec(
        TaskType.INSTRUMENTAL,
        "分离伴奏",
        FIF.MUSIC,
        "得到可用于卡拉 OK 的伴奏轨。",
        "预计输出 1 个文件：*_伴奏",
        ("伴奏",),
    ),
    TaskType.HARMONY: TaskSpec(
        TaskType.HARMONY,
        "提取和声",
        FIF.PEOPLE,
        "经过两阶段处理，得到主唱与和声两轨。",
        "预计输出 2 个文件：*_主唱、*_和声",
        ("主唱", "和声"),
    ),
}

#: 任务的真实阶段（需求文档 §9.3——PyMSS 无逐块进度，只展示可知阶段）。
TASK_STAGES: tuple[str, ...] = (
    "准备音频",
    "下载模型",
    "加载模型",
    "分离处理中",
    "编码/接收输出",
    "保存结果",
)

#: 阶段索引常量，与 TASK_STAGES 对应。
STAGE_PREPARE = 0
STAGE_DOWNLOAD = 1
STAGE_LOAD = 2
STAGE_SEPARATE = 3
STAGE_ENCODE = 4
STAGE_SAVE = 5


@dataclass
class TaskDependency:
    """单个任务当前的依赖可用性（模型状态）。"""

    task: TaskType
    ready: bool
    badge: str = ""
    reason: str = ""
    download_bytes: int = 0
    is_external: bool = False


def format_size(num_bytes: int) -> str:
    """以 GB/MB 显示模型或下载体积，例如 ``1.48 GB``。"""
    if num_bytes <= 0:
        return "0 MB"
    gib = num_bytes / (1024**3)
    if gib >= 1:
        return f"{gib:.2f} GB"
    mib = num_bytes / (1024**2)
    return f"{mib:.0f} MB"


def format_elapsed(seconds: float) -> str:
    """耗时显示：``mm:ss``。"""
    total = max(0, int(seconds))
    return f"{total // 60:02d}:{total % 60:02d}"
