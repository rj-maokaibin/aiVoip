from __future__ import annotations

import hashlib
import json

from sqlalchemy.orm import Session

from app.contracts.enums import EvidenceScope, JobStatus, RunStatus
from app.db.models import AnalyzerRun, Job
from app.services.audit import audit


def create_packet_analysis_job(db:Session, *, case_id:str, evidence_id:str) -> Job:
    job=Job(case_id=case_id, type='ANALYZE_PACKET', status=JobStatus.PENDING.value, profile_id=None)
    db.add(job); db.flush()
    audit(db, case_id=case_id, event_type='PACKET_ANALYSIS_JOB_CREATED', target_type='job', target_id=job.id, detail={'evidence_id':evidence_id})
    db.commit(); db.refresh(job); return job


def _config_checksum(snapshot:dict|None) -> str|None:
    if snapshot is None: return None
    return hashlib.sha256(json.dumps(snapshot,sort_keys=True,separators=(',',':'),ensure_ascii=False,default=str).encode()).hexdigest()


def create_analyzer_run(
    db:Session,
    *, case_id:str,
    job_id:str,
    evidence_id:str,
    analyzer_name:str,
    analyzer_version:str,
    config_version:str='default',
    config_snapshot:dict|None=None,
    scope:EvidenceScope|str=EvidenceScope.CASE,
) -> AnalyzerRun:
    scope=EvidenceScope(scope)
    run=AnalyzerRun(
        case_id=case_id, job_id=job_id, analyzer_name=analyzer_name, analyzer_version=analyzer_version,
        config_version=config_version, config_checksum=_config_checksum(config_snapshot), config_snapshot=config_snapshot,
        scope=scope.value, status=RunStatus.PENDING.value, input_evidence_ids=[evidence_id], output_evidence_ids=[]
    )
    db.add(run); db.flush(); return run


def create_pcm_analysis_job(db:Session, *, case_id:str, evidence_id:str, profile_id:str) -> Job:
    job=Job(case_id=case_id, type='ANALYZE_PCM', status=JobStatus.PENDING.value, profile_id=profile_id)
    db.add(job); db.flush()
    audit(db, case_id=case_id, event_type='PCM_ANALYSIS_JOB_CREATED', target_type='job', target_id=job.id, detail={'evidence_id':evidence_id,'profile_id':profile_id})
    db.commit(); db.refresh(job); return job


def create_media_analysis_job(db:Session, *, case_id:str, evidence_id:str, profile_id:str) -> Job:
    job=Job(case_id=case_id, type='ANALYZE_MEDIA', status=JobStatus.PENDING.value, profile_id=profile_id)
    db.add(job); db.flush()
    audit(db, case_id=case_id, event_type='MEDIA_ANALYSIS_JOB_CREATED', target_type='job', target_id=job.id, detail={'evidence_id':evidence_id,'profile_id':profile_id})
    db.commit(); db.refresh(job); return job


def create_field_audio_analysis_job(db:Session, *, case_id:str, evidence_id:str) -> Job:
    job=Job(case_id=case_id, type='ANALYZE_FIELD_AUDIO', status=JobStatus.PENDING.value)
    db.add(job); db.flush()
    audit(db, case_id=case_id, event_type='FIELD_AUDIO_ANALYSIS_JOB_CREATED', target_type='job', target_id=job.id,
          detail={'evidence_id':evidence_id})
    db.commit(); db.refresh(job); return job


def create_image_analysis_job(db:Session, *, case_id:str, evidence_id:str) -> Job:
    job=Job(case_id=case_id, type='ANALYZE_IMAGE_METADATA', status=JobStatus.PENDING.value)
    db.add(job); db.flush()
    audit(db, case_id=case_id, event_type='IMAGE_METADATA_ANALYSIS_JOB_CREATED', target_type='job', target_id=job.id,
          detail={'evidence_id':evidence_id})
    db.commit(); db.refresh(job); return job


def create_field_media_alignment_job(db:Session, *, case_id:str, evidence_id:str, media_run_id:str) -> Job:
    job=Job(case_id=case_id,type='ALIGN_FIELD_MEDIA',status=JobStatus.PENDING.value)
    db.add(job); db.flush()
    audit(db,case_id=case_id,event_type='FIELD_MEDIA_ALIGNMENT_JOB_CREATED',target_type='job',target_id=job.id,
          detail={'evidence_id':evidence_id,'media_run_id':media_run_id})
    db.commit(); db.refresh(job); return job
