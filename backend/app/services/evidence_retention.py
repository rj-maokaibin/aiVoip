from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.evidence_retention_models import EvidenceRetentionState
from app.db.golden_models import GoldenCandidateAssessment
from app.db.models import Evidence
from app.integrations.storage import ObjectStorage, reproduction_object_storage
from app.services.audit import audit


GOLDEN_RETENTION_STATUSES = {"GOLDEN_CANDIDATE", "GOLDEN_READY"}
RAW_LONG_LIVED_TYPES = {"PCAP", "PCM", "PCM_RX", "PCM_TX", "AUDIO", "DEBUG", "LOG"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _is_raw(evidence: Evidence) -> bool:
    return str(evidence.kind or "RAW").upper() == "RAW"


def _golden_exempt(db: Session, case_id: str) -> bool:
    row = db.scalar(select(GoldenCandidateAssessment).where(GoldenCandidateAssessment.case_id == case_id).limit(1))
    return bool(row and str(row.status).upper() in GOLDEN_RETENTION_STATUSES)


def ensure_retention_state(db: Session, evidence: Evidence) -> EvidenceRetentionState:
    row = db.scalar(select(EvidenceRetentionState).where(EvidenceRetentionState.evidence_id == evidence.id).limit(1))
    golden = _golden_exempt(db, evidence.case_id)
    if row is None:
        policy = "LONG_TERM_GOLDEN" if golden else ("STANDARD_90D" if _is_raw(evidence) else "LONG_TERM_DERIVED")
        retain_until = None if policy != "STANDARD_90D" else evidence.created_at + timedelta(days=settings.evidence_retention_raw_days)
        row = EvidenceRetentionState(
            evidence_id=evidence.id,
            case_id=evidence.case_id,
            policy=policy,
            retain_until=retain_until,
            golden_exempt=golden,
            status="ACTIVE",
            metadata_json={"raw_days": settings.evidence_retention_raw_days},
        )
        db.add(row)
        db.flush()
        return row
    if golden and not row.golden_exempt:
        row.golden_exempt = True
        if row.status == "ACTIVE" and row.locked_at is None:
            row.policy = "LONG_TERM_GOLDEN"
            row.retain_until = None
            audit(db, case_id=evidence.case_id, actor="retention-policy", event_type="EVIDENCE_RETENTION_GOLDEN_EXEMPTED",
                  target_type="evidence", target_id=evidence.id,
                  detail={"policy":"LONG_TERM_GOLDEN","reason":"CASE_PROMOTED_TO_GOLDEN"})
            db.flush()
    return row


def initialize_case_retention(db: Session, case_id: str) -> list[EvidenceRetentionState]:
    rows = []
    for evidence in db.scalars(select(Evidence).where(Evidence.case_id == case_id).order_by(Evidence.created_at.asc())):
        rows.append(ensure_retention_state(db, evidence))
    return rows


def lock_evidence(db: Session, *, evidence_id: str, actor: str, reason: str) -> EvidenceRetentionState:
    evidence = db.get(Evidence, evidence_id)
    if not evidence:
        raise ValueError("EVIDENCE_NOT_FOUND")
    row = ensure_retention_state(db, evidence)
    if row.status == "EXPIRED":
        raise ValueError("EVIDENCE_ALREADY_EXPIRED")
    row.policy = "MANUAL_LOCK"
    row.retain_until = None
    row.locked_by = actor
    row.lock_reason = reason
    row.locked_at = utcnow()
    row.status = "LOCKED"
    audit(db, case_id=evidence.case_id, actor=actor, event_type="EVIDENCE_RETENTION_LOCKED",
          target_type="evidence", target_id=evidence.id, detail={"reason": reason})
    db.flush()
    return row


def unlock_evidence(db: Session, *, evidence_id: str, actor: str, reason: str | None = None) -> EvidenceRetentionState:
    evidence = db.get(Evidence, evidence_id)
    if not evidence:
        raise ValueError("EVIDENCE_NOT_FOUND")
    row = ensure_retention_state(db, evidence)
    if row.golden_exempt:
        raise ValueError("GOLDEN_EVIDENCE_CANNOT_BE_UNLOCKED_FROM_RETENTION")
    if row.status == "EXPIRED":
        raise ValueError("EVIDENCE_ALREADY_EXPIRED")
    row.policy = "STANDARD_90D" if _is_raw(evidence) else "LONG_TERM_DERIVED"
    row.retain_until = evidence.created_at + timedelta(days=settings.evidence_retention_raw_days) if _is_raw(evidence) else None
    row.locked_by = None
    row.lock_reason = None
    row.locked_at = None
    row.status = "ACTIVE"
    audit(db, case_id=evidence.case_id, actor=actor, event_type="EVIDENCE_RETENTION_UNLOCKED",
          target_type="evidence", target_id=evidence.id, detail={"reason": reason})
    db.flush()
    return row


def _remove_payload(evidence: Evidence) -> tuple[bool, list[str]]:
    removed = False
    errors: list[str] = []
    if evidence.session_id:
        try:
            reproduction_object_storage().remove(evidence.object_key)
            removed = True
        except Exception as exc:
            errors.append(f"reproduction:{type(exc).__name__}:{exc}")
    try:
        ObjectStorage().remove(evidence.object_key)
        removed = True
    except Exception as exc:
        errors.append(f"permanent:{type(exc).__name__}:{exc}")
    return removed, errors


def _refresh_golden_exemptions(db: Session, *, limit: int) -> int:
    """Refresh tracked raw evidence before deletion.

    Golden eligibility can be reached long after Evidence creation. Retention
    therefore re-checks active 90-day rows on every sweep before selecting due
    payloads. This prevents a later-promoted Golden Case from being accidentally
    expired by a stale policy snapshot.
    """
    candidates = list(db.scalars(
        select(EvidenceRetentionState).where(
            EvidenceRetentionState.status == "ACTIVE",
            EvidenceRetentionState.policy == "STANDARD_90D",
            EvidenceRetentionState.golden_exempt.is_(False),
        ).order_by(EvidenceRetentionState.retain_until.asc().nullslast()).limit(limit)
    ))
    changed = 0
    for row in candidates:
        evidence = db.get(Evidence, row.evidence_id)
        if not evidence:
            continue
        before = bool(row.golden_exempt)
        ensure_retention_state(db, evidence)
        if not before and row.golden_exempt:
            changed += 1
    db.flush()
    return changed


def expire_due_evidence(db: Session, *, now: datetime | None = None, actor: str = "retention-worker",
                        limit: int | None = None, storage_delete: bool = True) -> dict:
    now = now or utcnow()
    limit = limit or settings.evidence_retention_batch_size

    # Materialize policy rows for legacy Evidence first. This is bounded so the
    # scheduled task can safely converge on large databases over multiple runs.
    untracked = list(db.scalars(
        select(Evidence).where(~Evidence.id.in_(select(EvidenceRetentionState.evidence_id)))
        .order_by(Evidence.created_at.asc()).limit(limit)
    ))
    for evidence in untracked:
        ensure_retention_state(db, evidence)

    golden_refreshed = _refresh_golden_exemptions(db, limit=limit)

    due = list(db.scalars(
        select(EvidenceRetentionState).where(
            EvidenceRetentionState.status == "ACTIVE",
            EvidenceRetentionState.policy == "STANDARD_90D",
            EvidenceRetentionState.retain_until.is_not(None),
            EvidenceRetentionState.retain_until <= now,
            EvidenceRetentionState.locked_at.is_(None),
            EvidenceRetentionState.golden_exempt.is_(False),
        ).order_by(EvidenceRetentionState.retain_until.asc()).limit(limit)
    ))

    expired = failed = 0
    details = []
    for row in due:
        evidence = db.get(Evidence, row.evidence_id)
        if evidence is None:
            row.status = "ERROR"
            row.last_error = "EVIDENCE_ROW_NOT_FOUND"
            failed += 1
            continue
        removed, errors = (True, []) if not storage_delete else _remove_payload(evidence)
        if not removed and storage_delete:
            row.status = "ERROR"
            row.last_error = ";".join(errors)[-2000:]
            failed += 1
            details.append({"evidence_id": evidence.id, "status": "ERROR", "errors": errors})
            continue
        row.status = "EXPIRED"
        row.expired_at = now
        row.object_deleted_at = now if storage_delete else None
        row.last_error = None
        meta = dict(evidence.metadata_json or {})
        meta.update({"retention_status": "EXPIRED", "retention_expired_at": now.isoformat(), "payload_available": False})
        evidence.metadata_json = meta
        evidence.completeness = "UNAVAILABLE"
        audit(db, case_id=evidence.case_id, actor=actor, event_type="EVIDENCE_RETENTION_EXPIRED",
              target_type="evidence", target_id=evidence.id,
              detail={"policy": row.policy, "retain_until": row.retain_until.isoformat() if row.retain_until else None,
                      "sha256": evidence.sha256, "size_bytes": evidence.size_bytes})
        expired += 1
        details.append({"evidence_id": evidence.id, "status": "EXPIRED"})
    db.flush()
    return {"initialized": len(untracked), "golden_refreshed": golden_refreshed, "due": len(due), "expired": expired, "failed": failed, "details": details}


def retention_status(db: Session, evidence_id: str) -> dict:
    evidence = db.get(Evidence, evidence_id)
    if not evidence:
        raise ValueError("EVIDENCE_NOT_FOUND")
    row = ensure_retention_state(db, evidence)
    return {
        "evidence_id": evidence.id,
        "case_id": evidence.case_id,
        "policy": row.policy,
        "status": row.status,
        "retain_until": row.retain_until.isoformat() if row.retain_until else None,
        "golden_exempt": bool(row.golden_exempt),
        "locked_by": row.locked_by,
        "lock_reason": row.lock_reason,
        "locked_at": row.locked_at.isoformat() if row.locked_at else None,
        "expired_at": row.expired_at.isoformat() if row.expired_at else None,
        "payload_available": row.status != "EXPIRED",
    }
