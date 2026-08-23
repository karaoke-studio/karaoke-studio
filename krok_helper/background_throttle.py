"""工作台前后台 UI 节流。

判定标准不是「应用是否拥有焦点」：用户可能把主窗口摆在屏幕一边看字幕
预览、在另一边干别的事（窗口可见但失焦），此时 UI 刷新必须全速。真正的
后台是窗口最小化/严格隐藏，或页面被切走——没人可能看到画面，全速刷新
纯属浪费 CPU。

与 SUG 的 ``strange_uta_game.frontend.background_throttle`` 同一思路：
在 QApplication 上安装事件过滤器，监听窗口 Show/Hide/最小化/页面显隐等
事件后广播 ``visibility_maybe_changed``，消费方各自按自身 widget 重估。

任务线程（导出 / 下载 / 分离 worker、QMediaPlayer、异步字幕渲染线程）
不经这里，后台照常全速；本模块只管纯 UI 的定时器与动画：

- 分离页任务面板忙碌条 / 已用时文本（``separation.widgets.CurrentTaskPanel``）
- 字幕预览传输条 FPS 读数（``subtitle_render.frontend.preview_view``）
- 导出监视器预览图轮询（``subtitle_render.frontend.main_window``）

播放时钟（preview_view 的 ``_tick_timer`` / ``_position_poll_timer``）
刻意不接：预览播放属于不可节流路径——分屏场景下悬浮预览窗仍可见，
时钟停了字幕填色就断流。
"""

from __future__ import annotations

from typing import Callable, Optional

from PyQt6.QtCore import QEvent, QObject, pyqtSignal

# 触发重估的窗口事件。ShowToParent/HideToParent 覆盖页面（QStackedWidget
# 换页）显隐；WindowStateChange 覆盖最小化/还原（此时子 widget 不发 Hide）。
_REEVALUATE_EVENTS = frozenset(
    {
        QEvent.Type.Show,
        QEvent.Type.Hide,
        QEvent.Type.ShowToParent,
        QEvent.Type.HideToParent,
        QEvent.Type.WindowStateChange,
    }
)


class BackgroundThrottle(QObject):
    """跟踪「还有没有人能看到我们的 UI」并广播给需要降频的服务。"""

    visibility_maybe_changed = pyqtSignal()

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self._app = app
        app.applicationStateChanged.connect(self._refresh_visibility)
        app.installEventFilter(self)

    @property
    def is_visible(self) -> bool:
        """应用还有任何可见且未最小化的顶层窗口（悬浮预览窗可见即前台）。"""
        try:
            widgets = self._app.topLevelWidgets()
        except RuntimeError:
            return True
        return any(w.isVisible() and not w.isMinimized() for w in widgets)

    def eventFilter(self, obj, event) -> bool:
        if event.type() in _REEVALUATE_EVENTS and obj.isWidgetType():
            self._refresh_visibility()
        return super().eventFilter(obj, event)

    def _refresh_visibility(self, *args) -> None:
        # 无条件广播：各消费方按自己的 widget（而非全局可见性）重估，
        # 例如主窗口最小化但悬浮预览窗仍可见时两者需求不同。
        self.visibility_maybe_changed.emit()


_instance: Optional[BackgroundThrottle] = None


def background_throttle() -> Optional[BackgroundThrottle]:
    """进程级单例；QApplication 尚未创建（纯后端/测试场景）时返回 None。

    返回 None 时调用方跳过接入即可——定时器保持全速，行为与本模块
    存在前完全一致，是最安全的退化路径。
    """
    global _instance
    if _instance is None:
        from PyQt6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is None:
            return None
        _instance = BackgroundThrottle(app)
    return _instance


def ui_active(widget) -> bool:
    """widget 自身可见且其顶层窗口未最小化（不看焦点）。

    嵌入 QStackedWidget 的页面 widget 的 ``window()`` 解析到宿主主窗口，
    因此「页面被切走」（isVisible → False）与「窗口最小化」都判不活跃；
    窗口可见但失焦仍判活跃。该表达式不依赖单例，永远可用。
    """
    return widget.isVisible() and not widget.window().isMinimized()


class _GuardedEntry:
    """一条被 :class:`UiActivityGuard` 管理的活动（定时器/动画）。"""

    def __init__(
        self,
        guard: "UiActivityGuard",
        start: Callable[[], None],
        stop: Callable[[], None],
        is_running: Callable[[], bool],
        on_resume: Optional[Callable[[], None]],
    ):
        self._guard = guard
        self._start = start
        self._stop = stop
        self._is_running = is_running
        self._on_resume = on_resume
        # 业务尚未表态时，沿用对象当前状态作为期望值。
        self.desired = is_running()

    def set_desired(self, running: bool) -> None:
        """业务侧声明「想让这个活动跑/停」，是否真跑由当前可见性决定。"""
        self.desired = bool(running)
        self._guard.apply(self)

    def _resume(self) -> None:
        self._start()
        if self._on_resume is not None:
            self._on_resume()


class UiActivityGuard(QObject):
    """把定时器/动画的启停挂在 widget 可见性上。

    用法：``entry = guard.manage(timer, on_resume=refresh)``，业务侧只在
    任务开始/结束时调 ``entry.set_desired(True/False)``。widget 不可见
    （切页/最小化/隐藏）时统一停；恢复可见时重启 desired 的条目并回调
    ``on_resume``（立即补一次刷新）。所有路径以 ``is_running`` 实测为准，
    重复广播幂等。
    """

    def __init__(self, widget, parent=None):
        super().__init__(parent if parent is not None else widget)
        self._widget = widget
        self._entries: list[_GuardedEntry] = []
        self._visibility_callbacks: list[Callable[[], None]] = []
        throttle = background_throttle()
        if throttle is not None:
            throttle.visibility_maybe_changed.connect(self._reevaluate)

    def manage(self, timer, on_resume=None) -> _GuardedEntry:
        """管理 QTimer 或任何有 start/stop/isActive 的对象。"""
        return self._add(timer.start, timer.stop, timer.isActive, on_resume)

    def manage_animation(self, animator, on_resume=None) -> _GuardedEntry:
        """管理 qfluentwidgets IndeterminateProgressBar 等有 start/stop/isStarted 的动画。"""
        return self._add(animator.start, animator.stop, animator.isStarted, on_resume)

    def on_visibility(self, callback: Callable[[], None]) -> None:
        """注册每次可见性可能变化时都会收到的回调（回调自行判定状态）。"""
        self._visibility_callbacks.append(callback)

    def _add(self, start, stop, is_running, on_resume) -> _GuardedEntry:
        entry = _GuardedEntry(self, start, stop, is_running, on_resume)
        self._entries.append(entry)
        # 接管即校正：隐藏 widget 上已启动的定时器必须在 manage() 当场停下，
        # 不能等下一次 Show/Hide 事件才生效（FPS 定时器就是先 start、后 manage）。
        self.apply(entry)
        return entry

    def apply(self, entry: _GuardedEntry) -> None:
        try:
            should_run = entry.desired and ui_active(self._widget)
            running = entry._is_running()
            if should_run and not running:
                entry._resume()
            elif not should_run and running:
                entry._stop()
        except RuntimeError:
            # widget 的 C++ 对象已销毁而广播先于 guard 拆除到达：
            # guard 挂在该 widget 名下，随即一起被清理，无需任何动作。
            pass

    def _reevaluate(self) -> None:
        for entry in list(self._entries):
            self.apply(entry)
        for callback in list(self._visibility_callbacks):
            try:
                callback()
            except RuntimeError:
                pass
