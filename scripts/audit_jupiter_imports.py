from __future__ import annotations

import ast
from pathlib import Path

ERRORS: list[str] = []

# Audit application source only. The audit helper itself is intentionally
# excluded because it contains strings such as os.getenv( and json.dumps(
# as part of the checks it performs.
ROOTS = (
    Path("jupiter-core/app"),
    Path("analyze_runner.py"),
)

EXCLUDED_PARTS = {
    ".git",
    ".venv",
    "venv",
    ".jupiter_runtime_venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
}


def python_files(root: Path):
    if root.is_file() and root.suffix == ".py":
        yield root
        return
    if not root.exists():
        return
    for path in root.rglob("*.py"):
        if any(part in EXCLUDED_PARTS for part in path.parts):
            continue
        yield path


def imported_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


seen: set[Path] = set()
for root in ROOTS:
    for path in python_files(root):
        path = path.resolve()
        if path in seen:
            continue
        seen.add(path)
        try:
            text = path.read_text(encoding="utf-8-sig")
            tree = ast.parse(text, filename=str(path))
        except Exception as exc:
            ERRORS.append(f"syntax: {path}: {exc}")
            continue

        names = imported_names(tree)
        if "os.getenv(" in text and "os" not in names:
            ERRORS.append(f"missing import os: {path}")
        if "json.dumps(" in text and "json" not in names:
            ERRORS.append(f"missing import json: {path}")

if ERRORS:
    print("JUPITER STATIC AUDIT FAILED")
    for item in ERRORS:
        print(item)
    raise SystemExit(1)

print(f"JUPITER STATIC AUDIT = OK ({len(seen)} source files)")
