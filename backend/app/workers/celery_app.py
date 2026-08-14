from celery import Celery
from app.core.config import settings
celery_app=Celery('voip_ai', broker=settings.redis_url, backend=settings.redis_url, include=['app.workers.collector_tasks','app.workers.packet_tasks','app.workers.pcm_tasks','app.workers.media_tasks','app.workers.diagnosis_tasks','app.workers.reproduction_tasks','app.workers.reproduction_event_tasks','app.workers.device_provision_task','app.workers.feishu_long_connection_task'])
celery_app.conf.update(task_track_started=True, task_serializer='json', result_serializer='json', accept_content=['json'], timezone='UTC', enable_utc=True)
