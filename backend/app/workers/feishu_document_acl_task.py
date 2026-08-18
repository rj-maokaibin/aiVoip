from __future__ import annotations

import asyncio

from celery.utils.log import get_task_logger

from app.core.config import settings
from app.db.evidence_report_models import FeishuEvidenceDocumentBinding
from app.db.session import SessionLocal
from app.integrations.feishu.document_acl import FeishuDocumentAclService
from app.workers.celery_app import celery_app

log = get_task_logger(__name__)


@celery_app.task(name="feishu.sync_document_acl", bind=True, max_retries=3, default_retry_delay=5)
def sync_document_acl(self, case_id: str, document_id: str | None = None):
    if not settings.feishu_document_acl_enabled:
        return {"status": "SKIPPED", "reason": "FEISHU_DOCUMENT_ACL_DISABLED", "case_id": case_id}
    db = SessionLocal()
    try:
        if not document_id:
            binding = db.query(FeishuEvidenceDocumentBinding).filter(
                FeishuEvidenceDocumentBinding.case_id == case_id
            ).first()
            document_id = binding.document_id if binding else None
        if not document_id:
            return {"status": "NOT_FOUND", "reason": "FEISHU_DOCUMENT_NOT_BOUND", "case_id": case_id}
        row = asyncio.run(FeishuDocumentAclService().reconcile(
            db, case_id=case_id, document_id=document_id,
        ))
        db.commit()
        return {
            "status": row.status,
            "case_id": case_id,
            "document_id": document_id,
            "sync_mode": row.sync_mode,
            "effective_mode": row.effective_mode,
            "desired_revision": row.desired_revision,
            "applied_revision": row.applied_revision,
        }
    except Exception as exc:
        db.rollback()
        log.exception("Feishu document ACL sync failed case=%s document=%s", case_id, document_id)
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=min(40, 5 * (2 ** self.request.retries)))
        return {"status": "FAILED", "case_id": case_id, "document_id": document_id,
                "error": f"{type(exc).__name__}:{exc}"}
    finally:
        db.close()
