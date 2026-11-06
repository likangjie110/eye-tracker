"""The dependency rules in ``ARCHITECTURE.md``, enforced mechanically.

These tests parse the source tree, build the internal import graph and check the layering table
from ``ARCHITECTURE.md`` (rules R1-R5). Known, tracked exceptions are listed explicitly; an
exception that no longer occurs fails the suite so the list can only shrink. A separate check keeps
the module-level cycles from growing: existing cycles are allowed as a frozen set, a new cycle is
not.

The checks are written as pure functions over ``{module: source}`` maps so the negative cases can
be proven with small synthetic packages instead of mutating the real tree.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
PKG_ROOT = "eye_tracker"

FOUNDATION = {
    "eye_tracker.command_text",
    "eye_tracker.types",
    "eye_tracker.config",
    "eye_tracker.config_coerce",
    "eye_tracker.paths",
    "eye_tracker.ipc_runtime",
    "eye_tracker.reading",
    "eye_tracker.logging_setup",
    "eye_tracker.trace",
}
CONTROLLER = {
    "eye_tracker.engine.controller",
    "eye_tracker.engine.controller_calibration",
    "eye_tracker.engine.controller_hotkeys",
    "eye_tracker.engine.controller_panes",
    "eye_tracker.engine.controller_screens",
    "eye_tracker.engine.controller_support",
}
COMPOSITION = {
    "eye_tracker.app",
    "eye_tracker.app_startup",
    "eye_tracker.app_updates",
    "eye_tracker.app_windows",
    "eye_tracker.ipc",
    "eye_tracker.diagnostics",
    "eye_tracker.diagnostics_rules",
}
ENTRY = {"eye_tracker.cli", "eye_tracker.__main__"}

#: Which layers a layer may import. ``*`` is the composition root and may wire anything.
ALLOWED: dict[str, set[str]] = {
    "(pkg)": {"*"},
    "foundation": {"foundation", "(pkg)"},
    "gaze": {"foundation", "(pkg)", "gaze"},
    "engine": {"foundation", "(pkg)", "gaze", "engine"},
    "vision": {"foundation", "(pkg)", "vision"},
    "platform": {"foundation", "(pkg)", "platform"},
    "panes": {"foundation", "(pkg)", "engine", "panes", "platform"},
    "update": {"foundation", "(pkg)", "update"},
    "ui": {
        "foundation",
        "(pkg)",
        "engine",
        "gaze",
        "platform",
        "update",
        "vision",
        "ui",
        "controller",
    },
    "controller": {"*"},
    "composition": {"*"},
    "entry": {"*"},
}

#: Documented exceptions from ARCHITECTURE.md. Each maps to the tracker entry that removes it.
EXCEPTIONS: dict[tuple[str, str], str] = {
    ("eye_tracker.ui.settings_status", "eye_tracker.diagnostics"): "TD-ARCH-002",
}

#: Module-level cycles that exist today (frozen). New cycles fail; shrinking is allowed.
ALLOWED_CYCLES: list[frozenset[str]] = []

PURE_MODULES = (
    "eye_tracker.gaze.",
    "eye_tracker.engine.",
    "eye_tracker.reading.",
)
PURE_EXCEPTIONS = {
    "eye_tracker.engine.controller",
    "eye_tracker.engine.controller_calibration",
    "eye_tracker.engine.controller_screens",
    "eye_tracker.engine.controller_support",
}

QT_CV_ROOTS = {"PySide6", "cv2"}


def _module_name(path: Path) -> str:
    rel = path.relative_to(SRC).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def read_modules(root: Path = ROOT) -> dict[str, tuple[Path, str]]:
    package = root / "src" / PKG_ROOT
    modules: dict[str, tuple[Path, str]] = {}
    for path in sorted(package.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        modules[_module_name(path)] = (path, path.read_text(encoding="utf-8"))
    return modules


def _candidates(module: str, is_package: bool, node: ast.AST) -> list[str]:
    """The absolute names an import statement may refer to, before trimming to real modules."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if not isinstance(node, ast.ImportFrom):
        return []
    if not node.level:
        base = node.module or ""
        targets = [base] if base else []
        targets += [f"{base}.{alias.name}" if base else alias.name for alias in node.names]
        return [t for t in targets if t]
    parts = module.split(".") if is_package else module.split(".")[:-1]
    if node.level > 1:
        parts = parts[: -(node.level - 1)]
    base = ".".join([*parts, node.module] if node.module else parts)
    if any(alias.name == "*" for alias in node.names):
        return [base]
    return [base, *(f"{base}.{alias.name}" for alias in node.names)]


def _runtime_imports(tree: ast.AST) -> list[ast.Import | ast.ImportFrom]:
    """Import nodes that execute at runtime; ``if TYPE_CHECKING:`` blocks are type-only."""
    found: list[ast.Import | ast.ImportFrom] = []

    def visit(node: ast.AST, *, type_only: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if (
                isinstance(child, ast.If)
                and isinstance(child.test, ast.Name)
                and child.test.id == "TYPE_CHECKING"
            ):
                for branch in (child.body, child.orelse):
                    visit_children(branch, type_only=True)
                continue
            if isinstance(child, ast.Import | ast.ImportFrom) and not type_only:
                found.append(child)
            visit(child, type_only=type_only)

    def visit_children(nodes: list[ast.stmt], *, type_only: bool) -> None:
        for node in nodes:
            if isinstance(node, ast.Import | ast.ImportFrom):
                if not type_only:
                    found.append(node)
            else:
                visit(node, type_only=type_only)

    visit(tree, type_only=False)
    return found


def internal_edges(modules: dict[str, tuple[Path, str]]) -> set[tuple[str, str]]:
    known = set(modules)
    edges: set[tuple[str, str]] = set()
    for module, (path, source) in modules.items():
        tree = ast.parse(source)
        for node in _runtime_imports(tree):
            for candidate in _candidates(module, path.name == "__init__.py", node):
                target = candidate
                while target not in known and "." in target:
                    target = target.rsplit(".", 1)[0]
                if target in known and target != module:
                    edges.add((module, target))
    return edges


def layer_of(module: str) -> str:
    if module in FOUNDATION:
        return "foundation"
    if module in CONTROLLER:
        return "controller"
    if module in COMPOSITION:
        return "composition"
    if module in ENTRY:
        return "entry"
    if module == PKG_ROOT:
        return "(pkg)"
    parts = module.split(".")
    top = parts[1] if len(parts) > 1 else ""
    if top in ALLOWED:
        return top
    return "other"


def violations(edges: set[tuple[str, str]]) -> list[tuple[str, str, str, str]]:
    result = []
    for source, target in sorted(edges):
        if (source, target) in EXCEPTIONS:
            continue
        source_layer, target_layer = layer_of(source), layer_of(target)
        allowed = ALLOWED.get(source_layer, set())
        if "*" in allowed or target_layer in allowed:
            continue
        result.append((source, target, source_layer, target_layer))
    return result


def stale_exceptions(edges: set[tuple[str, str]]) -> list[tuple[str, str]]:
    return sorted(exception for exception in EXCEPTIONS if exception not in edges)


def sccs(nodes: set[str], edges: set[tuple[str, str]]) -> list[frozenset[str]]:
    graph: dict[str, set[str]] = defaultdict(set)
    for source, target in edges:
        if source in nodes and target in nodes:
            graph[source].add(target)
    index = 0
    indices: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    result: list[frozenset[str]] = []

    def strongconnect(node: str) -> None:
        nonlocal index
        indices[node] = low[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for neighbour in graph[node]:
            if neighbour not in indices:
                strongconnect(neighbour)
                low[node] = min(low[node], low[neighbour])
            elif neighbour in on_stack:
                low[node] = min(low[node], indices[neighbour])
        if low[node] == indices[node]:
            component = set()
            while True:
                popped = stack.pop()
                on_stack.discard(popped)
                component.add(popped)
                if popped == node:
                    break
            if len(component) > 1:
                result.append(frozenset(component))

    for node in sorted(nodes):
        if node not in indices:
            strongconnect(node)
    return result


def new_cycles(edges: set[tuple[str, str]], nodes: set[str] | None = None) -> list[frozenset[str]]:
    nodes = (
        nodes
        if nodes is not None
        else {source for source, _ in edges} | {target for _, target in edges}
    )
    return [
        cycle
        for cycle in sccs(nodes, edges)
        if not any(cycle <= allowed for allowed in ALLOWED_CYCLES)
    ]


def forbidden_qt_cv(
    modules: dict[str, tuple[Path, str]], *, top_level_only: bool = False
) -> list[tuple[str, str]]:
    findings: list[tuple[str, str]] = []
    for module, (_path, source) in modules.items():
        tree = ast.parse(source)
        candidates: list[ast.AST] = list(tree.body) if top_level_only else list(ast.walk(tree))
        for node in candidates:
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if name.split(".")[0] in QT_CV_ROOTS:
                    findings.append((module, name))
    return findings


def pure_modules(modules: dict[str, tuple[Path, str]]) -> dict[str, tuple[Path, str]]:
    return {
        module: value
        for module, value in modules.items()
        if module.startswith(PURE_MODULES) and module not in PURE_EXCEPTIONS
    }


def test_dependency_direction_is_clean() -> None:
    edges = internal_edges(read_modules())
    assert violations(edges) == [], (
        "import direction violates ARCHITECTURE.md; either move the import or add a tracked "
        "exception there. Offending edges: " + repr(violations(edges))
    )


def test_no_new_module_cycle() -> None:
    edges = internal_edges(read_modules())
    assert new_cycles(edges) == [], (
        "a module cycle appeared that is not in ALLOWED_CYCLES; add it to ARCHITECTURE.md's "
        "exception table and the tracker, or break the cycle"
    )


def test_exception_table_only_shrinks() -> None:
    edges = internal_edges(read_modules())
    assert stale_exceptions(edges) == [], (
        "these exceptions no longer occur - remove them from ARCHITECTURE.md and EXCEPTIONS"
    )
    for (source, target), tracker_id in EXCEPTIONS.items():
        assert tracker_id.startswith("TD-ARCH-"), (source, target)


def test_pure_logic_has_no_qt_or_opencv() -> None:
    findings = forbidden_qt_cv(pure_modules(read_modules()))
    assert findings == [], f"pure modules must not import Qt/OpenCV: {findings}"


def test_foundation_imports_qt_only_lazily() -> None:
    foundation = {module: value for module, value in read_modules().items() if module in FOUNDATION}
    assert forbidden_qt_cv(foundation, top_level_only=True) == []


def test_synthetic_wrong_direction_is_rejected() -> None:
    modules = {
        "eye_tracker.gaze.thing": (
            Path("gaze/thing.py"),
            "from ..ui.window import Window\n",
        ),
        "eye_tracker.ui.window": (Path("ui/window.py"), "x = 1\n"),
    }
    assert ("eye_tracker.gaze.thing", "eye_tracker.ui.window") in internal_edges(modules)
    assert violations(internal_edges(modules)) == [
        ("eye_tracker.gaze.thing", "eye_tracker.ui.window", "gaze", "ui")
    ]


def test_synthetic_pure_module_with_qt_is_rejected() -> None:
    modules = {
        "eye_tracker.engine.decision": (
            Path("engine/decision.py"),
            "from PySide6.QtCore import QObject\n",
        ),
    }
    assert forbidden_qt_cv(pure_modules(modules)) == [
        ("eye_tracker.engine.decision", "PySide6.QtCore")
    ]


def test_synthetic_new_cycle_is_rejected() -> None:
    edges = {
        ("eye_tracker.gaze.a", "eye_tracker.ui.b"),
        ("eye_tracker.ui.b", "eye_tracker.gaze.a"),
    }
    nodes = {source for source, _ in edges} | {target for _, target in edges}
    assert new_cycles(edges, nodes) == [frozenset({"eye_tracker.gaze.a", "eye_tracker.ui.b"})]


def test_synthetic_stale_exception_is_rejected() -> None:
    assert stale_exceptions(set()) == sorted(EXCEPTIONS)


def test_type_checking_imports_are_not_runtime_dependencies() -> None:
    modules = {
        "eye_tracker.engine.thing": (
            Path("engine/thing.py"),
            "from typing import TYPE_CHECKING\n\n"
            "if TYPE_CHECKING:\n"
            "    from eye_tracker.ui import window\n",
        ),
        "eye_tracker.ui.window": (Path("ui/window.py"), "x = 1\n"),
    }
    assert internal_edges(modules) == set()
