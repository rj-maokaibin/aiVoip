#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import secrets
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import run_golden_web_config as base

from app.automation.adapters.pbx.fusionpbx_config import FusionPbxConfigProvider
from app.automation.adapters.pbx.fusionpbx_mutation import FusionPbxMutationProvider
from app.automation.adapters.pbx.inventory import PbxInventoryService
from app.automation.adapters.pbx.profile import load_fusionpbx_profile
from app.automation.adapters.pbx.registration_esl import FreeSwitchRegistrationObserver
from app.automation.adapters.pbx.resource_authority import PbxExtensionLeaseManager
from app.automation.adapters.pbx.resource_manager import PbxResourceManager
from app.automation.adapters.pbx.runtime_read import FreeSwitchRuntimeReadProbe
from app.automation.adapters.pbx.source_fence import FusionPbxSourceFence
from app.automation.gates.golden_pbx_register import (
    GOLDEN_PBX_REGISTER_CASE_ID,
    PbxRegistrationGoldenGate,
)
from app.db.base import Base
from app.infrastructure.device_authority.keepalive import AuthorityKeepalive
import app.db.models  # noqa: F401
import app.automation.pbx_models  # noqa: F401

_TARGET_PASSWORD: str | None = None
_PBX_PROFILE = None


def _build_registration_probe(args):
    del args
    if _PBX_PROFILE is None:
        raise RuntimeError("PBX_PROFILE_NOT_INITIALIZED")
    return FreeSwitchRegistrationObserver(_PBX_PROFILE)


def _build_gate(*, args, definition, run_id, web, config, registration, authority):
    if _TARGET_PASSWORD is None:
        raise RuntimeError("PBX_TARGET_PASSWORD_NOT_INITIALIZED")
    target = str(args.target_number or definition.case.parameters.get("target_identity") or "")
    return PbxRegistrationGoldenGate(
        definition=definition,
        run_id=run_id,
        device_id=args.device_id,
        worker_id=args.worker_id,
        target_number=target,
        target_password=_TARGET_PASSWORD,
        web=web,
        config=config,
        registration_probe=registration,
        authority=authority,
        session_factory=base.SessionLocal,
        registration_timeout_seconds=args.registration_timeout,
    )


_original_summary = base._summary


def _summary(result, gate, *, device_id: str, model: str) -> dict:
    payload = _original_summary(result, gate, device_id=device_id, model=model)
    payload["gate"] = GOLDEN_PBX_REGISTER_CASE_ID
    payload["target_identity"] = gate.target_number
    payload["mutation"] = {
        "entry": "WEB",
        "ssh_fallback": False,
        "identity_fields_changed": ["number", "disName", "authId", "passwd"],
        "baseline_restored_on_cleanup": True,
        "secret_values_emitted": False,
    }
    payload["registration"] = {
        "provider": "freeswitch_esl_with_fs_cli_reconcile",
        "mutation": False,
        "esl_event_required": True,
    }
    payload["secret_values_emitted"] = False
    return payload


def _pbx_context(output_root: Path, profile_path: str):
    profile = load_fusionpbx_profile(profile_path)
    state_db = output_root / "g-pbx-002-state.sqlite"
    engine = create_engine(f"sqlite+pysqlite:///{state_db}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    config = FusionPbxConfigProvider(profile)
    fence = FusionPbxSourceFence(profile)
    runtime = FreeSwitchRuntimeReadProbe(profile)
    inventory_service = PbxInventoryService(
        Session, profile, config_provider=config, source_fence=fence
    )
    inventory = inventory_service.sync()
    return profile, Session, config, fence, runtime, inventory_service, inventory


async def _run_combined(args) -> tuple[int, dict]:
    global _TARGET_PASSWORD, _PBX_PROFILE
    output_root = Path(args.output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    profile, Session, config, fence, runtime, inventory_service, inventory = _pbx_context(
        output_root, args.pbx_profile
    )
    domains = config.discover_domains()
    if len(domains) != 1:
        raise RuntimeError("PBX_DOMAIN_CARDINALITY_INVALID")
    domain = domains[0]
    target_identity = str(args.target_number or "").strip()
    resource = inventory_service.ensure_automation_identity(
        pbx_node_id=inventory["node_id"],
        domain_id=domain.domain_id,
        extension=target_identity,
    )
    pbx_authority = PbxExtensionLeaseManager(Session, ttl_seconds=300)
    pbx_token = pbx_authority.acquire_endpoint(
        pbx_node_id=inventory["node_id"],
        extension=resource.extension,
        dial_alias=args.dial_alias,
        run_id=f"{GOLDEN_PBX_REGISTER_CASE_ID}:{target_identity}",
        owner_worker_id=args.worker_id,
    )
    pbx_keepalive = AuthorityKeepalive(pbx_authority, interval_seconds=30.0)
    pbx_keepalive.start(pbx_token)
    mutation = FusionPbxMutationProvider(
        profile,
        authority=pbx_authority,
        source_fence=fence,
        timeout_seconds=20,
    )
    manager = PbxResourceManager(
        Session,
        authority=pbx_authority,
        config=config,
        mutation=mutation,
        runtime=runtime,
    )
    target_password = secrets.token_urlsafe(24)
    _TARGET_PASSWORD = target_password
    _PBX_PROFILE = profile

    payload: dict = {
        "gate": GOLDEN_PBX_REGISTER_CASE_ID,
        "target_identity": target_identity,
        "dial_alias": args.dial_alias,
        "secret_values_emitted": False,
    }
    dut_rc = 2
    pbx_cleanup_ok = False
    provisioned = None
    try:
        provisioned = manager.provision_extension(
            pbx_token,
            password=target_password,
            template_identity="7102",
            dial_alias=args.dial_alias,
        )
        payload["pbx_provision"] = provisioned.safe_dict()
        alias_resolution = runtime.resolve_directory_identity(
            args.dial_alias, domain_name=domain.domain_name
        )
        payload["alias_resolution"] = alias_resolution
        if (
            alias_resolution is None
            or alias_resolution.get("resolved_identity") != target_identity
            or alias_resolution.get("number_alias") != args.dial_alias
        ):
            raise RuntimeError("PBX_DIAL_ALIAS_RESOLUTION_MISMATCH")

        base.GOLDEN_WEB_CONFIG_CASE_ID = GOLDEN_PBX_REGISTER_CASE_ID
        base._build_registration_probe = _build_registration_probe
        base._build_gate = _build_gate
        base._summary = _summary
        args.target_number = target_identity
        dut_rc, dut_payload = await base._run(args)
        payload.update(dut_payload)
        payload["pbx_provision"] = provisioned.safe_dict()
        payload["alias_resolution"] = alias_resolution
        payload["dial_alias"] = args.dial_alias
    except Exception as exc:
        payload["execution_error"] = type(exc).__name__
        payload["execution_error_code"] = getattr(exc, "code", None)
    finally:
        _TARGET_PASSWORD = None
        try:
            pbx_keepalive.raise_if_failed()
            pbx_token = pbx_keepalive.token
            cleaned = manager.deprovision_extension(pbx_token)
            payload["pbx_cleanup"] = cleaned.safe_dict()
            extension_absent = not config.extension_exists(target_identity)
            alias_absent = not config.extension_exists(args.dial_alias)
            extension_runtime_absent = not runtime.user_visible(
                target_identity, domain_name=domain.domain_name
            )
            alias_runtime_absent = not runtime.user_visible(
                args.dial_alias, domain_name=domain.domain_name
            )
            alias_resolution_absent = (
                runtime.resolve_directory_identity(
                    args.dial_alias, domain_name=domain.domain_name
                ) is None
            )
            pbx_cleanup_ok = all((
                extension_absent,
                alias_absent,
                extension_runtime_absent,
                alias_runtime_absent,
                alias_resolution_absent,
            ))
            payload["pbx_cleanup_verify"] = {
                "extension_absent": extension_absent,
                "dial_alias_absent": alias_absent,
                "extension_runtime_absent": extension_runtime_absent,
                "dial_alias_runtime_absent": alias_runtime_absent,
                "alias_resolution_absent": alias_resolution_absent,
            }
            if pbx_cleanup_ok:
                await pbx_keepalive.stop()
                pbx_token = pbx_keepalive.token
                manager.release_extension(pbx_token)
                payload["pbx_release_last"] = True
            else:
                payload["pbx_release_last"] = False
        except Exception as exc:
            payload["pbx_cleanup_error"] = type(exc).__name__
            payload["pbx_cleanup_error_code"] = getattr(exc, "code", None)
            payload["pbx_release_last"] = False
            try:
                await pbx_keepalive.stop()
            except Exception as keepalive_exc:
                payload["pbx_keepalive_error"] = type(keepalive_exc).__name__
        _PBX_PROFILE = None

    dut_pass = dut_rc == 0 and payload.get("verdict") == "PASS"
    final_pass = bool(
        dut_pass
        and pbx_cleanup_ok
        and payload.get("pbx_release_last") is True
        and payload.get("secret_values_emitted") is False
    )
    payload["dut_gate_pass"] = dut_pass
    payload["pbx_cleanup_pass"] = pbx_cleanup_ok
    payload["verdict"] = "PASS" if final_pass else "FAIL"
    return (0 if final_pass else 2), payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=GOLDEN_PBX_REGISTER_CASE_ID)
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
    parser.add_argument(
        "--web-profile", default="apf3260m_reyeeos_2_421_voip_v1.yaml"
    )
    parser.add_argument("--target-number", default="7900.a")
    parser.add_argument("--dial-alias", default="7900")
    parser.add_argument("--registration-timeout", type=float, default=60.0)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--profile-root", default="profiles")
    parser.add_argument(
        "--pbx-profile", default="profiles/pbx/fusionpbx_srv_v1.json"
    )
    parser.add_argument("--output-root", default="/tmp/golden-pbx-register-002")
    parser.add_argument("--allow-live-mutation", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    output_root = Path(args.output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    try:
        rc, payload = asyncio.run(_run_combined(args))
    except Exception as exc:
        rc = 2
        payload = {
            "gate": GOLDEN_PBX_REGISTER_CASE_ID,
            "verdict": "FAIL",
            "error": type(exc).__name__,
            "secret_values_emitted": False,
        }
    output = output_root / "golden-pbx-register-002.json"
    output.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
