from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.v1 import evidence_reports as api
from app.contracts.enums import IdempotencyStatus
from app.contracts.evidence_report import EvidenceReportStatus, REPORT_COMPOSER_VERSION, REPORT_SCHEMA_VERSION
from app.db.base import Base
from app.db.evidence_report_models import PreliminaryEvidenceReport
from app.db.models import AuditLog, Case, IdempotencyRecord
from app.reports.report_grounding import ReportGroundingError
from app.schemas.evidence_reports import EvidenceReportRebuildRequest


def _db() -> Session:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def _previous_report(case_id: str) -> PreliminaryEvidenceReport:
    return PreliminaryEvidenceReport(
        case_id=case_id,
        session_id=None,
        call_id=None,
        scope_type="CASE",
        scope_id=case_id,
        version=1,
        status=EvidenceReportStatus.COMPLETE.value,
        schema_version=REPORT_SCHEMA_VERSION,
        composer_version="evidence-brief-composer-v2",
        input_snapshot_hash="a" * 64,
        idempotency_key="report-v1-key",
        analyzer_versions_json={},
        completeness_json={"state": "COMPLETE", "reviewability": "FULLY_REVIEWABLE"},
        snapshot_json={"status": "COMPLETE", "report_version": 1},
        created_by="test",
    )


def _validation() -> dict:
    return {
        "schema_version": "report-grounding-v1",
        "status": "FAIL",
        "reviewability_status": "NOT_REVIEWABLE",
        "counts": {"blockers": 1, "warnings": 0, "issues": 1},
        "issues": [{
            "rule_id": "RG-005",
            "layer": "SEMANTIC",
            "severity": "BLOCKER",
            "code": "HIGH_DELTA_LOSS_SEMANTIC_CONTRADICTION",
            "message": "HIGH_DELTA with continuous Sequence cannot be Packet Loss",
        }],
    }


def test_grounding_blocked_rebuild_persists_failed_attempt_and_restores_previous_success(monkeypatch):
    with _db() as db:
        case = Case(case_no="CASE-RG-API-1", summary="grounding failure persistence", status="ANALYZING")
        db.add(case)
        db.flush()
        previous = _previous_report(case.id)
        db.add(previous)
        db.commit()
        previous_id = previous.id

        def fake_generate(db_arg, *, scope_type, scope_id, actor, force):
            previous_row = api._latest(db_arg, scope_type, scope_id)
            assert previous_row is not None
            previous_row.status = EvidenceReportStatus.SUPERSEDED.value
            failed = PreliminaryEvidenceReport(
                case_id=case.id,
                session_id=None,
                call_id=None,
                scope_type=scope_type,
                scope_id=scope_id,
                version=2,
                status=EvidenceReportStatus.COMPOSING.value,
                schema_version=REPORT_SCHEMA_VERSION,
                composer_version=REPORT_COMPOSER_VERSION,
                input_snapshot_hash="b" * 64,
                idempotency_key="report-v2-key",
                analyzer_versions_json={"packet_intelligence": "0.5.0"},
                completeness_json={"state": "COMPLETE", "reviewability": "FULLY_REVIEWABLE"},
                snapshot_json=None,
                supersedes_report_id=previous_row.id,
                created_by=actor,
            )
            db_arg.add(failed)
            db_arg.flush()
            raise ReportGroundingError(_validation())

        monkeypatch.setattr(api, "generate_evidence_report", fake_generate)

        with pytest.raises(HTTPException) as caught:
            api._rebuild(
                "CASE",
                case.id,
                EvidenceReportRebuildRequest(force=True),
                db,
                "rg-api-idempotency-1",
                SimpleNamespace(actor_id="actor-rg"),
            )

        assert caught.value.status_code == 422
        assert str(caught.value.detail).startswith("REPORT_GROUNDING_FAILED:")

        reports = list(db.scalars(
            select(PreliminaryEvidenceReport)
            .where(PreliminaryEvidenceReport.scope_type == "CASE", PreliminaryEvidenceReport.scope_id == case.id)
            .order_by(PreliminaryEvidenceReport.version.asc())
        ))
        assert len(reports) == 2
        old, failed = reports
        assert old.id == previous_id
        assert old.status == EvidenceReportStatus.COMPLETE.value
        assert failed.version == 2
        assert failed.status == EvidenceReportStatus.FAILED.value
        assert failed.error_code == "ReportGroundingError"
        assert "REPORT_GROUNDING_FAILED" in (failed.error_message or "")
        assert failed.completeness_json["state"] == "PARTIAL"
        assert failed.completeness_json["reviewability"] == "NOT_REVIEWABLE"
        assert failed.completeness_json["grounding_status"] == "FAIL"
        assert failed.snapshot_json["status"] == "FAILED"
        assert failed.snapshot_json["grounding_validation"]["issues"][0]["code"] == "HIGH_DELTA_LOSS_SEMANTIC_CONTRADICTION"

        idem = db.scalar(select(IdempotencyRecord).where(
            IdempotencyRecord.scope == f"POST:/api/v1/case/{case.id}/reports/evidence/rebuild",
            IdempotencyRecord.idempotency_key == "rg-api-idempotency-1",
        ))
        assert idem is not None
        assert idem.status == IdempotencyStatus.FAILED.value

        audit_row = db.scalar(select(AuditLog).where(
            AuditLog.event_type == "PRELIMINARY_EVIDENCE_REPORT_GROUNDING_BLOCKED",
            AuditLog.target_id == failed.id,
        ))
        assert audit_row is not None
        assert audit_row.actor == "actor-rg"
        assert audit_row.detail["grounding_status"] == "FAIL"
        assert audit_row.detail["counts"]["blockers"] == 1
        assert audit_row.detail["issues"][0]["rule_id"] == "RG-005"


def test_grounding_blocked_rebuild_without_previous_report_still_persists_failed_version(monkeypatch):
    with _db() as db:
        case = Case(case_no="CASE-RG-API-2", summary="first report grounding failure", status="ANALYZING")
        db.add(case)
        db.commit()

        def fake_generate(db_arg, *, scope_type, scope_id, actor, force):
            failed = PreliminaryEvidenceReport(
                case_id=case.id,
                scope_type=scope_type,
                scope_id=scope_id,
                version=1,
                status=EvidenceReportStatus.COMPOSING.value,
                schema_version=REPORT_SCHEMA_VERSION,
                composer_version=REPORT_COMPOSER_VERSION,
                input_snapshot_hash="c" * 64,
                idempotency_key="report-v1-blocked",
                analyzer_versions_json={},
                completeness_json={"state": "COMPLETE", "reviewability": "FULLY_REVIEWABLE"},
                created_by=actor,
            )
            db_arg.add(failed)
            db_arg.flush()
            raise ReportGroundingError(_validation())

        monkeypatch.setattr(api, "generate_evidence_report", fake_generate)

        with pytest.raises(HTTPException) as caught:
            api._rebuild(
                "CASE", case.id, EvidenceReportRebuildRequest(force=True), db,
                "rg-api-idempotency-2", SimpleNamespace(actor_id="actor-rg"),
            )
        assert caught.value.status_code == 422
        failed = api._latest(db, "CASE", case.id)
        assert failed is not None
        assert failed.version == 1
        assert failed.status == EvidenceReportStatus.FAILED.value
        assert failed.snapshot_json["reviewability_status"] == "NOT_REVIEWABLE"
