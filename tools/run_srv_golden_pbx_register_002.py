#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
TOOLS = ROOT / "tools"
for item in (str(BACKEND), str(TOOLS)):
    if item not in sys.path:
        sys.path.insert(0, item)

from app.db.base import Base
from app.db.models import ReproductionSession
from app.capture_v2.db_models import CaptureSession
import app.db.models  # noqa: F401
import app.automation.pbx_models  # noqa: F401
import run_golden_web_config as web_base
import run_golden_pbx_register_002 as combined
import resolve_current_web_credential_env as resolver


def _runtime_session(output_root: Path):
    state = output_root / "g-pbx-002-runtime.sqlite"
    engine = create_engine(f"sqlite+pysqlite:///{state}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _seed_authority_templates(Session, args) -> None:
    with Session() as db:
        existing = db.query(ReproductionSession).filter_by(device_id=args.device_id).first()
        if existing is not None:
            return
        reproduction = ReproductionSession(
            case_id="srv-direct-g-pbx-002", device_id=args.device_id,
            profile_key="voip-standard", profile_version="1", profile_checksum="0" * 64,
            effective_profile_snapshot={"source": "srv-direct", "model": args.model},
            platform_profile_id=args.platform_id or "mt7981", platform_profile_version="1",
        )
        db.add(reproduction); db.flush()
        db.add(CaptureSession(
            reproduction_session_id=reproduction.id, device_id=args.device_id,
            capture_profile_id="authority-only", capture_profile_version="1",
            platform_profile_id=args.platform_id or "mt7981", platform_profile_version="1",
            effective_profile={"authority_only": True, "source": "srv-direct"},
            cleanup_status="TEMPLATE",
        ))
        db.commit()


def _resolver_args(args, evidence_path: Path):
    return SimpleNamespace(
        base_url=args.web_base_url,
        device_id=args.device_id,
        model=args.model,
        device_host=args.host,
        ssh_port=args.port,
        ssh_username=args.username,
        ssh_password_env=f"LOCAL_SECRET:{args.host}:{args.port}",
        platform_id=args.platform_id,
        ssh_timeout=20.0,
        profile_path=str(ROOT / "profiles/web_api" / args.web_profile),
        secret_file=args.secret_file,
        username_env="UNUSED_WEB_USERNAME",
        password_env="UNUSED_WEB_PASSWORD",
        output=str(evidence_path.with_suffix(".runtime.env")),
        evidence_output=str(evidence_path),
        insecure_tls=args.web_insecure_tls,
    )


async def _run(args) -> tuple[int, dict]:
    output_root = Path(args.output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    evidence_path = output_root / "web-credential-resolution.json"
    candidate, evidence = await resolver._resolve(_resolver_args(args, evidence_path))
    evidence_path.write_text(json.dumps(evidence, sort_keys=True, indent=2) + "\n")
    if candidate is None or not evidence.get("web_endpoint_matches_selected_dut"):
        return 2, {
            "gate": "G-PBX-002", "verdict": "BLOCKED",
            "reason": "WEB_CREDENTIAL_TARGET_BINDING_UNPROVEN",
            "credential_resolution": evidence,
            "secret_values_emitted": False,
        }

    RuntimeSession = _runtime_session(output_root)
    _seed_authority_templates(RuntimeSession, args)
    old_session = web_base.SessionLocal
    old_user = os.environ.get("WEB_USERNAME")
    old_password = os.environ.get("WEB_PASSWORD")
    web_base.SessionLocal = RuntimeSession
    os.environ["WEB_USERNAME"] = candidate.username
    os.environ["WEB_PASSWORD"] = candidate.password
    try:
        gate_args = SimpleNamespace(
            device_id=args.device_id, model=args.model, host=args.host,
            port=args.port, username=args.username, platform_id=args.platform_id,
            password_env=f"LOCAL_SECRET:{args.host}:{args.port}",
            web_base_url=args.web_base_url,
            web_username_env="WEB_USERNAME", web_password_env="WEB_PASSWORD",
            web_insecure_tls=args.web_insecure_tls, web_profile=args.web_profile,
            target_number=args.target_number, dial_alias=args.dial_alias,
            registration_timeout=args.registration_timeout,
            worker_id=args.worker_id, profile_root=str(ROOT / "profiles"),
            pbx_profile=str(ROOT / "profiles/pbx/fusionpbx_srv_v1.json"),
            output_root=str(output_root), allow_live_mutation=True,
        )
        os.environ["REAL_LIVE_MUTATION"] = "EXPLICIT_ONLY"
        rc, payload = await combined._run_combined(gate_args)
        payload["credential_resolution"] = evidence
        payload["srv_direct_runtime_state"] = True
        payload["secret_values_emitted"] = False
        return rc, payload
    finally:
        web_base.SessionLocal = old_session
        if old_user is None:
            os.environ.pop("WEB_USERNAME", None)
        else:
            os.environ["WEB_USERNAME"] = old_user
        if old_password is None:
            os.environ.pop("WEB_PASSWORD", None)
        else:
            os.environ["WEB_PASSWORD"] = old_password


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="srv-direct G-PBX-002")
    parser.add_argument("--device-id", default="c132f336-df1b-4285-b175-01d848f23b5d")
    parser.add_argument("--model", default="APF3260-M")
    parser.add_argument("--host", default="10.48.8.74")
    parser.add_argument("--port", type=int, default=10002)
    parser.add_argument("--username", default="root")
    parser.add_argument("--platform-id", default="mt7981")
    parser.add_argument("--web-base-url", default="https://10.48.8.74:10003")
    parser.add_argument("--web-insecure-tls", action="store_true", default=True)
    parser.add_argument("--web-profile", default="apf3260m_reyeeos_2_421_voip_v1.yaml")
    parser.add_argument("--target-number", default="7900.a")
    parser.add_argument("--dial-alias", default="7900")
    parser.add_argument("--registration-timeout", type=float, default=60.0)
    parser.add_argument("--worker-id", default="srv-direct-g-pbx-002")
    parser.add_argument("--secret-file", default="/home/dev/secret.yaml")
    parser.add_argument("--output-root", default="validation/pbx/g-pbx-002-srv-direct")
    return parser


def main() -> int:
    args = _parser().parse_args()
    rc, payload = asyncio.run(_run(args))
    output = Path(args.output_root) / "golden-pbx-register-002.json"
    output.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
