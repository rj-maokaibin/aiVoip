import hashlib
from datetime import datetime, timezone
from sqlalchemy import select

from app.actions.registry import ActionRegistry
from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter
from app.contracts.enums import ActionRiskLevel, EvidenceCompleteness, EvidenceKind, EvidenceLevel, EvidenceScope, RunStatus
from app.db.models import ActionRun
from app.integrations.storage import ObjectStorage
from app.services.audit import audit
from app.services.evidence import create_evidence


class ActionPolicyError(RuntimeError): pass

def _utcnow(): return datetime.now(timezone.utc)


class ActionEngine:
    def __init__(self, registry=None, storage=None):
        self.registry=registry or ActionRegistry(); self.storage=storage or ObjectStorage()

    async def run_profile(self, db, *, case, device, job, password):
        profile=self.registry.profile(job.profile_id)
        adapter=AsyncSSHDeviceAdapter(ip=device.ip, port=device.ssh_port, username=device.username, password=password)
        await adapter.connect()
        try:
            for action_id in profile.actions:
                action=self.registry.action(action_id)
                existing=db.scalar(select(ActionRun).where(
                    ActionRun.job_id==job.id, ActionRun.device_id==device.id,
                    ActionRun.action_id==action.id,
                    ActionRun.status.in_([RunStatus.SUCCESS.value,RunStatus.PARTIAL_SUCCESS.value])
                ).order_by(ActionRun.started_at.desc()))
                if existing:
                    continue
                risk=ActionRiskLevel(action.risk_level)
                # EC-02 remains reserved. Existing approved M1 collector only auto-runs L0/L1.
                if risk not in {ActionRiskLevel.L0,ActionRiskLevel.L1}:
                    raise ActionPolicyError(f'APPROVAL_REQUIRED:{action.id}')
                run=ActionRun(case_id=case.id, job_id=job.id, device_id=device.id, action_id=action.id,
                              risk_level=risk.value, status=RunStatus.RUNNING.value, started_at=_utcnow())
                db.add(run); db.flush()
                audit(db, case_id=case.id, event_type='ACTION_STARTED', target_type='action_run', target_id=run.id,
                      detail={'action_id':action.id,'risk_level':risk.value})
                try:
                    if action.executor=='shell': result=await adapter.execute_shell(action.command, timeout=action.timeout)
                    elif action.executor=='aim': result=await adapter.execute_cli(action.command, timeout=action.timeout)
                    else: raise ActionPolicyError('UNSUPPORTED_EXECUTOR')
                    body=(result.stdout + (('\n[stderr]\n'+result.stderr) if result.stderr else '')).encode('utf-8', errors='replace')
                    sha=hashlib.sha256(body).hexdigest(); filename=f'{action.id}.txt'; object_key=f'cases/{case.id}/jobs/{job.id}/{run.id}/{filename}'
                    self.storage.put_bytes(object_key, body, 'text/plain; charset=utf-8')
                    ev=create_evidence(
                        db,case_id=case.id,device_id=device.id,job_id=job.id,action_run_id=run.id,
                        evidence_type=action.evidence_type,source='COLLECTOR',kind=EvidenceKind.RAW,scope=EvidenceScope.DEVICE,
                        level=EvidenceLevel.L1,completeness=EvidenceCompleteness.COMPLETE if result.exit_status==0 else EvidenceCompleteness.PARTIAL,
                        filename=filename,object_key=object_key,size_bytes=len(body),sha256=sha,content_type='text/plain',captured_at=_utcnow(),
                        producer_type='ACTION_RUN',producer_id=run.id,producer_version='1.0.0',metadata={'action_id':action.id,'exit_status':result.exit_status},
                    )
                    run.exit_status=result.exit_status
                    run.status=RunStatus.SUCCESS.value if result.exit_status==0 else RunStatus.PARTIAL_SUCCESS.value
                    run.finished_at=_utcnow()
                    audit(db, case_id=case.id, event_type='ACTION_FINISHED', target_type='action_run', target_id=run.id,
                          detail={'action_id':action.id,'status':run.status,'evidence_id':ev.id})
                    db.commit()
                except Exception as exc:
                    run.status=RunStatus.FAILED.value; run.error_message=f'{type(exc).__name__}:{exc}'; run.finished_at=_utcnow(); db.commit(); raise
        finally:
            await adapter.disconnect()
