"""字幕渲染的导出默认文件名模板。

原先文件名写死为 ``{素材名}_yurika出力``，用户无法改。现在做成可配置模板，
默认值渲染结果与改造前完全一致。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from krok_helper.errors import ProcessingError
from krok_helper.pipeline import render_name_template, validate_output_name_template
from krok_helper.subtitle_render.models import (
    DEFAULT_EXPORT_NAME_TEMPLATE,
    DEFAULT_OUTPUT_NAME_SUFFIX,
    EXPORT_NAME_TEMPLATE_FIELDS,
)

VALUES = {
    "source_name": "残酷な天使のテーゼ",
    "video_name": "残酷な天使のテーゼ_bg",
    "subtitle_name": "残酷な天使のテーゼ_timing",
}
ALLOWED = set(EXPORT_NAME_TEMPLATE_FIELDS)


class TestDefaultTemplate:
    def test_the_default_reproduces_the_old_hardcoded_name(self) -> None:
        """改造不能动老用户的输出习惯。"""
        rendered = render_name_template(DEFAULT_EXPORT_NAME_TEMPLATE, "导出文件名", VALUES)
        assert rendered == f"{VALUES['source_name']}{DEFAULT_OUTPUT_NAME_SUFFIX}"

    def test_every_documented_placeholder_actually_renders(self) -> None:
        """说明里列出的占位符必须都能用，否则文案在骗人。"""
        for name in EXPORT_NAME_TEMPLATE_FIELDS:
            assert render_name_template(f"{{{name}}}", "导出文件名", VALUES) == VALUES[name]


class TestCustomTemplates:
    @pytest.mark.parametrize(
        "template,expected",
        [
            ("{video_name}_KTV", "残酷な天使のテーゼ_bg_KTV"),
            ("{subtitle_name}", "残酷な天使のテーゼ_timing"),
            ("KTV_{source_name}_1080p", "KTV_残酷な天使のテーゼ_1080p"),
            ("固定名", "固定名"),
        ],
    )
    def test_it_renders(self, template: str, expected: str) -> None:
        assert render_name_template(template, "导出文件名", VALUES) == expected

    def test_a_missing_source_leaves_the_placeholder_empty(self) -> None:
        """还没载入视频时 {video_name} 应为空，而不是炸掉。"""
        values = dict(VALUES, video_name="")
        assert render_name_template("{video_name}KTV", "导出文件名", values) == "KTV"


class TestValidation:
    @pytest.mark.parametrize(
        "template", ["{bad_name}", "a/b", "a:b", "{video_name", ""]
    )
    def test_bad_templates_are_refused(self, template: str) -> None:
        with pytest.raises(ProcessingError):
            validate_output_name_template(template, "导出文件名", ALLOWED)

    def test_the_error_lists_the_supported_placeholders(self) -> None:
        with pytest.raises(ProcessingError) as excinfo:
            validate_output_name_template("{bad_name}", "导出文件名", ALLOWED)
        message = str(excinfo.value)
        for name in EXPORT_NAME_TEMPLATE_FIELDS:
            assert name in message

    def test_the_hires_vocabulary_is_unaffected(self) -> None:
        """共用同一个校验函数，别把 Hi-Res 的占位符词表带歪了。"""
        assert validate_output_name_template("{video_name}_off", "伴奏") == "{video_name}_off"
        with pytest.raises(ProcessingError):
            validate_output_name_template("{source_name}", "伴奏")


class TestDialog:
    @staticmethod
    def _dialog(template=DEFAULT_EXPORT_NAME_TEMPLATE):
        from krok_helper.subtitle_render.frontend.main_window import (
            _ExportLocationDialog,
        )

        return _ExportLocationDialog("source_video", "", Path("D:/out"), None, template)

    def test_a_bad_template_blocks_confirm_before_export(self) -> None:
        """模板写错要当场拦住，而不是等点了导出才报错。"""
        dialog = self._dialog()
        assert dialog.ok_button.isEnabled()
        dialog.name_template_edit.setText("{bad}")
        assert not dialog.ok_button.isEnabled()
        assert dialog.name_error_label.isVisibleTo(dialog)

    def test_an_empty_template_falls_back_to_the_default(self) -> None:
        dialog = self._dialog("{video_name}_KTV")
        dialog.name_template_edit.setText("")
        assert dialog.ok_button.isEnabled()
        assert dialog.name_template() == DEFAULT_EXPORT_NAME_TEMPLATE

    def test_it_round_trips_a_custom_template(self) -> None:
        dialog = self._dialog("{video_name}_KTV")
        assert dialog.name_template() == "{video_name}_KTV"
