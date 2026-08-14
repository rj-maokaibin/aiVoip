"""Celery resident task: run the Feishu WebSocket long-connection listener.

Runs on its own queue so it does not block other workers. The listener only acts
when FEISHU_LIVE_ENABLED=true and a long-connection bootstrap succeeds; otherwise
it sleeps and retries -- it must never crash the worker.
"""
from __future__ import annotations

import asyncio

from celery.utils.log import get_task_logger

from app.core.config import settings
from app.integrations.feishu.long_connection import run_long_connection
from app.workers.celery_app import celery_app

log = get_task_logger(__name__)


@celery_app.task(name='feishu.long_connection', bind=True, autoretry_for=(), max_retries=0)
def feishu_long_connection(self, *, run_seconds: float = 0.0):
    """Run the long-connection listener.

    run_seconds <= 0 means run indefinitely (resident worker); a positive value
    bounds the run (used by tests / one-shot diagnostics).
    """
    if not settings.feishu_live_enabled:
        return {'status': 'SKIPPED', 'reason': 'FEISHU_LIVE_DISABLED'}

    async def _run():
        if run_seconds and run_seconds > 0:
            task = asyncio.ensure_future(run_long_connection())
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=run_seconds)
            except asyncio.TimeoutError:
                task.cancel()
            return {'status': 'STOPPED', 'reason': f'bounded_{run_seconds}s'}
        return {'status': 'RUNNING', 'reconnects': await run_long_connection()}

    try:
        result = asyncio.run(_run())
    except Exception as exc:
        log.exception('feishu long-connection task failed')
        return {'status': 'FAILED', 'reason': f'{type(exc).__name__}:{exc}'}
    return result
