from celery import Celery
from app.core.config import settings
celery_app=Celery('voip_ai', broker=settings.redis_url, backend=settings.redis_url, include=['app.workers.collector_tasks','app.workers.packet_tasks','app.workers.pcm_tasks','app.workers.media_tasks','app.workers.attachment_tasks','app.workers.diagnosis_tasks','app.workers.evidence_report_tasks','app.workers.evidence_retention_tasks','app.workers.feishu_evidence_report_task','app.workers.feishu_document_acl_task','app.workers.reproduction_tasks','app.workers.reproduction_event_tasks','app.workers.device_provision_task','app.workers.feishu_long_connection_task'])
celery_app.conf.update(task_track_started=True, task_serializer='json', result_serializer='json', accept_content=['json'], timezone='UTC', enable_utc=True)

# Import producer-side signals in every process that imports the Celery app. This
# includes the Feishu API/WebSocket producer, so a successful reproduction.start
# publish can durably advance the AI2 suggestion from ACCEPTED to DISPATCHED.
from app.workers import ai2_dispatch_signals as _ai2_dispatch_signals  # noqa: E402,F401

celery_app.conf.beat_schedule = {
    'reproduction-reconcile': {
        'task': 'reproduction.reconcile',
        'schedule': 60.0,
        'options': {'queue': 'reproduction-control-high'},
    },
    'evidence-retention-sweep': {
        'task': 'evidence.retention_sweep',
        'schedule': 3600.0,
        'options': {'queue': 'celery'},
    },
}