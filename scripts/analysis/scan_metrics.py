#!/usr/bin/env python3
"""Read-only repository scan used by harness Phase 1 (``docs/exec-plans/active/``).

Produces the mechanical evidence the migration plan cites:

* module inventory (path, lines, module docstring),
* oversized files / functions / classes,
* an approximate McCabe score and nesting depth per function,
* the internal import graph, package-level edges and dependency cycles,
* near-duplicate function bodies,
* a few recurring hygiene markers (TODO, ``type: ignore``, bare except, ...).

It never writes to the repository. Run it from anywhere:

    uv run python scripts/analysis/scan_metrics.py            # Markdown to stdout
    uv run python scripts/analysis/scan_metrics.py --json     # JSON summary to stdout

The JSON form is what automation should consume; the Markdown form is for humans.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
TESTS = ROOT / "tests"
PKG = SRC / "eye_tracker"

SMALL_FILE_LINES = 300
BIG_FILE_LINES = 600
HUGE_FILE_LINES = 1000
LONG_FUNCTION_LINES = 60
HUGE_FUNCTION_LINES = 120
GOD_CLASS_LINES = 300
GOD_CLASS_METHODS = 20
DUP_MIN_LINES = 12
DUP_RATIO = 0.90

DECISION_NODES = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.ExceptHandler,
    ast.IfExp,
)
NESTING_NODES = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.Try,
    ast.With,
    ast.AsyncWith,
    ast.Match,
)


@dataclass
class FunctionInfo:
    module: str
    qualname: str
    file: str
    line: int
    end_line: int
    lines: int
    decisions: int
    nesting: int
    args: int
    is_async: bool = False


@dataclass
class ClassInfo:
    module: str
    name: str
    file: str
    line: int
    lines: int
    methods: int
    public_methods: int


@dataclass
class ModuleInfo:
    module: str
    file: str
    package: str
    lines: int
    docstring: str
    functions: list[str] = field(default_factory=list)
    classes: list[str] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)


def module_name(path: Path) -> str:
    rel = path.relative_to(SRC).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


SUBPACKAGES = {"engine", "gaze", "panes", "platform", "ui", "update", "vision"}


def package_of(module: str) -> str:
    parts = module.split(".")
    if len(parts) == 1:
        return "(pkg)"
    if len(parts) == 2:
        return parts[1] if parts[1] in SUBPACKAGES else "(root)"
    return parts[1]


def iter_py(base: Path) -> list[Path]:
    return sorted(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)


def walk_with_depth(node: ast.AST, depth: int = 0):
    """Yield (node, control-nesting-depth) for a function body."""
    for child in ast.iter_child_nodes(node):
        child_depth = depth + 1 if isinstance(child, NESTING_NODES) else depth
        yield child, child_depth
        yield from walk_with_depth(child, child_depth)


def decision_count(node: ast.AST) -> int:
    total = 0
    for child in ast.walk(node):
        if isinstance(child, DECISION_NODES):
            total += 1
        elif isinstance(child, ast.BoolOp):
            total += max(0, len(child.values) - 1)
        elif isinstance(child, ast.comprehension):
            total += 1 + len(child.ifs)
        elif isinstance(child, ast.Match):
            total += len(child.cases)
    return total


def max_nesting(node: ast.AST) -> int:
    return max((depth for _, depth in walk_with_depth(node)), default=0)


def resolve_import(module: str, is_pkg: bool, node: ast.AST) -> list[str]:
    targets: list[str] = []
    if isinstance(node, ast.Import):
        targets = [alias.name for alias in node.names]
    elif isinstance(node, ast.ImportFrom):
        base = node.module or ""
        if node.level:
            parts = module.split(".")
            if not is_pkg:
                parts = parts[:-1]
            up = node.level - 1
            if up:
                parts = parts[: len(parts) - up] if up < len(parts) else []
            base = ".".join([*parts, base] if base else parts)
        if node.names and node.names[0].name == "*":
            targets = [base]
        else:
            targets = [f"{base}.{alias.name}" if base else alias.name for alias in node.names]
            if base:
                targets.append(base)
    return [t for t in targets if t.startswith("eye_tracker")]


def normalize_body(source: str, node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Strip the docstring, comments and blank lines, collapse whitespace."""
    lines = source.splitlines()
    start = node.body[0].lineno - 1 if node.body else node.lineno
    if (
        node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    ):
        end = getattr(node.body[0], "end_lineno", node.body[0].lineno)
        start = end
    raw = "\n".join(lines[max(start - 1, 0) : getattr(node, "end_lineno", node.lineno)])
    out = []
    for raw_line in raw.splitlines():
        stripped = re.sub(r"#.*$", "", raw_line).strip()
        if stripped:
            out.append(re.sub(r"\s+", " ", stripped))
    return "\n".join(out)


def shingles(text: str, size: int = 3) -> set[int]:
    tokens = re.findall(r"[A-Za-z_][A-Za-z_0-9]*|\d+|==|!=|<=|>=|[-+*/%<>]=?|[(){}\[\],.:]", text)
    if len(tokens) < size:
        return {hashlib.blake2b(text.encode(), digest_size=8).hexdigest().__hash__()}
    return {hash(tuple(tokens[i : i + size])) for i in range(len(tokens) - size + 1)}


def scan() -> dict:
    modules: list[ModuleInfo] = []
    functions: list[FunctionInfo] = []
    classes: list[ClassInfo] = []
    edges: set[tuple[str, str]] = set()
    parse_errors: list[str] = []
    file_sources: dict[str, str] = {}
    bodies: dict[tuple[str, int], str] = {}

    for path in iter_py(PKG):
        mod = module_name(path)
        rel = str(path.relative_to(ROOT))
        source = path.read_text(encoding="utf-8")
        file_sources[mod] = source
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:  # pragma: no cover - would be a real finding
            parse_errors.append(f"{rel}:{exc.lineno}: {exc.msg}")
            continue
        info = ModuleInfo(
            module=mod,
            file=rel,
            package=package_of(mod),
            lines=source.count("\n") + (0 if source.endswith("\n") else 1),
            docstring=(ast.get_docstring(tree) or "").splitlines()[0]
            if ast.get_docstring(tree)
            else "",
        )
        modules.append(info)

        for node in ast.walk(tree):
            if isinstance(node, ast.Import | ast.ImportFrom):
                for target in resolve_import(mod, path.name == "__init__.py", node):
                    edges.add((mod, target))

        for node in tree.body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                info.functions.append(node.name)
            elif isinstance(node, ast.ClassDef):
                info.classes.append(node.name)

        # Function-level metrics, including methods.
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            qual = node.name
            f = FunctionInfo(
                module=mod,
                qualname=qual,
                file=rel,
                line=node.lineno,
                end_line=getattr(node, "end_lineno", node.lineno),
                lines=getattr(node, "end_lineno", node.lineno) - node.lineno + 1,
                decisions=decision_count(node),
                nesting=max_nesting(node),
                args=len(node.args.args) + len(node.args.kwonlyargs),
                is_async=isinstance(node, ast.AsyncFunctionDef),
            )
            functions.append(f)
            bodies[(mod, node.lineno)] = normalize_body(source, node)

        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            methods = [
                n for n in node.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
            ]
            classes.append(
                ClassInfo(
                    module=mod,
                    name=node.name,
                    file=rel,
                    line=node.lineno,
                    lines=getattr(node, "end_lineno", node.lineno) - node.lineno + 1,
                    methods=len(methods),
                    public_methods=sum(1 for m in methods if not m.name.startswith("_")),
                )
            )

    # Duplicate detection: candidate shingles + confirmation, functions with enough body.
    dup_candidates: list[dict[str, Any]] = []
    body_by_func: dict[int, tuple[FunctionInfo, str, set[int]]] = {}
    for idx, f in enumerate(functions):
        if f.lines < DUP_MIN_LINES:
            continue
        body = bodies.get((f.module, f.line))
        if body is None or len(body.splitlines()) < DUP_MIN_LINES:
            continue
        body_by_func[idx] = (f, body, shingles(body))

    items = list(body_by_func.values())
    seen_pairs: set[tuple[str, int, str, int]] = set()
    for i in range(len(items)):
        fi, bi, si = items[i]
        for j in range(i + 1, len(items)):
            fj, bj, sj = items[j]
            if fi.module == fj.module:
                continue
            union = len(si | sj)
            if not union or len(si & sj) / union < 0.55:
                continue
            ratio = SequenceMatcher(None, bi, bj).ratio()
            if ratio < DUP_RATIO:
                continue
            key = (fi.module, fi.line, fj.module, fj.line)
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            dup_candidates.append(
                {
                    "a": f"{fi.file}:{fi.line} {fi.qualname}",
                    "b": f"{fj.file}:{fj.line} {fj.qualname}",
                    "lines": min(fi.lines, fj.lines),
                    "ratio": round(ratio, 2),
                }
            )
    dup_candidates.sort(key=lambda d: (-d["lines"], -d["ratio"]))

    # Trim symbol-level import targets back to modules we actually scanned.
    module_set = {m.module for m in modules}
    normalized_edges: set[tuple[str, str]] = set()
    unresolved: set[str] = set()
    for src_mod, dst_mod in sorted(edges):
        if not dst_mod.startswith("eye_tracker"):
            continue
        target = dst_mod
        while target not in module_set and "." in target:
            target = target.rsplit(".", 1)[0]
        if target not in module_set or target == src_mod:
            unresolved.add(dst_mod)
            continue
        normalized_edges.add((src_mod, target))

    package_edges: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for src_mod, dst_mod in sorted(normalized_edges):
        a, b = package_of(src_mod), package_of(dst_mod)
        if a != b:
            package_edges[a][b] += 1

    cycle_members = find_cycles(module_set, normalized_edges)
    cycles = [
        {
            "members": members,
            "edges": [
                f"{a} -> {b}" for a, b in sorted(normalized_edges) if a in members and b in members
            ][:40],
        }
        for members in cycle_members
    ]

    test_lines = sum(p.read_text(encoding="utf-8").count("\n") for p in iter_py(TESTS))
    src_lines = sum(m.lines for m in modules)

    markers = scan_markers()

    return {
        "generated_at": __import__("datetime")
        .datetime.now()
        .astimezone()
        .isoformat(timespec="seconds"),
        "root": str(ROOT),
        "totals": {
            "python_files_src": len(modules),
            "python_lines_src": src_lines,
            "python_files_tests": len(iter_py(TESTS)),
            "python_lines_tests": test_lines,
            "functions": len(functions),
            "classes": len(classes),
            "internal_import_edges": len(normalized_edges),
        },
        "parse_errors": parse_errors,
        "modules": [asdict(m) for m in modules],
        "top_files": [
            {"file": m.file, "lines": m.lines} for m in sorted(modules, key=lambda m: -m.lines)[:20]
        ],
        "top_functions": [
            asdict(f) for f in sorted(functions, key=lambda f: (-f.lines, -f.decisions))[:20]
        ],
        "top_complex": [
            asdict(f)
            for f in sorted(
                (f for f in functions if f.lines >= 15),
                key=lambda f: (-f.decisions, -f.lines),
            )[:20]
        ],
        "long_functions": [asdict(f) for f in functions if f.lines >= LONG_FUNCTION_LINES],
        "huge_functions": [asdict(f) for f in functions if f.lines >= HUGE_FUNCTION_LINES],
        "deep_functions": sorted(
            (asdict(f) for f in functions if f.nesting >= 4), key=lambda d: -d["nesting"]
        )[:20],
        "god_classes": [
            asdict(c)
            for c in classes
            if c.lines >= GOD_CLASS_LINES or c.methods >= GOD_CLASS_METHODS
        ],
        "big_files": [m.file for m in modules if m.lines >= BIG_FILE_LINES],
        "huge_files": [m.file for m in modules if m.lines >= HUGE_FILE_LINES],
        "package_edges": {a: dict(b) for a, b in sorted(package_edges.items())},
        "unresolved_internal_imports": sorted(unresolved),
        "cycles": cycles,
        "duplicates": dup_candidates[:20],
        "markers": markers,
    }


def find_cycles(nodes: set[str], edges: set[tuple[str, str]]) -> list[list[str]]:
    graph: dict[str, set[str]] = defaultdict(set)
    for a, b in edges:
        if a in nodes and b in nodes:
            graph[a].add(b)

    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    low: dict[str, int] = {}
    result: list[list[str]] = []

    def strongconnect(v: str) -> None:
        nonlocal index
        indices[v] = low[v] = index
        index += 1
        stack.append(v)
        on_stack.add(v)
        for w in graph[v]:
            if w not in indices:
                strongconnect(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], indices[w])
        if low[v] == indices[v]:
            component = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                component.append(w)
                if w == v:
                    break
            if len(component) > 1:
                result.append(sorted(component))

    for node in sorted(nodes):
        if node not in indices:
            strongconnect(node)
    return sorted(result, key=len, reverse=True)[:15]


def scan_markers() -> dict[str, int]:
    patterns = {
        "TODO/FIXME/XXX": re.compile(r"\b(TODO|FIXME|XXX|HACK)\b"),
        "type: ignore": re.compile(r"type:\s*ignore"),
        "noqa": re.compile(r"#\s*noqa"),
        "bare except": re.compile(r"except\s*:"),
        "print() in src": re.compile(r"^\s*print\(", re.MULTILINE),
        "subprocess shell=True": re.compile(r"shell\s*=\s*True"),
        "os.system": re.compile(r"\bos\.system\s*\("),
        "eval/exec": re.compile(r"\b(eval|exec)\s*\("),
        "pickle": re.compile(r"\bpickle\."),
        "yaml.load": re.compile(r"yaml\.load\s*\("),
        "requests/urllib": re.compile(r"\b(requests|urllib|http\.client|socket)\b"),
        "time.sleep": re.compile(r"\btime\.sleep\s*\("),
        "mutable default": re.compile(r"def\s+\w+\([^)]*=\s*(\[\]|\{\})"),
        "assert in src": re.compile(r"^\s+assert\s", re.MULTILINE),
    }
    counts: dict[str, int] = {}
    for path in iter_py(PKG):
        text = path.read_text(encoding="utf-8")
        for name, pattern in patterns.items():
            counts[name] = counts.get(name, 0) + len(pattern.findall(text))
    return counts


def render_markdown(data: dict) -> str:
    out: list[str] = []
    t = data["totals"]
    out.append("# 机械扫描结果（scripts/analysis/scan_metrics.py）\n")
    out.append(f"生成时间：{data['generated_at']}  ")
    out.append(f"根目录：`{data['root']}`\n")
    out.append("## 总量\n")
    out.append(f"- src Python 文件：{t['python_files_src']}，代码行：{t['python_lines_src']}")
    out.append(f"- tests Python 文件：{t['python_files_tests']}，代码行：{t['python_lines_tests']}")
    summary = (
        f"- 函数：{t['functions']}，类：{t['classes']}，"
        f"内部 import 边：{t['internal_import_edges']}\n"
    )
    out.append(summary)

    out.append("## 超长文件（>=600 行）\n")
    out.append("| 文件 | 行数 |\n|---|---|")
    for item in data["top_files"][:20]:
        marker = " ⚠️" if item["lines"] >= HUGE_FILE_LINES else ""
        out.append(f"| {item['file']} | {item['lines']}{marker} |")
    out.append("")

    out.append("## 超长函数（>=60 行，按行数）\n")
    out.append("| 位置 | 函数 | 行数 | 决策点 | 嵌套 |\n|---|---|---|---|---|")
    for f in data["long_functions"][:25]:
        row = (
            f"| {f['file']}:{f['line']} | {f['qualname']} | {f['lines']} | "
            f"{f['decisions']} | {f['nesting']} |"
        )
        out.append(row)
    out.append("")

    out.append("## 高复杂度函数（决策点 top 20，>=15 行）\n")
    out.append("| 位置 | 函数 | 决策点 | 行数 | 嵌套 |\n|---|---|---|---|---|")
    for f in data["top_complex"]:
        row = (
            f"| {f['file']}:{f['line']} | {f['qualname']} | {f['decisions']} | "
            f"{f['lines']} | {f['nesting']} |"
        )
        out.append(row)
    out.append("")

    out.append("## God Object 候选（>=300 行或 >=20 方法）\n")
    out.append("| 位置 | 类 | 行数 | 方法数 | 公开方法 |\n|---|---|---|---|---|")
    for c in data["god_classes"]:
        row = (
            f"| {c['file']}:{c['line']} | {c['name']} | {c['lines']} | "
            f"{c['methods']} | {c['public_methods']} |"
        )
        out.append(row)
    out.append("")

    out.append("## 包间依赖计数（行 → 列）\n")
    packages = sorted(
        set(data["package_edges"]) | {q for row in data["package_edges"].values() for q in row}
    )
    out.append("| from \\ to | " + " | ".join(packages) + " |")
    out.append("|---|" + "|".join("---" for _ in packages) + "|")
    for a in packages:
        cells = [str(data["package_edges"].get(a, {}).get(b, "")) for b in packages]
        out.append(f"| {a} | " + " | ".join(cells) + " |")
    out.append("")

    out.append("## 循环依赖（模块级强连通分量）\n")
    if data["cycles"]:
        for cycle in data["cycles"]:
            out.append(f"- SCC（{len(cycle['members'])} 个模块）：" + ", ".join(cycle["members"]))
            out.append("  - 成员间依赖边：")
            for edge in cycle["edges"]:
                out.append(f"    - `{edge}`")
    else:
        out.append("- 未发现模块级循环依赖")
    out.append("")

    out.append("## 近似重复函数（跨模块，>=12 行）\n")
    if data["duplicates"]:
        out.append("| A | B | 较小行数 | 相似度 |\n|---|---|---|---|")
        for d in data["duplicates"]:
            out.append(f"| {d['a']} | {d['b']} | {d['lines']} | {d['ratio']} |")
    else:
        out.append("- 未发现达到阈值的跨模块重复函数")
    out.append("")

    out.append("## 卫生标记计数\n")
    out.append("| 标记 | 次数 |\n|---|---|")
    for name, count in data["markers"].items():
        out.append(f"| {name} | {count} |")
    out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json", action="store_true", help="emit the JSON summary instead of Markdown"
    )
    parser.add_argument("--out", type=Path, help="also write the chosen format to this path")
    args = parser.parse_args(argv)

    data = scan()
    text = json.dumps(data, ensure_ascii=False, indent=2) if args.json else render_markdown(data)
    print(text)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    return 1 if data["parse_errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
