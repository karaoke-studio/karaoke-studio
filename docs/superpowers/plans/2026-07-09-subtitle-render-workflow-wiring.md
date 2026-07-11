# 字幕渲染工作流接线 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将第 5 步「字幕视频生成」接入主工作流：自动读取第 4 步 SUG 打轴项目（`.sug` / SUG `Project`）并转换为渲染模块 `TimingTrack`，字幕 MP4 导出完成后自动填入第 6 步 Hi-Res 混流并切换过去。

**Architecture:** 主窗口提供一个轻量 workflow adapter，负责把嵌入式 SUG `ProjectStore.project` 直接交给字幕渲染页。字幕渲染模块新增 `sug_project.py` 适配层，把 SUG domain 的逐字时间戳、注音、演唱者/分色信息转换为统一的 `TimingTrack`；同时保留既有 `.lrc` 导入能力。

**Tech Stack:** Python 3.14、PyQt6、qfluentwidgets、SUG submodule `SugProjectParser` / domain model、现有 `SubtitleRenderWindow.load_from_lrc()` / `load_video()` / `DropZoneCard.set_path()`。

## Global Constraints

- 不修改 `krok_helper/lyrics_timing/src/strange_uta_game/` submodule 源码。
- 字幕源优先以 `.sug` / SUG `Project` 作为边界，直接保留时间戳、注音、演唱者/分色信息；`.lrc` 仅作为兼容导入入口。
- 第 5 步导出仍使用现有 QPainter + ffmpeg rawvideo pipe，不启用 native renderer。
- 用户可见字符串使用中文。
- 只实现本轮闭环：第 4 步 → 第 5 步读取；第 5 步导出完成 → 第 6 步填入字幕视频并切页。

---

### Task 1: Direct SUG project adapter

**Files:**
- Create: `krok_helper/subtitle_render/sug_project.py`
- Modify: `krok_helper/subtitle_render/frontend/main_window.py`
- Modify: `krok_helper/gui_qt.py`
- Modify: `krok_helper/subtitle_render/frontend/lyrics_list.py`
- Modify: `krok_helper/subtitle_render/models.py`
- Test: `tests/test_subtitle_render_sug_project.py`
- Test: `tests/test_subtitle_render_workflow.py`

**Interfaces:**
- Consumes: `self.lyrics_timing_page._store.project`, `.sug` files read by `SugProjectParser.load()`
- Produces: `timing_track_from_sug_project(project) -> TimingTrack`
- Produces: `load_sug_timing_track(path: Path) -> TimingTrack`
- Produces: `SubtitleRenderWindow.load_from_sug_project(project, source_path=None) -> TimingTrack | None`
- Produces: `KrokHelperQtApp._prepare_subtitle_render_from_workflow() -> object | None`

- [ ] **Step 1: Write the failing test**

```python
def test_timing_track_from_sug_project_preserves_timing_ruby_and_singers():
    main = Singer(id="main", name="主唱", color="#ff0000", is_default=True)
    ch = Character(
        char="愛",
        ruby=Ruby(parts=[RubyPart("あ"), RubyPart("い")]),
        check_count=2,
        timestamps=[1000, 1300],
        sentence_end_ts=1800,
        is_sentence_end=True,
        singer_id=main.id,
    )
    project = Project(
        metadata=ProjectMetadata(title="曲名"),
        singers=[main],
        sentences=[Sentence(singer_id=main.id, characters=[ch])],
        global_offset_ms=50,
    )

    track = timing_track_from_sug_project(project)

    assert track.meta.title == "曲名"
    assert track.meta.offset_ms == 50
    assert track.lines[0].singer_label == "主唱"
    assert track.lines[0].chars[0].start_ms == 1050
    assert track.lines[0].chars[0].role_label == "主唱"
    assert track.rubies[0].reading_parts == ["あ", "い"]
    assert track.rubies[0].reading_part_ms == [300]
```

```python
def test_prepare_subtitle_render_loads_sug_project_directly(monkeypatch):
    calls = {}

    class FakeExportService:
        def export(self, *args, **kwargs):
            raise AssertionError("workflow should not export an intermediate Nicokara LRC")

    class FakeSubtitlePage:
        def load_from_sug_project(self, project, source_path=None):
            calls["loaded_project"] = project
            calls["source_path"] = source_path
            return object()

    project = object()
    app = SimpleNamespace(
        lyrics_timing_page=SimpleNamespace(
            _store=SimpleNamespace(project=project, save_path=Path("song.sug"))
        ),
        subtitle_render_page=FakeSubtitlePage(),
        _show_module=lambda module_id: calls.setdefault("shown", module_id),
    )

    monkeypatch.setattr(gui_qt, "ExportService", FakeExportService, raising=False)

    result = KrokHelperQtApp._prepare_subtitle_render_from_workflow(app)

    assert result is project
    assert calls["loaded_project"] is project
    assert calls["source_path"] == Path("song.sug")
    assert calls["shown"] == WORKFLOW_SUBTITLE_RENDER
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:\Python314\python.exe -m pytest tests\test_subtitle_render_sug_project.py tests\test_subtitle_render_workflow.py -q`

Expected: FAIL because `krok_helper.subtitle_render.sug_project` / `load_from_sug_project` do not exist.

- [ ] **Step 3: Write minimal implementation**

Implement `sug_project.py` to convert SUG `Project` to `TimingTrack`, then add `SubtitleRenderWindow.load_from_sug()` / `load_from_sug_project()` and change `KrokHelperQtApp._prepare_subtitle_render_from_workflow()` to call `load_from_sug_project(project, source_path=save_path)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `C:\Python314\python.exe -m pytest tests\test_subtitle_render_sug_project.py tests\test_subtitle_render_workflow.py -q`

Expected: PASS.

- [ ] **Step 5: Wire step transition**

Modify `KrokHelperQtApp._show_module()` so navigating to `WORKFLOW_SUBTITLE_RENDER` calls `_prepare_subtitle_render_from_workflow()` before showing the page, while guarding against recursion with a private flag.

- [ ] **Step 6: Commit**

```bash
git add krok_helper/subtitle_render/sug_project.py krok_helper/subtitle_render/frontend/main_window.py krok_helper/subtitle_render/frontend/lyrics_list.py krok_helper/subtitle_render/models.py krok_helper/gui_qt.py tests/test_subtitle_render_sug_project.py tests/test_subtitle_render_workflow.py docs/superpowers/plans/2026-07-09-subtitle-render-workflow-wiring.md
git commit -m "feat: load SUG projects directly in subtitle workflow"
```

### Task 2: Subtitle export completion notifies host and advances to Hi-Res

**Files:**
- Modify: `krok_helper/subtitle_render/frontend/main_window.py`
- Modify: `krok_helper/gui_qt.py`
- Test: `tests/test_subtitle_render_workflow.py`

**Interfaces:**
- Consumes: `_workflow_context.accept_subtitle_video(path: Path) -> None`
- Produces: `SubtitleRenderWindow._finish_render_success(output_path: Path)` calls the workflow context when embedded.

- [ ] **Step 1: Write the failing test**

```python
def test_finish_render_success_passes_output_to_workflow_context(qapp, tmp_path):
    received = {}

    class Context:
        def accept_subtitle_video(self, path):
            received["path"] = path

    window = SubtitleRenderWindow(embedded=True, workflow_context=Context())
    output = tmp_path / "subtitle.mp4"

    window._finish_render_success(output)

    assert received["path"] == output
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:\Python314\python.exe -m pytest tests\test_subtitle_render_workflow.py::test_finish_render_success_passes_output_to_workflow_context -q`

Expected: FAIL because `_finish_render_success` does not call the context.

- [ ] **Step 3: Write minimal implementation**

In `SubtitleRenderWindow._finish_render_success`, after restoring export buttons:

```python
context = self._workflow_context
if context is not None and hasattr(context, "accept_subtitle_video"):
    context.accept_subtitle_video(output_path)
```

- [ ] **Step 4: Implement host acceptor**

Add `KrokHelperQtApp.accept_subtitle_video(self, path: Path) -> None`:

```python
def accept_subtitle_video(self, path: Path) -> None:
    self.set_video_path(path)
    self._show_module(WORKFLOW_HIRES_MIX)
```

Pass `workflow_context=self` when creating `SubtitleRenderWindow.for_embedding(...)`.

- [ ] **Step 5: Run tests**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
C:\Python314\python.exe -m pytest tests\test_subtitle_render_workflow.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add krok_helper/gui_qt.py krok_helper/subtitle_render/frontend/main_window.py tests/test_subtitle_render_workflow.py
git commit -m "feat: hand subtitle exports to hires workflow"
```

### Task 3: Smoke and regression verification

**Files:**
- No code changes unless verification exposes a defect.

**Interfaces:**
- Consumes: all previous tasks.
- Produces: verified branch ready to push.

- [ ] **Step 1: Run focused workflow tests**

```powershell
$env:QT_QPA_PLATFORM='offscreen'
C:\Python314\python.exe -m pytest tests\test_subtitle_render_workflow.py tests\test_subtitle_render_loaders.py -q
```

- [ ] **Step 2: Run subtitle render test suite**

```powershell
$env:QT_QPA_PLATFORM='offscreen'
$tests = Get-ChildItem -LiteralPath tests -Filter 'test_subtitle_render_*.py' | ForEach-Object FullName
C:\Python314\python.exe -m pytest @tests -q
```

- [ ] **Step 3: Run Qt embed smoke**

```powershell
$env:QT_QPA_PLATFORM='offscreen'
C:\Python314\python.exe -c "from PyQt6.QtWidgets import QApplication; app=QApplication([]); from krok_helper.gui_qt import KrokHelperQtApp; w=KrokHelperQtApp(); print(type(w.subtitle_render_page).__name__, w.subtitle_render_page._embedded)"
```

- [ ] **Step 4: Push**

```bash
git push origin feat/subtitle-render
```

Expected: branch contains one docs commit plus the implementation commits, and `origin/feat/subtitle-render` advances cleanly.
