from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.evidence_permissions import EvidencePermission, require_evidence_permission
from app.services.evidence_report_metrics import evidence_report_pipeline_metrics

router = APIRouter(tags=["evidence-report-metrics"])


@router.get("/system/evidence-report/metrics")
def report_metrics(
    window_days: int | None = Query(default=None, ge=1, le=365),
    db: Session = Depends(get_db),
    _identity=Depends(require_evidence_permission(EvidencePermission.VIEW_PIPELINE_METRICS)),
):
    return evidence_report_pipeline_metrics(db, window_days=window_days)
