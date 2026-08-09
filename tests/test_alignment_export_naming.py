"""对齐导出的命名模板 / 输出目录解析。

这些逻辑原本长在 ``KrokHelperQtApp`` 上，只能连着整个主窗口一起测；搬成
纯函数后直接测。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from krok_helper.alignment.export_naming import (
    render_output_path,
    resolve_output_dir,
    validate_name_template,
)
from krok_helper.errors import ProcessingError

VIDEO_FIELDS = {"video_name"}
AUDIO_FIELDS = {"audio_name", "video_name"}


class TestValidateNameTemplate:
    def test_strips_a_trailing_extension(self) -> None:
        assert validate_name_template(
            "{video_name}_对齐.mp4", "对齐后视频", allowed_fields=VIDEO_FIELDS, extensions=(".mp4", ".mkv")
        ) == "{video_name}_对齐"

    def test_keeps_a_template_without_extension(self) -> None:
        assert validate_name_template(
            "  {video_name}_对齐  ", "对齐后视频", allowed_fields=VIDEO_FIELDS, extensions=(".mp4",)
        ) == "{video_name}_对齐"

    def test_rejects_an_empty_template(self) -> None:
        with pytest.raises(ProcessingError, match="不能为空"):
            validate_name_template(".mp4", "对齐后视频", allowed_fields=VIDEO_FIELDS, extensions=(".mp4",))

    @pytest.mark.parametrize("bad", ["a/b", "a:b", "a?b", 'a"b', "a|b", "a*b", "a<b", "a>b", "a\\b"])
    def test_rejects_every_windows_invalid_char(self, bad: str) -> None:
        """挡的是 Windows 全套非法字符，不只是路径分隔符。"""
        with pytest.raises(ProcessingError, match="非法字符"):
            validate_name_template(bad, "对齐后视频", allowed_fields=VIDEO_FIELDS, extensions=())

    def test_unbalanced_braces_become_a_processing_error(self) -> None:
        """裸的 ValueError 会从只捕获 ProcessingError 的调用处逃出去，导致闪退。"""
        with pytest.raises(ProcessingError, match="大括号不配对"):
            validate_name_template("{video_name", "对齐后视频", allowed_fields=VIDEO_FIELDS, extensions=())

    def test_rejects_an_unknown_placeholder(self) -> None:
        with pytest.raises(ProcessingError, match="不支持的占位符"):
            validate_name_template(
                "{song_name}", "对齐后视频", allowed_fields=VIDEO_FIELDS, extensions=()
            )

    def test_audio_template_may_use_the_video_name(self) -> None:
        assert validate_name_template(
            "{video_name}_{audio_name}", "对齐后音频", allowed_fields=AUDIO_FIELDS, extensions=(".wav",)
        ) == "{video_name}_{audio_name}"


class TestResolveOutputDir:
    def test_follows_the_source_video_when_no_custom_dir(self, tmp_path: Path) -> None:
        video = tmp_path / "sub" / "song.mkv"
        assert resolve_output_dir(video, custom_dir=None) == video.parent

    def test_uses_the_custom_dir(self, tmp_path: Path) -> None:
        assert resolve_output_dir(tmp_path / "song.mkv", custom_dir=str(tmp_path)) == tmp_path

    def test_rejects_a_blank_custom_dir(self, tmp_path: Path) -> None:
        with pytest.raises(ProcessingError, match="选择对齐导出的保存目录"):
            resolve_output_dir(tmp_path / "song.mkv", custom_dir="   ")

    def test_rejects_a_missing_custom_dir(self, tmp_path: Path) -> None:
        with pytest.raises(ProcessingError, match="保存目录无效"):
            resolve_output_dir(tmp_path / "song.mkv", custom_dir=str(tmp_path / "nope"))


class TestRenderOutputPath:
    def _render(self, template: str, tmp_path: Path) -> Path:
        return render_output_path(
            template=template,
            video_path=tmp_path / "MV原盘.mkv",
            audio_path=tmp_path / "原唱.flac",
            extension=".mp4",
            label="对齐后视频",
            output_dir=tmp_path,
        )

    def test_fills_both_placeholders(self, tmp_path: Path) -> None:
        assert self._render("{video_name}_{audio_name}", tmp_path).name == "MV原盘_原唱.mp4"

    def test_trims_trailing_dots_and_spaces(self, tmp_path: Path) -> None:
        """Windows 上以点或空格结尾的文件名会被悄悄改写，先自己削掉。"""
        assert self._render("{video_name} . ", tmp_path).name == "MV原盘.mp4"

    def test_rejects_a_name_that_renders_empty(self, tmp_path: Path) -> None:
        with pytest.raises(ProcessingError, match="文件名不能为空"):
            self._render("  ", tmp_path)

    def test_rejects_invalid_chars_produced_by_the_source_name(self, tmp_path: Path) -> None:
        with pytest.raises(ProcessingError, match="非法字符"):
            render_output_path(
                template="{video_name}",
                video_path=Path("a?b.mkv"),
                audio_path=tmp_path / "原唱.flac",
                extension=".mp4",
                label="对齐后视频",
                output_dir=tmp_path,
            )

    def test_lands_in_the_given_output_dir(self, tmp_path: Path) -> None:
        target = tmp_path / "out"
        target.mkdir()
        path = render_output_path(
            template="{video_name}",
            video_path=tmp_path / "song.mkv",
            audio_path=tmp_path / "vocal.flac",
            extension=".wav",
            label="对齐后音频",
            output_dir=target,
        )
        assert path == target / "song.wav"


class TestHostWrappers:
    """宿主上那三个包装方法。

    纯函数搬出去后，宿主只剩"读 self 上的几个字符串 → 转调纯函数"。这一层
    也要测：``resolve_output_dir`` 与 ``pipeline`` 里的同名函数撞过车，
    只测纯函数的话，撞车在单测里完全看不见。
    """

    def _host(self):
        from types import SimpleNamespace

        from krok_helper.alignment.page import AlignmentPage

        return SimpleNamespace(
            align_video_name_template_value="{video_name}_对齐.mp4",
            align_audio_name_template_value="{video_name}_{audio_name}.wav",
            align_output_dir_mode_value="source_video",
            align_output_custom_dir_text="",
            _validate_alignment_name_template=(
                lambda *a, **k: AlignmentPage._validate_alignment_name_template(None, *a, **k)
            ),
        ), AlignmentPage

    def test_renders_the_video_target_path(self, tmp_path: Path) -> None:
        host, cls = self._host()
        host._resolve_alignment_name_templates = lambda **k: cls._resolve_alignment_name_templates(host, **k)
        host._resolve_alignment_output_dir = lambda p: cls._resolve_alignment_output_dir(host, p)

        path = cls._render_alignment_output_path(
            host,
            video_path=tmp_path / "MV原盘.mkv",
            audio_path=tmp_path / "原唱.flac",
            is_video_target=True,
        )

        assert path == tmp_path / "MV原盘_对齐.mp4"

    def test_renders_the_audio_target_path(self, tmp_path: Path) -> None:
        host, cls = self._host()
        host._resolve_alignment_name_templates = lambda **k: cls._resolve_alignment_name_templates(host, **k)
        host._resolve_alignment_output_dir = lambda p: cls._resolve_alignment_output_dir(host, p)

        path = cls._render_alignment_output_path(
            host,
            video_path=tmp_path / "MV原盘.mkv",
            audio_path=tmp_path / "原唱.flac",
            is_video_target=False,
        )

        assert path == tmp_path / "MV原盘_原唱.wav"

    def test_custom_output_dir_mode_is_honoured(self, tmp_path: Path) -> None:
        from krok_helper.alignment.page import AlignmentPage
        from krok_helper.settings import ALIGN_OUTPUT_DIR_CUSTOM

        host, _ = self._host()
        host.align_output_dir_mode_value = ALIGN_OUTPUT_DIR_CUSTOM
        host.align_output_custom_dir_text = str(tmp_path)

        assert AlignmentPage._resolve_alignment_output_dir(host, Path("D:/elsewhere/a.mkv")) == tmp_path

    def test_blank_custom_dir_is_rejected(self) -> None:
        from krok_helper.alignment.page import AlignmentPage
        from krok_helper.settings import ALIGN_OUTPUT_DIR_CUSTOM

        host, _ = self._host()
        host.align_output_dir_mode_value = ALIGN_OUTPUT_DIR_CUSTOM

        with pytest.raises(ProcessingError, match="选择对齐导出的保存目录"):
            AlignmentPage._resolve_alignment_output_dir(host, Path("D:/elsewhere/a.mkv"))
