"""Compatibility module: python -m app.capture_v2.gate_cli ..."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from app.capture_v2.gate.cli import main as gate_main


_R6_PRODUCT_GATE_ID = "R6-PRODUCT-REPORT-MATERIALIZE-RC56"
_R6_GOLDEN_RELATIVE = Path("validation/capture_v2/R6_APF1250_FIRST_8000_ABNORMAL_GOLDEN_RC33.json")
_MASTER_BASELINE_GATE_ID = "MASTER-BASELINE-INTEGRATION-RC59"
_MASTER_FIX_CANDIDATE_GATE_RE = re.compile(r"^MASTER-FIX-CANDIDATE-INTEGRATION-RC\d+$")
_MASTER_FIX_CANDIDATE_SHA = "391486c7a70f8e36c088dcb512397044a552c78c"
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
    """Dispatch one exact-SHA isolated master merge simulation."""
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


def _bounded_master_fix_candidate_regression(argv: list[str]) -> int | None:
    """Dispatch only the currently audited Draft PR #37 candidate.

    Retries may use a new RC suffix, but the candidate SHA remains exact and
    fail-closed. The underlying runner also fetches the fixed PR #37 branch and
    requires FETCH_HEAD to equal this SHA before creating the detached worktree.
    """
    if not argv or argv[0] != "evaluate":
        return None
    gate_id = str(_arg_value(argv, "--gate-id") or "").strip()
    if not _MASTER_FIX_CANDIDATE_GATE_RE.fullmatch(gate_id):
        return None
    candidate_sha = str(_arg_value(argv, "--bundle") or "").strip().lower()
    if candidate_sha != _MASTER_FIX_CANDIDATE_SHA:
        return None

    # master_fix_candidate_regression has an additional fixed-SHA assertion.
    # Keep it synchronized here rather than widening that module to arbitrary
    # candidate SHAs; this process-local assignment is reachable only after the
    # exact gate-id shape and exact audited SHA checks above have passed.
    from app.capture_v2.control import master_fix_candidate_regression as regression

    regression.CANDIDATE_SHA = _MASTER_FIX_CANDIDATE_SHA
    repo_root = Path(__file__).resolve().parents[3]
    return regression.main([
        "--repo-root", str(repo_root),
        "--candidate-sha", candidate_sha,
    ])


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    bounded = _bounded_r6_materialization(args)
    if bounded is not None:
        return bounded
    bounded = _bounded_master_baseline_regression(args)
    if bounded is not None:
        return bounded
    bounded = _bounded_master_fix_candidate_regression(args)
    if bounded is not None:
        return bounded
    return gate_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
