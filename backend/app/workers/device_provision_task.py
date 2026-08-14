"""Celery task: provision a DUT for background reproduction from a Feishu request.

Opens the SSH service and resolves the Poseidon password, then upserts the DUT into
~/secret.yaml so the reproduction platform's local_secret provider can use it.
Runs on a dedicated worker so a long Poseidon call never blocks the callback.
"""
from __future__ import annotations

import asyncio
from celery.utils.log import get_task_logger

from app.integrations.feishu.device_request import parse_device_request
from app.integrations.feishu.device_provision import DeviceProvisioner
from app.workers.celery_app import celery_app

log = get_task_logger(__name__)


@celery_app.task(name='device.provision_from_feishu', bind=True, autoretry_for=(), max_retries=0)
def provision_from_feishu(self, text: str):
    async def _run():
        req = parse_device_request(text)
        if not req.has_minimal():
            return {"status": "MISSING_PARAMS", "sn": req.sn, "ssh_ip": req.ssh_ip}
        result = await DeviceProvisioner().provision(
            web_url=req.web_url, ssh_ip=req.ssh_ip, ssh_port=req.ssh_port,
            sn=req.sn, mac=req.mac, product=req.product,
        )
        return {"status": "OK", **result}

    try:
        return asyncio.run(_run())
    except Exception as exc:
        log.exception('device provision failed')
        return {"status": "FAILED", "reason": f"{type(exc).__name__}:{exc}"}
