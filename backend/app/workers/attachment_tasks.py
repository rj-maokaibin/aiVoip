from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from celery.utils.log import get_task_logger
from sqlalchemy import select

from app.analyzers.attachments import (align_field_audio, analyze_field_audio as run_field_audio_analysis,
                                       decode_field_audio_to_wav, inspect_image)
from app.contracts.enums import JobStatus, RunStatus
from app.db.models import AnalyzerRun, Artifact, Evidence, Job
from app.db.session import SessionLocal
from app.integrations.storage import ObjectStorage, materialize_evidence
from app.services.analysis import create_analyzer_run
from app.services.audit import audit
from app.services.jobs import transition_job
from app.workers.celery_app import celery_app

log = get_task_logger(__name__)


def utcnow(): return datetime.now(timezone.utc)


def _finish(job_id: str, evidence_id: str, *, analyzer_name: str, event_type: str, analyze):
    db = SessionLocal(); run = None
    try:
        job = db.get(Job, job_id); evidence = db.get(Evidence, evidence_id)
        if not job or not evidence:
            return {'status': 'missing_job_or_evidence'}
        run = create_analyzer_run(db, case_id=job.case_id, job_id=job.id, evidence_id=evidence.id,
                                  analyzer_name=analyzer_name, analyzer_version='1.0.0')
        run.status = RunStatus.RUNNING.value; run.started_at = utcnow()
        transition_job(db, job, JobStatus.RUNNING, reason=f'{analyzer_name}_started'); db.commit()
        with tempfile.TemporaryDirectory(prefix='voip-attachment-') as td:
            local = Path(td) / (Path(evidence.filename).name or 'attachment.bin')
            materialize_evidence(evidence, local)
            try:
                result = analyze(local)
            except ValueError as exc:
                # A valid Evidence object can still use a codec/container unavailable
                # in this development environment. Persist that as an analyzed,
                # explainable degradation so the diagnosis does not retry forever.
                result = {
                    'status': 'PARTIAL_SUCCESS',
                    'summary': {'availability': 'DECODE_UNAVAILABLE', 'reason': str(exc)},
                    'findings': [],
                    'limitations': ['附件内容未解码，系统未基于其内容生成诊断结论。'],
                }
        encoded = json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode()
        key = f'cases/{job.case_id}/analysis/{run.id}/{analyzer_name}.json'
        ObjectStorage().put_bytes(key, encoded, 'application/json')
        status = RunStatus(result.get('status', 'SUCCESS'))
        run.status = status.value; run.finished_at = utcnow(); run.summary_json = result.get('summary'); run.result_object_key = key
        transition_job(db, job, JobStatus(status.value), reason=f'{analyzer_name}_complete')
        audit(db, case_id=job.case_id, event_type=event_type, target_type='analyzer_run', target_id=run.id,
              detail={'evidence_id': evidence.id, 'summary': result.get('summary')})
        db.commit()
        from app.workers.diagnosis_tasks import notify_case_changed
        notify_case_changed(job.case_id)
        return {'status': status.value, 'job_id': job.id, 'analyzer_run_id': run.id, 'summary': result.get('summary')}
    except Exception as exc:
        log.exception('%s failed', analyzer_name)
        if 'job' in locals() and job:
            job.error_code = type(exc).__name__; job.error_message = str(exc)
            try: transition_job(db, job, JobStatus.FAILED, reason=f'{analyzer_name}_failed')
            except Exception: pass
        if run:
            run.status = RunStatus.FAILED.value; run.finished_at = utcnow(); run.error_code = type(exc).__name__; run.error_message = str(exc)
        db.commit()
        if 'job' in locals() and job:
            from app.workers.diagnosis_tasks import notify_case_changed
            notify_case_changed(job.case_id)
        raise
    finally:
        db.close()


@celery_app.task(name='attachment.analyze_field_audio', bind=True, autoretry_for=(), max_retries=0)
def analyze_field_audio(self, job_id: str, evidence_id: str):
    db=SessionLocal()
    try:
        evidence=db.get(Evidence,evidence_id)
        pcm_format=(evidence.metadata_json or {}).get('pcm_format') if evidence else None
    finally:
        db.close()
    return _finish(job_id, evidence_id, analyzer_name='field_audio_intelligence',
                   event_type='FIELD_AUDIO_ANALYSIS_FINISHED',
                   analyze=lambda path:run_field_audio_analysis(path,pcm_format=pcm_format))


@celery_app.task(name='attachment.inspect_image', bind=True, autoretry_for=(), max_retries=0)
def analyze_image(self, job_id: str, evidence_id: str):
    return _finish(job_id, evidence_id, analyzer_name='image_attachment_intelligence',
                   event_type='IMAGE_METADATA_ANALYSIS_FINISHED', analyze=inspect_image)


@celery_app.task(name='attachment.align_field_media', bind=True, autoretry_for=(), max_retries=0)
def align_field_media(self, job_id: str, evidence_id: str, media_run_id: str):
    db=SessionLocal(); run=None
    try:
        job=db.get(Job,job_id); evidence=db.get(Evidence,evidence_id); media_run=db.get(AnalyzerRun,media_run_id)
        if not job or not evidence or not media_run or media_run.case_id!=job.case_id:
            return {'status':'missing_or_cross_case_input'}
        run=create_analyzer_run(db,case_id=job.case_id,job_id=job.id,evidence_id=evidence.id,
                                analyzer_name='field_media_alignment',analyzer_version='1.0.0',
                                config_version=f'media:{media_run.analyzer_version}',
                                config_snapshot={'media_run_id':media_run.id})
        run.input_evidence_ids=list(dict.fromkeys([evidence.id]+list(media_run.input_evidence_ids or [])))
        run.status=RunStatus.RUNNING.value; run.started_at=utcnow()
        transition_job(db,job,JobStatus.RUNNING,reason='field_media_alignment_started'); db.commit()
        storage=ObjectStorage()
        media_result=json.loads(storage.get_bytes(media_run.result_object_key))
        field_run=db.scalar(select(AnalyzerRun).where(AnalyzerRun.case_id==job.case_id,
                    AnalyzerRun.analyzer_name=='field_audio_intelligence',
                    AnalyzerRun.status.in_(['SUCCESS','PARTIAL_SUCCESS'])).order_by(AnalyzerRun.created_at.desc()).limit(1))
        field_result=json.loads(storage.get_bytes(field_run.result_object_key)) if field_run and field_run.result_object_key else {}
        field_events=list(field_result.get('click_pop_events') or [])+list(field_result.get('silence_segments') or [])
        rows=list(db.scalars(select(Artifact).where(Artifact.analyzer_run_id==media_run.id,Artifact.type.in_(['AUDIO_WAV','PCM_WAV']))))
        with tempfile.TemporaryDirectory(prefix='field-media-align-') as td:
            td=Path(td); field_source=td/Path(evidence.filename).name; materialize_evidence(evidence,field_source,permanent_storage=storage)
            field_wav=td/'field.wav'; decode_field_audio_to_wav(field_source,field_wav)
            rtp_meta={x.get('stream_id'):x for x in media_result.get('rtp_audio_tracks') or []}
            pcm_meta={(x.get('pcm_tap'),x.get('session_index')):x for x in media_result.get('pcm_audio_tracks') or []}
            tracks=[]
            for index,row in enumerate(rows):
                meta=row.metadata_json or {}; local=td/f'track-{index}.wav'; storage.get_to_file(row.object_key,local)
                stream_id=meta.get('stream_id'); pcm_tap=meta.get('pcm_tap')
                detail=rtp_meta.get(stream_id) or pcm_meta.get((pcm_tap,meta.get('session_index'))) or {}
                start=detail.get('start_time')
                if start is None: continue
                tracks.append({'path':str(local),'source':'RTP' if stream_id else 'PCM','stream_id':stream_id,
                               'pcm_tap':pcm_tap,'start_time':start,'field_events':field_events})
            result=align_field_audio(field_wav,tracks,(media_result.get('packet') or {}).get('calls') or [])
        encoded=json.dumps(result,ensure_ascii=False,separators=(',',':')).encode(); key=f'cases/{job.case_id}/analysis/{run.id}/field_media_alignment.json'
        storage.put_bytes(key,encoded,'application/json')
        run.status=RunStatus.SUCCESS.value; run.finished_at=utcnow(); run.summary_json=result['summary']; run.result_object_key=key
        transition_job(db,job,JobStatus.SUCCESS,reason='field_media_alignment_complete')
        audit(db,case_id=job.case_id,event_type='FIELD_MEDIA_ALIGNMENT_FINISHED',target_type='analyzer_run',target_id=run.id,
              detail={'evidence_id':evidence.id,'media_run_id':media_run.id,'summary':result['summary']})
        db.commit()
        from app.workers.diagnosis_tasks import notify_case_changed
        notify_case_changed(job.case_id)
        return {'status':'SUCCESS','job_id':job.id,'analyzer_run_id':run.id,'summary':result['summary']}
    except Exception as exc:
        log.exception('field media alignment failed')
        if 'job' in locals() and job:
            job.error_code=type(exc).__name__; job.error_message=str(exc)
            try: transition_job(db,job,JobStatus.FAILED,reason='field_media_alignment_failed')
            except Exception: pass
        if run:
            run.status=RunStatus.FAILED.value; run.finished_at=utcnow(); run.error_code=type(exc).__name__; run.error_message=str(exc)
        db.commit()
        if 'job' in locals() and job:
            from app.workers.diagnosis_tasks import notify_case_changed
            notify_case_changed(job.case_id)
        raise
    finally: db.close()
