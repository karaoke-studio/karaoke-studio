"""全局设置「波形对齐」那一页的内容 —— 现在由对齐页自己提供。

搬之前，这两块面板是全局设置对话框搭的，值由它隔空读写对齐页的属性
（``_host.align_video_name_template_value``，甚至穿两层去摸 ``align_video_zone.path``）。
现在对话框只拿到一块 ``QWidget``，校验与写回都在这边。所以这份测试盯的是：
填了什么 → ``apply()`` → 对齐页上真的变了；不合法 → 抛 ``ProcessingError`` 且
**一个字段都不写**。
"""

from __future__ import annotations

import pytest
from PyQt6.QtWidgets import QApplication

from krok_helper.alignment.page import AlignmentPage
from krok_helper.alignment.settings_fragment import AlignmentSettingsFragment
from krok_helper.errors import ProcessingError
from krok_helper.settings import ALIGN_OUTPUT_DIR_CUSTOM, ALIGN_OUTPUT_DIR_SOURCE_VIDEO
from tests.page_fakes import alignment_host, media_files


@pytest.fixture
def page():
    QApplication.instance() or QApplication([])
    widget = AlignmentPage(host=alignment_host())
    yield widget
    widget.deleteLater()


def test_the_fragment_opens_with_the_pages_current_values(page) -> None:
    page.set_name_templates("{video_name}_v", "{audio_name}_a")

    fragment = page.build_settings_fragment()

    assert fragment._video_template_edit.text() == "{video_name}_v"
    assert fragment._audio_template_edit.text() == "{audio_name}_a"
    assert fragment._source_radio.isChecked()


def test_applying_writes_back_to_the_page(page, tmp_path) -> None:
    fragment = page.build_settings_fragment()
    fragment._video_template_edit.setText("{video_name}_对齐")
    fragment._audio_template_edit.setText("{audio_name}_对齐")
    fragment._custom_radio.setChecked(True)
    fragment._dir_edit.setText(str(tmp_path))

    fragment.apply()

    assert page.name_templates() == ("{video_name}_对齐", "{audio_name}_对齐")
    assert page.output_dir_settings() == (ALIGN_OUTPUT_DIR_CUSTOM, str(tmp_path))


def test_an_empty_template_falls_back_to_the_default(page) -> None:
    fragment = page.build_settings_fragment()
    fragment._video_template_edit.setText("   ")

    fragment.apply()

    assert page.name_templates()[0]


@pytest.mark.parametrize(
    "directory,reason",
    [("", "选了指定目录却没填"), ("D:/这个目录不存在_krok", "填了个不存在的目录")],
)
def test_a_bad_output_dir_is_refused(page, directory: str, reason: str) -> None:
    fragment = page.build_settings_fragment()
    fragment._custom_radio.setChecked(True)
    fragment._dir_edit.setText(directory)

    with pytest.raises(ProcessingError):
        fragment.apply()

    assert reason  # 参数名进报告，出错时看得出是哪一种


def test_nothing_is_written_when_the_output_dir_is_refused(page) -> None:
    """先校验完再写 —— 不能留下"模板改了、目录没改"的半截状态。"""
    page.set_name_templates("{video_name}_old", "{audio_name}_old")
    fragment = page.build_settings_fragment()
    fragment._video_template_edit.setText("{video_name}_new")
    fragment._custom_radio.setChecked(True)
    fragment._dir_edit.setText("D:/这个目录不存在_krok")

    with pytest.raises(ProcessingError):
        fragment.apply()

    assert page.name_templates() == ("{video_name}_old", "{audio_name}_old")


def test_a_bad_template_is_refused(page) -> None:
    fragment = page.build_settings_fragment()
    fragment._video_template_edit.setText("{audio_name}_x")  # 视频模板不认这个占位符

    with pytest.raises(ProcessingError):
        fragment.apply()


def test_the_browse_button_starts_from_the_current_video(page, tmp_path, monkeypatch) -> None:
    """没填过目录时从字幕视频所在目录起步 —— 以前这是对话框穿两层摸 drop card。"""
    from PyQt6.QtWidgets import QFileDialog

    video, _audio = media_files(tmp_path)
    page.set_align_video_path(video)
    fragment = page.build_settings_fragment()
    seen: list[str] = []
    monkeypatch.setattr(
        QFileDialog, "getExistingDirectory", staticmethod(lambda _p, _t, init: (seen.append(init), "")[1])
    )

    fragment._choose_output_dir()

    assert seen == [str(tmp_path)]
