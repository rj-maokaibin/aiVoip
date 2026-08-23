from __future__ import annotations

import argparse
import json

from sqlalchemy import select

from app.capture_v2.db_models import CaptureLease, CaptureSession
from app.db.models import ArmValidationResult, ReproductionSession
from app.db.session import SessionLocal


def snapshot(session_id: str) -> dict:
    with SessionLocal() as db:
        session = db.get(ReproductionSession, session_id)
        if session is None:
            return {"exists": False, "session_id": session_id}

        capture = db.scalar(
            select(CaptureSession).where(CaptureSession.reproduction_session_id == session_id)
        )
        arm = db.scalar(
            select(ArmValidationResult)
            .where(ArmValidationResult.session_id == session_id)
            .order_by(ArmValidationResult.validation_no.desc())
            .limit(1)
        )
        lease = db.get(CaptureLease, capture.device_id) if capture is not None else None

        return {
            "exists": True,
            "session_id": session.id,
            "state": session.state,
            "terminal_reason": session.terminal_reason,
            "terminal_detail": session.terminal_detail_json or {},
            "cleanup_status": session.cleanup_status,
            "capture": None if capture is None else {
                "capture_session_id": capture.id,
                "state": capture.state,
                "health_status": capture.health_status,
                "path_ready": capture.path_ready_at is not None,
                "evidence_durable": capture.evidence_durable_at is not None,
                "cleanup_status": capture.cleanup_status,
                "lease_state": lease.state if lease else None,
                "lease_epoch": int(lease.lease_epoch) if lease else None,
            },
            "latest_arm_validation": None if arm is None else {
                "validation_no": int(arm.validation_no),
                "status": arm.status,
                "required_channels": arm.required_channels_json or [],
                "failed_reasons": arm.failed_reasons_json or [],
                "observed_channels": arm.observed_channels_json or {},
            },
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture V2 service rehearsal DB diagnostics")
    parser.add_argument("--session-id", required=True)
    args = parser.parse_args(argv)
    print(json.dumps(snapshot(args.session_id), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
