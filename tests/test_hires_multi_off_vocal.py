"""Hi-Res 混流：多条伴奏 + 单侧缺省，以及音频分离的伴奏转交。

需求：伴奏可以放多条（每条各出一个视频）；只放人声或只放伴奏都能生成；两者都
缺省才拒绝。分离模块跑完后把伴奏交给 Hi-Res。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from krok_helper.errors import ProcessingError
from krok_helper.pipeline import (
    OUTPUT_NAME_MODE_FIXED,
    OUTPUT_NAME_MODE_TEMPLATE,
    OUTPUT_NAME_MODE_VIDEO_NAME,
    resolve_off_output_paths,
)


@pytest.fixture()
def video(tmp_path: Path) -> Path:
    return tmp_path / "残酷な天使のテーゼ.mkv"


class TestOffVocalOutputNaming:
    def test_a_single_accompaniment_keeps_the_old_name(self, tmp_path, video) -> None:
        """只放一条时命名必须一如既往，不能因为支持了多条就改掉用户的输出习惯。"""
        outputs = resolve_off_output_paths(
            video, tmp_path, OUTPUT_NAME_MODE_VIDEO_NAME, None, [tmp_path / "a.flac"]
        )
        assert [path.name for path in outputs] == ["残酷な天使のテーゼ_off.mkv"]

    def test_a_single_accompaniment_honours_a_custom_template(self, tmp_path, video) -> None:
        outputs = resolve_off_output_paths(
            video, tmp_path, OUTPUT_NAME_MODE_TEMPLATE, "{video_name}-伴奏", [tmp_path / "a.flac"]
        )
        assert [path.name for path in outputs] == ["残酷な天使のテーゼ-伴奏.mkv"]

    def test_several_accompaniments_are_named_after_the_audio(self, tmp_path, video) -> None:
        outputs = resolve_off_output_paths(
            video,
            tmp_path,
            OUTPUT_NAME_MODE_VIDEO_NAME,
            None,
            [tmp_path / "s_伴奏.flac", tmp_path / "s_和声伴奏.flac"],
        )
        assert [path.name for path in outputs] == [
            "残酷な天使のテーゼ_s_伴奏.mkv",
            "残酷な天使のテーゼ_s_和声伴奏.mkv",
        ]

    def test_same_named_files_from_different_folders_do_not_collide(self, tmp_path, video) -> None:
        """两个目录下都叫「伴奏.flac」时不能互相覆盖。"""
        outputs = resolve_off_output_paths(
            video,
            tmp_path,
            OUTPUT_NAME_MODE_VIDEO_NAME,
            None,
            [tmp_path / "a" / "伴奏.flac", tmp_path / "b" / "伴奏.flac"],
        )
        assert len({path.name for path in outputs}) == 2

    def test_fixed_mode_falls_back_to_names_when_there_are_several(self, tmp_path, video) -> None:
        """固定名模式只有一个名字可用，多条时只能改用文件名区分。"""
        single = resolve_off_output_paths(
            video, tmp_path, OUTPUT_NAME_MODE_FIXED, None, [tmp_path / "a.flac"]
        )
        assert [path.name for path in single] == ["off_vocal.mkv"]
        several = resolve_off_output_paths(
            video, tmp_path, OUTPUT_NAME_MODE_FIXED, None, [tmp_path / "a.flac", tmp_path / "b.flac"]
        )
        assert len({path.name for path in several}) == 2

    def test_no_accompaniment_yields_no_output(self, tmp_path, video) -> None:
        assert resolve_off_output_paths(video, tmp_path, OUTPUT_NAME_MODE_VIDEO_NAME, None, []) == []

    def test_an_illegal_name_is_reported(self, tmp_path, video) -> None:
        with pytest.raises(ProcessingError):
            resolve_off_output_paths(
                video,
                tmp_path,
                OUTPUT_NAME_MODE_VIDEO_NAME,
                None,
                [tmp_path / 'a?b.flac', tmp_path / "c.flac"],
            )


class TestRunPipelineArguments:
    def test_single_and_plural_forms_cannot_both_be_given(self, tmp_path) -> None:
        from krok_helper.pipeline import run_pipeline

        with pytest.raises(ProcessingError):
            run_pipeline(
                video_path=tmp_path / "v.mkv",
                on_vocal_path=None,
                off_vocal_path=tmp_path / "a.flac",
                off_vocal_paths=[tmp_path / "b.flac"],
            )

    def test_still_refuses_when_both_sides_are_missing(self, tmp_path) -> None:
        """人声和伴奏都缺省是唯一不能生成的情况。"""
        from krok_helper.pipeline import run_pipeline

        with pytest.raises(Exception) as excinfo:
            run_pipeline(video_path=tmp_path / "v.mkv", on_vocal_path=None, off_vocal_paths=[])
        # ffmpeg 不一定装了；只要求它不是「静默通过」。
        assert excinfo.value is not None


class TestMultiFileDropZone:
    """伴奏卡的多文件模式：序号翻页、追加、移除。"""

    @staticmethod
    def _zone(tmp_path, count=3):
        from krok_helper.gui_qt import HIRES_AUDIO_EXTENSIONS, DropZoneCard

        files = []
        for name in ("s_伴奏.flac", "s_和声伴奏.flac", "demo.wav")[:count]:
            item = tmp_path / name
            item.write_bytes(b"x")
            files.append(item)
        zone = DropZoneCard(
            title="伴奏音频", hint="原始提示", extensions=HIRES_AUDIO_EXTENSIONS, multiple=True
        )
        zone.resize(320, 190)
        return zone, files

    def test_paths_accumulate_instead_of_replacing(self, tmp_path) -> None:
        """再拖一条进来不该顶掉已有的——那正是「多个伴奏」的意义。"""
        zone, files = self._zone(tmp_path)
        zone.add_paths(files[:1])
        zone.add_paths(files[1:])
        assert zone.paths == files

    def test_the_same_file_is_not_added_twice(self, tmp_path) -> None:
        zone, files = self._zone(tmp_path)
        zone.add_paths(files)
        zone.add_paths([files[0]])
        assert zone.paths == files

    def test_the_badge_pages_both_ways_and_wraps(self, tmp_path) -> None:
        zone, files = self._zone(tmp_path)
        zone.add_paths(files)
        assert zone.path == files[0]
        zone.show_next()
        assert zone.path == files[1]
        zone.show_previous()
        assert zone.path == files[0]
        zone.show_previous()  # 往前绕回末尾
        assert zone.path == files[-1]
        zone.show_next()  # 往后绕回开头
        assert zone.path == files[0]

    def test_the_badge_shows_position_and_total(self, tmp_path) -> None:
        zone, files = self._zone(tmp_path)
        zone.add_paths(files)
        assert zone._page_badge.text() == "1 / 3"
        zone.show_next()
        assert zone._page_badge.text() == "2 / 3"

    def test_the_pager_only_appears_when_there_is_more_than_one(self, tmp_path) -> None:
        zone, files = self._zone(tmp_path)
        zone.add_paths(files[:1])
        assert zone._page_badge.isHidden()
        zone.add_paths(files[1:2])
        assert not zone._page_badge.isHidden()

    def test_removing_the_current_one_falls_through_to_the_next(self, tmp_path) -> None:
        zone, files = self._zone(tmp_path)
        zone.add_paths(files)
        zone.show_next()  # 停在第 2 条
        zone.remove_current()
        assert zone.paths == [files[0], files[2]]
        assert zone.path == files[2]

    def test_removing_everything_returns_to_the_empty_state(self, tmp_path) -> None:
        zone, files = self._zone(tmp_path)
        zone.add_paths(files)
        for _ in range(len(files)):
            zone.remove_current()
        assert zone.paths == []
        assert zone.path is None
        assert zone.hint_label.text() == "原始提示"
        assert zone._remove_badge.isHidden() and zone._page_badge.isHidden()

    def test_the_hint_says_how_many_are_loaded(self, tmp_path) -> None:
        zone, files = self._zone(tmp_path)
        zone.add_paths(files)
        assert "3 个伴奏音频" in zone.hint_label.text()

    def test_a_single_file_zone_still_replaces(self, tmp_path) -> None:
        """视频/原唱卡没开多文件，行为必须和以前一样。"""
        from krok_helper.gui_qt import HIRES_AUDIO_EXTENSIONS, DropZoneCard

        first, second = tmp_path / "a.flac", tmp_path / "b.flac"
        for item in (first, second):
            item.write_bytes(b"x")
        zone = DropZoneCard(title="原唱", hint="", extensions=HIRES_AUDIO_EXTENSIONS)
        zone.add_paths([first])
        zone.add_paths([second])
        assert zone.paths == [second]
        assert zone.path == second


class TestAccompanimentHandoff:
    """分离产物交给 Hi-Res 混流。"""

    @staticmethod
    def _result(task, pairs, error=""):
        from krok_helper.audio_processing.separation.backend import ResultFile, TaskResult

        return TaskResult(
            task=task,
            title="t",
            finished_at="12:00",
            files=[ResultFile(path=str(path), label=label) for label, path in pairs],
            error=error,
        )

    @staticmethod
    def _audio(tmp_path, name):
        item = tmp_path / name
        item.write_bytes(b"x")
        return item

    def test_only_accompaniment_tracks_are_offered(self, tmp_path) -> None:
        """人声不是伴奏，不该被塞进 Hi-Res 的伴奏卡。"""
        from krok_helper.audio_processing.separation.handoff import collect_accompaniments
        from krok_helper.audio_processing.separation.states import TaskType

        vocal = self._audio(tmp_path, "人声.flac")
        inst = self._audio(tmp_path, "伴奏.flac")
        harmony = self._audio(tmp_path, "和声伴奏.flac")
        picked = collect_accompaniments(
            [
                self._result(TaskType.VOCAL, [("人声", vocal)]),
                self._result(TaskType.INSTRUMENTAL, [("伴奏", inst)]),
                self._result(TaskType.HARMONY, [("和声伴奏", harmony)]),
            ]
        )
        assert [path for _label, path in picked] == [inst, harmony]

    def test_a_failed_task_offers_nothing(self, tmp_path) -> None:
        from krok_helper.audio_processing.separation.handoff import collect_accompaniments
        from krok_helper.audio_processing.separation.states import TaskType

        assert collect_accompaniments(
            [self._result(TaskType.INSTRUMENTAL, [], error="炸了")]
        ) == []

    def test_a_file_that_vanished_is_dropped(self, tmp_path) -> None:
        """产物可能被用户移走了，塞一个不存在的路径进去只会让 Hi-Res 报错。"""
        from krok_helper.audio_processing.separation.handoff import collect_accompaniments
        from krok_helper.audio_processing.separation.states import TaskType

        assert collect_accompaniments(
            [self._result(TaskType.INSTRUMENTAL, [("伴奏", tmp_path / "没了.flac")])]
        ) == []

    def test_everything_is_checked_by_default(self, tmp_path) -> None:
        from krok_helper.audio_processing.separation.handoff import (
            AccompanimentHandoffDialog,
        )

        inst = self._audio(tmp_path, "伴奏.flac")
        harmony = self._audio(tmp_path, "和声伴奏.flac")
        dialog = AccompanimentHandoffDialog([("伴奏", inst), ("和声伴奏", harmony)])
        assert dialog.selected_paths() == [inst, harmony]

    def test_unchecking_everything_disables_the_confirm_button(self, tmp_path) -> None:
        from krok_helper.audio_processing.separation.handoff import (
            AccompanimentHandoffDialog,
        )

        inst = self._audio(tmp_path, "伴奏.flac")
        dialog = AccompanimentHandoffDialog([("伴奏", inst)])
        assert dialog.yesButton.isEnabled()
        dialog._checks[0][0].setChecked(False)
        assert not dialog.yesButton.isEnabled()
        assert dialog.selected_paths() == []


class TestDroppingSeveralFilesKeepsThemAll:
    """踩过的坑：卡片自己已经收好了，主窗口再回写一次会把列表塌成一条。"""

    @staticmethod
    def _drop(zone, paths):
        from PyQt6.QtCore import QMimeData, QPointF, Qt, QUrl
        from PyQt6.QtGui import QDropEvent

        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(path)) for path in paths])
        zone.dropEvent(
            QDropEvent(
                QPointF(5, 5),
                Qt.DropAction.CopyAction,
                mime,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
        )
        return mime  # 事件不持有 QMimeData，返回给调用方保命

    def test_a_multi_drop_keeps_every_file(self, tmp_path) -> None:
        from krok_helper.gui_qt import HIRES_AUDIO_EXTENSIONS, DropZoneCard

        files = []
        for name in ("a.flac", "b.flac", "c.flac"):
            item = tmp_path / name
            item.write_bytes(b"x")
            files.append(item)
        zone = DropZoneCard(
            title="伴奏", hint="", extensions=HIRES_AUDIO_EXTENSIONS, multiple=True
        )
        keep = self._drop(zone, files)  # noqa: F841
        assert zone.paths == files

    def test_the_hires_page_does_not_write_the_list_back(self) -> None:
        import inspect

        from krok_helper.gui_qt import KrokHelperQtApp

        source = inspect.getsource(KrokHelperQtApp._build_hires_page)
        assert "off_vocal_zone.pathChanged.connect" not in source

    def test_a_single_file_zone_still_takes_only_one(self, tmp_path) -> None:
        from krok_helper.gui_qt import HIRES_AUDIO_EXTENSIONS, DropZoneCard

        files = []
        for name in ("a.flac", "b.flac"):
            item = tmp_path / name
            item.write_bytes(b"x")
            files.append(item)
        zone = DropZoneCard(title="原唱", hint="", extensions=HIRES_AUDIO_EXTENSIONS)
        keep = self._drop(zone, files)  # noqa: F841
        assert zone.paths == files[:1]


class TestFlipAnimation:
    def test_paging_repeatedly_does_not_touch_a_deleted_animation(self, tmp_path) -> None:
        """动画是 DeleteWhenStopped，跑完 C++ 对象就没了；再翻页时若还引用它会直接崩。"""
        from krok_helper.gui_qt import HIRES_AUDIO_EXTENSIONS, DropZoneCard

        files = []
        for name in ("a.flac", "b.flac", "c.flac"):
            item = tmp_path / name
            item.write_bytes(b"x")
            files.append(item)
        zone = DropZoneCard(
            title="伴奏", hint="", extensions=HIRES_AUDIO_EXTENSIONS, multiple=True
        )
        zone.resize(330, 190)
        zone.add_paths(files)

        # 直接驱动动画分支，不依赖窗口是否真的可见（无头环境里 isVisible 为 False）。
        # 没有事件循环时收尾回调不会跑，序号自然不前进；这里要的是「反复触发不崩」。
        for _ in range(4):
            zone._flip_to((zone._index + 1) % len(files))
        assert zone.path in files
