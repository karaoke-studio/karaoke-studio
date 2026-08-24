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
        ROOT / "engine" / "export" / "export_command.py",
        ROOT / "engine" / "export" / "parallel_schedule.py",
        ROOT / "engine" / "render" / "raster_blur.py",
        ROOT / "engine" / "export" / "render_job.py",
        ROOT / "engine" / "export" / "render_job_policy.py",
        ROOT / "engine" / "render" / "render_bands.py",
        ROOT / "engine" / "ruby" / "timing.py",
        ROOT / "paint.py",
        ROOT / "serialization" / "paint.py",
        ROOT / "project" / "controller.py",
        ROOT / "project" / "load.py",
        ROOT / "project" / "recovery.py",
        ROOT / "project" / "resources.py",
        ROOT / "project" / "recent.py",
        ROOT / "settings" / "screen.py",
        ROOT / "project" / "session.py",
        ROOT / "settings" / "store.py",
        ROOT / "sources" / "loader.py",
        ROOT / "engine" / "timing" / "timecode.py",
        ROOT / "timing.py",
        ROOT / "serialization" / "timing.py",
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
        if path == ROOT / "models.py" or path == ROOT / "serialization" / "timing.py":
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
        "krok_helper.subtitle_render.native.backend",
    )

    assert not {
        target
        for target in targets
        if any(target == root or target.startswith(f"{root}.") for root in forbidden_roots)
    }


def test_subtitle_render_internal_import_graph_is_acyclic() -> None:
    assert _dependency_cycles(_internal_dependencies()) == []


def test_native_modules_are_grouped_behind_one_package_boundary() -> None:
    native_root = ROOT / "native"
    assert {"__init__.py", "backend.py", "protocol.py"} <= {
        path.name for path in native_root.glob("*.py")
    }
    assert not (ROOT / "native_backend.py").exists()
    assert not (ROOT / "native_protocol.py").exists()


def test_n3_compatibility_modules_are_grouped_behind_one_package_boundary() -> None:
    module_names = {
        "font_catalog.py",
        "font_fallback.py",
        "font_scheme.py",
        "project_import.py",
        "template_import.py",
    }
    n3_root = ROOT / "n3"
    assert {"__init__.py", *module_names} <= {
        path.name for path in n3_root.glob("*.py")
    }
    assert not any(
        (ROOT / name).exists()
        for name in {
            "n3_font_catalog.py",
            "n3_font_fallback.py",
            "n3_font_scheme.py",
            "n3_template_import.py",
            "n3proj_import.py",
        }
    )


def test_project_modules_are_grouped_behind_one_package_boundary() -> None:
    module_names = {
        "controller.py",
        "load.py",
        "recent.py",
        "recovery.py",
        "resources.py",
        "session.py",
        "store.py",
    }
    project_root = ROOT / "project"
    assert {"__init__.py", *module_names} <= {
        path.name for path in project_root.glob("*.py")
    }
    assert not any(
        (ROOT / name).exists()
        for name in {
            "project_controller.py",
            "project_load.py",
            "project_recovery.py",
            "project_resources.py",
            "project_store.py",
            "recent_projects.py",
            "session.py",
        }
    )


def test_settings_modules_are_grouped_behind_one_package_boundary() -> None:
    module_names = {
        "bridge.py",
        "preferences.py",
        "property_controllers.py",
        "screen.py",
        "store.py",
    }
    settings_root = ROOT / "settings"
    assert {"__init__.py", *module_names} <= {
        path.name for path in settings_root.glob("*.py")
    }
    assert not any(
        (ROOT / name).exists()
        for name in {
            "preferences.py",
            "property_controllers.py",
            "screen_settings.py",
            "settings_bridge.py",
            "settings_store.py",
        }
    )


def test_serialization_modules_are_grouped_behind_one_package_boundary() -> None:
    serialization_root = ROOT / "serialization"
    assert {"__init__.py", "compat.py", "paint.py", "timing.py"} <= {
        path.name for path in serialization_root.glob("*.py")
    }
    assert not (ROOT / "forward_compat.py").exists()
    assert not (ROOT / "paint_codec.py").exists()
    assert not (ROOT / "timing_codec.py").exists()


def test_source_modules_are_grouped_behind_one_package_boundary() -> None:
    module_names = {
        "guide_symbols.py",
        "loader.py",
        "reload.py",
        "subtitles.py",
        "sug.py",
    }
    sources_root = ROOT / "sources"
    assert {"__init__.py", *module_names} <= {
        path.name for path in sources_root.glob("*.py")
    }
    assert not any(
        (ROOT / name).exists()
        for name in {
            "guide_symbols.py",
            "source_loader.py",
            "source_reload.py",
            "subtitle_sources.py",
            "sug_project.py",
        }
    )


def test_line_style_semantics_have_one_engine_owner() -> None:
    owner = f"{PACKAGE}.engine.layout.line_style"
    delegated_names = {
        "_lane_count",
        "_layout_style_for_line",
        "_row_count_resolver",
    }
    for relative_path in (
        Path("engine/layout/layout_assignment.py"),
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

    line_style_targets = _import_targets(
        owner,
        ROOT / "engine/layout/line_style.py",
    )
    assert f"{PACKAGE}.engine.painter" not in line_style_targets

    painter_tree = ast.parse(
        (ROOT / "engine/painter.py").read_text(encoding="utf-8-sig")
    )
    painter_functions = {
        node.name
        for node in painter_tree.body
        if isinstance(node, ast.FunctionDef)
    }
    assert {
        "_auto_entry_reserve_ms",
        "_auto_exit_reserve_ms",
        "_bottom_align_resolver",
        "_display_line_compute_kwargs",
        "_effective_line_protect_ms",
        "_entry_animation_resolver",
        "_exit_animation_resolver",
        "_vertical_position_resolver",
    }.isdisjoint(painter_functions)


def test_layout_plan_builder_has_no_painter_dependency() -> None:
    module = f"{PACKAGE}.engine.layout.layout_plan_builder"
    targets = _import_targets(
        module,
        ROOT / "engine/layout/layout_plan_builder.py",
    )

    assert f"{PACKAGE}.engine.painter" not in targets


def test_guide_render_semantics_have_one_engine_owner() -> None:
    owner = f"{PACKAGE}.engine.guide"
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
    assert {
        ("guide_symbol_is_bitmap", "_guide_symbol_is_bitmap"),
        ("render_line_with_guide_symbols", "_line_with_guide_symbol"),
    } <= delegated
    targets = _import_targets(
        f"{owner}.semantics",
        ROOT / "engine/guide/semantics.py",
    )
    assert f"{PACKAGE}.engine.painter" not in targets


def test_line_pagination_semantics_have_one_engine_owner() -> None:
    owner = f"{PACKAGE}.engine.layout.line_pagination"
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
    targets = _import_targets(owner, ROOT / "engine/layout/line_pagination.py")
    assert f"{PACKAGE}.engine.painter" not in targets


def test_line_geometry_policy_has_no_painter_dependency() -> None:
    owner = f"{PACKAGE}.engine.layout.line_geometry"
    painter_path = ROOT / "engine/painter.py"
    painter_tree = ast.parse(painter_path.read_text(encoding="utf-8-sig"))
    inline = {
        node.name
        for node in painter_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    imports = {
        (alias.name, alias.asname)
        for node in painter_tree.body
        if isinstance(node, ast.ImportFrom) and node.module == owner
        for alias in node.names
    }

    assert "_line_has_role_labels" not in inline
    assert imports == {
        ("line_has_role_labels", "_line_has_role_labels"),
    }
    targets = _import_targets(owner, ROOT / "engine/layout/line_geometry.py")
    assert f"{PACKAGE}.engine.painter" not in targets


def test_signal_semantics_have_one_engine_owner() -> None:
    owner = f"{PACKAGE}.engine.layout.signal_semantics"
    painter_path = ROOT / "engine/painter.py"
    painter_tree = ast.parse(painter_path.read_text(encoding="utf-8-sig"))
    delegated_names = {
        "_display_style_for_signal_window",
        "_lit_signal_active",
        "_resolve_signal_display_lines",
        "_signal_head_context",
        "_signal_lead_in_ms",
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
    targets = _import_targets(owner, ROOT / "engine/layout/signal_semantics.py")
    assert f"{PACKAGE}.engine.painter" not in targets

    adapter = next(
        node
        for node in painter_tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_signal_display_lines_for_style"
    )
    calls = {
        node.func.id
        for node in ast.walk(adapter)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "_resolve_signal_display_lines" in calls


def test_display_schedule_projection_has_no_painter_dependency() -> None:
    owner = f"{PACKAGE}.engine.layout.display_schedule"
    schedule_path = ROOT / "engine/layout/display_schedule.py"
    targets = _import_targets(owner, schedule_path)

    assert f"{PACKAGE}.engine.painter" not in targets

    tree = ast.parse(schedule_path.read_text(encoding="utf-8-sig"))
    resolver_fields = {
        node.target.id
        for class_node in tree.body
        if isinstance(class_node, ast.ClassDef)
        and class_node.name == "DisplayScheduleResolvers"
        for node in class_node.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    assert resolver_fields == {"display_lines"}


def test_painter_delegates_display_schedule_projection() -> None:
    painter_path = ROOT / "engine/painter.py"
    tree = ast.parse(painter_path.read_text(encoding="utf-8-sig"))
    methods = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {
            "display_schedule_for_style",
            "display_windows_for_style",
        }
    }
    calls = {
        name: {
            node.func.id
            for node in ast.walk(method)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        for name, method in methods.items()
    }
    assert "resolve_display_windows" in calls["display_windows_for_style"]
    assert "resolve_display_schedule" in calls["display_schedule_for_style"]

    visible_adapter = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_visible_lines_for_style"
    )
    visible_calls = {
        node.func.id
        for node in ast.walk(visible_adapter)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "resolve_visible_display_lines" in visible_calls

    inline_functions = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }
    assert "_apply_constrained_page_sync" not in inline_functions
    assert "_extend_page_display_boundary" not in inline_functions
    assert "_single_visible_display_line" not in inline_functions


def test_display_resolver_has_no_painter_dependency() -> None:
    owner = f"{PACKAGE}.engine.layout.display_resolver"
    path = ROOT / "engine/layout/display_resolver.py"
    targets = _import_targets(owner, path)

    assert f"{PACKAGE}.engine.painter" not in targets


def test_painter_delegates_display_resolution_orchestration() -> None:
    painter_path = ROOT / "engine/painter.py"
    painter_module = f"{PACKAGE}.engine.painter"
    targets = _import_targets(painter_module, painter_path)

    assert f"{PACKAGE}.engine.layout.display_resolver" in targets
    tree = ast.parse(painter_path.read_text(encoding="utf-8-sig"))
    method = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "display_lines_for_style"
    )
    calls = {
        node.func.id
        for node in ast.walk(method)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "resolve_display_lines_for_style" in calls
    assert "StyleDisplayResolutionPorts" in calls

    guard_factory = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "animation_guard_ports_for_style"
    )
    guard_factory_calls = {
        node.func.id
        for node in ast.walk(guard_factory)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "AnimationGuardPorts" in guard_factory_calls
    inline_functions = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }
    assert "_apply_animation_time_guard" not in inline_functions
    assert "_display_lines_for_style" not in inline_functions

    resolver_path = ROOT / "engine/layout/display_resolver.py"
    resolver_tree = ast.parse(resolver_path.read_text(encoding="utf-8-sig"))
    resolver_functions = {
        node.name
        for node in resolver_tree.body
        if isinstance(node, ast.FunctionDef)
    }
    assert "apply_animation_time_guard" in resolver_functions
    assert "resolve_display_lines_for_style" in resolver_functions
    assert "resolve_display_timing" in resolver_functions
    assert "_resolve_page_sync_and_collisions" not in inline_functions

    painter_source = painter_path.read_text(encoding="utf-8-sig")
    assert "_DISPLAY_LINE_RESOLUTION_CACHE" not in painter_source
    assert "clear_display_line_resolution_cache" in painter_source


def test_layout_diagnostic_contracts_have_one_layout_owner() -> None:
    painter_path = ROOT / "engine/painter.py"
    painter_module = f"{PACKAGE}.engine.painter"
    targets = _import_targets(painter_module, painter_path)

    assert f"{PACKAGE}.engine.layout.layout_diagnostics" not in targets
    diagnostics_path = ROOT / "engine/layout/layout_diagnostics.py"
    diagnostics_targets = _import_targets(
        f"{PACKAGE}.engine.layout.layout_diagnostics",
        diagnostics_path,
    )
    assert f"{PACKAGE}.engine.painter" not in diagnostics_targets
    tree = ast.parse(painter_path.read_text(encoding="utf-8-sig"))
    inline_contracts = {
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and node.name
        in {
            "LayoutMarginWarning",
            "LayoutTimingDiagnostic",
            "_TimingCollisionAdjustment",
        }
    }
    assert inline_contracts == set()


def test_layout_diagnostics_backend_binds_layout_margin_policy() -> None:
    adapter_path = ROOT / "engine/render/layout_diagnostics_backend.py"
    tree = ast.parse(adapter_path.read_text(encoding="utf-8-sig"))
    method = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "check_layout_margins"
    )
    calls = {
        node.func.id
        for node in ast.walk(method)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "resolve_layout_margin_warnings" in calls
    painter_calls = {
        node.func.attr
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "painter_impl"
    }
    assert painter_calls == {
        "display_lines_for_style",
        "measure_display_line_horizontal_bounds",
    }

    painter_path = ROOT / "engine/painter.py"
    painter_tree = ast.parse(painter_path.read_text(encoding="utf-8-sig"))
    inline_functions = {
        node.name
        for node in painter_tree.body
        if isinstance(node, ast.FunctionDef)
    }
    assert "check_layout_margins" not in inline_functions


def test_layout_diagnostics_backend_binds_timing_diagnostic_policy() -> None:
    adapter_path = ROOT / "engine/render/layout_diagnostics_backend.py"
    tree = ast.parse(adapter_path.read_text(encoding="utf-8-sig"))
    method = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "layout_timing_diagnostics_for_style"
    )
    calls = {
        node.func.id
        for node in ast.walk(method)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert {
        "build_force_bottom_diagnostics",
        "build_page_shift_diagnostics",
        "build_timing_window_diagnostics",
    } <= calls
    private_painter_calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "painter_impl"
    }
    assert "_apply_animation_time_guard" not in private_painter_calls
    assert {
        "_display_lines_for_style",
        "_display_style_for_signal_window",
        "_line_center_override",
        "_line_total_width",
        "_resolve_line_x_smart",
        "_signal_head_context",
        "_signal_lead_in_ms",
        "_style_for_line",
    }.isdisjoint(private_painter_calls)
    assert all(not name.startswith("_") for name in private_painter_calls)

    painter_path = ROOT / "engine/painter.py"
    painter_tree = ast.parse(painter_path.read_text(encoding="utf-8-sig"))
    inline_functions = {
        node.name
        for node in painter_tree.body
        if isinstance(node, ast.FunctionDef)
    }
    assert "layout_timing_diagnostics_for_style" not in inline_functions


def test_layout_plan_orchestrator_has_explicit_painter_free_resolvers() -> None:
    owner = f"{PACKAGE}.engine.layout.layout_plan_orchestrator"
    targets = _import_targets(
        owner,
        ROOT / "engine/layout/layout_plan_orchestrator.py",
    )

    assert f"{PACKAGE}.engine.painter" not in targets

    source = (ROOT / "engine/layout/layout_plan_orchestrator.py").read_text(
        encoding="utf-8-sig"
    )
    tree = ast.parse(source)
    resolver_fields = {
        node.target.id
        for class_node in tree.body
        if isinstance(class_node, ast.ClassDef)
        and class_node.name == "LayoutPlanResolvers"
        for node in class_node.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    assert resolver_fields == {
        "display_lines",
        "page_offset_windows",
    }


def test_text_metrics_have_one_engine_owner() -> None:
    owner = f"{PACKAGE}.engine.text"
    painter_path = ROOT / "engine/painter.py"
    painter_tree = ast.parse(painter_path.read_text(encoding="utf-8-sig"))
    delegated_names = {
        "_build_font",
        "_build_latin_font",
        "_char_layout_width",
        "_char_path_left_offset",
        "_letter_spacing",
        "_line_text_width",
        "_make_font_for",
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
    assert delegated_names <= imported
    targets = _import_targets(
        f"{owner}.metrics",
        ROOT / "engine/text/metrics.py",
    )
    assert f"{PACKAGE}.engine.painter" not in targets


def test_qt_line_geometry_has_no_painter_dependency() -> None:
    owner = f"{PACKAGE}.engine.layout.qt_line_geometry"
    targets = _import_targets(owner, ROOT / "engine/layout/qt_line_geometry.py")

    assert f"{PACKAGE}.engine.painter" not in targets

    painter_tree = ast.parse(
        (ROOT / "engine/painter.py").read_text(encoding="utf-8-sig")
    )
    imported = {
        alias.name
        for node in painter_tree.body
        if isinstance(node, ast.ImportFrom) and node.module == owner
        for alias in node.names
    }
    assert "resolved_char_intervals_for_line" in imported
    assert "resolved_guide_anchor_bounds_for_line" in imported


def test_page_offset_plan_has_no_painter_dependency() -> None:
    owner = f"{PACKAGE}.engine.layout.page_offset_plan"
    plan_path = ROOT / "engine/layout/page_offset_plan.py"
    targets = _import_targets(owner, plan_path)

    assert f"{PACKAGE}.engine.painter" not in targets

    tree = ast.parse(plan_path.read_text(encoding="utf-8-sig"))
    resolver_fields = {
        node.target.id
        for class_node in tree.body
        if isinstance(class_node, ast.ClassDef)
        and class_node.name == "PageOffsetResolvers"
        for node in class_node.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    assert resolver_fields == {"display_lines", "measure_lines"}
    method = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "resolve_page_offset_windows"
    )
    calls = {
        node.func.id
        for node in ast.walk(method)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert {
        "build_page_offset_windows",
        "cached_page_offset_windows",
        "store_page_offset_windows",
    } <= calls

    selector = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "page_offsets_at_time"
    )
    assert any(
        isinstance(node, ast.Compare)
        for node in ast.walk(selector)
    )


def test_painter_delegates_page_offset_policy() -> None:
    painter_path = ROOT / "engine/painter.py"
    tree = ast.parse(painter_path.read_text(encoding="utf-8-sig"))
    method = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "resolved_page_offset_windows_for_style"
    )
    calls = {
        node.func.id
        for node in ast.walk(method)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "resolve_page_offset_windows" in calls
    assert {
        "build_page_offset_windows",
        "cached_page_offset_windows",
        "store_page_offset_windows",
    }.isdisjoint(calls)

    selector = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "resolved_page_offsets_for_style"
    )
    selector_calls = {
        node.func.id
        for node in ast.walk(selector)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "page_offsets_at_time" in selector_calls


def test_layout_plan_projection_has_no_painter_dependency() -> None:
    owner = f"{PACKAGE}.engine.layout.layout_plan_projection"
    targets = _import_targets(
        owner,
        ROOT / "engine/layout/layout_plan_projection.py",
    )

    assert f"{PACKAGE}.engine.painter" not in targets


def test_guide_metrics_and_image_resources_have_no_painter_dependency() -> None:
    modules = (
        (f"{PACKAGE}.engine.guide.metrics", ROOT / "engine/guide/metrics.py"),
        (
            f"{PACKAGE}.engine.render.image_resource",
            ROOT / "engine/render/image_resource.py",
        ),
    )
    for owner, path in modules:
        targets = _import_targets(owner, path)

        assert f"{PACKAGE}.engine.painter" not in targets


def test_guide_engine_modules_are_grouped_in_one_domain_package() -> None:
    guide_root = ROOT / "engine" / "guide"
    assert {"__init__.py", "metrics.py", "semantics.py"} <= {
        path.name for path in guide_root.glob("*.py")
    }
    assert not any(
        (ROOT / "engine" / name).exists()
        for name in ("guide_metrics.py", "guide_semantics.py")
    )


def test_text_layout_has_one_engine_owner() -> None:
    owner = f"{PACKAGE}.engine.text"
    painter_path = ROOT / "engine/painter.py"
    painter_tree = ast.parse(painter_path.read_text(encoding="utf-8-sig"))
    delegated_names = {
        "_build_role_text_layout",
        "_build_text_layout",
        "_char_left_positions",
        "_main_script_stroke_style",
        "_role_char_geometry_by_index",
        "_style_for_role_in_layout",
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
    assert delegated_names <= imported
    targets = _import_targets(
        f"{owner}.layout",
        ROOT / "engine/text/layout.py",
    )
    assert f"{PACKAGE}.engine.painter" not in targets


def test_text_engine_modules_are_grouped_in_one_domain_package() -> None:
    text_root = ROOT / "engine" / "text"
    assert {"__init__.py", "layout.py", "metrics.py"} <= {
        path.name for path in text_root.glob("*.py")
    }
    assert not any(
        (ROOT / "engine" / name).exists()
        for name in ("text_layout.py", "text_metrics.py")
    )


def test_layout_engine_modules_are_grouped_in_one_domain_package() -> None:
    module_names = {
        "display_schedule.py",
        "display_resolver.py",
        "layout_assignment.py",
        "layout_context.py",
        "layout_diagnostics.py",
        "layout_plan.py",
        "layout_plan_builder.py",
        "layout_plan_cache.py",
        "layout_plan_orchestrator.py",
        "layout_plan_projection.py",
        "line_geometry.py",
        "line_pagination.py",
        "line_style.py",
        "page_offset_plan.py",
        "page_placement.py",
        "page_plan.py",
        "qt_line_geometry.py",
        "semantic_plan.py",
        "signal_semantics.py",
    }
    layout_root = ROOT / "engine" / "layout"
    assert {"__init__.py", *module_names} <= {
        path.name for path in layout_root.glob("*.py")
    }
    assert not any((ROOT / "engine" / name).exists() for name in module_names)


def test_layout_package_has_no_painter_dependency() -> None:
    painter_module = f"{PACKAGE}.engine.painter"
    layout_root = ROOT / "engine" / "layout"

    offenders = {
        path.name
        for path in layout_root.glob("*.py")
        if painter_module
        in _import_targets(
            f"{PACKAGE}.engine.layout.{path.stem}",
            path,
        )
    }

    assert offenders == set()


def test_export_engine_modules_are_grouped_in_one_domain_package() -> None:
    module_names = {
        "encoder_select.py",
        "export_command.py",
        "native_export.py",
        "parallel_schedule.py",
        "render_job.py",
        "render_job_policy.py",
    }
    export_root = ROOT / "engine" / "export"
    assert {"__init__.py", *module_names} <= {
        path.name for path in export_root.glob("*.py")
    }
    assert not any((ROOT / "engine" / name).exists() for name in module_names)


def test_render_engine_modules_are_grouped_in_one_domain_package() -> None:
    module_names = {
        "animator.py",
        "image_resource.py",
        "layers.py",
        "layout_diagnostics_backend.py",
        "layout_plan_backend.py",
        "quantize.py",
        "raster_blur.py",
        "render_bands.py",
        "render_ir.py",
        "signal.py",
        "timeline_projection_backend.py",
        "title.py",
    }
    render_root = ROOT / "engine" / "render"
    assert {"__init__.py", *module_names} <= {
        path.name for path in render_root.glob("*.py")
    }
    assert not any((ROOT / "engine" / name).exists() for name in module_names)


def test_title_layout_has_one_render_owner() -> None:
    owner = f"{PACKAGE}.engine.render.title"
    painter_path = ROOT / "engine/painter.py"
    painter_tree = ast.parse(painter_path.read_text(encoding="utf-8-sig"))
    delegated_names = {
        "_TitleGlyphLayout",
        "_TitleOverlayLayout",
        "_build_title_font",
        "_build_title_latin_font",
        "_layout_title_overlay",
        "_make_title_font_for",
        "_make_title_overlay_layer",
        "_title_block_origin",
    }
    inline = {
        node.name
        for node in painter_tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    } & delegated_names
    imported = {
        alias.asname
        for node in painter_tree.body
        if isinstance(node, ast.ImportFrom) and node.module == owner
        for alias in node.names
    }

    assert inline == set()
    assert delegated_names <= imported
    assert {
        "_TitleOverlayLayer",
        "_build_title_overlay_layer",
        "_title_overlay_layer_key",
    }.isdisjoint(
        node.name
        for node in painter_tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    )
    assert f"{PACKAGE}.engine.painter" not in _import_targets(
        owner,
        ROOT / "engine/render/title.py",
    )


def test_signal_geometry_has_one_render_owner() -> None:
    owner = f"{PACKAGE}.engine.render.signal"
    painter_path = ROOT / "engine/painter.py"
    painter_tree = ast.parse(painter_path.read_text(encoding="utf-8-sig"))
    delegated_names = {
        "_SignalLayoutMetrics",
        "_SignalLitGroup",
        "_VolumeSignalGeometry",
        "_line_has_active_signal",
        "_lit_extinguish_transition_state",
        "_lit_transition_state",
        "_paint_signal_lits_with_ports",
        "_resolve_active_lit_indices",
        "_resolve_signal_layers_with_ports",
        "_resolve_signal_lit_groups",
        "_shape_active_index_and_phase",
        "_signal_layout_metrics",
        "_signal_lit_x",
        "_signal_lit_y",
        "_signal_local_x",
        "_signal_offset_x",
        "_signal_stroke_extent",
        "_volume_active_index_and_phase",
        "_volume_flash_alpha",
        "_volume_signal_column_rects",
        "_volume_signal_geometry",
        "_volume_signal_state",
    }
    inline = {
        node.name
        for node in painter_tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    } & delegated_names
    imported = {
        alias.asname
        for node in painter_tree.body
        if isinstance(node, ast.ImportFrom) and node.module == owner
        for alias in node.names
    }

    assert inline == set()
    assert delegated_names <= imported
    assert {
        "_SignalLitsLayer",
        "_draw_lit_shape",
        "_draw_lit_shape_raw",
        "_draw_volume_column",
        "_draw_volume_lit_group",
        "_paint_shape_signal_group",
        "_shape_signal_vertical_bounds",
        "_volume_signal_vertical_bounds",
        "_signal_layer_stack",
    }.isdisjoint(
        node.name
        for node in painter_tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    )
    assert f"{PACKAGE}.engine.painter" not in _import_targets(
        owner,
        ROOT / "engine/render/signal.py",
    )


def test_timing_engine_modules_are_grouped_in_one_domain_package() -> None:
    module_names = {"auto_chorus.py", "show_time.py", "timecode.py", "timeline.py"}
    timing_root = ROOT / "engine" / "timing"
    assert {"__init__.py", *module_names} <= {
        path.name for path in timing_root.glob("*.py")
    }
    assert not any((ROOT / "engine" / name).exists() for name in module_names)
    assert not (ROOT / "auto_chorus.py").exists()
    assert not (ROOT / "timecode.py").exists()


def test_style_engine_modules_are_grouped_in_one_domain_package() -> None:
    module_names = {
        "style_preview.py",
        "style_semantics.py",
        "title_semantics.py",
    }
    style_root = ROOT / "engine" / "style"
    assert {"__init__.py", *module_names} <= {
        path.name for path in style_root.glob("*.py")
    }
    assert not any((ROOT / "engine" / name).exists() for name in module_names)


def test_ruby_selection_has_one_engine_owner() -> None:
    owner = f"{PACKAGE}.engine.ruby"
    painter_path = ROOT / "engine/painter.py"
    painter_tree = ast.parse(painter_path.read_text(encoding="utf-8-sig"))
    delegated_names = {
        "_active_rubies_for_line",
        "_effective_ruby_for_target",
        "_find_ruby_text_indices",
        "_find_ruby_text_span",
        "_ruby_explicit_target_indices",
        "_ruby_owns_line",
        "_ruby_target_indices",
        "_ruby_target_x_range",
        "_ruby_text_span_x_range",
        "_ruby_time_indices",
        "_text_span_indices",
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
    assert delegated_names <= imported
    targets = _import_targets(
        f"{owner}.selection",
        ROOT / "engine/ruby/selection.py",
    )
    assert f"{PACKAGE}.engine.painter" not in targets


def test_ruby_style_has_one_engine_owner() -> None:
    owner = f"{PACKAGE}.engine.ruby"
    painter_path = ROOT / "engine/painter.py"
    painter_tree = ast.parse(painter_path.read_text(encoding="utf-8-sig"))
    delegated_names = {
        "_build_ruby_font",
        "_build_ruby_font_for_text",
        "_ruby_font_size",
        "_ruby_scale",
        "_ruby_script_stroke_style",
        "_ruby_style_for_target_indices",
        "_ruby_stroke2_enabled",
        "_ruby_stroke2_width",
        "_ruby_stroke2_width_value",
        "_ruby_stroke_width",
        "_ruby_uses_main_font",
        "_scaled_px",
        "_scaled_signed_px",
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
    assert delegated_names <= imported
    targets = _import_targets(
        f"{owner}.style",
        ROOT / "engine/ruby/style.py",
    )
    assert f"{PACKAGE}.engine.painter" not in targets


def test_ruby_layout_has_one_engine_owner() -> None:
    owner = f"{PACKAGE}.engine.ruby"
    painter_path = ROOT / "engine/painter.py"
    painter_tree = ast.parse(painter_path.read_text(encoding="utf-8-sig"))
    delegated_names = {
        "_resolve_ruby_alignment",
        "_ruby_char_gaps",
        "_ruby_interval_px",
        "_ruby_layout_draw_bounds",
        "_ruby_layout_gap",
        "_ruby_layout_left_offset",
        "_ruby_layout_left_overhang",
        "_ruby_layout_origins",
        "_ruby_layout_units",
        "_ruby_layout_width",
        "_ruby_unit_layouts",
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
    assert delegated_names <= imported
    targets = _import_targets(
        f"{owner}.layout",
        ROOT / "engine/ruby/layout.py",
    )
    assert f"{PACKAGE}.engine.painter" not in targets


def test_ruby_engine_modules_are_grouped_in_one_domain_package() -> None:
    ruby_root = ROOT / "engine" / "ruby"
    assert {
        "__init__.py",
        "layout.py",
        "selection.py",
        "style.py",
        "timing.py",
    } <= {path.name for path in ruby_root.glob("*.py")}
    assert not any(
        (ROOT / "engine" / name).exists()
        for name in (
            "ruby_layout.py",
            "ruby_selection.py",
            "ruby_style.py",
            "ruby_timing.py",
        )
    )


def test_workflow_frontend_modules_are_grouped_in_one_domain_package() -> None:
    module_names = {
        "background_tasks.py",
        "export_controller.py",
        "export_runtime.py",
        "import_controller.py",
    }
    workflow_root = ROOT / "frontend" / "workflow"
    assert {"__init__.py", *module_names} <= {
        path.name for path in workflow_root.glob("*.py")
    }
    assert not any((ROOT / "frontend" / name).exists() for name in module_names)


def test_dialog_frontend_modules_are_grouped_in_one_domain_package() -> None:
    module_names = {
        "auto_chorus_dialog.py",
        "fluent_dialogs.py",
        "guide_replacement.py",
    }
    dialogs_root = ROOT / "frontend" / "dialogs"
    assert {"__init__.py", *module_names} <= {
        path.name for path in dialogs_root.glob("*.py")
    }
    assert not any((ROOT / "frontend" / name).exists() for name in module_names)


def test_editor_frontend_modules_are_grouped_in_one_domain_package() -> None:
    module_names = {"lyrics_list.py", "timeline_view.py"}
    editor_root = ROOT / "frontend" / "editor"
    assert {"__init__.py", *module_names} <= {
        path.name for path in editor_root.glob("*.py")
    }
    assert not any((ROOT / "frontend" / name).exists() for name in module_names)


def test_widget_frontend_modules_are_grouped_in_one_domain_package() -> None:
    module_names = {
        "drop_panel.py",
        "font_loading.py",
        "theme.py",
        "workspace_switcher.py",
    }
    widgets_root = ROOT / "frontend" / "widgets"
    assert {"__init__.py", *module_names} <= {
        path.name for path in widgets_root.glob("*.py")
    }
    assert not any((ROOT / "frontend" / name).exists() for name in module_names)


def test_subtitle_render_window_delegates_background_tasks() -> None:
    window_path = ROOT / "frontend" / "main_window.py"
    worker_path = ROOT / "frontend" / "workflow" / "background_tasks.py"
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
        f"{PACKAGE}.frontend.workflow.background_tasks"
        in _import_targets(f"{PACKAGE}.frontend.main_window", window_path)
    )


def test_subtitle_render_window_delegates_export_thread_wiring() -> None:
    window_path = ROOT / "frontend" / "main_window.py"
    window_module = f"{PACKAGE}.frontend.main_window"
    targets = _import_targets(window_module, window_path)

    assert f"{PACKAGE}.frontend.workflow.export_runtime" in targets
    tree = ast.parse(window_path.read_text(encoding="utf-8-sig"))
    direct_worker_imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == f"{PACKAGE}.frontend.workflow.background_tasks"
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


def test_project_frontend_modules_are_grouped_in_one_domain_package() -> None:
    module_names = {
        "project_autosave.py",
        "project_commands.py",
        "project_recovery.py",
        "project_settings.py",
        "recent_projects.py",
    }
    project_root = ROOT / "frontend" / "project"
    assert {"__init__.py", *module_names} <= {
        path.name for path in project_root.glob("*.py")
    }
    assert not any((ROOT / "frontend" / name).exists() for name in module_names)


def test_subtitle_render_window_delegates_auto_save_thread_lifecycle() -> None:
    window_path = ROOT / "frontend" / "main_window.py"
    window_module = f"{PACKAGE}.frontend.main_window"
    targets = _import_targets(window_module, window_path)

    assert f"{PACKAGE}.frontend.project.project_autosave" in targets
    tree = ast.parse(window_path.read_text(encoding="utf-8-sig"))
    recovery_worker_imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == f"{PACKAGE}.frontend.workflow.background_tasks"
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

    assert f"{PACKAGE}.project.recovery" in targets
    tree = ast.parse(window_path.read_text(encoding="utf-8-sig"))
    forbidden = {"invalidate_recovery_project", "scan_recovery_projects"}
    direct_policy_imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == f"{PACKAGE}.project.store"
        for alias in node.names
    } & forbidden
    assert direct_policy_imports == set()


def test_subtitle_render_window_delegates_recovery_prompt_flow() -> None:
    window_path = ROOT / "frontend" / "main_window.py"
    window_module = f"{PACKAGE}.frontend.main_window"
    targets = _import_targets(window_module, window_path)

    assert f"{PACKAGE}.frontend.project.project_recovery" in targets
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

    assert f"{PACKAGE}.frontend.project.project_commands" in targets
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

    assert f"{PACKAGE}.frontend.project.recent_projects" in targets
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
        f"{PACKAGE}.frontend.project.project_settings"
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

    assert f"{PACKAGE}.project.resources" in targets
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

    assert f"{PACKAGE}.project.controller" in targets
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
        and node.module == f"{PACKAGE}.project.store"
        for alias in node.names
    } & forbidden
    assert direct_transaction_imports == set()


def test_subtitle_render_window_consumes_typed_project_load_plan() -> None:
    window_path = ROOT / "frontend" / "main_window.py"
    window_module = f"{PACKAGE}.frontend.main_window"
    targets = _import_targets(window_module, window_path)

    assert f"{PACKAGE}.project.load" in targets
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

    assert f"{PACKAGE}.sources.loader" in targets
    assert f"{PACKAGE}.sources.subtitles" not in targets
    assert f"{PACKAGE}.sources.sug" not in targets
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

    assert f"{PACKAGE}.frontend.workflow.import_controller" in targets
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

    assert f"{PACKAGE}.frontend.preview.preview_controller" in targets
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


def test_preview_frontend_modules_are_grouped_in_one_domain_package() -> None:
    module_names = {
        "playback.py",
        "preview_async.py",
        "preview_controller.py",
        "preview_graphics.py",
        "preview_media.py",
        "preview_view.py",
    }
    preview_root = ROOT / "frontend" / "preview"
    assert {"__init__.py", *module_names} <= {
        path.name for path in preview_root.glob("*.py")
    }
    assert not any((ROOT / "frontend" / name).exists() for name in module_names)


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

    assert f"{PACKAGE}.frontend.workflow.export_controller" in targets
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


def test_property_frontend_modules_are_grouped_in_one_domain_package() -> None:
    module_names = {
        "property_background_page.py",
        "property_effects_page.py",
        "property_inputs.py",
        "property_layout.py",
        "property_layout_page.py",
        "property_pages.py",
        "property_panel.py",
        "property_role_color_page.py",
        "property_role_fill_pages.py",
        "property_role_font_page.py",
        "property_role_page.py",
        "property_timing_page.py",
        "property_title_page.py",
        "property_widgets.py",
    }
    properties_root = ROOT / "frontend" / "properties"
    assert {"__init__.py", *module_names} <= {
        path.name for path in properties_root.glob("*.py")
    }
    assert not any((ROOT / "frontend" / name).exists() for name in module_names)


def test_subtitle_property_panel_delegates_page_registry_and_routing() -> None:
    panel_path = ROOT / "frontend" / "properties" / "property_panel.py"
    panel_module = f"{PACKAGE}.frontend.properties.property_panel"
    targets = _import_targets(panel_module, panel_path)

    assert f"{PACKAGE}.frontend.properties.property_pages" in targets
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
    panel_path = ROOT / "frontend" / "properties" / "property_panel.py"
    panel_module = f"{PACKAGE}.frontend.properties.property_panel"
    targets = _import_targets(panel_module, panel_path)

    assert f"{PACKAGE}.frontend.properties.property_widgets" in targets
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
    panel_path = ROOT / "frontend" / "properties" / "property_panel.py"
    panel_module = f"{PACKAGE}.frontend.properties.property_panel"
    targets = _import_targets(panel_module, panel_path)

    assert f"{PACKAGE}.frontend.properties.property_layout" in targets
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
    panel_path = ROOT / "frontend" / "properties" / "property_panel.py"
    panel_module = f"{PACKAGE}.frontend.properties.property_panel"
    targets = _import_targets(panel_module, panel_path)

    assert f"{PACKAGE}.frontend.properties.property_inputs" in targets
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
    panel_path = ROOT / "frontend" / "properties" / "property_panel.py"
    panel_module = f"{PACKAGE}.frontend.properties.property_panel"
    targets = _import_targets(panel_module, panel_path)

    assert f"{PACKAGE}.frontend.properties.property_title_page" in targets
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
    panel_path = ROOT / "frontend" / "properties" / "property_panel.py"
    panel_module = f"{PACKAGE}.frontend.properties.property_panel"
    targets = _import_targets(panel_module, panel_path)

    assert f"{PACKAGE}.frontend.properties.property_timing_page" in targets
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
    panel_path = ROOT / "frontend" / "properties" / "property_panel.py"
    panel_module = f"{PACKAGE}.frontend.properties.property_panel"
    targets = _import_targets(panel_module, panel_path)

    assert f"{PACKAGE}.frontend.properties.property_background_page" in targets
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
    panel_path = ROOT / "frontend" / "properties" / "property_panel.py"
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
    panel_path = ROOT / "frontend" / "properties" / "property_panel.py"
    panel_module = f"{PACKAGE}.frontend.properties.property_panel"
    targets = _import_targets(panel_module, panel_path)

    assert f"{PACKAGE}.frontend.properties.property_effects_page" in targets
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
    panel_path = ROOT / "frontend" / "properties" / "property_panel.py"
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
    panel_path = ROOT / "frontend" / "properties" / "property_panel.py"
    panel_module = f"{PACKAGE}.frontend.properties.property_panel"
    targets = _import_targets(panel_module, panel_path)

    assert f"{PACKAGE}.frontend.properties.property_layout_page" in targets
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
    panel_path = ROOT / "frontend" / "properties" / "property_panel.py"
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
    panel_path = ROOT / "frontend" / "properties" / "property_panel.py"
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
    panel_path = ROOT / "frontend" / "properties" / "property_panel.py"
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
    panel_path = ROOT / "frontend" / "properties" / "property_panel.py"
    panel_module = f"{PACKAGE}.frontend.properties.property_panel"
    targets = _import_targets(panel_module, panel_path)

    assert f"{PACKAGE}.frontend.properties.property_role_page" in targets
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
    panel_path = ROOT / "frontend" / "properties" / "property_panel.py"
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
    panel_path = ROOT / "frontend" / "properties" / "property_panel.py"
    panel_module = f"{PACKAGE}.frontend.properties.property_panel"
    targets = _import_targets(panel_module, panel_path)

    assert f"{PACKAGE}.frontend.properties.property_role_font_page" in targets
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
    panel_path = ROOT / "frontend" / "properties" / "property_panel.py"
    panel_module = f"{PACKAGE}.frontend.properties.property_panel"
    targets = _import_targets(panel_module, panel_path)

    assert f"{PACKAGE}.frontend.properties.property_role_color_page" in targets
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
    panel_path = ROOT / "frontend" / "properties" / "property_panel.py"
    panel_module = f"{PACKAGE}.frontend.properties.property_panel"
    targets = _import_targets(panel_module, panel_path)

    assert f"{PACKAGE}.frontend.properties.property_role_fill_pages" in targets
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
    panel_path = ROOT / "frontend" / "properties" / "property_panel.py"
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
    panel_path = ROOT / "frontend" / "properties" / "property_panel.py"
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
    panel_path = ROOT / "frontend" / "properties" / "property_panel.py"
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
