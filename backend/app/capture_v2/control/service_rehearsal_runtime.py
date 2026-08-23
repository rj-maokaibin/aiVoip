from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone

from sqlalchemy import func, select

from app.capture_v2.db_models import CaptureEpoch, CaptureLease, CaptureSegment, CaptureSession
from app.capture_v2.producer.identity import parse_process_record
from app.capture_v2.transport.readonly import ReadOnlyDeviceTransport
from app.contracts.enums import CleanupStatus, ReproductionState
from app.core.ids import new_id
from app.db.models import Case, CaseDevice, DeviceDiagnosticLock, ReproductionSession
from app.db.session import SessionLocal
from app.reproduction.orchestrator import ReproductionOrchestrator


def _now_tag() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def prepare_case(args) -> dict:
    case_id = new_id()
    device_id = new_id()
    with SessionLocal() as db:
        case = Case(
            id=case_id,
            case_no=f"CAPV2-R7-{args.sn}-{_now_tag()}-{case_id[:8]}",
            summary="Capture V2 bounded service-level activation rehearsal",
            created_by="capture-v2-service-rehearsal",
        )
        device = CaseDevice(
            id=device_id,
            case_id=case_id,
            ip=args.host,
            ssh_port=int(args.port),
            sn=args.sn,
            username=args.username,
            platform_id=args.platform_id,
            device_info={
                "model": args.model,
                "product_model": args.model,
                "platform": args.platform_id,
                "soc": args.platform_id,
                "validation_scope": "CAPTURE_V2_SERVICE_REHEARSAL",
            },
        )
        db.add(case)
        db.add(device)
        db.flush()
        session = ReproductionOrchestrator().create_session(
            db,
            case_id=case_id,
            profile_id=args.profile_id,
            device_id=device_id,
            actor="capture-v2-service-rehearsal",
        )
        session_id = session.id
        db.commit()
    return {
        "case_id": case_id,
        "device_id": device_id,
        "session_id": session_id,
        "profile_id": args.profile_id,
        "sn": args.sn,
        "model": args.model,
    }


def clone_session(args) -> dict:
    with SessionLocal() as db:
        source = db.get(ReproductionSession, args.source_session_id)
        if source is None:
            raise RuntimeError("SOURCE_REPRODUCTION_SESSION_NOT_FOUND")
        session = ReproductionOrchestrator().create_session(
            db,
            case_id=source.case_id,
            profile_id=source.profile_key,
            device_id=source.device_id,
            actor="capture-v2-service-rehearsal-v1-health",
            retry_parent_session_id=source.id,
        )
        session_id = session.id
        db.commit()
    return {"session_id": session_id, "source_session_id": args.source_session_id}


def enqueue_start(args) -> dict:
    from app.workers.reproduction_tasks import start_reproduction
    result = start_reproduction.apply_async(args=[args.session_id], queue="reproduction-control")
    return {"queued": True, "task_id": result.id, "session_id": args.session_id, "operation": "start"}


def enqueue_cancel(args) -> dict:
    from app.workers.reproduction_tasks import cancel_reproduction
    result = cancel_reproduction.apply_async(args=[args.session_id], queue="reproduction-control-high")
    return {"queued": True, "task_id": result.id, "session_id": args.session_id, "operation": "cancel"}


def _db_status(session_id: str) -> dict:
    with SessionLocal() as db:
        session = db.get(ReproductionSession, session_id)
        if session is None:
            return {"exists": False, "session_id": session_id}
        capture = db.scalar(select(CaptureSession).where(
            CaptureSession.reproduction_session_id == session_id
        ))
        lock = db.scalar(select(DeviceDiagnosticLock).where(
            DeviceDiagnosticLock.session_id == session_id
        ))
        payload = {
            "exists": True,
            "session_id": session_id,
            "case_id": session.case_id,
            "device_id": session.device_id,
            "state": session.state,
            "cleanup_status": session.cleanup_status,
            "terminal_reason": session.terminal_reason,
            "business_lock_status": lock.status if lock else None,
            "capture": None,
        }
        if capture is None:
            return payload
        lease = db.get(CaptureLease, capture.device_id)
        epochs = list(db.scalars(select(CaptureEpoch).where(
            CaptureEpoch.capture_session_id == capture.id
        ).order_by(CaptureEpoch.epoch_index)))
        segment_rows = db.execute(
            select(CaptureSegment.state, func.count(CaptureSegment.id))
            .where(CaptureSegment.capture_session_id == capture.id)
            .group_by(CaptureSegment.state)
        ).all()
        payload["capture"] = {
            "capture_session_id": capture.id,
            "state": capture.state,
            "health_status": capture.health_status,
            "path_ready": capture.path_ready_at is not None,
            "evidence_durable": capture.evidence_durable_at is not None,
            "ended": capture.ended_at is not None,
            "cleanup_status": capture.cleanup_status,
            "lease": {
                "state": lease.state if lease else None,
                "lease_epoch": int(lease.lease_epoch) if lease else None,
                "owner_worker_id": lease.owner_worker_id if lease else None,
            },
            "epochs": [
                {
                    "id": epoch.id,
                    "state": epoch.state,
                    "producer_pid": epoch.producer_pid,
                    "ended": epoch.ended_at is not None,
                    "kernel_drops": epoch.packets_dropped_kernel,
                }
                for epoch in epochs
            ],
            "segments_by_state": {str(state): int(count) for state, count in segment_rows},
        }
        return payload


async def _producer_status(session_id: str) -> dict:
    from app.workers.reproduction_tasks import _build_real_adapter
    with SessionLocal() as db:
        session = db.get(ReproductionSession, session_id)
        if session is None:
            return {"producer_count": None, "error": "SESSION_NOT_FOUND"}
    adapter = _build_real_adapter(session)
    await adapter.connect()
    try:
        records = await ReadOnlyDeviceTransport(adapter).list_tcpdump_processes()
        identities = [parse_process_record(row.pid, row.starttime, row.cmdline) for row in records]
        owned = [item for item in identities if item.owned_by_aivoip]
        return {
            "producer_count": len(owned),
            "producers": [
                {
                    "pid": item.pid,
                    "capture_epoch": item.capture_epoch,
                    "interface": item.interface,
                    "session_id": item.session_id,
                }
                for item in owned
            ],
        }
    finally:
        await adapter.disconnect()


def status(args) -> dict:
    payload = _db_status(args.session_id)
    if args.with_producer and payload.get("exists"):
        payload["dut"] = asyncio.run(_producer_status(args.session_id))
    return payload


def health_contract(args) -> dict:
    from app.capture_v2.runtime import assert_v1_live_capture_allowed, assert_selected_v2_live_capture_allowed
    from app.core.config import settings
    mode = str(args.expect).upper()
    if mode == "V1":
        assert_v1_live_capture_allowed()
    elif mode == "V2_REHEARSAL":
        selected = assert_selected_v2_live_capture_allowed()
        if selected.get("mode") != "ACTIVATION_REHEARSAL":
            raise RuntimeError("V2_REHEARSAL_CONTRACT_NOT_SELECTED")
    else:
        raise RuntimeError("EXPECT_MODE_INVALID")
    return {
        "contract_ok": True,
        "expected": mode,
        "capture_engine_version": settings.capture_engine_version,
        "capture_v2_production_enabled": bool(settings.capture_v2_production_enabled),
        "reproduction_platform_mode": settings.reproduction_platform_mode,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture V2 service rehearsal in-container probe")
    sub = parser.add_subparsers(dest="cmd", required=True)

    prepare = sub.add_parser("prepare")
    prepare.add_argument("--sn", required=True)
    prepare.add_argument("--host", required=True)
    prepare.add_argument("--port", required=True, type=int)
    prepare.add_argument("--username", required=True)
    prepare.add_argument("--platform-id", required=True)
    prepare.add_argument("--model", required=True)
    prepare.add_argument("--profile-id", default="VOIP_GENERIC_FULL_CAPTURE")

    clone = sub.add_parser("clone-session")
    clone.add_argument("--source-session-id", required=True)

    start = sub.add_parser("enqueue-start")
    start.add_argument("--session-id", required=True)

    cancel = sub.add_parser("enqueue-cancel")
    cancel.add_argument("--session-id", required=True)

    stat = sub.add_parser("status")
    stat.add_argument("--session-id", required=True)
    stat.add_argument("--with-producer", action="store_true")

    contract = sub.add_parser("health-contract")
    contract.add_argument("--expect", choices=["V1", "V2_REHEARSAL"], required=True)

    args = parser.parse_args(argv)
    if args.cmd == "prepare":
        payload = prepare_case(args)
    elif args.cmd == "clone-session":
        payload = clone_session(args)
    elif args.cmd == "enqueue-start":
        payload = enqueue_start(args)
    elif args.cmd == "enqueue-cancel":
        payload = enqueue_cancel(args)
    elif args.cmd == "status":
        payload = status(args)
    else:
        payload = health_contract(args)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
