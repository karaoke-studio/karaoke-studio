"""按括号自动识别和声段。

对标 NicoKaraMaker3 ``PartFontSelector`` 的 ``AutoChorus``；这里钉住的四条边界
行为都是从 ``AnalyzeAutoChorus`` 逆向来的，改算法时不能悄悄跑偏。
"""

from __future__ import annotations

import pytest

from krok_helper.subtitle_render.engine.timing.auto_chorus import (
    DEFAULT_CHORUS_BEGIN_CHARS,
    DEFAULT_CHORUS_END_CHARS,
    apply_chorus_roles,
    detect_chorus_spans,
    pick_chorus_role,
)


def _chars(text: str) -> list[str]:
    return list(text)


def test_a_bracketed_run_is_detected_with_the_brackets_included() -> None:
    """括号自己也算在和声段里 —— N3 的 for 循环是 num..i 闭区间。"""
    spans = detect_chorus_spans(_chars("駅前で（僕は窓辺で）"))

    # 駅0 前1 で2 （3 … ）9
    assert [(s.start, s.end) for s in spans] == [(3, 9)]
    assert list(spans[0].indices()) == list(range(3, 10))


def test_half_width_brackets_count_too() -> None:
    """起止是**字符集合**不是字符串，全角半角混用也认。"""
    spans = detect_chorus_spans(_chars("abc(def)"))

    assert [(s.start, s.end) for s in spans] == [(3, 7)]


def test_a_mixed_pair_still_closes() -> None:
    """开全角、收半角 —— 集合语义下这是合法的一段。"""
    spans = detect_chorus_spans(_chars("あ（い)う"))

    assert [(s.start, s.end) for s in spans] == [(1, 3)]


def test_several_runs_in_one_line() -> None:
    """收口后从下一个字符接着扫。"""
    spans = detect_chorus_spans(_chars("（あ）い（う）"))

    assert [(s.start, s.end) for s in spans] == [(0, 2), (4, 6)]


def test_an_unclosed_bracket_yields_nothing() -> None:
    """一行里的括号没闭合就整行不标。

    N3 在这里是 ``break``（放弃本行剩下的部分）而不是继续往后找。两者其实等价：
    第一个起始括号之后既然一个结束字符都没有，后面任何起始括号之后也不会有。
    照抄 ``break`` 只是为了行为完全对齐。
    """
    assert detect_chorus_spans(_chars("（あい")) == []
    assert detect_chorus_spans(_chars("あ（い（う")) == []


def test_nesting_closes_at_the_first_end_char() -> None:
    """不支持嵌套：第一个结束字符就收口，外层那个右括号留在段外。"""
    spans = detect_chorus_spans(_chars("（あ（い）う）"))

    assert [(s.start, s.end) for s in spans] == [(0, 4)]


def test_a_line_without_brackets_yields_nothing() -> None:
    assert detect_chorus_spans(_chars("月を見あげた")) == []


@pytest.mark.parametrize("begin,end", [("", DEFAULT_CHORUS_END_CHARS), (DEFAULT_CHORUS_BEGIN_CHARS, "")])
def test_empty_delimiters_detect_nothing(begin: str, end: str) -> None:
    """把起止字符清空等于关掉这个功能，不该炸也不该乱标。"""
    assert detect_chorus_spans(_chars("（あ）"), begin_chars=begin, end_chars=end) == []


# ── 落到标签上 ────────────────────────────────────────────────


def test_labels_are_filled_for_the_whole_span() -> None:
    texts = _chars("あ（い）")
    labels = [None, None, None, None]

    assert apply_chorus_roles(texts, labels, "和声") == [None, "和声", "和声", "和声"]


def test_an_existing_role_is_left_alone_by_default() -> None:
    """角色可能是在打轴模块里一个个点出来的，别无脑覆盖。"""
    texts = _chars("（あい）")
    labels = [None, "主唱", None, None]

    assert apply_chorus_roles(texts, labels, "和声") == ["和声", "主唱", "和声", "和声"]


def test_overwrite_replaces_everything_in_the_span() -> None:
    texts = _chars("（あい）")
    labels = [None, "主唱", None, None]

    result = apply_chorus_roles(texts, labels, "和声", overwrite=True)

    assert result == ["和声", "和声", "和声", "和声"]


def test_nothing_changes_without_brackets() -> None:
    texts = _chars("月を見あげた")
    labels = [None] * len(texts)

    assert apply_chorus_roles(texts, labels, "和声") == labels


# ── 挑角色 ────────────────────────────────────────────────────


def test_an_existing_chorus_role_is_reused() -> None:
    """N3 找的是名字含「コーラス」的方案；我们多认几个说法。"""
    assert pick_chorus_role(["主唱", "和声"]) == "和声"
    assert pick_chorus_role(["Main", "コーラス配色"]) == "コーラス配色"


def test_a_project_without_one_falls_back_to_a_new_name() -> None:
    assert pick_chorus_role(["主唱", "旁白"]) == "和声"
    assert pick_chorus_role([]) == "和声"


# ── 整源应用（宿主侧） ────────────────────────────────────────


@pytest.fixture
def window(monkeypatch):
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    from krok_helper.subtitle_render.frontend.main_window import SubtitleRenderWindow

    app = QApplication.instance() or QApplication([])
    widget = SubtitleRenderWindow.for_embedding(settings_provider=None)
    yield widget
    widget.close()
    widget.deleteLater()
    app.processEvents()


def _track(lines: list[str]):
    from krok_helper.subtitle_render.domain.models import TimingChar, TimingLine, TimingTrack

    return TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar(c, i * 100) for i, c in enumerate(text)],
                end_ms=len(text) * 100,
            )
            for text in lines
        ]
    )


def _labels(line) -> str:
    return "".join("C" if ch.role_label == "和声" else ("X" if ch.role_label else "-") for ch in line.chars)


def test_the_whole_source_is_scanned(window) -> None:
    track = _track(["駅前で（僕は窓辺で）", "月を見あげた", "（真っ白な月を見た）"])
    window._timing_track = track

    rows = window._apply_auto_chorus_roles(
        track, role="和声", begin_chars="（(", end_chars="）)", overwrite=False
    )

    assert rows == (0, 2), "没括号的那行不该被改"
    assert _labels(track.lines[0]) == "---CCCCCCC"
    assert _labels(track.lines[1]) == "------"
    assert _labels(track.lines[2]) == "CCCCCCCCCC"


def test_the_role_scheme_is_materialized(window) -> None:
    """不物化的话 painter 解析不到，改了角色毫无视觉变化。"""
    track = _track(["（あ）"])
    window._timing_track = track

    window._apply_auto_chorus_roles(
        track, role="和声", begin_chars="（(", end_chars="）)", overwrite=False
    )

    assert "和声" in window._style.custom_style_schemes


def test_the_whole_batch_is_one_undo_entry(window) -> None:
    """一行一条的话，整源跑完撤销要按几十次。"""
    track = _track(["（あ）", "（い）", "（う）"])
    window._timing_track = track
    window._undo_stack.clear()

    window._apply_auto_chorus_roles(
        track, role="和声", begin_chars="（(", end_chars="）)", overwrite=False
    )

    # 三行只留一条角色记录。栈里另有一条 style —— 那是"新建了「和声」这个配色
    # 方案"，本来就该能单独撤销；它排在前面，所以第一次 Ctrl+Z 撤的是角色。
    role_entries = [entry for entry in window._undo_stack if entry[0] == "char_roles_batch"]
    assert len(role_entries) == 1
    assert window._undo_stack[-1][0] == "char_roles_batch"
    _kind, _track_index, rows, _old, _new = role_entries[0]
    assert rows == (0, 1, 2)

    window._undo_edit()

    assert all(ch.role_label is None for line in track.lines for ch in line.chars)


def test_existing_roles_are_kept(window) -> None:
    track = _track(["（あい）"])
    track.lines[0].chars[1].role_label = "主唱"
    window._timing_track = track

    window._apply_auto_chorus_roles(
        track, role="和声", begin_chars="（(", end_chars="）)", overwrite=False
    )

    assert _labels(track.lines[0]) == "CXCC"


def test_nothing_to_do_returns_no_rows_and_no_undo(window) -> None:
    """没有括号时不该留下一条空的撤销记录。"""
    track = _track(["月を見あげた"])
    window._timing_track = track
    window._undo_stack.clear()

    rows = window._apply_auto_chorus_roles(
        track, role="和声", begin_chars="（(", end_chars="）)", overwrite=False
    )

    assert rows == ()
    assert window._undo_stack == []


# ── 导入时自动应用 ──────────────────────────────────────────


def test_import_auto_apply_fills_brackets_and_stays_undoable(window) -> None:
    """导入后自动识别：括号内容进「和声」，且整体一条撤销能退干净。"""
    track = _track(["駅前で（僕は窓辺で）", "月を見あげた"])
    window._timing_track = track
    window._undo_stack.clear()

    window._auto_apply_chorus_on_import(track)

    assert _labels(track.lines[0]) == "---CCCCCCC"
    assert _labels(track.lines[1]) == "------"
    assert "和声" in window._style.custom_style_schemes
    role_entries = [e for e in window._undo_stack if e[0] == "char_roles_batch"]
    assert len(role_entries) == 1

    window._undo_edit()

    assert all(ch.role_label is None for line in track.lines for ch in line.chars)


def test_import_auto_apply_honors_the_remembered_role(window) -> None:
    """上次弹窗里选过的角色优先于「和声」回落。"""
    track = _track(["（あ）"])
    # 让当前角色列表里出现「コーラス」，记忆值才有效。
    track.lines[0].chars[0].role_label = "コーラス"
    window._timing_track = track
    window._auto_chorus_role = "コーラス"
    window._undo_stack.clear()

    window._auto_apply_chorus_on_import(track)

    assert track.lines[0].chars[1].role_label == "コーラス"
    assert "コーラス" in window._style.custom_style_schemes


def test_import_auto_apply_is_a_no_op_when_disabled(window) -> None:
    track = _track(["（あ）"])
    window._timing_track = track
    window._auto_chorus_auto_apply = False
    window._undo_stack.clear()

    window._auto_apply_chorus_on_import(track)

    assert all(ch.role_label is None for ch in track.lines[0].chars)
    assert window._undo_stack == []
    assert "和声" not in window._style.custom_style_schemes


def test_import_auto_apply_is_silent_without_brackets(window) -> None:
    """没有括号的源不该物化方案、留撤销记录或弹提示。"""
    track = _track(["月を見あげた"])
    window._timing_track = track
    window._undo_stack.clear()

    window._auto_apply_chorus_on_import(track)

    assert window._undo_stack == []
    assert "和声" not in window._style.custom_style_schemes


def test_apply_timing_track_runs_auto_chorus_for_plain_imports(window, monkeypatch) -> None:
    calls: list[object] = []
    monkeypatch.setattr(
        window, "_auto_apply_chorus_on_import", lambda track: calls.append(track)
    )
    track = _track(["（あ）"])

    window._apply_timing_track(track, None)

    assert calls == [track]


def test_apply_timing_track_skips_auto_chorus_while_loading_project(
    window, monkeypatch
) -> None:
    """打开 .yurika / .n3proj 工程时角色是持久化数据，不能被自动识别改写。"""
    calls: list[object] = []
    monkeypatch.setattr(
        window, "_auto_apply_chorus_on_import", lambda track: calls.append(track)
    )
    track = _track(["（あ）"])
    window._loading_project = True
    try:
        window._apply_timing_track(track, None)
    finally:
        window._loading_project = False

    assert calls == []


# ── 偏好读写（auto_chorus.auto_apply） ───────────────────────


def test_runtime_preferences_default_auto_apply_to_enabled() -> None:
    from krok_helper.subtitle_render.settings.preferences import (
        load_app_runtime_preferences,
    )

    loaded = load_app_runtime_preferences(
        {}, chorus_begin_default="（(", chorus_end_default="）)"
    )

    assert loaded.auto_chorus_auto_apply is True


@pytest.mark.parametrize("raw,expected", [(False, False), (True, True), ("nope", True)])
def test_runtime_preferences_parse_auto_apply(raw, expected) -> None:
    from krok_helper.subtitle_render.settings.preferences import (
        load_app_runtime_preferences,
    )

    loaded = load_app_runtime_preferences(
        {"auto_chorus": {"auto_apply": raw}},
        chorus_begin_default="（(",
        chorus_end_default="）)",
    )

    assert loaded.auto_chorus_auto_apply is expected


def test_update_app_runtime_preferences_writes_auto_apply_and_keeps_unknown_keys() -> None:
    from krok_helper.subtitle_render.settings.preferences import (
        update_app_runtime_preferences,
    )

    data = update_app_runtime_preferences(
        {"auto_chorus": {"future_key": 1}},
        auto_chorus_role="和声",
        auto_chorus_begin_chars="（(",
        auto_chorus_end_chars="）)",
        auto_chorus_overwrite=False,
        auto_chorus_auto_apply=False,
        selected_scheme_key="global",
        preview_splitter_ratio=0.4,
        auto_save_enabled=True,
        auto_save_interval_minutes=5,
        project_backup_count=5,
    )

    assert data["auto_chorus"]["auto_apply"] is False
    assert data["auto_chorus"]["future_key"] == 1


# ── 弹窗与入口 ───────────────────────────────────────────────


def test_auto_chorus_dialog_auto_apply_checkbox_roundtrips(window) -> None:
    from krok_helper.subtitle_render.frontend.dialogs.auto_chorus_dialog import (
        AutoChorusDialog,
    )

    dialog = AutoChorusDialog(role_options=["主唱"], auto_apply=False, parent=window)
    try:
        assert dialog.auto_apply() is False
        dialog.auto_apply_check.setChecked(True)
        assert dialog.auto_apply() is True
    finally:
        dialog.close()
        dialog.deleteLater()


def test_loading_settings_dialog_has_auto_chorus_entry_emitting_signal(window) -> None:
    from krok_helper.subtitle_render.domain.timing import SubtitleLoadingSettings
    from krok_helper.subtitle_render.frontend.dialogs.workspace_dialogs import (
        SubtitleLoadingSettingsDialog,
    )

    dialog = SubtitleLoadingSettingsDialog(
        mode="global",
        effective=SubtitleLoadingSettings(),
        global_defaults=SubtitleLoadingSettings(),
        anchor=None,
        parent=window,
    )
    try:
        fired: list[bool] = []
        dialog.autoChorusRequested.connect(lambda: fired.append(True))

        dialog._auto_chorus_button.click()

        assert fired == [True]
    finally:
        dialog.close()
        dialog.deleteLater()


def test_source_settings_entry_routes_to_auto_chorus(window, monkeypatch) -> None:
    """「加载字幕设置 → 自动识别和声…」与右键菜单走同一条 _on_auto_chorus_requested。"""
    from krok_helper.subtitle_render.frontend import main_window as mw_mod

    fired: list[bool] = []
    monkeypatch.setattr(
        window, "_on_auto_chorus_requested", lambda: fired.append(True)
    )

    real_cls = mw_mod._SubtitleLoadingSettingsDialog
    created: dict[str, object] = {}

    class _FakeDialog(real_cls):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            created["dialog"] = self

        def exec(self):
            self.autoChorusRequested.emit()
            return mw_mod.QDialog.DialogCode.Rejected

    monkeypatch.setattr(mw_mod, "_SubtitleLoadingSettingsDialog", _FakeDialog)
    window._timing_track = _track(["（あ）"])
    try:
        window._on_source_settings_requested(None)
    finally:
        dialog = created.get("dialog")
        if dialog is not None:
            dialog.close()
            dialog.deleteLater()

    assert fired == [True]


def test_the_guide_symbol_is_left_alone(window) -> None:
    """导唱符是行首的引导标记，不属于括号里的和声段。"""
    from krok_helper.subtitle_render.domain.models import GuideSymbol

    track = _track(["（あ）"])
    symbol = GuideSymbol(name="导唱符", count=1)
    track.lines[0].guide_symbol = symbol
    window._timing_track = track

    window._apply_auto_chorus_roles(
        track, role="和声", begin_chars="（(", end_chars="）)", overwrite=False
    )

    assert track.lines[0].guide_symbol is symbol


# ── 弹窗与偏好记忆 ────────────────────────────────────────────


def test_the_dialog_opens_on_the_remembered_choice() -> None:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    from krok_helper.subtitle_render.frontend.dialogs.auto_chorus_dialog import AutoChorusDialog

    app = QApplication.instance() or QApplication([])
    dialog = AutoChorusDialog(
        role_options=["主唱", "和声"],
        selected_role="和声",
        begin_chars="[",
        end_chars="]",
        overwrite=True,
    )
    try:
        assert dialog.selected_role() == "和声"
        assert dialog.begin_chars() == "["
        assert dialog.end_chars() == "]"
        assert dialog.overwrite() is True
    finally:
        dialog.close()
        dialog.deleteLater()
        app.processEvents()


def test_an_empty_delimiter_disables_the_apply_button() -> None:
    """起止留空等于识别不出任何东西，别让用户点了没反应。"""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    from krok_helper.subtitle_render.frontend.dialogs.auto_chorus_dialog import AutoChorusDialog

    app = QApplication.instance() or QApplication([])
    dialog = AutoChorusDialog(role_options=["和声"], selected_role="和声")
    try:
        assert dialog.apply_button.isEnabled()

        dialog.begin_edit.setText("")

        assert not dialog.apply_button.isEnabled()
    finally:
        dialog.close()
        dialog.deleteLater()
        app.processEvents()


def test_a_project_without_a_chorus_role_offers_to_create_one() -> None:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    from krok_helper.subtitle_render.frontend.dialogs.auto_chorus_dialog import AutoChorusDialog

    app = QApplication.instance() or QApplication([])
    dialog = AutoChorusDialog(role_options=["主唱"])
    try:
        # 没有可选的和声角色时落在「新建」上，selected_role 返回空串交由宿主定名。
        assert dialog.selected_role() == ""
    finally:
        dialog.close()
        dialog.deleteLater()
        app.processEvents()


def test_the_preferences_survive_a_save_load_round_trip(window) -> None:
    window._auto_chorus_role = "和声"
    window._auto_chorus_begin_chars = "[{"
    window._auto_chorus_end_chars = "]}"
    window._auto_chorus_overwrite = True
    window._auto_chorus_auto_apply = False
    saved: dict = {}
    # 窗口在构造时把 provider 包进了 SubtitleRenderSettingsStore，
    # 事后替换 _settings_provider 不会生效；持久化走的是 _settings_store。
    window._settings_store = type(
        "Store",
        (),
        {
            "load": staticmethod(lambda: {}),
            "save": lambda _s, data: saved.update(data),
        },
    )()

    window._save_persisted_state()

    assert saved["auto_chorus"] == {
        "role": "和声",
        "begin_chars": "[{",
        "end_chars": "]}",
        "overwrite": True,
        "auto_apply": False,
    }
