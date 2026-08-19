from pathlib import Path
import ast
import re

ERRORS = []

for root in (Path("jupiter-core/app"), Path(".")):
    if not root.exists():
        continue
    for path in root.rglob("*.py"):
        if path.as_posix().startswith("jupiter-core/."):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            ast.parse(text)
        except Exception as exc:
            ERRORS.append(f"syntax: {path}: {exc}")
            continue

        if "os.getenv(" in text and not re.search(r"^\s*import os\s*$|^\s*from os import ", text, re.M):
            ERRORS.append(f"missing import os: {path}")

        if "json.dumps(" in text and not re.search(r"^\s*import json\s*$|^\s*from json import ", text, re.M):
            ERRORS.append(f"missing import json: {path}")

if ERRORS:
    print("JUPITER STATIC AUDIT FAILED")
    for item in ERRORS:
        print(item)
    raise SystemExit(1)

print("JUPITER STATIC AUDIT = OK")
