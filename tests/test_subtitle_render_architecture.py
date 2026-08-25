"""Dependency guardrails for the subtitle-render package."""

from __future__ import annotations

import ast
from collections import defaultdict
import importlib
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
        ROOT / "domain" / "background.py",
        ROOT / "contracts.py",
        ROOT / "engine" / "export" / "export_command.py",
        ROOT / "engine" / "export" / "parallel_schedule.py",
        ROOT / "engine" / "render" / "core" / "raster_blur.py",
        ROOT / "engine" / "export" / "render_job.py",
        ROOT / "engine" / "export" / "render_job_policy.py",
        ROOT / "engine" / "render" / "render_bands.py",
        ROOT / "engine" / "ruby" / "timing.py",
        ROOT / "domain" / "paint.py",
        ROOT / "serialization" / "paint.py",
        ROOT / "project" / "controller.py",
        ROOT / "project" / "load.py",
        ROOT / "project" / "recovery.py",
        ROOT / "project" / "resources.py",
        ROOT / "project" / "recent.py",
        ROOT / "settings" / "screen.py",
        ROOT / "settings" / "preferences.py",
        ROOT / "settings" / "property_controllers.py",
        ROOT / "project" / "session.py",
        ROOT / "settings" / "store.py",
        ROOT / "sources" / "loader.py",
        ROOT / "sources" / "reload.py",
        ROOT / "engine" / "timing" / "timecode.py",
        ROOT / "domain" / "timing.py",
        ROOT / "serialization" / "timing.py",
    )
    violations: dict[str, list[str]] = defaultdict(list)
    for path in paths:
        module = _module_name(path)
        for target in _import_targets(module, path):
            if target == forbidden or target.startswith(f"{forbidden}."):
                violations[module].append(target)

    assert not violations


def test_domain_value_modules_are_grouped_in_one_package() -> None:
    domain_root = ROOT / "domain"
    module_names = {"background.py", "models.py", "paint.py", "timing.py"}

    assert {"__init__.py", *module_names} <= {
        path.name for path in domain_root.glob("*.py")
    }
    assert not any((ROOT / name).exists() for name in module_names)


def test_moved_domain_modules_keep_their_public_import_paths() -> None:
    for module_name in ("background", "models", "paint", "timing"):
        compatibility_module = importlib.import_module(f"{PACKAGE}.{module_name}")
        domain_module = importlib.import_module(f"{PACKAGE}.domain.{module_name}")

        assert compatibility_module is domain_module


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
            if (
                not isinstance(node, ast.ImportFrom)
                or node.module != f"{PACKAGE}.domain.models"
            ):
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
            if (
                not isinstance(node, ast.ImportFrom)
                or node.module != f"{PACKAGE}.domain.models"
            ):
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
            if (
                not isinstance(node, ast.ImportFrom)
                or node.module != f"{PACKAGE}.domain.models"
            ):
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
        if (
            path == ROOT / "domain" / "models.py"
            or path == ROOT / "serialization" / "timing.py"
        ):
            continue
        module = _module_name(path)
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if (
                not isinstance(node, ast.ImportFrom)
                or node.module != f"{PACKAGE}.domain.models"
            ):
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
        "krok_helper.subtitle_render.domain.models",
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


def test_property_panel_delegates_normalized_style_updates() -> None:
    controller_path = ROOT / "settings" / "property_controllers.py"
    panel_path = ROOT / "frontend" / "properties" / "property_panel.py"
    controller_tree = ast.parse(controller_path.read_text(encoding="utf-8-sig"))
    panel_tree = ast.parse(panel_path.read_text(encoding="utf-8-sig"))
    controller_members = {
        node.name
        for node in controller_tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert {
        "PropertyStyleController",
        "StyleUpdateResult",
        "normalize_style_changes",
        "scheme_has_legacy_color_values",
    } <= controller_members

    panel_class = next(
        node
        for node in panel_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "PropertyPanel"
    )
    update_method = next(
        node
        for node in panel_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "_update_style"
    )
    delegated_calls = {
        node.func.attr
        for node in ast.walk(update_method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "_style_controller"
    }
    assert delegated_calls == {"update"}

    value_delegates = {
        method_name: {
            node.func.attr
            for node in ast.walk(
                next(
                    node
                    for node in panel_class.body
                    if isinstance(node, ast.FunctionDef)
                    and node.name == method_name
                )
            )
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "_style_controller"
        }
        for method_name in {"_scheme_own_value", "_scheme_value"}
    }
    assert value_delegates == {
        "_scheme_own_value": {"own_value"},
        "_scheme_value": {"value"},
    }
    snapshot_delegates = {
        method_name: {
            node.func.attr
            for node in ast.walk(
                next(
                    node
                    for node in panel_class.body
                    if isinstance(node, ast.FunctionDef)
                    and node.name == method_name
                )
            )
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "_style_controller"
        }
        for method_name in {"_current_karaoke_colors", "_current_scheme_snapshot"}
    }
    assert snapshot_delegates == {
        "_current_karaoke_colors": {"current_karaoke_colors"},
        "_current_scheme_snapshot": {"snapshot"},
    }

    panel_functions = {
        node.name
        for node in panel_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert {
        "_normalize_decoration_kind",
        "_normalize_entry_animation",
        "_normalize_exit_animation",
        "_normalize_horizontal_align",
        "_normalize_horizontal_layout",
        "_normalize_karaoke_animation",
        "_normalize_line_position",
        "_normalize_lit_style",
        "_normalize_lit_transition_mode",
        "_normalize_viewport_align",
        "_legacy_after_text_fill",
        "_legacy_colors_from_panel",
        "_scheme_has_legacy_color_values",
        "_scheme_from_current",
        "_style_scheme_changes",
    }.isdisjoint(panel_functions)

    targets = _import_targets(
        f"{PACKAGE}.settings.property_controllers",
        controller_path,
    )
    assert f"{PACKAGE}.frontend" not in targets
    assert not any(target.startswith(f"{PACKAGE}.frontend.") for target in targets)


def test_property_panel_delegates_role_scheme_lifecycle() -> None:
    controller_path = ROOT / "settings" / "property_controllers.py"
    panel_path = ROOT / "frontend" / "properties" / "property_panel.py"
    controller_tree = ast.parse(controller_path.read_text(encoding="utf-8-sig"))
    panel_tree = ast.parse(panel_path.read_text(encoding="utf-8-sig"))
    role_controller = next(
        node
        for node in controller_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "RoleSchemeController"
    )
    methods = {
        node.name
        for node in role_controller.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert {
        "add_scheme_changes",
        "apply_scheme_changes",
        "delete_changes",
        "import_preset_changes",
        "rename_changes",
    } <= methods

    panel_class = next(
        node
        for node in panel_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "PropertyPanel"
    )
    expected = {
        "_add_custom_scheme": {"add_scheme_changes"},
        "_apply_preset_to_current_target": {"apply_scheme_changes"},
        "_delete_current_role": {"delete_changes"},
        "_import_preset_schemes": {"import_preset_changes"},
        "_rename_current_role": {"rename_changes"},
    }
    actual = {}
    for method_name in expected:
        method = next(
            node
            for node in panel_class.body
            if isinstance(node, ast.FunctionDef) and node.name == method_name
        )
        actual[method_name] = {
            node.func.attr
            for node in ast.walk(method)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "_role_controller"
            and node.func.attr
            in {
                "add_scheme_changes",
                "apply_scheme_changes",
                "delete_changes",
                "import_preset_changes",
                "rename_changes",
            }
        }
    assert actual == expected


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
    owner = f"{PACKAGE}.engine.layout.line.style"
    delegated_names = {
        "_lane_count",
        "_layout_style_for_line",
        "_row_count_resolver",
    }
    for relative_path in (
        Path("engine/layout/page/assignment.py"),
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
        ROOT / "engine/layout/line/style.py",
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
    module = f"{PACKAGE}.engine.layout.plan.builder"
    targets = _import_targets(
        module,
        ROOT / "engine/layout/plan/builder.py",
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
    owner = f"{PACKAGE}.engine.layout.page.pagination"
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
    targets = _import_targets(owner, ROOT / "engine/layout/page/pagination.py")
    assert f"{PACKAGE}.engine.painter" not in targets


def test_line_geometry_policy_has_no_painter_dependency() -> None:
    owner = f"{PACKAGE}.engine.layout.line.geometry"
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
    targets = _import_targets(owner, ROOT / "engine/layout/line/geometry.py")
    assert f"{PACKAGE}.engine.painter" not in targets


def test_signal_semantics_have_one_engine_owner() -> None:
    owner = f"{PACKAGE}.engine.layout.display.signal"
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
    targets = _import_targets(owner, ROOT / "engine/layout/display/signal.py")
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
    owner = f"{PACKAGE}.engine.layout.display.schedule"
    schedule_path = ROOT / "engine/layout/display/schedule.py"
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
    owner = f"{PACKAGE}.engine.layout.display.resolver"
    path = ROOT / "engine/layout/display/resolver.py"
    targets = _import_targets(owner, path)

    assert f"{PACKAGE}.engine.painter" not in targets


def test_painter_delegates_display_resolution_orchestration() -> None:
    painter_path = ROOT / "engine/painter.py"
    painter_module = f"{PACKAGE}.engine.painter"
    targets = _import_targets(painter_module, painter_path)

    assert f"{PACKAGE}.engine.layout.display.resolver" in targets
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

    resolver_path = ROOT / "engine/layout/display/resolver.py"
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

    assert f"{PACKAGE}.engine.layout.display.diagnostics" not in targets
    diagnostics_path = ROOT / "engine/layout/display/diagnostics.py"
    diagnostics_targets = _import_targets(
        f"{PACKAGE}.engine.layout.display.diagnostics",
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


def test_layout_diagnostics_adapter_binds_layout_margin_policy() -> None:
    adapter_path = ROOT / "engine/render/adapters/layout_diagnostics.py"
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


def test_layout_diagnostics_adapter_binds_timing_diagnostic_policy() -> None:
    adapter_path = ROOT / "engine/render/adapters/layout_diagnostics.py"
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
    owner = f"{PACKAGE}.engine.layout.plan.orchestrator"
    targets = _import_targets(
        owner,
        ROOT / "engine/layout/plan/orchestrator.py",
    )

    assert f"{PACKAGE}.engine.painter" not in targets

    source = (ROOT / "engine/layout/plan/orchestrator.py").read_text(
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
    owner = f"{PACKAGE}.engine.layout.line.qt_geometry"
    targets = _import_targets(owner, ROOT / "engine/layout/line/qt_geometry.py")

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
    owner = f"{PACKAGE}.engine.layout.plan.page_offsets"
    plan_path = ROOT / "engine/layout/plan/page_offsets.py"
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
    owner = f"{PACKAGE}.engine.layout.plan.projection"
    targets = _import_targets(
        owner,
        ROOT / "engine/layout/plan/projection.py",
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
        "layout_context.py",
    }
    layout_root = ROOT / "engine" / "layout"
    assert {"__init__.py", *module_names} <= {
        path.name for path in layout_root.glob("*.py")
    }
    assert not any((ROOT / "engine" / name).exists() for name in module_names)
    line_root = layout_root / "line"
    assert {"__init__.py", "geometry.py", "qt_geometry.py", "style.py"} <= {
        path.name for path in line_root.glob("*.py")
    }
    assert not any(
        (layout_root / name).exists()
        for name in {"line_geometry.py", "line_style.py", "qt_line_geometry.py"}
    )
    plan_root = layout_root / "plan"
    assert {
        "__init__.py",
        "builder.py",
        "cache.py",
        "model.py",
        "orchestrator.py",
        "page_offsets.py",
        "projection.py",
        "semantic.py",
    } <= {path.name for path in plan_root.glob("*.py")}
    assert not any(
        (layout_root / name).exists()
        for name in {
            "layout_plan.py",
            "layout_plan_builder.py",
            "layout_plan_cache.py",
            "layout_plan_orchestrator.py",
            "layout_plan_projection.py",
            "page_offset_plan.py",
            "semantic_plan.py",
        }
    )
    display_root = layout_root / "display"
    assert {
        "__init__.py",
        "diagnostics.py",
        "resolver.py",
        "schedule.py",
        "signal.py",
    } <= {path.name for path in display_root.glob("*.py")}
    assert not any(
        (layout_root / name).exists()
        for name in {
            "display_resolver.py",
            "display_schedule.py",
            "layout_diagnostics.py",
            "signal_semantics.py",
        }
    )
    page_root = layout_root / "page"
    assert {
        "__init__.py",
        "assignment.py",
        "pagination.py",
        "placement.py",
        "plan.py",
    } <= {path.name for path in page_root.glob("*.py")}
    assert not any(
        (layout_root / name).exists()
        for name in {
            "layout_assignment.py",
            "line_pagination.py",
            "page_placement.py",
            "page_plan.py",
        }
    )


def test_layout_package_has_no_painter_dependency() -> None:
    painter_module = f"{PACKAGE}.engine.painter"
    layout_root = ROOT / "engine" / "layout"

    offenders = set()
    for path in layout_root.rglob("*.py"):
        relative_module = path.relative_to(ROOT).with_suffix("")
        module = f"{PACKAGE}." + ".".join(relative_module.parts)
        if painter_module in _import_targets(module, path):
            offenders.add(path.relative_to(layout_root).as_posix())

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
        "frame_analysis.py",
        "image_resource.py",
        "render_bands.py",
        "render_ir.py",
    }
    render_root = ROOT / "engine" / "render"
    assert {"__init__.py", *module_names} <= {
        path.name for path in render_root.glob("*.py")
    }
    assert not any((ROOT / "engine" / name).exists() for name in module_names)
    core_names = {
        "animator.py",
        "cache_keys.py",
        "layers.py",
        "quantize.py",
        "raster_blur.py",
    }
    core_root = render_root / "core"
    assert {"__init__.py", *core_names} <= {
        path.name for path in core_root.glob("*.py")
    }
    assert not any((render_root / name).exists() for name in core_names)
    element_names = {"signal.py", "title.py", "vertical.py"}
    elements_root = render_root / "elements"
    assert {"__init__.py", *element_names} <= {
        path.name for path in elements_root.glob("*.py")
    }
    assert not any((render_root / name).exists() for name in element_names)
    adapter_names = {
        "layout_diagnostics.py",
        "layout_plan.py",
        "timeline_projection.py",
    }
    adapter_root = render_root / "adapters"
    assert {"__init__.py", *adapter_names} <= {
        path.name for path in adapter_root.glob("*.py")
    }
    assert not any(
        (render_root / name).exists()
        for name in {
            "layout_diagnostics_backend.py",
            "layout_plan_backend.py",
            "timeline_projection_backend.py",
        }
    )


def test_painter_delegates_layout_cache_keys() -> None:
    painter_path = ROOT / "engine" / "painter.py"
    cache_path = ROOT / "engine" / "render" / "core" / "cache_keys.py"
    painter_tree = ast.parse(painter_path.read_text(encoding="utf-8-sig"))
    delegated_names = {
        "_layout_cache_sig",
        "_line_layout_signature",
        "_track_layout_signature",
    }
    painter_functions = {
        node.name for node in painter_tree.body if isinstance(node, ast.FunctionDef)
    }

    assert delegated_names.isdisjoint(painter_functions)
    assert f"{PACKAGE}.engine.render.core.cache_keys" in _import_targets(
        f"{PACKAGE}.engine.painter",
        painter_path,
    )
    assert f"{PACKAGE}.engine.painter" not in _import_targets(
        f"{PACKAGE}.engine.render.core.cache_keys",
        cache_path,
    )


def test_painter_delegates_frame_analysis() -> None:
    painter_path = ROOT / "engine" / "painter.py"
    analysis_path = ROOT / "engine" / "render" / "frame_analysis.py"
    painter_tree = ast.parse(painter_path.read_text(encoding="utf-8-sig"))
    window_functions = {
        node.name: node
        for node in painter_tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name
        in {"frame_content_intervals", "frame_has_content", "frame_vertical_bounds"}
    }
    expected_calls = {
        "frame_content_intervals": "_analyze_frame_content_intervals",
        "frame_has_content": "_analyze_frame_has_content",
        "frame_vertical_bounds": "_analyze_frame_vertical_bounds",
    }
    for function_name, delegated_call in expected_calls.items():
        calls = {
            node.func.id
            for node in ast.walk(window_functions[function_name])
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert delegated_call in calls

    assert f"{PACKAGE}.engine.render.frame_analysis" in _import_targets(
        f"{PACKAGE}.engine.painter",
        painter_path,
    )
    assert f"{PACKAGE}.engine.painter" not in _import_targets(
        f"{PACKAGE}.engine.render.frame_analysis",
        analysis_path,
    )


def test_title_layout_has_one_render_owner() -> None:
    owner = f"{PACKAGE}.engine.render.elements.title"
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
        ROOT / "engine/render/elements/title.py",
    )


def test_signal_geometry_has_one_render_owner() -> None:
    owner = f"{PACKAGE}.engine.render.elements.signal"
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
        ROOT / "engine/render/elements/signal.py",
    )


def test_vertical_layout_has_one_render_owner() -> None:
    owner = f"{PACKAGE}.engine.render.elements.vertical"
    painter_path = ROOT / "engine/painter.py"
    painter_tree = ast.parse(painter_path.read_text(encoding="utf-8-sig"))
    delegated_names = {
        "_VerticalLineLayout",
        "_layout_vertical_line",
        "_paint_line_vertical_layers_with_ports",
        "_paint_rubies_vertical_with_ports",
        "_resolve_vertical_columns",
        "_resolve_vertical_top",
        "_vertical_after_clip_rect",
        "_vertical_after_clip_pad_with_ports",
        "_vertical_before_clip_rect",
        "_vertical_before_clip_pad_with_ports",
        "_vertical_cell_width",
        "_vertical_fill_band_with_ports",
        "_vertical_glyph_offset",
        "_vertical_glyph_path",
        "_vertical_orientation",
        "_vertical_main_path_sig",
        "_vertical_layer_stack_with_ports",
        "_vertical_ruby_allowance",
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
    assert "_build_baked_path_stack" not in {
        node.name
        for node in painter_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_vertical_layer_stack" not in {
        node.name
        for node in painter_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    adapter_targets = {
        "_paint_rubies_vertical": "_paint_rubies_vertical_with_ports",
    }
    for adapter_name, target_name in adapter_targets.items():
        adapter = next(
            node
            for node in painter_tree.body
            if isinstance(node, ast.FunctionDef) and node.name == adapter_name
        )
        calls = {
            node.func.id
            for node in ast.walk(adapter)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert target_name in calls
    painter_members = {
        node.name
        for node in painter_tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert {
        "_vertical_ruby_layers",
        "_vertical_ruby_path_and_wipe",
    }.isdisjoint(painter_members)
    vertical_tree = ast.parse(
        (ROOT / "engine/render/elements/vertical.py").read_text(
            encoding="utf-8-sig"
        )
    )
    vertical_members = {
        node.name
        for node in vertical_tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert {
        "VerticalRubyPorts",
        "VerticalRubyWipeSegment",
        "paint_rubies_vertical",
        "vertical_ruby_layers",
        "vertical_ruby_path_and_wipe",
        "vertical_ruby_segment_wipe_state",
    } <= vertical_members
    assert f"{PACKAGE}.engine.painter" not in _import_targets(
        owner,
        ROOT / "engine/render/elements/vertical.py",
    )


def test_ruby_main_text_timing_has_one_painter_free_owner() -> None:
    timing_owner = f"{PACKAGE}.engine.ruby.timing"
    timing_path = ROOT / "engine/ruby/timing.py"
    painter_path = ROOT / "engine/painter.py"
    painter_tree = ast.parse(painter_path.read_text(encoding="utf-8-sig"))
    aliases = {
        "character_fill_ratio": "_character_fill_ratio",
        "is_utopia_group_marker": "_is_utopia_group_marker",
        "resolve_char_ruby_groups": "_resolve_char_ruby_groups",
        "ruby_for_char_index": "_ruby_for_char_index",
        "ruby_main_uses_base_timing": "_ruby_main_uses_base_timing",
        "utopia_main_group_for_index": "_utopia_main_group_for_index",
        "utopia_wipe_window_for_index": "_utopia_wipe_window_for_index",
    }
    imported = {
        alias.name: alias.asname
        for node in painter_tree.body
        if isinstance(node, ast.ImportFrom) and node.module == timing_owner
        for alias in node.names
        if alias.name in aliases
    }
    painter_members = {
        node.name
        for node in painter_tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert imported == aliases
    assert set(aliases.values()).isdisjoint(painter_members)
    targets = _import_targets(timing_owner, timing_path)
    assert f"{PACKAGE}.engine.painter" not in targets
    assert not any(
        target == f"{PACKAGE}.frontend"
        or target.startswith(f"{PACKAGE}.frontend.")
        for target in targets
    )

    timeline_path = ROOT / "engine/render/adapters/timeline_projection.py"
    timeline_tree = ast.parse(timeline_path.read_text(encoding="utf-8-sig"))
    assert any(
        isinstance(node, ast.ImportFrom)
        and node.module == f"{PACKAGE}.engine.ruby"
        and any(alias.name == "resolve_char_ruby_groups" for alias in node.names)
        for node in timeline_tree.body
    )
    assert not any(
        isinstance(node, ast.ImportFrom)
        and node.module == f"{PACKAGE}.engine.painter"
        for node in ast.walk(timeline_tree)
    )


def test_horizontal_contracts_have_one_painter_free_owner() -> None:
    owner = f"{PACKAGE}.engine.render.elements.horizontal"
    contract_owner = f"{owner}.contracts"
    contract_path = ROOT / "engine/render/elements/horizontal/contracts.py"
    painter_path = ROOT / "engine/painter.py"
    painter_tree = ast.parse(painter_path.read_text(encoding="utf-8-sig"))
    aliases = {
        "FillSegment": "_FillSegment",
        "LineCharTransition": "_LineCharTransition",
        "LineLayout": "_LineLayout",
        "RubyLayout": "_RubyLayout",
        "RubyWipeSegment": "_RubyWipeSegment",
        "SayatooLineLayout": "_SayatooLineLayout",
    }
    imported = {
        alias.name: alias.asname
        for node in painter_tree.body
        if isinstance(node, ast.ImportFrom) and node.module == owner
        for alias in node.names
        if alias.name in aliases
    }
    painter_classes = {
        node.name for node in painter_tree.body if isinstance(node, ast.ClassDef)
    }

    assert {"__init__.py", "contracts.py", "wipe.py"} <= {
        path.name
        for path in (ROOT / "engine/render/elements/horizontal").glob("*.py")
    }
    assert imported == aliases
    assert set(aliases.values()).isdisjoint(painter_classes)
    targets = _import_targets(contract_owner, contract_path)
    assert f"{PACKAGE}.engine.painter" not in targets
    assert not any(
        target == f"{PACKAGE}.frontend"
        or target.startswith(f"{PACKAGE}.frontend.")
        for target in targets
    )


def test_horizontal_positioning_has_one_painter_free_owner() -> None:
    owner = f"{PACKAGE}.engine.render.elements.horizontal"
    positioning_owner = f"{owner}.positioning"
    positioning_path = (
        ROOT / "engine/render/elements/horizontal/positioning.py"
    )
    painter_path = ROOT / "engine/painter.py"
    painter_tree = ast.parse(painter_path.read_text(encoding="utf-8-sig"))
    names = {
        "aligned_x0",
        "bottom_short_page_alignment",
        "lane_alignment",
        "layout_page_lines",
        "line_total_width",
        "line_lane_alignment",
        "n3_smart_font_size",
        "resolve_line_x",
        "resolve_line_x_smart",
        "row_layout_params",
        "smart_horizontal_dx",
    }
    imported = {
        alias.name: alias.asname
        for node in painter_tree.body
        if isinstance(node, ast.ImportFrom) and node.module == owner
        for alias in node.names
        if alias.name in names
    }
    painter_members = {
        node.name
        for node in painter_tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert imported == {name: f"_{name}" for name in names}
    assert {f"_{name}" for name in names}.isdisjoint(painter_members)
    targets = _import_targets(positioning_owner, positioning_path)
    assert f"{PACKAGE}.engine.painter" not in targets
    assert not any(
        target == f"{PACKAGE}.frontend"
        or target.startswith(f"{PACKAGE}.frontend.")
        for target in targets
    )


def test_horizontal_glyph_layout_has_one_painter_free_owner() -> None:
    owner = f"{PACKAGE}.engine.render.elements.horizontal"
    layout_owner = f"{owner}.layout"
    layout_path = ROOT / "engine/render/elements/horizontal/layout.py"
    painter_path = ROOT / "engine/painter.py"
    painter_tree = ast.parse(painter_path.read_text(encoding="utf-8-sig"))
    names = {
        "bitmap_guide_anchor_descent",
        "bitmap_guide_glyphs",
        "bitmap_guide_is_no_wipe",
        "clamp_role_baseline_y",
        "glyph_is_bitmap_guide",
        "glyph_path",
        "glyph_run_path",
        "glyph_run_rect",
        "glyph_run_signature",
        "glyph_runs",
        "glyph_runs_for_indices",
        "fixed_line_geometry",
        "n3_main_fill_rect",
        "resolve_role_baseline_y",
        "resolve_baseline_y",
        "resolve_display_baselines",
        "role_char_ink_ranges_by_index",
        "role_glyphs_by_index",
        "role_visual_text_padding",
        "text_glyph_runs",
    }
    imported = {
        alias.name: alias.asname
        for node in painter_tree.body
        if isinstance(node, ast.ImportFrom) and node.module == owner
        for alias in node.names
        if alias.name in names
    }
    painter_members = {
        node.name
        for node in painter_tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert imported == {name: f"_{name}" for name in names}
    assert {f"_{name}" for name in names}.isdisjoint(painter_members)
    targets = _import_targets(layout_owner, layout_path)
    assert f"{PACKAGE}.engine.painter" not in targets
    assert not any(
        target == f"{PACKAGE}.frontend"
        or target.startswith(f"{PACKAGE}.frontend.")
        for target in targets
    )


def test_horizontal_line_layout_uses_explicit_painter_free_ports() -> None:
    owner = f"{PACKAGE}.engine.render.elements.horizontal"
    layout_owner = f"{owner}.layout"
    layout_path = ROOT / "engine/render/elements/horizontal/layout.py"
    painter_path = ROOT / "engine/painter.py"
    layout_tree = ast.parse(layout_path.read_text(encoding="utf-8-sig"))
    painter_tree = ast.parse(painter_path.read_text(encoding="utf-8-sig"))
    builders = {
        "layout_line_uncached": "_build_horizontal_line_uncached",
        "layout_plain_line": "_build_horizontal_plain_line",
        "layout_role_line": "_build_horizontal_role_line",
    }
    layout_members = {
        node.name
        for node in layout_tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    imported = {
        alias.name: alias.asname
        for node in painter_tree.body
        if isinstance(node, ast.ImportFrom) and node.module == owner
        for alias in node.names
        if alias.name in builders
    }
    painter_functions = {
        node.name: node
        for node in painter_tree.body
        if isinstance(node, ast.FunctionDef)
    }

    assert {"HorizontalLayoutPorts", *builders} <= layout_members
    assert imported == builders
    for public_name, delegate_name in builders.items():
        wrapper = painter_functions[f"_{public_name}"]
        assert len(wrapper.body) == 1
        returned = wrapper.body[0]
        assert isinstance(returned, ast.Return)
        assert isinstance(returned.value, ast.Call)
        assert isinstance(returned.value.func, ast.Name)
        assert returned.value.func.id == delegate_name
        assert any(
            isinstance(argument, ast.Name)
            and argument.id == "_HORIZONTAL_LAYOUT_PORTS"
            for argument in returned.value.args
        )

    ports = [
        node.value
        for node in painter_tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "_HORIZONTAL_LAYOUT_PORTS"
            for target in node.targets
        )
    ]
    assert len(ports) == 1
    assert isinstance(ports[0], ast.Call)
    assert isinstance(ports[0].func, ast.Name)
    assert ports[0].func.id == "HorizontalLayoutPorts"
    assert {keyword.arg for keyword in ports[0].keywords} == {
        "char_layout_width",
        "karaoke_fill_segments",
        "layout_rubies",
        "role_ruby_vertical_extra",
    }

    targets = _import_targets(layout_owner, layout_path)
    assert f"{PACKAGE}.engine.painter" not in targets
    assert not any(
        target == f"{PACKAGE}.frontend"
        or target.startswith(f"{PACKAGE}.frontend.")
        for target in targets
    )


def test_horizontal_transition_math_has_one_painter_free_owner() -> None:
    owner = f"{PACKAGE}.engine.render.elements.horizontal"
    transition_owner = f"{owner}.transitions"
    transition_path = (
        ROOT / "engine/render/elements/horizontal/transitions.py"
    )
    painter_path = ROOT / "engine/painter.py"
    painter_tree = ast.parse(painter_path.read_text(encoding="utf-8-sig"))
    aliases = {
        "CHAR_FADE_IN_TIME_MS": "_CHAR_FADE_IN_TIME_MS",
        "CHAR_FADE_INTRO_DELAY_MS": "_CHAR_FADE_INTRO_DELAY_MS",
        "CHAR_FADE_OUT_TIME_MS": "_CHAR_FADE_OUT_TIME_MS",
        "char_fade_opacity": "_char_fade_opacity",
        "char_drip_char_transform": "_char_drip_char_transform",
        "character_transform": "_character_transform",
        "line_char_transition_context": "_line_char_transition_context",
        "spin_flip_char_transform": "_spin_flip_char_transform",
        "spin_flip_skew": "_spin_flip_skew",
        "transition_char_state": "_transition_char_state",
        "utopia_following_done_time": "_utopia_following_done_time",
    }
    imported = {
        alias.name: alias.asname
        for node in painter_tree.body
        if isinstance(node, ast.ImportFrom) and node.module == owner
        for alias in node.names
        if alias.name in aliases
    }
    painter_functions = {
        node.name
        for node in painter_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert imported == aliases
    assert {
        "_char_fade_delay_step",
        "_char_fade_opacity",
        "_char_drip_char_transform",
        "_character_transform",
        "_clamped_ratio",
        "_is_utopia_wiping",
        "_next_valid_char_index",
        "_spin_flip_skew",
        "_spin_flip_char_transform",
        "_staggered_char_progress",
        "_transition_char_state",
        "_utopia_following_done_time",
        "_utopia_intro_delay_step",
        "_utopia_tail_delay_ms",
        "_utopia_wipe_scale",
    }.isdisjoint(painter_functions)
    targets = _import_targets(transition_owner, transition_path)
    assert f"{PACKAGE}.engine.painter" not in targets
    assert not any(
        target == f"{PACKAGE}.frontend"
        or target.startswith(f"{PACKAGE}.frontend.")
        for target in targets
    )


def test_horizontal_wipe_geometry_has_one_painter_free_owner() -> None:
    owner = f"{PACKAGE}.engine.render.elements.horizontal"
    wipe_owner = f"{owner}.wipe"
    wipe_path = ROOT / "engine/render/elements/horizontal/wipe.py"
    painter_path = ROOT / "engine/painter.py"
    painter_tree = ast.parse(painter_path.read_text(encoding="utf-8-sig"))
    names = {
        "adjust_fill_release_edges",
        "fill_clip_band",
        "fill_clip_band_for_glyphs",
        "fill_clip_band_for_indices",
        "fill_extent_end",
        "fill_extent_left",
        "fill_extent_start",
        "n3_char_wipe_ranges_by_index",
        "n3_following_wipe_band",
        "n3_transformed_wipe_span",
        "offset_fill_segments",
        "run_fill_complete",
        "segment_fill_ratio",
        "segment_wipe_band_at",
        "segment_wipe_edges",
        "segment_wipe_times",
    }
    imported = {
        alias.name: alias.asname
        for node in painter_tree.body
        if isinstance(node, ast.ImportFrom) and node.module == owner
        for alias in node.names
        if alias.name in names
    }
    painter_members = {
        node.name
        for node in painter_tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert imported == {name: f"_{name}" for name in names}
    assert {f"_{name}" for name in names}.isdisjoint(painter_members)
    targets = _import_targets(wipe_owner, wipe_path)
    assert f"{PACKAGE}.engine.painter" not in targets
    assert not any(
        target == f"{PACKAGE}.frontend"
        or target.startswith(f"{PACKAGE}.frontend.")
        for target in targets
    )


def test_horizontal_layer_policy_has_one_painter_free_owner() -> None:
    owner = f"{PACKAGE}.engine.render.elements.horizontal"
    layer_owner = f"{owner}.layers"
    layer_path = ROOT / "engine/render/elements/horizontal/layers.py"
    painter_path = ROOT / "engine/painter.py"
    painter_tree = ast.parse(painter_path.read_text(encoding="utf-8-sig"))
    names = {
        "after_glow_loose_clip_rect",
        "after_glow_source_clip_rect",
        "before_glow_source_clip_rect",
        "glyph_run_after_glow_key",
        "glyph_run_layer_key",
        "glyph_run_needs_after_glow",
        "glyph_run_needs_before_glow_split",
        "horizontal_after_clip_rect",
        "horizontal_before_clip_rect",
        "inflate_rect",
        "karaoke_glow_states_differ",
        "karaoke_state_uses_image",
        "relative_fill_rect_signature",
    }
    imported = {
        alias.name: alias.asname
        for node in painter_tree.body
        if isinstance(node, ast.ImportFrom) and node.module == owner
        for alias in node.names
        if alias.name in names
    }
    painter_functions = {
        node.name
        for node in painter_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert imported == {name: f"_{name}" for name in names}
    assert {f"_{name}" for name in names}.isdisjoint(painter_functions)
    targets = _import_targets(layer_owner, layer_path)
    assert f"{PACKAGE}.engine.painter" not in targets
    assert not any(
        target == f"{PACKAGE}.frontend"
        or target.startswith(f"{PACKAGE}.frontend.")
        for target in targets
    )


def test_bitmap_guide_layers_use_explicit_painter_free_ports() -> None:
    owner = f"{PACKAGE}.engine.render.elements.horizontal"
    layer_owner = f"{owner}.layers"
    layer_path = ROOT / "engine/render/elements/horizontal/layers.py"
    painter_path = ROOT / "engine/painter.py"
    layer_tree = ast.parse(layer_path.read_text(encoding="utf-8-sig"))
    painter_tree = ast.parse(painter_path.read_text(encoding="utf-8-sig"))
    layer_members = {
        node.name
        for node in layer_tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    painter_classes = {
        node.name for node in painter_tree.body if isinstance(node, ast.ClassDef)
    }

    assert {
        "BitmapGuideLayer",
        "BitmapGuidePorts",
        "bitmap_guide_band_for_glyph",
        "bitmap_guide_band_for_segments",
        "bitmap_guide_target_rect",
        "paint_bitmap_guide_glyph",
        "paint_bitmap_guide_glyphs",
        "paint_bitmap_guide_transition_glyph",
    } <= layer_members
    assert "_BitmapGuideLayer" not in painter_classes

    ports = [
        node.value
        for node in painter_tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "_BITMAP_GUIDE_PORTS"
            for target in node.targets
        )
    ]
    assert len(ports) == 1
    assert isinstance(ports[0], ast.Call)
    assert isinstance(ports[0].func, ast.Name)
    assert ports[0].func.id == "BitmapGuidePorts"
    assert {keyword.arg for keyword in ports[0].keywords} == {
        "fill_clip_band",
        "fill_clip_band_for_glyphs",
        "n3_following_wipe_band",
    }

    targets = _import_targets(layer_owner, layer_path)
    assert f"{PACKAGE}.engine.painter" not in targets
    assert not any(
        target == f"{PACKAGE}.frontend"
        or target.startswith(f"{PACKAGE}.frontend.")
        for target in targets
    )


def test_glyph_layers_use_thin_compatibility_adapters_and_explicit_ports() -> None:
    owner = f"{PACKAGE}.engine.render.elements.horizontal"
    layer_owner = f"{owner}.layers"
    layer_path = ROOT / "engine/render/elements/horizontal/layers.py"
    painter_path = ROOT / "engine/painter.py"
    layer_tree = ast.parse(layer_path.read_text(encoding="utf-8-sig"))
    painter_tree = ast.parse(painter_path.read_text(encoding="utf-8-sig"))
    layer_classes = {
        node.name for node in layer_tree.body if isinstance(node, ast.ClassDef)
    }
    adapters = {
        node.name: node
        for node in painter_tree.body
        if isinstance(node, ast.ClassDef)
        and node.name
        in {
            "_GlyphRunAfterGlowLayer",
            "_GlyphRunBeforeGlowLayer",
            "_GlyphRunLayer",
            "_GlyphRunSplitGlowLayer",
        }
    }

    assert {
        "GlyphLayerPorts",
        "GlyphRunAfterGlowLayer",
        "GlyphRunBeforeGlowLayer",
        "GlyphRunLayer",
        "GlyphRunSplitGlowLayer",
        "ScopeBoundsLayer",
    } <= layer_classes
    assert set(adapters) == {
        "_GlyphRunAfterGlowLayer",
        "_GlyphRunBeforeGlowLayer",
        "_GlyphRunLayer",
        "_GlyphRunSplitGlowLayer",
    }
    for adapter in adapters.values():
        methods = {
            node.name
            for node in adapter.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert methods == {"__init__"}

    ports = [
        node.value
        for node in painter_tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "_GLYPH_LAYER_PORTS"
            for target in node.targets
        )
    ]
    assert len(ports) == 1
    assert isinstance(ports[0], ast.Call)
    assert isinstance(ports[0].func, ast.Name)
    assert ports[0].func.id == "GlyphLayerPorts"
    assert {keyword.arg for keyword in ports[0].keywords} == {
        "build_glyph_run_after_glow_layer",
        "build_glyph_run_glow_layer",
        "build_glyph_run_layer",
        "fill_clip_band",
        "fill_clip_band_for_glyphs",
        "n3_following_wipe_band",
        "paint_glyph_run_after_glow_wipe",
        "paint_glyph_run_before_glow_direct",
        "paint_glyph_run_combined_glow",
        "run_fill_complete",
    }

    targets = _import_targets(layer_owner, layer_path)
    assert f"{PACKAGE}.engine.painter" not in targets
    assert not any(
        target == f"{PACKAGE}.frontend"
        or target.startswith(f"{PACKAGE}.frontend.")
        for target in targets
    )


def test_horizontal_line_layer_stack_has_one_painter_free_owner() -> None:
    owner = f"{PACKAGE}.engine.render.elements.horizontal"
    layer_owner = f"{owner}.layers"
    layer_path = ROOT / "engine/render/elements/horizontal/layers.py"
    painter_path = ROOT / "engine/painter.py"
    layer_tree = ast.parse(layer_path.read_text(encoding="utf-8-sig"))
    painter_tree = ast.parse(painter_path.read_text(encoding="utf-8-sig"))
    layer_members = {
        node.name
        for node in layer_tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert {
        "LayerStackPorts",
        "glyph_run_can_combine_split_glow",
        "line_layer_stack",
    } <= layer_members
    imported = {
        alias.name: alias.asname
        for node in painter_tree.body
        if isinstance(node, ast.ImportFrom) and node.module == owner
        for alias in node.names
        if alias.name
        in {
            "LayerStackPorts",
            "glyph_run_can_combine_split_glow",
            "line_layer_stack",
        }
    }
    assert imported == {
        "LayerStackPorts": None,
        "glyph_run_can_combine_split_glow": (
            "_glyph_run_can_combine_split_glow"
        ),
        "line_layer_stack": "_build_horizontal_line_layer_stack",
    }

    painter_functions = {
        node.name: node
        for node in painter_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_glyph_run_can_combine_split_glow" not in painter_functions
    adapter = painter_functions["_line_layer_stack"]
    assert len(adapter.body) == 1
    assert isinstance(adapter.body[0], ast.Return)
    call = adapter.body[0].value
    assert isinstance(call, ast.Call)
    assert isinstance(call.func, ast.Name)
    assert call.func.id == "_build_horizontal_line_layer_stack"
    assert [arg.id for arg in call.args if isinstance(arg, ast.Name)] == [
        "layout",
        "t_ms",
        "_HORIZONTAL_LAYER_STACK_PORTS",
    ]

    ports = [
        node.value
        for node in painter_tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "_HORIZONTAL_LAYER_STACK_PORTS"
            for target in node.targets
        )
    ]
    assert len(ports) == 1
    assert isinstance(ports[0], ast.Call)
    assert isinstance(ports[0].func, ast.Name)
    assert ports[0].func.id == "LayerStackPorts"
    assert {keyword.arg for keyword in ports[0].keywords} == {
        "bitmap_guide_layer",
        "fill_clip_band_for_glyphs",
        "glyph_run_after_glow_layer",
        "glyph_run_before_glow_layer",
        "glyph_run_layer",
        "glyph_run_split_glow_layer",
    }

    targets = _import_targets(layer_owner, layer_path)
    assert f"{PACKAGE}.engine.painter" not in targets
    assert not any(
        target == f"{PACKAGE}.frontend"
        or target.startswith(f"{PACKAGE}.frontend.")
        for target in targets
    )


def test_character_transition_layer_stack_has_explicit_factory_ports() -> None:
    owner = f"{PACKAGE}.engine.render.elements.horizontal"
    layer_owner = f"{owner}.layers"
    layer_path = ROOT / "engine/render/elements/horizontal/layers.py"
    painter_path = ROOT / "engine/painter.py"
    layer_tree = ast.parse(layer_path.read_text(encoding="utf-8-sig"))
    painter_tree = ast.parse(painter_path.read_text(encoding="utf-8-sig"))
    layer_members = {
        node.name
        for node in layer_tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert {
        "TransitionLayerStackPorts",
        "char_transition_layer_stack",
    } <= layer_members
    imported = {
        alias.name: alias.asname
        for node in painter_tree.body
        if isinstance(node, ast.ImportFrom) and node.module == owner
        for alias in node.names
        if alias.name
        in {"TransitionLayerStackPorts", "char_transition_layer_stack"}
    }
    assert imported == {
        "TransitionLayerStackPorts": None,
        "char_transition_layer_stack": "_build_char_transition_layer_stack",
    }

    wrappers = {
        node.name: node
        for node in painter_tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_char_transition_layer_stack"
    }
    wrapper = wrappers["_char_transition_layer_stack"]
    assert len(wrapper.body) == 1
    returned = wrapper.body[0]
    assert isinstance(returned, ast.Return)
    assert isinstance(returned.value, ast.Call)
    assert isinstance(returned.value.func, ast.Name)
    assert returned.value.func.id == "_build_char_transition_layer_stack"
    assert any(
        isinstance(argument, ast.Name)
        and argument.id == "_CHAR_TRANSITION_LAYER_STACK_PORTS"
        for argument in returned.value.args
    )

    ports = [
        node.value
        for node in painter_tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "_CHAR_TRANSITION_LAYER_STACK_PORTS"
            for target in node.targets
        )
    ]
    assert len(ports) == 1
    assert isinstance(ports[0], ast.Call)
    assert isinstance(ports[0].func, ast.Name)
    assert ports[0].func.id == "TransitionLayerStackPorts"
    assert {keyword.arg for keyword in ports[0].keywords} == {
        "fill_clip_band_for_glyphs",
        "glyph_run_after_glow_layer",
        "glyph_run_before_glow_layer",
        "glyph_run_layer",
    }

    targets = _import_targets(layer_owner, layer_path)
    assert f"{PACKAGE}.engine.painter" not in targets
    assert not any(
        target == f"{PACKAGE}.frontend"
        or target.startswith(f"{PACKAGE}.frontend.")
        for target in targets
    )


def test_horizontal_ruby_geometry_has_one_painter_free_owner() -> None:
    owner = f"{PACKAGE}.engine.render.elements.horizontal"
    ruby_owner = f"{owner}.ruby"
    ruby_path = ROOT / "engine/render/elements/horizontal/ruby.py"
    painter_path = ROOT / "engine/painter.py"
    painter_tree = ast.parse(painter_path.read_text(encoding="utf-8-sig"))
    names = {
        "n3_ruby_fill_rect",
        "role_ruby_vertical_extra",
        "ruby_after_clip_rect",
        "ruby_after_clip_rect_at_time",
        "ruby_before_clip_rect_at_time",
        "ruby_glow_layer_key",
        "ruby_horizontal_gradient_rect_signature",
        "ruby_glow_can_combine_split",
        "ruby_glow_states_differ",
        "ruby_segment_wipe_state",
        "ruby_text_rect",
        "ruby_text_layer_key",
        "ruby_wipe_geometry",
        "ruby_wipe_state",
    }
    imported = {
        alias.name: alias.asname
        for node in painter_tree.body
        if isinstance(node, ast.ImportFrom) and node.module == owner
        for alias in node.names
        if alias.name in names
    }
    painter_functions = {
        node.name
        for node in painter_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert imported == {name: f"_{name}" for name in names}
    assert {f"_{name}" for name in names}.isdisjoint(painter_functions)
    targets = _import_targets(ruby_owner, ruby_path)
    assert f"{PACKAGE}.engine.painter" not in targets
    assert not any(
        target == f"{PACKAGE}.frontend"
        or target.startswith(f"{PACKAGE}.frontend.")
        for target in targets
    )

    style_owner = f"{PACKAGE}.engine.style.style_semantics"
    style_imports = {
        alias.name: alias.asname
        for node in painter_tree.body
        if isinstance(node, ast.ImportFrom) and node.module == style_owner
        for alias in node.names
        if alias.name == "effective_ruby_karaoke_colors"
    }
    assert style_imports == {
        "effective_ruby_karaoke_colors": "_effective_ruby_karaoke_colors"
    }
    assert "_effective_ruby_karaoke_colors" not in painter_functions


def test_horizontal_ruby_layers_use_thin_adapters_and_explicit_ports() -> None:
    owner = f"{PACKAGE}.engine.render.elements.horizontal"
    ruby_owner = f"{owner}.ruby"
    ruby_path = ROOT / "engine/render/elements/horizontal/ruby.py"
    painter_path = ROOT / "engine/painter.py"
    ruby_tree = ast.parse(ruby_path.read_text(encoding="utf-8-sig"))
    painter_tree = ast.parse(painter_path.read_text(encoding="utf-8-sig"))
    ruby_classes = {
        node.name for node in ruby_tree.body if isinstance(node, ast.ClassDef)
    }
    adapters = {
        node.name: node
        for node in painter_tree.body
        if isinstance(node, ast.ClassDef)
        and node.name
        in {"_RubyGlowLayer", "_RubySplitGlowLayer", "_RubyTextLayer"}
    }

    assert {
        "RubyGlowLayer",
        "RubyLayerPorts",
        "RubySplitGlowLayer",
        "RubyTextLayer",
    } <= ruby_classes
    assert set(adapters) == {
        "_RubyGlowLayer",
        "_RubySplitGlowLayer",
        "_RubyTextLayer",
    }
    for adapter in adapters.values():
        methods = {
            node.name
            for node in adapter.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert methods == {"__init__"}

    ports = [
        node.value
        for node in painter_tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "_RUBY_LAYER_PORTS"
            for target in node.targets
        )
    ]
    assert len(ports) == 1
    assert isinstance(ports[0], ast.Call)
    assert isinstance(ports[0].func, ast.Name)
    assert ports[0].func.id == "RubyLayerPorts"
    assert {keyword.arg for keyword in ports[0].keywords} == {
        "blit_cached_ruby_glow",
        "build_ruby_glow_layer",
        "build_ruby_text_layer",
        "paint_split_glow_path",
        "paint_text_layer_stack",
        "ruby_text_path_and_rect",
    }

    targets = _import_targets(ruby_owner, ruby_path)
    assert f"{PACKAGE}.engine.painter" not in targets
    assert not any(
        target == f"{PACKAGE}.frontend"
        or target.startswith(f"{PACKAGE}.frontend.")
        for target in targets
    )


def test_horizontal_ruby_fragment_uses_thin_painter_raster_port() -> None:
    owner = f"{PACKAGE}.engine.render.elements.horizontal"
    ruby_path = ROOT / "engine/render/elements/horizontal/ruby.py"
    painter_path = ROOT / "engine/painter.py"
    ruby_tree = ast.parse(ruby_path.read_text(encoding="utf-8-sig"))
    painter_tree = ast.parse(painter_path.read_text(encoding="utf-8-sig"))
    ruby_functions = {
        node.name for node in ruby_tree.body if isinstance(node, ast.FunctionDef)
    }
    imported = {
        alias.name: alias.asname
        for node in painter_tree.body
        if isinstance(node, ast.ImportFrom) and node.module == owner
        for alias in node.names
        if alias.name == "paint_ruby_karaoke_fragment"
    }
    wrapper = next(
        node
        for node in painter_tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_paint_ruby_karaoke_fragment"
    )

    assert "paint_ruby_karaoke_fragment" in ruby_functions
    assert imported == {
        "paint_ruby_karaoke_fragment": "_paint_horizontal_ruby_karaoke_fragment"
    }
    assert len(wrapper.body) == 1
    call = wrapper.body[0]
    assert isinstance(call, ast.Expr)
    assert isinstance(call.value, ast.Call)
    assert isinstance(call.value.func, ast.Name)
    assert call.value.func.id == "_paint_horizontal_ruby_karaoke_fragment"
    assert any(
        isinstance(argument, ast.Name) and argument.id == "_RUBY_LAYER_PORTS"
        for argument in call.value.args
    )


def test_horizontal_ruby_layer_stacks_use_thin_factory_ports() -> None:
    owner = f"{PACKAGE}.engine.render.elements.horizontal"
    ruby_owner = f"{owner}.ruby"
    ruby_path = ROOT / "engine/render/elements/horizontal/ruby.py"
    painter_path = ROOT / "engine/painter.py"
    ruby_tree = ast.parse(ruby_path.read_text(encoding="utf-8-sig"))
    painter_tree = ast.parse(painter_path.read_text(encoding="utf-8-sig"))
    ruby_members = {
        node.name
        for node in ruby_tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    names = {
        "RubyStackPorts",
        "ruby_glow_layers",
        "ruby_layer_stack",
        "ruby_text_layers",
    }

    assert names <= ruby_members
    imported = {
        alias.name: alias.asname
        for node in painter_tree.body
        if isinstance(node, ast.ImportFrom) and node.module == owner
        for alias in node.names
        if alias.name in names
    }
    assert imported == {
        "RubyStackPorts": None,
        "ruby_glow_layers": "_build_horizontal_ruby_glow_layers",
        "ruby_layer_stack": "_build_horizontal_ruby_layer_stack",
        "ruby_text_layers": "_build_horizontal_ruby_text_layers",
    }

    wrappers = {
        node.name: node
        for node in painter_tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name
        in {"_ruby_glow_layers", "_ruby_layer_stack", "_ruby_text_layers"}
    }
    assert set(wrappers) == {
        "_ruby_glow_layers",
        "_ruby_layer_stack",
        "_ruby_text_layers",
    }
    expected_calls = {
        "_ruby_glow_layers": "_build_horizontal_ruby_glow_layers",
        "_ruby_layer_stack": "_build_horizontal_ruby_layer_stack",
        "_ruby_text_layers": "_build_horizontal_ruby_text_layers",
    }
    for name, wrapper in wrappers.items():
        assert len(wrapper.body) == 1
        returned = wrapper.body[0]
        assert isinstance(returned, ast.Return)
        assert isinstance(returned.value, ast.Call)
        assert isinstance(returned.value.func, ast.Name)
        assert returned.value.func.id == expected_calls[name]
        assert any(
            isinstance(argument, ast.Name)
            and argument.id == "_RUBY_STACK_PORTS"
            for argument in returned.value.args
        )

    ports = [
        node.value
        for node in painter_tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "_RUBY_STACK_PORTS"
            for target in node.targets
        )
    ]
    assert len(ports) == 1
    assert isinstance(ports[0], ast.Call)
    assert isinstance(ports[0].func, ast.Name)
    assert ports[0].func.id == "RubyStackPorts"
    assert {keyword.arg for keyword in ports[0].keywords} == {
        "ruby_glow_layer",
        "ruby_split_glow_layer",
        "ruby_text_layer",
    }

    targets = _import_targets(ruby_owner, ruby_path)
    assert f"{PACKAGE}.engine.painter" not in targets
    assert not any(
        target == f"{PACKAGE}.frontend"
        or target.startswith(f"{PACKAGE}.frontend.")
        for target in targets
    )


def test_horizontal_ruby_layout_uses_one_explicit_geometry_port() -> None:
    owner = f"{PACKAGE}.engine.render.elements.horizontal"
    ruby_path = ROOT / "engine/render/elements/horizontal/ruby.py"
    painter_path = ROOT / "engine/painter.py"
    ruby_tree = ast.parse(ruby_path.read_text(encoding="utf-8-sig"))
    painter_tree = ast.parse(painter_path.read_text(encoding="utf-8-sig"))
    ruby_members = {
        node.name
        for node in ruby_tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    imported = {
        alias.name: alias.asname
        for node in painter_tree.body
        if isinstance(node, ast.ImportFrom) and node.module == owner
        for alias in node.names
        if alias.name == "layout_rubies"
    }
    wrappers = {
        node.name: node
        for node in painter_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_layout_rubies"
    }

    assert {"RubyLayoutPorts", "layout_rubies"} <= ruby_members
    assert imported == {"layout_rubies": "_build_horizontal_ruby_layouts"}
    wrapper = wrappers["_layout_rubies"]
    assert len(wrapper.body) == 1
    returned = wrapper.body[0]
    assert isinstance(returned, ast.Return)
    assert isinstance(returned.value, ast.Call)
    assert isinstance(returned.value.func, ast.Name)
    assert returned.value.func.id == "_build_horizontal_ruby_layouts"
    assert any(
        isinstance(argument, ast.Name)
        and argument.id == "_RUBY_LAYOUT_PORTS"
        for argument in returned.value.args
    )

    ports = [
        node.value
        for node in painter_tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "_RUBY_LAYOUT_PORTS"
            for target in node.targets
        )
    ]
    assert len(ports) == 1
    assert isinstance(ports[0], ast.Call)
    assert isinstance(ports[0].func, ast.Name)
    assert ports[0].func.id == "RubyLayoutPorts"
    assert {keyword.arg for keyword in ports[0].keywords} == {
        "ruby_wipe_geometry"
    }


def test_render_effect_metrics_have_one_painter_free_owner() -> None:
    owner = f"{PACKAGE}.engine.render.effects"
    metrics_owner = f"{owner}.metrics"
    metrics_path = ROOT / "engine/render/effects/metrics.py"
    painter_path = ROOT / "engine/painter.py"
    painter_tree = ast.parse(painter_path.read_text(encoding="utf-8-sig"))
    names = {
        "glow_blur_radii",
        "glow_concentration_level",
        "glow_extent",
        "glow_pen_width",
        "glow_radius",
        "main_stroke2_width",
        "ruby_baseline_y",
        "ruby_decoration_kind",
        "ruby_glow_concentration_level",
        "ruby_glow_radius",
        "ruby_paint_style",
        "ruby_shadow_dx",
        "ruby_shadow_dy",
        "ruby_stroke_extent",
        "ruby_vertical_extra",
        "ruby_visual_padding",
        "scaled_glow_radius",
        "stroke2_pen_width",
        "stroke_pen_width",
        "text_visual_padding",
        "title_visual_padding",
        "visual_stroke_extent",
        "visual_text_padding",
    }
    imported = {
        alias.name: alias.asname
        for node in painter_tree.body
        if isinstance(node, ast.ImportFrom) and node.module == owner
        for alias in node.names
        if alias.name in names
    }
    painter_members = {
        node.name
        for node in painter_tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert {"__init__.py", "metrics.py"} <= {
        path.name for path in (ROOT / "engine/render/effects").glob("*.py")
    }
    assert imported == {name: f"_{name}" for name in names}
    assert {f"_{name}" for name in names}.isdisjoint(painter_members)
    targets = _import_targets(metrics_owner, metrics_path)
    assert f"{PACKAGE}.engine.painter" not in targets
    assert not any(
        target == f"{PACKAGE}.frontend"
        or target.startswith(f"{PACKAGE}.frontend.")
        for target in targets
    )

    preview_path = ROOT / "engine/style/style_preview.py"
    preview_tree = ast.parse(preview_path.read_text(encoding="utf-8-sig"))
    painter_imports = {
        alias.name
        for node in preview_tree.body
        if isinstance(node, ast.ImportFrom)
        and node.module == f"{PACKAGE}.engine.painter"
        for alias in node.names
    }
    assert painter_imports == {
        "_paint_char_karaoke_stack",
        "_paint_ruby_karaoke_fragment",
    }


def test_render_fill_effects_own_brushes_and_resource_caches() -> None:
    owner = f"{PACKAGE}.engine.render.effects"
    fills_owner = f"{owner}.fills"
    fills_path = ROOT / "engine/render/effects/fills.py"
    painter_path = ROOT / "engine/painter.py"
    painter_tree = ast.parse(painter_path.read_text(encoding="utf-8-sig"))
    function_names = {
        "anchor_texture_brush",
        "brush_for_fill",
        "cached_fill_image",
        "cached_image_brush",
        "clear_fill_caches",
        "fill_brush_rect",
        "fill_is_alpha",
        "fill_signature",
        "gradient_stop_position",
        "gradient_stops",
        "karaoke_state_signature",
        "linear_gradient_brush",
        "split_gradient_stops",
        "split_vertical_brush",
        "valid_color",
    }
    cache_names = {
        "HARD_BAND_BRUSH_CACHE",
        "IMAGE_BRUSH_CACHE",
        "IMAGE_FILL_CACHE",
        "IMAGE_FILL_LOCK",
    }
    names = function_names | cache_names
    imported = {
        alias.name: alias.asname
        for node in painter_tree.body
        if isinstance(node, ast.ImportFrom)
        and node.module == owner
        for alias in node.names
        if alias.name in names
    }
    painter_members = {
        node.name
        for node in painter_tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    painter_assignments = {
        target.id
        for node in painter_tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
        )
        if isinstance(target, ast.Name)
    }

    assert imported == {name: f"_{name}" for name in names}
    assert {f"_{name}" for name in function_names}.isdisjoint(painter_members)
    assert {f"_{name}" for name in cache_names}.isdisjoint(painter_assignments)
    targets = _import_targets(fills_owner, fills_path)
    assert f"{PACKAGE}.engine.painter" not in targets
    assert not any(
        target == f"{PACKAGE}.frontend"
        or target.startswith(f"{PACKAGE}.frontend.")
        for target in targets
    )

    clear_cache = next(
        node
        for node in painter_tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "clear_before_layer_cache"
    )
    calls = {
        node.func.id
        for node in ast.walk(clear_cache)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "_clear_fill_caches" in calls


def test_render_raster_effects_have_one_painter_free_owner() -> None:
    owner = f"{PACKAGE}.engine.render.effects"
    raster_owner = f"{owner}.raster"
    raster_path = ROOT / "engine/render/effects/raster.py"
    effects_root = ROOT / "engine/render/effects"
    painter_path = ROOT / "engine/painter.py"
    painter_tree = ast.parse(painter_path.read_text(encoding="utf-8-sig"))
    names = {
        "paint_fill_path",
        "paint_glow_path",
        "paint_shadow_silhouette",
        "paint_split_glow_path",
        "paint_stroke_path",
        "paint_text_layer_stack",
    }
    imported = {
        alias.name: alias.asname
        for node in painter_tree.body
        if isinstance(node, ast.ImportFrom) and node.module == owner
        for alias in node.names
        if alias.name in names
    }
    painter_members = {
        node.name
        for node in painter_tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert raster_path.is_file()
    assert not (effects_root / "paths.py").exists()
    assert not (effects_root / "glow.py").exists()
    assert imported == {name: f"_{name}" for name in names}
    assert {f"_{name}" for name in names}.isdisjoint(painter_members)
    targets = _import_targets(raster_owner, raster_path)
    assert f"{PACKAGE}.engine.painter" not in targets
    assert f"{PACKAGE}.engine.render.core.raster_blur.blur_image" in targets
    assert not any(
        target == f"{PACKAGE}.frontend"
        or target.startswith(f"{PACKAGE}.frontend.")
        for target in targets
    )

    blur_path = ROOT / "engine/render/core/raster_blur.py"
    blur_tree = ast.parse(blur_path.read_text(encoding="utf-8-sig"))
    public_assignments = {
        target.id
        for node in blur_tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert {
        "blur_image",
        "gaussian_blur_image",
        "n3_gaussian_kernel_1d",
    } <= public_assignments




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
    module_names = {"edit_history.py", "lyrics_list.py", "timeline_view.py"}
    editor_root = ROOT / "frontend" / "editor"
    assert {"__init__.py", *module_names} <= {
        path.name for path in editor_root.glob("*.py")
    }
    assert not any((ROOT / "frontend" / name).exists() for name in module_names)


def test_subtitle_render_window_delegates_edit_history_dispatch() -> None:
    window_path = ROOT / "frontend" / "main_window.py"
    window_module = f"{PACKAGE}.frontend.main_window"
    targets = _import_targets(window_module, window_path)
    assert f"{PACKAGE}.frontend.editor.edit_history" in targets

    tree = ast.parse(window_path.read_text(encoding="utf-8-sig"))
    window_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SubtitleRenderWindow"
    )
    expected_calls = {"_undo_edit": "undo_edit", "_redo_edit": "redo_edit"}
    for method_name, expected_call in expected_calls.items():
        method = next(
            node
            for node in window_class.body
            if isinstance(node, ast.FunctionDef) and node.name == method_name
        )
        calls = {
            node.func.id
            for node in ast.walk(method)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert calls == {expected_call}

    history_path = ROOT / "frontend" / "editor" / "edit_history.py"
    assert f"{PACKAGE}.frontend.main_window" not in _import_targets(
        f"{PACKAGE}.frontend.editor.edit_history",
        history_path,
    )


def test_subtitle_render_window_delegates_role_assignments() -> None:
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
        and node.name == "_on_lyrics_roles_changed"
    )
    calls = {
        node.func.id
        for node in ast.walk(method)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert {"assign_role_to_title_rows", "assign_role_to_track_rows"} <= calls
    assert "guide_symbol_with_role_labels" not in calls
    assert "normalize_title_char_role_labels" not in calls


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


def test_subtitle_render_window_delegates_source_reload_planning() -> None:
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
        and node.name == "_reload_external_subtitle_source"
    )
    direct_merge_calls = {
        node.func.id
        for node in ast.walk(method)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert "plan_reloaded_tracks" in direct_merge_calls
    assert "merge_reloaded_track" not in direct_merge_calls


def test_subtitle_render_window_delegates_runtime_preference_loading() -> None:
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
        if isinstance(node, ast.FunctionDef) and node.name == "_load_persisted_state"
    )
    calls = {
        node.func.id
        for node in ast.walk(method)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert "load_app_runtime_preferences" in calls


def test_subtitle_render_window_delegates_runtime_preference_saving() -> None:
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
        if isinstance(node, ast.FunctionDef) and node.name == "_save_persisted_state"
    )
    calls = {
        node.func.id
        for node in ast.walk(method)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert "update_app_runtime_preferences" in calls


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
        "property_panel.py",
    }
    properties_root = ROOT / "frontend" / "properties"
    assert {"__init__.py", *module_names} <= {
        path.name for path in properties_root.glob("*.py")
    }
    assert not any((ROOT / "frontend" / name).exists() for name in module_names)
    controls_root = properties_root / "controls"
    assert {"__init__.py", "inputs.py", "layout.py", "widgets.py"} <= {
        path.name for path in controls_root.glob("*.py")
    }
    assert not any(
        (properties_root / name).exists()
        for name in {"property_inputs.py", "property_layout.py", "property_widgets.py"}
    )
    roles_root = properties_root / "roles"
    assert {"__init__.py", "color.py", "fills.py", "font.py", "page.py"} <= {
        path.name for path in roles_root.glob("*.py")
    }
    assert not any(
        (properties_root / name).exists()
        for name in {
            "property_role_color_page.py",
            "property_role_fill_pages.py",
            "property_role_font_page.py",
            "property_role_page.py",
        }
    )
    pages_root = properties_root / "pages"
    assert {
        "__init__.py",
        "background.py",
        "effects.py",
        "layout.py",
        "registry.py",
        "timing.py",
        "title.py",
    } <= {path.name for path in pages_root.glob("*.py")}
    assert not any(
        (properties_root / name).exists()
        for name in {
            "property_background_page.py",
            "property_effects_page.py",
            "property_layout_page.py",
            "property_pages.py",
            "property_timing_page.py",
            "property_title_page.py",
        }
    )


def test_subtitle_property_panel_delegates_page_registry_and_routing() -> None:
    panel_path = ROOT / "frontend" / "properties" / "property_panel.py"
    panel_module = f"{PACKAGE}.frontend.properties.property_panel"
    targets = _import_targets(panel_module, panel_path)

    assert f"{PACKAGE}.frontend.properties.pages.registry" in targets
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

    assert f"{PACKAGE}.frontend.properties.controls.widgets" in targets
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

    assert f"{PACKAGE}.frontend.properties.controls.layout" in targets
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

    assert f"{PACKAGE}.frontend.properties.controls.inputs" in targets
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

    assert f"{PACKAGE}.frontend.properties.pages.title" in targets
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

    assert f"{PACKAGE}.frontend.properties.pages.timing" in targets
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

    assert f"{PACKAGE}.frontend.properties.pages.background" in targets
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

    assert f"{PACKAGE}.frontend.properties.pages.effects" in targets
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

    assert f"{PACKAGE}.frontend.properties.pages.layout" in targets
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

    assert f"{PACKAGE}.frontend.properties.roles.page" in targets
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

    assert f"{PACKAGE}.frontend.properties.roles.font" in targets
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

    assert f"{PACKAGE}.frontend.properties.roles.color" in targets
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

    assert f"{PACKAGE}.frontend.properties.roles.fills" in targets
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
