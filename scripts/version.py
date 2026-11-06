"""Read the one version source for scripts and workflows.

``src/eye_tracker/__init__.py:__version__`` is the only declaration. The shell
implementations in CI and the AppImage script used to parse it with ``sed``,
which silently read nothing when the line was reformatted. This module parses
it with :mod:`ast`, validates it, and is the single reader for shell callers:

    python3 scripts/version.py             # 0.3.0
    python3 scripts/version.py --numeric   # 0.3
    python3 scripts/version.py --tag v0.3.0  # exit 1 if the tag does not match

The PyInstaller spec keeps its own AST reader because it runs inside
PyInstaller with a standalone ``sys.path``; hatchling reads the configured
path itself. Those are build-tool internals, not shell duplicates.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INIT = ROOT / "src" / "eye_tracker" / "__init__.py"

_NUMERIC = re.compile(r"\d+(?:\.\d+){0,2}")


def read_version(path: Path | None = None) -> str:
    """The string value of ``__version__`` in ``path``; raises if it is not a literal."""
    path = INIT if path is None else path
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "__version__":
                try:
                    value = ast.literal_eval(node.value)
                except (ValueError, TypeError) as exc:
                    raise ValueError(f"{path}: __version__ is not a literal string") from exc
                if isinstance(value, str) and value.strip():
                    return value
                raise ValueError(f"{path}: __version__ is not a non-empty string")
    raise ValueError(f"{path}: no __version__ assignment")


def numeric_version(version: str) -> str:
    """The leading ``X[.Y[.Z]]`` part needed by Windows resources and CFBundleVersion."""
    match = _NUMERIC.match(version)
    if match is None:
        raise ValueError(f"version {version!r} has no numeric prefix")
    return match.group(0)


def tag_matches(version: str, tag: str) -> bool:
    """A release tag must be ``v`` plus the exact version."""
    return tag == f"v{version}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--numeric", action="store_true", help="print the numeric version")
    parser.add_argument("--tag", metavar="TAG", help="fail unless TAG == v<version>")
    args = parser.parse_args(argv)
    try:
        version = read_version()
        numeric = numeric_version(version)
    except (OSError, SyntaxError, ValueError, TypeError) as exc:
        print(f"version: {exc}", file=sys.stderr)
        return 1
    if args.tag is not None and not tag_matches(version, args.tag):
        print(
            f"version: tag {args.tag} does not match __version__ {version!r} (expected v{version})",
            file=sys.stderr,
        )
        return 1
    print(numeric if args.numeric else version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
