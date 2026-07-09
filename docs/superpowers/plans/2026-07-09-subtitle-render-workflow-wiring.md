# 字幕渲染工作流接线 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将第 5 步「字幕视频生成」接入主工作流：自动读取第 4 步 SUG 打轴项目并转成 Nicokara LRC，字幕 MP4 导出完成后自动填入第 6 步 Hi-Res 混流并切换过去。

**Architecture:** 主窗口提供一个轻量 workflow adapter，负责从嵌入式 SUG `ProjectStore.project` 导出临时 Nicokara LRC，并接收字幕渲染页的导出完成通知。字幕渲染页只暴露小型公共方法/回调，不直接依赖 `KrokHelperQtApp` 的大类实现。

**Tech Stack:** Python 3.14、PyQt6、qfluentwidgets、SUG submodule `ExportService`、现有 `SubtitleRenderWindow.load_from_lrc()` / `load_video()` / `DropZoneCard.set_path()`。

## Global Constraints

- 不修改 `krok_helper/lyrics_timing/src/strange_uta_game/` submodule 源码。
- 字幕源仍以 SUG Nicokara 导出格式 `.lrc` 作为边界，保留时间戳、注音、演唱者标签与分色字段。
- 第 5 步导出仍使用现有 QPainter + ffmpeg rawvideo pipe，不启用 native renderer。
- 用户可见字符串使用中文。
- 只实现本轮闭环：第 4 步 → 第 5 步读取；第 5 步导出完成 → 第 6 步填入字幕视频并切页。

---

### Task 1: Host adapter loads SUG project into subtitle render

**Files:**
- Modify: `krok_helper/gui_qt.py`
- Test: `tests/test_subtitle_render_workflow.py`

**Interfaces:**
- Consumes: `self.lyrics_timing_page._store.project`, `strange_uta_game.backend.application.export_service.ExportService`
- Produces: `KrokHelperQtApp._prepare_subtitle_render_from_workflow() -> Path | None`

- [ ] **Step 1: Write the failing test**

```python
def test_prepare_subtitle_render_exports_sug_project_to_lrc_and_loads_page(tmp_path, monkeypatch):
    calls = {}

    class FakeResult:
        success = True
        file_path = str(tmp_path / "song.lrc")
        error_message = None

    class FakeExportService:
        def export(self, project, format_name, file_path, **kwargs):
            calls["project"] = project
            calls["format_name"] = format_name
            calls["file_path"] = file_path
            calls["kwargs"] = kwargs
            Path(file_path).write_text("@Title=song\n", encoding="utf-8")
            return FakeResult()

    class FakeSubtitlePage:
        def load_from_lrc(self, path):
            calls["loaded_lrc"] = path
            return object()

    app = SimpleNamespace(
        lyrics_timing_page=SimpleNamespace(
            _store=SimpleNamespace(project=object(), save_path=tmp_path / "song.sug")
        ),
        subtitle_render_page=FakeSubtitlePage(),
        _show_module=lambda module_id: calls.setdefault("shown", module_id),
    )

    monkeypatch.setattr("krok_helper.gui_qt.ExportService", FakeExportService)

    result = KrokHelperQtApp._prepare_subtitle_render_from_workflow(app)

    assert result == tmp_path / "song.lrc"
    assert calls["format_name"] == "Nicokara (带注音)"
    assert calls["kwargs"]["insert_singer_tags"] is True
    assert calls["loaded_lrc"] == tmp_path / "song.lrc"
    assert calls["shown"] == WORKFLOW_SUBTITLE_RENDER
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:\Python314\python.exe -m pytest tests\test_subtitle_render_workflow.py::test_prepare_subtitle_render_exports_sug_project_to_lrc_and_loads_page -q`

Expected: FAIL because `tests/test_subtitle_render_workflow.py` or `_prepare_subtitle_render_from_workflow` does not exist.

- [ ] **Step 3: Write minimal implementation**

Add a main-window helper that:

```python
def _prepare_subtitle_render_from_workflow(self) -> Path | None:
    store = getattr(getattr(self, "lyrics_timing_page", None), "_store", None)
    project = getattr(store, "project", None)
    if project is None:
        QMessageBox.information(self, APP_TITLE, "请先在第 4 步完成歌词打轴。")
        return None
    save_path = getattr(store, "save_path", None)
    base_dir = Path(save_path).parent if save_path else Path(tempfile.gettempdir()) / "KaraokeStudioSubtitleRender"
    base_dir.mkdir(parents=True, exist_ok=True)
    title = getattr(getattr(project, "metadata", None), "title", "") or "subtitle_render"
    output_path = base_dir / f"{safe_filename(title)}.lrc"
    result = ExportService().export(
        project,
        "Nicokara (带注音)",
        str(output_path),
        singer_ids=None,
        insert_singer_tags=True,
        insert_singer_each_line=False,
        singer_map={s.id: s.name for s in getattr(project, "singers", [])},
    )
    if not result.success:
        QMessageBox.critical(self, APP_TITLE, result.error_message or "导出 Nicokara LRC 失败。")
        return None
    if self.subtitle_render_page.load_from_lrc(output_path) is None:
        return None
    self._show_module(WORKFLOW_SUBTITLE_RENDER)
    return output_path
```

Use the existing Windows invalid filename rules when constructing `output_path`.

- [ ] **Step 4: Run test to verify it passes**

Run: `C:\Python314\python.exe -m pytest tests\test_subtitle_render_workflow.py::test_prepare_subtitle_render_exports_sug_project_to_lrc_and_loads_page -q`

Expected: PASS.

- [ ] **Step 5: Wire step transition**

Modify `KrokHelperQtApp._show_module()` so navigating to `WORKFLOW_SUBTITLE_RENDER` calls `_prepare_subtitle_render_from_workflow()` before showing the page, while guarding against recursion with a private flag.

- [ ] **Step 6: Commit**

```bash
git add krok_helper/gui_qt.py tests/test_subtitle_render_workflow.py
git commit -m "feat: load SUG timing into subtitle render workflow"
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
