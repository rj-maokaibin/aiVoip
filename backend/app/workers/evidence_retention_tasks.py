from __future__ import annotations

from celery.utils.log import get_task_logger

from app.core.config import settings
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
        db.commit()
        return {"status": "SUCCESS", **result}
    except Exception as exc:
        db.rollback()
        log.exception("Evidence retention sweep failed")
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc)
        return {"status": "FAILED", "error": f"{type(exc).__name__}:{exc}"}
    finally:
        db.close()
