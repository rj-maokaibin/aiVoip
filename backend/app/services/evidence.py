from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy.orm import Session

from app.contracts.enums import (
    EvidenceCompleteness,
    EvidenceKind,
    EvidenceLevel,
    EvidenceRelationType,
    EvidenceScope,
)
from app.core.errors import AppError
from app.db.models import Evidence, EvidenceRelation
from app.services.audit import audit


def utcnow():
    return datetime.now(timezone.utc)


def create_evidence(
    db: Session,
    *,
    evidence_id: str | None = None,
    case_id: str,
    evidence_type: str,
    source: str,
    filename: str,
    object_key: str,
    size_bytes: int,
    sha256: str,
    kind: EvidenceKind | str,
    scope: EvidenceScope | str,
    level: EvidenceLevel | str,
    completeness: EvidenceCompleteness | str,
    device_id: str | None = None,
    job_id: str | None = None,
    action_run_id: str | None = None,
    content_type: str | None = None,
    captured_at: datetime | None = None,
    time_range_start: datetime | None = None,
    time_range_end: datetime | None = None,
    producer_type: str | None = None,
    producer_id: str | None = None,
    producer_version: str | None = None,
    session_id: str | None = None,
    attempt_id: str | None = None,
    call_id: str | None = None,
    metadata: dict | None = None,
    parent_evidence_ids: Iterable[str] = (),
    relation_type: EvidenceRelationType | str = EvidenceRelationType.DERIVED_FROM,
    actor: str | None = None,
) -> Evidence:
    """Create append-only Evidence and its lineage in one DB unit of work.

    Derived evidence is required to name at least one parent. Raw evidence must not
    claim DERIVED_FROM lineage. Object immutability is enforced by never exposing an
    update path for object_key/sha256 and by using a new Evidence row for re-analysis.
    """
    kind = EvidenceKind(kind)
    scope = EvidenceScope(scope)
    level = EvidenceLevel(level)
    completeness = EvidenceCompleteness(completeness)
    relation_type = EvidenceRelationType(relation_type)
    parents = tuple(dict.fromkeys(parent_evidence_ids))
    if kind == EvidenceKind.DERIVED and not parents:
        raise AppError("EVIDENCE_LINEAGE_REQUIRED")
    for parent_id in parents:
        parent = db.get(Evidence, parent_id)
        if parent is None or parent.case_id != case_id:
            raise AppError("EVIDENCE_PARENT_INVALID", details={"parent_evidence_id": parent_id})
    row_kwargs = dict(
        case_id=case_id,
        device_id=device_id,
        job_id=job_id,
        action_run_id=action_run_id,
        type=evidence_type,
        source=source,
        kind=kind.value,
        source_scope=scope.value,
        level=level.value,
        completeness=completeness.value,
        filename=filename,
        object_key=object_key,
        size_bytes=size_bytes,
        sha256=sha256,
        content_type=content_type,
        captured_at=captured_at or utcnow(),
        time_range_start=time_range_start,
        time_range_end=time_range_end,
        producer_type=producer_type,
        producer_id=producer_id,
        producer_version=producer_version,
        session_id=session_id,
        attempt_id=attempt_id,
        call_id=call_id,
        metadata_json=metadata or {},
    )
    if evidence_id is not None:
        row_kwargs["id"] = evidence_id
    row = Evidence(**row_kwargs)
    db.add(row)
    db.flush()
    for parent_id in parents:
        db.add(EvidenceRelation(
            parent_evidence_id=parent_id,
            child_evidence_id=row.id,
            relation_type=relation_type.value,
        ))
    audit(
        db,
        case_id=case_id,
        actor=actor,
        event_type="EVIDENCE_CREATED",
        target_type="evidence",
        target_id=row.id,
        detail={
            "type": evidence_type,
            "kind": kind.value,
            "scope": scope.value,
            "level": level.value,
            "completeness": completeness.value,
            "parent_evidence_ids": list(parents),
        },
    )
    db.flush()
    return row
