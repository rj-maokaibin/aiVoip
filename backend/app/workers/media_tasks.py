from __future__ import annotations

import hashlib
import json
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from celery.utils.log import get_task_logger

from app.contracts.enums import JobStatus, RunStatus
from app.analyzers.media import MediaIntelligenceEngine
from app.analyzers.packet import TSharkAdapter
from app.analyzers.pcm import load_pcm_profile
from app.core.config import settings
from app.db.models import Artifact, Evidence, Job
from app.db.session import SessionLocal
from app.integrations.storage import ObjectStorage, materialize_evidence
from app.services.analysis import create_analyzer_run
from app.services.audit import audit
from app.services.jobs import transition_job
from app.workers.celery_app import celery_app

log = get_task_logger(__name__)
_PROFILE_ID = re.compile(r'^[A-Za-z0-9_.-]+$')


def utcnow(): return datetime.now(timezone.utc)


def _profile_path(profile_id: str) -> Path:
    if not _PROFILE_ID.fullmatch(profile_id):
        raise ValueError('PCM_PROFILE_ID_INVALID')
    root = (settings.profile_root / 'pcm').resolve()
    path = (root / f'{profile_id}.yaml').resolve()
    if root not in path.parents or not path.exists():
        raise ValueError('PCM_PROFILE_NOT_FOUND')
    return path


def _notify_reports(case_id:str,reason:str) -> None:
    from app.workers.evidence_report_tasks import notify_evidence_report_changed
    notify_evidence_report_changed(case_id,reason)


@celery_app.task(name='media.analyze_evidence', bind=True, autoretry_for=(), max_retries=0)
def analyze_media_evidence(self, job_id: str, evidence_id: str, profile_id: str = 'ruijie_aim_diag_v1'):
    db = SessionLocal(); run = None
    try:
        job = db.get(Job, job_id); evidence = db.get(Evidence, evidence_id)
        if not job or not evidence:
            return {'status':'missing_job_or_evidence'}
        if evidence.type not in {'PCAP','PCAPNG'} and not evidence.filename.lower().endswith(('.pcap','.pcapng')):
            raise ValueError('EVIDENCE_NOT_PCAP')
        profile = load_pcm_profile(_profile_path(profile_id))
        engine = MediaIntelligenceEngine(profile, TSharkAdapter(settings.tshark_binary, settings.tshark_timeout_seconds))
        run = create_analyzer_run(db, case_id=job.case_id, job_id=job.id, evidence_id=evidence.id,
                                  analyzer_name=engine.analyzer_name, analyzer_version=engine.analyzer_version,
                                  config_version=f"{profile.id}@{profile.version}+{engine.analyzer_profile.id}@{engine.analyzer_profile.version}",
                                  config_snapshot={"pcm_profile":profile.snapshot(),"analyzer_profile":engine.analyzer_profile.snapshot()})
        run.status=RunStatus.RUNNING.value; run.started_at=utcnow(); transition_job(db, job, JobStatus.RUNNING, reason='media_analysis_started'); db.commit()
        suffix=Path(evidence.filename).suffix or '.pcap'
        storage=ObjectStorage()
        with tempfile.TemporaryDirectory(prefix='voip-media-') as td:
            td=Path(td); local=td/f'input{suffix}'; out=td/'artifacts'; out.mkdir()
            materialize_evidence(evidence, local, permanent_storage=storage)
            result=engine.analyze_pcap(local, out)
            artifact_rows=[]
            for spec in result.get('artifacts', []):
                local_path=Path(spec.pop('local_path'))
                data=local_path.read_bytes(); sha=hashlib.sha256(data).hexdigest()
                object_key=f'cases/{job.case_id}/analysis/{run.id}/artifacts/{local_path.name}'
                storage.put_file(object_key, local_path, spec.get('content_type') or 'application/octet-stream')
                row=Artifact(case_id=job.case_id, analyzer_run_id=run.id, evidence_id=evidence.id,
                             type=spec['type'], filename=local_path.name, object_key=object_key,
                             content_type=spec.get('content_type'), size_bytes=len(data), sha256=sha,
                             metadata_json=spec.get('metadata') or {})
                db.add(row); db.flush(); artifact_rows.append(row)
                spec['artifact_id']=row.id; spec['object_key']=object_key; spec['sha256']=sha; spec['size_bytes']=len(data)
            encoded=json.dumps(result,ensure_ascii=False,separators=(',',':')).encode('utf-8')
            result_key=f'cases/{job.case_id}/analysis/{run.id}/media_analysis.json'
            storage.put_bytes(result_key,encoded,'application/json')
        final_status=result.get('status',RunStatus.SUCCESS.value)
        run.status=final_status; run.finished_at=utcnow(); run.summary_json=result.get('summary'); run.result_object_key=result_key
        transition_job(db, job, JobStatus(final_status), reason='media_analysis_complete')
        audit(db,case_id=job.case_id,event_type='MEDIA_ANALYSIS_FINISHED',target_type='analyzer_run',target_id=run.id,
              detail={'evidence_id':evidence.id,'profile_id':profile.id,'summary':result.get('summary'),'artifact_count':len(result.get('artifacts',[]))})
        db.commit()
        from app.workers.diagnosis_tasks import notify_case_changed
        notify_case_changed(job.case_id); _notify_reports(job.case_id,'media_analysis_complete')
        return {'status':final_status,'job_id':job.id,'analyzer_run_id':run.id,'summary':result.get('summary')}
    except Exception as exc:
        log.exception('media analysis failed')
        if 'job' in locals() and job:
            job.error_code=type(exc).__name__; job.error_message=str(exc)
            try: transition_job(db, job, JobStatus.FAILED, reason='media_analysis_failed')
            except Exception: pass
        if run:
            run.status=RunStatus.FAILED.value; run.finished_at=utcnow(); run.error_code=type(exc).__name__; run.error_message=str(exc)
        db.commit()
        if 'job' in locals() and job:
            from app.workers.diagnosis_tasks import notify_case_changed
            notify_case_changed(job.case_id); _notify_reports(job.case_id,'media_analysis_failed')
        raise
    finally:
        db.close()
