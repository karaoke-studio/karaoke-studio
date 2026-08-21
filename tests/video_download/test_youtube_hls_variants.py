"""YouTube 2026-08 起在格式表里混入的 HLS 变体。

这些 ``m3u8_native`` 条目的 ``tbr`` 普遍比同画质的 DASH 条目高（实测 1080p
4684 vs 1859），却一个体积字段都没有。老的挑选逻辑只按 tbr 排，于是每一档
清晰度的代表都换成了这些无体积条目，界面上所有清晰度显示的都是同一个几 MB
的数字 —— 那其实是音频轨的体积被当成了整条的体积。
"""

from __future__ import annotations

from krok_helper.video_download.format_parser import FormatParser


AUDIO = {
    "format_id": "140",
    "ext": "m4a",
    "vcodec": "none",
    "acodec": "mp4a.40.2",
    "abr": 129.5,
    "tbr": 129.5,
    "filesize": 3_538_445,
}


def _dash_1080p() -> dict:
    return {
        "format_id": "137",
        "ext": "mp4",
        "width": 1920,
        "height": 1080,
        "vcodec": "avc1.640028",
        "acodec": "none",
        "protocol": "https",
        "tbr": 1859.024,
        "filesize": 50_774_366,
    }


def _hls_1080p() -> dict:
    return {
        "format_id": "270",
        "ext": "mp4",
        "width": 1920,
        "height": 1080,
        "vcodec": "avc1.640028",
        "acodec": "none",
        "protocol": "m3u8_native",
        "tbr": 4684.109,
        "filesize": None,
        "filesize_approx": None,
    }


def test_sizeless_hls_variant_does_not_take_over_the_resolution() -> None:
    options = FormatParser().parse_formats([AUDIO, _hls_1080p(), _dash_1080p()], duration=219)

    # 同一档里只会留一个代表，它同时就是「最佳质量」那一条。
    row = options[0]
    assert row.download_format == "137+140"
    assert row.filesize == 50_774_366 + 3_538_445


def test_merged_size_is_unknown_instead_of_the_audio_only_size() -> None:
    """视频轨报不出体积、也估不出来时，整条应当是「未知」。

    以前写成 ``(video or 0) + (audio or 0)``，视频轨为 None 时报出去的是音频
    体积 —— 每档清晰度都显示同一个几 MB 的数字，用户以为解析坏了。
    """
    hls = _hls_1080p()
    hls.pop("tbr")

    options = FormatParser().parse_formats([AUDIO, hls], duration=219)

    row = options[0]
    assert row.filesize is None


def test_size_is_estimated_from_bitrate_when_the_format_has_none() -> None:
    """HLS 条目有 tbr，按码率 × 时长估一个，好过整行显示「-」。"""
    options = FormatParser().parse_formats([AUDIO, _hls_1080p()], duration=219)

    row = options[0]
    estimated = int(4684.109 * 1000 / 8 * 219)
    assert row.filesize == estimated + 3_538_445


def test_estimation_never_outranks_a_real_size() -> None:
    """估算值只用于显示；排序仍按「有没有真实体积」，否则 HLS 又会顶回代表位。"""
    options = FormatParser().parse_formats([AUDIO, _hls_1080p(), _dash_1080p()], duration=219)

    recommended = options[0]
    assert recommended.is_recommended
    assert recommended.download_format == "137+140"


def test_without_duration_there_is_no_estimate() -> None:
    options = FormatParser().parse_formats([AUDIO, _hls_1080p()])

    row = options[0]
    assert row.filesize is None
