from __future__ import annotations

from celery.utils.log import get_task_logger
from sqlalchemy import select

from app.core.config import settings
from app.db.evidence_report_models import FeishuEvidenceDocumentBinding, PreliminaryEvidenceReport
from app.db.session import SessionLocal
from app.integrations.feishu.evidence_document import FeishuEvidenceDocumentService
from app.services.audit import audit
from app.workers.celery_app import celery_app

log=get_task_logger(__name__)


@celery_app.task(name="feishu.project_evidence_report",bind=True,max_retries=3,default_retry_delay=3)
def project_case_evidence_document(self,case_id:str,report_id:str):
    if not settings.feishu_live_enabled:
        return {"status":"SKIPPED","reason":"FEISHU_LIVE_DISABLED","case_id":case_id,"report_id":report_id}
    db=SessionLocal()
    try:
        report=db.get(PreliminaryEvidenceReport,report_id)
        if not report or report.case_id!=case_id:
            return {"status":"NOT_FOUND","case_id":case_id,"report_id":report_id}
        binding=FeishuEvidenceDocumentService().project(db,case_id=case_id,report_id=report_id)
        db.commit()
        return {"status":"SYNCED","case_id":case_id,"report_id":report_id,"document_id":binding.document_id,"document_url":binding.document_url,"projection_version":binding.projection_version}
    except Exception as exc:
        db.rollback(); log.exception("Feishu evidence report projection failed case=%s report=%s",case_id,report_id)
        try:
            binding=db.scalar(select(FeishuEvidenceDocumentBinding).where(FeishuEvidenceDocumentBinding.case_id==case_id).limit(1))
            if binding:
                binding.status="FAILED"; binding.last_error=f"{type(exc).__name__}:{exc}"
            audit(db,case_id=case_id,actor="feishu-evidence-document",event_type="FEISHU_EVIDENCE_DOCUMENT_FAILED",
                  target_type="preliminary_evidence_report",target_id=report_id,detail={"error_code":type(exc).__name__,"error_message":str(exc)[:1000],"retry":self.request.retries})
            db.commit()
        except Exception:
            db.rollback()
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc,countdown=min(12,3*(2**self.request.retries)))
        return {"status":"FAILED","case_id":case_id,"report_id":report_id,"error":f"{type(exc).__name__}:{exc}"}
    finally:
        db.close()
