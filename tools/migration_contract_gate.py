#!/usr/bin/env python3
from __future__ import annotations

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSIONS = ROOT / "backend" / "migrations" / "versions"


def literal_assignment(tree: ast.Module, name: str):
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id == name for t in targets):
                value = node.value
                try:
                    return ast.literal_eval(value)
                except Exception:
                    return None
    return None


def main() -> int:
    revisions: dict[str, dict] = {}
    errors: list[str] = []
    for path in sorted(VERSIONS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rev = literal_assignment(tree, "revision")
        down = literal_assignment(tree, "down_revision")
        funcs = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        if not rev:
            errors.append(f"{path.name}: missing literal revision")
            continue
        if rev in revisions:
            errors.append(f"duplicate revision {rev}: {revisions[rev]['file']} and {path.name}")
        if "upgrade" not in funcs or "downgrade" not in funcs:
            errors.append(f"{path.name}: upgrade/downgrade functions are required")
        revisions[str(rev)] = {"file": path.name, "down_revision": down}

    parents: set[str] = set()
    for rev, row in revisions.items():
        down = row["down_revision"]
        if down is None:
            continue
        deps = list(down) if isinstance(down, (tuple, list)) else [down]
        for dep in deps:
            if str(dep) not in revisions:
                errors.append(f"{rev}: unknown down_revision {dep}")
            parents.add(str(dep))

    heads = sorted(set(revisions) - parents)
    roots = sorted([rev for rev, row in revisions.items() if row["down_revision"] is None])
    if len(heads) != 1:
        errors.append(f"expected exactly one migration head, got {heads}")
    if len(roots) != 1:
        errors.append(f"expected exactly one migration root, got {roots}")

    # Cycle/reachability check from head backwards.
    if len(heads) == 1:
        seen: set[str] = set()
        stack = [heads[0]]
        while stack:
            rev = stack.pop()
            if rev in seen:
                errors.append(f"migration cycle detected at {rev}")
                break
            seen.add(rev)
            down = revisions[rev]["down_revision"]
            if down is None:
                continue
            stack.extend(str(x) for x in (down if isinstance(down, (tuple, list)) else [down]))
        if len(seen) != len(revisions):
            errors.append(f"migration graph is disconnected: reachable={len(seen)} total={len(revisions)}")

    payload = {
        "status": "PASS" if not errors else "FAIL",
        "migration_count": len(revisions),
        "roots": roots,
        "heads": heads,
        "head": heads[0] if len(heads) == 1 else None,
        "errors": errors,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
