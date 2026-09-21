"""Timing-property page construction isolated from the property panel host."""

from __future__ import annotations

from typing import Any, Callable

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QWidget
from qfluentwidgets import CheckBox

from krok_helper.qfluent_compat import install_fluent_tooltip
from krok_helper.subtitle_render.frontend.properties.controls.inputs import (
    WheelFocusedComboBox,
    WheelFocusedSpinBox,
)
from krok_helper.subtitle_render.frontend.properties.controls.layout import (
    ResponsiveFieldGrid,
    compact_property_control,
    property_section,
)
from krok_helper.subtitle_render.frontend.widgets.workspace_switcher import (
    WorkspaceSwitcher,
)


OVERLAP_FALLBACK_OPTIONS = (
    ("lift", "抬升避让"),
    ("displace", "吃掉走字时长"),
)


def timing_spin(
    minimum: int,
    maximum: int,
    *,
    suffix: str = "",
) -> WheelFocusedSpinBox:
    """Create the compact integer input used by timing properties."""
    spin = WheelFocusedSpinBox()
    spin.setRange(minimum, maximum)
    spin.setSuffix(suffix)
    spin.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    compact_property_control(spin)
    spin._sync_text_minimum()
    return spin


class TimingPropertyPageBuilder:
    """Build timing controls while leaving style transitions with the host."""

    def __init__(
        self,
        host: Any,
        *,
        spin_factory: Callable[..., Any] = timing_spin,
        tooltip_installer: Callable[..., None] = install_fluent_tooltip,
    ) -> None:
        self._host = host
        self._spin_factory = spin_factory
        self._tooltip_installer = tooltip_installer

    def make_section(self) -> QFrame:
        host = self._host
        section, layout = property_section("时间")

        scope_row = QHBoxLayout()
        scope_row.setContentsMargins(0, 0, 0, 0)
        host._timing_scope_combo = WheelFocusedComboBox(section)
        compact_property_control(host._timing_scope_combo)
        host._timing_scope_combo.setToolTip(
            "选择要编辑时间的字幕轴（主字幕 / 副字幕源）；标题轴不参与。"
        )
        # 初始项在构造期静默加入：宿主的 scope 处理器此时可能尚未就绪。
        host._timing_scope_combo.blockSignals(True)
        host._timing_scope_combo.addItem("主字幕")
        host._timing_scope_combo.blockSignals(False)
        host._timing_scope_combo.currentIndexChanged.connect(
            lambda index: host._on_timing_scope_selected(index)
        )
        scope_row.addWidget(host._timing_scope_combo, 1)
        host._timing_follow_check = CheckBox("跟随主字幕时间策略", section)
        host._timing_follow_check.setToolTip(
            "开启时该轴的时间数值由主字幕轴推送、不可编辑；\n"
            "关闭时快照主轴当前值，此后该轴独立设置、不受主轴影响；\n"
            "重新开启会清空该轴自定义值，回到跟随。"
        )
        host._timing_follow_check.toggled.connect(
            lambda checked: host._update_track_timing_follow(checked)
        )
        scope_row.addWidget(host._timing_follow_check)
        layout.addLayout(scope_row)

        grid = ResponsiveFieldGrid(section, min_column_width=130, max_columns=4)

        host._line_lead_spin = self._add_spin(
            grid,
            "提前入场",
            0,
            10_000,
            "line_lead_in_ms",
        )
        host._line_tail_spin = self._add_spin(
            grid,
            "延迟退场",
            0,
            10_000,
            "line_tail_ms",
        )
        host._line_offset_spin = self._add_spin(
            grid,
            "偏移",
            -10_000,
            10_000,
            "timing_offset_ms",
        )

        host._section_gap_spin = self._spin_factory(0, 60_000, suffix=" ms")
        host._section_gap_spin.valueChanged.connect(
            lambda value: host._update_style(section_gap_ms=value)
        )
        # This source-loading option remains available for state synchronization,
        # but its visible editor lives in the lyrics-list toolbar settings.
        host._section_gap_spin.setVisible(False)

        host._section_ending_combo = WheelFocusedComboBox(section)
        compact_property_control(host._section_ending_combo)
        for label, value in (("保持", "hold"), ("段末清屏", "clear")):
            host._section_ending_combo.addItem(label, value)
        host._section_ending_combo.currentIndexChanged.connect(
            lambda _index: host._update_style(
                section_ending_mode=host._section_ending_combo.currentData()
            )
        )
        grid.add_field("段落结束", host._section_ending_combo)

        host._lane_gap_spin = self._add_spin(
            grid,
            "同轨间隔",
            0,
            5_000,
            "line_lane_gap_ms",
        )
        host._lane_gap_spin.setToolTip("同一显示轨上相邻两句之间保留的时间间隔。")

        host._line_protect_spin = self._add_spin(
            grid,
            "保护时间",
            0,
            10_000,
            "line_protect_ms",
        )
        host._line_protect_spin.setToolTip(
            "自动避让压缩显示时间时，必须在走字两侧留下的最小余量：每句演唱结束后\n"
            "至少保留这么久才消失，上屏也至少提前这么久。\n"
            "0 = 不保护，提前入场 / 延迟退场可以被压光。实际生效值不会超过"
            "「提前入场」与「延迟退场」中较小的那个。\n"
            "手工拖动过上屏 / 消失时间的句子不受此限制。"
        )
        host._entry_anim_protect_spin = self._add_spin(
            grid,
            "入场动画保护时间",
            0,
            10_000,
            "entry_anim_protect_ms",
        )
        host._entry_anim_protect_spin.setToolTip(
            "自动避让压缩显示时间时，入场动画最多被压到这么短"
            "（整段动画按窗口加速播放，不会被截断）。\n"
            "默认 250ms；0 = 不设下限，入场动画可以被完全压掉。\n"
            "实际压缩底线为 max(本值, 保护时间)；关「允许出入场动画重叠」时"
            "动画才参与压缩，开启时动画互相穿越、不压缩。"
        )
        host._exit_anim_protect_spin = self._add_spin(
            grid,
            "出场动画保护时间",
            0,
            10_000,
            "exit_anim_protect_ms",
        )
        host._exit_anim_protect_spin.setToolTip(
            "自动避让压缩显示时间时，退场动画至少保留的可见时长"
            "（整段动画按窗口加速播放，不会被截断）。\n"
            "默认 100ms；0 = 不设下限，退场动画可以被完全压掉。\n"
            "实际压缩底线为 max(本值, 保护时间)；关「允许出入场动画重叠」时"
            "动画才参与压缩，开启时动画互相穿越、不压缩。"
        )
        layout.addWidget(grid)

        sync_row = QHBoxLayout()
        sync_row.setContentsMargins(0, 0, 0, 0)
        host._sync_entry_check = CheckBox("同步入场", section)
        host._sync_entry_check.setToolTip(
            "未手工调整上屏时间的 T 会尽量提前到同步页的最早边界；默认只处理段首页，\n"
            "开启“每句同步”后处理每一页。"
            "提前量以“同位邻句消失时间 + 同轨间隔”为下界：够得着就与页内最早的 T "
            "对齐，够不着就停在该下界，绝不会为了提前而压缩上一页的消失时间。"
        )
        host._sync_entry_check.toggled.connect(
            lambda checked: host._update_style(sync_entry=checked)
        )
        host._sync_entry_check.toggled.connect(
            lambda _checked: host._sync_sync_each_page_enabled()
        )
        sync_row.addWidget(host._sync_entry_check)

        host._sync_ending_check = CheckBox("同步退场", section)
        host._sync_ending_check.setToolTip(
            "未手工调整消失时间的 T 会尽量延长到同步页的最晚边界；默认只处理段尾页，\n"
            "开启“每句同步”后处理每一页。"
            "发生像素碰撞时，各个 T 独立按先压缩前句退场、再压缩后句入场的"
            "顺序处理；不会改动未参与该次碰撞的页内兄弟行。"
        )
        host._sync_ending_check.toggled.connect(
            lambda checked: host._update_style(sync_ending=checked)
        )
        host._sync_ending_check.toggled.connect(
            lambda _checked: host._sync_sync_each_page_enabled()
        )
        sync_row.addWidget(host._sync_ending_check)

        host._sync_each_page_check = CheckBox("每句同步", section)
        host._sync_each_page_check.setToolTip(
            "关闭时，同步入场只作用于每段第一页，同步退场只作用于每段最后一页；\n"
            "开启时，每一页都会分别执行同步入场和同步退场。"
        )
        host._sync_each_page_check.toggled.connect(
            lambda checked: host._update_style(sync_each_page=checked)
        )
        host._sync_each_page_check.setEnabled(False)
        sync_row.addWidget(host._sync_each_page_check)
        sync_row.addStretch(1)
        layout.addLayout(sync_row)

        host._ruby_main_reading_units_check = CheckBox(
            "正文按注音字符切分（N3 式）",
            section,
        )
        host._ruby_main_reading_units_check.setToolTip(
            "正文内部已有时间点时，两种模式都会保留正文逐字时钟；"
            "缺失时，开启按注音可视字符数映射，"
            "关闭按注音内部时间点形成的时间段数均分正文。"
        )
        host._ruby_main_reading_units_check.toggled.connect(
            lambda checked: host._update_style(
                ruby_main_progress_mode=(
                    "reading_units" if checked else "checkpoint_segments"
                )
            )
        )
        host._n3_style_row = QHBoxLayout()
        host._n3_style_row.setContentsMargins(0, 0, 0, 0)
        host._n3_style_row.addWidget(host._ruby_main_reading_units_check)

        host._allow_animation_overlap_check = CheckBox(
            "允许出入场动画重叠",
            section,
        )
        host._allow_animation_overlap_check.setToolTip(
            "开启时，同轨间隔只约束主文字的稳定显示段，入场和退场动画可以互相重叠；\n"
            "关闭时，完整的入场、稳定显示和退场窗口都必须满足同轨间隔。"
        )
        host._allow_animation_overlap_check.toggled.connect(
            lambda checked: host._update_style(
                allow_entry_exit_animation_overlap=checked
            )
        )
        host._n3_style_row.addWidget(host._allow_animation_overlap_check)

        host._auto_fill_section_time_check = CheckBox(
            "自动填充段内时间",
            section,
        )
        host._auto_fill_section_time_check.setToolTip(
            "开启时，段内非段尾页的每句挂到下一页同视觉行句子的入场前"
            "（隔一个同轨间隔），下一页没有同行句子则不挂；段尾页填充到"
            "本页自然结束。\n"
            "关闭时，每句仅保留自己的退场窗口。"
        )
        host._auto_fill_section_time_check.toggled.connect(
            lambda checked: host._update_style(auto_fill_section_time=checked)
        )
        host._n3_style_row.addWidget(host._auto_fill_section_time_check)

        for tooltip_button in (
            host._sync_entry_check,
            host._sync_ending_check,
            host._sync_each_page_check,
            host._ruby_main_reading_units_check,
            host._allow_animation_overlap_check,
            host._auto_fill_section_time_check,
        ):
            self._tooltip_installer(tooltip_button, show_delay=300)
        host._n3_style_row.addStretch(1)
        layout.addLayout(host._n3_style_row)
        # 跟随主字幕时整体只读的控件清单（轴下拉与跟随开关不在此列）。
        host._timing_scope_managed_controls = (
            host._line_lead_spin,
            host._line_tail_spin,
            host._line_offset_spin,
            host._section_ending_combo,
            host._lane_gap_spin,
            host._line_protect_spin,
            host._entry_anim_protect_spin,
            host._exit_anim_protect_spin,
            host._sync_entry_check,
            host._sync_ending_check,
            host._sync_each_page_check,
            host._ruby_main_reading_units_check,
            host._allow_animation_overlap_check,
            host._auto_fill_section_time_check,
        )
        return section

    def make_overlap_section(self) -> QFrame:
        """「重叠设置」：行间重叠总开关 + 残余冲突的收尾策略（同一行）。

        与轴下拉同页：控件编辑经 ``_update_style`` 按当前 scope 改道——
        主轴写全局样式，非跟随副轴写该轴 ``display_timing.overrides``。
        """

        host = self._host
        section, layout = property_section("重叠设置")

        host._allow_inter_page_line_overlap_check = CheckBox(
            "启用行间重叠",
            section,
        )
        host._allow_inter_page_line_overlap_check.setToolTip(
            "关闭时，系统按每一行不含注音、描边、阴影和发光的主文字字形"
            "像素范围检测真实跨页冲突，只缩短发生冲突的两行的提前入场和延迟"
            "退场时间，不会截断任何走字区间或改变页内上屏顺序。自动压缩可以"
            "缩短动画时段：不会把非零入场动画自动压到时间设置中的"
            "「入场动画保护时间」以下；若动画时长或上屏时间由用户手工设定，"
            "则保留用户值；非零退场动画自动压缩时至少保留"
            "「出场动画保护时间」。是否允许入场和退场动画"
            "互相重叠，由时间设置中的“允许出入场动画重叠”单独控制。"
            "时间压缩仍无法消除冲突时，按右侧胶囊选择的收尾策略处理："
            "抬升避让 = 移动后进入的整页字幕；吃掉走字时长 = 由将要演唱的"
            "下一句直接顶掉还在走字的上一句。入场、退场和字符动画允许互相"
            "穿越，不因页面排版变化而扩大碰撞时间。开启后不执行跨页时间压缩"
            "或空间避让，允许跨页字幕直接重叠，适合需要刻意叠放的特殊效果。"
            "同一页内部的负行间距或手工重叠不受此开关影响。\n"
            "本设置属于时间策略：编辑对象为顶部选中的字幕轴；主字幕轴即"
            "全局值，副字幕源跟随主字幕时同步、取消跟随后可单独覆盖。"
        )
        host._allow_inter_page_line_overlap_check.toggled.connect(
            lambda checked: host._update_style(
                allow_inter_page_line_overlap=checked
            )
        )
        self._tooltip_installer(
            host._allow_inter_page_line_overlap_check,
            show_delay=300,
        )

        host._overlap_fallback_switch = WorkspaceSwitcher(section)
        host._overlap_fallback_switch.setAccessibleName("残余冲突处理")
        for key, label in OVERLAP_FALLBACK_OPTIONS:
            host._overlap_fallback_switch.addItem(key, label)
        host._overlap_fallback_switch.setToolTip(
            "时间压缩（含两侧保护时间底线）仍无法消除跨页冲突时的收尾策略：\n"
            "抬升避让 = 移动后进入的整页字幕（默认，旧行为）：避让优先吸附到"
            "已有布局行位，再沿布局方向寻找画布内空间，跨页空隙采用被重叠页面"
            "布局的行间距；放不下时改向反方向寻找，两边都放不下则保持原布局"
            "位置和绘制优先级；页面一旦移动，会保持位置直到本页播放完毕。\n"
            "吃掉走字时长 = 由将要演唱的下一句直接顶掉还在走字的上一句：被顶掉"
            "的句子按其「出场动画保护时间」播放退场动画，动画恰好在下一句上屏"
            "时刻结束（走字显示到退场开始为止，退场动画充当交接过渡）；手工拖过"
            "消失时间的句子不参与自动压缩、时间保持原值，顶掉只发生在渲染层。"
            "该模式不移动整页字幕（无页面平移避让）；单行页的行位上移"
            "（强制顶底 N3）照常。\n"
            "仅在关闭「启用行间重叠」时参与解算。\n"
            "本设置属于时间策略：编辑对象为顶部选中的字幕轴；主字幕轴即"
            "全局值，副字幕源跟随主字幕时同步、取消跟随后可单独覆盖。"
        )
        host._overlap_fallback_switch.currentItemChanged.connect(
            lambda mode: host._update_style(overlap_fallback_mode=mode)
        )
        # 勾选「启用行间重叠」后不存在跨页避让，收尾策略随之失效。
        host._allow_inter_page_line_overlap_check.toggled.connect(
            lambda checked: host._overlap_fallback_switch.setEnabled(not checked)
        )

        row = QWidget(section)
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(12)
        row_layout.addWidget(
            host._allow_inter_page_line_overlap_check,
            0,
            Qt.AlignmentFlag.AlignVCenter,
        )
        row_layout.addStretch(1)
        row_layout.addWidget(
            host._overlap_fallback_switch,
            0,
            Qt.AlignmentFlag.AlignVCenter,
        )
        layout.addWidget(row)

        # 重叠设置随轴编辑：纳入跟随态整体只读清单。
        host._timing_scope_managed_controls = (
            *(getattr(host, "_timing_scope_managed_controls", None) or ()),
            host._allow_inter_page_line_overlap_check,
            host._overlap_fallback_switch,
        )
        return section

    def _add_spin(
        self,
        grid: ResponsiveFieldGrid,
        label: str,
        minimum: int,
        maximum: int,
        model_field: str,
    ) -> Any:
        spin = self._spin_factory(minimum, maximum, suffix=" ms")
        spin.valueChanged.connect(
            lambda value, field=model_field: self._host._update_style(
                **{field: value}
            )
        )
        grid.add_field(label, spin)
        return spin
