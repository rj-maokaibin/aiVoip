#!/usr/bin/env python3
"""Materialize a private runtime env with an immutable BUILD_REVISION overlay.

The persistent production env contains site configuration and secrets/bootstrap
values. BUILD_REVISION is deployment state, so it is injected into a temporary
copy instead of mutating /etc/voip-ai/production.env.
"""

from __future__ import annotations

import argparse
import os
import re
import stat
from pathlib import Path

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
BUILD_REVISION_RE = re.compile(r"^\s*(?:export\s+)?BUILD_REVISION\s*=")


def _private_mode(path: Path) -> int:
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        raise RuntimeError(f"base env is not accessible: {path}: {exc.__class__.__name__}") from exc


def materialize(base_env: Path, output: Path, revision: str, *, require_private: bool = True) -> int:
    if not SHA_RE.fullmatch(revision):
        raise RuntimeError(f"revision must be an immutable 40-char lowercase git SHA: {revision!r}")
    if not base_env.is_file():
        raise RuntimeError(f"production env file missing: {base_env}")

    mode = _private_mode(base_env)
    if require_private and mode & 0o077:
        raise RuntimeError(
            f"base production env must not be group/world accessible: {base_env} mode={mode:04o}"
        )

    raw_lines = base_env.read_text(encoding="utf-8").splitlines()
    kept: list[str] = []
    removed = 0
    for line in raw_lines:
        if BUILD_REVISION_RE.match(line):
            removed += 1
            continue
        kept.append(line)

    output.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(kept).rstrip("\n")
    if body:
        body += "\n"
    body += f"BUILD_REVISION={revision}\n"
    output.write_text(body, encoding="utf-8")
    os.chmod(output, stat.S_IRUSR | stat.S_IWUSR)

    out_mode = stat.S_IMODE(output.stat().st_mode)
    if out_mode != 0o600:
        raise RuntimeError(f"runtime env mode must be 0600: {output} mode={out_mode:04o}")
    return removed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-env", required=True, type=Path)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--allow-insecure-base", action="store_true")
    args = parser.parse_args()

    try:
        removed = materialize(
            args.base_env.resolve(),
            args.out.resolve(),
            args.revision,
            require_private=not args.allow_insecure_base,
        )
    except RuntimeError as exc:
        print(f"RUNTIME_ENV=BLOCKED reason={exc}", file=__import__("sys").stderr)
        return 2

    print(
        "RUNTIME_ENV=PASS "
        f"revision={args.revision} persistent_build_revision_entries_ignored={removed} "
        "persistent_env_mutated=false mode=0600"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
