from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_fr027_report_generation_and_rebuild_version_are_audited():
    service = _read("backend/app/services/evidence_report.py")
    api = _read("backend/app/api/v1/evidence_reports.py")

    assert 'event_type="PRELIMINARY_EVIDENCE_REPORT_GENERATED"' in service
    assert '"version":version' in service
    assert '"forced":force' in service
    assert 'PRELIMINARY_EVIDENCE_REPORT_GROUNDING_BLOCKED' in api
    assert 'Idempotency-Key' in api


def test_fr027_bundle_generation_and_actual_download_are_audited():
    artifacts = _read("backend/app/services/evidence_report_artifacts.py")
    api = _read("backend/app/api/v1/evidence_reports.py")

    assert 'EVIDENCE_BUNDLE_GENERATED' in artifacts
    assert 'EVIDENCE_BUNDLE_DOWNLOAD_URL_ISSUED' in api
    assert 'EVIDENCE_BUNDLE_DOWNLOADED' in api
    assert '/bundle/download' in api
    assert 'require_evidence_permission(EvidencePermission.DOWNLOAD_EVIDENCE_BUNDLE)' in api


def test_fr026_retention_state_changes_are_audited():
    retention = _read("backend/app/services/evidence_retention.py")

    for event in (
        "EVIDENCE_RETENTION_GOLDEN_EXEMPTED",
        "EVIDENCE_RETENTION_LOCKED",
        "EVIDENCE_RETENTION_UNLOCKED",
        "EVIDENCE_RETENTION_EXPIRED",
    ):
        assert event in retention


def test_audit_rows_keep_actor_target_before_after_reason_trace_and_detail_contract():
    audit = _read("backend/app/services/audit.py")

    for token in (
        "actor_type",
        "event_type",
        "target_type",
        "target_id",
        "before_json",
        "after_json",
        "reason",
        "trace_id",
        "detail",
    ):
        assert token in audit
