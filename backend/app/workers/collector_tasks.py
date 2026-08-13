import asyncio
from celery.utils.log import get_task_logger
from app.workers.celery_app import celery_app
from app.db.session import SessionLocal
from app.db.models import Case, CaseDevice, Job
from app.integrations.credentials import get_credential_provider, CredentialError
from app.actions.executor import ActionEngine
from app.services.cases import transition_case
from app.services.jobs import transition_job
from app.services.audit import audit
from app.contracts.enums import CaseEvent, JobStatus

log=get_task_logger(__name__)


async def _run(job_id:str):
    db=SessionLocal()
    try:
        job=db.get(Job, job_id)
        if not job: return {'status':'missing_job'}
        case=db.get(Case, job.case_id)
        device=db.query(CaseDevice).filter(CaseDevice.case_id==case.id).order_by(CaseDevice.created_at.asc()).first()
        transition_job(db,job,JobStatus.RUNNING,reason='collector_job_started')
        transition_case(db,case,CaseEvent.COLLECTION_STARTED,'collector_job_started')
        db.commit()
        provider=get_credential_provider()
        password=await provider.get_password(sn=device.sn, ip=device.ip)
        # When the local secret file is authoritative, its username overrides any
        # UI/default value so real-device auth uses the correct account.
        from app.integrations.credentials import LocalSecretCredentialProvider
        if isinstance(provider, LocalSecretCredentialProvider):
            device.username = provider.resolve_username(ip=device.ip, fallback=device.username)
            db.flush()
        await ActionEngine().run_profile(db, case=case, device=device, job=job, password=password)
        transition_job(db,job,JobStatus.SUCCESS,reason='collector_job_complete')
        transition_case(db,case,CaseEvent.COLLECTION_COMPLETED,'basic_collection_complete')
        audit(db, case_id=case.id, event_type='COLLECT_JOB_FINISHED', target_type='job', target_id=job.id, detail={'status':JobStatus.SUCCESS.value})
        db.commit()
        from app.workers.diagnosis_tasks import notify_case_changed
        notify_case_changed(case.id)
        return {'status':JobStatus.SUCCESS.value,'job_id':job.id}
    except CredentialError as exc:
        if 'job' in locals() and job:
            job.error_code='CREDENTIAL_ERROR'; job.error_message=str(exc)
            try: transition_job(db,job,JobStatus.FAILED,reason='credential_error')
            except Exception: pass
            if 'case' in locals() and case:
                try: transition_case(db,case,CaseEvent.CASE_FAILED,'credential_error')
                except Exception: pass
            db.commit()
            from app.workers.diagnosis_tasks import notify_case_changed
            notify_case_changed(job.case_id)
        raise
    except Exception as exc:
        log.exception('collector job failed')
        if 'job' in locals() and job:
            job.error_code=type(exc).__name__; job.error_message=str(exc)
            try: transition_job(db,job,JobStatus.FAILED,reason='collector_error')
            except Exception: pass
            if 'case' in locals() and case:
                try: transition_case(db,case,CaseEvent.CASE_FAILED,'collector_error')
                except Exception: pass
            db.commit()
            from app.workers.diagnosis_tasks import notify_case_changed
            notify_case_changed(job.case_id)
        raise
    finally: db.close()

@celery_app.task(name='collector.collect_case', bind=True, autoretry_for=(), max_retries=0)
def collect_case(self, job_id:str): return asyncio.run(_run(job_id))
