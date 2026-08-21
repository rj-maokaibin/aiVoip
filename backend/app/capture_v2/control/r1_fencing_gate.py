from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from app.capture_v2.db_models import CaptureLease
from app.capture_v2.enums import CaptureLeaseState
from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.gate.evidence import GateEvidenceCollector
from app.capture_v2.gate.models import GateCaseResult, GateCheck, GateRunPaths, GateVerdict
from app.capture_v2.lease.manager import CaptureLeaseManager
from app.db.session import SessionLocal


def _emit(payload) -> None:
    if hasattr(payload, "as_dict"):
        payload = payload.as_dict()
    print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))


def _parse_expiry(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _acquire_when_available(manager: CaptureLeaseManager, *, device_id: str,
                            capture_session_id: str, worker_id: str):
    while True:
        try:
            return manager.acquire(
                device_id=device_id,
                capture_session_id=capture_session_id,
                owner_worker_id=worker_id,
            )
        except CaptureV2Error as exc:
            if exc.code != "LEASE_BUSY":
                raise
            expiry = _parse_expiry((exc.details or {}).get("expires_at"))
            now = datetime.now(timezone.utc)
            delay = 1.0 if expiry is None else max(0.25, (expiry - now).total_seconds() + 0.25)
            time.sleep(min(delay, 35.0))


def _expect_fenced(fn) -> str:
    try:
        fn()
    except CaptureV2Error as exc:
        return exc.code
    return "NO_ERROR"


def run(args) -> GateCaseResult:
    ttl = float(args.ttl_seconds)
    if ttl < 10.0 or ttl > 60.0:
        raise ValueError("TTL_SECONDS_OUT_OF_RANGE")
    manager = CaptureLeaseManager(SessionLocal, ttl_seconds=ttl)
    token_b = None
    try:
        token_a = _acquire_when_available(
            manager,
            device_id=args.device_id,
            capture_session_id=args.capture_session_a,
            worker_id=args.worker_a,
        )
        # Use real wall-clock expiry. No synthetic time and no DB mutation bypass.
        time.sleep(ttl + 0.5)
        token_b = manager.acquire(
            device_id=args.device_id,
            capture_session_id=args.capture_session_b,
            owner_worker_id=args.worker_b,
        )
        stale_renew = _expect_fenced(lambda: manager.renew(token_a))
        stale_validate = _expect_fenced(lambda: manager.validate(token_a))
        stale_release = _expect_fenced(lambda: manager.release(token_a))

        with SessionLocal() as db:
            row = db.get(CaptureLease, args.device_id)
            row_fact = None if row is None else {
                "device_id": row.device_id,
                "capture_session_id": row.capture_session_id,
                "owner_worker_id": row.owner_worker_id,
                "lease_epoch": int(row.lease_epoch),
                "state": row.state,
            }

        facts = {
            "before_lease_epoch": token_a.lease_epoch,
            "after_lease_epoch": token_b.lease_epoch,
            "stale_renew_code": stale_renew,
            "stale_validate_code": stale_validate,
            "stale_release_code": stale_release,
            "current_lease": row_fact,
            "ttl_seconds": ttl,
        }
        checks = (
            GateCheck(
                "takeover_increments_epoch",
                token_b.lease_epoch == token_a.lease_epoch + 1,
                token_a.lease_epoch + 1,
                token_b.lease_epoch,
            ),
            GateCheck("stale_renew_fenced", stale_renew == "LEASE_FENCED", "LEASE_FENCED", stale_renew),
            GateCheck("stale_validate_fenced", stale_validate == "LEASE_FENCED", "LEASE_FENCED", stale_validate),
            GateCheck("stale_release_fenced", stale_release == "LEASE_FENCED", "LEASE_FENCED", stale_release),
            GateCheck(
                "new_owner_remains_active",
                bool(row_fact)
                and row_fact.get("state") == CaptureLeaseState.ACTIVE.value
                and row_fact.get("lease_epoch") == token_b.lease_epoch
                and row_fact.get("capture_session_id") == token_b.capture_session_id
                and row_fact.get("owner_worker_id") == token_b.owner_worker_id,
                {
                    "state": CaptureLeaseState.ACTIVE.value,
                    "lease_epoch": token_b.lease_epoch,
                    "capture_session_id": token_b.capture_session_id,
                    "owner_worker_id": token_b.owner_worker_id,
                },
                row_fact,
            ),
        )
        verdict = GateVerdict.PASS if all(check.passed is True for check in checks) else GateVerdict.FAIL

        paths = GateRunPaths.create(Path(args.output_root), args.gate_id, args.device_id)
        collector = GateEvidenceCollector(
            session_factory=SessionLocal,
            adapter=None,
            object_root=Path(args.object_root) if args.object_root else None,
            repo_root=Path(args.repo_root) if args.repo_root else None,
        )
        asyncio.run(collector.collect(
            paths=paths,
            gate_id=args.gate_id,
            capture_session_id=token_b.capture_session_id,
            device_id=args.device_id,
            facts=facts,
        ))
        return GateCaseResult(
            gate_id=args.gate_id,
            verdict=verdict,
            checks=checks,
            summary="PostgreSQL expired takeover and stale-token fencing",
            evidence_bundle=str(paths.case_dir),
            facts=facts,
        )
    finally:
        if token_b is not None:
            try:
                manager.release(token_b)
            except CaptureV2Error:
                pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Capture V2 R1 expired takeover + stale fencing real PostgreSQL gate")
    p.add_argument("--device-id", required=True)
    p.add_argument("--capture-session-a", required=True)
    p.add_argument("--capture-session-b", required=True)
    p.add_argument("--worker-a", default="gate-r1-stale-a")
    p.add_argument("--worker-b", default="gate-r1-stale-b")
    p.add_argument("--gate-id", default="R1-02")
    p.add_argument("--ttl-seconds", type=float, default=10.0)
    p.add_argument("--object-root", default="")
    p.add_argument("--output-root", default="/tmp/capture-v2-gates")
    p.add_argument("--repo-root", default="")
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        result = run(build_parser().parse_args(argv))
        _emit(result)
        return 0 if result.verdict == GateVerdict.PASS else 2
    except CaptureV2Error as exc:
        _emit({"ok": False, "error": exc.code, "details": exc.details})
        return 2
    except Exception as exc:
        _emit({"ok": False, "error": f"R1_FENCING_GATE_ERROR:{type(exc).__name__}", "details": {"message": str(exc)}})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
