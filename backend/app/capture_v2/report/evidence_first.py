from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from app.capture_v2.db_models import EvidenceAsset
from app.capture_v2.errors import CaptureV2Error
from app.core.ids import new_id


@dataclass(frozen=True)
class FindingEvidenceRequest:
    finding_id: str
    title: str
    conclusion: str
    confidence: str
    required_asset_types: tuple[str, ...]
    evidence_asset_ids: tuple[str, ...]
    why: tuple[str, ...] = ()


class EvidenceAssetRepository:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    def create(self, *, capture_session_id: str, asset_type: str, title: str,
               description: str | None = None, storage_key: str | None = None,
               source_refs: list[str] | None = None,
               capture_attempt_id: str | None = None, call_ref: str | None = None,
               start_ts: datetime | None = None, end_ts: datetime | None = None,
               metadata: dict | None = None, idempotency_key: str | None = None) -> str:
        if idempotency_key:
            with self.session_factory() as db:
                existing = db.scalar(select(EvidenceAsset).where(
                    EvidenceAsset.idempotency_key == idempotency_key
                ))
                if existing is not None:
                    same = (
                        existing.capture_session_id == capture_session_id
                        and existing.capture_attempt_id == capture_attempt_id
                        and existing.call_ref == call_ref
                        and existing.asset_type == asset_type
                        and existing.title == title
                        and existing.description == description
                        and existing.storage_key == storage_key
                        and list(existing.source_refs or []) == list(source_refs or [])
                        and existing.start_ts == start_ts
                        and existing.end_ts == end_ts
                        and dict(existing.metadata_json or {}) == dict(metadata or {})
                    )
                    if not same:
                        raise CaptureV2Error(
                            "EVIDENCE_ASSET_IDEMPOTENCY_CONFLICT",
                            details={"idempotency_key": idempotency_key, "evidence_asset_id": existing.id},
                        )
                    return existing.id
        with self.session_factory() as db:
            with db.begin():
                row = EvidenceAsset(
                    id=new_id(), idempotency_key=idempotency_key,
                    capture_session_id=capture_session_id,
                    capture_attempt_id=capture_attempt_id, call_ref=call_ref,
                    asset_type=asset_type, title=title, description=description,
                    storage_key=storage_key, source_refs=source_refs or [],
                    start_ts=start_ts, end_ts=end_ts, metadata_json=metadata or {},
                )
                db.add(row)
                db.flush()
                return row.id


class EvidenceFirstReportBuilder:
    """Build a report manifest; unsupported findings stay visibly unsupported."""

    def __init__(self, session_factory):
        self.session_factory = session_factory

    def build(self, *, capture_session_id: str, quality: dict,
              findings: list[FindingEvidenceRequest]) -> dict:
        with self.session_factory() as db:
            assets = list(db.query(EvidenceAsset).filter(
                EvidenceAsset.capture_session_id == capture_session_id
            ).all())
        by_id = {a.id: a for a in assets}
        output_findings = []
        for finding in findings:
            selected = [by_id[i] for i in finding.evidence_asset_ids if i in by_id]
            present_types = {a.asset_type for a in selected}
            missing = [kind for kind in finding.required_asset_types if kind not in present_types]
            supported = not missing and bool(selected)
            output_findings.append({
                "finding_id": finding.finding_id,
                "title": finding.title,
                "conclusion": finding.conclusion if supported else "EVIDENCE_INSUFFICIENT_FOR_CONCLUSION",
                "requested_conclusion": finding.conclusion,
                "confidence": finding.confidence if supported else "INSUFFICIENT",
                "supported": supported,
                "missing_evidence_types": missing,
                "why": list(finding.why),
                "evidence": [
                    {
                        "asset_id": a.id,
                        "type": a.asset_type,
                        "title": a.title,
                        "description": a.description,
                        "storage_key": a.storage_key,
                        "source_refs": list(a.source_refs or []),
                        "start_ts": a.start_ts.isoformat() if a.start_ts else None,
                        "end_ts": a.end_ts.isoformat() if a.end_ts else None,
                    }
                    for a in selected
                ],
            })
        return {
            "schema_version": "evidence-report-v2.1",
            "capture_session_id": capture_session_id,
            "quality": quality,
            "findings": output_findings,
            "evidence_asset_count": len(assets),
        }
