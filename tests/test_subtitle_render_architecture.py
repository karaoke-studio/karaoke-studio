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
        ROOT / "contracts.py",
        ROOT / "recent_projects.py",
        ROOT / "session.py",
        ROOT / "settings_store.py",
    )
    violations: dict[str, list[str]] = defaultdict(list)
    for path in paths:
        module = _module_name(path)
        for target in _import_targets(module, path):
            if target == forbidden or target.startswith(f"{forbidden}."):
                violations[module].append(target)

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
