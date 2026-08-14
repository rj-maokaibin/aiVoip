"""Celery task: provision a DUT for background reproduction from a Feishu request.

Opens the SSH service, resolves the Poseidon password, and upserts the DUT into the
device_credentials DB table (the reproduction platform's local_secret provider reads
it from there). On success this also AUTO-CREATES a Case and a ReproductionSession
and kicks off autonomous reproduction — closing the gap where a Feishu message only
opened SSH but never started the reproduction flow.
Runs on a dedicated worker so long Poseidon calls never block the callback.
"""
from __future__ import annotations

import asyncio
from celery.utils.log import get_task_logger
from sqlalchemy import select

from app.integrations.feishu.device_request import parse_device_request
from app.integrations.feishu.device_provision import DeviceProvisioner
from app.workers.celery_app import celery_app

log = get_task_logger(__name__)


def _autostart_reproduction(sn: str, product: str | None, chat_id: str | None = None) -> dict:
    """Create a Case + ReproductionSession for the provisioned DUT and start it.

    ``chat_id`` (the Feishu group that @bot'ed) is bound to the Case so the
    diagnosis conclusion card is pushed back to that SAME group.

    Returns {'case_id','session_id','started': bool}. Uses device_credentials as the
    authoritative host/port/sn source so the reproduction platform can connect.
    """
    from app.db.models import Case, CaseDevice, DeviceCredential, ReproductionSession
    from app.db.session import SessionLocal
    from app.reproduction.orchestrator import ReproductionOrchestrator
    from app.reproduction.profile import ReproductionProfileRegistry
    from app.workers.reproduction_tasks import start_reproduction

    db = SessionLocal()
    try:
        cred = db.scalar(select(DeviceCredential).where(DeviceCredential.sn == sn))
        if cred is None:
            return {'case_id': None, 'session_id': None, 'started': False,
                    'reason': 'DEVICE_CREDENTIAL_NOT_FOUND'}
        ip = cred.ip or ''
        port = cred.ssh_port or 22
        # Reuse an existing open Case for this SN if present, else create one.
        case = db.scalar(
            select(Case).join(CaseDevice, CaseDevice.case_id == Case.id)
            .where(CaseDevice.sn == sn, Case.status.in_(['NEW', 'ANALYZING']))
            .order_by(Case.created_at.desc()).limit(1)
        )
        if case is None:
            from app.services.cases import create_case
            summary = f"飞书自动开通复现 · {product or sn} · {sn}"
            case = create_case(db, summary=summary, ip=ip, ssh_port=port, sn=sn,
                               created_by='feishu-provision')
            db.refresh(case)
        else:
            db.refresh(case)
        device = db.scalar(select(CaseDevice).where(
            CaseDevice.case_id == case.id, CaseDevice.sn == sn))
        if device is None:
            device = CaseDevice(case_id=case.id, ip=ip, ssh_port=port, sn=sn,
                                username='root', device_info={'product': product} if product else {})
            db.add(device); db.flush()
        # Bind the Case to the source Feishu group so the conclusion card returns
        # to the SAME chat (even when different faults come from different groups).
        if chat_id:
            try:
                from app.integrations.feishu.service import bind_case_to_chat
                bind_case_to_chat(db, case_id=case.id, chat_id=chat_id)
                db.flush()
            except Exception:
                log.exception('bind case to feishu chat failed; provision continues')
        # Create a reproduction session for this case/device and start it.
        orch = ReproductionOrchestrator(registry=ReproductionProfileRegistry())
        session = orch.create_session(db, case_id=case.id,
                                      profile_id='VOIP_GENERIC_FULL_CAPTURE',
                                      device_id=device.id, actor='feishu-provision')
        db.commit()
        start_reproduction.apply_async(args=[session.id], queue='reproduction')
        return {'case_id': case.id, 'session_id': session.id, 'started': True}
    except Exception as exc:  # provision succeeded; autostart must not fail the provision
        log.exception('autostart reproduction failed')
        return {'case_id': None, 'session_id': None, 'started': False,
                'reason': f'{type(exc).__name__}:{exc}'}
    finally:
        db.close()


@celery_app.task(name='device.provision_from_feishu', bind=True, autoretry_for=(), max_retries=0)
def provision_from_feishu(self, text: str, chat_id: str | None = None):
    async def _run():
        req = parse_device_request(text)
        if not req.has_minimal():
            return {"status": "MISSING_PARAMS", "sn": req.sn, "ssh_ip": req.ssh_ip}
        result = await DeviceProvisioner().provision(
            web_url=req.web_url, ssh_ip=req.ssh_ip, ssh_port=req.ssh_port,
            sn=req.sn, mac=req.mac, product=req.product,
        )
        sn = result.get("sn") or req.sn
        # Auto-create Case + ReproductionSession, bind to the source Feishu chat
        # and start autonomous reproduction.
        auto = _autostart_reproduction(sn=sn, product=req.product, chat_id=chat_id)
        return {"status": "OK", **result, "autostart": auto}

    try:
        return asyncio.run(_run())
    except Exception as exc:
        log.exception('device provision failed')
        return {"status": "FAILED", "reason": f"{type(exc).__name__}:{exc}"}
