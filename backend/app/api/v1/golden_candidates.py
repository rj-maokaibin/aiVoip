from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import ENGINEER_ROLES, READ_ROLES, get_db, require_roles
from app.db.golden_models import GoldenCandidateAssessment
from app.db.models import Case
from app.golden.service import GoldenCandidateService, STATUSES

router = APIRouter(tags=["golden-candidates"])


def _payload(row: GoldenCandidateAssessment) -> dict:
    return GoldenCandidateService.as_dict(row)


@router.get('/cases/{case_id}/golden-candidate')
def get_case_golden_candidate(
    case_id: str,
    refresh: bool = Query(default=True),
    db: Session = Depends(get_db),
    _identity = Depends(require_roles(*READ_ROLES)),
):
    if not db.get(Case, case_id):
        raise HTTPException(404, 'CASE_NOT_FOUND')
    service = GoldenCandidateService()
    row = db.scalar(select(GoldenCandidateAssessment).where(GoldenCandidateAssessment.case_id == case_id))
    if refresh or row is None:
        row = service.refresh(db, case_id, actor='golden-candidate-api')
        db.commit(); db.refresh(row)
    return _payload(row)


@router.post('/cases/{case_id}/golden-candidate/refresh')
def refresh_case_golden_candidate(
    case_id: str,
    db: Session = Depends(get_db),
    identity = Depends(require_roles(*ENGINEER_ROLES)),
):
    if not db.get(Case, case_id):
        raise HTTPException(404, 'CASE_NOT_FOUND')
    row = GoldenCandidateService().refresh(db, case_id, actor=identity.actor_id)
    db.commit(); db.refresh(row)
    return _payload(row)


@router.get('/golden-candidates')
def list_golden_candidates(
    status: str | None = Query(default=None),
    verification_tier: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    _identity = Depends(require_roles(*READ_ROLES)),
):
    stmt = select(GoldenCandidateAssessment).order_by(
        GoldenCandidateAssessment.updated_at.desc(), GoldenCandidateAssessment.case_id.asc()
    )
    if status:
        normalized = status.upper()
        if normalized not in STATUSES:
            raise HTTPException(422, 'GOLDEN_STATUS_INVALID')
        stmt = stmt.where(GoldenCandidateAssessment.status == normalized)
    if verification_tier:
        tier = verification_tier.upper()
        if tier not in {'A', 'B'}:
            raise HTTPException(422, 'GOLDEN_VERIFICATION_TIER_INVALID')
        stmt = stmt.where(GoldenCandidateAssessment.verification_tier == tier)
    rows = list(db.scalars(stmt.limit(limit)))
    return {'items': [_payload(x) for x in rows], 'count': len(rows)}


@router.get('/golden-candidates/summary')
def golden_candidates_summary(
    db: Session = Depends(get_db),
    _identity = Depends(require_roles(*READ_ROLES)),
):
    rows = db.execute(
        select(GoldenCandidateAssessment.status, func.count(GoldenCandidateAssessment.id))
        .group_by(GoldenCandidateAssessment.status)
    ).all()
    by_status = {status: 0 for status in STATUSES}
    for status, count in rows:
        by_status[str(status)] = int(count)
    tier_rows = db.execute(
        select(GoldenCandidateAssessment.verification_tier, func.count(GoldenCandidateAssessment.id))
        .where(GoldenCandidateAssessment.verification_tier.is_not(None))
        .group_by(GoldenCandidateAssessment.verification_tier)
    ).all()
    return {
        'schema_version': 'golden-candidate-summary-v1',
        'total': sum(by_status.values()),
        'by_status': by_status,
        'by_verification_tier': {str(tier): int(count) for tier, count in tier_rows},
        'eval_ready_count': by_status['GOLDEN_READY'],
    }


@router.post('/golden-candidates/backfill')
def backfill_golden_candidates(
    limit: int = Query(default=500, ge=1, le=5000),
    db: Session = Depends(get_db),
    identity = Depends(require_roles(*ENGINEER_ROLES)),
):
    case_ids = list(db.scalars(select(Case.id).order_by(Case.updated_at.desc()).limit(limit)))
    service = GoldenCandidateService()
    counts = {status: 0 for status in STATUSES}
    failures: list[dict] = []
    for case_id in case_ids:
        try:
            row = service.refresh(db, case_id, actor=identity.actor_id)
            counts[row.status] = counts.get(row.status, 0) + 1
        except Exception as exc:
            failures.append({'case_id': case_id, 'error': f'{type(exc).__name__}:{exc}'})
    db.commit()
    return {
        'schema_version': 'golden-candidate-backfill-v1',
        'processed': len(case_ids),
        'by_status': counts,
        'failure_count': len(failures),
        'failures': failures[:100],
    }
