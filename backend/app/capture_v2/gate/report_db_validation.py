from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import delete, func, select

from app.capture_v2.db_models import CaptureSession, EvidenceAsset
from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.report.evidence_first import (
    EvidenceAssetRepository,
    EvidenceFirstReportBuilder,
    FindingEvidenceRequest,
)
from app.db.session import SessionLocal


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def validate_real_postgres_evidence_first_report(*, device_id: str, marker: str) -> dict:
    """Exercise evidence-first report semantics against the configured PostgreSQL DB.

    The latest real CaptureSession is only an FK/scope anchor. The validator uses
    clearly tagged validation-only EvidenceAsset rows, verifies fail-closed report
    behavior, then deletes every row it created. It cannot substitute for the real
    abnormal first-digit-loss Product E2E Golden.
    """
    with SessionLocal() as db:
        dialect = str(db.get_bind().dialect.name)
        capture = db.scalar(
            select(CaptureSession)
            .where(CaptureSession.device_id == device_id)
            .order_by(CaptureSession.created_at.desc())
            .limit(1)
        )
        capture_session_id = str(capture.id) if capture is not None else None

    effect = "VALIDATES_EVIDENCE_FIRST_DB_RUNTIME_ONLY_NOT_ABNORMAL_E2E_PASS"
    if dialect != "postgresql":
        return {"verdict": "INCONCLUSIVE", "reason": "REAL_POSTGRESQL_REQUIRED", "dialect": dialect,
                "release_gate_effect": effect}
    if not capture_session_id:
        return {"verdict": "INCONCLUSIVE", "reason": "CAPTURE_SESSION_FK_ANCHOR_NOT_FOUND", "dialect": dialect,
                "release_gate_effect": effect}

    token = uuid4().hex[:12]
    repo = EvidenceAssetRepository(SessionLocal)
    builder = EvidenceFirstReportBuilder(SessionLocal)
    created_ids: list[str] = []
    checks: dict[str, bool] = {}
    snapshot: dict = {}
    now = _utcnow()
    common_meta = {
        "validation_only": True,
        "validation_kind": "R6_REAL_POSTGRES_EVIDENCE_FIRST_SELF_TEST",
        "marker": str(marker)[:80],
        "token": token,
    }

    try:
        pcap_key = f"r6-report-selftest:{token}:pcap"
        pcap_id = repo.create(
            capture_session_id=capture_session_id,
            asset_type="PCAP",
            title="validation PCAP evidence",
            description="validation-only evidence-first DB self-test",
            storage_key=f"validation-only/{token}/pcap",
            source_refs=[f"validation:{token}:pcap"],
            start_ts=now,
            end_ts=now + timedelta(seconds=1),
            metadata=common_meta,
            idempotency_key=pcap_key,
        )
        created_ids.append(pcap_id)
        same_pcap_id = repo.create(
            capture_session_id=capture_session_id,
            asset_type="PCAP",
            title="validation PCAP evidence",
            description="validation-only evidence-first DB self-test",
            storage_key=f"validation-only/{token}/pcap",
            source_refs=[f"validation:{token}:pcap"],
            start_ts=now,
            end_ts=now + timedelta(seconds=1),
            metadata=common_meta,
            idempotency_key=pcap_key,
        )
        checks["evidence_asset_idempotent_create"] = same_pcap_id == pcap_id

        conflict_code = None
        try:
            repo.create(
                capture_session_id=capture_session_id,
                asset_type="PCAP",
                title="changed title must conflict",
                description="validation-only evidence-first DB self-test",
                storage_key=f"validation-only/{token}/pcap",
                source_refs=[f"validation:{token}:pcap"],
                start_ts=now,
                end_ts=now + timedelta(seconds=1),
                metadata=common_meta,
                idempotency_key=pcap_key,
            )
        except CaptureV2Error as exc:
            conflict_code = exc.code
        checks["evidence_asset_idempotency_conflict_fails_closed"] = (
            conflict_code == "EVIDENCE_ASSET_IDEMPOTENCY_CONFLICT"
        )

        insufficient_req = FindingEvidenceRequest(
            finding_id=f"validation-insufficient-{token}",
            title="first digit loss root cause",
            conclusion="DSP_DTMF_EVENT_LOST",
            confidence="HIGH",
            required_asset_types=("PCAP", "FXS"),
            evidence_asset_ids=(pcap_id,),
            why=("validation-only: deliberately omit required FXS evidence",),
        )
        insufficient = builder.build(
            capture_session_id=capture_session_id,
            quality={"validation_only": True, "capture_completeness": "PARTIAL"},
            findings=[insufficient_req],
        )
        row = insufficient["findings"][0]
        checks["missing_required_evidence_is_unsupported"] = row["supported"] is False
        checks["missing_required_evidence_forces_insufficient_conclusion"] = (
            row["conclusion"] == "EVIDENCE_INSUFFICIENT_FOR_CONCLUSION"
        )
        checks["missing_required_evidence_forces_insufficient_confidence"] = row["confidence"] == "INSUFFICIENT"
        checks["missing_fxs_is_explicit"] = "FXS" in row["missing_evidence_types"]
        checks["requested_root_cause_not_leaked_as_conclusion"] = row["conclusion"] != row["requested_conclusion"]

        fxs_id = repo.create(
            capture_session_id=capture_session_id,
            asset_type="FXS",
            title="validation FXS evidence",
            description="validation-only evidence-first DB self-test",
            storage_key=f"validation-only/{token}/fxs",
            source_refs=[f"validation:{token}:fxs"],
            start_ts=now,
            end_ts=now + timedelta(seconds=1),
            metadata=common_meta,
            idempotency_key=f"r6-report-selftest:{token}:fxs",
        )
        created_ids.append(fxs_id)
        supported_req = FindingEvidenceRequest(
            finding_id=f"validation-supported-{token}",
            title="bounded supported finding",
            conclusion="TEST_SUPPORTED_CONCLUSION",
            confidence="HIGH",
            required_asset_types=("PCAP", "FXS"),
            evidence_asset_ids=(pcap_id, fxs_id),
            why=("validation-only: both required evidence types are selected",),
        )
        supported = builder.build(
            capture_session_id=capture_session_id,
            quality={"validation_only": True, "capture_completeness": "COMPLETE"},
            findings=[supported_req],
        )
        srow = supported["findings"][0]
        checks["complete_required_evidence_is_supported"] = srow["supported"] is True
        checks["supported_conclusion_preserved"] = srow["conclusion"] == "TEST_SUPPORTED_CONCLUSION"
        checks["supported_confidence_preserved"] = srow["confidence"] == "HIGH"
        checks["supported_missing_evidence_empty"] = srow["missing_evidence_types"] == []
        checks["supported_evidence_count_exact"] = len(srow["evidence"]) == 2

        with SessionLocal() as db:
            persisted = list(db.scalars(select(EvidenceAsset).where(EvidenceAsset.id.in_(created_ids))))
        checks["validation_assets_persisted_in_real_db"] = len(persisted) == 2
        snapshot = {
            "dialect": dialect,
            "capture_session_fk_anchor": capture_session_id,
            "created_asset_ids": list(created_ids),
            "insufficient_finding": row,
            "supported_finding": srow,
            "persisted_asset_count": len(persisted),
        }
    finally:
        if created_ids:
            with SessionLocal() as db:
                with db.begin():
                    db.execute(delete(EvidenceAsset).where(EvidenceAsset.id.in_(created_ids)))
            with SessionLocal() as db:
                left = int(db.scalar(
                    select(func.count(EvidenceAsset.id)).where(EvidenceAsset.id.in_(created_ids))
                ) or 0)
            checks["validation_assets_cleanup_verified"] = left == 0

    passed = bool(checks) and all(checks.values())
    return {
        "verdict": "PASS" if passed else "FAIL",
        "reason": "REAL_POSTGRES_EVIDENCE_FIRST_SELF_TEST_COMPLETED" if passed else "REAL_POSTGRES_EVIDENCE_FIRST_SELF_TEST_FAILED",
        "release_gate_effect": effect,
        "checks": checks,
        "snapshot_before_cleanup": snapshot,
        "cleanup_verified": checks.get("validation_assets_cleanup_verified") is True,
    }
