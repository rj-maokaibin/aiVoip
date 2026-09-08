#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from collections.abc import Mapping
from pathlib import Path

import httpx

from app.automation.adapters.entries.web import WebEntryAdapter
from app.automation.adapters.pbx.registration import FusionPbxRegistrationProbe
from app.automation.adapters.web_auth.apf3260m import build_apf3260m_luci_auth_provider
from app.automation.adapters.web_auth.base import SessionManager, WebCredential
from app.automation.adapters.web_profiles.schema import WebApiProfile
from app.automation.gates.golden_web_config import (
    GOLDEN_WEB_CONFIG_CASE_ID,
    build_numeric_probe,
    observed_account,
    registration_identity_from_snapshot,
    snapshot_writable_bundle,
)
from app.automation.gates.runtime_binding import (
    finalize_authority_bound_run,
    prepare_authority_bound_run,
    record_authority_ref,
)
from app.automation.registry import TestRegistry
from app.capture_v2.lease.manager import CaptureLeaseManager
from app.db.session import SessionLocal
from app.infrastructure.device_authority.capture_lease_adapter import CaptureLeaseCompatibilityAdapter
from app.infrastructure.transport.http import HttpApiTransport


def _row(value):
    current = value
    for _ in range(3):
        if isinstance(current, list) and current and isinstance(current[0], Mapping):
            return current[0]
        if isinstance(current, Mapping) and "data" in current:
            current = current["data"]
            continue
        break
    raise RuntimeError("RECOVERY_VOIP_USER_ROW_REQUIRED")


async def run(args) -> dict:
    if os.getenv("REAL_LIVE_MUTATION") != "EXPLICIT_ONLY":
        raise RuntimeError("RECOVERY_LIVE_MUTATION_NOT_AUTHORIZED")
    username = os.getenv(args.web_username_env, "").strip()
    password = os.getenv(args.web_password_env, "")
    if not username or not password:
        raise RuntimeError("RECOVERY_WEB_CREDENTIAL_REQUIRED")

    profile = WebApiProfile.load_yaml(Path(args.profile_path))
    client = httpx.AsyncClient(
        base_url=args.web_base_url,
        verify=not args.web_insecure_tls,
        http2=False,
        limits=httpx.Limits(max_keepalive_connections=0),
        headers={"Connection": "close"},
    )
    web = WebEntryAdapter(
        profile=profile,
        session_manager=SessionManager(
            HttpApiTransport(args.web_base_url, client=client),
            build_apf3260m_luci_auth_provider(timestamp_provider=lambda: str(int(time.time()))),
            lambda: WebCredential(username=username, password=password),
        ),
    )
    authority = CaptureLeaseCompatibilityAdapter(CaptureLeaseManager(SessionLocal, ttl_seconds=120.0))
    token = None
    authority_run_id = None
    reproduction_session_id = None
    mutation_sent = False
    passed = False
    try:
        await web.session_manager.ensure_session()
        before_read = await web.read_voip_account(0)
        snapshot = snapshot_writable_bundle(before_read)
        before = observed_account(before_read)
        if str(before.get("number")) not in {"7900", args.target_number}:
            raise RuntimeError(f"RECOVERY_UNEXPECTED_CURRENT_NUMBER:{before.get('number')}")
        target = build_numeric_probe(snapshot, args.target_number)
        before_row = _row(snapshot["voipUserInfo"])
        target_row = _row(target["voipUserInfo"])
        auth_preserved = before_row.get("authId") == target_row.get("authId")
        password_preserved = before_row.get("passwd") == target_row.get("passwd")
        if not auth_preserved or not password_preserved:
            raise RuntimeError("RECOVERY_IDENTITY_SECRET_DRIFT")

        if str(before.get("number")) != args.target_number or str(before.get("disName")) != args.target_number:
            definition = TestRegistry(Path(args.profile_root) / "tests").definition(GOLDEN_WEB_CONFIG_CASE_ID)
            authority_run_id, reproduction_session_id = prepare_authority_bound_run(
                session_factory=SessionLocal, device_id=args.device_id, worker_id=args.worker_id, definition=definition
            )
            token = authority.acquire(device_id=args.device_id, run_id=authority_run_id, owner_worker_id=args.worker_id)
            authority.validate(token)
            mutation = await web.configure_voip_bundle(target)
            mutation_sent = True
            if not mutation.accepted and not mutation.unknown_result:
                raise RuntimeError(f"RECOVERY_WEB_SAVE_REJECTED:{mutation.error}")

        after_read = await web.read_voip_account(0)
        after = observed_account(after_read)
        readback_ok = (
            str(after.get("number")) == args.target_number
            and str(after.get("disName")) == args.target_number
            and after.get("authId") == before.get("authId")
        )
        if not readback_ok:
            raise RuntimeError("RECOVERY_WEB_READBACK_MISMATCH")

        identity = registration_identity_from_snapshot(target)
        registration = await FusionPbxRegistrationProbe().wait_registered(
            number=identity,
            timeout_seconds=args.registration_timeout,
        )
        result = {
            "schema": "golden-web-baseline-recovery-v1",
            "status": "PASS" if registration.registered else "FAIL",
            "device_id": args.device_id,
            "before_number": str(before.get("number")),
            "after_number": str(after.get("number")),
            "after_disname": str(after.get("disName")),
            "auth_id_preserved": auth_preserved and after.get("authId") == before.get("authId"),
            "password_preserved": password_preserved,
            "mutation_sent": mutation_sent,
            "mutation_entry": "WEB" if mutation_sent else None,
            "ssh_fallback": False,
            "registration_identity": identity,
            "registration_restored": bool(registration.registered),
            "authority_type": "CaptureLeaseManager" if token is not None else None,
            "release_last": True,
            "secret_values_emitted": False,
        }
        if not registration.registered:
            raise RuntimeError("RECOVERY_SIP_REGISTRATION_NOT_RESTORED")
        passed = True
        return result
    finally:
        if token is not None:
            authority.release(token)
            if authority_run_id is not None:
                record_authority_ref(session_factory=SessionLocal, run_id=authority_run_id, token=token)
        if authority_run_id is not None and reproduction_session_id is not None:
            finalize_authority_bound_run(
                session_factory=SessionLocal, run_id=authority_run_id,
                reproduction_session_id=reproduction_session_id, passed=passed
            )
        await client.aclose()


def main() -> int:
    parser = argparse.ArgumentParser(description="Recover Golden WEB DUT baseline to the preserved SIP identity")
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--target-number", default="7102")
    parser.add_argument("--web-base-url", required=True)
    parser.add_argument("--profile-path", required=True)
    parser.add_argument("--profile-root", default="profiles")
    parser.add_argument("--web-username-env", default="WEB_USERNAME")
    parser.add_argument("--web-password-env", default="WEB_PASSWORD")
    parser.add_argument("--web-insecure-tls", action="store_true")
    parser.add_argument("--registration-timeout", type=float, default=60.0)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        payload = asyncio.run(run(args))
    except Exception as exc:
        payload = {
            "schema": "golden-web-baseline-recovery-v1",
            "status": "FAIL",
            "error": type(exc).__name__,
            "reason": str(exc) if str(exc).startswith("RECOVERY_") else type(exc).__name__,
            "secret_values_emitted": False,
        }
        Path(args.output).write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(payload, sort_keys=True))
        return 2
    Path(args.output).write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
