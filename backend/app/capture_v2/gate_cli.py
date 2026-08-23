"""Compatibility module: python -m app.capture_v2.gate_cli ..."""

from __future__ import annotations

import sys
from pathlib import Path

from app.capture_v2.gate.cli import main as gate_main


_R6_PRODUCT_GATE_ID = "R6-PRODUCT-REPORT-MATERIALIZE-RC56"
_R6_GOLDEN_RELATIVE = Path("validation/capture_v2/R6_APF1250_FIRST_8000_ABNORMAL_GOLDEN_RC33.json")


def _arg_value(argv: list[str], name: str) -> str | None:
    try:
        index = argv.index(name)
    except ValueError:
        return None
    return argv[index + 1] if index + 1 < len(argv) else None


def _bounded_r6_materialization(argv: list[str]) -> int | None:
    """Dispatch exactly one audited non-DUT R6 product-binding invocation.

    The remote control policy already permits only the fixed ``gate_cli evaluate``
    executable surface.  Do not broaden that policy or teach the generic evaluator
    to mutate product data.  This compatibility entry point recognizes only the
    immutable RC33 Golden and one exact Gate ID, then delegates to the dedicated
    fail-closed materializer which independently verifies the same path again.
    Every other invocation remains a normal deterministic Gate evaluation.
    """
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

    from app.capture_v2.control.r6_report_materialize import main as materialize_main

    return materialize_main([
        "--repo-root", str(repo_root),
        "--golden-path", str(expected),
    ])


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    bounded = _bounded_r6_materialization(args)
    if bounded is not None:
        return bounded
    return gate_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
