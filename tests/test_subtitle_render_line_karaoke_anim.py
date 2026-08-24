"""逐行唱字特效覆盖。

原先唱字特效只有全局一档，任何一行都改不了——次字幕只能跟着主字幕走。
现在 ``LineAnimationOverride`` 也能带唱字动画，``inherit`` 表示仍跟全局。
"""

from __future__ import annotations

import pytest

from krok_helper.subtitle_render.models import (
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
        assert values == {"inherit", "none", "utopia"}

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
        from krok_helper.subtitle_render.models import TimingChar, TimingTrack

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
        from krok_helper.subtitle_render.native_protocol import track_to_ir

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
        from krok_helper.subtitle_render.native_protocol import gpu_unsupported_features

        style = Style()
        style.karaoke_anim = "utopia"
        assert gpu_unsupported_features(self._track(), style) == ()
