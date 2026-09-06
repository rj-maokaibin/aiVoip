#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
from pathlib import Path

import httpx

from app.automation.adapters.entries.web import WebEntryAdapter
from app.automation.adapters.pbx.registration import FusionPbxRegistrationProbe
from app.automation.adapters.web_auth.apf3260m import build_apf3260m_luci_auth_provider
from app.automation.adapters.web_auth.base import SessionManager, WebCredential
from app.automation.adapters.web_auth.legacy_luci import LegacyLuciAuthError
from app.automation.adapters.web_profiles.schema import WebApiProfile
from app.automation.gates.golden_web_config import (
    GOLDEN_WEB_CONFIG_CASE_ID,
    WEB_CONFIG_ROUTE,
    GoldenWebConfigGate,
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
from app.infrastructure.transport.http import HttpApiTransport
from app.infrastructure.transport.ssh import SharedSshTransport


_SAFE_AUTH_ERROR_CODE = re.compile(r"^[A-Z0-9_]+(?::[0-9]{3})?$")


def _safe_exception_code(exc: Exception) -> str | None:
    """Retain only an allowlisted non-secret auth reason for live diagnostics."""

    if not isinstance(exc, LegacyLuciAuthError):
        return None
    value = str(exc).strip()
    if not _SAFE_AUTH_ERROR_CODE.fullmatch(value):
        return None
    return value


def _safe_transport_diagnostics(gate: GoldenWebConfigGate) -> dict:
    allowed = (
        "request_id",
        "attempt",
        "method",
        "path",
        "elapsed_ms",
        "status_code",
        "error",
    )
    items = gate.runtime.get("sanitized_mutation_transport_evidence") or ()
    mutation = []
    if isinstance(items, (list, tuple)):
        for item in items:
            if isinstance(item, dict):
                mutation.append({key: item.get(key) for key in allowed})
    observation_allowed = (
        "attempt",
        "phase",
        "elapsed_ms",
        "status_code",
        "accepted",
        "error",
        "detail",
    )
    observation_items = gate.runtime.get("sanitized_unknown_observation_diagnostics") or ()
    observation = []
    if isinstance(observation_items, (list, tuple)):
        for item in observation_items:
            if isinstance(item, dict):
                observation.append({key: item.get(key) for key in observation_allowed})
    return {
        "mutation": mutation,
        "initial_readback_available": bool(
            gate.runtime.get("unknown_initial_readback_available", False)
        ),
        "observation": observation,
    }


def _summary(result, gate: GoldenWebConfigGate, *, device_id: str, model: str) -> dict:
    token = gate.runtime.get("token")
    return {
        "gate": GOLDEN_WEB_CONFIG_CASE_ID,
        "run_id": gate.run_id,
        "device_id": device_id,
        "model": model,
        "verdict": result.verdict.value,
        "state": result.state.value,
        "terminal_reason": result.terminal_reason,
        "route": WEB_CONFIG_ROUTE.as_dict(),
        "target_number": gate.target_number,
        "mutation": {
            "entry": "WEB",
            "ssh_fallback": False,
            "identity_fields_changed": ["number", "disName"],
            "auth_id_preserved": True,
            "password_preserved": True,
        },
        "registration": {
            "provider": "fusionpbx_fs_cli_read_only",
            "mutation": False,
        },
        "snapshot": {
            "captured": gate.runtime.get("snapshot") is not None,
            "raw_runtime_only": True,
            "secret_values_persisted": False,
        },
        "authority": {
            "type": "CaptureLeaseManager",
            "lease_epoch": getattr(token, "lease_epoch", None),
            "release_last": True,
        },
        "secret_values_emitted": False,
        "transport_diagnostics": _safe_transport_diagnostics(gate),
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
        raise RuntimeError("WEB_GOLDEN_LIVE_MUTATION_NOT_EXPLICITLY_AUTHORIZED")

    username = os.environ.get(args.web_username_env, "").strip()
    password = os.environ.get(args.web_password_env, "")
    if not username or not password:
        raise RuntimeError("WEB_GOLDEN_CREDENTIAL_NOT_RESOLVED")

    profile_root = Path(args.profile_root)
    definition = TestRegistry(profile_root / "tests").definition(GOLDEN_WEB_CONFIG_CASE_ID)
    web_profile = WebApiProfile.load_yaml(profile_root / "web_api" / args.web_profile)
    target_number = str(args.target_number or definition.case.parameters.get("target_number") or "").strip()
    if not target_number:
        raise RuntimeError("WEB_GOLDEN_TARGET_NUMBER_REQUIRED")

    # The DUT WEB service can close an idle HTTP/1.1 keep-alive connection without
    # advertising that closure. A later Save on that stale pooled socket then fails
    # immediately with RemoteProtocolError and its result is necessarily UNKNOWN.
    # Disable keep-alive reuse for this real-device runner so the one authorized
    # mutation is sent on a fresh connection. This does NOT add a mutation retry.
    # Force HTTP/1.1 connection-close semantics for the legacy LuCI CGI.
    # The APF3260-M CGI has been observed to close the socket immediately after
    # accepting a Save, before emitting a parseable HTTP response. Keeping the
    # request on a fresh non-persistent HTTP/1.1 connection avoids stale-pool
    # reuse while preserving the strict single-mutation/no-retry contract.
    client = httpx.AsyncClient(
        base_url=args.web_base_url,
        verify=not args.web_insecure_tls,
        http2=False,
        limits=httpx.Limits(max_keepalive_connections=0),
        headers={"Connection": "close"},
    )
    http_transport = HttpApiTransport(args.web_base_url, client=client)
    credential = WebCredential(username=username, password=password)
    auth = build_apf3260m_luci_auth_provider(
        timestamp_provider=lambda: str(int(time.time()))
    )
    session_manager = SessionManager(http_transport, auth, lambda: credential)
    web = WebEntryAdapter(profile=web_profile, session_manager=session_manager)

    # Read-only authentication must succeed before DeviceAuthority is acquired;
    # an unproven credential can never reach the mutation path.
    await session_manager.ensure_session()

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
    ssh_adapter = build_asyncssh_adapter(spec, password_env=args.password_env)
    authority = CaptureLeaseCompatibilityAdapter(
        CaptureLeaseManager(SessionLocal, ttl_seconds=120.0)
    )
    config = ConfigFrameworkExecutor(
        SharedSshTransport(ssh_adapter),
        allowed_modules=("voipUserInfo",),
        authority=authority,
    )
    registration = FusionPbxRegistrationProbe()
    gate = GoldenWebConfigGate(
        definition=definition,
        run_id=run_id,
        device_id=args.device_id,
        worker_id=args.worker_id,
        target_number=target_number,
        web=web,
        config=config,
        registration_probe=registration,
        authority=authority,
        session_factory=SessionLocal,
        registration_timeout_seconds=args.registration_timeout,
    )

    result = None
    await ssh_adapter.connect()
    try:
        result = await gate.run()
        rc = 0 if result.verdict.value == "PASS" else 2
        return rc, _summary(result, gate, device_id=args.device_id, model=args.model)
    finally:
        try:
            token = gate.runtime.get("token")
            if token is not None:
                record_authority_ref(session_factory=SessionLocal, run_id=run_id, token=token)
            finalize_authority_bound_run(
                session_factory=SessionLocal,
                run_id=run_id,
                reproduction_session_id=reproduction_session_id,
                passed=bool(result is not None and result.verdict.value == "PASS"),
            )
        finally:
            await ssh_adapter.disconnect()
            await client.aclose()


def main() -> int:
    parser = argparse.ArgumentParser(description=GOLDEN_WEB_CONFIG_CASE_ID)
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--username", default="root")
    parser.add_argument("--platform-id", default=None)
    parser.add_argument("--password-env", default="ENV:SIP_ABA_SSH_PASSWORD")
    parser.add_argument("--web-base-url", required=True)
    parser.add_argument("--web-username-env", default="WEB_USERNAME")
    parser.add_argument("--web-password-env", default="WEB_PASSWORD")
    parser.add_argument("--web-insecure-tls", action="store_true")
    parser.add_argument("--web-profile", default="apf3260m_reyeeos_2_421_voip_v1.yaml")
    parser.add_argument("--target-number", default=None)
    parser.add_argument("--registration-timeout", type=float, default=60.0)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--profile-root", default="profiles")
    parser.add_argument("--output-root", default="/tmp/golden-web-config-001")
    parser.add_argument("--allow-live-mutation", action="store_true")
    args = parser.parse_args()

    output_root = Path(args.output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    try:
        rc, payload = asyncio.run(_run(args))
    except Exception as exc:
        rc = 2
        payload = {
            "gate": GOLDEN_WEB_CONFIG_CASE_ID,
            "verdict": "INCONCLUSIVE",
            "error": type(exc).__name__,
            "route": WEB_CONFIG_ROUTE.as_dict(),
            "secret_values_emitted": False,
        }
        error_code = _safe_exception_code(exc)
        if error_code is not None:
            payload["error_code"] = error_code
    output = output_root / "golden-web-config-001.json"
    output.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
