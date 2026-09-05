"""SubtitleRenderWindow 的素材加载器测试。

通过 monkeypatch ``probe_media`` 避免真实 ffprobe 调用；通过
``QT_QPA_PLATFORM=offscreen`` 保证无显示器环境也能构造 Qt widget。
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
import sys
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, Qt  # noqa: E402
from PyQt6.QtGui import QColor, QImage  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402
from qfluentwidgets import (  # noqa: E402
    ComboBox,
    LineEdit,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    SpinBox,
    TransparentToolButton,
)
import krok_helper  # noqa: E402, F401 - registers the bundled SUG source path
from strange_uta_game.backend.domain import (  # noqa: E402
    Character,
    Project,
    Sentence,
    Singer,
)
from strange_uta_game.backend.infrastructure.persistence.sug_io import (  # noqa: E402
    SugProjectParser,
)

from krok_helper.models import MediaInfo  # noqa: E402
from krok_helper.subtitle_render.domain.models import (  # noqa: E402
    GuideSymbol,
    LyricsLayout,
    Style,
    SubtitleStyleScheme,
    TimingChar,
    TimingLine,
    TimingTrack,
    style_to_dict,
)
from krok_helper.subtitle_render.frontend import main_window as mw  # noqa: E402
from krok_helper.subtitle_render.frontend.editor.lyrics_list import COL_CONTENT  # noqa: E402
from krok_helper.subtitle_render.frontend.project.source_watch import (  # noqa: E402
    SourceReloadDisposition,
    SubtitleSourceWatchRuntime,
    subtitle_source_key,
)
from krok_helper.subtitle_render.frontend.widgets.workspace_switcher import (  # noqa: E402
    WorkspaceSwitcher,
)


def test_main_window_uses_public_layout_diagnostics_boundary() -> None:
    source_path = Path("krok_helper/subtitle_render/frontend/main_window.py")
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert (
        "krok_helper.subtitle_render.engine.layout.display.diagnostics"
        in imported_modules
    )
    assert "krok_helper.subtitle_render.engine.painter" not in imported_modules


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _make_window(qapp, monkeypatch):
    monkeypatch.setattr(mw, "fluent_error", lambda *a, **k: None)
    monkeypatch.setattr(mw, "fluent_warning", lambda *a, **k: None)
    monkeypatch.setattr(
        mw.SubtitleRenderWindow,
        "_resolve_ffprobe_path",
        lambda self: "ffprobe",
    )
    return mw.SubtitleRenderWindow(embedded=False)


# ---------------------------------------------------------------------------
# A1 字幕：填充左侧歌词列表
# ---------------------------------------------------------------------------


def test_legacy_project_load_migrates_spacing_to_used_layouts(qapp, monkeypatch, tmp_path):
    """schema<3 的旧工程装载时迁移：空格宽度绑定使用过的布局，方案槽位丢弃。"""
    win = _make_window(qapp, monkeypatch)
    lrc = tmp_path / "legacy.lrc"
    lrc.write_text("[00:01:00]a[00:02:00]b[00:03:00]\n", encoding="utf-8")
    style = Style(
        space_width_percent=20,
        layouts=[LyricsLayout(name="A", layout_id="a", line_alignments=["left"])],
        custom_style_schemes={"标题": SubtitleStyleScheme(space_width_percent=80)},
    )

    win._apply_project_data(
        {
            "schema_version": 2,
            "subtitle_path": str(lrc),
            "style": style_to_dict(style),
            "line_layout_indices": [1, 0],
        }
    )

    assert win._style.space_width_percent == 20
    assert win._style.layouts[0].space_width_percent == 20
    assert win._style.layouts[0].letter_spacing_px == 0
    assert win._style.custom_style_schemes["标题"].space_width_percent is None

    # 当前版本工程（含迁移后重新保存的）不再迁移，布局缺键即继承全局。
    # v4 引入多标题，v3 工程装载时仍会走一次迁移。
    win._apply_project_data(
        {
            "schema_version": 4,
            "subtitle_path": str(lrc),
            "style": style_to_dict(style),
            "line_layout_indices": [1, 0],
        }
    )
    assert win._style.layouts[0].space_width_percent is None
    assert win._style.custom_style_schemes["标题"].space_width_percent == 80


def test_load_subtitle_wires_preview_and_transport(qapp, monkeypatch, tmp_path):
    """A4：加载字幕后预览面板 / 时间轴滑块都应该联动起来。"""
    win = _make_window(qapp, monkeypatch)
    assert not win._preview_panel.is_populated()

    lrc = tmp_path / "demo.lrc"
    lrc.write_bytes(
        b"\xef\xbb\xbf"
        + (
            "[00:01:00]a[00:01:50]b[00:02:00]c[00:02:50]\r\n"
            "\r\n"
            "@Title=Foo\r\n"
        ).encode("utf-8")
    )
    track = win.load_from_lrc(lrc)
    assert track is not None

    # 预览面板被切到 populated 状态，canvas 拿到了 track
    assert win._preview_panel.is_populated()
    assert win._preview_panel.canvas._track is track

    # transport 滑块上限按 track 时长收敛（行末 2500ms）
    assert win._transport_bar._slider.maximum() == 2500
    assert win._preview_panel.canvas._duration_ms == 2500

    # 滑块拖动 → preview canvas 同步时间
    win._transport_bar.set_time(1700)
    assert win._preview_panel.canvas.current_time_ms == 1700


def test_external_lrc_timestamp_change_hot_reloads_without_confirmation(
    qapp, monkeypatch, tmp_path
):
    win = _make_window(qapp, monkeypatch)
    lrc = tmp_path / "watched.lrc"
    lrc.write_text(
        "[00:01:00]a[00:01:50]b[00:02:00]\n",
        encoding="utf-8-sig",
    )
    assert win.load_from_lrc(lrc) is not None
    line = win._timing_track.lines[0]
    line.layout_index = 2
    line.guide_symbol = GuideSymbol(
        path_commands=(("M", 0.0, 0.0), ("L", 1.0, 1.0)),
    )
    line.inline_guide_symbols = {1: line.guide_symbol}
    line.chars[0].role_label = "手工角色"
    win._undo_stack.append(("keep",))
    win._transport_bar.set_time(1200)
    win._project_dirty = False
    notices: list[str] = []
    monkeypatch.setattr(
        mw.InfoBar,
        "success",
        lambda **kwargs: notices.append(kwargs["content"]),
    )
    monkeypatch.setattr(
        mw,
        "fluent_question",
        lambda *args, **kwargs: pytest.fail("纯时间变化不应要求确认"),
    )

    lrc.write_text(
        "[00:02:00]a[00:02:50]b[00:03:00]\n",
        encoding="utf-8-sig",
    )
    win._reload_external_subtitle_source(win._subtitle_source_key(lrc))

    line = win._timing_track.lines[0]
    assert [char.start_ms for char in line.chars] == [2000, 2500]
    assert line.end_ms == 3000
    assert line.layout_index == 2
    assert line.guide_symbol is not None
    assert 1 in line.inline_guide_symbols
    assert line.chars[0].role_label == "手工角色"
    assert win._undo_stack[0] == ("keep",)
    assert win._transport_bar.current_time_ms == 1200
    assert win._project_dirty is True
    assert notices == ["已自动载入 watched.lrc 的最新时间轴。"]


def test_external_sug_timestamp_change_uses_same_hot_reload_path(
    qapp, monkeypatch, tmp_path
):
    singer = Singer(id="main", name="主唱", color="#ff0000", is_default=True)
    char = Character(
        char="歌",
        check_count=1,
        timestamps=[1000],
        sentence_end_ts=1800,
        is_sentence_end=True,
        is_line_end=True,
        singer_id=singer.id,
    )
    project = Project(
        singers=[singer],
        sentences=[Sentence(singer_id=singer.id, characters=[char])],
    )
    sug = tmp_path / "watched.sug"
    SugProjectParser.save(project, str(sug))
    win = _make_window(qapp, monkeypatch)
    assert win.load_from_sug(sug) is not None
    win._timing_track.lines[0].layout_index = 3
    win._project_dirty = False
    monkeypatch.setattr(mw.InfoBar, "success", lambda **kwargs: None)
    monkeypatch.setattr(
        mw,
        "fluent_question",
        lambda *args, **kwargs: pytest.fail("纯时间变化不应要求确认"),
    )

    char.timestamps = [2300]
    char.sentence_end_ts = 3100
    SugProjectParser.save(project, str(sug))
    win._reload_external_subtitle_source(win._subtitle_source_key(sug))

    line = win._timing_track.lines[0]
    assert line.chars[0].start_ms == 2300
    assert line.end_ms == 3100
    assert line.layout_index == 3
    assert win._project_dirty is True


def _save_timing_sug(path: Path, lines: list[tuple[str, int, int]]) -> None:
    """写一个最小 SUG：每元素为 (文本, 字起始 ms, 行末 ms) 的一行歌词。"""
    singer = Singer(id="main", name="主唱", color="#ff0000", is_default=True)
    sentences = [
        Sentence(
            singer_id=singer.id,
            characters=[
                Character(
                    char=text,
                    check_count=1,
                    timestamps=[start_ms],
                    sentence_end_ts=end_ms,
                    is_sentence_end=True,
                    is_line_end=True,
                    singer_id=singer.id,
                )
            ],
        )
        for text, start_ms, end_ms in lines
    ]
    SugProjectParser.save(Project(singers=[singer], sentences=sentences), str(path))


def _save_grouped_sug(
    path: Path,
    lines: list[tuple[str, str, int, int]],
    groups: list[tuple[str, list[str], bool]],
) -> None:
    """写一个带分色分轴计划的 SUG：每元素为 (歌手id, 文本, 起始, 行末)。"""
    from strange_uta_game.backend.domain import AxisGroup

    singers = [
        Singer(id="a", name="主唱", color="#ff0000", is_default=True),
        Singer(id="b", name="和声", color="#00ff00"),
    ]
    sentences = [
        Sentence(
            singer_id=singer_id,
            characters=[
                Character(
                    char=text,
                    check_count=1,
                    timestamps=[start_ms],
                    sentence_end_ts=end_ms,
                    is_sentence_end=True,
                    is_line_end=True,
                    singer_id=singer_id,
                )
            ],
        )
        for singer_id, text, start_ms, end_ms in lines
    ]
    project = Project(singers=singers, sentences=sentences)
    project.set_axis_groups(
        [AxisGroup(name=name, singer_ids=ids, is_primary=primary) for name, ids, primary in groups]
    )
    SugProjectParser.save(project, str(path))


def test_workflow_handoff_splits_grouped_sug_into_sources(qapp, monkeypatch, tmp_path):
    """第 4→5 步交接：带 axis_groups 的 .sug 拆成主字幕 + 分组副字幕源。"""
    sug = tmp_path / "grouped.sug"
    _save_grouped_sug(
        sug,
        [("a", "君", 1000, 1400), ("b", "酱", 2000, 2400)],
        [("主轴", ["a"], True), ("副轴", ["b"], False)],
    )
    win = _make_window(qapp, monkeypatch)

    returned = win.load_or_reload_sug(sug)

    assert returned is win._timing_track
    assert ["".join(ch.text for ch in line.chars) for line in win._timing_track.lines] == ["君"]
    assert [source.name for source in win._extra_sources] == ["副轴"]
    assert [
        "".join(ch.text for ch in line.chars) for line in win._extra_sources[0].track.lines
    ] == ["酱"]
    assert win._extra_sources[0].sug_axis_singer_ids == frozenset({"b"})
    assert win._project_document.subtitle_axis_singer_ids == frozenset({"a"})


def test_workflow_handoff_same_groups_reload_updates_all_axes(
    qapp, monkeypatch, tmp_path
):
    """分组不变、纯改时间：主轴与轴副源都走增量合并，本地编辑保留。"""
    sug = tmp_path / "grouped.sug"
    _save_grouped_sug(
        sug,
        [("a", "君", 1000, 1400), ("b", "酱", 2000, 2400)],
        [("主轴", ["a"], True), ("副轴", ["b"], False)],
    )
    win = _make_window(qapp, monkeypatch)
    assert win.load_or_reload_sug(sug) is not None
    win._timing_track.lines[0].layout_index = 2
    win._extra_sources[0].track.lines[0].chars[0].role_label = "手工角色"
    win._project_dirty = False
    notices: list[str] = []
    monkeypatch.setattr(
        mw.InfoBar, "success", lambda **kwargs: notices.append(kwargs["content"])
    )
    monkeypatch.setattr(
        mw,
        "fluent_question",
        lambda *args, **kwargs: pytest.fail("纯时间变化不应要求确认"),
    )

    _save_grouped_sug(
        sug,
        [("a", "君", 1300, 1700), ("b", "酱", 2300, 2700)],
        [("主轴", ["a"], True), ("副轴", ["b"], False)],
    )
    win.load_or_reload_sug(sug)

    assert win._timing_track.lines[0].chars[0].start_ms == 1300
    assert win._timing_track.lines[0].layout_index == 2
    extra_line = win._extra_sources[0].track.lines[0]
    assert extra_line.chars[0].start_ms == 2300
    assert extra_line.chars[0].role_label == "手工角色"
    assert win._project_dirty is True
    assert notices == ["已自动载入 grouped.sug 的最新时间轴。"]


def test_workflow_handoff_group_added_after_single_load_splits(qapp, monkeypatch, tmp_path):
    """单轴加载后 SUG 里新增分组：重交接整体重装为分轴。"""
    sug = tmp_path / "late.sug"
    _save_grouped_sug(sug, [("a", "君", 1000, 1400), ("b", "酱", 2000, 2400)], [])
    win = _make_window(qapp, monkeypatch)
    assert win.load_or_reload_sug(sug) is not None
    assert win._extra_sources == []
    notices: list[str] = []
    monkeypatch.setattr(
        mw.InfoBar, "success", lambda **kwargs: notices.append(kwargs["content"])
    )

    _save_grouped_sug(
        sug,
        [("a", "君", 1000, 1400), ("b", "酱", 2000, 2400)],
        [("主轴", ["a"], True), ("副轴", ["b"], False)],
    )
    win.load_or_reload_sug(sug)

    assert [source.name for source in win._extra_sources] == ["副轴"]
    assert win._project_document.subtitle_axis_singer_ids == frozenset({"a"})
    assert [
        "".join(ch.text for ch in line.chars) for line in win._timing_track.lines
    ] == ["君"]
    assert any("重新分轴" in notice for notice in notices)


def test_project_reopen_restores_axis_filters_from_snapshot(qapp, monkeypatch, tmp_path):
    """.yurika 快照里的主/副轴过滤在工程打开时按各自口径重建。"""
    sug = tmp_path / "grouped.sug"
    _save_grouped_sug(
        sug,
        [("a", "君", 1000, 1400), ("b", "酱", 2000, 2400)],
        [("主轴", ["a"], True), ("副轴", ["b"], False)],
    )
    win = _make_window(qapp, monkeypatch)

    win._apply_project_data(
        {
            "subtitle_path": str(sug),
            "subtitle_sug_axis_singer_ids": ["a"],
            "extra_subtitle_sources": [
                {"name": "副轴", "path": str(sug), "sug_axis_singer_ids": ["b"]}
            ],
        }
    )

    assert [
        "".join(ch.text for ch in line.chars) for line in win._timing_track.lines
    ] == ["君"]
    assert [source.name for source in win._extra_sources] == ["副轴"]
    assert [
        "".join(ch.text for ch in line.chars)
        for line in win._extra_sources[0].track.lines
    ] == ["酱"]
    assert win._extra_sources[0].sug_axis_singer_ids == frozenset({"b"})
    assert win._project_document.subtitle_axis_singer_ids == frozenset({"a"})


def test_workflow_handoff_same_path_merges_preserving_overlays(
    qapp, monkeypatch, tmp_path
):
    """第 4→5 步交接：同路径且文件已变化时走 watcher 同款增量合并。"""
    sug = tmp_path / "watched.sug"
    _save_timing_sug(sug, [("歌", 1000, 1800)])
    win = _make_window(qapp, monkeypatch)
    assert win.load_from_sug(sug) is not None
    line = win._timing_track.lines[0]
    line.layout_index = 3
    line.chars[0].role_label = "手工角色"
    # 哨兵：纯时间合并不得清空撤销历史（合并可能滚动更新栈顶 style 记录）
    win._undo_stack.append(("keep",))
    win._project_dirty = False
    notices: list[str] = []
    monkeypatch.setattr(
        mw.InfoBar, "success", lambda **kwargs: notices.append(kwargs["content"])
    )
    monkeypatch.setattr(
        mw,
        "fluent_question",
        lambda *args, **kwargs: pytest.fail("纯时间变化不应要求确认"),
    )

    _save_timing_sug(sug, [("歌", 2300, 3100)])
    returned = win.load_or_reload_sug(sug)

    assert returned is win._timing_track
    line = win._timing_track.lines[0]
    assert line.chars[0].start_ms == 2300
    assert line.end_ms == 3100
    assert line.layout_index == 3
    assert line.chars[0].role_label == "手工角色"
    assert ("keep",) in win._undo_stack
    assert win._project_dirty is True
    assert notices == ["已自动载入 watched.sug 的最新时间轴。"]


def test_workflow_handoff_same_path_unchanged_sug_is_noop(
    qapp, monkeypatch, tmp_path
):
    """第 4→5 步交接：同路径且内容未变时不重载、不清撤销栈、不标脏。"""
    sug = tmp_path / "watched.sug"
    _save_timing_sug(sug, [("歌", 1000, 1800)])
    win = _make_window(qapp, monkeypatch)
    assert win.load_from_sug(sug) is not None
    win._timing_track.lines[0].layout_index = 3
    undo_before = list(win._undo_stack)
    win._project_dirty = False
    notices: list[str] = []
    monkeypatch.setattr(
        mw.InfoBar, "success", lambda **kwargs: notices.append(kwargs["content"])
    )
    monkeypatch.setattr(mw.InfoBar, "warning", lambda **kwargs: notices.append(kwargs["content"]))

    returned = win.load_or_reload_sug(sug)

    assert returned is win._timing_track
    assert win._timing_track.lines[0].layout_index == 3
    assert win._undo_stack == undo_before
    assert win._project_dirty is False
    assert notices == []


def test_workflow_handoff_structural_conflict_declined_keeps_current(
    qapp, monkeypatch, tmp_path
):
    """结构冲突被拒绝：保留当前内容并记住 digest，重复交接不再打扰。"""
    sug = tmp_path / "watched.sug"
    _save_timing_sug(sug, [("歌", 1000, 1800), ("词", 2000, 2800)])
    win = _make_window(qapp, monkeypatch)
    assert win.load_from_sug(sug) is not None
    win._timing_track.lines[1].layout_index = 1
    win._project_dirty = False
    questions: list[object] = []
    monkeypatch.setattr(
        mw, "fluent_question", lambda *args, **kwargs: questions.append(args) or False
    )
    monkeypatch.setattr(mw.InfoBar, "success", lambda **kwargs: None)

    # 新版本删掉了带本地覆盖的第 2 行 → 结构冲突
    _save_timing_sug(sug, [("歌", 2300, 3100)])
    returned = win.load_or_reload_sug(sug)

    assert returned is win._timing_track
    assert len(win._timing_track.lines) == 2
    assert win._timing_track.lines[0].chars[0].start_ms == 1000
    assert win._timing_track.lines[1].layout_index == 1
    assert win._project_dirty is False
    assert len(questions) == 1

    # 拒绝后 digest 已被记住：再次交接同一文件不再弹窗、不再重载
    returned = win.load_or_reload_sug(sug)
    assert returned is win._timing_track
    assert len(win._timing_track.lines) == 2
    assert len(questions) == 1


def test_workflow_handoff_structural_conflict_accepted_loads_new(
    qapp, monkeypatch, tmp_path
):
    """结构冲突被接受：载入新内容并清空撤销栈（结构已变化）。"""
    sug = tmp_path / "watched.sug"
    _save_timing_sug(sug, [("歌", 1000, 1800), ("词", 2000, 2800)])
    win = _make_window(qapp, monkeypatch)
    assert win.load_from_sug(sug) is not None
    win._timing_track.lines[1].layout_index = 1
    win._undo_stack.append(("keep",))
    monkeypatch.setattr(mw, "fluent_question", lambda *args, **kwargs: True)
    monkeypatch.setattr(mw.InfoBar, "success", lambda **kwargs: None)

    _save_timing_sug(sug, [("歌", 2300, 3100)])
    returned = win.load_or_reload_sug(sug)

    assert returned is win._timing_track
    assert len(win._timing_track.lines) == 1
    assert win._timing_track.lines[0].chars[0].start_ms == 2300
    assert win._timing_track.lines[0].end_ms == 3100
    assert win._undo_stack == []
    assert win._project_dirty is True


def test_workflow_handoff_different_path_full_reloads(
    qapp, monkeypatch, tmp_path
):
    """第 4→5 步交接：路径不同于当前主字幕源时回退整体重载。"""
    sug_a = tmp_path / "a.sug"
    sug_b = tmp_path / "b.sug"
    _save_timing_sug(sug_a, [("歌", 1000, 1800)])
    _save_timing_sug(sug_b, [("词", 3300, 4100)])
    win = _make_window(qapp, monkeypatch)
    assert win.load_from_sug(sug_a) is not None
    win._timing_track.lines[0].chars[0].role_label = "手工角色"
    win._undo_stack.append(("keep",))
    monkeypatch.setattr(mw.InfoBar, "success", lambda **kwargs: None)

    returned = win.load_or_reload_sug(sug_b)

    assert returned is win._timing_track
    assert win._subtitle_path == sug_b
    assert win._timing_track.lines[0].chars[0].start_ms == 3300
    assert win._timing_track.lines[0].chars[0].role_label != "手工角色"
    assert win._undo_stack == []
    assert win._watch_primary_subtitle_source is True
    assert win._subtitle_source_key(sug_b) in win._source_watch_states
    assert win._subtitle_source_key(sug_a) not in win._source_watch_states


def test_has_project_conflicting_sug_requires_other_primary_source(
    qapp, monkeypatch, tmp_path
):
    """询问条件：已载入主字幕且不是同一文件才需要选择安置方式。"""
    sug_a = tmp_path / "a.sug"
    sug_b = tmp_path / "b.sug"
    _save_timing_sug(sug_a, [("歌", 1000, 1800)])
    _save_timing_sug(sug_b, [("词", 3300, 4100)])
    win = _make_window(qapp, monkeypatch)

    assert win.has_project_conflicting_sug(sug_a) is False

    assert win.load_from_sug(sug_a) is not None
    assert win.has_project_conflicting_sug(sug_a) is False
    assert win.has_project_conflicting_sug(sug_b) is True


def test_new_project_from_sug_prompts_save_then_loads_fresh(
    qapp, monkeypatch, tmp_path
):
    """新建项目安置：先走未保存确认，确认后新建工程并载入该 SUG 为主字幕。"""
    sug_a = tmp_path / "a.sug"
    sug_b = tmp_path / "b.sug"
    _save_timing_sug(sug_a, [("歌", 1000, 1800)])
    _save_timing_sug(sug_b, [("词", 3300, 4100)])
    win = _make_window(qapp, monkeypatch)
    assert win.load_from_sug(sug_a) is not None
    win._timing_track.lines[0].layout_index = 3
    win._undo_stack.append(("keep",))
    win._project_dirty = True
    choices: list[object] = []
    monkeypatch.setattr(
        mw, "fluent_choice", lambda *args, **kwargs: choices.append(args[1]) or 0
    )

    def fake_save() -> bool:
        choices.append(("save",))
        win._project_dirty = False
        return True

    monkeypatch.setattr(win, "_save_project", fake_save)
    monkeypatch.setattr(mw.InfoBar, "success", lambda **kwargs: None)

    returned = win.new_project_from_sug(sug_b)

    assert returned is win._timing_track
    assert "未保存的改动" in choices
    assert ("save",) in choices
    assert win._subtitle_path == sug_b
    assert win._timing_track.lines[0].chars[0].start_ms == 3300
    # 新工程：旧主字幕的本地编辑与撤销历史一并作废
    assert win._timing_track.lines[0].layout_index != 3
    assert ("keep",) not in win._undo_stack
    assert win._project_dirty is True


def test_new_project_from_sug_cancelled_keeps_current(qapp, monkeypatch, tmp_path):
    """新建项目安置：未保存确认选择取消时中止，当前工程保持不变。"""
    sug_a = tmp_path / "a.sug"
    sug_b = tmp_path / "b.sug"
    _save_timing_sug(sug_a, [("歌", 1000, 1800)])
    _save_timing_sug(sug_b, [("词", 3300, 4100)])
    win = _make_window(qapp, monkeypatch)
    assert win.load_from_sug(sug_a) is not None
    win._project_dirty = True
    monkeypatch.setattr(mw, "fluent_choice", lambda *args, **kwargs: 2)

    returned = win.new_project_from_sug(sug_b)

    assert returned is None
    assert win._subtitle_path == sug_a
    assert win._timing_track.lines[0].chars[0].start_ms == 1000
    assert win._project_dirty is True


def test_add_sug_as_extra_source_appends_track(qapp, monkeypatch, tmp_path):
    """新建字幕轨道安置：SUG 作为副字幕源加入，主字幕与工程上下文保留。"""
    sug_a = tmp_path / "a.sug"
    sug_b = tmp_path / "b.sug"
    _save_timing_sug(sug_a, [("歌", 1000, 1800)])
    _save_timing_sug(sug_b, [("词", 3300, 4100)])
    win = _make_window(qapp, monkeypatch)
    assert win.load_from_sug(sug_a) is not None
    win._project_dirty = False

    returned = win.add_sug_as_extra_source(sug_b)

    assert returned is not None
    assert [source.name for source in win._extra_sources] == ["b"]
    assert win._extra_sources[0].path == sug_b
    assert [
        "".join(ch.text for ch in line.chars)
        for line in win._extra_sources[0].track.lines
    ] == ["词"]
    # 主字幕不受影响
    assert win._subtitle_path == sug_a
    assert win._timing_track.lines[0].chars[0].start_ms == 1000
    assert win._active_source_index == 1
    assert win._project_dirty is True


def test_add_sug_as_extra_source_replaces_same_path(qapp, monkeypatch, tmp_path):
    """新建字幕轨道安置：同路径副源整体替换，不产生重复源。"""
    sug_a = tmp_path / "a.sug"
    sug_b = tmp_path / "b.sug"
    _save_timing_sug(sug_a, [("歌", 1000, 1800)])
    _save_timing_sug(sug_b, [("词", 3300, 4100)])
    win = _make_window(qapp, monkeypatch)
    assert win.load_from_sug(sug_a) is not None
    assert win.add_sug_as_extra_source(sug_b) is not None

    _save_timing_sug(sug_b, [("新", 500, 900)])
    returned = win.add_sug_as_extra_source(sug_b)

    assert returned is not None
    assert len(win._extra_sources) == 1
    assert [
        "".join(ch.text for ch in line.chars)
        for line in win._extra_sources[0].track.lines
    ] == ["新"]


def test_add_sug_as_extra_source_without_primary_is_rejected(
    qapp, monkeypatch, tmp_path
):
    """新建字幕轨道安置：没有主字幕时拒绝并提示（正常交接不会走到这里）。"""
    sug = tmp_path / "solo.sug"
    _save_timing_sug(sug, [("歌", 1000, 1800)])
    win = _make_window(qapp, monkeypatch)
    infos: list[tuple[str, str]] = []
    monkeypatch.setattr(
        mw, "fluent_info", lambda parent, title, content: infos.append((title, content))
    )

    returned = win.add_sug_as_extra_source(sug)

    assert returned is None
    assert win._extra_sources == []
    assert infos == [("无法新建字幕轨道", "当前项目没有主字幕，请先导入主字幕。")]


def test_in_memory_sug_handoff_does_not_enable_file_watching(
    qapp, monkeypatch, tmp_path
):
    singer = Singer(id="main", name="主唱", color="#ff0000", is_default=True)
    project = Project(
        singers=[singer],
        sentences=[
            Sentence(
                singer_id=singer.id,
                characters=[
                    Character(
                        char="歌",
                        check_count=1,
                        timestamps=[1000],
                        singer_id=singer.id,
                    )
                ],
            )
        ],
    )
    source_path = tmp_path / "workflow.sug"
    # SUG keeps portable settings beside argv[0].  Point that probe at this
    # test's writable sandbox instead of the installed Python directory.
    monkeypatch.setattr(
        sys,
        "argv",
        [str(tmp_path / "pytest.exe"), *sys.argv[1:]],
    )
    monkeypatch.setattr(
        mw.SubtitleRenderWindow,
        "_resolve_unresolved_resource_labels",
        lambda self, *_args, **_kwargs: None,
    )
    win = _make_window(qapp, monkeypatch)

    track = win.load_from_sug_project(
        project,
        source_path,
        nicokara_tags={
            "title": "联动曲名",
            "artist": "联动歌手",
            "tagging_by": "打轴者",
            "custom": ["@Emoji=主唱"],
        },
    )
    assert track is not None
    assert track.meta.title == "联动曲名"
    assert track.meta.artist == "联动歌手"
    assert track.meta.tagging_by == "打轴者"
    assert track.meta.custom == ["@Emoji=主唱"]
    assert track.page_plan is not None
    assert track.loading_settings_mode == "global"
    assert any(
        row.kind == "page_marker"
        for row in win._lyrics_panel._presentation_rows
    )

    assert win._watch_primary_subtitle_source is False
    assert win._subtitle_source_key(source_path) not in win._source_watch_states
    assert str(source_path.resolve()) not in win._source_watcher.files()


def test_external_lrc_filesystem_watcher_detects_change(
    qapp, monkeypatch, tmp_path
):
    win = _make_window(qapp, monkeypatch)
    monkeypatch.setattr(mw.InfoBar, "success", lambda **kwargs: None)
    monkeypatch.setattr(mw.InfoBar, "warning", lambda **kwargs: None)
    monkeypatch.setattr(
        mw,
        "fluent_question",
        lambda *args, **kwargs: pytest.fail("纯时间变化不应要求确认"),
    )
    lrc = tmp_path / "watch-event.lrc"
    lrc.write_text(
        "[00:01:00]a[00:01:50]b[00:02:00]\n",
        encoding="utf-8-sig",
    )
    assert win.load_from_lrc(lrc) is not None
    assert str(lrc.resolve()) in win._source_watcher.files()

    lrc.write_text(
        "[00:04:00]a[00:04:50]b[00:05:00]\n",
        encoding="utf-8-sig",
    )
    deadline = time.monotonic() + 3.0
    while win._timing_track.lines[0].chars[0].start_ms != 4000:
        qapp.processEvents()
        if time.monotonic() >= deadline:
            break
        QTest.qWait(50)

    assert win._timing_track.lines[0].chars[0].start_ms == 4000


def test_source_watch_runtime_defers_pending_reload_while_suspended(qapp):
    suspended = [True]
    runtime = SubtitleSourceWatchRuntime(
        reload_suspended=lambda: suspended[0],
    )
    ready: list[bool] = []
    runtime.pendingReady.connect(lambda: ready.append(True))

    runtime.queue("source-key")
    assert runtime.pending_keys == {"source-key"}
    assert not runtime.timer.isActive()

    suspended[0] = False
    runtime.start_pending(0)
    QTest.qWait(1)
    qapp.processEvents()

    assert ready == [True]
    assert runtime.take_pending() == ("source-key",)
    assert runtime.pending_keys == set()
    runtime.deleteLater()


def test_source_watch_runtime_owns_missing_and_duplicate_file_state(qapp, tmp_path):
    path = tmp_path / "watched.lrc"
    path.write_text("unchanged", encoding="utf-8")
    baseline = TimingTrack(
        lines=[TimingLine(chars=[TimingChar("a", 1000)], end_ms=1500)]
    )
    runtime = SubtitleSourceWatchRuntime()
    runtime.set_baseline(path, baseline)
    key = subtitle_source_key(path)

    duplicate = runtime.prepare_reload(
        key,
        load_candidate=lambda _path: pytest.fail("duplicate must not parse"),
        primary_track=baseline,
    )
    assert duplicate is not None
    assert duplicate.disposition == SourceReloadDisposition.DUPLICATE

    path.unlink()
    first_missing = runtime.prepare_reload(
        key,
        load_candidate=lambda _path: baseline,
        primary_track=baseline,
    )
    second_missing = runtime.prepare_reload(
        key,
        load_candidate=lambda _path: baseline,
        primary_track=baseline,
    )
    assert first_missing is not None and first_missing.notify is True
    assert second_missing is not None and second_missing.notify is False
    assert first_missing.disposition == SourceReloadDisposition.MISSING
    runtime.deleteLater()


def test_source_watch_runtime_owns_retry_and_baseline_acceptance(qapp, tmp_path):
    path = tmp_path / "watched.lrc"
    path.write_text("before", encoding="utf-8")
    baseline = TimingTrack(
        lines=[TimingLine(chars=[TimingChar("a", 1000)], end_ms=1500)]
    )
    candidate = TimingTrack(
        lines=[TimingLine(chars=[TimingChar("a", 2000)], end_ms=2500)]
    )
    runtime = SubtitleSourceWatchRuntime()
    runtime.set_baseline(path, baseline)
    key = subtitle_source_key(path)
    path.write_text("after", encoding="utf-8")

    def fail_partial_load(_path):
        raise OSError("partial")

    retrying = runtime.prepare_reload(
        key,
        load_candidate=fail_partial_load,
        primary_track=baseline,
    )
    assert retrying is not None
    assert retrying.disposition == SourceReloadDisposition.RETRYING
    assert key in runtime.pending_keys
    runtime.pending_keys.clear()
    runtime.timer.stop()

    ready = runtime.prepare_reload(
        key,
        load_candidate=lambda _path: candidate,
        primary_track=baseline,
    )
    assert ready is not None
    assert ready.disposition == SourceReloadDisposition.READY
    assert runtime.state(key).baseline == baseline

    runtime.ignore_prepared(ready)
    assert runtime.state(key).baseline == baseline
    duplicate = runtime.prepare_reload(
        key,
        load_candidate=lambda _path: pytest.fail("ignored revision is acknowledged"),
        primary_track=baseline,
    )
    assert duplicate is not None
    assert duplicate.disposition == SourceReloadDisposition.DUPLICATE

    path.write_text("after-again", encoding="utf-8")
    accepted = runtime.prepare_reload(
        key,
        load_candidate=lambda _path: candidate,
        primary_track=baseline,
    )
    assert accepted is not None
    assert accepted.disposition == SourceReloadDisposition.READY
    runtime.accept_prepared(accepted)
    assert runtime.state(key).baseline == candidate
    assert runtime.state(key).baseline is not candidate

    path.write_text("always-partial", encoding="utf-8")
    dispositions = []
    for _attempt in range(6):
        failed = runtime.prepare_reload(
            key,
            load_candidate=fail_partial_load,
            primary_track=candidate,
        )
        assert failed is not None
        dispositions.append(failed.disposition)
        runtime.pending_keys.clear()
        runtime.timer.stop()
    assert dispositions == [
        SourceReloadDisposition.RETRYING,
        SourceReloadDisposition.RETRYING,
        SourceReloadDisposition.RETRYING,
        SourceReloadDisposition.RETRYING,
        SourceReloadDisposition.RETRYING,
        SourceReloadDisposition.FAILED,
    ]
    runtime.deleteLater()


def test_load_subtitle_populates_lyrics_panel(qapp, monkeypatch, tmp_path):
    win = _make_window(qapp, monkeypatch)
    assert not win._lyrics_panel.is_populated()

    lrc = tmp_path / "lyrics.lrc"
    lrc.write_bytes(
        b"\xef\xbb\xbf"
        + (
            "[00:01:00]あ[00:01:50]\r\n"
            "[00:02:00]い[00:02:50]う[00:03:00]\r\n"
            "\r\n"
            "[00:04:00]え[00:04:50]\r\n"
            "\r\n"
            "@Title=Demo\r\n"
        ).encode("utf-8")
    )

    track = win.load_from_lrc(lrc)
    assert track is not None
    assert ["".join(char.text for char in line.chars) for line in track.lines] == [
        "あ",
        "いう",
        "",
        "え",
    ]
    assert win._lyrics_panel.is_populated()

    table = win._lyrics_panel.table_widget
    lyric_rows = [
        row
        for row, presentation in enumerate(win._lyrics_panel._presentation_rows)
        if presentation.kind == "lyric"
    ]
    # body 保留 4 行（含中间空行）；显示表把空行折成段落 / 分页标记。
    assert len(lyric_rows) == 3
    assert table.item(lyric_rows[0], COL_CONTENT).text() == "あ"
    assert table.item(lyric_rows[1], COL_CONTENT).text() == "いう"
    assert table.item(lyric_rows[2], COL_CONTENT).text() == "え"


# ---------------------------------------------------------------------------
# A2 / A3 视频 / 音频加载
# ---------------------------------------------------------------------------


def test_load_video_populates_preview_panel(qapp, monkeypatch, tmp_path):
    win = _make_window(qapp, monkeypatch)
    fake_info = MediaInfo(
        path=tmp_path / "bg.mp4",
        duration=120.5,
        video_streams=1,
        audio_streams=1,
        subtitle_streams=0,
        sample_rate=48000,
        channels=2,
        video_width=1920,
        video_height=1080,
        video_fps=59.94,
    )
    monkeypatch.setattr(mw, "probe_media", lambda probe, path: fake_info)

    assert not win._preview_panel.is_populated()
    result = win.load_video(tmp_path / "bg.mp4")
    assert result is fake_info
    assert win.video_info is fake_info
    assert win._video_path == tmp_path / "bg.mp4"
    assert win._preview_panel.is_populated()
    assert win._preview_panel.canvas.has_video_source
    assert win._preview_panel.canvas._duration_ms == 120_500


@pytest.mark.parametrize(
    ("width", "height"),
    [(1920, 1080), (1440, 1080), (1080, 1920), (2560, 1080)],
)
def test_preview_window_follows_non_16_9_video_without_padding(
    qapp, monkeypatch, tmp_path, width, height
):
    """导入非 16:9 视频后，预览窗口按视频比例换形，画面不再被补成 16:9。"""
    win = _make_window(qapp, monkeypatch)
    win.resize(1600, 900)
    win.show()
    fake_info = MediaInfo(
        path=tmp_path / "bg.mp4",
        duration=10.0,
        video_streams=1,
        audio_streams=0,
        subtitle_streams=0,
        video_width=width,
        video_height=height,
        video_fps=60.0,
    )
    monkeypatch.setattr(mw, "probe_media", lambda probe, path: fake_info)
    win._show_preview_window()
    qapp.processEvents()

    win.load_video(tmp_path / "bg.mp4")
    qapp.processEvents()

    # 导出画布 = 视频原始分辨率
    assert (win._export_width_spin.value(), win._export_height_spin.value()) == (
        width,
        height,
    )
    assert (
        win._preview_panel.canvas._output_width,
        win._preview_panel.canvas._output_height,
    ) == (width, height)

    # 预览画面填满预览框（没有黑边），且窗口本身就是视频比例
    frame = win._preview_window._preview_frame
    panel = win._preview_panel
    assert frame.size() == panel.size()
    assert panel.width() / panel.height() == pytest.approx(width / height, rel=0.02)
    video_area_height = win._preview_window.height() - (
        win._preview_window._TITLE_BAR_HEIGHT
    )
    assert win._preview_window.width() / video_area_height == pytest.approx(
        width / height, rel=0.02
    )
    win.close()


def test_load_video_with_missing_test_file_does_not_start_video_player(qapp, monkeypatch, tmp_path):
    """A7 稳定性：probe 已 mock 时，假路径不应启动 Qt Multimedia 后台线程。"""
    win = _make_window(qapp, monkeypatch)
    fake_info = MediaInfo(
        path=tmp_path / "bg.mp4",
        duration=10.0,
        video_streams=1,
        audio_streams=0,
        subtitle_streams=0,
        video_width=1280,
        video_height=720,
        video_fps=30.0,
    )
    monkeypatch.setattr(mw, "probe_media", lambda probe, path: fake_info)

    result = win.load_video(tmp_path / "bg.mp4")

    assert result is fake_info
    assert win._preview_panel.canvas.has_video_source
    assert win._preview_panel.canvas._video_player is None


def test_transport_playback_state_syncs_to_preview_canvas(qapp, monkeypatch):
    win = _make_window(qapp, monkeypatch)

    win._transport_bar.play()
    assert win._preview_panel.canvas._video_playing is True
    win._transport_bar.pause()

    assert win._preview_panel.canvas._video_playing is False


def test_load_video_rejects_audio_only_file(qapp, monkeypatch, tmp_path):
    win = _make_window(qapp, monkeypatch)
    fake_info = MediaInfo(
        path=tmp_path / "song.flac",
        duration=180,
        video_streams=0,
        audio_streams=1,
        subtitle_streams=0,
        sample_rate=44100,
        channels=2,
    )
    monkeypatch.setattr(mw, "probe_media", lambda probe, path: fake_info)

    result = win.load_video(tmp_path / "song.flac")
    assert result is None
    assert win.video_info is None
    assert not win._preview_panel.is_populated()


def test_load_audio_via_api_sets_audio_info(qapp, monkeypatch, tmp_path):
    """load_audio 公开 API 仍可用——给将来高级用户 / A10 嵌入工作流喂独立音频。

    UI 当前不暴露此入口（音频从视频自动取），但 API 必须保持 round-trip。
    """
    win = _make_window(qapp, monkeypatch)
    fake_info = MediaInfo(
        path=tmp_path / "song.wav",
        duration=200.0,
        video_streams=0,
        audio_streams=1,
        subtitle_streams=0,
        sample_rate=44100,
        channels=2,
    )
    monkeypatch.setattr(mw, "probe_media", lambda probe, path: fake_info)

    result = win.load_audio(tmp_path / "song.wav")
    assert result is fake_info
    assert win.audio_info is fake_info
    assert win._audio_path == tmp_path / "song.wav"


def test_load_audio_rejects_video_with_no_audio(qapp, monkeypatch, tmp_path):
    win = _make_window(qapp, monkeypatch)
    fake_info = MediaInfo(
        path=tmp_path / "silent.mp4",
        duration=60,
        video_streams=1,
        audio_streams=0,
        subtitle_streams=0,
        video_width=1920,
        video_height=1080,
        video_fps=30.0,
    )
    monkeypatch.setattr(mw, "probe_media", lambda probe, path: fake_info)

    result = win.load_audio(tmp_path / "silent.mp4")
    assert result is None
    assert win.audio_info is None


def test_load_video_with_precomputed_info_skips_probe(qapp, monkeypatch, tmp_path):
    """工作流交接路径：传入预探测结果时不再起内部 ffprobe。"""
    win = _make_window(qapp, monkeypatch)
    fake_info = MediaInfo(
        path=tmp_path / "bg.mp4",
        duration=12.0,
        video_streams=1,
        audio_streams=1,
        subtitle_streams=0,
        video_width=1280,
        video_height=720,
        video_fps=60.0,
    )

    def _fail(probe, path):  # pragma: no cover — 内部探测被调用即失败
        raise AssertionError("预探测路径不应再调用 probe_media")

    monkeypatch.setattr(mw, "probe_media", _fail)

    result = win.load_video(tmp_path / "bg.mp4", info=fake_info)

    assert result is fake_info
    assert win.video_info is fake_info
    assert win._video_path == tmp_path / "bg.mp4"


def test_load_audio_with_precomputed_info_skips_probe(qapp, monkeypatch, tmp_path):
    win = _make_window(qapp, monkeypatch)
    fake_info = MediaInfo(
        path=tmp_path / "song.flac",
        duration=200.0,
        video_streams=0,
        audio_streams=1,
        subtitle_streams=0,
        sample_rate=44100,
        channels=2,
    )

    def _fail(probe, path):  # pragma: no cover — 内部探测被调用即失败
        raise AssertionError("预探测路径不应再调用 probe_media")

    monkeypatch.setattr(mw, "probe_media", _fail)

    result = win.load_audio(tmp_path / "song.flac", info=fake_info)

    assert result is fake_info
    assert win.audio_info is fake_info


def test_load_media_async_probes_in_background_then_loads(
    qapp, monkeypatch, tmp_path
):
    """「进入下一步」交接：探测在后台线程完成，UI 线程只做加载，且只探测一次。"""
    win = _make_window(qapp, monkeypatch)
    fake_info = MediaInfo(
        path=tmp_path / "bg.mp4",
        duration=12.0,
        video_streams=1,
        audio_streams=1,
        subtitle_streams=0,
        video_width=1280,
        video_height=720,
        video_fps=60.0,
    )
    probe_calls: list[Path] = []
    monkeypatch.setattr(
        mw, "probe_media", lambda probe, path: probe_calls.append(path) or fake_info
    )

    win.load_media_async(tmp_path / "bg.mp4", as_video=True)
    deadline = time.monotonic() + 5.0
    while win.video_info is not fake_info and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)

    assert win.video_info is fake_info
    assert win._video_path == tmp_path / "bg.mp4"
    # 后台探测一次，load_video 拿预探测结果不再探测。
    assert probe_calls == [tmp_path / "bg.mp4"]


def test_load_media_async_probe_failure_falls_back_to_sync_load(
    qapp, monkeypatch, tmp_path
):
    """后台探测失败时回退同步加载，沿用 _probe 的错误弹窗语义。"""
    win = _make_window(qapp, monkeypatch)

    def _boom(probe, path):
        raise RuntimeError("probe failed")

    monkeypatch.setattr(mw, "probe_media", _boom)
    errors: list[tuple[str, str]] = []
    monkeypatch.setattr(
        mw, "fluent_error", lambda parent, title, content: errors.append((title, content))
    )

    win.load_media_async(tmp_path / "bg.mp4", as_video=True)
    deadline = time.monotonic() + 5.0
    while not errors and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)

    assert win.video_info is None
    assert errors and errors[0][0] == "加载视频失败"


def test_load_video_auto_loads_audio_from_same_file(qapp, monkeypatch, tmp_path):
    """新增 A7 后行为：视频含音频流时，load_video 自动把视频路径喂给 TransportBar。"""
    win = _make_window(qapp, monkeypatch)
    fake_info = MediaInfo(
        path=tmp_path / "bg.mp4",
        duration=60,
        video_streams=1,
        audio_streams=1,
        subtitle_streams=0,
        sample_rate=48000,
        channels=2,
        video_width=1920,
        video_height=1080,
        video_fps=60.0,
    )
    monkeypatch.setattr(mw, "probe_media", lambda probe, path: fake_info)

    win.load_video(tmp_path / "bg.mp4")
    # audio_path / audio_info 应该同步指向视频文件
    assert win._audio_path == tmp_path / "bg.mp4"
    assert win.audio_info is fake_info


def test_load_video_without_audio_stream_keeps_audio_unset(qapp, monkeypatch, tmp_path):
    """视频无音频流时不应错误地把视频路径设为 audio_source。"""
    win = _make_window(qapp, monkeypatch)
    fake_info = MediaInfo(
        path=tmp_path / "silent.mp4",
        duration=60,
        video_streams=1,
        audio_streams=0,
        subtitle_streams=0,
        video_width=1280,
        video_height=720,
        video_fps=30.0,
    )
    monkeypatch.setattr(mw, "probe_media", lambda probe, path: fake_info)

    win.load_video(tmp_path / "silent.mp4")
    assert win._audio_path is None
    assert win.audio_info is None


def test_static_sequence_and_solid_backgrounds_populate_preview(qapp, monkeypatch, tmp_path):
    win = _make_window(qapp, monkeypatch)
    image = tmp_path / "frame_0000.png"
    frame = QImage(64, 36, QImage.Format.Format_RGB32)
    frame.fill(QColor("#123456"))
    assert frame.save(str(image))

    assert win.load_background_image(image) is True
    assert win._background_source.kind == "image"
    assert win._preview_panel.is_populated()

    assert win.load_background_sequence(image, 24) is True
    assert win._background_source.kind == "image_sequence"
    assert win._background_source.path.endswith("frame_%04d.png")
    assert win._background_source.source_fps == 24
    assert win._background_source.sequence_start_number == 0

    win.set_solid_background("#654321")
    assert win._background_source.kind == "solid"
    assert win._background_source.color == "#654321"


def test_browse_background_sequence_uses_fluent_fps_input(
    qapp, monkeypatch, tmp_path
):
    win = _make_window(qapp, monkeypatch)
    image = tmp_path / "frame_0000.png"
    frame = QImage(64, 36, QImage.Format.Format_RGB32)
    frame.fill(QColor("#123456"))
    assert frame.save(str(image))
    monkeypatch.setattr(
        mw.QFileDialog,
        "getOpenFileName",
        lambda *_args, **_kwargs: (str(image), ""),
    )
    captured: dict[str, object] = {}

    def get_int(parent, title, label, **kwargs):
        captured.update(parent=parent, title=title, label=label, kwargs=kwargs)
        return 24, True

    monkeypatch.setattr(mw, "fluent_get_int", get_int)
    expected_default_fps = win._screen_settings.fps
    win._browse_background_sequence()

    assert captured == {
        "parent": win,
        "title": "图片序列帧率",
        "label": "源帧率（每秒图片数）",
        "kwargs": {
            "value": expected_default_fps,
            "minimum": 1,
            "maximum": 240,
        },
    }
    assert win._background_source.kind == "image_sequence"
    assert win._background_source.source_fps == 24


def test_build_render_job_uses_independent_audio_with_static_background(
    qapp, monkeypatch, tmp_path
):
    win = _make_window(qapp, monkeypatch)
    lrc = tmp_path / "lyrics.lrc"
    lrc.write_text("[00:00:00]a[00:01:00]\n", encoding="utf-8")
    win.load_from_lrc(lrc)
    win.set_solid_background("#000000")

    audio = tmp_path / "song.wav"
    audio.write_bytes(b"fake")
    audio_info = MediaInfo(
        path=audio, duration=5.0, video_streams=0, audio_streams=1,
        subtitle_streams=0, sample_rate=48000, channels=2,
    )
    monkeypatch.setattr(mw, "probe_media", lambda probe, path: audio_info)
    win.load_audio(audio)
    win._export_dir_edit.setText(str(tmp_path))
    win._export_name_edit.setText("out")

    job = win._build_render_job()

    assert job.background_video_path is None
    assert job.background_source.kind == "solid"
    assert job.audio_path == audio
    assert job.include_audio is True
    assert job.duration_ms == 5000


def test_video_background_rejects_independent_audio(qapp, monkeypatch, tmp_path):
    win = _make_window(qapp, monkeypatch)
    video = tmp_path / "bg.mp4"
    audio = tmp_path / "song.wav"
    video_info = MediaInfo(
        path=video, duration=5.0, video_streams=1, audio_streams=1,
        subtitle_streams=0, sample_rate=48000, channels=2,
        video_width=320, video_height=180, video_fps=60.0,
    )
    audio_info = MediaInfo(
        path=audio, duration=5.0, video_streams=0, audio_streams=1,
        subtitle_streams=0, sample_rate=48000, channels=2,
    )
    monkeypatch.setattr(
        mw, "probe_media", lambda probe, path: video_info if path == video else audio_info
    )

    win.load_video(video)
    assert win.load_audio(audio) is None
    assert win._audio_path == video
    assert all(not action.isEnabled() for action in win._audio_menu_actions)


def test_switching_to_video_clears_existing_independent_audio(
    qapp, monkeypatch, tmp_path
):
    win = _make_window(qapp, monkeypatch)
    audio = tmp_path / "song.wav"
    video = tmp_path / "silent.mp4"
    audio_info = MediaInfo(
        path=audio, duration=5.0, video_streams=0, audio_streams=1,
        subtitle_streams=0, sample_rate=48000, channels=2,
    )
    video_info = MediaInfo(
        path=video, duration=5.0, video_streams=1, audio_streams=0,
        subtitle_streams=0, video_width=320, video_height=180, video_fps=60.0,
    )
    monkeypatch.setattr(
        mw, "probe_media", lambda probe, path: audio_info if path == audio else video_info
    )

    win.set_solid_background("#000000")
    assert win.load_audio(audio) is audio_info
    win.load_video(video)

    assert win._audio_path is None
    assert win._audio_info is None


def test_subtitle_and_video_panels_can_coexist(qapp, monkeypatch, tmp_path):
    win = _make_window(qapp, monkeypatch)

    # 字幕
    lrc = tmp_path / "lyrics.lrc"
    lrc.write_bytes(
        b"\xef\xbb\xbf"
        + "[00:01:00]a[00:01:50]b[00:02:00]\r\n\r\n@Title=Test\r\n".encode("utf-8")
    )
    win.load_from_lrc(lrc)

    # 视频（带音频流）
    video_info = MediaInfo(
        path=tmp_path / "bg.mp4",
        duration=60,
        video_streams=1,
        audio_streams=1,
        subtitle_streams=0,
        sample_rate=48000,
        channels=2,
        video_width=1920,
        video_height=1080,
        video_fps=60.0,
    )
    monkeypatch.setattr(mw, "probe_media", lambda probe, path: video_info)
    win.load_video(tmp_path / "bg.mp4")

    assert win._lyrics_panel.is_populated()
    assert win._preview_panel.is_populated()
    # 音频自动来自视频
    assert win.audio_info is video_info


def test_export_tab_builds_render_job_from_loaded_media(qapp, monkeypatch, tmp_path):
    win = _make_window(qapp, monkeypatch)

    lrc = tmp_path / "lyrics.lrc"
    lrc.write_bytes(
        b"\xef\xbb\xbf"
        + "[00:01:00]a[00:01:50]b[00:02:00]\r\n\r\n@Title=Test\r\n".encode("utf-8")
    )
    win.load_from_lrc(lrc)

    video_info = MediaInfo(
        path=tmp_path / "bg.mp4",
        duration=5.0,
        video_streams=1,
        audio_streams=1,
        subtitle_streams=0,
        video_width=1920,
        video_height=1080,
        video_fps=60.0,
    )
    monkeypatch.setattr(mw, "probe_media", lambda probe, path: video_info)
    video = tmp_path / "bg.mp4"
    win.load_video(video)

    output = tmp_path / "custom.mp4"
    win._export_dir_edit.setText(str(tmp_path))
    win._export_name_edit.setText("custom")
    win._export_width_spin.setValue(1280)
    win._export_height_spin.setValue(720)
    win._export_fps_combo.setCurrentIndex(win._export_fps_combo.findData(120))
    win._export_encoder_combo.setCurrentIndex(win._export_encoder_combo.findData("nvenc"))
    win._export_preset_combo.setCurrentText("slow")
    win._export_crf_spin.setValue(23)
    win._export_render_workers_combo.setCurrentIndex(
        win._export_render_workers_combo.findData(16)
    )
    blocked = win._export_native_check.blockSignals(True)
    win._export_native_check.setChecked(True)
    win._export_native_check.blockSignals(blocked)
    job = win._build_render_job()

    assert job.background_video_path == video
    assert job.output_path == output
    assert job.width == 1280
    assert job.height == 720
    assert job.fps == 120
    assert job.duration_ms == 5000
    assert job.include_audio is True
    assert job.encoder_mode == "nvenc"
    assert job.preset == "slow"
    assert job.crf == 23
    assert job.render_workers == 16
    assert job.native_export_enabled is False
    # fe4eb4b 起 Windows 默认走 GPU 字幕导出（AGENTS.md §9），其余平台仍关闭。
    assert job.gpu_export_enabled is (sys.platform == "win32")


def test_export_output_prefills_dir_and_yurika_name(qapp, monkeypatch, tmp_path):
    win = _make_window(qapp, monkeypatch)
    win._set_export_directory_settings(
        mw.EXPORT_DIR_SOURCE_VIDEO,
        "",
        persist=False,
    )

    def _info(path):
        return MediaInfo(
            path=path, duration=5.0, video_streams=1, audio_streams=0,
            subtitle_streams=0, video_width=320, video_height=180, video_fps=60.0,
        )

    monkeypatch.setattr(mw, "probe_media", lambda probe, path: _info(path))

    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    win.load_video(first_dir / "Dark spiral journey.mp4")
    assert win._export_dir_edit.text() == str(first_dir)
    assert win._export_name_edit.text() == "Dark spiral journey_yurika出力"

    # 从预览区的真实信号入口换视频，也会同时更新来源目录与输出文件名。
    win._preview_panel.pathDropped.emit(second_dir / "second.mp4")
    assert win._export_dir_edit.text() == str(second_dir)
    assert win._export_name_edit.text() == "second_yurika出力"

    # 即使旧项目手工改过文件名，主动换视频也要按新视频重新命名。
    win._export_name_edit.setText("my custom")
    win.load_video(tmp_path / "third.mp4")
    assert win._export_name_edit.text() == "third_yurika出力"

    # 固定导出文件夹是应用偏好；换视频只更新文件名，不覆盖该目录。
    fixed_dir = tmp_path / "fixed-exports"
    win._set_export_directory_settings(
        mw.EXPORT_DIR_CUSTOM,
        str(fixed_dir),
        persist=False,
    )
    win.load_video(tmp_path / "fourth.mp4")
    assert win._export_dir_edit.text() == str(fixed_dir)
    assert win._export_name_edit.text() == "fourth_yurika出力"


def test_dropped_video_syncs_export_before_preview_window_request(
    qapp, monkeypatch, tmp_path
):
    """预览窗口链路不能成为导出页同步的前置条件。"""
    win = _make_window(qapp, monkeypatch)
    win._set_export_directory_settings(
        mw.EXPORT_DIR_SOURCE_VIDEO,
        "",
        persist=False,
    )
    video = tmp_path / "new" / "replacement.mp4"
    video.parent.mkdir()
    video.write_bytes(b"fake")
    monkeypatch.setattr(
        mw,
        "probe_media",
        lambda probe, path: MediaInfo(
            path=path,
            duration=5.0,
            video_streams=1,
            audio_streams=0,
            subtitle_streams=0,
            video_width=320,
            video_height=180,
            video_fps=60.0,
        ),
    )

    export_state_at_preview_request = []
    monkeypatch.setattr(
        win,
        "_request_preview_window",
        lambda: export_state_at_preview_request.append(
            (win._export_dir_edit.text(), win._export_name_edit.text())
        ),
    )

    win._preview_panel.pathDropped.emit(video)

    assert export_state_at_preview_request == [
        (str(video.parent), "replacement_yurika出力")
    ]


def test_loading_project_video_keeps_saved_output_name(qapp, monkeypatch, tmp_path):
    win = _make_window(qapp, monkeypatch)
    video = tmp_path / "source.mp4"
    video.write_bytes(b"fake")
    win._export_name_edit.setText("工程保存的输出名")
    win._export_auto_name = "工程保存的输出名"

    monkeypatch.setattr(
        mw,
        "probe_media",
        lambda probe, path: MediaInfo(
            path=path,
            duration=5.0,
            video_streams=1,
            audio_streams=0,
            subtitle_streams=0,
            video_width=1920,
            video_height=1080,
            video_fps=60.0,
        ),
    )

    win._apply_project_data(
        {
            "background": {
                "kind": "video",
                "path": str(video),
                "color": "#000000",
            },
            "output": {
                "output_path": str(tmp_path / "工程保存的输出名.mp4"),
            },
        }
    )

    assert win._export_name_edit.text() == "工程保存的输出名"


def _install_active_render(win):
    class FakeThread:
        def isRunning(self):
            return True

    class FakeWorker:
        def __init__(self):
            self.cancel_called = False

        def cancel(self):
            self.cancel_called = True

    worker = FakeWorker()
    win._render_thread = FakeThread()
    win._render_worker = worker
    win._export_stop_button.setEnabled(True)
    return worker


def test_stop_render_export_confirms_before_requesting_cancel(qapp, monkeypatch):
    win = _make_window(qapp, monkeypatch)
    worker = _install_active_render(win)
    questions = []

    def confirm(*args, **kwargs):
        questions.append((args, kwargs))
        return True

    monkeypatch.setattr(mw, "fluent_question", confirm)

    win._stop_render_export()

    assert len(questions) == 1
    args, kwargs = questions[0]
    assert args[0] is win
    assert args[1] == "停止导出"
    assert "未完成文件" in args[2]
    assert kwargs == {
        "yes_text": "停止导出",
        "no_text": "继续导出",
        "default_cancel": True,
    }
    assert worker.cancel_called is True
    assert win._export_stop_button.isEnabled() is False
    assert "停止导出" in win._export_status_label.text()


def test_stop_render_export_keeps_running_when_confirmation_is_rejected(
    qapp, monkeypatch
):
    win = _make_window(qapp, monkeypatch)
    worker = _install_active_render(win)
    win._export_status_label.setText("正在导出… 10/100 帧")
    monkeypatch.setattr(mw, "fluent_question", lambda *args, **kwargs: False)

    win._stop_render_export()

    assert worker.cancel_called is False
    assert win._export_stop_button.isEnabled() is True
    assert win._export_status_label.text() == "正在导出… 10/100 帧"


def test_render_log_does_not_flash_ffmpeg_command_in_status(qapp, monkeypatch):
    win = _make_window(qapp, monkeypatch)
    # 页面隐藏期间 log 会转为 pending 攒批（见 background_throttle），
    # 这两个用例验证的是可见路径的文本行为。
    win.show()
    qapp.processEvents()
    win._export_status_label.setText("正在准备导出…")

    win._on_render_log("执行命令:")
    assert win._export_status_label.text() == "正在准备导出…"

    win._on_render_log("ffmpeg -y -c:v h264_nvenc output.mp4")
    assert win._export_status_label.text() == "正在准备导出…"

    win._on_render_log("多进程导出: 8 个 worker")
    assert win._export_status_label.text() == "多进程导出: 8 个 worker"


def test_render_log_does_not_flash_late_sei_warning_in_status(qapp, monkeypatch):
    win = _make_window(qapp, monkeypatch)
    win.show()
    qapp.processEvents()
    progress = "正在导出… 3002/10079 帧"
    win._export_status_label.setText(progress)

    win._on_render_log(
        "[h264 @ 000001b8440d22c0] Late SEI is not implemented. "
        "Update your FFmpeg version to the newest one from Git."
    )
    assert win._export_status_label.text() == progress

    win._on_render_log(
        "[h264 @ 000001b8440d22c0] If you want to help, upload a sample of this file "
        "to https://streams.videolan.org/upload/ and contact the ffmpeg-devel mailing list."
    )
    assert win._export_status_label.text() == progress


# ---------------------------------------------------------------------------
# 布局完整性
# ---------------------------------------------------------------------------


def test_window_shell_components_present(qapp, monkeypatch):
    win = _make_window(qapp, monkeypatch)

    # 顶部命令栏中的工作区导航 + stack
    assert win._nav_btns is not None
    assert len(win._nav_btns) == 2
    assert win._stack.count() == 2

    # 四区 widget 已挂载
    assert win._lyrics_panel is not None
    assert win._preview_panel is not None
    assert win._transport_bar is not None
    assert win._property_panel is not None
    assert win._tracks_view is not None
    assert isinstance(win._preview_tab, mw.PreviewWorkspaceView)
    preview_controls = win._preview_tab.controls
    assert preview_controls.preview_panel is win._preview_panel
    assert preview_controls.lyrics_panel is win._lyrics_panel
    assert preview_controls.property_panel is win._property_panel
    assert preview_controls.tracks_view is win._tracks_view
    assert win._export_start_button is not None
    assert win._export_stop_button is not None
    assert win._export_stop_button.isEnabled() is False
    assert win._export_encoder_combo is not None
    assert win._export_preset_combo is not None
    assert win._export_crf_spin.value() == 18
    assert win._export_native_check is not None
    assert isinstance(win._bottom_navigation, WorkspaceSwitcher)
    assert isinstance(win._export_dir_edit, LineEdit)
    assert isinstance(win._export_name_edit, LineEdit)
    assert isinstance(win._export_codec_combo, ComboBox)
    assert win._export_codec_combo.currentData() == "h264"
    assert isinstance(win._export_encoder_combo, ComboBox)
    assert isinstance(win._export_crf_spin, SpinBox)
    assert isinstance(win._export_progress, ProgressBar)
    assert isinstance(win._export_start_button, PrimaryPushButton)
    assert isinstance(win._export_stop_button, PushButton)
    assert isinstance(win._lyrics_panel._add_source_btn, TransparentToolButton)
    assert isinstance(win._lyrics_panel._remove_source_btn, TransparentToolButton)

    # 属性面板 6 个分类页（顶部 segmented 导航，页面用 accessibleName 标注）
    assert win._property_panel.count() == 6
    assert [win._property_panel.widget(i).accessibleName() for i in range(6)] == [
        "角色",
        "布局",
        "时间",
        "特效",
        "标题",
        "背景/音频",
    ]


def test_workspace_navigation_switches_export_and_back_to_preview(qapp, monkeypatch):
    win = _make_window(qapp, monkeypatch)

    win._nav_btns["export"].click()
    qapp.processEvents()
    assert win._stack.currentWidget() is win._export_tab
    assert win._bottom_navigation.currentRouteKey() == "export"

    win._nav_btns["preview"].click()
    qapp.processEvents()
    assert win._stack.currentWidget() is win._preview_tab
    assert win._bottom_navigation.currentRouteKey() == "preview"


def test_preview_window_button_is_anchored_to_preview_tab_only(qapp, monkeypatch):
    win = _make_window(qapp, monkeypatch)
    win.resize(1280, 720)
    win._video_settings_panel.set_populated(True)
    win.show()
    qapp.processEvents()

    button = win._show_preview_btn
    font_tab = win._property_panel._navigation.widget("font")
    assert button.parentWidget() is win._property_panel._navigation_row
    assert win._preview_tab.isAncestorOf(button)
    assert not win._export_tab.isAncestorOf(button)
    assert button.isVisible() is True
    assert button.height() == font_tab.height()
    font_tab_top = font_tab.mapTo(
        win._property_panel._navigation_row,
        QPoint(0, 0),
    ).y()
    assert button.geometry().top() == font_tab_top
    right_margin = (
        win._property_panel._navigation_layout.contentsMargins().right()
    )
    assert (
        button.geometry().right()
        == win._property_panel._navigation_row.width() - right_margin - 1
    )

    win._nav_btns["export"].click()
    qapp.processEvents()
    assert button.isVisible() is False

    win._nav_btns["preview"].click()
    qapp.processEvents()
    assert button.isVisible() is True
    win.close()


def test_layout_issue_button_lists_and_jumps_to_problem_line(qapp, monkeypatch):
    win = _make_window(qapp, monkeypatch)
    track = TimingTrack(
        lines=[
            TimingLine(chars=[TimingChar(text="短句", start_ms=1_000)], end_ms=2_000),
            TimingLine(
                chars=[TimingChar(text="这是一句超长歌词", start_ms=4_200)],
                end_ms=5_000,
            ),
        ]
    )
    warning = mw.LayoutMarginWarning(
        line_index=1,
        text="这是一句超长歌词",
        level="overflow",
        left=-20,
        right=1940,
    )
    monkeypatch.setattr(
        mw,
        "check_layout_margins",
        lambda _track, _style, _width: [warning],
    )
    monkeypatch.setattr(
        mw,
        "layout_timing_diagnostics_for_style",
        lambda *_args: [],
    )
    toast_calls: list[dict] = []
    monkeypatch.setattr(
        mw.InfoBar,
        "warning",
        lambda **kwargs: toast_calls.append(kwargs),
    )
    preview_calls: list[bool] = []
    monkeypatch.setattr(
        win,
        "_show_preview_window",
        lambda: preview_calls.append(True),
    )

    win._apply_timing_track(track, None)
    win._check_layout_margins()
    win.resize(1280, 720)
    win._video_settings_panel.set_populated(True)
    win.show()
    qapp.processEvents()

    assert toast_calls
    assert win._layout_issues_button.isVisible() is True
    assert (
        win._layout_issues_button.geometry().right()
        < win._show_preview_btn.geometry().left()
    )
    assert win._layout_issues_button.toolTip() == "当前字幕诊断（1 条）"

    win._show_layout_issues()
    qapp.processEvents()
    dialog = win._layout_issues_dialog
    assert dialog is not None
    assert dialog._list_widget.count() == 1
    item = dialog._list_widget.item(0)
    assert "主字幕 · 第 2 行" in item.text()
    assert "字幕溢出画面" in item.text()
    assert "测得范围" in dialog._detail.toPlainText()

    dialog._list_widget.itemClicked.emit(item)
    qapp.processEvents()
    assert (
        win._lyrics_panel.table_widget.currentRow()
        == win._lyrics_panel._display_row_for_track_line(1)
    )
    assert win._transport_bar.current_time_ms == 4_200
    assert preview_calls == [True]

    timing = mw.LayoutTimingDiagnostic(
        kind="timing",
        line_indices=(1,),
        title="时间窗口自动压缩",
        summary="第 2 行退场被提前",
        detail="基础自动窗口：00:03.000 – 00:06.000\n最终消费窗口：00:03.000 – 00:05.100",
    )
    monkeypatch.setattr(
        mw,
        "check_layout_margins",
        lambda _track, _style, _width: [],
    )
    monkeypatch.setattr(
        mw,
        "layout_timing_diagnostics_for_style",
        lambda *_args: [timing],
    )
    win._check_layout_margins()
    assert win._layout_issues_button.toolTip() == "当前字幕诊断（1 条）"
    assert dialog._list_widget.count() == 1
    assert "时间窗口自动压缩" in dialog._list_widget.item(0).text()
    assert "最终消费窗口" in dialog._detail.toPlainText()

    monkeypatch.setattr(
        mw,
        "check_layout_margins",
        lambda _track, _style, _width: [],
    )
    monkeypatch.setattr(
        mw,
        "layout_timing_diagnostics_for_style",
        lambda *_args: [],
    )
    win._check_layout_margins()
    assert win._layout_issues_button.isHidden() is True
    assert dialog._summary_label.text().startswith("未发现字幕布局或时间问题")
    win.close()


def test_timeline_line_selection_switches_source_and_selects_row(
    qapp, monkeypatch, tmp_path
):
    """点击底部字幕轨道的句子块 → 歌词列表切到对应源并选中该行。"""
    win = _make_window(qapp, monkeypatch)
    win._apply_timing_track(
        TimingTrack(
            lines=[
                TimingLine(chars=[TimingChar(text="あ", start_ms=1_000)], end_ms=2_000),
                TimingLine(is_blank=True),
                TimingLine(chars=[TimingChar(text="け", start_ms=3_500)], end_ms=4_500),
            ]
        ),
        None,
    )
    extra_track = TimingTrack(
        lines=[
            TimingLine(chars=[TimingChar(text="こ", start_ms=1_200)], end_ms=2_000),
            TimingLine(chars=[TimingChar(text="そ", start_ms=3_000)], end_ms=4_000),
        ]
    )
    win._extra_sources = [
        mw.ExtraSubtitleSource(
            name="コーラス", path=tmp_path / "extra.lrc", track=extra_track
        )
    ]
    win._refresh_source_ui()
    win._sync_tracks_view()

    # 同源（主字幕）选中：不切源，直接选中并滚动到该行（中间隔空行 → 行 2）
    win._tracks_view.lineSelected.emit(0, 2)
    assert win._lyrics_panel.current_source_index() == 0
    assert win._lyrics_panel.table_widget.currentRow() == (
        win._lyrics_panel._display_row_for_track_line(2)
    )

    # 副字幕源（轨道 1）：切源后选中该轨的行
    win._tracks_view.lineSelected.emit(1, 1)
    assert win._lyrics_panel.current_source_index() == 1
    extra_row = win._lyrics_panel._display_row_for_track_line(1)
    assert extra_row is not None
    assert win._lyrics_panel.table_widget.currentRow() == extra_row

    # 越界行号静默忽略，不抛异常也不改选区
    win._tracks_view.lineSelected.emit(0, 99)
    assert win._lyrics_panel.current_source_index() == 1
    win.close()


def test_playback_shortcut_is_disabled_outside_preview_tab(qapp, monkeypatch):
    win = _make_window(qapp, monkeypatch)
    toggles: list[bool] = []
    monkeypatch.setattr(
        win._transport_bar, "toggle_play", lambda: toggles.append(True)
    )
    assert win._space_shortcut.isEnabled() is True

    win._nav_btns["export"].click()
    qapp.processEvents()

    assert win._space_shortcut.isEnabled() is False
    win._space_shortcut.activated.emit()
    assert toggles == []

    win._nav_btns["preview"].click()
    qapp.processEvents()
    assert win._space_shortcut.isEnabled() is True
    win._space_shortcut.activated.emit()
    assert toggles == [True]


def test_preview_window_is_visible_only_on_preview_tab_and_pauses(
    qapp, monkeypatch
):
    win = _make_window(qapp, monkeypatch)
    win.resize(1280, 720)
    win.show()
    qapp.processEvents()
    paused: list[bool] = []
    monkeypatch.setattr(win._transport_bar, "pause", lambda: paused.append(True))
    win._transport_bar.set_time(1234)

    win._show_preview_window()
    qapp.processEvents()
    preview_geometry = win._preview_window.geometry()
    assert win._preview_window.isVisible() is True

    win._nav_btns["export"].click()
    qapp.processEvents()
    assert win._preview_window.isVisible() is False
    assert paused
    assert win._transport_bar.current_time_ms == 1234

    win._nav_btns["preview"].click()
    qapp.processEvents()
    assert win._preview_window.isVisible() is True
    assert win._preview_window.geometry() == preview_geometry
    assert win._transport_bar.current_time_ms == 1234
    win.close()


def test_preview_window_hides_across_workflow_visibility_and_restores_paused(
    qapp, monkeypatch
):
    win = _make_window(qapp, monkeypatch)
    win.show()
    qapp.processEvents()
    paused: list[bool] = []
    monkeypatch.setattr(win._transport_bar, "pause", lambda: paused.append(True))
    win._show_preview_window()
    qapp.processEvents()
    assert win._preview_window.isVisible() is True

    win.hide()
    qapp.processEvents()
    assert win._preview_window.isVisible() is False
    assert paused

    win.show()
    qapp.processEvents()
    assert win._preview_window.isVisible() is True
    win.close()


def test_user_closed_preview_does_not_reopen_after_context_switch(qapp, monkeypatch):
    win = _make_window(qapp, monkeypatch)
    win.show()
    qapp.processEvents()
    win._show_preview_window()
    qapp.processEvents()

    win._preview_window._close_button.click()
    qapp.processEvents()
    assert win._preview_window_requested is False
    assert win._preview_window.isVisible() is False

    win._nav_btns["export"].click()
    win._nav_btns["preview"].click()
    qapp.processEvents()
    assert win._preview_window.isVisible() is False
    win.close()


def test_preview_request_is_deferred_while_hidden_or_exporting(qapp, monkeypatch):
    win = _make_window(qapp, monkeypatch)
    win._request_preview_window()
    assert win._preview_window.isVisible() is False

    win.show()
    qapp.processEvents()
    assert win._preview_window.isVisible() is True

    win._render_thread = object()
    win._sync_preview_window_visibility()
    qapp.processEvents()
    assert win._preview_window.isVisible() is False

    win._render_thread = None
    win._sync_preview_window_visibility()
    qapp.processEvents()
    assert win._preview_window.isVisible() is True
    win.close()


def test_lyrics_list_centers_only_explicit_single_line_page(qapp, monkeypatch):
    win = _make_window(qapp, monkeypatch)
    lines = [
        TimingLine(chars=[TimingChar(text=text, start_ms=i * 1000)], end_ms=(i + 1) * 1000)
        for i, text in enumerate(("感傷", "哀しみ", "真っ白", "わたし"))
    ]
    lines[2].break_before = "page"
    lines[3].break_before = "paragraph"
    win._lyrics_panel.set_track(TimingTrack(lines=lines))

    second_alignment = win._lyrics_panel._table.item(1, COL_CONTENT).textAlignment()
    single_alignment = win._lyrics_panel._table.item(2, COL_CONTENT).textAlignment()
    assert second_alignment & Qt.AlignmentFlag.AlignRight
    assert single_alignment & Qt.AlignmentFlag.AlignHCenter


def test_preview_window_corners_stay_above_player_controls(qapp, monkeypatch):
    """控制栏显示后，四角仍须命中无边框窗口的 resize grip。"""
    win = _make_window(qapp, monkeypatch)
    preview = win._preview_window
    preview.resize(800, 450)
    preview.show()
    qapp.processEvents()
    preview.show_controls()

    edge = mw.Qt.Edge
    expected = {
        QPoint(2, 2): (edge.TopEdge | edge.LeftEdge).value,
        QPoint(preview.width() - 3, 2): (edge.TopEdge | edge.RightEdge).value,
        QPoint(2, preview.height() - 3): (edge.BottomEdge | edge.LeftEdge).value,
        QPoint(preview.width() - 3, preview.height() - 3): (
            edge.BottomEdge | edge.RightEdge
        ).value,
    }
    for point, edge_bits in expected.items():
        hit = preview.childAt(point)
        assert isinstance(hit, mw._WindowEdgeGrip)
        assert hit._edge_bits == edge_bits


def test_preview_window_resizes_from_all_four_corners(qapp, monkeypatch):
    win = _make_window(qapp, monkeypatch)
    preview = win._preview_window
    preview.show()
    qapp.processEvents()

    edge = Qt.Edge
    cases = [
        (edge.TopEdge | edge.LeftEdge, -30, -20),
        (edge.TopEdge | edge.RightEdge, 30, -20),
        (edge.BottomEdge | edge.LeftEdge, -30, 20),
        (edge.BottomEdge | edge.RightEdge, 30, 20),
    ]
    for edges, dx, dy in cases:
        preview.setGeometry(100, 100, 800, 450)
        qapp.processEvents()
        grip = next(g for g in preview._edge_grips if g._edge_bits == edges.value)
        QTest.mousePress(grip, Qt.MouseButton.LeftButton, pos=QPoint(7, 7))
        QTest.mouseMove(grip, QPoint(7 + dx, 7 + dy))
        QTest.mouseRelease(
            grip,
            Qt.MouseButton.LeftButton,
            pos=QPoint(7 + dx, 7 + dy),
        )
        qapp.processEvents()

        assert preview.width() == 830
        assert preview.height() == 470


def test_drop_panel_accepts_correct_extensions(qapp, monkeypatch, tmp_path):
    """歌词 / 预览两个拖拽面板的扩展名校验。"""
    win = _make_window(qapp, monkeypatch)

    lrc = tmp_path / "x.lrc"
    lrc.write_text("[00:00:00]a[00:00:50]", encoding="utf-8")
    mp4 = tmp_path / "x.mp4"
    mp4.write_bytes(b"\x00")
    png = tmp_path / "x.png"
    png.write_bytes(b"\x00")

    assert win._lyrics_panel.accepts(lrc) is True
    assert win._lyrics_panel.accepts(mp4) is False

    assert win._preview_panel.accepts(mp4) is True
    assert win._preview_panel.accepts(png) is True
    assert win._preview_panel.accepts(lrc) is False

    actions = [
        win._preview_panel._empty_actions.itemAt(i).widget().text()
        for i in range(win._preview_panel._empty_actions.count())
    ]
    assert actions == ["视频", "静态图", "图片序列", "纯色"]


def test_preview_workspace_background_actions_reach_coordinator(qapp, monkeypatch):
    calls = []
    handlers = {
        "_browse_video": "video",
        "_browse_background_image": "image",
        "_browse_background_sequence": "sequence",
        "_choose_solid_background": "solid",
    }
    for method_name, call_name in handlers.items():
        monkeypatch.setattr(
            mw.SubtitleRenderWindow,
            method_name,
            lambda self, name=call_name: calls.append(name),
        )

    win = _make_window(qapp, monkeypatch)
    try:
        for panel in (win._preview_panel, win._video_settings_panel):
            for index in range(panel._empty_actions.count()):
                panel._empty_actions.itemAt(index).widget().click()

        assert calls == [
            "video",
            "image",
            "sequence",
            "solid",
            "video",
            "image",
            "sequence",
            "solid",
        ]
    finally:
        win.close()
