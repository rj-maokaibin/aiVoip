from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from app.db.session import SessionLocal
from app.integrations.feishu.evidence_document import FeishuEvidenceDocumentService
from app.reports.v2.migration import rollout_from_env
from app.services.evidence_report import generate_evidence_report
from human_evidence_feishu_live_acceptance import (
    REAL_GOLDEN_001_SHA256,
    _block_text,
    _list_document_blocks,
    _refresh_stale_golden_dtmf,
    _select_bound_golden,
)

STAGES = {"SHADOW", "CANARY", "DEFAULT"}
V2_SCHEMA = "preliminary-evidence-report-v2"
SHARED_RESULT_PATH = Path("/validation/evidence_v2_production_acceptance.json")


def _validate_v2(v2: dict) -> None:
    if v2.get("schema") != V2_SCHEMA:
        raise RuntimeError("EVIDENCE_V2_SCHEMA_MISMATCH")
    semantic = v2.get("semantic_validation") or {}
    if semantic.get("status") != "PASS":
        raise RuntimeError("EVIDENCE_V2_SEMANTIC_NOT_PASS")
    p0 = [
        item for item in (semantic.get("violations") or [])
        if str((item or {}).get("severity") or "").upper() == "P0"
    ]
    if p0:
        raise RuntimeError("EVIDENCE_V2_P0_SEMANTIC_DIVERGENCE")


def _persist_result(result_path: Path, payload: dict) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(text, encoding="utf-8")

    # Production mounts /validation read-write from the host. Persist the exact
    # PASS/FAIL payload there as well so a fail-closed docker exec cannot hide
    # the first causal error before deploy/voip-ai reaches its docker cp step.
    # Local/unit-test environments without the production mount are unchanged.
    if SHARED_RESULT_PATH != result_path and SHARED_RESULT_PATH.parent.is_dir():
        SHARED_RESULT_PATH.write_text(text, encoding="utf-8")


async def run(*, stage: str, expected_revision: str) -> dict:
    stage = stage.upper()
    if stage not in STAGES:
        raise RuntimeError(f"INVALID_EVIDENCE_V2_ROLLOUT_STAGE:{stage}")
    observed_revision = str(os.getenv("BUILD_REVISION") or "").strip()
    if observed_revision != expected_revision:
        raise RuntimeError(
            f"EVIDENCE_V2_ACCEPTANCE_REVISION_MISMATCH:{observed_revision}:{expected_revision}"
        )

    rollout = rollout_from_env()
    expected_mode = "SHADOW" if stage == "SHADOW" else "V2"
    if rollout.mode != expected_mode or rollout.strict_validator is not True:
        raise RuntimeError(
            f"EVIDENCE_V2_RUNTIME_MODE_MISMATCH:stage={stage}:mode={rollout.mode}:strict={rollout.strict_validator}"
        )

    db = SessionLocal()
    try:
        binding, previous_report = _select_bound_golden(db)
        original_document_id = str(binding.document_id)
        original_projection_version = int(binding.projection_version or 0)
        refresh = _refresh_stale_golden_dtmf(db, case_id=str(binding.case_id))
        report, payload, reused = generate_evidence_report(
            db,
            scope_type=previous_report.scope_type,
            scope_id=previous_report.scope_id,
            actor=f"evidence-v2-production-{stage.lower()}",
            force=True,
        )
        rollout_meta = payload.get("evidence_v2_rollout") or {}
        if rollout_meta.get("mode") != expected_mode:
            raise RuntimeError("EVIDENCE_V2_PAYLOAD_ROLLOUT_MODE_MISMATCH")

        if stage == "SHADOW":
            if payload.get("active_projection") != "V1":
                raise RuntimeError("EVIDENCE_V2_SHADOW_CHANGED_ACTIVE_PROJECTION")
            v2 = payload.get("v2_shadow")
            if not isinstance(v2, dict):
                raise RuntimeError("EVIDENCE_V2_SHADOW_CANONICAL_MISSING")
            _validate_v2(v2)
            db.commit()
            return {
                "status": "PASS",
                "contract": "evidence-v2-production-rollout-acceptance-v1",
                "stage": stage,
                "source_revision": observed_revision,
                "rollout_mode": rollout.mode,
                "strict_validator": rollout.strict_validator,
                "golden_identity": "BOUND_REAL_GOLDEN_001",
                "golden_sha256": REAL_GOLDEN_001_SHA256,
                "report_id": report.id,
                "report_version": report.version,
                "report_reused": reused,
                "active_projection": payload.get("active_projection"),
                "v2_semantic_status": (v2.get("semantic_validation") or {}).get("status"),
                "v2_publishable": v2.get("publishable"),
                "feishu_projection_attempted": False,
                "canonical_readback": "NOT_REQUIRED_IN_SHADOW",
                "analyzer_refresh_performed": refresh.get("performed"),
                "analyzer_refresh_components": refresh.get("components"),
            }

        if payload.get("active_projection") != "V2":
            raise RuntimeError("EVIDENCE_V2_ACTIVE_PROJECTION_NOT_V2")
        v2 = payload.get("v2_canonical")
        if not isinstance(v2, dict):
            raise RuntimeError("EVIDENCE_V2_CANONICAL_MISSING")
        _validate_v2(v2)

        service = FeishuEvidenceDocumentService()
        projected = await service.project(db, case_id=report.case_id, report_id=report.id)
        if str(projected.document_id) != original_document_id:
            raise RuntimeError("EVIDENCE_V2_FEISHU_DOCUMENT_ID_CHANGED")
        if str(projected.projected_report_id) != str(report.id):
            raise RuntimeError("EVIDENCE_V2_FEISHU_REPORT_BINDING_MISMATCH")
        if int(projected.projection_version or 0) <= original_projection_version:
            raise RuntimeError("EVIDENCE_V2_FEISHU_PROJECTION_VERSION_NOT_ADVANCED")
        db.commit()

        blocks = await _list_document_blocks(service, original_document_id)
        remote_text = "\n".join(_block_text(block) for block in blocks)
        required = [
            "Evidence Report V2",
            f"Report {report.id}",
            "Semantic Validator：PASS",
            "Active Projection：V2",
            "飞书仅投影 Canonical V2",
        ]
        missing = [marker for marker in required if marker not in remote_text]
        if missing:
            raise RuntimeError("EVIDENCE_V2_FEISHU_CANONICAL_READBACK_MISSING:" + ",".join(missing))

        return {
            "status": "PASS",
            "contract": "evidence-v2-production-rollout-acceptance-v1",
            "stage": stage,
            "source_revision": observed_revision,
            "rollout_mode": rollout.mode,
            "strict_validator": rollout.strict_validator,
            "golden_identity": "BOUND_REAL_GOLDEN_001",
            "golden_sha256": REAL_GOLDEN_001_SHA256,
            "report_id": report.id,
            "report_version": report.version,
            "report_reused": reused,
            "active_projection": payload.get("active_projection"),
            "v2_semantic_status": (v2.get("semantic_validation") or {}).get("status"),
            "v2_publishable": v2.get("publishable"),
            "feishu_projection_attempted": True,
            "document_reused": True,
            "projection_version": projected.projection_version,
            "remote_block_count": len(blocks),
            "canonical_readback": "PASS",
            "canonical_readback_markers": required,
            "analyzer_refresh_performed": refresh.get("performed"),
            "analyzer_refresh_components": refresh.get("components"),
        }
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", required=True, choices=sorted(STAGES))
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--result", required=True, type=Path)
    args = parser.parse_args()
    try:
        payload = asyncio.run(run(stage=args.stage, expected_revision=args.expected_revision))
    except Exception as exc:
        payload = {
            "status": "FAIL",
            "contract": "evidence-v2-production-rollout-acceptance-v1",
            "stage": args.stage,
            "source_revision": str(os.getenv("BUILD_REVISION") or ""),
            "error_code": type(exc).__name__,
            "error_message": str(exc)[:500],
        }
    _persist_result(args.result, payload)
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload.get("status") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
