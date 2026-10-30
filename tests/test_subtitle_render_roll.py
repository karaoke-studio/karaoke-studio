"""前黑/后黑领域模型与时间线计算测试。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from krok_helper.subtitle_render.domain.background import BackgroundSource
from krok_helper.subtitle_render.domain.models import Style, TitleOverlay, TitleTimeWindow
from krok_helper.subtitle_render.domain.roll import (
    ROLL_FILL_FIT,
    ROLL_FILL_STRETCH,
    ROLL_MAIN_TRANSITION_BLACK,
    ROLL_MAIN_TRANSITION_CROSSFADE,
    ROLL_MAIN_TRANSITION_WHITE,
    RollSegmentConfig,
    build_roll_timeline,
    effective_roll_ms,
    roll_fill_dest_rect,
    roll_layer_alphas,
    roll_segment_active,
    roll_segment_from_dict,
    roll_segment_to_dict,
    roll_tile_geometry,
    shift_style_title_overlays,
)


def _video_config(duration_ms=4000, **kwargs) -> RollSegmentConfig:
    return RollSegmentConfig(
        enabled=True,
        source=BackgroundSource(kind="video", path="D:/media/intro.mp4"),
        duration_ms=duration_ms,
        **kwargs,
    )


# --------------------------------------------------------------------------- 段配置

def test_segment_defaults_are_three_seconds_and_300ms_transitions():
    config = RollSegmentConfig(enabled=True, source=BackgroundSource(kind="solid"))
    assert config.duration_ms == 3000
    assert config.main_transition_ms == 300
    assert config.self_transition_ms == 300
    assert config.main_transition_style == ROLL_MAIN_TRANSITION_CROSSFADE
    assert config.fill == ROLL_FILL_FIT


def test_roll_source_duration_hint_format():
    from krok_helper.subtitle_render.domain.roll import format_roll_source_duration

    assert format_roll_source_duration(0) == "【素材时长： 00:00:00.000（0.000秒）】"
    assert (
        format_roll_source_duration(90_500)
        == "【素材时长： 00:01:30.500（90.500秒）】"
    )
    assert (
        format_roll_source_duration(3_723_004)
        == "【素材时长： 01:02:03.004（3723.004秒）】"
    )


def test_segment_disabled_by_switch_or_duration_or_source():
    assert not roll_segment_active(RollSegmentConfig())
    assert not roll_segment_active(RollSegmentConfig(enabled=True, duration_ms=0))
    missing = RollSegmentConfig(
        enabled=True,
        duration_ms=1000,
        source=BackgroundSource(kind="video", path=None),
    )
    assert not roll_segment_active(missing)
    assert effective_roll_ms(missing) == 0


def test_segment_round_trip_keeps_all_parameters():
    config = RollSegmentConfig(
        enabled=True,
        source=BackgroundSource(
            kind="image_sequence", path="D:/seq/frame_%04d.png", source_fps=30,
            sequence_start_number=1,
        ),
        source_width=640,
        source_height=360,
        source_duration_ms=12_345,
        audio_path="D:/audio/intro.wav",
        duration_ms=2500,
        fill="tile",
        auto_offset_subtitles=False,
        main_transition_ms=800,
        main_transition_style="crossfade",
        self_transition_ms=300,
        self_transition_style="white",
    )
    restored = roll_segment_from_dict(roll_segment_to_dict(config))
    assert restored == config.normalized()


def test_segment_from_dict_defaults_for_legacy_payload():
    assert roll_segment_from_dict(None) == RollSegmentConfig()
    empty = roll_segment_from_dict({})
    assert not empty.enabled
    assert empty.fill == ROLL_FILL_FIT
    assert empty.auto_offset_subtitles is True
    assert roll_segment_from_dict({"fill": "bogus"}).fill == ROLL_FILL_FIT


def test_segment_normalize_clamps_negative_durations():
    config = RollSegmentConfig(
        enabled=True,
        duration_ms=-5,
        main_transition_ms=-1,
        self_transition_ms=-1,
        source=BackgroundSource(kind="solid"),
    )
    assert effective_roll_ms(config) == 0


# --------------------------------------------------------------------------- 时间线

def _post(duration_ms=3000, **kwargs) -> RollSegmentConfig:
    return RollSegmentConfig(
        enabled=True,
        source=BackgroundSource(kind="solid", color="#101010"),
        duration_ms=duration_ms,
        **kwargs,
    )


def test_segment_has_audio_requires_active_and_audio_source():
    from krok_helper.subtitle_render.domain.roll import roll_segment_has_audio

    assert not roll_segment_has_audio(RollSegmentConfig())
    video_with_audio = RollSegmentConfig(
        enabled=True,
        duration_ms=1000,
        source=BackgroundSource(kind="video", path="D:/intro.mp4"),
        source_has_audio=True,
    )
    assert roll_segment_has_audio(video_with_audio)
    silent_video = replace(video_with_audio, source_has_audio=False)
    assert not roll_segment_has_audio(silent_video)
    independent = RollSegmentConfig(
        enabled=True,
        duration_ms=1000,
        source=BackgroundSource(kind="solid"),
        audio_path="D:/intro.wav",
    )
    assert roll_segment_has_audio(independent)
    assert not roll_segment_has_audio(replace(video_with_audio, duration_ms=0))


def test_timeline_without_rolls_matches_legacy_duration():
    plan = build_roll_timeline(
        None, None, track_end_ms=90_000,
        main_media_end_ms=120_000, background_kind="video",
    )
    assert not plan.active
    assert plan.total_ms == 120_000
    assert plan.content_end_ms == 120_000
    assert plan.lyric_shift_ms == 0
    assert plan.main_end_ms == 120_000


def test_timeline_total_takes_max_of_track_and_video_plus_rolls():
    # 需求口径：total = max(前黑+字幕轴, 前黑+视频+后黑)。
    plan = build_roll_timeline(
        _video_config(4000), _post(3000),
        track_end_ms=90_000,
        main_media_end_ms=120_000, background_kind="video",
    )
    assert plan.pre_ms == 4000
    assert plan.post_ms == 3000
    assert plan.main_end_ms == 4000 + 120_000
    assert plan.total_ms == max(4000 + 90_000, 4000 + 120_000 + 3000)
    assert plan.total_ms == 127_000


def test_timeline_post_attaches_after_video_even_when_track_longer():
    plan = build_roll_timeline(
        _video_config(1000), _post(2000),
        track_end_ms=200_000,
        main_media_end_ms=60_000, background_kind="video",
    )
    # 后黑接在原背景视频后：60000+1000 处，即使字幕轴更长。
    assert plan.main_end_ms == 61_000
    # 整体时长由更长的字幕轴分支决定。
    assert plan.total_ms == 201_000
    assert plan.content_end_ms == 201_000


def test_timeline_non_video_background_uses_content_end_for_post():
    plan = build_roll_timeline(
        None, _post(5000),
        track_end_ms=100_000, main_audio_end_ms=80_000,
        background_kind="image",
    )
    assert plan.main_end_ms == 100_000
    assert plan.total_ms == 105_000


def test_timeline_lyric_shift_follows_auto_offset_switch():
    plan_on = build_roll_timeline(
        _video_config(4000), None, track_end_ms=10_000, background_kind="solid",
    )
    assert plan_on.lyric_shift_ms == 4000
    plan_off = build_roll_timeline(
        _video_config(4000, auto_offset_subtitles=False), None,
        track_end_ms=10_000, background_kind="solid",
    )
    assert plan_off.lyric_shift_ms == 0
    # 后黑不产生任何字幕偏移。
    plan_post_only = build_roll_timeline(
        None, _post(4000), track_end_ms=10_000, background_kind="solid",
    )
    assert plan_post_only.lyric_shift_ms == 0


def test_timeline_inactive_segment_contributes_nothing():
    disabled = RollSegmentConfig(
        enabled=False,
        duration_ms=9000,
        source=BackgroundSource(kind="solid"),
    )
    plan = build_roll_timeline(
        disabled, _post(2000), track_end_ms=30_000, background_kind="solid",
    )
    assert plan.pre_ms == 0
    assert plan.main_end_ms == 30_000
    assert plan.total_ms == 32_000


# --------------------------------------------------------------------------- 过渡 alpha

def _plan_with_transitions(pre_style=ROLL_MAIN_TRANSITION_BLACK, v=1000):
    return build_roll_timeline(
        _video_config(
            4000,
            main_transition_ms=v,
            main_transition_style=pre_style,
            self_transition_ms=200,
        ),
        None,
        track_end_ms=10_000, background_kind="solid",
    )


def test_layer_alphas_without_rolls():
    plan = build_roll_timeline(None, None, track_end_ms=1000, background_kind="solid")
    alphas = roll_layer_alphas(plan, 0)
    assert alphas.main_alpha == 1.0
    assert alphas.pre_alpha == 0.0
    assert alphas.post_alpha == 0.0


def test_layer_alphas_pre_black_transition_curve():
    plan = _plan_with_transitions(ROLL_MAIN_TRANSITION_BLACK, v=1000)
    # Adobe 式居中窗口 [3500, 4500]（剪切点 4000）：前半前黑淡出，后半主段淡入。
    start = roll_layer_alphas(plan, 3500)
    assert abs(start.pre_alpha - 1.0) < 1e-9
    assert start.main_alpha == 0.0
    cut = roll_layer_alphas(plan, 4000)
    assert cut.pre_alpha == 0.0
    assert cut.main_alpha == 0.0
    quarter = roll_layer_alphas(plan, 3750)  # 前半中点
    assert abs(quarter.pre_alpha - 0.5) < 1e-9
    assert abs(quarter.main_alpha - 0.0) < 1e-9
    three_quarters = roll_layer_alphas(plan, 4250)  # 后半中点
    assert abs(three_quarters.pre_alpha - 0.0) < 1e-9
    assert abs(three_quarters.main_alpha - 0.5) < 1e-9
    # 主段本体区主层全显。
    body = roll_layer_alphas(plan, 5000)
    assert body.main_alpha == 1.0
    assert body.pre_alpha == 0.0


def test_layer_alphas_crossfade_blends_over_full_window():
    plan = _plan_with_transitions(ROLL_MAIN_TRANSITION_CROSSFADE, v=1000)
    quarter = roll_layer_alphas(plan, 3250)  # x=0.25
    assert abs(quarter.pre_alpha - 1.0) < 1e-9
    assert abs(quarter.main_alpha - 0.25) < 1e-9
    mid = roll_layer_alphas(plan, 3500)  # x=0.5
    assert abs(mid.pre_alpha - 1.0) < 1e-9
    assert abs(mid.main_alpha - 0.5) < 1e-9


def test_layer_alphas_white_transition_flash_peaks_at_cut():
    plan = _plan_with_transitions(ROLL_MAIN_TRANSITION_WHITE, v=1000)
    cut = roll_layer_alphas(plan, 4000)
    assert abs(cut.flash_alpha - 1.0) < 1e-9
    quarter = roll_layer_alphas(plan, 3750)
    assert abs(quarter.flash_alpha - 0.5) < 1e-9
    after_quarter = roll_layer_alphas(plan, 4250)
    assert abs(after_quarter.flash_alpha - 0.5) < 1e-9
    outside = roll_layer_alphas(plan, 2500)
    assert outside.flash_alpha == 0.0


def test_layer_alphas_self_fade_in_at_start():
    plan = _plan_with_transitions(ROLL_MAIN_TRANSITION_BLACK, v=0)
    start = roll_layer_alphas(plan, 0)
    assert start.pre_alpha == 0.0  # 自我淡入从全黑开始
    half = roll_layer_alphas(plan, 100)  # S=200 → 0.5
    assert abs(half.pre_alpha - 0.5) < 1e-9
    settled = roll_layer_alphas(plan, 500)
    assert settled.pre_alpha == 1.0


def test_layer_alphas_post_black_and_self_fade_out():
    plan = build_roll_timeline(
        None,
        _post(
            2000,
            main_transition_ms=1000,
            main_transition_style=ROLL_MAIN_TRANSITION_BLACK,
            self_transition_ms=500,
        ),
        track_end_ms=10_000, background_kind="solid",
    )
    # Adobe 式居中窗口 [9500, 10500]（剪切点 10000）：主段前半淡出、后黑后半淡入。
    before = roll_layer_alphas(plan, 9250)
    assert before.main_alpha == 1.0
    assert before.post_alpha == 0.0
    quarter = roll_layer_alphas(plan, 9750)  # 前半中点
    assert abs(quarter.main_alpha - 0.5) < 1e-9
    assert quarter.post_alpha == 0.0
    cut = roll_layer_alphas(plan, 10_000)
    assert cut.main_alpha == 0.0
    assert cut.post_alpha == 0.0
    three_quarters = roll_layer_alphas(plan, 10_250)  # 后半中点
    assert abs(three_quarters.main_alpha - 0.0) < 1e-9
    assert abs(three_quarters.post_alpha - 0.5) < 1e-9
    # 后黑结尾 500ms 自我淡出：total=12000，t=11750 → 0.5。
    fade_out = roll_layer_alphas(plan, 11_750)
    assert abs(fade_out.post_alpha - 0.5) < 1e-9


# --------------------------------------------------------------------------- 填充几何

def test_fill_geometry_modes():
    # stretch 铺满。
    assert roll_fill_dest_rect(ROLL_FILL_STRETCH, 640, 480, 1920, 1080) == (0, 0, 1920, 1080)
    # fit 等比完整放入 + 居中黑边（4:3 素材进 16:9 画布：高顶满、左右留边）。
    x, y, w, h = roll_fill_dest_rect(ROLL_FILL_FIT, 640, 480, 1920, 1080)
    assert (w, h) == (1440, 1080)
    assert (x, y) == (240, 0)
    # original 原始尺寸居中（可越界）。
    assert roll_fill_dest_rect("original", 640, 480, 1920, 1080) == (
        (1920 - 640) // 2, (1080 - 480) // 2, 640, 480,
    )
    # tile 列行数 = ceil(画布/素材)。
    assert roll_tile_geometry(640, 480, 1920, 1080) == (3, 3)
    assert roll_tile_geometry(1920, 1080, 1920, 1080) == (1, 1)


# --------------------------------------------------------------------------- 标题平移

def _style_with_titles() -> Style:
    return Style(
        title_overlays=[
            TitleOverlay(
                name="正片标题",
                enabled=True,
                show_mode="head",
                head_offset_ms=10_000,
                duration_ms=5000,
            ),
            TitleOverlay(
                name="前黑区间标题",
                enabled=True,
                show_mode="custom",
                custom_windows=[TitleTimeWindow(begin_ms=1000, end_ms=2500)],
            ),
        ]
    )


def test_title_shift_moves_head_and_custom_windows():
    style, clamped = shift_style_title_overlays(_style_with_titles(), 30_000)
    assert clamped == 0
    head, pre_window = style.title_overlays
    assert head.head_offset_ms == 40_000
    assert pre_window.custom_windows[0].begin_ms == 31_000
    assert pre_window.custom_windows[0].end_ms == 32_500


def test_title_shift_down_clamps_would_be_negative_windows():
    # 前黑 30s 开启期间作者的时间：主片标题 display 40s（正片 10s + 前黑），
    # 前黑区间标题 display 1~2.5s。关闭前黑整体 -30s：前者恢复 10s，
    # 后者落入负值被钳 0（完全负区间 → 空窗）。
    style = Style(
        title_overlays=[
            TitleOverlay(
                name="正片标题",
                enabled=True,
                show_mode="head",
                head_offset_ms=40_000,
                duration_ms=5000,
            ),
            TitleOverlay(
                name="前黑区间标题",
                enabled=True,
                show_mode="custom",
                custom_windows=[TitleTimeWindow(begin_ms=1000, end_ms=2500)],
            ),
        ]
    )
    restored, clamped = shift_style_title_overlays(style, -30_000)
    assert clamped == 1
    head, pre_window = restored.title_overlays
    assert head.head_offset_ms == 10_000
    window = pre_window.custom_windows[0]
    # 完全落入负区间的窗口：锚回 0 但保留原持续时长（1~2.5s → 0~1.5s），
    # 标题不凭空消失。
    assert window.begin_ms == 0
    assert window.end_ms == 1_500


def test_title_shift_zero_delta_returns_style_untouched():
    style = _style_with_titles()
    same, clamped = shift_style_title_overlays(style, 0)
    assert same is style
    assert clamped == 0


# --------------------------------------------------------------------------- CPU/GPU 渲染口径

def test_painter_roll_shift_delays_lyrics_not_titles():
    """roll_shift_ms 只后移歌词（painter 展示层），标题用原始 display 时间。"""
    from krok_helper.subtitle_render.domain.timing import (
        TimingChar,
        TimingLine,
        TimingTrack,
    )
    from krok_helper.subtitle_render.engine.painter import frame_has_content

    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar(text="测", start_ms=10_000)],
                end_ms=12_000,
            )
        ]
    )
    style = Style(
        title_overlays=[
            TitleOverlay(
                name="标题",
                enabled=True,
                show_mode="custom",
                custom_windows=[TitleTimeWindow(begin_ms=0, end_ms=3000)],
            )
        ]
    )
    # 无偏移：5s 处无歌词（歌词 10s 起）、标题（0-3s）也不可见。
    assert not frame_has_content(track, 5000, style)
    # +8000 偏移：歌词推到 18s 起 → 15s 无歌词；标题窗口不受偏移 → 5s 仍不可见。
    assert not frame_has_content(track, 5000, style, roll_shift_ms=8000)
    # 标题不受 roll 偏移：1s 处可见性与偏移无关。
    assert frame_has_content(track, 1000, style)
    assert frame_has_content(track, 1000, style, roll_shift_ms=8000)
    # 歌词在 10s（无偏移）/ 18s（+8000）处可见。
    assert frame_has_content(track, 10_500, style)
    assert frame_has_content(track, 10_500, style, roll_shift_ms=8000) is False
    assert frame_has_content(track, 18_500, style, roll_shift_ms=8000)


def test_render_ir_roll_shift_bakes_payload_offset_not_deltas():
    """GPU IR：payload 全局 timing_offset_ms +P、每源 delta 不变、标题窗口不动。

    C++ 侧行窗口偏移 = payload全局 + meta + delta，跟随/非跟随副轨都恰好
    +P（与 CPU painter 同口径）；标题窗口是展示坐标，不参与全局偏移。
    """
    from krok_helper.subtitle_render.domain.timing import (
        TimingTrack as Track,
        TimingLine as Line,
        TimingChar as Char,
    )
    from krok_helper.subtitle_render.engine.render.render_ir import (
        build_render_ir,
        build_style_patch_ir,
    )

    track = Track(
        lines=[Line(chars=[Char(text="测", start_ms=1000)], end_ms=2000)]
    )
    style = Style()
    base = build_render_ir(track, style, width=64, height=36, fps=60)
    shifted = build_render_ir(track, style, width=64, height=36, fps=60, roll_shift_ms=4000)
    base_offset = int(base["style"]["timing_offset_ms"])
    assert int(shifted["style"]["timing_offset_ms"]) == base_offset + 4000
    # 每源 offset_ms（meta + delta）不变；C++ 侧再叠加 payload 全局值 → 全源 +P。
    assert (
        shifted["track"]["meta"]["offset_ms"]
        == base["track"]["meta"]["offset_ms"]
    )
    assert shifted["extra_tracks"] == base["extra_tracks"]
    # 标题段不受影响。
    assert shifted["titles"] == base["titles"]
    # 差分 style patch 同口径。
    patch = build_style_patch_ir(
        track, style, width=64, height=36, fps=60, roll_shift_ms=4000
    )
    assert int(patch["style"]["timing_offset_ms"]) == base_offset + 4000


def test_document_round_trip_keeps_roll_configs():
    """SubtitleProjectDocument → project_payload → ProjectLoadPlan 全链路保真。"""
    from krok_helper.subtitle_render.project.load import ProjectLoadPlan
    from krok_helper.subtitle_render.project.session import SubtitleProjectDocument

    document = SubtitleProjectDocument(
        pre_roll=RollSegmentConfig(
            enabled=True,
            source=BackgroundSource(kind="image", path="D:/intro.gif"),
            source_width=100,
            source_height=80,
            source_duration_ms=12_345,
            duration_ms=2500,
            fill="tile",
            auto_offset_subtitles=False,
            main_transition_ms=400,
            main_transition_style="crossfade",
        ),
        post_roll=RollSegmentConfig(
            enabled=True,
            source=BackgroundSource(kind="solid", color="#204060"),
            audio_path="D:/outro.wav",
            duration_ms=1800,
            self_transition_ms=250,
            self_transition_style="white",
        ),
    )
    payload = document.to_project_data(
        screen={"width": 1920, "height": 1080, "fps": 60},
        selected_scheme_key="global",
        output={},
    )
    assert payload["pre_roll"]["duration_ms"] == 2500
    assert payload["pre_roll"]["source_duration_ms"] == 12_345
    assert payload["post_roll"]["audio_path"] == "D:/outro.wav"
    plan = ProjectLoadPlan.from_data(payload)
    assert plan.pre_roll == document.pre_roll
    assert plan.post_roll == document.post_roll
    # 旧工程（无前黑/后黑键）→ None。
    legacy = ProjectLoadPlan.from_data(
        {"style": {}, "screen": {"width": 1920, "height": 1080, "fps": 60}}
    )
    assert legacy.pre_roll is None and legacy.post_roll is None


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_transport_bar_maps_display_time_to_media_with_pre_roll(qapp):
    """需求10：前黑开启时 seek/播放头映射 display = 媒体位置 + 前黑。"""
    from krok_helper.subtitle_render.frontend.preview.preview_view import TransportBar

    class _FakeController:
        def __init__(self):
            self.positions: list[int] = []
            self.playing = False
            self._media = True

        def has_media(self):
            return self._media

        def seek(self, ms):
            self.positions.append(int(ms))

        def play(self):
            self.playing = True

        def pause(self):
            self.playing = False

        def position(self):
            return 0

        def duration(self):
            return 5000

    bar = TransportBar()
    controller = _FakeController()
    bar.attach_playback_controller(controller)
    bar.set_duration(10_000)

    def _seek(ms: int) -> None:
        bar._set_slider_silently(ms)  # 同步滑块值（头区判断读它）
        bar._on_slider_changed(ms)    # 用户拖动路径 → 映射到媒体位置

    # 无前黑：滑块 7s → 媒体 7s。
    _seek(7000)
    assert controller.positions[-1] == 7000

    # 前黑 4s：display 7s → 媒体 3s。
    class _Plan:
        pre_ms = 4000

    bar.set_roll_plan(_Plan())
    _seek(7000)
    assert controller.positions[-1] == 3000
    # 头区（display < 前黑）→ 媒体钳 0，播放头区生效。
    _seek(2000)
    assert controller.positions[-1] == 0
    assert bar._media_head_active()
    # 关闭前黑恢复直通。
    bar.set_roll_plan(type("P", (), {"pre_ms": 0})())
    _seek(7000)
    assert controller.positions[-1] == 7000


def test_title_shift_partial_overlap_keeps_visible_part():
    """跨前黑边界的窗口（authored 10~40s，前黑 30s 关闭 → -20~10s）保留正片剩余。"""
    style = Style(
        title_overlays=[
            TitleOverlay(
                name="跨界标题",
                enabled=True,
                show_mode="custom",
                custom_windows=[TitleTimeWindow(begin_ms=10_000, end_ms=40_000)],
            )
        ]
    )
    restored, clamped = shift_style_title_overlays(style, -30_000)
    assert clamped == 1
    window = restored.title_overlays[0].custom_windows[0]
    assert window.begin_ms == 0  # -20s 钳 0
    assert window.end_ms == 10_000  # 正片剩余 10s 完整保留


def test_timeline_auto_offset_off_does_not_add_pre_to_track_axis():
    """auto_offset 关闭：字幕轴保持原时间，总时长不再无条件 +前黑。"""
    plan = build_roll_timeline(
        _video_config(30_000, auto_offset_subtitles=False),
        None,
        track_end_ms=200_000,
        main_media_end_ms=60_000,
        background_kind="video",
    )
    assert plan.lyric_shift_ms == 0
    # total = max(字幕轴 200s, 30+60+0=90s) = 200s（不加前黑）。
    assert plan.total_ms == 200_000
    # 标题尾锚（内容结束）= max(字幕轴, 前黑+主素材) = 200s。
    assert plan.content_end_ms == 200_000

    plan_on = build_roll_timeline(
        _video_config(30_000, auto_offset_subtitles=True),
        None,
        track_end_ms=200_000,
        main_media_end_ms=60_000,
        background_kind="video",
    )
    # 对照：auto 开 → max(30+200, 30+60) = 230s。
    assert plan_on.total_ms == 230_000
    assert plan_on.content_end_ms == 230_000


def test_timeline_view_displays_and_writes_display_coordinates(qapp):
    """轨道视图按 display 坐标显示块、写回时减回源坐标（含把手覆盖）。"""
    from krok_helper.subtitle_render.frontend.editor.timeline_view import (
        TrackTimelineView,
        build_lanes,
    )
    from krok_helper.subtitle_render.domain.timing import (
        TimingChar,
        TimingLine,
        TimingTrack,
    )

    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar(text="测", start_ms=10_000), TimingChar(text="试", start_ms=12_000)],
                end_ms=14_000,
            )
        ]
    )
    # 无偏移：块时刻 = 源时刻。
    lanes = build_lanes([("主字幕", track)])
    assert lanes[0].blocks[0].start_ms == 10_000
    assert lanes[0].blocks[0].end_ms == 14_000
    # +8000：整体按 display 显示。
    lanes = build_lanes([("主字幕", track)], time_offset_ms=8000)
    assert lanes[0].blocks[0].start_ms == 18_000
    assert lanes[0].blocks[0].end_ms == 22_000
    assert lanes[0].blocks[0].cells[0].start_ms == 18_000

    view = TrackTimelineView()
    view.set_tracks([("主字幕", track)])
    view.set_timing_display_offset(8000)
    block = view._lanes[0].blocks[0]
    assert block.start_ms == 18_000
    # 把手拖到 display 20s → 写回源坐标 12s。
    view._set_display_start_override(0, block, 20_000)
    assert track.lines[0].display_start_override_ms == 12_000
    # 余量编辑同理（display → source）。
    view._set_display_end_override(0, block, 25_000)
    assert track.lines[0].display_end_override_ms == 17_000


# --------------------------------------------------------------------------- 证据补强

def test_roll_shift_with_hand_tuned_display_overrides_and_extra_axis(qapp):
    """手调上屏/离屏覆盖 + 非跟随副轴（独立时间 overrides）随前黑整体平移。"""
    from krok_helper.subtitle_render.domain.timing import (
        TrackDisplayTiming,
        TimingChar,
        TimingLine,
        TimingTrack,
    )

    main = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar(text="主", start_ms=10_000)],
                end_ms=12_000,
                display_start_override_ms=9_500,  # 手调提前上屏
                display_end_override_ms=12_300,   # 手调延迟离屏
            )
        ]
    )
    extra = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar(text="副", start_ms=5_000)],
                end_ms=6_000,
            )
        ],
        display_timing=TrackDisplayTiming(
            follow_main=False,
            overrides={"timing_offset_ms": 2_000},  # 副轴独立时间设置
        ),
    )
    from krok_helper.subtitle_render.engine.painter import frame_has_content

    from krok_helper.subtitle_render.domain.models import Style as _Style

    style = _Style()  # 默认 lead_in 1800ms 也随偏移整体平移
    roll = 4_000
    # 无前黑：主轨行 10000~12000（含默认入场提前量）→ 10500 可见。
    assert frame_has_content(main, 10_500, style)
    # +前黑：主轨窗口平移到 12200~16200 → 11000/11500 均不可见；
    # 副轴有效窗口 = 源[5200,9800](含默认 lead) + 轴偏移 2000 + 前黑 4000
    # → display [11200, 15800]：11500 可见——同一时刻副轴可见、主轨不可见，
    # 验证副轴随前黑平移且独立时间不被双计。
    assert not frame_has_content(main, 11_500, style, roll_shift_ms=roll)
    assert frame_has_content(main, 11_500, style, [extra], roll_shift_ms=roll)
    # 主轨手调上屏 9500（源）→ 13500（display）后可见。
    assert frame_has_content(main, 13_600, style, roll_shift_ms=roll)
    # 副轴独立时间不受 roll 双计：display 11500 → 副轴源+2000-4000=7500 在
    # 其窗口内 ✓；若偏移漏加/双加都会错位。
    assert frame_has_content(main, 11_500, style, [extra], roll_shift_ms=roll)
    # GPU IR：副轴按绝对 overrides 叠加后与 CPU 同口径（payload 全局 +P、
    # 每源 offset_ms 保持源值）。
    from krok_helper.subtitle_render.engine.render.render_ir import build_render_ir

    base = build_render_ir(main, style, width=64, height=36, fps=60, extra_tracks=[extra])
    shifted = build_render_ir(
        main, style, width=64, height=36, fps=60, extra_tracks=[extra], roll_shift_ms=roll
    )
    assert (
        int(shifted["style"]["timing_offset_ms"])
        == int(base["style"]["timing_offset_ms"]) + roll
    )
    for base_extra, shifted_extra in zip(base["extra_tracks"], shifted["extra_tracks"]):
        # C++：sourceOffset = meta+delta（源口径不变），叠加 payload 全局 +P
        # → 非跟随副轴有效偏移 = 2000 + P，与 CPU 的 style_for_track(绝对值)
        # + roll_shift 完全一致。
        assert (
            shifted_extra["meta"]["offset_ms"] == base_extra["meta"]["offset_ms"]
        )


def test_pre_roll_reveals_full_lead_in_animation(qapp):
    """需求6：行首入场（指示灯/音量柱类 lead-in）在视频开头被钳断的动画，
    前黑开启后可完整显示。"""
    from krok_helper.subtitle_render.domain.timing import (
        TimingChar,
        TimingLine,
        TimingTrack,
    )
    from krok_helper.subtitle_render.engine.painter import frame_has_content

    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar(text="测", start_ms=1_000)],
                end_ms=3_000,
            )
        ]
    )
    style = Style(line_lead_in_ms=5_000, line_tail_ms=200)
    # 无前黑：lead-in 窗口 [1000-5000, 3000+200] 被钳到 [0, 3200]——
    # 入场动画前 4 秒在视频外不可见（t=0 行已在场）。
    assert frame_has_content(track, 0, style)
    # 前黑 8000 + 自动偏移：窗口整体平移到 [4000, 11200]——
    # 动画从 display 4000 完整开始（3999 不可见、4001 可见）。
    assert not frame_has_content(track, 3_999, style, roll_shift_ms=8_000)
    assert frame_has_content(track, 4_001, style, roll_shift_ms=8_000)
    assert frame_has_content(track, 11_100, style, roll_shift_ms=8_000)
    assert not frame_has_content(track, 11_300, style, roll_shift_ms=8_000)


def _gpu_renderer_for_roll():
    import os as _os

    if _os.name != "nt":
        return None
    from krok_helper.subtitle_render.native.backend import (
        NativeRendererProcess,
        resolve_native_renderer_path,
    )

    path = resolve_native_renderer_path()
    if path is None:
        return None
    return NativeRendererProcess(path, response_timeout_s=15.0)


def test_gpu_and_painter_render_identical_frames_with_roll_shift(qapp):
    """GPU（Direct2D 回读）与 CPU Painter 在同一 display 时刻出同一布局帧。

    前黑开启（roll_shift=4000）：display t=5500 应见到主轨行（源 1500），
    t=1000（前黑区）无字幕；两后端帧 alpha 包络一致。"""
    from PyQt6.QtGui import QImage

    from krok_helper.subtitle_render.domain.timing import (
        TimingChar,
        TimingLine,
        TimingTrack,
    )
    from krok_helper.subtitle_render.engine.painter import paint_frame

    renderer = _gpu_renderer_for_roll()
    if renderer is None:
        pytest.skip("native subtitle renderer executable is not built")

    from krok_helper.subtitle_render.native.backend import SharedFrameRingReader

    _W, _H = 640, 360  # 与既有 GPU 测试同尺寸（sidecar 波段几何基准）

    from tests.test_subtitle_render_gpu_backend import _g1_style

    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar(text="K", start_ms=1_000),
                    TimingChar(text="a", start_ms=1_800),
                ],
                end_ms=2_600,
            )
        ]
    )
    # 沿用既有 GPU↔Painter 对比测试的 parity 基线样式（字体/描边/发光），
    # 排除字体回退度量差，聚焦前黑偏移本身。
    style = _g1_style()
    roll = 4_000
    timestamps = (1_000, 5_500, 6_200)
    gpu_frames: list[bytes] = []
    reader = None
    try:
        with renderer:
            renderer.configure_gpu(
                track,
                style,
                width=_W,
                height=_H,
                fps=60,
                force_warp=True,
                roll_shift_ms=roll,
            )
            for index, t_ms in enumerate(timestamps):
                event = renderer.render_gpu_frame(t_ms, force_warp=True, frame_index=index)
                if reader is None:
                    reader = SharedFrameRingReader.from_event(event)
                    reader.attach()
                image = reader.read_qimage(event).convertToFormat(
                    QImage.Format.Format_RGBA8888
                )
                bits = image.constBits()
                bits.setsize(image.sizeInBytes())
                gpu_frames.append(bytes(bits))
    finally:
        if reader is not None:
            reader.close()

    def alpha_bounds(payload: bytes) -> tuple[int, int, int]:
        """行级 alpha 包络：忽略抗锯齿噪声（alpha_min=32，与既有 GPU
        对比测试同一阈值思路），返回 (top, bottom, alpha_max)。"""
        alpha_max = 0
        top, bottom = _H, -1
        for row in range(_H):
            row_alpha_max = 0
            base = row * _W * 4
            for col in range(_W):
                a = payload[base + col * 4 + 3]
                if a > row_alpha_max:
                    row_alpha_max = a
            if row_alpha_max > 32:
                alpha_max = max(alpha_max, row_alpha_max)
                top = min(top, row)
                bottom = max(bottom, row)
        if bottom < top:
            return 0, 0, 0
        return top, bottom, alpha_max

    for t_ms, gpu in zip(timestamps, gpu_frames):
        image = QImage(_W, _H, QImage.Format.Format_RGBA8888)
        image.fill(0)
        paint_frame(image, track, t_ms, style, roll_shift_ms=roll)
        bits = image.constBits()
        bits.setsize(image.sizeInBytes())
        cpu = bytes(bits)
        gpu_bounds = alpha_bounds(gpu)
        cpu_bounds = alpha_bounds(cpu)
        # 前黑区（t=1000）：两后端都为空帧。
        if t_ms == 1_000:
            assert gpu_bounds[2] == 0 and cpu_bounds[2] == 0
            continue
        assert gpu_bounds[2] > 0 and cpu_bounds[2] > 0, t_ms
        # 包络容差：行高的 6%（字体光栅化/DWrite 度量的既有差异）。
        tolerance = max(6, _H * 6 // 100)
        assert abs(gpu_bounds[0] - cpu_bounds[0]) <= tolerance, (t_ms, gpu_bounds, cpu_bounds)
        assert abs(gpu_bounds[1] - cpu_bounds[1]) <= tolerance, (t_ms, gpu_bounds, cpu_bounds)
        assert abs(gpu_bounds[2] - cpu_bounds[2]) <= 3, (t_ms, gpu_bounds, cpu_bounds)


def _wait_until(qapp, predicate, timeout_ms: int = 5000) -> bool:
    import time

    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    qapp.processEvents()
    return predicate()


class _DictSettingsProvider:
    def __init__(self, data=None):
        self.data = dict(data or {})

    def load(self):
        return dict(self.data)

    def save(self, data):
        self.data = dict(data)


def test_roll_refresh_runs_as_async_worker_task(qapp):
    """开关前黑：重算走后台线程（真异步），结果回 GUI 线程落地。"""
    import importlib

    mw = importlib.import_module(
        "krok_helper.subtitle_render.frontend.main_window"
    )
    win = mw.SubtitleRenderWindow(embedded=True, settings_provider=_DictSettingsProvider())
    try:
        win._on_roll_config_changed(
            "pre",
            {"enabled": True, "duration_ms": 4000, "source_kind": "solid"},
        )
        # 回调立即返回：任务已派发（代数前进），轨道偏移由后台结果落地。
        assert win._roll_refresh_generation >= 1
        applied = _wait_until(
            qapp, lambda: win._tracks_view._timing_display_offset_ms == 4000
        )
        assert applied, "异步联动刷新未在超时内落地"
        assert win._transport_bar._slider.maximum() == 4000  # 总时长已联动
        # 关闭前黑：新代数的结果落地，偏移归零。
        win._on_roll_config_changed("pre", {"enabled": False})
        assert _wait_until(
            qapp, lambda: win._tracks_view._timing_display_offset_ms == 0
        )
        assert win._roll_refresh_generation >= 2
    finally:
        win.deleteLater()
        qapp.processEvents()


def test_roll_refresh_drops_stale_async_results(qapp, monkeypatch):
    """连改两次：只有最新代数的结果生效（过期结果被丢弃）。"""
    import importlib

    mw = importlib.import_module(
        "krok_helper.subtitle_render.frontend.main_window"
    )
    win = mw.SubtitleRenderWindow(embedded=True, settings_provider=_DictSettingsProvider())
    try:
        win._on_roll_config_changed(
            "pre",
            {"enabled": True, "duration_ms": 1000, "source_kind": "solid"},
        )
        win._on_roll_config_changed(
            "pre",
            {"duration_ms": 6000},
        )
        generation = win._roll_refresh_generation
        assert generation >= 2
        assert _wait_until(
            qapp, lambda: win._tracks_view._timing_display_offset_ms == 6000
        )
        # 旧代数结果迟到也不得回退已应用的状态。
        from krok_helper.subtitle_render.frontend.workflow.background_tasks import (
            RollRefreshResult,
        )

        win._on_roll_refresh_done(RollRefreshResult(generation - 1, None, None))
        assert win._tracks_view._timing_display_offset_ms == 6000
    finally:
        win.deleteLater()
        qapp.processEvents()


def test_roll_refresh_falls_back_to_sync_on_worker_failure(qapp, monkeypatch):
    """worker 计算失败（极端并发）→ 同步回退，状态仍正确落地。"""
    import importlib

    mw = importlib.import_module(
        "krok_helper.subtitle_render.frontend.main_window"
    )
    win = mw.SubtitleRenderWindow(embedded=True, settings_provider=_DictSettingsProvider())
    try:
        import krok_helper.subtitle_render.frontend.workflow.background_tasks as tasks

        def _boom(*args, **kwargs):
            raise RuntimeError("并发读失败")

        monkeypatch.setattr(tasks, "build_lanes", _boom, raising=False)
        win._on_roll_config_changed(
            "pre",
            {"enabled": True, "duration_ms": 2500, "source_kind": "solid"},
        )
        assert _wait_until(
            qapp, lambda: win._tracks_view._timing_display_offset_ms == 2500
        )
    finally:
        win.deleteLater()
        qapp.processEvents()


def test_subtitle_export_timing_reports_actual_pre_post(qapp):
    """交接给 Hi-Res 的前/后黑时长 = 实际时间线，而不是配置名义值。"""
    import importlib

    mw = importlib.import_module(
        "krok_helper.subtitle_render.frontend.main_window"
    )
    win = mw.SubtitleRenderWindow(
        embedded=True, settings_provider=_DictSettingsProvider()
    )
    try:
        pre = RollSegmentConfig(enabled=True, duration_ms=1200)
        post = RollSegmentConfig(enabled=True, duration_ms=800)
        plan = build_roll_timeline(
            pre,
            post,
            track_end_ms=5000,
            main_media_end_ms=5000,
            background_kind="video",
        )
        win._current_roll_plan = lambda: plan
        assert win.subtitle_export_timing() == (1200, 800)
        # 后黑关闭时，字幕尾超过主段也不能当成后黑音频切片。
        no_post = build_roll_timeline(
            pre,
            None,
            track_end_ms=10_000,
            main_media_end_ms=5_000,
            background_kind="video",
        )
        win._current_roll_plan = lambda: no_post
        assert win.subtitle_export_timing() == (1200, 0)
    finally:
        win.deleteLater()
        qapp.processEvents()


def test_review_post_only_and_disabled_layers(qapp):
    from PyQt6.QtWidgets import QGraphicsScene
    from krok_helper.subtitle_render.frontend.preview.roll_preview import RollPreviewController
    scene = QGraphicsScene()
    controller = RollPreviewController(scene)
    cfg = RollSegmentConfig(enabled=True, duration_ms=2000)
    plan = build_roll_timeline(None, cfg, track_end_ms=20000,
                               main_media_end_ms=10000, background_kind="video")
    controller.set_plan(plan)
    assert controller.source.pre is None
    assert controller.source.post.end_ms == 12000
    # 默认输出尺寸 1920x1080 时纯色/白闪矩形必须已初始化成整画布
    # （否则初次显示只剩一个点）。
    assert controller._post_rect.rect().width() == 1920
    assert controller._post_rect.rect().height() == 1080
    assert controller._flash_rect.rect().width() == 1920
    controller.set_time(11000, False)
    assert controller._post_rect.isVisible()
    controller._apply_layer(controller.source.post, 0.25, is_pre=False)
    assert controller._post_rect.opacity() == 0.25
    controller.set_plan(None)
    assert not controller._post_rect.isVisible()
    controller.shutdown()


def test_review_segment_audio_stays_inside_duration(qapp, monkeypatch):
    from krok_helper.subtitle_render.frontend.preview.roll_preview import RollSegmentFrameSource, RollSegmentRuntime
    source = RollSegmentFrameSource()
    segment = RollSegmentRuntime(RollSegmentConfig(), 10000, 12000)
    segment.audio_player = object()
    calls = []
    monkeypatch.setattr(source, "_sync_player", lambda player, pos, active: calls.append(active))
    source._playing = True
    for t in (9500, 10000, 11999, 12000, 12500):
        source._t_ms = t
        source._update_segment(segment, 1.0)
    assert calls == [False, True, True, False, False]
    segment.audio_player = None


def test_review_post_ends_before_longer_lyrics():
    cfg = RollSegmentConfig(enabled=True, duration_ms=2000, self_transition_ms=1000)
    plan = build_roll_timeline(None, cfg, track_end_ms=20000,
                               main_media_end_ms=10000, background_kind="video")
    assert roll_layer_alphas(plan, 11500).post_alpha == 0.5
    assert roll_layer_alphas(plan, 12000).post_alpha == 0


def test_review_post_image_visible_during_transition(qapp, tmp_path):
    """后黑图片/视频要在与主视频的过渡窗口内就可见（而不是等正式起点）。"""
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QImage
    from PyQt6.QtWidgets import QGraphicsScene
    from krok_helper.subtitle_render.frontend.preview.roll_preview import RollPreviewController

    path = tmp_path / "post.png"
    image = QImage(32, 32, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.red)
    assert image.save(str(path))
    config = RollSegmentConfig(
        enabled=True,
        duration_ms=2000,
        main_transition_ms=1000,
        main_transition_style=ROLL_MAIN_TRANSITION_CROSSFADE,
        source=BackgroundSource(kind="image", path=str(path)),
    )
    plan = build_roll_timeline(
        None, config, track_end_ms=20000,
        main_media_end_ms=10000, background_kind="video",
    )
    scene = QGraphicsScene()
    controller = RollPreviewController(scene)
    try:
        controller.set_plan(plan)
        # 主视频结束前 500ms = 交叉溶解中点：此时后黑 alpha 0.5，必须已显示。
        controller.set_time(9500, False)
        assert controller.source.post is not None
        assert controller._post_item.isVisible()
        assert abs(controller._post_item.opacity() - 0.5) < 0.01
        # 纯色段同一路径：透明度真正落到 rect（不是固定 1.0）。
        solid = RollSegmentConfig(
            enabled=True,
            duration_ms=2000,
            self_transition_ms=1000,
            source=BackgroundSource(kind="solid", color="#123456"),
        )
        solid_plan = build_roll_timeline(
            None, solid, track_end_ms=20000,
            main_media_end_ms=10000, background_kind="video",
        )
        controller.set_plan(solid_plan)
        controller.set_time(11500, False)  # 后黑自我淡出中点
        assert controller._post_rect.isVisible()
        assert abs(controller._post_rect.opacity() - 0.5) < 0.01
    finally:
        controller.shutdown()


@pytest.mark.parametrize("suffix", ["gif", "webp"])
def test_review_animation_seek_uses_project_time(qapp, tmp_path, suffix):
    from PIL import Image
    from krok_helper.subtitle_render.frontend.preview.roll_preview import RollSegmentFrameSource
    path = tmp_path / ("animation." + suffix)
    Image.new("RGB", (8, 8), "red").save(path, save_all=True,
        append_images=[Image.new("RGB", (8, 8), "blue")], duration=[100, 200], loop=0)
    source = RollSegmentFrameSource()
    config = RollSegmentConfig(enabled=True, duration_ms=2000,
        source=BackgroundSource(kind="image", path=str(path)))
    source.set_plan(build_roll_timeline(config, None, track_end_ms=1000))
    try:
        for t, expected in ((150, "blue"), (20, "red"), (450, "blue")):
            source.set_time(t, False)
            pixmap = source.layer_pixmap(source.pre)
            color = pixmap.toImage().pixelColor(960, 540)
            assert (color.blue() > color.red()) == (expected == "blue")
    finally:
        source.shutdown()


def test_review_animation_export_prepares_clocked_frames(qapp, tmp_path, monkeypatch):
    from PIL import Image
    import importlib
    renderer = importlib.import_module("krok_helper.subtitle_render.engine.renderer")
    from tests.test_subtitle_render_roll_export import _job
    path = tmp_path / "animation.webp"
    Image.new("RGB", (8, 8), "red").save(path, save_all=True,
        append_images=[Image.new("RGB", (8, 8), "blue")], duration=[100, 200], loop=0)
    config = RollSegmentConfig(enabled=True, duration_ms=1000,
        source=BackgroundSource(kind="image", path=str(path)))
    job = _job(tmp_path, pre=config)
    manifests = []
    def inspect(prepared, **kwargs):
        from pathlib import Path
        manifest = Path(prepared.pre_roll.source.path)
        text = manifest.read_text(encoding="utf-8")
        assert "file '0.png'" in text and "file '1.png'" in text
        assert text.count("duration ") == renderer._frame_count(1000, job.fps)
        manifests.append(manifest)
        return prepared.output_path
    monkeypatch.setattr(renderer, "_render_subtitle_video_prepared", inspect)
    renderer.render_subtitle_video(job)
    assert not manifests[0].exists()


def test_transport_legacy_player_position_does_not_jump_out_of_pre_roll(qapp):
    """回退播放器路径：前黑区主媒体停在 0，positionChanged 不得把播放头弹出头区。"""
    from krok_helper.subtitle_render.frontend.preview.preview_view import TransportBar

    bar = TransportBar()
    try:
        bar._has_audio = True  # noqa: SLF001 - 直接模拟旧路径的音频播放器就绪
        bar.set_roll_plan(type("P", (), {"pre_ms": 4000})())
        bar.set_duration(10_000)
        bar._slider.setValue(1_500)  # 用户在头区拖动/暂停
        bar._on_player_position(0)  # 主媒体被钳在 0 → 回报 0
        assert bar.current_time_ms == 1_500
        # 主段区仍按 display = 媒体位置 + 前黑偏移映射。
        bar._slider.setValue(7_000)
        bar._on_player_position(2_500)
        assert bar.current_time_ms == 6_500
    finally:
        bar.deleteLater()


def test_review_video_segment_uses_prepared_normal_source(qapp, tmp_path, monkeypatch):
    """段视频直接使用普通背景的预览源，不再有独立 CFR 转码流程。"""
    import importlib

    roll_preview = importlib.import_module(
        "krok_helper.subtitle_render.frontend.preview.roll_preview"
    )
    source_video = tmp_path / "src.mp4"
    source_video.write_bytes(b"fake")
    proxy = tmp_path / "proxy.mp4"
    proxy.write_bytes(b"fake-proxy")
    calls: list[tuple] = []

    def fake_prepared(path, quality="high"):
        calls.append((Path(path), str(quality)))
        return proxy

    monkeypatch.setattr(
        roll_preview, "prepared_qt_playback_source", fake_prepared
    )
    source = roll_preview.RollSegmentFrameSource()
    source.set_preview_quality("low")
    config = RollSegmentConfig(
        enabled=True,
        duration_ms=1000,
        source=BackgroundSource(kind="video", path=str(source_video)),
        source_has_audio=True,
    )
    source.set_plan(build_roll_timeline(config, None, track_end_ms=1000))
    try:
        assert calls and calls[0][1] == "low"
        segment = source.pre
        assert segment is not None
        assert segment.video_playback_path == proxy
        assert Path(segment.video_player.source().toLocalFile()) == proxy
        assert segment.audio_player is not None
        assert Path(segment.audio_player.source().toLocalFile()) == proxy
    finally:
        source.shutdown()


def test_review_video_segment_prepares_normal_source_async(qapp, tmp_path, monkeypatch):
    """缓存缺失时先用源文件，后台走普通背景准备命令，完成后切代理。"""
    import importlib

    roll_preview = importlib.import_module(
        "krok_helper.subtitle_render.frontend.preview.roll_preview"
    )
    source_video = tmp_path / "src.mp4"
    source_video.write_bytes(b"fake")
    proxy = tmp_path / "proxy.mp4"
    proxy.write_bytes(b"fake-proxy")
    calls: list[tuple] = []

    def fake_prepared(*args, **kwargs):
        return None

    def fake_prepare(path, quality="high"):
        calls.append((Path(path), str(quality)))
        return proxy

    monkeypatch.setattr(
        roll_preview, "prepared_qt_playback_source", fake_prepared
    )
    monkeypatch.setattr(roll_preview, "qt_playback_source", fake_prepare)
    source = roll_preview.RollSegmentFrameSource()
    source.set_preview_quality("high")
    config = RollSegmentConfig(
        enabled=True,
        duration_ms=1000,
        source=BackgroundSource(kind="video", path=str(source_video)),
    )
    source.set_plan(build_roll_timeline(config, None, track_end_ms=1000))
    try:
        segment = source.pre
        assert segment is not None
        # 缓存缺失时播放器不初始化，也不在 GUI 线程转封装。
        assert segment.video_playback_path is None
        assert segment.video_player is None
        assert _wait_until(
            qapp,
            lambda: segment.video_playback_path == proxy
            and segment.video_player is not None
            and Path(segment.video_player.source().toLocalFile()) == proxy,
        )
        assert calls == [(source_video, "high")]
    finally:
        source.shutdown()


def test_review_param_changes_reuse_segment_without_preparation(
    qapp, tmp_path, monkeypatch
):
    """改时长/填充/转场只更新运行态，不重新准备素材或重建播放器。"""
    import importlib

    from dataclasses import replace

    roll_preview = importlib.import_module(
        "krok_helper.subtitle_render.frontend.preview.roll_preview"
    )
    video = tmp_path / "src.mp4"
    video.write_bytes(b"fake")
    proxy = tmp_path / "proxy.mp4"
    proxy.write_bytes(b"fake-proxy")
    calls: list[Path] = []
    monkeypatch.setattr(
        roll_preview,
        "prepared_qt_playback_source",
        lambda path, quality="high": proxy,
    )
    monkeypatch.setattr(
        roll_preview,
        "qt_playback_source",
        lambda path, quality="high": calls.append(Path(path)) or proxy,
    )
    source = roll_preview.RollSegmentFrameSource()
    config = RollSegmentConfig(
        enabled=True,
        duration_ms=1000,
        source=BackgroundSource(kind="video", path=str(video)),
        source_has_audio=True,
    )
    source.set_plan(build_roll_timeline(config, None, track_end_ms=2000))
    try:
        segment = source.pre
        assert segment is not None
        player = segment.video_player
        count = len(calls)
        changed = replace(
            config,
            duration_ms=1800,
            fill="stretch",
            self_transition_ms=500,
            self_transition_style="white",
        )
        source.set_plan(build_roll_timeline(changed, None, track_end_ms=2000))
        assert source.pre is segment
        assert segment.video_player is player
        assert segment.config.duration_ms == 1800
        assert segment.config.fill == "stretch"
        assert len(calls) == count
    finally:
        source.shutdown()


def test_main_window_param_change_does_not_prepare_media(
    qapp, tmp_path, monkeypatch
):
    """主窗口改时长/填充/转场：只刷新现有预览，不再请求素材准备。"""
    import importlib

    mw = importlib.import_module(
        "krok_helper.subtitle_render.frontend.main_window"
    )
    roll_preview = importlib.import_module(
        "krok_helper.subtitle_render.frontend.preview.roll_preview"
    )
    video = tmp_path / "intro.mp4"
    video.write_bytes(b"fake")
    proxy = tmp_path / "proxy.mp4"
    proxy.write_bytes(b"fake-proxy")
    calls: list[Path] = []
    monkeypatch.setattr(
        roll_preview,
        "prepared_qt_playback_source",
        lambda path, quality="high": proxy,
    )
    monkeypatch.setattr(
        roll_preview,
        "qt_playback_source",
        lambda path, quality="high": calls.append(Path(path)) or proxy,
    )
    win = mw.SubtitleRenderWindow(
        embedded=True, settings_provider=_DictSettingsProvider()
    )
    try:
        win._on_roll_config_changed(
            "pre", {"enabled": True, "source_kind": "video"}
        )
        win._apply_roll_source(
            "pre",
            BackgroundSource(kind="video", path=str(video)),
            source_duration_ms=1000,
            source_has_audio=False,
            source_width=64,
            source_height=64,
        )
        assert _wait_until(
            qapp,
            lambda: win._current_roll_plan().active
            and bool(calls) is False,
        )
        count = len(calls)
        win._on_roll_config_changed(
            "pre",
            {
                "duration_ms": 5000,
                "fill": "stretch",
                "main_transition_ms": 0,
            },
        )
        assert _wait_until(
            qapp,
            lambda: win._pre_roll is not None
            and win._pre_roll.duration_ms == 5000
            and win._pre_roll.fill == "stretch",
        )
        assert len(calls) == count
    finally:
        win.deleteLater()
        qapp.processEvents()


def test_project_fps_change_does_not_prepare_roll_media(qapp, tmp_path, monkeypatch):
    """改项目帧率只刷新预览状态，不再触发段视频重建/转码。"""
    import importlib

    mw = importlib.import_module(
        "krok_helper.subtitle_render.frontend.main_window"
    )
    roll_preview = importlib.import_module(
        "krok_helper.subtitle_render.frontend.preview.roll_preview"
    )
    calls: list[tuple] = []

    def fake_prepare(path, quality="high"):
        calls.append((Path(path), str(quality)))
        return Path(path)

    video = tmp_path / "intro.mp4"
    video.write_bytes(b"fake")
    monkeypatch.setattr(roll_preview, "qt_playback_source", fake_prepare)
    monkeypatch.setattr(
        roll_preview,
        "prepared_qt_playback_source",
        lambda path, quality="high": video,
    )
    win = mw.SubtitleRenderWindow(
        embedded=True, settings_provider=_DictSettingsProvider()
    )
    try:
        win._on_roll_config_changed(
            "pre", {"enabled": True, "source_kind": "video"}
        )
        win._apply_roll_source(
            "pre",
            BackgroundSource(kind="video", path=str(video)),
            source_duration_ms=1000,
            source_has_audio=False,
            source_width=64,
            source_height=64,
        )
        assert _wait_until(qapp, lambda: win._current_roll_plan().active)
        count_before = len(calls)
        win._export_fps_combo.setCurrentIndex(win._export_fps_combo.findData(120))
        win._export_fps_combo.setCurrentIndex(win._export_fps_combo.findData(60))
        assert _wait_until(qapp, lambda: win._screen_settings.fps == 60)
        assert len(calls) == count_before
    finally:
        win.deleteLater()
        qapp.processEvents()


def test_review_segment_player_seeks_only_on_start_or_large_drift(qapp):
    """播放中不逐 tick seek（否则 Qt 位置回报粒度会造成一卡一卡）。"""
    from PyQt6.QtMultimedia import QMediaPlayer

    from krok_helper.subtitle_render.frontend.preview.roll_preview import (
        RollSegmentFrameSource,
    )

    class _FakePlayer:
        def __init__(self):
            self._state = QMediaPlayer.PlaybackState.PlayingState
            self._position = 0
            self.seeks: list[int] = []
            self.plays = 0
            self.pauses = 0

        def playbackState(self):
            return self._state

        def position(self):
            return self._position

        def setPosition(self, value):
            self.seeks.append(int(value))
            self._position = int(value)

        def play(self):
            self.plays += 1
            self._state = QMediaPlayer.PlaybackState.PlayingState

        def pause(self):
            self.pauses += 1
            self._state = QMediaPlayer.PlaybackState.PausedState

    source = RollSegmentFrameSource()
    player = _FakePlayer()
    # 播放中 300ms 偏差属正常回报粒度 → 不 seek。
    source._sync_player(player, 300, True)
    assert player.seeks == []
    # 大偏差（拖回/卡顿）→ 重新定位。
    source._sync_player(player, 1200, True)
    assert player.seeks == [1200]
    # 起播（暂停/停止态）必须精确对齐目标。
    player._state = QMediaPlayer.PlaybackState.PausedState
    player._position = 0
    source._sync_player(player, 450, True)
    assert player.seeks == [1200, 450]
    assert player.plays == 1
    # 暂停态 seek 到目标并暂停。
    source._sync_player(player, 800, False)
    assert player.seeks[-1] == 800
    assert player.pauses == 1


def test_review_main_video_stays_visible_until_main_end(qapp):
    """前黑开启时不能用 display 时刻直接和媒体时长比，否则主视频提前 pre_ms 消失。"""
    from krok_helper.subtitle_render.frontend.preview.preview_graphics import (
        PreviewGraphicsView,
    )

    class _FakePlayer:
        def duration(self):
            return 10_000

    view = PreviewGraphicsView()
    try:
        view._roll_plan = build_roll_timeline(
            _video_config(4000), None, track_end_ms=10_000,
            main_media_end_ms=10_000, background_kind="video",
        )
        view._external_player = _FakePlayer()
        view._background_source = BackgroundSource(kind="video", path="D:/main.mp4")
        # display 13_999 = 媒体 9_999ms（尚未结束）→ 可见。
        view._t_ms = 13_999
        view._sync_video_position()
        assert view._video_item.isVisible()
        # display 14_000 = 媒体 10_000ms（主段结束）→ 隐藏。
        view._t_ms = 14_000
        view._sync_video_position()
        assert not view._video_item.isVisible()
    finally:
        view.deleteLater()
        qapp.processEvents()


def test_layer_alphas_pre_dip_does_not_overlap_post_crossfade():
    """前黑 dip 淡入与后黑 crossfade 窗口不得重叠（预览/导出同钳制）。"""
    pre = RollSegmentConfig(
        enabled=True,
        duration_ms=2000,
        main_transition_ms=1000,
        main_transition_style=ROLL_MAIN_TRANSITION_BLACK,
        source=BackgroundSource(kind="solid"),
    )
    post = RollSegmentConfig(
        enabled=True,
        duration_ms=2000,
        main_transition_ms=1000,
        main_transition_style=ROLL_MAIN_TRANSITION_CROSSFADE,
        source=BackgroundSource(kind="solid"),
    )
    plan = build_roll_timeline(
        pre, post, track_end_ms=0,
        main_media_end_ms=1000, background_kind="video",
    )
    # 主段开头 [2000,2500] 由前黑 dip 淡入；后黑 crossfade 被钳到 [2500,3000]。
    at_pre_fade = roll_layer_alphas(plan, 2200)
    assert at_pre_fade.main_alpha > 0
    assert at_pre_fade.post_alpha == 0
    after = roll_layer_alphas(plan, 2600)
    assert after.main_alpha == 1.0
    assert after.post_alpha > 0


def test_main_window_export_includes_roll_segment_audio(qapp, tmp_path):
    """主工程无音频时，前黑/后黑独立音频也必须让导出带音轨。"""
    import importlib

    mw = importlib.import_module(
        "krok_helper.subtitle_render.frontend.main_window"
    )
    audio = tmp_path / "intro.wav"
    audio.write_bytes(b"fake")
    win = mw.SubtitleRenderWindow(
        embedded=True, settings_provider=_DictSettingsProvider()
    )
    try:
        assert not win._export_includes_audio()
        win._pre_roll = RollSegmentConfig(
            enabled=True,
            duration_ms=1000,
            source=BackgroundSource(kind="solid"),
            audio_path=str(audio),
        )
        assert win._export_includes_audio()
        win._pre_roll = RollSegmentConfig(
            enabled=True,
            duration_ms=1000,
            source=BackgroundSource(kind="video", path=str(audio)),
            source_has_audio=False,
        )
        assert not win._export_includes_audio()
    finally:
        win.deleteLater()
        qapp.processEvents()


def test_paint_roll_fill_tile_uses_source_pixel_size(qapp):
    """平铺必须按素材像素尺寸算格子，低画质代理缩水不影响几何。"""
    from PyQt6.QtGui import QColor, QPixmap

    from krok_helper.subtitle_render.frontend.preview.roll_preview import (
        paint_roll_fill,
    )

    canvas = QPixmap(8, 8)
    canvas.fill(QColor(0, 0, 0))
    source = QPixmap(1, 1)  # 模拟被缩到 1x1 的低画质代理
    source.fill(QColor(255, 0, 0))
    paint_roll_fill(canvas, source, "tile", source_width=2, source_height=2)
    image = canvas.toImage()
    # 每个原始 2x2 格子铺一次；(2,0) 处在第二个格子内，必须是红色。
    assert image.pixelColor(0, 0).red() > 200
    assert image.pixelColor(2, 0).red() > 200
    assert image.pixelColor(0, 2).red() > 200


def test_roll_enable_uses_three_second_solid_default(qapp):
    """直接打开总开关即得 3s 纯色段（默认 3s + 300ms 过渡动画）。"""
    import importlib

    mw = importlib.import_module(
        "krok_helper.subtitle_render.frontend.main_window"
    )
    win = mw.SubtitleRenderWindow(
        embedded=True, settings_provider=_DictSettingsProvider()
    )
    try:
        win._on_roll_config_changed("pre", {"enabled": True})
        assert _wait_until(
            qapp,
            lambda: win._pre_roll is not None
            and win._pre_roll.duration_ms == 3000
            and win._current_roll_plan().pre_ms == 3000,
        )
        config = win._pre_roll
        assert config.source.kind == "solid"
        assert config.main_transition_ms == 300
        assert config.self_transition_ms == 300
    finally:
        win.deleteLater()
        qapp.processEvents()


def test_apply_roll_source_keeps_user_duration_and_records_source_duration(qapp):
    import importlib

    mw = importlib.import_module(
        "krok_helper.subtitle_render.frontend.main_window"
    )
    win = mw.SubtitleRenderWindow(
        embedded=True, settings_provider=_DictSettingsProvider()
    )
    try:
        win._pre_roll = RollSegmentConfig(
            enabled=True,
            duration_ms=3000,
            source=BackgroundSource(kind="solid"),
        )
        win._apply_roll_source(
            "pre",
            BackgroundSource(kind="image", path="D:/img/bg.png"),
            source_duration_ms=1500,
            source_has_audio=False,
            source_width=100,
            source_height=50,
        )
        assert win._pre_roll.duration_ms == 3000  # 不因加载素材自动改写
        assert win._pre_roll.source_duration_ms == 1500
    finally:
        win.deleteLater()
        qapp.processEvents()


def test_roll_browse_video_keeps_duration_and_shows_source_hint(
    qapp, tmp_path, monkeypatch
):
    """加载视频在后台探测；只记录素材时长提示，不改用户持续时长。"""
    import importlib

    from krok_helper.models import MediaInfo
    import krok_helper.subtitle_render.frontend.workflow.background_tasks as tasks

    mw = importlib.import_module(
        "krok_helper.subtitle_render.frontend.main_window"
    )
    video = tmp_path / "intro.mp4"
    video.write_bytes(b"fake")
    win = mw.SubtitleRenderWindow(
        embedded=True, settings_provider=_DictSettingsProvider()
    )
    try:
        win._pre_roll = RollSegmentConfig(
            enabled=True,
            duration_ms=3000,
            source=BackgroundSource(kind="solid"),
        )
        monkeypatch.setattr(
            mw.QFileDialog,
            "getOpenFileName",
            lambda *args, **kwargs: (str(video), ""),
        )
        monkeypatch.setattr(
            tasks,
            "probe_media",
            lambda ffprobe, path: MediaInfo(
                path=path,
                duration=2.5,
                video_streams=1,
                audio_streams=0,
                subtitle_streams=0,
                video_width=640,
                video_height=360,
                video_fps=30.0,
            ),
        )
        monkeypatch.setattr(
            tasks, "qt_playback_source", lambda *args, **kwargs: video
        )
        win._on_roll_browse("pre", "video")
        assert _wait_until(
            qapp,
            lambda: win._pre_roll is not None
            and win._pre_roll.source.kind == "video"
            and win._pre_roll.source_duration_ms == 2500,
        )
        assert win._pre_roll.duration_ms == 3000
        assert _wait_until(
            qapp,
            lambda: win._property_panel._roll_duration_hints["pre"]["video"].text()
            == "【素材时长： 00:00:02.500（2.500秒）】",
        )
        hint = win._property_panel._roll_duration_hints["pre"]["video"]
        assert hint.toolTip() == "【素材时长： 00:00:02.500（2.500秒）】"
    finally:
        win.deleteLater()
        qapp.processEvents()


def test_roll_defaults_app_preference_round_trip(qapp):
    """app 级默认值：改一次后新建窗口/新段仍沿用，且随 settings 保存。"""
    import importlib

    from krok_helper.subtitle_render.domain.roll import (
        roll_config_with_defaults,
        roll_defaults_from_dict,
        roll_defaults_to_dict,
    )

    custom = roll_defaults_from_dict(
        {
            "duration_ms": 4500,
            "fill": "tile",
            "main_transition_ms": 600,
            "main_transition_style": "crossfade",
            "self_transition_ms": 250,
            "self_transition_style": "white",
            "auto_offset_subtitles": False,
        }
    )
    assert roll_defaults_to_dict(roll_config_with_defaults(custom)) == custom
    provider = _DictSettingsProvider({"roll_defaults": custom})
    mw = importlib.import_module(
        "krok_helper.subtitle_render.frontend.main_window"
    )
    win = mw.SubtitleRenderWindow(embedded=True, settings_provider=provider)
    try:
        assert win._roll_defaults == custom
        # 面板在没有工程段时显示 app 默认值。
        assert win._property_panel._roll_duration_spins["pre"].value() == 4.5
        assert win._property_panel._roll_fill_combos["pre"].currentData() == "tile"
        assert (
            win._property_panel._roll_main_transition_spins["pre"].value() == 600
        )
        # 新建段（尚未启用/未配置）拿到同一份默认值。
        config = win._roll_config_for("pre")
        assert config.duration_ms == 4500
        assert config.main_transition_style == "crossfade"
        assert config.auto_offset_subtitles is False
        # 保存后写回 app 设置。
        win._roll_defaults = roll_defaults_from_dict(
            {**custom, "duration_ms": 8000}
        )
        win._save_persisted_state()
        assert provider.data["roll_defaults"]["duration_ms"] == 8000
    finally:
        win.deleteLater()
        qapp.processEvents()


def test_roll_media_worker_reuses_normal_preview_preparation(
    qapp, tmp_path, monkeypatch
):
    """段视频后台准备与普通背景同口径：同一个 qt_playback_source，无帧率参数。"""
    from krok_helper.models import MediaInfo
    from krok_helper.subtitle_render.frontend.workflow import background_tasks as tasks

    video = tmp_path / "intro.mp4"
    video.write_bytes(b"fake")
    calls: list[tuple[Path, str]] = []
    monkeypatch.setattr(
        tasks,
        "probe_media",
        lambda ffprobe, path: MediaInfo(
            path=path,
            duration=2.5,
            video_streams=1,
            audio_streams=1,
            subtitle_streams=0,
            video_width=640,
            video_height=360,
            video_fps=30.0,
        ),
    )
    monkeypatch.setattr(
        tasks,
        "qt_playback_source",
        lambda path, quality="high", **kwargs: calls.append((Path(path), str(quality)))
        or Path(path),
    )
    worker = tasks._RollMediaLoadWorker(
        1,
        "pre",
        "video",
        video,
        ffprobe_path="ffprobe",
        preview_fps=120,
        preview_quality="high",
    )
    result = tasks.RollMediaLoadResult(1, "pre", "video")
    stages: list[str] = []
    metadata: list[object] = []
    worker.stageChanged.connect(stages.append)
    worker.metadataReady.connect(metadata.append)
    worker._load_video(result)
    assert result.error == ""
    assert len(metadata) == 1
    assert metadata[0].source.kind == "video"
    assert calls == [(video, "high")]
    assert stages == ["读取素材信息…", "准备预览…"]


def test_roll_media_load_worker_reads_sequence_and_static_image(qapp, tmp_path):
    """后台素材 worker：图片序列帧数/尺寸与静态图尺寸在非 GUI 线程也能算。"""
    from PyQt6.QtGui import QColor, QImage

    from krok_helper.subtitle_render.frontend.workflow.background_tasks import (
        RollMediaLoadResult,
        _RollMediaLoadWorker,
    )

    for index in range(1, 4):
        image = QImage(32, 24, QImage.Format.Format_RGB32)
        image.fill(QColor("#305070"))
        assert image.save(str(tmp_path / f"frame_{index:04d}.png"))
    worker = _RollMediaLoadWorker(
        7,
        "pre",
        "image_sequence",
        tmp_path / "frame_0001.png",
        ffprobe_path="ffprobe",
        preview_fps=30,
        preview_quality="high",
    )
    result = RollMediaLoadResult(7, "pre", "image_sequence")
    worker._load_image_sequence(result)
    assert result.error == ""
    assert result.source.kind == "image_sequence"
    assert result.source.path.endswith("frame_%04d.png")
    assert result.source_width == 32
    assert result.source_height == 24
    assert result.source_duration_ms == 100  # 3 帧 / 30fps
    assert result.preview_frame is not None
    assert result.preview_frame_path is not None
    assert result.preview_frame_path.name == "frame_0001.png"

    static_worker = _RollMediaLoadWorker(
        8,
        "post",
        "image",
        tmp_path / "frame_0001.png",
        ffprobe_path="ffprobe",
        preview_fps=60,
        preview_quality="high",
    )
    static_result = RollMediaLoadResult(8, "post", "image")
    static_worker._load_image(static_result)
    assert static_result.error == ""
    assert static_result.source.kind == "image"
    assert static_result.source_width == 32
    assert static_result.source_height == 24
    assert static_result.source_duration_ms == 0
    assert static_result.preview_frame is not None
    assert static_result.preview_frame_path == tmp_path / "frame_0001.png"


def test_open_roll_defaults_applies_and_persists(qapp, monkeypatch):
    """菜单「前黑/后黑默认值…」保存后立即成为新段默认并写盘。"""
    import importlib

    mw = importlib.import_module(
        "krok_helper.subtitle_render.frontend.main_window"
    )
    provider = _DictSettingsProvider()
    win = mw.SubtitleRenderWindow(embedded=True, settings_provider=provider)
    selected = {
        "duration_ms": 6500,
        "fill": "tile",
        "main_transition_style": "black",
        "main_transition_ms": 450,
        "self_transition_style": "white",
        "self_transition_ms": 150,
        "auto_offset_subtitles": True,
    }

    class _FakeDialog:
        def __init__(self, *args, **kwargs):
            pass

        def exec(self):
            return mw.QDialog.DialogCode.Accepted

        def selection(self):
            return dict(selected)

    monkeypatch.setattr(mw, "_RollDefaultsDialog", _FakeDialog)
    monkeypatch.setattr(mw.InfoBar, "success", lambda *args, **kwargs: None)
    try:
        win._open_roll_defaults()
        assert win._roll_defaults == selected
        assert win._roll_config_for("pre").duration_ms == 6500
        assert win._property_panel._roll_duration_spins["pre"].value() == 6.5
        assert provider.data["roll_defaults"]["duration_ms"] == 6500
    finally:
        win.deleteLater()
        qapp.processEvents()


def test_main_window_title_duration_uses_roll_content_end(qapp):
    """标题自定义窗口默认结束时间跟随后黑之前的 content_end（不含后黑尾巴）。"""
    import importlib

    mw = importlib.import_module(
        "krok_helper.subtitle_render.frontend.main_window"
    )
    win = mw.SubtitleRenderWindow(
        embedded=True, settings_provider=_DictSettingsProvider()
    )
    try:
        win._pre_roll = RollSegmentConfig(
            enabled=True,
            duration_ms=1000,
            source=BackgroundSource(kind="solid"),
        )
        win._post_roll = RollSegmentConfig(
            enabled=True,
            duration_ms=2000,
            source=BackgroundSource(kind="solid"),
        )
        plan = win._current_roll_plan()
        assert plan.total_ms == 3000
        assert plan.content_end_ms == 1000
        assert win._title_content_duration_ms() == 1000
    finally:
        win.deleteLater()
        qapp.processEvents()


def test_review_preview_volume_reaches_segment_audio(qapp):
    """预览音量滑块必须同步到段音频（段音频走独立 QMediaPlayer）。"""
    import importlib

    mw = importlib.import_module(
        "krok_helper.subtitle_render.frontend.main_window"
    )
    win = mw.SubtitleRenderWindow(
        embedded=True, settings_provider=_DictSettingsProvider()
    )
    try:
        win._transport_bar._volume_slider.setValue(42)
        canvas = win._preview_panel.canvas
        controller = getattr(canvas, "_roll_preview", None)
        source = (
            controller.source
            if controller is not None
            else getattr(canvas, "_roll_source", None)
        )
        assert source is not None
        assert abs(float(source._volume) - 0.42) < 1e-6
    finally:
        win.deleteLater()
        qapp.processEvents()


def test_review_loading_state_has_persistent_progress(qapp, monkeypatch, tmp_path):
    from krok_helper.subtitle_render.frontend import main_window as mw
    from PyQt6.QtCore import QThread
    win = mw.SubtitleRenderWindow(embedded=True, settings_provider=_DictSettingsProvider())
    try:
        # Keep the real GUI flow pending without running external probes.
        monkeypatch.setattr(QThread, "start", lambda self: None)
        win._start_roll_media_load("pre", "video", tmp_path / "pre.mp4")
        first = win._roll_media_generations["pre"]
        tooltip = win._roll_media_tooltips["pre"]
        assert not tooltip.isHidden()
        assert not tooltip.busyBar.isHidden()
        win._start_roll_media_load("post", "video", tmp_path / "post.mp4")
        win._on_roll_media_progress(first, "pre", 42)
        assert tooltip.progressBar.value() == 42
        assert not tooltip.progressBar.isHidden()
        assert tooltip.busyBar.isHidden()
        assert "42%" in tooltip.contentLabel.text()
    finally:
        win.shutdown_for_host_exit()
        win.deleteLater()
        qapp.processEvents()


def test_review_roll_render_surface_matches_viewport(qapp, tmp_path):
    from PyQt6.QtGui import QImage, QColor
    from krok_helper.subtitle_render.frontend.preview.roll_preview import RollSegmentFrameSource
    path = tmp_path / "red.png"
    image = QImage(100, 100, QImage.Format.Format_RGB32)
    image.fill(QColor("red")); image.save(str(path))
    source = RollSegmentFrameSource()
    source.set_output_size(3840, 2160)
    source.set_render_scale(0.25)
    source.set_plan(build_roll_timeline(RollSegmentConfig(enabled=True, duration_ms=2000,
        source=BackgroundSource(kind="image", path=str(path))), None, track_end_ms=1000))
    pixmap = source.layer_pixmap(source.pre)
    assert (pixmap.width(), pixmap.height()) == (960, 540)
    assert pixmap.toImage().pixelColor(480, 270).red() == 255
    source.shutdown()


def test_real_roll_video_primes_paused_first_frame(qapp, tmp_path, monkeypatch):
    import subprocess
    from tests.test_subtitle_render_roll_export import _ffmpeg_path
    from krok_helper.subtitle_render.frontend.preview.roll_preview import RollSegmentFrameSource
    import krok_helper.subtitle_render.frontend.preview.roll_preview as module
    ffmpeg = _ffmpeg_path()
    if not ffmpeg:
        pytest.skip("需要 ffmpeg")
    path = tmp_path / "source.mp4"
    subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i",
        "color=red:size=64x64:rate=30:duration=2", "-f", "lavfi", "-i",
        "anullsrc=r=48000:cl=stereo", "-t", "2", "-c:v", "libx264", "-c:a", "aac", str(path)], check=True)
    monkeypatch.setattr(module, "prepared_qt_playback_source", lambda *args: path)
    source = RollSegmentFrameSource()
    source.set_output_size(64, 64)
    cfg = RollSegmentConfig(enabled=True, duration_ms=2000, source_has_audio=True,
        source=BackgroundSource(kind="video", path=str(path)))
    try:
        source.set_plan(build_roll_timeline(cfg, None, track_end_ms=1000))
        assert _wait_until(qapp, lambda: source.pre.video_frame is not None)
        assert source.pre.video_frame.pixelColor(32, 32).red() > 200
        assert source.pre.video_player is source.pre.audio_player
        assert not source.pre.priming_frame
        assert source.pre.video_player.playbackState().name != "PlayingState"
    finally:
        source.shutdown()
        qapp.processEvents()
