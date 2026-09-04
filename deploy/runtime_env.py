#!/usr/bin/env python3
"""Materialize a private runtime env from immutable source-controlled deployment state.

The persistent production env contains site configuration and secrets/bootstrap
values. BUILD_REVISION and the Evidence V2 global projection are deployment
state, so they are injected into a temporary copy instead of mutating
/etc/voip-ai/production.env.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
from pathlib import Path

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
BUILD_REVISION_RE = re.compile(r"^\s*(?:export\s+)?BUILD_REVISION\s*=")
EVIDENCE_V2_PROJECT_RE = re.compile(r"^\s*(?:export\s+)?PRELIMINARY_EVIDENCE_V2_PROJECT\s*=")
ROLLOUT_SCHEMA = "evidence-v2-production-rollout-v1"
VALID_STAGES = {"SHADOW", "CANARY", "DEFAULT"}
EXPECTED_PROJECTION = {
    "SHADOW": "V1",
    "CANARY": "V1",
    "DEFAULT": "V2",
}
DEFAULT_ROLLOUT_PATH = Path(__file__).resolve().with_name("evidence_v2_rollout.json")


def _private_mode(path: Path) -> int:
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        raise RuntimeError(f"base env is not accessible: {path}: {exc.__class__.__name__}") from exc


def _rollout_projection(rollout_path: Path) -> tuple[str, bool]:
    if not rollout_path.is_file():
        raise RuntimeError(f"Evidence V2 rollout contract missing: {rollout_path}")
    try:
        payload = json.loads(rollout_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Evidence V2 rollout contract is unreadable/invalid: {rollout_path}: {exc.__class__.__name__}"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Evidence V2 rollout contract must be a JSON object")

    schema = payload.get("schema_version")
    if schema != ROLLOUT_SCHEMA:
        raise RuntimeError(f"Evidence V2 rollout schema mismatch: {schema!r}")

    stage = str(payload.get("stage") or "").upper()
    if stage not in VALID_STAGES:
        raise RuntimeError(f"Evidence V2 rollout stage invalid: {stage!r}")
    if payload.get("strict_validator") is not True:
        raise RuntimeError("Evidence V2 rollout requires strict_validator=true")

    projection = str(payload.get("default_projection") or "").upper()
    expected_projection = EXPECTED_PROJECTION[stage]
    if projection != expected_projection:
        raise RuntimeError(
            "Evidence V2 rollout projection mismatch: "
            f"stage={stage} expected={expected_projection} actual={projection or 'missing'}"
        )

    return stage, stage == "DEFAULT"


def materialize(
    base_env: Path,
    output: Path,
    revision: str,
    *,
    require_private: bool = True,
    rollout_path: Path = DEFAULT_ROLLOUT_PATH,
) -> int:
    if not SHA_RE.fullmatch(revision):
        raise RuntimeError(f"revision must be an immutable 40-char lowercase git SHA: {revision!r}")
    if not base_env.is_file():
        raise RuntimeError(f"production env file missing: {base_env}")

    mode = _private_mode(base_env)
    if require_private and mode & 0o077:
        raise RuntimeError(
            f"base production env must not be group/world accessible: {base_env} mode={mode:04o}"
        )

    rollout_stage, evidence_v2_project = _rollout_projection(rollout_path)

    raw_lines = base_env.read_text(encoding="utf-8").splitlines()
    kept: list[str] = []
    removed = 0
    project_entries_ignored = 0
    for line in raw_lines:
        if BUILD_REVISION_RE.match(line):
            removed += 1
            continue
        if EVIDENCE_V2_PROJECT_RE.match(line):
            project_entries_ignored += 1
            continue
        kept.append(line)

    output.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(kept).rstrip("\n")
    if body:
        body += "\n"
    body += f"BUILD_REVISION={revision}\n"
    body += f"PRELIMINARY_EVIDENCE_V2_PROJECT={'true' if evidence_v2_project else 'false'}\n"
    output.write_text(body, encoding="utf-8")
    os.chmod(output, stat.S_IRUSR | stat.S_IWUSR)

    out_mode = stat.S_IMODE(output.stat().st_mode)
    if out_mode != 0o600:
        raise RuntimeError(f"runtime env mode must be 0600: {output} mode={out_mode:04o}")

    print(
        "EVIDENCE_V2_RUNTIME_PROJECTION=PASS "
        f"stage={rollout_stage} global_project={'true' if evidence_v2_project else 'false'} "
        f"persistent_project_entries_ignored={project_entries_ignored}"
    )
    return removed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-env", required=True, type=Path)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--rollout", type=Path, default=DEFAULT_ROLLOUT_PATH)
    parser.add_argument("--allow-insecure-base", action="store_true")
    args = parser.parse_args()

    try:
        removed = materialize(
            args.base_env.resolve(),
            args.out.resolve(),
            args.revision,
            require_private=not args.allow_insecure_base,
            rollout_path=args.rollout.resolve(),
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
