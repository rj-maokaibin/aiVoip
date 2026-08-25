from __future__ import annotations

import asyncio

from celery.utils.log import get_task_logger
from sqlalchemy import select

from app.core.config import settings
from app.db.evidence_report_models import FeishuEvidenceDocumentBinding, PreliminaryEvidenceReport
from app.db.session import SessionLocal
from app.integrations.feishu.evidence_document_human_v2 import HumanFeishuEvidenceDocumentService
from app.integrations.feishu.service import FeishuCaseCardService
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
        binding=asyncio.run(HumanFeishuEvidenceDocumentService().project(db,case_id=case_id,report_id=report_id))
        card_status="NOT_BOUND"
        try:
            asyncio.run(FeishuCaseCardService().sync_case_card(db,case_id=case_id))
            card_status="SYNCED"
        except ValueError as exc:
            if str(exc)=="FEISHU_RECEIVE_ID_NOT_CONFIGURED":
                card_status="NOT_BOUND"
            else:
                card_status="FAILED"
                log.exception("Feishu evidence summary card sync failed case=%s",case_id)
        except Exception as exc:
            card_status="FAILED"
            log.exception("Feishu evidence summary card sync failed case=%s",case_id)
            audit(db,case_id=case_id,actor="feishu-evidence-document",event_type="FEISHU_EVIDENCE_CARD_SYNC_FAILED",
                  target_type="preliminary_evidence_report",target_id=report_id,detail={"error_code":type(exc).__name__,"error_message":str(exc)[:1000]})
        db.commit()
        acl_status="DISABLED"
        if settings.feishu_document_acl_enabled and binding.document_id:
            try:
                from app.workers.feishu_document_acl_task import sync_document_acl
                sync_document_acl.apply_async(args=[case_id,binding.document_id],queue="diagnosis",countdown=1)
                acl_status="QUEUED"
            except Exception as exc:
                acl_status="QUEUE_FAILED"
                log.exception("Feishu document ACL sync enqueue failed case=%s",case_id)
                with SessionLocal() as audit_db:
                    audit(audit_db,case_id=case_id,actor="feishu-evidence-document",event_type="FEISHU_DOCUMENT_ACL_QUEUE_FAILED",
                          target_type="feishu_evidence_document",target_id=binding.id,
                          detail={"document_id":binding.document_id,"error_code":type(exc).__name__,"error_message":str(exc)[:500]})
                    audit_db.commit()
        return {"status":"SYNCED","case_id":case_id,"report_id":report_id,"document_id":binding.document_id,"document_url":binding.document_url,
                "projection_version":binding.projection_version,"case_card":card_status,"document_acl":acl_status}
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
