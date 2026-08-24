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
        ROOT / "project_recovery.py",
        ROOT / "project_resources.py",
        ROOT / "recent_projects.py",
        ROOT / "screen_settings.py",
        ROOT / "session.py",
        ROOT / "settings_store.py",
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
