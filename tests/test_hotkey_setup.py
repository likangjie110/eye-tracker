"""Characterization tests for the hotkey registration rules.

The logic was extracted from ``Controller._register_hotkeys``; behaviour is pinned here with a
fake manager so the rules (disabled, suspended, unsupported, announce-once, cleared combinations)
do not depend on a QApplication or a platform.
"""

from __future__ import annotations

from types import SimpleNamespace

from eye_tracker.engine.hotkey_setup import register_hotkeys


class Manager:
    supported = True
    note = ""

    def __init__(self) -> None:
        self.registered: dict[str, object] = {}
        self.failing: set[str] = set()
        self.errors: dict[str, str] = {}
        self.unregister_calls = 0
        self.stop_calls = 0

    def unregister_all(self) -> None:
        self.unregister_calls += 1

    def stop(self) -> None:
        self.stop_calls += 1

    def register(self, name: str, combo: str, callback: object) -> bool:
        if combo in self.failing:
            return False
        self.registered[name] = callback
        return True

    def last_error(self, name: str) -> str | None:
        return self.errors.get(name)


def settings(**overrides: object) -> SimpleNamespace:
    values = {
        "enabled": True,
        "toggle_tracking": "ctrl+alt+t",
        "toggle_privacy": "ctrl+alt+p",
        "recalibrate": "ctrl+alt+c",
    }
    values.update(overrides)
    return SimpleNamespace(hotkeys=SimpleNamespace(**values))


def run(
    manager: Manager,
    settings_obj: SimpleNamespace | None = None,
    announced: set[tuple[str, str]] | None = None,
    *,
    suspended: bool = False,
) -> tuple[set[tuple[str, str]], list[tuple[str, str]], list[str]]:
    notes: list[tuple[str, str]] = []
    pressed: list[str] = []
    result = register_hotkeys(
        manager,
        settings_obj or settings(),
        announced if announced is not None else set(),
        suspended=suspended,
        on_hotkey=pressed.append,
        notify=lambda title, message: notes.append((title, message)),
    )
    return result, notes, pressed


def test_registers_every_configured_hotkey_and_calls_back() -> None:
    manager = Manager()
    announced, notes, pressed = run(manager)
    assert set(manager.registered) == {"toggle_tracking", "toggle_privacy", "recalibrate"}
    assert announced == set()
    assert notes == []
    # The registered callback carries the action name.
    callback = manager.registered["toggle_privacy"]
    assert callable(callback)
    callback()
    assert pressed == ["toggle_privacy"]


def test_a_cleared_combination_is_skipped() -> None:
    manager = Manager()
    run(manager, settings(toggle_tracking="  "))
    assert set(manager.registered) == {"toggle_privacy", "recalibrate"}


def test_suspended_leaves_everything_untouched() -> None:
    manager = Manager()
    announced = {("toggle_tracking", "ctrl+alt+t")}
    result, notes, _ = run(manager, announced=announced, suspended=True)
    assert result is announced
    assert manager.unregister_calls == 0
    assert notes == []


def test_disabled_releases_and_clears_announced_failures() -> None:
    manager = Manager()
    result, _, _ = run(manager, settings(enabled=False), announced={("x", "y")})
    assert manager.unregister_calls == 1
    assert manager.stop_calls == 1
    assert manager.registered == {}
    assert result == set()


def test_unsupported_keeps_the_announced_set() -> None:
    manager = Manager()
    manager.supported = False
    announced = {("x", "y")}
    result, notes, _ = run(manager, announced=announced)
    assert result is announced
    assert manager.registered == {}
    assert notes == []


def test_a_failure_is_announced_once() -> None:
    manager = Manager()
    manager.failing.add("ctrl+alt+t")
    manager.errors["toggle_tracking"] = "taken by another app."
    announced, notes, _ = run(manager)
    assert announced == {("toggle_tracking", "ctrl+alt+t")}
    assert len(notes) == 1
    assert "taken by another app" in notes[0][1]
    assert "Hotkeys" in notes[0][1]

    # The next apply with the same failure is silent.
    announced, notes, _ = run(manager, announced=announced)
    assert announced == {("toggle_tracking", "ctrl+alt+t")}
    assert notes == []


def test_a_failure_without_a_reason_uses_the_generic_message() -> None:
    manager = Manager()
    manager.failing.add("ctrl+alt+c")
    _, notes, _ = run(manager)
    assert len(notes) == 1
    assert "ctrl+alt+c" in notes[0][1]


def test_releasing_failures_does_not_stop_registration() -> None:
    class BrokenManager(Manager):
        def unregister_all(self) -> None:
            raise RuntimeError("platform gone")

    manager = BrokenManager()
    announced, notes, _ = run(manager)
    assert announced == set()
    assert notes == []
    assert set(manager.registered) == {"toggle_tracking", "toggle_privacy", "recalibrate"}


def test_a_manager_without_errors_still_reports_a_failure() -> None:
    manager = Manager()
    manager.failing.add("ctrl+alt+p")
    manager.errors["toggle_privacy"] = "   "
    _, notes, _ = run(manager)
    assert len(notes) == 1
    assert "ctrl+alt+p" in notes[0][1]
