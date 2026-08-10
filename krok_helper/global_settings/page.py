"""全局设置与波形对齐设置两个对话框。

**这已经是独立对象**（不是 QWidget —— 它不常驻界面，只是按需搭对话框），
与外壳的往来全部经过构造时注入的 :class:`SettingsHost`。

设置的**读写生命周期**（``_load_settings_into_ui`` / ``_save_all_settings`` /
``set_ffmpeg_dir`` 这些）仍留在外壳：它们要把各页的设置接缝串起来，是外壳的活。
这里只负责「设置对话框」本身。
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Protocol, runtime_checkable

from PyQt6.QtCore import QObject, QTimer, QUrl, Qt
from PyQt6.QtGui import QDesktopServices, QIcon, QTextDocument
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    FluentIcon as FIF,
    HyperlinkCard,
    LineEdit as QLineEdit,
    Pivot,
    PrimaryPushButton,
    PushButton as QPushButton,
    PushSettingCard,
    ScrollArea as FluentScrollArea,
    RadioButton as QRadioButton,
    SettingCard,
    SettingCardGroup,
    SubtitleLabel,
    SwitchButton,
)

from krok_helper.config import APP_LOGO_PATH, APP_TITLE, APP_VERSION, FFMPEG_DIR_PLACEHOLDER
from krok_helper.errors import ProcessingError
from krok_helper.logging_config import get_active_log_dir
from krok_helper.pipeline import (
    DEFAULT_OFF_NAME_TEMPLATE,
    DEFAULT_ON_NAME_TEMPLATE,
    OUTPUT_NAME_MODE_FIXED,
    OUTPUT_NAME_MODE_TEMPLATE,
    validate_output_name_template,
)
from krok_helper.qfluent_compat import (
    ModelessDialog,
    ask_fluent_confirm,
    exec_modeless_dialog,
    show_fluent_error,
    show_fluent_info,
)
from krok_helper.settings import (
    import_legacy_sug_settings,
    save_app_settings,
)
from krok_helper.ui_kit import StyledComboBox
from krok_helper.updater import ensure_updater_settings
from krok_helper.updater.dialogs import UpdateSourceOrderDialog
from krok_helper.updater.settings import UpdaterSettings
from krok_helper.updater.sources import SOURCE_LABELS, normalize_order

__all__ = [
    "SettingsDialogs",
    "SettingsHost",
    "add_setting_card_actions",
    "build_settings_tab_page",
    "relax_setting_card_height",
]


@runtime_checkable
class SettingsHost(Protocol):
    """设置对话框需要外壳提供的能力。

    分两组。前一组是普通的宿主服务；后一组是**波形对齐页的设置片段** ——
    对话框目前直接读写对齐页的状态，等对齐页也对象化之后，这一组应当收敛成
    "向对齐页要一段设置界面"，而不是隔空改它的属性。分组写在这里，是为了让
    那笔欠账看得见。
    """

    # ── 普通宿主服务 ──
    settings: object

    def set_ffmpeg_dir(self, path: Path | None) -> None: ...

    def sync_ffmpeg_labels(self) -> None: ...

    def sync_lyrics_timing_host_paths(self) -> None: ...

    def install_single_click_combo_behavior(self, combo: object) -> None: ...

    def start_workbench_update_check(
        self,
        *,
        manual: bool,
        updater_settings=None,
        status_label=None,
        trigger_button=None,
    ) -> None:
        """跑一次更新检查。手动触发时会把状态标签与触发按钮交给它自己管。"""
        ...

    #: 命名模板与输出模式 —— 设置的读写生命周期留在外壳，这里读它、写回它。
    output_name_mode_value: str
    on_name_template_value: str
    off_name_template_value: str
    ffmpeg_dir_text: str

    def build_alignment_settings_fragment(self, parent: QWidget | None = None) -> QWidget:
        """向对齐页要一段设置界面。

        以前这里是九项穿透（模板值、输出目录、连 ``align_video_zone`` 那张
        drop card 都直接摸），对话框等于在远程操作另一页的内部状态。现在只要
        一块填好值的 ``QWidget``，怎么排布、怎么校验都归它自己。
        """
        ...

    def collect_page_settings(self) -> None:
        """写盘前让外壳把各页当前的设置收进 ``settings`` —— 收谁是外壳的事。"""
        ...


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


class SettingsDialogs(QObject):
    """按需搭建并弹出两个设置对话框。

    是 ``QObject`` 而不是普通对象：主题预览要用 ``schedule_theme_refresh`` 挂一个
    ``QTimer(self)``，Qt 的这类管道都要求宿主是 QObject。以前 ``self`` 是主窗口，
    天然满足；对象化之后如果只是个普通对象，换主题会在信号槽里静默抛 TypeError ——
    表现就是"选了主题没反应"。
    """

    def __init__(self, *, host: SettingsHost, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._host = host
        #: 对话框的父窗口，只用于定位与继承图标 —— 和 ``host`` 分开传，
        #: 这样 ``SettingsHost`` 不必顺带要求"你还得是个 QWidget"。
        self._parent = parent

    def open_page_settings(self, context: str) -> None:
        dialog = ModelessDialog(self._parent)
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

        alignment_fragment = None
        if context == "align":
            # 界面由对齐页自己提供，这里只负责摆进版式、保存时叫它一声。
            alignment_fragment = self._host.build_alignment_settings_fragment(dialog)
            shell.addWidget(alignment_fragment)
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
            if self._host.output_name_mode_value == OUTPUT_NAME_MODE_TEMPLATE:
                template_radio.setChecked(True)
            else:
                fixed_radio.setChecked(True)
            on_template_edit = QLineEdit(dialog)
            on_template_edit.setText(self._host.on_name_template_value)
            off_template_edit = QLineEdit(dialog)
            off_template_edit.setText(self._host.off_name_template_value)

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
                mode = self._host.output_name_mode_value
                on_template = self._host.on_name_template_value
                off_template = self._host.off_name_template_value
                if alignment_fragment is not None:
                    # 对齐那一页的校验与写回都归它自己，不合法照样抛 ProcessingError。
                    alignment_fragment.apply()
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
                    ffmpeg_dir_text=self._host.ffmpeg_dir_text,
                )
            except ProcessingError as exc:
                show_fluent_error(dialog, str(exc))
                return

            status_label.setText("设置已保存到本地。")
            show_fluent_info(dialog, f"设置已保存：\n{saved_path}")

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
            report = import_legacy_sug_settings(src, self._host.settings)
            save_app_settings(self._host.settings)
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

    def open_global_settings(self) -> None:
        updater_settings = ensure_updater_settings(self._host.settings)
        dialog = ModelessDialog(self._parent)
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
        _initial_theme = getattr(self._host.settings, "ui_theme", _T_AUTO)
        for _i, (_lbl, _val) in enumerate(_theme_options):
            if _val == _initial_theme:
                theme_combo.setCurrentIndex(_i)
                break
        theme_combo.setMinimumWidth(150)
        self._host.install_single_click_combo_behavior(theme_combo)
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
            if self._host.settings.ui_theme != value:
                self._host.settings.ui_theme = value
                try:
                    save_app_settings(self._host.settings)
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
        ffmpeg_display.setText(self._host.ffmpeg_dir_text)
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
        self._host.install_single_click_combo_behavior(proxy_combo)
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
            updated = UpdaterSettings.load(self._host.settings)
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
            self._host.start_workbench_update_check(
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

            self._host.set_ffmpeg_dir(ffmpeg_dir)
            self._host.settings.ffmpeg_dir = self._host.ffmpeg_dir_text
            # 写入新选择的界面主题。``UpdaterSettings.save`` 内部
            # 会调 ``save_app_settings(self._host.settings)``，连同 ``ui_theme``
            # 一起持久化。这里只需更新 in-memory 字段即可。
            _selected_idx = theme_combo.currentIndex()
            if 0 <= _selected_idx < len(_theme_options):
                self._host.settings.ui_theme = _theme_options[_selected_idx][1]
            updated = UpdaterSettings.load(self._host.settings)
            updated.enabled = updater_enabled_check.isChecked()
            updated.check_on_startup = startup_check.isChecked()
            updated.min_check_interval_hours = interval
            updated.proxy_mode = current_proxy_mode()
            updated.proxy_manual_url = proxy_manual_edit.text().strip()
            updated.source_order = normalize_order(source_order)
            updated.save(self._host.settings)
            update_status_label.setText("设置已保存到本地。")

        save_button.clicked.connect(save_global_settings)
        dialog.exec()

    def _save_settings_payload(
        self,
        *,
        output_name_mode: str,
        on_template: str,
        off_template: str,
        ffmpeg_dir_text: str,
    ) -> Path:
        """校验对话框自己那几项，再连同各页的设置一起落盘。

        对齐那几项原先也在这里校验和写回（模板、输出目录），现在归对齐页的
        设置片段 —— 保存时它已经先跑过 ``apply()`` 了。
        """
        ffmpeg_dir = Path(ffmpeg_dir_text).expanduser() if ffmpeg_dir_text.strip() else None
        if ffmpeg_dir is not None and not ffmpeg_dir.is_dir():
            raise ProcessingError("所选 ffmpeg 目录无效，请重新选择。")

        if output_name_mode not in {OUTPUT_NAME_MODE_FIXED, OUTPUT_NAME_MODE_TEMPLATE}:
            raise ProcessingError("输出命名模式无效，请重新选择。")
        if output_name_mode == OUTPUT_NAME_MODE_TEMPLATE:
            on_template = validate_output_name_template(on_template, "原唱")
            off_template = validate_output_name_template(off_template, "伴奏")

        self._host.output_name_mode_value = output_name_mode
        self._host.on_name_template_value = on_template
        self._host.off_name_template_value = off_template
        self._host.ffmpeg_dir_text = str(ffmpeg_dir) if ffmpeg_dir else ""
        self._host.sync_ffmpeg_labels()
        self._host.settings.output_name_mode = self._host.output_name_mode_value
        self._host.settings.on_name_template = self._host.on_name_template_value
        self._host.settings.off_name_template = self._host.off_name_template_value
        self._host.settings.ffmpeg_dir = self._host.ffmpeg_dir_text
        self._host.sync_lyrics_timing_host_paths()
        self._host.collect_page_settings()
        return save_app_settings(self._host.settings)
