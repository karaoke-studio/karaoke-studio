"""Export location, preview, and progress presentation components."""

from __future__ import annotations

from math import isfinite
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QSize, Qt
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
    FluentIcon as FIF,
    LineEdit as FluentLineEdit,
    PrimaryPushButton as FluentPrimaryPushButton,
    PushButton as FluentPushButton,
    RadioButton as FluentRadioButton,
    SimpleCardWidget,
    SpinBox as FluentSpinBox,
    StrongBodyLabel,
)

from krok_helper.qfluent_compat import ModelessDialog
from krok_helper.subtitle_render.engine.export.encoder_select import (
    ENCODER_AUTO,
    ENCODER_CPU,
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

EXPORT_DIR_SOURCE_VIDEO = "source_video"
EXPORT_DIR_CUSTOM = "custom"
EXPORT_PREVIEW_DEFAULT_WIDTH = 640
EXPORT_PREVIEW_MIN_WIDTH = 320


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
