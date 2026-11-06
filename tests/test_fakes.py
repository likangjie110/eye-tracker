"""Contract test keeping the UI test double honest (TD-TEST-009).

The UI reaches the controller both through direct attributes and through
``getattr(controller, name, fallback)`` lookups, so a fake that misses one of those names silently
sends a UI test down the fallback path instead of the production one. This test derives the
controller API that ``src/eye_tracker/ui`` actually references and asserts that the shared
``FakeController`` exposes every public part of it.
"""

from __future__ import annotations

import ast
from pathlib import Path

from eye_tracker.engine.controller import Controller
from test_ui_basic import FakeController

UI = Path(__file__).resolve().parents[1] / "src" / "eye_tracker" / "ui"

#: Names that hold the controller in UI code (``controller``, ``self._controller``, ...).
_CONTROLLER_EXPRESSIONS = frozenset({"controller", "_controller", "ctl"})
#: Below this the extraction has rotted (e.g. an AST walk that no longer matches the UI code).
_MIN_EXPECTED = 25


def _is_controller(node: ast.AST) -> bool:
    """Whether ``node`` is one of the expressions that hold the controller."""
    if isinstance(node, ast.Name):
        return node.id in _CONTROLLER_EXPRESSIONS
    return isinstance(node, ast.Attribute) and node.attr in {"_controller", "controller"}


def _controller_names_in(source: str) -> set[str]:
    """The controller names ``source`` reads directly or looks up through ``getattr``."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and _is_controller(node.value):
            names.add(node.attr)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) > 1
            and _is_controller(node.args[0])
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            names.add(node.args[1].value)
        elif isinstance(node, ast.For | ast.comprehension) and isinstance(
            node.iter, ast.Tuple | ast.List | ast.Set
        ):
            for element in node.iter.elts:
                if isinstance(element, ast.Tuple) and element.elts:
                    # ("signal_name", slot) pairs, connected with getattr below.
                    first = element.elts[0]
                    if isinstance(first, ast.Constant) and isinstance(first.value, str):
                        names.add(first.value)
                elif isinstance(element, ast.Constant) and isinstance(element.value, str):
                    # ("signal_name", "signal_name", ...) tuples, also connected with getattr.
                    names.add(element.value)
    return names


def _ui_controller_names() -> set[str]:
    names: set[str] = set()
    for path in sorted(UI.rglob("*.py")):
        names |= _controller_names_in(path.read_text(encoding="utf-8"))
    return names


def test_fake_controller_exposes_the_controller_api_the_ui_uses(qapp: object) -> None:
    """Every public controller name the UI reaches for must exist on ``FakeController``."""
    used = _ui_controller_names()
    controller_api = {name for name in dir(Controller) if not name.startswith("_")}
    expected = used & controller_api
    assert len(expected) >= _MIN_EXPECTED, (
        "the UI/controller extraction found too little; has the UI code shape changed? "
        f"found: {sorted(expected)}"
    )
    fake = FakeController()
    missing = sorted(name for name in expected if not hasattr(fake, name))
    assert missing == [], f"FakeController is missing controller API the UI uses: {missing}"
