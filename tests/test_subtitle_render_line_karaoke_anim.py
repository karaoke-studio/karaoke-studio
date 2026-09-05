"""逐行唱字特效覆盖。

原先唱字特效只有全局一档，任何一行都改不了——次字幕只能跟着主字幕走。
现在 ``LineAnimationOverride`` 也能带唱字动画，``inherit`` 表示仍跟全局。
"""

from __future__ import annotations

import pytest

from krok_helper.subtitle_render.domain.models import (
    LineAnimationOverride,
    Style,
    TimingLine,
    effective_karaoke_animation,
    line_animation_override_from_dict,
    line_animation_override_to_dict,
    style_with_line_animation,
)


def _resolved(global_karaoke: str, override: LineAnimationOverride | None) -> str:
    style = Style()
    style.karaoke_anim = global_karaoke
    line = TimingLine()
    line.animation_override = override
    return effective_karaoke_animation(style_with_line_animation(style, line))


class TestPerLineOverride:
    def test_a_line_can_switch_the_sung_effect_off(self) -> None:
        assert _resolved("utopia", LineAnimationOverride(karaoke_anim="none")) == "none"

    def test_a_line_can_switch_it_on_while_the_global_is_off(self) -> None:
        assert _resolved("none", LineAnimationOverride(karaoke_anim="utopia")) == "utopia"

    def test_the_global_setting_is_untouched_by_a_line(self) -> None:
        style = Style()
        style.karaoke_anim = "utopia"
        line = TimingLine()
        line.animation_override = LineAnimationOverride(karaoke_anim="none")
        style_with_line_animation(style, line)
        assert style.karaoke_anim == "utopia"

    def test_reverse_lines_use_the_independent_effect(self) -> None:
        style = Style(karaoke_anim="utopia", reverse_karaoke_anim="no_wipe")
        line = TimingLine(wipe_reverse=True)
        assert effective_karaoke_animation(
            style_with_line_animation(style, line)
        ) == "no_wipe"

    def test_normal_lines_ignore_the_reverse_effect(self) -> None:
        style = Style(karaoke_anim="none", reverse_karaoke_anim="utopia")
        assert effective_karaoke_animation(
            style_with_line_animation(style, TimingLine())
        ) == "none"

    def test_reverse_effect_does_not_leak_through_the_line_style_cache(self) -> None:
        from krok_helper.subtitle_render.engine.layout.layout_context import layout_pass
        from krok_helper.subtitle_render.engine.layout.line.style import style_for_line

        style = Style(karaoke_anim="utopia", reverse_karaoke_anim="no_wipe")
        reverse = TimingLine(wipe_reverse=True)
        normal = TimingLine(wipe_reverse=False)
        with layout_pass():
            reverse_style = style_for_line(style, reverse)
            normal_style = style_for_line(style, normal)
        assert effective_karaoke_animation(reverse_style) == "no_wipe"
        assert effective_karaoke_animation(normal_style) == "utopia"


class TestInheritKeepsFollowingTheGlobal:
    """踩过的坑：把 "inherit" 写进行样式，会让 effective_ 转而去看这一行被覆盖后的
    入退场动画，全局显式设的 utopia 就丢了。"""

    def test_an_entry_exit_override_does_not_drop_the_global_sung_effect(self) -> None:
        override = LineAnimationOverride(entry_anim="fade", exit_anim="fade")
        assert override.karaoke_anim == "inherit"
        assert _resolved("utopia", override) == "utopia"

    def test_inherit_follows_a_global_none(self) -> None:
        override = LineAnimationOverride(entry_anim="fade", exit_anim="fade")
        assert _resolved("none", override) == "none"

    def test_the_legacy_utopia_inference_still_works(self) -> None:
        """老项目没有 karaoke_anim，靠入退场里的 utopia 推导。"""
        override = LineAnimationOverride(entry_anim="utopia", exit_anim="utopia")
        assert _resolved("inherit", override) == "utopia"

    def test_other_fields_are_still_applied(self) -> None:
        style = Style()
        style.karaoke_anim = "utopia"
        line = TimingLine()
        line.animation_override = LineAnimationOverride(
            entry_anim="rise",
            entry_duration_ms=500,
            exit_anim="spin_flip",
            exit_duration_ms=200,
            karaoke_anim="none",
        )
        resolved = style_with_line_animation(style, line)
        assert (resolved.entry_anim, resolved.entry_lead_ms) == ("rise", 500)
        assert (resolved.exit_anim, resolved.exit_fade_ms) == ("spin_flip", 200)


class TestPersistence:
    def test_it_round_trips(self) -> None:
        override = LineAnimationOverride(
            entry_anim="fade", exit_anim="fade", karaoke_anim="none"
        )
        assert line_animation_override_from_dict(
            line_animation_override_to_dict(override)
        ) == override

    def test_a_project_without_the_key_reads_as_inherit(self) -> None:
        """旧项目的渲染结果必须和加这个字段之前一致。"""
        legacy = {
            "entry_anim": "fade",
            "entry_duration_ms": 300,
            "exit_anim": "fade",
            "exit_duration_ms": 300,
        }
        assert line_animation_override_from_dict(legacy).karaoke_anim == "inherit"

    @pytest.mark.parametrize("value", ["乱写", None, 3, ""])
    def test_a_bad_value_falls_back_to_inherit(self, value) -> None:
        legacy = {
            "entry_anim": "fade",
            "entry_duration_ms": 300,
            "exit_anim": "fade",
            "exit_duration_ms": 300,
            "karaoke_anim": value,
        }
        assert line_animation_override_from_dict(legacy).karaoke_anim == "inherit"


class TestRenderCacheKey:
    def test_the_style_cache_distinguishes_the_sung_effect(self) -> None:
        """缓存键是逐字段列举的；漏掉唱字，只有它不同的两行会串味。"""
        import inspect

        from krok_helper.subtitle_render.engine import painter

        source = inspect.getsource(painter._style_for_line)
        assert "override.karaoke_anim" in source


class TestDialog:
    @staticmethod
    def _dialog(override):
        from krok_helper.subtitle_render.frontend.editor.lyrics_list import _LineAnimationDialog

        style = Style()
        style.karaoke_anim = "utopia"
        return _LineAnimationDialog(style, override)

    def test_it_offers_inherit_none_and_utopia(self) -> None:
        dialog = self._dialog(None)
        values = {
            dialog._karaoke_combo.itemData(i)
            for i in range(dialog._karaoke_combo.count())
        }
        assert values == {"inherit", "none", "no_wipe", "utopia"}

    def test_it_round_trips_the_choice(self) -> None:
        override = LineAnimationOverride(
            entry_anim="fade", exit_anim="fade", karaoke_anim="none"
        )
        dialog = self._dialog(override)
        assert dialog.animation_override().karaoke_anim == "none"

    def test_inheriting_globally_yields_no_override(self) -> None:
        dialog = self._dialog(None)
        assert dialog.animation_override() is None

    def test_the_summary_only_mentions_a_changed_sung_effect(self) -> None:
        from krok_helper.subtitle_render.frontend.editor.lyrics_list import _animation_summary

        style = Style()
        plain = LineAnimationOverride(entry_anim="fade", exit_anim="fade")
        assert "唱字" not in _animation_summary(style, plain)
        changed = LineAnimationOverride(
            entry_anim="fade", exit_anim="fade", karaoke_anim="none"
        )
        assert "唱字" in _animation_summary(style, changed)


class TestGpuParity:
    """CPU 侧改了唱字，GPU 侧必须拿到同样的逐行值——否则 GPU 导出会静默按全局渲染。"""

    @staticmethod
    def _track():
        from krok_helper.subtitle_render.domain.models import TimingChar, TimingTrack

        def line(text: str, start: int, override=None) -> TimingLine:
            item = TimingLine()
            item.chars = [
                TimingChar(text=ch, start_ms=start + i * 200)
                for i, ch in enumerate(text)
            ]
            item.end_ms = start + len(text) * 200
            item.animation_override = override
            return item

        track = TimingTrack()
        track.lines = [
            line("AAA", 0),
            line("BBB", 2000, LineAnimationOverride(
                entry_anim="fade", exit_anim="fade", karaoke_anim="none")),
            line("CCC", 4000, LineAnimationOverride(entry_anim="fade", exit_anim="fade")),
        ]
        return track

    def test_the_ir_carries_the_per_line_sung_effect(self, qapp) -> None:
        from krok_helper.subtitle_render.engine.painter import build_track_layout_plan
        from krok_helper.subtitle_render.native.protocol import track_to_ir

        style = Style()
        style.karaoke_anim = "utopia"
        track = self._track()
        ir = track_to_ir(
            track,
            style,
            layout_plan=build_track_layout_plan(track, style),
        )
        # 无覆盖 / 覆盖成 none / inherit
        assert [line["karaoke_anim"] for line in ir["lines"]] == [
            "utopia",
            "none",
            "utopia",
        ]

    def test_it_does_not_force_a_painter_fallback(self, qapp) -> None:
        """协议本来就按行发这个字段，不需要退回 Painter。"""
        from krok_helper.subtitle_render.native.protocol import gpu_unsupported_features

        style = Style()
        style.karaoke_anim = "utopia"
        assert gpu_unsupported_features(self._track(), style) == ()

    def test_the_ir_uses_the_independent_reverse_effect(self, qapp) -> None:
        from krok_helper.subtitle_render.engine.painter import build_track_layout_plan
        from krok_helper.subtitle_render.native.protocol import track_to_ir

        track = self._track()
        track.lines[0].wipe_reverse = True
        style = Style(karaoke_anim="utopia", reverse_karaoke_anim="no_wipe")
        ir = track_to_ir(
            track,
            style,
            layout_plan=build_track_layout_plan(track, style),
        )
        assert ir["lines"][0]["karaoke_anim"] == "no_wipe"

    def test_reverse_effect_does_not_leak_to_a_matching_normal_line_ir(self, qapp) -> None:
        from krok_helper.subtitle_render.domain.models import TimingChar, TimingTrack
        from krok_helper.subtitle_render.engine.painter import build_track_layout_plan
        from krok_helper.subtitle_render.native.protocol import track_to_ir

        track = TimingTrack(lines=[
            TimingLine(
                chars=[TimingChar("反", 0)], end_ms=500, wipe_reverse=True
            ),
            TimingLine(
                chars=[TimingChar("正", 1000)], end_ms=1500
            ),
        ])
        style = Style(karaoke_anim="utopia", reverse_karaoke_anim="no_wipe")
        ir = track_to_ir(
            track,
            style,
            layout_plan=build_track_layout_plan(track, style),
        )
        assert [line["karaoke_anim"] for line in ir["lines"]] == [
            "no_wipe",
            "utopia",
        ]


def _section_edge_track():
    """段A 三页（每页 2 行）：首 {0,1}、中 {2,3}、尾 {4,5}；
    段B 单页（2 行）{6,7} 既是首又是尾。"""
    from krok_helper.subtitle_render.domain.models import TimingChar, TimingTrack
    from krok_helper.subtitle_render.domain.timing import (
        TrackPage,
        TrackPagePlan,
        TrackSection,
    )

    def line(text: str, start: int, override=None) -> TimingLine:
        item = TimingLine()
        item.chars = [
            TimingChar(text=ch, start_ms=start + i * 200)
            for i, ch in enumerate(text)
        ]
        item.end_ms = start + len(text) * 200
        item.animation_override = override
        return item

    track = TimingTrack()
    track.lines = [
        line("AA", 0),
        line("BB", 3000),
        line("CC", 9000),
        line("DD", 12000),
        line("EE", 15000),
        line("FF", 18000),
        line("GG", 40000),
        line("HH", 43000),
    ]
    track.page_plan = TrackPagePlan(
        sections=[
            TrackSection(
                pages=[
                    TrackPage(2, "default"),
                    TrackPage(2, "default"),
                    TrackPage(2, "default"),
                ]
            ),
            TrackSection(pages=[TrackPage(2, "default")]),
        ]
    )
    return track


class TestSectionEdgeAnimation:
    """段首尾独立动画：默认各页只替换自己一侧，单页段两侧都换。"""

    @staticmethod
    def _style(**extra):
        return Style(
            section_edge_anim_enabled=True,
            section_head_anim="utopia",
            section_tail_anim="slide_out",
            **extra,
        )

    def test_each_edge_page_replaces_only_its_own_side_by_default(self) -> None:
        from krok_helper.subtitle_render.engine.layout.display.section_edges import (
            section_edge_context,
        )
        from krok_helper.subtitle_render.engine.layout.layout_context import layout_pass
        from krok_helper.subtitle_render.engine.layout.line.style import style_for_line

        track = _section_edge_track()
        style = self._style()
        # 八行 layout_index/歌手/覆盖完全一致：如果逐行样式缓存键漏了段边缘
        # 标记，中页会直接命中首页的缓存条目、跟着一起换动画。
        with layout_pass():
            section_edge_context(track, style)
            resolved = [style_for_line(style, line) for line in track.lines]
        assert [(s.entry_anim, s.exit_anim) for s in resolved] == [
            ("utopia", "fade"),  # 段A 首页：只换入场
            ("utopia", "fade"),
            ("fade", "fade"),  # 中页：不动
            ("fade", "fade"),
            ("fade", "slide_out"),  # 段A 尾页：只换退场
            ("fade", "slide_out"),
            ("utopia", "slide_out"),  # 段B 单页：既是首又是尾，两侧都换
            ("utopia", "slide_out"),
        ]

    def test_both_animations_replaces_both_sides_on_every_edge_page(self) -> None:
        from krok_helper.subtitle_render.engine.layout.display.section_edges import (
            section_edge_context,
        )
        from krok_helper.subtitle_render.engine.layout.layout_context import layout_pass
        from krok_helper.subtitle_render.engine.layout.line.style import style_for_line

        track = _section_edge_track()
        style = self._style(section_edge_both_animations=True)
        with layout_pass():
            section_edge_context(track, style)
            resolved = [style_for_line(style, line) for line in track.lines]
        assert [(s.entry_anim, s.exit_anim) for s in resolved] == [
            ("utopia", "slide_out"),
            ("utopia", "slide_out"),
            ("fade", "fade"),
            ("fade", "fade"),
            ("utopia", "slide_out"),
            ("utopia", "slide_out"),
            ("utopia", "slide_out"),
            ("utopia", "slide_out"),
        ]

    def test_reverse_resolution_order_keeps_the_cache_honest(self) -> None:
        from krok_helper.subtitle_render.engine.layout.display.section_edges import (
            section_edge_context,
        )
        from krok_helper.subtitle_render.engine.layout.layout_context import layout_pass
        from krok_helper.subtitle_render.engine.layout.line.style import style_for_line

        track = _section_edge_track()
        style = self._style()
        with layout_pass():
            section_edge_context(track, style)
            middle = style_for_line(style, track.lines[2])
            head = style_for_line(style, track.lines[0])
        assert (middle.entry_anim, middle.exit_anim) == ("fade", "fade")
        assert (head.entry_anim, head.exit_anim) == ("utopia", "fade")

    def test_disabled_leaves_every_line_untouched(self) -> None:
        from krok_helper.subtitle_render.engine.layout.display.section_edges import (
            section_edge_context,
        )
        from krok_helper.subtitle_render.engine.layout.layout_context import layout_pass
        from krok_helper.subtitle_render.engine.layout.line.style import style_for_line

        track = _section_edge_track()
        style = Style(
            section_edge_anim_enabled=False,
            section_edge_both_animations=True,
            section_head_anim="utopia",
            section_tail_anim="slide_out",
        )
        with layout_pass():
            section_edge_context(track, style)
            resolved = [style_for_line(style, line) for line in track.lines]
        assert all(
            (s.entry_anim, s.exit_anim) == ("fade", "fade") for s in resolved
        )

    def test_a_manual_override_wins_on_edge_lines(self) -> None:
        from krok_helper.subtitle_render.engine.layout.display.section_edges import (
            section_edge_context,
        )
        from krok_helper.subtitle_render.engine.layout.layout_context import layout_pass
        from krok_helper.subtitle_render.engine.layout.line.style import style_for_line

        track = _section_edge_track()
        track.lines[0].animation_override = LineAnimationOverride(
            entry_anim="rise", exit_anim="rise"
        )
        style = self._style()
        with layout_pass():
            section_edge_context(track, style)
            resolved = [style_for_line(style, line) for line in track.lines]
        assert (resolved[0].entry_anim, resolved[0].exit_anim) == ("rise", "rise")
        assert (resolved[1].entry_anim, resolved[1].exit_anim) == (
            "utopia",
            "fade",
        )

    def test_the_style_cache_key_includes_the_edge_flag(self) -> None:
        """缓存键是逐字段列举的；漏掉段边缘标记，中页会串首页的动画。"""
        import inspect

        from krok_helper.subtitle_render.engine.layout.line import style as line_style

        source = inspect.getsource(line_style.style_for_line)
        assert "line_section_edge_flags" in source


class TestSectionEdgeSummary:
    """歌词列表「特效」列显示替换后的实际生效值，手动覆盖仍优先。"""

    def test_each_side_shows_only_its_own_replacement_by_default(self) -> None:
        from krok_helper.subtitle_render.frontend.editor.lyrics_list import (
            _animation_summary,
        )

        style = Style(
            section_edge_anim_enabled=True,
            section_head_anim="utopia",
            section_tail_anim="slide_out",
        )
        assert _animation_summary(style, None, (True, False)) == "全局：Utopia / 淡出"
        assert _animation_summary(style, None, (False, True)) == "全局：淡入 / 滑出"
        # 单页段两侧都替换。
        assert _animation_summary(style, None, (True, True)) == "全局：Utopia / 滑出"
        assert _animation_summary(style, None) == "全局：淡入 / 淡出"

    def test_both_mode_replaces_the_other_side_too(self) -> None:
        from krok_helper.subtitle_render.frontend.editor.lyrics_list import (
            _animation_summary,
        )

        style = Style(
            section_edge_anim_enabled=True,
            section_edge_both_animations=True,
            section_head_anim="utopia",
            section_tail_anim="slide_out",
        )
        assert _animation_summary(style, None, (True, False)) == "全局：Utopia / 滑出"
        assert _animation_summary(style, None, (False, True)) == "全局：Utopia / 滑出"

    def test_a_manual_override_still_wins_in_the_summary(self) -> None:
        from krok_helper.subtitle_render.frontend.editor.lyrics_list import (
            _animation_summary,
        )

        style = Style(
            section_edge_anim_enabled=True,
            section_head_anim="utopia",
            section_tail_anim="slide_out",
        )
        override = LineAnimationOverride(entry_anim="rise", exit_anim="rise")
        assert _animation_summary(style, override, (True, False)) == "上升 / 上升"


class TestSectionEdgeGpuParity:
    """GPU 拿到的逐行入退场必须已经是替换后的值——协议按行发字段，无需 C++ 改动。"""

    def test_the_ir_carries_the_replaced_per_line_animations(self, qapp) -> None:
        from krok_helper.subtitle_render.engine.painter import build_track_layout_plan
        from krok_helper.subtitle_render.native.protocol import track_to_ir

        track = _section_edge_track()
        style = TestSectionEdgeAnimation._style()
        ir = track_to_ir(
            track,
            style,
            layout_plan=build_track_layout_plan(track, style),
        )
        assert [line["entry_anim"] for line in ir["lines"]] == [
            "utopia", "utopia", "fade", "fade", "fade", "fade", "utopia", "utopia",
        ]
        assert [line["exit_anim"] for line in ir["lines"]] == [
            "fade", "fade", "fade", "fade", "slide_out", "slide_out",
            "slide_out", "slide_out",
        ]

    def test_it_does_not_force_a_painter_fallback(self, qapp) -> None:
        from krok_helper.subtitle_render.native.protocol import gpu_unsupported_features

        assert gpu_unsupported_features(
            _section_edge_track(), TestSectionEdgeAnimation._style()
        ) == ()


class TestSectionEdgeListRefresh:
    """段首尾设置变化后，歌词列表「特效」列跟着显示生效值。"""

    @staticmethod
    def _effect_texts(panel) -> list[str]:
        table = panel.table_widget
        texts = []
        for row in range(table.rowCount()):
            item = table.item(row, 2)  # COL_EFFECT
            if item is not None and item.text():
                texts.append(item.text())
        return texts

    def test_the_effect_column_follows_the_section_edge_settings(self, qapp) -> None:
        from krok_helper.subtitle_render.frontend.editor.lyrics_list import LyricsPanel

        panel = LyricsPanel()
        panel.set_track(_section_edge_track())
        assert self._effect_texts(panel) == ["全局：淡入 / 淡出"] * 8

        panel.set_style(
            Style(
                section_edge_anim_enabled=True,
                section_head_anim="utopia",
                section_tail_anim="slide_out",
            )
        )
        assert self._effect_texts(panel) == [
            "全局：Utopia / 淡出",
            "全局：Utopia / 淡出",
            "全局：淡入 / 淡出",
            "全局：淡入 / 淡出",
            "全局：淡入 / 滑出",
            "全局：淡入 / 滑出",
            "全局：Utopia / 滑出",
            "全局：Utopia / 滑出",
        ]

        # 打开「同时设置出入场」：段边缘页两侧一起换。
        panel.set_style(
            Style(
                section_edge_anim_enabled=True,
                section_edge_both_animations=True,
                section_head_anim="utopia",
                section_tail_anim="slide_out",
            )
        )
        assert self._effect_texts(panel) == [
            "全局：Utopia / 滑出",
            "全局：Utopia / 滑出",
            "全局：淡入 / 淡出",
            "全局：淡入 / 淡出",
            "全局：Utopia / 滑出",
            "全局：Utopia / 滑出",
            "全局：Utopia / 滑出",
            "全局：Utopia / 滑出",
        ]

        # 关掉开关回到全局值——来回切都要跟得上。
        panel.set_style(Style())
        assert self._effect_texts(panel) == ["全局：淡入 / 淡出"] * 8
