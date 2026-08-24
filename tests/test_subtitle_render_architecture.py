"""Dependency guardrails for the subtitle-render package."""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path


ROOT = Path("krok_helper/subtitle_render")
PACKAGE = "krok_helper.subtitle_render"


def _module_name(path: Path) -> str:
    relative = path.relative_to(ROOT).with_suffix("")
    parts = [PACKAGE, *relative.parts]
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _module_files() -> dict[str, Path]:
    return {_module_name(path): path for path in ROOT.rglob("*.py")}


def _import_targets(module: str, path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.update(alias.name for alias in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:
            package_parts = package.split(".")
            keep = len(package_parts) - (node.level - 1)
            base = ".".join(package_parts[:keep])
            target = base + (f".{node.module}" if node.module else "")
        else:
            target = node.module or ""
        targets.add(target)
        targets.update(
            f"{target}.{alias.name}" for alias in node.names if alias.name != "*"
        )
    return targets


def _internal_dependencies() -> dict[str, set[str]]:
    files = _module_files()
    dependencies: dict[str, set[str]] = {module: set() for module in files}
    for module, path in files.items():
        for target in _import_targets(module, path):
            candidate = target
            while candidate.startswith(PACKAGE):
                if candidate in files:
                    if candidate != module:
                        dependencies[module].add(candidate)
                    break
                candidate, separator, _ = candidate.rpartition(".")
                if not separator:
                    break
    return dependencies


def _dependency_cycles(graph: dict[str, set[str]]) -> list[tuple[str, ...]]:
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    components: list[tuple[str, ...]] = []

    def visit(module: str) -> None:
        nonlocal index
        indices[module] = index
        lowlinks[module] = index
        index += 1
        stack.append(module)
        on_stack.add(module)

        for dependency in graph[module]:
            if dependency not in indices:
                visit(dependency)
                lowlinks[module] = min(lowlinks[module], lowlinks[dependency])
            elif dependency in on_stack:
                lowlinks[module] = min(lowlinks[module], indices[dependency])

        if lowlinks[module] != indices[module]:
            return
        component: list[str] = []
        while True:
            dependency = stack.pop()
            on_stack.remove(dependency)
            component.append(dependency)
            if dependency == module:
                break
        if len(component) > 1:
            components.append(tuple(sorted(component)))

    for module in graph:
        if module not in indices:
            visit(module)
    return sorted(components)


def test_subtitle_render_engine_does_not_depend_on_frontend() -> None:
    forbidden = f"{PACKAGE}.frontend"
    violations: dict[str, list[str]] = defaultdict(list)
    for path in (ROOT / "engine").rglob("*.py"):
        module = _module_name(path)
        for target in _import_targets(module, path):
            if target == forbidden or target.startswith(f"{forbidden}."):
                violations[module].append(target)

    assert not violations


def test_subtitle_render_non_ui_state_does_not_depend_on_frontend() -> None:
    forbidden = f"{PACKAGE}.frontend"
    paths = (
        ROOT / "background.py",
        ROOT / "contracts.py",
        ROOT / "engine" / "export_command.py",
        ROOT / "engine" / "parallel_schedule.py",
        ROOT / "engine" / "raster_blur.py",
        ROOT / "engine" / "render_job.py",
        ROOT / "engine" / "render_job_policy.py",
        ROOT / "engine" / "render_bands.py",
        ROOT / "engine" / "ruby_timing.py",
        ROOT / "paint.py",
        ROOT / "paint_codec.py",
        ROOT / "project_controller.py",
        ROOT / "project_load.py",
        ROOT / "project_recovery.py",
        ROOT / "project_resources.py",
        ROOT / "recent_projects.py",
        ROOT / "screen_settings.py",
        ROOT / "session.py",
        ROOT / "settings_store.py",
        ROOT / "source_loader.py",
        ROOT / "timecode.py",
        ROOT / "timing.py",
        ROOT / "timing_codec.py",
    )
    violations: dict[str, list[str]] = defaultdict(list)
    for path in paths:
        module = _module_name(path)
        for target in _import_targets(module, path):
            if target == forbidden or target.startswith(f"{forbidden}."):
                violations[module].append(target)

    assert not violations


def test_background_consumers_use_the_focused_domain_contract() -> None:
    legacy_names = {
        "Background",
        "BackgroundSource",
        "background_sequence_frame_path",
        "infer_image_sequence_pattern",
    }
    violations: dict[str, list[str]] = defaultdict(list)
    for path in ROOT.rglob("*.py"):
        if path.name in {"background.py", "models.py"}:
            continue
        module = _module_name(path)
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module != f"{PACKAGE}.models":
                continue
            imported = {alias.name for alias in node.names} & legacy_names
            if imported:
                violations[module].extend(sorted(imported))

    assert not violations


def test_paint_consumers_use_the_focused_domain_contract() -> None:
    legacy_names = {
        "ColorFillMode",
        "ColorLayerKey",
        "ColorStateKey",
        "KaraokeColors",
        "KaraokeColorState",
        "PaintFill",
        "_paint_fill",
    }
    violations: dict[str, list[str]] = defaultdict(list)
    for path in ROOT.rglob("*.py"):
        if path.name in {"models.py", "paint.py"}:
            continue
        module = _module_name(path)
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module != f"{PACKAGE}.models":
                continue
            imported = {alias.name for alias in node.names} & legacy_names
            if imported:
                violations[module].extend(sorted(imported))

    assert not violations


def test_timing_consumers_use_the_focused_domain_contract() -> None:
    legacy_names = {
        "EntryAnimation",
        "ExitAnimation",
        "GuideSymbol",
        "KaraokeAnimation",
        "LineAnimationOverride",
        "LineBreakKind",
        "RubyAnnotation",
        "SubtitleLoadingSettings",
        "SubtitleSource",
        "TimingChar",
        "TimingLine",
        "TimingTrack",
        "TimingTrackMeta",
        "TrackPage",
        "TrackPagePlan",
        "TrackSection",
        "guide_symbol_has_visual",
        "guide_symbol_replacement_count",
        "guide_symbol_replaces_prefix",
        "guide_symbol_role_labels",
        "guide_symbol_with_role_labels",
        "line_visible_chars",
        "timing_line_start_ms",
    }
    violations: dict[str, list[str]] = defaultdict(list)
    for path in ROOT.rglob("*.py"):
        if path.name in {"models.py", "timing.py"}:
            continue
        module = _module_name(path)
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module != f"{PACKAGE}.models":
                continue
            imported = {alias.name for alias in node.names} & legacy_names
            if imported:
                violations[module].extend(sorted(imported))

    assert not violations


def test_timing_codec_consumers_use_the_focused_persistence_contract() -> None:
    legacy_names = {
        "guide_symbol_from_dict",
        "guide_symbol_to_dict",
        "line_animation_override_from_dict",
        "line_animation_override_to_dict",
        "subtitle_loading_settings_from_dict",
        "subtitle_loading_settings_to_dict",
        "track_page_plan_from_dict",
        "track_page_plan_to_dict",
    }
    violations: dict[str, list[str]] = defaultdict(list)
    for path in ROOT.rglob("*.py"):
        if path.name in {"models.py", "timing_codec.py"}:
            continue
        module = _module_name(path)
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module != f"{PACKAGE}.models":
                continue
            imported = {alias.name for alias in node.names} & legacy_names
            if imported:
                violations[module].extend(sorted(imported))

    assert not violations


def test_render_job_consumers_use_the_export_contract() -> None:
    violations: dict[str, list[str]] = defaultdict(list)
    for path in ROOT.rglob("*.py"):
        if path.name == "renderer.py":
            continue
        module = _module_name(path)
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module != f"{PACKAGE}.engine.renderer":
                continue
            if any(alias.name == "RenderJob" for alias in node.names):
                violations[module].append("RenderJob")

    assert not violations


def test_subtitle_render_host_contract_has_no_implementation_dependencies() -> None:
    path = ROOT / "contracts.py"
    targets = _import_targets(f"{PACKAGE}.contracts", path)
    forbidden_roots = (
        "PyQt6",
        "krok_helper.subtitle_render.engine",
        "krok_helper.subtitle_render.frontend",
        "krok_helper.subtitle_render.models",
        "krok_helper.subtitle_render.native_backend",
    )

    assert not {
        target
        for target in targets
        if any(target == root or target.startswith(f"{root}.") for root in forbidden_roots)
    }


def test_subtitle_render_internal_import_graph_is_acyclic() -> None:
    assert _dependency_cycles(_internal_dependencies()) == []


def test_line_style_semantics_have_one_engine_owner() -> None:
    owner = f"{PACKAGE}.engine.line_style"
    delegated_names = {
        "_lane_count",
        "_layout_style_for_line",
        "_row_count_resolver",
    }
    for relative_path in (
        Path("engine/layout_assignment.py"),
        Path("engine/painter.py"),
    ):
        path = ROOT / relative_path
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        inline = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        } & delegated_names
        imported = {
            alias.asname
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and node.module == owner
            for alias in node.names
        }

        assert inline == set()
        assert delegated_names <= imported

    line_style_targets = _import_targets(owner, ROOT / "engine/line_style.py")
    assert f"{PACKAGE}.engine.painter" not in line_style_targets


def test_layout_plan_builder_has_no_painter_dependency() -> None:
    module = f"{PACKAGE}.engine.layout_plan_builder"
    targets = _import_targets(module, ROOT / "engine/layout_plan_builder.py")

    assert f"{PACKAGE}.engine.painter" not in targets


def test_guide_render_semantics_have_one_engine_owner() -> None:
    owner = f"{PACKAGE}.engine.guide_semantics"
    painter_path = ROOT / "engine/painter.py"
    painter_tree = ast.parse(painter_path.read_text(encoding="utf-8-sig"))
    inline = {
        node.name
        for node in painter_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    delegated = {
        (alias.name, alias.asname)
        for node in painter_tree.body
        if isinstance(node, ast.ImportFrom) and node.module == owner
        for alias in node.names
    }

    assert "_line_with_guide_symbol" not in inline
    assert delegated == {
        ("render_line_with_guide_symbols", "_line_with_guide_symbol")
    }
    targets = _import_targets(owner, ROOT / "engine/guide_semantics.py")
    assert f"{PACKAGE}.engine.painter" not in targets


def test_line_pagination_semantics_have_one_engine_owner() -> None:
    owner = f"{PACKAGE}.engine.line_pagination"
    painter_path = ROOT / "engine/painter.py"
    painter_tree = ast.parse(painter_path.read_text(encoding="utf-8-sig"))
    delegated_names = {
        "_line_center_override",
        "_renderable_page_lines",
        "_renderable_page_map",
    }
    inline = {
        node.name
        for node in painter_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    } & delegated_names
    imported = {
        alias.asname
        for node in painter_tree.body
        if isinstance(node, ast.ImportFrom) and node.module == owner
        for alias in node.names
    }

    assert inline == set()
    assert imported == delegated_names
    targets = _import_targets(owner, ROOT / "engine/line_pagination.py")
    assert f"{PACKAGE}.engine.painter" not in targets


def test_subtitle_render_window_delegates_background_tasks() -> None:
    window_path = ROOT / "frontend" / "main_window.py"
    worker_path = ROOT / "frontend" / "background_tasks.py"
    worker_names = {"_RecoverySaveWorker", "_MediaProbeWorker", "_RenderWorker"}

    window_tree = ast.parse(window_path.read_text(encoding="utf-8-sig"))
    worker_tree = ast.parse(worker_path.read_text(encoding="utf-8-sig"))
    inline_workers = {
        node.name for node in window_tree.body if isinstance(node, ast.ClassDef)
    } & worker_names
    extracted_workers = {
        node.name for node in worker_tree.body if isinstance(node, ast.ClassDef)
    } & worker_names

    assert inline_workers == set()
    assert extracted_workers == worker_names
    assert (
        f"{PACKAGE}.frontend.background_tasks"
        in _import_targets(f"{PACKAGE}.frontend.main_window", window_path)
    )


def test_subtitle_render_window_delegates_export_thread_wiring() -> None:
    window_path = ROOT / "frontend" / "main_window.py"
    window_module = f"{PACKAGE}.frontend.main_window"
    targets = _import_targets(window_module, window_path)

    assert f"{PACKAGE}.frontend.export_runtime" in targets
    tree = ast.parse(window_path.read_text(encoding="utf-8-sig"))
    direct_worker_imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == f"{PACKAGE}.frontend.background_tasks"
        for alias in node.names
    }
    assert "_RenderWorker" not in direct_worker_imports
    window_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SubtitleRenderWindow"
    )
    expected_calls = {
        "_start_render_export": {"is_active", "prepare", "start"},
        "_stop_render_export": {"cancel", "is_active"},
    }

    for method_name, expected in expected_calls.items():
        method = next(
            node
            for node in window_class.body
            if isinstance(node, ast.FunctionDef) and node.name == method_name
        )
        controller_calls = {
            node.func.attr
            for node in ast.walk(method)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "_export_runtime_controller"
        }
        assert controller_calls == expected


def test_subtitle_render_window_delegates_auto_save_thread_lifecycle() -> None:
    window_path = ROOT / "frontend" / "main_window.py"
    window_module = f"{PACKAGE}.frontend.main_window"
    targets = _import_targets(window_module, window_path)

    assert f"{PACKAGE}.frontend.project_autosave" in targets
    tree = ast.parse(window_path.read_text(encoding="utf-8-sig"))
    recovery_worker_imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == f"{PACKAGE}.frontend.background_tasks"
        for alias in node.names
    }
    assert "_RecoverySaveWorker" not in recovery_worker_imports
    inline_timer_assignments = {
        target.attr
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (
            node.targets if isinstance(node, ast.Assign) else [node.target]
        )
        if isinstance(target, ast.Attribute)
        and target.attr in {"_auto_save_timer", "_periodic_auto_save_timer"}
    }
    assert inline_timer_assignments == set()


def test_subtitle_render_window_delegates_project_recovery_policy() -> None:
    window_path = ROOT / "frontend" / "main_window.py"
    window_module = f"{PACKAGE}.frontend.main_window"
    targets = _import_targets(window_module, window_path)

    assert f"{PACKAGE}.project_recovery" in targets
    tree = ast.parse(window_path.read_text(encoding="utf-8-sig"))
    forbidden = {"invalidate_recovery_project", "scan_recovery_projects"}
    direct_policy_imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == f"{PACKAGE}.project_store"
        for alias in node.names
    } & forbidden
    assert direct_policy_imports == set()


def test_subtitle_render_window_delegates_recovery_prompt_flow() -> None:
    window_path = ROOT / "frontend" / "main_window.py"
    window_module = f"{PACKAGE}.frontend.main_window"
    targets = _import_targets(window_module, window_path)

    assert f"{PACKAGE}.frontend.project_recovery" in targets
    tree = ast.parse(window_path.read_text(encoding="utf-8-sig"))
    window_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SubtitleRenderWindow"
    )
    method = next(
        node
        for node in window_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "check_crash_recovery"
    )
    prompt_literals = {
        node.value
        for node in ast.walk(method)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert "字幕项目恢复文件损坏" not in prompt_literals
    assert "发现字幕项目恢复数据" not in prompt_literals


def test_subtitle_render_window_delegates_native_project_commands() -> None:
    window_path = ROOT / "frontend" / "main_window.py"
    window_module = f"{PACKAGE}.frontend.main_window"
    targets = _import_targets(window_module, window_path)

    assert f"{PACKAGE}.frontend.project_commands" in targets
    tree = ast.parse(window_path.read_text(encoding="utf-8-sig"))
    window_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SubtitleRenderWindow"
    )
    methods = {
        node.name: node
        for node in window_class.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {
            "_confirm_discard_changes",
            "_open_project",
            "_save_project_as",
        }
    }
    controller_calls = {
        node.func.attr
        for method in methods.values()
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "_project_command_controller"
    }
    assert controller_calls == {
        "choose_open_path",
        "choose_save_path",
        "confirm_discard",
    }


def test_subtitle_render_window_delegates_recent_projects() -> None:
    window_path = ROOT / "frontend" / "main_window.py"
    window_module = f"{PACKAGE}.frontend.main_window"
    targets = _import_targets(window_module, window_path)

    assert f"{PACKAGE}.frontend.recent_projects" in targets
    tree = ast.parse(window_path.read_text(encoding="utf-8-sig"))
    window_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SubtitleRenderWindow"
    )
    method_names = {
        "_load_recent_projects",
        "_persist_recent_projects",
        "_rebuild_recent_projects_menu",
        "_set_recent_projects",
        "_record_recent_project",
        "_clear_recent_projects",
        "_open_recent_project",
    }
    controller_calls = {
        node.func.attr
        for method in window_class.body
        if isinstance(method, ast.FunctionDef) and method.name in method_names
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "_recent_projects_controller"
    }
    assert controller_calls == {
        "clear",
        "load",
        "open",
        "persist",
        "rebuild_menu",
        "record",
        "set_paths",
    }


def test_subtitle_render_window_imports_project_settings_dialog() -> None:
    window_path = ROOT / "frontend" / "main_window.py"
    window_module = f"{PACKAGE}.frontend.main_window"

    assert (
        f"{PACKAGE}.frontend.project_settings"
        in _import_targets(window_module, window_path)
    )
    tree = ast.parse(window_path.read_text(encoding="utf-8-sig"))
    inline_dialogs = {
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "_AutoSaveSettingsDialog"
    }
    assert inline_dialogs == set()


def test_subtitle_render_window_delegates_project_resource_policy() -> None:
    window_path = ROOT / "frontend" / "main_window.py"
    window_module = f"{PACKAGE}.frontend.main_window"
    targets = _import_targets(window_module, window_path)

    assert f"{PACKAGE}.project_resources" in targets
    tree = ast.parse(window_path.read_text(encoding="utf-8-sig"))
    window_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SubtitleRenderWindow"
    )
    method = next(
        node
        for node in window_class.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_missing_project_resources"
    )
    calls = {
        node.func.id
        for node in ast.walk(method)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert calls == {"find_missing_project_resources"}


def test_subtitle_render_window_delegates_project_file_transactions() -> None:
    window_path = ROOT / "frontend" / "main_window.py"
    window_module = f"{PACKAGE}.frontend.main_window"
    targets = _import_targets(window_module, window_path)

    assert f"{PACKAGE}.project_controller" in targets
    tree = ast.parse(window_path.read_text(encoding="utf-8-sig"))
    forbidden = {
        "backup_project_file",
        "inspect_project_file",
        "save_render_project",
    }
    direct_transaction_imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == f"{PACKAGE}.project_store"
        for alias in node.names
    } & forbidden
    assert direct_transaction_imports == set()


def test_subtitle_render_window_consumes_typed_project_load_plan() -> None:
    window_path = ROOT / "frontend" / "main_window.py"
    window_module = f"{PACKAGE}.frontend.main_window"
    targets = _import_targets(window_module, window_path)

    assert f"{PACKAGE}.project_load" in targets
    tree = ast.parse(window_path.read_text(encoding="utf-8-sig"))
    window_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SubtitleRenderWindow"
    )
    method = next(
        node
        for node in window_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "_apply_project_data_inner"
    )
    split_calls = {
        node.func.id
        for node in ast.walk(method)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "split_project_paths" not in split_calls


def test_subtitle_render_window_delegates_subtitle_source_loading() -> None:
    window_path = ROOT / "frontend" / "main_window.py"
    window_module = f"{PACKAGE}.frontend.main_window"
    targets = _import_targets(window_module, window_path)

    assert f"{PACKAGE}.source_loader" in targets
    assert f"{PACKAGE}.subtitle_sources" not in targets
    assert f"{PACKAGE}.sug_project" not in targets
    tree = ast.parse(window_path.read_text(encoding="utf-8-sig"))
    window_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SubtitleRenderWindow"
    )
    method_names = {
        "load_from_lrc",
        "load_from_sug",
        "load_from_sug_project",
        "_load_timing_track_file",
    }
    loader_calls = {
        node.func.attr
        for method in window_class.body
        if isinstance(method, ast.FunctionDef) and method.name in method_names
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "_subtitle_source_loader"
    }
    assert loader_calls == {"load_file", "load_lrc", "load_sug", "load_sug_project"}


def test_subtitle_render_window_delegates_n3_import_commands() -> None:
    window_path = ROOT / "frontend" / "main_window.py"
    window_module = f"{PACKAGE}.frontend.main_window"
    targets = _import_targets(window_module, window_path)

    assert f"{PACKAGE}.frontend.import_controller" in targets
    tree = ast.parse(window_path.read_text(encoding="utf-8-sig"))
    direct_n3_imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == f"{PACKAGE}.n3proj_import"
        for alias in node.names
    }
    assert "load_n3proj" not in direct_n3_imports
    assert "N3_PROJECT_FILTER" not in direct_n3_imports
    window_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SubtitleRenderWindow"
    )
    method_names = {"_import_n3_project", "_import_n3_project_path"}
    controller_calls = {
        node.func.attr
        for method in window_class.body
        if isinstance(method, ast.FunctionDef) and method.name in method_names
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "_n3_import_controller"
    }
    assert controller_calls == {"choose_path", "load", "rebase_style_for_video"}


def test_subtitle_render_window_delegates_preview_window_state() -> None:
    window_path = ROOT / "frontend" / "main_window.py"
    window_module = f"{PACKAGE}.frontend.main_window"
    targets = _import_targets(window_module, window_path)

    assert f"{PACKAGE}.frontend.preview_controller" in targets
    tree = ast.parse(window_path.read_text(encoding="utf-8-sig"))
    window_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SubtitleRenderWindow"
    )
    method_names = {
        "_preview_window_context_allowed",
        "_hide_preview_window_for_context",
        "_sync_preview_window_visibility",
        "_request_preview_window",
        "_on_preview_window_user_closed",
        "_show_preview_window",
    }
    controller_calls = {
        node.func.attr
        for method in window_class.body
        if isinstance(method, ast.FunctionDef) and method.name in method_names
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "_preview_window_controller"
    }
    assert controller_calls == {
        "activate_visible",
        "context_allowed",
        "hide",
        "request",
        "sync",
        "user_closed",
    }


def test_subtitle_render_window_delegates_preview_preferences() -> None:
    window_path = ROOT / "frontend" / "main_window.py"
    tree = ast.parse(window_path.read_text(encoding="utf-8-sig"))
    window_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SubtitleRenderWindow"
    )
    delegated_methods = {
        "_on_gpu_preview_changed": "apply_gpu_enabled",
        "_on_preview_quality_changed": "apply_quality",
        "_on_gpu_preview_fallback": "report_gpu_fallback",
    }

    for method_name, controller_method in delegated_methods.items():
        method = next(
            node
            for node in window_class.body
            if isinstance(node, ast.FunctionDef) and node.name == method_name
        )
        calls = {
            node.func.attr
            for node in ast.walk(method)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "_preview_preference_controller"
        }
        assert calls == {controller_method}


def test_subtitle_render_window_delegates_preview_duration() -> None:
    window_path = ROOT / "frontend" / "main_window.py"
    tree = ast.parse(window_path.read_text(encoding="utf-8-sig"))
    window_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SubtitleRenderWindow"
    )
    method = next(
        node
        for node in window_class.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_refresh_transport_duration"
    )
    controller_calls = {
        node.func.attr
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "_preview_duration_controller"
    }

    assert controller_calls == {"refresh"}


def test_subtitle_render_window_delegates_export_job_assembly() -> None:
    window_path = ROOT / "frontend" / "main_window.py"
    window_module = f"{PACKAGE}.frontend.main_window"
    targets = _import_targets(window_module, window_path)

    assert f"{PACKAGE}.frontend.export_controller" in targets
    tree = ast.parse(window_path.read_text(encoding="utf-8-sig"))
    window_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SubtitleRenderWindow"
    )
    delegated_methods = {
        "_build_render_job": "build",
        "_current_export_duration_ms": "resolve_duration_ms",
    }

    for method_name, controller_method in delegated_methods.items():
        method = next(
            node
            for node in window_class.body
            if isinstance(node, ast.FunctionDef) and node.name == method_name
        )
        calls = {
            node.func.attr
            for node in ast.walk(method)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "_export_job_controller"
        }
        assert calls == {controller_method}


def test_subtitle_property_panel_delegates_page_registry_and_routing() -> None:
    panel_path = ROOT / "frontend" / "property_panel.py"
    panel_module = f"{PACKAGE}.frontend.property_panel"
    targets = _import_targets(panel_module, panel_path)

    assert f"{PACKAGE}.frontend.property_pages" in targets
    tree = ast.parse(panel_path.read_text(encoding="utf-8-sig"))
    panel_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "PropertyPanel"
    )
    init_method = next(
        node
        for node in panel_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    route_method = next(
        node
        for node in panel_class.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_on_navigation_changed"
    )

    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "build_property_pages"
        for node in ast.walk(init_method)
    )
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "property_page_index"
        for node in ast.walk(route_method)
    )


def test_subtitle_property_panel_delegates_shared_widget_primitives() -> None:
    panel_path = ROOT / "frontend" / "property_panel.py"
    panel_module = f"{PACKAGE}.frontend.property_panel"
    targets = _import_targets(panel_module, panel_path)

    assert f"{PACKAGE}.frontend.property_widgets" in targets
    tree = ast.parse(panel_path.read_text(encoding="utf-8-sig"))
    inline_names = {
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef)
    }
    assert "ToggleSwitch" not in inline_names
    assert "CollapsibleSection" not in inline_names
    assert "_PillSelector" not in inline_names
    assert "_FolderTabPanel" not in inline_names
    assert "_ClickableRow" not in inline_names
    assert "_SubGroup" not in inline_names


def test_subtitle_property_panel_delegates_responsive_layout_primitives() -> None:
    panel_path = ROOT / "frontend" / "property_panel.py"
    panel_module = f"{PACKAGE}.frontend.property_panel"
    targets = _import_targets(panel_module, panel_path)

    assert f"{PACKAGE}.frontend.property_layout" in targets
    tree = ast.parse(panel_path.read_text(encoding="utf-8-sig"))
    inline_names = {
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef)
    }
    assert "_ResponsiveRoleHeader" not in inline_names
    assert "_ResponsiveFieldGrid" not in inline_names
    assert "_ResponsivePropertyPair" not in inline_names
    inline_functions = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }
    assert "_field" not in inline_functions
    assert "_section_pair" not in inline_functions
    assert "_compact_control" not in inline_functions
    assert "_section" not in inline_functions
    assert "_plain_card" not in inline_functions
    assert "_inline_section" not in inline_functions


def test_subtitle_property_panel_delegates_shared_input_primitives() -> None:
    panel_path = ROOT / "frontend" / "property_panel.py"
    panel_module = f"{PACKAGE}.frontend.property_panel"
    targets = _import_targets(panel_module, panel_path)

    assert f"{PACKAGE}.frontend.property_inputs" in targets
    tree = ast.parse(panel_path.read_text(encoding="utf-8-sig"))
    inline_names = {
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef)
    }
    assert "_GrowingPlainTextEdit" not in inline_names
    assert "_DynamicStackedWidget" not in inline_names
    assert "_WheelFocusedComboBox" not in inline_names
    assert "_NoWheelSpinBox" not in inline_names
    font_adapter = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and node.name == "_WheelFocusedFontComboBox"
    )
    assert [base.id for base in font_adapter.bases if isinstance(base, ast.Name)] == [
        "WheelFocusedFontComboBox"
    ]
    timecode_adapter = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "_TimecodeEdit"
    )
    assert [
        base.id for base in timecode_adapter.bases if isinstance(base, ast.Name)
    ] == ["TimecodeEdit"]
    spin_adapters = {
        node.name: [
            base.id for base in node.bases if isinstance(base, ast.Name)
        ]
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and node.name in {"_WheelFocusedSpinBox", "_WheelFocusedDoubleSpinBox"}
    }
    assert spin_adapters == {
        "_WheelFocusedSpinBox": ["WheelFocusedSpinBox"],
        "_WheelFocusedDoubleSpinBox": ["WheelFocusedDoubleSpinBox"],
    }
    assert "_UnitProtectedSpinBoxMixin" not in inline_names


def test_subtitle_property_panel_delegates_title_page_construction() -> None:
    panel_path = ROOT / "frontend" / "property_panel.py"
    panel_module = f"{PACKAGE}.frontend.property_panel"
    targets = _import_targets(panel_module, panel_path)

    assert f"{PACKAGE}.frontend.property_title_page" in targets
    tree = ast.parse(panel_path.read_text(encoding="utf-8-sig"))
    panel_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "PropertyPanel"
    )
    delegated = {
        "_make_title_text_section": "make_text_section",
        "_make_title_style_section": "make_style_section",
        "_make_title_time_section": "make_time_section",
    }
    for method_name, builder_method in delegated.items():
        method = next(
            node
            for node in panel_class.body
            if isinstance(node, ast.FunctionDef) and node.name == method_name
        )
        calls = {
            node.func.attr
            for node in ast.walk(method)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "_title_page_builder"
        }
        assert calls == {builder_method}


def test_subtitle_property_panel_delegates_timing_page_construction() -> None:
    panel_path = ROOT / "frontend" / "property_panel.py"
    panel_module = f"{PACKAGE}.frontend.property_panel"
    targets = _import_targets(panel_module, panel_path)

    assert f"{PACKAGE}.frontend.property_timing_page" in targets
    tree = ast.parse(panel_path.read_text(encoding="utf-8-sig"))
    panel_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "PropertyPanel"
    )
    method = next(
        node
        for node in panel_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "_make_timing_section"
    )
    calls = {
        node.func.attr
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "_timing_page_builder"
    }
    assert calls == {"make_section"}


def test_subtitle_property_panel_delegates_background_screen_construction() -> None:
    panel_path = ROOT / "frontend" / "property_panel.py"
    panel_module = f"{PACKAGE}.frontend.property_panel"
    targets = _import_targets(panel_module, panel_path)

    assert f"{PACKAGE}.frontend.property_background_page" in targets
    tree = ast.parse(panel_path.read_text(encoding="utf-8-sig"))
    panel_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "PropertyPanel"
    )
    method = next(
        node
        for node in panel_class.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_make_screen_size_section"
    )
    calls = {
        node.func.attr
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "_background_page_builder"
    }
    assert calls == {"make_screen_size_section"}


def test_subtitle_property_panel_delegates_background_source_construction() -> None:
    panel_path = ROOT / "frontend" / "property_panel.py"
    tree = ast.parse(panel_path.read_text(encoding="utf-8-sig"))
    panel_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "PropertyPanel"
    )
    delegated = {
        "_make_background_source_section": "make_source_section",
        "_make_background_detail_page": "make_detail_page",
    }
    for method_name, builder_method in delegated.items():
        method = next(
            node
            for node in panel_class.body
            if isinstance(node, ast.FunctionDef) and node.name == method_name
        )
        calls = {
            node.func.attr
            for node in ast.walk(method)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "_background_page_builder"
        }
        assert calls == {builder_method}


def test_subtitle_property_panel_delegates_animation_construction() -> None:
    panel_path = ROOT / "frontend" / "property_panel.py"
    panel_module = f"{PACKAGE}.frontend.property_panel"
    targets = _import_targets(panel_module, panel_path)

    assert f"{PACKAGE}.frontend.property_effects_page" in targets
    tree = ast.parse(panel_path.read_text(encoding="utf-8-sig"))
    panel_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "PropertyPanel"
    )
    method = next(
        node
        for node in panel_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "_make_animation_section"
    )
    calls = {
        node.func.attr
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "_effects_page_builder"
    }
    assert calls == {"make_animation_section"}


def test_subtitle_property_panel_delegates_indicator_construction() -> None:
    panel_path = ROOT / "frontend" / "property_panel.py"
    tree = ast.parse(panel_path.read_text(encoding="utf-8-sig"))
    panel_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "PropertyPanel"
    )
    method = next(
        node
        for node in panel_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "_make_lit_section"
    )
    calls = {
        node.func.attr
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "_effects_page_builder"
    }
    assert calls == {"make_lit_section"}


def test_subtitle_property_panel_delegates_viewport_construction() -> None:
    panel_path = ROOT / "frontend" / "property_panel.py"
    panel_module = f"{PACKAGE}.frontend.property_panel"
    targets = _import_targets(panel_module, panel_path)

    assert f"{PACKAGE}.frontend.property_layout_page" in targets
    tree = ast.parse(panel_path.read_text(encoding="utf-8-sig"))
    panel_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "PropertyPanel"
    )
    method = next(
        node
        for node in panel_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "_make_viewport_section"
    )
    calls = {
        node.func.attr
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "_layout_page_builder"
    }
    assert calls == {"make_viewport_section"}


def test_subtitle_property_panel_delegates_vertical_layout_construction() -> None:
    panel_path = ROOT / "frontend" / "property_panel.py"
    tree = ast.parse(panel_path.read_text(encoding="utf-8-sig"))
    panel_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "PropertyPanel"
    )
    method = next(
        node
        for node in panel_class.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_make_vertical_layout_section"
    )
    calls = {
        node.func.attr
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "_layout_page_builder"
    }
    assert calls == {"make_vertical_section"}


def test_subtitle_property_panel_delegates_ruby_layout_construction() -> None:
    panel_path = ROOT / "frontend" / "property_panel.py"
    tree = ast.parse(panel_path.read_text(encoding="utf-8-sig"))
    panel_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "PropertyPanel"
    )
    method = next(
        node
        for node in panel_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "_make_ruby_section"
    )
    calls = {
        node.func.attr
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "_layout_page_builder"
    }
    assert calls == {"make_ruby_section"}


def test_subtitle_property_panel_delegates_row_structure_construction() -> None:
    panel_path = ROOT / "frontend" / "property_panel.py"
    tree = ast.parse(panel_path.read_text(encoding="utf-8-sig"))
    panel_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "PropertyPanel"
    )
    method = next(
        node
        for node in panel_class.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_make_row_structure_section"
    )
    calls = {
        node.func.attr
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "_layout_page_builder"
    }
    assert calls == {"make_row_structure_section"}


def test_subtitle_property_panel_delegates_role_page_composition() -> None:
    panel_path = ROOT / "frontend" / "property_panel.py"
    panel_module = f"{PACKAGE}.frontend.property_panel"
    targets = _import_targets(panel_module, panel_path)

    assert f"{PACKAGE}.frontend.property_role_page" in targets
    tree = ast.parse(panel_path.read_text(encoding="utf-8-sig"))
    panel_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "PropertyPanel"
    )
    method = next(
        node
        for node in panel_class.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_make_font_color_section"
    )
    calls = {
        node.func.attr
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "_role_page_builder"
    }
    assert calls == {"make_font_color_section"}


def test_subtitle_property_panel_delegates_role_font_construction() -> None:
    panel_path = ROOT / "frontend" / "property_panel.py"
    tree = ast.parse(panel_path.read_text(encoding="utf-8-sig"))
    panel_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "PropertyPanel"
    )
    method = next(
        node
        for node in panel_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "_make_font_section"
    )
    calls = {
        node.func.attr
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "_role_page_builder"
    }
    assert calls == {"make_font_section"}


def test_subtitle_property_panel_delegates_role_font_settings_page() -> None:
    panel_path = ROOT / "frontend" / "property_panel.py"
    panel_module = f"{PACKAGE}.frontend.property_panel"
    targets = _import_targets(panel_module, panel_path)

    assert f"{PACKAGE}.frontend.property_role_font_page" in targets
    tree = ast.parse(panel_path.read_text(encoding="utf-8-sig"))
    panel_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "PropertyPanel"
    )
    method = next(
        node
        for node in panel_class.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_make_font_settings_page"
    )
    calls = {
        node.func.attr
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "_role_font_page_builder"
    }
    assert calls == {"make_page"}


def test_subtitle_property_panel_delegates_role_color_construction() -> None:
    panel_path = ROOT / "frontend" / "property_panel.py"
    panel_module = f"{PACKAGE}.frontend.property_panel"
    targets = _import_targets(panel_module, panel_path)

    assert f"{PACKAGE}.frontend.property_role_color_page" in targets
    tree = ast.parse(panel_path.read_text(encoding="utf-8-sig"))
    panel_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "PropertyPanel"
    )
    method = next(
        node
        for node in panel_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "_make_color_section"
    )
    calls = {
        node.func.attr
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "_role_color_page_builder"
    }
    assert calls == {"make_section"}


def test_subtitle_property_panel_delegates_solid_fill_editor() -> None:
    panel_path = ROOT / "frontend" / "property_panel.py"
    panel_module = f"{PACKAGE}.frontend.property_panel"
    targets = _import_targets(panel_module, panel_path)

    assert f"{PACKAGE}.frontend.property_role_fill_pages" in targets
    tree = ast.parse(panel_path.read_text(encoding="utf-8-sig"))
    panel_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "PropertyPanel"
    )
    method = next(
        node
        for node in panel_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "_make_solid_fill_page"
    )
    calls = {
        node.func.attr
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "_role_fill_pages_builder"
    }
    assert calls == {"make_solid_page"}


def test_subtitle_property_panel_delegates_gradient_fill_editor() -> None:
    panel_path = ROOT / "frontend" / "property_panel.py"
    tree = ast.parse(panel_path.read_text(encoding="utf-8-sig"))
    panel_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "PropertyPanel"
    )
    method = next(
        node
        for node in panel_class.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_make_gradient_fill_page"
    )
    calls = {
        node.func.attr
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "_role_fill_pages_builder"
    }
    assert calls == {"make_gradient_page"}


def test_subtitle_property_panel_delegates_split_fill_editor() -> None:
    panel_path = ROOT / "frontend" / "property_panel.py"
    tree = ast.parse(panel_path.read_text(encoding="utf-8-sig"))
    panel_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "PropertyPanel"
    )
    method = next(
        node
        for node in panel_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "_make_split_fill_page"
    )
    calls = {
        node.func.attr
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "_role_fill_pages_builder"
    }
    assert calls == {"make_split_page"}


def test_subtitle_property_panel_delegates_image_fill_editor() -> None:
    panel_path = ROOT / "frontend" / "property_panel.py"
    tree = ast.parse(panel_path.read_text(encoding="utf-8-sig"))
    panel_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "PropertyPanel"
    )
    method = next(
        node
        for node in panel_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "_make_image_fill_page"
    )
    calls = {
        node.func.attr
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "_role_fill_pages_builder"
    }
    assert calls == {"make_image_page"}


def test_subtitle_render_window_delegates_missing_resource_state() -> None:
    window_path = ROOT / "frontend" / "main_window.py"
    tree = ast.parse(window_path.read_text(encoding="utf-8-sig"))
    window_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SubtitleRenderWindow"
    )
    delegated_methods = {
        "_merge_unresolved_resource_references": (
            "merge_unresolved_resource_references"
        ),
        "_resolve_unresolved_resource_labels": "resolve_missing_resource_labels",
    }
    for method_name, expected_call in delegated_methods.items():
        method = next(
            node
            for node in window_class.body
            if isinstance(node, ast.FunctionDef) and node.name == method_name
        )
        calls = {
            node.func.attr
            for node in ast.walk(method)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "_project_session"
        }
        assert expected_call in calls


def test_subtitle_render_window_delegates_project_save_state() -> None:
    window_path = ROOT / "frontend" / "main_window.py"
    tree = ast.parse(window_path.read_text(encoding="utf-8-sig"))
    window_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SubtitleRenderWindow"
    )
    method = next(
        node
        for node in window_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "_write_project"
    )
    session_calls = {
        node.func.attr
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "_project_session"
    }
    assert {
        "begin_save",
        "complete_save",
        "fail_save",
        "record_save_inspection_failure",
    } <= session_calls
    direct_state_assignments = {
        target.attr
        for node in ast.walk(method)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (
            node.targets if isinstance(node, ast.Assign) else [node.target]
        )
        if isinstance(target, ast.Attribute)
        and target.attr in {
            "_project_disk_revision",
            "_project_path",
            "_project_save_error",
            "_project_saving",
            "_saved_revision",
        }
    }
    assert direct_state_assignments == set()


def test_subtitle_render_window_delegates_project_identity_adoption() -> None:
    window_path = ROOT / "frontend" / "main_window.py"
    tree = ast.parse(window_path.read_text(encoding="utf-8-sig"))
    window_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SubtitleRenderWindow"
    )
    method_names = {
        "_new_project",
        "_open_project_path",
        "_import_n3_project_path",
        "_restore_recovery_candidate",
    }
    for method in window_class.body:
        if not isinstance(method, ast.FunctionDef) or method.name not in method_names:
            continue
        session_calls = {
            node.func.attr
            for node in ast.walk(method)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "_project_session"
        }
        assert "adopt_project_identity" in session_calls
        direct_identity_assignments = {
            target.attr
            for node in ast.walk(method)
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            for target in (
                node.targets if isinstance(node, ast.Assign) else [node.target]
            )
            if isinstance(target, ast.Attribute)
            and target.attr in {"_project_disk_revision", "_project_path"}
        }
        assert direct_identity_assignments == set()
