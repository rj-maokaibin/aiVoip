from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from app.capture_v2.gate.cli import _device, _resolve_reproduction_session_id
from app.capture_v2.gate.context import build_asyncssh_adapter
from app.capture_v2.gate.r4_preflight import run_r4_no_handset_preflight
from app.capture_v2.gate.r4_real import run_r4_real_fxs_basic
from app.capture_v2.gate.r7_soak import run_r7_validation_soak
from app.capture_v2.gate.runner import GateRunner
from app.core.config import settings
from app.db.session import SessionLocal


def _json(payload) -> None:
    if hasattr(payload, "as_dict"):
        payload = payload.as_dict()
    print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))


def _runner(adapter, args) -> GateRunner:
    return GateRunner(
        session_factory=SessionLocal,
        adapter=adapter,
        profile_root=Path(args.profile_root),
        requested_profile_id=args.profile_id,
        object_root=Path(args.object_root),
        repo_root=Path(args.repo_root) if args.repo_root else None,
        output_root=Path(args.output_root),
    )


async def _run(args) -> int:
    spec = _device(args)
    reproduction_session_id = _resolve_reproduction_session_id(
        args.reproduction_session_id,
        device_id=spec.device_id,
    )
    adapter = build_asyncssh_adapter(spec, password_env=args.password_env)
    await adapter.connect()
    try:
        runner = _runner(adapter, args)
        normal_gate = str(args.gate_id).upper().replace("_", "-")
        if normal_gate.startswith("R7-00"):
            result = await run_r7_validation_soak(
                runner,
                reproduction_session_id=reproduction_session_id,
                device=spec.as_profile_device(),
                worker_id=args.worker_id,
                gate_id=args.gate_id,
                duration_seconds=args.duration,
                cycle_interval_seconds=0.5,
                transport=args.transport,
            )
        elif normal_gate.startswith("R4-00"):
            result = await run_r4_no_handset_preflight(
                runner,
                reproduction_session_id=reproduction_session_id,
                device=spec.as_profile_device(),
                worker_id=args.worker_id,
                gate_id=args.gate_id,
                duration_seconds=args.duration,
                transport=args.transport,
            )
        else:
            result = await run_r4_real_fxs_basic(
                runner,
                reproduction_session_id=reproduction_session_id,
                device=spec.as_profile_device(),
                worker_id=args.worker_id,
                gate_id=args.gate_id,
                duration_seconds=args.duration,
                transport=args.transport,
            )
        _json(result)
        return 0 if result.verdict.value == "PASS" else 2
    finally:
        await adapter.disconnect()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="capture-v2-r4-real")
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--username", default=getattr(settings, "ssh_username", "admin"))
    parser.add_argument("--platform-id", choices=["mt7621", "mt7981"])
    parser.add_argument("--password-env", default="CAPTURE_GATE_SSH_PASSWORD")
    parser.add_argument("--profile-root", default=str(getattr(settings, "profile_root", "/app/profiles")))
    parser.add_argument("--profile-id", default=getattr(settings, "capture_v2_profile_id", "voip-standard"))
    parser.add_argument("--object-root", default=str(getattr(settings, "reproduction_object_root", "/tmp/voip-reproduction-objects")))
    parser.add_argument("--output-root", default="/tmp/capture-v2-gates")
    parser.add_argument("--repo-root", default="")
    parser.add_argument("--reproduction-session-id", required=True, help="Existing UUID or AUTO_NEW")
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--gate-id", default="R4-01-REAL-FXS-BASIC")
    parser.add_argument("--duration", type=float, default=90.0)
    parser.add_argument("--transport", choices=["sftp", "scp"], default="scp")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
