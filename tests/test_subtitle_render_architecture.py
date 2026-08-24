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
        ROOT / "engine" / "render_job.py",
        ROOT / "engine" / "render_job_policy.py",
        ROOT / "engine" / "render_bands.py",
        ROOT / "paint.py",
        ROOT / "paint_codec.py",
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
