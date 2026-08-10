"""全局设置里「波形对齐」那一页的内容 —— 由对齐页自己提供。

以前这两块面板是**全局设置对话框**搭的，值也由它隔空读写对齐页的属性：
``_host.align_video_name_template_value``、甚至 ``_host.align_video_zone.path``
（穿两层去摸一张 drop card）。对齐页对象化之后这笔账就还得起了：设置界面归
提供设置的那一页，对话框只负责把它嵌进自己的版式里。

对话框和这里的约定只有两条：
* 构造出来就是填好当前值的一块 ``QWidget``，往版式里 ``addWidget`` 即可；
* 按下保存时调一次 :meth:`AlignmentSettingsFragment.apply`，不合法抛
  ``ProcessingError``（对话框照原样弹错误卡片）。

控件用的是 qfluentwidgets 那一套（``LineEdit`` / ``PushButton`` / ``RadioButton``）
—— 从 ``PyQt6.QtWidgets`` 导同名类会让控件退回系统外观，这个坑踩过好几次。
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtWidgets import (
    QButtonGroup,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import LineEdit as QLineEdit, PushButton as QPushButton, RadioButton as QRadioButton

from krok_helper.settings import (
    ALIGN_OUTPUT_DIR_CUSTOM,
    ALIGN_OUTPUT_DIR_SOURCE_VIDEO,
    DEFAULT_ALIGNED_AUDIO_NAME_TEMPLATE,
    DEFAULT_ALIGNED_VIDEO_NAME_TEMPLATE,
)
from krok_helper.theme_workbench import palette as wb_palette, themed as wb_themed


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    wb_themed(
        label,
        lambda: f'font-family: "Microsoft YaHei UI"; font-size: 9pt; color: {wb_palette().text_hint};',
    )
    return label


def _panel(title: str) -> tuple[QFrame, QGridLayout]:
    panel = QFrame()
    panel.setObjectName("WhitePanel")
    layout = QGridLayout(panel)
    layout.setContentsMargins(14, 14, 14, 14)
    layout.setHorizontalSpacing(10)
    layout.setVerticalSpacing(10)
    heading = QLabel(title)
    heading.setObjectName("PanelTitle")
    layout.addWidget(heading, 0, 0)
    return panel, layout


class AlignmentSettingsFragment(QWidget):
    """对齐导出的命名与位置两块面板。"""

    def __init__(self, page, *, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._page = page

        shell = QVBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(18)

        video_template, audio_template = page.name_templates()
        dir_mode, custom_dir = page.output_dir_settings()

        naming_panel, naming = _panel("对齐导出命名")
        self._video_template_edit = QLineEdit(self)
        self._video_template_edit.setText(video_template)
        self._audio_template_edit = QLineEdit(self)
        self._audio_template_edit.setText(audio_template)
        naming.addWidget(QLabel("对齐后视频模板"), 1, 0)
        naming.addWidget(self._video_template_edit, 1, 1)
        naming.addWidget(QLabel("对齐后音频模板"), 2, 0)
        naming.addWidget(self._audio_template_edit, 2, 1)
        naming.addWidget(
            _hint("默认: 对齐后视频 {video_name}_aligned.mp4；对齐后音频 {audio_name}_aligned.wav。"), 3, 1
        )
        naming.addWidget(
            _hint("视频模板支持 {video_name}；音频模板支持 {audio_name} 和 {video_name}。不用写扩展名。"), 4, 1
        )
        naming.setColumnStretch(1, 1)
        shell.addWidget(naming_panel)

        output_panel, output = _panel("对齐导出位置")
        self._mode_group = QButtonGroup(self)
        self._source_radio = QRadioButton("保存在字幕视频所在目录")
        self._custom_radio = QRadioButton("保存在指定目录")
        self._mode_group.addButton(self._source_radio)
        self._mode_group.addButton(self._custom_radio)
        if dir_mode == ALIGN_OUTPUT_DIR_CUSTOM:
            self._custom_radio.setChecked(True)
        else:
            self._source_radio.setChecked(True)

        self._dir_edit = QLineEdit(self)
        self._dir_edit.setReadOnly(True)
        self._dir_edit.setPlaceholderText("点击选择保存文件夹")
        self._dir_edit.setText(custom_dir)
        self._dir_button = QPushButton("选择文件夹")

        self._source_radio.toggled.connect(lambda _checked: self._sync_dir_enabled())
        self._custom_radio.toggled.connect(lambda _checked: self._sync_dir_enabled())
        self._dir_button.clicked.connect(self._choose_output_dir)
        # 只读的输入框点一下也等于点「选择文件夹」，跟搬过来之前一样。
        self._dir_edit.mousePressEvent = (
            lambda event: self._choose_output_dir() if self._custom_radio.isChecked() else None
        )
        self._sync_dir_enabled()

        dir_row = QHBoxLayout()
        dir_row.setContentsMargins(0, 0, 0, 0)
        dir_row.setSpacing(8)
        dir_row.addWidget(self._dir_edit, 1)
        dir_row.addWidget(self._dir_button)
        output.addWidget(self._source_radio, 1, 1)
        output.addWidget(self._custom_radio, 2, 1)
        output.addWidget(QLabel("指定目录"), 3, 0)
        output.addLayout(dir_row, 3, 1)
        output.addWidget(_hint("导出时会以这里作为另存为窗口的默认目录。"), 4, 1)
        output.setColumnStretch(1, 1)
        shell.addWidget(output_panel)

    # ── 内部 ────────────────────────────────────────────────────

    def _sync_dir_enabled(self) -> None:
        enabled = self._custom_radio.isChecked()
        self._dir_edit.setEnabled(enabled)
        self._dir_button.setEnabled(enabled)

    def _choose_output_dir(self) -> None:
        init_dir = self._dir_edit.text().strip()
        if not init_dir:
            # 没填过就从当前字幕视频所在目录起步 —— 以前这是对话框穿两层去
            # 摸 align_video_zone.path，现在向页面要。
            current = self._page.current_video_path()
            if current is not None:
                init_dir = str(current.parent)
        if not init_dir:
            init_dir = str(Path.home())
        path = QFileDialog.getExistingDirectory(self, "选择对齐导出保存目录", init_dir)
        if path:
            self._dir_edit.setText(path)
            self._custom_radio.setChecked(True)
            self._sync_dir_enabled()

    # ── 对话框调的两条 ──────────────────────────────────────────

    def apply(self) -> None:
        """校验并写回对齐页；不合法抛 ``ProcessingError``。"""
        video_template = self._page.validate_name_template(
            self._video_template_edit.text().strip() or DEFAULT_ALIGNED_VIDEO_NAME_TEMPLATE,
            "对齐后视频",
            allowed_fields={"video_name"},
            extensions=(".mp4", ".mkv"),
        )
        audio_template = self._page.validate_name_template(
            self._audio_template_edit.text().strip() or DEFAULT_ALIGNED_AUDIO_NAME_TEMPLATE,
            "对齐后音频",
            allowed_fields={"audio_name", "video_name"},
            extensions=(".wav",),
        )
        mode = ALIGN_OUTPUT_DIR_CUSTOM if self._custom_radio.isChecked() else ALIGN_OUTPUT_DIR_SOURCE_VIDEO

        # 先把两项都校验完再写，免得模板合法、目录不合法时留下写了一半的状态。
        self._page.set_output_dir_settings(mode, self._dir_edit.text().strip())
        self._page.set_name_templates(video_template, audio_template)
