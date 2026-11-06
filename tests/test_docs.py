"""Documentation stays in sync with the code."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_generator():
    spec = importlib.util.spec_from_file_location(
        "gen_config_docs", ROOT / "scripts" / "gen_config_docs.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_configuration_reference_is_up_to_date() -> None:
    generator = _load_generator()
    assert generator.main(["--check"]) == 0, "run: uv run python scripts/gen_config_docs.py"


def test_hotkey_defaults_are_listed_per_platform_only_when_they_differ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generator = _load_generator()
    config = generator.config
    monkeypatch.setattr(config, "_HOTKEY_MODIFIERS", {"win32": "ctrl+alt+meta"})
    monkeypatch.setattr(config, "_HOTKEY_MODIFIERS_OTHER", "ctrl+alt+meta")
    assert generator._default("hotkeys.toggle_tracking", "ctrl+alt+meta+t") == "`ctrl+alt+meta+t`"
    monkeypatch.setattr(config, "_HOTKEY_MODIFIERS", {"darwin": "ctrl+alt"})
    assert generator._default("hotkeys.recalibrate", "ctrl+alt+meta+c") == (
        "`ctrl+alt+meta+c` Windows<br>`ctrl+alt+c` macOS<br>`ctrl+alt+meta+c` Linux"
    )
    # Not a hotkey: as it is.
    assert generator._default("camera.device", "0") == "`0`"


def test_section_notes_take_their_numbers_from_the_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The countdown is part of the away time (r2-docs-06): the page says when it starts."""
    generator = _load_generator()
    monkeypatch.setattr(
        generator.config,
        "PresenceSettings",
        lambda: SimpleNamespace(away_timeout_s=60, warning_s=15),
    )
    notes = generator.section_notes()
    assert "appears after 45 s without you, and the action follows at 60 s" in notes["presence"]
    assert f"`{generator.config.HotkeySettings().toggle_tracking}`" in notes["hotkeys"]


def test_every_setting_is_documented() -> None:
    from eye_tracker.config import describe_settings

    undocumented = [row["key"] for row in describe_settings() if not str(row["doc"]).strip()]
    assert undocumented == []


PAGES = [
    ROOT / "README.md",
    ROOT / "README.tr.md",
    ROOT / "AGENTS.md",
    ROOT / "ARCHITECTURE.md",
    ROOT / "CONTRIBUTING.md",
    ROOT / "SECURITY.md",
    ROOT / "CHANGELOG.md",
    *sorted((ROOT / "docs").glob("*.md")),
]


def _local_targets(page: Path) -> list[str]:
    """Relative link and image targets of a Markdown page: ``[text](target)`` and the
    ``<img src="target">`` of the HTML blocks (the README hero and demo)."""
    text = page.read_text(encoding="utf-8")
    targets = re.findall(r"\]\(([^)\s]+)\)", text) + re.findall(r"\ssrc=\"([^\"]+)\"", text)
    return [t for t in targets if "://" not in t and not t.startswith(("#", "mailto:"))]


def test_relative_links_in_markdown_resolve() -> None:
    broken = []
    for page in PAGES:
        for target in _local_targets(page):
            path = target.split("#", 1)[0]
            if path and not (page.parent / path).exists():
                broken.append(f"{page.relative_to(ROOT)} -> {target}")
    # assets/hero.png is rendered from assets/hero.svg and committed with it.
    assert broken == [], f"missing link or image targets: {broken}"


def test_images_are_checked_too(tmp_path: Path) -> None:
    """Regression (r2-docs-11): a missing README hero image went unnoticed, because
    only Markdown links were checked, not ``<img src>``."""
    page = tmp_path / "README.md"
    page.write_text(
        '<img src="assets/hero.png" alt="x">\n[docs](docs/a.md) ![shot](shot.png)\n'
        '<img src="https://example.org/badge.svg">\n',
        encoding="utf-8",
    )
    assert _local_targets(page) == ["docs/a.md", "shot.png", "assets/hero.png"]


REQUIRED_HARNESS_DOCS = [
    "AGENTS.md",
    "ARCHITECTURE.md",
    "docs/index.md",
    "docs/CI.md",
    "docs/QUALITY_SCORE.md",
    "docs/design-docs/index.md",
    "docs/design-docs/core-beliefs.md",
    "docs/design-docs/decisions.md",
    "docs/exec-plans/active/harness-migration.md",
    "docs/exec-plans/tech-debt-tracker.md",
    "docs/exec-plans/completed/README.md",
]


def _documented_packages() -> list[str]:
    src = ROOT / "src" / "eye_tracker"
    return sorted(p.name for p in src.iterdir() if p.is_dir() and p.name != "__pycache__")


def _missing_index_entries(docs_dir: Path, index_text: str) -> list[str]:
    """Pages under ``docs/`` that ``index.md`` does not link.

    ``index.md`` itself is the index, so it is exempt.
    """
    return [
        page.name
        for page in sorted(docs_dir.glob("*.md"))
        if page.name != "index.md" and f"]({page.name})" not in index_text
    ]


def _stale_supported_versions(text: str, version: str) -> list[str]:
    """``X.Y.x`` version rows in a support table that are not the current minor."""
    major, minor = version.split(".")[:2]
    return [
        f"{found_major}.{found_minor}.x"
        for found_major, found_minor in re.findall(r"\b(\d+)\.(\d+)\.x\b", text)
        if (found_major, found_minor) != (major, minor)
    ]


def test_required_harness_documents_exist() -> None:
    missing = [rel for rel in REQUIRED_HARNESS_DOCS if not (ROOT / rel).is_file()]
    assert missing == [], f"missing harness documents: {missing}"


def test_agents_md_stays_a_map() -> None:
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert len(text.splitlines()) <= 200, "AGENTS.md is a map, not an encyclopedia"
    for heading in (
        "## Where to go next",
        "## Commands",
        "## Repository map",
        "## Hard constraints",
    ):
        assert heading in text, heading
    for target in (
        "ARCHITECTURE.md",
        "docs/index.md",
        "docs/design-docs/core-beliefs.md",
        "docs/exec-plans/active/harness-migration.md",
        "docs/CI.md",
    ):
        assert target in text, target
        assert (ROOT / target).exists(), f"AGENTS.md points at a missing file: {target}"


def test_architecture_md_covers_every_package() -> None:
    text = (ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
    missing = [f"{name}/" for name in _documented_packages() if f"{name}/" not in text]
    assert missing == [], f"ARCHITECTURE.md does not mention: {missing}"
    for module in (
        "types.py",
        "config.py",
        "paths.py",
        "logging_setup.py",
        "trace.py",
        "ipc.py",
        "app.py",
        "cli.py",
        "diagnostics.py",
    ):
        assert module in text, f"ARCHITECTURE.md does not mention {module}"


def test_docs_index_lists_every_page() -> None:
    docs = ROOT / "docs"
    index_text = (docs / "index.md").read_text(encoding="utf-8")
    assert _missing_index_entries(docs, index_text) == []


def test_docs_index_check_detects_a_missing_entry(tmp_path: Path) -> None:
    """The completeness check must fail when a page is added without an index entry."""
    (tmp_path / "a.md").write_text("# a\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("# b\n", encoding="utf-8")
    assert _missing_index_entries(tmp_path, "# Index\n[a](a.md)\n") == ["b.md"]


def test_security_supported_versions_match_the_current_release() -> None:
    text = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    init = (ROOT / "src" / "eye_tracker" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "([^"]+)"', init, re.MULTILINE)
    assert match is not None, "cannot read __version__"
    assert _stale_supported_versions(text, match.group(1)) == []


def test_stale_version_check_detects_drift() -> None:
    assert _stale_supported_versions("| 0.1.x | yes |", "0.3.0") == ["0.1.x"]
    assert _stale_supported_versions("| 0.3.x | yes |", "0.3.0") == []


def test_network_claims_are_not_absolute() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    architecture = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    assert "never sends a single byte" not in readme
    assert "never touches the network" not in architecture
    assert "unless you turn on the update check" in readme


def test_troubleshooting_is_linked_from_the_readme() -> None:
    assert "docs/troubleshooting.md" in (ROOT / "README.md").read_text(encoding="utf-8")
