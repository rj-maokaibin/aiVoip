#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size <= 0:
        raise SystemExit(f"required evidence missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Fail-closed validator for reusable Full Acceptance evidence")
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--current-manifest", type=Path)
    args = parser.parse_args()

    root = args.root.resolve()
    revision = args.revision.strip()
    if len(revision) != 40 or any(c not in "0123456789abcdef" for c in revision):
        raise SystemExit(f"invalid revision: {revision!r}")

    evidence = _load(root / "full-acceptance-result.json")
    golden = _load(root / "offline-golden-result.json")
    human = _load(root / "offline-golden-human-artifacts" / "human-golden-result.json")
    frontend = _load(root / "validation" / "frontend_acceptance_result.json")
    prior_manifest = _load(root / "validation" / "source_manifest_expected.json")

    required_human = [
        root / "offline-golden-human-artifacts" / "pcm_rx_periodic_spectrum_human_v2.png",
        root / "offline-golden-human-artifacts" / "pcm_rx_periodic_spectrogram_human_v2.png",
        root / "offline-golden-human-artifacts" / "pcm_rx_dtmf_first_digit_inspector_human_v2.png",
    ]
    for path in required_human:
        if not path.is_file() or path.stat().st_size <= 0:
            raise SystemExit(f"required human evidence missing: {path}")

    assert evidence.get("schema_version") == "full-software-acceptance-v2", evidence
    assert evidence.get("revision") == revision, evidence
    assert evidence.get("passed") is True, evidence
    assert evidence.get("frozen_contracts", {}).get("passed") is True, evidence
    assert evidence.get("full_release_gate", {}).get("passed") is True, evidence
    assert evidence.get("golden_001") == {
        "passed": True,
        "checks_passed": 142,
        "checks_total": 142,
    }, evidence
    assert evidence.get("human_evidence", {}).get("passed") is True, evidence
    assert evidence.get("performance_v3", {}).get("status") == "PASS", evidence

    assert golden.get("passed") is True, golden
    assert golden.get("checks_passed") == 142, golden
    assert golden.get("checks_total") == 142, golden
    assert human.get("status") == "PASS", human

    assert frontend.get("revision") == revision, frontend
    assert frontend.get("passed") is True, frontend
    assert frontend.get("npm_audit_passed") is True, frontend
    assert frontend.get("production_build_passed") is True, frontend
    assert frontend.get("execution_source") == "GITHUB_HOSTED", frontend
    assert evidence.get("frontend_acceptance", {}).get("package_lock_sha256") == frontend.get("package_lock_sha256"), evidence

    prior_aggregate = prior_manifest.get("aggregate_sha256")
    assert isinstance(prior_aggregate, str) and len(prior_aggregate) == 64, prior_manifest
    if args.current_manifest is not None:
        current_manifest = _load(args.current_manifest.resolve())
        assert current_manifest.get("aggregate_sha256") == prior_aggregate, {
            "prior": prior_manifest,
            "current": current_manifest,
        }
        assert current_manifest.get("file_count") == prior_manifest.get("file_count"), {
            "prior": prior_manifest,
            "current": current_manifest,
        }

    result = {
        "status": "PASS",
        "contract": "full-acceptance-reusable-evidence-v1",
        "revision": revision,
        "source_manifest_aggregate_sha256": prior_aggregate,
        "golden_checks": "142/142",
        "human_artifacts": [
            {"name": p.name, "sha256": _sha256(p), "bytes": p.stat().st_size}
            for p in required_human
        ],
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
