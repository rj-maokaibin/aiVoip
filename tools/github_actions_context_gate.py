#!/usr/bin/env python3
"""Fail fast on GitHub Actions contexts that are invalid before runner dispatch.

A workflow using ``${{ runner.* }}`` in ``jobs.<job_id>.env`` fails during
GitHub's workflow evaluation phase.  That produces the confusing signature
``conclusion=failure`` with zero jobs, because no runner ever receives a job.

This stdlib-only gate intentionally checks the narrow contract that caused the
2026-08-27 acceptance failures.  Step-level env/run may use ``runner`` and is
not rejected.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def invalid_job_env_runner_context(path: Path) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    in_jobs = False
    current_job: str | None = None
    in_job_env = False

    for lineno, raw in enumerate(lines, 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))

        if indent == 0:
            in_jobs = stripped == "jobs:"
            current_job = None
            in_job_env = False
            continue
        if not in_jobs:
            continue

        if indent == 2 and stripped.endswith(":"):
            current_job = stripped[:-1].strip()
            in_job_env = False
            continue
        if current_job is None:
            continue

        if indent == 4:
            in_job_env = stripped == "env:"
            continue
        if indent <= 4:
            in_job_env = False

        if in_job_env and indent >= 6 and "${{ runner." in raw:
            findings.append(
                {
                    "path": str(path),
                    "line": lineno,
                    "job": current_job,
                    "code": "RUNNER_CONTEXT_NOT_AVAILABLE_IN_JOB_ENV",
                    "text": stripped,
                }
            )
    return findings


def scan(root: Path) -> dict[str, object]:
    workflow_dir = root / ".github" / "workflows"
    files = sorted([*workflow_dir.glob("*.yml"), *workflow_dir.glob("*.yaml")])
    findings: list[dict[str, object]] = []
    for path in files:
        findings.extend(invalid_job_env_runner_context(path))
    return {
        "schema_version": "github-actions-context-gate-v1",
        "status": "PASS" if not findings else "FAIL",
        "workflow_count": len(files),
        "finding_count": len(findings),
        "findings": findings,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    args = parser.parse_args()
    result = scan(Path(args.root).resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
