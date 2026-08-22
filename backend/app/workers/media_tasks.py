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
from app.analyzers.media.candidate_artifacts import gate_candidate_audio_artifacts, sanitize_gated_media_pcm
from app.analyzers.media.candidate_decision import CANDIDATE_DECISION_VERSION, apply_candidate_decisions
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
MEDIA_GATED_ANALYZER_VERSION = '0.5.0'


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
                                  analyzer_name=engine.analyzer_name, analyzer_version=MEDIA_GATED_ANALYZER_VERSION,
                                  config_version=f"{profile.id}@{profile.version}+{engine.analyzer_profile.id}@{engine.analyzer_profile.version}+{CANDIDATE_DECISION_VERSION}",
                                  config_snapshot={"pcm_profile":profile.snapshot(),"analyzer_profile":engine.analyzer_profile.snapshot(),
                                                   "candidate_decision_version":CANDIDATE_DECISION_VERSION,
                                                   "base_media_engine_version":engine.analyzer_version})
        run.status=RunStatus.RUNNING.value; run.started_at=utcnow(); transition_job(db, job, JobStatus.RUNNING, reason='media_analysis_started'); db.commit()
        suffix=Path(evidence.filename).suffix or '.pcap'
        storage=ObjectStorage()
        with tempfile.TemporaryDirectory(prefix='voip-media-') as td:
            td=Path(td); local=td/f'input{suffix}'; out=td/'artifacts'; out.mkdir()
            materialize_evidence(evidence, local, permanent_storage=storage)
            raw_result=engine.analyze_pcap(local, out)
            # The persisted contract is Media Intelligence 0.5.0: base 0.4 engine
            # facts plus CandidateDecision v1 and RTP energy-window evidence.
            raw_result['base_engine_version']=raw_result.get('version') or engine.analyzer_version
            raw_result['version']=MEDIA_GATED_ANALYZER_VERSION
            raw_result['candidate_decision_version']=CANDIDATE_DECISION_VERSION
            gated=apply_candidate_decisions({
                'packet_intelligence': raw_result.get('packet'),
                'pcm_intelligence': None,
                'media_intelligence': raw_result,
            })
            result=gated['media_intelligence'] or raw_result
            # CandidateDecision has already consumed the raw PCM detector events.
            # Move them behind the candidate-only boundary before persistence so the
            # deterministic Diagnosis reasoner cannot recreate fallback hypotheses
            # from rejected click/silence detector hits.
            result=sanitize_gated_media_pcm(result)
            # PCM detector clips are also candidates. Rejected/inconclusive clips
            # remain downloadable audit artifacts but are not exposed as main
            # AUDIO_CLIP attachments unless their CandidateDecision was promoted.
            result=gate_candidate_audio_artifacts(result)
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
              detail={'evidence_id':evidence.id,'profile_id':profile.id,'summary':result.get('summary'),'artifact_count':len(result.get('artifacts',[])),
                      'analyzer_version':MEDIA_GATED_ANALYZER_VERSION,'base_media_engine_version':engine.analyzer_version,
                      'candidate_decision':(result.get('summary') or {}).get('candidate_decision'),
                      'candidate_audio_artifacts':(result.get('summary') or {}).get('candidate_audio_artifacts')})
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
