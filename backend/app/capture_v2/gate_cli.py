"""Compatibility module: python -m app.capture_v2.gate_cli ..."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from app.capture_v2.gate.cli import main as gate_main


_R6_PRODUCT_GATE_ID = "R6-PRODUCT-REPORT-MATERIALIZE-RC56"
_R6_GOLDEN_RELATIVE = Path("validation/capture_v2/R6_APF1250_FIRST_8000_ABNORMAL_GOLDEN_RC33.json")
_MASTER_BASELINE_GATE_ID = "MASTER-BASELINE-INTEGRATION-RC59"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _arg_value(argv: list[str], name: str) -> str | None:
    try:
        index = argv.index(name)
    except ValueError:
        return None
    return argv[index + 1] if index + 1 < len(argv) else None


def _bounded_r6_materialization(argv: list[str]) -> int | None:
    """Dispatch exactly one audited non-DUT R6 product-binding invocation."""
    if not argv or argv[0] != "evaluate":
        return None
    if _arg_value(argv, "--gate-id") != _R6_PRODUCT_GATE_ID:
        return None
    bundle = _arg_value(argv, "--bundle")
    if not bundle:
        return None
    repo_root = Path(__file__).resolve().parents[3]
    expected = (repo_root / _R6_GOLDEN_RELATIVE).resolve()
    supplied = Path(bundle)
    if not supplied.is_absolute():
        supplied = (Path.cwd() / supplied).resolve()
    else:
        supplied = supplied.resolve()
    if supplied != expected:
        return None

    from app.capture_v2.control.r6_report_materialize_guarded import main as materialize_main

    return materialize_main([
        "--repo-root", str(repo_root),
        "--golden-path", str(expected),
    ])


def _bounded_master_baseline_regression(argv: list[str]) -> int | None:
    """Dispatch one exact-SHA isolated master merge simulation.

    ``GATE_EVALUATE`` remains the only remote executable surface. For this one Gate
    ID, ``--bundle`` is interpreted only as an immutable 40-hex master commit SHA.
    The delegated runner creates a detached temporary worktree, performs a no-commit
    merge of the feature head into that exact master commit, runs acceptance, and
    removes the worktree. It never updates the PR branch or creates a merge commit.
    """
    if not argv or argv[0] != "evaluate":
        return None
    if _arg_value(argv, "--gate-id") != _MASTER_BASELINE_GATE_ID:
        return None
    master_sha = str(_arg_value(argv, "--bundle") or "").strip().lower()
    if not _SHA_RE.fullmatch(master_sha):
        return None

    from app.capture_v2.control.master_baseline_regression import main as regression_main

    repo_root = Path(__file__).resolve().parents[3]
    return regression_main([
        "--repo-root", str(repo_root),
        "--master-sha", master_sha,
    ])


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    bounded = _bounded_r6_materialization(args)
    if bounded is not None:
        return bounded
    bounded = _bounded_master_baseline_regression(args)
    if bounded is not None:
        return bounded
    return gate_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
