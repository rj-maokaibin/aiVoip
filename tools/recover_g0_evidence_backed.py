#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
from pathlib import Path
from typing import Any, Mapping

from app.automation.gates.golden_cfg_config import (
    GOLDEN_CFG_CONFIG_CASE_ID,
    G0_MODULE,
    extract_set_payload,
)
from app.automation.gates.runtime_binding import (
    finalize_authority_bound_run,
    prepare_authority_bound_run,
    record_authority_ref,
)
from app.automation.registry import TestRegistry
from app.capture_v2.gate.context import build_asyncssh_adapter
from app.capture_v2.gate.models import GateDeviceSpec
from app.capture_v2.lease.manager import CaptureLeaseManager
from app.db.session import SessionLocal
from app.infrastructure.config_framework.executor import ConfigFrameworkExecutor
from app.infrastructure.device_authority.capture_lease_adapter import CaptureLeaseCompatibilityAdapter
from app.infrastructure.device_authority.keepalive import AuthorityKeepalive
from app.infrastructure.transport.ssh import SharedSshTransport


AUTH_SCHEMA = "g0-evidence-backed-recovery-v1"


def _first_disname(payload: Mapping[str, Any]) -> str:
    rows = payload.get("data")
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], Mapping):
        raise RuntimeError("G0_RECOVERY_ACCOUNT_MISSING")
    return str(rows[0].get("disName") or "")


def _load_authorization(path: Path, *, device_id: str) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != AUTH_SCHEMA:
        raise RuntimeError("G0_RECOVERY_AUTH_SCHEMA_INVALID")
    if data.get("enabled") is not True:
        raise RuntimeError("G0_RECOVERY_AUTH_NOT_ENABLED")
    if int(data.get("pr_number") or 0) != 153:
        raise RuntimeError("G0_RECOVERY_AUTH_PR_MISMATCH")
    if str(data.get("device_id") or "") != device_id:
        raise RuntimeError("G0_RECOVERY_AUTH_DEVICE_MISMATCH")
    if str(data.get("expected_current_disname") or "") != "G0":
        raise RuntimeError("G0_RECOVERY_AUTH_CURRENT_INVALID")
    if str(data.get("restore_disname") or "") != "7102":
        raise RuntimeError("G0_RECOVERY_AUTH_TARGET_INVALID")
    evidence = data.get("evidence")
    if not isinstance(evidence, Mapping):
        raise RuntimeError("G0_RECOVERY_AUTH_EVIDENCE_MISSING")
    if str(evidence.get("har_sha256") or "") != "f00aa36822869b4e391b51f873076429c74ea054440b9a73b0fc87601a9a4411":
        raise RuntimeError("G0_RECOVERY_AUTH_HAR_DIGEST_MISMATCH")
    if str(evidence.get("baseline_sha256") or "") != "6d243e44ce30dcaabdffdc0a04d8bdfbc212ded5b62c1f26f8387a5f0e9cf6de":
        raise RuntimeError("G0_RECOVERY_AUTH_BASELINE_DIGEST_MISMATCH")
    return data


async def _run(args) -> tuple[int, dict[str, Any]]:
    if not args.allow_live_recovery or os.getenv("REAL_LIVE_MUTATION") != "EXPLICIT_ONLY":
        raise RuntimeError("G0_RECOVERY_LIVE_MUTATION_NOT_AUTHORIZED")

    authorization = _load_authorization(Path(args.authorization), device_id=args.device_id)
    expected_current = str(authorization["expected_current_disname"])
    restore_disname = str(authorization["restore_disname"])

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
    keepalive = AuthorityKeepalive(authority, interval_seconds=30.0)
    config = ConfigFrameworkExecutor(
        SharedSshTransport(adapter),
        allowed_modules=(G0_MODULE,),
        authority=authority,
    )

    token = None
    release_ok = False
    passed = False
    pre_disname = None
    post_disname = None
    mutation_status = None
    remote_whoami = None
    error = None

    await adapter.connect()
    try:
        identity = await adapter.execute_shell(
            "whoami 2>/dev/null || id -un 2>/dev/null || (test -n \"${USER:-}\" && printf '%s\\n' \"$USER\")",
            timeout=args.command_timeout,
            retries=1,
        )
        if identity.exit_status != 0:
            raise RuntimeError(f"G0_RECOVERY_REMOTE_IDENTITY_FAILED:{identity.exit_status}")
        remote_whoami = str(identity.stdout or "").strip().splitlines()[0] if str(identity.stdout or "").strip() else ""
        if not remote_whoami:
            raise RuntimeError("G0_RECOVERY_REMOTE_IDENTITY_EMPTY")

        token = authority.acquire(
            device_id=args.device_id,
            run_id=run_id,
            owner_worker_id=args.worker_id,
        )
        keepalive.start(token)

        current = await config.get(G0_MODULE, timeout=args.command_timeout)
        if not current.success:
            raise RuntimeError(f"G0_RECOVERY_PRE_READ_FAILED:{current.rcode}")
        payload = extract_set_payload(current)
        pre_disname = _first_disname(payload)
        if pre_disname != expected_current:
            raise RuntimeError(f"G0_RECOVERY_PRECONDITION_CHANGED:{pre_disname}")

        restore_payload = copy.deepcopy(payload)
        rows = restore_payload["data"]
        rows[0]["disName"] = restore_disname

        mutation = await config.set(
            G0_MODULE,
            restore_payload,
            authority_token=keepalive.token,
            timeout=args.command_timeout,
        )
        mutation_status = mutation.status.value

        verify = await config.get(G0_MODULE, timeout=args.command_timeout)
        if not verify.success:
            raise RuntimeError(f"G0_RECOVERY_POST_READ_FAILED:{verify.rcode}")
        verify_payload = extract_set_payload(verify)
        post_disname = _first_disname(verify_payload)
        if post_disname != restore_disname:
            raise RuntimeError(f"G0_RECOVERY_REVERSE_VERIFY_FAILED:{post_disname}")
        passed = True
    except Exception as exc:
        error = f"{type(exc).__name__}:{exc}"
    finally:
        if token is not None:
            try:
                await keepalive.stop()
            except Exception as exc:
                if error is None:
                    error = f"{type(exc).__name__}:{exc}"
                    passed = False
            try:
                live_token = keepalive.token
            except Exception:
                live_token = token
            try:
                authority.release(live_token)
                token = live_token
                release_ok = True
            except Exception as exc:
                if error is None:
                    error = f"{type(exc).__name__}:{exc}"
                passed = False
        await adapter.disconnect()

    if token is not None:
        record_authority_ref(session_factory=SessionLocal, run_id=run_id, token=token)
    finalize_authority_bound_run(
        session_factory=SessionLocal,
        run_id=run_id,
        reproduction_session_id=reproduction_session_id,
        passed=passed and release_ok,
    )

    evidence = authorization["evidence"]
    summary = {
        "schema": "g0-evidence-backed-recovery-result-v1",
        "run_id": run_id,
        "device_id": args.device_id,
        "module": G0_MODULE,
        "field": "disName",
        "pre_disname": pre_disname,
        "expected_pre_disname": expected_current,
        "restore_disname": restore_disname,
        "post_disname": post_disname,
        "restore_verified": bool(passed and post_disname == restore_disname),
        "mutation_status": mutation_status,
        "identity_fields_changed": False,
        "resolved_ssh_username": args.username,
        "remote_whoami": remote_whoami,
        "authority": {
            "lease_epoch": getattr(token, "lease_epoch", None),
            "release_verified": release_ok,
        },
        "evidence": {
            "har_sha256": evidence["har_sha256"],
            "baseline_sha256": evidence["baseline_sha256"],
            "documented_sequence": evidence["documented_sequence"],
        },
        "secret_values_persisted": False,
        "raw_module_snapshot_persisted": False,
        "error": error,
        "verdict": "PASS" if passed and release_ok else "INCONCLUSIVE",
    }
    return (0 if summary["verdict"] == "PASS" else 2), summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Evidence-backed cleanup for failed G0 residue")
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--username", required=True)
    parser.add_argument("--platform-id", default=None)
    parser.add_argument("--password-env", required=True)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--profile-root", default="profiles")
    parser.add_argument("--authorization", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--command-timeout", type=float, default=20.0)
    parser.add_argument("--allow-live-recovery", action="store_true")
    args = parser.parse_args()

    try:
        rc, summary = asyncio.run(_run(args))
    except Exception as exc:
        rc = 2
        summary = {
            "schema": "g0-evidence-backed-recovery-result-v1",
            "verdict": "INCONCLUSIVE",
            "error": f"{type(exc).__name__}:{exc}",
            "secret_values_persisted": False,
            "raw_module_snapshot_persisted": False,
        }
    Path(args.output).write_text(
        json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
