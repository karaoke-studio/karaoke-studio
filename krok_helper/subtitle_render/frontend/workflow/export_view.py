"""Export location, preview, and progress presentation components."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import Optional, Sequence

from PyQt6.QtCore import QSize, Qt, pyqtSignal as Signal
from PyQt6.QtGui import QColor, QImage, QPixmap
from PyQt6.QtWidgets import (
    QButtonGroup,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    CaptionLabel,
    CheckBox,
    ComboBox as FluentComboBox,
    FluentIcon as FIF,
    LineEdit as FluentLineEdit,
    PrimaryPushButton as FluentPrimaryPushButton,
    ProgressBar as FluentProgressBar,
    PushButton as FluentPushButton,
    RadioButton as FluentRadioButton,
    SimpleCardWidget,
    SpinBox as FluentSpinBox,
    StrongBodyLabel,
    ToolButton as FluentToolButton,
)

from krok_helper.qfluent_compat import ModelessDialog
from krok_helper.subtitle_render.engine.export.encoder_select import (
    CODEC_H264,
    CODEC_HEVC,
    CPU_PRESETS,
    ENCODER_AMF,
    ENCODER_AUTO,
    ENCODER_CPU,
    ENCODER_NVENC,
    ENCODER_QSV,
)
from krok_helper.subtitle_render.engine.export.render_job import (
    OUTPUT_FORMAT_MOV_QTRLE,
    OUTPUT_FORMAT_MOV_TRANSPARENT,
    OUTPUT_FORMAT_MP4,
    OUTPUT_FORMAT_PNG_COMPOSITED,
    OUTPUT_FORMAT_PNG_TRANSPARENT,
)
from krok_helper.subtitle_render.domain.models import (
    DEFAULT_EXPORT_NAME_TEMPLATE,
    EXPORT_NAME_TEMPLATE_FIELDS,
)
from krok_helper.subtitle_render.frontend.widgets.theme import (
    palette,
    stage_bg,
    themed,
)
from krok_helper.subtitle_render.frontend.preview.player_window import AspectRatioBox

EXPORT_DIR_SOURCE_VIDEO = "source_video"
EXPORT_DIR_CUSTOM = "custom"
EXPORT_PREVIEW_DEFAULT_WIDTH = 640
EXPORT_PREVIEW_MIN_WIDTH = 320

EXPORT_FORMAT_CHOICES: tuple[tuple[str, str], ...] = (
    (OUTPUT_FORMAT_MP4, "MP4 视频"),
    (OUTPUT_FORMAT_PNG_TRANSPARENT, "PNG 序列（透明字幕）"),
    (OUTPUT_FORMAT_PNG_COMPOSITED, "PNG 序列（含背景）"),
    (OUTPUT_FORMAT_MOV_TRANSPARENT, "透明视频 ProRes 4444"),
    (OUTPUT_FORMAT_MOV_QTRLE, "透明视频 QuickTime 动画"),
)
"""(output_format, 显示名) 供导出页「输出格式」下拉与状态栏文案共用。"""

EXPORT_FORMAT_SUFFIX_LABELS = {
    OUTPUT_FORMAT_MP4: ".mp4",
    OUTPUT_FORMAT_MOV_TRANSPARENT: ".mov",
    OUTPUT_FORMAT_MOV_QTRLE: ".mov",
    OUTPUT_FORMAT_PNG_TRANSPARENT: "\\ PNG 序列文件夹",
    OUTPUT_FORMAT_PNG_COMPOSITED: "\\ PNG 序列文件夹",
}
"""导出文件名输入框后缀徽标文案（PNG 序列以导出名建子文件夹）。"""


def make_card_icon_badge(icon: FIF) -> QLabel:
    """Build the themed icon badge shared by export cards."""
    badge = QLabel()
    badge.setObjectName("SrExportCardBadge")
    badge.setFixedSize(26, 26)
    badge.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def _qss() -> str:
        # themed() 主题切换时重跑 factory，顺带把 pixmap 换成当前品牌色
        p = palette()
        badge.setPixmap(
            icon.icon(color=QColor(p.accent_primary)).pixmap(QSize(14, 14))
        )
        tint = (
            "rgba(255, 122, 140, 0.18)"
            if p.is_dark
            else "rgba(255, 90, 111, 0.12)"
        )
        return f"#SrExportCardBadge {{ background: {tint}; border-radius: 8px; }}"

    themed(badge, _qss)
    return badge


def make_export_card(
    title_text: str,
    theme_labels: list[QWidget],
    header_action: Optional[QWidget] = None,
    icon: Optional[FIF] = None,
) -> tuple[SimpleCardWidget, QVBoxLayout]:
    """Build one export settings card while retaining themed labels."""
    card = SimpleCardWidget()
    layout = QVBoxLayout(card)
    layout.setContentsMargins(20, 14, 20, 16)
    layout.setSpacing(10)
    header = StrongBodyLabel(title_text)
    theme_labels.append(header)
    header_row = QHBoxLayout()
    header_row.setContentsMargins(0, 0, 0, 0)
    header_row.setSpacing(8)
    if icon is not None:
        header_row.addWidget(
            make_card_icon_badge(icon),
            0,
            Qt.AlignmentFlag.AlignVCenter,
        )
    header_row.addWidget(header)
    header_row.addStretch(1)
    if header_action is not None:
        header_row.addWidget(header_action, 0, Qt.AlignmentFlag.AlignVCenter)
    layout.addLayout(header_row)
    return card, layout


def make_export_spin(
    minimum: int,
    maximum: int,
    value: int,
    suffix: str,
) -> FluentSpinBox:
    """Build an export numeric field with the established dimensions."""
    spin = FluentSpinBox()
    spin.setRange(minimum, maximum)
    spin.setValue(value)
    spin.setSuffix(suffix)
    spin.setMinimumHeight(32)
    return spin


def make_labeled_export_control(
    label_text: str,
    control: QWidget,
    theme_labels: list[QWidget],
) -> QWidget:
    """Wrap an export control with its retained semantic caption."""
    box = QWidget()
    # 工作台全局 QSS 会给裸 QWidget 刷底色，在白色卡片里会显出灰块
    box.setObjectName("SrExportFieldBox")
    themed(box, lambda: "#SrExportFieldBox { background: transparent; }")
    layout = QVBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(4)
    label = CaptionLabel(label_text)
    theme_labels.append(label)
    layout.addWidget(label)
    layout.addWidget(control)
    return box


def sync_export_preset_enabled(encoder_combo, preset_combo) -> None:
    """Apply the existing CPU-preset availability rule to export controls."""
    mode = str(encoder_combo.currentData() or ENCODER_CPU)
    cpu_possible = mode in (ENCODER_CPU, ENCODER_AUTO)
    preset_combo.setEnabled(cpu_possible)
    preset_combo.setToolTip(
        "" if cpu_possible else "CPU preset 仅在 CPU / libx264 编码时生效。"
    )


class ExportLocationDialog(ModelessDialog):
    """字幕视频导出目录与文件名偏好。"""

    def __init__(
        self,
        mode: str,
        custom_dir: str,
        initial_dir: Path,
        parent: Optional[QWidget] = None,
        name_template: str = DEFAULT_EXPORT_NAME_TEMPLATE,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("导出视频位置与命名")
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        title = StrongBodyLabel("导出视频位置", self)
        layout.addWidget(title)
        self.source_radio = FluentRadioButton("保存在字幕视频所在目录", self)
        self.custom_radio = FluentRadioButton("保存在指定目录", self)
        group = QButtonGroup(self)
        group.addButton(self.source_radio)
        group.addButton(self.custom_radio)
        layout.addWidget(self.source_radio)
        layout.addWidget(self.custom_radio)

        directory_row = QHBoxLayout()
        directory_row.setContentsMargins(24, 0, 0, 0)
        directory_row.setSpacing(8)
        self.directory_edit = FluentLineEdit(self)
        self.directory_edit.setReadOnly(True)
        self.directory_edit.setPlaceholderText("选择指定目录")
        self.directory_edit.setText(custom_dir)
        self.browse_button = FluentPushButton(FIF.FOLDER, "浏览", self)
        directory_row.addWidget(self.directory_edit, 1)
        directory_row.addWidget(self.browse_button)
        layout.addLayout(directory_row)

        hint = CaptionLabel(
            "没有视频素材时，将使用背景素材或字幕文件所在目录。",
            self,
        )
        layout.addWidget(hint)

        layout.addSpacing(6)
        layout.addWidget(StrongBodyLabel("默认文件名", self))
        self.name_template_edit = FluentLineEdit(self)
        self.name_template_edit.setPlaceholderText(DEFAULT_EXPORT_NAME_TEMPLATE)
        self.name_template_edit.setText(name_template)
        layout.addWidget(self.name_template_edit)

        placeholder_lines = "；".join(
            f"{{{name}}} {desc}" for name, desc in EXPORT_NAME_TEMPLATE_FIELDS.items()
        )
        name_hint = CaptionLabel(
            f"可用占位符：{placeholder_lines}。不用写 .mp4。\n"
            f"留空则用默认：{DEFAULT_EXPORT_NAME_TEMPLATE}。"
            "改动只影响之后新载入的素材，不会改掉你已经手填的文件名。",
            self,
        )
        name_hint.setWordWrap(True)
        layout.addWidget(name_hint)
        self.name_error_label = CaptionLabel("", self)
        self.name_error_label.setWordWrap(True)
        self.name_error_label.setVisible(False)
        layout.addWidget(self.name_error_label)
        self.name_template_edit.textChanged.connect(self._sync_controls)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        cancel_button = FluentPushButton("取消", self)
        self.ok_button = FluentPrimaryPushButton("确定", self)
        cancel_button.clicked.connect(self.reject)
        self.ok_button.clicked.connect(self.accept)
        button_row.addWidget(cancel_button)
        button_row.addWidget(self.ok_button)
        layout.addLayout(button_row)

        self._initial_dir = initial_dir
        self.source_radio.setChecked(mode != EXPORT_DIR_CUSTOM)
        self.custom_radio.setChecked(mode == EXPORT_DIR_CUSTOM)
        self.source_radio.toggled.connect(self._sync_controls)
        self.custom_radio.toggled.connect(self._sync_controls)
        self.browse_button.clicked.connect(self._browse)
        self._sync_controls()

    def _browse(self) -> None:
        start = self.directory_edit.text().strip() or str(self._initial_dir)
        selected = QFileDialog.getExistingDirectory(
            self, "选择字幕视频导出目录", start
        )
        if selected:
            self.directory_edit.setText(selected)
            self.custom_radio.setChecked(True)
            self._sync_controls()

    def _name_template_error(self) -> str:
        """模板不合法时的原因；空串表示可用。留空视为用默认。"""
        from krok_helper.errors import ProcessingError
        from krok_helper.pipeline import validate_output_name_template

        template = self.name_template_edit.text().strip()
        if not template:
            return ""
        try:
            validate_output_name_template(
                template, "导出文件名", set(EXPORT_NAME_TEMPLATE_FIELDS)
            )
        except ProcessingError as exc:
            return str(exc)
        return ""

    def _sync_controls(self) -> None:
        custom = self.custom_radio.isChecked()
        self.directory_edit.setEnabled(custom)
        self.browse_button.setEnabled(custom)
        # 模板写错就在这里拦住，别等到点了导出才报错。
        error = self._name_template_error()
        self.name_error_label.setText(error)
        self.name_error_label.setVisible(bool(error))
        self.ok_button.setEnabled(
            (not custom or bool(self.directory_edit.text().strip())) and not error
        )

    def selection(self) -> tuple[str, str]:
        mode = EXPORT_DIR_CUSTOM if self.custom_radio.isChecked() else EXPORT_DIR_SOURCE_VIDEO
        return mode, self.directory_edit.text().strip()

    def name_template(self) -> str:
        return self.name_template_edit.text().strip() or DEFAULT_EXPORT_NAME_TEMPLATE


def export_preview_width(
    view_size: QSize,
    device_pixel_ratio: float,
    output_width: int,
    output_height: int,
) -> int:
    """Return the fitted preview width in physical pixels."""
    safe_output_width = max(int(output_width), 1)
    fallback = min(safe_output_width, EXPORT_PREVIEW_DEFAULT_WIDTH)
    if (
        view_size.width() <= 0
        or view_size.height() <= 0
        or output_width <= 0
        or output_height <= 0
        or not isfinite(device_pixel_ratio)
        or device_pixel_ratio <= 0
    ):
        return fallback
    fitted_logical_width = min(
        float(view_size.width()),
        float(view_size.height()) * output_width / output_height,
    )
    physical_width = int(round(fitted_logical_width * device_pixel_ratio))
    return min(safe_output_width, max(EXPORT_PREVIEW_MIN_WIDTH, physical_width))


def physical_preview_size(size: QSize, device_pixel_ratio: float) -> QSize:
    """Convert a logical widget size to a positive physical-pixel size."""
    dpr = device_pixel_ratio if isfinite(device_pixel_ratio) and device_pixel_ratio > 0 else 1.0
    return QSize(
        max(int(round(size.width() * dpr)), 1),
        max(int(round(size.height() * dpr)), 1),
    )


def scaled_preview_pixmap(
    frame: QPixmap,
    logical_size: QSize,
    device_pixel_ratio: float,
) -> QPixmap:
    """Scale a frame for a logical widget while retaining physical pixels."""
    dpr = device_pixel_ratio if isfinite(device_pixel_ratio) and device_pixel_ratio > 0 else 1.0
    target_size = physical_preview_size(logical_size, dpr)
    pixmap = frame.scaled(
        target_size,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    if pixmap.size() != target_size:
        pixmap = pixmap.copy(
            max((pixmap.width() - target_size.width()) // 2, 0),
            max((pixmap.height() - target_size.height()) // 2, 0),
            target_size.width(),
            target_size.height(),
        )
    pixmap.setDevicePixelRatio(dpr)
    return pixmap


class ExportMonitorView(QLabel):
    """导出预览画面（仿 N3 出力预览）：保持纵横比缩放显示最近合成帧。

    无帧时显示占位文案；有帧后 resize 会用原图重新缩放，避免累积模糊。
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._frame: Optional[QPixmap] = None
        self.setMinimumSize(1, 1)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        themed(
            self,
            lambda: (
                f"background: {stage_bg()}; border-radius: 8px;"
                f" color: {palette().text_hint}; font-size: 10pt;"
            ),
        )
        self.clear_frame()

    def set_frame(self, image: QImage) -> None:
        self._frame = QPixmap.fromImage(image)
        self.setText("")
        self._rescale()

    def clear_frame(self) -> None:
        self._frame = None
        self.setPixmap(QPixmap())
        self.setText("准备开始导出")

    def _rescale(self) -> None:
        if self._frame is None or self._frame.isNull():
            return
        self.setPixmap(
            scaled_preview_pixmap(
                self._frame,
                self.size(),
                float(self.devicePixelRatioF()),
            )
        )

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._rescale()


@dataclass(frozen=True)
class ExportWorkspaceControls:
    """Explicit widget contract consumed by the application coordinator."""

    theme_labels: list[QWidget]
    settings_col: QWidget
    location_settings_button: FluentToolButton
    directory_edit: FluentLineEdit
    browse_button: FluentPushButton
    name_edit: FluentLineEdit
    format_combo: FluentComboBox
    name_suffix_label: QLabel
    width_spin: FluentSpinBox
    height_spin: FluentSpinBox
    fps_combo: FluentComboBox
    encoder_combo: FluentComboBox
    codec_combo: FluentComboBox
    preset_combo: FluentComboBox
    crf_spin: FluentSpinBox
    render_workers_combo: FluentComboBox
    native_check: CheckBox
    gpu_preview_check: CheckBox
    gpu_export_check: CheckBox
    monitor_card: SimpleCardWidget
    monitor_layout: QVBoxLayout
    eta_label: CaptionLabel
    monitor_header: QHBoxLayout
    monitor_view: ExportMonitorView
    monitor_frame: AspectRatioBox
    format_label: CaptionLabel
    progress: FluentProgressBar
    status_label: CaptionLabel
    start_button: FluentPrimaryPushButton
    stop_button: FluentPushButton


class ExportWorkspaceView(QWidget):
    """Own the export workspace widgets and report user intent via signals."""

    locationSettingsRequested = Signal()
    directoryEditingFinished = Signal()
    browseRequested = Signal()
    encoderChanged = Signal()
    codecChanged = Signal()
    formatChanged = Signal()
    startRequested = Signal()
    stopRequested = Signal()

    def __init__(
        self,
        *,
        fps_options: Sequence[int],
        render_worker_options: Sequence[int],
        gpu_preview_checked: bool,
        gpu_controls_visible: bool,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("SubtitleExportPage")
        themed(
            self,
            lambda: "#SubtitleExportPage { background: transparent; }",
        )
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 4, 24, 16)
        outer.setSpacing(10)

        # 内容列限制最大宽度并水平居中，宽屏下表单不再拉满整行。
        column = QWidget()
        column.setObjectName("SrExportColumn")
        themed(column, lambda: "#SrExportColumn { background: transparent; }")
        column.setMaximumWidth(1200)
        column.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        layout = QVBoxLayout(column)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        center_row = QHBoxLayout()
        center_row.setContentsMargins(0, 0, 0, 0)
        center_row.addStretch(1)
        center_row.addWidget(column)
        center_row.addStretch(1)
        outer.addLayout(center_row, 1)

        # qfluentwidgets 语义标签自行跟随主题；保留实例引用，防止被 GC 移出
        # styleSheetManager 的 WeakKeyDictionary 后主题失效（同 SUG 导出页的教训）。
        theme_labels: list[QWidget] = []
        body_row = QHBoxLayout()
        body_row.setContentsMargins(0, 0, 0, 0)
        body_row.setSpacing(16)
        settings_col = QWidget()
        settings_col.setObjectName("SrExportSettingsCol")
        themed(
            settings_col,
            lambda: "#SrExportSettingsCol { background: transparent; }",
        )
        settings_col.setFixedWidth(430)
        settings_layout = QVBoxLayout(settings_col)
        settings_layout.setContentsMargins(0, 0, 0, 0)
        settings_layout.setSpacing(12)

        location_settings_button = FluentToolButton(FIF.SETTING)
        location_settings_button.setToolTip("导出视频位置与默认文件名设置")
        location_settings_button.setFixedSize(30, 30)
        location_settings_button.setIconSize(QSize(16, 16))
        location_settings_button.clicked.connect(
            lambda _checked=False: self.locationSettingsRequested.emit()
        )
        output_card, output_layout = make_export_card(
            "输出文件",
            theme_labels,
            location_settings_button,
            icon=FIF.SAVE_AS,
        )
        dir_row = QHBoxLayout()
        dir_row.setContentsMargins(0, 0, 0, 0)
        dir_row.setSpacing(8)
        directory_edit = FluentLineEdit()
        directory_edit.setPlaceholderText("选择输出文件夹")
        directory_edit.editingFinished.connect(self.directoryEditingFinished.emit)
        browse_button = FluentPushButton(FIF.FOLDER, "浏览")
        browse_button.clicked.connect(
            lambda _checked=False: self.browseRequested.emit()
        )
        dir_row.addWidget(directory_edit, 1)
        dir_row.addWidget(browse_button)
        output_layout.addLayout(dir_row)

        # 输出格式独占一行：与文件名同行会严重挤压文件名输入框。
        format_row = QHBoxLayout()
        format_row.setContentsMargins(0, 0, 0, 0)
        format_row.setSpacing(8)
        format_combo = FluentComboBox()
        format_combo.setMinimumHeight(32)
        for format_value, format_text in EXPORT_FORMAT_CHOICES:
            format_combo.addItem(format_text, userData=format_value)
        format_combo.setToolTip(
            "PNG 序列输出到以导出名命名的独立子文件夹（名称_000001.png 起）；"
            "透明视频两种编码均带 alpha 通道：ProRes 4444 体积大但剪辑软件"
            "兼容性最好（推荐），QuickTime 动画无损且体积小——注意多数播放器"
            "不合成透明视频的 alpha（显示为黑底），属正常现象，导入剪辑软件"
            "即为透明。"
        )
        format_combo.currentIndexChanged.connect(
            lambda _index: self.formatChanged.emit()
        )
        format_row.addWidget(
            make_labeled_export_control("输出格式", format_combo, theme_labels)
        )
        output_layout.addLayout(format_row)

        name_row = QHBoxLayout()
        name_row.setContentsMargins(0, 0, 0, 0)
        name_row.setSpacing(8)
        name_edit = FluentLineEdit()
        name_edit.setPlaceholderText("文件名（默认：视频文件名_yurika出力）")
        name_suffix = QLabel(".mp4")
        name_suffix.setObjectName("SrExportExtBadge")
        themed(
            name_suffix,
            lambda: (
                "#SrExportExtBadge {{ color: {fg}; background: {bg};"
                " border-radius: 6px; padding: 4px 8px;"
                " font-size: 9pt; font-weight: 600; }}"
            ).format(
                fg=palette().accent_primary,
                bg=(
                    "rgba(255, 122, 140, 0.16)"
                    if palette().is_dark
                    else "rgba(255, 90, 111, 0.10)"
                ),
            ),
        )
        name_row.addWidget(name_edit, 1)
        name_row.addWidget(name_suffix)
        output_layout.addLayout(name_row)
        settings_layout.addWidget(output_card)

        params_card, params_layout = make_export_card(
            "画面与编码",
            theme_labels,
            icon=FIF.VIDEO,
        )
        sync_hint = QLabel("宽度 / 高度 / 帧率与预览页的「画面」设置双向联动。")
        sync_hint.setObjectName("SrExportSyncHint")
        sync_hint.setWordWrap(True)
        themed(
            sync_hint,
            lambda: (
                "#SrExportSyncHint {{ background: {bg}; color: {fg};"
                " border-radius: 6px; padding: 6px 10px; font-size: 9pt; }}"
            ).format(
                bg="#26313F" if palette().is_dark else "#EEF4FF",
                fg="#A6C8FF" if palette().is_dark else "#3D6BBF",
            ),
        )
        params_layout.addWidget(sync_hint)

        params_row = QHBoxLayout()
        params_row.setContentsMargins(0, 0, 0, 0)
        params_row.setSpacing(10)
        width_spin = make_export_spin(160, 7680, 1920, "")
        height_spin = make_export_spin(90, 4320, 1080, "")
        # 关闭键盘跟踪，确保高度变化仅在提交最终值时按 N3 语义重算。
        width_spin.setKeyboardTracking(False)
        height_spin.setKeyboardTracking(False)
        fps_combo = FluentComboBox()
        fps_combo.setMinimumHeight(32)
        for fps in fps_options:
            fps_combo.addItem(f"{fps} fps", userData=fps)
        params_row.addWidget(
            make_labeled_export_control("宽度", width_spin, theme_labels)
        )
        params_row.addWidget(
            make_labeled_export_control("高度", height_spin, theme_labels)
        )
        params_row.addWidget(
            make_labeled_export_control("帧率", fps_combo, theme_labels)
        )
        params_layout.addLayout(params_row)

        encode_row = QHBoxLayout()
        encode_row.setContentsMargins(0, 0, 0, 0)
        encode_row.setSpacing(10)
        encoder_combo = FluentComboBox()
        encoder_combo.setMinimumHeight(32)
        encoder_combo.addItem("CPU 软编", userData=ENCODER_CPU)
        encoder_combo.addItem("自动硬编", userData=ENCODER_AUTO)
        encoder_combo.addItem("NVIDIA NVENC", userData=ENCODER_NVENC)
        encoder_combo.addItem("Intel QSV", userData=ENCODER_QSV)
        encoder_combo.addItem("AMD AMF", userData=ENCODER_AMF)
        encoder_combo.currentIndexChanged.connect(
            lambda _index: self.encoderChanged.emit()
        )
        codec_combo = FluentComboBox()
        codec_combo.setMinimumHeight(32)
        codec_combo.addItem("H.264 (AVC)", userData=CODEC_H264)
        codec_combo.addItem("H.265 (HEVC)", userData=CODEC_HEVC)
        codec_combo.setToolTip(
            "H.265 同画质体积更小，但编码更慢、老设备兼容性略差。"
        )
        codec_combo.currentIndexChanged.connect(
            lambda _index: self.codecChanged.emit()
        )
        encode_row.addWidget(
            make_labeled_export_control("编码器", encoder_combo, theme_labels)
        )
        encode_row.addWidget(
            make_labeled_export_control("视频编码", codec_combo, theme_labels)
        )
        params_layout.addLayout(encode_row)

        quality_row = QHBoxLayout()
        quality_row.setContentsMargins(0, 0, 0, 0)
        quality_row.setSpacing(10)
        preset_combo = FluentComboBox()
        preset_combo.setMinimumHeight(32)
        for preset in CPU_PRESETS:
            preset_combo.addItem(preset, userData=preset)
        preset_combo.setCurrentText("medium")
        crf_spin = make_export_spin(0, 51, 18, "")
        crf_spin.setToolTip(
            "CRF 质量：数值越小画质越高、文件越大；18 约为视觉无损。"
        )
        quality_row.addWidget(
            make_labeled_export_control("CPU preset", preset_combo, theme_labels)
        )
        quality_row.addWidget(
            make_labeled_export_control("质量 (CRF)", crf_spin, theme_labels)
        )
        params_layout.addLayout(quality_row)

        render_workers_combo = FluentComboBox()
        render_workers_combo.setMinimumHeight(32)
        render_workers_combo.addItem("自动（最多 8 进程）", userData=0)
        for workers in render_worker_options[1:]:
            render_workers_combo.addItem(f"{workers} 进程", userData=workers)
        render_workers_combo.setToolTip(
            "字幕帧渲染进程数。12/16 适合核心数较多且内存充足的电脑；"
            "进程越多不一定越快，且会明显增加内存占用。"
        )
        params_layout.addWidget(
            make_labeled_export_control(
                "渲染进程",
                render_workers_combo,
                theme_labels,
            )
        )
        settings_layout.addWidget(params_card)

        # 显式 parent 防止可见 CheckBox 在加入布局前短暂成为顶层窗口。
        native_check = CheckBox("实验：使用 native 字幕渲染器导出", settings_col)
        native_check.setChecked(False)
        native_check.setEnabled(False)
        native_check.setVisible(False)
        native_check.setToolTip("native 字幕渲染器暂时停用。")
        gpu_preview_check = CheckBox("使用 GPU 渲染字幕预览", settings_col)
        gpu_preview_check.setChecked(gpu_preview_checked)
        gpu_preview_check.setVisible(gpu_controls_visible)
        gpu_preview_check.setToolTip(
            "使用稳定的 G5 shared-memory/QImage 路径加速字幕透明层；"
            "不可用或失败时自动回退 Painter。"
        )
        gpu_export_check = CheckBox("使用 GPU 渲染字幕导出", settings_col)
        gpu_export_check.setChecked(gpu_controls_visible)
        gpu_export_check.setVisible(gpu_controls_visible)
        gpu_export_check.setToolTip(
            "仅用 Direct2D 渲染字幕条带，仍由当前 ffmpeg 编码器输出；"
            "失败时会删除半成品并从头回退 Painter。"
        )
        settings_layout.addWidget(gpu_preview_check)
        settings_layout.addWidget(gpu_export_check)
        settings_layout.addWidget(native_check)
        settings_layout.addStretch(1)

        monitor_card = SimpleCardWidget()
        monitor_layout = QVBoxLayout(monitor_card)
        monitor_layout.setContentsMargins(20, 14, 20, 16)
        monitor_layout.setSpacing(10)
        monitor_header = QHBoxLayout()
        monitor_header.setContentsMargins(0, 0, 0, 0)
        monitor_title = StrongBodyLabel("导出预览")
        theme_labels.append(monitor_title)
        eta_label = CaptionLabel("")
        monitor_header.setSpacing(8)
        monitor_header.addWidget(
            make_card_icon_badge(FIF.MOVIE),
            0,
            Qt.AlignmentFlag.AlignVCenter,
        )
        monitor_header.addWidget(monitor_title)
        monitor_header.addStretch(1)
        monitor_header.addWidget(eta_label)
        monitor_layout.addLayout(monitor_header)
        monitor_view = ExportMonitorView()
        monitor_frame = AspectRatioBox(
            monitor_view,
            aspect_ratio=width_spin.value() / height_spin.value(),
        )
        monitor_frame.setMinimumSize(240, 135)
        monitor_layout.addWidget(monitor_frame, 1)
        format_label = CaptionLabel("输出格式: MP4 · H.264 (AVC)")
        monitor_layout.addWidget(format_label)

        body_row.addWidget(settings_col, 0, Qt.AlignmentFlag.AlignTop)
        body_row.addWidget(monitor_card, 0, Qt.AlignmentFlag.AlignTop)
        body_row.addStretch(1)
        layout.addStretch(1)
        layout.addLayout(body_row)
        layout.addStretch(1)

        progress = FluentProgressBar()
        progress.setRange(0, 1)
        progress.setValue(0)
        layout.addWidget(progress)

        status_label = CaptionLabel("")
        status_label.setWordWrap(True)
        status_label.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        layout.addWidget(status_label)

        action_row = QHBoxLayout()
        action_row.setContentsMargins(0, 0, 0, 0)
        action_row.setSpacing(8)
        start_button = FluentPrimaryPushButton(FIF.PLAY, "开始导出")
        start_button.setMinimumHeight(38)
        start_button.clicked.connect(
            lambda _checked=False: self.startRequested.emit()
        )
        stop_button = FluentPushButton(FIF.CLOSE, "停止导出")
        stop_button.setMinimumHeight(38)
        stop_button.setEnabled(False)
        stop_button.clicked.connect(
            lambda _checked=False: self.stopRequested.emit()
        )
        action_row.addWidget(start_button, 1)
        action_row.addWidget(stop_button)
        layout.addLayout(action_row)

        self.controls = ExportWorkspaceControls(
            theme_labels=theme_labels,
            settings_col=settings_col,
            location_settings_button=location_settings_button,
            directory_edit=directory_edit,
            browse_button=browse_button,
            name_edit=name_edit,
            format_combo=format_combo,
            name_suffix_label=name_suffix,
            width_spin=width_spin,
            height_spin=height_spin,
            fps_combo=fps_combo,
            encoder_combo=encoder_combo,
            codec_combo=codec_combo,
            preset_combo=preset_combo,
            crf_spin=crf_spin,
            render_workers_combo=render_workers_combo,
            native_check=native_check,
            gpu_preview_check=gpu_preview_check,
            gpu_export_check=gpu_export_check,
            monitor_card=monitor_card,
            monitor_layout=monitor_layout,
            eta_label=eta_label,
            monitor_header=monitor_header,
            monitor_view=monitor_view,
            monitor_frame=monitor_frame,
            format_label=format_label,
            progress=progress,
            status_label=status_label,
            start_button=start_button,
            stop_button=stop_button,
        )


def format_eta_seconds(seconds: float) -> str:
    """导出剩余时间的短文案：1 小时 5 分 / 3 分 20 秒 / 45 秒。"""
    total = max(int(round(seconds)), 0)
    if total >= 3600:
        return f"{total // 3600} 小时 {total % 3600 // 60} 分"
    if total >= 60:
        return f"{total // 60} 分 {total % 60} 秒"
    return f"{total} 秒"


def format_elapsed_seconds(seconds: float) -> str:
    """Format a completed export duration with minutes and seconds."""
    total = max(int(round(seconds)), 0)
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分 {seconds} 秒"
    return f"{minutes} 分 {seconds} 秒"


def format_warning_lines(warnings: list) -> str:
    """把余白警告压成「第 1、3 行」式的短文案，最多点名 4 行。"""
    numbers = [str(w.line_index + 1) for w in warnings[:4]]
    text = f"第 {'、'.join(numbers)} 行"
    if len(warnings) > 4:
        text += f" 等 {len(warnings)} 行"
    return text


__all__ = [
    "EXPORT_DIR_CUSTOM",
    "EXPORT_DIR_SOURCE_VIDEO",
    "EXPORT_PREVIEW_DEFAULT_WIDTH",
    "EXPORT_PREVIEW_MIN_WIDTH",
    "ExportLocationDialog",
    "ExportMonitorView",
    "ExportWorkspaceControls",
    "ExportWorkspaceView",
    "export_preview_width",
    "format_elapsed_seconds",
    "format_eta_seconds",
    "format_warning_lines",
    "make_card_icon_badge",
    "make_export_card",
    "make_export_spin",
    "make_labeled_export_control",
    "physical_preview_size",
    "scaled_preview_pixmap",
    "sync_export_preset_enabled",
]
