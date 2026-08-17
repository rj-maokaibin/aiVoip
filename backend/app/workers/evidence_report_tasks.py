from __future__ import annotations

from celery.utils.log import get_task_logger
from sqlalchemy import or_, select

from app.contracts.enums import ReproductionCallStatus, ReproductionState
from app.db.models import AnalyzerRun, Case, ReproductionCall, ReproductionSession
from app.db.session import SessionLocal
from app.services.evidence_report import generate_evidence_report
from app.workers.celery_app import celery_app

log=get_task_logger(__name__)
_ACTIVE_ANALYZER={"PENDING","RUNNING"}
_TERMINAL_SESSION={ReproductionState.COMPLETED.value,ReproductionState.PARTIAL_SUCCESS.value,ReproductionState.FAILED.value,
                   ReproductionState.CANCELLED.value,ReproductionState.WATCH_TIMEOUT.value,ReproductionState.CAPTURE_TIMEOUT.value}


def notify_evidence_report_changed(case_id:str,reason:str="analyzer_changed") -> None:
    try: refresh_case_evidence_reports.apply_async(args=[case_id,reason],queue="diagnosis",countdown=3)
    except Exception: log.exception("failed to enqueue evidence report refresh case=%s",case_id)


def _active_analyzer_exists(db,case_id:str) -> bool:
    return db.scalar(select(AnalyzerRun.id).where(AnalyzerRun.case_id==case_id,AnalyzerRun.status.in_(_ACTIVE_ANALYZER)).limit(1)) is not None


@celery_app.task(name="evidence_report.refresh_case",bind=True,max_retries=3,default_retry_delay=2)
def refresh_case_evidence_reports(self,case_id:str,reason:str="case_changed"):
    db=SessionLocal(); generated=[]; errors=[]
    try:
        if not db.get(Case,case_id): return {"status":"CASE_NOT_FOUND","case_id":case_id}
        if _active_analyzer_exists(db,case_id) and self.request.retries < self.max_retries: raise self.retry(countdown=2)
        calls=list(db.scalars(select(ReproductionCall).where(ReproductionCall.case_id==case_id,or_(
            ReproductionCall.ended_at.is_not(None),ReproductionCall.status.in_([ReproductionCallStatus.ENDED.value,ReproductionCallStatus.ANALYZING.value,ReproductionCallStatus.ANALYZED.value])
        )).order_by(ReproductionCall.call_no.asc())))
        for call in calls:
            try:
                row,_payload,replay=generate_evidence_report(db,scope_type="CALL",scope_id=call.id,actor="evidence-report-worker",force=False)
                db.commit(); generated.append({"scope":"CALL","id":call.id,"report_id":row.id,"version":row.version,"replay":replay})
            except Exception as exc:
                db.rollback(); errors.append({"scope":"CALL","id":call.id,"error":f"{type(exc).__name__}:{exc}"}); log.exception("call report failed call=%s",call.id)
        sessions=list(db.scalars(select(ReproductionSession).where(ReproductionSession.case_id==case_id).order_by(ReproductionSession.created_at.asc())))
        for session in sessions:
            if session.ended_at is None and session.state not in _TERMINAL_SESSION: continue
            try:
                row,_payload,replay=generate_evidence_report(db,scope_type="SESSION",scope_id=session.id,actor="evidence-report-worker",force=False)
                db.commit(); generated.append({"scope":"SESSION","id":session.id,"report_id":row.id,"version":row.version,"replay":replay})
            except Exception as exc:
                db.rollback(); errors.append({"scope":"SESSION","id":session.id,"error":f"{type(exc).__name__}:{exc}"}); log.exception("session report failed session=%s",session.id)
        try:
            row,_payload,replay=generate_evidence_report(db,scope_type="CASE",scope_id=case_id,actor="evidence-report-worker",force=False)
            db.commit(); generated.append({"scope":"CASE","id":case_id,"report_id":row.id,"version":row.version,"replay":replay})
            try:
                from app.workers.feishu_evidence_report_task import project_case_evidence_document
                project_case_evidence_document.apply_async(args=[case_id,row.id],queue="diagnosis",countdown=1)
            except Exception: log.exception("failed to enqueue Feishu report projection case=%s",case_id)
        except Exception as exc:
            db.rollback(); errors.append({"scope":"CASE","id":case_id,"error":f"{type(exc).__name__}:{exc}"}); log.exception("case report failed case=%s",case_id)
        return {"status":"PARTIAL" if errors else "SUCCESS","case_id":case_id,"reason":reason,"generated":generated,"errors":errors}
    finally: db.close()
