import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from celery.utils.log import get_task_logger

from app.contracts.enums import JobStatus, RunStatus
from app.analyzers.packet import PacketIntelligenceEngine, TSharkAdapter
from app.core.config import settings
from app.db.models import AnalyzerRun, Evidence, Job
from app.db.session import SessionLocal
from app.integrations.storage import ObjectStorage, materialize_evidence
from app.services.analysis import create_analyzer_run
from app.services.audit import audit
from app.services.jobs import transition_job
from app.workers.celery_app import celery_app

log=get_task_logger(__name__)


def utcnow(): return datetime.now(timezone.utc)


def _notify_reports(case_id:str,reason:str) -> None:
    from app.workers.evidence_report_tasks import notify_evidence_report_changed
    notify_evidence_report_changed(case_id,reason)


@celery_app.task(name='packet.analyze_evidence', bind=True, autoretry_for=(), max_retries=0)
def analyze_evidence(self, job_id:str, evidence_id:str):
    db=SessionLocal()
    run=None
    try:
        job=db.get(Job, job_id)
        evidence=db.get(Evidence, evidence_id)
        if not job or not evidence:
            return {'status':'missing_job_or_evidence'}
        if evidence.type not in {'PCAP','PCAPNG'} and not evidence.filename.lower().endswith(('.pcap','.pcapng')):
            raise ValueError('EVIDENCE_NOT_PCAP')

        engine=PacketIntelligenceEngine(TSharkAdapter(settings.tshark_binary, settings.tshark_timeout_seconds))
        run=create_analyzer_run(db, case_id=job.case_id, job_id=job.id, evidence_id=evidence.id,
                                analyzer_name=engine.analyzer_name, analyzer_version=engine.analyzer_version,
                                config_version=f"{engine.analyzer_profile.id}@{engine.analyzer_profile.version}",
                                config_snapshot={"analyzer_profile":engine.analyzer_profile.snapshot()})
        run.status=RunStatus.RUNNING.value; run.started_at=utcnow()
        transition_job(db, job, JobStatus.RUNNING, reason='packet_analysis_started'); db.commit()

        suffix=Path(evidence.filename).suffix or '.pcap'
        with tempfile.TemporaryDirectory(prefix='voip-packet-') as td:
            local=Path(td)/f'input{suffix}'
            materialize_evidence(evidence, local)
            result=engine.analyze_pcap(local)
            encoded=json.dumps(result, ensure_ascii=False, separators=(',',':')).encode('utf-8')
            result_key=f'cases/{job.case_id}/analysis/{run.id}/packet_analysis.json'
            ObjectStorage().put_bytes(result_key, encoded, 'application/json')

        run.status=RunStatus.SUCCESS.value; run.finished_at=utcnow(); run.summary_json=result.get('summary'); run.result_object_key=result_key
        transition_job(db, job, JobStatus.SUCCESS, reason='packet_analysis_complete')
        audit(db, case_id=job.case_id, event_type='PACKET_ANALYSIS_FINISHED', target_type='analyzer_run', target_id=run.id,
              detail={'evidence_id':evidence.id,'summary':result.get('summary'),'analyzer_version':run.analyzer_version})
        db.commit()
        from app.workers.diagnosis_tasks import notify_case_changed
        notify_case_changed(job.case_id); _notify_reports(job.case_id,'packet_analysis_complete')
        return {'status':JobStatus.SUCCESS.value,'job_id':job.id,'analyzer_run_id':run.id,'summary':result.get('summary')}
    except Exception as exc:
        log.exception('packet analysis failed')
        if 'job' in locals() and job:
            job.error_code=type(exc).__name__; job.error_message=str(exc)
            try: transition_job(db, job, JobStatus.FAILED, reason='packet_analysis_failed')
            except Exception: pass
        if run:
            run.status=RunStatus.FAILED.value; run.finished_at=utcnow(); run.error_code=type(exc).__name__; run.error_message=str(exc)
        db.commit()
        if 'job' in locals() and job:
            from app.workers.diagnosis_tasks import notify_case_changed
            notify_case_changed(job.case_id); _notify_reports(job.case_id,'packet_analysis_failed')
        raise
    finally:
        db.close()
