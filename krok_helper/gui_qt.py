from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from copy import deepcopy
from pathlib import Path
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
    QSize,
    QTimer,
    Qt,
    pyqtProperty,
)
from PyQt6.QtGui import QColor, QIcon, QKeySequence, QPainter, QPixmap, QShortcut
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QDoubleSpinBox,
    QLabel,
    QMainWindow,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    ComboBox as QComboBox,
    FluentIcon as FIF,
    InfoBar,
    InfoBarPosition,
    LineEdit as QLineEdit,
    PlainTextEdit as QPlainTextEdit,
    PushButton as QPushButton,
    setThemeColor,
    ToolButton,
    qconfig,
)

from krok_helper.qfluent_compat import (
    apply_qfluent_menu_lifetime_patch,
    apply_qfluent_tooltip_parent_patch,
    ask_fluent_confirm,
    show_fluent_error,
    show_fluent_info,
    show_fluent_warning,
)
from krok_helper.alignment import AlignmentHandoffDialog
from krok_helper.alignment.page import AlignmentPage
from krok_helper.global_settings.page import SettingsDialogs
from krok_helper.hires.page import DropZoneCard, HiResPage
from krok_helper.lyrics_search.page import LyricsSearchPage
from krok_helper.config import (
    APP_LOGO_PATH,
    APP_TITLE,
    APP_VERSION,
    WINDOW_HEIGHT,
    WINDOW_MIN_HEIGHT,
    WINDOW_MIN_WIDTH,
    WINDOW_WIDTH,
)
from krok_helper.errors import ProcessingError
from krok_helper.ffmpeg import find_tool, probe_media
from krok_helper.pipeline import (
    DEFAULT_OFF_NAME_TEMPLATE,
    DEFAULT_ON_NAME_TEMPLATE,
    OUTPUT_NAME_MODE_FIXED,
    OUTPUT_NAME_MODE_TEMPLATE,
    OUTPUT_NAME_MODE_VIDEO_NAME,
    validate_output_name_template,
)
from krok_helper.settings import (
    AppSettings,
    consume_corruption_backup,
    get_settings_path,
    load_app_settings,
    migrate_strange_uta_game_settings,
    save_app_settings,
    sync_baseline_field,
)
from krok_helper.background import BackgroundTask
from krok_helper.media_formats import (
    HIRES_AUDIO_EXTENSIONS,
    format_media_duration,
)
from krok_helper.sug_compat import apply_sug_compat_patches
from krok_helper.workflow_bar import (
    WORKFLOW_STEPS,
    WorkflowStepper,
)
from krok_helper.workflow import (
    WORKFLOW_HIRES_MIX,
    WORKFLOW_LYRICS_SEARCH,
    WORKFLOW_LYRICS_TIMING,
    WORKFLOW_SUBTITLE_RENDER,
    WORKFLOW_VIDEO_DOWNLOAD,
    WORKFLOW_WAVEFORM_ALIGN,
)
from krok_helper.ui_kit import (
    CardWidget,
    DEFAULT_UI_FONT_FAMILIES,
    ElidedLabel,
    build_app_ui_font,
)
from krok_helper.updater import CheckResult, UpdateChecker, ensure_updater_settings
from krok_helper.updater.dialogs import WorkbenchUpdateDialog
from krok_helper.updater.settings import UpdaterSettings
from krok_helper.updater.sources import SOURCE_LABELS
from krok_helper.video_download import VideoDownloadPage
from krok_helper.windows import set_explicit_app_user_model_id

apply_qfluent_menu_lifetime_patch()
apply_qfluent_tooltip_parent_patch()


#: 换页动画 240ms；转交提示要等它跑完再弹，否则 InfoBar 的滑入动画会被卡住。
PAGE_TRANSITION_SETTLE_MS = 320
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
        config = deepcopy(latest.lyrics_timing)
        self._fill_sparse_shortcut_defaults(config)
        self.inject_host_managed_settings(config)
        self._app_settings.lyrics_timing = config
        sync_baseline_field(self._app_settings, "lyrics_timing")
        return deepcopy(self._app_settings.lyrics_timing)

    def save(self, data: dict) -> None:
        latest = load_app_settings()
        config = deepcopy(data)
        self.inject_host_managed_settings(config)
        latest.lyrics_timing = config
        # 这里要写的正是模块命名空间，合并回盘上的旧值等于把自己的改动丢掉。
        save_app_settings(latest, merge_module_namespaces=False)
        self._app_settings.lyrics_timing = deepcopy(latest.lyrics_timing)
        # 宿主的整份写盘不该再把这一段当成"自己的改动"端出去。
        sync_baseline_field(self._app_settings, "lyrics_timing")

    def save_partial(self, changes: dict[str, object]) -> None:
        latest = load_app_settings()
        config = deepcopy(latest.lyrics_timing)
        for path, value in changes.items():
            self._set_nested(config, path, value)
        self.inject_host_managed_settings(config)
        latest.lyrics_timing = config
        save_app_settings(latest, merge_module_namespaces=False)
        self._app_settings.lyrics_timing = deepcopy(config)
        sync_baseline_field(self._app_settings, "lyrics_timing")

    def load_extra(self, key: str, default):
        field_name = self._EXTRA_FIELDS.get(key)
        if field_name is None:
            return deepcopy(default)
        latest = load_app_settings()
        setattr(self._app_settings, field_name, deepcopy(getattr(latest, field_name, default)))
        sync_baseline_field(self._app_settings, field_name)
        return deepcopy(getattr(self._app_settings, field_name, default))

    def save_extra(self, key: str, data) -> None:
        field_name = self._EXTRA_FIELDS.get(key)
        if field_name is None:
            return
        latest = load_app_settings()
        setattr(latest, field_name, deepcopy(data))
        save_app_settings(latest, merge_module_namespaces=False)
        setattr(self._app_settings, field_name, deepcopy(data))
        sync_baseline_field(self._app_settings, field_name)

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

    @staticmethod
    def _fill_sparse_shortcut_defaults(config: dict) -> None:
        """Preserve mode-specific empty bindings in migrated sparse settings."""
        shortcuts = config.setdefault("shortcuts", {})
        if not isinstance(shortcuts, dict):
            return
        edit_mode = shortcuts.setdefault("edit_mode", {})
        if not isinstance(edit_mode, dict):
            return
        # SUG <= 1.5.1 falls back a missing edit-mode value to the timing-mode
        # default (Backspace -> delete_timestamp).  An explicit empty binding
        # is semantically different from a missing value and must survive the
        # host's sparse settings migration.
        edit_mode.setdefault("delete_timestamp", "")

    def inject_host_managed_settings(self, config: dict) -> None:
        """Overlay settings owned by the workbench onto SUG's namespace.

        These values deliberately do not use the copy already stored below
        ``lyrics_timing``.  That copy may be stale during initial construction
        or after SUG saves an old settings snapshot.
        """
        ffmpeg_path = _resolve_sug_ffmpeg_path(self._app_settings.ffmpeg_dir)
        self._set_nested(config, "tools.ffmpeg_path", ffmpeg_path)

        proxy = UpdaterSettings.load(self._app_settings)
        self._set_nested(config, "updater.proxy.mode", proxy.proxy_mode)
        self._set_nested(config, "updater.proxy.manual_url", proxy.proxy_manual_url)


def _resolve_sug_ffmpeg_path(ffmpeg_dir_text: str) -> str:
    ffmpeg_dir = Path(ffmpeg_dir_text).expanduser() if ffmpeg_dir_text.strip() else None
    try:
        return find_tool("ffmpeg", ffmpeg_dir)
    except ProcessingError:
        if os.name == "nt":
            try:
                return find_tool("ffmpeg.exe", ffmpeg_dir)
            except ProcessingError:
                pass
    return "ffmpeg"


TASKBAR_LOGO_PATH = Path(__file__).resolve().parent / "assets" / "logo" / "logo2.png"
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


def sync_fluent_ui_fonts() -> None:
    qconfig.set(qconfig.fontFamilies, DEFAULT_UI_FONT_FAMILIES, save=False)


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
    """工作台主窗口（外壳）。

    只负责壳的事：工作流步条与换页、主题、快捷键作用域、崩溃恢复、更新检查，
    以及把各页产物在步骤之间转交。

    四个页面都是独立对象（``align_page`` / ``lyrics_page`` / ``hires_page``
    与 ``_settings_dialogs``），本类同时实现它们各自的宿主契约 ——
    ``AlignmentHost`` / ``LyricsSearchHost`` / ``HiResHost`` / ``SettingsHost``，
    页面只能透过这些接口回头找外壳。
    """
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
        self._update_checker: UpdateChecker | None = None
        self._update_launch_worker = None
        self._update_progress_win = None
        self._force_quitting_for_update = False
        self._update_exit_prepared = False
        self.active_module = WORKFLOW_VIDEO_DOWNLOAD
        self._loading_settings_into_ui = True
        #: 设置是否已经灌进界面。为假时 :meth:`_save_all_settings` 只落盘不收集。
        self._settings_loaded = False

        self.output_name_mode_value = OUTPUT_NAME_MODE_FIXED
        self.on_name_template_value = DEFAULT_ON_NAME_TEMPLATE
        self.off_name_template_value = DEFAULT_OFF_NAME_TEMPLATE
        self.ffmpeg_dir_text = ""
        self._media_duration_cache: dict[Path, str] = {}
        self._restoring_from_maximized = False
        self._startup_geometry_applied = False
        self._page_transition_overlay: PageTransitionOverlay | None = None
        self._page_switch_anim: QPropertyAnimation | None = None
        self._settings_dialogs = SettingsDialogs(host=self, parent=self)

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

        QTimer.singleShot(800, self._check_lyrics_timing_crash_recovery)
        QTimer.singleShot(1200, self._check_subtitle_render_crash_recovery)
        QTimer.singleShot(1500, self._notify_settings_corruption_if_any)
        QTimer.singleShot(2500, self._check_for_workbench_update_on_startup)


    # ── HiResHost 实现 ───────────────────────────────────────────
    # 这几个是「页面 → 外壳」的公开接口（见 krok_helper.hires.page.HiResHost）。
    # 其余还是 mixin 的页面仍直接用下划线私有方法，等它们各自对象化时一并收口。

    def track_background_task(self, task: BackgroundTask) -> BackgroundTask:
        task.finished.connect(task.deleteLater)
        return task

    # ── AlignmentHost 实现 ───────────────────────────────────────

    def set_panel_enabled(self, panel, enabled: bool) -> None:
        self._set_panel_enabled(panel, enabled)

    def build_media_info(self, path: Path | None, label: str) -> str:
        return self._build_media_info(path, label)

    def focused_widget_is_text_input(self) -> bool:
        return self._focused_widget_is_text_input()

    # ── SettingsHost 实现 ────────────────────────────────────────

    def sync_ffmpeg_labels(self) -> None:
        self._sync_ffmpeg_labels()

    def sync_lyrics_timing_host_paths(self) -> None:
        self._sync_lyrics_timing_host_paths()

    def start_workbench_update_check(
        self,
        *,
        manual: bool,
        updater_settings=None,
        status_label=None,
        trigger_button=None,
    ) -> None:
        self._start_workbench_update_check(
            manual=manual,
            updater_settings=updater_settings,
            status_label=status_label,
            trigger_button=trigger_button,
        )

    def build_alignment_settings_fragment(self, parent=None):
        """全局设置里「波形对齐」那一页的内容，向对齐页要。"""
        return self.align_page.build_settings_fragment(parent)

    def collect_page_settings(self) -> None:
        """写盘前把各页当前的设置收进 settings —— 收谁归外壳管。"""
        for page in self._workflow_pages():
            page.collect_settings()

    # ── LyricsSearchHost 实现 ────────────────────────────────────

    def install_single_click_combo_behavior(self, combo) -> None:
        self._install_single_click_combo_behavior(combo)

    def import_current_lyrics_to_timing(self) -> None:
        self._import_current_lyrics_to_timing()

    def resolve_ffmpeg_dir(self) -> Path | None:
        return self._resolve_ffmpeg_dir()

    def resolve_output_name_mode(self) -> str:
        return self._resolve_output_name_mode()

    def resolve_output_name_templates(self, *, require_valid: bool = False) -> tuple[str, str]:
        return self._resolve_output_name_templates(require_valid=require_valid)

    def notify_handoff(self, title: str, content: str) -> None:
        self._notify_handoff(title, content)

    def _open_settings_window(self, context: str) -> None:
        # 对齐页还是 mixin，仍用这个私有名字；它对象化后改走 open_settings_window。
        self._settings_dialogs.open_page_settings(context)

    def open_settings_window(self, context: str) -> None:
        self._settings_dialogs.open_page_settings(context)

    # ── 工作流转交入口（WorkflowHost）：素材落在 Hi-Res 页 ──────────

    def set_video_path(self, path: Path) -> None:
        self.hires_page.set_video_path(path)

    def set_on_vocal_path(self, path: Path) -> None:
        self.hires_page.set_on_vocal_path(path)

    def set_off_vocal_path(self, path: Path) -> None:
        self.hires_page.set_off_vocal_path(path)

    def add_off_vocal_paths(self, paths: Sequence[Path]) -> list[Path]:
        return self.hires_page.add_off_vocal_paths(paths)

    def accept_separated_accompaniment(self, paths: Sequence[Path]) -> list[Path]:
        return self.hires_page.accept_separated_accompaniment(paths)

    def accept_source_as_on_vocal(self, path: Path) -> bool:
        return self.hires_page.accept_source_as_on_vocal(path)

    def _workflow_pages(self) -> list:
        """已经建好的工作流页面。

        外壳跟页面打交道就走这一条（重绘、收设置、查后台任务），不再按方法名
        去 ``getattr`` —— 名字对不上时那种写法是静默空转，出过事。
        """
        pages = []
        for name in ("align_page", "lyrics_page", "hires_page"):
            page = getattr(self, name, None)
            if page is not None:
                pages.append(page)
        return pages

    def _running_background_tasks(self) -> list[BackgroundTask]:
        tasks: list[BackgroundTask] = []
        # 每个页面自己持有后台任务，关窗前挨个问。
        tasks_by_page = (page.running_tasks() for page in self._workflow_pages())
        for running in tasks_by_page:
            tasks.extend(running)
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
        page = getattr(self, "lyrics_page", None)
        if page is not None:
            page.refresh_layout_direction()

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
            # 状态驱动的样式（依据 in-memory 计数/选中态生成 QSS）需要在主题切换时
            # 重跑，因为 themed() 只覆盖"恒等 lambda"场景。这活儿归各页自己 ——
            # 以前是外壳按**方法名**去 getattr 自己身上的一串私有方法，页面搬走之后
            # 那串名字全成了 None，配着 `if fn is None: continue` 一声不响地空转，
            # 换主题后对齐页的卡片/模式按钮/导出面板就一直留在旧配色里。
            for page in self._workflow_pages():
                try:
                    page.rerender_after_theme_change()
                except Exception:
                    logging.getLogger(__name__).warning("页面重绘失败：%s", type(page).__name__, exc_info=True)
        except Exception:
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
        self.global_settings_button.clicked.connect(self._settings_dialogs.open_global_settings)
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
        self.align_page = AlignmentPage(host=self, parent=self.page_stack)
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
        self.lyrics_page = LyricsSearchPage(host=self, parent=self.page_stack)
        self._sync_lyrics_timing_host_paths()
        ensure_sug_src_path()
        apply_sug_compat_patches()
        from strange_uta_game.frontend.main_window import MainWindow as LyricsTimingMainWindow

        lyrics_timing_settings = KrokHelperSettingsBridge(self.settings, self._save_all_settings)
        self.lyrics_timing_settings_bridge = lyrics_timing_settings
        self.lyrics_timing_page = LyricsTimingMainWindow.for_embedding(
            parent=self.page_stack,
            settings_provider=lyrics_timing_settings,
            ai_timing_host=self._build_ai_timing_host(),
        )
        # EMBEDDING §8：SUG 字体缓存后台预热（幂等）。embedded 构造路径不会
        # 自行预热，宿主补调一次，把字体枚举成本从首次打开字体选择器挪到
        # 启动空闲期。
        try:
            from strange_uta_game.frontend import font_cache as sug_font_cache

            sug_font_cache.prewarm_async()
        except Exception:
            logging.getLogger(__name__).warning("SUG 字体缓存预热调度失败", exc_info=True)
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

        from krok_helper.subtitle_render import create_embedded_subtitle_render
        from krok_helper.subtitle_render.settings_bridge import (
            KrokHelperSubtitleRenderSettingsBridge,
        )

        self.subtitle_render_settings_bridge = KrokHelperSubtitleRenderSettingsBridge(
            self.settings, self._save_all_settings
        )
        self.subtitle_render_page = create_embedded_subtitle_render(
            parent=self.page_stack,
            settings_provider=self.subtitle_render_settings_bridge,
            workflow_context=self,
        )
        try:
            self.subtitle_render_page.connect_project_state_changed(
                self._on_subtitle_project_state_changed
            )
            self._on_subtitle_project_state_changed(
                self.subtitle_render_page.project_state()
            )
        except Exception:
            pass
        self.hires_page = HiResPage(host=self, parent=self.page_stack)
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
        module_changed = previous_module != module_id
        if module_changed and previous_module == WORKFLOW_LYRICS_TIMING:
            self._notify_lyrics_timing_host_visibility(False)
        # 离开波形对齐页就把预览停掉。这一段原先查的是外壳自己的
        # ``align_preview_process`` —— 页面搬走之后它恒为 None，条件永远不成立，
        # 切到别的步骤后 ffplay 会一直响。
        align_page = getattr(self, "align_page", None)
        if (
            previous_module == WORKFLOW_WAVEFORM_ALIGN
            and module_id != WORKFLOW_WAVEFORM_ALIGN
            and align_page is not None
        ):
            align_page.stop_preview()
        self.active_module = module_id
        # 旧页快照必须抢在 setCurrentWidget 之前拍 —— 之后它就被 stack 隐藏了。
        # getattr 容忍测试里的 SimpleNamespace 假 app
        capture_outgoing = getattr(self, "_capture_outgoing_page", None)
        outgoing = capture_outgoing(previous_module, module_id) if capture_outgoing is not None else None
        self._sync_page_stack_margins(module_id)
        self.page_stack.setCurrentWidget(self.module_pages[module_id])
        animate_page = getattr(self, "_animate_page_switch", None)
        if animate_page is not None and outgoing is not None:
            animate_page(self.module_pages[module_id], outgoing)
        self.workflow_stepper.setCurrentModule(module_id)
        self._sync_workflow_shortcut_scope()
        if module_changed and module_id == WORKFLOW_LYRICS_TIMING:
            self._notify_lyrics_timing_host_visibility(True)

    def _notify_lyrics_timing_host_visibility(self, visible: bool) -> None:
        """Forward the host workflow visibility lifecycle to embedded SUG."""
        timing_page = getattr(self, "lyrics_timing_page", None)
        callback = getattr(timing_page, "on_host_visibility_changed", None)
        if not callable(callback):
            return
        try:
            callback(bool(visible))
        except Exception:
            logging.getLogger(__name__).warning(
                "通知歌词打轴模块宿主可见性失败: visible=%s",
                visible,
                exc_info=True,
            )

    def _capture_outgoing_page(
        self, previous_module: str | None, module_id: str
    ) -> tuple[QPixmap, int] | None:
        """拍下即将离场页面的快照，并算出滑动方向。

        必须在 ``setCurrentWidget`` 之前调用。返回 ``None`` 表示这次切换不做
        动画（窗口没显示、没有上一页、或前后是同一页）。
        """
        # getattr 容忍测试里的 SimpleNamespace 假 app
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

    def _notify_handoff(self, title: str, content: str) -> None:
        """转交产物时右下角提示一条。

        各条转交链路都只放素材、不跳页面，界面上因此没有任何动静 —— 用户点完
        「交给下一步」会怀疑到底有没有生效。这条提示就是唯一的反馈。

        提示本身不属于转交流程，弹不出来（窗口正在关、Fluent 内部状态异常）
        也不该把素材投放一起带崩，所以整体兜底。

        **挂在 ``page_stack`` 上，不要挂主窗口或 centralWidget**：实测那两处
        InfoBar 的滑入动画不推进（``slideAni`` 一直停在 currentTime=0），提示
        条就卡在起始位置 —— 也就是窗口右边缘之外 —— 用户什么也看不到。挂
        ``page_stack`` 动画正常，位置也更合理：贴在内容区右下角。

        换页动画在跑的时候同样会把滑入动画卡住（``accept_subtitle_video`` 就是
        先跳页再提示），所以这种情况下等它跑完再弹。
        """
        if getattr(self, "_page_switch_anim", None) is not None:
            QTimer.singleShot(
                PAGE_TRANSITION_SETTLE_MS,
                lambda: self._show_handoff_toast(title, content),
            )
            return
        self._show_handoff_toast(title, content)

    def _show_handoff_toast(self, title: str, content: str) -> None:
        host = getattr(self, "page_stack", None) or self
        try:
            bar = InfoBar.success(
                title=title,
                content=content,
                parent=host,
                position=InfoBarPosition.BOTTOM_RIGHT,
                duration=3000,
            )
            # 保险：Fluent 的滑入动画在本工作台里时灵时不灵（换页刚跑完那一小段
            # 尤其明显，实测能拖到两秒后才推进第一帧），期间提示条一直停在起始
            # 位置 —— 也就是内容区右边缘之外，等于没弹。这里直接把它摆到动画
            # 自己算好的终点：动画真跑起来也是走到同一个位置，不冲突，多条并存
            # 时的堆叠偏移也由库算好了。
            ani = bar.property("slideAni")
            if ani is not None and ani.endValue() is not None:
                bar.move(ani.endValue())
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).debug("转交提示弹出失败", exc_info=True)

    def accept_subtitle_video(self, path: Path) -> None:
        """第 5 步渲染好的成片放进第 6 步的视频卡，并切到第 6 步。

        **这条链路是要跳页的**，与另外几条（对齐、分离）刻意不同：渲染完成
        意味着这一版成片已经定稿，下一步就是混流；而分离/对齐往往还要在原地
        接着处理下一首，跳走反而打断。
        """
        self.set_video_path(path)
        self._show_module(WORKFLOW_HIRES_MIX)
        self._notify_handoff("成片已交给下一步", f"「{path.name}」已放入第 6 步 Hi-Res 混流的视频卡。")

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
        show_fluent_error(
            self,
            f"不支持的项目文件：\n{project_path}\n\n支持 .sug 和 .yurika 项目。",
        )

    def open_lyrics_timing_project(self, project_path: Path) -> None:
        project_path = project_path.expanduser()
        if project_path.suffix.lower() != ".sug":
            show_fluent_error(self, f"不支持的项目文件:\n{project_path}")
            return
        if not project_path.is_file():
            show_fluent_error(self, f"项目文件不存在:\n{project_path}")
            return

        lyrics_timing_page = getattr(self, "lyrics_timing_page", None)
        if lyrics_timing_page is None or not hasattr(lyrics_timing_page, "open_initial_project"):
            show_fluent_error(self, "打轴模块尚未准备好，无法打开 .sug 项目。")
            return

        self._show_module(WORKFLOW_LYRICS_TIMING)
        lyrics_timing_page.open_initial_project(str(project_path))

    def open_subtitle_render_project(self, project_path: Path) -> None:
        project_path = project_path.expanduser()
        if project_path.suffix.lower() != ".yurika":
            show_fluent_error(self, f"不支持的项目文件:\n{project_path}")
            return
        if not project_path.is_file():
            show_fluent_error(self, f"项目文件不存在:\n{project_path}")
            return

        subtitle_render_page = getattr(self, "subtitle_render_page", None)
        if subtitle_render_page is None or not hasattr(
            subtitle_render_page, "open_initial_project"
        ):
            show_fluent_error(
                self,
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
        """把界面上的值收进 ``settings`` 并落盘。

        **界面还没灌过设置时只落盘、不收集**：``_build_ui`` 期间任何一个页面都可能
        叫到这里（音频分离页建后端时就会），那时界面上全是默认值，一收集就把用户
        上次的配置整份冲掉 —— 而 ``_load_settings_into_ui`` 排在 ``_build_ui``
        后面，随后那次"加载"读到的已经是被自己冲掉的那份。

        这一段照样落盘：页面在构造期间写进 ``settings`` 的东西（比如分离页的
        ``pymss`` 命名空间）该存还是要存，只是不能拿空界面去覆盖别人的字段。
        """
        if self._settings_loaded:
            self.settings.output_name_mode = self.output_name_mode_value
            self.settings.on_name_template = self.on_name_template_value
            self.settings.off_name_template = self.off_name_template_value
            self.settings.ffmpeg_dir = self.ffmpeg_dir_text
            self.collect_page_settings()
        self._sync_lyrics_timing_host_paths()
        return save_app_settings(self.settings)

    def _bind_shortcuts(self) -> None:
        # Ctrl+S 是跨模块的（对齐导出 / 打轴保存），留在外壳；
        # 其余三个只在波形对齐页有意义，由页面自己绑。
        self.shortcut_export = QShortcut(QKeySequence("Ctrl+S"), self)
        self.shortcut_export.activated.connect(self._handle_export_or_save_shortcut)
        self._sync_workflow_shortcut_scope()

    def _sync_workflow_shortcut_scope(self) -> None:
        if not hasattr(self, "shortcut_export"):
            return
        align_active = self.active_module == WORKFLOW_WAVEFORM_ALIGN
        timing_active = self.active_module == WORKFLOW_LYRICS_TIMING
        align_page = getattr(self, "align_page", None)
        if align_page is not None:
            align_page.sync_shortcut_scope(align_active)
        self.shortcut_export.setEnabled(align_active or timing_active)

    def _focused_widget_is_text_input(self) -> bool:
        widget = QApplication.focusWidget()
        return isinstance(widget, (QLineEdit, QPlainTextEdit, QComboBox, QDoubleSpinBox))

    def _handle_export_or_save_shortcut(self) -> None:
        if self.active_module == WORKFLOW_LYRICS_TIMING:
            lyrics_timing_page = getattr(self, "lyrics_timing_page", None)
            if lyrics_timing_page is not None and hasattr(lyrics_timing_page, "trigger_save"):
                lyrics_timing_page.trigger_save()
            return
        if self.active_module != WORKFLOW_WAVEFORM_ALIGN or self._focused_widget_is_text_input():
            return
        align_page = getattr(self, "align_page", None)
        if align_page is not None:
            align_page.trigger_export()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if (
            watched is getattr(self, "page_stack", None)
            and event.type() == QEvent.Type.Resize
            and getattr(self, "_page_switch_anim", None) is not None
        ):
            # 快照是按旧尺寸拍的，拉伸后继续播只会拉花，直接收尾露出真实页面
            self._end_page_transition()
        return super().eventFilter(watched, event)


    def _import_current_lyrics_to_timing(self) -> None:
        """把歌词检索页选中的那条交给第 4 步打轴。

        "有没有可导入的歌词"由检索页自己判断（它持有候选与预览）；这里只管
        装进打轴模块并切页。
        """
        lyrics_text = self.lyrics_page.current_lyrics_for_timing()
        if lyrics_text is None:
            return
        lyrics_timing_page = getattr(self, "lyrics_timing_page", None)
        if lyrics_timing_page is None or not hasattr(lyrics_timing_page, "import_lyrics_from_text"):
            show_fluent_error(self, "打轴模块尚未准备好，无法导入歌词。")
            return
        try:
            imported = bool(lyrics_timing_page.import_lyrics_from_text(lyrics_text))
        except Exception as exc:
            show_fluent_error(self, f"导入到打轴失败：\n{exc}")
            return
        if not imported:
            return
        self._show_module(WORKFLOW_LYRICS_TIMING)

    def _set_panel_enabled(self, panel: QWidget, enabled: bool):
        for w in panel.findChildren(QWidget):
            w.setEnabled(enabled)

    def _load_settings_into_ui(self) -> None:
        self._loading_settings_into_ui = True
        # 收集方向在这之前一律禁止，见 :meth:`_save_all_settings`。
        self._settings_loaded = False
        self.set_ffmpeg_dir(Path(self.settings.ffmpeg_dir) if self.settings.ffmpeg_dir.strip() else None)
        self.set_output_name_mode(self.settings.output_name_mode)
        self.set_output_name_templates(self.settings.on_name_template, self.settings.off_name_template)
        align_page = getattr(self, "align_page", None)
        if align_page is not None:
            align_page.load_settings()
        lyrics_page = getattr(self, "lyrics_page", None)
        if lyrics_page is not None:
            lyrics_page.restore_preferences()
        self._loading_settings_into_ui = False
        self._settings_loaded = True

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

    def _sync_ffmpeg_labels(self) -> None:
        page = getattr(self, "hires_page", None)
        if page is not None:
            page.set_ffmpeg_dir_text(self.ffmpeg_dir_text)
        align_page = getattr(self, "align_page", None)
        if align_page is not None:
            align_page.refresh_media_info()

    def set_ffmpeg_dir(self, path: Path | None) -> None:
        self.ffmpeg_dir_text = str(path) if path is not None else ""
        self.settings.ffmpeg_dir = self.ffmpeg_dir_text
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
        # Fluent 对话框没有 informativeText 那一层，正文直接拼在一起。
        show_fluent_warning(
            self,
            "检测到上次的配置文件 settings.json 损坏，已使用默认值重建。\n\n"
            f"原文件已备份到：\n{backup}\n\n"
            "打轴模块的设置 / 词典 / 演唱者 / 网络词典缓存如丢失，可在「全局设置 → "
            "工具 → 打轴模块数据导入」从原 StrangeUtaGame 目录或备份中恢复。",
        )

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

    def _build_ai_timing_host(self):
        """构建注入 SUG「AI 打轴」的宿主能力（阶段 G 嵌入契约）。

        失败时返回 None——SUG 回落 standalone 默认配置，绝不阻塞工作台启动。
        """
        try:
            from krok_helper.audio_processing.separation.ai_timing_host import (
                KaraokeAiTimingHost,
            )

            separation_page = getattr(self, "audio_separation_page", None)
            if getattr(separation_page, "backend", None) is None:
                return None

            def _open_separation_page():
                # SUG AI 打轴引导「去音频分离」：切到第 2 步并选中分离页签
                self._show_module(WORKFLOW_WAVEFORM_ALIGN)
                wrapper = getattr(self, "audio_processing_page", None)
                switch_tab = getattr(wrapper, "switch_tab", None)
                if callable(switch_tab):
                    from krok_helper.audio_processing.page import TAB_SEPARATION

                    switch_tab(TAB_SEPARATION)

            # 传 getter 而非实例：PyMSS/MSST 模式切换会整体替换后端对象
            cache_root = get_settings_path().parent / "lyrics_timing_cache"
            return KaraokeAiTimingHost(
                lambda: separation_page.backend,
                cache_root,
                page=separation_page,
                navigate=_open_separation_page,
            )
        except Exception:
            logging.getLogger(__name__).warning(
                "构建 AI 打轴宿主能力失败，SUG 将使用独立默认配置", exc_info=True
            )
            return None

    def _sync_lyrics_timing_host_paths(self) -> None:
        """Inject host-managed runtime settings into the embedded timing module."""
        cache_dir = get_settings_path().parent / "lyrics_timing_cache"
        os.environ["SUG_CACHE_DIR"] = str(cache_dir)

        bridge = getattr(self, "lyrics_timing_settings_bridge", None)
        if bridge is None:
            bridge = KrokHelperSettingsBridge(self.settings, self._save_all_settings)
        bridge.inject_host_managed_settings(self.settings.lyrics_timing)

        timing_page = getattr(self, "lyrics_timing_page", None)
        setting_interface = getattr(timing_page, "settingInterface", None)
        settings = setting_interface.get_settings() if setting_interface is not None else None
        if settings is not None:
            settings.reload()

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
                show_fluent_error(self, "检查更新失败：返回结果无效。")
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
                show_fluent_error(self, f"{result.error}\n\n{details}" if details else result.error)
            return
        if not result.has_update or result.release is None:
            if status_label is not None:
                remote = result.release.version if result.release is not None else APP_VERSION
                status_label.setText(f"已是最新版本。当前 v{APP_VERSION}，远端 v{remote}。")
            elif manual:
                show_fluent_info(self, f"当前已经是最新版本：v{APP_VERSION}")
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
        from krok_helper.subtitle_render.frontend.fluent_dialogs import fluent_choice

        source_label = SOURCE_LABELS.get(result.primary_source, result.primary_source)
        body = release.body.strip()
        # Fluent 对话框没有 QMessageBox 的 informativeText / detailedText 分层，
        # 版本信息直接拼进正文；更新说明太长会撑爆弹窗，截断后引导去 Release 页看。
        if len(body) > 600:
            body = body[:600].rstrip() + "\n……（完整更新说明见 Release 页面）"
        content = (
            f"当前版本 v{APP_VERSION}\n"
            f"发布于 {release.published_at[:10] or '未知日期'}\n"
            f"下载源：{source_label}"
        )
        if body:
            content += f"\n\n{body}"
        choice = fluent_choice(
            self,
            f"发现工作台新版本 v{release.version}",
            content,
            ("立即更新", "跳过此版本", "稍后再说"),
            default=0,
        )
        if choice == 1:
            settings.skipped_version = release.version
            settings.save(self.settings)
            return
        if choice == 0:
            self._launch_workbench_updater(result)

    def _launch_workbench_updater(self, result: CheckResult) -> None:
        if not result.release or not result.download_candidates:
            show_fluent_error(self, "缺少更新下载信息，请到 GitHub Release 手动下载。")
            return
        try:
            from krok_helper.updater import installer
            from krok_helper.updater.progress_window import UpdateProgressWindow
            from krok_helper.updater.worker import LaunchUpdaterWorker
        except Exception as exc:  # noqa: BLE001
            show_fluent_error(self, f"无法加载更新器：\n{exc}")
            return
        if not installer.is_updater_available():
            show_fluent_info(self, "缺少 Updater.exe。请到 GitHub Release 手动下载最新版本。")
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
                show_fluent_error(
                    self, f"无法启动 Updater：\n{getattr(lr, 'reason', '未知错误')}"
                )
                return
            self.request_force_quit()

        worker.done.connect(_on_launch_done, Qt.ConnectionType.QueuedConnection)
        worker.start()

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

    def _resolve_ffmpeg_dir(self) -> Path | None:
        if not self.ffmpeg_dir_text.strip():
            return None
        path = Path(self.ffmpeg_dir_text).expanduser()
        if not path.is_dir():
            raise ProcessingError("所选 ffmpeg 目录无效，请重新选择。")
        return path

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

        align_page = getattr(self, "align_page", None)
        if align_page is not None:
            align_page.stop_preview()
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
            show_fluent_info(self, "当前后台任务仍在运行，请等待完成后再关闭窗口。")
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
        align_page = getattr(self, "align_page", None)
        if align_page is not None:
            align_page.stop_preview()
        if not self._shutdown_project_modules(event):
            return
        if not self._shutdown_audio_separation():
            show_fluent_warning(
                self,
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
            show_fluent_error(self, f"{label}模块无法保存当前项目。")
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
                show_fluent_error(
                    self,
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
            show_fluent_error(self, f"{label}项目保存失败：\n{exc}")
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
                show_fluent_error(
                    None,
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
