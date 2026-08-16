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


@celery_app.task(name='feishu.sync_case_card', bind=True, autoretry_for=(), max_retries=0)
def sync_case_card(self, case_id: str, reason: str = 'case_changed'):
    """Best-effort asynchronous card refresh for runtime readiness changes."""
    from app.core.config import settings
    from app.db.session import SessionLocal
    from app.integrations.feishu.service import FeishuCaseCardService

    if not settings.feishu_live_enabled:
        return {'status': 'SKIPPED', 'reason': 'FEISHU_LIVE_DISABLED', 'case_id': case_id}
    db = SessionLocal()
    try:
        row = asyncio.run(FeishuCaseCardService().sync_case_card(db, case_id=case_id))
        db.commit()
        return {
            'status': 'SYNCED', 'case_id': case_id, 'reason': reason,
            'message_id': getattr(row, 'message_id', None),
        }
    except Exception as exc:
        db.rollback()
        log.exception('feishu case card sync failed case=%s reason=%s', case_id, reason)
        return {'status': 'FAILED', 'case_id': case_id, 'reason': reason,
                'error': f'{type(exc).__name__}:{exc}'}
    finally:
        db.close()


def _autostart_reproduction(sn: str, product: str | None, chat_id: str | None = None,
                           chat_type: str | None = None,
                           source_context: dict | None = None,
                           start_reproduction_session: bool = True,
                           case_summary: str | None = None) -> dict:
    """Create a Case + ReproductionSession for the provisioned DUT and start it.

    ``chat_id`` (the Feishu group that @bot'ed) is bound to the Case so the
    diagnosis conclusion card is pushed back to that SAME group.

    Returns {'case_id','session_id','started': bool}. Uses device_credentials as the
    authoritative host/port/sn source so the reproduction platform can connect.
    """
    from app.db.models import Case, CaseDevice, DeviceCredential, FeishuCaseBinding, ReproductionSession
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
        # A device can have several independent incidents at the same time. Only
        # a reply in the same Feishu thread may reuse a Case; SN alone is never a
        # safe correlation key.
        source_context = source_context or {}
        thread_key = source_context.get('root_message_id') or source_context.get('message_id')
        correlated_case_id = source_context.get('correlated_case_id')
        case = db.get(Case, correlated_case_id) if correlated_case_id else None
        if case is not None and case.status in {'RESOLVED', 'CLOSED', 'FAILED'}:
            case = None
        if case is None and chat_id and thread_key:
            case = db.scalar(
                select(Case).join(FeishuCaseBinding, FeishuCaseBinding.case_id == Case.id)
                .where(
                    FeishuCaseBinding.receive_id == chat_id,
                    Case.status.in_(['NEW', 'ANALYZING']),
                    (FeishuCaseBinding.source_root_message_id == thread_key) |
                    (FeishuCaseBinding.source_message_id == thread_key),
                )
                .order_by(Case.created_at.desc()).limit(1)
            )
        if case is None:
            from app.services.cases import create_case
            summary = case_summary or f"飞书自动诊断 · {product or sn} · {sn}"
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
        else:
            # Refresh the device's address from device_credentials: a provision
            # may have opened a NEW tunnel endpoint (IP/port) for this SN, and the
            # reproduction platform connects using CaseDevice.ip/ssh_port. Stale
            # values (old tunnel) would make the real platform fail to connect.
            if ip and (device.ip != ip or (port and device.ssh_port != port)):
                device.ip = ip
                if port:
                    device.ssh_port = port
                db.flush()
        # Bind the Case to the source Feishu group so the conclusion card returns
        # to the SAME chat (even when different faults come from different groups).
        if chat_id:
            try:
                from app.integrations.feishu.service import bind_case_to_chat
                bind_case_to_chat(db, case_id=case.id, chat_id=chat_id, chat_type=chat_type,
                                  source_context=source_context)
                db.flush()
            except Exception:
                log.exception('bind case to feishu chat failed; provision continues')
        if not start_reproduction_session:
            # Evidence First: after device access is prepared, start the
            # deterministic diagnosis cycle. It will choose existing evidence or
            # an approved minimal read-only collection before reproduction.
            db.commit()
            from app.services.diagnosis import create_diagnosis_job
            from app.workers.diagnosis_tasks import run_diagnosis
            job, run = create_diagnosis_job(db, case_id=case.id)
            run_diagnosis.apply_async(args=[run.id], queue='diagnosis')
            return {'case_id': case.id, 'case_no': case.case_no,
                    'session_id': None, 'started': False,
                    'workflow': 'EVIDENCE_FIRST', 'diagnosis_run_id': run.id,
                    'diagnosis_job_id': job.id}
        # Legacy/direct callers may still explicitly request a reproduction
        # session. The Feishu Intent Router never uses this branch initially.
        orch = ReproductionOrchestrator(registry=ReproductionProfileRegistry())
        session = orch.create_session(db, case_id=case.id,
                                      profile_id='VOIP_GENERIC_FULL_CAPTURE',
                                      device_id=device.id, actor='feishu-provision')
        db.commit()
        start_reproduction.apply_async(args=[session.id], queue='reproduction-control')
        return {'case_id': case.id, 'case_no': case.case_no,
                'session_id': session.id, 'started': True}
    except Exception as exc:  # provision succeeded; autostart must not fail the provision
        log.exception('autostart reproduction failed')
        return {'case_id': None, 'session_id': None, 'started': False,
                'reason': f'{type(exc).__name__}:{exc}'}
    finally:
        db.close()


@celery_app.task(name='device.provision_from_feishu', bind=True, autoretry_for=(), max_retries=0)
def provision_from_feishu(self, text: str, chat_id: str | None = None, chat_type: str | None = None,
                          source_context: dict | None = None, evidence_first: bool = False):
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
        auto = _autostart_reproduction(sn=sn, product=req.product, chat_id=chat_id, chat_type=chat_type,
                                       source_context=source_context,
                                       start_reproduction_session=not evidence_first,
                                       case_summary=text[:1000] if text else None)
        return {"status": "OK", **result, "autostart": auto}

    try:
        response = asyncio.run(_run())
        message_id = (source_context or {}).get('message_id')
        from app.integrations.feishu.feedback import case_created_text, enqueue_reply
        auto = response.get('autostart') or {}
        if response.get('status') == 'OK' and auto.get('case_no'):
            enqueue_reply(message_id, case_created_text(auto['case_no']))
        elif response.get('status') == 'OK':
            # Provisioning the access path is not a successful intake unless a
            # diagnosable Case was also created. Do not leave the user with only
            # the earlier "accepted" acknowledgement in this partial-failure case.
            from app.integrations.feishu.feedback import failed_text
            enqueue_reply(message_id, failed_text())
        elif response.get('status') == 'MISSING_PARAMS':
            enqueue_reply(message_id, '设备信息仍不完整，请提供设备 URL，或 IP+SN。')
        return response
    except Exception as exc:
        log.exception('device provision failed')
        from app.integrations.feishu.feedback import enqueue_reply, failed_text
        enqueue_reply((source_context or {}).get('message_id'), failed_text())
        return {"status": "FAILED", "reason": f"{type(exc).__name__}:{exc}"}


@celery_app.task(name='feishu.reply_text', bind=True, autoretry_for=(), max_retries=0)
def reply_feishu_text(self, message_id: str, text: str):
    from app.core.config import settings
    from app.integrations.feishu.transport import FeishuLiveTransport
    if not settings.feishu_live_enabled:
        return {'status': 'SKIPPED', 'reason': 'FEISHU_LIVE_DISABLED'}
    try:
        result = asyncio.run(FeishuLiveTransport().reply_text(message_id=message_id, text=text))
        return {'status': 'SENT', 'message_id': result.message_id}
    except Exception as exc:
        log.exception('feishu intake reply failed message=%s', message_id)
        return {'status': 'FAILED', 'reason': f'{type(exc).__name__}:{exc}'}


@celery_app.task(name='feishu.ingest_follow_up', bind=True, autoretry_for=(), max_retries=0)
def ingest_feishu_follow_up(self, case_id: str, text: str, source_context: dict | None = None):
    """Persist a thread reply as immutable Evidence and resume a waiting diagnosis."""
    import hashlib

    from app.contracts.enums import EvidenceCompleteness, EvidenceKind, EvidenceLevel, EvidenceScope
    from app.core.ids import new_id
    from app.db.models import Case
    from app.db.session import SessionLocal
    from app.integrations.storage import ObjectStorage
    from app.services.evidence import create_evidence
    from app.services.idempotency import begin_idempotent, complete_idempotent

    source_context = source_context or {}
    message_id = str(source_context.get('message_id') or '')
    normalized = text.strip()
    if not normalized:
        return {'status': 'SKIPPED', 'reason': 'EMPTY_FOLLOW_UP'}
    db = SessionLocal()
    try:
        case = db.get(Case, case_id)
        if case is None:
            return {'status': 'FAILED', 'reason': 'CASE_NOT_FOUND', 'case_id': case_id}
        handle = begin_idempotent(
            db, scope='FEISHU_CASE_FOLLOW_UP', key=message_id or None,
            payload={'case_id': case_id, 'text': normalized},
        )
        if handle.replay is not None:
            return {**handle.replay, 'duplicate': True}

        data = normalized.encode('utf-8')
        digest = hashlib.sha256(data).hexdigest()
        evidence_id = new_id()
        object_key = f'cases/{case_id}/evidence/{evidence_id}/feishu-follow-up.txt'
        ObjectStorage().put_bytes(object_key, data, 'text/plain; charset=utf-8')
        evidence = create_evidence(
            db, evidence_id=evidence_id, case_id=case_id,
            evidence_type='USER_RESPONSE', source='FEISHU_USER_REPLY',
            filename='feishu-follow-up.txt', object_key=object_key,
            size_bytes=len(data), sha256=digest, kind=EvidenceKind.RAW,
            scope=EvidenceScope.CASE, level=EvidenceLevel.L1,
            completeness=EvidenceCompleteness.COMPLETE,
            content_type='text/plain; charset=utf-8', producer_type='FEISHU',
            producer_id=message_id or None, producer_version='feishu-message-v1',
            metadata={'text': normalized, 'source_message_id': message_id or None,
                      'source_root_message_id': source_context.get('root_message_id'),
                      'sender_open_id': source_context.get('sender_open_id')},
            actor='feishu-user',
        )
        # The deterministic reasoner already consumes Case.summary. Keeping a
        # bounded copy there makes common field answers useful immediately,
        # while the immutable Evidence remains the authoritative record.
        marker = f'\n[用户补充] {normalized}'
        if marker not in case.summary:
            case.summary = f'{case.summary[:1400]}{marker[:600]}'
        response = {'status': 'OK', 'case_id': case_id, 'evidence_id': evidence.id}
        complete_idempotent(
            db, handle, response=response, status_code=200,
            resource_type='evidence', resource_id=evidence.id,
        )
        db.commit()
        from app.workers.diagnosis_tasks import notify_case_changed
        notify_case_changed(case_id)
        return response
    except Exception as exc:
        db.rollback()
        log.exception('feishu follow-up ingest failed case=%s message=%s', case_id, message_id)
        return {'status': 'FAILED', 'reason': f'{type(exc).__name__}:{exc}',
                'case_id': case_id, 'message_id': message_id}
    finally:
        db.close()


def _attachment_evidence_type(filename: str, message_type: str = '', content_type: str = '') -> str:
    lower = filename.lower()
    content_type = content_type.lower()
    if lower.endswith('.pcapng'):
        return 'PCAPNG'
    if lower.endswith('.pcap'):
        return 'PCAP'
    if lower.endswith('.wav') or content_type in {'audio/wav', 'audio/x-wav'}:
        return 'FIELD_AUDIO_WAV'
    if lower.endswith(('.pcm', '.raw')):
        return 'FIELD_AUDIO_RAW'
    if message_type == 'audio' or content_type.startswith('audio/'):
        return 'FIELD_AUDIO'
    if message_type == 'image' or content_type.startswith('image/') or lower.endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp')):
        return 'FIELD_IMAGE'
    return 'USER_UPLOAD'


def _ingest_feishu_attachments(text: str, chat_id: str | None, chat_type: str | None,
                               source_context: dict | None, attachments: list[dict]):
    """Download Feishu resources, persist immutable Evidence, then diagnose.

    No device provisioning or reproduction is performed by this task.
    """
    import hashlib
    from pathlib import Path

    from app.contracts.enums import CaseStatus, DiagnosisRunStatus, EvidenceCompleteness, EvidenceKind, EvidenceLevel, EvidenceScope
    from app.core.ids import new_case_no, new_id
    from app.db.models import Case, CaseStateHistory, DiagnosisRun, Evidence, FeishuCaseBinding
    from app.db.session import SessionLocal
    from app.integrations.feishu.service import bind_case_to_chat
    from app.integrations.feishu.transport import FeishuLiveTransport
    from app.integrations.storage import ObjectStorage
    from app.services.audit import audit
    from app.services.evidence import create_evidence

    source_context = source_context or {}
    message_id = str(source_context.get('message_id') or '')
    thread_key = str(source_context.get('root_message_id') or
                     source_context.get('parent_message_id') or message_id)

    async def download_all():
        transport = FeishuLiveTransport()
        downloaded = []
        failed = []
        for ref in attachments:
            try:
                resource = await transport.download_message_resource(
                    message_id=message_id, file_key=str(ref.get('file_key') or ''),
                    resource_type=str(ref.get('resource_type') or 'file'),
                )
                downloaded.append((ref, resource))
            except Exception as exc:
                log.exception('feishu attachment download failed message=%s file=%s',
                              message_id, ref.get('filename'))
                failed.append({**ref, 'stage': 'DOWNLOAD',
                               'reason': f'{type(exc).__name__}:{exc}'})
        return downloaded, failed

    downloaded, failed_attachments = asyncio.run(download_all())
    if not downloaded:
        return {'status': 'FAILED', 'reason': 'ALL_ATTACHMENTS_FAILED',
                'failed_attachments': failed_attachments, 'message_id': message_id}

    with SessionLocal() as db:
        correlated_case_id = source_context.get('correlated_case_id')
        case = db.get(Case, correlated_case_id) if correlated_case_id else None
        if case is not None and case.status in {'RESOLVED', 'CLOSED', 'FAILED'}:
            case = None
        if case is None and chat_id and thread_key:
            case = db.scalar(
                select(Case).join(FeishuCaseBinding, FeishuCaseBinding.case_id == Case.id)
                .where(
                    FeishuCaseBinding.receive_id == chat_id,
                    ((FeishuCaseBinding.source_root_message_id == thread_key) |
                     (FeishuCaseBinding.source_message_id == thread_key)),
                ).order_by(Case.created_at.desc()).limit(1)
            )
        if case is None:
            filenames = ', '.join(str(x.get('filename') or 'attachment') for x in attachments[:3])
            case = Case(case_no=new_case_no(), summary=(text.strip() or f'飞书附件诊断：{filenames}')[:2000],
                        status=CaseStatus.NEW.value, created_by='feishu-attachment')
            db.add(case); db.flush()
            db.add(CaseStateHistory(case_id=case.id, from_status=None, to_status=CaseStatus.NEW.value,
                                    reason='feishu_attachment_case_created'))
            audit(db, case_id=case.id, actor='feishu-attachment', event_type='CASE_CREATED',
                  target_type='case', target_id=case.id,
                  detail={'source': 'FEISHU_ATTACHMENT', 'attachment_count': len(attachments)})
        if chat_id:
            bind_case_to_chat(db, case_id=case.id, chat_id=chat_id, chat_type=chat_type,
                              source_context=source_context)

        evidence_ids = []
        storage = ObjectStorage()
        for ref, resource in downloaded:
            filename = Path(str(ref.get('filename') or 'attachment.bin')).name
            existing = next((row for row in db.scalars(select(Evidence).where(
                Evidence.case_id == case.id, Evidence.producer_id == message_id,
            )) if (row.metadata_json or {}).get('file_key') == ref.get('file_key')), None)
            if existing:
                evidence_ids.append(existing.id)
                continue
            evidence_id = new_id()
            digest = hashlib.sha256(resource.data).hexdigest()
            object_key = f'cases/{case.id}/evidence/{evidence_id}/{filename}'
            try:
                with db.begin_nested():
                    storage.put_bytes(object_key, resource.data, resource.content_type)
                    row = create_evidence(
                        db, evidence_id=evidence_id, case_id=case.id,
                        evidence_type=_attachment_evidence_type(filename, str(ref.get('message_type') or ''), resource.content_type or ''), source='FEISHU_ATTACHMENT',
                        filename=filename, object_key=object_key, size_bytes=len(resource.data),
                        sha256=digest, content_type=resource.content_type,
                        kind=EvidenceKind.RAW, scope=EvidenceScope.CASE, level=EvidenceLevel.L1,
                        completeness=EvidenceCompleteness.COMPLETE, producer_type='FEISHU',
                        producer_id=message_id, producer_version='feishu-resource-v1',
                        metadata={'file_key': ref.get('file_key'), 'message_type': ref.get('message_type'),
                                  'source_message_id': message_id}, actor='feishu-attachment',
                    )
                evidence_ids.append(row.id)
            except Exception as exc:
                log.exception('feishu attachment persist failed message=%s file=%s',
                              message_id, filename)
                failed_attachments.append({**ref, 'filename': filename, 'stage': 'PERSIST',
                                           'reason': f'{type(exc).__name__}:{exc}'})
        if not evidence_ids:
            db.rollback()
            return {'status': 'FAILED', 'reason': 'ALL_ATTACHMENTS_FAILED',
                    'failed_attachments': failed_attachments, 'message_id': message_id}
        db.commit()

        active = db.scalar(select(DiagnosisRun).where(
            DiagnosisRun.case_id == case.id,
            DiagnosisRun.status.in_([
                DiagnosisRunStatus.PENDING.value, DiagnosisRunStatus.ANALYZING.value,
                DiagnosisRunStatus.WAITING_EVIDENCE.value, DiagnosisRunStatus.WAITING_USER.value,
            ]),
        ).order_by(DiagnosisRun.created_at.desc()).limit(1))
        if active:
            from app.workers.diagnosis_tasks import notify_case_changed
            notify_case_changed(case.id)
            run_id = active.id
        else:
            from app.services.diagnosis import create_diagnosis_job
            from app.workers.diagnosis_tasks import run_diagnosis
            _job, run = create_diagnosis_job(db, case_id=case.id)
            run_diagnosis.apply_async(args=[run.id], queue='diagnosis')
            run_id = run.id
        return {'status': 'PARTIAL_SUCCESS' if failed_attachments else 'OK',
                'case_id': case.id, 'case_no': case.case_no,
                'evidence_ids': evidence_ids, 'diagnosis_run_id': run_id,
                'failed_attachments': failed_attachments, 'reproduction_started': False}


@celery_app.task(name='feishu.ingest_attachments', bind=True, autoretry_for=(), max_retries=0)
def ingest_feishu_attachments(self, text: str, chat_id: str | None, chat_type: str | None,
                              source_context: dict | None, attachments: list[dict]):
    from app.integrations.feishu.feedback import (
        attachment_failed_text, attachment_ready_text, enqueue_reply,
    )
    message_id = (source_context or {}).get('message_id')
    try:
        result = _ingest_feishu_attachments(
            text, chat_id, chat_type, source_context, attachments
        )
        if result.get('status') in {'OK', 'PARTIAL_SUCCESS'}:
            enqueue_reply(message_id, attachment_ready_text(
                result['case_no'], len(result.get('evidence_ids') or []),
                result.get('failed_attachments'),
            ))
        else:
            enqueue_reply(message_id, attachment_failed_text(result.get('failed_attachments')))
        return result
    except Exception as exc:
        log.exception('feishu attachment ingest failed message=%s', message_id)
        enqueue_reply(message_id, attachment_failed_text())
        return {'status': 'FAILED', 'reason': f'{type(exc).__name__}:{exc}',
                'message_id': message_id}
