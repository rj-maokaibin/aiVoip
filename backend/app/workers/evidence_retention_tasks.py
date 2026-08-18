from __future__ import annotations

from celery.utils.log import get_task_logger

from app.core.config import settings
from app.db.models import Evidence
from app.db.session import SessionLocal
from app.services.evidence_retention import expire_due_evidence
from app.workers.celery_app import celery_app

log = get_task_logger(__name__)


@celery_app.task(name="evidence.retention_sweep", bind=True, max_retries=2, default_retry_delay=30)
def evidence_retention_sweep(self):
    if not settings.evidence_retention_worker_enabled:
        return {"status": "SKIPPED", "reason": "EVIDENCE_RETENTION_WORKER_DISABLED"}
    db = SessionLocal()
    try:
        result = expire_due_evidence(db, actor="retention-worker")
        affected_cases = set()
        for detail in result.get("details", []):
            if detail.get("status") != "EXPIRED":
                continue
            evidence = db.get(Evidence, detail.get("evidence_id"))
            if evidence:
                affected_cases.add(evidence.case_id)
        db.commit()
        # Expiry changes Evidence Completeness and therefore the report input hash.
        # Rebuild asynchronously so a new immutable report version explicitly marks
        # raw payload expiry while historical versions remain unchanged.
        if affected_cases:
            try:
                from app.workers.evidence_report_tasks import notify_evidence_report_changed
                for case_id in sorted(affected_cases):
                    notify_evidence_report_changed(case_id, reason="evidence_retention_expired")
            except Exception:
                log.exception("failed to enqueue report refresh after retention expiry")
        return {"status": "SUCCESS", "affected_cases": sorted(affected_cases), **result}
    except Exception as exc:
        db.rollback()
        log.exception("Evidence retention sweep failed")
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc)
        return {"status": "FAILED", "error": f"{type(exc).__name__}:{exc}"}
    finally:
        db.close()
