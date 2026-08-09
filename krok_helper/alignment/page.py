"""波形对齐页（工作流第 2 步「音视频处理 → 波形对齐」）的全部界面与流程。

**这是物理拆分，还不是完全的解耦**：这些方法整体从 ``KrokHelperQtApp`` 搬到
这里，仍然以 mixin 的形式混回同一个对象，``self`` 语义和搬之前一模一样，因此
行为零变化、也不需要改任何调用点。收益是对齐页从此是一份能单独读、单独改、
单独 review 的文件，而不是散在八千行里的 78 个方法。

真正的解耦还差一步：把 mixin 变成独立的页面对象。挡在中间的是下面这份
「宿主接口」—— 对齐页目前直接读写宿主的这些成员：

* ``settings`` / ``save_app_settings`` —— 配置读写
* ``_track_background_task`` —— 后台任务登记（关窗时统一收尾）
* ``_resolve_ffmpeg_dir`` —— ffmpeg 位置
* ``active_module`` / ``_focused_widget_is_text_input`` —— 快捷键是否该响应
* ``_set_panel_enabled`` —— 忙碌时整块禁用
* ``_notify_handoff`` —— 转交产物的右下角提示
* ``set_on_vocal_path`` / ``subtitle_render_page`` —— 把产物交给第 5/6 步
* ``_open_settings_window`` —— 打开全局设置的对齐分页
* ``preview_timer`` / ``_offset_finalize_timer`` —— 两个只服务本页的定时器
  （改对象时应当跟着搬过来）

这份清单由 ``tests/test_alignment_page_boundary.py`` 钉住：多引用一个宿主成员
测试就会失败，边界不会在不知不觉中重新糊上。
"""

from __future__ import annotations

import math
import subprocess
import time
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QTimer, Qt, pyqtSlot as Slot
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QButtonGroup,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CheckBox as QCheckBox,
    FluentIcon as FIF,
    PlainTextEdit as QPlainTextEdit,
    PrimaryPushButton,
    ProgressBar as QProgressBar,
    PushButton as QPushButton,
    RadioButton as QRadioButton,
    Slider as QSlider,
    StrongBodyLabel,
    ToolButton,
)

from krok_helper.qfluent_compat import show_fluent_error, show_fluent_info
from krok_helper.alignment import export_naming
from krok_helper.alignment.drop_card import AlignmentDropCard
from krok_helper.alignment.handoff_dialog import AlignmentHandoffDialog
from krok_helper.alignment.waveform_view import WaveformView
from krok_helper.audio_alignment import (
    DEFAULT_ALIGNED_AUDIO_NAME_TEMPLATE,
    DEFAULT_ALIGNED_VIDEO_NAME_TEMPLATE,
    ENCODE_MODE_HARDWARE,
    ENCODE_MODE_SOFTWARE,
    LEAD_FILL_BLACK,
    LEAD_FILL_FREEZE,
    LEAD_FILL_WHITE,
    AlignmentPreviewProcess,
    AutoAlignResult,
    WaveformData,
    build_alignment_preview_command,
    estimate_waveform_alignment,
    export_aligned_audio,
    export_aligned_video,
    extract_waveform,
    format_offset,
)
from krok_helper.background import BackgroundTask
from krok_helper.config import APP_TITLE
from krok_helper.errors import ProcessingError
from krok_helper.ffmpeg import _build_subprocess_kwargs, find_tool, terminate_process
from krok_helper.media_formats import (
    ALIGN_AUDIO_EXTENSIONS,
    VIDEO_EXTENSIONS,
    format_media_duration,
)
from krok_helper.settings import (
    ALIGN_OUTPUT_DIR_CUSTOM,
    ALIGN_OUTPUT_DIR_SOURCE_VIDEO,
    ALIGN_TARGET_AUDIO,
    ALIGN_TARGET_VIDEO,
    save_app_settings,
)
from krok_helper.ui_kit import CardWidget, build_app_ui_font
from krok_helper.windows import open_in_explorer
from krok_helper.workflow import WORKFLOW_WAVEFORM_ALIGN

__all__ = ["AlignmentPageMixin"]


class AlignmentPageMixin:
    """波形对齐页。混入 ``KrokHelperQtApp``，不单独实例化。"""

    _ZOOM_SLIDER_MIN = 1
    _ZOOM_SLIDER_MAX = 800
    _ZOOM_MIN_PPS = 0.5
    _ZOOM_MAX_PPS = 1200.0

    def _slider_to_pps(self, slider_val: int) -> float:
        """Map slider [1, 800] to pixels_per_second [0.5, 1200] on a log scale.
        Each equal slider step produces an equal ratio change, so zoom feels
        natural across the whole range."""
        t = (slider_val - self._ZOOM_SLIDER_MIN) / (self._ZOOM_SLIDER_MAX - self._ZOOM_SLIDER_MIN)
        return self._ZOOM_MIN_PPS * ((self._ZOOM_MAX_PPS / self._ZOOM_MIN_PPS) ** t)

    def _pps_to_slider(self, pps: float) -> int:
        pps = max(self._ZOOM_MIN_PPS, min(self._ZOOM_MAX_PPS, pps))
        if pps <= self._ZOOM_MIN_PPS:
            return self._ZOOM_SLIDER_MIN
        t = math.log(pps / self._ZOOM_MIN_PPS) / math.log(self._ZOOM_MAX_PPS / self._ZOOM_MIN_PPS)
        return self._ZOOM_SLIDER_MIN + int(round(t * (self._ZOOM_SLIDER_MAX - self._ZOOM_SLIDER_MIN)))

    def _update_head_mode_buttons(self, selected_key: str | None) -> None:
        self._head_mode_current = selected_key  # 主题切换时由 _on_theme_changed 用到
        from krok_helper.theme_workbench import palette as _wb_pal
        p = _wb_pal()
        # 选中：品牌色实心；未选中：壳色 + 边框；禁用：壳色 + 灰文字
        # 浅深主题区别只在背景/边框/常规文字；强调红色一致。
        button_map = {
            "crop": getattr(self, "align_head_btn_crop", None),
            "black": getattr(self, "align_head_btn_black", None),
            "white": getattr(self, "align_head_btn_white", None),
            "freeze": getattr(self, "align_head_btn_freeze", None),
        }
        selected_style = (
            "QPushButton {"
            f" background: {p.accent_primary};"
            " color: white;"
            " border: none;"
            " border-radius: 6px;"
            " padding: 6px 12px;"
            "}"
        )
        unselected_style = (
            "QPushButton {"
            f" background: {p.input_bg};"
            f" color: {p.text_primary};"
            f" border: 1px solid {p.input_border};"
            " border-radius: 6px;"
            " padding: 6px 12px;"
            "}"
            "QPushButton:hover {"
            f" background: {'#3A2A2C' if p.is_dark else '#fff1f2'};"
            f" border: 1px solid {p.accent_primary};"
            f" color: {p.accent_primary};"
            "}"
        )
        disabled_style = (
            "QPushButton {"
            f" background: {p.input_bg};"
            f" color: {p.text_disabled};"
            f" border: 1px solid {p.input_border};"
            " border-radius: 6px;"
            " padding: 6px 12px;"
            "}"
        )
        for key, button in button_map.items():
            if button is None:
                continue
            if not button.isEnabled():
                button.setStyleSheet(disabled_style)
                button.setFont(build_app_ui_font(point_size=10.5, bold=False))
            else:
                is_selected = bool(selected_key and key == selected_key)
                button.setStyleSheet(selected_style if is_selected else unselected_style)
                button.setFont(build_app_ui_font(point_size=10.5, bold=is_selected))

    def _init_alignment_state(self) -> None:
        """波形对齐页的全部实例状态。

        单独一个方法，是为了让「外壳」和「对齐页」的边界看得见：这些属性
        将来要跟着页面一起搬进 ``krok_helper.alignment``，届时这里改成调
        页面对象的同名入口即可，``__init__`` 不必再跟着动。
        """
        #: 对齐预览的轮询定时器 —— 只服务本页，以前建在外壳 __init__ 里。
        self.preview_timer = QTimer(self)
        self.preview_timer.setInterval(300)
        self.preview_timer.timeout.connect(self._poll_alignment_preview)
        self.align_analysis_task: BackgroundTask | None = None
        self.align_auto_task: BackgroundTask | None = None
        self.align_export_task: BackgroundTask | None = None
        self.align_preview_process = None
        self.align_preview_started_at = 0.0
        self.align_preview_start_seconds = 0.0
        self._align_export_cancel_requested = False
        self._align_export_process: subprocess.Popen | None = None
        self._align_export_expected_outputs: list[Path] = []
        self._align_export_completed_outputs: list[Path] = []
        self._align_export_handoff_context: tuple[bool, Path, Path, str] | None = None
        self._alignment_handoff_dialog: AlignmentHandoffDialog | None = None
        self._alignment_handoff_payload: tuple[bool, Path, Path | None, Path | None] | None = None
        self.align_video_name_template_value = DEFAULT_ALIGNED_VIDEO_NAME_TEMPLATE
        self.align_audio_name_template_value = DEFAULT_ALIGNED_AUDIO_NAME_TEMPLATE
        self.align_output_dir_mode_value = ALIGN_OUTPUT_DIR_SOURCE_VIDEO
        self.align_output_custom_dir_text = ""
        self._align_lead_fill_selection = LEAD_FILL_BLACK
        self._align_encode_selection = (
            self.settings.align_encode_mode
            if self.settings.align_encode_mode in {ENCODE_MODE_SOFTWARE, ENCODE_MODE_HARDWARE}
            else ENCODE_MODE_SOFTWARE
        )
        #: 正在把设置灌进控件 —— 期间控件变化不该回写，否则会把偏好覆盖成中间态。
        #: 以前读的是外壳的 ``_loading_settings_into_ui``，那是整机加载的旗，
        #: 本页只关心自己恢复设置的那一小段。
        self._restoring_alignment_settings = False
        self.align_control_panel: QFrame | None = None
        self.align_open_output_button: QPushButton | None = None
        self.align_clear_button: QPushButton | None = None
        self.align_jump_to_end_button: QPushButton | None = None
        self.align_reset_view_button: QPushButton | None = None

    def _bind_alignment_shortcuts(self) -> None:
        """波形对齐页独有的快捷键。

        启用/禁用由 :meth:`_sync_workflow_shortcut_scope` 按当前模块统一切换 ——
        快捷键挂在主窗口上是全局的，不按模块关掉的话，在别的页面按空格会误触
        对齐页的播放。
        """
        self.shortcut_space = QShortcut(QKeySequence(Qt.Key.Key_Space), self)
        self.shortcut_space.activated.connect(self._handle_align_space_shortcut)
        self.shortcut_auto = QShortcut(QKeySequence("Ctrl+D"), self)
        self.shortcut_auto.activated.connect(self._handle_align_auto_shortcut)
        self.shortcut_drag_mode = QShortcut(QKeySequence("Alt+V"), self)
        self.shortcut_drag_mode.activated.connect(self._handle_align_drag_mode_shortcut)

    def _handle_align_space_shortcut(self) -> None:
        if self.active_module != WORKFLOW_WAVEFORM_ALIGN or self._focused_widget_is_text_input():
            return
        if self.align_preview_process is not None and self.align_preview_process.is_running():
            self._stop_alignment_preview()
            return
        if self.waveform_view.video_waveform is not None and self.waveform_view.audio_waveform is not None:
            self._start_alignment_preview()
        else:
            self._start_alignment_analysis()

    def _handle_align_auto_shortcut(self) -> None:
        if self.active_module != WORKFLOW_WAVEFORM_ALIGN or self._focused_widget_is_text_input():
            return
        self._auto_align_waveforms()

    def _handle_align_drag_mode_shortcut(self) -> None:
        if self.active_module != WORKFLOW_WAVEFORM_ALIGN or self._focused_widget_is_text_input():
            return
        if self.align_drag_pan_radio.isChecked():
            self.align_drag_offset_radio.setChecked(True)
        else:
            self.align_drag_pan_radio.setChecked(True)
        if hasattr(self, "align_drag_mode_button"):
            is_pan = self.align_drag_pan_radio.isChecked()
            self.align_drag_mode_button.setToolTip(
                "当前：平移视图，Alt+V 切换为移动偏移" if is_pan else "当前：移动偏移，Alt+V 切换为平移视图"
            )

    def _should_route_alignment_wheel(self, watched, event) -> bool:
        if not hasattr(self, "waveform_view"):
            return False
        waveform_view = self.waveform_view
        if not waveform_view.isVisible() or not waveform_view.isEnabled():
            return False
        if waveform_view.video_waveform is None or waveform_view.audio_waveform is None:
            return False
        watched_widgets = (
            waveform_view,
            getattr(self, "align_waveform_stage", None),
            getattr(self, "align_scroll_area", None),
            getattr(self, "align_scroll_viewport", None),
        )
        if not any(watched is widget for widget in watched_widgets if widget is not None):
            return False
        if hasattr(event, "globalPosition"):
            global_pos = event.globalPosition().toPoint()
        else:
            local_pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
            global_pos = watched.mapToGlobal(local_pos)
        return waveform_view.rect().contains(waveform_view.mapFromGlobal(global_pos))

    def _sync_alignment_zoom_slider(self) -> None:
        if hasattr(self, "align_zoom_slider"):
            self.align_zoom_slider.blockSignals(True)
            self.align_zoom_slider.setValue(self._pps_to_slider(self.waveform_view.pixels_per_second))
            self.align_zoom_slider.blockSignals(False)

    def _build_alignment_page(self) -> QWidget:
        from PyQt6.QtCore import QSize

        class AlignmentExportProxyButton(PrimaryPushButton):
            def __init__(self, owner: "KrokHelperQtApp") -> None:
                super().__init__(owner)
                self._owner = owner
                self.hide()

            def setEnabled(self, enabled: bool) -> None:
                super().setEnabled(enabled)
                self._owner._sync_alignment_export_buttons()

        scroll = QScrollArea()
        self.align_scroll_area = scroll
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.installEventFilter(self)

        page = QWidget()
        shell = QVBoxLayout(page)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(14)

        self.waveform_view = WaveformView()
        self.waveform_view.playheadChanged.connect(self._handle_playhead_changed)
        self.waveform_view.offsetChanged.connect(self._handle_waveform_offset_changed)
        self.waveform_view.trimChanged.connect(self._refresh_align_trim_status)
        self._last_fill_mode = None
        self._pending_offset_finalized_seconds = 0.0
        self._offset_finalize_timer = QTimer(self)
        self._offset_finalize_timer.setSingleShot(True)
        self._offset_finalize_timer.setInterval(50)
        self._offset_finalize_timer.timeout.connect(
            lambda: self._on_offset_finalized(self._pending_offset_finalized_seconds)
        )
        self.waveform_view.offsetChanged.connect(
            lambda seconds: (
                setattr(self, "_pending_offset_finalized_seconds", float(seconds)),
                self._offset_finalize_timer.start(),
            )
        )
        self._align_volume_refresh_timer = QTimer(self)
        self._align_volume_refresh_timer.setSingleShot(True)
        self._align_volume_refresh_timer.setInterval(120)
        self._align_volume_refresh_timer.timeout.connect(self._apply_alignment_preview_volume)
        self.waveform_view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.waveform_view.setMinimumHeight(300)
        self.wave_view = self.waveform_view

        self.align_export_button = AlignmentExportProxyButton(self)
        self.align_export_button.clicked.connect(self._start_aligned_export)
        self.align_export_button.setEnabled(False)

        self._align_nudge_step = 0.01

        self.align_video_zone = AlignmentDropCard(
            media_label="字幕视频",
            title="选择字幕视频",
            hint="支持 mkv / mp4 / mov / avi",
            extensions=VIDEO_EXTENSIONS,
            icon=FIF.VIDEO,
            theme="red",
        )
        self.align_video_zone.browseRequested.connect(self._choose_align_video)
        self.align_video_zone.pathChanged.connect(self.set_align_video_path)
        self.align_video_zone.durationTextChanged.connect(self._on_align_duration_text_changed)
        self.align_video_info_label = self.align_video_zone.detail_label
        self.align_video_zone.title_label.setText("字幕视频")
        self.align_video_zone.hint_label.setText("支持 mkv / mp4 / mov / avi")

        self.align_audio_zone = AlignmentDropCard(
            media_label="原唱音源",
            title="选择原唱音频",
            hint="支持 flac / wav / mp3 / m4a / aac / ape / alac / mkv / mp4",
            extensions=ALIGN_AUDIO_EXTENSIONS,
            icon=FIF.MUSIC,
            theme="blue",
        )
        self.align_audio_zone.browseRequested.connect(self._choose_align_audio)
        self.align_audio_zone.pathChanged.connect(self.set_align_audio_path)
        self.align_audio_zone.durationTextChanged.connect(self._on_align_duration_text_changed)
        self.align_audio_info_label = self.align_audio_zone.detail_label
        self.align_audio_zone.title_label.setText("原唱音频")
        self.align_audio_zone.hint_label.setText("支持 flac / wav / mp3 / m4a / aac / ape / alac / mkv / mp4")

        def clear_align_video_only() -> None:
            self.align_video_zone.clear_path()
            self._invalidate_alignment_waveforms()

        def clear_align_audio_only() -> None:
            self.align_audio_zone.clear_path()
            self._invalidate_alignment_waveforms()

        self.align_video_zone.removeRequested.connect(clear_align_video_only)
        self.align_audio_zone.removeRequested.connect(clear_align_audio_only)

        clear_button = QPushButton("清空")
        clear_button.clicked.connect(self._clear_alignment_inputs)
        clear_button.setMinimumHeight(36)
        clear_button.setMinimumWidth(84)
        clear_button_policy = clear_button.sizePolicy()
        clear_button_policy.setRetainSizeWhenHidden(True)
        clear_button.setSizePolicy(clear_button_policy)

        self.align_stop_export_button = QPushButton("停止导出")
        self.align_stop_export_button.setIcon(FIF.CLOSE.icon())
        self.align_stop_export_button.clicked.connect(self._stop_alignment_export)
        self.align_stop_export_button.setEnabled(False)
        self.align_stop_export_button.setMinimumHeight(36)

        open_output_button = QPushButton("打开输出目录")
        open_output_button.clicked.connect(self._open_align_output_dir)
        open_output_button.setMinimumHeight(36)

        self.align_open_output_button = open_output_button
        self.align_clear_button = clear_button

        self.align_material_card = CardWidget(radius=10, padding=(16, 14, 16, 14), spacing=12)
        material_layout = self.align_material_card.createVBoxLayout()
        material_header = QHBoxLayout()
        material_header.setContentsMargins(0, 0, 0, 0)
        material_header.setSpacing(10)
        material_title = QLabel("素材输入")
        material_title.setObjectName("PanelTitle")
        self.align_material_status_label = QLabel("① 先导入素材")
        self.align_material_status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # 初始 idle 样式由 ``_refresh_alignment_material_inputs`` 在构造完成后
        # 第一次调用时自动应用；主题切换也走同一函数（见 __init__ 末尾的
        # ``theme.changed.connect``）。这里只设字体。
        self.align_material_status_label.setFont(build_app_ui_font(point_size=10.5, bold=True))
        self.align_material_settings_button = ToolButton(FIF.SETTING)
        self.align_material_settings_button.setObjectName("AlignMaterialSettingsButton")
        self.align_material_settings_button.setToolTip("波形对齐设置")
        self.align_material_settings_button.setFixedSize(30, 30)
        self.align_material_settings_button.setIconSize(QSize(16, 16))
        self.align_material_settings_button.clicked.connect(lambda: self._open_settings_window("align"))

        material_header.addWidget(material_title)
        material_header.addWidget(self.align_material_status_label)
        material_header.addStretch(1)
        material_header.addWidget(self.align_clear_button)
        material_header.addSpacing(2)
        material_header.addWidget(self.align_material_settings_button, 0, Qt.AlignmentFlag.AlignVCenter)
        material_layout.addLayout(material_header)

        self.align_material_body = QWidget()
        self.align_material_body.setStyleSheet("background: transparent; border: 0;")
        material_body_layout = QHBoxLayout(self.align_material_body)
        material_body_layout.setContentsMargins(0, 0, 0, 0)
        material_body_layout.setSpacing(14)
        material_body_layout.addWidget(self.align_video_zone, 1)
        material_body_layout.addWidget(self.align_audio_zone, 1)
        material_layout.addWidget(self.align_material_body)
        shell.addWidget(self.align_material_card)

        shell.addWidget(self._build_waveform_toolbar())

        main_row = QWidget()
        main_row.setMinimumWidth(0)
        main_row.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.align_control_panel = main_row
        main_layout = QHBoxLayout(main_row)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(12)

        self.align_waveform_card = CardWidget(radius=10, padding=(16, 16, 16, 16), spacing=8)
        self.align_waveform_card.setMinimumWidth(0)
        self.align_waveform_card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        waveform_grid = QGridLayout(self.align_waveform_card)
        waveform_grid.setContentsMargins(16, 16, 16, 16)
        waveform_grid.setVerticalSpacing(8)
        waveform_grid.setHorizontalSpacing(0)
        waveform_grid.setRowStretch(0, 0)
        waveform_grid.setRowStretch(1, 1)
        waveform_grid.setRowStretch(2, 0)
        waveform_header = QLabel("波形工作区")
        waveform_header.setObjectName("PanelTitle")
        waveform_grid.addWidget(waveform_header, 0, 0, alignment=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        waveform_stage = QFrame(self.align_waveform_card)
        waveform_stage.setObjectName("AlignWaveformStage")
        waveform_stage.setMinimumWidth(0)
        waveform_stage.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        waveform_stage.setStyleSheet("QFrame#AlignWaveformStage { background: transparent; border: 0; }")
        self.align_waveform_stage = waveform_stage
        waveform_stage.installEventFilter(self)
        self.waveform_view.installEventFilter(self)
        self.align_scroll_viewport = scroll.viewport()
        self.align_scroll_viewport.installEventFilter(self)
        stage_grid = QGridLayout(waveform_stage)
        stage_grid.setContentsMargins(0, 0, 0, 0)
        stage_grid.setSpacing(0)
        stage_grid.addWidget(self.waveform_view, 0, 0)
        self.align_waveform_placeholder = QLabel(
            "导入字幕视频与原唱音源后，点击「生成波形」即可在此查看对齐视图"
        )
        self.align_waveform_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.align_waveform_placeholder.setWordWrap(True)
        self.align_waveform_placeholder.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        from krok_helper.theme_workbench import palette as _wb_pal, themed as _wb_th
        _wb_th(self.align_waveform_placeholder, lambda: f"color: {_wb_pal().text_secondary}; font-size: 12pt;")
        stage_grid.addWidget(self.align_waveform_placeholder, 0, 0, alignment=Qt.AlignmentFlag.AlignCenter)
        self.align_drag_mode_button = ToolButton(waveform_stage)
        self.align_drag_mode_button.setIcon(FIF.MOVE.icon())
        self.align_drag_mode_button.setToolTip("切换拖动模式 (Alt+V)")
        self.align_drag_mode_button.clicked.connect(self._handle_align_drag_mode_shortcut)
        self.align_drag_mode_button.setFixedSize(34, 34)
        stage_grid.addWidget(
            self.align_drag_mode_button,
            0,
            0,
            alignment=Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight,
        )
        self.align_nudge_panel = QFrame(self.align_waveform_card)
        self.align_nudge_panel.setObjectName("AlignNudgePanel")
        nudge_layout = QHBoxLayout(self.align_nudge_panel)
        nudge_layout.setContentsMargins(8, 8, 8, 8)
        nudge_layout.setSpacing(8)
        for text, delta in (("-0.1", -0.1), ("-0.01", -0.01), ("归零", None), ("+0.01", 0.01), ("+0.1", 0.1)):
            button = QPushButton(text)
            button.setMinimumHeight(30)
            if delta is None:
                button.clicked.connect(lambda _checked=False: self.waveform_view.set_offset(0.0))
            else:
                button.clicked.connect(lambda _checked=False, value=delta: self.waveform_view.nudge_offset(value))
            nudge_layout.addWidget(button)
        waveform_grid.addWidget(waveform_stage, 1, 0)
        waveform_grid.addWidget(self.align_nudge_panel, 2, 0, alignment=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom)
        main_layout.addWidget(self.align_waveform_card, 1)

        right_sidebar = self._build_adjustment_panels()
        right_sidebar.setFixedWidth(380)
        main_layout.addWidget(right_sidebar, 0)
        main_layout.setStretch(0, 1)
        main_layout.setStretch(1, 0)
        shell.addWidget(main_row)

        self.align_log_panel = CardWidget(radius=10, padding=(14, 12, 14, 12), spacing=8)
        log_layout = self.align_log_panel.createVBoxLayout()
        log_header = QHBoxLayout()
        log_header.setContentsMargins(0, 0, 0, 0)
        log_header.setSpacing(10)
        self.align_log_toggle_button = QPushButton("▸  对齐日志")
        self.align_log_toggle_button.setFlat(True)
        from krok_helper.theme_workbench import palette as _wb_pal2, themed as _wb_th2
        _wb_th2(self.align_log_toggle_button, lambda: (
            f"text-align: left; font-weight: 700; color: {_wb_pal2().panel_title};"
            " border: 0; background: transparent;"
        ))
        clear_log_button = QPushButton("清空日志")
        clear_log_button.setIcon(FIF.DELETE.icon())
        clear_log_button.hide()
        self.align_clear_log_button = clear_log_button
        log_header.addWidget(self.align_log_toggle_button, 1)
        log_header.addWidget(clear_log_button)
        log_layout.addLayout(log_header)
        self.align_log_container = QWidget()
        _wb_th2(self.align_log_container, lambda: f"background: {_wb_pal2().card_bg}; border: 0;")
        log_body_layout = QVBoxLayout(self.align_log_container)
        log_body_layout.setContentsMargins(0, 0, 0, 0)
        log_body_layout.setSpacing(0)
        self.align_log = QPlainTextEdit()
        self.align_log.setObjectName("LogText")
        self.align_log.setReadOnly(True)
        self.align_log.setMinimumHeight(120)
        self.log_text = self.align_log
        clear_log_button.clicked.connect(self.align_log.clear)
        log_body_layout.addWidget(self.align_log)
        log_layout.addWidget(self.align_log_container)
        self.align_log_container.hide()

        def toggle_log() -> None:
            expanded = not self.align_log_container.isVisible()
            self.align_log_container.setVisible(expanded)
            self.align_clear_log_button.setVisible(expanded)
            self.align_log_toggle_button.setText(("▾" if expanded else "▸") + "  对齐日志")

        self.align_log_toggle_button.clicked.connect(toggle_log)
        shell.addWidget(self.align_log_panel)
        shell.addStretch(1)

        self._refresh_alignment_material_inputs()
        self._refresh_align_target_ui()
        self._on_alignment_target_changed()
        self._refresh_alignment_preview_controls()
        scroll.setWidget(page)
        return scroll

    def _build_waveform_toolbar(self) -> QWidget:
        from PyQt6.QtCore import QSize

        toolbar_card = CardWidget(radius=10, padding=(14, 12, 14, 12), spacing=10)
        toolbar_card.setMinimumWidth(0)
        toolbar_card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = toolbar_card.createHBoxLayout()
        layout.setSpacing(6)

        self.align_analyze_button = QPushButton("生成波形")
        self.align_analyze_button.setIcon(FIF.MUSIC.icon())
        self.align_analyze_button.clicked.connect(self._start_alignment_analysis)
        self.align_analyze_button.setToolTip("生成波形 (空格)")
        self.align_auto_button = PrimaryPushButton("自动对齐")
        self.align_auto_button.setIcon(FIF.SYNC.icon())
        self.align_auto_button.clicked.connect(self._auto_align_waveforms)
        self.align_auto_button.setToolTip("自动对齐 (Ctrl+D)")
        self.btn_auto_align = self.align_auto_button
        self.align_preview_button = QPushButton("播放")
        self.align_preview_button.setIcon(FIF.PLAY.icon())
        self.align_preview_button.clicked.connect(self._toggle_alignment_preview)
        self.align_preview_button.setToolTip("播放 (空格)")

        toolbar_button_specs = (
            (self.align_analyze_button, 108),
            (self.align_auto_button, 118),
            (self.align_preview_button, 86),
        )
        for button, minimum_width in toolbar_button_specs:
            button.setMinimumHeight(36)
            button.setMaximumHeight(36)
            button.setMinimumWidth(minimum_width)
            button.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
            button.setIconSize(QSize(16, 16))
            layout.addWidget(button)

        self.align_drag_offset_radio = QRadioButton("移动字幕视频")
        self.align_drag_pan_radio = QRadioButton("平移视图")
        self.align_drag_offset_radio.setChecked(True)
        self.align_drag_group = QButtonGroup(self)
        self.align_drag_group.setExclusive(True)
        self.align_drag_group.addButton(self.align_drag_offset_radio)
        self.align_drag_group.addButton(self.align_drag_pan_radio)
        self.align_drag_offset_radio.toggled.connect(
            lambda checked: self.waveform_view.set_drag_mode("offset" if checked else "pan")
        )
        self.rb_drag_move = self.align_drag_offset_radio
        self.rb_drag_pan = self.align_drag_pan_radio
        self.align_drag_offset_radio.hide()
        self.align_drag_pan_radio.hide()

        volume_button = ToolButton(toolbar_card)
        volume_button.setIcon(FIF.VOLUME.icon())
        volume_button.setIconSize(QSize(18, 18))
        volume_button.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        volume_button.setStyleSheet("ToolButton { background: transparent; border: 0; }")
        layout.addWidget(volume_button)

        self.align_volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.align_volume_slider.setRange(0, 100)
        self.align_volume_slider.setValue(50)
        self.align_volume_slider.setMinimumWidth(36)
        self.align_volume_slider.setMaximumWidth(100)
        self.align_volume_slider.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.align_volume_slider.valueChanged.connect(self._queue_alignment_preview_volume_refresh)
        layout.addWidget(self.align_volume_slider)

        self.align_reset_view_button = QPushButton("回到开头")
        self.align_reset_view_button.setIcon(FIF.SKIP_BACK.icon())
        self.align_reset_view_button.setMinimumHeight(36)
        self.align_reset_view_button.setMaximumHeight(36)
        self.align_reset_view_button.setMinimumWidth(104)
        self.align_reset_view_button.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        self.align_reset_view_button.setIconSize(QSize(16, 16))
        self.align_reset_view_button.clicked.connect(self._reset_alignment_waveform_view)
        layout.addWidget(self.align_reset_view_button)

        self.align_jump_to_end_button = QPushButton("跳到末尾")
        self.align_jump_to_end_button.setIcon(FIF.SKIP_FORWARD.icon())
        self.align_jump_to_end_button.setMinimumHeight(36)
        self.align_jump_to_end_button.setMaximumHeight(36)
        self.align_jump_to_end_button.setMinimumWidth(104)
        self.align_jump_to_end_button.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        self.align_jump_to_end_button.setIconSize(QSize(16, 16))
        self.align_jump_to_end_button.clicked.connect(self.waveform_view.jump_to_end)
        layout.addWidget(self.align_jump_to_end_button)

        zoom_out = ToolButton(toolbar_card)
        zoom_out.setIcon(FIF.ZOOM_OUT.icon())
        zoom_out.setIconSize(QSize(18, 18))
        zoom_out.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        zoom_out.setStyleSheet("ToolButton { background: transparent; border: 0; }")
        layout.addWidget(zoom_out)

        self.align_zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self.align_zoom_slider.setRange(self._ZOOM_SLIDER_MIN, self._ZOOM_SLIDER_MAX)
        self.align_zoom_slider.setValue(self._pps_to_slider(120.0))
        self.align_zoom_slider.valueChanged.connect(lambda value: self.waveform_view.set_zoom(self._slider_to_pps(value)))
        self.align_zoom_slider.setMinimumWidth(36)
        self.align_zoom_slider.setMaximumWidth(110)
        self.align_zoom_slider.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout.addWidget(self.align_zoom_slider)

        zoom_in = ToolButton(toolbar_card)
        zoom_in.setIcon(FIF.ZOOM_IN.icon())
        zoom_in.setIconSize(QSize(18, 18))
        zoom_in.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        zoom_in.setStyleSheet("ToolButton { background: transparent; border: 0; }")
        layout.addWidget(zoom_in)

        self.align_progress = QProgressBar()
        self.align_progress.setRange(0, 1)
        self.align_progress.setValue(0)
        self.align_progress.setMinimumWidth(40)
        self.align_progress.setMaximumWidth(120)
        self.align_progress.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.align_progress.setTextVisible(False)
        layout.addWidget(self.align_progress)

        self.align_status_label = BodyLabel("准备生成波形")
        self.align_status_label.setMinimumWidth(210)
        self.align_status_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        from krok_helper.theme_workbench import palette as _wb_pal, themed as _wb_th
        _wb_th(self.align_status_label, lambda: f"color: {_wb_pal().text_secondary}; font-weight: 400;")
        layout.addWidget(self.align_status_label, 1)
        return toolbar_card

    def _build_adjustment_panels(self) -> QWidget:
        wrapper = QWidget()
        wrapper.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        wrapper.setStyleSheet("background: transparent; border: 0;")
        layout = QVBoxLayout(wrapper)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        self.align_control_card = CardWidget(radius=10, padding=(16, 16, 16, 16), spacing=12)
        self.align_control_card.setObjectName("AlignControlCard")
        from krok_helper.theme_workbench import palette as _wb_pal3, themed as _wb_th3
        _wb_th3(self.align_control_card, lambda: (
            "QFrame#AlignControlCard {{"
            " background: {bg}; border: 1px solid {border}; border-radius: 10px;"
            "}}"
            "QFrame#AlignControlCard QLabel {{ background: transparent; border: 0; }}"
            "QFrame#AlignControlCard QCheckBox {{ background: transparent; }}"
        ).format(bg=_wb_pal3().card_bg, border=_wb_pal3().card_border))
        self.subtitle_adjust_card = self.align_control_card
        self.SubtitleAdjust = self.align_control_card
        self.original_adjust_card = self.align_control_card
        self.OriginalAdjust = self.align_control_card
        control_layout = self.align_control_card.createVBoxLayout()
        control_layout.setSpacing(12)
        control_layout.addWidget(StrongBodyLabel("对齐控制"))

        segment = QWidget()
        segment.setObjectName("AlignTargetSegment")
        segment.setMinimumHeight(36)
        _wb_th3(segment, lambda: (
            "QWidget#AlignTargetSegment {{"
            " background: transparent;"
            " border: 1px solid {border};"
            " border-radius: 8px;"
            "}}"
        ).format(border=_wb_pal3().card_border))
        segment_layout = QHBoxLayout(segment)
        segment_layout.setContentsMargins(0, 0, 0, 0)
        segment_layout.setSpacing(0)
        self.align_target_video_radio = QRadioButton("对齐字幕视频")
        self.align_target_audio_radio = QRadioButton("对齐原唱音频")
        self.align_target_video_radio.setChecked(True)
        self.align_target_group = QButtonGroup(self)
        self.align_target_group.setExclusive(True)
        self.align_target_group.addButton(self.align_target_video_radio)
        self.align_target_group.addButton(self.align_target_audio_radio)
        self.align_target_video_radio.toggled.connect(self._on_alignment_target_changed)
        self.align_target_audio_radio.toggled.connect(self._on_alignment_target_changed)
        self.rb_adjust_subtitle = self.align_target_video_radio
        self.rb_adjust_original = self.align_target_audio_radio
        self.align_target_video_radio.hide()
        self.align_target_audio_radio.hide()
        self.align_target_video_button = QPushButton("对齐字幕视频")
        self.align_target_audio_button = QPushButton("对齐原唱音频")
        self.align_target_video_button.setCheckable(True)
        self.align_target_audio_button.setCheckable(True)
        self.align_target_video_button.clicked.connect(lambda _checked=False: self.align_target_video_radio.setChecked(True))
        self.align_target_audio_button.clicked.connect(lambda _checked=False: self.align_target_audio_radio.setChecked(True))
        segment_layout.addWidget(self.align_target_video_button, 1)
        segment_layout.addWidget(self.align_target_audio_button, 1)
        control_layout.addWidget(segment)

        self.align_control_placeholder = QFrame()
        self.align_control_placeholder.setObjectName("AlignControlPlaceholder")
        self.align_control_placeholder.setStyleSheet(
            "QFrame#AlignControlPlaceholder { background: transparent; border: 0; }"
            "ToolButton { background: transparent; border: 0; }"
        )
        placeholder_layout = QHBoxLayout(self.align_control_placeholder)
        placeholder_layout.setContentsMargins(18, 20, 18, 20)
        placeholder_layout.setSpacing(12)
        placeholder_icon = ToolButton(self.align_control_placeholder)
        placeholder_icon.setIcon(FIF.SETTING.icon())
        placeholder_icon.setEnabled(False)
        placeholder_layout.addWidget(placeholder_icon, 0, Qt.AlignmentFlag.AlignVCenter)
        placeholder_label = BodyLabel("请先导入素材并生成波形")
        from krok_helper.theme_workbench import palette as _wb_pal, themed as _wb_th
        _wb_th(placeholder_label, lambda: f"color: {_wb_pal().text_disabled};")
        placeholder_layout.addWidget(placeholder_label, 1, Qt.AlignmentFlag.AlignVCenter)
        control_layout.addWidget(self.align_control_placeholder)

        self.align_video_options_widget = QWidget()
        self.align_video_options_widget.setStyleSheet("background: transparent; border: 0;")
        video_layout = QVBoxLayout(self.align_video_options_widget)
        video_layout.setContentsMargins(0, 0, 0, 0)
        video_layout.setSpacing(12)

        self.align_lead_trim_radio = QRadioButton("裁剪")
        self.align_lead_fill_black_radio = QRadioButton("补黑")
        self.align_lead_fill_white_radio = QRadioButton("补白")
        self.align_lead_fill_freeze_radio = QRadioButton("首帧定格")
        self.align_lead_fill_black_radio.setChecked(True)
        self.align_lead_fill_group = QButtonGroup(self)
        self.align_lead_fill_group.setExclusive(True)
        for radio in (
            self.align_lead_trim_radio,
            self.align_lead_fill_black_radio,
            self.align_lead_fill_white_radio,
            self.align_lead_fill_freeze_radio,
        ):
            self.align_lead_fill_group.addButton(radio)

        self.align_lead_row_widget = QWidget()
        self.align_lead_row_widget.setStyleSheet("background: transparent; border: 0;")
        lead_row = QHBoxLayout(self.align_lead_row_widget)
        lead_row.setContentsMargins(0, 0, 0, 0)
        lead_row.setSpacing(10)
        lead_row.addWidget(BodyLabel("片头："))
        self.align_head_btn_crop = QPushButton("裁剪")
        self.align_head_btn_black = QPushButton("补黑")
        self.align_head_btn_white = QPushButton("补白")
        self.align_head_btn_freeze = QPushButton("首帧定格")

        def select_head_mode(key: str, radio: QRadioButton) -> None:
            if key == "black":
                self._last_fill_mode = LEAD_FILL_BLACK
            elif key == "white":
                self._last_fill_mode = LEAD_FILL_WHITE
            elif key == "freeze":
                self._last_fill_mode = LEAD_FILL_FREEZE
            radio.setChecked(True)
            self._update_head_mode_buttons(key)
            self._refresh_alignment_export_panels()

        for button, key, radio in (
            (self.align_head_btn_crop, "crop", self.align_lead_trim_radio),
            (self.align_head_btn_black, "black", self.align_lead_fill_black_radio),
            (self.align_head_btn_white, "white", self.align_lead_fill_white_radio),
            (self.align_head_btn_freeze, "freeze", self.align_lead_fill_freeze_radio),
        ):
            button.clicked.connect(lambda _checked=False, k=key, r=radio: select_head_mode(k, r))
            lead_row.addWidget(button, 1)
        video_layout.addWidget(self.align_lead_row_widget)

        self.align_head_trim_row_widget = QWidget()
        self.align_head_trim_row_widget.setStyleSheet("background: transparent; border: 0;")
        self.align_lead_trim_seconds_spin = QDoubleSpinBox()
        self.align_lead_trim_seconds_spin.setDecimals(3)
        self.align_lead_trim_seconds_spin.setRange(0.0, 99999.0)
        self.align_lead_trim_seconds_spin.setEnabled(False)
        self.spin_head_trim = self.align_lead_trim_seconds_spin
        self.align_head_trim_row_widget.hide()

        self.align_trim_none_radio = QRadioButton("不处理")
        self.align_trim_to_audio_radio = QRadioButton("裁到音频末尾")
        self.align_trim_none_radio.setChecked(True)
        self.rb_tail_none = self.align_trim_none_radio
        self.rb_tail_trim = self.align_trim_to_audio_radio
        self.align_trim_mode_group = QButtonGroup(self)
        self.align_trim_mode_group.setExclusive(True)
        self.align_trim_mode_group.addButton(self.align_trim_none_radio)
        self.align_trim_mode_group.addButton(self.align_trim_to_audio_radio)
        tail_row = QHBoxLayout()
        tail_row.setContentsMargins(0, 0, 0, 0)
        tail_row.setSpacing(14)
        tail_row.addWidget(BodyLabel("片尾："))
        tail_row.addWidget(self.align_trim_none_radio)
        tail_row.addWidget(self.align_trim_to_audio_radio)
        tail_row.addStretch(1)
        video_layout.addLayout(tail_row)
        self.align_trim_label = BodyLabel("未设置")
        from krok_helper.theme_workbench import palette as _wb_pal, themed as _wb_th
        _wb_th(self.align_trim_label, lambda: f"color: {_wb_pal().text_secondary};")
        self.align_trim_mark_button = QPushButton("设置尾裁点")
        self.align_trim_clear_button = QPushButton("清除尾裁点")
        self.align_trim_mark_button.setMinimumHeight(32)
        self.align_trim_clear_button.setMinimumHeight(32)
        self.align_trim_mark_button.clicked.connect(
            lambda: self.waveform_view.set_trim_end(self.waveform_view.playhead_seconds)
        )
        self.align_trim_clear_button.clicked.connect(self.waveform_view.clear_trim_end)
        tail_action_row = QHBoxLayout()
        tail_action_row.setContentsMargins(0, 0, 0, 0)
        tail_action_row.setSpacing(8)
        tail_action_row.addWidget(self.align_trim_mark_button)
        tail_action_row.addWidget(self.align_trim_clear_button)
        tail_action_row.addWidget(self.align_trim_label, 1)
        video_layout.addLayout(tail_action_row)
        self.chk_auto_trim = self.align_trim_to_audio_radio
        self.align_trim_none_radio.toggled.connect(
            lambda _checked: self._refresh_align_trim_status(self.waveform_view.trim_end_seconds)
        )
        self.align_trim_none_radio.toggled.connect(lambda _checked: self._refresh_alignment_export_panels())
        self.align_trim_to_audio_radio.toggled.connect(
            lambda _checked: self._refresh_align_trim_status(self.waveform_view.trim_end_seconds)
        )
        self.align_trim_to_audio_radio.toggled.connect(lambda _checked: self._refresh_alignment_export_panels())

        encode_row = QHBoxLayout()
        encode_row.setContentsMargins(0, 0, 0, 0)
        encode_row.setSpacing(14)
        encode_row.addWidget(BodyLabel("编码："))
        self.align_encode_software_radio = QRadioButton("软编(CPU)")
        self.align_encode_hardware_radio = QRadioButton("硬编(GPU)")
        self.align_encode_software_radio.setChecked(True)
        self.rb_codec_cpu = self.align_encode_software_radio
        self.rb_codec_gpu = self.align_encode_hardware_radio
        self.align_encode_group = QButtonGroup(self)
        self.align_encode_group.setExclusive(True)
        self.align_encode_group.addButton(self.align_encode_software_radio)
        self.align_encode_group.addButton(self.align_encode_hardware_radio)
        self.align_encode_software_radio.toggled.connect(
            lambda checked: self._handle_alignment_encode_mode_toggled(ENCODE_MODE_SOFTWARE, checked)
        )
        self.align_encode_hardware_radio.toggled.connect(
            lambda checked: self._handle_alignment_encode_mode_toggled(ENCODE_MODE_HARDWARE, checked)
        )
        self.align_encode_row_widget = QWidget()
        self.align_encode_row_widget.setStyleSheet("background: transparent; border: 0;")
        encode_inner = QHBoxLayout(self.align_encode_row_widget)
        encode_inner.setContentsMargins(0, 0, 0, 0)
        encode_inner.setSpacing(14)
        encode_inner.addWidget(self.align_encode_software_radio)
        encode_inner.addWidget(self.align_encode_hardware_radio)
        encode_inner.addStretch(1)
        encode_row.addWidget(self.align_encode_row_widget, 1)
        video_layout.addLayout(encode_row)

        option_row = QHBoxLayout()
        option_row.setContentsMargins(0, 0, 0, 0)
        option_row.setSpacing(14)
        self.align_force_1080p60_check = QCheckBox("重编码1080p60")
        self.align_use_video_audio_check = QCheckBox("保留源音轨")
        self.chk_reencode = self.align_force_1080p60_check
        self.chk_keep_audio = self.align_use_video_audio_check
        self.align_force_1080p60_card = self.align_force_1080p60_check
        self.align_use_video_audio_card = self.align_use_video_audio_check
        self.align_encode_software_card = self.align_encode_software_radio
        self.align_encode_hardware_card = self.align_encode_hardware_radio
        self.align_force_1080p60_check.toggled.connect(self._persist_alignment_preferences)
        self.align_use_video_audio_check.toggled.connect(self._persist_alignment_preferences)
        option_row.addWidget(self.align_force_1080p60_check)
        option_row.addWidget(self.align_use_video_audio_check)
        option_row.addStretch(1)
        video_layout.addLayout(option_row)
        control_layout.addWidget(self.align_video_options_widget)

        self.align_audio_offset_widget = QFrame()
        self.align_audio_offset_widget.setObjectName("AlignAudioOffsetPanel")
        _wb_th3(self.align_audio_offset_widget, lambda: (
            "QFrame#AlignAudioOffsetPanel {{"
            " background: transparent; border: 1px solid {border}; border-radius: 8px;"
            "}}"
        ).format(border=_wb_pal3().card_border))
        audio_offset_layout = QVBoxLayout(self.align_audio_offset_widget)
        audio_offset_layout.setContentsMargins(20, 26, 20, 26)
        audio_offset_layout.setSpacing(12)
        audio_offset_title = BodyLabel("原唱音源偏移")
        audio_offset_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        from krok_helper.theme_workbench import palette as _wb_pal, themed as _wb_th
        _wb_th(audio_offset_title, lambda: f"color: {_wb_pal().text_primary}; font-size: 13pt; background: transparent; border: 0;")
        self.align_offset_label = QLabel("+0.000s")
        self.label_offset = self.align_offset_label
        self.align_offset_label.setMinimumWidth(0)
        self.align_offset_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.align_offset_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # 数字偏移 —— 蓝色强调（深色下用稍亮变体）
        _wb_th3(self.align_offset_label, lambda: (
            'color: {color}; font-family: "Microsoft YaHei UI"; font-size: 24pt;'
            ' background: transparent; border: 0;'
        ).format(color="#6FA3FF" if _wb_pal3().is_dark else "#2F6BFF"))
        self.align_offset_label.setFont(build_app_ui_font(point_size=24, bold=True))
        audio_offset_layout.addStretch(1)
        audio_offset_layout.addWidget(audio_offset_title)
        audio_offset_layout.addWidget(self.align_offset_label)
        audio_offset_layout.addStretch(1)
        control_layout.addWidget(self.align_audio_offset_widget)
        self.align_audio_offset_widget.hide()

        control_body_height = max(
            self.align_control_placeholder.sizeHint().height(),
            self.align_video_options_widget.sizeHint().height(),
            self.align_audio_offset_widget.sizeHint().height(),
        )
        for body_widget in (
            self.align_control_placeholder,
            self.align_video_options_widget,
            self.align_audio_offset_widget,
        ):
            body_widget.setMinimumHeight(control_body_height)
            body_widget.setMaximumHeight(control_body_height)

        layout.addWidget(self.align_control_card, 0)

        self.align_export_card = CardWidget(radius=10, padding=(16, 16, 16, 16), spacing=12)
        self.align_export_card.setObjectName("AlignExportCard")
        _wb_th3(self.align_export_card, lambda: (
            "QFrame#AlignExportCard {{"
            " background: {bg}; border: 1px solid {border}; border-radius: 10px;"
            "}}"
            "QFrame#AlignExportCard QLabel {{ background: transparent; border: 0; }}"
        ).format(bg=_wb_pal3().card_bg, border=_wb_pal3().card_border))
        export_layout = self.align_export_card.createVBoxLayout()
        export_layout.setSpacing(12)
        export_layout.addWidget(StrongBodyLabel("导出"))
        self.align_export_duration_label = QLabel("预计时长 —:—（时长未知）")
        self.align_export_duration_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        _wb_th3(self.align_export_duration_label, lambda: (
            'color: {color}; font-family: "Microsoft YaHei UI"; font-size: 18pt;'
            ' font-weight: 500; background: transparent; border: 0;'
        ).format(color=_wb_pal3().text_primary))
        self.align_export_origin_label = BodyLabel("(原始 时长未知)")
        self.align_export_origin_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        from krok_helper.theme_workbench import palette as _wb_pal, themed as _wb_th
        _wb_th(self.align_export_origin_label, lambda: f"color: {_wb_pal().text_secondary}; background: transparent; border: 0;")
        export_layout.addWidget(self.align_export_duration_label)
        export_layout.addWidget(self.align_export_origin_label)
        self.align_mode_export_button = PrimaryPushButton("导出对齐视频  Ctrl+S")
        self.align_mode_export_button.setFont(build_app_ui_font(point_size=11, bold=True))
        self.align_mode_export_button.setMinimumHeight(46)
        self.align_mode_export_button.clicked.connect(
            lambda: self._trigger_alignment_export(
                ALIGN_TARGET_VIDEO if self._is_align_video_target() else ALIGN_TARGET_AUDIO
            )
        )
        self.ExportVideoBtn = self.align_mode_export_button
        self.btn_export_video = self.align_mode_export_button
        self.ExportWAVBtn = self.align_mode_export_button
        self.btn_export_wav = self.align_mode_export_button
        export_layout.addWidget(self.align_mode_export_button)
        export_actions = QHBoxLayout()
        export_actions.setContentsMargins(0, 0, 0, 0)
        export_actions.setSpacing(10)
        export_actions.addWidget(self.align_stop_export_button)
        export_actions.addWidget(self.align_open_output_button)
        export_layout.addLayout(export_actions)
        self.align_video_export_duration_label = QLabel("时长未知")
        self.align_video_export_origin_label = BodyLabel("(原始时长: 时长未知)")
        self.align_audio_export_duration_label = QLabel("时长未知")
        self.align_audio_export_origin_label = BodyLabel("(原始时长: 时长未知)")
        self.label_export_video_duration = self.align_video_export_duration_label
        self.label_export_video_src_duration = self.align_video_export_origin_label
        self.label_export_wav_duration = self.align_audio_export_duration_label
        self.label_export_wav_src_duration = self.align_audio_export_origin_label
        layout.addWidget(self.align_export_card, 0)
        layout.addStretch(1)

        self.waveform_view.offsetChanged.connect(self._refresh_original_adjustment_panel)
        self.waveform_view.offsetChanged.connect(self._refresh_alignment_export_panels)
        self.waveform_view.trimChanged.connect(lambda _value: self._refresh_alignment_export_panels())
        self._update_head_mode_buttons("black")
        self._refresh_original_adjustment_panel(self.waveform_view.offset_seconds)
        self._refresh_alignment_export_panels()
        return wrapper

    def _on_offset_finalized(self, seconds: float) -> None:
        if not hasattr(self, "align_target_video_radio") or not self.align_target_video_radio.isChecked():
            return
        if not hasattr(self, "align_lead_trim_radio"):
            return

        if seconds < 0:
            if self.align_lead_fill_white_radio.isChecked():
                self._last_fill_mode = LEAD_FILL_WHITE
            elif self.align_lead_fill_freeze_radio.isChecked():
                self._last_fill_mode = LEAD_FILL_FREEZE
            elif self.align_lead_fill_black_radio.isChecked():
                self._last_fill_mode = LEAD_FILL_BLACK

            self.align_lead_trim_radio.setEnabled(True)
            self.align_lead_fill_black_radio.setEnabled(False)
            self.align_lead_fill_white_radio.setEnabled(False)
            self.align_lead_fill_freeze_radio.setEnabled(False)
            if hasattr(self, "align_head_btn_crop"):
                self.align_head_btn_crop.setEnabled(True)
            if hasattr(self, "align_head_btn_black"):
                self.align_head_btn_black.setEnabled(False)
            if hasattr(self, "align_head_btn_white"):
                self.align_head_btn_white.setEnabled(False)
            if hasattr(self, "align_head_btn_freeze"):
                self.align_head_btn_freeze.setEnabled(False)
            self.align_lead_trim_radio.setChecked(True)
            self._update_head_mode_buttons("crop")
            return

        self.align_lead_trim_radio.setEnabled(False)
        self.align_lead_fill_black_radio.setEnabled(True)
        self.align_lead_fill_white_radio.setEnabled(True)
        self.align_lead_fill_freeze_radio.setEnabled(True)
        if hasattr(self, "align_head_btn_crop"):
            self.align_head_btn_crop.setEnabled(False)
        if hasattr(self, "align_head_btn_black"):
            self.align_head_btn_black.setEnabled(True)
        if hasattr(self, "align_head_btn_white"):
            self.align_head_btn_white.setEnabled(True)
        if hasattr(self, "align_head_btn_freeze"):
            self.align_head_btn_freeze.setEnabled(True)

        if self._last_fill_mode == LEAD_FILL_WHITE:
            self.align_lead_fill_white_radio.setChecked(True)
            self._update_head_mode_buttons("white")
        elif self._last_fill_mode == LEAD_FILL_FREEZE:
            self.align_lead_fill_freeze_radio.setChecked(True)
            self._update_head_mode_buttons("freeze")
        else:
            self.align_lead_fill_black_radio.setChecked(True)
            self._update_head_mode_buttons("black")

    def _set_alignment_nudge_step(self, seconds: float) -> None:
        """记下微调步长。

        原本后面还有一段给「小步/大步」两个按钮上色的代码，但那两个控件是早期
        布局的遗留、全仓从未创建，被 ``hasattr`` 守卫挡着恒不执行，已一并删除。
        """
        self._align_nudge_step = seconds

    def _trigger_alignment_export(self, target: str) -> None:
        if target == ALIGN_TARGET_VIDEO:
            self.align_target_video_radio.setChecked(True)
        else:
            self.align_target_audio_radio.setChecked(True)
        if self.align_export_button.isEnabled():
            self.align_export_button.click()

    def _sync_alignment_export_buttons(self) -> None:
        base_enabled = bool(getattr(self, "align_export_button", None) and self.align_export_button.isEnabled())
        is_video_target = bool(getattr(self, "align_target_video_radio", None) and self.align_target_video_radio.isChecked())
        if hasattr(self, "align_mode_export_button"):
            self.align_mode_export_button.setEnabled(base_enabled)
            self.align_mode_export_button.setText(
                "导出对齐视频  Ctrl+S" if is_video_target else "导出对齐 WAV  Ctrl+S"
            )
        video_enabled = base_enabled and is_video_target
        wav_enabled = base_enabled and not is_video_target
        if hasattr(self, "ExportVideoBtn") and self.ExportVideoBtn is not getattr(self, "align_mode_export_button", None):
            self.ExportVideoBtn.setEnabled(video_enabled)
        if hasattr(self, "btn_export_video") and self.btn_export_video is not getattr(self, "align_mode_export_button", None):
            self.btn_export_video.setEnabled(video_enabled)
        if hasattr(self, "ExportWAVBtn") and self.ExportWAVBtn is not getattr(self, "align_mode_export_button", None):
            self.ExportWAVBtn.setEnabled(wav_enabled)
        if hasattr(self, "btn_export_wav") and self.btn_export_wav is not getattr(self, "align_mode_export_button", None):
            self.btn_export_wav.setEnabled(wav_enabled)

    def _reset_alignment_waveform_view(self) -> None:
        self.waveform_view.reset_view()
        self._sync_alignment_zoom_slider()

    def _on_alignment_target_changed(self, *_args) -> None:
        target_track = ALIGN_TARGET_VIDEO if self.align_target_video_radio.isChecked() else ALIGN_TARGET_AUDIO
        if hasattr(self, "preview_timer"):
            self._stop_alignment_preview(log_message=False)
        self.waveform_view.set_target_track(target_track)
        self._refresh_align_target_ui()
        is_subtitle_target = self.rb_adjust_subtitle.isChecked()
        if self.subtitle_adjust_card is not self.original_adjust_card:
            self._set_panel_enabled(self.subtitle_adjust_card, is_subtitle_target)
            self._set_panel_enabled(self.original_adjust_card, not is_subtitle_target)
        has_waveforms = self.waveform_view.video_waveform is not None and self.waveform_view.audio_waveform is not None
        if hasattr(self, "align_control_placeholder"):
            self.align_control_placeholder.setVisible(not has_waveforms)
        if hasattr(self, "align_video_options_widget"):
            self.align_video_options_widget.setVisible(has_waveforms and is_subtitle_target)
        if hasattr(self, "align_audio_offset_widget"):
            self.align_audio_offset_widget.setVisible(has_waveforms and not is_subtitle_target)
        if not is_subtitle_target and hasattr(self, "align_lead_trim_radio"):
            self.align_lead_trim_radio.setChecked(False)
            if hasattr(self, "align_head_trim_row_widget"):
                self.align_head_trim_row_widget.setVisible(False)
            self.align_lead_trim_seconds_spin.setEnabled(False)
        self._apply_alignment_mode_styles()
        self._sync_alignment_export_buttons()
        self._refresh_alignment_export_panels()
        self._persist_alignment_preferences()

    def _alignment_accent_color(self) -> str:
        return "#F04452" if self._is_align_video_target() else "#2F6BFF"

    def _apply_alignment_mode_styles(self) -> None:
        if not hasattr(self, "align_target_video_radio"):
            return
        from krok_helper.theme_workbench import palette as _wb_pal
        p = _wb_pal()
        accent = self._alignment_accent_color()
        # 各组件的"非品牌部分"按主题分浅深
        seg_text     = p.text_primary
        seg_dis_text = p.text_disabled
        btn_dis_bg   = "#2D2D2D" if p.is_dark else "#E5E7EB"
        btn_dis_text = p.text_disabled
        nudge_bg     = p.card_bg
        nudge_border = p.card_border
        nudge_border_rgba = (
            "rgba(255, 255, 255, 0.08)" if p.is_dark else "rgba(229, 231, 235, 0.75)"
        )
        segment_button_style = f"""
        QPushButton {{
            background: transparent;
            color: {seg_text};
            border: 0;
            border-radius: 7px;
            padding: 8px 12px;
        }}
        QPushButton:checked {{
            background: {accent};
            color: #FFFFFF;
            border: 0;
        }}
        QPushButton:disabled {{
            background: transparent;
            color: {seg_dis_text};
            border: 0;
        }}
        """
        if hasattr(self, "align_target_video_button"):
            self.align_target_video_button.setChecked(self.align_target_video_radio.isChecked())
            self.align_target_audio_button.setChecked(self.align_target_audio_radio.isChecked())
            self.align_target_video_button.setStyleSheet(segment_button_style)
            self.align_target_audio_button.setStyleSheet(segment_button_style)
            self.align_target_video_button.setFont(
                build_app_ui_font(point_size=10.5, bold=self.align_target_video_button.isChecked())
            )
            self.align_target_audio_button.setFont(
                build_app_ui_font(point_size=10.5, bold=self.align_target_audio_button.isChecked())
            )
        button_style = f"""
        QPushButton {{
            background: {accent};
            color: #FFFFFF;
            border: 0;
            border-radius: 8px;
            padding: 7px 14px;
            font-size: 11pt;
        }}
        QPushButton:disabled {{
            background: {btn_dis_bg};
            color: {btn_dis_text};
        }}
        """
        if hasattr(self, "align_mode_export_button"):
            self.align_mode_export_button.setStyleSheet(button_style)
            self.align_mode_export_button.setFont(build_app_ui_font(point_size=11, bold=True))
        if hasattr(self, "align_offset_label"):
            self.align_offset_label.setText(format_offset(self.waveform_view.offset_seconds))
            self.align_offset_label.setStyleSheet(
                f'color: {accent}; font-family: "Microsoft YaHei UI"; font-size: 24pt; background: transparent; border: 0;'
            )
            self.align_offset_label.setFont(build_app_ui_font(point_size=24, bold=True))
        if hasattr(self, "align_nudge_panel"):
            self.align_nudge_panel.setStyleSheet(
                f"""
                QFrame#AlignNudgePanel {{
                    background: {nudge_bg};
                    border: 1px solid {nudge_border_rgba};
                    border-radius: 10px;
                }}
                QPushButton {{
                    background: {nudge_bg};
                    border: 1px solid {nudge_border};
                    border-radius: 7px;
                    padding: 5px 12px;
                }}
                QPushButton:hover {{
                    border-color: {accent};
                    color: {accent};
                }}
                """
            )

    def _refresh_original_adjustment_panel(self, seconds: float) -> None:
        if hasattr(self, "align_offset_label"):
            self.align_offset_label.setText(format_offset(seconds))

    def _on_align_duration_text_changed(self) -> None:
        """素材卡片的时长文案变了 —— 导出面板跟着刷新。

        构建期卡片就会写一次初始文案，那时导出面板还没建出来，因此保留
        原先"面板存在才刷新"的判断（原来写在卡片内部的 ``hasattr``）。
        """
        if hasattr(self, "align_video_export_duration_label"):
            self._refresh_alignment_export_panels()

    def _refresh_alignment_export_panels(self) -> None:
        video_waveform = self.waveform_view.video_waveform
        audio_waveform = self.waveform_view.audio_waveform

        if video_waveform is None:
            video_duration_text = "时长未知"
            video_origin_text = "时长未知"
        else:
            video_origin_text = format_media_duration(video_waveform.duration)
            video_duration_seconds = max(0.0, video_waveform.duration + self.waveform_view.offset_seconds)
            trim_duration = self._compute_video_trim_duration()
            if trim_duration is not None:
                video_duration_seconds = trim_duration
            video_duration_text = format_media_duration(video_duration_seconds)

        if audio_waveform is None:
            audio_duration_text = "时长未知"
            audio_origin_text = "时长未知"
        else:
            audio_origin_text = format_media_duration(audio_waveform.duration)
            audio_duration_seconds = max(0.0, audio_waveform.duration + self.waveform_view.offset_seconds)
            audio_duration_text = format_media_duration(audio_duration_seconds)

        self.align_video_export_duration_label.setText(video_duration_text)
        self.align_video_export_origin_label.setText(f"(原始时长: {video_origin_text})")
        self.align_audio_export_duration_label.setText(audio_duration_text)
        self.align_audio_export_origin_label.setText(f"(原始时长: {audio_origin_text})")
        if hasattr(self, "align_export_duration_label"):
            is_video_target = self._is_align_video_target()
            duration_text = video_duration_text if is_video_target else audio_duration_text
            origin_text = video_origin_text if is_video_target else audio_origin_text
            from krok_helper.theme_workbench import palette as _wb_pal
            p = _wb_pal()
            if duration_text == "时长未知":
                self.align_export_duration_label.setText("预计时长 —:—（时长未知）")
                self.align_export_duration_label.setStyleSheet(
                    f'color: {p.text_secondary}; font-family: "Microsoft YaHei UI"; font-size: 16pt;'
                    ' font-weight: 500; background: transparent; border: 0;'
                )
                self.align_export_origin_label.setText("")
            else:
                self.align_export_duration_label.setText(f"预计时长 {duration_text}")
                self.align_export_duration_label.setStyleSheet(
                    f'color: {p.text_primary}; font-family: "Microsoft YaHei UI"; font-size: 18pt;'
                    ' font-weight: 500; background: transparent; border: 0;'
                )
                self.align_export_origin_label.setText(f"(原始 {origin_text})")
            self._sync_alignment_export_buttons()
            self._apply_alignment_mode_styles()

    def _persist_alignment_preferences(self, *_args) -> None:
        if self._restoring_alignment_settings:
            return
        self._update_alignment_preferences_from_ui()
        save_app_settings(self.settings)

    def _handle_alignment_encode_mode_toggled(self, encode_mode: str, checked: bool) -> None:
        if not checked:
            return
        self._align_encode_selection = (
            encode_mode if encode_mode in {ENCODE_MODE_SOFTWARE, ENCODE_MODE_HARDWARE} else ENCODE_MODE_SOFTWARE
        )
        self._persist_alignment_preferences()

    def _current_alignment_encode_mode(self) -> str:
        if hasattr(self, "align_encode_software_radio") and self.align_encode_software_radio.isChecked():
            self._align_encode_selection = ENCODE_MODE_SOFTWARE
            return ENCODE_MODE_SOFTWARE
        if hasattr(self, "align_encode_hardware_radio") and self.align_encode_hardware_radio.isChecked():
            self._align_encode_selection = ENCODE_MODE_HARDWARE
            return ENCODE_MODE_HARDWARE
        return (
            self._align_encode_selection
            if self._align_encode_selection in {ENCODE_MODE_SOFTWARE, ENCODE_MODE_HARDWARE}
            else ENCODE_MODE_SOFTWARE
        )

    def _load_alignment_settings(self) -> None:
        """settings -> 对齐页（读方向）。与 :meth:`_collect_alignment_settings` 成对。

        灌值期间举旗：控件被 setChecked 会发信号，那时回写会把用户设置覆盖成
        中间态。以前读的是外壳的 ``_loading_settings_into_ui``（整机加载旗），
        本页只关心自己这一小段。
        """
        self._restoring_alignment_settings = True
        try:
            self.align_video_name_template_value = self.settings.align_video_name_template or DEFAULT_ALIGNED_VIDEO_NAME_TEMPLATE
            self.align_audio_name_template_value = self.settings.align_audio_name_template or DEFAULT_ALIGNED_AUDIO_NAME_TEMPLATE
            self.align_output_dir_mode_value = (
                self.settings.align_output_dir_mode
                if self.settings.align_output_dir_mode in {ALIGN_OUTPUT_DIR_SOURCE_VIDEO, ALIGN_OUTPUT_DIR_CUSTOM}
                else ALIGN_OUTPUT_DIR_SOURCE_VIDEO
            )
            self.align_output_custom_dir_text = self.settings.align_output_custom_dir.strip()
            if self.settings.align_target == ALIGN_TARGET_AUDIO:
                self.align_target_audio_radio.setChecked(True)
            else:
                self.align_target_video_radio.setChecked(True)
            self._align_encode_selection = (
                self.settings.align_encode_mode
                if self.settings.align_encode_mode in {ENCODE_MODE_SOFTWARE, ENCODE_MODE_HARDWARE}
                else ENCODE_MODE_SOFTWARE
            )
            if self._align_encode_selection == ENCODE_MODE_HARDWARE:
                self.align_encode_hardware_radio.setChecked(True)
            else:
                self.align_encode_software_radio.setChecked(True)
            self.align_force_1080p60_check.setChecked(bool(self.settings.align_force_1080p60))
            self.align_use_video_audio_check.setChecked(bool(self.settings.align_export_use_video_audio))
        finally:
            self._restoring_alignment_settings = False

    def _collect_alignment_settings(self) -> None:
        """对齐页 -> settings（写方向，命名模板部分）。

        控件当前值那部分在 :meth:`_update_alignment_preferences_from_ui`，
        它只在「不是正在把设置灌进界面」时才跑 —— 灌的过程中控件是中间态，
        回写会把用户设置盖成半成品。这里的模板值不读控件，所以无条件写。
        """
        self.settings.align_video_name_template = self.align_video_name_template_value
        self.settings.align_audio_name_template = self.align_audio_name_template_value

    def _update_alignment_preferences_from_ui(self) -> None:
        if hasattr(self, "align_target_video_radio"):
            self.settings.align_target = (
                ALIGN_TARGET_VIDEO if self.align_target_video_radio.isChecked() else ALIGN_TARGET_AUDIO
            )
        self.settings.align_encode_mode = self._current_alignment_encode_mode()
        if hasattr(self, "align_force_1080p60_check"):
            self.settings.align_force_1080p60 = self.align_force_1080p60_check.isChecked()
        if hasattr(self, "align_use_video_audio_check"):
            self.settings.align_export_use_video_audio = self.align_use_video_audio_check.isChecked()
        self.settings.align_output_dir_mode = self.align_output_dir_mode_value
        self.settings.align_output_custom_dir = self.align_output_custom_dir_text

    def set_align_video_path(self, path: Path) -> None:
        self.align_video_zone.set_path(path)
        self.align_video_info_label.setText(self._build_media_info(path, "字幕视频"))
        self._invalidate_alignment_waveforms()
        self._refresh_alignment_material_inputs()

    def set_align_audio_path(self, path: Path) -> None:
        self.align_audio_zone.set_path(path)
        self.align_audio_info_label.setText(self._build_media_info(path, "原唱音源"))
        self._invalidate_alignment_waveforms()
        self._refresh_alignment_material_inputs()

    def set_alignment_output_dir_settings(self, mode: str, custom_dir: str) -> None:
        if mode not in {ALIGN_OUTPUT_DIR_SOURCE_VIDEO, ALIGN_OUTPUT_DIR_CUSTOM}:
            raise ProcessingError("对齐输出位置无效，请重新选择。")
        self.align_output_dir_mode_value = mode
        self.align_output_custom_dir_text = custom_dir.strip()

    def _choose_align_video(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择用于对齐的字幕视频", "", "视频文件 (*.mkv *.mp4 *.mov *.avi);;所有文件 (*.*)")
        if path:
            self.set_align_video_path(Path(path))

    def _choose_align_audio(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择需要对齐的原唱音源", "", "音频或 MP4 文件 (*.flac *.wav *.mp3 *.m4a *.aac *.ape *.alac *.mkv *.mp4);;所有文件 (*.*)")
        if path:
            self.set_align_audio_path(Path(path))

    def _refresh_media_info_labels(self) -> None:
        self.align_video_info_label.setText(self._build_media_info(self.align_video_zone.path, "字幕视频"))
        self.align_audio_info_label.setText(self._build_media_info(self.align_audio_zone.path, "原唱音源"))
        if hasattr(self, "align_material_status_label"):
            self._refresh_alignment_material_inputs()

    def _refresh_alignment_material_inputs(self) -> None:
        if not hasattr(self, "align_material_status_label"):
            return
        from krok_helper.theme_workbench import palette as _wb_pal
        p = _wb_pal()
        has_video = self.align_video_zone.path is not None
        has_audio = self.align_audio_zone.path is not None
        count = int(has_video) + int(has_audio)
        self.align_video_zone.set_balanced_height(None)
        self.align_audio_zone.set_balanced_height(None)
        if count == 0:
            status_text = "① 先导入素材"
            # 警示徽章按主题切色板
            if p.is_dark:
                status_style = "background: #4A1A22; color: #FF9CAB; border: 1px solid #6A2530;"
            else:
                status_style = "background: #FFF1F2; color: #F04452; border: 1px solid #FFD1D8;"
            self.align_video_zone.set_display_mode("empty")
            self.align_audio_zone.set_display_mode("empty")
            self._align_empty_material_card_height = max(
                self.align_video_zone.sizeHint().height(),
                self.align_audio_zone.sizeHint().height(),
            )
        elif count == 1:
            missing = "原唱音频" if has_video else "字幕视频"
            status_text = f"● 已导入 1/2 · 还差{missing}"
            status_style = f"background: transparent; color: {p.text_primary}; border: 0;"
            self.align_video_zone.set_display_mode("ready" if has_video else "empty", missing_text="还需导入字幕视频")
            self.align_audio_zone.set_display_mode("ready" if has_audio else "empty", missing_text="还需导入原唱音频")
        else:
            status_text = "已导入 2/2"
            status_style = f"background: transparent; color: {p.text_secondary}; border: 0;"
            self.align_video_zone.set_display_mode("chip")
            self.align_audio_zone.set_display_mode("chip")
        if count == 1:
            balanced_height = getattr(
                self,
                "_align_empty_material_card_height",
                max(self.align_video_zone.sizeHint().height(), self.align_audio_zone.sizeHint().height()),
            )
            self.align_video_zone.set_balanced_height(balanced_height)
            self.align_audio_zone.set_balanced_height(balanced_height)
        self.align_material_status_label.setText(status_text)
        self.align_material_status_label.setStyleSheet(
            f"{status_style} border-radius: 7px; padding: 2px 10px;"
        )
        self.align_material_status_label.setFont(build_app_ui_font(point_size=10.5, bold=True))
        if self.align_clear_button is not None:
            self.align_clear_button.setVisible(count >= 1)
        if hasattr(self, "align_waveform_placeholder"):
            if count == 1:
                self.align_waveform_placeholder.setText(
                    f"再导入{'原唱音频' if has_video else '字幕视频'}后，点击「生成波形」即可在此查看对齐视图"
                )
            else:
                self.align_waveform_placeholder.setText(
                    "导入字幕视频与原唱音源后，点击「生成波形」即可在此查看对齐视图"
                )

    def _resolve_alignment_name_templates(self, *, require_valid: bool) -> tuple[str, str]:
        video_template = self.align_video_name_template_value or DEFAULT_ALIGNED_VIDEO_NAME_TEMPLATE
        audio_template = self.align_audio_name_template_value or DEFAULT_ALIGNED_AUDIO_NAME_TEMPLATE
        if require_valid:
            video_template = self._validate_alignment_name_template(
                video_template,
                "对齐后视频",
                allowed_fields={"video_name"},
                extensions=(".mp4", ".mkv"),
            )
            audio_template = self._validate_alignment_name_template(
                audio_template,
                "对齐后音频",
                allowed_fields={"audio_name", "video_name"},
                extensions=(".wav",),
            )
        return video_template, audio_template

    def _validate_alignment_name_template(
        self,
        template: str,
        label: str,
        *,
        allowed_fields: set[str],
        extensions: tuple[str, ...],
    ) -> str:
        return export_naming.validate_name_template(
            template,
            label,
            allowed_fields=allowed_fields,
            extensions=extensions,
        )

    def _append_align_log(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.align_log.appendPlainText(f"[{timestamp}] {message}")

    def _validate_alignment_inputs(self) -> tuple[Path, Path, Path | None]:
        video_path = self.align_video_zone.path
        audio_path = self.align_audio_zone.path
        ffmpeg_dir = self._resolve_ffmpeg_dir()
        if video_path is None or not video_path.is_file():
            raise ProcessingError("请先选择有效的字幕视频。")
        if audio_path is None or not audio_path.is_file():
            raise ProcessingError("请先选择有效的原唱音源。")
        return video_path, audio_path, ffmpeg_dir

    def _has_complete_alignment_inputs(self) -> bool:
        video_path = self.align_video_zone.path
        audio_path = self.align_audio_zone.path
        return (
            video_path is not None
            and audio_path is not None
            and video_path.is_file()
            and audio_path.is_file()
        )

    def _invalidate_alignment_waveforms(self) -> None:
        self._stop_alignment_preview(log_message=False)
        self.waveform_view.clear()
        self.align_status_label.setText("准备生成波形")
        if hasattr(self, "align_waveform_placeholder"):
            self.align_waveform_placeholder.show()
        if hasattr(self, "align_nudge_panel"):
            self.align_nudge_panel.hide()
        self._refresh_alignment_material_inputs()
        self._refresh_align_target_ui()
        self._refresh_alignment_preview_controls()

    def _start_alignment_analysis(self) -> None:
        if self.align_analysis_task is not None and self.align_analysis_task.isRunning():
            show_fluent_info(self, "当前波形任务还在处理中，请稍等。")
            return
        try:
            video_path, audio_path, ffmpeg_dir = self._validate_alignment_inputs()
        except ProcessingError as exc:
            show_fluent_error(self, str(exc))
            return

        self.align_log.clear()
        self.align_analyze_button.setEnabled(False)
        self.align_export_button.setEnabled(False)
        self.align_auto_button.setEnabled(False)
        self.align_preview_button.setEnabled(False)
        self.align_progress.setRange(0, 0)
        self.align_status_label.setText("生成波形中…")

        def runner(logger: Callable[[str], None]) -> tuple[WaveformData, WaveformData]:
            video_waveform = extract_waveform(video_path, ffmpeg_dir, logger, label="字幕视频音轨")
            audio_waveform = extract_waveform(audio_path, ffmpeg_dir, logger, label="原唱音源")
            return video_waveform, audio_waveform

        task = self._track_background_task("align_analysis_task", BackgroundTask(runner))
        task.log_message.connect(self._append_align_log)
        task.task_succeeded.connect(self._finish_alignment_analysis_success)
        task.task_failed.connect(self._finish_alignment_analysis_failure)
        task.start()

    def _finish_alignment_analysis_success(self, payload: object) -> None:
        self.align_progress.setRange(0, 1)
        self.align_progress.setValue(1)
        self.align_analyze_button.setEnabled(True)
        if not isinstance(payload, tuple) or len(payload) != 2:
            self._finish_alignment_analysis_failure("波形结果无效。")
            return
        video_waveform, audio_waveform = payload
        self.waveform_view.set_waveforms(video_waveform=video_waveform, audio_waveform=audio_waveform)
        self.align_video_info_label.setText(f"字幕视频: {format_media_duration(video_waveform.duration)}")
        self.align_audio_info_label.setText(f"原唱音源: {format_media_duration(audio_waveform.duration)}")
        self.align_status_label.setText("波形已生成")
        self._sync_alignment_zoom_slider()
        self._refresh_alignment_material_inputs()
        self._refresh_align_target_ui()
        self._refresh_alignment_preview_controls()

    def _finish_alignment_analysis_failure(self, message: str) -> None:
        self.align_progress.setRange(0, 1)
        self.align_progress.setValue(0)
        self.align_analyze_button.setEnabled(True)
        self.align_status_label.setText("波形生成失败")
        self._append_align_log(f"波形生成失败: {message}")
        self._refresh_alignment_preview_controls()
        show_fluent_error(self, message)

    def _auto_align_waveforms(self) -> None:
        if self.align_auto_task is not None and self.align_auto_task.isRunning():
            show_fluent_info(self, "当前自动对齐任务还在处理中，请稍等。")
            return
        if self.waveform_view.video_waveform is None or self.waveform_view.audio_waveform is None:
            show_fluent_error(self, "请先生成波形。")
            return

        video_start_seconds, audio_start_seconds = self.waveform_view.source_starts()
        target_track = ALIGN_TARGET_VIDEO if self._is_align_video_target() else ALIGN_TARGET_AUDIO
        self._stop_alignment_preview(log_message=False)
        self.align_analyze_button.setEnabled(False)
        self.align_auto_button.setEnabled(False)
        self.align_export_button.setEnabled(False)
        self.align_preview_button.setEnabled(False)
        self.align_progress.setRange(0, 0)
        self.align_status_label.setText("自动对齐中…")
        self._append_align_log(
            f"自动对齐分析从当前视图左边界开始: 视频 {video_start_seconds:.3f}s，音频 {audio_start_seconds:.3f}s"
        )

        def runner(_logger: Callable[[str], None]) -> AutoAlignResult:
            return estimate_waveform_alignment(
                self.waveform_view.video_waveform,
                self.waveform_view.audio_waveform,
                target_track=target_track,
                video_start_seconds=video_start_seconds,
                audio_start_seconds=audio_start_seconds,
            )

        task = self._track_background_task("align_auto_task", BackgroundTask(runner))
        task.task_succeeded.connect(self._finish_auto_align_success)
        task.task_failed.connect(self._finish_auto_align_failure)
        task.start()

    def _finish_auto_align_success(self, payload: object) -> None:
        self.align_progress.setRange(0, 1)
        self.align_progress.setValue(1)
        self.align_analyze_button.setEnabled(True)
        if not isinstance(payload, AutoAlignResult):
            self._finish_auto_align_failure("自动对齐结果无效。")
            return
        self.waveform_view.set_offset(payload.target_offset_seconds)
        self.waveform_view.set_playhead(max(0.0, payload.media_offset_seconds), keep_visible=True)
        confidence_percent = int(round(payload.confidence * 100))
        self.align_status_label.setText(f"自动对齐完成，置信度 {confidence_percent}%")
        target_label = "字幕视频" if self._is_align_video_target() else "原唱音源"
        self._append_align_log(
            f"自动对齐完成: 移动{target_label} {format_offset(payload.target_offset_seconds)}，"
            f"媒体相对偏移 {format_offset(payload.media_offset_seconds)}，置信度 {confidence_percent}%"
        )
        self._append_align_log(
            f"自动对齐评分: score={payload.score:.3f}, second={payload.second_score:.3f}, "
            f"overlap={payload.overlap_seconds:.2f}s, search=±{payload.search_seconds:.0f}s"
        )
        if payload.confidence < 0.55:
            self._append_align_log("自动对齐置信度偏低，建议先试听预览再确认。")
        self._refresh_alignment_preview_controls()

    def _finish_auto_align_failure(self, message: str) -> None:
        self.align_progress.setRange(0, 1)
        self.align_progress.setValue(0)
        self.align_analyze_button.setEnabled(True)
        self.align_status_label.setText("自动对齐失败")
        self._refresh_alignment_preview_controls()
        show_fluent_error(self, message)

    def _handle_align_target_changed(self) -> None:
        target_track = ALIGN_TARGET_VIDEO if self.align_target_video_radio.isChecked() else ALIGN_TARGET_AUDIO
        self._stop_alignment_preview(log_message=False)
        self.waveform_view.set_target_track(target_track)
        self._refresh_align_target_ui()

    def _refresh_align_target_ui(self) -> None:
        is_video_target = self._is_align_video_target()
        has_waveforms = self.waveform_view.video_waveform is not None and self.waveform_view.audio_waveform is not None
        self._handle_waveform_offset_changed(self.waveform_view.offset_seconds)
        self.align_drag_offset_radio.setText("移动字幕视频" if is_video_target else "移动原唱音源")
        self.align_export_button.setText("导出对齐视频" if is_video_target else "导出对齐音频")
        self.align_force_1080p60_check.setEnabled(has_waveforms and is_video_target)
        self.align_force_1080p60_card.setEnabled(has_waveforms and is_video_target)
        self.align_use_video_audio_check.setEnabled(has_waveforms and is_video_target)
        self.align_use_video_audio_card.setEnabled(has_waveforms and is_video_target)
        self._sync_align_tail_trim_controls()
        if has_waveforms and is_video_target:
            self.align_lead_row_widget.setEnabled(True)
            self.align_encode_row_widget.setEnabled(True)
            self.align_encode_software_card.setEnabled(True)
            self.align_encode_hardware_card.setEnabled(True)
            if self._align_lead_fill_selection == LEAD_FILL_WHITE:
                self.align_lead_fill_white_radio.setChecked(True)
            elif self._align_lead_fill_selection == LEAD_FILL_FREEZE:
                self.align_lead_fill_freeze_radio.setChecked(True)
            else:
                self.align_lead_fill_black_radio.setChecked(True)
            if self._align_encode_selection == ENCODE_MODE_HARDWARE:
                self.align_encode_hardware_radio.setChecked(True)
            else:
                self.align_encode_software_radio.setChecked(True)
            self._on_offset_finalized(self.waveform_view.offset_seconds)
        else:
            if self.align_lead_fill_white_radio.isChecked():
                self._align_lead_fill_selection = LEAD_FILL_WHITE
            elif self.align_lead_fill_freeze_radio.isChecked():
                self._align_lead_fill_selection = LEAD_FILL_FREEZE
            else:
                self._align_lead_fill_selection = LEAD_FILL_BLACK
            self.align_lead_fill_group.setExclusive(False)
            self.align_lead_trim_radio.setChecked(False)
            self.align_lead_fill_black_radio.setChecked(False)
            self.align_lead_fill_white_radio.setChecked(False)
            self.align_lead_fill_freeze_radio.setChecked(False)
            self.align_lead_fill_group.setExclusive(True)

            self.align_encode_group.setExclusive(False)
            self.align_encode_software_radio.setChecked(False)
            self.align_encode_hardware_radio.setChecked(False)
            self.align_encode_group.setExclusive(True)

            self.align_lead_row_widget.setEnabled(False)
            self.align_encode_row_widget.setEnabled(False)
            self.align_encode_software_card.setEnabled(False)
            self.align_encode_hardware_card.setEnabled(False)
            self.align_head_btn_crop.setEnabled(False)
            self.align_head_btn_black.setEnabled(False)
            self.align_head_btn_white.setEnabled(False)
            self.align_head_btn_freeze.setEnabled(False)
            self._update_head_mode_buttons(None)
        if self.align_control_panel is not None:
            self.align_control_panel.setEnabled(has_waveforms)
        self.waveform_view.setEnabled(has_waveforms)
        if hasattr(self, "align_control_placeholder"):
            self.align_control_placeholder.setVisible(not has_waveforms)
        if hasattr(self, "align_video_options_widget"):
            self.align_video_options_widget.setVisible(has_waveforms and is_video_target)
        if hasattr(self, "align_audio_offset_widget"):
            self.align_audio_offset_widget.setVisible(has_waveforms and not is_video_target)
        if hasattr(self, "align_waveform_placeholder"):
            self.align_waveform_placeholder.setVisible(not has_waveforms)
        if hasattr(self, "align_nudge_panel"):
            self.align_nudge_panel.setVisible(has_waveforms)
        if hasattr(self, "align_drag_mode_button"):
            self.align_drag_mode_button.setEnabled(has_waveforms)
        if has_waveforms:
            self.align_status_label.setText(self.align_status_label.text())
        self._refresh_align_trim_status(self.waveform_view.trim_end_seconds)
        self._apply_alignment_mode_styles()

    def _handle_waveform_offset_changed(self, seconds: float) -> None:
        self.align_offset_label.setText(format_offset(seconds))
        self._refresh_align_trim_status(self.waveform_view.trim_end_seconds)
        self._refresh_alignment_export_panels()
        self._apply_alignment_mode_styles()

    def _handle_playhead_changed(self, seconds: float) -> None:
        if (
            not self._suppress_preview_seek_restart
            and self.align_preview_process is not None
            and self.align_preview_process.is_running()
        ):
            self._restart_alignment_preview_from_playhead()

    def _restart_alignment_preview_from_playhead(self) -> None:
        self._start_alignment_preview()

    def _toggle_alignment_preview(self) -> None:
        if self.align_preview_process is not None and self.align_preview_process.is_running():
            self._stop_alignment_preview()
        else:
            self._start_alignment_preview()

    def _refresh_align_trim_status(self, trim_seconds: object) -> None:
        if not self._is_align_video_target():
            self.align_trim_label.setText("仅在导出字幕视频时生效")
            return

        manual_trim = trim_seconds if isinstance(trim_seconds, float) else self.waveform_view.trim_end_seconds
        parts: list[str] = []
        if manual_trim is not None:
            parts.append(f"手动尾裁到 {manual_trim:.3f}s")
        if self.align_trim_to_audio_radio.isChecked():
            auto_trim = self._compute_video_trim_duration()
            if auto_trim is not None and self.waveform_view.audio_waveform is not None:
                parts.append(f"自动最多保留到音频末尾 {self.waveform_view.audio_waveform.duration:.3f}s")
            else:
                parts.append("自动尾裁已开启")
        self.align_trim_label.setText("；".join(parts) if parts else "未设置")
        self._sync_align_tail_trim_controls()

    def _sync_align_tail_trim_controls(self) -> None:
        if not hasattr(self, "align_trim_mark_button"):
            return
        has_waveforms = self.waveform_view.video_waveform is not None and self.waveform_view.audio_waveform is not None
        is_video_target = self._is_align_video_target()
        base_enabled = has_waveforms and is_video_target
        has_manual_trim = self.waveform_view.trim_end_seconds is not None
        auto_trim_enabled = self.align_trim_to_audio_radio.isChecked()
        self.align_trim_none_radio.setEnabled(base_enabled)
        self.align_trim_to_audio_radio.setEnabled(base_enabled and not has_manual_trim)
        self.align_trim_mark_button.setEnabled(base_enabled and not auto_trim_enabled)
        self.align_trim_clear_button.setEnabled(base_enabled and not auto_trim_enabled and has_manual_trim)

    def _compute_video_trim_duration(self) -> float | None:
        if not self._is_align_video_target():
            return None
        if self.waveform_view.video_waveform is None:
            return None
        base_duration = max(0.0, self.waveform_view.video_waveform.duration + self.waveform_view.offset_seconds)
        if base_duration <= 0:
            return None
        candidates = [base_duration]
        if self.waveform_view.trim_end_seconds is not None:
            candidates.append(self.waveform_view.trim_end_seconds)
        if self.align_trim_to_audio_radio.isChecked() and self.waveform_view.audio_waveform is not None:
            candidates.append(self.waveform_view.audio_waveform.duration)
        trim_duration = min(candidates)
        if trim_duration < base_duration - 0.001:
            return max(0.001, trim_duration)
        return None

    def _refresh_alignment_preview_controls(self) -> None:
        has_inputs = self._has_complete_alignment_inputs()
        has_any_inputs = self.align_video_zone.path is not None or self.align_audio_zone.path is not None
        has_waveforms = self.waveform_view.video_waveform is not None and self.waveform_view.audio_waveform is not None
        is_playing = self.align_preview_process is not None and self.align_preview_process.is_running()
        is_exporting = self._is_align_export_running()
        is_busy = (
            (self.align_analysis_task is not None and self.align_analysis_task.isRunning())
            or (self.align_auto_task is not None and self.align_auto_task.isRunning())
            or is_exporting
        )
        self.align_analyze_button.setEnabled(has_inputs and not is_playing and not is_busy)
        self.align_auto_button.setEnabled(has_waveforms and not is_playing and not is_busy)
        self.align_preview_button.setEnabled(is_playing or (has_waveforms and not is_busy))
        if is_playing:
            self.align_preview_button.setText("停止")
            self.align_preview_button.setIcon(FIF.PAUSE.icon())
            self.align_preview_button.setToolTip("停止 (空格)")
        else:
            self.align_preview_button.setText("播放")
            self.align_preview_button.setIcon(FIF.PLAY.icon())
            self.align_preview_button.setToolTip("播放 (空格)")
        self.align_export_button.setEnabled(has_waveforms and not is_playing and not is_busy)
        self.align_stop_export_button.setEnabled(is_exporting)
        if self.align_open_output_button is not None:
            self.align_open_output_button.setEnabled(has_waveforms)
        if self.align_clear_button is not None:
            self.align_clear_button.setEnabled(has_any_inputs and not is_busy)
        if self.align_jump_to_end_button is not None:
            self.align_jump_to_end_button.setEnabled(has_waveforms)
        if self.align_reset_view_button is not None:
            self.align_reset_view_button.setEnabled(has_waveforms)
        self.align_zoom_slider.setEnabled(has_waveforms)
        if hasattr(self, "align_volume_slider"):
            self.align_volume_slider.setEnabled(has_waveforms)
        if not has_inputs and not is_busy and not is_playing:
            if self.align_video_zone.path is None and self.align_audio_zone.path is not None:
                self.align_status_label.setText("还需导入字幕视频后即可生成波形")
            elif self.align_video_zone.path is not None and self.align_audio_zone.path is None:
                self.align_status_label.setText("还需导入原唱音频后即可生成波形")
            elif self.waveform_view.video_waveform is None and self.waveform_view.audio_waveform is None:
                self.align_status_label.setText("准备生成波形")
        self._sync_alignment_export_buttons()

    def _queue_alignment_preview_volume_refresh(self, _value: int) -> None:
        if hasattr(self, "_align_volume_refresh_timer"):
            self._align_volume_refresh_timer.start()

    def _apply_alignment_preview_volume(self) -> None:
        if self.align_preview_process is not None and self.align_preview_process.is_running():
            self._restart_alignment_preview_from_playhead()

    def _is_align_export_running(self) -> bool:
        return self.align_export_task is not None and self.align_export_task.isRunning()

    def _register_align_export_process(self, process: subprocess.Popen | None) -> None:
        self._align_export_process = process

    def _cleanup_incomplete_align_exports(self) -> None:
        completed = set(self._align_export_completed_outputs)
        for path in self._align_export_expected_outputs:
            if path in completed or not path.exists():
                continue
            try:
                path.unlink()
                self._append_align_log(f"已清理未完成的输出文件: {path}")
            except OSError as exc:
                self._append_align_log(f"清理未完成的输出文件失败: {path} ({exc})")

    def _reset_align_export_state(self) -> None:
        self._align_export_cancel_requested = False
        self._align_export_process = None
        self._align_export_expected_outputs = []
        self._align_export_completed_outputs = []
        self._align_export_handoff_context = None

    def _stop_alignment_export(self) -> None:
        if not self._is_align_export_running():
            return
        if not self._align_export_cancel_requested:
            self._align_export_cancel_requested = True
            self.align_status_label.setText("正在停止导出…")
            self._append_align_log("正在停止导出…")
        process = self._align_export_process
        if process is not None:
            terminate_process(process)

    def _is_align_video_target(self) -> bool:
        return self.align_target_video_radio.isChecked()

    def _start_alignment_preview(self) -> None:
        if self.waveform_view.video_waveform is None or self.waveform_view.audio_waveform is None:
            show_fluent_error(self, "请先生成波形并完成对齐。")
            return
        try:
            video_path, audio_path, ffmpeg_dir = self._validate_alignment_inputs()
        except ProcessingError as exc:
            show_fluent_error(self, str(exc))
            return

        self._stop_alignment_preview(log_message=False)
        target_track = ALIGN_TARGET_VIDEO if self._is_align_video_target() else ALIGN_TARGET_AUDIO
        preview_start_seconds = self.waveform_view.playhead_seconds
        volume_percent = self.align_volume_slider.value() if hasattr(self, "align_volume_slider") else 50
        try:
            ffmpeg_path = find_tool("ffmpeg.exe", ffmpeg_dir)
            ffplay_path = find_tool("ffplay.exe", ffmpeg_dir)
            ffmpeg_command = build_alignment_preview_command(
                ffmpeg_path=ffmpeg_path,
                video_path=video_path,
                audio_path=audio_path,
                offset_seconds=self.waveform_view.offset_seconds,
                target_track=target_track,
                preview_start_seconds=preview_start_seconds,
            )
            ffplay_command = [
                ffplay_path,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nodisp",
                "-autoexit",
                "-volume",
                str(max(0, min(100, int(volume_percent)))),
                "-i",
                "pipe:0",
            ]
            ffmpeg_process = subprocess.Popen(
                ffmpeg_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                **_build_subprocess_kwargs(),
            )
            assert ffmpeg_process.stdout is not None
            try:
                ffplay_process = subprocess.Popen(
                    ffplay_command,
                    stdin=ffmpeg_process.stdout,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    **_build_subprocess_kwargs(),
                )
            except Exception:
                ffmpeg_process.terminate()
                raise
            finally:
                ffmpeg_process.stdout.close()
            self._append_align_log(f"预览音量: {int(volume_percent)}%")
            self.align_preview_process = AlignmentPreviewProcess(
                ffmpeg_process=ffmpeg_process,
                ffplay_process=ffplay_process,
            )
        except Exception as exc:  # noqa: BLE001
            self.align_preview_process = None
            self._append_align_log(f"播放预览失败: {exc}")
            show_fluent_error(self, f"播放预览失败:\n{exc}")
            self._refresh_alignment_preview_controls()
            return

        self.align_preview_started_at = time.monotonic()
        self.align_preview_start_seconds = preview_start_seconds
        self.align_status_label.setText("正在播放预览")
        self.preview_timer.start()
        self._refresh_alignment_preview_controls()

    def _stop_alignment_preview(self, *, log_message: bool = True) -> None:
        process = self.align_preview_process
        if process is not None:
            process.stop()
            self.align_preview_process = None
            if log_message:
                self._append_align_log("播放预览已停止")
        self.preview_timer.stop()
        self.align_preview_started_at = 0.0
        self.align_preview_start_seconds = 0.0
        self._refresh_alignment_preview_controls()

    def _poll_alignment_preview(self) -> None:
        process = self.align_preview_process
        if process is None:
            self.preview_timer.stop()
            self._refresh_alignment_preview_controls()
            return
        if process.is_running():
            elapsed = time.monotonic() - self.align_preview_started_at
            self._suppress_preview_seek_restart = True
            try:
                self.waveform_view.set_playhead(self.align_preview_start_seconds + elapsed, keep_visible=True)
            finally:
                self._suppress_preview_seek_restart = False
            return
        self.preview_timer.stop()
        self.align_preview_process = None
        self.align_preview_started_at = 0.0
        self.align_preview_start_seconds = 0.0
        self.align_status_label.setText("预览播放结束")
        self._append_align_log("播放预览结束")
        self._refresh_alignment_preview_controls()

    def _resolve_alignment_output_dir(self, video_path: Path) -> Path:
        custom_dir = (
            self.align_output_custom_dir_text
            if self.align_output_dir_mode_value == ALIGN_OUTPUT_DIR_CUSTOM
            else None
        )
        return export_naming.resolve_output_dir(video_path, custom_dir=custom_dir)

    def _render_alignment_output_path(
        self,
        *,
        video_path: Path,
        audio_path: Path,
        is_video_target: bool,
    ) -> Path:
        video_template, audio_template = self._resolve_alignment_name_templates(require_valid=True)
        return export_naming.render_output_path(
            template=video_template if is_video_target else audio_template,
            video_path=video_path,
            audio_path=audio_path,
            extension=".mp4" if is_video_target else ".wav",
            label="对齐后视频" if is_video_target else "对齐后音频",
            output_dir=self._resolve_alignment_output_dir(video_path),
        )

    def _start_aligned_export(self) -> None:
        if self.align_export_task is not None and self.align_export_task.isRunning():
            show_fluent_info(self, "当前导出任务还在处理中，请稍等。")
            return
        if self.waveform_view.video_waveform is None or self.waveform_view.audio_waveform is None:
            show_fluent_error(self, "请先生成波形并完成对齐。")
            return
        try:
            video_path, audio_path, ffmpeg_dir = self._validate_alignment_inputs()
            is_video_target = self._is_align_video_target()
            initial_path = self._render_alignment_output_path(
                video_path=video_path,
                audio_path=audio_path,
                is_video_target=is_video_target,
            )
            output_kind = "对齐视频" if is_video_target else "对齐音频"
            if is_video_target:
                output_path_text, _ = QFileDialog.getSaveFileName(
                    self,
                    "导出对齐视频",
                    str(initial_path),
                    "MP4 视频 (*.mp4);;Matroska 视频 (*.mkv);;所有文件 (*.*)",
                )
            else:
                output_path_text, _ = QFileDialog.getSaveFileName(
                    self,
                    "导出对齐音频",
                    str(initial_path),
                    "WAV 音频 (*.wav);;所有文件 (*.*)",
                )
        except ProcessingError as exc:
            show_fluent_error(self, str(exc))
            return

        if not output_path_text:
            return
        output_path = Path(output_path_text).expanduser()
        offset_seconds = self.waveform_view.offset_seconds
        encode_mode = self._current_alignment_encode_mode()
        if self.align_lead_fill_white_radio.isChecked():
            lead_fill_color = LEAD_FILL_WHITE
        elif self.align_lead_fill_freeze_radio.isChecked():
            lead_fill_color = LEAD_FILL_FREEZE
        else:
            lead_fill_color = LEAD_FILL_BLACK
        force_1080p60 = self.align_force_1080p60_check.isChecked()
        use_source_video_audio = self.align_use_video_audio_check.isChecked() if is_video_target else False
        video_trim_duration = self._compute_video_trim_duration() if is_video_target else None
        self._align_export_cancel_requested = False
        self._align_export_process = None
        self._align_export_expected_outputs = [output_path]
        self._align_export_completed_outputs = []
        self._align_export_handoff_context = (
            is_video_target,
            video_path,
            audio_path,
            output_kind,
        )

        self._stop_alignment_preview(log_message=False)
        self.align_analyze_button.setEnabled(False)
        self.align_auto_button.setEnabled(False)
        self.align_export_button.setEnabled(False)
        self.align_preview_button.setEnabled(False)
        self.align_progress.setRange(0, 0)
        self.align_status_label.setText("导出中…")

        def runner(logger: Callable[[str], None]) -> list[Path]:
            outputs: list[Path] = []
            if is_video_target:
                outputs.append(
                    export_aligned_video(
                        video_path=video_path,
                        audio_path=audio_path,
                        output_path=output_path,
                        offset_seconds=offset_seconds,
                        ffmpeg_dir=ffmpeg_dir,
                        logger=logger,
                        should_cancel=lambda: self._align_export_cancel_requested,
                        on_process_started=self._register_align_export_process,
                        encode_mode=encode_mode,
                        lead_fill_color=lead_fill_color,
                        force_1080p60=force_1080p60,
                        output_duration_seconds=video_trim_duration,
                        use_source_video_audio=use_source_video_audio,
                    )
                )
                self._align_export_completed_outputs.append(outputs[-1])
            else:
                outputs.append(
                    export_aligned_audio(
                        audio_path=audio_path,
                        output_path=output_path,
                        offset_seconds=offset_seconds,
                        ffmpeg_dir=ffmpeg_dir,
                        logger=logger,
                        should_cancel=lambda: self._align_export_cancel_requested,
                        on_process_started=self._register_align_export_process,
                    )
                )
                self._align_export_completed_outputs.append(outputs[-1])
            return outputs

        task = self._track_background_task("align_export_task", BackgroundTask(runner))
        task.log_message.connect(self._append_align_log)
        # Connect to bound QObject methods so Qt queues completion back to this
        # window's GUI thread. A lambda runs in BackgroundTask's worker thread
        # and would make the modal handoff dialog visible but unresponsive.
        task.task_succeeded.connect(self._finish_aligned_export_success)
        task.task_failed.connect(self._finish_aligned_export_failure)
        task.start()
        self._refresh_alignment_preview_controls()

    @Slot(object)
    def _finish_aligned_export_success(self, output_paths: object) -> None:
        context = self._align_export_handoff_context
        if context is None:
            self._finish_aligned_export(True, "", output_paths, "对齐文件")
            return
        is_video_target, video_path, audio_path, output_kind = context
        self._finish_aligned_export(
            True,
            "",
            output_paths,
            output_kind,
            is_video_target=is_video_target,
            source_video_path=video_path,
            source_audio_path=audio_path,
        )

    @Slot(str)
    def _finish_aligned_export_failure(self, message: str) -> None:
        context = self._align_export_handoff_context
        output_kind = context[3] if context is not None else "对齐文件"
        self._finish_aligned_export(False, message, None, output_kind)

    def _finish_aligned_export(
        self,
        success: bool,
        message: str,
        output_paths: object,
        output_kind: str,
        *,
        is_video_target: bool | None = None,
        source_video_path: Path | None = None,
        source_audio_path: Path | None = None,
    ) -> None:
        was_cancelled = self._align_export_cancel_requested
        self._align_export_process = None
        self.align_progress.setRange(0, 1)
        self.align_progress.setValue(1 if success and not was_cancelled else 0)
        self.align_analyze_button.setEnabled(True)
        if was_cancelled:
            self._cleanup_incomplete_align_exports()
            self.align_status_label.setText("导出已停止")
            self._refresh_alignment_preview_controls()
            self._reset_align_export_state()
            self._append_align_log("导出任务已终止，未完成的输出文件已清理。")
            return
        self.align_status_label.setText("导出完成" if success else "导出失败")
        self._refresh_alignment_preview_controls()
        self._reset_align_export_state()
        if success and isinstance(output_paths, list):
            exported_paths = [Path(path) for path in output_paths]
            if not exported_paths:
                return
            resolved_target = (
                self._is_align_video_target()
                if is_video_target is None
                else is_video_target
            )
            self._offer_alignment_handoff(
                is_video_target=resolved_target,
                output_path=exported_paths[-1],
                source_video_path=(
                    source_video_path
                    if source_video_path is not None
                    else getattr(getattr(self, "align_video_zone", None), "path", None)
                ),
                source_audio_path=(
                    source_audio_path
                    if source_audio_path is not None
                    else getattr(getattr(self, "align_audio_zone", None), "path", None)
                ),
            )
            return
        self._append_align_log(f"导出失败: {message}")
        show_fluent_error(self, message)

    def _offer_alignment_handoff(
        self,
        *,
        is_video_target: bool,
        output_path: Path,
        source_video_path: Path | None,
        source_audio_path: Path | None,
    ) -> None:
        dialog = AlignmentHandoffDialog(
            is_video_target=is_video_target,
            output_path=output_path,
            parent=self,
        )
        self._alignment_handoff_dialog = dialog
        self._alignment_handoff_payload = (
            is_video_target,
            output_path,
            source_video_path,
            source_audio_path,
        )
        dialog.accepted.connect(self._apply_alignment_handoff)
        dialog.finished.connect(self._clear_alignment_handoff_dialog)
        # Keep this confirmation modeless. The previous full-window Fluent
        # mask disabled the workbench (including its title-bar close button)
        # and could then fail to receive input itself on Windows.
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    @Slot()
    def _apply_alignment_handoff(self) -> None:
        dialog = self._alignment_handoff_dialog
        payload = self._alignment_handoff_payload
        if dialog is None or payload is None:
            return
        is_video_target, output_path, source_video_path, source_audio_path = payload
        send_to_subtitle, send_to_hires = dialog.selections()

        if send_to_subtitle:
            background_path = output_path if is_video_target else source_video_path
            render_page = getattr(self, "subtitle_render_page", None)
            load_video = getattr(render_page, "load_video", None)
            if background_path is not None and callable(load_video):
                load_video(Path(background_path))
                self._notify_handoff(
                    "背景素材已交给字幕渲染",
                    f"「{Path(background_path).name}」已放入第 5 步字幕视频生成。",
                )

        if send_to_hires:
            vocal_path = source_audio_path if is_video_target else output_path
            if vocal_path is not None:
                self.set_on_vocal_path(Path(vocal_path))
                self._notify_handoff(
                    "原唱音源已交给 Hi-Res",
                    f"「{Path(vocal_path).name}」已放入第 6 步 Hi-Res 混流的原唱卡。",
                )

    @Slot(int)
    def _clear_alignment_handoff_dialog(self, _result: int) -> None:
        self._alignment_handoff_dialog = None
        self._alignment_handoff_payload = None

    def _clear_alignment_inputs(self) -> None:
        if (
            (self.align_analysis_task is not None and self.align_analysis_task.isRunning())
            or (self.align_auto_task is not None and self.align_auto_task.isRunning())
            or (self.align_export_task is not None and self.align_export_task.isRunning())
        ):
            show_fluent_info(self, "当前对齐任务还在处理中，请稍等。")
            return
        self._stop_alignment_preview(log_message=False)
        self.align_video_zone.clear_path()
        self.align_audio_zone.clear_path()
        self.align_log.clear()
        self.waveform_view.clear()
        self._refresh_media_info_labels()
        self._refresh_align_target_ui()
        self._refresh_alignment_preview_controls()
        self.align_status_label.setText("准备生成波形")

    def _open_align_output_dir(self) -> None:
        video_path = self.align_video_zone.path
        source_path = video_path or self.align_audio_zone.path
        if source_path is None:
            show_fluent_info(self, "请先选择文件。")
            return
        try:
            output_dir = self._resolve_alignment_output_dir(video_path) if video_path is not None else source_path.parent
        except ProcessingError as exc:
            show_fluent_error(self, str(exc))
            return
        output_dir.mkdir(parents=True, exist_ok=True)
        open_in_explorer(output_dir)
