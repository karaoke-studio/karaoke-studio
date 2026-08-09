from __future__ import annotations

import ctypes
import logging
import math
import os
import subprocess
import sys
import threading
import time
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from string import Formatter
from typing import Callable, Sequence

from krok_helper import ensure_sug_src_path

DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_DONOTROUND = 1

os.environ["QFLUENT_WIDGETS_NO_PROMOTION"] = "1"


def _schedule_hard_process_exit(delay_seconds: float = 0.25) -> None:
    """Guarantee updater handoff even if Qt or a worker prevents normal teardown."""

    timer = threading.Timer(delay_seconds, lambda: os._exit(0))
    timer.daemon = True
    timer.start()

from PyQt6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QEvent,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSize,
    QThread,
    QTimer,
    Qt,
    QUrl,
    pyqtProperty,
    pyqtSignal as Signal,
    pyqtSlot as Slot,
)
from PyQt6.QtGui import QColor, QBrush, QDesktopServices, QFont, QFontMetrics, QIcon, QKeySequence, QPainter, QPalette, QPen, QPixmap, QShortcut, QTextDocument
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QBoxLayout,
    QButtonGroup,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGraphicsDropShadowEffect,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QTextBrowser,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QStyleOptionViewItem,
    QStyledItemDelegate,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CheckBox as QCheckBox,
    ComboBox as QComboBox,
    FluentIcon as FIF,
    HyperlinkCard,
    LineEdit as QLineEdit,
    ListWidget as FluentListWidget,
    PlainTextEdit as QPlainTextEdit,
    Pivot,
    PrimaryPushButton,
    ProgressBar as QProgressBar,
    PushButton as QPushButton,
    PushSettingCard,
    RadioButton as QRadioButton,
    ScrollArea as FluentScrollArea,
    SettingCard,
    SettingCardGroup,
    setThemeColor,
    Slider as QSlider,
    StrongBodyLabel,
    SubtitleLabel,
    SwitchButton,
    TableWidget as QTableWidget,
    TitleLabel,
    ToolButton,
    qconfig,
)
from qfluentwidgets.components.widgets.combo_box import ComboBoxMenu
from qfluentwidgets.components.widgets.menu import MenuAnimationType
from qfluentwidgets.components.widgets.table_view import TableItemDelegate

from krok_helper.qfluent_compat import (
    ModelessDialog,
    apply_qfluent_menu_lifetime_patch,
    apply_qfluent_tooltip_parent_patch,
    ask_fluent_confirm,
    exec_modeless_dialog,
    show_fluent_info,
    show_fluent_tooltip,
)
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
    export_aligned_audio,
    export_aligned_video,
    estimate_waveform_alignment,
    extract_waveform,
    format_offset,
    start_alignment_preview,
)
from krok_helper.alignment import AlignmentDropCard, AlignmentHandoffDialog, WaveformView
from krok_helper.config import (
    APP_NAME,
    APP_TITLE,
    APP_VERSION,
    WINDOW_HEIGHT,
    WINDOW_MIN_HEIGHT,
    WINDOW_MIN_WIDTH,
    WINDOW_WIDTH,
)
from krok_helper.errors import ExportCancelled, ProcessingError
from krok_helper.ffmpeg import _build_subprocess_kwargs, find_tool, probe_media, terminate_process
from krok_helper.lyrics import (
    DEFAULT_LYRICS_SEARCH_LIMIT,
    DEFAULT_LYRICS_PROVIDER_IDS,
    LYRICS_LANGUAGE_ORIGINAL,
    LYRICS_LANGUAGE_TRANSLATION,
    LYRICS_PREVIEW_LINE,
    LYRICS_PREVIEW_VERBATIM,
    LyricsPreview,
    LyricsSearchBatch,
    LyricsSearchCandidate,
    LyricsSearchService,
    UTATEN_RUBY_MARKER,
    build_lyrics_preview,
    extract_lyrics_query_from_file,
)
from krok_helper.logging_config import get_active_log_dir
from krok_helper.notifications import play_completion_sound
from krok_helper.pipeline import (
    DEFAULT_OFF_NAME_TEMPLATE,
    DEFAULT_ON_NAME_TEMPLATE,
    OUTPUT_NAME_MODE_FIXED,
    OUTPUT_NAME_MODE_TEMPLATE,
    OUTPUT_NAME_MODE_VIDEO_NAME,
    resolve_output_dir,
    resolve_off_output_paths,
    resolve_output_paths,
    run_pipeline,
    validate_output_name_template,
)
from krok_helper.settings import (
    ALIGN_OUTPUT_DIR_CUSTOM,
    ALIGN_TARGET_AUDIO,
    ALIGN_TARGET_VIDEO,
    ALIGN_OUTPUT_DIR_SOURCE_VIDEO,
    AppSettings,
    consume_corruption_backup,
    get_settings_path,
    import_legacy_sug_settings,
    load_app_settings,
    migrate_strange_uta_game_settings,
    save_app_settings,
)
from krok_helper.sug_compat import apply_sug_compat_patches
from krok_helper.ui_kit import (
    CardWidget,
    ControlBar,
    DEFAULT_UI_FONT_FAMILIES,
    ElidedLabel,
    build_app_ui_font,
)
from krok_helper.updater import CheckResult, UpdateChecker, ensure_updater_settings
from krok_helper.updater.settings import UpdaterSettings
from krok_helper.updater.sources import SOURCE_IDS, SOURCE_LABELS, normalize_order
from krok_helper.video_download import VideoDownloadPage
from krok_helper.windows import set_explicit_app_user_model_id

apply_qfluent_menu_lifetime_patch()
apply_qfluent_tooltip_parent_patch()


VIDEO_EXTENSIONS = {".mkv", ".mp4", ".mov", ".avi"}
AUDIO_EXTENSIONS = {".flac", ".wav", ".mp3", ".m4a", ".aac", ".ape", ".alac", ".mkv"}
HIRES_AUDIO_EXTENSIONS = AUDIO_EXTENSIONS | {".mp4"}
ALIGN_AUDIO_EXTENSIONS = AUDIO_EXTENSIONS | {".mp4"}
WINDOWS_INVALID_FILENAME_CHARS = '<>:"/\\|?*'
ALIGNMENT_TEMPLATE_FORMATTER = Formatter()
FFMPEG_DIR_PLACEHOLDER = "未设置，将优先使用系统 PATH 中的 ffmpeg"
WORKFLOW_VIDEO_DOWNLOAD = "video_download"
WORKFLOW_WAVEFORM_ALIGN = "waveform_align"
WORKFLOW_LYRICS_SEARCH = "lyrics_search"
WORKFLOW_LYRICS_TIMING = "lyrics_timing"
WORKFLOW_SUBTITLE_RENDER = "subtitle_render"
WORKFLOW_HIRES_MIX = "hires_mix"
LYRICS_SOURCE_OPTIONS = [
    ("聚合", DEFAULT_LYRICS_PROVIDER_IDS),
    ("QQ音乐", ("qm",)),
    ("酷狗音乐", ("kg",)),
    ("网易云音乐", ("ne",)),
    ("LRCLIB", ("lrclib",)),
    # UtaTen 走带注音的 LRC 专用通道，与上面几条通用歌词来源差别较大，故放最后单列。
    ("UtaTen", ("utaten",)),
]
LYRICS_SOURCE_MAP = {label: provider_ids for label, provider_ids in LYRICS_SOURCE_OPTIONS}
LYRICS_PREVIEW_MODE_OPTIONS = [
    ("按行 LRC", LYRICS_PREVIEW_LINE),
    ("按字 LRC", LYRICS_PREVIEW_VERBATIM),
]
LYRICS_PREVIEW_MODE_MAP = {label: mode for label, mode in LYRICS_PREVIEW_MODE_OPTIONS}


class KrokHelperSettingsBridge:
    """Bridge StrangeUtaGame config into krok-helper's settings namespace."""

    _EXTRA_FIELDS = {
        "dictionary": "lyrics_timing_dictionary",
        "singers": "lyrics_timing_singers",
        "network": "lyrics_timing_network_dictionary",
    }

    def __init__(self, app_settings: AppSettings, save_callback: Callable[[], object]) -> None:
        self._app_settings = app_settings
        self._save_callback = save_callback

    def load(self) -> dict:
        latest = load_app_settings()
        self._app_settings.lyrics_timing = deepcopy(latest.lyrics_timing)
        return deepcopy(self._app_settings.lyrics_timing)

    def save(self, data: dict) -> None:
        latest = load_app_settings()
        latest.lyrics_timing = deepcopy(data)
        save_app_settings(latest)
        self._app_settings.lyrics_timing = deepcopy(latest.lyrics_timing)

    def save_partial(self, changes: dict[str, object]) -> None:
        latest = load_app_settings()
        config = deepcopy(latest.lyrics_timing)
        for path, value in changes.items():
            self._set_nested(config, path, value)
        latest.lyrics_timing = config
        save_app_settings(latest)
        self._app_settings.lyrics_timing = deepcopy(config)

    def load_extra(self, key: str, default):
        field_name = self._EXTRA_FIELDS.get(key)
        if field_name is None:
            return deepcopy(default)
        latest = load_app_settings()
        setattr(self._app_settings, field_name, deepcopy(getattr(latest, field_name, default)))
        return deepcopy(getattr(self._app_settings, field_name, default))

    def save_extra(self, key: str, data) -> None:
        field_name = self._EXTRA_FIELDS.get(key)
        if field_name is None:
            return
        latest = load_app_settings()
        setattr(latest, field_name, deepcopy(data))
        save_app_settings(latest)
        setattr(self._app_settings, field_name, deepcopy(data))

    @staticmethod
    def _set_nested(target: dict, path: str, value: object) -> None:
        cursor = target
        keys = [key for key in str(path).split(".") if key]
        if not keys:
            return
        for key in keys[:-1]:
            child = cursor.get(key)
            if not isinstance(child, dict):
                child = {}
                cursor[key] = child
            cursor = child
        cursor[keys[-1]] = deepcopy(value)


class WorkbenchUpdateDialog(ModelessDialog):
    """Update prompt that shows the GitHub Release body directly."""

    def __init__(
        self,
        release,
        *,
        local_version: str,
        source_label: str = "",
        all_releases: list | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.user_choice = "later"
        self._release = release
        self.setWindowTitle(APP_TITLE)
        self.setModal(False)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setMinimumSize(720, 560)
        self._build_ui(release, local_version, source_label, all_releases or [])

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        QTimer.singleShot(0, self._bring_to_front)

    def _bring_to_front(self) -> None:
        self.raise_()
        self.activateWindow()

    @staticmethod
    def _compose_changelog(release, all_releases: list) -> tuple[str, int]:
        """聚合跨版本更新日志。

        返回 ``(markdown 文本, 版本数)``；只有 1 个版本时保持原样输出该版本 body。
        """
        releases = list(all_releases)
        if len(releases) <= 1:
            single = releases[0] if releases else release
            return (getattr(single, "body", "") or "").strip(), 1
        sections: list[str] = []
        for item in releases:
            date = (getattr(item, "published_at", "") or "")[:10]
            heading = f"## v{item.version}" + (f"（{date}）" if date else "")
            body = (getattr(item, "body", "") or "").strip() or "（该版本没有填写发布说明）"
            sections.append(f"{heading}\n\n{body}")
        return "\n\n---\n\n".join(sections), len(releases)

    def _build_ui(self, release, local_version: str, source_label: str, all_releases: list) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 26, 28, 22)
        layout.setSpacing(14)

        title = QLabel("发现新版本")
        title.setStyleSheet('font-family: "Microsoft YaHei UI"; font-size: 22pt; font-weight: 700; color: #111827;')
        layout.addWidget(title)

        version = QLabel(f"v{release.version}")
        version.setStyleSheet('font-family: "Microsoft YaHei UI"; font-size: 15pt; font-weight: 700; color: #111827;')
        layout.addWidget(version)

        published_at = release.published_at[:10] if getattr(release, "published_at", "") else "未知日期"
        meta_parts = [f"当前版本 v{local_version}", f"发布于 {published_at}"]
        if source_label:
            meta_parts.append(f"下载源：{source_label}")
        meta = QLabel("  |  ".join(meta_parts))
        meta.setStyleSheet('font-family: "Microsoft YaHei UI"; font-size: 10pt; color: #6b7280;')
        meta.setWordWrap(True)
        layout.addWidget(meta)

        body_text, release_count = self._compose_changelog(release, all_releases)
        content_label = QLabel(
            f"更新内容（当前版本之后共 {release_count} 个版本）："
            if release_count > 1
            else "更新内容："
        )
        content_label.setStyleSheet('font-family: "Microsoft YaHei UI"; font-size: 10.5pt; color: #111827;')
        layout.addWidget(content_label)

        body_view = QTextBrowser()
        body_view.setReadOnly(True)
        body_view.setOpenExternalLinks(True)
        body_view.setMinimumHeight(300)
        body_view.setStyleSheet(
            """
            QTextBrowser {
                background: #ffffff;
                border: 1px solid #d9dfe8;
                border-radius: 8px;
                padding: 12px;
                font-family: "Microsoft YaHei UI";
                font-size: 10.5pt;
                color: #111827;
            }
            """
        )
        if body_text:
            try:
                body_view.setMarkdown(body_text)
            except Exception:
                body_view.setPlainText(body_text)
        else:
            body_view.setPlainText("本次 Release 没有填写发布说明。")
        layout.addWidget(body_view, 1)

        html_url = (getattr(release, "html_url", "") or "").strip()
        if html_url:
            open_release_button = QPushButton(FIF.LINK, "在浏览器中查看完整发布说明")
            open_release_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(html_url)))
            layout.addWidget(open_release_button, 0, Qt.AlignmentFlag.AlignHCenter)

        button_row = QHBoxLayout()
        button_row.setSpacing(12)
        skip_button = QPushButton("跳过此版本")
        later_button = QPushButton("稍后再说")
        update_button = PrimaryPushButton("立即更新")
        skip_button.clicked.connect(self._choose_skip)
        later_button.clicked.connect(self._choose_later)
        update_button.clicked.connect(self._choose_update)
        button_row.addWidget(skip_button)
        button_row.addStretch(1)
        button_row.addWidget(later_button)
        button_row.addWidget(update_button)
        layout.addLayout(button_row)

    def _choose_update(self) -> None:
        self.user_choice = "update"
        self.accept()

    def _choose_later(self) -> None:
        self.user_choice = "later"
        self.reject()

    def _choose_skip(self) -> None:
        self.user_choice = "skip"
        self.accept()


LYRICS_LANGUAGE_OPTIONS = [
    ("原文", LYRICS_LANGUAGE_ORIGINAL),
    ("中文译文", LYRICS_LANGUAGE_TRANSLATION),
]
LYRICS_LANGUAGE_MAP = {label: value for label, value in LYRICS_LANGUAGE_OPTIONS}

APP_LOGO_PATH = Path(__file__).resolve().parent / "assets" / "logo" / "logo.jpg"
TASKBAR_LOGO_PATH = Path(__file__).resolve().parent / "assets" / "logo" / "logo2.png"
def combo_box_view_qss() -> str:
    from krok_helper.theme_workbench import palette

    p = palette()
    selected_bg = "#3A2A2C" if p.is_dark else "#FFF1F2"
    selected_text = p.text_primary if p.is_dark else "#111827"
    hover_bg = p.input_hover_bg if p.is_dark else "#F8FAFC"
    return f"""
    QAbstractItemView {{
        background-color: transparent;
        border: none;
        border-radius: 0px;
        padding: 4px;
        outline: none;
        color: {p.text_primary};
        selection-background-color: {selected_bg};
        selection-color: {selected_text};
    }}

    QAbstractItemView::item {{
        height: 32px;
        padding: 0 12px;
        border-radius: 6px;
        color: {p.text_primary};
    }}

    QAbstractItemView::item:hover {{
        background-color: {hover_bg};
    }}

    QAbstractItemView::item:selected {{
        background-color: {selected_bg};
        color: {selected_text};
    }}
    """
def open_in_explorer(path: Path) -> None:
    subprocess.Popen(["explorer", str(path)])


def load_app_icon() -> QIcon | None:
    if not APP_LOGO_PATH.exists():
        return None
    icon = QIcon(str(APP_LOGO_PATH))
    return None if icon.isNull() else icon


def load_taskbar_icon() -> QIcon | None:
    if TASKBAR_LOGO_PATH.exists():
        icon = QIcon(str(TASKBAR_LOGO_PATH))
        if not icon.isNull():
            return icon
    return load_app_icon()


def apply_safe_label_metrics(
    label: QLabel,
    font: QFont,
    *,
    top_padding: int = 3,
    bottom_padding: int = 2,
) -> None:
    margins = label.contentsMargins()
    label.setContentsMargins(margins.left(), top_padding, margins.right(), bottom_padding)
    label.setMinimumHeight(QFontMetrics(font).height() + top_padding + bottom_padding)


def apply_card_shadow(widget: QWidget, *, alpha: int = 20) -> None:
    shadow = QGraphicsDropShadowEffect(widget)
    shadow.setBlurRadius(12)
    shadow.setXOffset(0)
    shadow.setYOffset(2)
    shadow.setColor(QColor(16, 24, 40, alpha))
    widget.setGraphicsEffect(shadow)


def build_lyrics_ui_font(*, point_size: float = 10.5, bold: bool = False) -> QFont:
    return build_app_ui_font(point_size=point_size, bold=bold)


def sync_fluent_ui_fonts() -> None:
    qconfig.set(qconfig.fontFamilies, DEFAULT_UI_FONT_FAMILIES, save=False)


def format_media_duration(seconds: float | None) -> str:
    if seconds is None or seconds <= 0:
        return "时长未知"

    minutes = int(seconds // 60)
    remainder = seconds - minutes * 60
    if minutes:
        return f"{minutes}:{remainder:06.3f}"
    return f"{seconds:.3f}s"


class WhiteComboBoxMenu(ComboBoxMenu):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.NoDropShadowWindowHint)
        # 保留 qfluentwidgets 默认的透明顶层窗口，不要关闭 WA_TranslucentBackground
        self.view.setStyleSheet(combo_box_view_qss())
        self.view.setFrameShape(QFrame.Shape.NoFrame)
        self.hBoxLayout.setContentsMargins(0, 0, 0, 0)
        self.hBoxLayout.setSpacing(0)
        self.view.setViewportMargins(0, 0, 0, 0)
        self.setShadowEffect(blurRadius=0, offset=(0, 0), color=QColor(0, 0, 0, 0))

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        try:
            preference = ctypes.c_int(DWMWCP_DONOTROUND)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                int(self.winId()),
                DWMWA_WINDOW_CORNER_PREFERENCE,
                ctypes.byref(preference),
                ctypes.sizeof(preference),
            )
        except Exception:
            pass

    def exec(self, pos, ani=True, aniType=MenuAnimationType.DROP_DOWN):
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.adjustSize(pos, aniType)

        overflow = self.view.verticalScrollBar().maximum()
        if overflow > 0:
            self.view.setFixedHeight(self.view.height() + overflow + 8)

        self.adjustSize()
        return super().exec(pos, ani, aniType)

    def paintEvent(self, event) -> None:  # noqa: N802
        from krok_helper.theme_workbench import palette

        p = palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(p.input_border), 1))
        painter.setBrush(QColor(p.input_bg))
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 8, 8)


class StyledComboBox(QComboBox):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

    def _createComboMenu(self):
        return WhiteComboBoxMenu(self)


def relax_setting_card_height(
    card: SettingCard,
    *,
    available_width: int = 0,
    min_content_lines: int = 0,
) -> None:
    """让 ``SettingCard`` 的说明文字换行，并把卡片撑到能放下的真实高度。

    ``SettingCardGroup`` 内部的 ExpandLayout 只按卡片**当前**高度垂直堆叠，
    不做宽度→高度协商；而且卡片构造时给 hBoxLayout 设的整体 AlignVCenter
    会把文字列高度锁死在 sizeHint（单行说明）。因此这里显式完成三件事：
    清掉构造时的 addStretch(1) 让文字列独占剩余宽度、按换行结果给
    contentLabel 写 minimumHeight、再按内容总高把卡片高度写死。
    对话框显示后可用真实卡宽再调一次以修正估算误差。
    """
    card.contentLabel.setWordWrap(True)
    layout = card.hBoxLayout
    for index in range(layout.count()):
        item = layout.itemAt(index)
        spacer = item.spacerItem() if item is not None else None
        if spacer is not None and spacer.sizePolicy().horizontalPolicy() == QSizePolicy.Policy.Expanding:
            layout.setStretch(index, 0)
    layout.setStretchFactor(card.vBoxLayout, 1)
    # vBox 里 title/content 构造时带 AlignLeft：label 不拉伸、宽度被锁在
    # sizeHint（约 30 字符），实际换行宽度远窄于文字列宽。清成 0 让 label
    # 填满文字列，换行位置才与测量一致。
    for index in range(card.vBoxLayout.count()):
        item = card.vBoxLayout.itemAt(index)
        if item is not None:
            item.setAlignment(Qt.AlignmentFlag(0))
    width = available_width or card.width()
    if width > 0:
        card.resize(width, card.height())
        layout.activate()
    text_width = max(card.vBoxLayout.geometry().width(), 40)
    # QLabel.heightForWidth 对 CJK 换行会低估；QLabel 渲染 wordWrap 文本走
    # 的就是 QTextDocument（含默认 4px documentMargin），直接用它量才精确。
    document = QTextDocument()
    document.setDocumentMargin(4)
    document.setDefaultFont(card.contentLabel.font())
    document.setPlainText(card.contentLabel.text())
    document.setTextWidth(text_width)
    content_height = math.ceil(document.size().height())
    if min_content_lines > 0:
        content_height = max(
            content_height,
            card.contentLabel.fontMetrics().height() * min_content_lines,
        )
    card.contentLabel.setMinimumHeight(content_height)
    block_height = card.titleLabel.sizeHint().height() + content_height
    card.setFixedHeight(max(70 if card.contentLabel.text() else 50, block_height + 24))


def add_setting_card_actions(card: SettingCard, *widgets: QWidget, spacing: int = 8) -> None:
    """把一组控件依次挂到 ``SettingCard`` 右侧（右对齐，末尾补 16px 内边距）。"""
    for widget in widgets:
        card.hBoxLayout.addWidget(widget, 0, Qt.AlignmentFlag.AlignRight)
        card.hBoxLayout.addSpacing(spacing)
    if widgets:
        card.hBoxLayout.addSpacing(max(0, 16 - spacing))


def build_settings_tab_page(parent: QWidget, groups: list[SettingCardGroup]) -> FluentScrollArea:
    """把若干 ``SettingCardGroup`` 装进一个透明背景的纵向 Fluent 滚动页。"""
    page = FluentScrollArea(parent)
    page.setWidgetResizable(True)
    page.setFrameShape(QFrame.Shape.NoFrame)
    page.enableTransparentBackground()
    content = QWidget(page)
    layout = QVBoxLayout(content)
    layout.setContentsMargins(2, 8, 10, 4)
    layout.setSpacing(18)
    for group in groups:
        layout.addWidget(group)
    layout.addStretch(1)
    page.setWidget(content)
    return page


class UpdateSourceOrderDialog(ModelessDialog):
    """更新源优先级编辑弹窗（拖拽重排 + 上移 / 下移 / 恢复默认）。

    确定后从 :attr:`order` 读出最终顺序（已 ``normalize_order``）。
    """

    def __init__(self, current_order: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.order: list[str] = normalize_order(current_order)
        self.setWindowTitle("更新源优先级")
        self.setMinimumWidth(540)
        if parent is not None:
            self.setWindowIcon(parent.windowIcon())

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 20)
        root.setSpacing(12)

        title = TitleLabel("更新源优先级", self)
        root.addWidget(title)

        hint = BodyLabel(
            "按顺序尝试，前一项失败时自动降级到下一项。\n"
            "可以直接拖动条目，或选中后用右侧按钮微调。",
            self,
        )
        hint.setWordWrap(True)
        root.addWidget(hint)

        body = QWidget(self)
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 4, 0, 4)
        body_layout.setSpacing(12)

        self.order_list = FluentListWidget(body)
        self.order_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.order_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.order_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.order_list.setMinimumWidth(340)
        self.order_list.setMinimumHeight(170)
        for source in self.order:
            item = QListWidgetItem(SOURCE_LABELS.get(source, source))
            item.setData(Qt.ItemDataRole.UserRole, source)
            self.order_list.addItem(item)
        body_layout.addWidget(self.order_list, 1)

        button_column = QWidget(body)
        column_layout = QVBoxLayout(button_column)
        column_layout.setContentsMargins(0, 0, 0, 0)
        column_layout.setSpacing(8)
        self.move_up_button = QPushButton(FIF.UP, "上移", button_column)
        self.move_down_button = QPushButton(FIF.DOWN, "下移", button_column)
        self.reset_button = QPushButton("恢复默认", button_column)
        self.move_up_button.clicked.connect(lambda: self._move_selected(-1))
        self.move_down_button.clicked.connect(lambda: self._move_selected(1))
        self.reset_button.clicked.connect(self._reset_order)
        column_layout.addWidget(self.move_up_button)
        column_layout.addWidget(self.move_down_button)
        column_layout.addWidget(self.reset_button)
        column_layout.addStretch(1)
        body_layout.addWidget(button_column, 0)

        root.addWidget(body, 1)

        button_row = QHBoxLayout()
        button_row.setSpacing(12)
        self.yesButton = PrimaryPushButton("确定", self)
        self.cancelButton = QPushButton("取消", self)
        self.yesButton.clicked.connect(self.accept)
        self.cancelButton.clicked.connect(self.reject)
        button_row.addWidget(self.yesButton, 1)
        button_row.addWidget(self.cancelButton, 1)
        root.addLayout(button_row)

    def _selected_row(self) -> int:
        items = self.order_list.selectedItems()
        return self.order_list.row(items[0]) if items else -1

    def _move_selected(self, delta: int) -> None:
        row = self._selected_row()
        target = row + delta
        if row < 0 or target < 0 or target >= self.order_list.count():
            return
        item = self.order_list.takeItem(row)
        if item is None:
            return
        self.order_list.insertItem(target, item)
        self.order_list.setCurrentRow(target)

    def _reset_order(self) -> None:
        self.order_list.clear()
        for source in SOURCE_IDS:
            item = QListWidgetItem(SOURCE_LABELS.get(source, source))
            item.setData(Qt.ItemDataRole.UserRole, source)
            self.order_list.addItem(item)
        self.order_list.setCurrentRow(0)

    def accept(self) -> None:  # noqa: N802
        raw: list[str] = []
        for row in range(self.order_list.count()):
            item = self.order_list.item(row)
            if item is not None:
                raw.append(str(item.data(Qt.ItemDataRole.UserRole)))
        self.order = normalize_order(raw)
        super().accept()


class LyricsResultsDelegate(TableItemDelegate):
    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.margin = 1
        self.setCheckedColor("#D85C6C", "#D85C6C")

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:  # noqa: D401
        from krok_helper.theme_workbench import palette

        p = palette()
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)

        is_selected = index.row() in self.selectedRows or bool(option.state & QStyle.StateFlag.State_Selected)
        is_hovered = self.hoverRow == index.row() or bool(option.state & QStyle.StateFlag.State_MouseOver)
        background = ""
        if is_selected:
            background = p.preview_selection_bg
        elif is_hovered:
            background = p.table_row_hover

        text_brush = index.data(Qt.ItemDataRole.ForegroundRole)
        if is_selected:
            text_color = QColor(p.preview_selection_text)
        elif text_brush is not None:
            text_color = QBrush(text_brush).color()
        else:
            text_color = QColor(p.text_primary)
        opt.palette.setColor(QPalette.ColorRole.Text, text_color)
        opt.palette.setColor(QPalette.ColorRole.HighlightedText, text_color)
        opt.state &= ~QStyle.StateFlag.State_Selected
        opt.state &= ~QStyle.StateFlag.State_MouseOver

        if background:
            bg_rect = option.rect.adjusted(0, self.margin, 0, -self.margin)
            painter.save()
            painter.setPen(Qt.PenStyle.NoPen)
            painter.fillRect(bg_rect, QColor(background))
            if index.column() == 0:
                accent_height = max(4, bg_rect.height() - 10)
                painter.fillRect(bg_rect.left(), bg_rect.top() + 5, 3, accent_height, QColor(p.accent_primary))
            painter.restore()

        QStyledItemDelegate.paint(self, painter, opt, index)


@dataclass(frozen=True)
class WorkflowStepItem:
    module_id: str
    number: int
    title: str
    description: str
    implemented: bool


WORKFLOW_STEPS = [
    WorkflowStepItem(WORKFLOW_VIDEO_DOWNLOAD, 1, "视频下载", "下载在线视频", False),
    WorkflowStepItem(WORKFLOW_WAVEFORM_ALIGN, 2, "音视频处理", "波形对齐与音频分离", True),
    WorkflowStepItem(WORKFLOW_LYRICS_SEARCH, 3, "歌词检索", "搜索并获取歌词", True),
    WorkflowStepItem(WORKFLOW_LYRICS_TIMING, 4, "歌词打轴", "逐字 / 逐句打轴", False),
    WorkflowStepItem(WORKFLOW_SUBTITLE_RENDER, 5, "字幕视频生成", "渲染字幕样式", False),
    WorkflowStepItem(WORKFLOW_HIRES_MIX, 6, "Hi-Res 混流", "音视频混流导出", True),
]


class WorkflowStepButton(QWidget):
    clicked = Signal(int)

    def __init__(self, step: WorkflowStepItem, index: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.step = step
        self.index = index
        self._active = False
        self._hovered = False
        self._compact = False
        # 宿主 WorkflowStepper 置 True 后，活跃下划线由 stepper 的共享滑块绘制，
        # 本按钮内的静态 bottom_line 不再显示（避免双线）。
        self._shared_underline = False
        # 由宿主写入的瞬时状态文本（如打轴步骤的「当前 .sug 文件名 + 未保存」），
        # None 时回退到步骤的默认描述。
        self._status_text: str | None = None
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(60)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setObjectName("WorkflowStepItem")

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        self._content_layout = QHBoxLayout()
        self._content_layout.setContentsMargins(18, 10, 18, 8)
        self._content_layout.setSpacing(10)

        self.number_label = QLabel(str(step.number))
        self.number_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.number_label.setFixedSize(32, 32)
        self.number_label.setObjectName("WorkflowStepNumber")

        self._text_layout = QVBoxLayout()
        self._text_layout.setContentsMargins(0, 0, 0, 0)
        self._text_layout.setSpacing(1)

        self.title_label = QLabel(step.title)
        self.title_label.setObjectName("WorkflowStepTitle")
        self.desc_label = QLabel(step.description)
        self.desc_label.setObjectName("WorkflowStepDescription")
        self.desc_label.setWordWrap(False)
        self.bottom_line = QFrame(self)
        self.bottom_line.setObjectName("WorkflowStepUnderline")
        self.bottom_line.setFixedHeight(2)
        self.bottom_line.hide()

        self._text_layout.addWidget(self.title_label)
        self._text_layout.addWidget(self.desc_label)

        self._content_layout.addWidget(self.number_label, 0, Qt.AlignmentFlag.AlignVCenter)
        self._content_layout.addLayout(self._text_layout, 1)
        outer_layout.addLayout(self._content_layout)
        outer_layout.addWidget(self.bottom_line)
        self._refresh_style()
        # 跟随主题切换重刷颜色 —— 延迟到下个 event loop iter 避免与 SUG
        # ``_refresh_all_widgets`` 同步链上的 polish 操作重入（Win11 上
        # 与 Mica + qfluentwidgets lazy QSS 共同时序敏感）。
        from krok_helper.theme_workbench import schedule_theme_refresh, theme as _wb_theme
        _wb_theme.changed.connect(lambda: schedule_theme_refresh(self, self._refresh_style_safe))

    def _refresh_style_safe(self) -> None:
        try:
            self._refresh_style()
        except RuntimeError:
            pass

    def setActive(self, active: bool) -> None:
        if self._active == active:
            return
        self._active = active
        self._refresh_style()

    def setSharedUnderline(self, shared: bool) -> None:
        """开关共享滑动下划线模式（由 WorkflowStepper 注入）。"""
        if self._shared_underline == shared:
            return
        self._shared_underline = shared
        self._refresh_style()

    def set_status_text(self, text: str | None) -> None:
        """展示瞬时状态（如当前 .sug 文件名 / 未保存）。

        ``None`` 或空串恢复步骤的默认描述。非紧凑模式显示在描述行；紧凑模式
        描述行被隐藏，故并入标题行展示，保证收紧后状态依然可见。
        """
        self._status_text = text or None
        self._render_text()

    def _render_text(self) -> None:
        """根据紧凑态 + 状态文本决定标题/描述两行的内容与可见性。

        - 非紧凑：标题=步骤名；描述=状态文本（无则默认描述），可见。
        - 紧凑：仅一行（编号+标题），描述行隐藏，状态并入标题行
          （``步骤名 · 状态``），无状态时回到纯步骤名。
        """
        status = self._status_text
        if self._compact:
            self.desc_label.hide()
            self.title_label.setText(
                f"{self.step.title} · {status}" if status else self.step.title
            )
        else:
            self.title_label.setText(self.step.title)
            self.desc_label.setText(status or self.step.description)
            self.desc_label.show()

    def setCompact(self, compact: bool) -> None:
        if self._compact == compact:
            return
        self._compact = compact
        self._apply_compact_layout()
        self._refresh_style()

    def _apply_compact_layout(self) -> None:
        # 紧凑模式：编号 + 标题保留 → 整条像 ①视频下载 ②波形对齐 …；副标题（含
        # 状态）则由 _render_text 决定——非紧凑显示在描述行，紧凑并入标题行。
        if self._compact:
            self.setFixedHeight(32)
            self._content_layout.setContentsMargins(10, 2, 10, 2)
            self._content_layout.setSpacing(6)
            self.number_label.setFixedSize(22, 22)
        else:
            self.setFixedHeight(60)
            self._content_layout.setContentsMargins(18, 10, 18, 8)
            self._content_layout.setSpacing(10)
            self.number_label.setFixedSize(32, 32)
        self.title_label.setVisible(True)
        self._render_text()

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hovered = True
        self._refresh_style()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hovered = False
        self._refresh_style()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit(self.index)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _refresh_style(self) -> None:
        from krok_helper.theme_workbench import palette
        p = palette()
        # 工作流步骤色板 —— light/dark 各一套，状态分 active/hover/idle
        if p.is_dark:
            _active_bg     = "#3A2A2C"
            _active_title  = "#FFB3BE"
            _active_desc   = "#C58A92"
            _hover_bg      = "#2A2A2A"
            _idle_bg       = "transparent"
            _idle_title    = p.text_primary
            _idle_desc     = p.text_hint
            _num_bg        = "#2D2D2D"
            _num_border    = "#3E3E3E"
            _num_text      = p.text_secondary
        else:
            _active_bg     = "#FFF6F7"
            _active_title  = "#BC495A"
            _active_desc   = "#8F5B64"
            _hover_bg      = "#F6F8FB"
            _idle_bg       = "transparent"
            _idle_title    = "#1F2937"
            _idle_desc     = "#64748B"
            _num_bg        = "#FFFFFF"
            _num_border    = "#CBD5E1"
            _num_text      = "#64748B"

        if self._active:
            background = _active_bg
            title_color = _active_title
            desc_color = _active_desc
            number_background = p.accent_search
            number_color = "#FFFFFF"
            number_border = p.accent_search
        elif self._hovered:
            background = _hover_bg
            title_color = _idle_title
            desc_color = _idle_desc
            number_background = _num_bg
            number_color = _num_text
            number_border = _num_border
        else:
            background = _idle_bg
            title_color = _idle_title
            desc_color = _idle_desc
            number_background = _num_bg
            number_color = _num_text
            number_border = _num_border

        # 紧凑模式下编号圆点缩到 22px（对应 radius 11、字号 11），同时把活跃步标题字号收一档
        number_radius = 11 if self._compact else 16
        number_font_size = 11 if self._compact else 12
        title_font_size = 13 if self._compact else 14

        self.setStyleSheet(
            f"""
            QWidget#WorkflowStepItem {{
                background: {background};
                border: 0;
                border-radius: 10px;
            }}
            QLabel#WorkflowStepNumber {{
                background: {number_background};
                border: 1px solid {number_border};
                border-radius: {number_radius}px;
                color: {number_color};
                font-size: {number_font_size}px;
                font-weight: 700;
            }}
            QLabel#WorkflowStepTitle {{
                color: {title_color};
                font-size: {title_font_size}px;
                font-weight: 700;
            }}
            QLabel#WorkflowStepDescription {{
                color: {desc_color};
                font-size: 11px;
            }}
            QFrame#WorkflowStepUnderline {{
                background: {p.accent_search};
                border: 0;
                border-radius: 1px;
            }}
            """
        )
        # 紧凑模式下不显示底部下划线，避免活跃步的下划线把窄行撑出 2px 错位；
        # 共享滑块模式下静态线让位给 stepper 的滑动下划线
        self.bottom_line.setVisible(self._active and not self._compact and not self._shared_underline)


class WorkflowStepper(QWidget):
    currentChanged = Signal(int)
    stepClicked = Signal(int)

    def __init__(self, steps: list[WorkflowStepItem], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._steps = steps
        self._items: list[WorkflowStepButton] = []
        self._separators: list[QLabel] = []
        self._current_index = 0
        self._compact = False
        self.setObjectName("WorkflowStepper")

        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)

        for index, _step in enumerate(steps):
            item = self.createStepItem(index)
            self._items.append(item)
            self._layout.addWidget(item, 1)
            if index < len(steps) - 1:
                separator = QLabel("›")
                separator.setAlignment(Qt.AlignmentFlag.AlignCenter)
                from krok_helper.theme_workbench import palette as _wb_palette, themed as _wb_themed
                # 用闭包 + self 引用，避免主题切换覆盖紧凑模式下缩小的字号
                _wb_themed(
                    separator,
                    lambda _s=self: f"color: {_wb_palette().text_disabled}; "
                    f"font-size: {12 if _s._compact else 18}px; font-weight: 500;",
                )
                separator.setFixedWidth(24)
                self._layout.addWidget(separator, 0, Qt.AlignmentFlag.AlignVCenter)
                self._separators.append(separator)

        # 共享滑动下划线：替代各按钮内部的静态下划线，切换步骤时在按钮间平滑滑动。
        # 浮在 stepper 上绘制，不拦截鼠标；几何位置与按钮 bottom_line 完全一致。
        self._underline_anim: QPropertyAnimation | None = None
        self._underline = QFrame(self)
        self._underline.setObjectName("WorkflowStepUnderlineSlider")
        self._underline.setFixedHeight(2)
        self._underline.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        from krok_helper.theme_workbench import palette as _wb_palette, themed as _wb_themed
        _wb_themed(
            self._underline,
            lambda: f"background: {_wb_palette().accent_search}; border: 0; border-radius: 1px;",
        )
        self._underline.hide()
        for item in self._items:
            item.setSharedUnderline(True)

        self.updateStepStyles()

    def createStepItem(self, index: int) -> WorkflowStepButton:
        item = WorkflowStepButton(self._steps[index], index, self)
        item.clicked.connect(self._handleStepClicked)
        return item

    def currentIndex(self) -> int:
        return self._current_index

    def setCurrentIndex(self, index: int) -> None:
        if index < 0 or index >= len(self._steps):
            return
        if self._current_index == index:
            self.updateStepStyles()
            return
        previous_index = self._current_index
        self._current_index = index
        self.updateStepStyles()
        self._slide_underline(previous_index, index)
        self.currentChanged.emit(index)

    def _underline_rect(self, index: int) -> QRect:
        """第 ``index`` 个按钮对应的下划线矩形（stepper 自身坐标系）。"""
        geo = self._items[index].geometry()
        return QRect(geo.x(), geo.y() + geo.height() - 2, geo.width(), 2)

    def _stop_underline_anim(self) -> None:
        anim, self._underline_anim = self._underline_anim, None
        if anim is not None:
            try:
                anim.stop()
            except RuntimeError:
                pass

    def _snap_underline(self) -> None:
        """无动画地把滑动下划线贴到当前步骤（布局未就绪时先隐藏）。"""
        try:
            if self._compact:
                self._underline.hide()
                return
            target = self._underline_rect(self._current_index)
            if target.width() <= 0:
                return
            self._underline.setGeometry(target)
            self._underline.show()
        except RuntimeError:
            pass

    def _slide_underline(self, from_index: int, to_index: int) -> None:
        """切换步骤时让下划线从旧按钮平滑滑到新按钮。"""
        try:
            if self._compact:
                self._underline.hide()
                return
            target = self._underline_rect(to_index)
            if target.width() <= 0:
                # 布局尚未排布（如构造期），等 showEvent/resizeEvent 再 snap
                self._underline.hide()
                return
            start = (
                self._underline.geometry()
                if self._underline.isVisible() and self._underline.geometry().width() > 0
                else self._underline_rect(from_index)
            )
            if start.width() <= 0:
                start = target
            self._stop_underline_anim()
            if start == target or not self.isVisible():
                self._underline.setGeometry(target)
                self._underline.show()
                return
            self._underline.setGeometry(start)
            self._underline.show()
            anim = QPropertyAnimation(self._underline, b"geometry", self)
            anim.setDuration(260)
            anim.setStartValue(start)
            anim.setEndValue(target)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            # 动画期间若按钮几何因 stylesheet polish 等原因微调，收尾时贴准最终位置
            anim.finished.connect(self._snap_underline)
            anim.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
            self._underline_anim = anim
        except RuntimeError:
            pass

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # 等布局排完后把下划线贴到当前步骤（构造期按钮几何还是 0 宽）
        QTimer.singleShot(0, self._snap_underline)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        # 窗口拉伸改变按钮几何：停掉进行中的滑动，直接贴到新位置
        self._stop_underline_anim()
        self._snap_underline()

    def setCurrentModule(self, module_id: str) -> None:
        for index, step in enumerate(self._steps):
            if step.module_id == module_id:
                self.setCurrentIndex(index)
                return

    def moduleIdAt(self, index: int) -> str:
        return self._steps[index].module_id

    def setStepStatus(self, module_id: str, text: str | None) -> None:
        """把某一步的描述行替换为瞬时状态文本（None 恢复默认描述）。"""
        for index, step in enumerate(self._steps):
            if step.module_id == module_id:
                self._items[index].set_status_text(text)
                return

    def updateStepStyles(self) -> None:
        for index, item in enumerate(self._items):
            item.setActive(index == self._current_index)

    def updateStyles(self) -> None:
        self.updateStepStyles()

    def isCompact(self) -> bool:
        return self._compact

    def setCompact(self, compact: bool) -> None:
        if self._compact == compact:
            return
        self._compact = compact
        for item in self._items:
            item.setCompact(compact)
        # 分隔符 › 的字号在它的 themed 闭包里读 self._compact，这里只要重跑 QSS
        from krok_helper.theme_workbench import palette as _wb_palette
        sep_width = 12 if compact else 24
        for sep in self._separators:
            sep.setFixedWidth(sep_width)
            sep.setStyleSheet(
                f"color: {_wb_palette().text_disabled}; "
                f"font-size: {12 if compact else 18}px; font-weight: 500;"
            )
        # 滑动下划线遵循原按钮下划线语义：紧凑模式隐藏；展开时等布局排完后贴回
        self._stop_underline_anim()
        if compact:
            self._underline.hide()
        else:
            QTimer.singleShot(0, self._snap_underline)

    def _handleStepClicked(self, index: int) -> None:
        self.stepClicked.emit(index)


class CornerBadge(QLabel):
    """卡片右上角的小角标；左右键分别发不同的信号。"""

    clicked = Signal()
    rightClicked = Signal()

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        if event.button() == Qt.MouseButton.RightButton:
            self.rightClicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        # 右键已经用来翻上一条了，别再弹系统菜单。
        event.accept()


class CardFlipOverlay(QWidget):
    """卡片翻页时的翻转覆盖层。

    把切换前的样子拍成位图，横向压扁到 0 再展开成新的一张，看起来像卡片翻了个面。
    覆盖层自己铺满整张卡并填上卡片底色，压住下面真实的控件——否则位图缩窄时会露出
    底下没变的内容，动画就白做了。
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._pixmap = QPixmap()
        self._scale = 1.0
        self._background = QColor("#ffffff")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.hide()

    def prepare(self, pixmap: QPixmap, background: QColor) -> None:
        self._pixmap = pixmap
        self._background = background

    def _get_scale(self) -> float:
        return self._scale

    def _set_scale(self, value: float) -> None:
        self._scale = float(value)
        self.update()

    scale = pyqtProperty(float, fget=_get_scale, fset=_set_scale)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), self._background)
        if self._pixmap.isNull() or self._scale <= 0.001:
            return
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        ratio = self._pixmap.devicePixelRatio() or 1.0
        width = self._pixmap.width() / ratio
        height = self._pixmap.height() / ratio
        painter.translate(self.width() / 2.0, 0.0)
        painter.scale(max(0.0, self._scale), 1.0)
        painter.drawPixmap(QRectF(-width / 2.0, 0.0, width, height), self._pixmap,
                           QRectF(0, 0, self._pixmap.width(), self._pixmap.height()))


class DropZoneCard(CardWidget):
    pathChanged = Signal(Path)
    #: 多文件模式下条目增删（翻页不发）。
    pathsChanged = Signal()
    browseRequested = Signal()

    def __init__(
        self,
        *,
        title: str,
        hint: str,
        extensions: set[str],
        min_height: int = 220,
        icon_text: str = "",
        placeholder_icon: str = "",
        accent_bg: str = "#f6f8fb",
        multiple: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.extensions = {ext.lower() for ext in extensions}
        self.accent_bg = accent_bg
        #: 唯一真相；单文件模式下最多一个元素，``path`` 只是它的视图。
        self.paths: list[Path] = []
        self.multiple = multiple
        self._index = 0
        self._hovered = False
        self._drag_state = "idle"
        self._default_action_text = "点击选择文件，或直接拖进这个区域"

        self.setObjectName("DropZoneCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAcceptDrops(True)
        self.setMinimumHeight(min_height)

        layout = self.createVBoxLayout()
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)

        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(8)

        self.icon_label = QLabel(icon_text)
        self.icon_label.setObjectName("DropZoneIcon")
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.icon_label.setVisible(bool(icon_text))

        self.title_label = QLabel(title)
        self.title_label.setObjectName("DropZoneTitle")
        self.title_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        title_font = QFont("Microsoft YaHei UI", 12)
        title_font.setBold(True)
        apply_safe_label_metrics(self.title_label, title_font)
        title_row.addWidget(self.icon_label, 0, Qt.AlignmentFlag.AlignVCenter)
        title_row.addWidget(self.title_label, 1, Qt.AlignmentFlag.AlignVCenter)

        self._base_hint = hint
        self.hint_label = QLabel(hint)
        self.hint_label.setObjectName("DropZoneHint")
        self.hint_label.setWordWrap(True)
        self.hint_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self.placeholder_label = QLabel(placeholder_icon)
        self.placeholder_label.setObjectName("DropZonePlaceholder")
        self.placeholder_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self.path_label = QLabel("未选择文件")
        self.path_label.setObjectName("DropZonePath")
        self.path_label.setWordWrap(True)
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.path_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self.action_label = QLabel(self._default_action_text)
        self.action_label.setObjectName("DropZoneAction")
        self.action_label.setWordWrap(True)
        self.action_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self._status_badge = QLabel("✓", self)
        self._status_badge.setObjectName("DropZoneStatusBadge")
        self._status_badge.setFixedSize(22, 22)
        self._status_badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._status_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_badge.setStyleSheet(
            """
            QLabel#DropZoneStatusBadge {
                background: #10B981;
                color: white;
                border-radius: 11px;
                font-size: 13pt;
                font-weight: 700;
                qproperty-alignment: AlignCenter;
            }
            """
        )
        self._status_badge.hide()

        # ── 多文件：序号角标 + 移除按钮 ───────────────────────────
        self._page_badge = CornerBadge("1 / 1", self)
        self._page_badge.setObjectName("DropZonePageBadge")
        self._page_badge.setFixedHeight(22)
        self._page_badge.setMinimumWidth(46)
        self._page_badge.clicked.connect(self.show_next)
        self._page_badge.rightClicked.connect(self.show_previous)
        self._page_badge.hide()

        self._remove_badge = CornerBadge("✕", self)
        self._remove_badge.setObjectName("DropZoneRemoveBadge")
        self._remove_badge.setFixedSize(22, 22)
        self._remove_badge.setToolTip("移除当前这条")
        self._remove_badge.clicked.connect(self._on_remove_clicked)
        self._remove_badge.hide()

        self._flip_overlay = CardFlipOverlay(self)
        self._flip_anim: QPropertyAnimation | None = None

        layout.addLayout(title_row)
        layout.addWidget(self.hint_label)
        layout.addStretch(1)
        layout.addWidget(self.placeholder_label, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.path_label)
        layout.addWidget(self.action_label)
        self._refresh_style()
        # 跟随主题切换重刷颜色（延迟到下个 event loop iter，参见
        # WorkflowStepButton 同名说明）。
        from krok_helper.theme_workbench import schedule_theme_refresh, theme as _wb_theme
        _wb_theme.changed.connect(lambda: schedule_theme_refresh(self, self._refresh_style_safe))

    def _refresh_style_safe(self) -> None:
        try:
            self._refresh_style()
        except RuntimeError:
            pass

    def _current_background(self) -> str:
        return getattr(self, "_background_color", "#FFFFFF")

    @property
    def path(self) -> Path | None:
        """当前显示的那条；多文件模式下随序号变化。"""
        if not self.paths:
            return None
        return self.paths[min(self._index, len(self.paths) - 1)]

    def accepts(self, path: Path) -> bool:
        return path.is_file() and path.suffix.lower() in self.extensions

    def set_path(self, path: Path) -> None:
        """设为唯一一条（多文件模式下等于清空后重放）。"""
        self.paths = [path]
        self._index = 0
        self._drag_state = "idle"
        self._refresh_paths_ui()

    def add_paths(self, paths: Sequence[Path]) -> list[Path]:
        """追加若干条并跳到第一条新加入的；返回真正加进去的（去重后）。"""
        if not self.multiple:
            if paths:
                self.set_path(Path(paths[-1]))
                return [Path(paths[-1])]
            return []
        existing = {str(item).lower() for item in self.paths}
        added = [Path(item) for item in paths if str(item).lower() not in existing]
        if not added:
            return []
        first_new = len(self.paths)
        self.paths.extend(added)
        self._drag_state = "idle"
        self._go_to(first_new, animate=bool(first_new))
        return added

    def remove_current(self) -> None:
        """移除当前显示的这条，停在原位（即自动落到下一条）。"""
        if not self.paths:
            return
        index = min(self._index, len(self.paths) - 1)
        self.paths.pop(index)
        self._index = min(index, max(0, len(self.paths) - 1))
        self._refresh_paths_ui()

    def clear_path(self) -> None:
        self.paths = []
        self._index = 0
        self._drag_state = "idle"
        self._refresh_paths_ui()

    # ── 多文件翻页 ────────────────────────────────────────────────
    def _go_to(self, index: int, *, animate: bool = True) -> None:
        if not self.paths:
            return
        index %= len(self.paths)
        if index == self._index:
            self._refresh_paths_ui()
            return
        if not animate or not self.isVisible():
            self._index = index
            self._refresh_paths_ui()
            return
        self._flip_to(index)

    def show_next(self) -> None:
        self._go_to(self._index + 1)

    def show_previous(self) -> None:
        self._go_to(self._index - 1)

    def _refresh_paths_ui(self) -> None:
        current = self.path
        self.path_label.setText(str(current) if current is not None else "未选择文件")
        total = len(self.paths)
        show_pager = self.multiple and total > 1
        self._page_badge.setVisible(show_pager)
        if show_pager:
            self._page_badge.setText(f"{min(self._index, total - 1) + 1} / {total}")
            self._page_badge.setToolTip("左键下一条，右键上一条")
        self._remove_badge.setVisible(self.multiple and total > 0)
        if self.multiple and total > 1:
            self.hint_label.setText(f"已放入 {total} 个伴奏音频，将各生成一个混流视频。")
        else:
            self.hint_label.setText(self._base_hint)
        self._refresh_style()
        self._position_status_badge()

    def _flip_to(self, index: int) -> None:
        anim, self._flip_anim = self._flip_anim, None
        try:
            # 动画用的是 DeleteWhenStopped，跑完 C++ 对象就没了；这里可能拿到一个
            # 已经析构的壳子（连点翻页就会撞上），所以要兜住 RuntimeError。
            if anim is not None and anim.state() == QAbstractAnimation.State.Running:
                anim.stop()
        except RuntimeError:
            pass

        overlay = self._flip_overlay
        overlay.setGeometry(self.rect())
        overlay.prepare(self.grab(), QColor(self._current_background()))
        overlay.show()
        overlay.raise_()

        forward = QPropertyAnimation(overlay, b"scale", self)
        forward.setDuration(110)
        forward.setStartValue(1.0)
        forward.setEndValue(0.0)
        forward.setEasingCurve(QEasingCurve.Type.InCubic)

        def _swap() -> None:
            self._index = index
            self._refresh_paths_ui()
            overlay.hide()  # 抓图时别把覆盖层自己也抓进去
            overlay.prepare(self.grab(), QColor(self._current_background()))
            overlay.show()
            overlay.raise_()
            back = QPropertyAnimation(overlay, b"scale", self)
            back.setDuration(130)
            back.setStartValue(0.0)
            back.setEndValue(1.0)
            back.setEasingCurve(QEasingCurve.Type.OutCubic)

            def _done() -> None:
                overlay.hide()
                self._flip_anim = None  # 别留下指向已析构对象的引用

            back.finished.connect(_done)
            self._flip_anim = back
            back.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)

        forward.finished.connect(_swap)
        self._flip_anim = forward
        forward.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hovered = True
        self._refresh_style()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hovered = False
        self._refresh_style()
        super().leaveEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._position_status_badge()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.browseRequested.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def _accepted_drops(self, mime) -> list[Path]:
        """拖进来的东西里挑出能收的；单文件模式只取第一个。"""
        paths = [Path(url.toLocalFile()).expanduser() for url in mime.urls()]
        usable = [path for path in paths if str(path) and self.accepts(path)]
        if not self.multiple:
            return usable[:1]
        return usable

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if self._accepted_drops(event.mimeData()):
            self._drag_state = "accept"
            self._refresh_style()
            event.acceptProposedAction()
            return
        self._drag_state = "reject"
        self._refresh_style()
        event.ignore()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self._drag_state = "idle"
        self._refresh_style()
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        accepted = self._accepted_drops(event.mimeData())
        self._drag_state = "idle"
        if not accepted:
            self._refresh_style()
            event.ignore()
            return
        added = self.add_paths(accepted)
        event.acceptProposedAction()
        if added:
            self.pathChanged.emit(added[0])

    def _on_remove_clicked(self) -> None:
        self.remove_current()
        self.pathsChanged.emit()

    def _position_status_badge(self) -> None:
        right = max(0, self.width() - 32)
        self._status_badge.move(right, 10)
        self._status_badge.raise_()
        # 角标从右往左排：✓ 之后是序号，再往左是移除按钮。
        cursor = right
        if self._status_badge.isVisibleTo(self):
            cursor -= 6
        if self._page_badge.isVisibleTo(self):
            cursor -= self._page_badge.width()
            self._page_badge.move(max(0, cursor), 10)
            self._page_badge.raise_()
            cursor -= 6
        if self._remove_badge.isVisibleTo(self):
            cursor -= self._remove_badge.width()
            self._remove_badge.move(max(0, cursor), 10)
            self._remove_badge.raise_()

    def _refresh_style(self) -> None:
        from krok_helper.theme_workbench import palette
        p = palette()
        selected = bool(getattr(self, "_path", None) or self.path)
        border_width = "1.5"
        border_style = "dashed"
        # 拖拽/选中/idle 各态色板：light/dark 各一套
        if p.is_dark:
            _accept_bg, _accept_border, _accept_accent = "#1F2C40", "#5B9DFF", "#A6C8FF"
            _reject_bg, _reject_border, _reject_accent = "#3A1A1A", "#EF5A5A", "#FF9C9C"
            _hover_bg, _idle_bg = p.input_bg, p.card_bg
            _hover_border, _hover_accent = "#5B9DFF", "#5B9DFF"
            _selected_bg, _selected_border, _selected_accent = p.card_bg, "#3DB37D", "#6FE3A4"
            _idle_border, _idle_accent = "#3E3E3E", "#5B9DFF"
            _title_color, _hint_color, _placeholder_color, _path_color = (
                p.text_primary, p.text_secondary, "#525252", p.text_primary,
            )
        else:
            _accept_bg, _accept_border, _accept_accent = "#dbeafe", "#2f6fed", "#1d4ed8"
            _reject_bg, _reject_border, _reject_accent = "#fef2f2", "#ef4444", "#b91c1c"
            _hover_bg, _idle_bg = self.accent_bg, self.accent_bg
            _hover_border, _hover_accent = "#2f6fed", "#2f6fed"
            _selected_bg, _selected_border, _selected_accent = "#FFFFFF", "#10B981", "#177245"
            _idle_border, _idle_accent = "#C2CAD8", "#2f6fed"
            _title_color, _hint_color, _placeholder_color, _path_color = (
                "#1f2937", "#5b6677", "#C2CAD8", "#111827",
            )

        if self._drag_state == "accept":
            background = _accept_bg
            border = _accept_border
            accent = _accept_accent
            border_width = "2"
            border_style = "solid"
            action_text = "松开鼠标即可导入这个文件"
        elif self._drag_state == "reject":
            background = _reject_bg
            border = _reject_border
            accent = _reject_accent
            border_width = "2"
            border_style = "solid"
            action_text = "这个文件类型不支持，请换一个文件"
        elif self._hovered:
            background = _hover_bg
            border = _hover_border
            accent = _hover_accent
            border_width = "2"
            border_style = "solid"
            action_text = self._default_action_text
        elif selected:
            background = _selected_bg
            border = _selected_border
            accent = _selected_accent
            border_style = "solid"
            action_text = self._default_action_text
        else:
            background = _idle_bg
            border = _idle_border
            accent = _idle_accent
            action_text = self._default_action_text

        self.action_label.setText(action_text)
        self.placeholder_label.setVisible(self.path is None and bool(self.placeholder_label.text()))
        self._status_badge.setVisible(selected)
        # 翻转覆盖层要用卡片当前底色铺满，才能压住下面没变的内容。
        self._background_color = background
        total = len(self.paths)
        self._page_badge.setVisible(self.multiple and total > 1)
        self._remove_badge.setVisible(self.multiple and total > 0)
        self._page_badge.setStyleSheet(
            f"""
            QLabel#DropZonePageBadge {{
                background: {accent};
                color: white;
                border-radius: 11px;
                padding: 0 8px;
                font-family: "Microsoft YaHei UI";
                font-size: 9.5pt;
                font-weight: 700;
            }}
            """
        )
        self._remove_badge.setStyleSheet(
            f"""
            QLabel#DropZoneRemoveBadge {{
                background: transparent;
                border: 1px solid {border};
                border-radius: 11px;
                color: {_hint_color};
                font-size: 10pt;
                font-weight: 700;
            }}
            QLabel#DropZoneRemoveBadge:hover {{
                background: {_reject_bg};
                border-color: {_reject_border};
                color: {_reject_accent};
            }}
            """
        )

        self.setStyleSheet(
            f"""
            QFrame#DropZoneCard {{
                background: {background};
                border: {border_width}px {border_style} {border};
                border-radius: 10px;
            }}
            QLabel#DropZoneIcon {{
                background: transparent;
                border: 0;
                font-size: 16pt;
            }}
            QLabel#DropZoneTitle {{
                background: transparent;
                border: 0;
                color: {_title_color};
                font-family: "Microsoft YaHei UI";
                font-size: 12pt;
                font-weight: 700;
            }}
            QLabel#DropZoneHint {{
                background: transparent;
                border: 0;
                color: {_hint_color};
                font-family: "Microsoft YaHei UI";
                font-size: 10pt;
            }}
            QLabel#DropZonePlaceholder {{
                background: transparent;
                border: 0;
                color: {_placeholder_color};
                font-family: "Microsoft YaHei UI";
                font-size: 48px;
            }}
            QLabel#DropZonePath {{
                background: transparent;
                border: 0;
                color: {_path_color};
                font-family: "Consolas";
                font-size: 10pt;
            }}
            QLabel#DropZoneAction {{
                background: transparent;
                border: 0;
                color: {accent};
                font-family: "Microsoft YaHei UI";
                font-size: 10pt;
                font-weight: 700;
            }}
            """
        )
        self._position_status_badge()


class BackgroundTask(QThread):
    log_message = Signal(str)
    task_succeeded = Signal(object)
    task_failed = Signal(str)

    def __init__(self, runner: Callable[[Callable[[str], None]], object]) -> None:
        super().__init__()
        self._runner = runner
        self._task_name = getattr(runner, "__qualname__", getattr(runner, "__name__", "unknown"))

    def run(self) -> None:  # noqa: D401
        task_log = logging.getLogger("krok_helper.background_task")
        task_log.info("后台任务开始: %s", self._task_name)

        def emit_log(message: str) -> None:
            task_log.info("%s: %s", self._task_name, message)
            self.log_message.emit(message)

        try:
            result = self._runner(emit_log)
        except Exception as exc:  # noqa: BLE001
            task_log.exception("后台任务失败: %s", self._task_name)
            self.task_failed.emit(str(exc))
            return
        task_log.info("后台任务完成: %s", self._task_name)
        self.task_succeeded.emit(result)


class LyricsKeywordLineEdit(QLineEdit):
    """QLineEdit that accepts a dropped lyrics file and fills in a search keyword.

    QLineEdit 默认会把拖入的 file:// URL 当文本插入；我们拦下来改成「从文件提取
    歌曲名 → 替换输入内容 → 选中文本，方便用户回车搜索或继续编辑」。
    """

    fileDropped = Signal(str)  # 文件内提取出的关键词

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)

    @staticmethod
    def _first_local_file(mime) -> Path | None:
        if not mime.hasUrls():
            return None
        for url in mime.urls():
            local = url.toLocalFile()
            if not local:
                continue
            p = Path(local)
            if p.is_file():
                return p
        return None

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if self._first_local_file(event.mimeData()) is not None:
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if self._first_local_file(event.mimeData()) is not None:
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        path = self._first_local_file(event.mimeData())
        if path is None:
            super().dropEvent(event)
            return
        try:
            query = extract_lyrics_query_from_file(path)
        except Exception:
            query = path.stem
        if query:
            self.setText(query)
            self.selectAll()
            self.setFocus()
            self.fileDropped.emit(query)
        event.acceptProposedAction()


class PageTransitionOverlay(QWidget):
    """工作流换页动画的快照覆盖层。

    动画期间只把两张已光栅化的位图按偏移贴出来，完全不碰真实页面 —— 真实
    的 ``setCurrentWidget`` 在动画开始前就做完了，页面已排好版并被本覆盖层
    遮住。这样每帧的代价固定为两次 blit，与页面复杂度无关。

    对比直接动 ``page.pos``：那种做法每帧都要把入场页整棵子树重绘一遍（页面
    是 native HWND，拿不到 backingstore 的 blit 快路径），帧间隔实测等于该页
    一次全量重绘的耗时，重页会掉到 30fps 上下。
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._old_pixmap = QPixmap()
        self._new_pixmap = QPixmap()
        self._distance = 0
        self._offset = 0.0
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        # page_stack 下的各页都是 native HWND（SUG 侧的 winId() 把整条祖先链连同
        # 重叠的兄弟页一起提升了），native 窗口恒在非 native 兄弟之上合成。覆盖层
        # 自己也必须是 native，raise_() 才真的能压住页面。
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.hide()

    def start(self, old_pixmap: QPixmap, new_pixmap: QPixmap, distance: int) -> None:
        self._old_pixmap = old_pixmap
        self._new_pixmap = new_pixmap
        self._distance = distance
        self._offset = float(distance)

    def _get_offset(self) -> float:
        return self._offset

    def _set_offset(self, value: float) -> None:
        self._offset = float(value)
        self.update()

    offset = pyqtProperty(float, fget=_get_offset, fset=_set_offset)

    def paintEvent(self, event) -> None:  # noqa: N802
        from krok_helper.theme_workbench import palette as _wb_palette

        painter = QPainter(self)
        # 换页同时改边距时（打轴/字幕预览走贴边边距），新旧两张快照宽度会差 48px，
        # 先铺一层底避免露出未定义像素 —— WA_OpaquePaintEvent 承诺画满每个像素。
        painter.fillRect(self.rect(), QColor(_wb_palette().shell_bg))
        painter.drawPixmap(int(self._offset) - self._distance, 0, self._old_pixmap)
        painter.drawPixmap(int(self._offset), 0, self._new_pixmap)


class KrokHelperQtApp(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.settings = load_app_settings()
        # 取走「上次 load 是否检测到 settings.json 损坏」的状态。要在 super().__init__
        # 之后、`self.show()` 之前留住；真正弹窗在主窗口显示后再触发，避免和 splash
        # / 初始化对话框打架。
        self._settings_corruption_backup: Path | None = consume_corruption_backup()
        lyrics_timing_migrated = self.settings.lyrics_timing_migrated_v1
        if migrate_strange_uta_game_settings(self.settings) or (
            self.settings.lyrics_timing_migrated_v1 != lyrics_timing_migrated
        ):
            save_app_settings(self.settings)
        self.hires_task: BackgroundTask | None = None
        self.lyrics_search_task: BackgroundTask | None = None
        self.lyrics_fetch_task: BackgroundTask | None = None
        self.align_analysis_task: BackgroundTask | None = None
        self.align_auto_task: BackgroundTask | None = None
        self.align_export_task: BackgroundTask | None = None
        self._update_checker: UpdateChecker | None = None
        self._update_launch_worker = None
        self._update_progress_win = None
        self._force_quitting_for_update = False
        self._update_exit_prepared = False
        self.lyrics_search_service = LyricsSearchService()
        self.lyrics_search_results: list[LyricsSearchCandidate] = []
        self.lyrics_pending_results: list[LyricsSearchCandidate] = []
        self.lyrics_selected_candidate: LyricsSearchCandidate | None = None
        self.lyrics_search_keyword = ""
        self.lyrics_search_provider_ids: tuple[str, ...] = DEFAULT_LYRICS_PROVIDER_IDS
        self.lyrics_next_provider_pages: dict[str, int] = {}
        self.lyrics_has_more_results = False
        self._lyrics_loading_more = False
        self._lyrics_loading_key = ""
        self.align_preview_process = None
        self.align_preview_started_at = 0.0
        self.align_preview_start_seconds = 0.0
        self._hires_cancel_requested = False
        self._hires_process: subprocess.Popen | None = None
        self._hires_expected_outputs: list[Path] = []
        self._hires_completed_outputs: list[Path] = []
        self._hires_preexisting_outputs: set[Path] = set()
        self._align_export_cancel_requested = False
        self._align_export_process: subprocess.Popen | None = None
        self._align_export_expected_outputs: list[Path] = []
        self._align_export_completed_outputs: list[Path] = []
        self._align_export_handoff_context: tuple[bool, Path, Path, str] | None = None
        self._alignment_handoff_dialog: AlignmentHandoffDialog | None = None
        self._alignment_handoff_payload: tuple[bool, Path, Path | None, Path | None] | None = None
        self.active_module = WORKFLOW_VIDEO_DOWNLOAD
        self._loading_settings_into_ui = True

        self.output_name_mode_value = OUTPUT_NAME_MODE_FIXED
        self.on_name_template_value = DEFAULT_ON_NAME_TEMPLATE
        self.off_name_template_value = DEFAULT_OFF_NAME_TEMPLATE
        self.align_video_name_template_value = DEFAULT_ALIGNED_VIDEO_NAME_TEMPLATE
        self.align_audio_name_template_value = DEFAULT_ALIGNED_AUDIO_NAME_TEMPLATE
        self.align_output_dir_mode_value = ALIGN_OUTPUT_DIR_SOURCE_VIDEO
        self.align_output_custom_dir_text = ""
        self.ffmpeg_dir_text = ""
        self._align_lead_fill_selection = LEAD_FILL_BLACK
        self._align_encode_selection = (
            self.settings.align_encode_mode
            if self.settings.align_encode_mode in {ENCODE_MODE_SOFTWARE, ENCODE_MODE_HARDWARE}
            else ENCODE_MODE_SOFTWARE
        )
        self._media_duration_cache: dict[Path, str] = {}
        self._suppress_preview_seek_restart = False
        self._restoring_from_maximized = False
        self._startup_geometry_applied = False
        self._page_transition_overlay: PageTransitionOverlay | None = None
        self._page_switch_anim: QPropertyAnimation | None = None
        self.align_control_panel: QFrame | None = None
        self.align_open_output_button: QPushButton | None = None
        self.align_clear_button: QPushButton | None = None
        self.align_jump_to_end_button: QPushButton | None = None
        self.align_reset_view_button: QPushButton | None = None

        # 主题：``apply_settings_theme`` 已在 ``cli.run_gui`` 启动期把
        # qfluentwidgets Theme + QApplication palette settle 到目标模式
        # （依据 ``settings.ui_theme``）。这里只需按当前 ``palette`` 同步
        # 品牌色 + QSS；运行时主题切换走 ``_on_theme_changed`` 回调。
        from krok_helper.theme_workbench import palette, theme
        setThemeColor(palette().accent_primary, lazy=True)
        theme.changed.connect(self._on_theme_changed)

        self.setWindowTitle(APP_TITLE)
        app_icon = load_app_icon()
        if app_icon is not None:
            self.setWindowIcon(app_icon)
        self.resize(WINDOW_WIDTH, WINDOW_HEIGHT)
        self.setMinimumSize(WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT)

        self._apply_styles()
        self._build_ui()
        self._load_settings_into_ui()
        self._bind_shortcuts()

        self.preview_timer = QTimer(self)
        self.preview_timer.setInterval(300)
        self.preview_timer.timeout.connect(self._poll_alignment_preview)
        QTimer.singleShot(800, self._check_lyrics_timing_crash_recovery)
        QTimer.singleShot(1200, self._check_subtitle_render_crash_recovery)
        QTimer.singleShot(1500, self._notify_settings_corruption_if_any)
        QTimer.singleShot(2500, self._check_for_workbench_update_on_startup)

    def _track_background_task(self, attr_name: str, task: BackgroundTask) -> BackgroundTask:
        task.setObjectName(attr_name)
        setattr(self, attr_name, task)
        task.finished.connect(lambda attr_name=attr_name, task=task: self._cleanup_background_task(attr_name, task))
        task.finished.connect(task.deleteLater)
        return task

    def _cleanup_background_task(self, attr_name: str, task: BackgroundTask) -> None:
        if getattr(self, attr_name, None) is task:
            setattr(self, attr_name, None)

    def _running_background_tasks(self) -> list[BackgroundTask]:
        task_attrs = (
            "hires_task",
            "lyrics_search_task",
            "lyrics_fetch_task",
            "align_analysis_task",
            "align_auto_task",
            "align_export_task",
        )
        tasks: list[BackgroundTask] = []
        for attr_name in task_attrs:
            task = getattr(self, attr_name, None)
            if task is not None and task.isRunning():
                tasks.append(task)
        return tasks

    def showEvent(self, event) -> None:  # noqa: N802
        if not self._startup_geometry_applied:
            self._startup_geometry_applied = True
            QTimer.singleShot(0, self._apply_startup_window_geometry)
        super().showEvent(event)

    def changeEvent(self, event) -> None:  # noqa: N802
        if event.type() == QEvent.Type.WindowStateChange:
            old_state = event.oldState() if hasattr(event, "oldState") else Qt.WindowState.WindowNoState
            current_state = self.windowState()
            was_large = bool(
                old_state & (Qt.WindowState.WindowMaximized | Qt.WindowState.WindowFullScreen)
            )
            is_large = bool(
                current_state & (Qt.WindowState.WindowMaximized | Qt.WindowState.WindowFullScreen)
            )
            if was_large and not is_large and not self._restoring_from_maximized:
                self._restoring_from_maximized = True
                QTimer.singleShot(0, self._restore_windowed_geometry_centered)
        super().changeEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._refresh_lyrics_layout_direction()

    def _refresh_lyrics_layout_direction(self) -> None:
        layout = getattr(self, "lyrics_content_layout", None)
        if layout is None:
            return
        narrow = self.width() < 1220
        target_direction = QBoxLayout.Direction.TopToBottom if narrow else QBoxLayout.Direction.LeftToRight
        if layout.direction() != target_direction:
            layout.setDirection(target_direction)
        if narrow:
            layout.setStretch(0, 1)
            layout.setStretch(1, 1)
        else:
            layout.setStretch(0, 7)
            layout.setStretch(1, 6)

    def _apply_startup_window_geometry(self) -> None:
        self._restore_windowed_geometry_centered()

    def _restore_windowed_geometry_centered(self) -> None:
        try:
            screen = self.screen() or QApplication.primaryScreen()
            if screen is None:
                return
            available = screen.availableGeometry()
            safe_rect = available.adjusted(48, 72, -48, -88)
            if safe_rect.width() <= 0 or safe_rect.height() <= 0:
                safe_rect = available.adjusted(24, 24, -24, -48)

            min_width = min(WINDOW_MIN_WIDTH, safe_rect.width())
            min_height = min(WINDOW_MIN_HEIGHT, safe_rect.height())
            if min_width > 0 and min_height > 0:
                self.setMinimumSize(min_width, min_height)

            target_width = min(
                WINDOW_WIDTH,
                safe_rect.width(),
            )
            target_height = min(
                WINDOW_HEIGHT,
                safe_rect.height(),
            )
            target_width = max(min_width, target_width)
            target_height = max(min_height, target_height)
            left = safe_rect.x() + max(0, (safe_rect.width() - target_width) // 2)
            top = safe_rect.y() + max(0, (safe_rect.height() - target_height) // 2)
            self.setGeometry(left, top, target_width, target_height)
        finally:
            self._restoring_from_maximized = False

    def _on_theme_changed(self) -> None:
        """SUG ``theme.changed`` 回调：重应用工作台外壳 QSS + 品牌色 + 重跑
        状态驱动样式。

        **关键时序**：``theme.changed`` 是从 SUG ``_apply_theme_change`` /
        ``_reapply_win11_appearance`` 内同步发出的；那两个方法刚刚做完
        ``_refresh_all_widgets`` 递归 unpolish/polish 所有 widget。在它们的
        信号链上直接 setStyleSheet + 改子控件 QSS，等于在 Qt 还没把"polish
        完成"事件 drain 干净时就发起新一轮 QSS 替换 —— 容易触发 native
        access-violation（Win11 上配合 Mica + qfluentwidgets lazy stylesheet
        管理器尤其敏感）。

        修复：用 adapter 的 debounce 调度器把全部重活推到短暂 settle 之后，
        并把连续 emit 合并成最后状态的一次刷新。这样 SUG 的
        ``_apply_theme_change`` + double-singleShot 触发的
        ``_reapply_win11_appearance`` 两轮 polish 都先 settle，且用户连续切换时
        不会堆积多轮宿主 QSS 替换。
        """
        from krok_helper.theme_workbench import schedule_theme_refresh
        schedule_theme_refresh(self, self._apply_theme_refresh)

    def _apply_theme_refresh(self) -> None:
        """实际执行主题切换后的样式重应用 —— 由 ``_on_theme_changed``
        通过 debounce 延迟调度，避开 SUG 主题刷新链上的
        重入窗口。"""
        from krok_helper.theme_workbench import palette
        try:
            setThemeColor(palette().accent_primary, lazy=True)
            self._apply_styles()
            # 状态驱动的样式（依据 in-memory 计数/选中态生成 QSS）需要在
            # 主题切换时重跑，因为 themed() 只覆盖"恒等 lambda"场景。
            for fn_name in (
                "_refresh_alignment_material_inputs",
                "_apply_alignment_mode_styles",
                "_refresh_alignment_export_panels",
            ):
                fn = getattr(self, fn_name, None)
                if fn is None:
                    continue
                try:
                    fn()
                except Exception:
                    pass
            if hasattr(self, "_head_mode_current"):
                try:
                    self._update_head_mode_buttons(self._head_mode_current)
                except Exception:
                    pass
            if hasattr(self, "lyrics_results_table") and self.lyrics_search_results:
                try:
                    selected_key = self.lyrics_selected_candidate.key if self.lyrics_selected_candidate is not None else ""
                    self._render_lyrics_results_table(selected_key=selected_key)
                except Exception:
                    pass
        except Exception:
            import logging
            logging.getLogger(__name__).warning("主题切换刷新失败", exc_info=True)

    def _apply_styles(self) -> None:
        from krok_helper.theme_workbench import build_app_qss
        self.setStyleSheet(build_app_qss())

    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("AppRoot")
        shell = QVBoxLayout(central)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(12)

        self.workflow_stepper = WorkflowStepper(WORKFLOW_STEPS, self)
        self.workflow_stepper.stepClicked.connect(self._handle_workflow_step_clicked)

        self.workflow_bar = CardWidget(radius=10, padding=(12, 8, 12, 8), spacing=0)
        self.workflow_bar.setObjectName("WorkflowBar")
        self.workflow_bar.setFixedHeight(80)
        self._workflow_bar_layout = self.workflow_bar.createHBoxLayout()
        self._workflow_bar_layout.setContentsMargins(10, 8, 10, 8)
        self._workflow_bar_layout.setSpacing(10)
        self._workflow_bar_layout.addWidget(self.workflow_stepper, 1)

        # 折叠/展开按钮：紧凑模式 ↔ 完整模式的开关，状态持久化到 settings.workflow_compact
        self.workflow_compact_button = ToolButton(FIF.UP)
        self.workflow_compact_button.setObjectName("WorkflowCompactButton")
        self.workflow_compact_button.setFixedSize(48, 48)
        self.workflow_compact_button.setIconSize(QSize(16, 16))
        self.workflow_compact_button.clicked.connect(self._toggle_workflow_compact)
        self._workflow_bar_layout.addWidget(self.workflow_compact_button, 0, Qt.AlignmentFlag.AlignVCenter)

        self.global_settings_button = ToolButton(FIF.SETTING)
        self.global_settings_button.setObjectName("GlobalSettingsButton")
        self.global_settings_button.setToolTip("全局设置")
        self.global_settings_button.setFixedSize(48, 48)
        self.global_settings_button.setIconSize(QSize(20, 20))
        self.global_settings_button.clicked.connect(self._open_global_settings_window)
        self._workflow_bar_layout.addWidget(self.global_settings_button, 0, Qt.AlignmentFlag.AlignVCenter)

        workflow_bar_container = QWidget()
        workflow_bar_shell = QVBoxLayout(workflow_bar_container)
        workflow_bar_shell.setContentsMargins(24, 20, 24, 0)
        workflow_bar_shell.setSpacing(0)
        workflow_bar_shell.addWidget(self.workflow_bar)

        self.page_stack = QStackedWidget()
        self.page_stack.setObjectName("PageStack")
        # 换页动画期间窗口被拉伸的话，覆盖层上的快照就过期了，直接中断动画
        self.page_stack.installEventFilter(self)
        page_stack_container = QWidget()
        self._page_stack_container_layout = QVBoxLayout(page_stack_container)
        self._page_stack_normal_margins = (24, 0, 24, 16)
        self._page_stack_flush_margins = (0, 0, 0, 16)
        self._page_stack_container_layout.setContentsMargins(*self._page_stack_normal_margins)
        self._page_stack_container_layout.setSpacing(0)
        self._page_stack_container_layout.addWidget(self.page_stack)

        self.video_download_page = VideoDownloadPage(self.settings, self._save_all_settings, self)
        self.align_page = self._build_alignment_page()
        # 第 2 步「音视频处理」= Pivot 容器（波形对齐 / 音频分离），模块 ID 不变。
        from krok_helper.audio_processing import AudioProcessingPage, AudioSeparationPage

        self.audio_separation_page = AudioSeparationPage(
            self.settings,
            self._save_all_settings,
            parent=self.page_stack,
            workflow_context=self,
        )
        self.audio_processing_page = AudioProcessingPage(
            self.align_page,
            self.audio_separation_page,
            self.settings,
            self._save_all_settings,
            parent=self.page_stack,
        )
        self.lyrics_page = self._build_lyrics_page()
        self._sync_lyrics_timing_host_paths()
        ensure_sug_src_path()
        apply_sug_compat_patches()
        from strange_uta_game.frontend.main_window import MainWindow as LyricsTimingMainWindow

        lyrics_timing_settings = KrokHelperSettingsBridge(self.settings, self._save_all_settings)
        self.lyrics_timing_page = LyricsTimingMainWindow.for_embedding(
            parent=self.page_stack,
            settings_provider=lyrics_timing_settings,
        )
        try:
            self.lyrics_timing_page.export_to_next_requested.connect(
                self._export_lyrics_timing_to_next
            )
        except Exception:
            logging.getLogger(__name__).warning(
                "连接歌词打轴模块的下一步导出入口失败", exc_info=True
            )
        # Re-enable timeline zoom that SUG disables in embedded mode;
        # the host does not provide a replacement zoom control.
        try:
            editor = self.lyrics_timing_page.editorInterface
            timeline = getattr(editor, "timeline", None)
            if timeline is not None and hasattr(timeline, "set_zoom_enabled"):
                timeline.set_zoom_enabled(True)
        except Exception:
            pass
        # SUG embedded mode hides its title bar, but _update_title() keeps
        # calling setWindowTitle(), so mirror the current .sug file state to
        # the workflow description line for the lyrics timing step.
        try:
            self.lyrics_timing_page.windowTitleChanged.connect(
                self._on_lyrics_timing_title_changed
            )
        except Exception:
            pass

        from krok_helper.subtitle_render import SubtitleRenderWindow
        from krok_helper.subtitle_render.settings_bridge import (
            KrokHelperSubtitleRenderSettingsBridge,
        )

        self.subtitle_render_settings_bridge = KrokHelperSubtitleRenderSettingsBridge(
            self.settings, self._save_all_settings
        )
        self.subtitle_render_page = SubtitleRenderWindow.for_embedding(
            parent=self.page_stack,
            settings_provider=self.subtitle_render_settings_bridge,
            workflow_context=self,
        )
        try:
            self.subtitle_render_page.projectStateChanged.connect(
                self._on_subtitle_project_state_changed
            )
            self._on_subtitle_project_state_changed(
                self.subtitle_render_page.project_state()
            )
        except Exception:
            pass
        self.hires_page = self._build_hires_page()
        self.module_pages = {
            WORKFLOW_VIDEO_DOWNLOAD: self.video_download_page,
            WORKFLOW_WAVEFORM_ALIGN: self.audio_processing_page,
            WORKFLOW_LYRICS_SEARCH: self.lyrics_page,
            WORKFLOW_LYRICS_TIMING: self.lyrics_timing_page,
            WORKFLOW_SUBTITLE_RENDER: self.subtitle_render_page,
            WORKFLOW_HIRES_MIX: self.hires_page,
        }
        self.page_stack.addWidget(self.video_download_page)
        self.page_stack.addWidget(self.audio_processing_page)
        self.page_stack.addWidget(self.lyrics_page)
        self.page_stack.addWidget(self.lyrics_timing_page)
        self.page_stack.addWidget(self.subtitle_render_page)
        self.page_stack.addWidget(self.hires_page)

        shell.addWidget(workflow_bar_container)
        shell.addWidget(page_stack_container, 1)
        self.setCentralWidget(central)
        self.statusBar().hide()
        self._apply_workflow_compact(bool(self.settings.workflow_compact))
        self._show_module(WORKFLOW_VIDEO_DOWNLOAD)

    def _show_module(self, module_id: str) -> None:
        if module_id not in self.module_pages:
            return
        previous_module = self.active_module
        if (
            previous_module == WORKFLOW_WAVEFORM_ALIGN
            and module_id != WORKFLOW_WAVEFORM_ALIGN
            and getattr(self, "align_preview_process", None) is not None
            and self.align_preview_process.is_running()
        ):
            self._stop_alignment_preview()
        self.active_module = module_id
        # 旧页快照必须抢在 setCurrentWidget 之前拍 —— 之后它就被 stack 隐藏了。
        # getattr 容忍测试里的 SimpleNamespace 假 app（与上方 align_preview_process 同款写法）
        capture_outgoing = getattr(self, "_capture_outgoing_page", None)
        outgoing = capture_outgoing(previous_module, module_id) if capture_outgoing is not None else None
        self._sync_page_stack_margins(module_id)
        self.page_stack.setCurrentWidget(self.module_pages[module_id])
        animate_page = getattr(self, "_animate_page_switch", None)
        if animate_page is not None and outgoing is not None:
            animate_page(self.module_pages[module_id], outgoing)
        self.workflow_stepper.setCurrentModule(module_id)
        self._sync_workflow_shortcut_scope()

    def _capture_outgoing_page(
        self, previous_module: str | None, module_id: str
    ) -> tuple[QPixmap, int] | None:
        """拍下即将离场页面的快照，并算出滑动方向。

        必须在 ``setCurrentWidget`` 之前调用。返回 ``None`` 表示这次切换不做
        动画（窗口没显示、没有上一页、或前后是同一页）。
        """
        # getattr 容忍测试里的 SimpleNamespace 假 app（与上方 align_preview_process 同款写法）
        if not getattr(self, "isVisible", lambda: False)():
            return None
        previous_page = self.module_pages.get(previous_module) if previous_module else None
        if previous_page is None:
            return None
        old_index = self.page_stack.indexOf(previous_page)
        new_index = self.page_stack.indexOf(self.module_pages[module_id])
        if old_index < 0 or new_index < 0 or old_index == new_index:
            return None
        try:
            snapshot = previous_page.grab()
        except RuntimeError:
            return None
        if snapshot.isNull():
            return None
        return snapshot, (48 if new_index > old_index else -48)

    def _animate_page_switch(self, page: QWidget, outgoing: tuple[QPixmap, int]) -> None:
        """新页面沿工作流方向滑入的过渡动画（纯视觉，不影响布局与功能）。

        走快照过渡：真实换页在动画开始前已经完成（布局照常跑完），动画期间
        只有覆盖层在动，每帧固定两次位图 blit。直接动 ``page.pos`` 的老做法
        每帧要重绘入场页整棵子树，重页会掉到 30fps 上下；而且那种做法为了压住
        延迟的 LayoutRequest 得冻结 stack 布局，反过来又让首访页面在整段动画里
        停在未排版的默认几何（左上角 100x30），动画结束才炸开到全幅。
        """
        old_pixmap, distance = outgoing
        self._end_page_transition()

        # 换页后布局本来要等一次延迟的 LayoutRequest 才结算，这里先手动跑完，
        # 保证拍新页快照时它已经是全幅几何。
        container = self.page_stack.parentWidget()
        if container is not None and container.layout() is not None:
            container.layout().activate()
        stack_layout = self.page_stack.layout()
        if stack_layout is not None:
            stack_layout.activate()

        rect = self.page_stack.contentsRect()
        if rect.width() <= 0 or rect.height() <= 0:
            return
        try:
            new_pixmap = page.grab()
        except RuntimeError:
            return
        if new_pixmap.isNull():
            return

        overlay = self._page_transition_overlay
        if overlay is None:
            overlay = PageTransitionOverlay(self.page_stack)
            self._page_transition_overlay = overlay
        overlay.setGeometry(rect)
        overlay.start(old_pixmap, new_pixmap, distance)
        overlay.show()
        overlay.raise_()

        anim = QPropertyAnimation(overlay, b"offset", self)
        anim.setDuration(240)
        anim.setStartValue(float(distance))
        anim.setEndValue(0.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.finished.connect(self._end_page_transition)
        anim.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
        self._page_switch_anim = anim

    def _end_page_transition(self) -> None:
        """收尾／中断换页动画：停掉动画并撤下覆盖层，露出底下的真实页面。"""
        anim, self._page_switch_anim = getattr(self, "_page_switch_anim", None), None
        if anim is not None:
            try:
                anim.stop()
            except RuntimeError:
                pass
        overlay = self._page_transition_overlay
        if overlay is not None:
            try:
                overlay.hide()
            except RuntimeError:
                self._page_transition_overlay = None

    def accept_subtitle_video(self, path: Path) -> None:
        self.set_video_path(path)
        self._show_module(WORKFLOW_HIRES_MIX)

    def accept_separated_accompaniment(self, paths: Sequence[Path]) -> list[Path]:
        """第 2 步分离出的伴奏放进第 6 步的伴奏卡（追加，不顶掉已有的）。

        只放素材、不切页面：用户往往还要在第 2 步接着分下一首，跳走反而打断。
        """
        return self.add_off_vocal_paths(paths)

    def _export_lyrics_timing_to_next(self) -> None:
        """确保 SUG 项目落盘后，从该文件加载字幕并切换到第 5 步。"""
        from krok_helper.subtitle_render.frontend.fluent_dialogs import (
            fluent_choice,
            fluent_error,
            fluent_warning,
        )

        timing_page = getattr(self, "lyrics_timing_page", None)
        render_page = getattr(self, "subtitle_render_page", None)
        if timing_page is None or render_page is None:
            fluent_warning(
                self,
                "无法导出到下一步",
                "下一步模块尚未准备好，请稍后重试。",
            )
            return

        try:
            payload = timing_page.export_to_next_payload()
        except Exception as exc:
            logging.getLogger(__name__).exception("读取歌词打轴项目失败")
            fluent_error(
                self,
                "导出到下一步失败",
                f"无法读取当前打轴项目：\n{exc}",
            )
            return
        if not isinstance(payload, dict) or payload.get("project") is None:
            fluent_warning(
                self,
                "无法导出到下一步",
                "当前没有可导出的打轴项目。",
            )
            return

        source_value = payload.get("source_path")
        source_path = Path(str(source_value)) if source_value else None
        source_is_saved_sug = bool(
            source_path is not None
            and source_path.suffix.lower() == ".sug"
            and source_path.is_file()
        )
        dirty_checker = getattr(timing_page, "has_unsaved_changes", None)
        try:
            has_unsaved_changes = bool(
                callable(dirty_checker) and dirty_checker()
            )
        except Exception:
            logging.getLogger(__name__).warning(
                "检查歌词打轴项目保存状态失败", exc_info=True
            )
            has_unsaved_changes = True
        if has_unsaved_changes:
            choice = fluent_choice(
                self,
                "保存打轴项目",
                "当前项目包含未保存的修改。保存到 .sug 文件后再进入下一步吗？",
                ("保存并进入下一步", "取消"),
                default=0,
            )
            if choice != 0:
                return
        if has_unsaved_changes or not source_is_saved_sug:
            self._save_lyrics_timing_then_export(
                timing_page,
                force_save_as=not source_is_saved_sug,
            )
            return

        track = render_page.load_from_sug(source_path)
        if track is None:
            return

        # SUG 保存的是用户最初选择的媒体；视频直接作为第 5 步背景，纯音频
        # 则作为独立音轨。原始媒体不可用时再回退到当前播放音频。
        media_value = payload.get("media_path")
        media_path = Path(str(media_value)) if media_value else None
        media_kind = payload.get("media_kind")
        if media_path is not None and media_path.is_file():
            if media_kind == "video":
                render_page.load_video(media_path)
            else:
                render_page.load_audio(media_path)
        else:
            audio_value = payload.get("audio_path")
            audio_path = Path(str(audio_value)) if audio_value else None
            if audio_path is not None and audio_path.is_file():
                render_page.load_audio(audio_path)

        self._show_module(WORKFLOW_SUBTITLE_RENDER)

    def _save_lyrics_timing_then_export(
        self,
        timing_page: object,
        *,
        force_save_as: bool = False,
    ) -> None:
        """等待 SUG 异步保存成功后重新执行下一步导出。"""
        from krok_helper.subtitle_render.frontend.fluent_dialogs import fluent_error

        if getattr(self, "_lyrics_timing_export_waiting_for_save", False):
            return
        finished_signal = getattr(timing_page, "project_save_finished", None)
        failed_signal = getattr(timing_page, "project_save_failed", None)
        if force_save_as:
            trigger_save = lambda: self._trigger_lyrics_timing_save_as(timing_page)
        else:
            trigger_save = getattr(timing_page, "trigger_save", None)
        if (
            finished_signal is None
            or failed_signal is None
            or not callable(trigger_save)
        ):
            detail = (
                "歌词打轴模块不支持另存为当前项目，请重启应用后重试。"
                if force_save_as
                else "歌词打轴模块不支持保存完成通知，请重启应用后重试。"
            )
            fluent_error(
                self,
                "无法保存项目",
                detail,
            )
            return

        self._lyrics_timing_export_waiting_for_save = True

        def disconnect_callbacks() -> None:
            self._lyrics_timing_export_waiting_for_save = False
            for signal, callback in (
                (finished_signal, on_saved),
                (failed_signal, on_failed),
            ):
                try:
                    signal.disconnect(callback)
                except (TypeError, RuntimeError):
                    pass

        def on_saved(_path: str) -> None:
            disconnect_callbacks()
            QTimer.singleShot(0, self._export_lyrics_timing_to_next)

        def on_failed(message: str) -> None:
            disconnect_callbacks()
            fluent_error(self, "保存项目失败", str(message))

        finished_signal.connect(on_saved)
        failed_signal.connect(on_failed)
        try:
            started = bool(trigger_save())
        except Exception as exc:
            disconnect_callbacks()
            logging.getLogger(__name__).exception("发起歌词打轴项目保存失败")
            fluent_error(self, "保存项目失败", str(exc))
            return
        if not started:
            # 未命名项目关闭了“另存为”对话框，或保存任务未能启动。
            disconnect_callbacks()

    @staticmethod
    def _trigger_lyrics_timing_save_as(timing_page: object) -> bool:
        """请求 SUG 另存为；兼容当前尚未公开 trigger_save_as 的嵌入接口。"""
        public_trigger = getattr(timing_page, "trigger_save_as", None)
        if callable(public_trigger):
            return bool(public_trigger())

        editor = getattr(timing_page, "editorInterface", None)
        private_trigger = getattr(editor, "_on_save_as", None)
        store = getattr(timing_page, "_store", None)
        started_signal = getattr(store, "save_started", None)
        if not callable(private_trigger) or started_signal is None:
            return False

        started = False

        def on_started(_path: str) -> None:
            nonlocal started
            started = True

        started_signal.connect(on_started)
        try:
            private_trigger()
        finally:
            try:
                started_signal.disconnect(on_started)
            except (TypeError, RuntimeError):
                pass
        return started

    def _on_lyrics_timing_title_changed(self, title: str) -> None:
        """把 SUG 窗口标题里的项目状态镜像到「歌词打轴」步骤描述行。

        SUG 在 embedded 模式下标题栏不可见，但仍持续 setWindowTitle，这里据此
        把「当前 .sug 文件名 + 未保存」状态转贴到工作流步骤上（信息仅属于打轴
        这一功能，故只更新该步骤而非全局）。
        """
        stepper = getattr(self, "workflow_stepper", None)
        if stepper is None:
            return
        stepper.setStepStatus(
            WORKFLOW_LYRICS_TIMING, self._parse_lyrics_timing_status(title)
        )

    def _on_subtitle_project_state_changed(self, state: object) -> None:
        """Mirror the explicit subtitle project state to workflow step 5."""
        stepper = getattr(self, "workflow_stepper", None)
        if stepper is None:
            return
        status_factory = getattr(state, "status_text", None)
        try:
            status = status_factory() if callable(status_factory) else None
        except Exception:
            status = None
        stepper.setStepStatus(WORKFLOW_SUBTITLE_RENDER, status)

    @staticmethod
    def _parse_lyrics_timing_status(title: str) -> "str | None":
        """从 SUG 窗口标题解析「文件名 + 未保存」状态，无项目/格式不符时返回 None。

        SUG 标题格式（strange_uta_game/frontend/main_window.py::_update_title）：
          - 无项目: ``StrangeUtaGame - 歌词打轴工具 Bilibili@...``
          - 有项目: ``StrangeUtaGame - {name}{[未保存]} //Bilibili@...``
        这里耦合 SUG 的标题字面量，但解析容错：不匹配即返回 None，回退到步骤
        默认描述，绝不抛错。SUG 若改标题格式，最坏只是状态不再显示。
        """
        if not title or " //Bilibili@" not in title:
            return None
        core = title.split(" //Bilibili@", 1)[0]
        prefix = "StrangeUtaGame - "
        if core.startswith(prefix):
            core = core[len(prefix):]
        core = core.strip()
        dirty_mark = "[未保存]"
        dirty = core.endswith(dirty_mark)
        if dirty:
            core = core[: -len(dirty_mark)].strip()
        if not core:
            return None
        return f"{core} · 未保存" if dirty else core

    def open_project_file(self, project_path: Path) -> None:
        """Open a project path received from the command line or Windows shell."""
        suffix = project_path.suffix.lower()
        if suffix == ".sug":
            self.open_lyrics_timing_project(project_path)
            return
        if suffix == ".yurika":
            self.open_subtitle_render_project(project_path)
            return
        QMessageBox.critical(
            self,
            APP_TITLE,
            f"不支持的项目文件：\n{project_path}\n\n支持 .sug 和 .yurika 项目。",
        )

    def open_lyrics_timing_project(self, project_path: Path) -> None:
        project_path = project_path.expanduser()
        if project_path.suffix.lower() != ".sug":
            QMessageBox.critical(self, APP_TITLE, f"不支持的项目文件:\n{project_path}")
            return
        if not project_path.is_file():
            QMessageBox.critical(self, APP_TITLE, f"项目文件不存在:\n{project_path}")
            return

        lyrics_timing_page = getattr(self, "lyrics_timing_page", None)
        if lyrics_timing_page is None or not hasattr(lyrics_timing_page, "open_initial_project"):
            QMessageBox.critical(self, APP_TITLE, "打轴模块尚未准备好，无法打开 .sug 项目。")
            return

        self._show_module(WORKFLOW_LYRICS_TIMING)
        lyrics_timing_page.open_initial_project(str(project_path))

    def open_subtitle_render_project(self, project_path: Path) -> None:
        project_path = project_path.expanduser()
        if project_path.suffix.lower() != ".yurika":
            QMessageBox.critical(self, APP_TITLE, f"不支持的项目文件:\n{project_path}")
            return
        if not project_path.is_file():
            QMessageBox.critical(self, APP_TITLE, f"项目文件不存在:\n{project_path}")
            return

        subtitle_render_page = getattr(self, "subtitle_render_page", None)
        if subtitle_render_page is None or not hasattr(
            subtitle_render_page, "open_initial_project"
        ):
            QMessageBox.critical(
                self,
                APP_TITLE,
                "字幕渲染模块尚未准备好，无法打开 .yurika 项目。",
            )
            return

        self._show_module(WORKFLOW_SUBTITLE_RENDER)
        subtitle_render_page.open_initial_project(project_path)

    def _sync_page_stack_margins(self, module_id: str) -> None:
        layout = getattr(self, "_page_stack_container_layout", None)
        if layout is None:
            return
        flush_modules = {WORKFLOW_LYRICS_TIMING, WORKFLOW_SUBTITLE_RENDER}
        margins = (
            self._page_stack_flush_margins
            if module_id in flush_modules
            else self._page_stack_normal_margins
        )
        layout.setContentsMargins(*margins)

    def _handle_workflow_step_clicked(self, index: int) -> None:
        self._show_module(self.workflow_stepper.moduleIdAt(index))

    def _toggle_workflow_compact(self) -> None:
        new_state = not bool(self.settings.workflow_compact)
        self._apply_workflow_compact(new_state)
        self.settings.workflow_compact = new_state
        try:
            self._save_all_settings()
        except Exception:
            import logging
            logging.getLogger(__name__).warning("保存 workflow_compact 失败", exc_info=True)

    def _apply_workflow_compact(self, compact: bool) -> None:
        # 紧凑模式：bar 高度 80 → 44；同时收紧 bar 内边距、缩小右上角两个 48px 工具按钮，
        # 否则 48 高的按钮会撑爆 44 高的 bar。stepper 自己负责步条内部缩小。
        if not hasattr(self, "workflow_stepper"):
            return
        self.workflow_stepper.setCompact(compact)
        self.workflow_bar.setFixedHeight(44 if compact else 80)
        if hasattr(self, "_workflow_bar_layout") and self._workflow_bar_layout is not None:
            if compact:
                self._workflow_bar_layout.setContentsMargins(8, 4, 8, 4)
                self._workflow_bar_layout.setSpacing(6)
            else:
                self._workflow_bar_layout.setContentsMargins(10, 8, 10, 8)
                self._workflow_bar_layout.setSpacing(10)
        button_size = 32 if compact else 48
        icon_size = QSize(14, 14) if compact else QSize(20, 20)
        if hasattr(self, "global_settings_button"):
            self.global_settings_button.setFixedSize(button_size, button_size)
            self.global_settings_button.setIconSize(icon_size)
        if hasattr(self, "workflow_compact_button"):
            self.workflow_compact_button.setFixedSize(button_size, button_size)
            self.workflow_compact_button.setIconSize(QSize(12, 12) if compact else QSize(16, 16))
            self.workflow_compact_button.setIcon(FIF.DOWN if compact else FIF.UP)
            self.workflow_compact_button.setToolTip("展开工作流栏" if compact else "折叠工作流栏")

    def _save_all_settings(self) -> Path:
        self.settings.output_name_mode = self.output_name_mode_value
        self.settings.on_name_template = self.on_name_template_value
        self.settings.off_name_template = self.off_name_template_value
        self.settings.align_video_name_template = self.align_video_name_template_value
        self.settings.align_audio_name_template = self.align_audio_name_template_value
        self.settings.ffmpeg_dir = self.ffmpeg_dir_text
        self._sync_lyrics_timing_host_paths()
        if not self._loading_settings_into_ui:
            self._update_alignment_preferences_from_ui()
        return save_app_settings(self.settings)

    def _bind_shortcuts(self) -> None:
        self.shortcut_space = QShortcut(QKeySequence(Qt.Key.Key_Space), self)
        self.shortcut_space.activated.connect(self._handle_align_space_shortcut)
        self.shortcut_export = QShortcut(QKeySequence("Ctrl+S"), self)
        self.shortcut_export.activated.connect(self._handle_export_or_save_shortcut)
        self.shortcut_auto = QShortcut(QKeySequence("Ctrl+D"), self)
        self.shortcut_auto.activated.connect(self._handle_align_auto_shortcut)
        self.shortcut_drag_mode = QShortcut(QKeySequence("Alt+V"), self)
        self.shortcut_drag_mode.activated.connect(self._handle_align_drag_mode_shortcut)
        self._sync_workflow_shortcut_scope()

    def _sync_workflow_shortcut_scope(self) -> None:
        if not hasattr(self, "shortcut_space"):
            return
        align_active = self.active_module == WORKFLOW_WAVEFORM_ALIGN
        timing_active = self.active_module == WORKFLOW_LYRICS_TIMING
        self.shortcut_space.setEnabled(align_active)
        self.shortcut_auto.setEnabled(align_active)
        self.shortcut_drag_mode.setEnabled(align_active)
        self.shortcut_export.setEnabled(align_active or timing_active)

    def _focused_widget_is_text_input(self) -> bool:
        widget = QApplication.focusWidget()
        return isinstance(widget, (QLineEdit, QPlainTextEdit, QComboBox, QDoubleSpinBox))

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

    def _handle_export_or_save_shortcut(self) -> None:
        if self.active_module == WORKFLOW_LYRICS_TIMING:
            lyrics_timing_page = getattr(self, "lyrics_timing_page", None)
            if lyrics_timing_page is not None and hasattr(lyrics_timing_page, "trigger_save"):
                lyrics_timing_page.trigger_save()
            return
        if self.active_module != WORKFLOW_WAVEFORM_ALIGN or self._focused_widget_is_text_input():
            return
        self._start_aligned_export()

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

    def _build_lyrics_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("LyricsPage")
        shell = QVBoxLayout(page)
        shell.setContentsMargins(18, 18, 18, 18)
        shell.setSpacing(14)

        header = QVBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)
        title = QLabel("歌词检索")
        title.setObjectName("PageTitle")
        desc = QLabel(
            "输入歌名、歌手、专辑或歌词片段后搜索歌曲；结果会优先保留各来源原始搜索顺位，再用歌名、歌手、专辑等匹配度修正。"
        )
        desc.setObjectName("LyricsPageDescription")
        desc.setWordWrap(True)
        header.addWidget(title)
        header.addWidget(desc)
        shell.addLayout(header)

        search_panel = CardWidget(radius=10, padding=(18, 18, 18, 16), spacing=10)
        search_panel.setObjectName("LyricsSearchPanel")
        search_layout = search_panel.createGridLayout()
        search_layout.setHorizontalSpacing(10)
        search_layout.setVerticalSpacing(8)

        self.lyrics_source_combo = StyledComboBox()
        self.lyrics_source_combo.setObjectName("LyricsSourceCombo")
        self.lyrics_source_combo.addItems([label for label, _provider_ids in LYRICS_SOURCE_OPTIONS])
        self.lyrics_source_combo.setFont(build_lyrics_ui_font(point_size=10.5))
        self.lyrics_source_combo.setFixedWidth(156)
        self.lyrics_source_combo.setFixedHeight(42)
        self._install_single_click_combo_behavior(self.lyrics_source_combo)
        self.lyrics_source_combo.currentIndexChanged.connect(self._persist_lyrics_preferences)

        self.lyrics_keyword_edit = LyricsKeywordLineEdit()
        self.lyrics_keyword_edit.setObjectName("LyricsKeywordEdit")
        self.lyrics_keyword_edit.setPlaceholderText(
            "例如：Recollect / Reweave / Redo / Realize（也可以把歌词文件拖到这里自动提取歌名）"
        )
        self.lyrics_keyword_edit.setMinimumHeight(42)
        self.lyrics_keyword_edit.returnPressed.connect(self._start_lyrics_search)
        self.lyrics_search_button = PrimaryPushButton("搜索歌曲")
        self.lyrics_search_button.setObjectName("LyricsSearchButton")
        self.lyrics_search_button.setFixedSize(128, 42)
        self.lyrics_search_button.clicked.connect(self._start_lyrics_search)
        self.lyrics_status_label = QLabel("聚合模式覆盖 QQ音乐 / 酷狗音乐 / 网易云音乐 / LRCLIB；UtaTen 走带注音的日文专用通道，请单独选择。")
        self.lyrics_status_label.setObjectName("LyricsStatusText")
        self.lyrics_status_label.setWordWrap(True)
        self.lyrics_status_label.setFont(build_lyrics_ui_font(point_size=9.5))
        search_layout.addWidget(self.lyrics_source_combo, 0, 0)
        search_layout.addWidget(self.lyrics_keyword_edit, 0, 1)
        search_layout.addWidget(self.lyrics_search_button, 0, 2)
        search_layout.addWidget(self.lyrics_status_label, 1, 0, 1, 3)
        search_layout.setColumnStretch(1, 1)
        shell.addWidget(search_panel)

        content = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(14)
        self.lyrics_content_layout = content

        result_panel = CardWidget(radius=10, padding=(16, 16, 16, 16), spacing=12)
        result_panel.setObjectName("LyricsResultPanel")
        result_layout = result_panel.createVBoxLayout()
        result_title = QLabel("匹配结果")
        result_title.setObjectName("PanelTitle")
        self.lyrics_results_summary_label = QLabel("还没有搜索结果。")
        self.lyrics_results_summary_label.setObjectName("LyricsResultsSummary")
        self.lyrics_results_summary_label.setFont(build_lyrics_ui_font(point_size=9.5))
        self.lyrics_results_table = QTableWidget()
        self.lyrics_results_table.setRowCount(0)
        self.lyrics_results_table.setColumnCount(5)
        self.lyrics_results_table.setObjectName("LyricsResultsTable")
        self.lyrics_results_table.setHorizontalHeaderLabels(["歌曲", "艺术家", "专辑", "时长", "来源"])
        self.lyrics_results_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.lyrics_results_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.lyrics_results_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.lyrics_results_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.lyrics_results_table.setAlternatingRowColors(False)
        self.lyrics_results_table.setShowGrid(False)
        self.lyrics_results_table.setMouseTracking(True)
        self.lyrics_results_table.viewport().setMouseTracking(True)
        self.lyrics_results_table.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
        self.lyrics_results_table.setWordWrap(False)
        self.lyrics_results_table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.lyrics_results_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.lyrics_results_table.setFont(build_lyrics_ui_font(point_size=10.5))
        self.lyrics_results_table.verticalHeader().setVisible(False)
        self.lyrics_results_table.verticalHeader().setDefaultSectionSize(50)
        self.lyrics_results_table.horizontalHeader().setStretchLastSection(False)
        self.lyrics_results_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.lyrics_results_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.lyrics_results_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.lyrics_results_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self.lyrics_results_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.lyrics_results_table.delegate = LyricsResultsDelegate(self.lyrics_results_table)
        self.lyrics_results_table.setItemDelegate(self.lyrics_results_table.delegate)
        self.lyrics_results_table.installEventFilter(self)
        self.lyrics_results_table.currentCellChanged.connect(self._handle_lyrics_result_selected)
        self.lyrics_results_table.verticalScrollBar().valueChanged.connect(self._maybe_load_more_lyrics_results)
        result_layout.addWidget(result_title)
        result_layout.addWidget(self.lyrics_results_summary_label)
        result_layout.addWidget(self.lyrics_results_table, 1)
        QTimer.singleShot(0, self._resize_lyrics_results_columns)
        content.addWidget(result_panel, 7)

        preview_panel = CardWidget(radius=10, padding=(16, 16, 16, 16), spacing=12)
        preview_panel.setObjectName("LyricsPreviewPanel")
        self.lyrics_preview_panel = preview_panel
        preview_panel.installEventFilter(self)
        preview_layout = preview_panel.createVBoxLayout()
        preview_header = QHBoxLayout()
        preview_header.setContentsMargins(0, 0, 0, 0)
        preview_header.setSpacing(8)
        preview_title = QLabel("歌词预览")
        preview_title.setObjectName("PanelTitle")
        preview_header.addWidget(preview_title)
        preview_header.addStretch(1)

        preview_controls = QHBoxLayout()
        preview_controls.setContentsMargins(0, 0, 0, 0)
        preview_controls.setSpacing(8)

        self.copy_lyrics_button = QPushButton("复制歌词")
        self.copy_lyrics_button.setObjectName("LyricsCopyButton")
        self.copy_lyrics_button.setIcon(FIF.COPY.icon())
        self.copy_lyrics_button.setIconSize(QSize(16, 16))
        self.copy_lyrics_button.clicked.connect(self._copy_current_lyrics_preview)
        self.copy_lyrics_button.setFixedHeight(36)
        preview_controls.addWidget(self.copy_lyrics_button)
        self.lyrics_strip_intro_checkbox = QCheckBox("省略歌曲介绍")
        self.lyrics_strip_intro_checkbox.setObjectName("LyricsStripIntroCheck")
        self.lyrics_strip_intro_checkbox.setMinimumHeight(36)
        self.lyrics_strip_intro_checkbox.setChecked(True)
        self.lyrics_strip_intro_checkbox.toggled.connect(lambda _: self._refresh_lyrics_preview())
        self.lyrics_strip_intro_checkbox.toggled.connect(self._persist_lyrics_preferences)
        preview_controls.addWidget(self.lyrics_strip_intro_checkbox)
        self.lyrics_language_combo = StyledComboBox()
        self.lyrics_language_combo.setObjectName("LyricsLanguageCombo")
        self.lyrics_language_combo.addItems([label for label, _value in LYRICS_LANGUAGE_OPTIONS])
        self.lyrics_language_combo.setFixedWidth(112)
        self.lyrics_language_combo.setFixedHeight(36)
        self.lyrics_language_combo.setToolTip("切换原文 / 中文译文（无译文时禁用）")
        self.lyrics_language_combo.currentIndexChanged.connect(lambda _: self._refresh_lyrics_preview())
        self.lyrics_language_combo.currentIndexChanged.connect(self._persist_lyrics_preferences)
        self._install_single_click_combo_behavior(self.lyrics_language_combo)
        preview_controls.addWidget(self.lyrics_language_combo)
        self.lyrics_preview_mode_combo = StyledComboBox()
        self.lyrics_preview_mode_combo.setObjectName("LyricsPreviewModeCombo")
        self.lyrics_preview_mode_combo.addItems([label for label, _mode in LYRICS_PREVIEW_MODE_OPTIONS])
        self.lyrics_preview_mode_combo.setFixedWidth(138)
        self.lyrics_preview_mode_combo.setFixedHeight(36)
        self.lyrics_preview_mode_combo.currentIndexChanged.connect(lambda _: self._refresh_lyrics_preview())
        self.lyrics_preview_mode_combo.currentIndexChanged.connect(self._persist_lyrics_preferences)
        self._install_single_click_combo_behavior(self.lyrics_preview_mode_combo)
        preview_controls.addWidget(self.lyrics_preview_mode_combo)
        self.import_lyrics_to_timing_button = QPushButton("导入到打轴", preview_panel)
        self.import_lyrics_to_timing_button.setObjectName("LyricsImportButton")
        self.import_lyrics_to_timing_button.setIcon(FIF.SEND.icon())
        self.import_lyrics_to_timing_button.setIconSize(QSize(16, 16))
        self.import_lyrics_to_timing_button.clicked.connect(self._import_current_lyrics_to_timing)
        self.import_lyrics_to_timing_button.setFixedSize(138, 36)
        self.import_lyrics_to_timing_button.raise_()
        preview_header.addLayout(preview_controls)

        self.lyrics_preview_title_label = ElidedLabel("未选择歌曲")
        self.lyrics_preview_title_label.setObjectName("LyricsPreviewTitle")
        self.lyrics_preview_title_label.setFont(build_lyrics_ui_font(point_size=14, bold=True))
        self.lyrics_preview_meta_label = QLabel("来源: -")
        self.lyrics_preview_meta_label.setObjectName("LyricsPreviewMeta")
        self.lyrics_preview_meta_label.setWordWrap(True)
        self.lyrics_preview_meta_label.setFont(build_lyrics_ui_font(point_size=10.5))
        self.lyrics_match_summary_label = QLabel("匹配字段: -")
        self.lyrics_match_summary_label.setObjectName("LyricsMatchSummary")
        self.lyrics_match_summary_label.setWordWrap(True)
        self.lyrics_match_summary_label.setFont(build_lyrics_ui_font(point_size=9.5))
        self.lyrics_preview_hint_label = QLabel("搜索后选择一首歌，即可查看逐行或按字的 LRC 预览。")
        self.lyrics_preview_hint_label.setObjectName("LyricsPreviewHint")
        self.lyrics_preview_hint_label.setWordWrap(True)
        self.lyrics_preview_hint_label.setFont(build_lyrics_ui_font(point_size=9.5))

        self.lyrics_preview_edit = QPlainTextEdit()
        self.lyrics_preview_edit.setReadOnly(True)
        self.lyrics_preview_edit.setObjectName("LyricsPreviewText")
        self.lyrics_preview_edit.setFont(build_lyrics_ui_font(point_size=11))
        self.lyrics_preview_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.lyrics_preview_edit.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.lyrics_preview_edit.setPlaceholderText("歌词会显示在这里。")
        self.lyrics_preview_edit.setTabStopDistance(QFontMetrics(self.lyrics_preview_edit.font()).horizontalAdvance(" ") * 4)

        preview_layout.addLayout(preview_header)
        preview_layout.addWidget(self.lyrics_preview_title_label)
        preview_layout.addWidget(self.lyrics_preview_meta_label)
        preview_layout.addWidget(self.lyrics_match_summary_label)
        preview_layout.addWidget(self.lyrics_preview_hint_label)
        preview_layout.addWidget(self.lyrics_preview_edit, 1)
        QTimer.singleShot(0, self._position_lyrics_import_button)
        content.addWidget(preview_panel, 6)

        shell.addLayout(content, 1)
        self._refresh_lyrics_layout_direction()
        self._clear_lyrics_results()
        return page

    def _start_lyrics_search(self, *, load_more: bool = False) -> None:
        if self.lyrics_search_task is not None and self.lyrics_search_task.isRunning():
            return
        if load_more and not self.lyrics_has_more_results:
            return
        keyword = self.lyrics_search_keyword if load_more else self.lyrics_keyword_edit.text().strip()
        if not keyword:
            QMessageBox.information(self, APP_TITLE, "请输入搜索关键词。")
            return

        self.lyrics_search_button.setEnabled(False)
        provider_ids = self.lyrics_search_provider_ids if load_more else self._current_lyrics_source_ids()
        if load_more:
            self._lyrics_loading_more = True
            selected_key = self.lyrics_selected_candidate.key if self.lyrics_selected_candidate is not None else ""
            self._render_lyrics_results_table(selected_key=selected_key)
            self.lyrics_status_label.setText(f"已加载 {len(self.lyrics_search_results)} 条结果，正在加载更多…")
        else:
            self._lyrics_loading_more = False
            self.lyrics_status_label.setText("正在搜索歌词候选歌曲…")
            self.lyrics_search_keyword = keyword
            self.lyrics_search_provider_ids = provider_ids
            self.lyrics_next_provider_pages = {}
            self.lyrics_has_more_results = False
            self.lyrics_pending_results = []
            self._clear_lyrics_results()

        def runner(logger: Callable[[str], None]) -> tuple[bool, LyricsSearchBatch]:
            _ = logger
            return (
                load_more,
                self.lyrics_search_service.search_batch(
                    keyword,
                    provider_ids=provider_ids,
                    limit=DEFAULT_LYRICS_SEARCH_LIMIT,
                    provider_pages=self.lyrics_next_provider_pages if load_more else None,
                ),
            )

        task = self._track_background_task("lyrics_search_task", BackgroundTask(runner))
        task.task_succeeded.connect(self._finish_lyrics_search_success)
        task.task_failed.connect(self._finish_lyrics_search_failure)
        task.start()

    def _finish_lyrics_search_success(self, results: object) -> None:
        self.lyrics_search_button.setEnabled(True)
        load_more = False
        payload = results
        if isinstance(results, tuple) and len(results) == 2 and isinstance(results[0], bool):
            load_more = results[0]
            payload = results[1]

        batch = payload if isinstance(payload, LyricsSearchBatch) else None
        batch_results = list(batch.results) if batch is not None else (list(results) if isinstance(results, list) else [])
        if batch is not None:
            self.lyrics_pending_results.extend(batch.overflow_results)
        self.lyrics_next_provider_pages = dict(batch.next_provider_pages) if batch is not None else {}
        self.lyrics_has_more_results = bool(batch.has_more or self.lyrics_pending_results) if batch is not None else False

        if load_more:
            existing_keys = {candidate.key for candidate in self.lyrics_search_results}
            for candidate in batch_results:
                if candidate.key not in existing_keys:
                    self.lyrics_search_results.append(candidate)
                    existing_keys.add(candidate.key)
        else:
            self.lyrics_search_results = batch_results

        if not self.lyrics_search_results:
            self.lyrics_status_label.setText("没有找到匹配的歌词结果。")
            self._clear_lyrics_results()
            return

        selected_key = self.lyrics_selected_candidate.key if self.lyrics_selected_candidate is not None else ""
        self._render_lyrics_results_table(selected_key=selected_key if load_more else "")
        selected_source = self.lyrics_source_combo.currentText()
        if selected_source == "聚合":
            self.lyrics_status_label.setText(
                f"已加载 {len(self.lyrics_search_results)} 条候选结果，来源优先级：QQ > 酷狗 > 网易云 > LRCLIB。"
            )
        else:
            self.lyrics_status_label.setText(f"已加载 {len(self.lyrics_search_results)} 条候选结果，当前来源：{selected_source}。")
        self.lyrics_results_summary_label.setText(
            "结果优先保留各来源原始搜索顺位，再按歌曲、艺术家、专辑匹配度修正；同一首歌会保留不同来源。"
            + (" 向下滚动可继续加载更多结果。" if self.lyrics_has_more_results else "")
        )
        self._lyrics_loading_more = False
        self._render_lyrics_results_table(selected_key=selected_key if load_more else "")

    def _finish_lyrics_search_failure(self, message: str) -> None:
        self.lyrics_search_button.setEnabled(True)
        self._lyrics_loading_more = False
        if not self.lyrics_search_results:
            self._clear_lyrics_results()
        else:
            selected_key = self.lyrics_selected_candidate.key if self.lyrics_selected_candidate is not None else ""
            self._render_lyrics_results_table(selected_key=selected_key)
        self.lyrics_status_label.setText("歌词搜索失败。")
        QMessageBox.critical(self, APP_TITLE, message or "歌词搜索失败。")

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if (
            watched is getattr(self, "page_stack", None)
            and event.type() == QEvent.Type.Resize
            and getattr(self, "_page_switch_anim", None) is not None
        ):
            # 快照是按旧尺寸拍的，拉伸后继续播只会拉花，直接收尾露出真实页面
            self._end_page_transition()
        if event.type() == QEvent.Type.Wheel and self._should_route_alignment_wheel(watched, event):
            self.waveform_view.wheelEvent(event)
            if event.isAccepted():
                self._sync_alignment_zoom_slider()
                return True
            return False
        if (
            hasattr(self, "lyrics_results_table")
            and watched is self.lyrics_results_table
            and event.type() in {QEvent.Type.Resize, QEvent.Type.Show}
        ):
            QTimer.singleShot(0, self._resize_lyrics_results_columns)
        if (
            hasattr(self, "lyrics_preview_panel")
            and watched is self.lyrics_preview_panel
            and event.type() in {QEvent.Type.Resize, QEvent.Type.Show}
        ):
            QTimer.singleShot(0, self._position_lyrics_import_button)
        return super().eventFilter(watched, event)

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

    def _sync_alignment_zoom_slider(self) -> None:
        if hasattr(self, "align_zoom_slider"):
            self.align_zoom_slider.blockSignals(True)
            self.align_zoom_slider.setValue(self._pps_to_slider(self.waveform_view.pixels_per_second))
            self.align_zoom_slider.blockSignals(False)

    def _resize_lyrics_results_columns(self) -> None:
        viewport_width = self.lyrics_results_table.viewport().width()
        if viewport_width <= 0:
            return

        duration_width = 92
        source_width = 96
        remaining = max(120, viewport_width - duration_width - source_width)
        song_width = int(remaining * 0.36)
        artist_width = int(remaining * 0.27)
        album_width = max(0, remaining - song_width - artist_width)

        self.lyrics_results_table.setColumnWidth(0, song_width)
        self.lyrics_results_table.setColumnWidth(1, artist_width)
        self.lyrics_results_table.setColumnWidth(2, album_width)
        self.lyrics_results_table.setColumnWidth(3, duration_width)
        self.lyrics_results_table.setColumnWidth(4, source_width)

    def _render_lyrics_results_table(self, *, selected_key: str = "") -> None:
        from krok_helper.theme_workbench import palette

        p = palette()
        muted_text = p.text_secondary
        duration_text_color = p.text_hint if p.is_dark else "#475569"
        source_text = "#FF9AAA" if p.is_dark else "#B94D5D"
        row_count = len(self.lyrics_search_results) + (1 if self._lyrics_loading_more and self.lyrics_search_results else 0)
        self.lyrics_results_table.clearSpans()
        self.lyrics_results_table.setRowCount(row_count)
        self._resize_lyrics_results_columns()
        selected_row = -1
        for row, candidate in enumerate(self.lyrics_search_results):
            duration_text = format_media_duration(candidate.duration_seconds) if candidate.duration_seconds else "-"
            items = [
                QTableWidgetItem(candidate.title or "-"),
                QTableWidgetItem(candidate.artist or "-"),
                QTableWidgetItem(candidate.album or "-"),
                QTableWidgetItem(duration_text),
                QTableWidgetItem(candidate.provider_name),
            ]
            for column, item in enumerate(items):
                item.setData(Qt.ItemDataRole.UserRole, row)
                item.setFont(build_lyrics_ui_font(point_size=10.5, bold=(column == 0)))
                if column in (1, 2):
                    item.setForeground(QBrush(QColor(muted_text)))
                if column == 3:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                    item.setForeground(QBrush(QColor(duration_text_color)))
                elif column == 4:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    item.setFont(build_lyrics_ui_font(point_size=9.5, bold=True))
                    item.setForeground(QBrush(QColor(source_text)))
                else:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                self.lyrics_results_table.setItem(row, column, item)
            if selected_key and candidate.key == selected_key:
                selected_row = row

        if self._lyrics_loading_more and self.lyrics_search_results:
            loading_row = len(self.lyrics_search_results)
            loading_item = QTableWidgetItem("加载中...")
            loading_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            loading_item.setFont(build_lyrics_ui_font(point_size=9.5))
            loading_item.setForeground(QBrush(QColor(muted_text)))
            loading_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.lyrics_results_table.setSpan(loading_row, 0, 1, self.lyrics_results_table.columnCount())
            self.lyrics_results_table.setItem(loading_row, 0, loading_item)

        if selected_row < 0 and self.lyrics_search_results:
            selected_row = 0
        if selected_row >= 0:
            self.lyrics_results_table.selectRow(selected_row)
            self._handle_lyrics_result_selected(selected_row, 0, -1, -1)

    def _maybe_load_more_lyrics_results(self) -> None:
        if not self.lyrics_has_more_results or self._lyrics_loading_more:
            return
        if self.lyrics_search_task is not None and self.lyrics_search_task.isRunning():
            return
        scrollbar = self.lyrics_results_table.verticalScrollBar()
        if scrollbar.maximum() <= 0:
            return
        if scrollbar.value() < scrollbar.maximum() - 12:
            return
        if not self.lyrics_search_keyword:
            return
        if self.lyrics_pending_results:
            self._append_pending_lyrics_results()
            return
        self._start_lyrics_search(load_more=True)

    def _append_pending_lyrics_results(self) -> None:
        if not self.lyrics_pending_results:
            self.lyrics_has_more_results = bool(self.lyrics_next_provider_pages)
            return
        selected_key = self.lyrics_selected_candidate.key if self.lyrics_selected_candidate is not None else ""
        chunk = self.lyrics_pending_results[:DEFAULT_LYRICS_SEARCH_LIMIT]
        self.lyrics_pending_results = self.lyrics_pending_results[DEFAULT_LYRICS_SEARCH_LIMIT:]
        existing_keys = {candidate.key for candidate in self.lyrics_search_results}
        for candidate in chunk:
            if candidate.key not in existing_keys:
                self.lyrics_search_results.append(candidate)
                existing_keys.add(candidate.key)
        self.lyrics_has_more_results = bool(self.lyrics_pending_results or self.lyrics_next_provider_pages)
        self._render_lyrics_results_table(selected_key=selected_key)
        selected_source = self.lyrics_source_combo.currentText()
        if selected_source == "聚合":
            self.lyrics_status_label.setText(
                f"已加载 {len(self.lyrics_search_results)} 条候选结果，来源优先级：QQ > 酷狗 > 网易云 > LRCLIB。"
            )
        else:
            self.lyrics_status_label.setText(f"已加载 {len(self.lyrics_search_results)} 条候选结果，当前来源：{selected_source}。")
        self.lyrics_results_summary_label.setText(
            "结果优先保留各来源原始搜索顺位，再按歌曲、艺术家、专辑匹配度修正；同一首歌会保留不同来源。"
            + (" 向下滚动可继续加载更多结果。" if self.lyrics_has_more_results else "")
        )

    def _handle_lyrics_result_selected(
        self,
        current_row: int,
        current_column: int,
        previous_row: int,
        previous_column: int,
    ) -> None:
        _ = current_column, previous_row, previous_column
        if current_row < 0 or current_row >= len(self.lyrics_search_results):
            self.lyrics_selected_candidate = None
            self._refresh_lyrics_preview()
            return
        self.lyrics_selected_candidate = self.lyrics_search_results[current_row]
        self._ensure_selected_lyrics_loaded()
        self._refresh_lyrics_preview()

    def _refresh_lyrics_preview(self) -> None:
        candidate = self.lyrics_selected_candidate
        self._update_lyrics_language_combo_state(candidate)
        if candidate is None:
            self.lyrics_preview_title_label.setText("未选择歌曲")
            self.lyrics_preview_meta_label.setText("来源: -")
            self.lyrics_match_summary_label.setText("匹配字段: -")
            self.lyrics_preview_hint_label.setText("搜索后选择一首歌，即可查看逐行或按字的 LRC 预览。")
            self.lyrics_preview_edit.clear()
            self._refresh_lyrics_import_button(None)
            return

        if candidate.load_error:
            self.lyrics_preview_title_label.setText(f"{candidate.title or '未命名'}")
            self.lyrics_preview_meta_label.setText(
                f"歌手: {candidate.artist or '-'}    专辑: {candidate.album or '-'}    来源: {candidate.provider_name}"
            )
            self.lyrics_match_summary_label.setText("歌词加载失败")
            self.lyrics_preview_hint_label.setText(candidate.load_error)
            self.lyrics_preview_edit.setPlainText(candidate.load_error)
            self._refresh_lyrics_import_button(None)
            return

        if not candidate.lyrics_loaded:
            self.lyrics_preview_title_label.setText(f"{candidate.title or '未命名'}")
            self.lyrics_preview_meta_label.setText(
                f"歌手: {candidate.artist or '-'}    专辑: {candidate.album or '-'}    来源: {candidate.provider_name}"
            )
            self.lyrics_match_summary_label.setText(
                "匹配字段: "
                f"{candidate.match_source}；歌名 {candidate.title_score:.0f} / "
                f"歌手 {candidate.artist_score:.0f} / 专辑 {candidate.album_score:.0f}"
            )
            self.lyrics_preview_hint_label.setText(f"正在从 {candidate.provider_name} 加载歌词…")
            self.lyrics_preview_edit.setPlainText("正在加载歌词…")
            self._refresh_lyrics_import_button(None)
            return

        preview = self._build_current_lyrics_preview(candidate)
        self.lyrics_preview_title_label.setText(f"{candidate.title or '未命名'}")
        self.lyrics_preview_meta_label.setText(
            f"歌手: {candidate.artist or '-'}    专辑: {candidate.album or '-'}    来源: {candidate.provider_name}"
        )
        self.lyrics_match_summary_label.setText(
            "匹配字段: "
            f"{candidate.match_source}；歌名 {candidate.title_score:.0f} / "
            f"歌手 {candidate.artist_score:.0f} / 专辑 {candidate.album_score:.0f} / "
            f"歌词 {candidate.lyrics_score:.0f}"
        )
        self.lyrics_preview_hint_label.setText(self._build_lyrics_preview_hint(candidate, preview))
        self.lyrics_preview_edit.setPlainText(preview.text or "当前结果没有可显示的歌词。")
        self._refresh_lyrics_import_button(preview)

    def _build_current_lyrics_preview(self, candidate: LyricsSearchCandidate) -> LyricsPreview:
        return build_lyrics_preview(
            candidate,
            self._current_lyrics_preview_mode(),
            strip_intro_lines=self.lyrics_strip_intro_checkbox.isChecked(),
            language=self._current_lyrics_language(),
        )

    def _refresh_lyrics_import_button(self, preview: LyricsPreview | None) -> None:
        button = getattr(self, "import_lyrics_to_timing_button", None)
        if button is None:
            return
        button.setEnabled(bool(preview is not None and preview.text.strip()))
        self._position_lyrics_import_button()

    def _position_lyrics_import_button(self) -> None:
        button = getattr(self, "import_lyrics_to_timing_button", None)
        panel = getattr(self, "lyrics_preview_panel", None)
        combo = getattr(self, "lyrics_preview_mode_combo", None)
        if button is None or panel is None or combo is None:
            return
        if not panel.isVisible():
            return
        combo_pos = combo.mapTo(panel, combo.rect().topLeft())
        x = combo_pos.x()
        y = combo_pos.y() + combo.height() + 8
        max_x = max(0, panel.width() - button.width() - 16)
        max_y = max(0, panel.height() - button.height() - 16)
        button_x = min(max(x, 0), max_x)
        button.move(button_x, min(max(y, 0), max_y))
        button.raise_()

        title_label = getattr(self, "lyrics_preview_title_label", None)
        if title_label is not None:
            title_pos = title_label.mapTo(panel, title_label.rect().topLeft())
            available_width = button_x - title_pos.x() - 12
            title_label.setMaximumWidth(max(120, available_width))

    def _build_lyrics_preview_hint(self, candidate: LyricsSearchCandidate, preview: LyricsPreview) -> str:
        if candidate.provider_id == "utaten" and UTATEN_RUBY_MARKER in (preview.text or ""):
            return (
                f"{candidate.provider_name} 提供带注音的无时间戳 LRC；"
                "导入打轴后会按 ruby 块自动连词、不会重新注音。"
            )
        if preview.used_synced_lyrics and preview.used_estimated_char_timing:
            return (
                f"{candidate.provider_name} 提供了逐行同步歌词；当前“按字 LRC”是基于相邻行时间做的轻量估算，"
                "方便先预览卡拉 OK 节奏。"
            )
        if preview.used_synced_lyrics:
            return f"{candidate.provider_name} 提供了同步歌词，当前优先显示这个来源的字幕。"
        return f"{candidate.provider_name} 当前只有纯文本歌词，暂时无法提供真实时间轴。"

    def _copy_current_lyrics_preview(self) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is None:
            return
        clipboard.setText(self.lyrics_preview_edit.toPlainText())
        show_fluent_tooltip(
            "歌词已复制到剪切板",
            parent=self.copy_lyrics_button,
            global_pos=self.copy_lyrics_button.mapToGlobal(self.copy_lyrics_button.rect().center()),
            duration=1600,
        )

    def _import_current_lyrics_to_timing(self) -> None:
        candidate = self.lyrics_selected_candidate
        if candidate is None or candidate.load_error or not candidate.lyrics_loaded:
            QMessageBox.information(self, APP_TITLE, "请先选择并加载一条歌词。")
            return
        preview = self._build_current_lyrics_preview(candidate)
        lyrics_text = preview.text.strip()
        if not lyrics_text:
            QMessageBox.information(self, APP_TITLE, "当前筛选结果没有可导入的歌词。")
            self._refresh_lyrics_import_button(preview)
            return
        lyrics_timing_page = getattr(self, "lyrics_timing_page", None)
        if lyrics_timing_page is None or not hasattr(lyrics_timing_page, "import_lyrics_from_text"):
            QMessageBox.critical(self, APP_TITLE, "打轴模块尚未准备好，无法导入歌词。")
            return
        try:
            imported = bool(lyrics_timing_page.import_lyrics_from_text(lyrics_text))
        except Exception as exc:
            QMessageBox.critical(self, APP_TITLE, f"导入到打轴失败：\n{exc}")
            return
        if not imported:
            return
        self._show_module(WORKFLOW_LYRICS_TIMING)

    def _ensure_selected_lyrics_loaded(self) -> None:
        candidate = self.lyrics_selected_candidate
        if candidate is None or candidate.lyrics_loaded:
            return
        if self.lyrics_fetch_task is not None and self.lyrics_fetch_task.isRunning():
            return

        self._lyrics_loading_key = candidate.key

        def runner(logger: Callable[[str], None]) -> LyricsSearchCandidate:
            _ = logger
            return self.lyrics_search_service.fetch_lyrics(candidate)

        task = self._track_background_task("lyrics_fetch_task", BackgroundTask(runner))
        task.task_succeeded.connect(self._finish_lyrics_fetch_success)
        task.task_failed.connect(self._finish_lyrics_fetch_failure)
        task.start()

    def _finish_lyrics_fetch_success(self, result: object) -> None:
        self._lyrics_loading_key = ""
        loaded_candidate = result if isinstance(result, LyricsSearchCandidate) else None
        if loaded_candidate is not None:
            for index, candidate in enumerate(self.lyrics_search_results):
                if candidate.key == loaded_candidate.key:
                    self.lyrics_search_results[index] = loaded_candidate
                    if self.lyrics_selected_candidate is not None and self.lyrics_selected_candidate.key == loaded_candidate.key:
                        self.lyrics_selected_candidate = loaded_candidate
                    break
        self._refresh_lyrics_preview()
        if self.lyrics_selected_candidate is not None and not self.lyrics_selected_candidate.lyrics_loaded:
            QTimer.singleShot(0, self._ensure_selected_lyrics_loaded)

    def _finish_lyrics_fetch_failure(self, message: str) -> None:
        failed_key = self._lyrics_loading_key
        self._lyrics_loading_key = ""
        for candidate in self.lyrics_search_results:
            if candidate.key == failed_key:
                candidate.load_error = message or f"{candidate.provider_name} 歌词加载失败。"
                if self.lyrics_selected_candidate is not None and self.lyrics_selected_candidate.key == failed_key:
                    self.lyrics_selected_candidate = candidate
                break
        self._refresh_lyrics_preview()
        if self.lyrics_selected_candidate is not None and not self.lyrics_selected_candidate.lyrics_loaded and not self.lyrics_selected_candidate.load_error:
            QTimer.singleShot(0, self._ensure_selected_lyrics_loaded)

    def _clear_lyrics_results(self) -> None:
        self.lyrics_search_results = []
        self.lyrics_pending_results = []
        self.lyrics_selected_candidate = None
        self.lyrics_next_provider_pages = {}
        self.lyrics_has_more_results = False
        self._lyrics_loading_more = False
        self._lyrics_loading_key = ""
        self.lyrics_results_table.clearContents()
        self.lyrics_results_table.setRowCount(0)
        self.lyrics_results_summary_label.setText("还没有搜索结果。")
        self._refresh_lyrics_preview()

    def _build_hires_page(self) -> QWidget:
        page = QWidget()
        shell = QVBoxLayout(page)
        shell.setContentsMargins(20, 20, 20, 20)
        shell.setSpacing(16)

        header = QVBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)
        title = QLabel("卡拉 OK 字幕视频一键 Hi-Res 生成")
        title.setObjectName("PageTitle")
        desc = QLabel("把字幕视频拖进下方卡片，再按需放入原唱音频和 / 或伴奏音频。至少提供一条音频就可以开始生成。")
        desc.setWordWrap(True)
        from krok_helper.theme_workbench import palette as _wb_pal, themed as _wb_th
        _wb_th(desc, lambda: f"color: {_wb_pal().text_secondary}; font-size: 10.5pt;")
        header.addWidget(title)
        header.addWidget(desc)
        shell.addLayout(header)

        settings_card = CardWidget(radius=10, padding=(16, 16, 16, 16), spacing=10)
        apply_card_shadow(settings_card)
        settings_layout = settings_card.createGridLayout()
        settings_layout.setHorizontalSpacing(14)
        settings_layout.setVerticalSpacing(10)
        output_label = QLabel("输出目录")
        _wb_th(output_label, lambda: f'font-size: 11pt; font-weight: 400; color: {_wb_pal().text_secondary};')
        self.output_dir_label = QLabel("跟随字幕视频所在目录")
        self.output_dir_label.setWordWrap(True)
        _wb_th(self.output_dir_label, lambda: f'font-size: 11pt; color: {_wb_pal().text_primary}; font-weight: 500;')
        ffmpeg_title = QLabel("FFmpeg 目录 ⓘ")
        ffmpeg_title.setToolTip('FFmpeg 目录、输出命名等偏好设置可在"设置"窗口中调整并保存到本地。')
        _wb_th(ffmpeg_title, lambda: f'font-size: 11pt; font-weight: 400; color: {_wb_pal().text_secondary};')
        self.hires_ffmpeg_label = QLabel(FFMPEG_DIR_PLACEHOLDER)
        self.hires_ffmpeg_label.setWordWrap(True)
        settings_button = QPushButton("⚙ 设置")
        _wb_th(settings_button, lambda: (
            "QPushButton {{"
            " background: transparent;"
            " border: 1px solid {border};"
            " border-radius: 6px;"
            " padding: 6px 14px;"
            " color: {color};"
            " font-size: 10.5pt;"
            "}}"
            "QPushButton:hover {{"
            " background: {hover};"
            "}}"
        ).format(
            border=_wb_pal().input_border,
            color=_wb_pal().text_secondary,
            hover=_wb_pal().secondary_button_hover_bg,
        ))
        settings_button.clicked.connect(lambda: self._open_settings_window("hires"))
        settings_layout.addWidget(output_label, 0, 0)
        settings_layout.addWidget(self.output_dir_label, 0, 1)
        settings_layout.addWidget(settings_button, 0, 2)
        settings_layout.setColumnStretch(1, 1)
        shell.addWidget(settings_card)

        card_row = QHBoxLayout()
        card_row.setContentsMargins(0, 0, 0, 0)
        card_row.setSpacing(12)
        self.video_zone = DropZoneCard(
            title="字幕视频",
            hint="支持 mkv / mp4 / mov / avi\n这里会决定输出文件名和输出目录。",
            extensions=VIDEO_EXTENSIONS,
            min_height=190,
            icon_text="🎬",
            placeholder_icon="🎞",
            accent_bg="#EEF4FF",
        )
        self.video_zone.browseRequested.connect(self._choose_video)
        self.video_zone.pathChanged.connect(self.set_video_path)

        self.on_vocal_zone = DropZoneCard(
            title="原唱音频",
            hint="支持 flac / wav / mp3 / m4a / aac / ape / alac / mkv / mp4\n可单独生成原唱 Hi-Res 视频，也可和伴奏一起生成。",
            extensions=HIRES_AUDIO_EXTENSIONS,
            min_height=190,
            icon_text="🎤",
            placeholder_icon="🎙",
            accent_bg="#F3EEFF",
        )
        self.on_vocal_zone.browseRequested.connect(self._choose_on_audio)
        self.on_vocal_zone.pathChanged.connect(self.set_on_vocal_path)

        self.off_vocal_zone = DropZoneCard(
            title="伴奏音频",
            hint="支持 flac / wav / mp3 / m4a / aac / ape / alac / mkv / mp4\n可放入多条伴奏，每条各出一个视频；也可只放原唱。",
            extensions=HIRES_AUDIO_EXTENSIONS,
            min_height=190,
            icon_text="🎵",
            placeholder_icon="♪",
            accent_bg="#EAF7F4",
            multiple=True,
        )
        self.off_vocal_zone.browseRequested.connect(self._choose_off_audio)
        # 不接 pathChanged -> set_off_vocal_path：多文件卡在拖放里已经把自己更新好了，
        # 再回写一次等于 set_path，会把整份列表塌成一条。
        for drop_zone in (self.video_zone, self.on_vocal_zone, self.off_vocal_zone):
            apply_card_shadow(drop_zone)

        card_row.addWidget(self.video_zone, 1)
        card_row.addWidget(self.on_vocal_zone, 1)
        card_row.addWidget(self.off_vocal_zone, 1)
        shell.addLayout(card_row)

        log_panel = CardWidget(radius=10, padding=(16, 16, 16, 16), spacing=12)
        apply_card_shadow(log_panel)
        log_layout = log_panel.createGridLayout()
        log_layout.setVerticalSpacing(12)
        log_title = QLabel("处理日志")
        log_title.setObjectName("PanelTitle")
        def _log_button_qss():
            p = _wb_pal()
            return (
                "QPushButton {{"
                " background: transparent; border: 0; border-radius: 6px;"
                " color: {color}; font-size: 12pt;"
                "}}"
                "QPushButton:hover {{ background: {hover}; }}"
            ).format(color=p.text_secondary, hover=p.secondary_button_hover_bg)
        copy_log_btn = QPushButton("📋")
        copy_log_btn.setFixedSize(28, 28)
        copy_log_btn.setToolTip("复制全部日志")
        _wb_th(copy_log_btn, _log_button_qss)
        copy_log_btn.clicked.connect(self._copy_hires_log)
        clear_log_btn = QPushButton("🗑")
        clear_log_btn.setFixedSize(28, 28)
        clear_log_btn.setToolTip("清空日志")
        _wb_th(clear_log_btn, _log_button_qss)
        self.hires_log = QPlainTextEdit()
        self.hires_log.setObjectName("LogText")
        self.hires_log.setReadOnly(True)
        clear_log_btn.clicked.connect(self.hires_log.clear)
        self.hires_log.setPlaceholderText("运行后将在此显示 FFmpeg 输出与处理进度...")
        _wb_th(self.hires_log, lambda: (
            "QPlainTextEdit#LogText {{"
            " background: {bg};"
            " border: 1px solid {border};"
            " border-radius: 8px;"
            " color: {color};"
            ' font-family: "Consolas", "JetBrains Mono", monospace;'
            " font-size: 10pt;"
            " padding: 10px;"
            "}}"
        ).format(
            bg=_wb_pal().log_bg,
            border=_wb_pal().input_border,
            color=_wb_pal().log_text,
        ))
        log_layout.addWidget(log_title, 0, 0)
        log_layout.addWidget(copy_log_btn, 0, 1)
        log_layout.addWidget(clear_log_btn, 0, 2)
        log_layout.addWidget(self.hires_log, 1, 0, 1, 3)
        log_layout.setColumnStretch(0, 1)
        log_layout.setRowStretch(1, 1)
        shell.addWidget(log_panel, 1)

        controls_bar = ControlBar()
        controls = controls_bar.createHBoxLayout()
        self.hires_start_button = PrimaryPushButton("▶  开始生成")
        self.hires_start_button.clicked.connect(self._start_hires)
        self.hires_cancel_button = QPushButton("■  取消生成")
        self.hires_cancel_button.setEnabled(False)
        self.hires_cancel_button.clicked.connect(self._stop_hires)
        clear_button = QPushButton("✕  清空已选文件")
        clear_button.clicked.connect(self._clear_hires_inputs)
        open_output_button = QPushButton("📁  打开输出目录")
        open_output_button.clicked.connect(self._open_hires_output_dir)
        self.hires_progress = QProgressBar()
        self.hires_progress.setRange(0, 1)
        self.hires_progress.setValue(0)
        self.hires_progress.setFixedWidth(220)
        self.hires_progress.setFixedHeight(10)
        self.hires_progress.setTextVisible(True)
        _wb_th(self.hires_progress, lambda: (
            "QProgressBar {{"
            " border: 0; border-radius: 5px;"
            " background: {bg}; text-align: center; color: transparent;"
            "}}"
            "QProgressBar::chunk {{ background: #2f6fed; border-radius: 5px; }}"
        ).format(bg=_wb_pal().progress_bg))
        self.hires_status_label = QLabel("准备就绪")
        # 由 ``_set_hires_status_color`` 在状态变化时单独驱动 —— 不挂 themed()，
        # 否则会把动态 success/error/processing 颜色覆盖回 idle 文字色。
        self._set_hires_status_color(None)
        controls.addWidget(self.hires_start_button)
        controls.addWidget(self.hires_cancel_button)
        controls.addWidget(clear_button)
        controls.addWidget(open_output_button)
        controls.addStretch(1)
        controls.addWidget(self.hires_progress)
        controls.addSpacing(12)
        controls.addWidget(self.hires_status_label)
        controls_bar.apply_button_metrics(self.hires_start_button, self.hires_cancel_button, clear_button, open_output_button)
        shell.addWidget(controls_bar)
        return page

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
        self._align_nudge_step = seconds
        if not hasattr(self, "align_step_small_button") or not hasattr(self, "align_step_large_button"):
            return
        from krok_helper.theme_workbench import palette as _wb_pal
        p = _wb_pal()
        button_map = {
            self.align_step_small_button: seconds == 0.01,
            self.align_step_large_button: seconds == 0.1,
        }
        for button, checked in button_map.items():
            button.setChecked(checked)
            button.setFont(build_app_ui_font(point_size=10.5, bold=checked))
            if checked:
                if p.is_dark:
                    qss = "background: #4A1A22; border: 1px solid #FF5A6F; color: #FF9CAB;"
                else:
                    qss = "background: #fff1f2; border: 1px solid #ff4d5e; color: #ff2947;"
            else:
                qss = (
                    f"background: {p.input_bg}; border: 1px solid {p.input_border};"
                    f" color: {p.text_primary};"
                )
            button.setStyleSheet(qss)

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
        if hasattr(self, "subtitle_accent_bar"):
            self.subtitle_accent_bar.setVisible(is_subtitle_target)
        if hasattr(self, "original_accent_bar"):
            self.original_accent_bar.setVisible(not is_subtitle_target)
        if hasattr(self, "subtitle_adjust_badge"):
            self.subtitle_adjust_badge.setEnabled(False)
        if hasattr(self, "original_adjust_badge"):
            self.original_adjust_badge.setEnabled(False)
        if not is_subtitle_target and hasattr(self, "align_lead_trim_radio"):
            self.align_lead_trim_radio.setChecked(False)
            if hasattr(self, "align_head_trim_row_widget"):
                self.align_head_trim_row_widget.setVisible(False)
            self.align_lead_trim_seconds_spin.setEnabled(False)
        self._apply_alignment_mode_styles()
        self._sync_alignment_export_buttons()
        self._refresh_alignment_export_panels()
        self._persist_alignment_preferences()

    def _set_panel_enabled(self, panel: QWidget, enabled: bool):
        for w in panel.findChildren(QWidget):
            w.setEnabled(enabled)

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

    def _load_settings_into_ui(self) -> None:
        self._loading_settings_into_ui = True
        self.set_ffmpeg_dir(Path(self.settings.ffmpeg_dir) if self.settings.ffmpeg_dir.strip() else None)
        self.set_output_name_mode(self.settings.output_name_mode)
        self.set_output_name_templates(self.settings.on_name_template, self.settings.off_name_template)
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
        self._restore_lyrics_preferences()
        self._loading_settings_into_ui = False

    def _restore_lyrics_preferences(self) -> None:
        saved_source_ids = tuple(str(item) for item in (self.settings.lyrics_source_ids or DEFAULT_LYRICS_PROVIDER_IDS) if str(item))
        if not saved_source_ids:
            saved_source_ids = DEFAULT_LYRICS_PROVIDER_IDS
        for index, (label, provider_ids) in enumerate(LYRICS_SOURCE_OPTIONS):
            if provider_ids == saved_source_ids:
                self.lyrics_source_combo.setCurrentIndex(index)
                break

        saved_preview_mode = str(self.settings.lyrics_preview_mode or LYRICS_PREVIEW_LINE)
        for index, (label, mode) in enumerate(LYRICS_PREVIEW_MODE_OPTIONS):
            if mode == saved_preview_mode:
                self.lyrics_preview_mode_combo.setCurrentIndex(index)
                break
        saved_language = str(self.settings.lyrics_language or LYRICS_LANGUAGE_ORIGINAL)
        for index, (label, value) in enumerate(LYRICS_LANGUAGE_OPTIONS):
            if value == saved_language:
                self.lyrics_language_combo.setCurrentIndex(index)
                break
        self.lyrics_strip_intro_checkbox.setChecked(bool(self.settings.lyrics_strip_intro_lines))

    def _install_single_click_combo_behavior(self, combo: QComboBox) -> None:
        popup_view = getattr(combo, "view", None)
        if not callable(popup_view):
            return
        view = popup_view()
        if view is None:
            return
        view.pressed.connect(lambda index, combo=combo: self._handle_combo_popup_pressed(combo, index.row()))

    def _handle_combo_popup_pressed(self, combo: QComboBox, row: int) -> None:
        if row < 0 or row >= combo.count():
            return
        combo.setCurrentIndex(row)
        hide_popup = getattr(combo, "hidePopup", None)
        if callable(hide_popup):
            hide_popup()

    def _current_lyrics_source_ids(self) -> tuple[str, ...]:
        return LYRICS_SOURCE_MAP.get(self.lyrics_source_combo.currentText(), DEFAULT_LYRICS_PROVIDER_IDS)

    def _current_lyrics_preview_mode(self) -> str:
        return LYRICS_PREVIEW_MODE_MAP.get(self.lyrics_preview_mode_combo.currentText(), LYRICS_PREVIEW_LINE)

    def _current_lyrics_language(self) -> str:
        return LYRICS_LANGUAGE_MAP.get(self.lyrics_language_combo.currentText(), LYRICS_LANGUAGE_ORIGINAL)

    def _update_lyrics_language_combo_state(self, candidate: LyricsSearchCandidate | None) -> None:
        combo = getattr(self, "lyrics_language_combo", None)
        if combo is None:
            return
        has_translation = bool(candidate is not None and candidate.has_translation)
        translation_index = next(
            (i for i, (_label, value) in enumerate(LYRICS_LANGUAGE_OPTIONS) if value == LYRICS_LANGUAGE_TRANSLATION),
            -1,
        )
        if translation_index >= 0:
            set_item_enabled = getattr(combo, "setItemEnabled", None)
            if callable(set_item_enabled):
                set_item_enabled(translation_index, has_translation)
            else:
                # Fallback for plain QComboBox / future swap.
                model = combo.model() if hasattr(combo, "model") else None
                item = model.item(translation_index) if model is not None and hasattr(model, "item") else None
                if item is not None:
                    flags = item.flags()
                    if has_translation:
                        item.setFlags(flags | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                    else:
                        item.setFlags(flags & ~(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable))
        # If user has selected translation but the currently loaded song doesn't
        # have one, silently fall back to original *without* persisting, so the
        # saved preference is preserved for the next song that does have a
        # translation.
        if (
            candidate is not None
            and candidate.lyrics_loaded
            and not has_translation
            and self._current_lyrics_language() == LYRICS_LANGUAGE_TRANSLATION
        ):
            previous = self._loading_settings_into_ui
            self._loading_settings_into_ui = True
            try:
                for index, (_label, value) in enumerate(LYRICS_LANGUAGE_OPTIONS):
                    if value == LYRICS_LANGUAGE_ORIGINAL:
                        combo.setCurrentIndex(index)
                        break
            finally:
                self._loading_settings_into_ui = previous

    def _persist_lyrics_preferences(self, *_args) -> None:
        if self._loading_settings_into_ui:
            return
        source_ids = self._current_lyrics_source_ids()
        preview_mode = self._current_lyrics_preview_mode()
        language = self._current_lyrics_language()
        self.settings.lyrics_source_ids = tuple(source_ids)
        self.settings.lyrics_preview_mode = preview_mode
        self.settings.lyrics_language = language
        self.settings.lyrics_strip_intro_lines = self.lyrics_strip_intro_checkbox.isChecked()
        save_app_settings(self.settings)

    def _persist_alignment_preferences(self, *_args) -> None:
        if self._loading_settings_into_ui:
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

    def _sync_ffmpeg_labels(self) -> None:
        self.hires_ffmpeg_label.setText(self.ffmpeg_dir_text or FFMPEG_DIR_PLACEHOLDER)
        self._refresh_media_info_labels()

    def set_video_path(self, path: Path) -> None:
        self.video_zone.set_path(path)
        self.output_dir_label.setText(str(resolve_output_dir(path)))

    def set_on_vocal_path(self, path: Path) -> None:
        self.on_vocal_zone.set_path(path)

    def set_off_vocal_path(self, path: Path) -> None:
        self.off_vocal_zone.set_path(path)

    def add_off_vocal_paths(self, paths: Sequence[Path]) -> list[Path]:
        """追加伴奏（不覆盖已有的）；音频分离的转交也走这里。"""
        return self.off_vocal_zone.add_paths(list(paths))

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

    def set_ffmpeg_dir(self, path: Path | None) -> None:
        self.ffmpeg_dir_text = str(path) if path is not None else ""
        self._sync_ffmpeg_labels()
        self._sync_lyrics_timing_host_paths()

    def _notify_settings_corruption_if_any(self) -> None:
        """启动后若检测到上次 settings.json 损坏，弹一个红框告知用户。

        ``load_app_settings`` 在解析失败时会把坏文件备份成 ``settings.json.corrupt-<ts>``
        并把路径记到 :func:`consume_corruption_backup`；这里把它取出来展示，让
        v3.0.x 那种「全空配置」事故不再被用户默默吃掉。
        """
        backup = getattr(self, "_settings_corruption_backup", None)
        if backup is None:
            return
        self._settings_corruption_backup = None
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Icon.Warning)
        msg.setWindowTitle(APP_TITLE)
        msg.setText("检测到上次的配置文件 settings.json 损坏，已使用默认值重建。")
        msg.setInformativeText(
            f"原文件已备份到：\n{backup}\n\n"
            "打轴模块的设置 / 词典 / 演唱者 / 网络词典缓存如丢失，可在「全局设置 → "
            "工具 → 打轴模块数据导入」从原 StrangeUtaGame 目录或备份中恢复。"
        )
        msg.setStandardButtons(QMessageBox.StandardButton.Ok)
        msg.exec()

    def _check_lyrics_timing_crash_recovery(self) -> None:
        """启动时让嵌入的歌词打轴模块检查闪退恢复文件。

        若发现待恢复的临时文件，先切到歌词打轴模块（让用户看到上下文），
        再弹出"是否恢复"对话框。用户拒绝时停留在该模块，由用户自行返回。
        """
        page = getattr(self, "lyrics_timing_page", None)
        if page is None or not hasattr(page, "check_crash_recovery"):
            return
        try:
            has_pending = bool(
                hasattr(page, "has_pending_crash_recovery")
                and page.has_pending_crash_recovery()
            )
        except Exception:
            return
        if not has_pending:
            return
        self._show_module(WORKFLOW_LYRICS_TIMING)
        try:
            page.check_crash_recovery(dialog_parent=self)
        except Exception:
            pass

    def _check_subtitle_render_crash_recovery(self) -> None:
        """Let the embedded subtitle module handle pending recovery snapshots."""
        page = getattr(self, "subtitle_render_page", None)
        if page is None or not hasattr(page, "check_crash_recovery"):
            return
        try:
            has_pending = bool(
                hasattr(page, "has_pending_crash_recovery")
                and page.has_pending_crash_recovery()
            )
        except Exception:
            logging.getLogger(__name__).warning(
                "检查字幕项目恢复数据失败", exc_info=True
            )
            return
        if not has_pending:
            return
        self._show_module(WORKFLOW_SUBTITLE_RENDER)
        try:
            page.check_crash_recovery(dialog_parent=self)
        except Exception:
            logging.getLogger(__name__).warning(
                "处理字幕项目恢复数据失败", exc_info=True
            )

    def _sync_lyrics_timing_host_paths(self) -> None:
        """Inject host-managed runtime paths into the embedded timing module."""
        cache_dir = get_settings_path().parent / "lyrics_timing_cache"
        os.environ["SUG_CACHE_DIR"] = str(cache_dir)

        ffmpeg_dir = Path(self.ffmpeg_dir_text).expanduser() if self.ffmpeg_dir_text.strip() else None
        try:
            ffmpeg_path = find_tool("ffmpeg", ffmpeg_dir)
        except ProcessingError:
            if os.name == "nt":
                try:
                    ffmpeg_path = find_tool("ffmpeg.exe", ffmpeg_dir)
                except ProcessingError:
                    ffmpeg_path = "ffmpeg"
            else:
                ffmpeg_path = "ffmpeg"

        tools = self.settings.lyrics_timing.setdefault("tools", {})
        if isinstance(tools, dict):
            tools["ffmpeg_path"] = ffmpeg_path

    def set_output_name_mode(self, mode: str) -> None:
        if mode == OUTPUT_NAME_MODE_VIDEO_NAME:
            mode = OUTPUT_NAME_MODE_TEMPLATE
            self.set_output_name_templates(DEFAULT_ON_NAME_TEMPLATE, DEFAULT_OFF_NAME_TEMPLATE)
        if mode not in {OUTPUT_NAME_MODE_FIXED, OUTPUT_NAME_MODE_TEMPLATE}:
            raise ProcessingError(f"不支持的输出命名模式: {mode}")
        self.output_name_mode_value = mode

    def set_output_name_templates(self, on_template: str, off_template: str) -> None:
        self.on_name_template_value = on_template
        self.off_name_template_value = off_template

    def set_alignment_output_dir_settings(self, mode: str, custom_dir: str) -> None:
        if mode not in {ALIGN_OUTPUT_DIR_SOURCE_VIDEO, ALIGN_OUTPUT_DIR_CUSTOM}:
            raise ProcessingError("对齐输出位置无效，请重新选择。")
        self.align_output_dir_mode_value = mode
        self.align_output_custom_dir_text = custom_dir.strip()

    def _choose_video(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择字幕视频", "", "视频文件 (*.mkv *.mp4 *.mov *.avi);;所有文件 (*.*)")
        if path:
            self.set_video_path(Path(path))

    def _choose_on_audio(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择原唱音频",
            "",
            "音频文件 (*.flac *.wav *.mp3 *.m4a *.aac *.ape *.alac *.mkv *.mp4);;所有文件 (*.*)",
        )
        if path:
            self.set_on_vocal_path(Path(path))

    def _choose_off_audio(self) -> None:
        # 伴奏可以有多条，每条各出一个混流视频，所以这里允许多选。
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "选择伴奏音频（可多选）",
            "",
            "音频文件 (*.flac *.wav *.mp3 *.m4a *.aac *.ape *.alac *.mkv *.mp4);;所有文件 (*.*)",
        )
        if paths:
            self.add_off_vocal_paths([Path(item) for item in paths])

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

    def _build_media_info(self, path: Path | None, label: str) -> str:
        if path is None:
            return f"{label}: 时长未知"
        cache_key = path.expanduser()
        cached_duration = self._media_duration_cache.get(cache_key)
        if cached_duration is not None:
            return f"{label}: {cached_duration}"
        try:
            ffprobe_path = find_tool("ffprobe.exe", self._resolve_ffmpeg_dir())
            info = probe_media(ffprobe_path, path)
        except Exception:  # noqa: BLE001
            return f"{label}: 时长未知"
        duration_text = format_media_duration(info.duration)
        self._media_duration_cache[cache_key] = duration_text
        return f"{label}: {duration_text}"

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

    def _open_settings_window(self, context: str) -> None:
        dialog = ModelessDialog(self)
        title = "波形对齐设置" if context == "align" else "Hi-Res 生成设置"
        dialog.setWindowTitle(f"{APP_TITLE} - {title}")
        dialog.resize(860, 540)

        outer = QVBoxLayout(dialog)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea(dialog)
        scroll.setWidgetResizable(True)
        content = QWidget()
        scroll.setWidget(content)
        outer.addWidget(scroll)

        shell = QVBoxLayout(content)
        shell.setContentsMargins(20, 20, 20, 20)
        shell.setSpacing(18)

        heading = QLabel(title)
        heading.setStyleSheet('font-family: "Microsoft YaHei UI"; font-size: 18pt; font-weight: 700;')
        shell.addWidget(heading)

        status_label = QLabel("")
        from krok_helper.theme_workbench import palette as _wb_pal, themed as _wb_th
        _wb_th(status_label, lambda: f'font-family: "Microsoft YaHei UI"; font-size: 9pt; color: {_wb_pal().text_hint};')

        if context == "align":
            naming_panel = QFrame()
            naming_panel.setObjectName("WhitePanel")
            naming_layout = QGridLayout(naming_panel)
            naming_layout.setContentsMargins(14, 14, 14, 14)
            naming_title = QLabel("对齐导出命名")
            naming_title.setObjectName("PanelTitle")
            video_template_edit = QLineEdit(dialog)
            video_template_edit.setText(self.align_video_name_template_value)
            audio_template_edit = QLineEdit(dialog)
            audio_template_edit.setText(self.align_audio_name_template_value)
            naming_help_1 = QLabel("默认: 对齐后视频 {video_name}_aligned.mp4；对齐后音频 {audio_name}_aligned.wav。")
            from krok_helper.theme_workbench import palette as _wb_pal, themed as _wb_th
            _wb_th(naming_help_1, lambda: f'font-family: "Microsoft YaHei UI"; font-size: 9pt; color: {_wb_pal().text_hint};')
            naming_help_2 = QLabel("视频模板支持 {video_name}；音频模板支持 {audio_name} 和 {video_name}。不用写扩展名。")
            from krok_helper.theme_workbench import palette as _wb_pal, themed as _wb_th
            _wb_th(naming_help_2, lambda: f'font-family: "Microsoft YaHei UI"; font-size: 9pt; color: {_wb_pal().text_hint};')
            naming_layout.addWidget(naming_title, 0, 0)
            naming_layout.addWidget(QLabel("对齐后视频模板"), 1, 0)
            naming_layout.addWidget(video_template_edit, 1, 1)
            naming_layout.addWidget(QLabel("对齐后音频模板"), 2, 0)
            naming_layout.addWidget(audio_template_edit, 2, 1)
            naming_layout.addWidget(naming_help_1, 3, 1)
            naming_layout.addWidget(naming_help_2, 4, 1)
            naming_layout.setColumnStretch(1, 1)
            shell.addWidget(naming_panel)

            output_panel = QFrame()
            output_panel.setObjectName("WhitePanel")
            output_layout = QGridLayout(output_panel)
            output_layout.setContentsMargins(14, 14, 14, 14)
            output_layout.setHorizontalSpacing(10)
            output_layout.setVerticalSpacing(10)
            output_title = QLabel("对齐导出位置")
            output_title.setObjectName("PanelTitle")
            output_mode_group = QButtonGroup(dialog)
            output_source_radio = QRadioButton("保存在字幕视频所在目录")
            output_custom_radio = QRadioButton("保存在指定目录")
            output_mode_group.addButton(output_source_radio)
            output_mode_group.addButton(output_custom_radio)
            if self.align_output_dir_mode_value == ALIGN_OUTPUT_DIR_CUSTOM:
                output_custom_radio.setChecked(True)
            else:
                output_source_radio.setChecked(True)
            output_dir_edit = QLineEdit(dialog)
            output_dir_edit.setReadOnly(True)
            output_dir_edit.setPlaceholderText("点击选择保存文件夹")
            output_dir_edit.setText(self.align_output_custom_dir_text)
            output_dir_button = QPushButton("选择文件夹")

            def choose_align_output_dir() -> None:
                init_dir = output_dir_edit.text().strip()
                if not init_dir and self.align_video_zone.path is not None:
                    init_dir = str(self.align_video_zone.path.parent)
                if not init_dir:
                    init_dir = str(Path.home())
                path = QFileDialog.getExistingDirectory(dialog, "选择对齐导出保存目录", init_dir)
                if path:
                    output_dir_edit.setText(path)
                    output_custom_radio.setChecked(True)
                    sync_align_output_dir_enabled()

            def sync_align_output_dir_enabled() -> None:
                enabled = output_custom_radio.isChecked()
                output_dir_edit.setEnabled(enabled)
                output_dir_button.setEnabled(enabled)

            output_source_radio.toggled.connect(lambda _checked: sync_align_output_dir_enabled())
            output_custom_radio.toggled.connect(lambda _checked: sync_align_output_dir_enabled())
            output_dir_button.clicked.connect(choose_align_output_dir)
            output_dir_edit.mousePressEvent = lambda event: choose_align_output_dir() if output_custom_radio.isChecked() else None
            sync_align_output_dir_enabled()

            output_help = QLabel("导出时会以这里作为另存为窗口的默认目录。")
            from krok_helper.theme_workbench import palette as _wb_pal, themed as _wb_th
            _wb_th(output_help, lambda: f'font-family: "Microsoft YaHei UI"; font-size: 9pt; color: {_wb_pal().text_hint};')
            output_dir_row = QHBoxLayout()
            output_dir_row.setContentsMargins(0, 0, 0, 0)
            output_dir_row.setSpacing(8)
            output_dir_row.addWidget(output_dir_edit, 1)
            output_dir_row.addWidget(output_dir_button)
            output_layout.addWidget(output_title, 0, 0)
            output_layout.addWidget(output_source_radio, 1, 1)
            output_layout.addWidget(output_custom_radio, 2, 1)
            output_layout.addWidget(QLabel("指定目录"), 3, 0)
            output_layout.addLayout(output_dir_row, 3, 1)
            output_layout.addWidget(output_help, 4, 1)
            output_layout.setColumnStretch(1, 1)
            shell.addWidget(output_panel)
        else:
            naming_panel = QFrame()
            naming_panel.setObjectName("WhitePanel")
            naming_layout = QGridLayout(naming_panel)
            naming_layout.setContentsMargins(14, 14, 14, 14)
            naming_title = QLabel("输出命名")
            naming_title.setObjectName("PanelTitle")
            mode_group = QButtonGroup(dialog)
            fixed_radio = QRadioButton("默认命名: on_vocal.mkv / off_vocal.mkv")
            template_radio = QRadioButton("自定义模板: 使用你自己的命名样式")
            mode_group.addButton(fixed_radio)
            mode_group.addButton(template_radio)
            if self.output_name_mode_value == OUTPUT_NAME_MODE_TEMPLATE:
                template_radio.setChecked(True)
            else:
                fixed_radio.setChecked(True)
            on_template_edit = QLineEdit(dialog)
            on_template_edit.setText(self.on_name_template_value)
            off_template_edit = QLineEdit(dialog)
            off_template_edit.setText(self.off_name_template_value)

            def sync_template_enabled() -> None:
                enabled = template_radio.isChecked()
                on_template_edit.setEnabled(enabled)
                off_template_edit.setEnabled(enabled)

            fixed_radio.toggled.connect(lambda _checked: sync_template_enabled())
            template_radio.toggled.connect(lambda _checked: sync_template_enabled())
            sync_template_enabled()

            naming_help_1 = QLabel(
                "支持占位符 {video_name} 和 {audio_name}（这条音频自己的文件名）。"
                "不用写 .mkv。示例: {video_name}_karaoke_on"
            )
            naming_help_1.setWordWrap(True)
            from krok_helper.theme_workbench import palette as _wb_pal, themed as _wb_th
            _wb_th(naming_help_1, lambda: f'font-family: "Microsoft YaHei UI"; font-size: 9pt; color: {_wb_pal().text_hint};')
            naming_help_2 = QLabel(
                "默认: 原唱 on_vocal.mkv；伴奏 off_vocal.mkv。\n"
                "放多条伴奏时每条各出一个视频：模板里写了 {audio_name} 就按你写的位置放，"
                "没写则自动在末尾补上音频文件名以免重名。"
            )
            naming_help_2.setWordWrap(True)
            from krok_helper.theme_workbench import palette as _wb_pal, themed as _wb_th
            _wb_th(naming_help_2, lambda: f'font-family: "Microsoft YaHei UI"; font-size: 9pt; color: {_wb_pal().text_hint};')
            naming_layout.addWidget(naming_title, 0, 0)
            naming_layout.addWidget(fixed_radio, 1, 1)
            naming_layout.addWidget(template_radio, 2, 1)
            naming_layout.addWidget(QLabel("原唱模板"), 3, 0)
            naming_layout.addWidget(on_template_edit, 3, 1)
            naming_layout.addWidget(QLabel("伴奏模板"), 4, 0)
            naming_layout.addWidget(off_template_edit, 4, 1)
            naming_layout.addWidget(naming_help_1, 5, 1)
            naming_layout.addWidget(naming_help_2, 6, 1)
            naming_layout.setColumnStretch(1, 1)
            shell.addWidget(naming_panel)

        shell.addWidget(status_label)

        controls = QHBoxLayout()
        controls.addStretch(1)
        save_button = QPushButton("保存设置")
        close_button = QPushButton("关闭")
        close_button.clicked.connect(dialog.close)

        def save_settings_from_dialog() -> None:
            try:
                mode = self.output_name_mode_value
                on_template = self.on_name_template_value
                off_template = self.off_name_template_value
                align_video_template = self.align_video_name_template_value
                align_audio_template = self.align_audio_name_template_value
                align_output_dir_mode = self.align_output_dir_mode_value
                align_output_custom_dir = self.align_output_custom_dir_text
                if context == "align":
                    align_video_template = video_template_edit.text().strip() or DEFAULT_ALIGNED_VIDEO_NAME_TEMPLATE
                    align_audio_template = audio_template_edit.text().strip() or DEFAULT_ALIGNED_AUDIO_NAME_TEMPLATE
                    align_output_dir_mode = (
                        ALIGN_OUTPUT_DIR_CUSTOM
                        if output_custom_radio.isChecked()
                        else ALIGN_OUTPUT_DIR_SOURCE_VIDEO
                    )
                    align_output_custom_dir = output_dir_edit.text().strip()
                else:
                    mode = OUTPUT_NAME_MODE_TEMPLATE if template_radio.isChecked() else OUTPUT_NAME_MODE_FIXED
                    # 仅在选择「自定义模板」时才采用对话框里填写的模板；保存为「默认命名」
                    # 时保持既有的已校验模板值不变，避免未经路径合法性校验的输入被写入 settings。
                    if mode == OUTPUT_NAME_MODE_TEMPLATE:
                        on_template = on_template_edit.text().strip() or DEFAULT_ON_NAME_TEMPLATE
                        off_template = off_template_edit.text().strip() or DEFAULT_OFF_NAME_TEMPLATE

                saved_path = self._save_settings_payload(
                    output_name_mode=mode,
                    on_template=on_template,
                    off_template=off_template,
                    align_video_template=align_video_template,
                    align_audio_template=align_audio_template,
                    align_output_dir_mode=align_output_dir_mode,
                    align_output_custom_dir=align_output_custom_dir,
                    ffmpeg_dir_text=self.ffmpeg_dir_text,
                )
            except ProcessingError as exc:
                QMessageBox.critical(dialog, APP_TITLE, str(exc))
                return

            status_label.setText("设置已保存到本地。")
            QMessageBox.information(dialog, APP_TITLE, f"设置已保存：\n{saved_path}")

        save_button.clicked.connect(save_settings_from_dialog)
        controls.addWidget(save_button)
        controls.addWidget(close_button)
        shell.addLayout(controls)
        dialog.exec()

    def _choose_ffmpeg_for_dialog(self, parent: QWidget, target: QLineEdit) -> None:
        path = QFileDialog.getExistingDirectory(parent, "选择 ffmpeg 所在目录")
        if path:
            target.setText(path)

    def _import_legacy_sug_for_dialog(self, parent: QWidget) -> None:
        path = QFileDialog.getExistingDirectory(
            parent, "选择旧版 StrangeUtaGame 数据目录（含 config.json / dictionary.json 等）"
        )
        if not path:
            return
        src = Path(path)
        # 二次确认（主 config 是覆盖，词典/演唱者是合并）
        if not ask_fluent_confirm(
            parent,
            "将从该目录导入旧版 SUG 数据到工作台：\n\n"
            f"  {src}\n\n"
            "• 主配置：未知项会被忽略，缺失项使用默认值，整体覆盖现有配置\n"
            "• 词典 / 演唱者：按名称合并，工作台已有的同名条目优先保留\n"
            "• 网络词典缓存：整体覆盖\n\n"
            "是否继续？",
            yes_text="导入",
        ):
            return

        try:
            report = import_legacy_sug_settings(src, self.settings)
            save_app_settings(self.settings)
        except Exception as exc:
            show_fluent_info(parent, f"导入失败：\n{exc}")
            return

        lines: list[str] = []
        if report["imported"]:
            lines.append("已导入：" + "、".join(report["imported"]))
        if report["missing"]:
            lines.append("未找到（已跳过）：" + "、".join(report["missing"]))
        if report["added_dict_entries"]:
            lines.append(f"词典新增条目：{report['added_dict_entries']}")
        if report["added_singers"]:
            lines.append(f"演唱者新增：{report['added_singers']}")
        if report["skipped_unknown_keys"]:
            sample = "、".join(report["skipped_unknown_keys"][:5])
            extra = f"…（共 {len(report['skipped_unknown_keys'])} 个）" if len(report["skipped_unknown_keys"]) > 5 else ""
            lines.append(f"主配置忽略未知项：{sample}{extra}")
        if report["errors"]:
            lines.append("以下文件解析失败：" + "、".join(name for name, _ in report["errors"]))
        if not lines:
            lines.append("该目录下未找到任何可导入的 SUG 数据文件。")
        else:
            lines.append("")
            lines.append("请重启工作台让打轴模块重新加载新设置。")

        show_fluent_info(parent, "\n".join(lines))

    def _open_global_settings_window(self) -> None:
        updater_settings = ensure_updater_settings(self.settings)
        dialog = ModelessDialog(self)
        dialog.setWindowTitle(f"{APP_TITLE} - 全局设置")
        dialog.resize(880, 640)
        dialog.setMinimumSize(820, 560)

        outer = QVBoxLayout(dialog)
        outer.setContentsMargins(26, 20, 26, 18)
        outer.setSpacing(8)

        header = QWidget(dialog)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(10)

        heading = SubtitleLabel("全局设置", dialog)
        save_button = PrimaryPushButton(FIF.SAVE, "保存设置", dialog)
        close_button = QPushButton("关闭", dialog)
        close_button.clicked.connect(dialog.close)

        header_layout.addWidget(heading, 0, Qt.AlignmentFlag.AlignVCenter)
        header_layout.addStretch(1)
        header_layout.addWidget(save_button, 0, Qt.AlignmentFlag.AlignVCenter)
        header_layout.addWidget(close_button, 0, Qt.AlignmentFlag.AlignVCenter)
        outer.addWidget(header)

        pivot = Pivot(dialog)
        outer.addWidget(pivot)

        settings_stack = QStackedWidget(dialog)
        outer.addWidget(settings_stack, 1)

        # ── 界面 → 主题 ────────────────────────────────────────────────
        # 实时预览：变更 ComboBox 后延迟推 ``theme.mode``。不能在
        # currentIndexChanged 同步链里直接切主题，因为此时 qfluentwidgets 的
        # 下拉 popup 可能还没收尾，主题切换会触发全树 polish，容易和 popup
        # 销毁/重绘撞 native crash。
        from krok_helper.theme_workbench import theme as wb_theme, ThemeMode as WBThemeMode
        from krok_helper.settings import (
            UI_THEME_AUTO as _T_AUTO,
            UI_THEME_LIGHT as _T_LIGHT,
            UI_THEME_DARK as _T_DARK,
        )

        theme_group = SettingCardGroup("外观", dialog)
        theme_card = SettingCard(
            FIF.PALETTE,
            "界面主题",
            "自动跟随系统在 Win10/Win11 上均生效；强制浅色 / 深色会覆盖系统 Mica 材质。",
            theme_group,
        )
        theme_combo = StyledComboBox(theme_card)
        _theme_options = [("跟随系统", _T_AUTO), ("浅色", _T_LIGHT), ("深色", _T_DARK)]
        theme_combo.addItems([label for label, _v in _theme_options])
        _initial_theme = getattr(self.settings, "ui_theme", _T_AUTO)
        for _i, (_lbl, _val) in enumerate(_theme_options):
            if _val == _initial_theme:
                theme_combo.setCurrentIndex(_i)
                break
        theme_combo.setMinimumWidth(150)
        self._install_single_click_combo_behavior(theme_combo)
        add_setting_card_actions(theme_card, theme_combo)
        theme_group.addSettingCard(theme_card)

        def _selected_theme_value() -> str:
            idx = theme_combo.currentIndex()
            return _theme_options[idx][1] if 0 <= idx < len(_theme_options) else _T_AUTO

        def _theme_mode_for_value(value: str) -> WBThemeMode:
            return {
                _T_AUTO: WBThemeMode.AUTO,
                _T_LIGHT: WBThemeMode.LIGHT,
                _T_DARK: WBThemeMode.DARK,
            }.get(value, WBThemeMode.AUTO)

        _pending_theme_value: list[str] = [_initial_theme]

        def _apply_theme_combo_preview() -> None:
            value = _pending_theme_value[0]
            if self.settings.ui_theme != value:
                self.settings.ui_theme = value
                try:
                    save_app_settings(self.settings)
                except Exception:
                    import logging
                    logging.getLogger(__name__).warning("保存界面主题设置失败", exc_info=True)
            target = _theme_mode_for_value(value)
            if wb_theme.mode != target:
                wb_theme.mode = target

        def _on_theme_combo_changed(_idx: int) -> None:
            _pending_theme_value[0] = _selected_theme_value()
            from krok_helper.theme_workbench import schedule_theme_refresh
            schedule_theme_refresh(
                self,
                _apply_theme_combo_preview,
                timer_attr="_global_settings_theme_preview_timer",
            )

        theme_combo.currentIndexChanged.connect(_on_theme_combo_changed)
        # 关闭时若未保存则回退。回退同样延迟到 dialog 关闭事件之后。

        tools_group = SettingCardGroup("外部工具", dialog)
        ffmpeg_card = SettingCard(
            FIF.FOLDER,
            "FFmpeg 目录",
            "推荐选择 ffmpeg 的 bin 目录，例如 D:\\tools\\ffmpeg\\bin。"
            "波形对齐、Hi-Res 混流和嵌入式打轴模块都会使用这里的设置。",
            tools_group,
        )
        ffmpeg_display = QLineEdit(ffmpeg_card)
        ffmpeg_display.setText(self.ffmpeg_dir_text)
        ffmpeg_display.setPlaceholderText(FFMPEG_DIR_PLACEHOLDER)
        ffmpeg_display.setMinimumWidth(260)
        choose_button = QPushButton("选择目录", ffmpeg_card)
        choose_button.clicked.connect(lambda: self._choose_ffmpeg_for_dialog(dialog, ffmpeg_display))
        system_button = QPushButton("使用系统 PATH", ffmpeg_card)
        system_button.clicked.connect(lambda: ffmpeg_display.setText(""))
        add_setting_card_actions(ffmpeg_card, ffmpeg_display, choose_button, system_button)
        tools_group.addSettingCard(ffmpeg_card)

        migrate_group = SettingCardGroup("数据迁移", dialog)
        import_card = PushSettingCard(
            "选择旧版 SUG 数据目录…",
            FIF.DOWNLOAD,
            "打轴模块数据导入",
            "导入旧版 StrangeUtaGame standalone 的设置、词典、演唱者和网络词典缓存。"
            "主配置未知项会被忽略，缺失项使用默认值；词典 / 演唱者按名称去重合并，"
            "工作台已有的同名条目优先保留。导入后请重启工作台让设置生效。",
            migrate_group,
        )
        import_card.clicked.connect(lambda: self._import_legacy_sug_for_dialog(dialog))
        migrate_group.addSettingCard(import_card)

        proxy_group = SettingCardGroup("网络与代理", dialog)

        proxy_mode_card = SettingCard(
            FIF.GLOBE,
            "代理模式",
            "选择访问 GitHub 更新源与下载文件时使用的代理方式。",
            proxy_group,
        )
        proxy_combo = StyledComboBox(proxy_mode_card)
        proxy_options = [("使用系统代理", "system"), ("自动检测代理", "auto"), ("不使用代理", "off"), ("手动指定代理", "manual")]
        proxy_combo.addItems([label for label, _value in proxy_options])
        for index, (_label, value) in enumerate(proxy_options):
            if value == updater_settings.proxy_mode:
                proxy_combo.setCurrentIndex(index)
                break
        proxy_combo.setMinimumWidth(170)
        self._install_single_click_combo_behavior(proxy_combo)
        add_setting_card_actions(proxy_mode_card, proxy_combo)
        proxy_group.addSettingCard(proxy_mode_card)

        proxy_manual_card = SettingCard(
            FIF.LINK,
            "手动代理地址",
            "仅「手动指定代理」模式生效，例如 http://127.0.0.1:7890。",
            proxy_group,
        )
        proxy_manual_edit = QLineEdit(proxy_manual_card)
        proxy_manual_edit.setText(updater_settings.proxy_manual_url)
        proxy_manual_edit.setPlaceholderText("http://127.0.0.1:7890")
        proxy_manual_edit.setMinimumWidth(240)
        add_setting_card_actions(proxy_manual_card, proxy_manual_edit)
        proxy_group.addSettingCard(proxy_manual_card)

        proxy_status_card = SettingCard(
            FIF.WIFI,
            "当前生效代理",
            "正在解析代理…",
            proxy_group,
        )
        proxy_status_label = proxy_status_card.contentLabel
        auto_detect_button = QPushButton("自动检测", proxy_status_card)
        test_proxy_button = QPushButton("测试连通性", proxy_status_card)
        add_setting_card_actions(proxy_status_card, auto_detect_button, test_proxy_button)
        proxy_group.addSettingCard(proxy_status_card)

        def current_proxy_mode() -> str:
            return proxy_options[proxy_combo.currentIndex()][1]

        def resolve_proxy_status() -> tuple[str, dict[str, str] | None]:
            try:
                from krok_helper.network import resolve_proxy

                info, proxies = resolve_proxy(current_proxy_mode(), proxy_manual_edit.text().strip())
                if info is not None and info.is_valid:
                    source_names = {"system": "系统代理", "scan": "自动检测", "manual": "手动代理"}
                    return f"当前生效代理: {info.url}（{source_names.get(info.source, info.source or '代理')}）", proxies
                if current_proxy_mode() == "off":
                    return "当前不使用代理。", None
                return "当前未检测到可用代理。", None
            except Exception as exc:  # noqa: BLE001
                return f"代理解析失败: {exc}", None

        def refresh_proxy_status() -> None:
            text, _proxies = resolve_proxy_status()
            proxy_status_label.setText(text)
            relax_setting_card_height(proxy_status_card, min_content_lines=2)

        def auto_detect_proxy() -> None:
            previous_index = proxy_combo.currentIndex()
            for index, (_label, value) in enumerate(proxy_options):
                if value == "auto":
                    proxy_combo.setCurrentIndex(index)
                    break
            text, _proxies = resolve_proxy_status()
            proxy_status_label.setText(text)
            relax_setting_card_height(proxy_status_card, min_content_lines=2)
            if "未检测到" in text:
                proxy_combo.setCurrentIndex(previous_index)

        def test_proxy_connectivity() -> None:
            proxy_status_label.setText("正在测试 GitHub 连通性…")
            relax_setting_card_height(proxy_status_card, min_content_lines=2)
            QApplication.processEvents()
            try:
                from krok_helper.updater.worker import probe_github_connectivity

                _ok, message = probe_github_connectivity(
                    current_proxy_mode(),
                    proxy_manual_edit.text().strip(),
                )
                proxy_status_label.setText(message)
            except Exception as exc:  # noqa: BLE001
                proxy_status_label.setText(f"连通性测试失败: {exc}")
            relax_setting_card_height(proxy_status_card, min_content_lines=2)

        auto_detect_button.clicked.connect(auto_detect_proxy)
        test_proxy_button.clicked.connect(test_proxy_connectivity)

        update_group = SettingCardGroup("应用更新", dialog)

        auto_update_card = SettingCard(
            FIF.UPDATE,
            "启用工作台自动更新",
            "关闭后工作台不再自动检查和提示新版本。",
            update_group,
        )
        updater_enabled_check = SwitchButton(auto_update_card)
        updater_enabled_check.setOnText("开")
        updater_enabled_check.setOffText("关")
        updater_enabled_check.setChecked(updater_settings.enabled)
        add_setting_card_actions(auto_update_card, updater_enabled_check)
        update_group.addSettingCard(auto_update_card)

        startup_check_card = SettingCard(
            FIF.SYNC,
            "启动时静默检查更新",
            "每次启动工作台时在后台检查一次新版本。",
            update_group,
        )
        startup_check = SwitchButton(startup_check_card)
        startup_check.setOnText("开")
        startup_check.setOffText("关")
        startup_check.setChecked(updater_settings.check_on_startup)
        add_setting_card_actions(startup_check_card, startup_check)
        update_group.addSettingCard(startup_check_card)

        interval_card = SettingCard(
            FIF.HISTORY,
            "启动检查间隔",
            "两次启动时检查更新之间的最小时间间隔。",
            update_group,
        )
        interval_edit = QLineEdit(interval_card)
        interval_edit.setText(str(updater_settings.min_check_interval_hours))
        interval_edit.setFixedWidth(72)
        add_setting_card_actions(interval_card, interval_edit, BodyLabel("小时", interval_card))
        update_group.addSettingCard(interval_card)

        source_order = list(updater_settings.source_order)
        order_card = SettingCard(
            FIF.MOVE,
            "更新源优先级",
            " → ".join(SOURCE_LABELS.get(source, source) for source in source_order),
            update_group,
        )
        source_order_label = order_card.contentLabel
        edit_order_button = QPushButton("编辑顺序", order_card)
        add_setting_card_actions(order_card, edit_order_button)
        update_group.addSettingCard(order_card)

        check_now_card = SettingCard(
            FIF.SEARCH,
            "立即检查更新",
            f"当前版本 v{APP_VERSION}",
            update_group,
        )
        update_status_label = check_now_card.contentLabel
        check_now_button = QPushButton("检查更新", check_now_card)
        add_setting_card_actions(check_now_card, check_now_button)
        update_group.addSettingCard(check_now_card)

        def refresh_source_order_label() -> None:
            source_order_label.setText(" → ".join(SOURCE_LABELS.get(source, source) for source in source_order))
            relax_setting_card_height(order_card)

        def edit_source_order() -> None:
            nonlocal source_order
            order_dialog = UpdateSourceOrderDialog(source_order, dialog)
            if exec_modeless_dialog(order_dialog):
                source_order = order_dialog.order
                refresh_source_order_label()

        edit_order_button.clicked.connect(edit_source_order)
        refresh_source_order_label()

        def current_update_settings_from_ui() -> UpdaterSettings | None:
            try:
                interval = int(interval_edit.text().strip() or "0")
                if interval < 0:
                    raise ValueError
            except ValueError:
                update_status_label.setText("启动检查间隔必须是 0 或正整数。")
                return None
            updated = UpdaterSettings.load(self.settings)
            updated.enabled = updater_enabled_check.isChecked()
            updated.check_on_startup = startup_check.isChecked()
            updated.min_check_interval_hours = interval
            updated.proxy_mode = current_proxy_mode()
            updated.proxy_manual_url = proxy_manual_edit.text().strip()
            updated.source_order = normalize_order(source_order)
            return updated

        def check_now_with_current_ui() -> None:
            settings_for_check = current_update_settings_from_ui()
            if settings_for_check is None:
                return
            self._start_workbench_update_check(
                manual=True,
                updater_settings=settings_for_check,
                status_label=update_status_label,
                trigger_button=check_now_button,
            )

        check_now_button.clicked.connect(check_now_with_current_ui)

        def sync_proxy_manual_enabled() -> None:
            value = current_proxy_mode()
            proxy_manual_edit.setEnabled(value == "manual")
            refresh_proxy_status()

        proxy_combo.currentIndexChanged.connect(lambda _index: sync_proxy_manual_enabled())
        proxy_manual_edit.textChanged.connect(lambda _text: refresh_proxy_status())
        sync_proxy_manual_enabled()

        github_url = "https://github.com/karaoke-studio/karaoke-studio"
        about_group = SettingCardGroup("关于", dialog)

        product_icon: QIcon | FIF = QIcon(str(APP_LOGO_PATH)) if APP_LOGO_PATH.exists() else FIF.INFO
        product_card = SettingCard(
            product_icon,
            "Karaoke-Studio 卡拉OK工作台",
            f"版本 v{APP_VERSION}  |  B站 @凛夜delin",
            about_group,
        )
        about_group.addSettingCard(product_card)

        github_card = HyperlinkCard(
            github_url,
            "打开",
            FIF.GITHUB,
            "GitHub",
            github_url,
            about_group,
        )
        about_group.addSettingCard(github_card)

        log_card = PushSettingCard(
            "打开日志目录",
            FIF.DOCUMENT,
            "诊断日志",
            "遇到崩溃或任务失败时，请将此目录中的日志文件发给开发者。",
            about_group,
        )
        about_group.addSettingCard(log_card)

        def open_log_directory() -> None:
            try:
                directory = get_active_log_dir()
                directory.mkdir(parents=True, exist_ok=True)
                if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory))):
                    raise OSError("系统未能打开该目录")
            except Exception as exc:  # noqa: BLE001
                logging.getLogger(__name__).exception("打开日志目录失败")
                show_fluent_info(dialog, f"无法打开日志目录：{exc}")

        log_card.clicked.connect(open_log_directory)

        # 统一在卡片全部装配完后计算换行高度（右侧控件宽度需参与文字列宽）。
        # 先用估算卡宽算一次，对话框显示后再用真实卡宽精调。两张状态卡的文本
        # 会动态变长（连通性 / 检查结果），按两行预留。
        _card_min_lines = {proxy_status_card: 2, check_now_card: 2}

        def _relax_all_setting_cards(available_width: int = 740) -> None:
            for _card in (
                theme_card, ffmpeg_card, import_card,
                proxy_mode_card, proxy_manual_card, proxy_status_card,
                auto_update_card, startup_check_card, interval_card, order_card, check_now_card,
                product_card, github_card, log_card,
            ):
                relax_setting_card_height(
                    _card,
                    available_width=available_width,
                    min_content_lines=_card_min_lines.get(_card, 0),
                )

        _relax_all_setting_cards()

        def _refit_setting_cards() -> None:
            # 各页卡宽一致；隐藏页的卡尚未布局，统一用首页（可见页）组宽精调。
            _relax_all_setting_cards(available_width=tools_group.width())

        QTimer.singleShot(0, _refit_setting_cards)

        settings_stack.addWidget(build_settings_tab_page(settings_stack, [tools_group, migrate_group]))
        settings_stack.addWidget(build_settings_tab_page(settings_stack, [theme_group]))
        settings_stack.addWidget(build_settings_tab_page(settings_stack, [proxy_group, update_group]))
        settings_stack.addWidget(build_settings_tab_page(settings_stack, [about_group]))
        pivot.addItem(routeKey="tools", text="工具", onClick=lambda _checked: settings_stack.setCurrentIndex(0))
        pivot.addItem(routeKey="ui", text="界面", onClick=lambda _checked: settings_stack.setCurrentIndex(1))
        pivot.addItem(routeKey="network", text="网络与更新", onClick=lambda _checked: settings_stack.setCurrentIndex(2))
        pivot.addItem(routeKey="about", text="关于", onClick=lambda _checked: settings_stack.setCurrentIndex(3))
        pivot.setCurrentItem("tools")
        settings_stack.setCurrentIndex(0)

        def save_global_settings() -> None:
            try:
                ffmpeg_dir_text = ffmpeg_display.text().strip()
                ffmpeg_dir = Path(ffmpeg_dir_text).expanduser() if ffmpeg_dir_text else None
                if ffmpeg_dir is not None and not ffmpeg_dir.is_dir():
                    raise ProcessingError("所选 ffmpeg 目录无效，请重新选择。")
                interval = int(interval_edit.text().strip() or "0")
                if interval < 0:
                    raise ValueError
            except ValueError:
                show_fluent_info(dialog, "启动检查间隔必须是 0 或正整数。")
                return
            except ProcessingError as exc:
                show_fluent_info(dialog, str(exc))
                return

            self.set_ffmpeg_dir(ffmpeg_dir)
            self.settings.ffmpeg_dir = self.ffmpeg_dir_text
            # 写入新选择的界面主题。``UpdaterSettings.save`` 内部
            # 会调 ``save_app_settings(self.settings)``，连同 ``ui_theme``
            # 一起持久化。这里只需更新 in-memory 字段即可。
            _selected_idx = theme_combo.currentIndex()
            if 0 <= _selected_idx < len(_theme_options):
                self.settings.ui_theme = _theme_options[_selected_idx][1]
            updated = UpdaterSettings.load(self.settings)
            updated.enabled = updater_enabled_check.isChecked()
            updated.check_on_startup = startup_check.isChecked()
            updated.min_check_interval_hours = interval
            updated.proxy_mode = current_proxy_mode()
            updated.proxy_manual_url = proxy_manual_edit.text().strip()
            updated.source_order = normalize_order(source_order)
            updated.save(self.settings)
            update_status_label.setText("设置已保存到本地。")

        save_button.clicked.connect(save_global_settings)
        dialog.exec()

    def _check_for_workbench_update_on_startup(self) -> None:
        settings = ensure_updater_settings(self.settings)
        self._start_workbench_update_check(manual=False, updater_settings=settings)

    def _start_workbench_update_check(
        self,
        *,
        manual: bool,
        updater_settings: UpdaterSettings | None = None,
        status_label: QLabel | None = None,
        trigger_button: QPushButton | None = None,
    ) -> None:
        settings = updater_settings or UpdaterSettings.load(self.settings)
        if not manual and (not settings.enabled or not settings.check_on_startup):
            return
        if self._update_checker is not None:
            return
        if status_label is not None:
            status_label.setText("正在检查更新…")
        if trigger_button is not None:
            trigger_button.setEnabled(False)
        checker = UpdateChecker(settings, manual=manual, parent=self)
        self._update_checker = checker

        def finish(result: object) -> None:
            if self._update_checker is checker:
                self._update_checker = None
            if trigger_button is not None:
                trigger_button.setEnabled(True)
            self._handle_workbench_update_result(
                result,
                manual=manual,
                checker_settings=settings,
                status_label=status_label,
            )

        checker.finished.connect(finish)
        checker.start()

    def _handle_workbench_update_result(
        self,
        result: object,
        *,
        manual: bool,
        checker_settings: UpdaterSettings,
        status_label: QLabel | None = None,
    ) -> None:
        if not isinstance(result, CheckResult):
            if manual:
                QMessageBox.critical(self, APP_TITLE, "检查更新失败：返回结果无效。")
            return
        checker_settings.save(self.settings)
        if result.skipped_due_to_cooldown:
            if status_label is not None:
                status_label.setText(f"当前版本 v{APP_VERSION}")
            return
        if not result.ok:
            if status_label is not None:
                status_label.setText("检查更新失败。")
            if manual:
                details = "\n".join(
                    f"[{'OK' if not err else 'FAIL'}] {source} - {url}\n{err}"
                    for source, url, err in result.attempts
                )
                QMessageBox.critical(self, APP_TITLE, f"{result.error}\n\n{details}" if details else result.error)
            return
        if not result.has_update or result.release is None:
            if status_label is not None:
                remote = result.release.version if result.release is not None else APP_VERSION
                status_label.setText(f"已是最新版本。当前 v{APP_VERSION}，远端 v{remote}。")
            elif manual:
                QMessageBox.information(self, APP_TITLE, f"当前已经是最新版本：v{APP_VERSION}")
            return
        if status_label is not None:
            status_label.setText(f"发现新版本 v{result.release.version}")
        self._show_workbench_update_dialog(result, checker_settings)

    def _show_workbench_update_dialog(self, result: CheckResult, settings: UpdaterSettings) -> None:
        release = result.release
        if release is None:
            return
        source_label = SOURCE_LABELS.get(result.primary_source, result.primary_source)
        dialog = WorkbenchUpdateDialog(
            release,
            local_version=APP_VERSION,
            source_label=source_label,
            all_releases=result.all_releases,
            parent=self,
        )
        dialog.exec()

        if dialog.user_choice == "skip":
            settings.skipped_version = release.version
            settings.save(self.settings)
            return
        if dialog.user_choice == "later":
            return
        if dialog.user_choice == "update":
            self._launch_workbench_updater(result)
            return
        message = QMessageBox(self)
        message.setWindowTitle(APP_TITLE)
        message.setIcon(QMessageBox.Icon.Information)
        source_label = SOURCE_LABELS.get(result.primary_source, result.primary_source)
        message.setText(f"发现工作台新版本 v{release.version}")
        message.setInformativeText(
            f"当前版本 v{APP_VERSION}\n发布于 {release.published_at[:10] or '未知日期'}\n下载源：{source_label}"
        )
        if release.body.strip():
            message.setDetailedText(release.body.strip())
        update_button = message.addButton("立即更新", QMessageBox.ButtonRole.AcceptRole)
        skip_button = message.addButton("跳过此版本", QMessageBox.ButtonRole.DestructiveRole)
        later_button = message.addButton("稍后再说", QMessageBox.ButtonRole.RejectRole)
        message.exec()

        clicked = message.clickedButton()
        if clicked is skip_button:
            settings.skipped_version = release.version
            settings.save(self.settings)
            return
        if clicked is later_button:
            return
        if clicked is update_button:
            self._launch_workbench_updater(result)

    def _launch_workbench_updater(self, result: CheckResult) -> None:
        if not result.release or not result.download_candidates:
            QMessageBox.critical(self, APP_TITLE, "缺少更新下载信息，请到 GitHub Release 手动下载。")
            return
        try:
            from krok_helper.updater import installer
            from krok_helper.updater.progress_window import UpdateProgressWindow
            from krok_helper.updater.worker import LaunchUpdaterWorker
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, APP_TITLE, f"无法加载更新器：\n{exc}")
            return
        if not installer.is_updater_available():
            QMessageBox.information(self, APP_TITLE, "缺少 Updater.exe。请到 GitHub Release 手动下载最新版本。")
            return
        proxy_settings = UpdaterSettings.load(self.settings)
        from krok_helper.network import resolve_proxy

        info, _proxies = resolve_proxy(proxy_settings.proxy_mode, proxy_settings.proxy_manual_url)
        proxy_url = info.url if info is not None and info.is_valid else ""
        plan = installer.LaunchPlan(
            app_dir=installer.find_app_dir(),
            app_exe_name=installer.find_app_exe_name(),
            target_version=result.release.version,
            target_tag=result.release.tag,
            asset_name=result.primary_asset_name,
            download_urls=[(source, url) for source, url in result.download_candidates],
            proxy_url=proxy_url,
        )

        # launch_updater 内部的 Updater.exe 自更新会发起网络下载（数秒到数十秒），
        # 放到后台线程执行，主线程用进度窗给用户反馈并支持取消。
        progress_win = UpdateProgressWindow(self)
        progress_win.show()
        worker = LaunchUpdaterWorker(plan, parent=self)
        self._update_launch_worker = worker  # 防 GC
        self._update_progress_win = progress_win  # 防 GC

        worker.progress.connect(progress_win.update_from_text, Qt.ConnectionType.QueuedConnection)
        progress_win.cancelled.connect(worker.request_cancel)

        def _on_launch_done(launch_result: object) -> None:
            self._update_launch_worker = None
            self._update_progress_win = None
            progress_win.finish()
            lr = launch_result
            if getattr(lr, "reason", "") == "用户取消更新":
                return
            if not getattr(lr, "launched", False):
                QMessageBox.critical(
                    self, APP_TITLE, f"无法启动 Updater：\n{getattr(lr, 'reason', '未知错误')}"
                )
                return
            self.request_force_quit()

        worker.done.connect(_on_launch_done, Qt.ConnectionType.QueuedConnection)
        worker.start()

    def _save_settings_payload(
        self,
        *,
        output_name_mode: str,
        on_template: str,
        off_template: str,
        align_video_template: str,
        align_audio_template: str,
        align_output_dir_mode: str,
        align_output_custom_dir: str,
        ffmpeg_dir_text: str,
    ) -> Path:
        ffmpeg_dir = Path(ffmpeg_dir_text).expanduser() if ffmpeg_dir_text.strip() else None
        if ffmpeg_dir is not None and not ffmpeg_dir.is_dir():
            raise ProcessingError("所选 ffmpeg 目录无效，请重新选择。")
        if align_output_dir_mode not in {ALIGN_OUTPUT_DIR_SOURCE_VIDEO, ALIGN_OUTPUT_DIR_CUSTOM}:
            raise ProcessingError("对齐输出位置无效，请重新选择。")
        align_output_dir = (
            Path(align_output_custom_dir).expanduser() if align_output_custom_dir.strip() else None
        )
        if align_output_dir_mode == ALIGN_OUTPUT_DIR_CUSTOM:
            if align_output_dir is None:
                raise ProcessingError("请选择对齐导出的保存目录。")
            if not align_output_dir.is_dir():
                raise ProcessingError("所选对齐导出目录无效，请重新选择。")

        if output_name_mode not in {OUTPUT_NAME_MODE_FIXED, OUTPUT_NAME_MODE_TEMPLATE}:
            raise ProcessingError("输出命名模式无效，请重新选择。")
        if output_name_mode == OUTPUT_NAME_MODE_TEMPLATE:
            on_template = validate_output_name_template(on_template, "原唱")
            off_template = validate_output_name_template(off_template, "伴奏")

        align_video_template = self._validate_alignment_name_template(
            align_video_template,
            "对齐后视频",
            allowed_fields={"video_name"},
            extensions=(".mp4", ".mkv"),
        )
        align_audio_template = self._validate_alignment_name_template(
            align_audio_template,
            "对齐后音频",
            allowed_fields={"audio_name", "video_name"},
            extensions=(".wav",),
        )

        self.output_name_mode_value = output_name_mode
        self.on_name_template_value = on_template
        self.off_name_template_value = off_template
        self.align_video_name_template_value = align_video_template
        self.align_audio_name_template_value = align_audio_template
        self.set_alignment_output_dir_settings(
            align_output_dir_mode,
            str(align_output_dir) if align_output_dir is not None else "",
        )
        self.ffmpeg_dir_text = str(ffmpeg_dir) if ffmpeg_dir else ""
        self._sync_ffmpeg_labels()
        self.settings.output_name_mode = self.output_name_mode_value
        self.settings.on_name_template = self.on_name_template_value
        self.settings.off_name_template = self.off_name_template_value
        self.settings.align_video_name_template = self.align_video_name_template_value
        self.settings.align_audio_name_template = self.align_audio_name_template_value
        self.settings.ffmpeg_dir = self.ffmpeg_dir_text
        self._sync_lyrics_timing_host_paths()
        self._update_alignment_preferences_from_ui()
        return save_app_settings(self.settings)

    def _resolve_output_name_mode(self) -> str:
        if self.output_name_mode_value not in {OUTPUT_NAME_MODE_FIXED, OUTPUT_NAME_MODE_TEMPLATE}:
            raise ProcessingError("输出命名模式无效，请重新选择。")
        return self.output_name_mode_value

    def _resolve_output_name_templates(self, *, require_valid: bool) -> tuple[str, str]:
        on_template = self.on_name_template_value or DEFAULT_ON_NAME_TEMPLATE
        off_template = self.off_name_template_value or DEFAULT_OFF_NAME_TEMPLATE
        if require_valid:
            on_template = validate_output_name_template(on_template, "原唱")
            off_template = validate_output_name_template(off_template, "伴奏")
        return on_template, off_template

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
        normalized = template.strip()
        for extension in extensions:
            if normalized.lower().endswith(extension):
                normalized = normalized[: -len(extension)].rstrip()
                break
        if not normalized:
            raise ProcessingError(f"{label}模板不能为空。")
        # 覆盖 Windows 不允许的全部字符（\ / : * ? " < > |），而非只挡路径分隔符。
        invalid_chars = sorted({char for char in normalized if char in WINDOWS_INVALID_FILENAME_CHARS})
        if invalid_chars:
            joined = " ".join(invalid_chars)
            raise ProcessingError(f"{label}模板包含非法字符: {joined}")
        # 不配对的大括号会让 parse 抛 ValueError；转成 ProcessingError，避免从只
        # 捕获 ProcessingError 的调用处逃逸导致闪退。
        try:
            fields = list(ALIGNMENT_TEMPLATE_FORMATTER.parse(normalized))
        except ValueError as exc:
            raise ProcessingError(f"{label}模板的大括号不配对，请检查占位符是否写完整。") from exc
        for _, field_name, _, _ in fields:
            if field_name and field_name not in allowed_fields:
                supported = "、".join(f"{{{name}}}" for name in sorted(allowed_fields))
                raise ProcessingError(f"{label}模板包含不支持的占位符 {field_name}。当前支持 {supported}。")
        return normalized

    def _resolve_ffmpeg_dir(self) -> Path | None:
        if not self.ffmpeg_dir_text.strip():
            return None
        path = Path(self.ffmpeg_dir_text).expanduser()
        if not path.is_dir():
            raise ProcessingError("所选 ffmpeg 目录无效，请重新选择。")
        return path

    def _validate_hires_inputs(
        self,
    ) -> tuple[Path, Path | None, list[Path], Path, Path | None, str, str | None, str | None]:
        video_path = self.video_zone.path
        on_vocal_path = self.on_vocal_zone.path
        off_vocal_paths = list(self.off_vocal_zone.paths)
        ffmpeg_dir = self._resolve_ffmpeg_dir()
        output_name_mode = self._resolve_output_name_mode()

        missing: list[str] = []
        if video_path is None or not video_path.is_file():
            missing.append("字幕视频")
        if on_vocal_path is not None and not on_vocal_path.is_file():
            missing.append("原唱音频")
        if any(not path.is_file() for path in off_vocal_paths):
            missing.append("伴奏音频")
        if missing:
            raise ProcessingError(f"请先选择有效的文件: {', '.join(missing)}")
        assert video_path is not None

        if on_vocal_path is None and not off_vocal_paths:
            raise ProcessingError("请至少选择原唱音频或伴奏音频中的一个。")
        if on_vocal_path is not None:
            on_resolved = on_vocal_path.resolve()
            if any(path.resolve() == on_resolved for path in off_vocal_paths):
                raise ProcessingError("原唱音频和伴奏音频不能是同一个文件。")

        output_dir = resolve_output_dir(video_path)
        if output_name_mode == OUTPUT_NAME_MODE_TEMPLATE:
            on_template, off_template = self._resolve_output_name_templates(require_valid=True)
        else:
            on_template, off_template = None, None

        return (
            video_path,
            on_vocal_path,
            off_vocal_paths,
            output_dir,
            ffmpeg_dir,
            output_name_mode,
            on_template,
            off_template,
        )

    def _set_hires_status_color(self, color: str | None) -> None:
        # ``None`` 表示 idle —— 用当前主题的 ``text_secondary``；其它显式色
        # （success/error/processing）按原值传给 QSS，深色背景下这些状态色
        # 都有足够对比度。
        if color is None:
            from krok_helper.theme_workbench import palette as _wb_pal
            color = _wb_pal().text_secondary
        self.hires_status_label.setStyleSheet(
            f'font-family: "Microsoft YaHei UI"; font-size: 10pt; font-weight: 400; color: {color};'
        )

    def _copy_hires_log(self) -> None:
        QApplication.clipboard().setText(self.hires_log.toPlainText())

    def _is_hires_running(self) -> bool:
        return self.hires_task is not None and self.hires_task.isRunning()

    def _register_hires_process(self, process: subprocess.Popen | None) -> None:
        self._hires_process = process

    def _cleanup_incomplete_hires_outputs(self) -> None:
        completed = set(self._hires_completed_outputs)
        for path in self._hires_expected_outputs:
            if path in completed or path in self._hires_preexisting_outputs or not path.exists():
                continue
            try:
                path.unlink()
                self._append_hires_log(f"已清理未完成的输出文件: {path}")
            except OSError as exc:
                self._append_hires_log(f"清理未完成的输出文件失败: {path} ({exc})")

    def _reset_hires_cancel_state(self) -> None:
        self._hires_cancel_requested = False
        self._hires_process = None
        self._hires_expected_outputs = []
        self._hires_completed_outputs = []
        self._hires_preexisting_outputs = set()

    def _stop_hires(self) -> None:
        if not self._is_hires_running():
            return
        if not self._hires_cancel_requested:
            self._hires_cancel_requested = True
            self.hires_cancel_button.setEnabled(False)
            self.hires_status_label.setText("正在取消…")
            self._set_hires_status_color(None)
            self._append_hires_log("正在取消生成…")
        process = self._hires_process
        if process is not None:
            terminate_process(process)

    def _start_hires(self) -> None:
        if self._is_hires_running():
            QMessageBox.information(self, APP_TITLE, "当前任务还在处理中，请稍等。")
            return

        try:
            args = self._validate_hires_inputs()
        except ProcessingError as exc:
            QMessageBox.critical(self, APP_TITLE, str(exc))
            return

        self.hires_log.clear()
        (
            video_path,
            on_vocal_path,
            off_vocal_paths,
            output_dir,
            _ffmpeg_dir,
            output_name_mode,
            on_template,
            off_template,
        ) = args
        # 这里才会用真实的视频文件名渲染模板，可能因文件名导致生成的输出名为空
        # 等情况抛 ProcessingError；必须在主线程上兜住，否则异常会逃逸出 Qt 槽。
        try:
            on_output: Path | None = None
            if on_vocal_path is not None:
                on_output, _ = resolve_output_paths(
                    video_path,
                    output_dir,
                    output_name_mode,
                    on_name_template=on_template,
                    include_on=True,
                    include_off=False,
                    on_audio_path=on_vocal_path,
                )
            off_outputs = resolve_off_output_paths(
                video_path, output_dir, output_name_mode, off_template, off_vocal_paths
            )
        except ProcessingError as exc:
            QMessageBox.critical(self, APP_TITLE, str(exc))
            return
        self._hires_cancel_requested = False
        self._hires_process = None
        self._hires_expected_outputs = ([on_output] if on_output is not None else []) + off_outputs
        self._hires_completed_outputs = []
        self._hires_preexisting_outputs = {path for path in self._hires_expected_outputs if path.exists()}
        self.hires_start_button.setEnabled(False)
        self.hires_cancel_button.setEnabled(True)
        self.hires_progress.setRange(0, 0)
        total = len(self._hires_expected_outputs)
        self.hires_status_label.setText("处理中…" if total < 2 else f"处理中…（共 {total} 个输出）")
        self._set_hires_status_color("#2f6fed")

        def runner(logger: Callable[[str], None]) -> list[Path]:
            (
                video_path,
                on_vocal_path,
                off_vocal_paths,
                output_dir,
                ffmpeg_dir,
                output_name_mode,
                on_template,
                off_template,
            ) = args
            outputs = run_pipeline(
                video_path=video_path,
                on_vocal_path=on_vocal_path,
                off_vocal_paths=off_vocal_paths,
                output_dir=output_dir,
                ffmpeg_dir=ffmpeg_dir,
                output_name_mode=output_name_mode,
                on_name_template=on_template,
                off_name_template=off_template,
                logger=logger,
                should_cancel=lambda: self._hires_cancel_requested,
                on_process_started=self._register_hires_process,
            )
            self._hires_completed_outputs.extend(outputs)
            return outputs

        task = self._track_background_task("hires_task", BackgroundTask(runner))
        task.log_message.connect(self._append_hires_log)
        task.task_succeeded.connect(self._finish_hires_success)
        task.task_failed.connect(self._finish_hires_failure)
        task.start()

    def _append_hires_log(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.hires_log.appendPlainText(f"[{timestamp}] {message}")

    def _append_align_log(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.align_log.appendPlainText(f"[{timestamp}] {message}")

    def _finish_hires_success(self, outputs: object) -> None:
        was_cancelled = self._hires_cancel_requested
        self._hires_process = None
        self.hires_progress.setRange(0, 1)
        self.hires_progress.setValue(0 if was_cancelled else 1)
        self.hires_start_button.setEnabled(True)
        self.hires_cancel_button.setEnabled(False)
        if was_cancelled:
            self._cleanup_incomplete_hires_outputs()
            self.hires_status_label.setText("生成已取消")
            self._set_hires_status_color(None)
            self._append_hires_log("生成已取消，临时文件和未完成输出已清理。")
            self._reset_hires_cancel_state()
            return
        self.hires_status_label.setText("完成")
        self._set_hires_status_color("#10B981")
        self._reset_hires_cancel_state()
        lines = "\n".join(str(path) for path in outputs) if isinstance(outputs, list) else str(outputs)
        play_completion_sound()
        from krok_helper.subtitle_render.frontend.fluent_dialogs import fluent_info

        fluent_info(
            self,
            "Hi-Res 导出完成",
            f"文件已成功导出：\n{lines}",
            ok_text="确定",
            copyable=True,
        )

    def _finish_hires_failure(self, message: str) -> None:
        was_cancelled = self._hires_cancel_requested
        self._hires_process = None
        self.hires_progress.setRange(0, 1)
        self.hires_progress.setValue(0)
        self.hires_start_button.setEnabled(True)
        self.hires_cancel_button.setEnabled(False)
        if was_cancelled:
            self._cleanup_incomplete_hires_outputs()
            self.hires_status_label.setText("生成已取消")
            self._set_hires_status_color(None)
            self._append_hires_log("生成已取消，临时文件和未完成输出已清理。")
            self._reset_hires_cancel_state()
            return
        self.hires_status_label.setText("失败")
        self._set_hires_status_color("#EF4444")
        self._reset_hires_cancel_state()
        self._append_hires_log(f"处理失败: {message}")
        QMessageBox.critical(self, APP_TITLE, message)

    def _clear_hires_inputs(self) -> None:
        if self.hires_task is not None and self.hires_task.isRunning():
            QMessageBox.information(self, APP_TITLE, "当前生成任务还在处理中，请稍等。")
            return
        self.video_zone.clear_path()
        self.on_vocal_zone.clear_path()
        self.off_vocal_zone.clear_path()
        self.output_dir_label.setText("跟随字幕视频所在目录")
        self.hires_status_label.setText("已清空已选文件")
        self._set_hires_status_color(None)

    def _open_hires_output_dir(self) -> None:
        video_path = self.video_zone.path
        if video_path is None:
            QMessageBox.information(self, APP_TITLE, "请先选择字幕视频。")
            return
        output_dir = resolve_output_dir(video_path)
        output_dir.mkdir(parents=True, exist_ok=True)
        open_in_explorer(output_dir)

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
            QMessageBox.information(self, APP_TITLE, "当前波形任务还在处理中，请稍等。")
            return
        try:
            video_path, audio_path, ffmpeg_dir = self._validate_alignment_inputs()
        except ProcessingError as exc:
            QMessageBox.critical(self, APP_TITLE, str(exc))
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
        QMessageBox.critical(self, APP_TITLE, message)

    def _auto_align_waveforms(self) -> None:
        if self.align_auto_task is not None and self.align_auto_task.isRunning():
            QMessageBox.information(self, APP_TITLE, "当前自动对齐任务还在处理中，请稍等。")
            return
        if self.waveform_view.video_waveform is None or self.waveform_view.audio_waveform is None:
            QMessageBox.critical(self, APP_TITLE, "请先生成波形。")
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
        QMessageBox.critical(self, APP_TITLE, message)

    def _handle_align_target_changed(self) -> None:
        target_track = ALIGN_TARGET_VIDEO if self.align_target_video_radio.isChecked() else ALIGN_TARGET_AUDIO
        self._stop_alignment_preview(log_message=False)
        self.waveform_view.set_target_track(target_track)
        self._refresh_align_target_ui()

    def _refresh_align_target_ui(self) -> None:
        is_video_target = self._is_align_video_target()
        has_waveforms = self.waveform_view.video_waveform is not None and self.waveform_view.audio_waveform is not None
        if hasattr(self, "align_target_video_card"):
            self.align_target_video_card.sync_ui()
        if hasattr(self, "align_target_audio_card"):
            self.align_target_audio_card.sync_ui()
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
            QMessageBox.critical(self, APP_TITLE, "请先生成波形并完成对齐。")
            return
        try:
            video_path, audio_path, ffmpeg_dir = self._validate_alignment_inputs()
        except ProcessingError as exc:
            QMessageBox.critical(self, APP_TITLE, str(exc))
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
            QMessageBox.critical(self, APP_TITLE, f"播放预览失败:\n{exc}")
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
        if self.align_output_dir_mode_value == ALIGN_OUTPUT_DIR_CUSTOM:
            custom_dir = self.align_output_custom_dir_text.strip()
            if not custom_dir:
                raise ProcessingError("请先在波形对齐设置中选择对齐导出的保存目录。")
            output_dir = Path(custom_dir).expanduser()
            if not output_dir.is_dir():
                raise ProcessingError("波形对齐设置中的保存目录无效，请重新选择。")
            return output_dir
        return video_path.parent

    def _render_alignment_output_path(
        self,
        *,
        video_path: Path,
        audio_path: Path,
        is_video_target: bool,
    ) -> Path:
        video_template, audio_template = self._resolve_alignment_name_templates(require_valid=True)
        template = video_template if is_video_target else audio_template
        extension = ".mp4" if is_video_target else ".wav"
        try:
            stem = template.format(video_name=video_path.stem, audio_name=audio_path.stem).strip()
        except Exception as exc:  # noqa: BLE001
            label = "对齐后视频" if is_video_target else "对齐后音频"
            raise ProcessingError(f"{label}模板无法生成文件名: {exc}") from exc

        stem = stem.rstrip(". ")
        if not stem:
            raise ProcessingError("导出文件名不能为空。")
        invalid_chars = sorted({char for char in stem if char in WINDOWS_INVALID_FILENAME_CHARS})
        if invalid_chars:
            raise ProcessingError(f"文件名包含非法字符: {' '.join(invalid_chars)}")
        return self._resolve_alignment_output_dir(video_path) / f"{stem}{extension}"

    def _start_aligned_export(self) -> None:
        if self.align_export_task is not None and self.align_export_task.isRunning():
            QMessageBox.information(self, APP_TITLE, "当前导出任务还在处理中，请稍等。")
            return
        if self.waveform_view.video_waveform is None or self.waveform_view.audio_waveform is None:
            QMessageBox.critical(self, APP_TITLE, "请先生成波形并完成对齐。")
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
            QMessageBox.critical(self, APP_TITLE, str(exc))
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
        QMessageBox.critical(self, APP_TITLE, message)

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

        if send_to_hires:
            vocal_path = source_audio_path if is_video_target else output_path
            if vocal_path is not None:
                self.set_on_vocal_path(Path(vocal_path))

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
            QMessageBox.information(self, APP_TITLE, "当前对齐任务还在处理中，请稍等。")
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
            QMessageBox.information(self, APP_TITLE, "请先选择文件。")
            return
        try:
            output_dir = self._resolve_alignment_output_dir(video_path) if video_path is not None else source_path.parent
        except ProcessingError as exc:
            QMessageBox.critical(self, APP_TITLE, str(exc))
            return
        output_dir.mkdir(parents=True, exist_ok=True)
        open_in_explorer(output_dir)

    def request_force_quit(self) -> None:
        """Release embedded resources, then guarantee process exit for Updater."""

        if getattr(self, "_force_quitting_for_update", False):
            return
        self._force_quitting_for_update = True

        try:
            self.close()
        except Exception:
            pass
        # Use a Python timer rather than a QTimer: QApplication.quit() can stop
        # the Qt event loop while a non-Qt worker still keeps the process alive.
        # Schedule only after closeEvent has synchronously flushed recovery data
        # and released BASS resources.
        _schedule_hard_process_exit()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _prepare_force_quit_for_update(self) -> None:
        """Persist recoverable state and release SUG/BASS before updater handoff."""

        if getattr(self, "_update_exit_prepared", False):
            return
        self._update_exit_prepared = True

        try:
            self._stop_alignment_preview(log_message=False)
        except Exception:
            pass
        if not self._shutdown_audio_separation(timeout_ms=3000):
            logging.getLogger(__name__).warning(
                "更新强制退出前停止 PyMSS 服务失败"
            )
        try:
            self._save_all_settings()
        except Exception:
            pass

        for attr_name in ("lyrics_timing_page", "subtitle_render_page"):
            page = getattr(self, attr_name, None)
            flush_unsaved = getattr(page, "flush_unsaved", None) if page is not None else None
            if flush_unsaved is not None:
                try:
                    flush_unsaved()
                except Exception:
                    logging.getLogger(__name__).warning(
                        "强制退出前保存 %s 恢复数据失败", attr_name, exc_info=True
                    )

        timing_page = getattr(self, "lyrics_timing_page", None)
        if timing_page is not None:
            KrokHelperQtApp._release_lyrics_timing_resources(timing_page)

    def closeEvent(self, event) -> None:  # noqa: N802
        if getattr(self, "_force_quitting_for_update", False):
            self._prepare_force_quit_for_update()
            super().closeEvent(event)
            return
        subtitle_page = getattr(self, "subtitle_render_page", None)
        subtitle_busy = False
        audio_separation_page = getattr(self, "audio_separation_page", None)
        audio_separation_busy = False
        try:
            subtitle_busy = bool(
                subtitle_page is not None
                and hasattr(subtitle_page, "is_busy")
                and subtitle_page.is_busy()
            )
        except Exception:
            subtitle_busy = False
        try:
            audio_separation_busy = bool(
                audio_separation_page is not None
                and hasattr(audio_separation_page, "is_busy")
                and audio_separation_page.is_busy()
            )
        except Exception:
            audio_separation_busy = False
        if self._running_background_tasks() or subtitle_busy:
            QMessageBox.information(self, APP_TITLE, "当前后台任务仍在运行，请等待完成后再关闭窗口。")
            event.ignore()
            return
        if audio_separation_busy and not ask_fluent_confirm(
            self,
            "音频分离任务仍在运行。现在退出会停止当前任务并关闭工作台启动的 PyMSS 服务，"
            "外部服务中的请求可能仍会继续。是否停止任务并退出？",
            yes_text="停止任务并退出",
        ):
            event.ignore()
            return
        self._stop_alignment_preview(log_message=False)
        if not self._shutdown_project_modules(event):
            return
        if not self._shutdown_audio_separation():
            QMessageBox.warning(
                self,
                APP_TITLE,
                "PyMSS 服务未能正常停止，请稍后重试或先在音频分离页面停止服务。",
            )
            event.ignore()
            return
        try:
            self._save_all_settings()
        except Exception:
            pass
        super().closeEvent(event)

    def _shutdown_audio_separation(self, *, timeout_ms: int = 5000) -> bool:
        """Stop the managed PyMSS backend without touching external services."""
        page = getattr(self, "audio_separation_page", None)
        shutdown = getattr(page, "shutdown", None) if page is not None else None
        if not callable(shutdown):
            return True
        try:
            result = shutdown(timeout_ms=timeout_ms)
            return result is not False
        except Exception:
            logging.getLogger(__name__).warning(
                "停止 PyMSS 服务失败", exc_info=True
            )
            return False

    def _shutdown_project_modules(self, event) -> bool:
        """Confirm all dirty embedded projects, then release module resources."""
        if not KrokHelperQtApp._confirm_unsaved_projects(self, event):
            return False
        KrokHelperQtApp._finalize_lyrics_timing_shutdown(self)
        return True

    def _confirm_unsaved_projects(self, event, pages=None) -> bool:
        candidates = pages or (
            ("歌词打轴", getattr(self, "lyrics_timing_page", None)),
            ("字幕视频生成", getattr(self, "subtitle_render_page", None)),
        )
        dirty_pages: list[tuple[str, object]] = []
        for label, page in candidates:
            if page is None:
                continue
            try:
                state_factory = getattr(page, "project_state", None)
                state = state_factory() if callable(state_factory) else None
                if state is not None and hasattr(state, "dirty"):
                    dirty = bool(state.dirty)
                    status_factory = getattr(state, "status_text", None)
                    status = (
                        status_factory() if callable(status_factory) else None
                    )
                    if status:
                        label = f"{label}（{status}）"
                else:
                    dirty = bool(
                        hasattr(page, "has_unsaved_changes")
                        and page.has_unsaved_changes()
                    )
            except Exception:
                try:
                    dirty = bool(
                        hasattr(page, "has_unsaved_changes")
                        and page.has_unsaved_changes()
                    )
                except Exception:
                    dirty = False
            if dirty:
                dirty_pages.append((label, page))

        if not dirty_pages:
            return True

        from krok_helper.subtitle_render.frontend.fluent_dialogs import fluent_choice

        choice = fluent_choice(
            self,
            "未保存的更改",
            "以下项目有未保存的更改：\n\n"
            + "\n".join(f"• {label}" for label, _page in dirty_pages)
            + "\n\n是否在退出前全部保存？",
            ("全部保存", "全部放弃", "取消"),
            default=0,
        )

        if choice not in (0, 1):
            event.ignore()
            return False

        if choice == 1:
            for _label, page in dirty_pages:
                discard = getattr(page, "discard_unsaved", None)
                if callable(discard):
                    try:
                        discard()
                    except Exception:
                        logging.getLogger(__name__).warning(
                            "放弃嵌入项目更改失败", exc_info=True
                        )
                else:
                    # SUG's current embedding contract has no public discard API.
                    # Clear its dirty flag before the child closeEvent runs;
                    # otherwise embedded closeEvent would recreate a recovery file
                    # after the user explicitly chose to discard the changes.
                    store = getattr(page, "_store", None)
                    if store is not None and hasattr(store, "_dirty"):
                        try:
                            store._dirty = False
                        except Exception:
                            logging.getLogger(__name__).warning(
                                "清除歌词打轴未保存状态失败", exc_info=True
                            )
            return True

        for label, page in dirty_pages:
            if not KrokHelperQtApp._save_project_page_for_close(self, label, page):
                event.ignore()
                return False
        return True

    def _save_project_page_for_close(self, label: str, page: object) -> bool:
        """Save one embedded page and wait for SUG's asynchronous save result."""
        trigger_save = getattr(page, "trigger_save", None)
        if not callable(trigger_save):
            QMessageBox.critical(self, APP_TITLE, f"{label}模块无法保存当前项目。")
            return False

        store = getattr(page, "_store", None)
        started_signal = getattr(store, "save_started", None) if store is not None else None
        finished_signal = getattr(store, "save_finished", None) if store is not None else None
        error_signal = getattr(store, "save_error", None) if store is not None else None
        supports_async_wait = all(
            signal is not None
            for signal in (started_signal, finished_signal, error_signal)
        )

        state = {"started": False, "finished": False, "error": None, "timeout": False}

        def on_started(_path: str) -> None:
            state["started"] = True

        def on_finished(_path: str) -> None:
            state["finished"] = True

        def on_error(error: str) -> None:
            state["error"] = error

        if supports_async_wait:
            started_signal.connect(on_started)
            finished_signal.connect(on_finished)
            error_signal.connect(on_error)

        try:
            result = trigger_save()
            if result is False:
                return False

            if supports_async_wait and state["started"] and not (
                state["finished"] or state["error"]
            ):
                deadline = time.monotonic() + 120.0
                while not (state["finished"] or state["error"]):
                    if time.monotonic() >= deadline:
                        state["timeout"] = True
                        break
                    QApplication.processEvents()
                    time.sleep(0.01)

            if state["timeout"]:
                QMessageBox.critical(
                    self,
                    APP_TITLE,
                    f"等待{label}项目保存完成超时，工作台将保持打开。",
                )
                return False
            if state["error"]:
                return False

            try:
                return not bool(
                    hasattr(page, "has_unsaved_changes")
                    and page.has_unsaved_changes()
                )
            except Exception:
                return bool(state["finished"] or result is True)
        except Exception as exc:
            QMessageBox.critical(self, APP_TITLE, f"{label}项目保存失败：\n{exc}")
            return False
        finally:
            if supports_async_wait:
                for signal, slot in (
                    (started_signal, on_started),
                    (finished_signal, on_finished),
                    (error_signal, on_error),
                ):
                    try:
                        signal.disconnect(slot)
                    except (TypeError, RuntimeError):
                        pass

    def _shutdown_lyrics_timing(self, event) -> bool:
        """Compatibility wrapper retained for existing host integration tests."""
        page = getattr(self, "lyrics_timing_page", None)
        if page is None:
            return True
        if not KrokHelperQtApp._confirm_unsaved_projects(
            self, event, (("歌词打轴", page),)
        ):
            return False
        KrokHelperQtApp._finalize_lyrics_timing_shutdown(self)
        return True

    def _finalize_lyrics_timing_shutdown(self) -> None:
        """Clean SUG recovery files only after save/discard has been resolved."""
        page = getattr(self, "lyrics_timing_page", None)
        if page is None:
            return
        store = getattr(page, "_store", None)
        cleanup = getattr(store, "cleanup_temp_files", None) if store is not None else None
        if cleanup is not None:
            try:
                cleanup()
            except Exception:
                pass
        KrokHelperQtApp._release_lyrics_timing_resources(page)

    @staticmethod
    def _release_lyrics_timing_resources(page) -> None:
        if getattr(page, "_krok_helper_resources_released", False):
            return
        try:
            setattr(page, "_krok_helper_resources_released", True)
        except Exception:
            pass

        editor = getattr(page, "editorInterface", None)
        release_editor = getattr(editor, "release_resources", None) if editor is not None else None
        if release_editor is not None:
            try:
                release_editor()
                return
            except Exception:
                pass

        timing_service = getattr(page, "_timing_service", None)
        release_timing = getattr(timing_service, "release", None) if timing_service is not None else None
        if release_timing is not None:
            try:
                release_timing()
                return
            except Exception:
                pass

        audio_engine = getattr(page, "_audio_engine", None)
        release_engine = getattr(audio_engine, "release", None) if audio_engine is not None else None
        if release_engine is not None:
            try:
                release_engine()
            except Exception:
                pass


_GLOBAL_EXCEPTHOOK_INSTALLED = False


def _install_global_excepthook() -> None:
    """把 GUI 线程上未捕获的异常转成可见的错误弹窗，而不是让 PySide6 直接
    abort 进程（表现为闪退）。Qt 槽函数里抛出的 Python 异常无法穿过 C++ 事件
    循环，默认会终止程序；这里兜底成对话框 + 标准 excepthook 打印。"""
    global _GLOBAL_EXCEPTHOOK_INSTALLED
    if _GLOBAL_EXCEPTHOOK_INSTALLED:
        return
    previous_hook = sys.excepthook

    def hook(exc_type, exc_value, exc_tb):
        if not getattr(previous_hook, "_karaoke_studio_logging_hook", False):
            logging.getLogger(__name__).critical(
                "GUI 事件处理发生未捕获异常",
                exc_info=(exc_type, exc_value, exc_tb),
            )
        try:
            previous_hook(exc_type, exc_value, exc_tb)
        except Exception:
            pass
        try:
            app = QApplication.instance()
            if app is not None:
                for widget in app.topLevelWidgets():
                    if not isinstance(widget, KrokHelperQtApp):
                        continue
                    page = getattr(widget, "subtitle_render_page", None)
                    flush_unsaved = getattr(page, "flush_unsaved", None)
                    if callable(flush_unsaved):
                        try:
                            flush_unsaved()
                        except Exception:
                            logging.getLogger(__name__).warning(
                                "未处理异常后写字幕恢复数据失败", exc_info=True
                            )
                QMessageBox.critical(
                    None,
                    APP_TITLE,
                    f"发生未处理的错误，操作已中断：\n\n{exc_type.__name__}: {exc_value}",
                )
        except Exception:
            pass

    sys.excepthook = hook
    _GLOBAL_EXCEPTHOOK_INSTALLED = True


def launch_qt_app() -> int:
    set_explicit_app_user_model_id("KaraokeStudio.Desktop")
    sync_fluent_ui_fonts()
    _install_global_excepthook()
    app = QApplication.instance() or QApplication([])
    app.setFont(build_app_ui_font())
    app_icon = load_taskbar_icon()
    if app_icon is not None:
        app.setWindowIcon(app_icon)
    window = KrokHelperQtApp()
    window.show()
    exit_code = app.exec()
    window.deleteLater()
    app.processEvents()
    return exit_code
