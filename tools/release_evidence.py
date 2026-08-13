#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SOURCE_MANIFEST = ROOT / "release" / "source_manifest.json"


def source_manifest_payload(root: Path = ROOT) -> dict[str, Any]:
    path = root / "release" / "source_manifest.json"
    if not path.exists():
        raise FileNotFoundError(f"source manifest missing: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    digest = str(payload.get("aggregate_sha256") or "").strip()
    if len(digest) != 64:
        raise ValueError("source manifest aggregate_sha256 is invalid")
    return payload


def source_manifest_sha256(root: Path = ROOT) -> str:
    return str(source_manifest_payload(root)["aggregate_sha256"])


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def evidence_envelope(*, evidence_type: str, payload: dict[str, Any] | None = None, root: Path = ROOT) -> dict[str, Any]:
    out: dict[str, Any] = {
        "schema_version": 1,
        "evidence_type": evidence_type,
        "generated_at": utc_now_iso(),
        "source_manifest_aggregate_sha256": source_manifest_sha256(root),
    }
    if payload:
        out.update(payload)
    return out


def load_source_bound_evidence(path: Path, *, root: Path = ROOT) -> tuple[dict[str, Any] | None, str]:
    """Load evidence only when it belongs to the exact current source manifest.

    Returns (payload, reason).  Stale evidence is deliberately treated as unavailable,
    never silently accepted as release proof for a different source tree.
    """
    if not path.exists():
        return None, f"evidence artifact missing: {path}"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, f"evidence artifact is invalid JSON: {type(exc).__name__}"
    try:
        current = source_manifest_sha256(root)
    except Exception as exc:
        return None, f"current source manifest is unavailable: {type(exc).__name__}"
    actual = str(payload.get("source_manifest_aggregate_sha256") or "").strip()
    if not actual:
        return None, "evidence artifact is not source-bound (missing source_manifest_aggregate_sha256)"
    if actual != current:
        return None, f"stale evidence: source hash {actual} != current {current}"
    return payload, "exact-source evidence"
