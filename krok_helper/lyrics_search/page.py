"""歌词检索页（工作流第 3 步）。

与 :mod:`krok_helper.alignment.page` 同样的形态：整页从 ``KrokHelperQtApp``
搬出来，以 mixin 混回同一个对象 —— 物理拆分，``self`` 语义不变、调用点不用改。

页面目前还依赖宿主的这些成员（清单由 ``tests/test_lyrics_search_boundary.py``
钉住，只许变短）：

* ``settings`` / ``_loading_settings_into_ui`` —— 配置读写与"灌设置期间别回写"
* ``_track_background_task`` —— 搜索/抓取跑在后台线程
* ``_install_single_click_combo_behavior`` —— 下拉框单击即选（外壳的通用行为）
* ``_import_current_lyrics_to_timing`` —— 把歌词交给第 4 步打轴
* ``width`` —— QWidget 自己的
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QSize, QTimer, Qt, pyqtSignal as Signal
from PyQt6.QtGui import QBrush, QColor, QFontMetrics, QPainter, QPalette
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QBoxLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QStyle,
    QStyleOptionViewItem,
    QStyledItemDelegate,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    CheckBox as QCheckBox,
    FluentIcon as FIF,
    LineEdit as QLineEdit,
    PlainTextEdit as QPlainTextEdit,
    PrimaryPushButton,
    PushButton as QPushButton,
    TableWidget as QTableWidget,
)
from qfluentwidgets.components.widgets.table_view import TableItemDelegate

from krok_helper.background import BackgroundTask
from krok_helper.config import APP_TITLE
from krok_helper.lyrics import (
    DEFAULT_LYRICS_PROVIDER_IDS,
    DEFAULT_LYRICS_SEARCH_LIMIT,
    LYRICS_LANGUAGE_ORIGINAL,
    LYRICS_LANGUAGE_TRANSLATION,
    LYRICS_PREVIEW_LINE,
    LYRICS_PREVIEW_VERBATIM,
    LyricsPreview,
    LyricsSearchBatch,
    LyricsSearchCandidate,
    UTATEN_RUBY_MARKER,
    build_lyrics_preview,
    extract_lyrics_query_from_file,
)
from krok_helper.media_formats import format_media_duration
from krok_helper.qfluent_compat import show_fluent_tooltip
from krok_helper.settings import save_app_settings
from krok_helper.ui_kit import CardWidget, ElidedLabel, StyledComboBox, build_lyrics_ui_font

__all__ = [
    "LYRICS_LANGUAGE_MAP",
    "LYRICS_LANGUAGE_OPTIONS",
    "LYRICS_PREVIEW_MODE_MAP",
    "LYRICS_PREVIEW_MODE_OPTIONS",
    "LYRICS_SOURCE_MAP",
    "LYRICS_SOURCE_OPTIONS",
    "LyricsKeywordLineEdit",
    "LyricsResultsDelegate",
    "LyricsSearchPageMixin",
]


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

LYRICS_LANGUAGE_OPTIONS = [
    ("原文", LYRICS_LANGUAGE_ORIGINAL),
    ("中文译文", LYRICS_LANGUAGE_TRANSLATION),
]

LYRICS_LANGUAGE_MAP = {label: value for label, value in LYRICS_LANGUAGE_OPTIONS}


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


class LyricsSearchPageMixin:
    """歌词检索页。混入 ``KrokHelperQtApp``，不单独实例化。"""

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
