from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.gate.context import build_asyncssh_adapter
from app.capture_v2.gate.evidence import GateEvidenceCollector
from app.capture_v2.gate.evaluator import GateEvaluator
from app.capture_v2.gate.faults import GateFaultInjector, GateFaultPlan
from app.capture_v2.gate.models import GateDeviceSpec, GateRunPaths
from app.capture_v2.gate.runner import GateRunner
from app.core.config import settings
from app.db.session import SessionLocal


def _json(payload) -> None:
    if hasattr(payload, "as_dict"):
        payload = payload.as_dict()
    print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))


def _device(args) -> GateDeviceSpec:
    return GateDeviceSpec(
        device_id=args.device_id,
        model=args.model,
        host=args.host,
        port=args.port,
        username=args.username,
        platform_id=args.platform_id,
    )


def _common_device(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--device-id", required=True, help="Existing case_devices.id")
    parser.add_argument("--model", required=True, help="e.g. APF1250 or APF3260-M")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--username", default=getattr(settings, "ssh_username", "admin"))
    parser.add_argument("--platform-id", choices=["mt7621", "mt7981"])
    parser.add_argument("--password-env", default="CAPTURE_GATE_SSH_PASSWORD")


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


def _base_paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile-root", default=str(getattr(settings, "profile_root", "/app/profiles")))
    parser.add_argument("--profile-id", default=getattr(settings, "capture_v2_profile_id", "voip-standard"))
    parser.add_argument("--object-root", default=str(getattr(settings, "reproduction_object_root", "/tmp/voip-reproduction-objects")))
    parser.add_argument("--output-root", default="/tmp/capture-v2-gates")
    parser.add_argument("--repo-root", default="")


async def _cmd_ownership(args) -> int:
    spec = _device(args)
    adapter = build_asyncssh_adapter(spec, password_env=args.password_env)
    await adapter.connect()
    try:
        result, facts = await _runner(adapter, args).ownership_establish(
            reproduction_session_id=args.reproduction_session_id,
            device=spec.as_profile_device(),
            worker_id=args.worker_id,
            gate_id=args.gate_id,
        )
        _json(result)
        if args.state_file:
            Path(args.state_file).write_text(json.dumps(facts, indent=2) + "\n", encoding="utf-8")
        if args.hold_seconds > 0:
            await asyncio.sleep(args.hold_seconds)
        return 0 if result.verdict.value == "PASS" else 2
    finally:
        await adapter.disconnect()


async def _cmd_ownership_adopt(args) -> int:
    spec = _device(args)
    before = json.loads(Path(args.before_state).read_text(encoding="utf-8"))
    adapter = build_asyncssh_adapter(spec, password_env=args.password_env)
    await adapter.connect()
    try:
        result = await _runner(adapter, args).ownership_adopt(
            reproduction_session_id=args.reproduction_session_id,
            device=spec.as_profile_device(), worker_id=args.worker_id,
            before_state=before, gate_id=args.gate_id,
        )
        _json(result)
        return 0 if result.verdict.value == "PASS" else 2
    finally:
        await adapter.disconnect()


async def _cmd_segment(args) -> int:
    spec = _device(args)
    adapter = build_asyncssh_adapter(spec, password_env=args.password_env)
    await adapter.connect()
    try:
        plan = GateFaultPlan.load(Path(args.fault_plan) if args.fault_plan else None)
        result = await _runner(adapter, args).segment_normal(
            reproduction_session_id=args.reproduction_session_id,
            device=spec.as_profile_device(), worker_id=args.worker_id,
            duration_seconds=args.duration, cycle_interval_seconds=args.interval,
            gate_id=args.gate_id, fault_plan=plan,
            transport=args.transport,
        )
        _json(result)
        return 0 if result.verdict.value == "PASS" else 2
    finally:
        await adapter.disconnect()


async def _cmd_collect(args) -> int:
    spec = _device(args)
    adapter = build_asyncssh_adapter(spec, password_env=args.password_env)
    await adapter.connect()
    try:
        paths = GateRunPaths.create(Path(args.output_root), args.gate_id, spec.device_id)
        collector = GateEvidenceCollector(
            session_factory=SessionLocal, adapter=adapter,
            object_root=Path(args.object_root), repo_root=Path(args.repo_root) if args.repo_root else None,
        )
        bundle = await collector.collect(
            paths=paths, gate_id=args.gate_id,
            capture_session_id=args.capture_session_id,
            device_id=spec.device_id,
        )
        _json({"bundle": str(bundle)})
        return 0
    finally:
        await adapter.disconnect()


def _cmd_evaluate(args) -> int:
    result = GateEvaluator(Path(args.bundle)).evaluate(args.gate_id)
    _json(result)
    return 0 if result.verdict.value == "PASS" else 2


def _cmd_fault(args) -> int:
    injector = GateFaultInjector(
        store_root=Path(args.store_root) if args.store_root else None,
        quarantine_root=Path(args.quarantine_root),
    )
    if args.fault_action in {"kill", "term", "pause", "resume"}:
        injector.signal_worker(args.pid, args.fault_action)
        _json({"action": args.fault_action, "pid": args.pid, "ok": True})
        return 0
    if args.fault_action == "quarantine-copy":
        _json(injector.quarantine_server_copy(Path(args.path)))
        return 0
    if args.fault_action == "restore-copy":
        _json(injector.restore_quarantined(args.token))
        return 0
    raise CaptureV2Error("GATE_FAULT_ACTION_INVALID")


def _cmd_lease_race(args) -> int:
    # No DUT connection is required; this Gate must point at the real PostgreSQL DB.
    runner = GateRunner(
        session_factory=SessionLocal, adapter=None,
        profile_root=Path(args.profile_root), requested_profile_id=args.profile_id,
        object_root=Path(args.object_root), repo_root=Path(args.repo_root) if args.repo_root else None,
        output_root=Path(args.output_root),
    )
    result = runner.postgres_lease_race(
        device_id=args.device_id,
        capture_session_a=args.capture_session_a,
        capture_session_b=args.capture_session_b,
        worker_a=args.worker_a,
        worker_b=args.worker_b,
        gate_id=args.gate_id,
    )
    _json(result)
    return 0 if result.verdict.value == "PASS" else 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="capture-v2-gate", description="Capture Engine V2.1.1 real Gate CLI")
    sub = p.add_subparsers(dest="command", required=True)

    own = sub.add_parser("ownership", help="Establish A/B ownership and collect evidence")
    _common_device(own); _base_paths(own)
    own.add_argument("--reproduction-session-id", required=True)
    own.add_argument("--worker-id", required=True)
    own.add_argument("--gate-id", default="R2-ESTABLISH")
    own.add_argument("--state-file", default="")
    own.add_argument("--hold-seconds", type=float, default=0.0)

    adopt = sub.add_parser("ownership-adopt", help="R2-01 takeover and compare against a pre-crash state file")
    _common_device(adopt); _base_paths(adopt)
    adopt.add_argument("--reproduction-session-id", required=True)
    adopt.add_argument("--worker-id", required=True)
    adopt.add_argument("--before-state", required=True)
    adopt.add_argument("--gate-id", default="R2-01")

    seg = sub.add_parser("segment", help="Run C reliable Segment/SFTP/ACK Gate")
    _common_device(seg); _base_paths(seg)
    seg.add_argument("--reproduction-session-id", required=True)
    seg.add_argument("--worker-id", required=True)
    seg.add_argument("--gate-id", default="R3-01")
    seg.add_argument("--duration", type=float, default=30.0)
    seg.add_argument("--interval", type=float, default=0.5)
    seg.add_argument("--fault-plan", default="", help="Gate-only JSON failpoint plan")
    seg.add_argument("--transport", choices=["sftp", "scp"], default="sftp",
                    help="Exact download transport (scp for Dropbear without SFTP subsystem)")

    collect = sub.add_parser("collect", help="Collect immutable Gate evidence bundle")
    _common_device(collect); _base_paths(collect)
    collect.add_argument("--capture-session-id", required=True)
    collect.add_argument("--gate-id", required=True)

    ev = sub.add_parser("evaluate", help="Evaluate an evidence bundle")
    ev.add_argument("--bundle", required=True)
    ev.add_argument("--gate-id", required=True)

    race = sub.add_parser("lease-race", help="R1 real PostgreSQL first-acquire race")
    _base_paths(race)
    race.add_argument("--device-id", required=True)
    race.add_argument("--capture-session-a", required=True)
    race.add_argument("--capture-session-b", required=True)
    race.add_argument("--worker-a", default="gate-r1-a")
    race.add_argument("--worker-b", default="gate-r1-b")
    race.add_argument("--gate-id", default="R1-01")

    fault = sub.add_parser("fault", help="Explicit reversible Gate fault operations")
    fault.add_argument("fault_action", choices=["kill", "term", "pause", "resume", "quarantine-copy", "restore-copy"])
    fault.add_argument("--pid", type=int, default=0)
    fault.add_argument("--path", default="")
    fault.add_argument("--token", default="")
    fault.add_argument("--store-root", default=str(getattr(settings, "reproduction_object_root", "/tmp/voip-reproduction-objects")))
    fault.add_argument("--quarantine-root", default="/tmp/capture-v2-gate-quarantine")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "ownership":
            return asyncio.run(_cmd_ownership(args))
        if args.command == "ownership-adopt":
            return asyncio.run(_cmd_ownership_adopt(args))
        if args.command == "segment":
            return asyncio.run(_cmd_segment(args))
        if args.command == "collect":
            return asyncio.run(_cmd_collect(args))
        if args.command == "evaluate":
            return _cmd_evaluate(args)
        if args.command == "fault":
            return _cmd_fault(args)
        if args.command == "lease-race":
            return _cmd_lease_race(args)
        raise CaptureV2Error("GATE_COMMAND_INVALID")
    except CaptureV2Error as exc:
        _json({"ok": False, "error": exc.code, "details": exc.details})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
