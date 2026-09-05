#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from app.automation.gates.g0_recovery import G0RecoveryMarkerStore
from app.automation.gates.golden_cfg_config import GOLDEN_CFG_CONFIG_CASE_ID, G0_MODULE, G0_ROUTE, GoldenCfgConfigGate
from app.automation.gates.runtime_binding import finalize_authority_bound_run, prepare_authority_bound_run, record_authority_ref
from app.automation.registry import TestRegistry
from app.capture_v2.gate.context import build_asyncssh_adapter
from app.capture_v2.gate.models import GateDeviceSpec
from app.capture_v2.lease.manager import CaptureLeaseManager
from app.db.session import SessionLocal
from app.infrastructure.config_framework.executor import ConfigFrameworkExecutor
from app.infrastructure.device_authority.capture_lease_adapter import CaptureLeaseCompatibilityAdapter
from app.infrastructure.transport.ssh import SharedSshTransport


def _summary(result, gate: GoldenCfgConfigGate, *, device_id: str, model: str) -> dict:
    token = gate.runtime.get("token")
    recovery_retained = bool(
        gate.recovery_store is not None
        and gate.recovery_store.retained(run_id=gate.run_id)
    )
    return {
        "gate": GOLDEN_CFG_CONFIG_CASE_ID,
        "run_id": gate.run_id,
        "device_id": device_id,
        "model": model,
        "verdict": result.verdict.value,
        "state": result.state.value,
        "terminal_reason": result.terminal_reason,
        "route": G0_ROUTE.as_dict(),
        "probe": {
            "field": "disName",
            "value": gate.runtime.get("marker"),
            "identity_fields_changed": False,
        },
        "snapshot": {
            "module": G0_MODULE,
            "captured": gate.runtime.get("snapshot") is not None,
            "secret_values_persisted": False,
        },
        "recovery": {
            "marker_written": bool(gate.runtime.get("recovery_marker_written")),
            "marker_retained": recovery_retained,
            "secret_values_persisted": False,
            "scope": "voipUserInfo.disName",
        },
        "authority": {
            "type": "CaptureLeaseManager",
            "lease_epoch": getattr(token, "lease_epoch", None),
            "release_last": True,
        },
        "assertions": [
            {
                "id": item.assertion_id,
                "verdict": item.verdict.value,
                "source": item.source,
                "path": item.path,
                "expected": item.expected,
                "actual": item.actual,
                "reason": item.reason,
                "evidence_refs": list(item.evidence_refs),
                "route": item.route,
            }
            for item in result.assertions.results
        ],
        "state_history": [state.value for state in result.state_history],
    }


async def _run(args) -> tuple[int, dict]:
    if not args.allow_live_mutation or os.getenv("REAL_LIVE_MUTATION") != "EXPLICIT_ONLY":
        raise RuntimeError("G0_LIVE_MUTATION_NOT_EXPLICITLY_AUTHORIZED")

    definition = TestRegistry(Path(args.profile_root) / "tests").definition(GOLDEN_CFG_CONFIG_CASE_ID)
    run_id, reproduction_session_id = prepare_authority_bound_run(
        session_factory=SessionLocal,
        device_id=args.device_id,
        worker_id=args.worker_id,
        definition=definition,
    )
    spec = GateDeviceSpec(
        device_id=args.device_id,
        model=args.model,
        host=args.host,
        port=args.port,
        username=args.username,
        platform_id=args.platform_id,
    )
    adapter = build_asyncssh_adapter(spec, password_env=args.password_env)
    authority = CaptureLeaseCompatibilityAdapter(CaptureLeaseManager(SessionLocal, ttl_seconds=120.0))
    config = ConfigFrameworkExecutor(
        SharedSshTransport(adapter),
        allowed_modules=(G0_MODULE,),
        authority=authority,
    )
    recovery_store = G0RecoveryMarkerStore(Path(args.recovery_root).expanduser())
    gate = GoldenCfgConfigGate(
        definition=definition,
        run_id=run_id,
        device_id=args.device_id,
        worker_id=args.worker_id,
        config=config,
        authority=authority,
        session_factory=SessionLocal,
        command_timeout=args.command_timeout,
        recovery_store=recovery_store,
    )
    await adapter.connect()
    try:
        result = await gate.run()
    finally:
        await adapter.disconnect()

    token = gate.runtime.get("token")
    if token is not None:
        record_authority_ref(session_factory=SessionLocal, run_id=run_id, token=token)
    passed = result.verdict.value == "PASS"
    finalize_authority_bound_run(
        session_factory=SessionLocal,
        run_id=run_id,
        reproduction_session_id=reproduction_session_id,
        passed=passed,
    )
    return (0 if passed else 2), _summary(result, gate, device_id=args.device_id, model=args.model)


def main() -> int:
    parser = argparse.ArgumentParser(description=GOLDEN_CFG_CONFIG_CASE_ID)
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--username", default="root")
    parser.add_argument("--platform-id", default=None)
    parser.add_argument("--password-env", default="ENV:G0_SSH_PASSWORD")
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--profile-root", default="profiles")
    parser.add_argument("--output-root", default="/tmp/golden-cfg-config-001")
    parser.add_argument(
        "--recovery-root",
        default="~/.local/state/voip-ai/g0-recovery",
        help="runner-private persistent marker directory; never uploaded as evidence",
    )
    parser.add_argument("--command-timeout", type=float, default=20.0)
    parser.add_argument("--allow-live-mutation", action="store_true")
    args = parser.parse_args()
    output_root = Path(args.output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    try:
        rc, payload = asyncio.run(_run(args))
    except Exception as exc:
        rc = 2
        payload = {
            "gate": GOLDEN_CFG_CONFIG_CASE_ID,
            "verdict": "INCONCLUSIVE",
            "error": type(exc).__name__,
            "route": G0_ROUTE.as_dict(),
        }
    output = output_root / "golden-cfg-config-001.json"
    output.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
