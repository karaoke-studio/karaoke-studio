"""全局设置里换界面主题。

主题预览走 ``schedule_theme_refresh``，它会挂一个 ``QTimer(receiver)`` —— Qt 的
这类管道都要求 receiver 是 QObject。对象化之前 ``self`` 是主窗口，天然满足；
``SettingsDialogs`` 一旦是普通对象，换主题就在信号槽里抛 TypeError 并被全局钩子
吞掉，表现是"选了主题没反应"。

所以这里既测行为（选了就切），也钉死那条前提（这个对象必须是 QObject）。
"""

from __future__ import annotations

from PyQt6.QtCore import QObject
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QWidget

from krok_helper.global_settings.page import SettingsDialogs
from krok_helper.settings import AppSettings
from krok_helper.theme_workbench import ThemeMode
from krok_helper.ui_kit import StyledComboBox


class _Host(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.settings = AppSettings()
        self.ffmpeg_dir_text = ""
        self.output_name_mode_value = "fixed"
        self.on_name_template_value = "{video_name}_on"
        self.off_name_template_value = "{video_name}_off"

    def set_ffmpeg_dir(self, path) -> None: ...

    def sync_ffmpeg_labels(self) -> None: ...

    def sync_lyrics_timing_host_paths(self) -> None: ...

    def install_single_click_combo_behavior(self, combo) -> None: ...

    def start_workbench_update_check(self, **_kwargs) -> None: ...

    def build_alignment_settings_fragment(self, parent=None):
        return QWidget(parent)

    def collect_page_settings(self) -> None: ...


def test_the_dialog_owner_is_a_qobject() -> None:
    """前提：Qt 的定时器/信号管道要挂在 QObject 上。"""
    assert issubclass(SettingsDialogs, QObject)


def test_choosing_a_theme_actually_switches_it(monkeypatch) -> None:
    """选了主题，页面就该把新模式推给主题单例。

    这里**不动真的全局主题**：真切一次会让 SUG 去 polish 所有顶层控件，在整套
    测试里会碰到别的用例遗留的、C++ 侧已销毁的控件，直接把 pytest 进程带崩。
    所以换成假的主题对象，测的是"预览回调有没有跑到、推了什么值" —— 当初坏掉
    的正是这一段（回调在信号槽里抛 TypeError 被吞掉）。
    """
    from krok_helper.global_settings import page as settings_page
    from krok_helper.updater.settings import UpdaterSettings

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(settings_page, "ensure_updater_settings", lambda _s: UpdaterSettings())
    monkeypatch.setattr(settings_page, "save_app_settings", lambda _s: None)
    built: list = []
    monkeypatch.setattr(
        settings_page.ModelessDialog, "exec", lambda dialog: (built.append(dialog), 0)[1]
    )

    class _FakeTheme:
        def __init__(self) -> None:
            self.mode = ThemeMode.AUTO

    fake_theme = _FakeTheme()
    import krok_helper.theme_workbench as workbench

    monkeypatch.setattr(workbench, "theme", fake_theme)

    host = _Host()
    dialogs = SettingsDialogs(host=host, parent=host)
    try:
        dialogs.open_global_settings()
        dialog = built[-1]
        combo = next(
            c
            for c in dialog.findChildren(StyledComboBox)
            if c.count() == 3 and c.itemText(0) == "跟随系统"
        )

        combo.setCurrentIndex(2)  # 深色
        # 预览是防抖的（200ms），得真的等，光转事件循环不行。
        for _ in range(40):
            if fake_theme.mode is ThemeMode.DARK:
                break
            QTest.qWait(50)

        assert fake_theme.mode is ThemeMode.DARK, "选了深色但没推给主题 —— 预览回调多半炸在信号槽里"
        assert host.settings.ui_theme == "dark"
    finally:
        for dialog in built:
            dialog.close()
        host.close()
        host.deleteLater()
